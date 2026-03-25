FROM python:3.11-slim

WORKDIR /app

COPY shared/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY memory/ /app/memory/
COPY shared/ /app/shared/

ENV PYTHONPATH=/app

CMD ["python", "-m", "memory.reward_worker"]
