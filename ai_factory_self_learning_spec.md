# AI Factory — Self-Learning Agent
## Детальная спецификация разработки для AI-агентов

> **Версия:** 1.0 · 2026-03-23  
> **Назначение:** Передача задач Claude Code и другим AI-агентам  
> **Контекст:** Расширение проекта [ai-factory](https://github.com/alexeyban/ai-factory) до self-improving agent системы (AlphaZero-style)

---

## Содержание

- [2. Фазы разработки](#2-пошаговый-план-разработки)
  - [Phase 0 — Базовый рефакторинг](#phase-0--базовый-рефакторинг-под-learning-loop)
  - [Phase 1 — Memory Layer](#phase-1--memory-layer)
  - [Phase 2 — Skill Engine](#phase-2--skill-engine)
  - [Phase 3 — Dev Agent Evolution](#phase-3--dev-agent-evolution)
  - [Phase 4 — QA + Reward System](#phase-4--qa--reward-system)
  - [Phase 5 — Learning Loop](#phase-5--learning-loop-alphazero-style)
  - [Phase 6 — Self-Modification](#phase-6--self-modification)
  - [Phase 7 — Benchmarking](#phase-7--benchmarking-pipeline)
  - [Phase 8 — Infrastructure](#phase-8--infrastructure-production)
  - [Phase 9 — Anti-Patterns & Stability](#phase-9--anti-patterns--stability)
- [3. Порядок реализации и зависимости](#3-порядок-реализации-и-зависимости)
- [4. Итоговая структура файлов](#4-итоговая-структура-файлов)
- [5. Переменные окружения](#5-переменные-окружения)
- [6. Готовые промпты для Claude Code](#6-готовые-промпты-для-claude-code-по-фазам)
- [7. Чек-лист готовности](#7-чек-лист-готовности-к-запуску)

---


# ai-factory — CLAUDE.md

## Project Context
Python 3.11+, Temporal workflow engine, Kafka, PostgreSQL, Docker Compose.
Primary path: orchestrator/ (Temporal). Legacy path: kafka agents (lower priority).

## Execution Rules
- ALWAYS run: pytest tests/ before committing
- NEVER break existing Temporal workflow contracts
- ALWAYS add type hints to new Python code
- Use existing shared/llm.py — do NOT create alternative LLM clients
- Kafka messages MUST use defined contracts (see shared/contracts/)

## File Structure
orchestrator/    — Temporal workflows and activities
shared/          — LLM adapter, git helpers, prompts, kafka contracts
workspace/       — Generated project output (do not modify manually)
memory/          — NEW: episode store, skill engine, vector DB client

## Forbidden
- Do not touch workspace/projects/ directly
- Do not change LLM provider fallback logic without explicit instruction
```

### 0.4 Шаблон промпта для каждой фазы

```
Ты реализуешь Phase N проекта ai-factory (self-learning agent).

Контекст: [краткое описание фазы из этого документа]

Правила:
1. Читай CLAUDE.md перед началом
2. Пиши тесты (pytest) для каждого нового модуля
3. Не ломай существующие Temporal workflow контракты
4. Kafka-контракты в shared/contracts/ — обязательны
5. После каждого модуля запускай: pytest tests/ -x
6. Коммить после успешных тестов: feat(phaseN): <описание>
```

---

## 1. Архитектурный контекст проекта

AI Factory — Temporal-based multi-agent система доставки ПО.

### Текущие агенты

| Агент | Роль | Статус |
|-------|------|--------|
| PM | Планирование, документация, recovery | ✅ Работает |
| Architect | Архитектура, task breakdown | ✅ Работает |
| Decomposer | Разбивка больших задач | ✅ Работает |
| Dev | Реализация кода | ✅ Работает |
| QA | Валидация, merge в main | ✅ Работает |
| Analyst | Отчёты о состоянии проекта | ✅ Работает |

### Ключевое ограничение

> Все контракты и передача сообщений между агентами **обязательно через Kafka**. Temporal остаётся оркестратором, но межагентный обмен данными — через Kafka-топики с типизированными контрактами.

---

## 2. Пошаговый план разработки

---

### Phase 0 — Базовый рефакторинг под learning-loop

**Цель:** Подготовить ai-factory к итеративному обучению (stateful execution).

---

#### 0.1 Unified Task Contract

Создать единую схему задачи, используемую всеми агентами.

**Файлы для создания:**
- `shared/contracts/task_schema.yaml`
- `shared/contracts/task_loader.py`
- `tests/test_task_contract.py`

**`shared/contracts/task_schema.yaml`:**
```yaml
task_id: str           # уникальный ID
type: str              # dev | qa | refactor | docs
input_spec: dict       # входные данные задачи
tests: list[str]       # список тестовых сценариев
metrics: dict          # target метрики (coverage, perf)
constraints:
  max_tokens: int
  timeout_sec: int
  max_fix_attempts: int
```

**`shared/contracts/kafka_task_contract.py`:**
```python
@dataclass
class TaskContractMessage:
    task_id: str
    episode_id: str
    payload: dict
    schema_version: str = '1.0'
```

Kafka-топик: `task.contracts`

---

#### 0.2 Episode ID — система эпизодов

Каждый запуск workflow = один episode. Эпизод хранит всю историю выполнения.

**Файлы для создания:**
- `shared/episode.py`
- Расширить `orchestrator/activities.py`: добавить `episode_id` во все вызовы

**`shared/episode.py`:**
```python
def new_episode_id() -> str:
    return f"ep_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"

def log_episode_event(episode_id: str, event_type: str, data: dict):
    # Публикует в Kafka топик: episode.events
    ...
```

Kafka-топик `episode.events` — структура сообщения:
```json
{ "episode_id": "", "event_type": "", "agent": "", "timestamp": "", "data": {} }
```

---

#### 0.3 Refactor Orchestrator — loop execution

Изменить `orchestrator/workflows.py`: pipeline поддерживает N итераций на задачу.

**Изменения в `workflows.py`:**
- Добавить параметр `max_iterations: int` в `WorkflowInput`
- Обернуть `dev->qa` цикл в `while loop` со счётчиком итераций
- Передавать номер итерации в каждый activity
- Сохранять `best_solution` из всех итераций

---

### Phase 1 — Memory Layer

**Цель:** Добавить обучаемость через накопление опыта. Ключевой компонент системы.

---

#### 1.1 Storage — PostgreSQL расширение

PostgreSQL уже есть в `docker-compose.yml`. Добавить таблицы для memory layer.

**Файлы для создания:**
- `memory/migrations/001_memory_tables.sql`
- `memory/db.py` — async подключение (asyncpg)

**DDL схема (`memory/migrations/001_memory_tables.sql`):**
```sql
CREATE TABLE episodes (
    id VARCHAR PRIMARY KEY,
    workflow_run_id VARCHAR,
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    status VARCHAR,         -- success | failed | partial
    task_count INT
);

CREATE TABLE solutions (
    id SERIAL PRIMARY KEY,
    episode_id VARCHAR REFERENCES episodes(id),
    task_id VARCHAR,
    iteration INT,
    code_hash VARCHAR,
    reward FLOAT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE rewards (
    id SERIAL PRIMARY KEY,
    solution_id INT REFERENCES solutions(id),
    correctness FLOAT,
    performance FLOAT,
    complexity_penalty FLOAT,
    total FLOAT
);

CREATE TABLE skills (
    id VARCHAR PRIMARY KEY,
    name VARCHAR,
    code_path VARCHAR,
    embedding VECTOR(1536),
    success_rate FLOAT DEFAULT 0,
    use_count INT DEFAULT 0,
    tags TEXT[],
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE failures (
    id SERIAL PRIMARY KEY,
    episode_id VARCHAR,
    task_id VARCHAR,
    failure_type VARCHAR,  -- timeout | wrong_complexity | failed_tests
    error_message TEXT,
    context JSONB
);
```

> **Kafka-интеграция:** при записи в `episodes`/`solutions` также публиковать в топик `memory.events`.

---

#### 1.2 Episodic Memory API

**Файл: `memory/episodic.py`**
```python
class EpisodicMemory:
    async def store_episode(self, episode: EpisodeRecord) -> None: ...
    async def get_similar_tasks(self, task_embedding: list[float],
                                top_k: int = 5) -> list[SimilarTask]: ...
    async def get_best_solution(self, task_id: str) -> Solution | None: ...
    async def store_solution(self, solution: SolutionRecord) -> None: ...
```

---

#### 1.3 Failure Memory

**Файл: `memory/failures.py`**
```python
class FailureMemory:
    async def record_failure(self, episode_id: str, task_id: str,
                             failure_type: str, context: dict) -> None: ...
    async def get_failure_patterns(self, task_type: str) -> list[FailurePattern]: ...
```

Типы ошибок: `timeout`, `wrong_complexity`, `failed_tests`, `llm_error`, `git_conflict`

---

#### 1.4 Vector Memory — Qdrant

**Добавить в `docker-compose.yml`:**
```yaml
qdrant:
  image: qdrant/qdrant:latest
  ports:
    - "6333:6333"
  volumes:
    - qdrant_data:/qdrant/storage
```

**Файл: `memory/vector_store.py`**
```python
class VectorMemory:
    def __init__(self, qdrant_url: str):
        self.client = QdrantClient(url=qdrant_url)

    async def upsert_skill(self, skill: Skill) -> None: ...
    async def search_similar_skills(self, embedding: list[float],
                                     top_k: int = 5) -> list[Skill]: ...
    async def search_similar_episodes(self, task_embedding: list[float],
                                       top_k: int = 3) -> list[EpisodeSummary]: ...
```

> **Embedding-модель:** использовать существующий LLM provider через `shared/llm.py`.

---

### Phase 2 — Skill Engine

**Цель:** Агент накапливает reusable знания в виде переиспользуемых skill-модулей.

---

#### 2.1 Skill Schema

**Файл: `memory/skill.py`**
```python
@dataclass
class Skill:
    id: str                    # uuid
    name: str                  # human-readable
    code_path: str             # путь к .py файлу в skills/
    embedding: list[float]     # векторное представление
    success_rate: float        # 0.0 – 1.0
    use_count: int
    tags: list[str]            # ['sorting', 'python', 'performance']
    created_at: datetime
    last_used_at: datetime
```

---

#### 2.2 Skill Extraction Pipeline

После каждого успешного решения (QA passed) запускать extraction.

**Файл: `memory/skill_extractor.py`**

Алгоритм:
1. Получить код решения из solution record
2. Отправить в LLM промпт: `«Выдели переиспользуемый паттерн из кода. JSON: {name, description, code, tags}»`
3. Получить embedding для `(description + tags)`
4. Сохранить как `.py` файл в `skills/<id>.py`
5. Записать metadata в PostgreSQL + Qdrant

Kafka-топик: `skill.extracted` — публиковать при каждом новом skill.

---

#### 2.3 Skill Storage

```
skills/
  __init__.py
  <skill_id>.py     # каждый skill — отдельный .py
  registry.json     # кэш метаданных для быстрой загрузки
```

---

#### 2.4 Skill Retrieval

**Файл: `memory/skill_retriever.py`**
```python
async def get_relevant_skills(task_embedding: list[float],
                              top_k: int = 3) -> list[Skill]:
    candidates = await vector_memory.search_similar_skills(task_embedding, top_k * 2)
    ranked = sorted(candidates, key=lambda s: s.success_rate * 0.4 + similarity * 0.6)
    return ranked[:top_k]
```

---

#### 2.5 Skill Execution

**Файл: `memory/skill_executor.py`** — sandbox execution через `subprocess` с таймаутом.

---

### Phase 3 — Dev Agent Evolution

**Цель:** Dev агент становится policy + skill composer.

---

#### 3.1 Multi-Candidate Generation

Изменения в `orchestrator/activities.py` (dev_activity):
- Добавить параметр `num_candidates: int` (default=1, configurable)
- Генерировать K решений параллельно через `asyncio.gather`
- Передавать все K кандидатов в QA для оценки

```python
async def dev_activity(task: TaskContract, num_candidates: int = 3):
    candidates = await asyncio.gather(*[
        generate_solution(task, strategy=s)
        for s in get_strategies(num_candidates)
    ])
    return candidates  # список решений для QA
```

---

#### 3.2 Skill-Aware Prompting

Изменения в `shared/prompts/dev.py` — добавить в системный промпт:

```
## Available Skills
{skills_list}   # топ-3 релевантных skill

## Known Failure Patterns (avoid these)
{failure_patterns}  # из failure memory
```

---

#### 3.3 Code Composer

**Файл: `orchestrator/code_composer.py`**
```python
class CodeComposer:
    def compose(self, skills: list[Skill], new_code: str) -> str:
        """Объединяет skills + новый код в единый solution"""
        imports = self._extract_imports(skills)
        skill_code = self._merge_skill_functions(skills)
        return f"{imports}\n{skill_code}\n{new_code}"
```

---

#### 3.4 Exploration vs Exploitation

```bash
# env переменные
EXPLORATION_RATE=0.3   # 30% — explore new approaches
                        # 70% — exploit known skills
```

```python
if random.random() < exploration_rate:
    strategy = 'explore'  # генерировать без skills
else:
    strategy = 'exploit'  # использовать топ skills
```

---

### Phase 4 — QA + Reward System

**Цель:** Стабильный сигнал обучения для learning loop.

---

#### 4.1 QA Runner Upgrade

Изменения в `orchestrator/activities.py` (qa_activity):
- Интеграция с pytest — запускать тесты программно через `pytest.main()`
- Замер времени выполнения (`time.perf_counter`)
- Memory profiling через `tracemalloc`
- Публиковать результаты в Kafka топик: `qa.results`

Kafka топик `qa.results` — структура сообщения:
```json
{
  "episode_id": "",
  "task_id": "",
  "iteration": 0,
  "tests_passed": 0,
  "tests_failed": 0,
  "coverage": 0.0,
  "execution_time_ms": 0.0,
  "peak_memory_mb": 0.0
}
```

---

#### 4.2 Reward Engine

**Файл: `memory/reward.py`**
```python
@dataclass
class RewardWeights:
    correctness: float = 1.0
    performance: float = 0.3
    complexity_penalty: float = 0.2

class RewardEngine:
    def compute(self, qa_result: QAResult, weights: RewardWeights) -> float:
        correctness = qa_result.tests_passed / qa_result.tests_total
        performance = 1 / (1 + qa_result.execution_time_ms / 1000)
        complexity = self._cyclomatic_complexity(qa_result.code)
        return (
            correctness * weights.correctness
            + performance * weights.performance
            - complexity * weights.complexity_penalty
        )
```

Configurable weights через env: `REWARD_CORRECTNESS_W`, `REWARD_PERF_W`, `REWARD_COMPLEXITY_W`

---

#### 4.3 Regression Detection

При каждом новом решении сравнивать с `best_solution` из episodic memory. Если reward ниже — пометить как regression, не обновлять `best_solution`.

---

### Phase 5 — Learning Loop (AlphaZero-style)

**Цель:** Self-play итеративное обучение с накоплением лучших решений.

---

#### 5.1 Iteration Loop

Изменения в `orchestrator/workflows.py`:
```python
@workflow.defn
class LearningWorkflow:
    @workflow.run
    async def run(self, input: LearningWorkflowInput):
        best_reward = 0
        best_solution = None

        for iteration in range(input.max_iterations):
            candidates = await workflow.execute_activity(dev_activity, ...)
            results = await workflow.execute_activity(qa_activity, candidates)
            rewards = [reward_engine.compute(r) for r in results]

            if max(rewards) > best_reward:
                best_reward = max(rewards)
                best_solution = candidates[rewards.index(best_reward)]
                await workflow.execute_activity(extract_skill_activity, best_solution)

        return best_solution
```

---

#### 5.2 Replay Buffer

**Файл: `memory/replay_buffer.py`**
```python
class ReplayBuffer:
    def __init__(self, max_good: int = 100, max_bad: int = 50): ...
    def add(self, solution: Solution, reward: float) -> None: ...
    def sample_good(self, k: int) -> list[Solution]: ...
    def sample_bad(self, k: int) -> list[Solution]: ...
```

---

#### 5.3 Policy Update

**Файл: `memory/policy_updater.py`**
- **Prompt update:** добавлять в системный промпт Dev агента примеры из top-K решений
- **Skill weights:** увеличивать `success_rate` у использованных skills при успехе
- **Strategy:** обновлять `exploration_rate` (снижать по мере накопления skills)

---

### Phase 6 — Self-Modification

**Цель:** Агент улучшает собственные skills и параметры системы.

---

#### 6.1–6.3 Skill Refactoring, Merging, Pruning

**Файл: `memory/skill_optimizer.py`**
```python
class SkillOptimizer:
    async def refactor_skill(self, skill: Skill) -> Skill:
        """LLM оптимизирует код skill"""

    async def merge_skills(self, skills: list[Skill]) -> Skill:
        """Объединяет похожие skills (similarity > 0.9)"""

    async def prune_weak_skills(self, threshold: float = 0.3) -> int:
        """Удаляет skills с success_rate < threshold"""
```

Триггер: запускать `SkillOptimizer` каждые N эпизодов (конфиг: `SKILL_OPTIMIZE_EVERY_N_EPISODES=10`).

---

#### 6.4 Meta-Agent (опционально)

Отдельный Temporal workflow `MetaAnalysisWorkflow` — раз в сутки анализирует aggregate metrics и предлагает изменения конфигурации через PM Recovery механизм.

---

### Phase 7 — Benchmarking Pipeline

**Цель:** Training environment с измеримыми метриками прогресса.

---

#### 7.1 Dataset Loader

**Файл: `benchmarks/dataset_loader.py`**
- Формат: JSON с полями `task_id`, `description`, `difficulty`, `tests`, `expected_output`
- Уровни сложности: `easy`, `medium`, `hard`, `expert`

---

#### 7.2 Curriculum Learning

```python
class Curriculum:
    levels = ['easy', 'medium', 'hard', 'expert']
    threshold = 0.8  # success_rate для перехода на следующий уровень

    def get_next_task(self, current_stats: dict) -> Task:
        current_level = self._determine_level(current_stats)
        return self.dataset.sample(difficulty=current_level)
```

---

#### 7.3 Metrics Dashboard

Добавить в `docker-compose.yml` Grafana + Prometheus. Метрики:
- `success_rate` по уровням сложности
- `avg_reward` по эпизодам (trend)
- `skill_count` — рост базы знаний
- `exploration_rate` — снижение со временем

---

### Phase 8 — Infrastructure (Production)

**Цель:** Масштабируемая production-готовая инфраструктура.

---

#### 8.1 Docker Orchestration

Добавить в `docker-compose.yml`:

| Сервис | Образ | Задача |
|--------|-------|--------|
| `qdrant` | `qdrant/qdrant:latest` | Vector DB для skill search |
| `memory-worker` | `ai-factory/memory:latest` | Memory API сервис |
| `reward-worker` | `ai-factory/reward:latest` | Reward computation |
| `meta-agent` | `ai-factory/meta:latest` | Мета-агент анализа |
| `prometheus` | `prom/prometheus:latest` | Metrics collection |
| `grafana` | `grafana/grafana:latest` | Metrics dashboard |

---

#### 8.2 Kafka-топики — полная карта

| Топик | Producer | Consumer | Описание |
|-------|----------|----------|----------|
| `task.contracts` | Orchestrator | Dev, QA | Unified task contracts |
| `episode.events` | All agents | Memory Worker | Episode lifecycle events |
| `qa.results` | QA Agent | Reward Engine | Test results + metrics |
| `skill.extracted` | Skill Extractor | Vector DB | Новые skills |
| `memory.events` | Memory Worker | Meta Agent | Aggregate memory events |
| `reward.computed` | Reward Engine | Policy Updater | Reward signals |

---

#### 8.3 Observability

Добавить OpenTelemetry tracing в:
- `shared/llm.py` — трейсинг LLM вызовов
- `orchestrator/activities.py` — трейсинг каждого activity
- `memory/*.py` — трейсинг DB операций

---

### Phase 9 — Anti-Patterns & Stability

**Цель:** Защита от нестабильности и reward hacking.

---

#### 9.1 Loop Protection

```bash
STAGNATION_THRESHOLD=3   # итераций без улучшения → stop
MAX_ITERATIONS=5
```

---

#### 9.2 Reward Hacking Protection

- Тесты с hidden test cases (не входят в обучающий набор)
- Fingerprint для решений — не принимать одинаковый код как новое решение

---

#### 9.3 Determinism

```bash
RANDOM_SEED=42
```
- Логировать seed в каждый episode record
- Воспроизводимые запуски через episode_id replay

---

## 3. Порядок реализации и зависимости

| Приоритет | Phase | Зависит от | Блокирует |
|-----------|-------|------------|-----------|
| 1 (Critical) | Phase 0 | — | Все остальные |
| 2 (Critical) | Phase 1 | Phase 0 | Phases 2, 3, 4, 5 |
| 3 (High) | Phase 2 | Phase 1 | Phase 3, 5, 6 |
| 4 (High) | Phase 4 | Phase 1 | Phase 5 |
| 5 (High) | Phase 3 | Phases 1, 2 | Phase 5 |
| 6 (Medium) | Phase 5 | Phases 2, 3, 4 | Phase 6 |
| 7 (Medium) | Phase 8 | Phase 0 | Все сервисы |
| 8 (Medium) | Phase 7 | Phase 5 | — |
| 9 (Low) | Phase 6 | Phase 5 | — |
| 10 (Low) | Phase 9 | Phase 5 | — |

---

## 4. Итоговая структура файлов

```
ai-factory/
├── CLAUDE.md                          # NEW: инструкции для Claude Code
├── .claude/
│   └── settings.json                  # NEW: permissions config
├── memory/                            # NEW: вся learning-layer
│   ├── __init__.py
│   ├── db.py                          # asyncpg подключение
│   ├── episodic.py                    # Episodic Memory API
│   ├── failures.py                    # Failure Memory
│   ├── vector_store.py                # Qdrant client
│   ├── skill.py                       # Skill dataclass
│   ├── skill_extractor.py             # Extraction pipeline
│   ├── skill_retriever.py             # Top-K retrieval
│   ├── skill_executor.py              # Sandbox execution
│   ├── skill_optimizer.py             # Refactor/merge/prune
│   ├── reward.py                      # Reward Engine
│   ├── replay_buffer.py               # Replay Buffer
│   ├── policy_updater.py              # Policy Update
│   └── migrations/
│       └── 001_memory_tables.sql
├── skills/                            # NEW: extracted skill files
│   ├── __init__.py
│   └── registry.json
├── benchmarks/                        # NEW: evaluation
│   ├── dataset_loader.py
│   ├── curriculum.py
│   └── datasets/
│       └── easy.json
├── shared/
│   ├── contracts/                     # NEW: Kafka contracts
│   │   ├── task_schema.yaml
│   │   ├── task_loader.py
│   │   └── kafka_task_contract.py
│   ├── episode.py                     # NEW: episode management
│   ├── llm.py                         # EXISTING — не изменять
│   ├── git.py                         # EXISTING — не изменять
│   └── prompts/                       # MODIFY: добавить skills в dev/qa промпты
├── orchestrator/
│   ├── workflows.py                   # MODIFY: learning loop
│   ├── activities.py                  # MODIFY: episode_id, multi-candidate
│   ├── code_composer.py               # NEW
│   └── worker.py                      # EXISTING
└── tests/                             # EXTEND: тесты для всех новых модулей
```

---

## 5. Переменные окружения

Добавить в `.env` (дополнение к существующим):

| Переменная | Default | Описание |
|------------|---------|----------|
| `QDRANT_URL` | `http://localhost:6333` | Qdrant vector DB endpoint |
| `MEMORY_DB_URL` | `postgresql://...` | PostgreSQL для memory layer |
| `EXPLORATION_RATE` | `0.3` | Epsilon для exploration/exploitation |
| `MAX_ITERATIONS` | `5` | Максимум итераций на задачу |
| `STAGNATION_THRESHOLD` | `3` | Итераций без улучшения → stop |
| `SKILL_OPTIMIZE_EVERY_N` | `10` | Запуск SkillOptimizer каждые N эпизодов |
| `REWARD_CORRECTNESS_W` | `1.0` | Вес correctness в reward функции |
| `REWARD_PERF_W` | `0.3` | Вес performance в reward функции |
| `REWARD_COMPLEXITY_W` | `0.2` | Штраф за сложность кода |
| `NUM_CANDIDATES` | `3` | Количество кандидатов от Dev агента |
| `RANDOM_SEED` | `42` | Seed для воспроизводимости |
| `SKILL_SIMILARITY_THRESHOLD` | `0.9` | Порог для skill merging |
| `SKILL_PRUNE_THRESHOLD` | `0.3` | Минимальный success_rate для pruning |

---

## 6. Готовые промпты для Claude Code по фазам

### Phase 0

```
Реализуй Phase 0 (базовый рефакторинг) для ai-factory self-learning agent.

Задачи:
1. Создай CLAUDE.md в корне проекта (см. раздел 0.1 этой спецификации)
2. Создай .claude/settings.json с правами для автономной работы
3. Создай shared/contracts/task_schema.yaml
4. Создай shared/contracts/task_loader.py с валидацией
5. Создай shared/contracts/kafka_task_contract.py
6. Создай shared/episode.py с функциями new_episode_id() и log_episode_event()
7. Измени orchestrator/workflows.py: добавь max_iterations в WorkflowInput
8. Напиши tests/test_task_contract.py и tests/test_episode.py
9. Запусти pytest tests/ -x — все тесты должны пройти
10. git commit: feat(phase0): unified task contract and episode support
```

### Phase 1

```
Реализуй Phase 1 (Memory Layer) для ai-factory.

Задачи:
1. Добавь qdrant сервис в docker-compose.yml
2. Создай memory/migrations/001_memory_tables.sql
   (таблицы: episodes, solutions, rewards, skills, failures)
3. Создай memory/db.py с asyncpg подключением
4. Создай memory/episodic.py с классом EpisodicMemory
5. Создай memory/failures.py с классом FailureMemory
6. Создай memory/vector_store.py с Qdrant client
7. Все методы должны публиковать события в Kafka (используй существующий Kafka client)
8. Напиши tests/ для всех новых модулей (используй pytest-asyncio)
9. pytest tests/ -x
10. git commit: feat(phase1): memory layer with episodic and vector storage
```

### Phase 2

```
Реализуй Phase 2 (Skill Engine) для ai-factory.

Задачи:
1. Создай memory/skill.py с dataclass Skill
2. Создай memory/skill_extractor.py — pipeline извлечения skill после успешного QA
3. Создай skills/ директорию с __init__.py и registry.json
4. Создай memory/skill_retriever.py — top-K retrieval с ранжированием
5. Создай memory/skill_executor.py — sandbox execution через subprocess
6. Интегрировать skill_extractor в orchestrator/activities.py (вызов после qa_activity success)
7. Публиковать в Kafka топик skill.extracted при каждом новом skill
8. Тесты для всех новых модулей
9. pytest tests/ -x
10. git commit: feat(phase2): skill engine with extraction and retrieval
```

### Phase 3

```
Реализуй Phase 3 (Dev Agent Evolution) для ai-factory.

Задачи:
1. Измени orchestrator/activities.py: добавь num_candidates параметр в dev_activity
2. Реализуй параллельную генерацию K кандидатов через asyncio.gather
3. Создай orchestrator/code_composer.py с классом CodeComposer
4. Измени shared/prompts/dev.py: добавь секции Available Skills и Known Failure Patterns
5. Добавь epsilon-greedy логику (EXPLORATION_RATE env var)
6. Передавать все кандидаты в QA — QA выбирает лучший
7. Тесты
8. pytest tests/ -x
9. git commit: feat(phase3): multi-candidate dev with skill-aware prompting
```

### Phase 4

```
Реализуй Phase 4 (QA + Reward System) для ai-factory.

Задачи:
1. Измени orchestrator/activities.py: qa_activity интегрирует pytest.main()
2. Добавить замер времени и память (time.perf_counter, tracemalloc)
3. Создай memory/reward.py с классом RewardEngine и RewardWeights
4. Реализовать формулу: reward = correctness + performance * w1 - complexity_penalty
5. Добавь configurable weights через env vars
6. Добавь regression detection: сравнение с best_solution из episodic memory
7. Публиковать в Kafka топик qa.results и reward.computed
8. Тесты
9. pytest tests/ -x
10. git commit: feat(phase4): qa runner upgrade and reward engine
```

### Phase 5

```
Реализуй Phase 5 (Learning Loop) для ai-factory.

Задачи:
1. Создай новый LearningWorkflow в orchestrator/workflows.py
2. Реализовать iteration loop с best_solution tracking
3. Интегрировать extract_skill_activity при улучшении reward
4. Создай memory/replay_buffer.py
5. Создай memory/policy_updater.py — обновление промптов и skill weights
6. Добавить stagnation detection (STAGNATION_THRESHOLD env var)
7. Тесты
8. pytest tests/ -x
9. git commit: feat(phase5): alphazero-style learning loop
```

### Phase 6

```
Реализуй Phase 6 (Self-Modification) для ai-factory.

Задачи:
1. Создай memory/skill_optimizer.py с методами refactor_skill, merge_skills, prune_weak_skills
2. Триггер: запуск SkillOptimizer каждые SKILL_OPTIMIZE_EVERY_N эпизодов
3. Реализовать skill merging при similarity > SKILL_SIMILARITY_THRESHOLD
4. Реализовать pruning при success_rate < SKILL_PRUNE_THRESHOLD
5. (Опционально) Создай MetaAnalysisWorkflow в orchestrator/workflows.py
6. Тесты
7. pytest tests/ -x
8. git commit: feat(phase6): self-modification with skill optimizer
```

### Phase 7

```
Реализуй Phase 7 (Benchmarking) для ai-factory.

Задачи:
1. Создай benchmarks/dataset_loader.py
2. Создай benchmarks/datasets/easy.json с 10+ тестовыми задачами
3. Создай benchmarks/curriculum.py с логикой easy→medium→hard
4. Добавь prometheus и grafana в docker-compose.yml
5. Экспортировать метрики: success_rate, avg_reward, skill_count, exploration_rate
6. Тесты
7. pytest tests/ -x
8. git commit: feat(phase7): benchmarking pipeline and metrics dashboard
```

### Phase 8

```
Реализуй Phase 8 (Infrastructure) для ai-factory.

Задачи:
1. Добавь в docker-compose.yml: memory-worker, reward-worker, meta-agent, prometheus, grafana
2. Создай Dockerfile для каждого нового сервиса
3. Добавь OpenTelemetry tracing в shared/llm.py, orchestrator/activities.py, memory/*.py
4. Убедиться что все Kafka топики из карты (раздел 8.2) созданы и задокументированы
5. docker compose up -d --build — всё должно подняться
6. git commit: feat(phase8): production infrastructure with observability
```

### Phase 9

```
Реализуй Phase 9 (Anti-Patterns & Stability) для ai-factory.

Задачи:
1. Добавь loop protection: MAX_ITERATIONS, STAGNATION_THRESHOLD
2. Добавь solution fingerprinting (hash кода) — не принимать дубликаты
3. Создай hidden test cases для reward hacking protection
4. Добавь RANDOM_SEED во все random вызовы
5. Логировать seed в каждый episode record
6. Тесты для всех защитных механизмов
7. pytest tests/ -x
8. git commit: feat(phase9): stability and anti-pattern protection
```

---

## 7. Чек-лист готовности к запуску

| # | Проверка | Команда |
|---|----------|---------|
| 1 | Docker stack запущен | `docker compose ps` |
| 2 | Temporal UI доступен | `curl http://localhost:8088` |
| 3 | Qdrant доступен | `curl http://localhost:6333/healthz` |
| 4 | Kafka топики созданы | `docker exec kafka kafka-topics.sh --list` |
| 5 | PostgreSQL миграции применены | `python -m memory.migrations` |
| 6 | Все тесты проходят | `pytest tests/ -v` |
| 7 | LLM adapter работает | `python scripts/test_llm.py` |
| 8 | CLAUDE.md создан | `cat CLAUDE.md` |
| 9 | settings.json создан | `cat .claude/settings.json` |
| 10 | skills/ директория создана | `ls skills/` |

После прохождения всех проверок — запускать Claude Code:

```bash
# Автономный режим для конкретной фазы
claude --dangerously-skip-permissions -p "$(cat prompts/phase0.txt)"

# Или интерактивно с правами из settings.json
claude
```

---

*Документ сгенерирован: 2026-03-23. Версия спецификации: 1.0*
