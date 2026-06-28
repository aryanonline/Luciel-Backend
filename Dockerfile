# Luciel backend — production image (Phase B compute, §5.5).
# Multi-stage: build deps with uv, run a slim non-root runtime.
FROM python:3.12-slim AS base
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# --- builder: install dependencies into a venv via uv ---
FROM base AS builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
# Copy dependency manifests first for layer caching.
COPY pyproject.toml uv.lock* README.md ./
# Create the venv and install runtime deps (no dev extras in prod).
RUN uv venv /opt/venv
ENV VIRTUAL_ENV=/opt/venv PATH="/opt/venv/bin:$PATH"
COPY . .
RUN uv pip install --python /opt/venv/bin/python -e . "psycopg[binary]" "pydantic[email]"

# --- runtime: slim, non-root ---
FROM base AS runtime
RUN groupadd -r luciel && useradd -r -g luciel luciel
WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /app /app
ENV VIRTUAL_ENV=/opt/venv PATH="/opt/venv/bin:$PATH"
USER luciel
EXPOSE 8000
# Health: GET /healthz (see app/main.py). ALB target group checks this path.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
