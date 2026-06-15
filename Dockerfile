# ────────────────────────────────────────────────────────────────────────────
# TRON-X  Dockerfile
# Multi-stage build: builder installs deps, runtime image is lean.
# ────────────────────────────────────────────────────────────────────────────

# ── Stage 1: dependency builder ──────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

# System deps needed to compile some Python wheels
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    git \
    libssl-dev \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip \
 && pip install --prefix=/install --no-cache-dir -r requirements.txt


# ── Stage 2: runtime image ───────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

# Non-root user for security
RUN useradd --create-home --shell /bin/bash tronx
WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application source
COPY --chown=tronx:tronx . /app

# Directories that need to persist between restarts
RUN mkdir -p /data/tronx && chown -R tronx:tronx /data
ENV HOME=/data

# Runtime env (overridden by docker-compose env_file or -e flags)
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    APP_ENV=production \
    DEBUG=false \
    LOG_LEVEL=INFO

# Port
EXPOSE 8000

# Switch to non-root
USER tronx

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health')"

# Production server: 2 workers, no reload, access log off for performance
CMD ["uvicorn", "src.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "2", \
     "--loop", "uvloop", \
     "--http", "httptools", \
     "--no-access-log"]
