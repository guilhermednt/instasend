FROM cgr.dev/chainguard/python:latest-dev@sha256:3ca53097911a867be4f8190b8bc272d1465e9d95ef32ecc2cd70d841e2baeac3 AS builder

WORKDIR /app
RUN python -m venv /app/venv
ENV PATH="/app/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt


FROM cgr.dev/chainguard/python:latest@sha256:3a89a9027f47e44e9f9da201493013249a02fc87fa7269bba80f621d7ade83f8

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
