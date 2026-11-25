import os
import io
import time
import json
import math as m
import cv2
import numpy as np
import threading
from datetime import datetime, timezone, timedelta
from flask import Flask, Response, render_template, request, jsonify, send_file, send_from_directory
from PIL import Image, ImageDraw, ImageFont
from gtts import gTTS
import requests

# Import MediaPipe Pose an toàn cho mọi môi trường (Windows / Linux Render)
import mediapipe as mp
try:
    import mediapipe.python.solutions.pose as mp_pose
except ImportError:
    mp_pose = mp.solutions.pose

app = Flask(__name__)

# Khởi tạo MediaPipe Pose
# Bật static_image_mode=True để phân tích chính xác từng frame rời từ ESP32 không bị trôi điểm
pose = mp_pose.Pose(
    static_image_mode=True,
    model_complexity=1,
    min_detection_confidence=0.5,
    min_tracking_confidence=0.5
)

# Khóa luồng (Thread Lock) để đồng bộ dữ liệu
data_lock = threading.Lock()

# ================= CẤU HÌNH HỆ THỐNG & TẦN SUẤT CẢNH BÁO =================
CONFIG_FILE = os.path.join(os.path.dirname(__file__), "app_config.json")

default_settings = {
    "bad_posture_threshold_sec": 300,  # Mặc định 5 phút (300 giây) sai liên tục mới cảnh báo
    "alert_cooldown_sec": 60,          # Nhắc lại sau mỗi 60 giây nếu chưa sửa tư thế
    "speaker_enabled": True,           # Cho phép còi loa ESP32 kêu
    "vibrate_enabled": True,           # Rung điện thoại
    "voice_enabled": True,             # Giọng nói TTS
    "notification_enabled": True       # Thông báo đẩy
}

def load_system_settings():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
                cfg = dict(default_settings)
                cfg.update(saved)
                return cfg
        except Exception as e:
            print(f"[CONFIG] Lỗi đọc app_config.json: {e}")
    return dict(default_settings)

def save_system_settings(settings_dict):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(settings_dict, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        print(f"[CONFIG] Lỗi ghi app_config.json: {e}")
        return False

system_settings = load_system_settings()

# Biến lưu trữ khung hình mới nhất để phát qua /video_feed
latest_processed_frame = None
last_camera_packet_time = 0

# Biến trạng thái phân tích tư thế thích ứng đa góc
status_data = {
    "status": "Đang chờ camera...",
    "camera_view": "Chưa xác định",
    "neck_angle": 0,
    "torso_angle": 0,
    "posture_time": 0,
    "good_posture_time": 0,
    "bad_posture_time": 0,
    "error_message": "",
    "alert": False,
    "last_update": 0,
    "is_connected": False
}

# Biến đếm thời gian thực
good_start_time = None
bad_start_time = None
last_alert_triggered_time = 0

# Hàng đợi lệnh điều khiển Pan-Tilt Servo gửi tới ESP32-S3 CAM
pending_servo_cmd = None
latest_servo_status = {"cmd": "stop", "time": 0}

# Hàng đợi lệnh phát loa trực tiếp từ Web UI gửi tới ESP32-S3 CAM
pending_track_cmd = 0

# Múi giờ Việt Nam (UTC+7)
VN_TZ = timezone(timedelta(hours=7))

# Tải font chữ hỗ trợ tiếng Việt mượt mà trên Render Linux
cached_font = None

def get_vietnamese_font(font_size=24):
    global cached_font
    if cached_font is not None:
        return cached_font

    font_candidates = [
        "arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
        "Roboto-Regular.ttf"
    ]

    for fpath in font_candidates:
        if os.path.exists(fpath):
            try:
                cached_font = ImageFont.truetype(fpath, font_size)
                return cached_font
            except Exception:
                pass

    try:
        font_url = "https://raw.githubusercontent.com/google/fonts/main/apache/roboto/Roboto-Regular.ttf"
        local_font_path = os.path.join(os.path.dirname(__file__), "Roboto-Regular.ttf")
        if not os.path.exists(local_font_path):
            r = requests.get(font_url, timeout=5)
            if r.status_code == 200:
                with open(local_font_path, "wb") as f:
                    f.write(r.content)
        if os.path.exists(local_font_path):
            cached_font = ImageFont.truetype(local_font_path, font_size)
            return cached_font
    except Exception as e:
        print(f"Không thể tải font ngoài: {e}")

    cached_font = ImageFont.load_default()
    return cached_font

def draw_text_vietnamese(image, text, position, font_size=24, color=(0, 255, 0)):
    """Vẽ chữ tiếng Việt có dấu lên ảnh OpenCV thông qua Pillow"""
    try:
        font = get_vietnamese_font(font_size)
        image_pil = Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))
        draw = ImageDraw.Draw(image_pil)
        fill_color = (color[2], color[1], color[0]) if len(color) == 3 else color
        draw.text(position, text, font=font, fill=fill_color)
        return cv2.cvtColor(np.array(image_pil), cv2.COLOR_RGB2BGR)
    except Exception as e:
        cv2.putText(image, text, position, cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)
        return image

