/*
 * =====================================================================================
 * ESP32-S3 WROOM N16R8 CAM - POSTURE MONITORING CLIENT (OV2640)
 * BẢN TỐI ƯU TRUYỀN HÌNH ẢNH: HTTP KEEP-ALIVE + TĂNG TỐC ĐỘ KHUNG HÌNH (FPS)
 *
 * Các cải tiến tăng tốc:
 *  1. Tái sử dụng kết nối HTTPS (Keep-Alive): Bỏ qua bước bắt tay TLS/SSL sau lần đầu,
 *     giúp thời gian POST giảm từ ~1000ms xuống chỉ còn ~150 - 250ms!
 *  2. Giảm chu kỳ nghỉ UPLOAD_INTERVAL_MS xuống 200ms để tăng số FPS mượt mà.
 *  3. Điều chỉnh JPEG Quality = 14 (ảnh nhẹ ~10KB) giúp truyền qua mạng Internet cực nhanh.
 *  4. Cân bằng lại độ sáng/tương phản để giảm hiện tượng cháy sáng ám hồng của OV2640.
 * =====================================================================================
 */

#include <Arduino.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <WebServer.h>
#include "esp_camera.h"

// WebServer cục bộ để phục vụ nút bấm test loa từ trình duyệt điện thoại / PC
WebServer localServer(80);

// ================= 1. CẤU HÌNH WIFI =================
const char* WIFI_SSID = "Nha Dong Con";
const char* WIFI_PASS = "28121994";

// ================= 2. CẤU HÌNH SERVER CLOUD RENDER =================
const char* SERVER_URL = "https://ai-sitting-posture-1.onrender.com/api/upload";

// Chu kỳ nghỉ giữa 2 lần gửi (200ms giúp tốc độ đạt ~3 - 4 FPS mượt mà)
const unsigned long UPLOAD_INTERVAL_MS = 200;
const uint32_t      HTTP_TIMEOUT_MS    = 10000;  // timeout HTTP 10s
const int           MAX_FAIL_BEFORE_RECONNECT = 5;
const int           MAX_FAIL_BEFORE_RESTART   = 30;

unsigned long lastUploadTime = 0;
int failCount = 0;       // số lần gửi lỗi liên tiếp
int camFailCount = 0;    // số lần lấy frame lỗi liên tiếp

// Đối tượng mạng toàn cục để tái sử dụng kết nối (Keep-Alive)
WiFiClientSecure client;
HTTPClient http;
bool isHttpInitialized = false;

// ================= 3. ĐỊNH NGHĨA CHÂN VÀ GIAO TIẾP JQ6500-16P =================
// Giữ nguyên đúng các chân và lệnh từ bản test thực tế đã hoạt động ổn định
#define JQ6500_TX_PIN   41   // ESP32 GPIO41 (TX) --[1kΩ]--> JQ6500 RX (Chân 9)
#define JQ6500_RX_PIN   42   // ESP32 GPIO42 (RX) <---------- JQ6500 TX (Chân 10 - tùy chọn)
#define JQ_BAUD         9600
#define JQ_VOLUME       25   // Mức âm lượng chuẩn (0 - 30)

HardwareSerial JQSerial(1);

unsigned long lastAudioAlertTime = 0;
const unsigned long AUDIO_ALERT_COOLDOWN_MS = 6000; // Khoảng cách giữa 2 lần nhắc loa tối thiểu 6s

// Khung lệnh chuẩn JQ6500: 7E | len | cmd | [params] | EF (len = 2 + số byte tham số)
void jqSend(uint8_t cmd, const uint8_t* params = nullptr, uint8_t paramLen = 0) {
  JQSerial.write((uint8_t)0x7E);
  JQSerial.write((uint8_t)(2 + paramLen));
  JQSerial.write(cmd);
  for (uint8_t i = 0; i < paramLen; i++) JQSerial.write(params[i]);
  JQSerial.write((uint8_t)0xEF);
}

