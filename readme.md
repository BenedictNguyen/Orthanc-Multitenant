# Orthanc DICOM Gateway

## 1\. Giới thiệu

Đây là một hệ thống PACS Gateway được xây dựng dựa trên Orthanc, sử dụng Docker và Docker Compose để triển khai. Hệ thống được thiết kế để hoạt động như một cổng tiếp nhận, phân loại và lưu trữ hình ảnh DICOM từ nhiều máy khác nhau (modalities) như CT, MR, X-Ray, US...

Mỗi modality sẽ có một Orthanc instance riêng biệt để đảm bảo sự tách biệt, hiệu năng và dễ dàng quản lý. Dữ liệu sau khi ổn định sẽ được tự động chuyển tiếp (auto-forward) đến một hệ thống PACS trung tâm.

Hệ thống cũng tích hợp sẵn bộ công cụ giám sát hiệu năng bằng Prometheus và Grafana.

Hệ thống cũng tích hợp bộ giám sát trung tâm (Prometheus, Grafana, Loki, Promtail) và được cấu hình để gửi toàn bộ metrics và logs về datacenter qua kết nối VPN

Có sẵn bộ cài đặt VPN-client và VPN-server

## 2\. Kiến trúc hệ thống

Hệ thống bao gồm các thành phần chính sau:

  * **Orthanc Services**: 7 instances Orthanc riêng biệt, mỗi instance phục vụ cho một loại máy (modality) khác nhau (XR, BDM, US, CT, MR, XA) và một instance cho Worklist (WL).
  * **PostgreSQL Database**: Một cơ sở dữ liệu PostgreSQL trung tâm, nơi mỗi Orthanc instance sẽ lưu trữ index và cả dữ liệu file DICOM (storage).
  * **Prometheus**: Một service thu thập số liệu (metrics) từ tất cả các Orthanc instances để giám sát hiệu năng theo thời gian thực.
  * **Grafana**: Một service trực quan hóa dữ liệu, sử dụng nguồn dữ liệu từ Prometheus để hiển thị các biểu đồ về tình trạng hoạt động của hệ thống.

  * **Tự động chuyển tiếp**: Dữ liệu sau khi ổn định sẽ được tự động gửi đến PACS trung tâm (DATACENTER_PACS) thông qua script Lua.

  * **Giám sát tập trung**: Hệ thống được tích hợp sẵn bộ công cụ giám sát (Prometheus, Grafana, Loki, Promtail) và được cấu hình để gửi toàn bộ metrics và logs về Datacenter qua kết nối VPN.

## 3\. Chức năng chính

  * **Phân luồng theo Modality**: Mỗi loại máy (CT, MR,...) gửi hình ảnh tới một cổng DICOM và cổng HTTP riêng, được xử lý bởi một Orthanc instance riêng.
  * **Lưu trữ tập trung trên PostgreSQL**: Thay vì lưu trữ trên file system, toàn bộ dữ liệu DICOM và index được lưu trong PostgreSQL, giúp tăng cường hiệu suất và độ tin cậy.
  * **Tự động chuyển tiếp (Auto-Forwarding)**: Sử dụng Lua script (`autoforward.lua`) để tự động gửi các ca chụp (Series) đã ổn định tới một PACS trung tâm được định nghĩa trong file `.env`.
  * **Worklist Server**: Cung cấp một service Orthanc (`orthanc-wl`) chuyên cho chức năng DICOM Worklist.
  * **Giám sát hiệu năng (Monitoring)**: Tích hợp sẵn Prometheus và Grafana để theo dõi các chỉ số quan trọng như số lượng bệnh nhân/ca chụp, dung lượng lưu trữ, tài nguyên hệ thống....
  * **Giám sát tập trung**: Hệ thống được tích hợp sẵn bộ công cụ giám sát (Prometheus, Grafana, Loki, Promtail) và được cấu hình để gửi toàn bộ metrics và logs về Datacenter qua kết nối VPN.
  * **Triển khai dễ dàng**: Toàn bộ hệ thống được đóng gói bằng Docker, chỉ cần vài lệnh để khởi chạy.

