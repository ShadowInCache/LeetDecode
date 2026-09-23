# Explicit build for the LeetDecode API.
#
# A Dockerfile rather than relying on Railway's builder auto-detection: the
# build is then identical on Railway, locally, and anywhere else, and it cannot
# break because a platform changed its detection heuristics.

FROM python:3.12-slim

# - PYTHONDONTWRITEBYTECODE: no .pyc clutter in the image
# - PYTHONUNBUFFERED: logs reach stdout immediately, which is what Railway's
#   log viewer reads; without it output can sit in a buffer for minutes
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first, in their own layer: application code changes on every
# deploy, requirements rarely do, so this keeps the slow step cached.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Don't run as root.
RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

# Documentation only - Railway routes to whatever $PORT it injects.
EXPOSE 8080

# Shell form on purpose, so ${PORT} is expanded at runtime. Railway sets PORT;
# the default keeps `docker run` working locally without one.
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}
