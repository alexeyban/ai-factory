# Phase 3 — Dev Agent Evolution

**Приоритет:** High
**Зависимости:** Phase 1 (Memory), Phase 2 (Skills)
**Блокирует:** Phase 5

---

## Цель

Dev агент трансформируется из простого code generator в policy + skill composer: генерирует K кандидатов параллельно, использует накопленные skills в промпте, применяет epsilon-greedy стратегию exploration vs exploitation.

---

## Шаги реализации

### Шаг 1 — Multi-Candidate Generation

**Файл для изменения:** `orchestrator/activities.py`

**Текущий интерфейс `dev_activity`:**
```python
async def dev_activity(task: TaskContract, ...) -> DevResult
```

**Новый интерфейс:**
```python
async def dev_activity(
    task: TaskContract,
    episode_id: str,
    iteration: int = 0,
    num_candidates: int = 1,  # default=1 для backward compatibility
    exploration_rate: float = 0.3,
) -> list[DevResult]           # ВСЕГДА возвращает список (даже при num_candidates=1)
```

**Алгоритм генерации кандидатов:**
```python
async def dev_activity(...) -> list[DevResult]:
    strategies = _get_strategies(num_candidates, exploration_rate)
    candidates = await asyncio.gather(*[
        _generate_single_solution(task, strategy, episode_id, iteration)
        for strategy in strategies
    ], return_exceptions=True)
    # Фильтровать Exception результаты, логировать ошибки
    return [c for c in candidates if isinstance(c, DevResult)]
```

**Стратегии (`_get_strategies`):**
- `'explore'` — генерировать без skills, свежий подход
- `'exploit_top1'` — использовать skill с наивысшим success_rate
- `'exploit_top2'` — использовать второй по рейтингу skill
- Распределение: `floor(num_candidates * exploration_rate)` = explore, остальные = exploit

**Epsilon-greedy логика:**
```python
def _get_strategies(num_candidates: int, exploration_rate: float) -> list[str]:
    n_explore = max(1, round(num_candidates * exploration_rate))
    n_exploit = num_candidates - n_explore
    return ['explore'] * n_explore + ['exploit'] * n_exploit
```

---

### Шаг 2 — Skill-Aware Prompting

**Файл для изменения:** `shared/prompts/dev/system.txt` (или `shared/prompts/dev.py`)

Определить точное расположение промптов. Скорее всего — `shared/prompts/dev/system.txt` и `shared/prompts/dev/user.txt`.

**Добавить в системный промпт Dev агента:**
```
## Available Skills (use these if relevant)
{skills_context}

## Known Failure Patterns (avoid these)
{failure_patterns}

## Strategy: {strategy}
{"Explore: generate a fresh solution without using the skills above." if strategy == 'explore' else "Exploit: leverage the skills above to compose your solution."}
```

**Функция для построения контекста:**
```python
async def build_dev_context(
    task: TaskContract,
    strategy: str,
    skill_retriever,
    failure_memory: FailureMemory,
) -> dict:
    if strategy == 'explore':
        skills_context = "No skills provided — generate original solution."
        failure_patterns = ""
    else:
        embedding = await get_task_embedding(task.description)
        skills_context = await get_skill_context_for_prompt(embedding, ...)
        failure_patterns = await failure_memory.get_failure_summary(task.type)

    return {
        "skills_context": skills_context,
        "failure_patterns": failure_patterns,
        "strategy": strategy,
    }
```

---

### Шаг 3 — Code Composer

**Файл для создания:** `orchestrator/code_composer.py`

```python
class CodeComposer:
    def compose(self, skills: list[Skill], new_code: str) -> str:
        """
        Объединяет код skills + новый код в единый solution.
        Алгоритм:
        1. Извлечь import-строки из каждого skill-файла
        2. Дедуплицировать imports
        3. Извлечь функции/классы из skill-файлов (не дублировать main guard)
        4. Объединить: imports + skill_functions + new_code
        """
        imports = self._extract_imports(skills)
        skill_code = self._merge_skill_functions(skills)
        deduplicated_imports = self._deduplicate_imports(imports)
        return f"{deduplicated_imports}\n\n{skill_code}\n\n{new_code}"

    def _extract_imports(self, skills: list[Skill]) -> list[str]:
        """Читает .py файлы skills и извлекает строки import/from"""
        ...

    def _merge_skill_functions(self, skills: list[Skill]) -> str:
        """Объединяет функции из skills, убирая дублирующиеся"""
        ...

    def _deduplicate_imports(self, imports: list[str]) -> str:
        """Убирает дублирующиеся import-строки"""
        ...
```

