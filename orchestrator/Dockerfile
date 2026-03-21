FROM python:3.13-slim

WORKDIR /app

RUN pip install --no-cache-dir temporalio confluent-kafka[avro] openai

COPY . .

ENV PYTHONPATH=/app

CMD ["python", "orchestrator/worker.py"]