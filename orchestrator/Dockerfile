FROM python:3.13-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends git openssh-client \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir temporalio confluent-kafka[avro] openai pytest

COPY . .

ENV PYTHONPATH=/app

CMD ["python", "orchestrator/worker.py"]
