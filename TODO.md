# TODO — AI Factory Next Steps

## Fixed (recent)

### ~~PM / architect / decomposer return `status: null`~~ ✓ Fixed
Result dicts for `pm_activity`, `architect_activity`, and `decomposer_activity` lacked a
`"status"` key. `_wrap_activity_result` reads `result.get("status")` which returned `None`.
**Fix**: Added `"status": "success"` to all three result dicts.

### ~~Decomposer receives entire PM execution plan as raw text~~ ✓ Fixed
`architect_request["description"]` included the full `execution_plan` list (30+ task objects),
producing payloads orders of magnitude over the token limit.
**Fix**: Replaced full plan with title-only summary; capped `project_description` in architect
task contexts to 800 chars.

### ~~Task cache returning stale "success" across workflow runs~~ ✓ Fixed
`_execute_task_impl` returned cached success without checking `workflow_id`.
**Fix**: Added `previous_state.get("workflow_id") == workflow_id` guard.

### ~~Concurrent `git config` returning exit 255~~ ✓ Fixed
Wave 1 tasks race on concurrent writes to `.git/config`.
**Fix**: `git config` calls changed to `check=False` (idempotent).

### ~~PM Agent produces 0 tasks~~ ✓ Fixed
Architect LLM response (~42k chars) overflowed `LLM_MAX_PROMPT_TOKENS=8000`.
**Fix**: Truncated `architect_notes` and `analyst_notes` to 4000 chars each.

### ~~Temporal Deadlock errors (`[TMPRL1101]`)~~ ✓ Fixed
Oversized recovery description strings caused deadlock.
**Fix**: Compact `json.dumps` for recovery data; added `await asyncio.sleep(0)` yields.

### ~~GitHub PR merge fails with branch protection~~ ✓ Fixed
`create_and_merge_github_pr` now detects HTTP 405/422 and enables auto-merge via
GitHub GraphQL API (`enablePullRequestAutoMerge`) as fallback.

### ~~Large project wave size vs. rate limits~~ ✓ Fixed
Added `MAX_WAVE_SIZE` (default 20) and `INTER_WAVE_RATE_LIMIT_DELAY_SECONDS` (default 30s).

### ~~Dev agent writes to wrong file~~ ✓ Fixed (2026-03-24)
Dev agent created `{package}/{task_slug}.py` instead of modifying the actual target files.
**Fix**: `_task_module_path` now uses `output.files[0]` from the task contract. Dev prompt
updated with `target_files` list. LLM can return multiple files using `=== FILE: path ===`
separators; `_parse_multi_file_output()` extracts and writes each file.

### ~~`assigned_agent` missing from architect tasks~~ ✓ Fixed (2026-03-24)
Architect prompt schema did not include `assigned_agent`; all tasks showed `[type/?]`.
**Fix**: Added `assigned_agent` to architect user prompt schema; `_normalize_task` defaults
to `"dev"`.

### ~~`python` venv command not found~~ ✓ Fixed (2026-03-24)
Hardcoded `"python"` for venv creation fails on Linux (only `python3` available).
**Fix**: Changed to `sys.executable`.

### ~~Ollama fallback fails with model-not-found~~ ✓ Fixed (2026-03-24)
`.env` had `OLLAMA_MODEL=llama4:scout` but only `llama3:latest` is installed.
**Fix**: Updated `.env` to `OLLAMA_MODEL=llama3:latest`.

### ~~Phase 1 — Memory Layer~~ ✓ Done (2026-03-24)
PostgreSQL DDL (episodes, solutions, rewards, skills, failures), asyncpg MemoryDB client,
EpisodicMemory, FailureMemory, VectorMemory (Qdrant). 55 tests.

