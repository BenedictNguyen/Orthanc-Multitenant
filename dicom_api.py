from flask import Flask, request, jsonify
import pydicom
from pydicom.dataset import Dataset, FileDataset
import datetime, time, os
import requests
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

OUTPUT_DIR = r"E:\Dicomtest"  # thư mục chứa file DICOM giả
os.makedirs(OUTPUT_DIR, exist_ok=True)

ORTHANC_URL = "http://localhost:8042/instances"
ORTHANC_USER = "orthanc"
ORTHANC_PASS = "orthanc"

@app.route("/create-dicom", methods=["POST"])
def create_dicom():
    data = request.json
    patientId = data.get("patientId", "P000")
    patientName = data.get("patientName", "Unknown")
    accession = data.get("accession", "AC000")
    modality = data.get("modality", "CT")

    filename = os.path.join(OUTPUT_DIR, f"{accession}.dcm")

    # dataset tối thiểu
    ds = Dataset()
    ds.PatientID = patientId
    ds.PatientName = patientName
    ds.AccessionNumber = accession
    ds.Modality = modality
    ds.StudyInstanceUID = pydicom.uid.generate_uid()
    ds.SeriesInstanceUID = pydicom.uid.generate_uid()
    ds.SOPInstanceUID = pydicom.uid.generate_uid()
    ds.SOPClassUID = pydicom.uid.SecondaryCaptureImageStorage
    ds.SeriesNumber = "1"
    ds.InstanceNumber = "1"
    ds.StudyDate = datetime.date.today().strftime("%Y%m%d")
    ds.StudyTime = time.strftime("%H%M%S")

    # file meta
    file_meta = Dataset()
    file_meta.MediaStorageSOPClassUID = ds.SOPClassUID
    file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    file_meta.ImplementationClassUID = "1.2.826.0.1.3680043.2.1125.1"
    file_meta.TransferSyntaxUID = pydicom.uid.ExplicitVRLittleEndian

    dicom_file = FileDataset(filename, {}, file_meta=file_meta, preamble=b"\0" * 128)
    dicom_file.is_little_endian = True
    dicom_file.is_implicit_VR = False
    dicom_file.update(ds)

    dicom_file.save_as(filename)
    

    # upload lên Orthanc
    with open(filename, "rb") as f:
        r = requests.post(ORTHANC_URL, auth=(ORTHANC_USER, ORTHANC_PASS), data=f)
    
    if r.status_code == 200:
        return jsonify({"status": "ok", "file": filename, "studyUid": ds.StudyInstanceUID, "orthanc_id": r.json().get("ID")})
    else:
        return jsonify({"status": "error", "file": filename, "orthanc_response": r.text}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
