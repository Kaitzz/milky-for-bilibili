FROM python:3.12-slim

WORKDIR /app

# 安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制源码
COPY . .

# 默认状态存储在 /data（Railway Volume 挂载点）
ENV STATE_DIR=/data

CMD ["python", "main.py"]
