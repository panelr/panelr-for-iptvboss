FROM python:3.12-slim

RUN useradd --uid 1000 --create-home middleware
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY middleware ./middleware

ENV MW_DATA_DIR=/data \
    MW_LISTEN_PORT=8002 \
    PYTHONUNBUFFERED=1
RUN mkdir -p /data && chown middleware:middleware /data
VOLUME /data
USER middleware
EXPOSE 8002

HEALTHCHECK --interval=30s --timeout=6s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8002/middleware/v1/health', timeout=5)"

CMD ["python", "-m", "middleware"]
