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

# Nhận diện môi trường Render Cloud hay máy tính Local (Edge)
IS_RENDER_ENV = (
    os.environ.get("RENDER", "").lower() == "true"
    or "RENDER_EXTERNAL_HOSTNAME" in os.environ
    or "RENDER_SERVICE_ID" in os.environ
)

# Khởi tạo MediaPipe Pose tối ưu tốc độ cao (Video Tracking Mode + Lite Model)
pose = mp_pose.Pose(
    static_image_mode=False,
    model_complexity=0,
    min_detection_confidence=0.5,
    min_tracking_confidence=0.5
)

# Khóa luồng (Thread Lock) để đồng bộ dữ liệu
data_lock = threading.Lock()

# ================= CẤU HÌNH HỆ THỐNG & TẦN SUẤT CẢNH BÁO =================
CONFIG_FILE = os.path.join(os.path.dirname(__file__), "app_config.json")

default_settings = {
    "esp32_ip": "10.247.173.81",       # Địa chỉ IP của ESP32-S3 Cam (tự động cập nhật)
    "camera_view_mode": "auto",        # Chế độ góc quay: auto (tự động), side (ngang hông), diagonal (chéo), front (chính diện)
    "rotate_180": True,                # Xoay ngược 180 độ theo hướng đặt camera
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
latest_raw_frame_bytes = None
new_raw_frame_event = threading.Event()
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
# Bộ đệm Font chữ tối ưu tốc độ vẽ (tránh tải lại font mỗi frame)
font_cache = {}

def get_vietnamese_font(font_size=24):
    if font_size in font_cache:
        return font_cache[font_size]

    font_candidates = [
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/calibri.ttf",
        "arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
        os.path.join(os.path.dirname(__file__), "Roboto-Regular.ttf"),
        "Roboto-Regular.ttf"
    ]

    for fpath in font_candidates:
        if os.path.exists(fpath):
            try:
                font = ImageFont.truetype(fpath, font_size)
                font_cache[font_size] = font
                return font
            except Exception:
                pass

    try:
        local_font_path = os.path.join(os.path.dirname(__file__), "Roboto-Regular.ttf")
        if not os.path.exists(local_font_path):
            font_url = "https://raw.githubusercontent.com/google/fonts/main/apache/roboto/Roboto-Regular.ttf"
            r = requests.get(font_url, timeout=3)
            if r.status_code == 200:
                with open(local_font_path, "wb") as f:
                    f.write(r.content)
        if os.path.exists(local_font_path):
            font = ImageFont.truetype(local_font_path, font_size)
            font_cache[font_size] = font
            return font
    except Exception:
        pass

    font = ImageFont.load_default()
    font_cache[font_size] = font
    return font

def draw_multiple_texts_vietnamese(image, text_items):
    """
    Vẽ nhiều dòng chữ tiếng Việt lên ảnh OpenCV chỉ với 1 lần chuyển đổi PIL duy nhất.
    text_items: list of tuples (text, position, font_size, color)
    """
    try:
        image_pil = Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))
        draw = ImageDraw.Draw(image_pil)
        for text, position, font_size, color in text_items:
            font = get_vietnamese_font(font_size)
            fill_color = (color[2], color[1], color[0]) if len(color) == 3 else color
            draw.text(position, text, font=font, fill=fill_color)
        return cv2.cvtColor(np.array(image_pil), cv2.COLOR_RGB2BGR)
    except Exception:
        for text, position, font_size, color in text_items:
            cv2.putText(image, text, position, cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)
        return image

def draw_text_vietnamese(image, text, position, font_size=24, color=(0, 255, 0)):
    return draw_multiple_texts_vietnamese(image, [(text, position, font_size, color)])

def calculate_angle_vertical(p1, p2):
    """Tính góc nghiêng của đường thẳng p1->p2 so với phương thẳng đứng (độ)"""
    try:
        dx = p2[0] - p1[0]
        dy = p2[1] - p1[1]
        radians = m.atan2(abs(dx), abs(dy) + 1e-6)
        return int(m.degrees(radians))
    except Exception:
        return 0

