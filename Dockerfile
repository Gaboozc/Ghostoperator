FROM python:3.12-slim

# Tesseract (OCR local) con español
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr tesseract-ocr-spa tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY agent ./agent
COPY eval ./eval

ENV DATA_DIR=/data PYTHONUNBUFFERED=1
RUN mkdir -p /data
EXPOSE 8000
# Railway inyecta $PORT
CMD ["sh", "-c", "uvicorn agent.api:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
