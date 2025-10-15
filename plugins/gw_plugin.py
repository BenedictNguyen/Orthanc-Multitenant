import orthanc
import requests
import json
import os
import datetime
import time
import threading
import logging
from pydicom.dataset import Dataset, FileDataset
from pydicom.uid import generate_uid
from urllib.parse import quote
from logging.handlers import RotatingFileHandler

# ===================== CONFIG =====================
DATACENTER_PACS = os.getenv("DATACENTER_PACS", "DATACENTER_PACS")
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "5"))
CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "500"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "7"))

# Telerad integration
ENABLE_TELERAD = os.getenv("ENABLE_TELERAD", "false").lower() == "true"
TELERAD_URL = os.getenv("TELERAD_URL", "https://telerad-api.caresnova.ai/api")
SECRET_KEY = os.getenv("SECRET_KEY", "")

# Woklist config
WORKLIST_FOLDER = os.getenv("WORKLIST_DATABASE", "/var/lib/orthanc/worklists")
WORKLIST_PULL_INTERVAL = int(os.getenv("WORKLIST_PULL_INTERVAL", "5"))

# Interval cleanup (in hours)
CLEANUP_INTERVAL_HOURS = float(os.getenv("CLEANUP_INTERVAL_HOURS", "CLEANUP_INTERVAL_HOURS"))
DELETE_OLDER_THAN_DAYS = int(os.getenv("DELETE_OLDER_THAN_DAYS", "DELETE_OLDER_THAN_DAYS"))

# Health check log file and interval (in minutes)
HEALTH_LOG_FILE = os.getenv("HEALTH_LOG_FILE", "/var/log/orthanc/health.log")
HEALTH_INTERVAL_MINUTES = float(os.getenv("HEALTH_INTERVAL_MINUTES", "3"))

# Detail log file
DETAIL_LOG_FILE = os.getenv("DETAIL_LOG_FILE", "/var/log/orthanc/detail.log")
os.makedirs(os.path.dirname(DETAIL_LOG_FILE), exist_ok=True)
# ===================== LOGGING FUNCTIONS =====================
detail_handler = RotatingFileHandler(
    DETAIL_LOG_FILE,
    maxBytes=1*1024*1024,
    backupCount=2,
    encoding='utf-8'
)
detail_handler.setFormatter(logging.Formatter('[%(asctime)s] [%(levelname)s] %(message)s'))

detail_logger = logging.getLogger('detail')
detail_logger.setLevel(logging.DEBUG)
detail_logger.addHandler(detail_handler)

def log(msg, level="INFO", detail_only=False):
    """
    Log message to detail file with rotation
    level: INFO, WARNING, ERROR
    """
    if level == "ERROR":
        detail_logger.error(msg)
    elif level == "WARNING":
        detail_logger.warning(msg)
    else:
        detail_logger.info(msg)

def log_detail(msg):
    """Log only to detail file"""
    detail_logger.debug(msg)

# ===================== CONFIG =====================
def no_special_characters(s: str):
    if s is None:
        return ""
    import re
    return re.sub(r"[!@#$%^&*()_+={}\[\]:;\"'<>?,./\\|`~]", "", s)

def convert_dicom_datetime(iso_date_string, timezone_offset=7):
    if not iso_date_string:
        return ("", "")
    dt = datetime.datetime.fromisoformat(iso_date_string.replace("Z", "+00:00"))
    dt = dt + datetime.timedelta(hours=timezone_offset)
    return dt.strftime("%Y%m%d"), dt.strftime("%H%M%S")

def convert_dicom_gender(gender: str) -> str:
    if not gender:
        return "O"
    gender = gender.upper()
    if gender.startswith("M"): return "M"
    if gender.startswith("F"): return "F"
    return "O"

# ===================== FORWARD FUNCTIONS =====================
def forward_chunk(instances, target, retry=MAX_RETRIES):
    for attempt in range(1, retry + 1):
        try:
            orthanc.RestApiPost(f"/modalities/{target}/store", json.dumps(instances))
            log_detail(f"[FORWARD] Sent {len(instances)} instances to {target} (attempt {attempt})")
            return True
        except Exception as e:
            log_detail(f"[FORWARD] Retry {attempt}/{retry} failed ({len(instances)} instances): {str(e)}")
            if attempt == retry:
                log(f"[FORWARD] Failed to send {len(instances)} instances to {target}: {str(e)}", level="ERROR")
            time.sleep(2 ** attempt)
    return False


