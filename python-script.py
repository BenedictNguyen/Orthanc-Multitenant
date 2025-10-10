import json
import logging
import os
import pprint
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import orthanc  # type: ignore
import requests
from pydicom import dcmread
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

# Global scheduler to hold all tasks
scheduler = {}
pacs_destination_map = {}
pacs_aet_map = {}
stop_flag = threading.Event()

# Thread pool for async operations - Increased from 4 to 8 workers
thread_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="EOrderUpdate")
active_tasks = 0
task_lock = threading.Lock()

# --- BẮT ĐẦU CODE MỚI ---

# Cấu hình cho việc gom gói và gửi ảnh
FORWARDING_PACK_SIZE = 50  # Số lượng instance mỗi gói để gửi đi
DATACENTER_PEER = 'datacenter' # Tên peer của datacenter trong cấu hình Orthanc

# Biến toàn cục để theo dõi các instance của series đang hoạt động
# Dùng để đếm và gom gói instance trước khi gửi
series_instance_tracker = {}
series_tracker_lock = threading.Lock()

# --- KẾT THÚC CODE MỚI ---


# Performance optimization: Instance batching and caching
instance_batch_queue = {}  # Queue instances for batch processing
instance_batch_lock = threading.Lock()
batch_timeout = 10  # seconds to wait before processing batch
batch_size_limit = 50  # maximum instances in a batch
batches_processed_count = 0  # Counter for total batches processed

# Study modality cache
study_modality_cache = {}
study_modality_cache_lock = threading.Lock()

# Store retry configuration
store_retry_attempts = 3
store_retry_delay = 5  # seconds between retries
network_error_patterns = [
    "DicomAssociation - C-STORE",
    "DIMSE No data available",
    "timeout in non-blocking mode",
    "Connection refused",
    "Connection timeout",
    "Network is unreachable",
]

# Job failure monitoring
jobs_failed_count = 0  # Counter for total failed jobs
jobs_retried_count = 0  # Counter for total jobs that were retried

DEFAULT_PACS_DESTINATION = "cloud-pacs"
DEFAULT_PACS_AET = "01RIS"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("OrthancrCronjob")


class ScheduledTask:
    """Class representing a scheduled task with execution interval"""

    def __init__(self, name, interval_seconds, callback, args=None):
        """
        Initialize a scheduled task

        Parameters:
        - name: unique identifier for the task
        - interval_seconds: time in seconds between executions
        - callback: function to call when task is triggered
        - args: arguments to pass to the callback function
        """
        self.name = name
        self.interval = interval_seconds
        self.callback = callback
        self.args = args or []
        self.last_run = None
        self.is_running = False
        self.thread = None

    def start(self):
        """Start the scheduled task in a separate thread"""
        if not self.is_running:
            self.is_running = True
            self.thread = threading.Thread(target=self._run)
            self.thread.daemon = True
            self.thread.start()
            logger.info(
                f"Task '{self.name}' started with interval {self.interval} seconds"
            )

    def stop(self):
        """Stop the scheduled task"""
        if self.is_running:
            self.is_running = False
            if self.thread:
                self.thread.join(timeout=1)
            logger.info(f"Task '{self.name}' stopped")

    def _run(self):
        """Run the task periodically"""
        while self.is_running and not stop_flag.is_set():
            try:
                now = datetime.now()
                if (
                    self.last_run is None
                    or (now - self.last_run).total_seconds() >= self.interval
                ):
                    logger.info(f"Executing task '{self.name}'")
                    self.callback(*self.args)
                    self.last_run = now
                time.sleep(1)  # Check every second
            except Exception as e:
                logger.error(f"Error in task '{self.name}': {str(e)}")
                time.sleep(5)


def delete_old_studies():
    """Periodically delete old studies to free up space."""
    ORTHANC_URL = 'http://localhost:8042'
    # Giữ lại các study trong 1 ngày, các study cũ hơn sẽ bị xóa.
    # Đặt giá trị 0 để xóa tất cả các study của ngày hôm trước.
    DAYS_TO_KEEP = 1 

    logger.info("Running scheduled task: delete_old_studies")
    try:
        cutoff_date = datetime.now().date() - timedelta(days=DAYS_TO_KEEP)
        
        studies_resp = orthanc.RestApiGet('/studies')
        studies = parse_list_orthanc_string(studies_resp)
        
        deleted_count = 0
        for study_id in studies:
            try:
                study_details_resp = orthanc.RestApiGet(f'/studies/{study_id}')
                study_details = parse_orthanc_json(study_details_resp)
                study_date_str = study_details.get("MainDicomTags", {}).get("StudyDate")

                if not study_date_str:
                    continue

                study_date = datetime.strptime(study_date_str, "%Y%m%d").date()
                if study_date < cutoff_date:
                    orthanc.RestApiDelete(f'/studies/{study_id}')
                    logger.info(f"Deleted old study {study_id} from date {study_date_str}")
                    deleted_count += 1
            except Exception as e:
                logger.warning(f"Could not process or delete study {study_id}: {str(e)}")
        
        logger.info(f"Finished delete_old_studies task. Deleted {deleted_count} studies.")
        orthanc.LogWarning(f"Cleanup task finished. Deleted {deleted_count} old studies.")

    except Exception as e:
        logger.error(f"Error in 'delete_old_studies' task: {str(e)}")
        orthanc.LogWarning(f"Error in 'delete_old_studies' task: {str(e)}")


def convert_dicom_datetime(iso_date_string, timezone_offset=7):
    """
    Extract both date and time from ISO 8601 date string and return as a tuple.

    Args:
        iso_date_string (str): ISO 8601 formatted date string (e.g., '2025-05-06T02:02:34.000Z')
        timezone_offset (int): Hours offset from UTC (default: 7)

    Returns:
        tuple: (date_string, time_string) where:
            - date_string is in 'YYYYMMDD' format
            - time_string is in 'HHMMSS' format
            Both adjusted for the specified timezone
    """
    if iso_date_string is "" or iso_date_string is None:
        return ("", "")
    # Parse the ISO 8601 date string to UTC time
    dt = datetime.fromisoformat(iso_date_string.replace("Z", "+00:00"))

    # Adjust to the specified timezone (UTC+7 by default)
    dt = dt + timedelta(hours=timezone_offset)

    # Format and return as tuple
    date_string = dt.strftime("%Y%m%d")
    time_string = dt.strftime("%H%M%S")

    return (date_string, time_string)


def convert_dicom_gender(gender: str) -> str:
    if gender == "MALE":
        return "M"
    if gender == "FEMALE":
        return "F"
    return "O"


