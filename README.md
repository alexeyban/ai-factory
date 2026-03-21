# AI Factory

AI Factory is a Temporal-based multi-agent software delivery system. It takes a project brief, creates a project repository, writes versioned delivery documents, decomposes work into agent tasks, generates code, runs QA, records project state, and keeps artifacts committed into a Git-backed project workspace.

The current implementation is focused on a practical local pipeline: Docker services, Temporal orchestration, prompt-driven agent roles, OpenAI-compatible LLM access with provider fallback, and generated project repositories under `workspace/projects/`.

## Current Flow

The active end-to-end path runs through the Temporal worker in [orchestrator/](/home/legion/PycharmProjects/ai-factory/orchestrator):

1. `PM` captures the incoming request, creates a structured delivery plan, and writes agent assignments.
2. `Architect` produces a versioned architecture package and task breakdown.
3. `Dev` implements work in task branches.
4. `QA` validates task branches and merges approved work into `main`.
5. `Analyst` writes the current project state, risks, and recommendations.
6. `PM recovery` can re-plan blocked work and split or reassign tasks.

The repo still contains Kafka-oriented standalone agents, but the primary working path is the Temporal workflow.

## Main Components

- [main.py](/home/legion/PycharmProjects/ai-factory/main.py): simple local workflow launcher.
- [orchestrator/workflows.py](/home/legion/PycharmProjects/ai-factory/orchestrator/workflows.py): Temporal workflow definitions and failure handling.
- [orchestrator/activities.py](/home/legion/PycharmProjects/ai-factory/orchestrator/activities.py): PM, architect, dev, QA, analyst, recovery, continuation, git, and artifact logic.
- [orchestrator/worker.py](/home/legion/PycharmProjects/ai-factory/orchestrator/worker.py): Temporal worker bootstrap.
- [shared/llm.py](/home/legion/PycharmProjects/ai-factory/shared/llm.py): provider-agnostic LLM adapter with fallback, rate-limit awareness, and cooldown memory.
- [shared/prompts](/home/legion/PycharmProjects/ai-factory/shared/prompts): prompt definitions for PM, architect, dev, QA, and analyst roles.
- [shared/git.py](/home/legion/PycharmProjects/ai-factory/shared/git.py): generated project git initialization, remote setup, branch, commit, merge, and push helpers.
- [docker-compose.yml](/home/legion/PycharmProjects/ai-factory/docker-compose.yml): local stack definition.

## Generated Project Behavior

Each generated project is created under [workspace/projects](/home/legion/PycharmProjects/ai-factory/workspace/projects). The pipeline currently:

- initializes a git repository
- connects it to a GitHub remote such as `git@github.com:alexeyban/<project>.git`
- writes versioned PM documents, architecture documents, QA reports, analyst reports, plans, and continuation notes
- commits and pushes those artifacts as the workflow progresses
- uses task branches for implementation work and merges validated changes back to `main`

For architecture specifically, each iteration creates versioned `.md` and `.drawio` files.

## Agent Roles

- `PM`: senior technical project manager who plans delivery, documents tasks for all agents, checks actual completion, and re-plans blocked or oversized work.
- `Architect`: senior solution architect covering backend, frontend, data, AI/ML, infrastructure, messaging, security, and observability.
- `Dev`: senior Python developer and senior data scientist focused on production Python, ML, and AI implementation.
- `QA`: senior automation QA engineer responsible for impartial unit, integration, and end-to-end validation.
- `Analyst`: senior system analyst and data analyst who summarizes delivery state, issues, patterns, risks, and recommendations.

## Recovery And Continuation

The orchestration layer includes several resilience mechanisms:

- `Dev -> QA -> Dev` self-healing loop with structured QA feedback
- PM-assisted recovery cycles for blocked or failed tasks
- persisted task state in `.ai_factory/tasks`
- continuation plans in `.ai_factory/continuations`
- a 15-minute execution budget per individual task before continuation planning is written
- non-retryable workflow failure on internal Python logic bugs such as `AttributeError` or `TypeError`

## LLM Layer

The shared LLM adapter in [shared/llm.py](/home/legion/PycharmProjects/ai-factory/shared/llm.py) currently supports:

- OpenAI-compatible chat completion calls
- provider-specific model normalization
- mock mode
- fallback across providers
- Gemini local rate limiting
- 12-hour provider cooldown memory after provider-side rate-limit failures

Typical provider order is:

1. `opencode`
2. `gemini`
3. `openai`
4. `deepseek`
5. `ollama`

When a provider returns `429`, it is marked on cooldown and skipped for subsequent requests until the cooldown expires.

Relevant environment variables include:

- `MOCK_LLM`
- `LLM_PROVIDER`
- `LLM_MODEL`
- `LLM_FALLBACK_ORDER`
- `LLM_PROVIDER_COOLDOWN_SECONDS`
- `OPENCODE_API_KEY`
- `GEMINI_API_KEY`
- `OPENAI_API_KEY`
- `DEEPSEEK_API_KEY`
- `OLLAMA_API_KEY` or `OLLANA_API_KEY`

## Local Stack

The default Docker stack includes:

- Zookeeper
- Kafka
- Schema Registry
- Postgres
- Temporal
- Temporal Web UI
- Orchestrator worker
- PM agent container

Temporal Web UI:

- `http://localhost:8088`

## Running Locally

Start the stack:

```bash
docker compose up -d --build
```

Check status:

```bash
docker compose ps
```

Launch a local workflow manually:

```bash
.venv/bin/python main.py
```

Smoke test the LLM adapter:

```bash
.venv/bin/python scripts/test_llm.py --model opencode/bigpickle
```

Stop the stack:

```bash
docker compose down --remove-orphans
```

## Current Limitations

- The Kafka standalone agent path still lags behind the Temporal-first path.
- Real LLM execution still depends on external provider quotas and availability.
- Generated project delivery is functional but still experimental and not yet a production-grade autonomous software platform.
- The generated code path is still inconsistent in how deeply it materializes full multi-file implementations for large projects.

## Purpose

AI Factory is a practical prototype for autonomous, Git-backed software delivery:

- prompt-defined specialist agents
- Temporal orchestration
- resumable task execution
- versioned documentation
- LLM-backed planning and generation
- QA-driven correction loops
- GitHub-oriented project delivery