def forward_series(series_id, target):
    try:
        series_raw = orthanc.RestApiGet(f"/series/{series_id}")
        series_json = json.loads(series_raw)
        instances = series_json.get("Instances", [])
        if not instances:
            log_detail(f"[FORWARD] Series {series_id} has no instances, skipping")
            return
        
        log_detail(f"[FORWARD] Series {series_id}: {len(instances)} instances")
        for i in range(0, len(instances), CHUNK_SIZE):
            chunk = instances[i:i + CHUNK_SIZE]
            ok = forward_chunk(chunk, target)
            if not ok:
                log(f"[FORWARD] Series {series_id} failed at chunk {i // CHUNK_SIZE + 1}", level="ERROR")
                return
        log_detail(f"[FORWARD] Series {series_id} forwarded successfully")
    except Exception as e:
        log(f"[FORWARD] Error forwarding series {series_id}: {str(e)}", level="ERROR")


def forward_study(study_id, target):
    try:
        study_raw = orthanc.RestApiGet(f"/studies/{study_id}")
        study_json = json.loads(study_raw)
        series_ids = study_json.get("Series", [])
        log_detail(f"[FORWARD] Study {study_id}: {len(series_ids)} series to {target}")
        for sid in series_ids:
            forward_series(sid, target)
        log(f"[FORWARD] Study {study_id} forwarded to {target}")
    except Exception as e:
        log(f"[FORWARD] Error forwarding study {study_id}: {str(e)}", level="ERROR")


# ===================== TELERAD INTEGRATION =====================
def update_telerad_eorders(accession, study_uid, status, count_series=0, count_instances=0, extras=None):
    if not ENABLE_TELERAD:
        log_detail("[TELERAD] Disabled, skipping update")
        return
    if not TELERAD_URL or not SECRET_KEY:
        log("[TELERAD] Missing TELERAD_URL or SECRET_KEY", level="ERROR")
        return

    valid_statuses = {
        "COMPLETED", "SCHEDULED", "CANCELLED",
        "UPLOAD_FAILED", "UPLOADING", "WAIT_FOR_CONFIRM",
        "NEED_RESEND", "UNUSUAL_COMPLETED",
        "UNUSUAL_UPLOADING", "UNUSUAL_UPLOAD_FAILED"
    }
    if status not in valid_statuses:
        log(f"[TELERAD] Invalid status '{status}'", level="WARNING")
        return

    payload = {
        "accessionNumber": accession,
        "studyInstanceUID": study_uid,
        "status": status,
        "countSeries": count_series or 0,
        "countInstances": count_instances or 0,
        "storageAET": os.getenv("PACS_AET", "ORTHANC_MT"),
    }
    if extras:
        payload["extras"] = extras
    headers = {"Content-Type": "application/json", "x-api-key": SECRET_KEY}

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            log_detail(f"[TELERAD] Sending status '{status}' for {accession} (attempt {attempt})")
            r = requests.post(f"{TELERAD_URL}/e-orders/update-status", json=payload, headers=headers, timeout=10)
            if 200 <= r.status_code < 300:
                log_detail(f"[TELERAD] Success: {accession} -> {status}")
                return
            else:
                log_detail(f"[TELERAD] HTTP {r.status_code}: {r.text}")
                if attempt == MAX_RETRIES:
                    log(f"[TELERAD] Failed status '{status}' for {accession}: HTTP {r.status_code}", level="ERROR")
        except Exception as e:
            log_detail(f"[TELERAD] Attempt {attempt} error: {str(e)}")
            if attempt == MAX_RETRIES:
                log(f"[TELERAD] Error updating {accession}: {str(e)}", level="ERROR")
        time.sleep(2 ** attempt)


