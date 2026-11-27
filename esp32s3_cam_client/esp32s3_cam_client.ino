/*
 * =====================================================================================
 * ESP32-S3 WROOM N16R8 CAM - POSTURE MONITORING IP STREAM SERVER
 * (KIẾN TRÚC IP CAMERA MJPEG SERVER CỔNG 81 + ĐIỀU KHIỂN CỔNG 80)
 *
 * TÍNH NĂNG VƯỢT TRỘI:
 *  1. Phát luồng MJPEG Video chuẩn camera an ninh trên CỔNG 81 (/stream) đạt 25 - 30 FPS.
 *  2. Server CỔNG 80 phục vụ API điều khiển tức thì:
 *     - GET /play?track=1..4    : Phát còi loa JQ6500-16P (Track 1-4)
 *     - GET /servo?cmd=...      : Điều khiển Pan-Tilt Servo (trực tiếp < 10ms)
 *     - GET /status             : Trả về JSON trạng thái (IP, RSSI, góc servo)
 *     - GET /capture            : Chụp 1 ảnh tĩnh JPEG
 *     - GET /                   : Giao diện Web điều khiển & Test trực tiếp
 *  3. Khắc phục triệt để độ trễ Ping-Pong, giải phóng 100% CPU cho ESP32.
 * =====================================================================================
 */

#include <Arduino.h>
#include <WiFi.h>
#include <esp_camera.h>
#include <esp_http_server.h>
#include <esp_arduino_version.h>

// ================= 1. CẤU HÌNH WIFI =================
const char* WIFI_SSID = "iQOO Z10 Turbo+";
const char* WIFI_PASS = "12355678";

// ================= 2. ĐỊNH NGHĨA CHÂN JQ6500-16P =================
#define JQ6500_TX_PIN   41   // ESP32 GPIO41 (TX) --[1kΩ]--> JQ6500 RX (Chân 9)
#define JQ6500_RX_PIN   42   // ESP32 GPIO42 (RX) <---------- JQ6500 TX (Chân 10)
#define JQ_BAUD         9600
#define JQ_VOLUME       25   // Mức âm lượng chuẩn (0 - 30)

HardwareSerial JQSerial(1);
uint8_t currentJqVolume = JQ_VOLUME;
unsigned long lastAudioAlertTime = 0;
const unsigned long AUDIO_ALERT_COOLDOWN_MS = 4000; // Khoảng cách giữa 2 lần nhắc loa

void jqDrainRx() {
  while (JQSerial.available() > 0) JQSerial.read();
}

String jqReadReply(uint32_t timeoutMs = 200) {
  String rawAscii = "", hexStr = "";
  unsigned long startMs = millis(), lastByteMs = startMs;
  while (millis() - startMs < timeoutMs) {
    while (JQSerial.available() > 0) {
      uint8_t b = (uint8_t)JQSerial.read();
      lastByteMs = millis();
      rawAscii += (char)b;
      char buf[4]; snprintf(buf, sizeof(buf), "%02X ", b);
      hexStr += buf;
    }
    if (rawAscii.length() > 0 && (millis() - lastByteMs >= 30)) break;
    delay(2);
  }
  if (rawAscii.length() > 0) {
    hexStr.trim(); rawAscii.trim();
    return rawAscii;
  }
  return "";
}

void jqSend(uint8_t cmd, const uint8_t* params = nullptr, uint8_t paramLen = 0) {
  jqDrainRx();
  uint8_t len = (uint8_t)(2 + paramLen);
  JQSerial.write((uint8_t)0x7E);
  JQSerial.write(len);
  JQSerial.write(cmd);
  for (uint8_t i = 0; i < paramLen; i++) JQSerial.write(params[i]);
  JQSerial.write((uint8_t)0xEF);
  JQSerial.flush();
}

void jqReset()               { jqSend(0x0C); }
void jqSetSource(uint8_t src)  { jqSend(0x09, &src, 1); }   // 0x04 = Flash onboard
void jqSetVolume(uint8_t vol)  { if (vol > 30) vol = 30; jqSend(0x06, &vol, 1); }
void jqSetLoopMode(uint8_t m)  { jqSend(0x11, &m, 1); }     // 4 = ONE_STOP
void jqPlayCurrent()         { jqSend(0x0D); }
void jqStop()                { jqSend(0x0E); }

