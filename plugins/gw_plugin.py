import orthanc
import requests
import json
import os
import datetime
import time
import threading
from pydicom.dataset import Dataset, FileDataset
from pydicom.uid import generate_uid
from urllib.parse import quote

# ===================== CONFIG =====================
SKG_API_NEW_STUDY = os.getenv("SKG_API_NEW_STUDY", "http://host.docker.internal:5000/api/v1/new-study")
SKG_API_STABLE_STUDY = os.getenv("SKG_API_STABLE_STUDY", "http://host.docker.internal:5000/api/v1/stable-study")
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
HEALTH_LOG_FILE = os.getenv("HEALTH_LOG_FILE", "/home/oscar/orthanc-plugins/health.log")
HEALTH_INTERVAL_MINUTES = float(os.getenv("HEALTH_INTERVAL_MINUTES", "3"))

# ===================== CONFIG =====================
def log(msg):
    print(f"[GW_PLUGIN] {msg}", flush=True)
    orthanc.LogWarning(f"[GW_PLUGIN] {msg}")

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
            log(f"[FORWARD] Sent {len(instances)} instances to {target}")
            return True
        except Exception as e:
            log(f"[FORWARD] Retry {attempt}/{retry} failed ({len(instances)} instances): {str(e)}")
            time.sleep(2 ** attempt)
    return False


def forward_series(series_id, target):
    try:
        series_raw = orthanc.RestApiGet(f"/series/{series_id}")
        series_json = json.loads(series_raw)
        instances = series_json.get("Instances", [])
        if not instances:
            log(f"[FORWARD] Series {series_id} has no instances, skipping")
            return
        for i in range(0, len(instances), CHUNK_SIZE):
            chunk = instances[i:i + CHUNK_SIZE]
            ok = forward_chunk(chunk, target)
            if not ok:
                log(f"[FORWARD] Series {series_id}, chunk {i // CHUNK_SIZE + 1} failed permanently")
                return
        log(f"[FORWARD] Series {series_id} forwarded successfully ({len(instances)} instances)")
    except Exception as e:
        log(f"[FORWARD] Error forwarding series {series_id}: {str(e)}")


def forward_study(study_id, target):
    try:
        study_raw = orthanc.RestApiGet(f"/studies/{study_id}")
        study_json = json.loads(study_raw)
        series_ids = study_json.get("Series", [])
        log(f"[FORWARD] Forwarding study {study_id} to {target}, {len(series_ids)} series")
        for sid in series_ids:
            forward_series(sid, target)
    except Exception as e:
        log(f"[FORWARD] Error forwarding study {study_id}: {str(e)}")


# ===================== TELERAD INTEGRATION =====================
def update_telerad_eorders(accession, study_uid, status, count_series=0, count_instances=0, extras=None):
    if not ENABLE_TELERAD:
        log("[TELERAD] Disabled, skipping update.")
        return
    if not TELERAD_URL or not SECRET_KEY:
        log("[TELERAD] Missing TELERAD_URL or SECRET_KEY.")
        return

    valid_statuses = {
        "COMPLETED", "SCHEDULED", "CANCELLED",
        "UPLOAD_FAILED", "UPLOADING", "WAIT_FOR_CONFIRM",
        "NEED_RESEND", "UNUSUAL_COMPLETED",
        "UNUSUAL_UPLOADING", "UNUSUAL_UPLOAD_FAILED"
    }
    if status not in valid_statuses:
        log(f"[TELERAD] Invalid status '{status}', skipping update.")
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
            log(f"[TELERAD] Sending status '{status}' (try {attempt}/{MAX_RETRIES})...")
            r = requests.post(f"{TELERAD_URL}/e-orders/update-status", json=payload, headers=headers, timeout=10)
            if 200 <= r.status_code < 300:
                log(f"[TELERAD] Success: HTTP {r.status_code}")
                return
            else:
                log(f"[TELERAD] Failed (HTTP {r.status_code}): {r.text}")
        except Exception as e:
            log(f"[TELERAD] Error: {str(e)}")
        time.sleep(2 ** attempt)
    log(f"[TELERAD] Max retry reached for study {study_uid}")


