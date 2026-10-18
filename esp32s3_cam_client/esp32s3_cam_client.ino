/*
 * =====================================================================================
 * ESP32-S3 WROOM N16R8 CAM - POSTURE MONITORING CLIENT (OV2640)
 *
 * Chức năng:
 *  1. Khởi động Camera OV2640 với 8MB PSRAM (chân chuẩn ESP32-S3 WROOM CAM).
 *  2. Tinh chỉnh độ sáng, tương phản và độ nét cho cảm biến OV2640.
 *  3. Định kỳ chụp ảnh JPEG 640x480 (VGA).
 *  4. Gửi ảnh qua HTTPS POST lên Server Flask Render.com (/api/upload).
 *  5. Nhận phản hồi JSON (neck_angle, torso_angle, alert, posture_time).
 *  6. In kết quả trực quan ra Serial Monitor và sẵn sàng nối còi/loa khi cần.
 * =====================================================================================
 */

#include <Arduino.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include "esp_camera.h"

// ================= 1. CẤU HÌNH WIFI =================
const char* WIFI_SSID = "TEN_WIFI_CUA_BAN";
const char* WIFI_PASS = "MAT_KHAU_WIFI";

// ================= 2. CẤU HÌNH SERVER CLOUD RENDER =================
// Thay thế bằng domain Render chính xác của bạn
const char* SERVER_URL = "https://ai-sitting-posture-1.onrender.com/api/upload";

// Chu kỳ chụp và gửi ảnh (1200ms - 1500ms là tối ưu nhất cho mạng Internet)
const unsigned long UPLOAD_INTERVAL_MS = 1200;
unsigned long lastUploadTime = 0;

// ================= 3. ĐỊNH NGHĨA CHÂN CAMERA ESP32-S3 WROOM CAM =================
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

// LED báo hiệu trạng thái (nếu bo mạch có LED tích hợp ở GPIO 21 hoặc 38, tuỳ bo)
#define LED_INDICATOR_PIN  -1

void initCamera() {
  camera_config_t config;
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
  config.xclk_freq_hz = 20000000;
  config.pixel_format = PIXFORMAT_JPEG;
  
  // Cấu hình tối ưu độ phân giải VGA (640x480) cho MediaPipe
  config.frame_size   = FRAMESIZE_VGA;
  config.jpeg_quality = 10;                     // 10 là mức nén sắc nét cao
  config.fb_count     = 2;                      // Double buffering với 8MB PSRAM
  config.fb_location  = CAMERA_FB_IN_PSRAM;
  config.grab_mode    = CAMERA_GRAB_LATEST;     // Luôn lấy khung hình mới nhất

  esp_err_t err = esp_camera_init(&config);
  if (err != ESP_OK) {
    Serial.printf("[CAMERA] Khởi tạo thất bại, mã lỗi: 0x%x\n", err);
    while (true) { delay(1000); }
  }
  Serial.println("[CAMERA] Khởi tạo Camera OV2640 thành công!");

  // Tinh chỉnh cảm biến OV2640 cho hình ảnh sáng rõ, không bị nhoè
  sensor_t * s = esp_camera_sensor_get();
  if (s != NULL) {
    s->set_brightness(s, 1);     // Tăng sáng nhẹ (-2 đến 2)
    s->set_contrast(s, 1);       // Tăng tương phản nhẹ (-2 đến 2)
    s->set_saturation(s, 0);     // Độ bão hòa màu bình thường
    s->set_special_effect(s, 0); // Không dùng hiệu ứng màu
    s->set_whitebal(s, 1);       // Bật tự động cân bằng trắng
    s->set_awb_gain(s, 1);       // Bật AWB Gain
    s->set_wb_mode(s, 0);        // Chế độ Auto WB
    s->set_exposure_ctrl(s, 1);  // Bật tự động phơi sáng
    s->set_aec2(s, 1);           // Bật AEC2
    s->set_gain_ctrl(s, 1);      // Bật AGC
    s->set_bpc(s, 1);            // Bật sửa điểm ảnh chết
    s->set_wpc(s, 1);            // Bật cân bằng trắng pixel
    s->set_raw_gma(s, 1);        // Bật Gamma
    s->set_lenc(s, 1);           // Sửa méo ống kính
    s->set_hmirror(s, 0);        // Không lật ngang (hoặc đổi 1 nếu muốn soi gương)
    s->set_vflip(s, 0);          // Không lật dọc
    Serial.println("[CAMERA] Đã tinh chỉnh thông số cảm biến OV2640 tối ưu.");
  }
}

