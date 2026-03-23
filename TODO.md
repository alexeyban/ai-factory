# TODO — AI Factory Next Steps

## High Priority

### ~~PM Agent produces 0 tasks~~ ✓ Fixed
**Fix applied**: Truncated `architect_notes` and `analyst_notes` to 4000 chars each before
inserting into the PM prompt. The architect LLM response (~42k chars) was overflowing
`LLM_MAX_PROMPT_TOKENS=8000`, causing the JSON output to be silently cut off.
Remaining: add a regression test to catch future prompt-size regressions.

### ~~Temporal Deadlock errors (`[TMPRL1101]`)~~ ✓ Fixed
**Fix applied**:
- Replaced raw `str(dev_qa_results)` in recovery description with a compact `json.dumps`
  (first 10 items, only `task_id`/`status`/`error` fields) in both `OrchestratorWorkflow`
  and `ProjectWorkflow`.
- Added `await asyncio.sleep(0)` after each wave in `_dispatch_tasks` and inside
  `_decompose_large_tasks` to yield the event loop and prevent >2s blocking.

### ~~Multiple concurrent workflows competing for rate limits~~ ✓ Fixed
**Fix applied**: `main.py` now queries Temporal for running `OrchestratorWorkflow` instances
before starting. If any are found it prints a warning and aborts unless `FORCE_START=true`.

---

## Features

### GitHub PR auto-merge — verify end-to-end
The PR creation code (`shared/git.py::create_and_merge_github_pr`) was added but not yet
observed to run because no workflow reached the QA-pass stage in testing. Need to:
- Let a full pipeline run to completion
- Confirm PRs appear in GitHub and branches are cleaned up
- Test the fallback path (no GITHUB_TOKEN) explicitly

### Branch cleanup for interrupted workflows
If a workflow fails mid-task, the task branch remains open on GitHub. Add a cleanup step
in the analyst activity or a dedicated `cleanup_stale_branches` activity that runs at the
end of `OrchestratorWorkflow`.

### Task resumability for PM-0-tasks failure
When PM returns 0 tasks, the workflow ends without doing any work. Add a retry: if
`tasks_created == 0`, retry PM with a stripped-down prompt (just the project description,
no architect output). Cap at 2 retries.

### Dev agent output path
Currently dev agent writes files to `workspace/projects/<name>/` but the output path in
the task contract (`output.files`) is not always respected. Verify files land in the
correct locations per task spec.

---

## Infrastructure

### ~~LLM cooldown too aggressive~~ ✓ Fixed
**Fix applied**: Default `PROVIDER_COOLDOWN_SECONDS` reduced from 60s to 15s.
Still configurable via env var. Exponential backoff per-provider is a future improvement.

### ~~docker-compose `version` obsolete warning~~ ✓ Fixed
**Fix applied**: Removed `version: "3.9"` from `docker-compose.yml`.

### Tests
- Add integration test for `create_and_merge_github_pr` with a mock HTTP server
- Add regression test for PM activity with a large architect response (catch 0-task bug)
- Add test for `_workflow_id` propagation through `_run_one`
