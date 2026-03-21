FROM python:3.11-slim

WORKDIR /app

COPY shared /app/shared
COPY orchestrator /app/orchestrator
COPY shared/requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

ENV PYTHONPATH=/app

CMD ["python", "orchestrator/main.py"]