# ===================== WORKLIST SYNC ======================
def create_dicom_worklist(order):
    ds = Dataset()

    # --- Thông tin bệnh nhân ---
    patient_id = order.get("patientId", "UNKNOWN")
    ds.PatientID = str(patient_id).split(".")[-1]
    ds.PatientName = str(order.get("patient", {}).get("fullName", "UNKNOWN"))
    ds.PatientSex = convert_dicom_gender(order.get("patient", {}).get("gender", "O"))
    dob = order.get("patient", {}).get("dob", "")
    ds.PatientBirthDate, _ = convert_dicom_datetime(dob)

    # --- Thông tin study ---
    ds.AccessionNumber = str(order.get("accessionNumber", ""))
    ds.StudyInstanceUID = order.get("studyInstanceUID", generate_uid())
    ds.StudyDescription = str(order.get("procedure", {}).get("name", ""))
    modality = (
        order.get("modality", {})
        .get("masterModality", {})
        .get("customId", "CR")
    )
    ds.Modality = modality

    # --- Thông tin bác sĩ & khoa ---
    ds.ReferringPhysicianName = str(order.get("referralPhysician", ""))
    ds.InstitutionName = str(order.get("clinic", {}).get("clinicName", ""))
    ds.InstitutionalDepartmentName = ds.InstitutionName

    # --- Scheduled Procedure Step Sequence ---
    step_ds = Dataset()
    step_ds.Modality = modality
    step_ds.ScheduledStationAETitle = "ORTHANC_WL"
    step_ds.ScheduledStationName = "STATION1"
    step_ds.ScheduledProcedureStepID = f"SPS_{ds.AccessionNumber or generate_uid()}"
    step_ds.ScheduledProcedureStepDescription = ds.StudyDescription or "Imaging Procedure"

    # Thời gian thực hiện
    scheduled_date = order.get("scheduledDate", "")
    date, time_ = convert_dicom_datetime(scheduled_date)
    step_ds.ScheduledProcedureStepStartDate = date or datetime.datetime.now().strftime("%Y%m%d")
    step_ds.ScheduledProcedureStepStartTime = time_ or datetime.datetime.now().strftime("%H%M%S")
    step_ds.ScheduledPerformingPhysicianName = str(order.get("referralPhysician", "Doctor"))
    step_ds.ScheduledProcedureStepLocation = "Room1"
    step_ds.ScheduledProcedureStepStatus = "SCHEDULED"

    # --- Thêm Sequence bắt buộc ---
    ds.ScheduledProcedureStepSequence = [step_ds]

    # --- Các tag bổ sung hữu ích ---
    ds.RequestingPhysician = ds.ReferringPhysicianName
    ds.RequestedProcedureDescription = ds.StudyDescription
    ds.RequestedProcedureID = f"RP_{ds.AccessionNumber or generate_uid()}"
    ds.RequestedProcedurePriority = "ROUTINE"
    ds.RequestedProcedureLocation = "Radiology"
    ds.RequestedProcedureComments = "Auto-generated by Orthanc GW Plugin"

    # --- File Meta ---
    file_meta = Dataset()
    file_meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.31"
    file_meta.MediaStorageSOPInstanceUID = generate_uid()
    file_meta.ImplementationClassUID = generate_uid()

    # --- Lưu file ---
    os.makedirs(WORKLIST_FOLDER, exist_ok=True)
    acc_clean = no_special_characters(ds.AccessionNumber or generate_uid())
    filename = os.path.join(WORKLIST_FOLDER, f"{acc_clean}.wl")

    file_ds = FileDataset(filename, {}, file_meta=file_meta, preamble=b"\0" * 128)
    file_ds.update(ds)
    file_ds.is_little_endian = True
    file_ds.is_implicit_VR = True
    file_ds.save_as(filename, write_like_original=False)
    
    log_detail(f"[WORKLIST] Created: {ds.AccessionNumber} | Patient: {ds.PatientName} | Modality: {modality}")
    log(f"[WORKLIST] Created: {ds.AccessionNumber}")

def delete_worklist(accession_number):
    try:
        acc_clean = no_special_characters(accession_number)
        path = os.path.join(WORKLIST_FOLDER, f"{acc_clean}.wl")
        if os.path.exists(path):
            os.remove(path)
            log(f"[WORKLIST] Deleted: {accession_number}")
        else:
            log_detail(f"[WORKLIST] Not found for deletion: {accession_number}")
    except Exception as e:
        log(f"[WORKLIST] Delete failed for {accession_number}: {str(e)}", level="ERROR")

