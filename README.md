# AI Factory

AI Factory is a Temporal-based multi-agent software delivery system. It takes a project brief, creates a project repository, writes versioned delivery documents, decomposes work into agent tasks, generates code, runs QA, records project state, and keeps artifacts committed into a Git-backed project workspace.

## Workflows

### OrchestratorWorkflow (primary delivery pipeline)

```
OrchestratorWorkflow
  → pm_activity          (delivery plan, task breakdown, agent assignments)
  → architect_activity   (architecture docs + task list with assigned_agent)
  → decomposer_activity  (split tasks exceeding token limit into subtasks)
  → process_all_tasks    (wave-based dispatch with dependency ordering)
      → dev_activity     (implement in task branch; multi-file output supported)
      → qa_activity      (lint + typecheck + pytest + LLM summary; merge to main)
      → dev_activity     (self-healing fix cycle, up to DEV_QA_MAX_FIX_ATTEMPTS=2)
  → analyst_activity     (final project state, risks, recommendations)
  → pm_activity          (recovery re-planning if tasks blocked, up to PM_MAX_RECOVERY_CYCLES=2)
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
| `orchestrator/workflows.py` | Temporal workflow + subworkflow definitions |
| `orchestrator/activities.py` | All activity implementations |
| `orchestrator/worker.py` | Temporal worker bootstrap |
| `shared/llm.py` | Provider-agnostic LLM adapter with fallback and cooldown |
| `shared/git.py` | Git repo init, branch, commit, merge, push, GitHub PR helpers |
| `shared/tools.py` | Local deterministic skills: syntax, lint, typecheck, pytest, file tree |
| `shared/prompts/<role>/` | `system.txt` + `user.txt` per agent role |
| `scripts/debug_*.py` | Isolation test runners for each agent (no Temporal needed) |

## Agent Roles

Each role has prompt templates in `shared/prompts/<role>/`.

- **PM** — delivery plan, task assignments, agent guidance, recovery re-planning
- **Architect** — versioned architecture docs (`.md` + `.drawio`) and task breakdown with `assigned_agent`
- **Decomposer** — splits tasks whose prompt exceeds `TASK_DECOMPOSITION_TOKEN_LIMIT` (default 8000 tokens)
- **Dev** — implements in a task branch; outputs one or more files using `=== FILE: path ===` format
- **QA** — runs syntax check, lint (ruff), typecheck (mypy), pytest+coverage, LLM summary; merges to main on pass
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
| `LLM_MODEL` | `opencode/bigpickle` | Primary model |
| `LLM_FALLBACK_ORDER` | `opencode,gemini,openai,deepseek,ollama` | Fallback chain |
| `LLM_MAX_PROMPT_TOKENS` | `8000` | Token limit before decomposition |
| `LLM_PROVIDER_COOLDOWN_SECONDS` | `15` | Cooldown after 429 |
| `MOCK_LLM` | `false` | Skip real LLM calls |
| `OLLAMA_MODEL` | `llama3:latest` | Local Ollama model |
| `TEMPORAL_ADDRESS` | `temporal:7233` | Temporal server |
| `TASK_QUEUE` | `ai-factory-tasks` | Temporal task queue |
| `WORKFLOW_LLM_ACTIVITY_TIMEOUT_MINUTES` | `30` | Per-activity LLM timeout |
| `DEV_QA_MAX_FIX_ATTEMPTS` | `2` | Self-healing loop limit |
| `PM_MAX_RECOVERY_CYCLES` | `2` | PM re-planning limit |
| `MAX_TASK_EXECUTION_SECONDS` | `900` | Per-task budget |
| `MAX_WAVE_SIZE` | `20` | Max concurrent tasks per wave |
| `INTER_WAVE_RATE_LIMIT_DELAY_SECONDS` | `30` | Delay between waves after 429 |
| `PROJECTS_ROOT` | `/workspace/projects` | Generated project repos |
| `WORKSPACE_ROOT` | `/workspace` | Pipeline workspace root |
| `AI_FACTORY_ROOT` | `/workspace/.ai_factory` | Contexts, tasks, continuations |

## Isolation Debug Scripts

Each agent can be tested independently without running the full Docker stack:

```bash
# PM agent
PYTHONPATH=. LLM_MODEL=opencode/bigpickle .venv/bin/python scripts/debug_pm.py

# Architect (feed it PM output)
PYTHONPATH=. LLM_MODEL=minimax/MiniMax-M2.5-Free \
    PM_OUTPUT=/tmp/debug_pm_<id>.json \
    .venv/bin/python scripts/debug_architect.py

# Decomposer (feed it architect output)
PYTHONPATH=. LLM_MODEL=minimax/MiniMax-M2.5-Free \
    ARCHITECT_OUTPUT=/tmp/debug_architect_<id>.json \
    .venv/bin/python scripts/debug_decomposer.py

# Dev agent (single task)
PYTHONPATH=. LLM_MODEL=minimax/MiniMax-M2.5-Free \
    .venv/bin/python scripts/debug_dev.py

# QA agent (single artifact)
PYTHONPATH=. LLM_MODEL=minimax/MiniMax-M2.5-Free \
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

## Pipeline Status (2026-03-24)

All major failure modes are fixed. Isolation tests pass for PM, Architect, Decomposer, and Dev.

| Component | Status |
|-----------|--------|
| PM activity | ✓ Tested in isolation |
| Architect activity | ✓ Tested — `assigned_agent` correctly populated |
| Decomposer activity | ✓ Tested — all subtasks have type + title + assigned_agent |
| Dev activity | ✓ Tested — multi-file output to correct target paths, QA passes |
| QA activity | Script ready (`debug_qa.py`), full run pending |
| Analyst activity | Not yet tested in isolation |
| Full e2e pipeline | Not yet re-validated after recent fixes |

Known limitations:
- opencode/MiniMax free tier is consistently rate-limited during testing; falls back to OpenAI automatically
- GitHub PR auto-merge requires branch protection to be configured with auto-merge enabled on the repo
- The Kafka standalone agent path lags behind the Temporal implementation

## Purpose

AI Factory is a practical prototype for autonomous, Git-backed software delivery:
- prompt-defined specialist agents
- Temporal orchestration with wave-based task dispatch
- resumable task execution with self-healing loops
- versioned documentation committed to the project repo
- LLM-backed planning and multi-file code generation
- QA-driven correction loops
- GitHub-oriented project delivery
