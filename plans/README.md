# AI Factory — Self-Learning Agent: Планы реализации

Детальные планы для каждой фазы из спецификации `ai_factory_self_learning_spec.md`.

---

## Порядок реализации

| Приоритет | Файл | Фаза | Зависит от | Блокирует |
|-----------|------|------|------------|-----------|
| 1 — Critical | [phase0_refactoring.md](phase0_refactoring.md) | Unified Contract + Episodes | — | Все |
| 2 — Critical | [phase1_memory_layer.md](phase1_memory_layer.md) | PostgreSQL + Qdrant + Memory API | Phase 0 | 2,3,4,5 |
| 3 — High | [phase2_skill_engine.md](phase2_skill_engine.md) | Skill Extract/Retrieve/Execute | Phase 1 | 3,5,6 |
| 4 — High | [phase4_qa_reward_system.md](phase4_qa_reward_system.md) | QA Runner + RewardEngine | Phase 1 | Phase 5 |
| 5 — High | [phase3_dev_agent_evolution.md](phase3_dev_agent_evolution.md) | Multi-candidate + Skill Prompting | Phase 1,2 | Phase 5 |
| 6 — Medium | [phase5_learning_loop.md](phase5_learning_loop.md) | LearningWorkflow + ReplayBuffer | Phase 2,3,4 | 6,7,9 |
| 7 — Medium | [phase8_infrastructure.md](phase8_infrastructure.md) | Docker + Kafka + OpenTelemetry | Phase 0 | все сервисы |
| 8 — Medium | [phase7_benchmarking.md](phase7_benchmarking.md) | Dataset + Curriculum + Grafana | Phase 5 | — |
| 9 — Low | [phase6_self_modification.md](phase6_self_modification.md) | SkillOptimizer + MetaAgent | Phase 5 | — |
| 10 — Low | [phase9_stability.md](phase9_stability.md) | Fingerprinting + Hidden Tests + Seed | Phase 5 | — |

---

## Новые файлы по фазам

### Phase 0
- `shared/contracts/task_schema.yaml`
- `shared/contracts/task_loader.py`
- `shared/contracts/kafka_task_contract.py`
- `shared/episode.py`
- `tests/test_task_contract.py`
- `tests/test_episode.py`

### Phase 1
- `memory/migrations/001_memory_tables.sql`
- `memory/db.py`
- `memory/episodic.py`
- `memory/failures.py`
- `memory/vector_store.py`

### Phase 2
- `memory/skill.py`
- `memory/skill_extractor.py`
- `memory/skill_retriever.py`
- `memory/skill_executor.py`
- `skills/__init__.py`
- `skills/registry.json`

### Phase 3
- `orchestrator/code_composer.py`

### Phase 4
- `memory/reward.py`

### Phase 5
- `memory/replay_buffer.py`
- `memory/policy_updater.py`

### Phase 6
- `memory/skill_optimizer.py`

### Phase 7
- `benchmarks/dataset_loader.py`
- `benchmarks/curriculum.py`
- `benchmarks/datasets/easy.json`
- `benchmarks/metrics_exporter.py`

### Phase 8
- `infra/dockerfiles/memory-worker.Dockerfile`
- `infra/dockerfiles/reward-worker.Dockerfile`
- `infra/dockerfiles/meta-agent.Dockerfile`
- `infra/prometheus.yml`
- `infra/kafka_topics.sh`
- `memory/worker.py`
- `memory/reward_worker.py`

### Phase 9
- (расширения существующих модулей)

---

## Изменяемые файлы

| Файл | Фазы |
|------|------|
| `docker-compose.yml` | 1 (Qdrant), 7 (Prometheus/Grafana), 8 (все сервисы) |
| `orchestrator/workflows.py` | 0 (iterations), 5 (LearningWorkflow), 6 (MetaAgent) |
| `orchestrator/activities.py` | 0 (episode_id), 3 (multi-candidate), 4 (pytest+reward) |
| `shared/llm.py` | 8 (OpenTelemetry) |
| `shared/prompts/dev/` | 3 (skills + failure patterns) |
| `.env` | 0,1,3,4,5,6,7,9 (новые переменные) |

---

## Ключевые переменные окружения (итого)

```bash
# Phase 0
MAX_ITERATIONS=1
RANDOM_SEED=42

# Phase 1
QDRANT_URL=http://localhost:6333
MEMORY_DB_URL=postgresql://...

# Phase 3
EXPLORATION_RATE=0.3
NUM_CANDIDATES=3

# Phase 4
REWARD_CORRECTNESS_W=1.0
REWARD_PERF_W=0.3
REWARD_COMPLEXITY_W=0.2

# Phase 5
STAGNATION_THRESHOLD=3

# Phase 6
SKILL_OPTIMIZE_EVERY_N=10
SKILL_SIMILARITY_THRESHOLD=0.9
SKILL_PRUNE_THRESHOLD=0.3

# Phase 9
MAX_COMPLEXITY=20
MAX_SOLUTION_LINES=200
```

---

## Чек-лист перед стартом каждой фазы

1. Прочитать соответствующий план-файл полностью
2. Прочитать актуальный `CLAUDE.md` в корне проекта
3. Запустить `pytest tests/` — убедиться что все тесты зелёные ДО начала работы
4. Реализовать изменения пошагово (не все сразу)
5. После каждого модуля: `pytest tests/ -x`
6. Коммит только при зелёных тестах: `feat(phaseN): <описание>`
