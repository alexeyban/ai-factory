import asyncio
import json
import os
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Dict, Any, Mapping, NoReturn
from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

from agents.decomposer.agent import estimate_tokens as estimate_task_tokens
from agents.decomposer.agent import normalize_task_contract

with workflow.unsafe.imports_passed_through():
    from shared.episode import new_episode_id, log_episode_event

with workflow.unsafe.imports_passed_through():
    from orchestrator.activities import (
        pm_activity,
        pm_recovery_activity,
        architect_activity,
        decomposer_activity,
        process_single_task,
        dev_activity,
        qa_activity,
        dev_task,
        qa_task,
        refactor_task,
        setup_task,
        docs_task,
        analyst_activity,
        cleanup_stale_branches_activity,
        extract_skill_activity,
        policy_update_activity,
        skill_optimization_activity,
        MAX_PM_RECOVERY_CYCLES,
    )


LLM_ACTIVITY_TIMEOUT_MINUTES = int(
    os.getenv("WORKFLOW_LLM_ACTIVITY_TIMEOUT_MINUTES", "30")
)
TASK_BATCH_TIMEOUT_HOURS = int(os.getenv("WORKFLOW_TASK_BATCH_TIMEOUT_HOURS", "24"))
TASK_EXECUTION_TIMEOUT_HOURS = int(os.getenv("WORKFLOW_TASK_EXECUTION_TIMEOUT_HOURS", "2"))

# Route task type / assigned_agent to the appropriate named Temporal activity
# and the dedicated task queue for that agent container.
# Tasks in the same wave (all deps satisfied) run in parallel.
_AGENT_TO_ACTIVITY = {
    "dev": dev_task,
    "qa": qa_task,
}
_TYPE_TO_ACTIVITY = {
    "feature": dev_task,
    "bugfix": dev_task,
    "refactor": refactor_task,
    "setup": setup_task,
    "test": qa_task,
    "docs": docs_task,
}
# Each named activity runs in its own agent container on a dedicated queue.
_ACTIVITY_TASK_QUEUE: dict = {
    dev_task: "dev-agent-tasks",
    qa_task: "qa-agent-tasks",
    refactor_task: "refactor-agent-tasks",
    setup_task: "setup-agent-tasks",
    docs_task: "docs-agent-tasks",
}


def _pick_activity(task: Dict[str, Any]):
    """Return the named activity function for this task."""
    agent = (task.get("assigned_agent") or "").lower()
    if agent in _AGENT_TO_ACTIVITY:
        return _AGENT_TO_ACTIVITY[agent]
    task_type = (task.get("type") or "feature").lower()
    return _TYPE_TO_ACTIVITY.get(task_type, dev_task)
TASK_DECOMPOSITION_TOKEN_LIMIT = int(
    os.getenv("TASK_DECOMPOSITION_TOKEN_LIMIT", "8000")
)
MAX_WAVE_SIZE = int(os.getenv("MAX_WAVE_SIZE", "20"))
INTER_WAVE_RATE_LIMIT_DELAY_SECONDS = int(
    os.getenv("INTER_WAVE_RATE_LIMIT_DELAY_SECONDS", "30")
)


def _raise_non_retryable_python_failure(exc: Exception) -> NoReturn:
    if isinstance(
        exc, (AttributeError, KeyError, IndexError, TypeError, AssertionError)
    ):
        raise ApplicationError(
            f"Non-retryable workflow code failure: {exc}",
            type=exc.__class__.__name__,
            non_retryable=True,
        ) from exc
    raise exc


def _require_activity_result(name: str, result: Any) -> Dict[str, Any]:
    if result is None:
        raise RuntimeError(f"{name} returned no result")
    if not isinstance(result, Mapping):
        raise TypeError(f"{name} returned invalid result type: {type(result).__name__}")
    return dict(result)


def _require_task_list(name: str, result: Any) -> list[Dict[str, Any]]:
    if result is None:
        raise RuntimeError(f"{name} returned no result")
    if not isinstance(result, list):
        raise TypeError(f"{name} returned invalid result type: {type(result).__name__}")

    tasks: list[Dict[str, Any]] = []
    for item in result:
        if not isinstance(item, Mapping):
            raise TypeError(f"{name} returned invalid task type: {type(item).__name__}")
        tasks.append(dict(item))
    return tasks


def _load_result_from_file(envelope: Dict[str, Any]) -> Dict[str, Any]:
    """Read the full activity result from disk given a slim Temporal envelope.

    Context files are immutable once written — reading them directly is safe
    on workflow replay because the content never changes between runs.
    Uses sandbox_unrestricted() to bypass Temporal's I/O sandbox for this
    deterministic file read.
    """
    context_file = envelope.get("_context_file")
    if not context_file:
        return envelope

    try:
        with workflow.unsafe.sandbox_unrestricted():
            data = json.loads(Path(context_file).read_text(encoding="utf-8"))
        data.pop("_meta", None)
        return data
    except Exception as exc:
        workflow.logger.warning(f"[workflow] Failed to load context file {context_file}: {exc}")
        return envelope


