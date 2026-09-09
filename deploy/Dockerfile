# TradeCard approval shim — production image.
#
# Build:
#   docker build -f deploy/Dockerfile -t tradecard-shim:v1.0.0 .
#
# Run (persistent volume for the SQLite db + writeups):
#   docker run --rm -d --name tradecard-shim \
#     -p 8787:8787 \
#     -v tradecard-state:/data \
#     -e TRADECARD_AUTH_TOKEN="$(openssl rand -base64 32)" \
#     tradecard-shim:v1.0.0
#
# The image ships nothing broker-side — no trading engine, no keys, no
# credentials. The shim is the wire between the customer's own engine
# (on-box or LAN-adjacent) and their signing card. Runs as an unprivileged
# user; the /data volume is where the shim persists cards, prompts, and
# passbook rows. Vendor operates nothing here (per the zero-vendor-infra
# decision, NEXT_SESSION.md §11) — this image is published to Docker Hub
# for customer self-hosting.

FROM python:3.13-slim AS base

# System hardening: no shell login for the app user, no build tools left
# in the final image, no root at runtime.
RUN groupadd --system --gid 10001 tradecard && \
    useradd --system --uid 10001 --gid tradecard --home /home/tradecard \
        --shell /usr/sbin/nologin --create-home tradecard

# Install deps first so a code-only change hits the layer cache.
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir '.[shim]' && \
    rm -rf /root/.cache

# State volume — SQLite db + writeups live here across restarts.
RUN mkdir -p /data && chown -R tradecard:tradecard /data
VOLUME ["/data"]

USER tradecard
EXPOSE 8787

# TRADECARD_AUTH_TOKEN can be injected at run-time; if unset, the shim
# runs OPEN and prints a loopback-only warning (safe defaults).
ENV TRADECARD_DB=/data/approval.db \
    TRADECARD_WRITEUP_DIR=/data/intel_writeups \
    TRADECARD_HOST=0.0.0.0 \
    TRADECARD_PORT=8787

# The entry-point resolves the four env vars into CLI flags, then execs
# tradecard-shim. Kept as a single line so PID 1 is uvicorn (proper
# SIGTERM handling for graceful shutdown).
ENTRYPOINT ["/bin/sh", "-c", "exec tradecard-shim \
    --host \"$TRADECARD_HOST\" --port \"$TRADECARD_PORT\" \
    --db \"$TRADECARD_DB\" --writeup-dir \"$TRADECARD_WRITEUP_DIR\" \
    ${TRADECARD_AUTH_TOKEN:+--auth-token \"$TRADECARD_AUTH_TOKEN\"}"]

# Healthcheck hits the public /healthz surface — no auth needed even when
# TRADECARD_AUTH_TOKEN is set.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request, sys; \
        sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8787/healthz', timeout=2).status == 200 else 1)"