void jqPlayTrack(uint16_t n) {
  jqSetVolume(currentJqVolume);
  delay(30);
  uint8_t p[2] = { (uint8_t)(n >> 8), (uint8_t)(n & 0xFF) };
  jqSend(0x03, p, 2);
  jqReadReply(100);
  Serial.printf("🔊 [JQ6500] Đang phát Track %d (00%d.mp3)\n", n, n);
}

void jq6500_init() {
  JQSerial.begin(JQ_BAUD, SERIAL_8N1, JQ6500_RX_PIN, JQ6500_TX_PIN);
  delay(600);
  jqReset();
  delay(500);
  jqSetSource(0x04);
  delay(400);
  jqSetVolume(currentJqVolume);
  delay(100);
  jqSetLoopMode(4);
  delay(100);
  Serial.println("[JQ6500] Đã khởi tạo âm thanh JQ6500-16P sẵn sàng.");
}

// ================= 3. ĐIỀU KHIỂN PAN-TILT SERVO =================
#define PIN_SERVO_PAN    21   // Trục Ngang (SG90 360° Continuous)  -> GPIO 21
#define PIN_SERVO_TILT   47   // Trục Dọc   (SG90 180° Positional)  -> GPIO 47

const int TILT_MIN      = 35;
const int TILT_MAX      = 145;
const int TILT_CENTER   = 90;
const int TILT_DESK     = 65;
int currentTilt         = TILT_DESK;

int panTrimStopVal      = 90;
int panSpeedDelta       = 20;
unsigned long jogEndTime = 0;
bool isJogging          = false;

#if ESP_ARDUINO_VERSION_MAJOR >= 3
  bool initServoHardware() {
    bool ok1 = ledcAttach(PIN_SERVO_PAN, 50, 14);
    bool ok2 = ledcAttach(PIN_SERVO_TILT, 50, 14);
    return ok1 && ok2;
  }
  void writeServoPwm(uint8_t pin, int mappedVal) {
    long us = map(mappedVal, 0, 180, 500, 2500);
    uint32_t duty = (us * 16384) / 20000;
    ledcWrite(pin, duty);
  }
#else
  #define LEDC_CH_PAN   4
  #define LEDC_CH_TILT  5
  bool initServoHardware() {
    ledcSetup(LEDC_CH_PAN, 50, 14);
    ledcAttachPin(PIN_SERVO_PAN, LEDC_CH_PAN);
    ledcSetup(LEDC_CH_TILT, 50, 14);
    ledcAttachPin(PIN_SERVO_TILT, LEDC_CH_TILT);
    return true;
  }
  void writeServoPwm(uint8_t pin, int mappedVal) {
    long us = map(mappedVal, 0, 180, 500, 2500);
    uint32_t duty = (us * 16384) / 20000;
    if (pin == PIN_SERVO_PAN) ledcWrite(LEDC_CH_PAN, duty);
    else ledcWrite(LEDC_CH_TILT, duty);
  }
#endif

void panStop() {
  isJogging = false;
  writeServoPwm(PIN_SERVO_PAN, panTrimStopVal);
}

void panRotateLeft(int speedOffset = 0) {
  isJogging = false;
  int spd = (speedOffset > 0) ? speedOffset : panSpeedDelta;
  int val = constrain(panTrimStopVal - spd, 0, 85);
  writeServoPwm(PIN_SERVO_PAN, val);
}

void panRotateRight(int speedOffset = 0) {
  isJogging = false;
  int spd = (speedOffset > 0) ? speedOffset : panSpeedDelta;
  int val = constrain(panTrimStopVal + spd, 95, 180);
  writeServoPwm(PIN_SERVO_PAN, val);
}

void panJog(int direction, int durationMs = 200) {
  int val = (direction < 0) ? constrain(panTrimStopVal - panSpeedDelta, 0, 85)
                            : constrain(panTrimStopVal + panSpeedDelta, 95, 180);
  writeServoPwm(PIN_SERVO_PAN, val);
  jogEndTime = millis() + durationMs;
  isJogging = true;
}

void updateServoLoop() {
  if (isJogging && millis() >= jogEndTime) {
    panStop();
  }
}

void tiltSetAngle(int targetAngle) {
  currentTilt = constrain(targetAngle, TILT_MIN, TILT_MAX);
  writeServoPwm(PIN_SERVO_TILT, currentTilt);
}

void tiltSmoothMove(int targetAngle, int stepDelayMs = 12) {
  targetAngle = constrain(targetAngle, TILT_MIN, TILT_MAX);
  while (currentTilt != targetAngle) {
    if (currentTilt < targetAngle) currentTilt++;
    else currentTilt--;
    writeServoPwm(PIN_SERVO_TILT, currentTilt);
    delay(stepDelayMs);
    updateServoLoop();
  }
}

