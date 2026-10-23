# glibc-based slim image: tls_client loads a prebuilt Go shared library (.so) that
# does not work on musl/alpine. amd64 and arm64 (aarch64) are both supported.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/vendor/spotapi \
    HOME=/tmp \
    DATA_DIR=/data \
    PORT=8080

# No apt step: python:*-slim already ships ca-certificates (needed by the Go TLS client).
# PID 1 / signal handling: compose sets `init: true` (docker's tini).

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt \
 # fail the build early if the tls_client native library cannot be loaded on this arch
 && python -c "import tls_client; tls_client.Session(client_identifier='chrome_120')" \
 && test -s /etc/ssl/certs/ca-certificates.crt

COPY vendor/ ./vendor/
COPY app/ ./app/
RUN python -c "import spotapi, app.main" \
 && mkdir -p /data && chown 1000:1000 /data

USER 1000:1000
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8080\")}/api/v1/health', timeout=4).status==200 else 1)"

# Exactly ONE worker: the Spotify session/websocket is cached in-process.
CMD ["sh", "-c", "exec uvicorn app.main:app_factory --factory --host 0.0.0.0 --port ${PORT} --workers 1 --proxy-headers --no-server-header"]
