# Phase 1 — Memory Layer

**Приоритет:** Critical
**Зависимости:** Phase 0
**Блокирует:** Phase 2, 3, 4, 5

---

## Цель

Добавить обучаемость через накопление опыта: PostgreSQL-таблицы для хранения эпизодов, решений, наград и ошибок; Qdrant для векторного поиска по задачам и навыкам.

---

## Шаги реализации

### Шаг 1 — Qdrant в Docker

**Файл для изменения:** `docker-compose.yml`

Добавить сервис:
```yaml
qdrant:
  image: qdrant/qdrant:latest
  ports:
    - "6333:6333"
  volumes:
    - qdrant_data:/qdrant/storage
  healthcheck:
    test: ["CMD", "curl", "-f", "http://localhost:6333/healthz"]
    interval: 10s
    timeout: 5s
    retries: 5
```
Добавить `qdrant_data` в секцию `volumes`.

---

### Шаг 2 — PostgreSQL Migration

**Файл для создания:** `memory/migrations/001_memory_tables.sql`

Полная DDL схема:
```sql
-- Эпизоды (каждый запуск workflow)
CREATE TABLE IF NOT EXISTS episodes (
    id VARCHAR PRIMARY KEY,
    workflow_run_id VARCHAR,
    started_at TIMESTAMPTZ DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    status VARCHAR CHECK (status IN ('running', 'success', 'failed', 'partial')),
    task_count INT DEFAULT 0,
    random_seed INT,
    metadata JSONB DEFAULT '{}'
);

-- Решения, генерируемые Dev агентом
CREATE TABLE IF NOT EXISTS solutions (
    id SERIAL PRIMARY KEY,
    episode_id VARCHAR REFERENCES episodes(id) ON DELETE CASCADE,
    task_id VARCHAR NOT NULL,
    iteration INT NOT NULL DEFAULT 0,
    code_hash VARCHAR NOT NULL,   -- SHA256 для fingerprinting
    code_path VARCHAR,            -- путь к файлу в workspace/
    reward FLOAT,
    is_best BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_solutions_task ON solutions(task_id);
CREATE INDEX IF NOT EXISTS idx_solutions_episode ON solutions(episode_id);

-- Детализированные reward метрики
CREATE TABLE IF NOT EXISTS rewards (
    id SERIAL PRIMARY KEY,
    solution_id INT REFERENCES solutions(id) ON DELETE CASCADE,
    correctness FLOAT NOT NULL,      -- tests_passed / tests_total
    performance FLOAT NOT NULL,      -- 1/(1 + exec_time_ms/1000)
    complexity_penalty FLOAT NOT NULL, -- cyclomatic complexity
    total FLOAT NOT NULL,
    tests_passed INT,
    tests_total INT,
    execution_time_ms FLOAT,
    peak_memory_mb FLOAT,
    computed_at TIMESTAMPTZ DEFAULT NOW()
);

-- Навыки (skills), извлечённые из успешных решений
CREATE TABLE IF NOT EXISTS skills (
    id VARCHAR PRIMARY KEY,          -- UUID
    name VARCHAR NOT NULL,
    description TEXT,
    code_path VARCHAR NOT NULL,      -- путь к skills/<id>.py
    success_rate FLOAT DEFAULT 0.0,
    use_count INT DEFAULT 0,
    tags TEXT[] DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    last_used_at TIMESTAMPTZ,
    is_active BOOLEAN DEFAULT TRUE   -- FALSE = pruned
);

-- Неудачи — паттерны ошибок
CREATE TABLE IF NOT EXISTS failures (
    id SERIAL PRIMARY KEY,
    episode_id VARCHAR REFERENCES episodes(id) ON DELETE SET NULL,
    task_id VARCHAR NOT NULL,
    failure_type VARCHAR CHECK (
        failure_type IN ('timeout', 'wrong_complexity', 'failed_tests',
                         'llm_error', 'git_conflict', 'unknown')
    ),
    error_message TEXT,
    context JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_failures_task ON failures(task_id);
CREATE INDEX IF NOT EXISTS idx_failures_type ON failures(failure_type);
```