def _project_context(
    initial_task: Dict[str, Any], pm_result: Dict[str, Any] | None = None
) -> Dict[str, Any]:
    context = {
        "project_name": initial_task.get("project_name")
        or (pm_result or {}).get("project_name")
        or initial_task.get("title")
        or initial_task.get("task_id")
        or "project",
        "project_repo_path": (pm_result or {}).get(
            "project_repo_path", initial_task.get("project_repo_path", "")
        ),
        "project_description": initial_task.get("description", ""),
        "github_url": initial_task.get("github_url", ""),
    }
    return context


def _normalize_tasks(
    tasks: list[Dict[str, Any]], project_context: Dict[str, Any]
) -> list[Dict[str, Any]]:
    return [
        normalize_task_contract(task, project_context=project_context)
        for task in tasks
        if isinstance(task, Mapping)
    ]


def _estimate_task_tokens(task: Dict[str, Any]) -> int:
    return estimate_task_tokens(json.dumps(task, indent=2, ensure_ascii=True))


async def _decompose_large_tasks(
    tasks: list[Dict[str, Any]],
    project_context: Dict[str, Any],
    retry_policy: RetryPolicy,
) -> list[Dict[str, Any]]:
    workflow_id = workflow.info().workflow_id
    expanded: list[Dict[str, Any]] = []
    for task in tasks:
        normalized_task = normalize_task_contract(task, project_context=project_context)
        if _estimate_task_tokens(normalized_task) <= TASK_DECOMPOSITION_TOKEN_LIMIT:
            expanded.append(normalized_task)
            continue

        decomposed_envelope = await workflow.execute_activity(
            decomposer_activity,
            {**project_context, **normalized_task, "_workflow_id": workflow_id},
            start_to_close_timeout=timedelta(minutes=LLM_ACTIVITY_TIMEOUT_MINUTES),
            retry_policy=retry_policy,
        )
        decomposed_full = _load_result_from_file(_require_activity_result("decomposer_activity", decomposed_envelope))
        expanded.extend(
            _normalize_tasks(
                decomposed_full.get("tasks", []), project_context
            )
        )
        await asyncio.sleep(0)  # yield to prevent TMPRL1101 deadlock

    return expanded


async def _prepare_execution_tasks(
    tasks: list[Dict[str, Any]],
    project_context: Dict[str, Any],
    retry_policy: RetryPolicy,
) -> list[Dict[str, Any]]:
    normalized_tasks = _normalize_tasks(tasks, project_context)
    return await _decompose_large_tasks(normalized_tasks, project_context, retry_policy)


async def _dispatch_tasks(
    tasks: list[Dict[str, Any]],
    retry_policy: RetryPolicy,
) -> list[Dict[str, Any]]:
    """Dispatch tasks as named Temporal activities in dependency order.

    Tasks whose dependencies are all satisfied run concurrently in a wave.
    Each successive wave starts only after its prerequisites complete.
    Failures in one wave are recorded but do not block the next wave.
    Each task is dispatched to the typed activity matching its type/agent:
      feature/bugfix → DEV_Task
      test           → QA_Task
      refactor       → REFACTOR_Task
      setup          → SETUP_Task
      docs           → DOCS_Task
    """
    if not tasks:
        return []

    workflow_id = workflow.info().workflow_id
    workflow.logger.info(
        f"[{workflow_id}] Dispatching {len(tasks)} tasks to agents (dependency-ordered)"
    )

    async def _run_one(task: Dict[str, Any]) -> Dict[str, Any]:
        activity_fn = _pick_activity(task)
        task_queue = _ACTIVITY_TASK_QUEUE.get(activity_fn, "ai-factory-tasks")
        return await workflow.execute_activity(
            activity_fn,
            {**task, "_workflow_id": workflow_id},
            task_queue=task_queue,
            start_to_close_timeout=timedelta(hours=TASK_EXECUTION_TIMEOUT_HOURS),
            retry_policy=retry_policy,
        )

    def _make_error(task: Dict[str, Any], reason: str) -> Dict[str, Any]:
        return {
            "task_id": task.get("task_id", "unknown"),
            "project_name": task.get("project_name"),
            "project_repo_path": task.get("project_repo_path"),
            "status": "error",
            "error": reason,
        }

    completed: dict[str, Dict[str, Any]] = {}  # task_id → result
    remaining = list(tasks)
    wave = 0
    prev_wave_had_rate_limit = False

    while remaining:
        ready = [
            t for t in remaining
            if all(dep in completed for dep in t.get("dependencies", []))
        ]

        if not ready:
            # Circular or missing dependencies — run everything left to avoid deadlock
            workflow.logger.warning(
                f"[{workflow_id}] Dependency deadlock: running {len(remaining)} remaining tasks unconditionally"
            )
            ready = list(remaining)

        # Cap wave size to avoid overwhelming LLM rate limits on large projects
        if len(ready) > MAX_WAVE_SIZE:
            workflow.logger.info(
                f"[{workflow_id}] Wave too large ({len(ready)} tasks), capping at {MAX_WAVE_SIZE}"
            )
            ready = ready[:MAX_WAVE_SIZE]

        wave += 1
        ready_ids = [t.get("task_id") for t in ready]
        workflow.logger.info(
            f"[{workflow_id}] Wave {wave}: dispatching {len(ready)} tasks {ready_ids}"
        )

        # Add inter-wave delay after a wave that hit rate limits
        if prev_wave_had_rate_limit:
            workflow.logger.info(
                f"[{workflow_id}] Previous wave had rate-limit failures; "
                f"waiting {INTER_WAVE_RATE_LIMIT_DELAY_SECONDS}s before next wave"
            )
            await asyncio.sleep(INTER_WAVE_RATE_LIMIT_DELAY_SECONDS)

        raw = await asyncio.gather(*[_run_one(t) for t in ready], return_exceptions=True)

        prev_wave_had_rate_limit = False
        for task, outcome in zip(ready, raw):
            task_id = task.get("task_id", "unknown")
            if isinstance(outcome, BaseException):
                workflow.logger.error(
                    f"[{workflow_id}] Task {task_id} raised: {outcome}"
                )
                result = _make_error(task, str(outcome))
                if "429" in str(outcome) or "rate" in str(outcome).lower():
                    prev_wave_had_rate_limit = True
            elif isinstance(outcome, Mapping):
                result = dict(outcome)
                if result.get("status") == "error" and (
                    "429" in str(result.get("error", ""))
                    or "rate" in str(result.get("error", "")).lower()
                ):
                    prev_wave_had_rate_limit = True
            else:
                result = _make_error(task, f"Unexpected result type: {type(outcome).__name__}")

            completed[task_id] = result
            remaining.remove(task)
        await asyncio.sleep(0)  # yield event loop between waves to prevent TMPRL1101

    return list(completed.values())