def calculate_signed_angle_vertical(p1, p2, facing_dir):
    """
    Tính góc nghiêng có dấu theo hướng mặt (facing_dir = +1 nhìn phải, -1 nhìn trái):
    - Dương (+): Ngả/cúi về phía trước
    - Âm (-): Ngửa ra phía sau
    """
    try:
        dx = (p2[0] - p1[0]) * facing_dir
        dy = abs(p2[1] - p1[1]) + 1e-6
        angle = int(m.degrees(m.atan2(abs(dx), dy)))
        return angle if dx >= 0 else -angle
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
    forced_view = system_settings.get("camera_view_mode", "auto")

    ls = lm[mp_pose.PoseLandmark.LEFT_SHOULDER.value]
    rs = lm[mp_pose.PoseLandmark.RIGHT_SHOULDER.value]
    le = lm[mp_pose.PoseLandmark.LEFT_EAR.value]
    re = lm[mp_pose.PoseLandmark.RIGHT_EAR.value]
    nose = lm[mp_pose.PoseLandmark.NOSE.value]

    # Xác định bên nào hướng về phía camera rõ hơn
    left_score = (ls.visibility + le.visibility) / 2.0
    right_score = (rs.visibility + re.visibility) / 2.0
    use_left = left_score >= right_score

    # Nếu người dùng đã khóa góc quay thủ công
    if forced_view == "side":
        return ("SIDE_LEFT" if use_left else "SIDE_RIGHT"), use_left
    elif forced_view == "diagonal":
        return ("DIAGONAL_LEFT" if use_left else "DIAGONAL_RIGHT"), use_left
    elif forced_view == "front":
        return "FRONT", use_left

    # TỰ ĐỘNG PHÂN BIỆT GÓC CHÍNH XÁC (BẤT BIẾN THEO KHOẢNG CÁCH NGỒI XA/GẦN):
    min_sh_x = min(ls.x, rs.x)
    max_sh_x = max(ls.x, rs.x)
    mid_sh_x = (ls.x + rs.x) / 2.0
    shoulder_dx = abs(ls.x - rs.x)

    # Tỷ lệ lệch của mũi so với tâm 2 vai (0.0 = ngay chính giữa, >= 0.5 = sát mép vai hoặc ra ngoài)
    nose_center_offset_ratio = abs(nose.x - mid_sh_x) / max(shoulder_dx, 0.05)
    nose_outside = (nose.x < min_sh_x - 0.02) or (nose.x > max_sh_x + 0.02)

    visible_ear = le if use_left else re
    ear_nose_dx = abs(visible_ear.x - nose.x)
    ear_vis_diff = abs(le.visibility - re.visibility)

    # 1. Ngang hông 90°: 2 vai chập vào nhau (shoulder_dx rất nhỏ), hoặc mũi chìa hẳn ra ngoài 2 vai, hoặc 1 tai bị che khuất hẳn
    if shoulder_dx < 0.12 or nose_outside or ear_vis_diff > 0.45:
        return ("SIDE_LEFT" if use_left else "SIDE_RIGHT"), use_left

    # 2. Chính diện (Frontal): Mũi nằm gần tâm giữa 2 vai (lệch < 28% bề rộng vai)
    if nose_center_offset_ratio < 0.28 and ear_vis_diff <= 0.40:
        return "FRONT", use_left

    # 3. Chéo bàn 3/4 (45°): Mũi lệch về một bên vai nhưng chưa vượt hẳn ra ngoài
    if ear_nose_dx > 0.09 or nose_center_offset_ratio >= 0.28:
        return ("DIAGONAL_LEFT" if use_left else "DIAGONAL_RIGHT"), use_left

    return "FRONT", use_left

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