def calculate_angle_vertical(p1, p2):
    """Tính góc nghiêng của đường thẳng p1->p2 so với phương thẳng đứng (độ)"""
    try:
        dx = p2[0] - p1[0]
        dy = p2[1] - p1[1]
        radians = m.atan2(abs(dx), abs(dy) + 1e-6)
        return int(m.degrees(radians))
    except Exception:
        return 0

def calculate_angle_horizontal(p1, p2):
    """Tính góc nghiêng của đường thẳng p1->p2 so với phương ngang (độ)"""
    try:
        dx = abs(p2[0] - p1[0]) + 1e-6
        dy = abs(p2[1] - p1[1])
        radians = m.atan2(dy, dx)
        return int(m.degrees(radians))
    except Exception:
        return 0

def detect_camera_view(lm, w, h):
    """
    Tự động nhận diện góc đặt Camera:
    - FRONT: Chính diện (trực diện màn hình webcam)
    - DIAGONAL_LEFT / DIAGONAL_RIGHT: Chéo góc bàn 3/4
    - SIDE_LEFT / SIDE_RIGHT: Ngang hông 90 độ
    """
    ls = lm[mp_pose.PoseLandmark.LEFT_SHOULDER.value]
    rs = lm[mp_pose.PoseLandmark.RIGHT_SHOULDER.value]
    le = lm[mp_pose.PoseLandmark.LEFT_EAR.value]
    re = lm[mp_pose.PoseLandmark.RIGHT_EAR.value]
    nose = lm[mp_pose.PoseLandmark.NOSE.value]

    shoulder_dx = abs(ls.x - rs.x) * w
    head_height = max(abs(nose.y - (ls.y + rs.y) / 2.0) * h, 20.0)
    shoulder_ratio = shoulder_dx / head_height

    # Xác định bên nào nhìn rõ hơn
    left_score = (ls.visibility + le.visibility) / 2.0
    right_score = (rs.visibility + re.visibility) / 2.0
    use_left = left_score >= right_score

    if shoulder_ratio > 0.85:
        return "FRONT", use_left
    elif shoulder_ratio >= 0.32:
        return ("DIAGONAL_LEFT" if use_left else "DIAGONAL_RIGHT"), use_left
    else:
        return ("SIDE_LEFT" if use_left else "SIDE_RIGHT"), use_left

def create_standby_frame(message="Đang chờ tín hiệu từ ESP32-S3 CAM..."):
    """Tạo khung hình nền đen chờ kết nối khi camera chưa bật"""
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    cv2.rectangle(frame, (20, 20), (620, 460), (50, 50, 50), 2)
    cv2.circle(frame, (320, 200), 50, (0, 165, 255), 3)
    cv2.putText(frame, "CAM", (295, 210), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 165, 255), 2)
    frame = draw_text_vietnamese(frame, message, (130, 280), font_size=26, color=(200, 200, 200))
    current_time_str = datetime.now(VN_TZ).strftime("%H:%M:%S")
    frame = draw_text_vietnamese(frame, f"Thời gian server: {current_time_str} (UTC+7)", (180, 330), font_size=20, color=(150, 150, 150))
    ret, buffer = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
    return buffer.tobytes()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/ping')