@workflow.defn
class OrchestratorWorkflow:
    """Main workflow orchestrating the AI factory pipeline"""

    @workflow.run
    async def run(self, initial_task: Dict[str, Any]) -> Dict[str, Any]:
        try:
            retry_policy = RetryPolicy(
                initial_interval=timedelta(seconds=1),
                backoff_coefficient=2.0,
                maximum_interval=timedelta(minutes=5),
                maximum_attempts=3,
            )

            workflow_id = workflow.info().workflow_id
            # Propagate workflow_id into all activity inputs so context files
            # are saved under the correct workflow directory (not "unknown").
            initial_task = {**initial_task, "_workflow_id": workflow_id}

            # Phase 0: episode tracking
            episode_id: str = initial_task.get("episode_id") or new_episode_id()
            max_iterations: int = int(initial_task.get("max_iterations", 1))
            initial_task = {**initial_task, "episode_id": episode_id}

            log_episode_event(
                episode_id=episode_id,
                event_type="workflow_started",
                agent="orchestrator",
                data={"workflow_id": workflow_id, "max_iterations": max_iterations},
            )

            workflow.logger.info(
                f"[{workflow_id}] Starting orchestrator workflow for task: "
                f"{initial_task.get('description', 'unknown')[:100]} "
                f"(episode={episode_id}, max_iterations={max_iterations})"
            )

            pm_envelope = _require_activity_result(
                "pm_activity",
                await workflow.execute_activity(
                    pm_activity,
                    initial_task,
                    start_to_close_timeout=timedelta(
                        minutes=LLM_ACTIVITY_TIMEOUT_MINUTES
                    ),
                    retry_policy=retry_policy,
                ),
            )
            pm_result = _load_result_from_file(pm_envelope)

            workflow.logger.info(
                f"[{workflow_id}] PM completed, generated {len(pm_result.get('execution_plan', []))} planned assignments"
            )

            # Summarise PM plan to titles only — full task objects can be 30+ items
            # and would overflow Temporal's 512 KB payload and decomposer inputs.
            _pm_plan_titles = "\n".join(
                f"- [{t.get('assigned_agent', 'dev')}] {t.get('title', t.get('description', ''))[:80]}"
                for t in (pm_result.get("execution_plan") or [])[:30]
            )
            architect_request = {
                **initial_task,
                "project_name": pm_result.get(
                    "project_name", initial_task.get("project_name")
                ),
                "project_repo_path": pm_result.get("project_repo_path"),
                "description": (
                    f"{initial_task.get('description', '')}\n\n"
                    f"PM delivery summary:\n{pm_result.get('delivery_summary', '')}\n\n"
                    f"PM architect guidance:\n{pm_result.get('architect_guidance', [])}\n\n"
                    f"PM execution plan tasks:\n{_pm_plan_titles}"
                ),
            }

            architect_envelope = _require_activity_result(
                "architect_activity",
                await workflow.execute_activity(
                    architect_activity,
                    architect_request,
                    start_to_close_timeout=timedelta(
                        minutes=LLM_ACTIVITY_TIMEOUT_MINUTES
                    ),
                    retry_policy=retry_policy,
                ),
            )
            architect_result = _load_result_from_file(architect_envelope)

            workflow.logger.info(
                f"[{workflow_id}] Architect completed, got {len(architect_result.get('tasks', []))} tasks"
            )

            tasks = architect_result.get("tasks", [])
            project_context = _project_context(initial_task, pm_result)
            tasks = await _prepare_execution_tasks(tasks, project_context, retry_policy)

            # If architect produced 0 tasks, retry PM with a stripped-down prompt
            # (description only, no architect/analyst notes) then re-run architect.
            _PM_RETRY_LIMIT = 2
            _pm_retry = 0
            while not tasks and _pm_retry < _PM_RETRY_LIMIT:
                _pm_retry += 1
                workflow.logger.warning(
                    f"[{workflow_id}] No tasks from architect (attempt {_pm_retry}/{_PM_RETRY_LIMIT}). "
                    f"Retrying PM with stripped-down prompt."
                )
                stripped_task = {
                    **initial_task,
                    "description": initial_task.get("description", ""),
                    "_pm_retry": _pm_retry,
                }
                pm_retry_envelope = _require_activity_result(
                    "pm_activity",
                    await workflow.execute_activity(
                        pm_activity,
                        stripped_task,
                        start_to_close_timeout=timedelta(minutes=LLM_ACTIVITY_TIMEOUT_MINUTES),
                        retry_policy=retry_policy,
                    ),
                )
                pm_result = _load_result_from_file(pm_retry_envelope)
                _pm_plan_titles = "\n".join(
                    f"- [{t.get('assigned_agent', 'dev')}] {t.get('title', t.get('description', ''))[:80]}"
                    for t in (pm_result.get("execution_plan") or [])[:30]
                )
                architect_retry_envelope = _require_activity_result(
                    "architect_activity",
                    await workflow.execute_activity(
                        architect_activity,
                        {
                            **stripped_task,
                            "project_name": pm_result.get("project_name", initial_task.get("project_name")),
                            "project_repo_path": pm_result.get("project_repo_path"),
                            "description": (
                                f"{initial_task.get('description', '')}\n\n"
                                f"PM delivery summary:\n{pm_result.get('delivery_summary', '')}\n\n"
                                f"PM execution plan tasks:\n{_pm_plan_titles}"
                            ),
                        },
                        start_to_close_timeout=timedelta(minutes=LLM_ACTIVITY_TIMEOUT_MINUTES),
                        retry_policy=retry_policy,
                    ),
                )
                architect_result = _load_result_from_file(architect_retry_envelope)
                project_context = _project_context(initial_task, pm_result)
                tasks = await _prepare_execution_tasks(
                    architect_result.get("tasks", []), project_context, retry_policy
                )

            if not tasks:
                workflow.logger.warning(
                    f"[{workflow_id}] No tasks returned after {_pm_retry} PM retries; aborting"
                )
                return {
                    "status": "complete",
                    "pm_result": pm_result,
                    "architect_result": architect_result,
                    "dev_qa_results": [],
                    "analysis": {"status": "skipped", "reason": "no tasks after PM retries"},
                }

            workflow.logger.info(
                f"[{workflow_id}] Processing {len(tasks)} tasks in parallel"
            )

            dev_qa_results = _require_task_list(
                "_dispatch_tasks",
                await _dispatch_tasks(tasks, retry_policy),
            )

            recovery_rounds = []
            recovery_cycle = 0
            while recovery_cycle < MAX_PM_RECOVERY_CYCLES and any(
                result.get("status") != "success"
                or result.get("qa_status") not in {None, "success"}
                or result.get("error")
                for result in dev_qa_results
            ):
                recovery_cycle += 1
                workflow.logger.info(
                    f"[{workflow_id}] Starting PM recovery cycle {recovery_cycle}"
                )

                recovery_request = {
                    **initial_task,
                    "project_name": pm_result.get(
                        "project_name", initial_task.get("project_name")
                    ),
                    "project_repo_path": pm_result.get("project_repo_path"),
                    "recovery_cycle": recovery_cycle,
                    "failure_summary": [
                        {
                            "task_id": r.get("task_id"),
                            "status": r.get("status"),
                            "error": r.get("error"),
                        }
                        for r in dev_qa_results
                    ],
                    "description": (
                        f"{initial_task.get('description', '')}\n\n"
                        f"Recovery cycle {recovery_cycle}\n"
                        f"Previous execution results:\n"
                        + json.dumps(
                            [
                                {k: r.get(k) for k in ("task_id", "status", "error")}
                                for r in dev_qa_results[:10]
                            ],
                            ensure_ascii=True,
                        )
                    ),
                }

                pm_recovery_result = _load_result_from_file(_require_activity_result(
                    "pm_recovery_activity",
                    await workflow.execute_activity(
                        pm_recovery_activity,
                        recovery_request,
                        start_to_close_timeout=timedelta(
                            minutes=LLM_ACTIVITY_TIMEOUT_MINUTES
                        ),
                        retry_policy=retry_policy,
                    ),
                ))

                recovery_architect_result = _load_result_from_file(_require_activity_result(
                    "architect_activity",
                    await workflow.execute_activity(
                        architect_activity,
                        {
                            **recovery_request,
                            "description": (
                                f"{recovery_request.get('description', '')}\n\n"
                                f"PM recovery summary:\n{pm_recovery_result.get('delivery_summary', '')}\n\n"
                                f"PM recovery guidance:\n{pm_recovery_result.get('architect_guidance', [])}\n\n"
                                f"PM recovery plan:\n{pm_recovery_result.get('execution_plan', [])}"
                            ),
                        },
                        start_to_close_timeout=timedelta(
                            minutes=LLM_ACTIVITY_TIMEOUT_MINUTES
                        ),
                        retry_policy=retry_policy,
                    ),
                ))

                recovery_tasks = recovery_architect_result.get("tasks", [])
                recovery_tasks = await _prepare_execution_tasks(
                    recovery_tasks,
                    _project_context(recovery_request, pm_recovery_result),
                    retry_policy,
                )
                if not recovery_tasks:
                    recovery_rounds.append(
                        {
                            "cycle": recovery_cycle,
                            "pm_result": pm_recovery_result,
                            "architect_result": recovery_architect_result,
                            "results": [],
                        }
                    )
                    break

                recovery_results = _require_task_list(
                    "_dispatch_tasks",
                    await _dispatch_tasks(recovery_tasks, retry_policy),
                )

                recovery_rounds.append(
                    {
                        "cycle": recovery_cycle,
                        "pm_result": pm_recovery_result,
                        "architect_result": recovery_architect_result,
                        "results": recovery_results,
                    }
                )
                dev_qa_results.extend(recovery_results)

            workflow.logger.info(
                f"[{workflow_id}] All tasks processed, running analyst"
            )

            analysis = _require_activity_result(
                "analyst_activity",
                await workflow.execute_activity(
                    analyst_activity,
                    {
                        "dev_qa_results": dev_qa_results,
                        "workflow_id": workflow_id,
                        "_workflow_id": workflow_id,
                        "project_goal": pm_result.get("project_goal", "")[:500],
                        "delivery_summary": pm_result.get("delivery_summary", "")[:500],
                        "analyst_guidance": pm_result.get("analyst_guidance", []),
                    },
                    start_to_close_timeout=timedelta(
                        minutes=LLM_ACTIVITY_TIMEOUT_MINUTES
                    ),
                    retry_policy=retry_policy,
                ),
            )

            final_status = "complete"
            if any(
                result.get("status") != "success"
                or result.get("qa_status") not in {None, "success"}
                or result.get("error")
                for result in dev_qa_results
            ):
                final_status = "needs_attention"

            # Clean up stale task-* branches for all completed tasks
            completed_task_ids = [r.get("task_id") for r in dev_qa_results if r.get("task_id")]
            await workflow.execute_activity(
                cleanup_stale_branches_activity,
                {
                    "project_repo_path": pm_result.get("project_repo_path", ""),
                    "completed_task_ids": completed_task_ids,
                },
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=RetryPolicy(maximum_attempts=1),
            )

            workflow.logger.info(
                f"[{workflow_id}] Workflow completed with status {final_status}"
            )

            log_episode_event(
                episode_id=episode_id,
                event_type="workflow_finished",
                agent="orchestrator",
                data={"workflow_id": workflow_id, "status": final_status},
            )

            return {
                "status": final_status,
                "episode_id": episode_id,
                "pm_result": pm_result,
                "architect_result": architect_result,
                "dev_qa_results": dev_qa_results,
                "recovery_rounds": recovery_rounds,
                "analysis": analysis,
            }
        except Exception as exc:
            workflow_id = workflow.info().workflow_id
            workflow.logger.error(f"[{workflow_id}] Workflow failed: {exc}")
            _raise_non_retryable_python_failure(exc)


