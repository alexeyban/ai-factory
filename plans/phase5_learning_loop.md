# Phase 5 — Learning Loop (AlphaZero-style)

**Приоритет:** High
**Зависимости:** Phase 2 (Skills), Phase 3 (Dev Evolution), Phase 4 (Reward)
**Блокирует:** Phase 6, 7, 9

---

## Цель

Self-play итеративное обучение: система многократно решает задачи, накапливает лучшие решения в Replay Buffer, обновляет политику Dev агента и веса навыков на основе reward сигнала.

---

## Шаги реализации

### Шаг 1 — LearningWorkflow

**Файл для изменения:** `orchestrator/workflows.py`

Создать новый Temporal workflow:

```python
@dataclass
class LearningWorkflowInput:
    task: TaskContract
    max_iterations: int = 5              # MAX_ITERATIONS env var
    num_candidates: int = 3              # NUM_CANDIDATES env var
    exploration_rate: float = 0.3       # EXPLORATION_RATE env var
    stagnation_threshold: int = 3       # STAGNATION_THRESHOLD env var
    episode_id: str = field(default_factory=new_episode_id)

@dataclass
class LearningWorkflowResult:
    best_solution: DevResult
    best_reward: float
    total_iterations: int
    stopped_reason: str    # 'max_iterations' | 'stagnation' | 'perfect_score'
    skills_extracted: int

@workflow.defn
class LearningWorkflow:
    @workflow.run
    async def run(self, input: LearningWorkflowInput) -> LearningWorkflowResult:
        best_reward = 0.0
        best_solution = None
        skills_extracted = 0
        stagnation_count = 0
        stopped_reason = 'max_iterations'

        for iteration in range(input.max_iterations):
            # 1. Генерация кандидатов
            candidates = await workflow.execute_activity(
                dev_activity,
                args=[input.task, input.episode_id, iteration,
                      input.num_candidates, input.exploration_rate],
                ...
            )

            # 2. QA + Reward
            qa_result = await workflow.execute_activity(
                qa_activity,
                args=[candidates, input.task, input.episode_id, iteration],
                ...
            )

            # 3. Обновление best solution
            if qa_result.reward > best_reward:
                best_reward = qa_result.reward
                best_solution = qa_result.best_candidate
                stagnation_count = 0

                # 4. Извлечение skill при улучшении
                if qa_result.passed:
                    extracted = await workflow.execute_activity(
                        extract_skill_activity,
                        args=[qa_result.best_candidate, input.episode_id],
                        ...
                    )
                    if extracted:
                        skills_extracted += 1
            else:
                stagnation_count += 1

            # 5. Stagnation detection
            if stagnation_count >= input.stagnation_threshold:
                stopped_reason = 'stagnation'
                break

            # 6. Perfect score early stop
            if best_reward >= 0.99:
                stopped_reason = 'perfect_score'
                break

        # 7. Policy Update
        await workflow.execute_activity(
            policy_update_activity,
            args=[input.episode_id, best_solution, best_reward],
            ...
        )

        return LearningWorkflowResult(
            best_solution=best_solution,
            best_reward=best_reward,
            total_iterations=iteration + 1,
            stopped_reason=stopped_reason,
            skills_extracted=skills_extracted,
        )
```

**Новые activities:**
- `extract_skill_activity` — обёртка над `SkillExtractor.extract_from_solution()`
- `policy_update_activity` — обёртка над `PolicyUpdater.update()`

---

### Шаг 2 — Replay Buffer

**Файл для создания:** `memory/replay_buffer.py`

```python
from collections import deque
import random
from dataclasses import dataclass

@dataclass
class BufferedSolution:
    solution: DevResult         # или путь к коду
    reward: float
    task_id: str
    episode_id: str
    iteration: int
    skills_used: list[str]      # IDs использованных skills

class ReplayBuffer:
    def __init__(
        self,
        max_good: int = 100,  # top solutions (reward > threshold)
        max_bad: int = 50,    # bad solutions (для anti-pattern learning)
        good_threshold: float = 0.7,
    ):
        self._good: deque[BufferedSolution] = deque(maxlen=max_good)
        self._bad: deque[BufferedSolution] = deque(maxlen=max_bad)
        self._threshold = good_threshold

    def add(self, solution: BufferedSolution) -> None:
        if solution.reward >= self._threshold:
            self._good.append(solution)
        else:
            self._bad.append(solution)

    def sample_good(self, k: int) -> list[BufferedSolution]:
        """Случайная выборка k хороших решений."""
        k = min(k, len(self._good))
        return random.sample(list(self._good), k)

    def sample_bad(self, k: int) -> list[BufferedSolution]:
        """Случайная выборка k плохих решений (для failure learning)."""
        k = min(k, len(self._bad))
        return random.sample(list(self._bad), k)

    def get_best(self, task_id: str) -> BufferedSolution | None:
        """Лучшее решение для конкретной задачи."""
        relevant = [s for s in self._good if s.task_id == task_id]
        return max(relevant, key=lambda s: s.reward, default=None)

    def size(self) -> dict:
        return {"good": len(self._good), "bad": len(self._bad)}

    def to_json(self) -> str:
        """Сериализация для persistence между запусками."""
        ...

    @classmethod
    def from_json(cls, data: str) -> "ReplayBuffer":
        ...
```

**Persistence:** Replay Buffer должен сохраняться в `workspace/.ai_factory/replay_buffer.json` и загружаться при старте следующего workflow.

