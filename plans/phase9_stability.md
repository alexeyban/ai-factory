# Phase 9 — Anti-Patterns & Stability

**Приоритет:** Low
**Зависимости:** Phase 5 (Learning Loop)
**Блокирует:** ничего

---

## Цель

Защита от нестабильности и reward hacking: loop protection, solution fingerprinting, hidden tests, детерминизм через random seed.

---

## Шаги реализации

### Шаг 1 — Loop Protection

Уже частично реализовано в Phase 5 (stagnation detection). Расширить:

**Файл для изменения:** `orchestrator/workflows.py`

```python
# Конфигурация из .env
MAX_ITERATIONS = int(os.getenv("MAX_ITERATIONS", "5"))
STAGNATION_THRESHOLD = int(os.getenv("STAGNATION_THRESHOLD", "3"))

# В LearningWorkflow.run():
for iteration in range(MAX_ITERATIONS):
    # ... основной цикл ...

    # Loop protection checks:
    if stagnation_count >= STAGNATION_THRESHOLD:
        logger.warning(f"Stagnation after {stagnation_count} iterations, stopping")
        await log_episode_event(..., event_type="loop_protected_stop", ...)
        stopped_reason = 'stagnation'
        break

    if best_reward >= 0.99:
        stopped_reason = 'perfect_score'
        break
```

**Дополнительная защита — infinite loop в skill executor:**
```python
# В memory/skill_executor.py уже есть timeout, но добавить:
ABSOLUTE_TIMEOUT_SEC = int(os.getenv("SKILL_EXECUTOR_TIMEOUT", "10"))
```

---

### Шаг 2 — Solution Fingerprinting

**Место реализации:** `memory/episodic.py` (метод уже предусмотрен в Phase 1)

**Алгоритм:**
```python
import hashlib

def compute_code_hash(code: str) -> str:
    """
    SHA256 fingerprint кода (нормализованный):
    1. Убрать комментарии
    2. Убрать пустые строки
    3. Нормализовать пробелы
    4. Вычислить SHA256
    """
    import ast
    try:
        # Попытка нормализации через AST unparse
        tree = ast.parse(code)
        normalized = ast.unparse(tree)
    except SyntaxError:
        normalized = code.strip()

    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()
```

**Проверка дубликатов:**
```python
# В qa_activity, перед оценкой каждого кандидата:
code_hash = compute_code_hash(candidate.code)
is_duplicate = await episodic_memory.check_solution_fingerprint(code_hash, task.task_id)

if is_duplicate:
    logger.warning(f"Duplicate solution detected for task {task.task_id}, skipping")
    # Не оценивать, не сохранять в replay buffer
    continue
```

**Таблица solution_fingerprints** (добавить в миграцию Phase 1):
```sql
-- Уже есть поле code_hash в solutions, использовать его
-- Добавить уникальный индекс:
CREATE UNIQUE INDEX IF NOT EXISTS idx_solutions_task_hash
    ON solutions(task_id, code_hash)
    WHERE is_active = TRUE;
```

---

### Шаг 3 — Hidden Test Cases

**Цель:** Dev агент не должен видеть hidden tests, но они влияют на финальный reward.

**Место реализации:** `orchestrator/activities.py` + `benchmarks/dataset_loader.py`

**Алгоритм:**
```python
# В qa_activity:
async def qa_activity(candidates, task: TaskContract, ...) -> QAResult:
    # Шаг 1: Запустить публичные тесты (видимые Dev агенту)
    public_result = await _run_tests(candidate, task.tests)

    # Шаг 2: Если публичные тесты прошли — запустить hidden tests
    if public_result.all_passed and hasattr(task, 'hidden_tests'):
        hidden_result = await _run_tests(candidate, task.hidden_tests)
        # Hidden tests влияют на reward но НЕ возвращаются Dev агенту как feedback
        combined_reward = _combine_rewards(public_result, hidden_result)
    else:
        combined_reward = public_result.reward

    return QAResult(
        passed=public_result.all_passed,
        reward=combined_reward,
        feedback=public_result.feedback,  # только публичный feedback
        hidden_score=hidden_result.score if hidden_result else None,
    )
```

**Разделение тестов в TaskContract:**
```python
@dataclass
class TaskContract:
    # ... существующие поля ...
    tests: list[str] = field(default_factory=list)          # публичные
    hidden_tests: list[str] = field(default_factory=list)   # скрытые (не в LLM промпте)
```

---

### Шаг 4 — Determinism (Random Seed)

**Конфигурация:**
```
RANDOM_SEED=42
```

**Места применения:**

1. **`shared/episode.py`:**
```python
import random
import numpy as np

def set_global_seed(seed: int) -> None:
    """Устанавливает seed для всех random генераторов."""
    random.seed(seed)
    # np.random.seed(seed)  # если используется numpy
    # Для воспроизводимости LLM — temperature=0 + seed в API запросе (если поддерживается)
```

2. **`orchestrator/workflows.py`:**
```python
# В начале каждого workflow:
seed = int(os.getenv("RANDOM_SEED", "42"))
set_global_seed(seed)
# Логировать seed в episode record:
await episodic_memory.store_episode(EpisodeRecord(
    ...,
    random_seed=seed,
    ...
))
```

