FROM python:3.10-slim

# Cài đặt các thư viện đồ họa còn thiếu của Linux
RUN apt-get update && apt-get install -y \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Cài đặt thư viện Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Chạy server
CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:10000"]
