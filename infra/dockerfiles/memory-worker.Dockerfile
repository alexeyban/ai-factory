FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY shared/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir \
        asyncpg \
        qdrant-client \
        prometheus_client \
        opentelemetry-api \
        opentelemetry-sdk \
        opentelemetry-exporter-otlp-proto-grpc

# Copy application code
COPY memory/ /app/memory/
COPY shared/ /app/shared/
COPY skills/ /app/skills/

ENV PYTHONPATH=/app

EXPOSE 9091

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:9091/health || exit 1

CMD ["python", "-m", "memory.worker"]
