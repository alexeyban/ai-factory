# Phase 2 — Skill Engine

**Приоритет:** High
**Зависимости:** Phase 1 (Memory Layer)
**Блокирует:** Phase 3, 5, 6

---

## Цель

Агент накапливает reusable знания в виде переиспользуемых skill-модулей. После каждого успешного QA-прохода — извлекать переиспользуемый паттерн, векторизовать и сохранять. При следующих задачах — подбирать релевантные skills и включать в промпт.

---

## Шаги реализации

### Шаг 1 — Skill Schema

**Файл для создания:** `memory/skill.py`

```python
from dataclasses import dataclass, field
from datetime import datetime
import uuid

@dataclass
class Skill:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    description: str = ""
    code_path: str = ""            # путь к skills/<id>.py
    embedding: list[float] = field(default_factory=list)
    success_rate: float = 0.0      # 0.0 – 1.0
    use_count: int = 0
    tags: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.utcnow)
    last_used_at: datetime | None = None
    is_active: bool = True

    def to_dict(self) -> dict:
        """Для хранения в Qdrant payload и PostgreSQL"""
        ...

    @classmethod
    def from_dict(cls, data: dict) -> "Skill": ...
```

---

### Шаг 2 — Skills Directory Structure

**Создать:**
```
skills/
  __init__.py           # экспортирует SkillRegistry
  registry.json         # кэш метаданных всех навыков
```

**`skills/registry.json` формат:**
```json
{
  "version": "1.0",
  "last_updated": "2026-03-24T...",
  "skills": {
    "<skill_id>": {
      "name": "...",
      "tags": [...],
      "success_rate": 0.0,
      "use_count": 0,
      "code_path": "skills/<id>.py"
    }
  }
}
```

**`skills/__init__.py`:**
```python
class SkillRegistry:
    def load(self) -> dict[str, dict]: ...          # загрузить registry.json
    def save(self, skills: dict[str, dict]) -> None: ...  # обновить registry.json
    def add_skill(self, skill: Skill) -> None: ...
    def get_skill_code(self, skill_id: str) -> str: ...   # прочитать .py файл
```

---

### Шаг 3 — Skill Extractor

**Файл для создания:** `memory/skill_extractor.py`

**Алгоритм extraction:**
1. Получить код решения из `SolutionRecord`
2. Отправить в LLM промпт для извлечения паттерна:
   ```
   Analyze this Python code and extract a reusable pattern.
   Return JSON: {"name": str, "description": str, "code": str, "tags": list[str]}
   Code: {code}
   ```
3. Распарсить JSON-ответ LLM
4. Получить embedding для `(description + " " + " ".join(tags))` через `shared/llm.py`
5. Сохранить код как `skills/<skill_id>.py`
6. Обновить `skills/registry.json`
7. Записать в PostgreSQL таблицу `skills` (через Memory Layer Phase 1)
8. Записать embedding в Qdrant через `VectorMemory.upsert_skill()`
9. Публиковать в Kafka топик `skill.extracted`

```python
class SkillExtractor:
    def __init__(self, llm_client, vector_memory: VectorMemory,
                 db: MemoryDB, kafka_producer=None): ...

    async def extract_from_solution(self, solution: SolutionRecord,
                                     code: str) -> Skill | None:
        """Основная точка входа. None если extraction не дал результата."""
        ...

    async def _call_llm_for_pattern(self, code: str) -> dict | None:
        """Вызов LLM для извлечения паттерна. Обработка JSON парсинг ошибок."""
        ...

    async def _get_embedding(self, text: str) -> list[float]:
        """Embedding через shared/llm.py или hash-based fallback."""
        ...

    def _save_skill_file(self, skill_id: str, code: str) -> str:
        """Сохраняет код в skills/<skill_id>.py, возвращает путь."""
        ...
```

**Kafka сообщение (`skill.extracted`):**
```json
{
  "skill_id": "<uuid>",
  "name": "...",
  "tags": [...],
  "episode_id": "ep_...",
  "task_id": "T001",
  "timestamp": "..."
}
```

---

### Шаг 4 — Skill Retriever

**Файл для создания:** `memory/skill_retriever.py`

