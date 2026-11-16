FROM cgr.dev/chainguard/python:latest-dev AS builder

WORKDIR /app
RUN python -m venv /app/venv
ENV PATH="/app/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt


FROM cgr.dev/chainguard/python:latest

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
