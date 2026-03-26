# AI Factory

AI Factory is a Temporal-based multi-agent software delivery system. It takes a project brief, creates a project repository, writes versioned delivery documents, decomposes work into agent tasks, generates code, runs QA, records project state, and keeps artifacts committed into a Git-backed project workspace.

## Workflows

### OrchestratorWorkflow (primary delivery pipeline)

```
OrchestratorWorkflow
  → pm_activity          (delivery plan, task breakdown, agent assignments)
      ↳ uses lightweight design briefing (≤600 words) instead of full Architect JSON
      ↳ architect_guidance carries cross-cutting standards for ALL dev tasks
  → architect_activity   (architecture docs + task list with assigned_agent)
      ↳ architect_guidance from PM is embedded into every dev task's input.context
  → decomposer_activity  (split tasks exceeding token limit into subtasks)
  → process_all_tasks    (wave-based dispatch with dependency ordering)
      → dev_activity     (implement in task branch; multi-file output supported)
      → qa_activity      (lint + typecheck + pytest + LLM summary; merge to main)
                          QA receives attempt_number — detects if fix_suggestion was applied
      → dev_activity     (self-healing fix cycle, up to DEV_QA_MAX_FIX_ATTEMPTS=2)
  → analyst_activity     (final project state, risks, recommendations)
      ↳ receives PM project_goal, delivery_summary, analyst_guidance as context
  → pm_activity          (recovery re-planning if tasks blocked, up to PM_MAX_RECOVERY_CYCLES=2)
      ↳ recovery failure_summary includes qa_root_cause + qa_fix_suggestion per failed task
      ↳ recovery carries architect_guidance + delivery_summary from original PM plan
  → cleanup_stale_branches_activity (delete merged task-* branches)
```

### LearningWorkflow (AlphaZero-style self-play — Phase 5)

```
LearningWorkflow  (per task, N iterations)
  for iteration in range(max_iterations):
      → dev_activity           (generate candidate(s) with epsilon-greedy strategy)
      → qa_activity            (validate + compute reward via RewardEngine)
      → extract_skill_activity (on improvement + QA pass → extract reusable skill)
      [stop on stagnation or perfect score]
  → policy_update_activity     (update skill weights, prompt examples, exploration rate)
```

The repo still contains Kafka-oriented standalone agents under `agents/dispatcher/`, but the primary working path is the Temporal workflow.

## Main Components

| File | Role |
|------|------|
| `main.py` | Local workflow launcher |
| `orchestrator/workflows.py` | Temporal workflow definitions (Orchestrator, Project, Learning) |
| `orchestrator/activities.py` | All activity implementations |
| `orchestrator/worker.py` | Temporal worker bootstrap |
| `orchestrator/code_composer.py` | AST-based skill + new code combiner (Phase 3) |
| `shared/llm.py` | Provider-agnostic LLM adapter with fallback and cooldown |
| `shared/git.py` | Git repo init, branch, commit, merge, push, GitHub PR helpers |
| `shared/tools.py` | Deterministic tools: syntax, lint, typecheck, pytest+coverage, junit XML |
| `shared/prompts/<role>/` | `system.txt` + `user.txt` per agent role |
| `memory/db.py` | asyncpg PostgreSQL client (MemoryDB) |
| `memory/episodic.py` | Episode + solution storage and best-solution lookup |
| `memory/failures.py` | Failure pattern accumulation and prompt formatting |
| `memory/vector_store.py` | Qdrant-backed vector memory for skill/episode similarity |
| `memory/reward.py` | RewardEngine: correctness × w_c + perf × w_p − complexity × w_x |
| `memory/replay_buffer.py` | Fixed-capacity good/bad solution buffer with JSON persistence |
| `memory/policy_updater.py` | Skill weights, prompt examples, exploration rate adaptation |
| `memory/skill.py` | Skill dataclass |
| `memory/skill_extractor.py` | LLM-driven skill extraction → skills/ + PostgreSQL + Qdrant |
| `memory/skill_retriever.py` | Top-K skill retrieval ranked by similarity × 0.6 + success_rate × 0.4 |
| `memory/skill_executor.py` | Subprocess sandbox for executing skills |
| `skills/__init__.py` | SkillRegistry: local registry.json cache |
| `scripts/debug_*.py` | Isolation test runners for each agent (no Temporal needed) |
| `shared/tracing.py` | OpenTelemetry tracing with no-op fallback when SDK absent |
| `memory/worker.py` | Kafka consumer + Prometheus metrics endpoint (port 9091) |
| `memory/reward_worker.py` | Kafka consumer on qa.results → RewardEngine → reward.computed |
| `orchestrator/meta_agent_worker.py` | Episode tracker; triggers SkillOptimizer every N episodes |
| `benchmarks/dataset_loader.py` | Loads easy/medium/hard/expert benchmark task datasets |
| `benchmarks/curriculum.py` | Curriculum state machine; promotes on 80% success rate |
| `benchmarks/metrics_exporter.py` | Prometheus metrics wrapper for benchmark pipeline |
| `infra/prometheus.yml` | Prometheus scrape config targeting memory-worker |
| `infra/grafana/` | Grafana dashboards + datasource provisioning |
| `infra/otel-collector.yml` | OpenTelemetry collector: OTLP gRPC/HTTP → logging exporter |
| `infra/kafka_topics.sh` | Creates all 6 Kafka topics with retention policies |
| `infra/dockerfiles/` | Dockerfiles for memory-worker, reward-worker, meta-agent |

