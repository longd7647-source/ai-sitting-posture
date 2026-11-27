# 📘 HƯỚNG DẪN CÀI ĐẶT & KẾT NỐI HỆ THỐNG
## Thiết Bị Theo Dõi Tư Thế Ngồi Độc Lập (ESP32-S3-CAM + JQ6500-16P + Python AI)

Tài liệu này hướng dẫn chi tiết cách đấu nối phần cứng, nạp Firmware cho **ESP32-S3-CAM**, cấu hình âm thanh cho mô-đun phát giọng nói **JQ6500-16P**, và khởi chạy **Server Python MediaPipe**.

---

### 1. SƠ ĐỒ ĐẤU NỐI PHẦN CỨNG (WIRING DIAGRAM)

#### 📌 Nhận diện mô-đun JQ6500-16P (Loại 16 chân DIP):
Đặt module nằm dọc trước mặt sao cho **CỔNG MICRO-USB HƯỚNG LÊN TRÊN**:
- Hàng chân bên trái (từ trên xuống dưới): Chân 1 (K1) $\rightarrow$ Chân 8 (BUSY)
- Hàng chân bên phải (từ dưới lên trên): Chân 9 (RX) $\rightarrow$ Chân 16 (SPK+)

```text
               +-----[ CỔNG MICRO-USB ]-----+
               |                            |
    (K1)   [1] |                            | [16]  SPK+  ---> Loa (+)
    (K2)   [2] |                            | [15]  SPK-  ---> Loa (-)
    (K3)   [3] |                            | [14]  ADC_L
    (K4)   [4] |        JQ6500-16P          | [13]  ADC_R
    (K5)   [5] |        VOICE MODULE        | [12]  VCC   <--- 5V ESP32 / Nguồn ngoài
  (SGND)   [6] |                            | [11]  GND   <--- GND ESP32
 (ADKEY)   [7] |                            | [10]  TX    ---> GPIO 42 (RX) ESP32
  (BUSY)   [8] |                            | [ 9]  RX    <--- Trở 1kΩ (hoặc nối thẳng) <--- GPIO 41 (TX) ESP32
               +----------------------------+
```

#### 🔌 Bảng kết nối chi tiết giữa ESP32-S3-CAM và JQ6500-16P (Bản chuẩn hoạt động):
| Chân ESP32-S3-CAM | Chân JQ6500-16P | Chân DFPlayer cũ (đối chiếu) | Chức năng / Hướng dẫn đấu nối |
| :--- | :--- | :--- | :--- |
| **5V** (hoặc nguồn ngoài 5V) | **Chân 12 (VCC)** | *Chân 1 (VCC)* | Dây Đỏ: Cấp nguồn 5V cho JQ6500 |
| **GND** | **Chân 11 (GND)** | *Chân 7 hoặc 10 (GND)* | Dây Đen: Nối chung mass (Ground) |
| **GPIO 41** (TX) | **Chân 9 (RX)** | *Chân 2 (RX)* | Dây Vàng: Truyền dữ liệu UART *(nối tiếp trở 1kΩ hoặc nối thẳng đều an toàn vì ESP32-S3 xuất mức logic 3.3V; khi debug tín hiệu UART yếu nên thử nối thẳng không qua trở)* |
| **GPIO 42** (RX) | **Chân 10 (TX)** | *Chân 3 (TX)* | Dây Xanh: Nhận phản hồi trạng thái từ JQ6500 *(nên nối để Serial Monitor đọc được gói phản hồi `[JQ6500 RX]`)* |
| *Không nối ESP32* | **Chân 16 (SPK+)** | *Chân 6 (SPK_1)* | Nối vào cực dương (+) của Loa 3W (4Ω hoặc 8Ω) |
| *Không nối ESP32* | **Chân 15 (SPK-)** | *Chân 8 (SPK_2)* | Nối vào cực âm (-) của Loa 3W |

> [!TIP]
> **Khuyến nghị nguồn:** Cả ESP32-S3-CAM và loa 3W đều tiêu thụ dòng khá lớn khi phát âm thanh và stream camera. Bạn nên dùng củ sạc điện thoại **5V - 2A** cấp nguồn qua cổng Type-C của ESP32-S3-CAM để đảm bảo video không bị chớp giật và âm thanh không bị rè hay sụt nguồn reset vi điều khiển.

---

### 2. NẠP FILE ÂM THANH CẢNH BÁO VÀO BỘ NHỚ FLASH JQ6500-16P