**Файл для создания:** `memory/migrations/run_migrations.py`
- Скрипт применения миграций через asyncpg
- Поддержка `python -m memory.migrations`

---

### Шаг 3 — Database Client

**Файл для создания:** `memory/db.py`

```python
import asyncpg
from contextlib import asynccontextmanager

class MemoryDB:
    def __init__(self, dsn: str):
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        self._pool = await asyncpg.create_pool(self._dsn, min_size=2, max_size=10)

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()

    @asynccontextmanager
    async def transaction(self):
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                yield conn

    async def execute(self, query: str, *args): ...
    async def fetch(self, query: str, *args) -> list[dict]: ...
    async def fetchrow(self, query: str, *args) -> dict | None: ...
```

---

### Шаг 4 — Episodic Memory API

**Файл для создания:** `memory/episodic.py`

```python
@dataclass
class EpisodeRecord:
    id: str
    workflow_run_id: str
    started_at: datetime
    status: str
    task_count: int
    random_seed: int = 42
    metadata: dict = field(default_factory=dict)

@dataclass
class SolutionRecord:
    episode_id: str
    task_id: str
    iteration: int
    code_hash: str
    code_path: str
    reward: float

@dataclass
class SimilarTask:
    task_id: str
    similarity: float
    best_reward: float
    solution_path: str

class EpisodicMemory:
    def __init__(self, db: MemoryDB, kafka_producer=None): ...

    async def store_episode(self, episode: EpisodeRecord) -> None:
        # INSERT в episodes
        # Публиковать в Kafka топик: memory.events (event_type=episode_stored)
        ...

    async def update_episode_status(self, episode_id: str, status: str,
                                     finished_at: datetime) -> None: ...

    async def get_similar_tasks(self, task_embedding: list[float],
                                top_k: int = 5) -> list[SimilarTask]:
        # Использует VectorMemory.search_similar_episodes()
        ...

    async def get_best_solution(self, task_id: str) -> SolutionRecord | None:
        # SELECT по task_id ORDER BY reward DESC LIMIT 1
        ...

    async def store_solution(self, solution: SolutionRecord) -> None:
        # INSERT в solutions + rewards
        # Обновить is_best флаг если reward > текущий best
        # Публиковать в Kafka: memory.events (event_type=solution_stored)
        ...

    async def check_solution_fingerprint(self, code_hash: str, task_id: str) -> bool:
        # Возвращает True если такой hash уже видели для task_id
        ...
```

---

### Шаг 5 — Failure Memory

**Файл для создания:** `memory/failures.py`

```python
@dataclass
class FailurePattern:
    failure_type: str
    count: int
    last_error: str
    common_context: dict

class FailureMemory:
    def __init__(self, db: MemoryDB, kafka_producer=None): ...

    async def record_failure(self, episode_id: str, task_id: str,
                              failure_type: str, error_message: str,
                              context: dict) -> None:
        # INSERT в failures
        # Публиковать в Kafka: memory.events (event_type=failure_recorded)
        ...

    async def get_failure_patterns(self, task_type: str,
                                    limit: int = 5) -> list[FailurePattern]:
        # SELECT GROUP BY failure_type для задач типа task_type
        # Вернуть топ паттернов для включения в промпт Dev агента
        ...

    async def get_failure_summary(self, task_id: str) -> str:
        # Форматированный текст для включения в промпт
        ...
```

---

### Шаг 6 — Vector Memory (Qdrant)

**Файл для создания:** `memory/vector_store.py`