@workflow.defn
class ProjectWorkflow:
    """Alternative workflow that uses human-readable IDs"""

    @workflow.run
    async def run(self, project_name: str, description: str) -> Dict[str, Any]:
        import time

        task_id = f"{project_name}-{int(time.time())}"

        initial_task = {
            "task_id": task_id,
            "description": description,
            "project_name": project_name,
        }

        try:
            retry_policy = RetryPolicy(
                initial_interval=timedelta(seconds=1),
                backoff_coefficient=2.0,
                maximum_interval=timedelta(minutes=5),
                maximum_attempts=3,
            )

            workflow_id = workflow.info().workflow_id
            initial_task = {**initial_task, "_workflow_id": workflow_id}

            # Phase 0: episode tracking
            episode_id: str = new_episode_id()
            initial_task = {**initial_task, "episode_id": episode_id}

            log_episode_event(
                episode_id=episode_id,
                event_type="workflow_started",
                agent="orchestrator",
                data={"workflow_id": workflow_id, "project_name": project_name},
            )

            workflow.logger.info(
                f"[{workflow_id}] Starting project workflow: {project_name} (episode={episode_id})"
            )

            pm_result = _load_result_from_file(_require_activity_result(
                "pm_activity",
                await workflow.execute_activity(
                    pm_activity,
                    initial_task,
                    start_to_close_timeout=timedelta(
                        minutes=LLM_ACTIVITY_TIMEOUT_MINUTES
                    ),
                    retry_policy=retry_policy,
                ),
            ))

            _pm_plan_titles_proj = "\n".join(
                f"- [{t.get('assigned_agent', 'dev')}] {t.get('title', t.get('description', ''))[:80]}"
                for t in (pm_result.get("execution_plan") or [])[:30]
            )
            architect_result = _load_result_from_file(_require_activity_result(
                "architect_activity",
                await workflow.execute_activity(
                    architect_activity,
                    {
                        **initial_task,
                        "project_name": pm_result.get("project_name", project_name),
                        "project_repo_path": pm_result.get("project_repo_path"),
                        "description": (
                            f"{description}\n\nPM delivery summary:\n{pm_result.get('delivery_summary', '')}\n\n"
                            f"PM architect guidance:\n{pm_result.get('architect_guidance', [])}\n\n"
                            f"PM execution plan tasks:\n{_pm_plan_titles_proj}"
                        ),
                    },
                    start_to_close_timeout=timedelta(
                        minutes=LLM_ACTIVITY_TIMEOUT_MINUTES
                    ),
                    retry_policy=retry_policy,
                ),
            ))

            tasks = architect_result.get("tasks", [])
            tasks = await _prepare_execution_tasks(
                tasks, _project_context(initial_task, pm_result), retry_policy
            )

            _PM_RETRY_LIMIT = 2
            _pm_retry = 0
            while not tasks and _pm_retry < _PM_RETRY_LIMIT:
                _pm_retry += 1
                workflow.logger.warning(
                    f"[{workflow_id}] No tasks from architect (attempt {_pm_retry}/{_PM_RETRY_LIMIT}). "
                    f"Retrying PM with stripped-down prompt."
                )
                stripped_task = {**initial_task, "_pm_retry": _pm_retry}
                pm_result = _load_result_from_file(_require_activity_result(
                    "pm_activity",
                    await workflow.execute_activity(
                        pm_activity,
                        stripped_task,
                        start_to_close_timeout=timedelta(minutes=LLM_ACTIVITY_TIMEOUT_MINUTES),
                        retry_policy=retry_policy,
                    ),
                ))
                _pm_plan_titles_proj = "\n".join(
                    f"- [{t.get('assigned_agent', 'dev')}] {t.get('title', t.get('description', ''))[:80]}"
                    for t in (pm_result.get("execution_plan") or [])[:30]
                )
                architect_result = _load_result_from_file(_require_activity_result(
                    "architect_activity",
                    await workflow.execute_activity(
                        architect_activity,
                        {
                            **stripped_task,
                            "project_name": pm_result.get("project_name", project_name),
                            "project_repo_path": pm_result.get("project_repo_path"),
                            "description": (
                                f"{description}\n\n"
                                f"PM delivery summary:\n{pm_result.get('delivery_summary', '')}\n\n"
                                f"PM execution plan tasks:\n{_pm_plan_titles_proj}"
                            ),
                        },
                        start_to_close_timeout=timedelta(minutes=LLM_ACTIVITY_TIMEOUT_MINUTES),
                        retry_policy=retry_policy,
                    ),
                ))
                tasks = await _prepare_execution_tasks(
                    architect_result.get("tasks", []),
                    _project_context(initial_task, pm_result),
                    retry_policy,
                )

            if not tasks:
                workflow.logger.warning(
                    f"[{workflow_id}] No tasks after {_pm_retry} PM retries; aborting"
                )
                return {
                    "status": "complete",
                    "project_name": project_name,
                    "pm_result": pm_result,
                    "dev_qa_results": [],
                }

            dev_qa_results = _require_task_list(
                "_dispatch_tasks",
                await _dispatch_tasks(tasks, retry_policy),
            )

            recovery_rounds = []
            recovery_cycle = 0
            while recovery_cycle < MAX_PM_RECOVERY_CYCLES and any(
                result.get("status") != "success"
                or result.get("qa_status") not in {None, "success"}
                or result.get("error")
                for result in dev_qa_results
            ):
                recovery_cycle += 1
                recovery_request = {
                    **initial_task,
                    "project_name": pm_result.get("project_name", project_name),
                    "project_repo_path": pm_result.get("project_repo_path"),
                    "recovery_cycle": recovery_cycle,
                    "failure_summary": [
                        {
                            "task_id": r.get("task_id"),
                            "status": r.get("status"),
                            "error": r.get("error"),
                        }
                        for r in dev_qa_results
                    ],
                    "description": (
                        f"{description}\n\nRecovery cycle {recovery_cycle}\n"
                        "Previous execution results:\n"
                        + json.dumps(
                            [
                                {k: r.get(k) for k in ("task_id", "status", "error")}
                                for r in dev_qa_results[:10]
                            ],
                            ensure_ascii=True,
                        )
                    ),
                }

                pm_recovery_result = _load_result_from_file(_require_activity_result(
                    "pm_recovery_activity",
                    await workflow.execute_activity(
                        pm_recovery_activity,
                        recovery_request,
                        start_to_close_timeout=timedelta(
                            minutes=LLM_ACTIVITY_TIMEOUT_MINUTES
                        ),
                        retry_policy=retry_policy,
                    ),
                ))

                recovery_architect_result = _load_result_from_file(_require_activity_result(
                    "architect_activity",
                    await workflow.execute_activity(
                        architect_activity,
                        {
                            **recovery_request,
                            "description": (
                                f"{recovery_request.get('description', '')}\n\n"
                                f"PM recovery summary:\n{pm_recovery_result.get('delivery_summary', '')}\n\n"
                                f"PM recovery guidance:\n{pm_recovery_result.get('architect_guidance', [])}\n\n"
                                f"PM recovery plan:\n{pm_recovery_result.get('execution_plan', [])}"
                            ),
                        },
                        start_to_close_timeout=timedelta(
                            minutes=LLM_ACTIVITY_TIMEOUT_MINUTES
                        ),
                        retry_policy=retry_policy,
                    ),
                ))

                recovery_tasks = recovery_architect_result.get("tasks", [])
                recovery_tasks = await _prepare_execution_tasks(
                    recovery_tasks,
                    _project_context(recovery_request, pm_recovery_result),
                    retry_policy,
                )
                if not recovery_tasks:
                    recovery_rounds.append(
                        {
                            "cycle": recovery_cycle,
                            "pm_result": pm_recovery_result,
                            "architect_result": recovery_architect_result,
                            "results": [],
                        }
                    )
                    break

                recovery_results = _require_task_list(
                    "_dispatch_tasks",
                    await _dispatch_tasks(recovery_tasks, retry_policy),
                )

                recovery_rounds.append(
                    {
                        "cycle": recovery_cycle,
                        "pm_result": pm_recovery_result,
                        "architect_result": recovery_architect_result,
                        "results": recovery_results,
                    }
                )
                dev_qa_results.extend(recovery_results)

            analysis = _require_activity_result(
                "analyst_activity",
                await workflow.execute_activity(
                    analyst_activity,
                    {"dev_qa_results": dev_qa_results, "workflow_id": workflow_id},
                    start_to_close_timeout=timedelta(
                        minutes=LLM_ACTIVITY_TIMEOUT_MINUTES
                    ),
                    retry_policy=retry_policy,
                ),
            )

            final_status = "complete"
            if any(
                result.get("status") != "success"
                or result.get("qa_status") not in {None, "success"}
                or result.get("error")
                for result in dev_qa_results
            ):
                final_status = "needs_attention"

            log_episode_event(
                episode_id=episode_id,
                event_type="workflow_finished",
                agent="orchestrator",
                data={"workflow_id": workflow_id, "status": final_status},
            )

            return {
                "status": final_status,
                "episode_id": episode_id,
                "project_name": project_name,
                "pm_result": pm_result,
                "architect_result": architect_result,
                "dev_qa_results": dev_qa_results,
                "recovery_rounds": recovery_rounds,
                "analysis": analysis,
            }
        except Exception as exc:
            workflow_id = workflow.info().workflow_id
            workflow.logger.error(f"[{workflow_id}] Workflow failed: {exc}")
            _raise_non_retryable_python_failure(exc)


