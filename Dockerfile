# syntax=docker/dockerfile:1
# ── Stage 1: builder ──────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

# Install uv
RUN pip install --no-cache-dir uv

# Copy dependency files first (layer cache)
COPY pyproject.toml .
COPY src/ src/

# Create venv and install all deps (no dev extras)
RUN uv venv /opt/venv && \
    uv pip install --python /opt/venv/bin/python -e ".[dev]"

# ── Stage 2: runtime ──────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

WORKDIR /app

# Copy the venv from builder
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /build/src /app/src

# Make venv the default Python
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH="/app/src" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Non-root user for security
RUN addgroup --system maneki && adduser --system --ingroup maneki maneki
USER maneki

# Render injects $PORT at runtime; default to 8000 for local docker run
EXPOSE 8000
CMD uvicorn maneki.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1
