from flask import Flask, request, jsonify
import cv2
import numpy as np
import time

# Import MediaPipe chuẩn trên Linux tránh lỗi 'no attribute solutions'
import mediapipe as mp
try:
    import mediapipe.python.solutions.pose as mp_pose
except ImportError:
    mp_pose = mp.solutions.pose

app = Flask(__name__)

# Khởi tạo model Pose
pose = mp_pose.Pose(
    static_image_mode=True,
    model_complexity=0,
    min_detection_confidence=0.5
)

THRESHOLD_NECK = 40.0
THRESHOLD_BACK = 10.0
BAD_POSTURE_DURATION = 5.0

bad_posture_start_time = None

def calculate_angle(p1, p2):
    dx = p2[0] - p1[0]
    dy = p2[1] - p1[1]
    return np.degrees(np.arctan2(abs(dx), abs(dy)))

@app.route('/', methods=['GET'])
def home():
    return "AI Sitting Posture Cloud Server is Running!"

@app.route('/predict', methods=['POST'])
def predict():
    global bad_posture_start_time
    
    img_bytes = request.data
    if not img_bytes:
        return jsonify({"error": "No image data"}), 400

    np_arr = np.frombuffer(img_bytes, np.uint8)
    image = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
    if image is None:
        return jsonify({"error": "Decode failed"}), 400

    h, w, _ = image.shape
    rgb_image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    results = pose.process(rgb_image)

    current_time = time.time()
    neck_angle = 0.0
    back_angle = 0.0
    should_alert = False
    status = "Khong tim thay nguoi"

    if results.pose_landmarks:
        lm = results.pose_landmarks.landmark
        ear = (lm[mp_pose.PoseLandmark.LEFT_EAR.value].x * w, lm[mp_pose.PoseLandmark.LEFT_EAR.value].y * h)
        shoulder = (lm[mp_pose.PoseLandmark.LEFT_SHOULDER.value].x * w, lm[mp_pose.PoseLandmark.LEFT_SHOULDER.value].y * h)
        hip = (lm[mp_pose.PoseLandmark.LEFT_HIP.value].x * w, lm[mp_pose.PoseLandmark.LEFT_HIP.value].y * h)

        neck_angle = float(calculate_angle(shoulder, ear))
        back_angle = float(calculate_angle(hip, shoulder))

        if neck_angle >= THRESHOLD_NECK or back_angle >= THRESHOLD_BACK:
            if bad_posture_start_time is None:
                bad_posture_start_time = current_time
            elapsed = current_time - bad_posture_start_time
            if elapsed >= BAD_POSTURE_DURATION:
                should_alert = True
                status = f"Sai tu the ({elapsed:.1f}s)"
            else:
                status = f"Canh bao lech ({elapsed:.1f}s)"
        else:
            bad_posture_start_time = None
            status = "Tu the tot"

    return jsonify({
        "status": status,
        "neck_angle": round(neck_angle, 1),
        "back_angle": round(back_angle, 1),
        "alert": should_alert
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