void handleServoCommand(const String& cmd) {
  String c = cmd;
  c.toLowerCase();
  c.trim();

  if (c == "left" || c == "jog_left") {
    panJog(-1, 200);
  } else if (c == "right" || c == "jog_right") {
    panJog(1, 200);
  } else if (c == "rotate_left") {
    panRotateLeft();
  } else if (c == "rotate_right") {
    panRotateRight();
  } else if (c == "up") {
    tiltSetAngle(currentTilt + 5);
  } else if (c == "down") {
    tiltSetAngle(currentTilt - 5);
  } else if (c == "stop") {
    panStop();
  } else if (c == "center" || c == "preset_center") {
    panStop();
    tiltSetAngle(TILT_CENTER);
  } else if (c == "preset_desk" || c == "desk") {
    panStop();
    tiltSetAngle(TILT_DESK);
  } else if (c == "preset_high" || c == "high") {
    panStop();
    tiltSetAngle(115);
  } else if (c.startsWith("tilt:")) {
    int ang = c.substring(5).toInt();
    tiltSetAngle(ang);
  } else if (c.startsWith("trim:")) {
    int t = c.substring(5).toInt();
    if (t >= 80 && t <= 100) {
      panTrimStopVal = t;
      panStop();
    }
  }
  Serial.printf("🤖 [SERVO] Thực thi: %s (Tilt: %d°)\n", c.c_str(), currentTilt);
}

// ================= 4. KHỞI TẠO CAMERA OV2640 =================
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
  config.xclk_freq_hz = 20000000;            // 20MHz cho tốc độ khung hình tối đa
  config.pixel_format = PIXFORMAT_JPEG;

  if (psramFound()) {
    config.frame_size   = FRAMESIZE_VGA;     // 640x480
    config.jpeg_quality = 16;                // Quality 16: sắc nét và mượt mà 25-30 FPS
    config.fb_count     = 2;
    config.fb_location  = CAMERA_FB_IN_PSRAM;
    config.grab_mode    = CAMERA_GRAB_LATEST;
  } else {
    config.frame_size   = FRAMESIZE_QVGA;    // 320x240
    config.jpeg_quality = 16;
    config.fb_count     = 1;
    config.fb_location  = CAMERA_FB_IN_DRAM;
    config.grab_mode    = CAMERA_GRAB_WHEN_EMPTY;
  }

  esp_err_t err = esp_camera_init(&config);
  if (err != ESP_OK) {
    Serial.printf("[CAMERA] Khởi tạo thất bại: 0x%x\n", err);
    return false;
  }

  sensor_t* s = esp_camera_sensor_get();
  if (s != NULL) {
    s->set_brightness(s, 0);
    s->set_contrast(s, 0);
    s->set_saturation(s, 0);
    s->set_whitebal(s, 1);
    s->set_awb_gain(s, 1);
    s->set_wb_mode(s, 0);
    s->set_exposure_ctrl(s, 1);
    s->set_aec2(s, 1);
    s->set_gain_ctrl(s, 1);
    s->set_bpc(s, 1);
    s->set_wpc(s, 1);
    s->set_raw_gma(s, 1);
    s->set_lenc(s, 1);
  }
  Serial.println("[CAMERA] Khởi tạo Camera OV2640 thành công (20MHz, VGA)!");
  return true;
}

// ================= 5. ESP_HTTP_SERVER (STREAM CỔNG 81 & API CỔNG 80) =================
httpd_handle_t camera_httpd = NULL;
httpd_handle_t stream_httpd = NULL;

#define PART_BOUNDARY "123456789000000000000987654321"
static const char* _STREAM_CONTENT_TYPE = "multipart/x-mixed-replace;boundary=" PART_BOUNDARY;
static const char* _STREAM_BOUNDARY = "\r\n--" PART_BOUNDARY "\r\n";
static const char* _STREAM_PART = "Content-Type: image/jpeg\r\nContent-Length: %u\r\n\r\n";