def sync_worklist():
    """Đồng bộ định kỳ với Telerad eOrders"""
    if not ENABLE_TELERAD:
        log_detail("[WORKLIST] Sync disabled (ENABLE_TELERAD=false)")
        return

    while True:
        try:
            headers = {"x-api-key": SECRET_KEY}
            statuses = ["SCHEDULED", "WAITING", "CANCELLED", "NEED_RESEND"]
            data = []

            for st in statuses:
                filter_str = quote(f"status||$eq||{st}")
                url = f"{TELERAD_URL}/e-orders/clinic-e-orders?filter={filter_str}&limit=500"
                log_detail(f"[WORKLIST] Fetching {st} from API")
                r = requests.get(url, headers=headers, timeout=10)

                if r.status_code == 200:
                    part = r.json().get("data", [])
                    data.extend(part)
                    log_detail(f"[WORKLIST] Got {len(part)} {st} orders")
                else:
                    log(f"[WORKLIST] HTTP {r.status_code} for {st}", level="WARNING")

            # Xử lý dữ liệu tổng hợp
            for order in data:
                status = order.get("status", "").upper()
                acc = order.get("accessionNumber", "").strip()

                if not acc:
                    continue

                if status in ["SCHEDULED", "WAITING"]:
                    create_dicom_worklist(order)
                elif status == "CANCELLED":
                    delete_worklist(acc)
                
                elif status == "NEED_RESEND":
                    log(f"[RESEND] Processing {acc}")
                    try:
                        # --- Kiểm tra Orthanc có study chưa ---
                        found_study = None
                        studies = json.loads(orthanc.RestApiGet("/studies"))
                        for s in studies:
                            info = json.loads(orthanc.RestApiGet(f"/studies/{s}"))
                            if info.get("MainDicomTags", {}).get("AccessionNumber") == acc:
                                found_study = s
                                break

                        # --- Nếu chưa có, pull từ PACS ---
                        if not found_study:
                            log(f"[RESEND] Pulling {acc} from PACS {DATACENTER_PACS}")
                            query_payload = {
                                "Level": "STUDY",
                                "Query": {"AccessionNumber": acc}
                            }
                            orthanc.RestApiPost(f"/modalities/{DATACENTER_PACS}/query", json.dumps(query_payload))
                            orthanc.RestApiPost(f"/modalities/{DATACENTER_PACS}/retrieve", json.dumps(query_payload))

                            # Đợi PACS gửi ảnh về
                            for wait_count in range(20):
                                time.sleep(1)
                                log_detail(f"[RESEND] Waiting for {acc} ({wait_count + 1}/20)")
                                studies = json.loads(orthanc.RestApiGet("/studies"))
                                for s in studies:
                                    info = json.loads(orthanc.RestApiGet(f"/studies/{s}"))
                                    if info.get("MainDicomTags", {}).get("AccessionNumber") == acc:
                                        found_study = s
                                        break
                                if found_study:
                                    break

                        # --- Nếu vẫn không thấy ---
                        if not found_study:
                            log(f"[RESEND] Study {acc} not found after pull", level="ERROR")
                            continue

                        # --- Gửi lại study tới Telerad ---
                        forward_study(found_study, DATACENTER_PACS)

                        # --- Cập nhật trạng thái lên Telerad ---
                        study_info = json.loads(orthanc.RestApiGet(f"/studies/{found_study}"))
                        study_uid = study_info.get("MainDicomTags", {}).get("StudyInstanceUID", "")
                        stats_raw = orthanc.RestApiGet(f"/studies/{found_study}/statistics")
                        stats_json = json.loads(stats_raw)
                        count_series = int(stats_json.get("CountSeries", 0))
                        count_instances = int(stats_json.get("CountInstances", 0))

                        update_telerad_eorders(acc, study_uid, "COMPLETED", count_series, count_instances)
                        log(f"[RESEND] Completed {acc}")
                    except Exception as e:
                        log(f"[RESEND] Error {acc}: {e}", level="ERROR")

            log_detail(f"[WORKLIST] Sync completed ({len(data)} orders processed)")
        except Exception as e:
            log(f"[WORKLIST] Sync error: {str(e)}", level="ERROR")

        time.sleep(WORKLIST_PULL_INTERVAL)

