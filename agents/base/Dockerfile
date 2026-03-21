FROM python:3.11-slim

WORKDIR /app

# system deps
RUN apt-get update && apt-get install -y \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# copy shared code
COPY shared /app/shared

# install deps
COPY shared/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# copy agent
COPY agent.py /app/agent.py

# workspace mount point
RUN mkdir -p /workspace

ENV PYTHONPATH=/app

CMD ["python", "agent.py"]