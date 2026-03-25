FROM python:3.11-slim

WORKDIR /app

COPY shared/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir asyncpg qdrant-client

COPY memory/ /app/memory/
COPY orchestrator/ /app/orchestrator/
COPY shared/ /app/shared/
COPY skills/ /app/skills/

ENV PYTHONPATH=/app

CMD ["python", "-m", "orchestrator.meta_agent_worker"]