### ~~Phase 2 — Skill Engine~~ ✓ Done (2026-03-24)
Skill dataclass, SkillRegistry (registry.json), SkillExtractor (LLM → skills/*.py + DB + Qdrant),
SkillRetriever (similarity × 0.6 + success_rate × 0.4), SkillExecutor (subprocess sandbox). 44 tests.

### ~~Phase 3 — Dev Agent Evolution~~ ✓ Done (2026-03-24)
Multi-candidate generation, CodeComposer (AST-based import dedup + function merging),
epsilon-greedy explore/exploit strategies, skill-aware dev prompts, failure patterns injection. 25 tests.

### ~~Phase 4 — QA + Reward System~~ ✓ Done (2026-03-24)
RewardEngine (correctness × w_c + perf × w_p − complexity × w_x), cyclomatic complexity via AST,
junit XML parsing (`parse_junit_xml`), regression detection (`check_regression`),
Kafka publishing to `qa.results` + `reward.computed` topics. 29 tests.

### ~~Phase 5 — Learning Loop~~ ✓ Done (2026-03-24)
`LearningWorkflow` (AlphaZero-style): N iterations dev→qa→reward with stagnation detection
and perfect-score early stop. `ReplayBuffer` (good/bad deques, JSON persistence).
`PolicyUpdater` (skill weights, prompt examples, adaptive epsilon decay).
`extract_skill_activity` + `policy_update_activity` registered in worker. 38 tests.

---

## High Priority

### ~~End-to-end verification of a full workflow run~~ ✓ Done (2026-03-26)
Calclib workflow ran successfully: 5 waves, 7/8 tasks passed. PM recovery cycle triggered for T005.
Self-healing loop activated on T004 and T008. Dev agent writes to correct target files confirmed.
- [x] Run `scripts/run_e2e_test.py` against `https://github.com/alexeyban/calclib`
- [x] Confirm PM → architect → decomposer → dev → QA → analyst pipeline executes
- [x] GITHUB_TOKEN for PR auto-merge — added to `.env` via `gh auth token` (2026-03-28)
- [x] Confirm dev agent writes to correct target files

### ~~Temporal nondeterminism `[TMPRL1100]` on recovery cycle~~ ✓ Fixed (2026-03-28)
`architect_activity` hardcoded stage `"architect"` for both main and recovery calls. Recovery architect
overwrote main architect's context file; `_load_result_from_file` read stale data on replay → different
task list → nondeterminism.
**Fix**: `architect_activity` now uses `f"architect_recovery_{recovery_cycle}"` stage when
`task.get("recovery_cycle")` is set, keeping main architect file (`output_architect.json`) immutable.

### ~~T005 zero-byte test file bug~~ ✓ Fixed (2026-03-27)
`_parse_multi_file_output` stripped trailing code fences but not opening ` ```python\n ` fences.
Empty code blocks (LLM truncation) were silently written as 0-byte files.
**Fix**: `orchestrator/activities.py` — strip opening+closing fences inline via regex; skip entries
with empty content after stripping (log warning). 11 regression tests in `tests/test_parse_multi_file_output.py`.

### ~~Security audit fixes~~ ✓ Done (2026-03-30)
Full security audit performed; 4 issues found and fixed:
- **Path traversal**: `_parse_multi_file_output` rejects `..`, absolute paths, shell-special chars; write sites add `.resolve()` bounds check. 4 regression tests added.
- **SSH MITM**: `shared/git.py` pre-populates `~/.ssh/known_hosts` with GitHub's real public keys; all containers use `StrictHostKeyChecking=yes`.
- **GitHub token in HTTPS URLs**: `push_branch()` HTTPS fallback now injects token via `GIT_CONFIG_*` env var as `Authorization: Basic` header — token never appears in remote URL, error messages, or process listings.
- **Grafana password**: parameterized via `GRAFANA_ADMIN_PASSWORD` env var (was hardcoded `admin`).

### ~~QA pipeline systemic failures (reversi-alpha-zero run)~~ ✓ Fixed (2026-03-30)
Analysis of all 10 failed tasks found 6 root-cause bugs:
- **Syntax check on non-.py files**: `shared/tools.py` `syntax_check()` now returns `ok=True` immediately for non-`.py` files. Test: `test_syntax_check_skips_non_py_files`.
- **pip install crash**: `_install_project_dependencies()` changed from `check=True` → `check=False`; logs warning and continues so pytest can report useful `ModuleNotFoundError`.
- **Dev artifact drift**: dev prompt `user.txt` now has ARTIFACT LOCK (primary file type immutable across fix attempts).
- **Missing deps in requirements.txt**: DEPENDENCY RULE added to dev prompt (write `requirements.txt` alongside implementation when using external libs).
- **LLM truncation**: COMPLETENESS REQUIRED added to dev prompt (all brackets must be closed; no mid-function truncation).
- **Test file bugs unresolvable**: TEST FILE FIXES section added to dev prompt (dev may also write fixed test file if QA explicitly reports a test bug).

### ~~Claude Code CLI as LLM provider~~ ✓ Done (2026-03-30)
`shared/llm.py`: added `claude` provider using `claude -p ... --output-format json --no-session-persistence`.
Works with Claude Code subscription credentials from `~/.claude/` — no `ANTHROPIC_API_KEY` needed.
`orchestrator/Dockerfile`: Claude CLI installed; `build-essential` + `librdkafka-dev` added for confluent-kafka build.
`docker-compose.yml`: `~/.claude:/root/.claude:ro` volume-mounted in all 6 worker containers.
`401` added to `FALLBACK_STATUS_CODES` so opencode `CreditsError` triggers fallback to next provider.

### ~~Temporal sandbox error on worker startup~~ ✓ Fixed (2026-03-30)
`orchestrator/workflows.py`: decomposer agent imports were at module level, transitively pulling in `httpx` → `urllib.request.Request` inside Temporal's sandbox → `RestrictedWorkflowAccessError`.
**Fix**: wrapped decomposer imports in `with workflow.unsafe.imports_passed_through():`.

### QA isolation test completion
`scripts/debug_qa.py` was created but not yet successfully run end-to-end.
- [ ] Run `debug_qa.py` against calclib artifact
- [ ] Confirm lint, typecheck, pytest, LLM summary all produce expected output

### Analyst isolation test
No isolation test script exists for the analyst yet.
- [ ] Create `scripts/debug_analyst.py` following the same pattern as other debug scripts
- [ ] Feed it results from a `debug_dev` run and verify the analyst report output

### Dev self-healing loop regression test
The multi-file dev output change touched the core dev loop. Add a unit test to catch regressions.
- [ ] Test that `_parse_multi_file_output` correctly handles: single file, multi-file, no header
- [ ] Test that `_task_module_path` prefers `output.files[0]` over slug fallback
- [ ] Add a mock-LLM integration test for the full dev → QA loop

---

## Next Phases


### ~~Phase 6 — Self-Modification~~ ✓ Done (2026-03-24)
See `plans/phase6_self_modification.md`. SkillOptimizer, meta-agent-worker, skill rewrite/merge/prune.

### ~~Phase 7 — Benchmarking~~ ✓ Done (2026-03-24)
See `plans/phase7_benchmarking.md`. DatasetLoader, Curriculum state machine, MetricsExporter.

### ~~Phase 8 — Infrastructure~~ ✓ Code complete (2026-03-24)
See `plans/phase8_infra.md`. Dockerfiles, OTel tracing, Prometheus/Grafana, Kafka topics script.
Runtime (`docker compose up`) not fully validated end-to-end.
- [x] Add `asyncpg` to orchestrator Dockerfile — switched to `requirements.txt` install (2026-03-27)

### ~~Phase 9 — Anti-Patterns / Stability~~ ✓ Done (2026-03-26)
See `plans/phase9_stability.md`. `compute_code_hash`, `set_global_seed`, loop protection tests (303/303).
- [x] Hidden tests in `qa_activity` (Phase 9 Step 3): `_run_hidden_tests()` implemented, wired into `qa_activity` and `_build_dev_prompt` (2026-03-28)

### Option B — Dev/QA loop at workflow level (deferred)
Move dev→QA self-healing loop from inside `_execute_task_impl()` to workflow-level code.
Benefit: dev and QA each appear as separate Temporal activities in the UI (currently invisible).
This mirrors `LearningWorkflow` pattern. Cost: significant refactor of `_dispatch_tasks` + `process_single_task`.
- [ ] Design the refactor — see plan notes for approach

---

## Features

### ~~Local skills pre-validation~~ ✓ Done
`shared/tools.py`: syntax, file tree, import map, lint, typecheck, pytest+coverage, error history.
Wired into dev prompt (`existing_code`) and QA pipeline.

### ~~Branch cleanup for interrupted workflows~~ ✓ Done
`cleanup_stale_branches_activity` added; called at end of `OrchestratorWorkflow`.

### ~~Task resumability when PM returns 0 tasks~~ ✓ Done
PM retry loop (up to 2×) in both `OrchestratorWorkflow` and `ProjectWorkflow`.

### Multi-file dev output for complex tasks
Dev agent now supports `=== FILE: path ===` headers for writing multiple files per task.
- [ ] Monitor real pipeline runs to confirm LLMs reliably use the format
- [ ] Add fallback heuristic: if LLM ignores headers, detect changed files via git diff

### GitHub Actions CI integration
After QA merges to `main`, trigger a GitHub Actions workflow and wait for result.
- [ ] Poll `GET /repos/{owner}/{repo}/actions/runs` after push
- [ ] Fail QA if CI fails (feed back to dev self-healing loop)

### Streaming LLM output for long dev tasks
Large dev tasks can time out because the full response must complete before timeout fires.
- [ ] Evaluate streaming support in `shared/llm.py`
- [ ] Use streaming for dev and QA activities where supported by provider

---

## Infrastructure

### Tests
- [x] Integration tests for `create_and_merge_github_pr` — `tests/test_git_github.py`
- [x] Regression tests for PM activity with large architect response — `tests/test_pm_regression.py`
- [x] Unit tests for `shared/tools.py` — `tests/test_tools.py` (incl. non-.py syntax check skip)
- [x] `workflow_id` propagation through task state cache — `tests/test_pm_regression.py`
- [x] Unit tests for `_parse_multi_file_output` — `tests/test_parse_multi_file_output.py` (19 tests incl. path traversal security)
- [ ] Unit tests for `_task_module_path`
- [ ] Mock-LLM integration test for full dev → QA loop
- **Total: 325 tests passing** (up from 303)

### Isolation debug scripts
- [x] `scripts/debug_pm.py`
- [x] `scripts/debug_architect.py`
- [x] `scripts/debug_decomposer.py`
- [x] `scripts/debug_dev.py`
- [x] `scripts/debug_qa.py` (created, not fully validated)
- [ ] `scripts/debug_analyst.py`

### Kafka standalone agent path
`agents/dispatcher/` and `shared/standalone_dispatcher.py` lag behind the Temporal implementation.
Either bring up to parity or remove.

### ~~LLM cooldown too aggressive~~ ✓ Fixed
`PROVIDER_COOLDOWN_SECONDS` reduced from 60s to 15s.

### ~~docker-compose `version` obsolete warning~~ ✓ Fixed
Removed `version: "3.9"` from `docker-compose.yml`.

### MiniMax M2.5 Free on opencode
Model aliases added to `shared/llm.py`. Cannot fully validate — opencode free tier is
consistently rate-limited during testing sessions. Falls back to OpenAI automatically.
- [ ] Retry MiniMax isolation test when opencode quota resets (daily/hourly limit)
- [x] `OPENCODE_MODEL` env var added to `.env` and documented in README

### ~~`confluent-kafka` Docker build failure~~ ✓ Fixed (2026-03-30)
`orchestrator/Dockerfile` was missing C build toolchain required by `confluent-kafka`.
**Fix**: added `build-essential librdkafka-dev` to apt-get install.