def ping():
    return "OK", 200

@app.route('/api/upload', methods=['POST'])
def upload_frame():
    """Nhận ảnh từ ESP32-S3 và tự động phân tích theo góc đặt camera tương ứng"""
    global latest_processed_frame, last_camera_packet_time
    global good_start_time, bad_start_time, last_alert_triggered_time, status_data
    global pending_servo_cmd, pending_track_cmd

    img_bytes = request.data
    if not img_bytes and 'file' in request.files:
        img_bytes = request.files['file'].read()

    if not img_bytes:
        return jsonify({"error": "No image data received"}), 400

    np_arr = np.frombuffer(img_bytes, np.uint8)
    image = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)

    if image is None:
        return jsonify({"error": "Failed to decode image"}), 400

    now = time.time()
    last_camera_packet_time = now
    h, w = image.shape[:2]

    # Xử lý qua MediaPipe Pose
    image_rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    results = pose.process(image_rgb)

    green = (0, 255, 127) # Chuẩn
    red = (50, 50, 255)   # Sai
    neck_angle = 0
    torso_angle = 0
    posture_status = "Đang tìm cơ thể..."
    posture_time = 0
    error_msg = ""
    should_alert = False
    color = green
    camera_view_label = "Chưa xác định"
    head_forward_offset = 0
    hip_visible = False
    view_type = "UNKNOWN"

    if results.pose_landmarks:
        lm = results.pose_landmarks.landmark
        keypoints = {landmark.name: (int(lm[landmark.value].x * w), int(lm[landmark.value].y * h))
                     for landmark in mp_pose.PoseLandmark}

        # 1. Tự động nhận diện góc đặt Camera
        view_type, use_left = detect_camera_view(lm, w, h)

        # Trích xuất các điểm quan trọng theo bên nhìn rõ hơn
        ear_lm = lm[mp_pose.PoseLandmark.LEFT_EAR.value] if use_left else lm[mp_pose.PoseLandmark.RIGHT_EAR.value]
        shoulder_lm = lm[mp_pose.PoseLandmark.LEFT_SHOULDER.value] if use_left else lm[mp_pose.PoseLandmark.RIGHT_SHOULDER.value]
        hip_lm = lm[mp_pose.PoseLandmark.LEFT_HIP.value] if use_left else lm[mp_pose.PoseLandmark.RIGHT_HIP.value]
        nose_lm = lm[mp_pose.PoseLandmark.NOSE.value]

        ear_pt = (int(ear_lm.x * w), int(ear_lm.y * h))
        shoulder_pt = (int(shoulder_lm.x * w), int(shoulder_lm.y * h))
        hip_pt = (int(hip_lm.x * w), int(hip_lm.y * h))
        nose_pt = (int(nose_lm.x * w), int(nose_lm.y * h))

        # Kiểm tra hông có bị bàn che khuất không
        hip_visible = (hip_lm.visibility >= 0.55 and hip_pt[1] < h - 20)

        # 2. Xử lý thuật toán thích ứng theo từng góc camera
        if view_type == "FRONT":
            camera_view_label = "Chính diện (Frontal)"
            # Góc nghiêng đầu (Head Tilt) so với phương ngang
            ls_pt = keypoints.get('LEFT_SHOULDER', (0, 0))
            rs_pt = keypoints.get('RIGHT_SHOULDER', (0, 0))
            le_pt = keypoints.get('LEFT_EAR', (0, 0))
            re_pt = keypoints.get('RIGHT_EAR', (0, 0))

            neck_angle = calculate_angle_horizontal(le_pt, re_pt)      # Độ nghiêng đầu
            torso_angle = calculate_angle_horizontal(ls_pt, rs_pt)     # Độ lệch 2 vai

            # Cúi sát màn hình: khoảng cách từ cằm/mũi tới vai bị thu hẹp
            nose_shoulder_dist = abs(nose_pt[1] - (ls_pt[1] + rs_pt[1]) / 2.0)
            slouch_forward = (nose_shoulder_dist < 45)

            is_bad = (neck_angle >= 14 or torso_angle >= 10 or slouch_forward)

            if neck_angle >= 14:
                error_msg += f"Nghiêng đầu ({neck_angle}°). "
            if torso_angle >= 10:
                error_msg += f"Lệch vai ({torso_angle}°). "
            if slouch_forward:
                error_msg += "Cúi sát màn hình. "

        elif "DIAGONAL" in view_type:
            side_name = "Hông Trái" if use_left else "Hông Phải"
            camera_view_label = f"Chéo Bàn 3/4 ({side_name})"

            # Góc gập cổ: Đo từ TAI -> VAI (Craniovertebral Angle) - chuẩn y khoa công thái học
            neck_angle = calculate_angle_vertical(shoulder_pt, ear_pt)

            if hip_visible:
                # Nếu thấy rõ hông -> đo góc vai-hông
                torso_angle = calculate_angle_vertical(hip_pt, shoulder_pt)
                is_bad = (neck_angle >= 32 or torso_angle >= 18)
                if torso_angle >= 18:
                    error_msg += f"Lưng nghiêng ({torso_angle}°). "
            else:
                # BỊ BÀN CHE HÔNG: Dùng tiêu chuẩn Forward Head Posture (FHP) & độ đổ thân trên
                # Trục cổ bình thường: ear_x gần tương đương shoulder_x. Khi gù lưng, ear_x trượt ra trước
                head_forward_offset = abs(ear_pt[0] - shoulder_pt[0])
                torso_angle = calculate_angle_vertical(shoulder_pt, nose_pt) # Góc thân trên thay thế
                is_bad = (neck_angle >= 32 or head_forward_offset > 55)
                if head_forward_offset > 55:
                    error_msg += "Gù lưng/nhô đầu ra trước. "

            if neck_angle >= 32:
                error_msg += f"Cổ quá cúi ({neck_angle}°). "

        else: # SIDE_LEFT hoặc SIDE_RIGHT (90 độ)
            side_name = "Hông Trái" if use_left else "Hông Phải"
            camera_view_label = f"Ngang Hông 90° ({side_name})"

            neck_angle = calculate_angle_vertical(shoulder_pt, ear_pt)
            if hip_visible:
                torso_angle = calculate_angle_vertical(hip_pt, shoulder_pt)
                is_bad = (neck_angle >= 35 or torso_angle >= 15)
                if torso_angle >= 15:
                    error_msg += f"Lưng nghiêng ({torso_angle}°). "
            else:
                torso_angle = calculate_angle_vertical(shoulder_pt, nose_pt)
                is_bad = (neck_angle >= 35)

            if neck_angle >= 35:
                error_msg += f"Cổ quá cúi ({neck_angle}°). "

        # 3. Đánh giá trạng thái thời gian và cảnh báo
        if not is_bad:
            color = green
            posture_status = "Đúng tư thế"
            if good_start_time is None:
                good_start_time = now
            bad_start_time = None
            posture_time = int(now - good_start_time)
        else:
            color = red
            posture_status = "Sai tư thế"
            if bad_start_time is None:
                bad_start_time = now
            good_start_time = None
            posture_time = int(now - bad_start_time)

            thresh_sec = system_settings.get("bad_posture_threshold_sec", 300)
            cooldown_sec = system_settings.get("alert_cooldown_sec", 60)

            # Chỉ cảnh báo khi ngồi sai liên tục đủ thời gian ngưỡng (ví dụ: 5 phút = 300s)
            if posture_time >= thresh_sec:
                if (now - last_alert_triggered_time) >= cooldown_sec:
                    should_alert = True
                    last_alert_triggered_time = now

        # 4. Vẽ khung xương MediaPipe
        connections = [
            ('LEFT_SHOULDER', 'RIGHT_SHOULDER'),
            ('LEFT_SHOULDER', 'LEFT_ELBOW'),
            ('LEFT_ELBOW', 'LEFT_WRIST'),
            ('RIGHT_SHOULDER', 'RIGHT_ELBOW'),
            ('RIGHT_ELBOW', 'RIGHT_WRIST'),
            ('LEFT_SHOULDER', 'NOSE'),
            ('RIGHT_SHOULDER', 'NOSE'),
            ('NOSE', 'LEFT_EAR'),
            ('NOSE', 'RIGHT_EAR')
        ]

        if hip_visible:
            connections.extend([
                ('LEFT_SHOULDER', 'LEFT_HIP'),
                ('RIGHT_SHOULDER', 'RIGHT_HIP'),
                ('LEFT_HIP', 'RIGHT_HIP')
            ])

        for p1, p2 in connections:
            if p1 in keypoints and p2 in keypoints:
                cv2.line(image, keypoints[p1], keypoints[p2], color, 2)

        # Vẽ đường gióng trục Cổ Tai - Vai
        cv2.line(image, shoulder_pt, ear_pt, (255, 200, 0), 2)
        cv2.circle(image, ear_pt, 5, (255, 200, 0), -1)
        cv2.circle(image, shoulder_pt, 5, (0, 255, 255), -1)

        # Chú thích lên ảnh
        image = draw_text_vietnamese(image, f"Cổ: {neck_angle}° | Lưng: {torso_angle}°", (15, 25), 22, color)
        image = draw_text_vietnamese(image, f"Góc cam: {camera_view_label}", (15, 55), 18, (255, 255, 255))
        image = draw_text_vietnamese(image, f"{posture_status}: {posture_time}s", (15, h - 35), 24, color)

    else:
        posture_status = "Không phát hiện người"
        image = draw_text_vietnamese(image, "Không thể nhận diện tư thế", (15, 30), 22, red)
        good_start_time = None
        bad_start_time = None

    # Hiển thị giờ Việt Nam chuẩn (UTC+7)
    vn_time_str = datetime.now(VN_TZ).strftime("%H:%M:%S")
    image = draw_text_vietnamese(image, f"Giờ VN: {vn_time_str}", (w - 180, h - 35), 20, (255, 255, 255))

    # Mã hóa JPEG đưa vào luồng phát Web
    ret, jpeg_buffer = cv2.imencode('.jpg', image, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
    frame_bytes = jpeg_buffer.tobytes()

    thresh_sec = system_settings.get("bad_posture_threshold_sec", 300)
    cooldown_sec = system_settings.get("alert_cooldown_sec", 60)
    speaker_enabled = system_settings.get("speaker_enabled", True)
    speaker_should_alert = should_alert and speaker_enabled

    # Lấy lệnh điều khiển Servo & Lệnh phát loa đang chờ để gửi kèm phản hồi về cho ESP32
    servo_cmd_to_send = ""
    track_to_play_now = 0
    with data_lock:
        if pending_servo_cmd is not None:
            servo_cmd_to_send = pending_servo_cmd
            pending_servo_cmd = None
        if pending_track_cmd > 0:
            track_to_play_now = pending_track_cmd
            pending_track_cmd = 0

    # Phân loại âm thanh cảnh báo theo 4 file MP3 chuẩn mới trong templates/
    # Track 1 (001.mp3): Khởi động thành công (dành riêng cho khởi động mạch / hệ thống sẵn sàng)
    # Track 2 (002.mp3): Cúi gập cổ quá sâu (Forward Head Posture)
    # Track 3 (003.mp3): Gù lưng / thân trên đổ gập ra trước (Slouching)
    # Track 4 (004.mp3): Sai tư thế phối hợp (cả cổ và lưng) hoặc lệch vai / vẹo người
    alert_track = 2  # Mặc định cảnh báo tư thế bắt đầu từ Track 2
    has_bad_neck = (neck_angle >= 32) or (view_type == "FRONT" and neck_angle >= 14)
    has_bad_torso = (torso_angle >= 18) or (not hip_visible and head_forward_offset > 55)

    if has_bad_neck and has_bad_torso:
        alert_track = 4  # Sai phối hợp cả cổ và lưng
    elif has_bad_torso:
        alert_track = 3  # Gù lưng
    elif has_bad_neck:
        alert_track = 2  # Cúi cổ
    else:
        alert_track = 4  # Sai tư thế khác (lệch vai, nghiêng đầu...)

    with data_lock:
        latest_processed_frame = frame_bytes
        status_data = {
            "status": posture_status,
            "camera_view": camera_view_label,
            "neck_angle": neck_angle,
            "torso_angle": torso_angle,
            "posture_time": posture_time,
            "good_posture_time": posture_time if posture_status == "Đúng tư thế" else 0,
            "bad_posture_time": posture_time if posture_status == "Sai tư thế" else 0,
            "error_message": error_msg.strip(),
            "alert": should_alert,
            "speaker_alert": speaker_should_alert,
            "track": alert_track,
            "bad_posture_threshold_sec": thresh_sec,
            "alert_cooldown_sec": cooldown_sec,
            "speaker_enabled": speaker_enabled,
            "last_update": now,
            "is_connected": True
        }

    return jsonify({
        "status": posture_status,
        "error_msg": error_msg.strip(),
        "camera_view": camera_view_label,
        "neck_angle": neck_angle,
        "torso_angle": torso_angle,
        "alert": speaker_should_alert,  # ESP32 chỉ phát loa khi còi bật
        "track": alert_track,           # Số thứ tự bài âm thanh (1, 2, 3, 4)
        "play_track": track_to_play_now, # Lệnh phát loa trực tiếp từ Web UI (1-4, 0 nếu không có)
        "app_alert": should_alert,
        "posture_time": posture_time,
        "bad_posture_threshold_sec": thresh_sec,
        "alert_cooldown_sec": cooldown_sec,
        "servo_cmd": servo_cmd_to_send
    }), 200

def generate_mjpeg_stream():
    """Phát luồng MJPEG thời gian thực cho trình duyệt Web"""
    while True:
        with data_lock:
            now = time.time()
            if latest_processed_frame is not None and (now - last_camera_packet_time <= 8):
                frame_to_send = latest_processed_frame
            else:
                msg = "Đang chờ tín hiệu từ ESP32-S3 CAM..." if last_camera_packet_time == 0 else "Mất kết nối với ESP32-S3 CAM!"
                frame_to_send = create_standby_frame(msg)

        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + frame_to_send + b'\r\n')
        time.sleep(0.08)