## 4\. Yêu cầu hệ thống

  * **Docker và Docker Compose** đã được cài đặt.
    * **Docker**: Phiên bản 20.10.x hoặc mới hơn.
    * **Docker Compose**: Phiên bản v2.x hoặc mới hơn.
  * **Hệ thống vpn-client** đã được cài đặt, cấu hình và đang chạy ổn định trên cùng máy chủ hoặc trong cùng mạng.
  * **Nhận được địa chỉ IP VPN** của các máy chủ giám sát tại Datacenter từ Central Admin.

## 5\. Hướng dẫn Cài đặt và Vận hành

### Bước 1: Tải mã nguồn

Tải hoặc clone toàn bộ thư mục dự án về máy chủ của bạn.

### Bước 2: Cấu hình môi trường

Mở file `.env` và chỉnh sửa các thông số cho phù hợp với hệ thống PACS trung tâm của bạn. Đây là đích mà các Orthanc instance sẽ tự động chuyển tiếp dữ liệu đến.

**File: `.env`**

```env
# =================================================
# THONG TIN DO CENTRAL ADMIN CUNG CAP
# =================================================
# UUID duy nhất để định danh site này trên hệ thống trung tâm
SITE_UUID="123e4567-e89b-12d3-a456-426614174000"

# =================================================
# THONG TIN MO TA CHO SITE (Site Admin tự điền)
# =================================================
SITE_NAME="BenhVienChoRay"
SITE_LOCATION="TPHCM"

# =================================================
# CAU HINH MAT KHAU VA PACS TRUNG TAM
# =================================================
POSTGRES_PASSWORD=your_secure_password
ORTHANC_DB_PASSWORD=your_secure_password
GRAFANA_ADMIN_PASSWORD=your_secure_password

# Thong tin PACS trung tam để forward dữ liệu DICOM
DATACENTER_AET=DATACENTER_PACS
DATACENTER_IP=192.168.1.100
DATACENTER_PORT=11112
```

### Bước 3: Cấu hình mật khẩu (Tùy chọn nhưng khuyến khích)

Trong file `docker-compose.yml`, các mật khẩu đang được để trống và sử dụng giá trị mặc định (`orthanc`, `admin`). Để tăng cường bảo mật, bạn nên tạo thêm các biến môi trường hoặc thay đổi trực tiếp:

  * `POSTGRES_PASSWORD`: Mật khẩu cho user `orthanc` của PostgreSQL.
  * `ORTHANC_DB_PASSWORD`: Mật khẩu mà Orthanc dùng để kết nối tới PostgreSQL.
  * `GRAFANA_ADMIN_PASSWORD`: Mật khẩu cho user `admin` của Grafana.

### Bước 4: Cấu hình địa chỉ gửi dữ liệu logs
Bạn cần cập nhật địa chỉ IP của các máy chủ trung tâm (thông qua mạng VPN).

## Prometheus (prometheus.yml):
Mở file prometheus.yml, tìm đến phần remote_write và thay thế <IP_VPN_CUA_PROMETHEUS_TRUNG_TAM> bằng địa chỉ IP VPN của máy chủ Prometheus tại Datacenter.
```
remote_write:
  - url: "http://<IP_VPN_CUA_PROMETHEUS_TRUNG_TAM>:9090/api/v1/write"
```

## Promtail (promtail-config.yml):
Mở file promtail-config.yml, tìm đến phần clients và thay thế <IP_VPN_CUA_LOKI_TRUNG_TAM> bằng địa chỉ IP VPN của máy chủ Loki tại Datacenter.
```
clients:
  # ...
  - url: "http://<IP_VPN_CUA_LOKI_TRUNG_TAM>:3100/loki/api/v1/push"
```

