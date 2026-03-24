# Phase 6 — Self-Modification

**Приоритет:** Medium
**Зависимости:** Phase 5 (Learning Loop)
**Блокирует:** ничего (конечная фаза ветки)

---

## Цель

Агент улучшает собственную базу навыков: рефакторит отдельные skills через LLM, объединяет похожие skills в более общие, удаляет слабые. Опционально — MetaAgent для анализа системных параметров.

---

## Шаги реализации

### Шаг 1 — Skill Optimizer

**Файл для создания:** `memory/skill_optimizer.py`

```python
class SkillOptimizer:
    def __init__(
        self,
        db: MemoryDB,
        vector_memory: VectorMemory,
        llm_client,
        skill_registry: SkillRegistry,
        similarity_threshold: float = 0.9,  # SKILL_SIMILARITY_THRESHOLD env var
        prune_threshold: float = 0.3,        # SKILL_PRUNE_THRESHOLD env var
    ): ...

    async def run_optimization_cycle(self, episode_count: int) -> dict:
        """
        Основная точка входа. Вызывается каждые SKILL_OPTIMIZE_EVERY_N эпизодов.
        Возвращает статистику: {refactored: N, merged: M, pruned: K}
        """
        stats = {}
        stats['refactored'] = await self._refactor_weak_skills()
        stats['merged'] = await self._merge_similar_skills()
        stats['pruned'] = await self.prune_weak_skills()
        return stats

    async def refactor_skill(self, skill: Skill) -> Skill | None:
        """
        LLM оптимизирует код skill.
        Алгоритм:
        1. Прочитать текущий код из skills/<id>.py
        2. Отправить в LLM:
           "Optimize this Python function for clarity and performance.
            Return only the improved code, no explanations.
            Code: {code}"
        3. Прогнать тесты нового кода (если есть тест-файл)
        4. Если тесты прошли — заменить файл, обновить embedding
        5. Если тесты упали — откатить, вернуть None
        """
        ...

    async def merge_skills(self, skills: list[Skill]) -> Skill | None:
        """
        Объединяет список похожих skills (similarity > threshold) в один.
        Алгоритм:
        1. Отправить в LLM код всех skills:
           "Merge these similar Python functions into one general implementation.
            Return JSON: {name, description, code, tags}"
        2. Создать новый skill файл
        3. Обновить PostgreSQL: новый skill + пометить старые is_active=False
        4. Обновить Qdrant: удалить старые, добавить новый
        5. Обновить registry.json
        6. Вернуть новый Skill
        """
        ...

    async def prune_weak_skills(self, threshold: float | None = None) -> int:
        """
        Удаляет skills с success_rate < threshold.
        Алгоритм:
        1. SELECT * FROM skills WHERE success_rate < threshold AND is_active=TRUE
        2. Пометить is_active=FALSE в PostgreSQL (soft delete)
        3. Удалить из Qdrant коллекции
        4. Обновить registry.json (убрать из активных)
        5. НЕ удалять .py файлы (для истории / возможного восстановления)
        6. Вернуть количество pruned skills
        """
        ...

    async def _refactor_weak_skills(self) -> int:
        """
        Выбирает skills с success_rate < 0.5 и use_count > 3 для рефакторинга.
        Возвращает количество успешно рефакторенных.
        """
        candidates = await self._get_refactor_candidates()
        count = 0
        for skill in candidates:
            result = await self.refactor_skill(skill)
            if result:
                count += 1
        return count

    async def _merge_similar_skills(self) -> int:
        """
        Находит кластеры похожих skills через Qdrant (cosine similarity > threshold).
        Объединяет каждый кластер.
        """
        clusters = await self._find_similar_clusters()
        count = 0
        for cluster in clusters:
            if len(cluster) >= 2:
                merged = await self.merge_skills(cluster)
                if merged:
                    count += 1
        return count

    async def _find_similar_clusters(self) -> list[list[Skill]]:
        """
        Использует Qdrant для поиска групп похожих skills:
        1. Для каждого active skill ищет соседей с similarity > threshold
        2. Строит граф схожести
        3. Выделяет connected components как кластеры
        """
        ...
```

---

### Шаг 2 — Триггер оптимизации

**Место:** `orchestrator/activities.py` или `orchestrator/workflows.py`

