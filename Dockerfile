FROM orthancteam/orthanc:24.9.1-full-debian

# Cài pip và requests
RUN apt-get update && \
    apt-get install -y python3-pip && \
    pip3 install --no-cache-dir requests && \
    rm -rf /var/lib/apt/lists/*
