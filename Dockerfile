# Optional: for hosts that run containers (Render, Google Cloud Run, Fly.io, ...)
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr poppler-utils fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

ENV PORT=8080
# Set APP_PASSWORD as an environment variable / secret on your host.
CMD streamlit run app.py --server.port=${PORT} --server.address=0.0.0.0 --server.headless=true