# ===================== WORKLIST SYNC ======================
def create_dicom_worklist(order):
    ds = Dataset()

    # --- Thông tin bệnh nhân ---
    ds.PatientID = str(order.get("patient", {}).get("id", "UNKNOWN")).split(".")[-1]
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

    # --- Scheduled Step Sequence ---
    step_ds = Dataset()
    step_ds.Modality = modality
    step_ds.ScheduledStationAETitle = "ORTHANC_MT"
    scheduled_date = order.get("scheduledDate", "")
    date, time_ = convert_dicom_datetime(scheduled_date)
    step_ds.ScheduledProcedureStepStartDate = date or datetime.datetime.now().strftime("%Y%m%d")
    step_ds.ScheduledProcedureStepStartTime = time_ or datetime.datetime.now().strftime("%H%M%S")
    step_ds.ScheduledProcedureStepStatus = "SCHEDULED"
    step_ds.ScheduledProcedureStepID = f"STP{ds.AccessionNumber}"
    step_ds.ScheduledPerformingPhysicianName = str(order.get("referralPhysician", ""))
    step_ds.ScheduledProcedureStepDescription = ds.StudyDescription

    ds.ScheduledProcedureStepSequence = [step_ds]

    # --- File Meta ---
    file_meta = Dataset()
    file_meta.MediaStorageSOPClassUID = generate_uid()
    file_meta.MediaStorageSOPInstanceUID = generate_uid()
    file_meta.ImplementationClassUID = generate_uid()

    # --- Lưu file ---
    os.makedirs(WORKLIST_FOLDER, exist_ok=True)
    acc_clean = no_special_characters(ds.AccessionNumber)
    filename = os.path.join(WORKLIST_FOLDER, f"{acc_clean}.wl")

    if os.path.exists(filename):
            # log(f"[WORKLIST] Skip existing: {filename}")
            return

    file_ds = FileDataset(filename, {}, file_meta=file_meta, preamble=b"\0" * 128)
    file_ds.update(ds)
    file_ds.is_little_endian = True
    file_ds.is_implicit_VR = True
    file_ds.save_as(filename, write_like_original=False)

    log(f"[WORKLIST] Created: {filename}")


def delete_worklist(accession_number):
    try:
        acc_clean = no_special_characters(accession_number)
        path = os.path.join(WORKLIST_FOLDER, f"{acc_clean}.wl")

        if os.path.exists(path):
            os.remove(path)
            log(f"[WORKLIST][INFO] Deleted: {accession_number}")
        else:
            log(f"")
    except Exception as e:
        log(f"[WORKLIST][ERROR] Delete failed for {accession_number}: {str(e)}")