def find_studies_by_accession(accession):
    """Tìm danh sách study_id trong Orthanc có AccessionNumber tương ứng"""
    try:
        studies = json.loads(orthanc.RestApiGet("/studies"))
        matched = []
        for sid in studies:
            meta = json.loads(orthanc.RestApiGet(f"/studies/{sid}"))
            tags = meta.get("MainDicomTags", {})
            if tags.get("AccessionNumber", "") == accession:
                matched.append(sid)
        return matched
    except Exception as e:
        log(f"[RESEND] Error finding {accession}: {e}", level="ERROR")
        return []


def resend_study_by_accession(accession):
    """Gửi lại toàn bộ study có accessionNumber tương ứng"""
    studies = find_studies_by_accession(accession)
    if not studies:
        log(f"[RESEND] No study found for {accession}", level="WARNING")
        return

    for study_id in studies:
        try:
            forward_study(study_id, DATACENTER_PACS)

            study_info = json.loads(orthanc.RestApiGet(f"/studies/{study_id}"))
            tags = study_info.get("MainDicomTags", {})
            stats = json.loads(orthanc.RestApiGet(f"/studies/{study_id}/statistics"))
            count_series = stats.get("CountSeries", 0)
            count_instances = stats.get("CountInstances", 0)

            update_telerad_eorders(
                accession=accession,
                study_uid=tags.get("StudyInstanceUID", ""),
                status="COMPLETED",
                count_series=count_series,
                count_instances=count_instances,
                extras={"resend": True}
            )
            log(f"[RESEND] Study {study_id} completed")

        except Exception as e:
            log(f"[RESEND] Error {study_id}: {e}", level="ERROR")


# ===================== EVENT HANDLERS =====================
def OnNewStudy(study_id):
    try:
        study_raw = orthanc.RestApiGet(f"/studies/{study_id}")
        study_json = json.loads(study_raw)

        study_tags = study_json.get("MainDicomTags", {})
        patient_tags = study_json.get("PatientMainDicomTags", {})

        modality = study_tags.get("ModalitiesInStudy", "")
        if not modality:
            series_ids = study_json.get("Series", [])
            modalities = set()
            for sid in series_ids:
                try:
                    series_raw = orthanc.RestApiGet(f"/series/{sid}")
                    series_json = json.loads(series_raw)
                    mod = series_json["MainDicomTags"].get("Modality", "")
                    if mod:
                        modalities.add(mod)
                except Exception as e:
                    log_detail(f"[NEW_STUDY] Error reading series {sid}: {str(e)}")
            modality = ",".join(sorted(modalities)) if modalities else "UNKNOWN"

        payload = {
            "accessionNumber": study_tags.get("AccessionNumber", "MISSING"),
            "studyInstanceUID": study_tags.get("StudyInstanceUID", ""),
            "storageAet": os.getenv("PACS_AET", "ORTHANC_MT"),
            "extras": {
                "studyDicomTags": study_tags,
                "patientDicomTags": patient_tags,
                "otherDicomTags": {"ModalitiesInStudy": modality}
            }
        }

        log_detail(f"[NEW_STUDY] {payload['accessionNumber']} | UID: {payload['studyInstanceUID']}")
        log(f"[NEW_STUDY] Received: {payload['accessionNumber']}")

        update_telerad_eorders(
            accession=payload["accessionNumber"],
            study_uid=payload["studyInstanceUID"],
            status="COMPLETED",
            extras=payload["extras"]
        )
    except Exception as e:
        log(f"[NEW_STUDY] Error: {str(e)}", level="ERROR")

