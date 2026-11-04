FROM ghcr.io/home-assistant/base:latest
RUN apk add --no-cache python3 py3-pip \
 && python3 -m venv /opt/bluetti-venv
ENV PATH="/opt/bluetti-venv/bin:${PATH}"
RUN pip install --no-cache-dir "bluetti-bt-lib==0.1.8" "paho-mqtt==2.1.0"
COPY rootfs /
RUN chmod +x /run.sh
CMD ["/run.sh"]