def sync_worklist():
    """Đồng bộ định kỳ với Telerad eOrders (kèm cơ chế NEED_RESEND)"""
    if not ENABLE_TELERAD:
        log("[WORKLIST] Disabled")
        return

    while True:
        try:
            headers = {"x-api-key": SECRET_KEY}
            statuses = ["SCHEDULED", "WAITING", "CANCELLED", "NEED_RESEND"]
            data = []

            for st in statuses:
                # Encode từng filter eq
                filter_str = quote(f"status||$eq||{st}")
                url = f"{TELERAD_URL}/e-orders/clinic-e-orders?filter={filter_str}&limit=500"

                log(f"[WORKLIST] Fetching {st} from {url}")
                r = requests.get(url, headers=headers, timeout=10)

                if r.status_code == 200:
                    part = r.json().get("data", [])
                    data.extend(part)
                    log(f"[WORKLIST] Got {len(part)} {st} orders")
                else:
                    log(f"[WORKLIST] HTTP {r.status_code} for {st}: {r.text}")

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
                    log(f"[WORKLIST] NEED_RESEND detected for {acc}")
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
                            log(f"[RESEND] Study {acc} not found locally, pulling from PACS {DATACENTER_PACS}")
                            query_payload = {
                                "Level": "STUDY",
                                "Query": {"AccessionNumber": acc}
                            }
                            orthanc.RestApiPost(f"/modalities/{DATACENTER_PACS}/query", json.dumps(query_payload))
                            orthanc.RestApiPost(f"/modalities/{DATACENTER_PACS}/retrieve", json.dumps(query_payload))

                            # Đợi PACS gửi ảnh về (tối đa 20 giây)
                            for _ in range(20):
                                time.sleep(1)
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
                            log(f"[RESEND] Failed to pull study {acc} from PACS (not found after retry).")
                            continue

                        # --- Gửi lại study tới Telerad ---
                        log(f"[RESEND] Forwarding study {found_study} (Acc={acc}) to Telerad...")
                        forward_study(found_study, DATACENTER_PACS)

                        # --- Cập nhật trạng thái lên Telerad ---
                        study_info = json.loads(orthanc.RestApiGet(f"/studies/{found_study}"))
                        study_uid = study_info.get("MainDicomTags", {}).get("StudyInstanceUID", "")
                        stats_raw = orthanc.RestApiGet(f"/studies/{found_study}/statistics")
                        stats_json = json.loads(stats_raw)
                        count_series = int(stats_json.get("CountSeries", 0))
                        count_instances = int(stats_json.get("CountInstances", 0))

                        update_telerad_eorders(acc, study_uid, "COMPLETED", count_series, count_instances)
                        log(f"[RESEND] Resent {acc} successfully")
                    except Exception as e:
                        log(f"[RESEND] Error while resending {acc}: {e}")

            # log(f"[WORKLIST] Synced OK ({len(data)} orders total)")
        except Exception as e:
            log(f"[WORKLIST] Sync error: {str(e)}")

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
        log(f"[RESEND] Error finding study for {accession}: {e}")
        return []