3. **`memory/replay_buffer.py`:**
```python
# В sample_good/sample_bad:
rng = random.Random(seed)  # локальный RNG, не глобальный
return rng.sample(list(self._good), k)
```

4. **`orchestrator/activities.py` (dev_activity):**
```python
# При использовании exploration/exploitation выбора:
rng = random.Random(seed + episode_hash)  # детерминированный per-episode
```

**Episode Replay:**
Механизм воспроизведения конкретного episode:
```python
def replay_episode(episode_id: str) -> None:
    """
    Загружает episode из PostgreSQL, восстанавливает random_seed,
    и повторяет выполнение.
    """
    episode = db.get_episode(episode_id)
    set_global_seed(episode.random_seed)
    # ... запустить workflow с теми же параметрами ...
```

---

### Шаг 5 — Reward Hacking Protection (дополнительно)

Помимо hidden tests — добавить проверки:

**1. Complexity gate:**
```python
# В RewardEngine.compute():
if cyclomatic_complexity > MAX_ACCEPTABLE_COMPLEXITY:
    return 0.0  # жёсткий штраф за чрезмерную сложность
MAX_ACCEPTABLE_COMPLEXITY = float(os.getenv("MAX_COMPLEXITY", "20"))
```

**2. Code similarity gate:**
```python
# Если код слишком похож на тесты (reward hacking через memorization):
def _check_test_memorization(code: str, tests: list[str]) -> bool:
    """Возвращает True если код содержит hardcoded expected values из тестов."""
    for test in tests:
        expected_values = _extract_expected_values(test)
        if any(val in code for val in expected_values):
            return True
    return False
```

**3. Size limits:**
```python
MAX_SOLUTION_LINES = int(os.getenv("MAX_SOLUTION_LINES", "200"))
if len(candidate.code.splitlines()) > MAX_SOLUTION_LINES:
    # Применить size penalty
    reward *= 0.5
```

---

### Шаг 6 — Тесты

**Файлы для создания:**
- `tests/test_fingerprinting.py`
- `tests/test_determinism.py`
- `tests/test_loop_protection.py`

**`tests/test_fingerprinting.py`:**
- `test_same_code_same_hash()` — одинаковый код → одинаковый hash
- `test_whitespace_normalization()` — разные пробелы → одинаковый hash
- `test_different_code_different_hash()` — разный код → разный hash
- `test_duplicate_detection()` — дублирующий кандидат пропускается в QA
- `test_comment_normalization()` — код с комментариями и без → одинаковый hash

**`tests/test_determinism.py`:**
- `test_same_seed_same_sample()` — один seed → одинаковая выборка из replay buffer
- `test_episode_stores_seed()` — episode record содержит random_seed
- `test_set_global_seed_reproducible()` — после set_global_seed случайные числа воспроизводимы

**`tests/test_loop_protection.py`:**
- `test_stops_at_max_iterations()` — не выполняет больше MAX_ITERATIONS итераций
- `test_stagnation_stops_early()` — останавливается при STAGNATION_THRESHOLD без улучшений
- `test_perfect_score_stops_early()` — reward=1.0 → немедленная остановка
- `test_stopped_reason_correct()` — LearningWorkflowResult содержит правильный stopped_reason

---

## Порядок выполнения

1. Реализовать `compute_code_hash()` в `memory/episodic.py`
2. Добавить duplicate check в `qa_activity`
3. Добавить `hidden_tests` в `TaskContract`
4. Добавить hidden test execution в `qa_activity`
5. Реализовать `set_global_seed()` в `shared/episode.py`
6. Применить seed во всех random вызовах
7. Добавить complexity gate и size limits в `RewardEngine`
8. Написать тесты
9. `pytest tests/ -x`
10. Коммит: `feat(phase9): stability and anti-pattern protection`

---

## Критерии готовности

- [ ] Дублирующий код не оценивается дважды
- [ ] Hidden tests не попадают в LLM промпт
- [ ] `RANDOM_SEED=42` даёт воспроизводимые результаты
- [ ] `MAX_ITERATIONS` и `STAGNATION_THRESHOLD` соблюдаются
- [ ] `pytest tests/test_fingerprinting.py` — зелёный
- [ ] `pytest tests/test_determinism.py` — зелёный
- [ ] `pytest tests/test_loop_protection.py` — зелёный
- [ ] `pytest tests/` — существующие тесты не сломаны

---

## Риски

- **AST normalization edge cases:** некоторые валидные Python конструкции могут не парситься `ast.parse` (Python 3.10+ match/case и т.д.). Добавить fallback к строковому hash.
- **Hidden test isolation:** если hidden tests требуют внешних зависимостей — могут упасть по другим причинам. Нужны self-contained hidden tests.
- **Seed + async:** в async контексте `random.seed()` может влиять на другие корутины. Использовать `random.Random(seed)` для local RNG вместо глобального.
- **LLM non-determinism:** даже с `temperature=0` некоторые LLM провайдеры не гарантируют детерминизм. Документировать это ограничение.
