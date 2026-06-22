# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm

# Run as an unprivileged user (defense in depth: limits blast radius of any RCE).
RUN useradd --create-home --uid 10001 appuser
WORKDIR /app

# Install deps first for better layer caching.
COPY requirements.txt requirements.txt
RUN pip3 install --no-cache-dir -r requirements.txt gunicorn

# Application code. Secrets/venv/frontend/db/media are excluded via .dockerignore.
COPY --chown=appuser:appuser . .

USER appuser
EXPOSE 8080

# gthread workers: most endpoints do blocking network I/O (Semantic Scholar /
# Ollama) or PDF parsing; LLM_ENABLED=false in production means no Ollama
# calls happen here at all (see config.py).
CMD ["gunicorn", "-b", "0.0.0.0:8080", \
     "--workers", "2", "--threads", "4", "--timeout", "60", \
     "app:app"]
