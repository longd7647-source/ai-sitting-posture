import os
import io
import time
import math as m
import cv2
import numpy as np
import threading
from datetime import datetime
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

# Khởi tạo MediaPipe Pose (Tối ưu RAM 512MB: model_complexity=1, static_image_mode=False)
pose = mp_pose.Pose(
    static_image_mode=False,
    model_complexity=1,
    smooth_landmarks=True,
    min_detection_confidence=0.5,
    min_tracking_confidence=0.5
)

# Khóa luồng (Thread Lock) để đồng bộ dữ liệu giữa luồng POST ảnh từ ESP32-S3 và luồng Web GET
data_lock = threading.Lock()

# Biến lưu trữ khung hình mới nhất để phát qua /video_feed
latest_processed_frame = None
last_camera_packet_time = 0

# Biến trạng thái phân tích tư thế
status_data = {
    "status": "Đang chờ camera...",
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
            print("Đang tải font Roboto hỗ trợ tiếng Việt cho Linux...")
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

def find_angle(x1, y1, x2, y2):
    """Tính góc nghiêng so với trục thẳng đứng thẳng góc theo công thức gốc"""
    try:
        denominator = m.sqrt((x2 - x1)**2 + (y2 - y1)**2) * abs(y1)
        if denominator == 0:
            return 90
        cos_theta = ((y2 - y1) * (-y1)) / denominator
        cos_theta = max(-1.0, min(1.0, cos_theta))
        theta = m.acos(cos_theta)
        return int(180 / m.pi * theta)
    except Exception:
        return 90

def create_standby_frame(message="Đang chờ tín hiệu từ ESP32-S3 CAM..."):
    """Tạo khung hình nền đen chờ kết nối khi camera chưa bật"""
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    cv2.rectangle(frame, (20, 20), (620, 460), (50, 50, 50), 2)
    cv2.circle(frame, (320, 200), 50, (0, 165, 255), 3)
    cv2.putText(frame, "CAM", (295, 210), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 165, 255), 2)
    frame = draw_text_vietnamese(frame, message, (130, 280), font_size=26, color=(200, 200, 200))
    current_time_str = datetime.now().strftime("%H:%M:%S")
    frame = draw_text_vietnamese(frame, f"Thời gian server: {current_time_str}", (200, 330), font_size=20, color=(150, 150, 150))
    ret, buffer = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
    return buffer.tobytes()

@app.route('/')
def index():
    """Giao diện Dashboard chính"""
    return render_template('index.html')

@app.route('/ping')
def ping():
    """Endpoint keep-alive cho cron-job để Render không ngủ"""
    return "OK", 200