def OnStableStudy(study_id):
    try:
        study_raw = orthanc.RestApiGet(f"/studies/{study_id}")
        study_json = json.loads(study_raw)
        study_tags = study_json.get("MainDicomTags", {})
        study_uid = study_tags.get("StudyInstanceUID", "")
        accession_number = study_tags.get("AccessionNumber", "MISSING")

        modality = study_tags.get("ModalitiesInStudy", "")
        if not modality:
            series_ids = study_json.get("Series", [])
            modalities = set()
            for sid in series_ids:
                try:
                    series_raw = orthanc.RestApiGet(f"/series/{sid}")
                    series_json = json.loads(series_raw)
                    mod = series_json["MainDicomTags"].get("Modality", "")
                    if mod:
                        modalities.add(mod)
                except Exception as e:
                    log_detail(f"[STABLE_STUDY] Error reading series {sid}: {str(e)}")
            modality = ",".join(sorted(modalities)) if modalities else "UNKNOWN"

        stats_raw = orthanc.RestApiGet(f"/studies/{study_id}/statistics")
        stats_json = json.loads(stats_raw)
        count_series = int(stats_json.get("CountSeries", 0))
        count_instances = int(stats_json.get("CountInstances", 0))

        log_detail(f"[STABLE_STUDY] {accession_number} | Series: {count_series} | Instances: {count_instances}")
        log(f"[STABLE_STUDY] {accession_number} stable")

        delete_worklist(accession_number)
        update_telerad_eorders(accession_number, study_uid, "COMPLETED", count_series, count_instances)
        forward_study(study_id, DATACENTER_PACS)
    except Exception as e:
        log(f"[STABLE_STUDY] Error: {str(e)}", level="ERROR")


# ===================== CLEANUP + RESTART =====================
def clean_old_studies():
    try:
        log_detail("[CLEANUP] Starting scan...")
        studies = json.loads(orthanc.RestApiGet("/studies"))
        now = datetime.datetime.now()
        deleted = 0
        for s in studies:
            s_info = json.loads(orthanc.RestApiGet(f"/studies/{s}"))
            tags = s_info.get("MainDicomTags", {})
            date_str = tags.get("StudyDate", "")
            if date_str:
                study_date = datetime.datetime.strptime(date_str, "%Y%m%d")
                if (now - study_date).days > DELETE_OLDER_THAN_DAYS:
                    orthanc.RestApiDelete(f"/studies/{s}")
                    deleted += 1
                    log_detail(f"[CLEANUP] Deleted study {s} (Date: {date_str})")
        if deleted > 0:
            log(f"[CLEANUP] Deleted {deleted} studies (>{DELETE_OLDER_THAN_DAYS} days)")
        else:
            log_detail("[CLEANUP] No old studies to delete")
    except Exception as e:
        log(f"[CLEANUP] Error: {str(e)}", level="ERROR")


def schedule_cleanup(interval_hours=CLEANUP_INTERVAL_HOURS):
    def job():
        while True:
            clean_old_studies()
            time.sleep(interval_hours * 3600)
    t = threading.Thread(target=job, daemon=True)
    t.start()
    log_detail(f"[CLEANUP] Scheduled every {interval_hours} hour(s)")

# ===================== HEALTH CHECK =====================
def health_check():
    """Kiểm tra tình trạng plugin và ghi vào health log"""
    try:
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        msg = f"[HEALTH] {now} | plugin=running\n"
        with open(HEALTH_LOG_FILE, "a") as f:
            f.write(msg)
        log_detail("[HEALTH] Check completed")
    except Exception as e:
        log(f"[HEALTH] Error: {str(e)}", level="ERROR")

def schedule_health_check(interval_minutes=HEALTH_INTERVAL_MINUTES):
    def job():
        while True:
            health_check()
            time.sleep(interval_minutes * 60)
    t = threading.Thread(target=job, daemon=True)
    t.start()
    log_detail(f"[HEALTH] Scheduled every {interval_minutes} minute(s)")

# ===================== CHANGE HANDLER =====================
def OnChange(changeType, level, resourceId):
    try:
        if changeType == orthanc.ChangeType.NEW_STUDY:
            OnNewStudy(resourceId)
        elif changeType == orthanc.ChangeType.STABLE_STUDY:
            OnStableStudy(resourceId)
    except Exception as e:
        log(f"[CHANGE] Error: {str(e)}", level="ERROR")


# ===================== STARTUP =====================
def Initialize():
    log("=" * 60)
    log("GW Plugin v2.0 initializing...")
    log(f"Detail log: {DETAIL_LOG_FILE}")
    log(f"Health log: {HEALTH_LOG_FILE}")
    log(f"Telerad: {'Enabled' if ENABLE_TELERAD else 'Disabled'}")
    
    orthanc.RegisterOnChangeCallback(OnChange)
    threading.Thread(target=sync_worklist, daemon=True).start()
    schedule_cleanup()
    schedule_health_check()
    
    log("GW Plugin initialized successfully")
    log("=" * 60)


def Finalize():
    log("GW Plugin shutting down...")

Initialize()