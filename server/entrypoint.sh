#!/bin/sh
set -e
mkdir -p /data && chown -R appuser:appuser /data
exec gosu appuser uvicorn main:app \
    --host 0.0.0.0 \
    --port 8000 \
    --proxy-headers \
    --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-127.0.0.1}" \
    --ws-per-message-deflate false
