# ============================
# Script: test_upload.ps1
# Purpose:
#   - Ask for an AccessionNumber (must match the order on Telerad)
#   - Edit the DICOM file
#   - Decompress image if needed
#   - Upload to Orthanc Gateway
# ============================

# --- Default Configuration ---
$DicomToolsPath = "C:\Mysoftware\dcmtk\bin"    # Path to dcmodify, storescu, dcmdjpeg
$DicomFolder = "E:\Dicomtest"        # Folder containing the test DICOM file
$SourceFile = "$DicomFolder\0002.dcm"
$ModifiedFile = "$DicomFolder\0002_modified.dcm"
$UncompressedFile = "$DicomFolder\0002_uncompressed.dcm"

$OrthancAET = "ORTHANC_MT"
$OrthancHost = "localhost"
$OrthancPort = 4243

# --- Ask for Accession Number ---
$AccessionNumber = Read-Host "Enter AccessionNumber (e.g. C3AN63272385)"
Write-Host "AccessionNumber entered: $AccessionNumber" -ForegroundColor Cyan

# --- Check source file existence ---
if (-not (Test-Path $SourceFile)) {
    Write-Host "Error: Source file not found: $SourceFile" -ForegroundColor Red
    exit
}

# --- Remove old files if exist ---
Remove-Item $ModifiedFile -ErrorAction SilentlyContinue
Remove-Item $UncompressedFile -ErrorAction SilentlyContinue

# --- Modify AccessionNumber ---
Write-Host "Modifying AccessionNumber..."
Copy-Item $SourceFile $ModifiedFile -Force

& "$DicomToolsPath\dcmodify.exe" -nb -i "(0008,0050)=$AccessionNumber" $ModifiedFile
if (-not (Test-Path $ModifiedFile)) {
    Write-Host "Error: Failed to modify DICOM file: $ModifiedFile" -ForegroundColor Red
    exit
}
Write-Host "Created modified file: $ModifiedFile"

# --- Decompress image (to avoid Store Failed errors) ---
Write-Host "Checking and decompressing JPEG (if needed)..."
& "$DicomToolsPath\dcmdjpeg.exe" $ModifiedFile $UncompressedFile

if (-not (Test-Path $UncompressedFile)) {
    Write-Host "Warning: Decompression failed, using modified file instead." -ForegroundColor Yellow
    $UncompressedFile = $ModifiedFile
} else {
    Write-Host "Decompressed file created: $UncompressedFile"
}

# --- Upload to Orthanc ---
Write-Host ("Uploading to Orthanc ({0}@{1}:{2})..." -f $OrthancAET, $OrthancHost, $OrthancPort)
& "$DicomToolsPath\storescu.exe" -v -aec $OrthancAET $OrthancHost $OrthancPort $UncompressedFile

Write-Host "Upload completed successfully!"
Write-Host "`nUse the following command to check Orthanc logs:"
Write-Host "docker logs -f orthanc-multitenant"