def auto_create_worklist():
    """Create worklist automatically"""
    try:
        logger.info("Running auto create worklist task")
        # orthanc.LogWarning("Auto create worklist task started")
        orders = get_eorders()
        if orders:
            for order in orders:
                order_status = order.get("status", "")
                logger.info(f"Order {order['id']} status: {order_status}")
                if order_status == "SCHEDULED":
                    logger.info(f"Order {order['id']} is scheduled")
                    # Generate a new study instance UID
                    study_instance_uid = orthanc.RestApiGet(
                        "/tools/generate-uid?level=study"
                    ).decode("utf-8")
                    logger.info(f"Study Instance UID: {study_instance_uid}")
                    logger.info(f"Order: {order}")
                    patient_id = order.get("patient", {}).get("id", "").split(".")[-1]
                    logger.info(f"Patient ID: {patient_id}")
                    patient_name = order.get("patient", {}).get("fullName", "")
                    logger.info(f"Patient Name: {patient_name}")
                    patient_sex = order.get("patient", {}).get("gender", "")
                    logger.info(f"Patient Sex: {patient_sex}")
                    patient_dob = order.get("patient", {}).get("dob", "")
                    (patient_dob_dicom, _) = convert_dicom_datetime(patient_dob)
                    logger.info(f"Patient DOB: {patient_dob_dicom}")
                    patient_addess = order.get("patient", {}).get("address", "")
                    logger.info(f"Patient Address: {patient_addess}")
                    institution_name = order.get("clinic", {}).get("clinicName", "")
                    logger.info(f"Instution Name: {institution_name}")
                    accession_number = order.get("accessionNumber", "")
                    logger.info(f"Acc No: {accession_number}")
                    referring_physician = order.get("referralPhysician", "")
                    logger.info(f"Referring Physician: {referring_physician}")
                    study_description = order.get("procedure", {}).get("name", "")
                    logger.info(f"Study Description: {study_description}")
                    modality = (
                        order.get("modality", {})
                        .get("masterModality", {})
                        .get("customId", "")
                    )
                    logger.info(f"Modality: {modality}")

                    device = order.get("device", None)
                    device_aet = f"{modality}1"
                    if device:
                        device_aet = device.get("AETitle", "")
                        logger.info(f"Scheduled Stattion AETitle: {device_aet}")

                    logger.info(f"Study Description: {study_description}")
                    scheduled_date = order.get("scheduledDate", "")
                    (scheduled_start_date, scheduled_start_time) = (
                        convert_dicom_datetime(scheduled_date)
                    )
                    logger.info(f"Scheduled Date: {scheduled_start_date}")
                    logger.info(f"Scheduled Time: {scheduled_start_time}")

                    payload = {
                        "PatientID": patient_id,
                        "PatientName": patient_name,
                        "PatientSex": convert_dicom_gender(patient_sex),
                        "PatientBirthDate": patient_dob_dicom,
                        "PatientAddress": patient_addess,
                        "InstitutionName": institution_name,
                        "InstitutionalDepartmentName": institution_name,
                        "AccessionNumber": accession_number,
                        "RequestingPhysician": "",
                        "ReferringPhysicianName": referring_physician,
                        "StudyDescription": study_description,
                        "RequestedProcedureDescription": study_description,
                        "ReasonForTheRequestedProcedure": study_description,
                        "Modality": modality,
                        "ScheduledStationAETitle": device_aet,
                        "ScheduledProcedureStepStartDate": scheduled_start_date,
                        "ScheduledProcedureStepStartTime": scheduled_start_time,
                        "ScheduledProcedureStepStatus": "SCHEDULED",
                    }
                    save_worklist(payload=payload, suid=study_instance_uid)
                if order_status == "NEED_RESEND":
                    logger.info(f"Order {order['id']} need resend")
                    order_study_uid = order.get("studyInstanceUID", "")
                    order_accession_number = order.get("accessionNumber", "")
                    if order_study_uid == "":
                        orthanc.LogWarning(
                            f"EOrder {order['id']} has no study instance uid"
                        )
                        continue
                    resend_study(
                        study_uid=order_study_uid,
                        accession_number=order_accession_number,
                    )
                    logger.info(
                        f"Order {order['id']} resend study {order_study_uid} successfully"
                    )
                    orthanc.LogWarning(
                        f"Order {order['id']} resend study {order_study_uid} successfully"
                    )
                if order_status == "CANCELLED":
                    logger.info(f"Order {order['id']} need delete worklist")
                    order_accession_number = order.get("accessionNumber", "")
                    delete_worklist(
                        accession_number=order_accession_number,
                    )
                    logger.info(f"Order {order['id']} worklist deleted successfully")
        # orthanc.LogWarning("Auto create worklist task completed")
    except Exception as e:
        logger.error(f"Error in 'auto_create_worklist': {str(e)}")
        orthanc.LogWarning(f"Error in 'auto_create_worklist': {str(e)}")


def get_instance_by_id(id: str) -> dict:
    try:
        instance = orthanc.RestApiGet(
            f"/instances/{id}?requestedTags=StudyInstanceUID;AccessionNumber"
        )
        instance_data = parse_orthanc_json(instance)
        return instance_data
    except Exception as e:
        orthanc.LogWarning(f"Error in 'get_instance_by_id': {str(e)}")
        return None


def get_study_by_id(id: str) -> dict:
    try:
        study = orthanc.RestApiGet(
            f"/studies/{id}?requestedTags=ModalitiesInStudy;NumberOfStudyRelatedInstances;NumberOfStudyRelatedSeries"
        )
        study_data = parse_orthanc_json(study)
        return study_data
    except Exception as e:
        orthanc.LogWarning(f"Error in 'get_study_by_id': {str(e)}")
        return None


def resend_study(study_uid: str, accession_number: str):
    """Resend study to Telerad system"""
    try:
        logger.info(
            f"Running resend study task with study uid {study_uid} and accession number {accession_number}"
        )
        orthanc.LogWarning(
            f"Running resend study task with study uid {study_uid} and accession number {accession_number}"
        )
        study = get_study(study_uid=study_uid)
        if not study:
            logger.error(f"Study {study_uid} not found")
            orthanc.LogWarning(f"Study {study_uid} not found")
            update_eorder_status(
                accession_number=accession_number,
                status="COMPLETED",
                study_uid=study_uid,
                errors=f"Study {study_uid} not found in local gateway",
            )
            return
        study_id = study.get("ID", "")
        modality = get_study_modality(study_id)
        pacs_destination = get_pacs_destination(modality)
        pacs_aet = get_pacs_aet(modality)
        logger.info(f"Pacs destination: {pacs_destination}")
        job = orthanc.RestApiPost(
            f"/modalities/cloud-pacs/store",
            '{"Asynchronous": true,"Compress": false,"Permissive": false,"Priority": 0,"Resources": ["'
            + study_id
            + '"],"Synchronous": false, "StorageCommitment": false}',
        )
        job_data = parse_orthanc_json(job)
        logger.info(f"Job data: {job_data}")
        job_id = job_data.get("ID", "")
        logger.info(f"Job ID: {job_id}")
        update_eorder_status_async(
            accession_number=accession_number,
            status="COMPLETED",
            study_uid=study_uid,
            job_id=job_id,
            storage_aet=pacs_aet,
        )
        logger.info(f"Resend study {study_uid} started with job {job_id}")
        orthanc.LogWarning(f"Resend study {study_uid} started with job {job_id}")
    except Exception as e:
        logger.error(f"Error in 'resend_study': {str(e)}", exc_info=True)


def monitor_thread_pool():
    """Monitor and log thread pool statistics"""
    try:
        status = get_thread_pool_status()
        logger.info(f"Thread pool status: {status}")

        if status["active_tasks"] > 0:
            orthanc.LogWarning(
                f"Thread pool: {status['active_tasks']} active tasks, {status['max_workers']} max workers"
            )

        # Log warning if thread pool is getting full
        if status["active_tasks"] >= status["max_workers"]:
            orthanc.LogWarning(
                f"Thread pool is at capacity: {status['active_tasks']}/{status['max_workers']} tasks"
            )

    except Exception as e:
        logger.error(f"Error monitoring thread pool: {str(e)}")