Mô-đun JQ6500-16P **không sử dụng thẻ nhớ MicroSD** mà lưu trực tiếp vào chip nhớ Flash 16Mbit (2MB) tích hợp sẵn:

1. Dùng cáp Micro-USB cắm trực tiếp module JQ6500-16P vào cổng USB của máy tính Windows.
2. Máy tính sẽ nhận diện JQ6500 như một ổ đĩa CD ảo tên là `CD Drive`.
3. Mở ổ đĩa đó, chạy file `MusicTool.exe` (hoặc phần mềm nạp âm thanh tích hợp).
4. Chuẩn bị 4 file âm thanh định dạng `.mp3` (đã có sẵn trong thư mục `templates/` của dự án):
   - `001.mp3`: *"Khởi động thành công"* -> Âm báo hệ thống khởi động & kết nối Wi-Fi sẵn sàng (Track 1)
   - `002.mp3`: *"Cảnh báo cổ"* -> Cúi gập cổ quá sâu / Forward Head Posture (Track 2)
   - `003.mp3`: *"Cảnh báo lưng"* -> Gù lưng / đổ thân người ra trước (Track 3)
   - `004.mp3`: *"Cảnh báo phối hợp"* -> Sai tư thế phối hợp cổ và lưng / lệch vai (Track 4)
5. Nhấn nút thêm file MP3 theo đúng thứ tự 1 (`001.mp3`), 2 (`002.mp3`), 3 (`003.mp3`), 4 (`004.mp3`) rồi bấm nút nạp (Copy to Flash).
6. Khi hoàn tất, rút cáp Micro-USB ra và cắm lại JQ6500 vào mạch với ESP32-S3.

> [!NOTE]
> **Mẹo kiểm tra loa độc lập:** Cấp nguồn 5V/GND cho JQ6500:
> - Nối tắt chân **1 (K1)** xuống **GND (Chân 11)**: Loa phát file `001.mp3`.
> - Nối tắt chân **2 (K2)** xuống **GND (Chân 11)**: Loa phát file `002.mp3`.
> - Nối tắt chân **3 (K3)** xuống **GND (Chân 11)**: Loa phát file `003.mp3`.
> - Nối tắt chân **4 (K4)** xuống **GND (Chân 11)**: Loa phát file `004.mp3`.

---

### 3. HƯỚNG DẪN NẠP CODE CHO ESP32-S3-CAM BẰNG ARDUINO IDE

1. **Mở Arduino IDE** và cài đặt gói board **esp32** (từ Espressif, phiên bản 2.0.14 hoặc 3.x).
2. Mở file mã nguồn:
   `Firmware_ESP32_S3_CAM/Firmware_ESP32_S3_CAM.ino`
3. Chỉnh sửa thông tin Wi-Fi ở đầu file:
   ```cpp
   const char* ssid     = "TÊN_WIFI_NHÀ_BẠN";
   const char* password = "MẬT_KHẨU_WIFI";
   ```
4. **Cài đặt thông số Board trong menu `Tools` của Arduino IDE:**
   - **Board:** `ESP32S3 Dev Module`
   - **PSRAM:** `OPI PSRAM` *(Cực kỳ quan trọng để camera chạy mượt và không bị thiếu RAM)*
   - **Flash Size:** `8MB` hoặc `16MB` (Tùy phiên bản module của bạn)
   - **Flash Mode:** `QIO 80MHz`
   - **USB CDC On Boot:** `Enabled`
   - **Upload Speed:** `921600`
   - **Port:** Chọn đúng cổng COM của mạch ESP32-S3.
5. Nhấn nút **Upload** để nạp code.
6. Mở **Serial Monitor** (tốc độ `115200 baud`). Khi ESP32 khởi động và kết nối Wi-Fi thành công, nó sẽ in ra địa chỉ IP:
   ```text
   ✅ [Wi-Fi] Kết nối THÀNH CÔNG!
   📡 ĐỊA CHỈ IP CỦA ESP32-S3: http://192.168.1.50
   📹 ĐƯỜNG DẪN VIDEO STREAM : http://192.168.1.50:81/stream
   🔊 ĐƯỜNG DẪN LỆNH LOA MP3 : http://192.168.1.50/play?track=1
   ```

---

### 4. CẤU HÌNH PYTHON SERVER VÀ KHỞI ĐỘNG

