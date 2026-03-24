# Phase 4 — QA + Reward System

**Приоритет:** High
**Зависимости:** Phase 1 (Memory Layer)
**Блокирует:** Phase 5

---

## Цель

Создать стабильный и измеримый обучающий сигнал. QA агент получает расширенные метрики (время, память, coverage), RewardEngine вычисляет скалярный reward, система детектирует регрессии.

---

## Шаги реализации

### Шаг 1 — QA Runner Upgrade

**Файл для изменения:** `orchestrator/activities.py`

**Текущий QA**: выполняет валидацию через LLM или shell команды.

**Новый QA**: программная интеграция с pytest + замеры.

```python
import pytest
import time
import tracemalloc
from io import StringIO

@dataclass
class QAMetrics:
    tests_passed: int
    tests_failed: int
    tests_total: int
    coverage: float           # 0.0 – 1.0 (если coverage доступен)
    execution_time_ms: float
    peak_memory_mb: float
    error_output: str = ""

async def qa_activity(
    candidates: list[DevResult] | DevResult,
    task: TaskContract,
    episode_id: str,
    iteration: int = 0,
) -> QAResult:
    # Нормализовать candidates в список
    if isinstance(candidates, DevResult):
        candidates = [candidates]

    best_result = None
    best_reward = -1.0

    for candidate in candidates:
        metrics = await _run_pytest_for_candidate(candidate, task)
        reward = reward_engine.compute(metrics, candidate.code)

        if reward > best_reward:
            best_reward = reward
            best_result = QAResult(
                candidate=candidate,
                metrics=metrics,
                reward=reward,
                passed=metrics.tests_failed == 0,
            )

    # Публиковать в Kafka: qa.results
    await _publish_qa_result(best_result, episode_id, iteration)
    return best_result
```

**Функция `_run_pytest_for_candidate`:**
```python
async def _run_pytest_for_candidate(candidate: DevResult,
                                     task: TaskContract) -> QAMetrics:
    # 1. Сохранить код кандидата во временный файл
    # 2. Запустить pytest.main() через subprocess (не в-процессе, чтобы изолировать)
    # 3. Замерить время через time.perf_counter (вокруг subprocess.run)
    # 4. Замерить память через tracemalloc (до/после subprocess)
    # 5. Распарсить junit XML output pytest для получения pass/fail статистики
    ...
```

**Почему subprocess а не pytest.main() напрямую:**
- Изоляция: код кандидата может упасть, завершить процесс, использовать sys.exit
- Чистое замерение памяти
- Нет side effects на основной процесс

**Kafka топик `qa.results`:**
```json
{
  "episode_id": "ep_...",
  "task_id": "T001",
  "iteration": 0,
  "tests_passed": 5,
  "tests_failed": 0,
  "tests_total": 5,
  "coverage": 0.87,
  "execution_time_ms": 342.5,
  "peak_memory_mb": 45.2,
  "reward": 0.91,
  "timestamp": "..."
}
```

---

### Шаг 2 — Reward Engine

**Файл для создания:** `memory/reward.py`

```python
import ast
import math
from dataclasses import dataclass

@dataclass
class RewardWeights:
    correctness: float = 1.0        # REWARD_CORRECTNESS_W env var
    performance: float = 0.3        # REWARD_PERF_W env var
    complexity_penalty: float = 0.2 # REWARD_COMPLEXITY_W env var

    @classmethod
    def from_env(cls) -> "RewardWeights":
        import os
        return cls(
            correctness=float(os.getenv("REWARD_CORRECTNESS_W", "1.0")),
            performance=float(os.getenv("REWARD_PERF_W", "0.3")),
            complexity_penalty=float(os.getenv("REWARD_COMPLEXITY_W", "0.2")),
        )

class RewardEngine:
    def __init__(self, weights: RewardWeights | None = None):
        self.weights = weights or RewardWeights.from_env()

    def compute(self, metrics: QAMetrics, code: str) -> float:
        """
        reward = correctness * w_c + performance * w_p - complexity * w_x

        correctness  = tests_passed / tests_total  (0.0 – 1.0)
        performance  = 1 / (1 + exec_time_ms / 1000)  (0.0 – 1.0, ближе к 0 ms = ближе к 1)
        complexity   = cyclomatic_complexity(code) / MAX_COMPLEXITY  (нормализовано)
        """
        if metrics.tests_total == 0:
            return 0.0

        correctness = metrics.tests_passed / metrics.tests_total
        performance = 1.0 / (1.0 + metrics.execution_time_ms / 1000.0)
        complexity = self._cyclomatic_complexity(code) / 10.0  # нормализация

        return (
            correctness * self.weights.correctness
            + performance * self.weights.performance
            - complexity * self.weights.complexity_penalty
        )

    def _cyclomatic_complexity(self, code: str) -> float:
        """
        Приближение цикломатической сложности через AST:
        complexity = 1 + (число if/elif/for/while/except/and/or)
        """
        try:
            tree = ast.parse(code)
            count = sum(
                1 for node in ast.walk(tree)
                if isinstance(node, (
                    ast.If, ast.For, ast.While, ast.ExceptHandler,
                    ast.BoolOp, ast.comprehension
                ))
            )
            return float(count + 1)
        except SyntaxError:
            return 1.0  # если код не парсится — минимальная сложность
```