```python
async def get_relevant_skills(
    task_description: str,
    task_embedding: list[float],
    vector_memory: VectorMemory,
    db: MemoryDB,
    top_k: int = 3
) -> list[Skill]:
    """
    Алгоритм ранжирования:
    1. Получить топ (top_k * 2) кандидатов из Qdrant по vector similarity
    2. Для каждого получить success_rate из PostgreSQL
    3. Итоговый score = similarity * 0.6 + success_rate * 0.4
    4. Отсортировать по score, вернуть топ-K
    5. Обновить last_used_at и use_count для выбранных skills
    """
    ...

async def get_skill_context_for_prompt(
    task_description: str,
    task_embedding: list[float],
    vector_memory: VectorMemory,
    db: MemoryDB
) -> str:
    """Форматирует топ-3 skills в строку для включения в промпт."""
    skills = await get_relevant_skills(...)
    # Формат:
    # ## Available Skills
    # 1. **<name>** (success_rate: 0.85, tags: python, sorting)
    #    <description>
    #    Code: skills/<id>.py
    ...
```

---

### Шаг 5 — Skill Executor

**Файл для создания:** `memory/skill_executor.py`

```python
import subprocess
import tempfile
from pathlib import Path

class SkillExecutor:
    def __init__(self, timeout_sec: int = 10): ...

    def execute(self, skill: Skill, inputs: dict) -> SkillResult:
        """
        Sandbox execution через subprocess:
        1. Создать временный .py файл с skill кодом + test harness
        2. subprocess.run с timeout и capture_output=True
        3. Распарсить stdout как JSON результат
        4. Обработать timeout (subprocess.TimeoutExpired)
        """
        ...

@dataclass
class SkillResult:
    success: bool
    output: str | None
    error: str | None
    execution_time_ms: float
```

**Ограничения sandbox:**
- `timeout_sec=10` по умолчанию
- Запрещать сетевой доступ (концептуально — в MVP через timeout)
- Не передавать secrets/credentials в subprocess environment

---

### Шаг 6 — Интеграция в Activities

**Файл для изменения:** `orchestrator/activities.py`

После успешного `qa_activity` (когда тесты прошли):
```python
# В qa_activity, при успехе:
if qa_result.tests_passed == qa_result.tests_total:
    skill_extractor = SkillExtractor(...)
    skill = await skill_extractor.extract_from_solution(solution_record, dev_output.code)
    if skill:
        logger.info(f"New skill extracted: {skill.name} ({skill.id})")
```

---

### Шаг 7 — Тесты

**Файлы для создания:**
- `tests/test_skill.py`
- `tests/test_skill_extractor.py`
- `tests/test_skill_retriever.py`
- `tests/test_skill_executor.py`

**`tests/test_skill_extractor.py`:**
- `test_extract_from_solution_success()` — mock LLM возвращает валидный JSON, skill создаётся
- `test_extract_handles_invalid_llm_json()` — LLM возвращает невалидный JSON → возвращает None
- `test_skill_file_saved_correctly()` — файл записан в `skills/<id>.py`
- `test_kafka_published_on_extraction()` — mock Kafka получает сообщение

**`tests/test_skill_retriever.py`:**
- `test_retrieval_ranking()` — высокий success_rate повышает позицию
- `test_returns_top_k()` — не больше top_k результатов
- `test_use_count_updated()` — use_count увеличивается после retrieval

**`tests/test_skill_executor.py`:**
- `test_execute_simple_skill()` — выполняет тривиальный Python код
- `test_timeout_handling()` — бесконечный цикл → TimeoutExpired, success=False
- `test_sandbox_captures_output()` — stdout корректно захватывается

---

## Порядок выполнения

1. Создать `memory/skill.py`
2. Создать `skills/` директорию с `__init__.py` и пустым `registry.json`
3. Создать `memory/skill_extractor.py`
4. Создать `memory/skill_retriever.py`
5. Создать `memory/skill_executor.py`
6. Интегрировать `SkillExtractor` в `orchestrator/activities.py`
7. Написать тесты
8. `pytest tests/ -x`
9. Коммит: `feat(phase2): skill engine with extraction and retrieval`

---

## Критерии готовности

- [ ] После успешного QA новый skill автоматически извлекается
- [ ] `skills/registry.json` обновляется
- [ ] Qdrant содержит embedding нового skill
- [ ] `pytest tests/test_skill_extractor.py` — зелёный
- [ ] `pytest tests/test_skill_retriever.py` — зелёный
- [ ] `pytest tests/test_skill_executor.py` — зелёный
- [ ] `pytest tests/` — существующие тесты не сломаны

---

## Риски

- **LLM JSON parsing:** LLM часто возвращает невалидный JSON — нужен robust парсинг (regex fallback, повторный запрос).
- **Embedding size:** убедиться что размерность embedding совпадает с `VECTOR_DIM=1536` в Qdrant коллекции.
- **skills/ git pollution:** добавить `skills/*.py` в `.gitignore` или сделать отдельный git-ignore правило — skills генерируются динамически.
- **Skill quality:** LLM может извлечь бесполезный паттерн — пока принимаем всё, pruning будет в Phase 6.
