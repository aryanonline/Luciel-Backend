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

# --- builder: install the LOCKED dependency set into a venv via uv ---
FROM base AS builder
# uv is pinned to the version CI uses (setup-uv `version:` in ci.yml): the lockfile
# was written by it and `--frozen` installs it verbatim. Bump both together.
COPY --from=ghcr.io/astral-sh/uv:0.12.23 /uv /usr/local/bin/uv
WORKDIR /app
# --frozen: install exactly uv.lock, never re-resolve — the image ships the versions
# the CI run on the same commit tested (round 7 WP-9: before the lock, installs
# floated within the pyproject ranges, so a green commit could deploy different
# versions than it tested). A pyproject change without a matching lock update fails
# CI's `uv lock --check` step first.
# --no-dev: runtime deps only (no extras; the dev extra is CI's).
# UV_PYTHON + UV_PYTHON_DOWNLOADS=never: the venv is built on the base image's own
# interpreter, which the runtime stage shares; uv never fetches a different one.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON=/usr/local/bin/python3.12 \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1
# Dependency manifests first for layer caching: third-party deps install in a layer
# that only a pyproject/lock change invalidates.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
ENV VIRTUAL_ENV=/opt/venv PATH="/opt/venv/bin:$PATH"
COPY . .
# Then the project itself (editable, as before: /app is copied into the runtime too).
RUN uv sync --frozen --no-dev

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
# --timeout-graceful-shutdown 22: see scripts/app-entrypoint.sh.
CMD uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips "${LUCIEL_TRUSTED_PROXY_CIDRS:-*}" --timeout-keep-alive 75 --timeout-graceful-shutdown 22