def onOrthancStarted():
    """Called when Orthanc server has started and plugin is loaded"""
    global scheduler
    logger.info("Orthanc Python Cronjob Plugin started")

    auto_create_worklist_task = ScheduledTask(
        "auto_create_worklist", 5, auto_create_worklist
    )
    scheduler["auto_create_worklist_task"] = auto_create_worklist_task
    auto_create_worklist_task.start()

    # Add thread pool monitoring task
    thread_pool_monitor_task = ScheduledTask(
        "thread_pool_monitor", 30, monitor_thread_pool  # Check every 30 seconds
    )
    scheduler["thread_pool_monitor_task"] = thread_pool_monitor_task
    thread_pool_monitor_task.start()

    # Add instance batch cleanup task
    instance_batch_cleanup_task = ScheduledTask(
        "instance_batch_cleanup",
        batch_timeout,
        cleanup_instance_batch_periodic,
    )
    scheduler["instance_batch_cleanup_task"] = instance_batch_cleanup_task
    instance_batch_cleanup_task.start()

    # Add batch performance monitoring task
    #batch_performance_monitor_task = ScheduledTask(
    #    "batch_performance_monitor", 60, monitor_batch_performance
    #)
    #scheduler["batch_performance_monitor_task"] = batch_performance_monitor_task
    #batch_performance_monitor_task.start()
    
    # Add the new cleanup task
    cleanup_studies_task = ScheduledTask(
        "delete_old_studies", 86400, delete_old_studies # Run every 24 hours
    )
    scheduler["cleanup_studies_task"] = cleanup_studies_task
    cleanup_studies_task.start()

    logger.info("All scheduled tasks have been started")


def onOrthancStopped():
    """Called when Orthanc server is shutting down"""
    global scheduler, stop_flag, thread_pool
    logger.info("Orthanc Python Cronjob Plugin stopping")

    # Signal all threads to stop
    stop_flag.set()

    # Stop all scheduled tasks
    for task_name, task in scheduler.items():
        logger.info(f"Stopping task '{task_name}'")
        task.stop()

    # Shutdown thread pool gracefully
    logger.info("Shutting down thread pool...")
    thread_pool.shutdown(wait=True, timeout=30)
    logger.info("Thread pool shutdown completed")

    logger.info("All scheduled tasks have been stopped")


def get_thread_pool_status():
    """Get current thread pool status for monitoring"""
    global active_tasks, thread_pool
    with task_lock:
        return {
            "active_tasks": active_tasks,
            "max_workers": thread_pool._max_workers,
            "thread_pool_shutdown": thread_pool._shutdown,
        }


def get_eorders() -> list[dict]:
    url = os.getenv("TELERAD_URL")
    api_key = os.getenv("TELERAD_API_KEY")  # rõ ràng, không lẫn với SECRET_KEY
    logger.info(f"Get EOrder from Telerad System: {url}")
    try:
        start = datetime.now() - timedelta(hours=12)
        end = datetime.now() + timedelta(hours=12)
        filters = [
            "filter=status||$in||SCHEDULED,NEED_RESEND,CANCELLED",
            "sort=updatedDate||DESC",
            "page=1",
            "limit=1000",
        ]
        headers = {
            "Content-Type": "application/json",
            "x-api-key": api_key,
        }
        response = requests.get(
            f"{url}/e-orders/clinic-e-orders/?{'&'.join(filters)}",
            headers=headers,
            timeout=10,
        )
        response.raise_for_status()
        return response.json().get("data", [])
    except requests.exceptions.RequestException as req_err:
        orthanc.LogWarning(f"HTTP request failed: {req_err}")
    except Exception as e:
        orthanc.LogWarning(f"Unexpected error: {e}")

def get_pacs_destination(modality: str) -> str:
    global pacs_destination_map, pacs_aet_map
    try:
        logger.info(f"Get pacs destination for modality: {modality}")
        logger.info(f"Pacs destination map before: {pacs_destination_map}")
        logger.info(f"Pacs aet map before: {pacs_aet_map}")
        if modality in pacs_destination_map:
            logger.info(
                f"Read from cache pacs destination: {pacs_destination_map[modality]}"
            )
            return pacs_destination_map[modality]
        modalities = orthanc.RestApiGet("/modalities?expand=true")
        modalities_data = parse_orthanc_json(modalities)

        if modality in modalities_data:
            pacs_destination_map[modality] = modality
            modality_config = modalities_data[modality]
            pacs_aet_map[modality] = modality_config.get("AET")
            logger.info(
                f"Set pacs destination map 'get_pacs_destination': {pacs_destination_map}"
            )
            logger.info(f"Set pacs aet map 'get_pacs_destination': {pacs_aet_map}")
            return modality

        pacs_destination_map[modality] = DEFAULT_PACS_DESTINATION
        pacs_aet_map[modality] = modalities_data.get(DEFAULT_PACS_DESTINATION, {}).get(
            "AET", "01RIS"
        )
        logger.info(
            f"Set pacs destination map 'get_pacs_destination': {pacs_destination_map}"
        )
        logger.info(f"Set pacs aet map 'get_pacs_destination': {pacs_aet_map}")
        return DEFAULT_PACS_DESTINATION
    except Exception as err:
        orthanc.LogWarning(f"Orthanc request failed: {repr(err)}")


def get_instance_modality(instance_id: str) -> str:
    """Get instance modality (no caching needed since instances are processed once)"""
    try:
        instance = orthanc.RestApiGet(
            f"/instances/{instance_id}?requestedTags=Modality"
        )
        instance_data = parse_orthanc_json(instance)
        modality = instance_data.get("RequestedTags", {}).get("Modality", "")

        logger.info(f"API call for instance {instance_id} modality: {modality}")
        return modality
    except Exception as err:
        orthanc.LogWarning(f"Orthanc request failed: {repr(err)}")
        return ""


def get_study_modality(study_id: str) -> str:
    """Get study modality (no caching - keep it simple)"""
    try:
        study = orthanc.RestApiGet(
            f"/studies/{study_id}?requestedTags=ModalitiesInStudy"
        )
        study_data = parse_orthanc_json(study)
        # modalities_in_study = "SR\\US" or "US//SR" or "US" -> "US"
        modality = study_data.get("RequestedTags", {}).get("ModalitiesInStudy", "")
        if "\\" in modality:
            modalities_in_study = modality.split("\\")
            modalities_in_study = sorted(modalities_in_study)
            modality = "_".join(modalities_in_study)

        logger.info(f"API call for study {study_id} modality: {modality}")
        return modality
    except Exception as err:
        orthanc.LogWarning(f"Orthanc request failed: {repr(err)}")
        return ""


def get_pacs_aet(modality: str) -> str:
    global pacs_aet_map, pacs_destination_map
    try:
        logger.info(f"Get pacs aet for modality: {modality}")
        logger.info(f"Pacs aet map before: {pacs_aet_map}")
        if modality in pacs_aet_map:
            logger.info(f"Read from cache pacs aet: {pacs_aet_map[modality]}")
            return pacs_aet_map[modality]
        modalities = orthanc.RestApiGet("/modalities?expand=true")
        modalities_data = parse_orthanc_json(modalities)
        if modality in modalities_data:
            modality_config = modalities_data[modality]
            aet = modality_config.get("AET")
            pacs_aet_map[modality] = aet
            logger.info(f"Set pacs aet map 'get_pacs_aet': {pacs_aet_map}")
            return aet

        aet = modalities_data.get(DEFAULT_PACS_DESTINATION, {}).get(
            "AET", DEFAULT_PACS_AET
        )
        pacs_aet_map[modality] = aet
        logger.info(f"Set pacs aet map 'get_pacs_aet': {pacs_aet_map}")
        return aet
    except Exception as err:
        orthanc.LogWarning(f"Orthanc request failed: {repr(err)}")