**Env variables:**
```
REWARD_CORRECTNESS_W=1.0
REWARD_PERF_W=0.3
REWARD_COMPLEXITY_W=0.2
```

---

### Шаг 3 — Regression Detection

**Место реализации:** `memory/episodic.py` (расширение) + `orchestrator/activities.py`

**Алгоритм:**
```python
async def _check_regression(
    new_reward: float,
    task_id: str,
    episodic_memory: EpisodicMemory
) -> bool:
    """
    Возвращает True если новое решение хуже лучшего известного.
    """
    best = await episodic_memory.get_best_solution(task_id)
    if best is None:
        return False  # нет истории — не регрессия
    return new_reward < best.reward

# В qa_activity:
is_regression = await _check_regression(qa_result.reward, task.task_id, ...)
if is_regression:
    logger.warning(f"Regression detected: {qa_result.reward:.3f} < {best.reward:.3f}")
    # НЕ обновлять best_solution
    # Добавить пометку в QAResult
else:
    await episodic_memory.store_solution(solution_record)
```

---

### Шаг 4 — Reward Publishing

**Kafka топик `reward.computed`:**
```json
{
  "episode_id": "ep_...",
  "task_id": "T001",
  "iteration": 0,
  "reward": 0.91,
  "correctness": 1.0,
  "performance": 0.75,
  "complexity_penalty": 0.15,
  "is_regression": false,
  "is_best": true,
  "timestamp": "..."
}
```

---

### Шаг 5 — Тесты

**Файлы для создания:**
- `tests/test_reward_engine.py`
- `tests/test_qa_runner.py`
- `tests/test_regression_detection.py`

**`tests/test_reward_engine.py`:**
- `test_perfect_solution_reward()` — 100% тесты, быстро, простой код → reward близко к максимуму
- `test_zero_tests_passed()` — tests_passed=0 → reward=0.0
- `test_complexity_penalty_applied()` — сложный код снижает reward
- `test_performance_factor()` — медленное выполнение снижает reward
- `test_weights_from_env()` — `RewardWeights.from_env()` читает env vars
- `test_cyclomatic_complexity_simple()` — линейный код → complexity=1
- `test_cyclomatic_complexity_branchy()` — много if/for → complexity>5

**`tests/test_qa_runner.py`:**
- `test_runs_pytest_for_candidate()` — mock subprocess, проверяет что pytest вызывается
- `test_returns_best_candidate()` — из 3 кандидатов выбирает с наибольшим reward
- `test_publishes_to_kafka()` — mock Kafka producer получает `qa.results`
- `test_metrics_time_measured()` — execution_time_ms > 0

**`tests/test_regression_detection.py`:**
- `test_no_regression_first_solution()` — нет истории → не регрессия
- `test_regression_detected()` — новый reward < best → is_regression=True
- `test_improvement_not_regression()` — новый reward > best → is_regression=False, solution сохраняется

---

## Порядок выполнения

1. Создать `memory/reward.py` с `RewardEngine` и `RewardWeights`
2. Обновить `qa_activity` в `orchestrator/activities.py`:
   - Добавить subprocess-запуск pytest
   - Интегрировать замеры времени и памяти
   - Вызывать `RewardEngine.compute()`
3. Добавить regression detection логику
4. Добавить Kafka publishing для `qa.results` и `reward.computed`
5. Написать тесты
6. `pytest tests/ -x`
7. Коммит: `feat(phase4): qa runner upgrade and reward engine`

---

## Критерии готовности

- [ ] QA activity запускает pytest программно через subprocess
- [ ] `RewardEngine.compute()` возвращает корректный скалярный reward
- [ ] Regression detection работает: плохое решение не перезаписывает лучшее
- [ ] Kafka публикует `qa.results` и `reward.computed`
- [ ] `pytest tests/test_reward_engine.py` — зелёный
- [ ] `pytest tests/test_qa_runner.py` — зелёный
- [ ] `pytest tests/` — существующие тесты не сломаны

---

## Риски

- **subprocess pytest:** нужно корректно передавать путь к тестовому файлу задачи. Если у задачи нет тестов — graceful fallback (reward based on LLM review).
- **junit XML parsing:** pytest должен запускаться с `--junit-xml=<tmp_file>` для парсинга. Обработать случай когда XML не создан (pytest упал до запуска тестов).
- **tracemalloc overhead:** tracemalloc добавляет overhead ~10-30%. Учитывать при нормализации performance reward.
- **cyclomatic complexity для multi-file:** если код кандидата распределён по нескольким файлам — считать сложность для основного файла.