@app.route('/api/upload', methods=['POST'])
def upload_frame():
    """Nhận raw JPEG bytes từ ESP32-S3-WROOM CAM và xử lý phân tích tư thế"""
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

    green = (0, 255, 127) # Xanh lá chuẩn BGR
    red = (50, 50, 255)   # Đỏ chuẩn BGR
    neck_inclination = 0
    torso_inclination = 0
    posture_status = "Đang tìm cơ thể..."
    posture_time = 0
    error_msg = ""
    should_alert = False
    color = green

    if results.pose_landmarks:
        lm = results.pose_landmarks.landmark
        
        # Kiểm tra độ rõ nét (visibility) giữa bên trái và bên phải để đặt cam bên nào cũng nhận đúng
        left_visibility = (lm[mp_pose.PoseLandmark.LEFT_SHOULDER.value].visibility + 
                           lm[mp_pose.PoseLandmark.LEFT_HIP.value].visibility) / 2.0
        right_visibility = (lm[mp_pose.PoseLandmark.RIGHT_SHOULDER.value].visibility + 
                            lm[mp_pose.PoseLandmark.RIGHT_HIP.value].visibility) / 2.0

        use_left = left_visibility >= right_visibility

        shoulder_lm = lm[mp_pose.PoseLandmark.LEFT_SHOULDER.value] if use_left else lm[mp_pose.PoseLandmark.RIGHT_SHOULDER.value]
        hip_lm = lm[mp_pose.PoseLandmark.LEFT_HIP.value] if use_left else lm[mp_pose.PoseLandmark.RIGHT_HIP.value]
        nose_lm = lm[mp_pose.PoseLandmark.NOSE.value]

        shoulder_pt = (int(shoulder_lm.x * w), int(shoulder_lm.y * h))
        hip_pt = (int(hip_lm.x * w), int(hip_lm.y * h))
        nose_pt = (int(nose_lm.x * w), int(nose_lm.y * h))

        # Tính góc cổ và góc lưng theo công thức gốc
        neck_inclination = find_angle(shoulder_pt[0], shoulder_pt[1], nose_pt[0], nose_pt[1])
        torso_inclination = find_angle(hip_pt[0], hip_pt[1], shoulder_pt[0], shoulder_pt[1])

        neck_inclination = min(max(neck_inclination, 0), 90)
        torso_inclination = min(max(torso_inclination, 0), 90)

        # Đánh giá tư thế: Cổ < 40 độ và Lưng < 10 độ là đúng
        is_good = (neck_inclination < 40 and torso_inclination < 10)

        if is_good:
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

            if neck_inclination >= 40:
                error_msg += "Cổ quá cúi. "
            if torso_inclination >= 10:
                error_msg += "Lưng quá nghiêng. "

            # Nếu ngồi sai liên tục >= 6 giây -> kích hoạt cờ cảnh báo
            if posture_time >= 6:
                should_alert = True
                last_alert_triggered_time = now

        # Vẽ khung xương MediaPipe
        keypoints = {landmark.name: (int(lm[landmark.value].x * w), int(lm[landmark.value].y * h))
                     for landmark in mp_pose.PoseLandmark}

        connections = [
            ('LEFT_SHOULDER', 'RIGHT_SHOULDER'),
            ('LEFT_SHOULDER', 'LEFT_HIP'),
            ('RIGHT_SHOULDER', 'RIGHT_HIP'),
            ('LEFT_HIP', 'RIGHT_HIP'),
            ('LEFT_SHOULDER', 'NOSE'),
            ('RIGHT_SHOULDER', 'NOSE'),
            ('NOSE', 'LEFT_EAR'),
            ('NOSE', 'RIGHT_EAR'),
            ('LEFT_SHOULDER', 'LEFT_ELBOW'),
            ('LEFT_ELBOW', 'LEFT_WRIST'),
            ('RIGHT_SHOULDER', 'RIGHT_ELBOW'),
            ('RIGHT_ELBOW', 'RIGHT_WRIST'),
            ('LEFT_HIP', 'LEFT_KNEE'),
            ('RIGHT_HIP', 'RIGHT_KNEE')
        ]

        for p1, p2 in connections:
            if p1 in keypoints and p2 in keypoints:
                cv2.line(image, keypoints[p1], keypoints[p2], color, 2)

        for pt_name in ['NOSE', 'LEFT_SHOULDER', 'RIGHT_SHOULDER', 'LEFT_HIP', 'RIGHT_HIP']:
            if pt_name in keypoints:
                cv2.circle(image, keypoints[pt_name], 5, color, -1)

        side_str = "Hông Trái" if use_left else "Hông Phải"
        image = draw_text_vietnamese(image, f"Cổ: {neck_inclination}° | Lưng: {torso_inclination}° ({side_str})", (15, 25), 22, color)
        image = draw_text_vietnamese(image, f"{posture_status}: {posture_time}s", (15, h - 35), 24, color)

    else:
        posture_status = "Không phát hiện người"
        image = draw_text_vietnamese(image, "Không thể nhận diện tư thế", (15, 30), 22, red)
        good_start_time = None
        bad_start_time = None

    # Hiển thị thời gian hiện tại ở góc dưới phải
    current_time_str = datetime.now().strftime("%H:%M:%S")
    image = draw_text_vietnamese(image, f"Giờ: {current_time_str}", (w - 140, h - 35), 20, (255, 255, 255))

    # Mã hóa JPEG đưa vào luồng phát Web
    ret, jpeg_buffer = cv2.imencode('.jpg', image, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
    frame_bytes = jpeg_buffer.tobytes()

    with data_lock:
        latest_processed_frame = frame_bytes
        status_data = {
            "status": posture_status,
            "neck_angle": neck_inclination,
            "torso_angle": torso_inclination,
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
        "neck_angle": neck_inclination,
        "torso_angle": torso_inclination,
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
        time.sleep(0.1)

@app.route('/video_feed')
def video_feed():
    """Route xem trực tiếp video trên trình duyệt Web"""
    return Response(generate_mjpeg_stream(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/api/status')
def api_status():
    """API trả về JSON tình trạng tư thế và cảnh báo cho Web"""
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
    """Tạo giọng nói tiếng Việt bằng gTTS xuất thẳng ra bộ nhớ (không tạo file rác)"""
    text = request.args.get('text', 'Xin vui lòng điều chỉnh lại tư thế ngồi')
    try:
        tts = gTTS(text=text, lang='vi')
        fp = io.BytesIO()
        tts.write_to_fp(fp)
        fp.seek(0)
        return send_file(fp, mimetype="audio/mpeg")
    except Exception as e:
        print(f"Lỗi tạo TTS: {e}")
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, threaded=True)
