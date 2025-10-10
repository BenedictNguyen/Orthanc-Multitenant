FROM orthancteam/orthanc:latest-full

# Cài pip + các thư viện Python cần cho plugin
RUN apt-get update && \
    apt-get install -y python3-pip && \
    python3 -m pip install --no-cache-dir --break-system-packages \
        requests pydicom flask flask-cors && \
    rm -rf /var/lib/apt/lists/*

 # Verify Python plugin is available
RUN ls -la /usr/share/orthanc/plugins-disabled/ | grep Python || \
    ls -la /usr/share/orthanc/plugins/ | grep Python

# Create directory for custom Python scripts
RUN mkdir -p /etc/orthanc/python

EXPOSE 8042 4242