def get_study_modality_from_study_data(study_data: dict) -> str:
    try:
        study_requested_tags = study_data.get("RequestedTags", {})
        modality = study_requested_tags.get("ModalitiesInStudy", "")
        if "\\" in modality:
            modalities_in_study = modality.split("\\")
            modalities_in_study = sorted(modalities_in_study)
            return "_".join(modalities_in_study)
        return modality
    except Exception as err:
        orthanc.LogWarning(f"Orthanc request failed: {repr(err)}")


def get_study(study_uid: str) -> dict:
    try:
        studies = orthanc.RestApiPost(
            f"/tools/find",
            '{"Level": "Study", "Limit": 1, "Query": {"StudyInstanceUID": "'
            + study_uid
            + '"}, "RequestedTags": ["ModalitiesInStudy", "Manufacturer", "StationName", "SeriesNumber"], "Expand": true}',
        )
        studies_data = parse_list_orthanc_json(studies)
        if len(studies_data) is 0:
            return None
        orthanc.LogWarning(f"Study data: {studies_data}")
        return studies_data[0]
    except Exception as err:
        orthanc.LogWarning(f"Orthanc request failed: {repr(err)}")


def delete_worklist(accession_number: str):
    worklist_database = os.getenv("WORKLIST_DATABASE")
    if not accession_number:
        return
    work_item_path = os.path.join(
        worklist_database, f"{no_special_characters(accession_number)}.wl"
    )
    if not os.path.exists(work_item_path):
        return
    work_item = dcmread(work_item_path)
    if accession_number == work_item.AccessionNumber:
        os.remove(work_item_path)


def update_telerad_eorders(
    accession_number: str,
    study_uid: str,
    storage_aet: str = None,
    status: str = None,
    count_series: int = 0,
    count_instances: int = 0,
    job_id: str = "",
    errors: str = "",
    extras: dict = None,
) -> dict:
    url = os.getenv("TELERAD_URL")
    api_key = os.getenv("SECRET_KEY")
    orthanc.LogWarning(f"Update EOrder on Telerad System: {url}")
    try:
        payload = {
            "studyInstanceUID": study_uid,
        }
        if errors:
            payload["uploadErrors"] = errors
        if job_id:
            payload["jobId"] = job_id
        if extras:
            payload["extras"] = extras
        if accession_number:
            payload["accessionNumber"] = accession_number
        if status:
            payload["status"] = status
        if storage_aet:
            payload["storageAET"] = storage_aet
        if count_series > 0:
            payload["countSeries"] = count_series
        if count_instances > 0:
            payload["countInstances"] = count_instances
        orthanc.LogWarning(f"EOrder payload for status: {status}")
        pprint.pprint(payload)
        headers = {"Content-Type": "application/json", "x-api-key": api_key}
        response = requests.post(
            f"{url}/e-orders/update-status", headers=headers, json=payload, timeout=10
        )
        response.raise_for_status()  # Raise an exception for non-2xx responses
        json_response = response.json()
        # Check for an error in the response
        if json_response.get("error"):
            error_message = json_response.get("error", "Unknown error")
            raise ValueError(f"Error from server: {error_message}")
    except requests.exceptions.RequestException as req_err:
        orthanc.LogWarning(f"HTTP request failed: {req_err}")
    except Exception as e:
        orthanc.LogWarning(f"An unexpected error occurred: {e}")


def get_file_extension(filename):
    _, extension = os.path.splitext(filename)
    return extension[1:]


def no_accent(s: str):
    if s == "":
        return ""
    s = re.sub(r"[àáạảãâầấậẩẫăằắặẳẵ]", "a", s)
    s = re.sub(r"[ÀÁẠẢÃĂẰẮẶẲẴÂẦẤẬẨẪ]", "A", s)
    s = re.sub(r"[èéẹẻẽêềếệểễ]", "e", s)
    s = re.sub(r"[ÈÉẸẺẼÊỀẾỆỂỄ]", "E", s)
    s = re.sub(r"[òóọỏõôồốộổỗơờớợởỡ]", "o", s)
    s = re.sub(r"[ÒÓỌỎÕÔỒỐỘỔỖƠỜỚỢỞỠ]", "O", s)
    s = re.sub(r"[ìíịỉĩ]", "i", s)
    s = re.sub(r"[ÌÍỊỈĨ]", "I", s)
    s = re.sub(r"[ùúụủũưừứựửữ]", "u", s)
    s = re.sub(r"[ƯỪỨỰỬỮÙÚỤỦŨ]", "U", s)
    s = re.sub(r"[ỳýỵỷỹ]", "y", s)
    s = re.sub(r"[ỲÝỴỶỸ]", "Y", s)
    s = re.sub(r"[Đ]", "D", s)
    s = re.sub(r"[đ]", "d", s)

    if len(s) > 50:
        s = f"{s[:50]} ..."

    return s


def no_special_characters(s: str):
    if s is None:
        return ""
    s = re.sub(r"[!@#$%^&*()_+={}\[\]:;\"'<>?,./\\|`~]", "", s)
    return s


def is_network_error(error_message: str) -> bool:
    """Check if error is a network-related error that should be ignored/retried"""
    error_lower = error_message.lower()
    for pattern in network_error_patterns:
        if pattern.lower() in error_lower:
            return True
    return False


def retry_store_operation(job_id: str, max_attempts: int = 3) -> tuple[bool, str, str]:
    """
    Retry store operation by resubmitting the existing failed job

    Returns:
        tuple: (success, job_id, error_message)
    """
    global jobs_failed_count, jobs_retried_count
    jobs_failed_count += 1
    jobs_retried_count += 1
    for attempt in range(max_attempts):
        try:
            # Resubmit the existing job
            orthanc.RestApiPost(f"/jobs/{job_id}/resubmit", "")

            logger.info(
                f"Retry attempt {attempt + 1}/{max_attempts} successful for job {job_id}"
            )
            orthanc.LogWarning(
                f"Retry attempt {attempt + 1}/{max_attempts} successful for job {job_id}"
            )

            return True, job_id, ""

        except Exception as e:
            error_msg = str(e)
            logger.warning(
                f"Retry attempt {attempt + 1}/{max_attempts} failed for job {job_id}: {error_msg}"
            )
            orthanc.LogWarning(
                f"Retry attempt {attempt + 1}/{max_attempts} failed for job {job_id}: {error_msg}"
            )

            if attempt < max_attempts - 1:
                time.sleep(store_retry_delay)

    return False, job_id, f"All {max_attempts} retry attempts failed"


def parse_request_payload(req_body):
    try:
        string_data = req_body.decode("utf-8")
        payload = json.loads(string_data)
        return payload

    except UnicodeDecodeError as e:
        raise UnicodeDecodeError(f"Failed to decode binary data as UTF-8: {str(e)}")
    except json.JSONDecodeError as e:
        raise json.JSONDecodeError(
            f"Failed to parse data as JSON: {str(e)}", e.doc, e.pos
        )


def parse_orthanc_json(orthanc_data: bytes) -> dict:
    try:
        string_data = orthanc_data.decode("utf-8")
        payload = json.loads(string_data)
        return payload

    except UnicodeDecodeError as e:
        raise UnicodeDecodeError(f"Failed to decode binary data as UTF-8: {str(e)}")
    except json.JSONDecodeError as e:
        raise json.JSONDecodeError(
            f"Failed to parse data as JSON: {str(e)}", e.doc, e.pos
        )


