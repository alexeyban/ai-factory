FROM python:3.11-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends git openssh-client curl \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://claude.ai/install.sh | CLAUDE_CODE_INSTALL_DIR=/usr/local/bin bash

COPY shared/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir asyncpg qdrant-client

COPY memory/ /app/memory/
COPY orchestrator/ /app/orchestrator/
COPY shared/ /app/shared/
COPY skills/ /app/skills/
COPY infra/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV PYTHONPATH=/app

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "orchestrator.meta_agent_worker"]
