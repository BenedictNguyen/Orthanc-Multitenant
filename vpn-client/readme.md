# Hướng dẫn Cài đặt VPN Client (WireGuard)

Tài liệu này hướng dẫn cách cài đặt VPN Client tại site để kết nối về Datacenter.
Kết nối VPN này là bắt buộc để hệ thống Orthanc-gw có thể gửi dữ liệu giám sát (metrics và logs) về trung tâm.

## Quy trình Cài đặt

**Bước 1: Nhận "Gói thông tin khởi tạo" từ Central Admin**

Bạn sẽ nhận được các thông tin sau: `SITE_UUID`, `SITE_VPN_IP`, `VPN_SERVER_PUBLIC_KEY`, và `VPN_SERVER_ENDPOINT`.

**Bước 2: Tạo cặp khóa (Private & Public Key) cho Client**

Chạy lệnh sau trên máy chủ của bạn:
`wg genkey | tee client-privatekey | wg pubkey > client-publickey`

**Bước 3: Gửi Public Key về Trung tâm**

Sao chép nội dung file `client-publickey` và gửi lại cho Central Admin. **Chờ họ xác nhận đã cập nhật server trước khi tiếp tục.**

**Bước 4: Cấu hình file `config/wg0.conf`**

Dùng thông tin đã nhận để điền vào file này.
```ini
[Interface]
# 1. Dán địa chỉ IP VPN mà Central Admin đã cấp cho bạn.
Address = <YOUR_ASSIGNED_VPN_IP>

# 2. Dán nội dung file "client-privatekey" bạn đã tạo ở Bước 1.
PrivateKey = <PASTE_YOUR_CLIENT_PRIVATE_KEY_HERE>
DNS = 8.8.8.8

[Peer]
# 3. Dán Public Key của server trung tâm.
PublicKey = <PASTE_THE_VPN_SERVER_PUBLIC_KEY_HERE>

# 4. Dán địa chỉ endpoint của server trung tâm.
Endpoint = <PASTE_THE_VPN_SERVER_ENDPOINT_HERE>

# Định tuyến traffic trong mạng VPN qua tunnel.
AllowedIPs = 10.8.0.0/24
PersistentKeepalive = 25
```

**Bước 5: Cấu hình file `docker-compose.yml`**
```
services:
  wireguard:
    image: linuxserver/wireguard
    container_name: vpn-site-client
    cap_add:
      - NET_ADMIN
      - SYS_MODULE
    environment:
      - PUID=1000
      - PGID=1000
      - TZ=Asia/Ho_Chi_Minh
    volumes:
      - ./config:/config
      - /lib/modules:/lib/modules
    sysctls:
      - net.ipv4.ip_forward=1
    restart: unless-stopped
```
## Vận hành
**Khởi động Client**
Sau khi đã hoàn tất cấu hình, mở terminal tại thư mục vpn-client và chạy lệnh:
```
docker-compose up -d
```
**Kiểm tra kết nối**
Để kiểm tra trạng thái kết nối VPN, sử dụng lệnh:
```
docker exec vpn-site-client wg show
```
Nếu bạn thấy thông tin về "latest handshake", điều đó có nghĩa là kết nối đã thành công.
