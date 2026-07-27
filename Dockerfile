FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .

# 云平台会通过 PORT 环境变量告诉容器该监听哪个端口
ENV PORT=8000
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT}
