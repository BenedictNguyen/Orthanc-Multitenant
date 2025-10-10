CREATE DATABASE "orthanc_db_wl";
GRANT ALL PRIVILEGES ON DATABASE "orthanc_db_wl" TO "orthanc";
CREATE DATABASE "orthanc_db_main";
GRANT ALL PRIVILEGES ON DATABASE "orthanc_db_main" TO "orthanc";

-- ============================
-- Database riêng cho RIS
-- ============================
CREATE DATABASE "ris_db";
GRANT ALL PRIVILEGES ON DATABASE "ris_db" TO "orthanc";

\c ris_db;

-- Bảng Orders để lưu lệnh chụp từ RIS UI
CREATE TABLE IF NOT EXISTS orders (
    id SERIAL PRIMARY KEY,
    accession_number VARCHAR(64) UNIQUE NOT NULL,
    patient_id VARCHAR(64) NOT NULL,
    patient_name VARCHAR(128) NOT NULL,
    modality VARCHAR(16) NOT NULL,
    study_description TEXT,
    scheduled_date DATE,
    scheduled_time TIME,
    status VARCHAR(32) DEFAULT 'SCHEDULED',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Index để tăng tốc query theo accession_number và patient_id
CREATE INDEX IF NOT EXISTS idx_orders_accession ON orders(accession_number);
CREATE INDEX IF NOT EXISTS idx_orders_patient ON orders(patient_id);