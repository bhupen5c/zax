# Zax — container image for Railway / Render / Fly / any Docker host.
FROM python:3.12-slim

WORKDIR /app

# System deps: ffmpeg helps edge-tts; curl for the healthcheck.
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Bind all interfaces and keep state on the mounted volume. $PORT is injected
# by the host (config.py reads it); 8777 is the local default.
ENV ZAX_HOST=0.0.0.0 \
    ZAX_DATA_DIR=/data \
    PYTHONUNBUFFERED=1

EXPOSE 8777
CMD ["python", "-m", "zax"]