def parse_list_orthanc_json(orthanc_data: bytes) -> list[dict]:
    try:
        string_data = orthanc_data.decode("utf-8")
        payload = json.loads(string_data)
        return payload

    except UnicodeDecodeError as e:
        raise UnicodeDecodeError(f"Failed to decode binary data as UTF-8: {str(e)}")
    except json.JSONDecodeError as e:
        raise json.JSONDecodeError(
            f"Failed to parse data as JSON: {str(e)}", e.doc, e.pos
        )


def parse_list_orthanc_string(orthanc_data: bytes) -> list[str]:
    try:
        string_data = orthanc_data.decode("utf-8")
        payload = json.loads(string_data)
        return payload
    except UnicodeDecodeError as e:
        raise UnicodeDecodeError(f"Failed to decode binary data as UTF-8: {str(e)}")
    except json.JSONDecodeError as e:
        raise json.JSONDecodeError(
            f"Failed to parse data as JSON: {str(e)}", e.doc, e.pos
        )


def validate_delete_worklist(payload):
    required_tags = ["AccessionNumber"]

    missing_fields = [
        tag for tag in required_tags if tag not in payload or tag == "" or tag == None
    ]
    is_valid = len(missing_fields) == 0
    return is_valid, missing_fields


def validate_resend_dicom(payload):
    required_tags = ["StudyInstanceUID"]

    missing_fields = [
        tag for tag in required_tags if tag not in payload or tag == "" or tag == None
    ]
    is_valid = len(missing_fields) == 0
    return is_valid, missing_fields


def validate_create_worklist(payload):
    required_tags = [
        "PatientID",
        "PatientName",
        "PatientSex",
        "PatientBirthDate",
        "PatientAddress",
        "InstitutionName",
        "InstitutionalDepartmentName",
        "AccessionNumber",
        "RequestingPhysician",
        "ReferringPhysicianName",
        "StudyDescription",
        "RequestedProcedureDescription",
        "ReasonForTheRequestedProcedure",
        "Modality",
        "ScheduledStationAETitle",
        "ScheduledProcedureStepStartDate",
        "ScheduledProcedureStepStartTime",
        "ScheduledProcedureStepStatus",
    ]

    missing_fields = [
        tag for tag in required_tags if tag not in payload or tag == "" or tag == None
    ]
    is_valid = len(missing_fields) == 0
    return is_valid, missing_fields


def save_worklist(payload: dict, suid: str):
    ds = Dataset()

    # FileMeta Information
    ds.file_meta = FileMetaDataset()
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.file_meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.31"
    ds.file_meta.MediaStorageSOPInstanceUID = generate_uid()

    ds.SOPClassUID = ds.file_meta.MediaStorageSOPClassUID
    ds.SOPInstanceUID = ds.file_meta.MediaStorageSOPInstanceUID

    ds.SpecificCharacterSet = "ISO_IR 100"

    # Patient Demographic
    ds.PatientID = payload["PatientID"]
    ds.PatientName = no_accent(payload["PatientName"])
    ds.PatientSex = payload["PatientSex"]
    ds.PatientBirthDate = payload["PatientBirthDate"]
    # ds.PatientAddress = no_accent(payload["PatientAddress"])

    # Institution
    ds.InstitutionName = no_accent(payload["InstitutionName"])
    ds.InstitutionalDepartmentName = no_accent(payload["InstitutionalDepartmentName"])

    # Request Info
    ds.AccessionNumber = payload["AccessionNumber"]
    ds.RequestingPhysician = no_accent(payload["RequestingPhysician"])
    ds.ReferringPhysicianName = no_accent(payload["ReferringPhysicianName"])

    # Procedure Info
    ds.StudyInstanceUID = suid
    ds.StudyDescription = no_accent(payload["StudyDescription"])
    ds.RequestedProcedureDescription = no_accent(
        payload["RequestedProcedureDescription"]
    )
    ds.ReasonForTheRequestedProcedure = no_accent(
        payload["ReasonForTheRequestedProcedure"]
    )
    ds.RequestedProcedureID = payload.get(
        "RequestedProcedureID", f"RP{payload.get('AccessionNumber', '')}"
    )  # Ensure present

    # Scheduled Procedure Step Sequence
    step_sequence_ds = Dataset()
    step_sequence_ds.Modality = payload["Modality"]
    step_sequence_ds.ScheduledStationAETitle = payload["ScheduledStationAETitle"]
    step_sequence_ds.ScheduledProcedureStepStartDate = payload[
        "ScheduledProcedureStepStartDate"
    ]
    step_sequence_ds.ScheduledProcedureStepStartTime = payload[
        "ScheduledProcedureStepStartTime"
    ]
    step_sequence_ds.ScheduledProcedureStepStatus = payload[
        "ScheduledProcedureStepStatus"
    ]
    step_sequence_ds.ScheduledProcedureStepID = payload.get(
        "ScheduledProcedureStepID", f"STP{payload.get('AccessionNumber', '')}"
    )
    step_sequence_ds.ScheduledProcedureStepDescription = no_accent(
        payload["RequestedProcedureDescription"]
    )
    step_sequence_ds.ScheduledPerformingPhysicianName = no_accent(
        payload.get("RequestingPhysician", "Unknown Performing Physician")
    )
    ds.ScheduledProcedureStepSequence = [step_sequence_ds]

    # Save to .wl file
    save_folder = os.getenv("WORKLIST_DATABASE")
    os.makedirs(save_folder, exist_ok=True)
    logger.info(
        f"Saving worklist to {no_special_characters(payload['AccessionNumber'])}"
    )
    #   orthanc.LogWarning(
    #       f"Saving worklist to {no_special_characters(payload['AccessionNumber'])}"
    #   )
    file_name = f"{save_folder}/{no_special_characters(payload['AccessionNumber'])}.wl"
    ds.save_as(file_name, write_like_original=False)


def update_eorder_status(
    accession_number: str,
    study_uid: str,
    storage_aet: str = None,
    status: str = None,
    count_series: int = 0,
    count_instances: int = 0,
    job_id: str = "",
    errors: str = "",
    extras: dict = None,
):
    logger.info(
        f"Update EOrder status: {status}, study uid: {study_uid}, accession number: {accession_number}"
    )
    delete_worklist(accession_number)
    update_telerad_eorders(
        accession_number=accession_number,
        status=status,
        study_uid=study_uid,
        storage_aet=storage_aet,
        count_series=count_series,
        count_instances=count_instances,
        job_id=job_id,
        errors=errors,
        extras=extras,
    )