void connectWiFi() {
  Serial.printf("[WIFI] Đang kết nối tới: %s", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  
  int attempts = 0;
  while (WiFi.status() != WL_CONNECTED && attempts < 30) {
    delay(500);
    Serial.print(".");
    attempts++;
  }

  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("\n[WIFI] Kết nối thành công!");
    Serial.printf("[WIFI] Địa chỉ IP: %s | RSSI: %d dBm\n", WiFi.localIP().toString().c_str(), WiFi.RSSI());
  } else {
    Serial.println("\n[WIFI] Kết nối thất bại! Vui lòng kiểm tra lại SSID/Password.");
  }
}

void sendFrameToCloud() {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[WIFI] Mất mạng, đang thử kết nối lại...");
    WiFi.reconnect();
    return;
  }

  // 1. Chụp khung hình mới nhất từ cảm biến
  camera_fb_t * fb = esp_camera_fb_get();
  if (!fb) {
    Serial.println("[CAMERA] Không thể lấy khung hình!");
    return;
  }

  // 2. Chuẩn bị kết nối HTTPS tới Render
  WiFiClientSecure client;
  client.setInsecure();          // Bỏ qua xác thực chứng chỉ SSL để gửi mượt mà
  client.setTimeout(8000);       // Timeout 8s

  HTTPClient http;
  if (http.begin(client, SERVER_URL)) {
    http.addHeader("Content-Type", "image/jpeg");
    http.addHeader("User-Agent", "ESP32-S3-WROOM-CAM");

    unsigned long startPost = millis();
    int httpResponseCode = http.POST(fb->buf, fb->len);
    unsigned long duration = millis() - startPost;

    if (httpResponseCode == 200) {
      String response = http.getString();
      Serial.printf("[POST OK %lums] Phản hồi: %s\n", duration, response.c_str());

      // Phân tích kết quả JSON
      StaticJsonDocument<384> doc;
      DeserializationError err = deserializeJson(doc, response);
      if (!err) {
        const char* status = doc["status"] | "N/A";
        int neck = doc["neck_angle"] | 0;
        int torso = doc["torso_angle"] | 0;
        int posture_time = doc["posture_time"] | 0;
        bool alert = doc["alert"] | false;

        Serial.printf(">> [%s] Cổ: %d° | Lưng: %d° | Duy trì: %ds | Alert: %s\n",
                      status, neck, torso, posture_time, alert ? "TRUE (BẬT)" : "false");

        if (alert) {
          Serial.println(">>> [CẢNH BÁO] Người dùng đang ngồi sai tư thế liên tục! <<<");
          // Tại đây có thể kích hoạt GPIO còi buzzer hoặc DFPlayer Mini nếu gắn thêm phần cứng
        }
      }
    } else {
      Serial.printf("[POST LỖI] HTTP Code: %d (Mất %lums)\n", httpResponseCode, duration);
    }
    http.end();
  } else {
    Serial.println("[HTTP] Không thể khởi tạo kết nối tới server Render!");
  }

  // 3. Giải phóng bộ đệm ảnh
  esp_camera_fb_return(fb);
}

void setup() {
  Serial.begin(115200);
  delay(1000);
  Serial.println("\n\n========================================================");
  Serial.println("  ESP32-S3 WROOM N16R8 CAM - POSTURE MONITORING CLIENT  ");
  Serial.println("========================================================");

  // Khởi động Camera và WiFi
  initCamera();
  connectWiFi();
}

void loop() {
  unsigned long now = millis();
  if (now - lastUploadTime >= UPLOAD_INTERVAL_MS) {
    lastUploadTime = now;
    sendFrameToCloud();
  }
  delay(10);
}