def resend_study_by_accession(accession):
    """Gửi lại toàn bộ study có accessionNumber tương ứng"""
    studies = find_studies_by_accession(accession)
    if not studies:
        log(f"[RESEND] No study found in Orthanc for accession {accession}")
        return

    for study_id in studies:
        try:
            log(f"[RESEND] Forwarding study {study_id} for accession {accession}")
            forward_study(study_id, DATACENTER_PACS)

            # Lấy lại thông tin study để gửi cập nhật lên Telerad
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
            log(f"[RESEND] Study {study_id} resent successfully to Telerad")

        except Exception as e:
            log(f"[RESEND] Error resending study {study_id}: {e}")


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
                    log(f"[NEW_STUDY] Error reading series {sid}: {str(e)}")
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

        log(f"[NEW_STUDY] Sending studyInstanceUID={payload['studyInstanceUID']}")
        log(json.dumps(payload, indent=2))

        r = requests.post(SKG_API_NEW_STUDY, json=payload, timeout=HTTP_TIMEOUT)
        log(f"[POST] Success: {SKG_API_NEW_STUDY} (status={r.status_code})")

        update_telerad_eorders(
            accession=payload["accessionNumber"],
            study_uid=payload["studyInstanceUID"],
            status="COMPLETED",
            extras=payload["extras"]
        )
    except Exception as e:
        log(f"[NEW_STUDY] Unexpected error: {str(e)}")

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
                    log(f"[STABLE_STUDY] Error reading series {sid}: {str(e)}")
            modality = ",".join(sorted(modalities)) if modalities else "UNKNOWN"

        stats_raw = orthanc.RestApiGet(f"/studies/{study_id}/statistics")
        stats_json = json.loads(stats_raw)
        count_series = int(stats_json.get("CountSeries", 0))
        count_instances = int(stats_json.get("CountInstances", 0))

        payload = {
            "accessionNumber": accession_number,
            "studyInstanceUID": study_uid,
            "modality": modality,
            "countSeries": count_series,
            "countInstances": count_instances,
            "event_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        requests.post(SKG_API_STABLE_STUDY, json=payload, timeout=HTTP_TIMEOUT)
        delete_worklist(accession_number)
        update_telerad_eorders(accession_number, study_uid, "COMPLETED", count_series, count_instances)
        forward_study(study_id, DATACENTER_PACS)
    except Exception as e:
        log(f"[STABLE_STUDY] Unexpected error: {str(e)}")


# ===================== CLEANUP + RESTART =====================
def clean_old_studies():
    try:
        log("[CLEANUP] Starting Orthanc DB cleanup...")
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
        log(f"[CLEANUP] Deleted {deleted} old studies (> {DELETE_OLDER_THAN_DAYS} days)")
    except Exception as e:
        log(f"[CLEANUP] Error: {str(e)}")


# def restart_orthanc():
#     try:
#         log("[CLEANUP] Restarting Orthanc service...")
#         orthanc.RestApiPost("/tools/restart", "")
#     except Exception as e:
#         log(f"[CLEANUP] Restart failed: {str(e)}")


def schedule_cleanup(interval_hours=CLEANUP_INTERVAL_HOURS):
    def job():
        while True:
            clean_old_studies()
            # restart_orthanc()
            time.sleep(interval_hours * 3600)
    t = threading.Thread(target=job, daemon=True)
    t.start()
    log(f"[CLEANUP] Scheduled every {interval_hours} hour(s)")

# ===================== HEALTH CHECK =====================
def health_check():
    """Kiểm tra tình trạng plugin và ghi vào health log"""
    try:
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        skg_status = "ok"

        # Kiểm tra API SKG (optional)
        try:
            test_url = SKG_API_STABLE_STUDY.replace("/stable-study", "/health")
            r = requests.get(test_url, timeout=3)
            if r.status_code  != 200:
                skg_status = f"bad({r.status_code})"
        except Exception:
            skg_status = "unreachable"

        msg = f"[HEALTH] {now} | plugin=gw_plugin | status=running | skg_api={skg_status}\n"

        # Ghi ra file
        with open(HEALTH_LOG_FILE, "a") as f:
            f.write(msg)

        # Ghi thêm vào log Orthanc (tiện debug)
        orthanc.LogWarning(msg.strip())
    except Exception as e:
        orthanc.LogError(f"[HEALTH] Error: {str(e)}")

def schedule_health_check(interval_minutes=HEALTH_INTERVAL_MINUTES):
    def job():
        while True:
            health_check()
            time.sleep(interval_minutes * 60)
    t = threading.Thread(target=job, daemon=True)
    t.start()
    log(f"[HEALTH] Scheduled every {interval_minutes} minute(s)")

# ===================== CHANGE HANDLER =====================
def OnChange(changeType, level, resourceId):
    try:
        if changeType == orthanc.ChangeType.NEW_STUDY:
            OnNewStudy(resourceId)
        elif changeType == orthanc.ChangeType.STABLE_STUDY:
            OnStableStudy(resourceId)
    except Exception as e:
        log(f"[CHANGE] Error handling change: {str(e)}")


# ===================== STARTUP =====================
def Initialize():
    log("=" * 60)
    log("GW Plugin initializing (NEW_STUDY + STABLE_STUDY + Auto Forward + TELERAD + CLEANUP)...")
    orthanc.RegisterOnChangeCallback(OnChange)
    threading.Thread(target=sync_worklist, daemon=True).start()
    schedule_cleanup()
    schedule_health_check()
    log("[WORKLIST] Background sync started.")
    log("[OK] Callbacks & cleanup scheduler registered")


def Finalize():
    log("GW Plugin shutting down...")

Initialize()