```python
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct

SKILLS_COLLECTION = "skills"
EPISODES_COLLECTION = "episodes"
VECTOR_DIM = 1536  # размерность embedding

class VectorMemory:
    def __init__(self, qdrant_url: str):
        self.client = QdrantClient(url=qdrant_url)

    async def init_collections(self) -> None:
        # Создать коллекции если не существуют
        for name in [SKILLS_COLLECTION, EPISODES_COLLECTION]:
            if not await self._collection_exists(name):
                self.client.create_collection(
                    collection_name=name,
                    vectors_config=VectorParams(size=VECTOR_DIM, distance=Distance.COSINE)
                )

    async def upsert_skill(self, skill_id: str, embedding: list[float],
                            metadata: dict) -> None:
        self.client.upsert(collection_name=SKILLS_COLLECTION, points=[
            PointStruct(id=skill_id, vector=embedding, payload=metadata)
        ])

    async def search_similar_skills(self, embedding: list[float],
                                     top_k: int = 5) -> list[dict]:
        results = self.client.search(
            collection_name=SKILLS_COLLECTION,
            query_vector=embedding,
            limit=top_k
        )
        return [{"id": r.id, "score": r.score, **r.payload} for r in results]

    async def upsert_episode(self, episode_id: str, task_embedding: list[float],
                              metadata: dict) -> None: ...

    async def search_similar_episodes(self, task_embedding: list[float],
                                       top_k: int = 3) -> list[dict]: ...
```

**Embedding:** использовать `shared/llm.py` — запрос к LLM для получения embedding текста задачи.
Если LLM не поддерживает embedding — использовать хеш-based fallback (TF-IDF или просто нули).

---

### Шаг 7 — Kafka Integration

Все операции Memory Layer публикуют в топик `memory.events`:

```json
{
  "event_type": "episode_stored|solution_stored|failure_recorded|skill_stored",
  "episode_id": "ep_...",
  "task_id": "T001",
  "timestamp": "2026-03-24T...",
  "data": {}
}
```

Использовать существующий Kafka клиент из проекта. Публикация — fire-and-forget (не блокировать основной поток при недоступности Kafka).

---

### Шаг 8 — Обновление .env и docker-compose

Новые переменные:
```
QDRANT_URL=http://localhost:6333
MEMORY_DB_URL=postgresql://user:password@localhost:5432/ai_factory_memory
```

---

### Шаг 9 — Тесты

**Файлы для создания:**
- `tests/test_episodic_memory.py`
- `tests/test_failure_memory.py`
- `tests/test_vector_store.py`

Использовать `pytest-asyncio`. Mock для asyncpg через `asyncpg.MockConnection` или отдельную тестовую БД.

**`test_episodic_memory.py`:**
- `test_store_and_retrieve_episode()` — сохранить и получить эпизод
- `test_get_best_solution()` — возвращает решение с максимальным reward
- `test_solution_fingerprint_detection()` — дублирующий hash обнаруживается
- `test_store_episode_publishes_kafka()` — mock Kafka producer получает сообщение

**`test_failure_memory.py`:**
- `test_record_and_retrieve_failure()` — базовый CRUD
- `test_get_failure_patterns_aggregation()` — корректная группировка по типам
- `test_failure_summary_format()` — вывод корректный строки для промпта

**`test_vector_store.py`:**
- `test_init_collections()` — коллекции создаются (mock Qdrant)
- `test_upsert_and_search_skill()` — вставка и поиск возвращают результат

---

## Порядок выполнения

1. Добавить Qdrant в `docker-compose.yml`
2. Создать `memory/migrations/001_memory_tables.sql`
3. Создать `memory/migrations/run_migrations.py`
4. Создать `memory/db.py`
5. Создать `memory/episodic.py`
6. Создать `memory/failures.py`
7. Создать `memory/vector_store.py`
8. Написать тесты для всех модулей
9. `pytest tests/ -x`
10. Коммит: `feat(phase1): memory layer with episodic and vector storage`

---

## Критерии готовности

- [ ] `docker compose up qdrant` — сервис поднимается
- [ ] SQL миграции применяются без ошибок
- [ ] `pytest tests/test_episodic_memory.py` — зелёный
- [ ] `pytest tests/test_failure_memory.py` — зелёный
- [ ] `pytest tests/test_vector_store.py` — зелёный
- [ ] `pytest tests/` — существующие тесты не сломаны

---

## Риски

- **asyncpg в тестах:** нужен либо реальный PostgreSQL в CI, либо качественный mock. Рекомендуется `pytest-asyncpg` или отдельная тестовая БД через `docker-compose.test.yml`.
- **Qdrant embedding размерность:** если LLM не возвращает embedding нужного размера — адаптировать `VECTOR_DIM` к реальному значению.
- **Kafka unavailability:** memory layer не должен падать если Kafka недоступна — всегда try/except вокруг publish.
