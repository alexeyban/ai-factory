# Phase 0 — Базовый рефакторинг под Learning Loop

**Приоритет:** Critical (блокирует все остальные фазы)
**Зависимости:** нет
**Блокирует:** Phase 1–9

---

## Цель

Подготовить ai-factory к итеративному обучению: ввести единый контракт задачи, систему эпизодов и поддержку N-итерационного loop в оркестраторе.

---

## Шаги реализации

### Шаг 1 — Unified Task Contract

**Файлы для создания:**
- `shared/contracts/__init__.py`
- `shared/contracts/task_schema.yaml`
- `shared/contracts/task_loader.py`
- `shared/contracts/kafka_task_contract.py`

**Детали `task_schema.yaml`:**
```yaml
task_id: str           # уникальный ID задачи
type: str              # dev | qa | refactor | docs
input_spec: dict       # входные данные задачи
tests: list[str]       # список тестовых сценариев
metrics: dict          # target метрики (coverage, perf)
constraints:
  max_tokens: int      # лимит токенов
  timeout_sec: int     # таймаут выполнения
  max_fix_attempts: int # максимум попыток исправления
```

**Детали `task_loader.py`:**
- Функция `load_task(data: dict) -> TaskContract` — валидирует по YAML-схеме
- Функция `validate_task(task: dict) -> bool` — проверяет обязательные поля
- Использовать `jsonschema` или `pydantic` для валидации
- Поддерживать обратную совместимость с существующим контрактом из `decomposer/agent.py`

**Детали `kafka_task_contract.py`:**
```python
@dataclass
class TaskContractMessage:
    task_id: str
    episode_id: str
    payload: dict
    schema_version: str = '1.0'
    timestamp: str = field(default_factory=...)
```
- Kafka-топик: `task.contracts`
- Метод `to_json() -> str` и `from_json(s: str) -> TaskContractMessage`

**Ключевое требование:** новый контракт должен быть совместим с существующим полем `assigned_agent` и другими полями из текущего `normalize_task_contract` в `decomposer/agent.py`.

---

### Шаг 2 — Episode ID System

**Файлы для создания:**
- `shared/episode.py`

**Детали `shared/episode.py`:**
```python
def new_episode_id() -> str:
    # Формат: ep_YYYYMMDD_HHMMSS_<8 hex chars>
    return f"ep_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"

def log_episode_event(episode_id: str, event_type: str, agent: str, data: dict) -> None:
    # Публикует в Kafka топик: episode.events
    # Структура: { "episode_id", "event_type", "agent", "timestamp", "data" }
    ...
```

**Типы событий (`event_type`):**
- `workflow_started` / `workflow_finished`
- `task_started` / `task_finished`
- `iteration_started` / `iteration_finished`
- `dev_generated` / `qa_passed` / `qa_failed`

**Интеграция в `orchestrator/activities.py`:**
- Добавить `episode_id: str` параметр в сигнатуры всех activity-функций
- В начале/конце каждого activity вызывать `log_episode_event(...)`
- Передавать `episode_id` через `workflow.execute_activity(...)` params

---

### Шаг 3 — Refactor Orchestrator для Loop

**Файл для изменения:** `orchestrator/workflows.py`

**Изменения в `WorkflowInput`:**
```python
@dataclass
class WorkflowInput:
    # ... существующие поля ...
    episode_id: str = field(default_factory=new_episode_id)
    max_iterations: int = 1  # default=1 сохраняет текущее поведение
    iteration: int = 0       # текущая итерация (для передачи в activity)
```

**Изменения в основном workflow:**
```python
best_solution = None
best_reward = 0.0

for iteration in range(input.max_iterations):
    # передавать iteration в dev_activity и qa_activity
    solution = await workflow.execute_activity(dev_activity, ..., iteration=iteration)
    qa_result = await workflow.execute_activity(qa_activity, ..., iteration=iteration)

    if qa_result.reward > best_reward:
        best_reward = qa_result.reward
        best_solution = solution

return best_solution or last_solution
```

**Важно:** обернуть цикл так, чтобы при `max_iterations=1` поведение было идентично текущему (не ломать существующие тесты).

---

### Шаг 4 — Тесты

**Файлы для создания:**
- `tests/test_task_contract.py`
- `tests/test_episode.py`

**`tests/test_task_contract.py`:**
- `test_validate_valid_task()` — валидный контракт проходит
- `test_validate_missing_required_fields()` — ошибка при отсутствии обязательных полей
- `test_task_contract_message_serialization()` — JSON сериализация/десериализация
- `test_backward_compatibility()` — старые контракты из decomposer принимаются

**`tests/test_episode.py`:**
- `test_new_episode_id_format()` — проверка формата `ep_YYYYMMDD_...`
- `test_new_episode_id_uniqueness()` — два вызова дают разные ID
- `test_log_episode_event_structure()` — проверка структуры сообщения (mock Kafka)

---

### Шаг 5 — Обновление .env

Добавить в `.env` и `docker-compose.yml`:
```
MAX_ITERATIONS=1           # default=1, backward compatible
RANDOM_SEED=42             # для воспроизводимости
```

---

## Порядок выполнения

1. Создать `shared/contracts/` структуру
2. Реализовать и покрыть тестами `task_loader.py`
3. Создать `shared/episode.py`
4. Расширить `WorkflowInput` в `workflows.py`
5. Добавить `episode_id` в activity-сигнатуры
6. Обернуть dev→qa в iteration loop
7. Запустить `pytest tests/ -x`
8. Коммит: `feat(phase0): unified task contract and episode support`

---

## Критерии готовности

- [ ] `pytest tests/test_task_contract.py` — все тесты зелёные
- [ ] `pytest tests/test_episode.py` — все тесты зелёные
- [ ] `pytest tests/` — существующие тесты не сломаны
- [ ] Workflow с `max_iterations=1` ведёт себя как раньше
- [ ] `episode_id` присутствует во всех activity-вызовах

---

## Риски и предосторожности

- **Главный риск:** изменение сигнатур activity ломает Temporal workflow contracts. Добавлять `episode_id` как keyword-аргумент с default value.
- **Совместимость:** существующий `normalize_task_contract` в `decomposer/agent.py` — не трогать, только дополнять.
- **Kafka:** если Kafka недоступна, `log_episode_event` должен падать gracefully (try/except + warning log).
