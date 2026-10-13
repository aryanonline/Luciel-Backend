# Luciel backend — production image (Phase B compute, §5.5).
# Multi-stage: build deps with uv, run a slim non-root runtime.
FROM python:3.12.15-slim AS base
# Apply OS security upgrades at build time so the image doesn't ship stale base
# CVEs. The python:3.12-slim base carries perl (CVE-2026-12087, CRITICAL) which
# ECR scan-on-push blocks (§5.14); `apt-get upgrade` pulls the patched perl even
# though we never install postgresql-client. Refresh the base tag periodically too.
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# --- builder: install dependencies into a venv via uv ---
FROM base AS builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
# Copy dependency manifests first for layer caching.
COPY pyproject.toml uv.lock* CLAUDE.md ./
# Create the venv and install runtime deps (no dev extras in prod).
RUN uv venv /opt/venv
ENV VIRTUAL_ENV=/opt/venv PATH="/opt/venv/bin:$PATH"
COPY . .
RUN uv pip install --python /opt/venv/bin/python -e . "psycopg[binary]" "pydantic[email]"

# --- runtime: slim, non-root ---
FROM base AS runtime
# bash for the migrate scripts. We deliberately do NOT install postgresql-client:
# it pulls in perl (CVE-2026-12087, CRITICAL). SQL is run via scripts/run_sql.py
# using psycopg (already in the venv), so psql is unnecessary.
RUN apt-get update && apt-get install -y --no-install-recommends bash \
    && rm -rf /var/lib/apt/lists/*
RUN groupadd -r luciel && useradd -r -g luciel luciel
WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /app /app
ENV VIRTUAL_ENV=/opt/venv PATH="/opt/venv/bin:$PATH"
USER luciel
EXPOSE 8000
# Health: GET /healthz (see app/main.py). ALB target group checks this path.
# --proxy-headers: the ALB terminates TLS, so without it request.url reconstructs
# as http:// and Twilio's X-Twilio-Signature (signed over the https:// webhook URL)
# never matches. Keep in sync with scripts/app-entrypoint.sh.
# --forwarded-allow-ips: the VPC CIDR (LUCIEL_TRUSTED_PROXY_CIDRS, set by CDK), so
# the client address is the one the ALB appended, not a spoofed leftmost entry.
# Falls back to "*" only where no proxy exists (local docker run).
# --timeout-keep-alive 75: MUST exceed the ALB idle timeout (60s default); the 5s
# uvicorn default made the ALB reuse dead connections -> 502s (2026-08-18).
CMD uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips "${LUCIEL_TRUSTED_PROXY_CIDRS:-*}" --timeout-keep-alive 75