@app.route('/video_feed')
def video_feed():
    return Response(generate_mjpeg_stream(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/api/status')
def api_status():
    with data_lock:
        now = time.time()
        is_conn = (now - last_camera_packet_time) <= 8 if last_camera_packet_time > 0 else False
        res = dict(status_data)
        res["is_connected"] = is_conn
        if not is_conn and last_camera_packet_time > 0:
            res["status"] = "Mất kết nối camera"
            res["alert"] = False
    return jsonify(res)

@app.route('/api/tts')
def api_tts():
    text = request.args.get('text', 'Xin vui lòng điều chỉnh lại tư thế ngồi')
    try:
        tts = gTTS(text=text, lang='vi')
        fp = io.BytesIO()
        tts.write_to_fp(fp)
        fp.seek(0)
        return send_file(fp, mimetype="audio/mpeg")
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/settings', methods=['GET', 'POST'])
def api_settings():
    global system_settings
    if request.method == 'POST':
        try:
            data = request.get_json(silent=True) or request.form.to_dict()
            with data_lock:
                if "bad_posture_threshold_sec" in data:
                    system_settings["bad_posture_threshold_sec"] = int(data["bad_posture_threshold_sec"])
                if "alert_cooldown_sec" in data:
                    system_settings["alert_cooldown_sec"] = int(data["alert_cooldown_sec"])
                if "speaker_enabled" in data:
                    val = data["speaker_enabled"]
                    system_settings["speaker_enabled"] = val in [True, "true", "True", 1, "1"]
                if "vibrate_enabled" in data:
                    val = data["vibrate_enabled"]
                    system_settings["vibrate_enabled"] = val in [True, "true", "True", 1, "1"]
                if "voice_enabled" in data:
                    val = data["voice_enabled"]
                    system_settings["voice_enabled"] = val in [True, "true", "True", 1, "1"]
                if "notification_enabled" in data:
                    val = data["notification_enabled"]
                    system_settings["notification_enabled"] = val in [True, "true", "True", 1, "1"]
                save_system_settings(system_settings)
            return jsonify({"success": True, "settings": system_settings}), 200
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 400
    else:
        with data_lock:
            return jsonify(system_settings), 200

@app.route('/audio/<filename>')
def serve_template_audio(filename):
    """Phục vụ trực tiếp các file âm thanh (001.mp3 - 004.mp3) từ thư mục templates/"""
    templates_dir = os.path.join(os.path.dirname(__file__), "templates")
    return send_from_directory(templates_dir, filename)

@app.route('/api/servo', methods=['GET', 'POST'])
def api_servo():
    """Nhận lệnh điều khiển & hiệu chuẩn Pan-Tilt Servo từ Web UI và đẩy vào hàng đợi cho ESP32"""
    global pending_servo_cmd, latest_servo_status
    cmd = request.args.get('cmd')
    if not cmd and request.is_json:
        data = request.get_json(silent=True) or {}
        cmd = data.get('cmd')
    if not cmd and request.form:
        cmd = request.form.get('cmd')

    if not cmd:
        return jsonify({"success": False, "error": "Thiếu tham số cmd"}), 400

    cmd = str(cmd).strip().lower()

    # Xử lý các lệnh đặc thù kèm tham số (Slider Tilt, Slider Trim)
    if cmd == "tilt":
        angle = request.args.get('angle')
        if not angle and request.is_json:
            angle = (request.get_json(silent=True) or {}).get('angle')
        if not angle and request.form:
            angle = request.form.get('angle')
        if angle is not None:
            try:
                ang_int = max(35, min(145, int(angle)))
                cmd = f"tilt:{ang_int}"
            except Exception:
                pass
    elif cmd == "trim":
        val = request.args.get('val')
        if not val and request.is_json:
            val = (request.get_json(silent=True) or {}).get('val')
        if not val and request.form:
            val = request.form.get('val')
        if val is not None:
            try:
                val_int = max(80, min(100, int(val)))
                cmd = f"trim:{val_int}"
            except Exception:
                pass

    valid_prefixes = ('tilt:', 'trim:')
    valid_exact = {
        'up', 'down', 'left', 'right', 'stop', 'center', 'desk',
        'preset_desk', 'preset_center', 'preset_high', 'high',
        'jog_left', 'jog_right', 'rotate_left', 'rotate_right', 'sweep'
    }

    if cmd not in valid_exact and not any(cmd.startswith(p) for p in valid_prefixes):
        return jsonify({"success": False, "error": f"Lệnh không hợp lệ: {cmd}"}), 400

    with data_lock:
        pending_servo_cmd = cmd
        latest_servo_status = {"cmd": cmd, "time": time.time()}

    return jsonify({"success": True, "cmd": cmd, "queued": True}), 200

@app.route('/api/servo/status')
def api_servo_status():
    """Lấy trạng thái lệnh servo mới nhất"""
    with data_lock:
        return jsonify({
            "latest_cmd": latest_servo_status["cmd"],
            "last_time": latest_servo_status["time"],
            "has_pending": pending_servo_cmd is not None
        })

@app.route('/api/play_track', methods=['GET', 'POST'])
def api_play_track():
    """Nhận lệnh phát loa từ Web UI và đẩy vào hàng đợi gửi xuống ESP32-S3 qua /api/upload"""
    global pending_track_cmd
    track = request.args.get('track', type=int)
    if not track and request.is_json:
        track = (request.get_json(silent=True) or {}).get('track')
    if track in [1, 2, 3, 4]:
        with data_lock:
            pending_track_cmd = track
        return jsonify({"success": True, "track": track, "queued": True}), 200
    return jsonify({"success": False, "error": "Track không hợp lệ (1-4)"}), 400

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, threaded=True)