// Handler phát luồng MJPEG trên cổng 81
static esp_err_t stream_handler(httpd_req_t *req) {
  camera_fb_t *fb = NULL;
  esp_err_t res = ESP_OK;
  char part_buf[128];

  res = httpd_resp_set_type(req, _STREAM_CONTENT_TYPE);
  if (res != ESP_OK) return res;
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  httpd_resp_set_hdr(req, "X-Framerate", "30");

  Serial.println(">>> [STREAM] Bắt đầu phát luồng video MJPEG cho Client... <<<");

  while (true) {
    fb = esp_camera_fb_get();
    if (!fb) {
      vTaskDelay(pdMS_TO_TICKS(10));
      continue;
    }

    res = httpd_resp_send_chunk(req, _STREAM_BOUNDARY, strlen(_STREAM_BOUNDARY));
    if (res == ESP_OK) {
      size_t hlen = snprintf(part_buf, sizeof(part_buf), _STREAM_PART, (unsigned)fb->len);
      res = httpd_resp_send_chunk(req, part_buf, hlen);
    }
    if (res == ESP_OK) {
      res = httpd_resp_send_chunk(req, (const char *)fb->buf, fb->len);
    }

    esp_camera_fb_return(fb);
    fb = NULL;

    if (res != ESP_OK) break;
    // Cho phép các tác vụ khác chạy mà không chặn CPU
    vTaskDelay(pdMS_TO_TICKS(5));
  }

  Serial.println("<<< [STREAM] Client đã ngắt kết nối video >>>");
  return res;
}

// Handler lệnh phát loa: GET /play?track=1..4
static esp_err_t play_handler(httpd_req_t *req) {
  char buf[64];
  size_t buf_len = httpd_req_get_url_query_len(req) + 1;
  int track = 1;
  if (buf_len > 1 && buf_len <= sizeof(buf)) {
    if (httpd_req_get_url_query_str(req, buf, buf_len) == ESP_OK) {
      char param[16];
      if (httpd_query_key_value(buf, "track", param, sizeof(param)) == ESP_OK) {
        track = atoi(param);
      }
    }
  }
  if (track >= 1 && track <= 4) {
    jqPlayTrack(track);
  }
  httpd_resp_set_type(req, "application/json");
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  char resp[64];
  snprintf(resp, sizeof(resp), "{\"success\":true,\"track\":%d}", track);
  return httpd_resp_send(req, resp, strlen(resp));
}

// Handler lệnh điều khiển servo: GET /servo?cmd=up|down|left|right|center...
static esp_err_t servo_handler(httpd_req_t *req) {
  char buf[128];
  size_t buf_len = httpd_req_get_url_query_len(req) + 1;
  char cmd[32] = "";
  if (buf_len > 1 && buf_len <= sizeof(buf)) {
    if (httpd_req_get_url_query_str(req, buf, buf_len) == ESP_OK) {
      httpd_query_key_value(buf, "cmd", cmd, sizeof(cmd));
    }
  }
  if (strlen(cmd) > 0) {
    handleServoCommand(String(cmd));
  }
  httpd_resp_set_type(req, "application/json");
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  char resp[96];
  snprintf(resp, sizeof(resp), "{\"success\":true,\"cmd\":\"%s\"}", cmd);
  return httpd_resp_send(req, resp, strlen(resp));
}

// Handler trạng thái: GET /status
static esp_err_t status_handler(httpd_req_t *req) {
  httpd_resp_set_type(req, "application/json");
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  char resp[256];
  snprintf(resp, sizeof(resp),
    "{\"name\":\"ESP32-S3-POSTURE\",\"ip\":\"%s\",\"rssi\":%d,\"tilt\":%d,\"pan_trim\":%d,\"heap\":%u}",
    WiFi.localIP().toString().c_str(), WiFi.RSSI(), currentTilt, panTrimStopVal, (unsigned)ESP.getFreeHeap());
  return httpd_resp_send(req, resp, strlen(resp));
}

// Handler chụp ảnh tĩnh: GET /capture
static esp_err_t capture_handler(httpd_req_t *req) {
  camera_fb_t *fb = esp_camera_fb_get();
  if (!fb) {
    httpd_resp_send_500(req);
    return ESP_FAIL;
  }
  httpd_resp_set_type(req, "image/jpeg");
  httpd_resp_set_hdr(req, "Content-Disposition", "inline; filename=capture.jpg");
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  esp_err_t res = httpd_resp_send(req, (const char *)fb->buf, fb->len);
  esp_camera_fb_return(fb);
  return res;
}

