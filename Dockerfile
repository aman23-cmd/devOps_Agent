# ═══════════════════════════════════════════════════════════════
#  DevOps Pipeline Agent — Production Docker Image
#  Multi-stage build for minimal final image size
# ═══════════════════════════════════════════════════════════════

# ── Stage 1: Builder ─────────────────────────────────────────
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

# Install OS-level build deps (psycopg2 needs libpq-dev, gcc)
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc libpq-dev && \
    rm -rf /var/lib/apt/lists/*

# Install Python deps into a virtual env (keeps final image clean)
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install -r requirements.txt


# ── Stage 2: Runtime ─────────────────────────────────────────
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Only runtime C libs needed (no gcc)
RUN apt-get update && \
    apt-get install -y --no-install-recommends libpq5 curl && \
    rm -rf /var/lib/apt/lists/*

# Copy venv from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# Copy application code
COPY agents/ ./agents/
COPY api/ ./api/
COPY config/ ./config/
COPY db/ ./db/

# Create non-root user for security
RUN groupadd -r devops && useradd -r -g devops devops
USER devops

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

EXPOSE 8000

# Default: run the webhook API
CMD ["uvicorn", "api.webhook_receiver:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