```python
SKILL_OPTIMIZE_EVERY_N = int(os.getenv("SKILL_OPTIMIZE_EVERY_N", "10"))

# В конце каждого episode (после policy_update_activity):
episode_count = await get_total_episode_count(db)
if episode_count % SKILL_OPTIMIZE_EVERY_N == 0:
    await workflow.execute_activity(
        skill_optimization_activity,
        args=[episode_count],
        ...
    )
```

**Activity:**
```python
async def skill_optimization_activity(episode_count: int) -> dict:
    optimizer = SkillOptimizer(...)
    stats = await optimizer.run_optimization_cycle(episode_count)
    logger.info(f"Skill optimization: {stats}")
    # Публиковать в Kafka: memory.events (event_type=skills_optimized)
    return stats
```

---

### Шаг 3 — MetaAgent (опционально)

**Файл для создания:** `orchestrator/workflows.py` (дополнение)

```python
@workflow.defn
class MetaAnalysisWorkflow:
    """
    Запускается раз в сутки (через Temporal schedule или внешний cron).
    Анализирует aggregate metrics и предлагает изменения конфигурации.
    """

    @workflow.run
    async def run(self) -> MetaAnalysisResult:
        # 1. Получить aggregate stats из PostgreSQL за последние N эпизодов
        # 2. Если avg_reward снижается — предложить увеличить exploration_rate
        # 3. Если skill_count > 200 — запустить более агрессивное pruning
        # 4. Записать рекомендации в workspace/.ai_factory/meta_recommendations.json
        # 5. (Опционально) Отправить в Slack/webhook
        ...
```

Этот шаг — **низкий приоритет**, реализовать только после того как Шаги 1-2 работают стабильно.

---

### Шаг 4 — Обновление .env

```
SKILL_OPTIMIZE_EVERY_N=10
SKILL_SIMILARITY_THRESHOLD=0.9
SKILL_PRUNE_THRESHOLD=0.3
```

---

### Шаг 5 — Тесты

**Файлы для создания:**
- `tests/test_skill_optimizer.py`

**`tests/test_skill_optimizer.py`:**
- `test_prune_weak_skills()` — skills с success_rate < threshold помечаются is_active=False
- `test_prune_returns_count()` — возвращает правильное количество
- `test_refactor_skill_updates_code()` — mock LLM возвращает новый код, файл обновляется
- `test_refactor_skill_rollback_on_test_failure()` — если тесты упали, файл не изменяется
- `test_merge_skills_creates_new()` — объединение создаёт новый skill, старые деактивируются
- `test_find_similar_clusters()` — mock Qdrant, корректные кластеры
- `test_optimization_cycle_runs_all()` — `run_optimization_cycle()` вызывает все 3 метода

---

## Порядок выполнения

1. Создать `memory/skill_optimizer.py` — методы `refactor_skill`, `merge_skills`, `prune_weak_skills`
2. Реализовать `_find_similar_clusters` через Qdrant
3. Добавить `skill_optimization_activity` в `orchestrator/activities.py`
4. Добавить триггер в workflow (каждые N эпизодов)
5. (Опционально) Реализовать `MetaAnalysisWorkflow`
6. Написать тесты
7. `pytest tests/ -x`
8. Коммит: `feat(phase6): self-modification with skill optimizer`

---

## Критерии готовности

- [ ] `SkillOptimizer.prune_weak_skills()` удаляет слабые skills
- [ ] `SkillOptimizer.merge_skills()` объединяет похожие
- [ ] `SkillOptimizer.refactor_skill()` улучшает код через LLM
- [ ] Триггер запускается каждые `SKILL_OPTIMIZE_EVERY_N` эпизодов
- [ ] `pytest tests/test_skill_optimizer.py` — зелёный
- [ ] `pytest tests/` — существующие тесты не сломаны

---

## Риски

- **LLM refactoring quality:** LLM может ухудшить код при рефакторинге. Обязательная проверка тестами перед применением — критическое требование.
- **Merge conflicts:** объединение skills может породить невалидный Python код. Парсить AST перед сохранением.
- **Qdrant cluster detection:** наивный алгоритм O(N²). При skill_count > 1000 нужен более эффективный подход (approximate kNN clustering).
- **Data loss risk:** soft delete (is_active=False) безопаснее hard delete. .py файлы не удалять.
- **Circular refactoring:** система не должна рефакторить один skill дважды подряд — добавить `last_optimized_at` поле.