**Важно:** Composer используется в `_generate_single_solution` когда strategy='exploit':
```python
if strategy == 'exploit' and relevant_skills:
    composed_code = code_composer.compose(relevant_skills, raw_llm_code)
else:
    composed_code = raw_llm_code
```

---

### Шаг 4 — QA Integration для Multi-Candidate

**Файл для изменения:** `orchestrator/activities.py`

QA activity должен принимать список кандидатов:
```python
async def qa_activity(
    candidates: list[DevResult],  # новый тип
    task: TaskContract,
    episode_id: str,
    iteration: int = 0,
) -> QAResult:
    """
    1. Прогнать тесты для каждого кандидата
    2. Вычислить reward для каждого (если Phase 4 уже реализована)
    3. Выбрать лучшего кандидата (highest reward, или первый прошедший тесты)
    4. Вернуть QAResult с best_candidate
    """
```

**Backward compatibility:** если `candidates` — это один `DevResult` (старый формат) — обернуть в список.

---

### Шаг 5 — Обновление .env

```
EXPLORATION_RATE=0.3
NUM_CANDIDATES=3
```

Если переменные отсутствуют — использовать defaults (backward compatible).

---

### Шаг 6 — Тесты

**Файлы для создания:**
- `tests/test_dev_multi_candidate.py`
- `tests/test_code_composer.py`

**`tests/test_dev_multi_candidate.py`:**
- `test_generates_num_candidates()` — при `num_candidates=3` возвращает 3 результата
- `test_single_candidate_backward_compat()` — `num_candidates=1` возвращает список из 1
- `test_exploration_distribution()` — при `exploration_rate=1.0` все стратегии 'explore'
- `test_handles_partial_failure()` — если 1 из 3 кандидатов упал с Exception, возвращает 2
- `test_skill_context_in_exploit()` — при strategy='exploit' skills передаются в промпт

**`tests/test_code_composer.py`:**
- `test_compose_single_skill()` — код skill + новый код объединены
- `test_deduplicates_imports()` — одинаковые imports не дублируются
- `test_compose_no_skills()` — пустой список skills → только new_code
- `test_preserves_skill_functions()` — функции из skill-файла присутствуют в результате

---

## Порядок выполнения

1. Определить точный путь к Dev промптам (`shared/prompts/dev/`)
2. Создать `orchestrator/code_composer.py`
3. Обновить `dev_activity` в `orchestrator/activities.py`:
   - Добавить `num_candidates` параметр
   - Реализовать `asyncio.gather` генерацию
   - Добавить `build_dev_context` с skill/failure injection
4. Обновить `qa_activity` для приёма списка кандидатов
5. Обновить Dev промпты для поддержки `{skills_context}` и `{failure_patterns}`
6. Написать тесты
7. `pytest tests/ -x`
8. Коммит: `feat(phase3): multi-candidate dev with skill-aware prompting`

---

## Критерии готовности

- [x] `dev_activity(num_candidates=3)` возвращает 3 результата (через _get_strategies + asyncio.gather)
- [x] При `strategy='exploit'` skills присутствуют в промпте (_build_dev_prompt + skills_context)
- [x] При `strategy='explore'` skills не передаются (пустые skills_context/failure_patterns)
- [x] `code_composer.compose()` корректно объединяет код (imports dedup + function merge)
- [x] `pytest tests/test_dev_multi_candidate.py` — зелёный (12/12)
- [x] `pytest tests/test_code_composer.py` — зелёный (13/13)
- [x] `pytest tests/` — существующие тесты не сломаны (148/148)

---

## Риски

- **asyncio.gather с exceptions:** при параллельных LLM-вызовах один может упасть. `return_exceptions=True` обязателен, нужна фильтрация результатов.
- **Temporal activity timeout:** при `num_candidates=3` время выполнения утраивается. Убедиться что `WORKFLOW_LLM_ACTIVITY_TIMEOUT_MINUTES` достаточен (или поднять для фазы 3+).
- **QA backward compat:** изменение сигнатуры `qa_activity` — критически важна совместимость с существующими Temporal workflow контрактами. Использовать Union type или перегрузку.
- **Code composer quality:** LLM код может использовать неправильные импорты. Composer должен справляться с ошибками парсинга — fallback к `new_code` без composition.