---

### Шаг 3 — Policy Updater

**Файл для создания:** `memory/policy_updater.py`

```python
class PolicyUpdater:
    def __init__(self, replay_buffer: ReplayBuffer, db: MemoryDB,
                 skill_registry: SkillRegistry): ...

    async def update(self, episode_id: str, best_solution: DevResult,
                      best_reward: float) -> None:
        await self._update_prompt_examples(best_solution, best_reward)
        await self._update_skill_weights(best_solution)
        await self._update_exploration_rate(best_reward)

    async def _update_prompt_examples(self, solution: DevResult,
                                        reward: float) -> None:
        """
        Добавляет лучшие решения из Replay Buffer в системный промпт Dev агента
        как few-shot примеры.

        Если reward > 0.8: сохраняет пример в shared/prompts/dev/examples.json
        Промпт включает до 3 таких примеров.
        """
        ...

    async def _update_skill_weights(self, solution: DevResult) -> None:
        """
        Для каждого использованного skill_id:
        - Если solution.reward > 0.7: success_rate += (1 - current_rate) * 0.1
        - Если solution.reward < 0.3: success_rate -= current_rate * 0.1
        - Обновить в PostgreSQL и Qdrant payload
        """
        ...

    async def _update_exploration_rate(self, best_reward: float) -> None:
        """
        Адаптивное снижение exploration_rate:
        - Если накоплено skills > 20 и avg_reward > 0.7: rate *= 0.95
        - Минимум: 0.1 (всегда немного исследуем)
        - Сохранять в workspace/.ai_factory/policy_state.json
        """
        ...
```

---

### Шаг 4 — Stagnation Detection

Уже встроено в `LearningWorkflow` (счётчик `stagnation_count`).

Дополнительно — логировать событие стагнации:
```python
await log_episode_event(
    episode_id=input.episode_id,
    event_type="stagnation_detected",
    agent="learning_workflow",
    data={"iteration": iteration, "stagnation_count": stagnation_count}
)
```

---

### Шаг 5 — Обновление .env

```
MAX_ITERATIONS=5
STAGNATION_THRESHOLD=3
```

---

### Шаг 6 — Тесты

**Файлы для создания:**
- `tests/test_replay_buffer.py`
- `tests/test_policy_updater.py`
- `tests/test_learning_workflow.py`

**`tests/test_replay_buffer.py`:**
- `test_add_good_solution()` — reward >= threshold → попадает в _good
- `test_add_bad_solution()` — reward < threshold → попадает в _bad
- `test_maxlen_eviction()` — при переполнении старые удаляются
- `test_sample_good_k()` — возвращает k элементов
- `test_get_best_for_task()` — корректный выбор лучшего по task_id
- `test_persistence_roundtrip()` — to_json() → from_json() сохраняет данные

**`tests/test_policy_updater.py`:**
- `test_skill_weight_increases_on_success()` — success_rate растёт при reward > 0.7
- `test_skill_weight_decreases_on_failure()` — success_rate падает при reward < 0.3
- `test_exploration_rate_decreases()` — rate снижается при накоплении skills

**`tests/test_learning_workflow.py`** (интеграционный, через mock activities):
- `test_stops_on_stagnation()` — после N итераций без улучшения → stopped_reason='stagnation'
- `test_stops_on_perfect_score()` — reward=1.0 → early stop
- `test_extracts_skill_on_improvement()` — при улучшении reward вызывается extract_skill_activity
- `test_returns_best_solution()` — возвращает решение с максимальным reward

---

## Порядок выполнения

1. Создать `memory/replay_buffer.py`
2. Создать `memory/policy_updater.py`
3. Добавить `LearningWorkflow` в `orchestrator/workflows.py`
4. Добавить `extract_skill_activity` и `policy_update_activity` в `orchestrator/activities.py`
5. Написать тесты
6. `pytest tests/ -x`
7. Коммит: `feat(phase5): alphazero-style learning loop`

---

## Критерии готовности

- [ ] `LearningWorkflow` выполняет N итераций dev→qa→reward
- [ ] Stagnation detection останавливает цикл после STAGNATION_THRESHOLD итераций без улучшения
- [ ] Replay Buffer накапливает good/bad решения между итерациями
- [ ] Policy Updater обновляет skill weights после каждого эпизода
- [ ] `pytest tests/test_replay_buffer.py` — зелёный
- [ ] `pytest tests/test_learning_workflow.py` — зелёный
- [ ] `pytest tests/` — существующие тесты не сломаны

---

## Риски

- **Temporal workflow complexity:** `LearningWorkflow` — длительный workflow. Нужно учитывать `MAX_TASK_EXECUTION_SECONDS=900`. При `max_iterations=5` и 5+ кандидатах легко превысить лимит. Рассмотреть child workflows или continuation.
- **Replay Buffer persistence:** при перезапуске workflow буфер теряется. `workspace/.ai_factory/replay_buffer.json` должен читаться в начале workflow.
- **Policy state drift:** если `exploration_rate` хранится в файле, параллельные workflows могут конфликтовать. Добавить блокировку или использовать PostgreSQL для состояния политики.
- **Circular dependency:** PolicyUpdater меняет промпты Dev агента, которые влияют на следующие итерации. Изменения должны применяться только в СЛЕДУЮЩЕМ episode, не в текущем.
