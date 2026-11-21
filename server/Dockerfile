FROM cgr.dev/chainguard/python:latest-dev@sha256:c047e6d1e47b09144a771bfde6595a00c49dc8571caa7dd62df88634e84304ae AS builder

WORKDIR /app
RUN python -m venv /app/venv
ENV PATH="/app/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt


FROM cgr.dev/chainguard/python:latest@sha256:f6e5c2e056393d48fa8afa1ea58a1394db4cd5de1a8f0f48105c8e88fcd4f9d3

WORKDIR /app
ENV PATH="/app/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    DB_PATH=/data/shares.db

COPY --from=builder /app/venv /app/venv
COPY main.py protocol.py ./

# Runs as the image's built-in non-root user (nonroot, UID 65532).
# The /data bind mount must be writable by that UID:
#   sudo chown -R 65532:65532 server/data
# uvicorn reads FORWARDED_ALLOW_IPS from the environment (.env).
EXPOSE 8000

ENTRYPOINT ["uvicorn", "main:app", \
    "--host", "0.0.0.0", \
    "--port", "8000", \
    "--proxy-headers", \
    "--ws-per-message-deflate", "false"]
