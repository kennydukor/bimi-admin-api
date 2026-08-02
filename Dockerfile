# syntax=docker/dockerfile:1.7
# ============================================================
# Bimi Admin API — production image
#
# Multi-stage: a builder stage compiles wheels, the final stage copies only
# the installed packages and the app, runs as a non-root user, and serves with
# gunicorn managing uvicorn workers. Small, reproducible, and safe to publish.
# ============================================================

# ---- builder ------------------------------------------------
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Build deps only needed to compile wheels (asyncpg, bcrypt). Kept out of final.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# Install into an isolated prefix we can copy wholesale into the runtime image.
RUN pip install --prefix=/install -r requirements.txt


# ---- runtime ------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/install/bin:$PATH" \
    # gunicorn tuning; override at deploy time if needed.
    WEB_CONCURRENCY=4 \
    PORT=8000

# curl is used by the container HEALTHCHECK below.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system app && useradd --system --gid app --home /app app

WORKDIR /app

# Copy the pre-built site-packages from the builder.
COPY --from=builder /install /install

# App code (respecting .dockerignore).
COPY . .

# entrypoint runs optional migrations, then execs the server.
RUN chmod +x docker-entrypoint.sh && chown -R app:app /app

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://localhost:${PORT}/health" || exit 1

ENTRYPOINT ["./docker-entrypoint.sh"]
# gunicorn with uvicorn workers = async app + graceful multi-worker management.
CMD ["sh", "-c", "gunicorn app.main:app \
      --workers ${WEB_CONCURRENCY} \
      --worker-class uvicorn.workers.UvicornWorker \
      --bind 0.0.0.0:${PORT} \
      --timeout 60 \
      --graceful-timeout 30 \
      --access-logfile - \
      --error-logfile -"]