# ---------------------------------------------------------------------------
# Phase 5 — Learning Loop (AlphaZero-style self-play)
# ---------------------------------------------------------------------------

_DEFAULT_MAX_ITERATIONS = int(os.getenv("MAX_ITERATIONS", "5"))
_SKILL_OPTIMIZE_EVERY_N = int(os.getenv("SKILL_OPTIMIZE_EVERY_N", "10"))
_DEFAULT_STAGNATION_THRESHOLD = int(os.getenv("STAGNATION_THRESHOLD", "3"))
_LEARNING_ACTIVITY_TIMEOUT_MINUTES = int(
    os.getenv("WORKFLOW_LLM_ACTIVITY_TIMEOUT_MINUTES", "30")
)


@dataclass
class LearningWorkflowInput:
    task: Dict[str, Any]
    max_iterations: int = field(default_factory=lambda: _DEFAULT_MAX_ITERATIONS)
    num_candidates: int = field(
        default_factory=lambda: int(os.getenv("NUM_CANDIDATES", "1"))
    )
    exploration_rate: float = field(
        default_factory=lambda: float(os.getenv("EXPLORATION_RATE", "0.3"))
    )
    stagnation_threshold: int = field(
        default_factory=lambda: _DEFAULT_STAGNATION_THRESHOLD
    )
    episode_id: str = ""