void jqSetSource(uint8_t src)  { jqSend(0x09, &src, 1); }   // 0x04 = Flash onboard của module
void jqSetVolume(uint8_t vol)  { if (vol > 30) vol = 30; jqSend(0x06, &vol, 1); }
void jqSetLoopMode(uint8_t m)  { jqSend(0x11, &m, 1); }     // 4 = ONE_STOP (phát xong 1 bài là dừng hẳn)

void jqPlayTrack(uint16_t n) {
  uint8_t p[2] = { (uint8_t)(n >> 8), (uint8_t)(n & 0xFF) };
  jqSend(0x03, p, 2);
}

void jqStop() {
  uint8_t stopCmd[] = {0x7E, 0x02, 0x0E, 0xEF};
  JQSerial.write(stopCmd, sizeof(stopCmd));
}

void jq6500_init() {
  JQSerial.begin(JQ_BAUD, SERIAL_8N1, JQ6500_RX_PIN, JQ6500_TX_PIN);
  delay(1000);               // Chờ JQ6500 khởi động ổn định
  jqSetSource(0x04);         // Chọn bộ nhớ Flash
  delay(100);
  jqSetVolume(JQ_VOLUME);    // Đặt âm lượng 25/30
  delay(100);
  jqSetLoopMode(4);          // Đặt chế độ ONE_STOP: phát xong 1 file thì dừng ngay, không tự phát tiếp
  delay(100);
  Serial.println("[JQ6500] Khởi tạo mô-đun âm thanh thành công (GPIO41 TX, GPIO42 RX, ONE_STOP)!");
}

// ================= 4. ĐỊNH NGHĨA CHÂN CAMERA ESP32-S3 WROOM CAM =================
#define PWDN_GPIO_NUM     -1
#define RESET_GPIO_NUM    -1
#define XCLK_GPIO_NUM     15
#define SIOD_GPIO_NUM      4
#define SIOC_GPIO_NUM      5

#define Y9_GPIO_NUM       16
#define Y8_GPIO_NUM       17
#define Y7_GPIO_NUM       18
#define Y6_GPIO_NUM       12
#define Y5_GPIO_NUM       10
#define Y4_GPIO_NUM        8
#define Y3_GPIO_NUM        9
#define Y2_GPIO_NUM       11
#define VSYNC_GPIO_NUM     6
#define HREF_GPIO_NUM      7
#define PCLK_GPIO_NUM     13