1. Mở file [Code/config.py](file:///c:/Users/GIGABYTE/Downloads/NhanDienTuTheNgoi-main/Code/config.py):
   - Cập nhật địa chỉ `ESP32_IP` bằng IP đã hiển thị trên Serial Monitor:
     ```python
     ESP32_IP = "192.168.1.50"  # <-- Thay bằng IP thực tế của ESP32-S3-CAM
     ```
2. Khởi động server xử lý AI:
   ```bash
   python run.py
   ```
   *(Hoặc: `python Code/app.py`)*

3. Mở trình duyệt web truy cập:
   `http://localhost:5000`

---

### 5. KIỂM TRA HỆ THỐNG VÀ CÁC CÁCH TEST LOA JQ6500

Mã nguồn đã tích hợp sẵn **3 cách test loa tức thì** từ ESP32-S3 và Server:

1. **Cách 1: Tự động phát khi cắm nguồn (Auto-Test):**
   - Mỗi lần ESP32-S3 khởi động xong (hoặc khi bấm nút Reset `EN`), ESP32 sẽ **tự động gửi lệnh phát thử Track 1 (`001.mp3`)** ra loa.
   - Giúp bạn biết ngay phần cứng, nguồn và dây nối (GPIO 41 & 42) đã thông suốt.

2. **Cách 2: Test bằng bàn phím qua Serial Monitor (Khuyên dùng khi cắm máy tính):**
   - Mở **Serial Monitor** trong Arduino IDE (tốc độ `115200 baud`).
   - Gõ phím `1` rồi nhấn Enter: Phát Track 1 (`001.mp3` - Khởi động thành công).
   - Gõ phím `2` rồi nhấn Enter: Phát Track 2 (`002.mp3` - Cúi cổ quá sâu).
   - Gõ phím `3` rồi nhấn Enter: Phát Track 3 (`003.mp3` - Gù lưng / đổ thân trước).
   - Gõ phím `4` rồi nhấn Enter: Phát Track 4 (`004.mp3` - Sai tư thế phối hợp / lệch vai).
   - Gõ phím `t` rồi nhấn Enter: Chạy chẩn đoán UART 2 chiều (hỏi số file Flash, âm lượng, trạng thái và thử phát Track 1).
   - Gõ phím `p` rồi nhấn Enter: Gửi lệnh Play hiện tại (`0x0D`) để thử tương thích module.
   - Gõ phím `+` hoặc `v` rồi nhấn Enter: Đặt âm lượng cực đại (30/30). Gõ `-`: Hạ âm lượng xuống 15/30 để kiểm tra sụt áp nguồn USB.
   - Gõ phím `s` rồi nhấn Enter: Dừng phát ngay lập tức.

3. **Cách 3: Bấm nút test từ điện thoại / máy tính qua Web (Khi cùng mạng Wi-Fi):**
   - Mở trình duyệt web truy cập trực tiếp vào IP của ESP32 (ví dụ: `http://192.168.1.50/`).
   - Màn hình sẽ hiện giao diện điều khiển với 4 nút bấm kiểm tra lần lượt từng file âm thanh:
     - `📢 Track 1: Khởi động thành công (001.mp3)`
     - `📢 Track 2: Cúi cổ quá sâu (002.mp3)`
     - `📢 Track 3: Gù lưng / đổ trước (003.mp3)`
     - `📢 Track 4: Sai tư thế phối hợp (004.mp3)`
     - `⏹️ Dừng phát âm thanh`

4. **Cơ chế cảnh báo tự động khi phát hiện ngồi sai tư thế:**
   - Server AI liên tục phân tích góc cổ và lưng, tự động chọn mã bài hát phù hợp gửi về cho ESP32:
     - Khởi động mạch / sẵn sàng $\rightarrow$ Loa tự phát Track 1 (`001.mp3`).
     - Cúi gập cổ ($\ge 32^\circ$, hoặc góc trực diện $\ge 14^\circ$) $\rightarrow$ Loa tự phát Track 2 (`002.mp3`).
     - Gù lưng / đổ thân trước ($\ge 18^\circ$) $\rightarrow$ Loa tự phát Track 3 (`003.mp3`).
     - Sai phối hợp cả cổ và lưng hoặc ngồi lệch vai / nghiêng người $\rightarrow$ Loa tự phát Track 4 (`004.mp3`).
   - Có thời gian giãn cách 6 giây giữa 2 lần cảnh báo để tránh âm thanh bị ngắt quãng hoặc nói chồng chéo.
