# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Start the full Docker stack
docker compose up -d --build

# Check stack status
docker compose ps

# Launch a workflow manually (requires stack running)
.venv/bin/python main.py

# Smoke test the LLM adapter
.venv/bin/python scripts/test_llm.py --model opencode/bigpickle

# Run tests
pytest tests/

# Run a single test
pytest tests/test_decomposer_agent.py::test_normalize_task_contract_adds_standard_schema

# Stop the stack
docker compose down --remove-orphans
```

No Makefile or pyproject.toml exists. Dependencies are in `shared/requirements.txt`.

## Architecture

AI Factory is a **Temporal-based multi-agent software delivery pipeline**. It takes a project brief, creates a GitHub-backed project repo, runs LLM-powered specialist agents, and produces versioned code and documentation.

### Primary Pipeline (Temporal)

```
OrchestratorWorkflow
  → pm_activity          (plan + task breakdown)
  → architect_activity   (architecture docs + task list)
  → decomposer_activity  (split tasks exceeding token limit)
  → process_all_tasks    (per-task dev/QA loop)
      → dev_activity     (implement in task branch)
      → qa_activity      (validate + merge to main, or return feedback)
      → dev_activity     (fix cycle, up to DEV_QA_MAX_FIX_ATTEMPTS=2)
  → analyst_activity     (final project state)
  → pm_activity          (recovery cycle if tasks blocked, up to PM_MAX_RECOVERY_CYCLES=2)
```

The Kafka-based standalone agents (`agents/dispatcher/`, `shared/standalone_dispatcher.py`) are a legacy path and lag behind the Temporal implementation.

### Key Files

| File | Role |
|------|------|
| `orchestrator/workflows.py` | Temporal workflow + subworkflow definitions |
| `orchestrator/activities.py` | All activity implementations (PM, arch, dev, QA, analyst, git) |
| `orchestrator/worker.py` | Worker bootstrap |
| `shared/llm.py` | Provider-agnostic LLM adapter with fallback and cooldown |
| `shared/git.py` | Git repo init, branch, commit, merge, push helpers |
| `shared/prompts/loader.py` | Prompt template rendering |
| `shared/prompts/<role>/` | `system.txt` + `user.txt` per agent role |
| `main.py` | Local workflow launcher |

### Agent Roles

Each role has prompt templates in `shared/prompts/<role>/`. The Temporal activities in `orchestrator/activities.py` invoke these roles via `shared/llm.py`.

- **PM** – delivery plan, task assignments, recovery re-planning
- **Architect** – versioned architecture docs + task breakdown (`.md` and `.drawio`)
- **Decomposer** – splits tasks whose prompt exceeds `TASK_DECOMPOSITION_TOKEN_LIMIT` (default 8000)
- **Dev** – implements in a task branch; has `normal/` and `bugfix/` prompt variants
- **QA** – validates branch and either merges to main or returns structured feedback
- **Analyst** – records final state, risks, recommendations

### LLM Layer

`shared/llm.py` provides OpenAI-compatible chat completions with:
- Provider fallback order: `opencode → gemini → openai → deepseek → ollama`
- 12-hour cooldown after a provider returns 429
- Token estimation to enforce `LLM_MAX_PROMPT_TOKENS` (default 8000)
- Mock mode via `MOCK_LLM=true`

### State Persistence

Generated project repos land in `workspace/projects/<name>/`. Pipeline state is stored in `workspace/.ai_factory/`:
- `contexts/<workflow_id>/` – JSON context files per stage
- `tasks/` – task state JSON
- `continuations/` – continuation plans written when a task hits the 15-minute budget

### Task Contract

Every task passed between agents must conform to this schema (enforced by `decomposer/agent.py::normalize_task_contract`):

```json
{
  "task_id": "T001",
  "title": "Short descriptive title",
  "description": "Implementation-ready description",
  "type": "feature|bugfix|refactor|setup|test",
  "dependencies": [],
  "input": { "files": [], "context": "..." },
  "output": { "files": [], "artifacts": [], "expected_result": "..." },
  "verification": { "method": "pytest|manual|review", "test_file": null, "criteria": [] },
  "acceptance_criteria": [],
  "estimated_size": "small|medium|large",
  "can_parallelize": true
}
```

### Key Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `LLM_PROVIDER` | `opencode` | Primary LLM provider |
| `LLM_FALLBACK_ORDER` | `opencode,gemini,openai,deepseek,ollama` | Fallback chain |
| `LLM_MAX_PROMPT_TOKENS` | `8000` | Max tokens before decomposition |
| `MOCK_LLM` | `false` | Skip real LLM calls |
| `TEMPORAL_ADDRESS` | `temporal:7233` | Temporal server |
| `TASK_QUEUE` | `ai-factory-tasks` | Temporal task queue |
| `WORKFLOW_LLM_ACTIVITY_TIMEOUT_MINUTES` | `30` | Per-activity LLM timeout |
| `DEV_QA_MAX_FIX_ATTEMPTS` | `2` | Self-healing loop limit |
| `PM_MAX_RECOVERY_CYCLES` | `2` | PM re-planning limit |
| `MAX_TASK_EXECUTION_SECONDS` | `900` | Per-task budget before continuation plan |

### Resilience Mechanisms

- `Dev → QA → Dev` self-healing loop with structured QA feedback
- PM recovery cycles for blocked/failed tasks
- Non-retryable workflow failure on Python logic errors (`AttributeError`, `TypeError`)
- 15-minute task execution budget; continuation plan written on timeout

## Agent Design Principle

One agent = one responsibility. When adding a new capability, prefer a new agent over expanding an existing one. Keep tasks atomic and verifiable with explicit input/output contracts.
