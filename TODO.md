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

### ~~GitHub PR merge fails with branch protection~~ ✓ Fixed
`create_and_merge_github_pr` now detects HTTP 405/422, logs a clear warning, and
automatically enables auto-merge via the GitHub GraphQL API as a fallback.
`create_and_merge_github_pr` return dict gains `auto_merge: bool` field.

### ~~Large project wave size vs. rate limits~~ ✓ Fixed
Added `MAX_WAVE_SIZE` env var (default 20) that caps each wave in `_dispatch_tasks`.
Added `INTER_WAVE_RATE_LIMIT_DELAY_SECONDS` (default 30s) delay before the next wave
when the previous wave contained rate-limit failures (HTTP 429 errors).

---

## Features

### ~~Local skills pre-validation~~ ✓ Done
`shared/tools.py` with 7 skills (syntax check, file tree, import map, lint, typecheck,
pytest+coverage, git diff + error history). Wired into `_build_dev_prompt` (existing_code
block) and `_run_qa_for_artifact` (syntax → lint → typecheck → pytest pipeline).
`_install_project_dependencies` installs ruff, mypy, pytest-cov.

### ~~Branch cleanup for interrupted workflows~~ ✓ Done
`cleanup_stale_branches_activity` added to `activities.py`. Lists remote `task-*` branches,
deletes those whose task IDs appear in the completed set (via GitHub API or git push --delete).
Called at the end of `OrchestratorWorkflow` after analyst runs.

### ~~Task resumability when PM returns 0 tasks~~ ✓ Done
Both `OrchestratorWorkflow` and `ProjectWorkflow` now retry PM (with description-only prompt,
no architect/analyst notes) and re-run architect if 0 tasks are returned. Capped at 2 retries.

### Dev agent output path verification
Files written by dev agent don't always respect the `output.files` paths in the task contract.
- [ ] Audit task contracts for `output.files` values
- [ ] Confirm dev agent commit includes expected files

---

## Infrastructure

### Tests
- [x] Integration tests for `create_and_merge_github_pr` — `tests/test_git_github.py`
  (happy path, 405 → auto-merge, 422 → auto-merge, both-fail, no-token, non-github)
- [x] Regression test for PM activity with large architect response — `tests/test_pm_regression.py`
- [x] Unit tests for `shared/tools.py` — `tests/test_tools.py`
- [x] Test for `workflow_id` propagation through task state cache — `tests/test_pm_regression.py`

### Kafka standalone agent path
The agents in `agents/dispatcher/` and `shared/standalone_dispatcher.py` lag behind
the Temporal implementation and are unmaintained. Either bring them up to parity or
remove them to reduce confusion.

### ~~LLM cooldown too aggressive~~ ✓ Fixed
Default `PROVIDER_COOLDOWN_SECONDS` reduced from 60s to 15s.

### ~~docker-compose `version` obsolete warning~~ ✓ Fixed
Removed `version: "3.9"` from `docker-compose.yml`.