### Bước 4: Khởi chạy hệ thống

Mở terminal hoặc PowerShell tại thư mục gốc của dự án và chạy lệnh sau:

```bash
docker-compose up -d
```

Lệnh này sẽ tự động build và khởi chạy tất cả các services ở chế độ nền (detached mode).

### Bước 5: Kiểm tra trạng thái

Sử dụng lệnh sau để kiểm tra xem tất cả các container đã khởi động và đang ở trạng thái "running" hay "healthy" chưa:

```bash
docker-compose ps
```

## 6\. Truy cập các Services

Sau khi khởi động thành công, bạn có thể truy cập các giao diện web của hệ thống qua các cổng sau:

| Service | URL truy cập | AET | Cổng DICOM | Ghi chú |
| :--- | :--- | :--- | :--- | :--- |
| **Orthanc Worklist** | `http://<địa-chỉ-ip>:8043` | `ORTHANC_WL` | `4243` | Cung cấp DICOM Worklist. |
| **Orthanc XR** | `http://<địa-chỉ-ip>:8044` | `ORTHANC_XR` | `4244` | Gateway cho máy X-Ray. |
| **Orthanc BDM** | `http://<địa-chỉ-ip>:8045` | `ORTHANC_BDM` | `4245` | Gateway cho máy Bone Density. |
| **Orthanc US** | `http://<địa-chỉ-ip>:8046` | `ORTHANC_US` | `4246` | Gateway cho máy siêu âm. |
| **Orthanc CT** | `http://<địa-chỉ-ip>:8047` | `ORTHANC_CT` | `4247` | Gateway cho máy CT. |
| **Orthanc MR** | `http://<địa-chỉ-ip>:8048` | `ORTHANC_MR` | `4248` | Gateway cho máy MRI. |
| **Orthanc XA** | `http://<địa-chỉ-ip>:8049` | `ORTHANC_XA` | `4249` | Gateway cho máy Angiography. |
| **Prometheus** | `http://<địa-chỉ-ip>:9090` | - | - | Giao diện giám sát metrics. |
| **Grafana** (local) | `http://<địa-chỉ-ip>:3000` | - | - | Giao diện biểu đồ. Login: `admin`/`admin` (mặc định, mật khẩu trong file .env). |

## 7\. Chi tiết về các Services và Cấu hình

  * **`postgres`**:

      * Sử dụng image `postgres:15-alpine`.
      * Dữ liệu được lưu tại `./data/postgres-db`.
      * Khi khởi động lần đầu, sẽ tự động chạy file `init-db.sql` để tạo các database riêng cho từng instance Orthanc.

  * **`orthanc-*` (các services Orthanc)**:

      * Sử dụng image `orthancteam/orthanc:latest-full`.
      * Mỗi instance được cấu hình để kết nối tới một database riêng trên PostgreSQL.
      * Sử dụng chung file cấu hình `config/orthanc.json` và `lua/autoforward.lua` (trừ `orthanc-wl`).
      * `orthanc-wl` có file cấu hình riêng `config/orthanc-wl.json` để bật chức năng Worklist.

  * **`prometheus`**:

      * Được cấu hình qua file `config/prometheus.yml`.
      * Tự động thu thập metrics từ tất cả 7 services Orthanc tại endpoint `/tools/metrics-prometheus`.

  * **`grafana`**:

      * Dữ liệu (dashboards, settings) được lưu trong volume `grafana-storage`.
      * Tự động nhận cấu hình data source từ file `grafana/provisioning/datasources/prometheus-ds.yml` để kết nối tới Prometheus.

## 8\. Dừng hệ thống

Để dừng toàn bộ hệ thống, chạy lệnh sau tại thư mục gốc:

```bash
docker-compose down
```

Nếu bạn muốn xóa cả volume dữ liệu của Grafana và các container đã dừng, sử dụng:

```bash
docker-compose down -v
```
