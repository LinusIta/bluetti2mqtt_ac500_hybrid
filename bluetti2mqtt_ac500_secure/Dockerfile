FROM python:3.12-slim-bookworm
RUN pip install --no-cache-dir "bleak==1.1.1" "paho-mqtt==2.1.0"
COPY rootfs/app /app
WORKDIR /app
ENV PYTHONUNBUFFERED=1
CMD ["python", "/app/bridge.py"]
