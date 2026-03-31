FROM python:3.13-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends git openssh-client curl build-essential librdkafka-dev \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://claude.ai/install.sh | CLAUDE_CODE_INSTALL_DIR=/usr/local/bin bash

COPY shared/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY . .

ENV PYTHONPATH=/app

# Container entrypoint: copies host claude credentials into writable ~/.claude/
# and writes a permissive settings.json so agents can use Bash/Read/Write/Edit tools.
COPY infra/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "orchestrator/worker.py"]
