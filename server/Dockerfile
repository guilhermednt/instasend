FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py protocol.py entrypoint.sh ./
RUN chmod +x entrypoint.sh \
    && adduser --disabled-password --gecos "" appuser \
    && mkdir -p /data \
    && chown appuser:appuser /data

EXPOSE 8000

ENTRYPOINT ["./entrypoint.sh"]