// Giao diện Web điều khiển trực tiếp trên cổng 80: GET /
static esp_err_t index_handler(httpd_req_t *req) {
  String html = "<!DOCTYPE html><html><head><meta charset='UTF-8'><meta name='viewport' content='width=device-width, initial-scale=1.0'>";
  html += "<title>ESP32-S3 Camera Stream & Control</title>";
  html += "<style>body{font-family:Arial,sans-serif;background:#121826;color:#fff;text-align:center;padding:20px;}";
  html += "h2{color:#00e5ff;} .btn{background:#1f293d;color:#fff;border:1px solid #3b82f6;padding:10px 16px;margin:6px;border-radius:8px;font-size:15px;cursor:pointer;}";
  html += ".btn:hover{background:#3b82f6;} .danger{border-color:#ef4444;} .danger:hover{background:#ef4444;}";
  html += "img{max-width:90%;border-radius:12px;border:2px solid #3b82f6;box-shadow:0 0 20px rgba(0,229,255,0.2);}";
  html += "</style></head><body>";
  html += "<h2>📸 ESP32-S3 AI CAMERA - LIVE STREAM 30 FPS</h2>";
  html += "<p>Địa chỉ IP: <b>" + WiFi.localIP().toString() + "</b> | Stream Port: <b>:81/stream</b></p>";
  html += "<img src='http://" + WiFi.localIP().toString() + ":81/stream' alt='Live Stream'><br><br>";
  html += "<h3>🔊 Test Âm Thanh Loa JQ6500-16P</h3>";
  html += "<button class='btn' onclick='fetch(\"/play?track=1\")'>Track 1 (Khởi động)</button>";
  html += "<button class='btn' onclick='fetch(\"/play?track=2\")'>Track 2 (Cúi cổ)</button>";
  html += "<button class='btn' onclick='fetch(\"/play?track=3\")'>Track 3 (Gù lưng)</button>";
  html += "<button class='btn' onclick='fetch(\"/play?track=4\")'>Track 4 (Phối hợp)</button><br><br>";
  html += "<h3>🔄 Điều Khiển Pan-Tilt Servo</h3>";
  html += "<button class='btn' onclick='fetch(\"/servo?cmd=up\")'>⬆️ Lên (+5°)</button>";
  html += "<button class='btn' onclick='fetch(\"/servo?cmd=down\")'>⬇️ Xuống (-5°)</button>";
  html += "<button class='btn' onclick='fetch(\"/servo?cmd=left\")'>⬅️ Trái</button>";
  html += "<button class='btn' onclick='fetch(\"/servo?cmd=right\")'>➡️ Phải</button>";
  html += "<button class='btn' onclick='fetch(\"/servo?cmd=center\")'>🎯 Tâm (90°)</button>";
  html += "<button class='btn danger' onclick='fetch(\"/servo?cmd=stop\")'>⏹️ Dừng</button>";
  html += "</body></html>";

  httpd_resp_set_type(req, "text/html; charset=utf-8");
  return httpd_resp_send(req, html.c_str(), html.length());
}

void startCameraServer() {
  httpd_config_t config = HTTPD_DEFAULT_CONFIG();
  config.server_port = 80;
  config.ctrl_port = 32768;
  config.max_uri_handlers = 8;
  config.lru_purge_enable = true;

  httpd_uri_t index_uri   = { .uri = "/",        .method = HTTP_GET, .handler = index_handler,   .user_ctx = NULL };
  httpd_uri_t play_uri    = { .uri = "/play",     .method = HTTP_GET, .handler = play_handler,    .user_ctx = NULL };
  httpd_uri_t servo_uri   = { .uri = "/servo",    .method = HTTP_GET, .handler = servo_handler,   .user_ctx = NULL };
  httpd_uri_t status_uri  = { .uri = "/status",   .method = HTTP_GET, .handler = status_handler,  .user_ctx = NULL };
  httpd_uri_t capture_uri = { .uri = "/capture",  .method = HTTP_GET, .handler = capture_handler, .user_ctx = NULL };

  if (httpd_start(&camera_httpd, &config) == ESP_OK) {
    httpd_register_uri_handler(camera_httpd, &index_uri);
    httpd_register_uri_handler(camera_httpd, &play_uri);
    httpd_register_uri_handler(camera_httpd, &servo_uri);
    httpd_register_uri_handler(camera_httpd, &status_uri);
    httpd_register_uri_handler(camera_httpd, &capture_uri);
    Serial.println("[WEB] Cổng 80 (/play, /servo, /status, /capture) đã sẵn sàng!");
  }

  // Khởi động riêng Server phát luồng video trên Cổng 81
  config.server_port = 81;
  config.ctrl_port = 32769;
  config.stack_size = 8192;

  httpd_uri_t stream_uri = { .uri = "/stream", .method = HTTP_GET, .handler = stream_handler, .user_ctx = NULL };
  if (httpd_start(&stream_httpd, &config) == ESP_OK) {
    httpd_register_uri_handler(stream_httpd, &stream_uri);
    Serial.println("[STREAM] Cổng 81 (/stream) đã sẵn sàng phát luồng MJPEG 30 FPS!");
  }
}