def update_eorder_status_async(
    accession_number: str,
    study_uid: str,
    storage_aet: str = None,
    status: str = None,
    count_series: int = 0,
    count_instances: int = 0,
    job_id: str = "",
    errors: str = "",
    extras: dict = None,
    max_retries: int = 3,
):
    """
    Async version of update_eorder_status that uses a thread pool.
    This prevents Orthanc from becoming unresponsive during API calls.
    Maximum of 4 concurrent threads for API updates.
    """
    global active_tasks, thread_pool

    def _update_in_background():
        """This function runs in a thread pool worker"""
        global active_tasks
        thread_id = threading.current_thread().ident
        thread_name = threading.current_thread().name

        try:
            with task_lock:
                active_tasks += 1

            start_time = time.time()
            orthanc.LogWarning(
                f"Thread {thread_name} ({thread_id}): Starting async update for study {study_uid} status: {status}"
            )

            # Retry logic for API calls
            for attempt in range(max_retries):
                try:
                    # delete worklist first
                    delete_worklist(accession_number)
                    # Call the original function
                    update_telerad_eorders(
                        accession_number=accession_number,
                        status=status,
                        study_uid=study_uid,
                        storage_aet=storage_aet,
                        count_series=count_series,
                        count_instances=count_instances,
                        job_id=job_id,
                        errors=errors,
                        extras=extras,
                    )

                    # Success - break out of retry loop
                    end_time = time.time()
                    duration = end_time - start_time
                    orthanc.LogWarning(
                        f"Thread {thread_name} ({thread_id}): Async update with status {status} completed in {duration:.2f} seconds for study {study_uid} (attempt {attempt + 1})"
                    )
                    break

                except Exception as e:
                    if attempt < max_retries - 1:
                        wait_time = 2**attempt  # Exponential backoff
                        orthanc.LogWarning(
                            f"Thread {thread_name} ({thread_id}): Attempt {attempt + 1} failed for study {study_uid}: {str(e)}. Retrying in {wait_time} seconds..."
                        )
                        time.sleep(wait_time)
                    else:
                        # Final attempt failed
                        orthanc.LogWarning(
                            f"Thread {thread_name} ({thread_id}): All {max_retries} attempts failed for study {study_uid}: {str(e)}"
                        )

        except Exception as e:
            orthanc.LogWarning(
                f"Thread {thread_name} ({thread_id}): Unexpected error for study {study_uid}: {str(e)}"
            )
        finally:
            with task_lock:
                active_tasks -= 1
                orthanc.LogWarning(f"Active tasks: {active_tasks}")

    # Submit task to thread pool
    future = thread_pool.submit(_update_in_background)

    # Log immediately without waiting
    logger.info(
        f"Submitted async update to thread pool for EOrder status: {status}, study uid: {study_uid}, accession number: {accession_number}"
    )
    orthanc.LogWarning(f"Async update submitted to thread pool for study {study_uid}")

    # Log current thread pool status
    with task_lock:
        orthanc.LogWarning(
            f"Thread pool status - Active tasks: {active_tasks}, Max workers: 4"
        )


def add_instance_to_batch(
    instance_id: str, modality: str, pacs_destination: str, pacs_aet: str
):
    """Add an instance to the batch processing queue"""
    global instance_batch_queue, instance_batch_lock

    with instance_batch_lock:
        # Group instances by modality and destination for efficient batching
        batch_key = f"{modality}_{pacs_destination}"

        if batch_key not in instance_batch_queue:
            instance_batch_queue[batch_key] = {
                "modality": modality,
                "pacs_destination": pacs_destination,
                "pacs_aet": pacs_aet,
                "timestamp": time.time(),
                "instances": [],
            }

        # Add instance to batch
        instance_batch_queue[batch_key]["instances"].append(instance_id)

        # Check if batch is ready for processing
        total_instances = sum(
            len(batch["instances"]) for batch in instance_batch_queue.values()
        )
        oldest_timestamp = min(
            entry["timestamp"] for entry in instance_batch_queue.values()
        )
        logger.info(f"Total instances: {total_instances}")
        logger.info(f"Oldest timestamp: {oldest_timestamp}")
        logger.info(f"Batch timeout: {batch_timeout}")
        logger.info(f"Batch size limit: {batch_size_limit}")

        should_process = (
            total_instances >= batch_size_limit
            or time.time() - oldest_timestamp >= batch_timeout
        )

        if should_process:
            logger.info(f"Processing batch with {total_instances} instances")
            # Release lock before calling process_instance_batch to avoid deadlock
            process_batch = True
        else:
            process_batch = False

    # Process batch outside of lock to avoid deadlock
    if process_batch:
        process_instance_batch()


def process_instance_batch():
    """Process all instances in the batch queue"""
    global instance_batch_queue, instance_batch_lock, batches_processed_count

    with instance_batch_lock:
        if not instance_batch_queue:
            return

        # Get all batches to process
        batches_to_process = list(instance_batch_queue.items())
        instance_batch_queue.clear()  # Clear the queue

    logger.info(f"Processing {len(batches_to_process)} instance batches")

    # Increment the batch counter
    batches_processed_count += len(batches_to_process)

    # Process each batch
    for batch_key, batch_info in batches_to_process:
        try:
            instances = batch_info["instances"]
            pacs_destination = batch_info["pacs_destination"]

            # Create a single job for all instances in this batch
            resources_json = json.dumps(instances)
            job_payload = {
                "Asynchronous": True,
                "Compress": False,
                "Permissive": False,
                "Priority": 0,
                "Resources": instances,
                "Synchronous": False,
                "StorageCommitment": False,
            }

            job = orthanc.RestApiPost(
                f"/modalities/cloud-pacs/store", json.dumps(job_payload)
            )
            job_data = parse_orthanc_json(job)
            job_id = job_data.get("ID", "")

            #logger.info(
            #    f"Created batch job {job_id} for {len(instances)} instances to {pacs_destination}"
            #)
            #orthanc.LogWarning(
            #    f"Batch job {job_id} created for {len(instances)} instances"
            #)

        except Exception as e:
            logger.error(
                f"Error creating batch job for {len(batch_info['instances'])} instances: {str(e)}"
            )
            orthanc.LogWarning(
                f"Error creating batch job for {len(batch_info['instances'])} instances: {str(e)}"
            )


def cleanup_instance_batch_periodic():
    """Periodic cleanup and processing of instance batch queue"""
    try:
        global instance_batch_queue, instance_batch_lock, batches_processed_count

        with instance_batch_lock:
            current_time = time.time()
            batches_to_process = []

            for batch_key, batch_info in instance_batch_queue.items():
                age = current_time - batch_info["timestamp"]

                if (
                    age >= batch_timeout
                ):  # Timeout reached - process (including expired)
                    batches_to_process.append((batch_key, batch_info))

            # Remove all batches that will be processed from queue
            for batch_key, _ in batches_to_process:
                del instance_batch_queue[batch_key]
                logger.info(f"Removed batch {batch_key} from queue for processing")

        # Process all timed-out and expired batches outside of lock to avoid deadlock
        if batches_to_process:
            logger.info(
                f"Processing {len(batches_to_process)} batches (timed-out and expired)"
            )
            # Increment the batch counter for all processed batches
            batches_processed_count += len(batches_to_process)
            for batch_key, batch_info in batches_to_process:
                try:
                    instances = batch_info["instances"]
                    pacs_destination = batch_info["pacs_destination"]

                    job_payload = {
                        "Asynchronous": True,
                        "Compress": False,
                        "Permissive": False,
                        "Priority": 0,
                        "Resources": instances,
                        "Synchronous": False,
                        "StorageCommitment": False,
                    }

                    job = orthanc.RestApiPost(
                        f"/modalities/{pacs_destination}/store",
                        json.dumps(job_payload),
                    )
                    job_data = parse_orthanc_json(job)
                    job_id = job_data.get("ID", "")

                    #logger.info(
                    #    f"Created batch job {job_id} for {len(instances)} instances to {pacs_destination}"
                    #)
                    #orthanc.LogWarning(
                    #    f"Batch job {job_id} created for {len(instances)} instances"
                    #)

                except Exception as e:
                    logger.error(f"Error creating batch job: {str(e)}")
                    orthanc.LogWarning(f"Error creating batch job: {str(e)}")

        if batches_to_process:
            logger.info(f"Batch cleanup: {len(batches_to_process)} batches processed")

    except Exception as e:
        logger.error(f"Error in periodic instance batch cleanup: {str(e)}")