## Agent Roles

Each role has prompt templates in `shared/prompts/<role>/`.

- **PM** — delivery plan, task assignments, agent guidance, recovery re-planning
- **Architect** — versioned architecture docs (`.md` + `.drawio`) and task breakdown with `assigned_agent`
- **Decomposer** — splits tasks whose prompt exceeds `TASK_DECOMPOSITION_TOKEN_LIMIT` (default 8000 tokens)
- **Dev** — implements in a task branch; outputs one or more files using `=== FILE: path ===` format
- **QA** — runs syntax check (Python-only; non-.py files skipped), lint (ruff), typecheck (mypy), pytest+coverage, LLM summary; merges to main on pass
- **Analyst** — records final state, risks, recommendations

## Task Contract

Every task passed between agents must conform to this schema:

```json
{
  "task_id": "T001",
  "title": "Short descriptive title",
  "description": "Implementation-ready description",
  "type": "feature|bugfix|refactor|setup|test",
  "assigned_agent": "dev|qa|architect|pm",
  "dependencies": [],
  "input":  { "files": [], "context": "..." },
  "output": { "files": ["path/to/target.py"], "artifacts": [], "expected_result": "..." },
  "verification": { "method": "pytest|manual|review", "test_file": null, "criteria": [] },
  "acceptance_criteria": [],
  "estimated_size": "small|medium|large",
  "can_parallelize": true
}
```

The dev agent writes the files listed in `output.files`. When multiple files are needed, the LLM uses `=== FILE: path ===` separators in its response.

## Dev Agent: Multi-File Output

The dev agent supports writing multiple files per task:

```
=== FILE: calclib/calc.py ===
<complete file content>

=== FILE: tests/test_calc.py ===
<complete file content>
```

File paths come from the task contract's `output.files` list. If the LLM returns a single block with no headers, it is written to `output.files[0]` (or a slug-derived fallback).

## LLM Layer

`shared/llm.py` provides OpenAI-compatible chat completions with:
- Provider fallback: `opencode → gemini → openai → deepseek → ollama`
- 15-second cooldown after a provider returns 429
- Token estimation to enforce `LLM_MAX_PROMPT_TOKENS` (default 8000)
- MiniMax M2.5 Free aliases for opencode provider
- Mock mode via `MOCK_LLM=true`

## Key Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `LLM_PROVIDER` | `opencode` | Primary LLM provider |
| `LLM_MODEL` | `glm-5` | Primary model (OpenCode GLM-5) |
| `LLM_FALLBACK_ORDER` | `opencode,gemini,openai,deepseek,ollama` | Fallback chain |
| `OPENCODE_MODEL` | `glm-5` | OpenCode provider model |
| `LLM_MAX_PROMPT_TOKENS` | `8000` | Token limit before decomposition |
| `LLM_PROVIDER_COOLDOWN_SECONDS` | `15` | Cooldown after 429 |
| `MOCK_LLM` | `false` | Skip real LLM calls |
| `TEMPORAL_ADDRESS` | `temporal:7233` | Temporal server |
| `TASK_QUEUE` | `ai-factory-tasks` | Temporal task queue |
| `WORKFLOW_LLM_ACTIVITY_TIMEOUT_MINUTES` | `30` | Per-activity LLM timeout |
| `DEV_QA_MAX_FIX_ATTEMPTS` | `2` | Self-healing loop limit |
| `PM_MAX_RECOVERY_CYCLES` | `2` | PM re-planning limit |
| `MAX_WAVE_SIZE` | `20` | Max concurrent tasks per wave |
| `QDRANT_URL` | `http://localhost:6333` | Qdrant vector DB |
| `MEMORY_DB_URL` | `postgresql://...` | PostgreSQL for memory tables |
| `MAX_ITERATIONS` | `5` | LearningWorkflow iterations per task |
| `STAGNATION_THRESHOLD` | `3` | Non-improving iterations before stop |
| `NUM_CANDIDATES` | `1` | Dev candidates per iteration |
| `EXPLORATION_RATE` | `0.3` | Epsilon-greedy exploration fraction |
| `REWARD_CORRECTNESS_W` | `1.0` | Correctness weight in reward formula |
| `REWARD_PERF_W` | `0.3` | Performance weight in reward formula |
| `REWARD_COMPLEXITY_W` | `0.2` | Complexity penalty weight |

## Isolation Debug Scripts

Each agent can be tested independently without running the full Docker stack:

```bash
# PM agent
PYTHONPATH=. LLM_MODEL=glm-5 .venv/bin/python scripts/debug_pm.py

# Architect (feed it PM output)
PYTHONPATH=. LLM_MODEL=glm-5 \
    PM_OUTPUT=/tmp/debug_pm_<id>.json \
    .venv/bin/python scripts/debug_architect.py

# Decomposer (feed it architect output)
PYTHONPATH=. LLM_MODEL=glm-5 \
    ARCHITECT_OUTPUT=/tmp/debug_architect_<id>.json \
    .venv/bin/python scripts/debug_decomposer.py

# Dev agent (single task)
PYTHONPATH=. LLM_MODEL=glm-5 \
    .venv/bin/python scripts/debug_dev.py

# QA agent (single artifact)
PYTHONPATH=. LLM_MODEL=glm-5 \
    ARTIFACT=/path/to/artifact.py \
    .venv/bin/python scripts/debug_qa.py
```

All debug scripts default to `/tmp/ai-factory-debug/` as workspace and the `calclib` GitHub repo as the target project. Override with env vars — see the docstring at the top of each script.

## Running the Full Stack

```bash
# Start services
docker compose up -d --build

# Check status
docker compose ps

# Launch a workflow
.venv/bin/python main.py

# Smoke test LLM adapter
.venv/bin/python scripts/test_llm.py --model opencode/bigpickle

# Run tests
pytest tests/

# Stop
docker compose down --remove-orphans
```

Temporal Web UI: `http://localhost:8088`

## State Persistence

| Location | Contents |
|----------|----------|
| `workspace/projects/<name>/` | Generated project repo |
| `workspace/.ai_factory/contexts/<workflow_id>/` | JSON context files per pipeline stage |
| `workspace/.ai_factory/tasks/` | Per-task state JSON (in-progress, success, fail) |
| `workspace/.ai_factory/continuations/` | Continuation plans written on timeout |
| `workspace/.ai_factory/replay_buffer.json` | Good/bad solution buffer (Phase 5) |
| `workspace/.ai_factory/policy_state.json` | Exploration rate + reward rolling average (Phase 5) |
| `skills/` | Extracted skill `.py` files + `registry.json` |

## Self-Learning Stack (Phases 0–9)

```
Phase 0  Episode tracking (episode_id, log_episode_event)
Phase 1  Memory layer: PostgreSQL + Qdrant (episodes, solutions, skills, failures)
Phase 2  Skill Engine: extract → store → retrieve → inject into dev prompt
Phase 3  Dev evolution: multi-candidate generation, CodeComposer, epsilon-greedy
Phase 4  QA + Reward: RewardEngine, junit XML, regression detection, Kafka events
Phase 5  Learning Loop: LearningWorkflow, ReplayBuffer, PolicyUpdater
Phase 6  Benchmarking datasets: easy/medium/hard/expert tasks, Curriculum, MetricsExporter
Phase 7  Benchmarking pipeline: DatasetLoader, Curriculum state machine, Prometheus metrics
Phase 8  Production infra: memory-worker, reward-worker, meta-agent, OTel tracing, Grafana dashboards
Phase 9  Agent interaction quality: PM design briefing, architect_guidance propagation,
         analyst PM-context, recovery QA root-cause, prompt label fixes, QA attempt tracking
```

**Test suite: 303 tests, all passing** (`PYTHONPATH=. pytest tests/`)

## Pipeline Status (2026-03-25)

| Component | Status |
|-----------|--------|
| PM activity | ✓ Lightweight design briefing, cross-cutting standards in architect_guidance |
| Architect activity | ✓ architect_guidance propagated to every dev task's input.context |
| Decomposer activity | ✓ All subtasks have type + title + assigned_agent |
| Dev activity | ✓ Multi-file output, multi-candidate, skill-aware prompting |
| QA activity | ✓ Reward, regression detection, Kafka publishing, attempt_number tracking |
| Analyst activity | ✓ Receives PM project_goal, delivery_summary, analyst_guidance |
| Recovery cycle | ✓ failure_summary includes qa_root_cause + qa_fix_suggestion per task |
| LearningWorkflow | ✓ Stagnation detection, perfect-score stop, policy update |
| Memory / Skill Engine | ✓ PostgreSQL + Qdrant backed, full test coverage |
| Benchmarking | ✓ easy/medium/hard/expert datasets, Curriculum, MetricsExporter |
| Production infra | ✓ Dockerfiles, OTel tracing, Prometheus/Grafana, Kafka topics script |
| Phase 9 stability | ✓ compute_code_hash, set_global_seed, loop protection tests |
| Full e2e pipeline | Not yet re-validated end-to-end |

## Purpose

AI Factory is a practical prototype for autonomous, self-improving software delivery:
- prompt-defined specialist agents (PM, Architect, Decomposer, Dev, QA, Analyst)
- Temporal orchestration with wave-based task dispatch and self-healing loops
- AlphaZero-style iterative learning: dev → qa → reward → skill extraction → policy update
- Episodic memory, skill accumulation, and replay buffer for policy improvement
- LLM-backed planning, multi-file code generation, and GitHub-oriented delivery