// ================= 4. KHỞI TẠO CAMERA =================
bool initCamera() {
  camera_config_t config;
  memset(&config, 0, sizeof(config));

  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer   = LEDC_TIMER_0;
  config.pin_d0       = Y2_GPIO_NUM;
  config.pin_d1       = Y3_GPIO_NUM;
  config.pin_d2       = Y4_GPIO_NUM;
  config.pin_d3       = Y5_GPIO_NUM;
  config.pin_d4       = Y6_GPIO_NUM;
  config.pin_d5       = Y7_GPIO_NUM;
  config.pin_d6       = Y8_GPIO_NUM;
  config.pin_d7       = Y9_GPIO_NUM;
  config.pin_xclk     = XCLK_GPIO_NUM;
  config.pin_pclk     = PCLK_GPIO_NUM;
  config.pin_vsync    = VSYNC_GPIO_NUM;
  config.pin_href     = HREF_GPIO_NUM;
  config.pin_sccb_sda = SIOD_GPIO_NUM;
  config.pin_sccb_scl = SIOC_GPIO_NUM;
  config.pin_pwdn     = PWDN_GPIO_NUM;
  config.pin_reset    = RESET_GPIO_NUM;
  config.xclk_freq_hz = 10000000;            // 10MHz ổn định, không bị tràn buffer
  config.pixel_format = PIXFORMAT_JPEG;

  if (psramFound()) {
    config.frame_size   = FRAMESIZE_VGA;     // 640x480
    config.jpeg_quality = 14;                // 14: dung lượng nhẹ ~10KB, truyền cực nhanh
    config.fb_count     = 2;                 // 2 buffer: chống tràn buffer
    config.fb_location  = CAMERA_FB_IN_PSRAM;
    config.grab_mode    = CAMERA_GRAB_LATEST; // luôn lấy khung hình mới nhất
    Serial.println("[CAMERA] Chế độ PSRAM: VGA (640x480), Quality 14, 2 buffer, GRAB_LATEST");
  } else {
    config.frame_size   = FRAMESIZE_QVGA;    // 320x240
    config.jpeg_quality = 15;
    config.fb_count     = 1;
    config.fb_location  = CAMERA_FB_IN_DRAM;
    config.grab_mode    = CAMERA_GRAB_WHEN_EMPTY;
    Serial.println("[CAMERA] CẢNH BÁO: Không có PSRAM -> Dùng QVGA");
  }

  esp_err_t err = ESP_FAIL;
  for (int attempt = 1; attempt <= 3; attempt++) {
    err = esp_camera_init(&config);
    if (err == ESP_OK) break;
    Serial.printf("[CAMERA] Khởi tạo lần %d thất bại, mã lỗi: 0x%x\n", attempt, err);
    esp_camera_deinit();
    delay(500);
  }
  if (err != ESP_OK) return false;

  Serial.println("[CAMERA] Khởi tạo Camera OV2640 thành công!");

  // Tinh chỉnh cảm biến OV2640 (khắc phục màu ám hồng và tối ưu phơi sáng)
  sensor_t* s = esp_camera_sensor_get();
  if (s != NULL) {
    s->set_brightness(s, 0);     // Mức sáng cân bằng (tránh cháy sáng góc tường)
    s->set_contrast(s, 0);       // Độ tương phản chuẩn
    s->set_saturation(s, 0);     // Độ bão hòa màu bình thường
    s->set_special_effect(s, 0);
    s->set_whitebal(s, 1);       // Bật tự động cân bằng trắng
    s->set_awb_gain(s, 1);       // AWB Gain
    s->set_wb_mode(s, 0);        // Auto WB
    s->set_exposure_ctrl(s, 1);  // Auto Exposure
    s->set_aec2(s, 1);
    s->set_gain_ctrl(s, 1);
    s->set_bpc(s, 1);
    s->set_wpc(s, 1);
    s->set_raw_gma(s, 1);
    s->set_lenc(s, 1);           // Chống méo viền
    s->set_hmirror(s, 0);
    s->set_vflip(s, 0);
    Serial.println("[CAMERA] Đã tinh chỉnh thông số cảm biến tối ưu.");
  }
  return true;
}

// ================= 5. WIFI =================
void connectWiFi() {
  Serial.printf("[WIFI] Đang kết nối tới: %s", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);          // Tắt WiFi modem sleep để gửi gói tin liên tục không trễ
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASS);

  int attempts = 0;
  while (WiFi.status() != WL_CONNECTED && attempts < 40) {
    delay(500);
    Serial.print(".");
    attempts++;
  }

  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("\n[WIFI] Kết nối thành công!");
    Serial.printf("[WIFI] IP: %s | RSSI: %d dBm\n", WiFi.localIP().toString().c_str(), WiFi.RSSI());
    
    // Cấu hình trước SSL Client
    client.setInsecure();
    client.setTimeout(HTTP_TIMEOUT_MS / 1000);
  } else {
    Serial.println("\n[WIFI] Kết nối thất bại! Sẽ tự thử lại trong loop().");
  }
}

// ================= 6. TRÍCH XUẤT JSON ĐƠN GIẢN =================
String extractJsonString(const String& json, const String& key) {
  String searchKey = "\"" + key + "\":\"";
  int startIdx = json.indexOf(searchKey);
  if (startIdx == -1) return "";
  startIdx += searchKey.length();
  int endIdx = json.indexOf("\"", startIdx);
  if (endIdx == -1) return "";
  return json.substring(startIdx, endIdx);
}

