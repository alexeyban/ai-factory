FROM python:3.11-slim

WORKDIR /app

COPY shared /app/shared
COPY agents/dev/agent.py /app/agent.py
COPY shared/requirements.txt .

RUN apt-get update \
    && apt-get install -y --no-install-recommends git openssh-client \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt

ENV PYTHONPATH=/app

CMD ["python", "agent.py"]
