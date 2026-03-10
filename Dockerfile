FROM python:3.11-slim

# System deps for Prophet (cmdstan) and supervisord
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    g++ \
    supervisor \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (Docker layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code (bust cache on every deploy)
ARG CACHE_BUST=1
COPY . .

# Create data directory for volume mount fallback
RUN mkdir -p /data

# Entrypoint script handles DB init + supervisord
RUN chmod +x /app/entrypoint.sh

EXPOSE 8501

ENTRYPOINT ["/app/entrypoint.sh"]
