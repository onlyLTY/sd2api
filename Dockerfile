FROM mcr.microsoft.com/playwright/python:v1.62.0-noble

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DISPLAY=:99

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        fonts-noto-cjk \
        novnc \
        openbox \
        python3-venv \
        tint2 \
        websockify \
        x11vnc \
        xvfb \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/sd2api-venv
ENV PATH="/opt/sd2api-venv/bin:${PATH}"

WORKDIR /app

COPY pyproject.toml README.md ./
COPY config.example.json ./config.example.json
COPY sd2api ./sd2api
RUN python -m pip install --no-cache-dir .

COPY docker/start-container.sh /usr/local/bin/start-sd2api
RUN chmod 0755 /usr/local/bin/start-sd2api \
    && mkdir -p /data/profiles /data/db /data/uploads

EXPOSE 8765 6080
VOLUME ["/data/profiles", "/data/db", "/data/uploads"]

CMD ["/usr/local/bin/start-sd2api"]
