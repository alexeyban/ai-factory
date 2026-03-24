# Phase 8 — Infrastructure (Production)

**Приоритет:** Medium
**Зависимости:** Phase 0 (базовая структура)
**Блокирует:** все сервисы в production

---

## Цель

Масштабируемая production-готовая инфраструктура: отдельные Docker-сервисы для каждого компонента, полная карта Kafka-топиков, OpenTelemetry трейсинг.

---

## Шаги реализации

### Шаг 1 — Docker Compose расширение

**Файл для изменения:** `docker-compose.yml`

Добавить следующие сервисы (в дополнение к уже существующим temporal, kafka, postgresql):

#### memory-worker
```yaml
memory-worker:
  build:
    context: .
    dockerfile: infra/dockerfiles/memory-worker.Dockerfile
  environment:
    - MEMORY_DB_URL=${MEMORY_DB_URL}
    - QDRANT_URL=${QDRANT_URL}
    - KAFKA_BOOTSTRAP_SERVERS=${KAFKA_BOOTSTRAP_SERVERS}
    - OTEL_EXPORTER_OTLP_ENDPOINT=${OTEL_ENDPOINT:-http://otel-collector:4317}
  ports:
    - "8080:8080"   # Prometheus metrics endpoint
  depends_on:
    - postgresql
    - qdrant
    - kafka
  healthcheck:
    test: ["CMD", "curl", "-f", "http://localhost:8080/health"]
    interval: 30s
    retries: 3
```

#### reward-worker
```yaml
reward-worker:
  build:
    context: .
    dockerfile: infra/dockerfiles/reward-worker.Dockerfile
  environment:
    - MEMORY_DB_URL=${MEMORY_DB_URL}
    - KAFKA_BOOTSTRAP_SERVERS=${KAFKA_BOOTSTRAP_SERVERS}
    - REWARD_CORRECTNESS_W=${REWARD_CORRECTNESS_W:-1.0}
    - REWARD_PERF_W=${REWARD_PERF_W:-0.3}
    - REWARD_COMPLEXITY_W=${REWARD_COMPLEXITY_W:-0.2}
    - OTEL_EXPORTER_OTLP_ENDPOINT=${OTEL_ENDPOINT:-http://otel-collector:4317}
  depends_on:
    - kafka
    - memory-worker
```

#### meta-agent
```yaml
meta-agent:
  build:
    context: .
    dockerfile: infra/dockerfiles/meta-agent.Dockerfile
  environment:
    - MEMORY_DB_URL=${MEMORY_DB_URL}
    - TEMPORAL_ADDRESS=${TEMPORAL_ADDRESS}
    - KAFKA_BOOTSTRAP_SERVERS=${KAFKA_BOOTSTRAP_SERVERS}
    - SKILL_OPTIMIZE_EVERY_N=${SKILL_OPTIMIZE_EVERY_N:-10}
  depends_on:
    - temporal
    - memory-worker
```

#### qdrant (если не добавлен в Phase 1)
```yaml
qdrant:
  image: qdrant/qdrant:latest
  ports:
    - "6333:6333"
  volumes:
    - qdrant_data:/qdrant/storage
```

#### prometheus + grafana (если не добавлены в Phase 7)
```yaml
prometheus:
  image: prom/prometheus:latest
  ports:
    - "9090:9090"
  volumes:
    - ./infra/prometheus.yml:/etc/prometheus/prometheus.yml
    - prometheus_data:/prometheus

grafana:
  image: grafana/grafana:latest
  ports:
    - "3000:3000"
  volumes:
    - grafana_data:/var/lib/grafana
  depends_on:
    - prometheus
```

#### OpenTelemetry Collector (опционально)
```yaml
otel-collector:
  image: otel/opentelemetry-collector:latest
  volumes:
    - ./infra/otel-collector.yml:/etc/otel-collector-config.yml
  ports:
    - "4317:4317"   # gRPC
    - "4318:4318"   # HTTP
```

---

### Шаг 2 — Dockerfiles

**Файлы для создания в `infra/dockerfiles/`:**

#### `memory-worker.Dockerfile`
```dockerfile
FROM python:3.11-slim

WORKDIR /app
COPY shared/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install asyncpg qdrant-client prometheus-client opentelemetry-api opentelemetry-sdk

COPY memory/ /app/memory/
COPY shared/ /app/shared/

CMD ["python", "-m", "memory.worker"]
```

**`memory/worker.py`** — точка входа memory-worker:
```python
"""
Kafka consumer: слушает топики memory.events, qa.results, reward.computed
Маршрутизирует в соответствующие handler'ы EpisodicMemory, FailureMemory
Экспортирует Prometheus метрики на порт 8080
"""
```

#### `reward-worker.Dockerfile`
```dockerfile
FROM python:3.11-slim

WORKDIR /app
COPY shared/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY memory/ /app/memory/
COPY shared/ /app/shared/

CMD ["python", "-m", "memory.reward_worker"]
```

**`memory/reward_worker.py`** — точка входа reward-worker:
```python
"""
Kafka consumer: слушает топик qa.results
Вычисляет reward через RewardEngine
Публикует в reward.computed
"""
```

#### `meta-agent.Dockerfile`
```dockerfile
FROM python:3.11-slim

WORKDIR /app
COPY shared/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY memory/ /app/memory/
COPY orchestrator/ /app/orchestrator/
COPY shared/ /app/shared/

CMD ["python", "-m", "orchestrator.meta_agent_worker"]
```

---

### Шаг 3 — Kafka Topic Map (полная)

Создать документацию и скрипт инициализации всех топиков:

