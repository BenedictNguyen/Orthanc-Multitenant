FROM orthancteam/orthanc:latest-full

# Cài pip + các thư viện Python cần cho plugin
RUN apt-get update && \
    apt-get install -y python3-pip && \
    python3 -m pip install --no-cache-dir --break-system-packages \
        requests pydicom flask flask-cors && \
    rm -rf /var/lib/apt/lists/*