int extractJsonInt(const String& json, const String& key) {
  String searchKey = "\"" + key + "\":";
  int startIdx = json.indexOf(searchKey);
  if (startIdx == -1) return 0;
  startIdx += searchKey.length();
  while (startIdx < (int)json.length() && (json[startIdx] == ' ' || json[startIdx] == '\"')) startIdx++;
  int endIdx = startIdx;
  while (endIdx < (int)json.length() && (isDigit(json[endIdx]) || json[endIdx] == '-')) endIdx++;
  return json.substring(startIdx, endIdx).toInt();
}

void onPostureAlert(int track, int neck, int torso, const String& status) {
  unsigned long now = millis();
  if (now - lastAudioAlertTime < AUDIO_ALERT_COOLDOWN_MS) {
    return; // Chưa hết thời gian chờ giữa 2 lần phát cảnh báo (tránh chồng âm thanh)
  }
  lastAudioAlertTime = now;

  // Đảm bảo track hợp lệ từ 1 đến 4
  if (track < 1 || track > 4) {
    if (neck >= 32 && torso >= 18) track = 3;
    else if (torso >= 18) track = 2;
    else track = 1;
  }

  Serial.printf(">>> [CẢNH BÁO LOA JQ6500] Phát Track %d (00%d.mp3) | Cổ: %d°, Lưng: %d° (%s) <<<\n",
                track, track, neck, torso, status.c_str());
  jqPlayTrack(track);
}

// ================= 7. CHỤP VÀ GỬI ẢNH TỐI ƯU KEEP-ALIVE =================
void sendFrameToCloud() {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[WIFI] Mất mạng, đang thử kết nối lại...");
    WiFi.reconnect();
    isHttpInitialized = false;
    return;
  }

  // Lấy ngay khung hình mới nhất từ PSRAM
  camera_fb_t* fb = esp_camera_fb_get();
  if (!fb) {
    camFailCount++;
    Serial.printf("[CAMERA] Không thể lấy khung hình! (%d lần liên tiếp)\n", camFailCount);
    if (camFailCount >= 5) {
      Serial.println("[CAMERA] Lỗi liên tiếp -> khởi động lại thiết bị...");
      delay(500);
      ESP.restart();
    }
    return;
  }
  camFailCount = 0;

  if (fb->format != PIXFORMAT_JPEG || fb->len == 0) {
    esp_camera_fb_return(fb);
    return;
  }

  // Tái sử dụng kết nối (Keep-Alive): Chỉ khởi tạo lại khi socket bị ngắt
  if (!isHttpInitialized || !client.connected()) {
    http.end();
    client.stop();
    client.setInsecure();
    client.setTimeout(HTTP_TIMEOUT_MS / 1000);
    http.setTimeout(HTTP_TIMEOUT_MS);
    http.setReuse(true); // BẬT KEEP-ALIVE

    if (http.begin(client, SERVER_URL)) {
      isHttpInitialized = true;
    } else {
      Serial.println("[HTTP] Không thể kết nối tới server Render!");
      esp_camera_fb_return(fb);
      return;
    }
  }

  // Thiết lập Headers cho mỗi lượt POST
  http.addHeader("Content-Type", "image/jpeg");
  http.addHeader("User-Agent", "ESP32-S3-WROOM-CAM");
  http.addHeader("Connection", "keep-alive");

  unsigned long startPost = millis();
  int httpCode = http.POST(fb->buf, fb->len);
  unsigned long duration = millis() - startPost;

  bool ok = false;

  if (httpCode == 200) {
    ok = true;
    String response = http.getString(); // Bắt buộc đọc hết response để socket sẵn sàng cho frame tiếp theo
    Serial.printf("[POST OK %lums | %u bytes] %s\n", duration, (unsigned)fb->len, response.c_str());

    String status = extractJsonString(response, "status");
    int neck         = extractJsonInt(response, "neck_angle");
    int torso        = extractJsonInt(response, "torso_angle");
    int posture_time = extractJsonInt(response, "posture_time");
    int track        = extractJsonInt(response, "track");
    bool alert = (response.indexOf("\"alert\":true") != -1 || response.indexOf("\"alert\": true") != -1);

    Serial.printf(">> [%s] Cổ: %d° | Lưng: %d° | Duy trì: %ds | Track: %d | Alert: %s\n",
                  status.c_str(), neck, torso, posture_time, track, alert ? "TRUE" : "false");

    if (alert) onPostureAlert(track, neck, torso, status);
  } else {
    Serial.printf("[POST LỖI] HTTP Code: %d (%s) - mất %lums\n",
                  httpCode, HTTPClient::errorToString(httpCode).c_str(), duration);
    // Nếu kết nối lỗi hoặc server đóng socket -> Đánh dấu để lần tới kết nối lại
    http.end();
    client.stop();
    isHttpInitialized = false;
  }

  // Giải phóng buffer ảnh ngay lập tức
  esp_camera_fb_return(fb);

  // Tự phục hồi khi lỗi mạng liên tiếp
  if (ok) {
    failCount = 0;
  } else {
    failCount++;
    if (failCount == MAX_FAIL_BEFORE_RECONNECT) {
      Serial.println("[RECOVER] Lỗi liên tiếp -> reset WiFi...");
      WiFi.disconnect();
      delay(200);
      WiFi.reconnect();
      isHttpInitialized = false;
    } else if (failCount >= MAX_FAIL_BEFORE_RESTART) {
      Serial.println("[RECOVER] Lỗi quá nhiều -> khởi động lại thiết bị...");
      delay(500);
      ESP.restart();
    }
  }
}

