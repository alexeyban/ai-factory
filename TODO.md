# TODO — AI Factory Next Steps

## Fixed (recent)

### ~~PM / architect / decomposer return `status: null`~~ ✓ Fixed
Result dicts for `pm_activity`, `architect_activity`, and `decomposer_activity` lacked a
`"status"` key. `_wrap_activity_result` reads `result.get("status")` which returned `None`,
causing the slim envelope's `status` field to be null.
**Fix**: Added `"status": "success"` to all three result dicts.

### ~~Decomposer receives entire PM execution plan as raw text~~ ✓ Fixed
`architect_request["description"]` in both `OrchestratorWorkflow` and `ProjectWorkflow`
included the full `execution_plan` list (30+ task objects serialized as JSON), which was
embedded as `project_description` in every normalized architect task. When passed to
decomposer, this produced payloads orders of magnitude over the token limit.
**Fix**: Replaced full plan with a title-only summary (`_pm_plan_titles`) in both workflows.
Also capped `project_description` in architect task contexts to 800 chars.

### ~~Task cache returning stale "success" across workflow runs~~ ✓ Fixed
`_execute_task_impl` checked `previous_state.get("status") == "success"` without verifying
the cached result belonged to the current `workflow_id`. Tasks that succeeded in any previous
workflow were returned from cache without re-running.
**Fix**: Added `previous_state.get("workflow_id") == workflow_id` guard; `workflow_id` is
now persisted in every `_save_task_state` call.

### ~~Concurrent `git config` returning exit 255~~ ✓ Fixed
Wave 1 tasks all start simultaneously and call `ensure_repo` on the same repo. Concurrent
writes to `.git/config` race and one or more processes return exit 255.
**Fix**: Changed `git config user.name/email` calls in `ensure_repo` and `clone_or_pull_project`
to use `check=False` — they are idempotent and failures are harmless.

### ~~PM Agent produces 0 tasks~~ ✓ Fixed
Truncated `architect_notes` and `analyst_notes` to 4000 chars each before inserting into
the PM prompt. The architect LLM response (~42k chars) was overflowing `LLM_MAX_PROMPT_TOKENS=8000`.

### ~~Temporal Deadlock errors (`[TMPRL1101]`)~~ ✓ Fixed
Replaced raw `str(dev_qa_results)` in recovery description with compact `json.dumps` (first
10 items, only `task_id`/`status`/`error` fields). Added `await asyncio.sleep(0)` yields.

### ~~Multiple concurrent workflows competing for rate limits~~ ✓ Fixed
`main.py` checks for running `OrchestratorWorkflow` instances and aborts unless `FORCE_START=true`.

---

## High Priority

### End-to-end verification of a full workflow run
The accumulated fixes (null status, decomposer overflow, stale cache, git race) have been
deployed but not yet verified with a successful full run from PM through analyst.
- [ ] Run a fresh workflow against a small project (< 10 tasks)
- [ ] Confirm PM → architect → decomposer → dev → QA → analyst all complete with `status: success`
- [ ] Verify GitHub PRs are created and merged by `create_and_merge_github_pr`
- [ ] Confirm no more exit-255 errors in dev-worker / qa-worker logs

### GitHub PR merge fails with branch protection
`create_and_merge_github_pr` uses the squash/merge API directly. If the target repo has
branch protection rules requiring status checks or reviews, the merge call returns 405/422.
- [ ] Detect 405/422 response and log a clear warning instead of silently returning `ok: False`
- [ ] Consider supporting auto-merge flag (sets PR to auto-merge when checks pass)

### Large project wave size vs. rate limits
Projects decomposed into 100+ tasks saturate LLM provider quotas during Wave 1 dispatch.
- [ ] Implement wave size cap (e.g. `MAX_WAVE_SIZE=20`) in `_dispatch_tasks`
- [ ] Add inter-wave delay when previous wave had rate-limit failures

---

## Features

### Local skills pre-validation (plan exists)
A plan (`stateful-sprouting-ripple.md`) exists to add `shared/tools.py` with 7 deterministic
skills (syntax check, file tree, import map, lint, typecheck, pytest+coverage, git diff) and
wire them into dev context and QA pre-validation.
- [ ] Create `shared/tools.py`
- [ ] Create `tests/test_tools.py`
- [ ] Update `shared/prompts/dev/user.txt` with `{existing_code}` block
- [ ] Update `_build_dev_prompt` to pass existing code context
- [ ] Update `_run_qa_for_artifact` with syntax → lint → typecheck → pytest pipeline
- [ ] Update `_install_project_dependencies` to include ruff, mypy, pytest-cov

### Branch cleanup for interrupted workflows
If a workflow fails mid-task, the task branch remains open on GitHub.
- [ ] Add `cleanup_stale_branches` step at end of `OrchestratorWorkflow` (or in analyst activity)
- [ ] List all remote branches matching `task-*` pattern and delete branches whose tasks are done

### Task resumability when PM returns 0 tasks
When PM produces an empty execution plan, the workflow ends without doing any work.
- [ ] Retry PM with a stripped-down prompt (project description only, no architect output) if `tasks_created == 0`
- [ ] Cap at 2 retries

### Dev agent output path verification
Files written by dev agent don't always respect the `output.files` paths in the task contract.
- [ ] Audit task contracts for `output.files` values
- [ ] Confirm dev agent commit includes expected files

---

## Infrastructure

### Tests
- [ ] Integration test for `create_and_merge_github_pr` with a mock HTTP server
- [ ] Regression test for PM activity with a large architect response (catch 0-task bug)
- [ ] Unit tests for `shared/tools.py` (stdlib only — no ruff/mypy needed)
- [ ] Test for `workflow_id` propagation through task state cache

### Kafka standalone agent path
The agents in `agents/dispatcher/` and `shared/standalone_dispatcher.py` lag behind
the Temporal implementation and are unmaintained. Either bring them up to parity or
remove them to reduce confusion.

### ~~LLM cooldown too aggressive~~ ✓ Fixed
Default `PROVIDER_COOLDOWN_SECONDS` reduced from 60s to 15s.

### ~~docker-compose `version` obsolete warning~~ ✓ Fixed
Removed `version: "3.9"` from `docker-compose.yml`.
