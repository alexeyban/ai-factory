import asyncio
import json
import os
from datetime import timedelta
from typing import Dict, Any, Mapping, NoReturn
from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

from agents.decomposer.agent import estimate_tokens as estimate_task_tokens
from agents.decomposer.agent import normalize_task_contract

with workflow.unsafe.imports_passed_through():
    from orchestrator.activities import (
        pm_activity,
        pm_recovery_activity,
        architect_activity,
        decomposer_activity,
        process_single_task,
        analyst_activity,
        MAX_PM_RECOVERY_CYCLES,
    )


LLM_ACTIVITY_TIMEOUT_MINUTES = int(
    os.getenv("WORKFLOW_LLM_ACTIVITY_TIMEOUT_MINUTES", "30")
)
TASK_BATCH_TIMEOUT_HOURS = int(os.getenv("WORKFLOW_TASK_BATCH_TIMEOUT_HOURS", "6"))
TASK_DECOMPOSITION_TOKEN_LIMIT = int(
    os.getenv("TASK_DECOMPOSITION_TOKEN_LIMIT", "8000")
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
    expanded: list[Dict[str, Any]] = []
    for task in tasks:
        normalized_task = normalize_task_contract(task, project_context=project_context)
        if _estimate_task_tokens(normalized_task) <= TASK_DECOMPOSITION_TOKEN_LIMIT:
            expanded.append(normalized_task)
            continue

        decomposed = await workflow.execute_activity(
            decomposer_activity,
            {**project_context, **normalized_task},
            start_to_close_timeout=timedelta(minutes=LLM_ACTIVITY_TIMEOUT_MINUTES),
            retry_policy=retry_policy,
        )
        expanded.extend(
            _normalize_tasks(
                _require_task_list("decomposer_activity", decomposed), project_context
            )
        )

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
    """Dispatch each task as a separate Temporal activity and collect results.

    Each task is executed concurrently via asyncio.gather so Temporal tracks
    every task individually — with its own history, retries, and timeout.
    return_exceptions=True ensures one failing task never cancels the others.
    """
    if not tasks:
        return []

    workflow_id = workflow.info().workflow_id
    workflow.logger.info(
        f"[{workflow_id}] Dispatching {len(tasks)} tasks to agents"
    )

    async def _run_one(task: Dict[str, Any]) -> Dict[str, Any]:
        return await workflow.execute_activity(
            process_single_task,
            task,
            start_to_close_timeout=timedelta(hours=TASK_BATCH_TIMEOUT_HOURS),
            retry_policy=retry_policy,
        )

    raw = await asyncio.gather(*[_run_one(t) for t in tasks], return_exceptions=True)

    results: list[Dict[str, Any]] = []
    for i, outcome in enumerate(raw):
        task_id = tasks[i].get("task_id", f"task_{i}")
        if isinstance(outcome, BaseException):
            workflow.logger.error(
                f"[{workflow_id}] Task {task_id} raised: {outcome}"
            )
            results.append(
                {
                    "task_id": task_id,
                    "project_name": tasks[i].get("project_name"),
                    "project_repo_path": tasks[i].get("project_repo_path"),
                    "status": "error",
                    "error": str(outcome),
                }
            )
        elif isinstance(outcome, Mapping):
            results.append(dict(outcome))
        else:
            results.append(
                {
                    "task_id": task_id,
                    "status": "error",
                    "error": f"Unexpected result type: {type(outcome).__name__}",
                }
            )

    return results


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

            workflow.logger.info(
                f"[{workflow_id}] Starting orchestrator workflow for task: {initial_task.get('description', 'unknown')[:100]}"
            )

            pm_result = _require_activity_result(
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

            workflow.logger.info(
                f"[{workflow_id}] PM completed, generated {len(pm_result.get('execution_plan', []))} planned assignments"
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
                    f"PM execution plan:\n{pm_result.get('execution_plan', [])}"
                ),
            }

            architect_result = _require_activity_result(
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

            workflow.logger.info(
                f"[{workflow_id}] Architect completed, got {len(architect_result.get('tasks', []))} tasks"
            )

            tasks = architect_result.get("tasks", [])
            project_context = _project_context(initial_task, pm_result)
            tasks = await _prepare_execution_tasks(tasks, project_context, retry_policy)

            if not tasks:
                workflow.logger.warning(
                    f"[{workflow_id}] No tasks returned from architect"
                )
                return {
                    "status": "complete",
                    "pm_result": pm_result,
                    "architect_result": architect_result,
                    "dev_qa_results": [],
                    "analysis": {"status": "skipped", "reason": "no tasks"},
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
                or result.get("qa", {}).get("status") not in {None, "success"}
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
                        f"Previous execution results:\n{dev_qa_results}"
                    ),
                }

                pm_recovery_result = _require_activity_result(
                    "pm_recovery_activity",
                    await workflow.execute_activity(
                        pm_recovery_activity,
                        recovery_request,
                        start_to_close_timeout=timedelta(
                            minutes=LLM_ACTIVITY_TIMEOUT_MINUTES
                        ),
                        retry_policy=retry_policy,
                    ),
                )

                recovery_architect_result = _require_activity_result(
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
                )

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
                or result.get("qa", {}).get("status") not in {None, "success"}
                or result.get("error")
                for result in dev_qa_results
            ):
                final_status = "needs_attention"

            workflow.logger.info(
                f"[{workflow_id}] Workflow completed with status {final_status}"
            )

            return {
                "status": final_status,
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

            workflow.logger.info(
                f"[{workflow_id}] Starting project workflow: {project_name}"
            )

            pm_result = _require_activity_result(
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

            architect_result = _require_activity_result(
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
                            f"PM execution plan:\n{pm_result.get('execution_plan', [])}"
                        ),
                    },
                    start_to_close_timeout=timedelta(
                        minutes=LLM_ACTIVITY_TIMEOUT_MINUTES
                    ),
                    retry_policy=retry_policy,
                ),
            )

            tasks = architect_result.get("tasks", [])
            tasks = await _prepare_execution_tasks(
                tasks, _project_context(initial_task, pm_result), retry_policy
            )

            if not tasks:
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
                or result.get("qa", {}).get("status") not in {None, "success"}
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
                        f"{description}\n\nRecovery cycle {recovery_cycle}\nPrevious execution results:\n{dev_qa_results}"
                    ),
                }

                pm_recovery_result = _require_activity_result(
                    "pm_recovery_activity",
                    await workflow.execute_activity(
                        pm_recovery_activity,
                        recovery_request,
                        start_to_close_timeout=timedelta(
                            minutes=LLM_ACTIVITY_TIMEOUT_MINUTES
                        ),
                        retry_policy=retry_policy,
                    ),
                )

                recovery_architect_result = _require_activity_result(
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
                )

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
                or result.get("qa", {}).get("status") not in {None, "success"}
                or result.get("error")
                for result in dev_qa_results
            ):
                final_status = "needs_attention"

            return {
                "status": final_status,
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