def get_cache_statistics():
    """Get cache statistics for monitoring"""
    global instance_batch_queue, batches_processed_count, jobs_failed_count, jobs_retried_count

    with instance_batch_lock:
        batch_queue_size = len(instance_batch_queue)
        total_batched_instances = sum(
            len(batch["instances"]) for batch in instance_batch_queue.values()
        )

    return {
        "instance_batch_queue_size": batch_queue_size,
        "total_batched_instances": total_batched_instances,
        "batches_processed_count": batches_processed_count,
        "jobs_failed_count": jobs_failed_count,
        "jobs_retried_count": jobs_retried_count,
    }

# --- BẮT ĐẦU CODE MỚI ---

def forward_instance_pack(instances, peer, series_uid, reason=""):
    """Gửi một gói các instance đến một peer được chỉ định."""
    if not instances:
        return

    logger.info(
        f"Forwarding pack of {len(instances)} instances for series {series_uid} to peer '{peer}'. Reason: {reason}"
    )
    orthanc.LogWarning(
        f"Forwarding pack of {len(instances)} for series {series_uid} to '{peer}'. Reason: {reason}"
    )

    try:
        payload = {
            "Resources": list(instances), # Đảm bảo là một list
            "Asynchronous": True,
            "Compress": False # Có thể đặt True để nén
        }
        orthanc.RestApiPost(f'/peers/{peer}/store', json.dumps(payload))
    except Exception as e:
        error_msg = f"Failed to forward pack for series {series_uid} to peer '{peer}': {str(e)}"
        logger.error(error_msg)
        orthanc.LogWarning(error_msg)

# --- KẾT THÚC CODE MỚI ---

def monitor_batch_performance():
    """Monitor and log batch processing statistics"""
    try:
        cache_stats = get_cache_statistics()

        if cache_stats["total_batched_instances"] > 0:
            orthanc.LogWarning(
                f"Batch stats - Instance batches: {cache_stats['instance_batch_queue_size']} "
                f"({cache_stats['total_batched_instances']} instances), "
                f"Total processed: {cache_stats['batches_processed_count']}"
            )

        # Log job failure statistics
        if cache_stats["jobs_failed_count"] > 0:
            orthanc.LogWarning(
                f"Job failure stats - Failed jobs: {cache_stats['jobs_failed_count']}, "
                f"Retried jobs: {cache_stats['jobs_retried_count']}"
            )

    except Exception as e:
        logger.error(f"Error monitoring batch performance: {str(e)}")


def OnHealthCheck(output, uri, **request):
    """Health check endpoint that includes thread pool status and cache statistics"""
    thread_pool_status = get_thread_pool_status()
    cache_stats = get_cache_statistics()

    health_data = {
        "message": "OK",
        "thread_pool": thread_pool_status,
        "cache_statistics": cache_stats,
        "timestamp": datetime.now().isoformat(),
    }

    output.AnswerBuffer(json.dumps(health_data), "application/json")


def OnCreateWorklist(output, uri, **request):
    study_instance_uid = orthanc.RestApiGet("/tools/generate-uid?level=study").decode(
        "utf-8"
    )
    if request["method"] == "PATCH":
        output.SendMethodNotAllowed("PATCH")

    if request["method"] == "POST":
        try:
            pprint.pprint(request["body"])
            payload = parse_request_payload(request["body"])
            is_valid, missing_fields = validate_create_worklist(payload)
            if not is_valid:
                output.AnswerBuffer(
                    json.dumps({"error": f"Missing required tags: {missing_fields}"}),
                    "application/json",
                )
                return
            # implement your own logic here
            save_worklist(payload, study_instance_uid)
            output.AnswerBuffer(
                json.dumps({"study_instance_uid": study_instance_uid}),
                "application/json",
            )
        except Exception as e:
            output.AnswerBuffer(json.dumps({"error": str(e)}), "application/json")

    if request["method"] == "PUT":
        try:
            payload = parse_request_payload(request["body"])
            is_valid, missing_fields = validate_create_worklist(payload)
            if not is_valid:
                output.AnswerBuffer(
                    json.dumps({"error": f"Missing required tags: {missing_fields}"}),
                    "application/json",
                )
                return
            status = payload["ScheduledProcedureStepStatus"]
            if status == "CANCELLED" or status == "COMPLETED":
                accession_number = payload.get("AccessionNumber", "")
                delete_worklist(accession_number)
                output.AnswerBuffer(
                    json.dumps({"deleted_count": 1}), "application/json"
                )
            else:
                save_worklist(payload, study_instance_uid)
                output.AnswerBuffer(
                    json.dumps({"study_instance_uid": study_instance_uid}),
                    "application/json",
                )
        except Exception as e:
            output.AnswerBuffer(json.dumps({"error": repr(e)}), "application/json")

    if request["method"] == "GET":
        work_items = []
        worklist_database = os.getenv("WORKLIST_DATABASE")
        for work_item_file in os.listdir(worklist_database):
            work_item_path = os.path.join(worklist_database, work_item_file)
            # only return wl file
            if get_file_extension(work_item_file) == "wl":
                work_item = dcmread(work_item_path, force=True)
                work_items.append(work_item.to_json_dict())
        output.AnswerBuffer(json.dumps(work_items), "application/json")


def OnResendDICOM(output, uri, **request):
    if request["method"] == "POST":
        try:
            pprint.pprint(request["body"])
            payload = parse_request_payload(request["body"])
            is_valid, missing_fields = validate_resend_dicom(payload)
            if not is_valid:
                output.AnswerBuffer(
                    json.dumps({"error": f"Missing required tags: {missing_fields}"}),
                    "application/json",
                )
                return
            study_uid = payload.get("StudyInstanceUID", "")
            study = get_study(study_uid=study_uid)
            study_id = study.get("ID", "")
            modality = get_study_modality(study_id)
            pacs_destination = get_pacs_destination(modality)
            logger.info(f"Pacs destination: {pacs_destination}")
            job = orthanc.RestApiPost(
                f"/modalities/{pacs_destination}/store",
                '{"Asynchronous": true,"Compress": true,"Permissive": false,"Priority": 0,"Resources": ["'
                + study_id
                + '"],"Synchronous": false, "StorageCommitment": false}',
            )
            job_data = parse_orthanc_json(job)
            job_id = job_data.get("ID", "")
            output.AnswerBuffer(
                json.dumps({"msg": "OK", "jobId": job_id}), "application/json"
            )
        except Exception as e:
            output.AnswerBuffer(json.dumps({"error": str(e)}), "application/json")


def OnCancelWorklist(output, uri, **request):
    if request["method"] == "POST":
        try:
            pprint.pprint(request["body"])
            payload = parse_request_payload(request["body"])
            is_valid, missing_fields = validate_delete_worklist(payload)
            if not is_valid:
                output.AnswerBuffer(
                    json.dumps({"error": f"Missing required tags: {missing_fields}"}),
                    "application/json",
                )
                return
            accession_number = payload.get("AccessionNumber", "")
            delete_worklist(accession_number)
            output.AnswerBuffer(json.dumps({"msg": "OK"}), "application/json")
        except Exception as e:
            output.AnswerBuffer(json.dumps({"error": str(e)}), "application/json")