def process_single_frame(image):
    """Phân tích 1 khung hình qua MediaPipe Pose và vẽ kết quả lên ảnh"""
    global latest_processed_frame, last_camera_packet_time
    global good_start_time, bad_start_time, last_alert_triggered_time, status_data

    # Xoay ngược 180 độ nếu camera lắp ngược
    if system_settings.get("rotate_180", True):
        image = cv2.rotate(image, cv2.ROTATE_180)

    now = time.time()
    last_camera_packet_time = now
    h, w = image.shape[:2]

    # Resize 320x240 để MediaPipe chạy siêu tốc (~15ms)
    small_rgb = cv2.resize(image, (320, 240))
    small_rgb = cv2.cvtColor(small_rgb, cv2.COLOR_BGR2RGB)
    results = pose.process(small_rgb)

    green = (0, 255, 127) # Chuẩn
    red = (50, 50, 255)   # Sai
    neck_angle = 0
    torso_angle = 0
    posture_status = "Đang tìm cơ thể..."
    posture_type = "KHÔNG RÕ"
    posture_detail = "Đang tìm cơ thể..."
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

        ear_lm = lm[mp_pose.PoseLandmark.LEFT_EAR.value] if use_left else lm[mp_pose.PoseLandmark.RIGHT_EAR.value]
        shoulder_lm = lm[mp_pose.PoseLandmark.LEFT_SHOULDER.value] if use_left else lm[mp_pose.PoseLandmark.RIGHT_SHOULDER.value]
        hip_lm = lm[mp_pose.PoseLandmark.LEFT_HIP.value] if use_left else lm[mp_pose.PoseLandmark.RIGHT_HIP.value]
        nose_lm = lm[mp_pose.PoseLandmark.NOSE.value]

        ear_pt = (int(ear_lm.x * w), int(ear_lm.y * h))
        shoulder_pt = (int(shoulder_lm.x * w), int(shoulder_lm.y * h))
        hip_pt = (int(hip_lm.x * w), int(hip_lm.y * h))
        nose_pt = (int(nose_lm.x * w), int(nose_lm.y * h))

        hip_visible = (hip_lm.visibility >= 0.55 and hip_pt[1] < h - 20)

        # 2. Xử lý thuật toán thích ứng theo từng góc camera (phân biệt rõ Quá cúi vs Ngửa ra sau)
        ls_pt = keypoints.get('LEFT_SHOULDER', (0, 0))
        rs_pt = keypoints.get('RIGHT_SHOULDER', (0, 0))
        mid_shoulder_pt = ((ls_pt[0] + rs_pt[0]) // 2, (ls_pt[1] + rs_pt[1]) // 2)

        if view_type == "FRONT":
            camera_view_label = "Chính diện (Frontal)"
            le_pt = keypoints.get('LEFT_EAR', (0, 0))
            re_pt = keypoints.get('RIGHT_EAR', (0, 0))

            neck_angle = calculate_angle_horizontal(le_pt, re_pt)
            torso_angle = calculate_angle_horizontal(ls_pt, rs_pt)

            shoulder_width_px = abs(ls_pt[0] - rs_pt[0])
            nose_shoulder_dist = abs(nose_pt[1] - mid_shoulder_pt[1])
            nose_shoulder_ratio = nose_shoulder_dist / max(shoulder_width_px, 1.0)
            slouch_forward = (nose_shoulder_ratio < 0.30)

            is_bad = (neck_angle >= 14 or torso_angle >= 10 or slouch_forward)
            if slouch_forward:
                posture_type = "QUÁ CÚI"
                posture_detail = "Cúi sát màn hình"
                error_msg = "Bạn đang cúi quá sát màn hình. Hãy lùi người ra sau!"
            elif neck_angle >= 14 or torso_angle >= 10:
                posture_type = "NGHIÊNG LỆCH"
                posture_detail = "Nghiêng đầu / Lệch vai"
                error_msg = f"Nghiêng đầu ({neck_angle}°) hoặc lệch vai ({torso_angle}°). Hãy ngồi cân bằng!"
            else:
                posture_type = "CHUẨN"
                posture_detail = "Đúng tư thế"

        else: # DIAGONAL hoặc SIDE (góc nhìn ngang/chéo - phân biệt 2 lỗi chính: Quá cúi vs Ngửa ra sau)
            side_name = "Hông Trái" if use_left else "Hông Phải"
            if "DIAGONAL" in view_type:
                camera_view_label = f"Chéo Bàn 3/4 ({side_name})"
                neck_fwd_thresh = 32
                torso_fwd_thresh = 18
                # Dùng tâm 2 vai (mid_shoulder_pt) để loại bỏ sai số chiều ngang vai ở góc 45°
                neck_base_pt = mid_shoulder_pt
            else:
                camera_view_label = f"Ngang Hông 90° ({side_name})"
                neck_fwd_thresh = 35
                torso_fwd_thresh = 16
                neck_base_pt = shoulder_pt

            # Hướng mặt (+1 nếu mặt quay sang phải, -1 nếu quay sang trái)
            facing_dir = 1 if nose_pt[0] >= ear_pt[0] else -1
            neck_signed = calculate_signed_angle_vertical(neck_base_pt, ear_pt, facing_dir)
            neck_angle = abs(neck_signed)

            if hip_visible:
                torso_signed = calculate_signed_angle_vertical(hip_pt, shoulder_pt, facing_dir)
                torso_angle = abs(torso_signed)
            else:
                # Khi bàn che khuất hông, đo từ tâm 2 vai tới mũi để không bị cộng thêm nửa bề rộng vai
                torso_signed = calculate_signed_angle_vertical(mid_shoulder_pt, nose_pt, facing_dir)
                torso_angle = abs(torso_signed)

            head_forward_offset = (ear_pt[0] - neck_base_pt[0]) * facing_dir

            # Lỗi 1: Ngửa ra sau quá mức (trượt dài / dựa ghế quá sâu >= 18°)
            is_lean_back = (hip_visible and torso_signed <= -18) or (not hip_visible and neck_signed <= -22)
            # Lỗi 2: Quá cúi / Gù lưng về phía trước
            torso_check_thresh = torso_fwd_thresh if hip_visible else 28
            is_lean_forward = (
                (neck_signed >= neck_fwd_thresh)
                or (torso_signed >= torso_check_thresh)
                or (not hip_visible and head_forward_offset > 55)
            )

            is_bad = is_lean_back or is_lean_forward
            if is_lean_back:
                posture_type = "NGỬA RA SAU"
                posture_detail = "Ngửa ra sau quá mức"
                error_msg = f"Ngửa người ra sau quá mức ({torso_angle}°). Hãy ngồi ngay ngắn lại, tránh trượt dài trên ghế!"
            elif is_lean_forward:
                posture_type = "QUÁ CÚI"
                posture_detail = "Quá cúi / Gù lưng"
                error_msg = f"Ngồi quá cúi / Gù lưng (Cổ {neck_angle}°, Lưng {torso_angle}°). Hãy ngẩng đầu và ngồi thẳng lưng lên!"
            else:
                posture_type = "CHUẨN"
                posture_detail = "Đúng tư thế"

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

        cv2.line(image, shoulder_pt, ear_pt, (255, 200, 0), 2)
        cv2.circle(image, ear_pt, 5, (255, 200, 0), -1)
        cv2.circle(image, shoulder_pt, 5, (0, 255, 255), -1)

        vn_time_str = datetime.now(VN_TZ).strftime("%H:%M:%S")
        image = draw_multiple_texts_vietnamese(image, [
            (f"Cổ: {neck_angle}° | Lưng: {torso_angle}°", (15, 25), 22, color),
            (f"Góc cam: {camera_view_label}", (15, 55), 18, (255, 255, 255)),
            (f"{posture_detail}: {posture_time}s", (15, h - 35), 22, color),
            (f"Giờ VN: {vn_time_str}", (w - 180, h - 35), 20, (255, 255, 255))
        ])

    else:
        posture_status = "Không phát hiện người"
        posture_type = "KHÔNG RÕ"
        posture_detail = "Không phát hiện người"
        good_start_time = None
        bad_start_time = None
        vn_time_str = datetime.now(VN_TZ).strftime("%H:%M:%S")
        image = draw_multiple_texts_vietnamese(image, [
            ("Không thể nhận diện tư thế", (15, 30), 22, red),
            (f"Giờ VN: {vn_time_str}", (w - 180, h - 35), 20, (255, 255, 255))
        ])

    ret, jpeg_buffer = cv2.imencode('.jpg', image, [int(cv2.IMWRITE_JPEG_QUALITY), 75])
    frame_bytes = jpeg_buffer.tobytes()

    if posture_type == "NGỬA RA SAU":
        alert_track = 4
    elif posture_type == "QUÁ CÚI":
        has_bad_neck = (neck_angle >= 32) or (view_type == "FRONT" and neck_angle >= 14)
        has_bad_torso = (torso_angle >= 16) or (not hip_visible and head_forward_offset > 55)
        if has_bad_neck and has_bad_torso:
            alert_track = 4
        elif has_bad_torso:
            alert_track = 3
        else:
            alert_track = 2
    else:
        alert_track = 4

    thresh_sec = system_settings.get("bad_posture_threshold_sec", 300)
    cooldown_sec = system_settings.get("alert_cooldown_sec", 60)
    speaker_enabled = system_settings.get("speaker_enabled", True)
    speaker_should_alert = should_alert and speaker_enabled

    # NẾU CẦN CẢNH BÁO: BẮN LỆNH QUA ESP32 TRONG THREAD RIÊNG KHÔNG CHẶN VIDEO
    if speaker_should_alert and not IS_RENDER_ENV:
        esp_ip = system_settings.get("esp32_ip", "10.247.173.81")
        def _trigger_speaker(ip, trk):
            try: requests.get(f"http://{ip}/play?track={trk}", timeout=1.0)
            except Exception: pass
        threading.Thread(target=_trigger_speaker, args=(esp_ip, alert_track), daemon=True).start()

    with data_lock:
        latest_processed_frame = frame_bytes
        status_data = {
            "status": posture_status,
            "posture_type": posture_type,
            "posture_detail": posture_detail,
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

    return {
        "status": posture_status,
        "posture_type": posture_type,
        "posture_detail": posture_detail,
        "error_msg": error_msg.strip(),
        "camera_view": camera_view_label,
        "neck_angle": neck_angle,
        "torso_angle": torso_angle,
        "alert": speaker_should_alert,
        "track": alert_track,
        "posture_time": posture_time,
        "bad_posture_threshold_sec": thresh_sec,
        "alert_cooldown_sec": cooldown_sec
    }

@app.route('/api/upload', methods=['POST'])
def upload_frame():
    """Tương thích ngược: Nhận ảnh từ ESP32 nếu chạy chế độ client POST"""
    img_bytes = request.data
    if not img_bytes and 'file' in request.files:
        img_bytes = request.files['file'].read()

    if not img_bytes:
        return jsonify({"error": "No image data received"}), 400

    np_arr = np.frombuffer(img_bytes, np.uint8)
    image = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)

    if image is None:
        return jsonify({"error": "Failed to decode image"}), 400

    resp = process_single_frame(image)
    return jsonify(resp), 200

# ================= KÉO LUỒNG VIDEO MJPEG TỪ ESP32 CỔNG 81 (25-30 FPS) =================
import urllib.request

def get_local_subnet_prefix():
    """Lấy tiền tố mạng LAN hiện tại của máy tính, ví dụ: 10.247.173. hoặc 192.168.1."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        parts = local_ip.split('.')
        if len(parts) == 4:
            return f"{parts[0]}.{parts[1]}.{parts[2]}."
    except Exception:
        pass
    return "10.247.173."

def auto_discover_esp32_ip():
    """Tự động quét nhanh dải IP mạng LAN để tìm ESP32 nếu IP bị thay đổi"""
    found_ip = None
    prefix = get_local_subnet_prefix()

    def _probe(ip_str):
        nonlocal found_ip
        if found_ip: return
        try:
            r = requests.get(f"http://{ip_str}/status", timeout=0.3)
            if r.status_code == 200 and "ESP32-S3-POSTURE" in r.text:
                found_ip = ip_str
        except Exception:
            pass

    threads = []
    scan_list = list(range(75, 95)) + list(range(50, 75)) + list(range(2, 50)) + list(range(95, 200))
    for i in scan_list:
        t = threading.Thread(target=_probe, args=(f"{prefix}{i}",), daemon=True)
        threads.append(t)
        t.start()
    for t in threads:
        t.join(timeout=0.35)
    return found_ip

def esp32_stream_worker():
    """Luồng nền độc lập liên tục kéo luồng MJPEG từ ESP32-S3 cổng 81 với tốc độ cao nhất (Zero-Latency)"""
    global latest_raw_frame_bytes
    if IS_RENDER_ENV:
        return  # Trên Render không kéo trực tiếp mà nhận sync từ Local

    print("[STREAM WORKER] Bắt đầu luồng kéo video MJPEG từ ESP32-S3 cổng 81 (Zero-Latency)...")
    consecutive_errs = 0

    while True:
        esp_ip = system_settings.get("esp32_ip", "10.247.173.81")
        url = f"http://{esp_ip}:81/stream"
        try:
            r = requests.get(url, stream=True, timeout=(3.0, 8.0))
            if r.status_code == 200:
                print(f"[STREAM WORKER] ✅ Đã kết nối thành công tới luồng video: {url}")
                consecutive_errs = 0
                bytes_data = b""

                for chunk in r.iter_content(chunk_size=4096):
                    if not chunk:
                        break
                    bytes_data += chunk

                    # Bóc tách khung hình JPEG, CHỈ GIỮ LẠI KHUNG MỚI NHẤT, BỎ KHUNG CŨ ĐỂ TRIỆT TIÊU ĐỘ TRỄ
                    last_valid_jpg = None
                    while True:
                        a = bytes_data.find(b'\xff\xd8') # JPEG SOI
                        if a == -1:
                            bytes_data = bytes_data[-1:] if len(bytes_data) > 0 else b""
                            break
                        if a > 0:
                            bytes_data = bytes_data[a:]

                        b = bytes_data.find(b'\xff\xd9', 2) # JPEG EOI sau SOI
                        if b == -1:
                            break

                        last_valid_jpg = bytes_data[:b+2]
                        bytes_data = bytes_data[b+2:]

                    if last_valid_jpg is not None:
                        latest_raw_frame_bytes = last_valid_jpg
                        new_raw_frame_event.set()
            else:
                consecutive_errs += 1
        except Exception as e:
            consecutive_errs += 1

        if consecutive_errs >= 3:
            try:
                new_ip = auto_discover_esp32_ip()
                if new_ip and new_ip != esp_ip:
                    print(f"[AUTO DISCOVER] Phát hiện ESP32 ở IP mới: {new_ip}")
                    system_settings["esp32_ip"] = new_ip
                    save_system_settings(system_settings)
                    consecutive_errs = 0
            except Exception:
                pass
        time.sleep(1.0)

def ai_processor_worker():
    """Luồng xử lý AI riêng biệt: Luôn lấy khung hình MỚI NHẤT từ luồng nhận, triệt tiêu 100% trễ tích lũy"""
    global latest_raw_frame_bytes
    if IS_RENDER_ENV:
        return

    print("[AI WORKER] Khởi động luồng xử lý AI độc lập (Zero-Queue)...")
    while True:
        new_raw_frame_event.wait(timeout=0.05)
        new_raw_frame_event.clear()

        raw_bytes = latest_raw_frame_bytes
        if raw_bytes is None:
            continue

        try:
            arr = np.frombuffer(raw_bytes, dtype=np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if img is not None:
                process_single_frame(img)
        except Exception as e:
            pass

stream_thread = threading.Thread(target=esp32_stream_worker, daemon=True)
stream_thread.start()

ai_thread = threading.Thread(target=ai_processor_worker, daemon=True)
ai_thread.start()

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
        time.sleep(0.02) # Giảm trễ luồng Web xuống ~50 FPS mượt mà

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

@app.route('/api/sync', methods=['POST'])
def api_sync():
    """Nhận dữ liệu đồng bộ từ Local Server lên Cloud Render"""
    global latest_processed_frame, status_data, last_camera_packet_time
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "No JSON data"}), 400

    with data_lock:
        if "status_data" in data and isinstance(data["status_data"], dict):
            status_data.update(data["status_data"])
            status_data["last_update"] = time.time()
            status_data["is_connected"] = True
        last_camera_packet_time = time.time()

        if "frame_b64" in data and data["frame_b64"]:
            import base64
            try:
                latest_processed_frame = base64.b64decode(data["frame_b64"])
            except Exception as e:
                print(f"[SYNC] Lỗi giải mã frame_b64: {e}")

    return jsonify({"success": True, "message": "Synced successfully"}), 200

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

    # Bắn trực tiếp sang ESP32 cổng 80: http://{esp_ip}/servo?cmd={cmd}
    if not IS_RENDER_ENV:
        esp_ip = system_settings.get("esp32_ip", "10.247.173.81")
        def _forward_servo(ip, c):
            try: requests.get(f"http://{ip}/servo?cmd={c}", timeout=1.0)
            except Exception: pass
        threading.Thread(target=_forward_servo, args=(esp_ip, cmd), daemon=True).start()

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
    """Nhận lệnh phát loa từ Web UI và gửi xuống ESP32-S3 cổng 80"""
    global pending_track_cmd
    track = request.args.get('track', type=int)
    if not track and request.is_json:
        track = (request.get_json(silent=True) or {}).get('track')
    if track in [1, 2, 3, 4]:
        with data_lock:
            pending_track_cmd = track
        if not IS_RENDER_ENV:
            esp_ip = system_settings.get("esp32_ip", "10.247.173.81")
            def _forward_track(ip, t):
                try: requests.get(f"http://{ip}/play?track={t}", timeout=1.0)
                except Exception: pass
            threading.Thread(target=_forward_track, args=(esp_ip, track), daemon=True).start()
        return jsonify({"success": True, "track": track, "queued": True}), 200
    return jsonify({"success": False, "error": "Track không hợp lệ (1-4)"}), 400

@app.route('/api/esp32_ip', methods=['GET', 'POST'])
def api_esp32_ip():
    """Xem hoặc cập nhật IP của ESP32-S3 Camera"""
    ip = request.args.get('ip')
    if not ip and request.is_json:
        ip = (request.get_json(silent=True) or {}).get('ip')
    if ip:
        system_settings["esp32_ip"] = ip.strip()
        save_system_settings(system_settings)
        return jsonify({"success": True, "esp32_ip": system_settings["esp32_ip"]})
    return jsonify({"esp32_ip": system_settings.get("esp32_ip", "10.247.173.81")})

@app.route('/api/rotate', methods=['GET', 'POST'])
def api_rotate():
    """Bật/tắt xoay khung hình camera 180 độ"""
    req_val = request.args.get('enabled')
    if req_val is not None:
        system_settings["rotate_180"] = (req_val.lower() in ['true', '1', 'yes'])
    else:
        system_settings["rotate_180"] = not system_settings.get("rotate_180", True)
    save_system_settings(system_settings)
    return jsonify({"success": True, "rotate_180": system_settings["rotate_180"]})

@app.route('/api/camera_view_mode', methods=['GET', 'POST'])
def api_camera_view_mode():
    """Xem hoặc cài đặt chế độ góc quay camera: auto, side, diagonal, front"""
    mode = request.args.get('mode')
    if not mode and request.is_json:
        mode = (request.get_json(silent=True) or {}).get('mode')
    if mode in ['auto', 'side', 'diagonal', 'front']:
        system_settings["camera_view_mode"] = mode
        save_system_settings(system_settings)
        return jsonify({"success": True, "camera_view_mode": mode})
    return jsonify({"camera_view_mode": system_settings.get("camera_view_mode", "auto")})

# ================= ĐỒNG BỘ LOCAL -> CLOUD RENDER (EDGE - CLOUD HYBRID) =================
CLOUD_SYNC_ENDPOINT = os.environ.get("CLOUD_SYNC_URL", "https://ai-sitting-posture-1.onrender.com/api/sync")

def background_cloud_sync():
    """Chạy ngầm trên máy tính Local: Đẩy dữ liệu và ảnh nén lên Cloud Render định kỳ ~0.3s (3 FPS) giảm trễ tối đa"""
    import base64
    if IS_RENDER_ENV:
        return  # Đang chạy trên Render thì không tự sync lên chính nó

    print(f"[HYBRID] Đã kích hoạt luồng đồng bộ Cloud -> {CLOUD_SYNC_ENDPOINT}")
    last_sync_time = 0

    while True:
        time.sleep(0.1)
        now = time.time()
        # Đồng bộ nhanh mỗi 0.35 giây (~3 FPS) khi có dữ liệu camera
        if now - last_sync_time < 0.35:
            continue

        payload = None
        with data_lock:
            if latest_processed_frame is not None and (now - last_camera_packet_time <= 8):
                try:
                    payload = {
                        "status_data": dict(status_data),
                        "frame_b64": base64.b64encode(latest_processed_frame).decode('utf-8')
                    }
                except Exception:
                    pass

        if payload:
            try:
                r = requests.post(CLOUD_SYNC_ENDPOINT, json=payload, timeout=1.8)
                if r.status_code == 200:
                    last_sync_time = now
            except Exception:
                pass

sync_thread = threading.Thread(target=background_cloud_sync, daemon=True)
sync_thread.start()

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, threaded=True)