**Файл для создания:** `infra/kafka_topics.sh`
```bash
#!/bin/bash
# Создание всех Kafka топиков для ai-factory

KAFKA_CONTAINER="kafka"
KAFKA_BOOTSTRAP="localhost:9092"

create_topic() {
    docker exec $KAFKA_CONTAINER kafka-topics.sh \
        --bootstrap-server $KAFKA_BOOTSTRAP \
        --create --if-not-exists \
        --topic "$1" \
        --partitions "${2:-3}" \
        --replication-factor 1
}

# Существующие топики (проверить что есть)
create_topic "task.contracts" 3
create_topic "episode.events" 3
create_topic "qa.results" 3
create_topic "skill.extracted" 3
create_topic "memory.events" 3
create_topic "reward.computed" 3
```

**Полная карта топиков:**

| Топик | Producer | Consumer | Retention | Partitions |
|-------|----------|----------|-----------|------------|
| `task.contracts` | Orchestrator | Dev, QA | 7d | 3 |
| `episode.events` | All agents | Memory Worker | 30d | 3 |
| `qa.results` | QA Agent | Reward Worker | 7d | 3 |
| `skill.extracted` | Skill Extractor | Vector DB, Meta | 30d | 3 |
| `memory.events` | Memory Worker | Meta Agent | 30d | 3 |
| `reward.computed` | Reward Worker | Policy Updater | 7d | 3 |

---

### Шаг 4 — OpenTelemetry Tracing

**Файл для изменения:** `shared/llm.py`

Добавить трейсинг LLM вызовов:
```python
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

tracer = trace.get_tracer(__name__)

# В методе вызова LLM:
with tracer.start_as_current_span("llm.chat_completion") as span:
    span.set_attribute("llm.provider", provider)
    span.set_attribute("llm.model", model)
    span.set_attribute("llm.prompt_tokens", estimated_tokens)
    result = await _call_provider(...)
    span.set_attribute("llm.response_tokens", len(result))
```

**Файл для изменения:** `orchestrator/activities.py`

Добавить трейсинг каждого activity:
```python
with tracer.start_as_current_span(f"activity.{activity_name}") as span:
    span.set_attribute("episode_id", episode_id)
    span.set_attribute("task_id", task.task_id)
    span.set_attribute("iteration", iteration)
    result = await _run_activity(...)
    span.set_attribute("activity.success", result.success)
```

**Файлы для изменения:** `memory/*.py`

Добавить трейсинг DB операций:
```python
with tracer.start_as_current_span("db.query") as span:
    span.set_attribute("db.table", "solutions")
    span.set_attribute("db.operation", "INSERT")
    await db.execute(...)
```

**Конфигурация OTEL:**
```
OTEL_SERVICE_NAME=ai-factory
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317
OTEL_TRACES_EXPORTER=otlp
```

---

### Шаг 5 — Health Checks

Добавить health check endpoints для каждого сервиса:

**`memory/worker.py`** — HTTP `/health`:
```python
# Простой HTTP server на отдельном порту
# GET /health → {"status": "ok", "db": "connected", "qdrant": "connected"}
```

---

### Шаг 6 — Тесты Infrastructure

**Файл для создания:** `tests/test_infrastructure.py`

Smoke tests для Docker stack:
```python
def test_memory_worker_health():
    """Проверить что memory-worker поднимается (используем requests)."""
    ...

def test_kafka_topics_exist():
    """Проверить что все топики созданы."""
    ...

def test_qdrant_accessible():
    """Проверить что Qdrant healthz endpoint отвечает."""
    ...
```

**Интеграционный тест:** `tests/test_e2e_stack.py`
- Запустить весь stack через `docker compose up`
- Запустить тестовый workflow
- Проверить что episode записан в PostgreSQL
- Проверить что Kafka топики содержат сообщения

---

## Порядок выполнения

1. Создать Dockerfiles для всех новых сервисов
2. Создать точки входа (`memory/worker.py`, `memory/reward_worker.py`)
3. Добавить сервисы в `docker-compose.yml`
4. Создать `infra/kafka_topics.sh`
5. Добавить OpenTelemetry в `shared/llm.py`
6. Добавить OpenTelemetry в `orchestrator/activities.py`
7. Добавить OpenTelemetry в `memory/*.py`
8. Создать `infra/prometheus.yml` и Grafana config
9. `docker compose up -d --build` — всё должно подняться
10. Запустить smoke tests
11. Коммит: `feat(phase8): production infrastructure with observability`

---

## Критерии готовности

- [ ] `docker compose up -d --build` — все сервисы запускаются без ошибок
- [ ] `docker compose ps` — все сервисы healthy
- [ ] `curl http://localhost:8080/health` → `{"status": "ok"}`
- [ ] `curl http://localhost:6333/healthz` → Qdrant OK
- [ ] `curl http://localhost:9090` → Prometheus UI доступен
- [ ] `curl http://localhost:3000` → Grafana доступен
- [ ] Все 6 Kafka топиков созданы
- [ ] OpenTelemetry traces появляются в коллекторе

---

## Риски

- **Port conflicts:** многие порты (9090, 3000, 8080) могут быть заняты. Сделать все порты configurable через .env.
- **Build time:** каждый Dockerfile требует установки зависимостей. Использовать multi-stage builds и layer caching.
- **Service dependencies:** memory-worker зависит от qdrant и postgresql — нужны retry логика и graceful startup.
- **OTel performance:** трейсинг добавляет overhead. Использовать sampling (например, 10% traces в production).
- **Kafka consumer groups:** каждый сервис должен иметь уникальный `group.id` для независимого потребления.
