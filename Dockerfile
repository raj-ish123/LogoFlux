# ── Base image ────────────────────────────────────────────────────────────────
FROM python:3.11-slim

# ── System deps: ffmpeg + OpenCV runtime libs ─────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# ── Python deps ───────────────────────────────────────────────────────────────
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── App code (everything not excluded by .dockerignore) ───────────────────────
COPY . .

# ── Runtime ───────────────────────────────────────────────────────────────────
# CAP injects $PORT at runtime. 1 worker + long timeout for video processing.
ENV PORT=8000
EXPOSE 8000

CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT} --timeout 300 --workers 1 logoswap_app:app"]
