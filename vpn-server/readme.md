
# Hướng dẫn Cài đặt VPN Server (WireGuard)

## 1\. Giới thiệu

Tài liệu này hướng dẫn cách cài đặt và cấu hình máy chủ VPN trung tâm (Hub) sử dụng WireGuard. Máy chủ này sẽ hoạt động như một điểm cuối tập trung, cho phép các máy chủ tại các site (Spoke) kết nối vào để tạo thành một mạng riêng ảo an toàn.

Mục đích chính là để quản trị viên tại Datacenter có thể truy cập từ xa vào các máy chủ ở site để thực hiện các tác vụ bảo trì, giám sát và sửa lỗi.

-----

## 2\. Yêu cầu cần chuẩn bị

  * **Docker** và **Docker Compose** đã được cài đặt trên máy chủ.
  * Máy chủ có một **địa chỉ IP Public cố định** (hoặc một domain trỏ về IP đó).
  * **Mở port `51820/UDP`** trên tường lửa (firewall) của máy chủ và của nhà cung cấp mạng để cho phép lưu lượng WireGuard đi qua.

-----

## 3\. Các bước cài đặt và cấu hình

### Bước 1: Chuẩn bị cấu trúc thư mục

Tạo một thư mục tên là `vpn-server` trên máy chủ Datacenter với cấu trúc như sau:

```
vpn-server/
├── config/
│   └── wg0.conf
└── docker-compose.yml
```

Bạn có thể tạo nhanh bằng các lệnh sau:

```bash
mkdir -p vpn-server/config
touch vpn-server/docker-compose.yml vpn-server/config/wg0.conf
```

### Bước 2: Tạo cặp khóa (Private & Public Key) cho Server

Trên máy chủ, mở terminal và chạy lệnh sau để tạo một cặp khóa mới cho chính máy chủ VPN:

```bash
wg genkey | tee server-privatekey | wg pubkey > server-publickey
```

Lệnh này sẽ tạo ra hai file:

  * `server-privatekey`: Khóa bí mật của máy chủ. Bạn sẽ sử dụng nó trong file cấu hình.
  * `server-publickey`: Khóa công khai của máy chủ. Bạn sẽ cần gửi khóa này cho quản trị viên tại các site để họ cấu hình kết nối.

### Bước 3: Cấu hình file `config/wg0.conf`

Mở file `config/wg0.conf` và cập nhật nội dung theo mẫu dưới đây. File này là nơi định nghĩa thông số của server và quản lý tất cả các client sẽ kết nối vào.

```ini
[Interface]
# Địa chỉ IP của Server trong mạng VPN (luôn là .1)
Address = 10.8.0.1/24
ListenPort = 51820

# ---> THAY THẾ: Dán nội dung file "server-privatekey" bạn vừa tạo vào đây.
PrivateKey = <NOI_DUNG_FILE_SERVER_PRIVATEKEY>

# Các lệnh iptables để cho phép NAT traffic, giúp client có thể giao tiếp
PostUp = iptables -A FORWARD -i %i -j ACCEPT; iptables -t nat -A POSTROUTING -o eth0 -j MASQUERADE
PostDown = iptables -D FORWARD -i %i -j ACCEPT; iptables -t nat -D POSTROUTING -o eth0 -j MASQUERADE


# ======================================================================
# PHẦN QUẢN LÝ CLIENTS (PEERS)
# Mỗi [Peer] tương ứng với một client (một site) kết nối vào.
# ======================================================================

# --- MẪU: Client tại Site A ---
[Peer]
# ---> THAY THẾ: Dán Public Key của Client A vào đây.
PublicKey = <PUBLIC_KEY_CUA_CLIENT_A>

# Cấp một địa chỉ IP duy nhất cho Client A trong mạng VPN.
AllowedIPs = 10.8.0.2/32

# Giữ kết nối ổn định khi client đứng sau NAT.
PersistentKeepalive = 25


# --- MẪU: Client tại Site B ---
[Peer]
# ---> THAY THẾ: Dán Public Key của Client B vào đây.
PublicKey = <PUBLIC_KEY_CUA_CLIENT_B>

# Cấp một địa chỉ IP duy nhất cho Client B trong mạng VPN.
AllowedIPs = 10.8.0.3/32
PersistentKeepalive = 25

# ... Sao chép khối [Peer] ở trên để thêm các client mới ...
```

### Bước 4: Điền thông tin file `docker-compose.yml`

Sao chép nội dung dưới đây vào file `vpn-server/docker-compose.yml`.

```yaml
services:
  wireguard:
    image: linuxserver/wireguard
    container_name: vpn-central-server
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
    ports:
      - "51820:51820/udp"
    sysctls:
      - net.ipv4.ip_forward=1
    restart: unless-stopped
```

-----

## 4\. Vận hành

Sau khi đã hoàn tất các bước cấu hình, mở terminal tại thư mục `vpn-server` và chạy lệnh:

```bash
docker-compose up -d
```

Dịch vụ VPN Server sẽ khởi động và sẵn sàng chấp nhận kết nối từ các client.

-----

## 5\. Quản lý Client

### Thêm một Client mới

Để cho phép một site mới kết nối, bạn cần thực hiện:

1.  Nhận **Public Key** từ quản trị viên tại site đó.
2.  Mở file `config/wg0.conf` trên server.
3.  Sao chép một khối `[Peer]` có sẵn.
4.  Dán **Public Key** của client mới vào mục `PublicKey`.
5.  Cấp cho họ một địa chỉ `AllowedIPs` mới và duy nhất (ví dụ: `10.8.0.4/32`).
6.  Lưu file và khởi động lại container WireGuard để áp dụng thay đổi:
    ```bash
    docker-compose restart wireguard
    ```

### Kiểm tra kết nối

Để xem trạng thái hiện tại của VPN server và danh sách các client đang kết nối, sử dụng lệnh:

```bash
docker exec vpn-central-server wg show
```

Lệnh này sẽ hiển thị thông tin về các "peer" (client) đã kết nối, thời gian "handshake" cuối cùng, và lượng dữ liệu đã truyền.

-----

## 6\. Dừng dịch vụ

Để dừng VPN server, chạy lệnh:

```bash
docker-compose down
```