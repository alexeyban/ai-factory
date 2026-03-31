FROM python:3.13-slim

WORKDIR /app

RUN pip install --no-cache-dir fastapi uvicorn temporalio httpx jinja2 python-multipart

COPY orchestrator/ /app/orchestrator/
COPY shared/ /app/shared/
COPY web/ /app/web/

ENV PYTHONPATH=/app

CMD ["uvicorn", "web.app:app", "--host", "0.0.0.0", "--port", "8088"]
