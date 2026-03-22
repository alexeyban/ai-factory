# TODO — AI Factory Next Steps

## High Priority

### PM Agent produces 0 tasks
The PM agent frequently returns an empty task list. Root causes to investigate:
- Prompt truncation (`LLM_MAX_PROMPT_TOKENS=8000` cuts the user prompt) — the architect LLM response (~42k chars) is too large to pass back through the PM plan step
- The plan parsing regex / JSON extraction may be silently failing when the LLM output is truncated
- Consider splitting the PM activity: one call for architect analysis, a separate call for task generation with a smaller, focused prompt
- Increase `LLM_MAX_PROMPT_TOKENS` or compress intermediate context before passing to PM

### Temporal Deadlock errors (`[TMPRL1101]`)
Occurs when the workflow coroutine blocks the event loop for >2s. Likely caused by heavy JSON serialization or file I/O inside the workflow code (not in an activity). Audit `orchestrator/workflows.py` for any blocking calls outside `await workflow.execute_activity(...)`.

### Multiple concurrent workflows competing for rate limits
When multiple `main.py` invocations run simultaneously, all LLM providers hit 429. Add a guard in `main.py` or document that only one workflow should run at a time. Alternatively add a workflow mutex via a Temporal signal/search attribute.

---

## Features

### GitHub PR auto-merge — verify end-to-end
The PR creation code (`shared/git.py::create_and_merge_github_pr`) was added but not yet observed to run because no workflow reached the QA-pass stage in testing. Need to:
- Let a full pipeline run to completion
- Confirm PRs appear in GitHub and branches are cleaned up
- Test the fallback path (no GITHUB_TOKEN) explicitly

### Branch cleanup for interrupted workflows
If a workflow fails mid-task, the task branch remains open on GitHub. Add a cleanup step in the analyst activity or a dedicated `cleanup_stale_branches` activity that runs at the end of `OrchestratorWorkflow`.

### Task resumability for PM-0-tasks failure
When PM returns 0 tasks, the workflow ends without doing any work. Add a retry: if `tasks_created == 0`, retry PM with a stripped-down prompt (just the project description, no architect output). Cap at 2 retries.

### Improve prompt size management
- Architect output (~40-50k chars) is too large to pass through subsequent steps
- Add a summarization step after architect: compress the full architecture doc to a 2000-token summary for use in downstream prompts
- Store full artifact on disk; pass only the summary in the task dict

### Dev agent output path
Currently dev agent writes files to `workspace/projects/<name>/` but the output path in the task contract (`output.files`) is not always respected. Verify files land in the correct locations per task spec.

---

## Infrastructure

### LLM cooldown too aggressive
60-second cooldown after any 429 causes long idle periods when all providers are rate-limited simultaneously. Consider:
- Shorter initial cooldown (10-15s) with exponential backoff per retry
- Stagger requests across providers instead of sequential fallback

### docker-compose `version` obsolete warning
Remove the `version: "3.9"` line from `docker-compose.yml` to suppress warnings.

### Tests
- Add integration test for `create_and_merge_github_pr` with a mock HTTP server
- Add test for PM activity with a real (small) prompt to catch 0-task regressions
- Add test for `_workflow_id` propagation through `_run_one`
