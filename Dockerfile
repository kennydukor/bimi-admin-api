# syntax=docker/dockerfile:1.7
# ============================================================
# Bimi Admin API — production image
#
# Multi-stage build using a self-contained virtualenv. The builder compiles/
# installs everything into /opt/venv; the runtime stage copies that venv
# wholesale. Because a venv carries both its console scripts (gunicorn) AND its
# site-packages together, putting /opt/venv/bin on PATH makes both resolve — no
# PYTHONPATH juggling, no "ModuleNotFoundError: gunicorn".
#
# This Dockerfile assumes the app is at the build-context root (app/, scripts/,
# requirements.txt, docker-entrypoint.sh all at top level).
# ============================================================

# ---- builder ------------------------------------------------
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Build deps for any packages without a prebuilt wheel (asyncpg, bcrypt).
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

# Create the venv and install into it. Everything lands under /opt/venv.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt


# ---- runtime ------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # Put the venv first so `python`, `gunicorn`, etc. resolve to it.
    PATH="/opt/venv/bin:$PATH" \
    WEB_CONCURRENCY=4 \
    PORT=8000

# curl for the container HEALTHCHECK.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system app && useradd --system --gid app --home /app app

WORKDIR /app

# The whole virtualenv — scripts + packages together, so imports work.
COPY --from=builder /opt/venv /opt/venv

# App code (respecting .dockerignore).
COPY . .

RUN chmod +x docker-entrypoint.sh && chown -R app:app /app

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://localhost:${PORT}/health" || exit 1

ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["sh", "-c", "gunicorn app.main:app \
      --workers ${WEB_CONCURRENCY} \
      --worker-class uvicorn.workers.UvicornWorker \
      --bind 0.0.0.0:${PORT} \
      --timeout 60 \
      --graceful-timeout 30 \
      --access-logfile - \
      --error-logfile -"]
