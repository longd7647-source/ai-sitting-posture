import os
import io
import time
import math as m
import cv2
import numpy as np
import threading
from datetime import datetime, timezone, timedelta
from flask import Flask, Response, render_template, request, jsonify, send_file
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

            if posture_time >= 5: # Sai liên tục >= 5 giây -> báo động
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
            "last_update": now,
            "is_connected": True
        }

    return jsonify({
        "status": posture_status,
        "camera_view": camera_view_label,
        "neck_angle": neck_angle,
        "torso_angle": torso_angle,
        "alert": should_alert,
        "posture_time": posture_time
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

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, threaded=True)