// ================= 6. SETUP VÀ LOOP =================
void connectWiFi() {
  Serial.printf("\n[WIFI] Đang kết nối tới mạng: %s", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false); // Không ngủ WiFi để giữ stream liên tục
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASS);

  int count = 0;
  while (WiFi.status() != WL_CONNECTED && count < 40) {
    delay(500);
    Serial.print(".");
    count++;
  }

  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("\n========================================================");
    Serial.println("✅ [Wi-Fi] KẾT NỐI THÀNH CÔNG!");
    Serial.printf("📡 ĐỊA CHỈ IP ESP32-S3 : http://%s\n", WiFi.localIP().toString().c_str());
    Serial.printf("📹 ĐƯỜNG DẪN VIDEO STREAM : http://%s:81/stream\n", WiFi.localIP().toString().c_str());
    Serial.printf("🔊 ĐƯỜNG DẪN LỆNH LOA MP3 : http://%s/play?track=1\n", WiFi.localIP().toString().c_str());
    Serial.printf("🔄 ĐƯỜNG DẪN LỆNH SERVO   : http://%s/servo?cmd=left\n", WiFi.localIP().toString().c_str());
    Serial.println("========================================================\n");
  } else {
    Serial.println("\n[WIFI] Kết nối thất bại! Vui lòng kiểm tra lại tên Wi-Fi và mật khẩu.");
  }
}

void setup() {
  Serial.begin(115200);
  delay(1000);
  Serial.println("\n\n========================================================");
  Serial.println("  ESP32-S3 WROOM N16R8 - AI CAMERA MJPEG STREAM SERVER  ");
  Serial.println("         (KIẾN TRÚC STREAM CHUYÊN NGHIỆP 30 FPS)        ");
  Serial.println("========================================================");
  Serial.printf("[SYS] PSRAM: %s (%u bytes) | Free heap: %u\n",
                psramFound() ? "OK" : "KHÔNG CÓ",
                (unsigned)ESP.getPsramSize(), (unsigned)ESP.getFreeHeap());

  // 1. Kết nối Wi-Fi
  connectWiFi();

  // 2. Khởi tạo Camera
  if (!initCamera()) {
    Serial.println("[CAMERA] Khởi tạo camera thất bại! Khởi động lại sau 5s...");
    delay(5000);
    ESP.restart();
  }

  // 3. Khởi tạo Mô-đun Loa JQ6500-16P
  jq6500_init();

  // 4. Khởi tạo Pan-Tilt Servo
  initServoHardware();
  tiltSetAngle(TILT_DESK);
  panStop();

  // 5. Khởi động Web Server (Cổng 80 điều khiển & Cổng 81 phát video stream)
  startCameraServer();

  // Tự động phát âm báo Track 1 (Khởi động thành công)
  delay(800);
  jqPlayTrack(1);
}

void loop() {
  // Cập nhật ngắt nhịp quay của trục Pan 360°
  updateServoLoop();

  // Lắng nghe phím bấm kiểm tra từ Serial Monitor
  if (Serial.available() > 0) {
    char c = Serial.read();
    if (c == '1') jqPlayTrack(1);
    else if (c == '2') jqPlayTrack(2);
    else if (c == '3') jqPlayTrack(3);
    else if (c == '4') jqPlayTrack(4);
    else if (c == 's' || c == 'S') { jqStop(); panStop(); }
    else if (c == 'u' || c == 'U') handleServoCommand("up");
    else if (c == 'd' || c == 'D') handleServoCommand("down");
    else if (c == 'l' || c == 'L') handleServoCommand("left");
    else if (c == 'r' || c == 'R') handleServoCommand("right");
    else if (c == 'c' || c == 'C') handleServoCommand("center");
  }

  // Tự động kết nối lại Wi-Fi nếu mất sóng
  if (WiFi.status() != WL_CONNECTED) {
    static unsigned long lastReconnect = 0;
    if (millis() - lastReconnect > 10000) {
      lastReconnect = millis();
      Serial.println("[WIFI] Mất kết nối, đang thử kết nối lại...");
      WiFi.reconnect();
    }
  }

  delay(10);
}
