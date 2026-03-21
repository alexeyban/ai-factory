FROM python:3.11-slim

WORKDIR /app

COPY shared /app/shared
COPY agents/qa/agent.py /app/agent.py
COPY shared/requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

ENV PYTHONPATH=/app

CMD ["python", "agent.py"]