def OnChange(changeType, level, resourceId):
    if changeType == orthanc.ChangeType.ORTHANC_STARTED:
        onOrthancStarted()
        orthanc.LogWarning("Started")

    if changeType == orthanc.ChangeType.ORTHANC_STOPPED:
        onOrthancStopped()
        orthanc.LogWarning("Stopped")

    if changeType == orthanc.ChangeType.NEW_INSTANCE:
        try:
            # Lấy thông tin cần thiết: Series UID và Tenant
            instance_tags_json = orthanc.RestApiGet(f'/instances/{resourceId}/simplified-tags')
            instance_tags = parse_orthanc_json(instance_tags_json)
            series_uid = instance_tags.get("SeriesInstanceUID")

            tenant = orthanc.RestApiGet(f'/instances/{resourceId}/metadata/Tenant').decode('utf-8')

            if not series_uid or not tenant:
                logger.warning(f"Could not get Series UID or Tenant for instance {resourceId}. Skipping.")
                return

            with series_tracker_lock:
                # Khởi tạo tracker cho series nếu chưa có
                if series_uid not in series_instance_tracker:
                    series_instance_tracker[series_uid] = {
                        "instances": set(), # Dùng set để tránh trùng lặp
                        "tenant": tenant
                    }
                
                # Thêm instance vào tracker
                series_instance_tracker[series_uid]["instances"].add(resourceId)
                
                # Kiểm tra nếu đủ số lượng để đóng gói và gửi
                if len(series_instance_tracker[series_uid]["instances"]) >= FORWARDING_PACK_SIZE:
                    instances_to_forward = series_instance_tracker[series_uid]["instances"]
                    
                    # Gửi gói đi
                    forward_instance_pack(
                        instances_to_forward,
                        DATACENTER_PEER,
                        series_uid,
                        reason="pack_size_reached"
                    )
                    
                    # Xóa các instance đã gửi khỏi tracker
                    series_instance_tracker[series_uid]["instances"] = set()

        except Exception as e:
            logger.error(f"Error processing NEW_INSTANCE {resourceId}: {str(e)}")

    # THÊM LOGIC MỚI CHO STABLE_SERIES
    if changeType == orthanc.ChangeType.STABLE_SERIES:
        try:
            series_tags_json = orthanc.RestApiGet(f'/series/{resourceId}/simplified-tags')
            series_tags = parse_orthanc_json(series_tags_json)
            series_uid = series_tags.get("SeriesInstanceUID")

            if not series_uid:
                return

            with series_tracker_lock:
                # Kiểm tra xem series này có trong tracker không
                if series_uid in series_instance_tracker:
                    tracker = series_instance_tracker[series_uid]
                    remaining_instances = tracker["instances"]

                    if remaining_instances:
                        # Gửi đi các instance còn lại
                        forward_instance_pack(
                            remaining_instances,
                            DATACENTER_PEER,
                            series_uid,
                            reason="stable_series"
                        )
                    
                    # Dọn dẹp tracker cho series này để giải phóng bộ nhớ
                    del series_instance_tracker[series_uid]
                    logger.info(f"Cleaned up tracker for stable series: {series_uid}")

        except Exception as e:
            logger.error(f"Error processing STABLE_SERIES {resourceId}: {str(e)}")

    if changeType == orthanc.ChangeType.NEW_STUDY:
        orthanc.LogWarning(f"START STUDY")
        study = orthanc.RestApiGet(
            f"/studies/{resourceId}?requestedTags=ModalitiesInStudy"
        )
        study_stats = orthanc.RestApiGet(f"/studies/{resourceId}/statistics")
        study_data = parse_orthanc_json(study)
        study_stats_data = parse_orthanc_json(study_stats)
        study_tags = study_data.get("MainDicomTags", {})

        accession_number = study_tags.get("AccessionNumber", "")
        count_series = study_stats_data.get("CountSeries", 0)
        count_instances = study_stats_data.get("CountInstances", 0)
        study_uid = study_tags.get("StudyInstanceUID", "")

        modality = get_study_modality_from_study_data(study_data)
        pacs_destination = get_pacs_destination(modality)
        pacs_aet = get_pacs_aet(modality)

        logger.info(f"Pacs destination: {pacs_destination}")
        logger.info(f"Pacs AET: {pacs_aet}")
        orthanc.LogWarning(f"Modality: {modality}")
        orthanc.LogWarning(f"Pacs destination: {pacs_destination}")
        orthanc.LogWarning(f"Pacs AET: {pacs_aet}")
        orthanc.LogWarning(f"Accession Number: {accession_number}")
        orthanc.LogWarning(f"Count Series: {count_series}")
        orthanc.LogWarning(f"Count Instances: {count_instances}")
        orthanc.LogWarning(f"Study UID: {study_uid}")

        study_resource = get_study(study_uid)
        study_dicom_tags = study_resource.get("MainDicomTags", {})
        patient_dicom_tags = study_resource.get("PatientMainDicomTags", {})
        other_dicom_tags = study_resource.get("RequestedTags", {})

        extras = {
            "studyDicomTags": study_dicom_tags,
            "patientDicomTags": patient_dicom_tags,
            "otherDicomTags": other_dicom_tags,
        }

        update_eorder_status_async(
            accession_number=accession_number,
            status="COMPLETED",
            study_uid=study_uid,
            count_series=count_series,
            count_instances=count_instances,
            storage_aet=pacs_aet,
            extras=extras,
        )

    if changeType == orthanc.ChangeType.STABLE_STUDY:
        study = orthanc.RestApiGet(
            f"/studies/{resourceId}?requestedTags=ModalitiesInStudy"
        )
        study_stats = orthanc.RestApiGet(f"/studies/{resourceId}/statistics")
        study_data = parse_orthanc_json(study)
        study_stats_data = parse_orthanc_json(study_stats)
        study_tags = study_data.get("MainDicomTags", {})

        accession_number = study_tags.get("AccessionNumber", "")
        count_series = study_stats_data.get("CountSeries", 0)
        count_instances = study_stats_data.get("CountInstances", 0)
        study_uid = study_tags.get("StudyInstanceUID", "")

        modality = get_study_modality_from_study_data(study_data)
        pacs_destination = get_pacs_destination(modality)
        pacs_aet = get_pacs_aet(modality)

        logger.info(f"Pacs destination: {pacs_destination}")
        logger.info(f"Pacs AET: {pacs_aet}")
        orthanc.LogWarning(f"Accession Number: {accession_number}")
        orthanc.LogWarning(f"Count Series: {count_series}")
        orthanc.LogWarning(f"Count Instances: {count_instances}")
        orthanc.LogWarning(f"Study UID: {study_uid}")

        # Use async version instead of blocking call
        update_eorder_status_async(
            accession_number=accession_number,
            study_uid=study_uid,
            status="COMPLETED",
            storage_aet=pacs_aet,
            count_series=count_series,
            count_instances=count_instances,
        )

        # This returns immediately - no blocking!
        orthanc.LogWarning(f"STABLE STUDY processing completed for {resourceId}")

    if changeType == orthanc.ChangeType.JOB_FAILURE:
        job_id = resourceId
        orthanc.LogWarning(f"Job {resourceId} has failed")
        retry_store_operation(job_id)
        orthanc.LogWarning(f"Job {job_id} has been retried")


orthanc.RegisterOnChangeCallback(OnChange)
orthanc.RegisterRestCallback("/e-orders", OnCreateWorklist)
orthanc.RegisterRestCallback("/e-orders/cancel", OnCancelWorklist)
orthanc.RegisterRestCallback("/e-orders/resend-dicom", OnResendDICOM)
orthanc.RegisterRestCallback("/health", OnHealthCheck)