FROM python:3.13-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ghostscript poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ .

ENV PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    IPP_PORT=631 \
    WEB_PORT=8080 \
    SECONDS_PER_PAGE=2

VOLUME /data
EXPOSE 631 8080

CMD ["python", "server.py"]
