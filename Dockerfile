FROM python:3.10-slim

# Cài đặt thư viện hệ thống cần thiết cho OpenCV, MediaPipe và font chữ
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    fonts-dejavu-core \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Sao chép và cài đặt các thư viện Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Sao chép toàn bộ mã nguồn
COPY . .

# Mở cổng lắng nghe mặc định
ENV PORT=10000
EXPOSE 10000

# Khởi chạy Gunicorn với 1 worker và 4 threads (bắt buộc 1 worker để chia sẻ bộ nhớ MJPEG và State)
CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:10000", "--workers", "1", "--threads", "4", "--timeout", "120"]
