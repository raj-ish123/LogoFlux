# ── Base image ────────────────────────────────────────────────────────────────
FROM python:3.11-slim

# ── System deps: ffmpeg + build tools for opencv ──────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# ── Python deps ───────────────────────────────────────────────────────────────
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── App code ──────────────────────────────────────────────────────────────────
COPY logoswap/     ./logoswap/
COPY logoswap_app.py .

# ── Runtime ───────────────────────────────────────────────────────────────────
# Render sets $PORT; gunicorn binds to it.
# 1 worker because video processing is CPU-heavy; 300 s timeout for long renders.
ENV PORT=10000
EXPOSE 10000

CMD gunicorn --bind "0.0.0.0:$PORT" --timeout 300 --workers 1 logoswap_app:app