// ================= 8. SETUP / LOOP =================
void setup() {
  Serial.begin(115200);
  delay(1000);
  Serial.println("\n\n========================================================");
  Serial.println("  ESP32-S3 WROOM N16R8 CAM - POSTURE MONITORING CLIENT  ");
  Serial.println("         (BẢN TỐI ƯU TỐC ĐỘ HTTP KEEP-ALIVE)            ");
  Serial.println("========================================================");
  Serial.printf("[SYS] PSRAM: %s (%u bytes) | Free heap: %u\n",
                psramFound() ? "OK" : "KHONG CO",
                (unsigned)ESP.getPsramSize(), (unsigned)ESP.getFreeHeap());

  // Kết nối WiFi trước
  connectWiFi();

  // Khởi động Camera
  if (!initCamera()) {
    Serial.println("[CAMERA] Khởi tạo camera thất bại. Khởi động lại sau 5s...");
    delay(5000);
    ESP.restart();
  }

  // Khởi tạo mô-đun âm thanh JQ6500-16P (đặt âm lượng sẵn sàng)
  jq6500_init();

  // Cấu hình WebServer cục bộ để test loa qua trình duyệt
  localServer.on("/", []() {
    String html = "<!DOCTYPE html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>"
                  "<title>Test Loa JQ6500 (GPIO 41/42)</title>"
                  "<style>body{font-family:sans-serif;text-align:center;padding:25px;background:#0f172a;color:#f8fafc;}"
                  "h2{margin-bottom:8px;}p.sub{color:#94a3b8;margin-bottom:20px;font-size:14px;}"
                  "button{width:90%;max-width:340px;padding:14px;margin:8px auto;display:block;font-size:16px;font-weight:bold;border:none;border-radius:10px;cursor:pointer;color:white;box-shadow:0 4px 6px rgba(0,0,0,0.3);transition:0.2s;}"
                  "button:active{transform:scale(0.98);}"
                  ".btn1{background:#059669;}.btn2{background:#2563eb;}.btn3{background:#d97706;}.btn4{background:#7c3aed;}.btn-stop{background:#dc2626;}</style></head>"
                  "<body><h2>🔊 BẢNG TEST LOA JQ6500-16P</h2>"
                  "<p class='sub'>Phần cứng: ESP32-S3 TX=GPIO41, RX=GPIO42</p>"
                  "<button class='btn1' onclick=\"fetch('/play?track=1')\">📢 Track 1: Cúi cổ quá sâu (001.mp3)</button>"
                  "<button class='btn2' onclick=\"fetch('/play?track=2')\">📢 Track 2: Gù lưng / đổ trước (002.mp3)</button>"
                  "<button class='btn3' onclick=\"fetch('/play?track=3')\">📢 Track 3: Sai tư thế phối hợp (003.mp3)</button>"
                  "<button class='btn4' onclick=\"fetch('/play?track=4')\">📢 Track 4: Tư thế 4 / Sẵn sàng (004.mp3)</button>"
                  "<button class='btn-stop' onclick=\"fetch('/stop')\">⏹️ Dừng phát âm thanh</button>"
                  "<p style='color:#94a3b8;font-size:13px;margin-top:15px;'>Hoặc gõ phím <b>1, 2, 3, 4</b> trong Serial Monitor (115200) để test.</p>"
                  "</body></html>";
    localServer.send(200, "text/html", html);
  });

  localServer.on("/play", []() {
    int track = 1;
    if (localServer.hasArg("track")) {
      track = localServer.arg("track").toInt();
    }
    if (track < 1 || track > 4) track = 1;
    Serial.printf("[WEB TEST] Nhận lệnh từ Web: Phát Track %d\n", track);
    jqPlayTrack(track);
    localServer.send(200, "text/plain", "OK: Dang phat Track " + String(track));
  });

  localServer.on("/stop", []() {
    Serial.println("[WEB TEST] Nhận lệnh từ Web: Dừng phát");
    jqStop();
    localServer.send(200, "text/plain", "OK: Da dung phat");
  });

  localServer.begin();
  Serial.printf("[WEB TEST] Khởi động trang test loa tại: http://%s/\n", WiFi.localIP().toString().c_str());

  // TỰ ĐỘNG PHÁT THỬ NGAY KHI KHỞI ĐỘNG ĐỂ BÁO HIỆU PHẦN CỨNG SẴN SÀNG
  Serial.println("\n========================================================");
  Serial.println(">>> [AUTO TEST] Đang tự động phát thử Track 1 (001.mp3) <<<");
  Serial.println(">>> Phần cứng: ESP32 GPIO 41 (TX) -> [1kΩ] -> JQ6500 RX <<<");
  Serial.println("========================================================\n");
  delay(1000);
  jqPlayTrack(1);
}