@dataclass
class LearningWorkflowResult:
    best_solution: Dict[str, Any]
    best_reward: float
    total_iterations: int
    stopped_reason: str   # 'max_iterations' | 'stagnation' | 'perfect_score'
    skills_extracted: int


@workflow.defn
class LearningWorkflow:
    """AlphaZero-style iterative learning loop for a single task.

    Each iteration:
        1. dev_activity  — generate candidate solution(s)
        2. qa_activity   — validate + compute reward
        3. Track best reward; detect stagnation
        4. On improvement → extract_skill_activity
    After all iterations:
        5. policy_update_activity — update skill weights + prompt examples
    """

    @workflow.run
    async def run(self, inp: LearningWorkflowInput) -> LearningWorkflowResult:
        task = inp.task
        workflow_id = workflow.info().workflow_id

        episode_id: str = inp.episode_id or new_episode_id()
        task = {
            **task,
            "episode_id": episode_id,
            "_workflow_id": workflow_id,
            "num_candidates": inp.num_candidates,
            "exploration_rate": inp.exploration_rate,
        }

        retry_policy = RetryPolicy(
            initial_interval=timedelta(seconds=1),
            backoff_coefficient=2.0,
            maximum_interval=timedelta(minutes=5),
            maximum_attempts=3,
        )
        activity_timeout = timedelta(minutes=_LEARNING_ACTIVITY_TIMEOUT_MINUTES)

        best_reward: float = 0.0
        best_solution: Dict[str, Any] = {}
        skills_extracted: int = 0
        stagnation_count: int = 0
        stopped_reason: str = "max_iterations"
        iteration: int = 0

        log_episode_event(
            episode_id=episode_id,
            event_type="learning_started",
            agent="learning_workflow",
            data={
                "workflow_id": workflow_id,
                "max_iterations": inp.max_iterations,
                "num_candidates": inp.num_candidates,
            },
        )

        for iteration in range(inp.max_iterations):
            workflow.logger.info(
                f"[{workflow_id}] LearningWorkflow iteration {iteration + 1}/{inp.max_iterations}"
                f" (best_reward={best_reward:.4f}, stagnation={stagnation_count})"
            )

            # 1. Dev: generate candidate(s)
            dev_envelope = await workflow.execute_activity(
                dev_activity,
                {**task, "attempt_number": iteration + 1},
                start_to_close_timeout=activity_timeout,
                retry_policy=retry_policy,
            )
            dev_result = _load_result_from_file(
                _require_activity_result("dev_activity", dev_envelope)
            )

            # 2. QA: validate + compute reward
            qa_envelope = await workflow.execute_activity(
                qa_activity,
                {
                    **task,
                    "attempt_number": iteration + 1,
                    "artifact": dev_result.get("artifact", ""),
                    "candidates": dev_result.get("candidates", []),
                },
                start_to_close_timeout=activity_timeout,
                retry_policy=retry_policy,
            )
            qa_result = _load_result_from_file(
                _require_activity_result("qa_activity", qa_envelope)
            )

            iteration_reward: float = float(qa_result.get("reward", 0.0))

            # 3. Track best + stagnation
            if iteration_reward > best_reward:
                best_reward = iteration_reward
                best_solution = {
                    **qa_result,
                    "task_id": task.get("task_id", ""),
                    "iteration": iteration,
                    "artifact": dev_result.get("artifact", ""),
                    "code": dev_result.get("code", ""),
                    "skills_used": dev_result.get("skills_used", []),
                }
                stagnation_count = 0

                # 4. Extract skill on genuine improvement
                if qa_result.get("status") == "success":
                    extract_result = await workflow.execute_activity(
                        extract_skill_activity,
                        {
                            "task_id": task.get("task_id", ""),
                            "episode_id": episode_id,
                            "artifact": dev_result.get("artifact", ""),
                            "code": dev_result.get("code", ""),
                        },
                        start_to_close_timeout=timedelta(minutes=5),
                        retry_policy=RetryPolicy(maximum_attempts=1),
                    )
                    if (extract_result or {}).get("extracted"):
                        skills_extracted += 1
            else:
                stagnation_count += 1

            log_episode_event(
                episode_id=episode_id,
                event_type="iteration_complete",
                agent="learning_workflow",
                data={
                    "iteration": iteration,
                    "reward": iteration_reward,
                    "best_reward": best_reward,
                    "stagnation_count": stagnation_count,
                },
            )

            # 5. Stagnation stop
            if stagnation_count >= inp.stagnation_threshold:
                stopped_reason = "stagnation"
                log_episode_event(
                    episode_id=episode_id,
                    event_type="stagnation_detected",
                    agent="learning_workflow",
                    data={"iteration": iteration, "stagnation_count": stagnation_count},
                )
                workflow.logger.info(
                    f"[{workflow_id}] Stagnation after {stagnation_count} non-improving iterations"
                )
                break

            # 6. Perfect score stop
            if best_reward >= 0.99:
                stopped_reason = "perfect_score"
                workflow.logger.info(
                    f"[{workflow_id}] Perfect score ({best_reward:.4f}) — stopping early"
                )
                break

            await asyncio.sleep(0)  # yield to prevent TMPRL1101

        # 7. Policy update (effects apply to NEXT episode — avoids circular dependency)
        await workflow.execute_activity(
            policy_update_activity,
            {
                "episode_id": episode_id,
                "best_solution": best_solution,
                "best_reward": best_reward,
            },
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )

        # 8. Skill optimization — runs every SKILL_OPTIMIZE_EVERY_N episodes
        try:
            with workflow.unsafe.sandbox_unrestricted():
                import os as _os
                _policy_path = (
                    _os.path.join(_os.getenv("AI_FACTORY_WORKSPACE", "workspace"),
                                  ".ai_factory", "policy_state.json")
                )
                import json as _json
                _pstate = {}
                try:
                    with open(_policy_path) as _f:
                        _pstate = _json.load(_f)
                except Exception:
                    pass
            _total_episodes = int(_pstate.get("reward_samples", 0))
        except Exception:
            _total_episodes = 0
        if _SKILL_OPTIMIZE_EVERY_N > 0 and _total_episodes > 0 and _total_episodes % _SKILL_OPTIMIZE_EVERY_N == 0:
            await workflow.execute_activity(
                skill_optimization_activity,
                {"episode_count": _total_episodes},
                start_to_close_timeout=timedelta(minutes=10),
                retry_policy=RetryPolicy(maximum_attempts=1),
            )

        log_episode_event(
            episode_id=episode_id,
            event_type="learning_finished",
            agent="learning_workflow",
            data={
                "workflow_id": workflow_id,
                "best_reward": best_reward,
                "total_iterations": iteration + 1,
                "stopped_reason": stopped_reason,
                "skills_extracted": skills_extracted,
            },
        )

        workflow.logger.info(
            f"[{workflow_id}] LearningWorkflow done: "
            f"reward={best_reward:.4f} iterations={iteration + 1} "
            f"reason={stopped_reason} skills={skills_extracted}"
        )

        return LearningWorkflowResult(
            best_solution=best_solution,
            best_reward=best_reward,
            total_iterations=iteration + 1,
            stopped_reason=stopped_reason,
            skills_extracted=skills_extracted,
        )
