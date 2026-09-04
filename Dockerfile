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
# CAP injects $PORT at runtime.
# IMPORTANT: keep --workers 1 so the in-memory job store (_jobs) is shared;
# --threads lets that single worker serve concurrent /poll requests while a
# job runs in a background thread. Long timeout covers heavy video renders.
ENV PORT=8000
EXPOSE 8000

CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT} --timeout 600 --workers 1 --threads 8 logoswap_app:app"]