void loop() {
  // Lắng nghe yêu cầu từ trang Web Test loa
  localServer.handleClient();

  // Lắng nghe phím bấm từ Serial Monitor để test loa
  if (Serial.available() > 0) {
    char c = Serial.read();
    if (c == '1') {
      Serial.println("\n>>> [SERIAL TEST] Gửi lệnh phát Track 1 (001.mp3) <<<");
      jqPlayTrack(1);
    } else if (c == '2') {
      Serial.println("\n>>> [SERIAL TEST] Gửi lệnh phát Track 2 (002.mp3) <<<");
      jqPlayTrack(2);
    } else if (c == '3') {
      Serial.println("\n>>> [SERIAL TEST] Gửi lệnh phát Track 3 (003.mp3) <<<");
      jqPlayTrack(3);
    } else if (c == '4') {
      Serial.println("\n>>> [SERIAL TEST] Gửi lệnh phát Track 4 (004.mp3) <<<");
      jqPlayTrack(4);
    } else if (c == 's' || c == 'S') {
      Serial.println("\n>>> [SERIAL TEST] Dừng phát âm thanh <<<");
      jqStop();
    } else if (c == '+' || c == 'v' || c == 'V') {
      Serial.println("\n>>> [SERIAL TEST] Đặt âm lượng tối đa (30/30) <<<");
      jqSetVolume(30);
    }
  }

  unsigned long now = millis();
  if (now - lastUploadTime >= UPLOAD_INTERVAL_MS) {
    sendFrameToCloud();
    lastUploadTime = millis(); // Cập nhật sau khi gửi xong để luôn giãn cách đúng 200ms
  }
  delay(5);
}
