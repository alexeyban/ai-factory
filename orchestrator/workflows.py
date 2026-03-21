import os
from datetime import timedelta
from typing import Dict, Any, Mapping
from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from orchestrator.activities import (
        pm_activity,
        pm_recovery_activity,
        architect_activity,
        process_all_tasks,
        analyst_activity,
        MAX_PM_RECOVERY_CYCLES,
    )


LLM_ACTIVITY_TIMEOUT_MINUTES = int(
    os.getenv("WORKFLOW_LLM_ACTIVITY_TIMEOUT_MINUTES", "30")
)
TASK_BATCH_TIMEOUT_HOURS = int(os.getenv("WORKFLOW_TASK_BATCH_TIMEOUT_HOURS", "6"))


def _raise_non_retryable_python_failure(exc: Exception) -> None:
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

            dev_qa_results = _require_activity_result(
                "process_all_tasks",
                await workflow.execute_activity(
                    process_all_tasks,
                    tasks,
                    start_to_close_timeout=timedelta(hours=TASK_BATCH_TIMEOUT_HOURS),
                    retry_policy=retry_policy,
                ),
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

                recovery_results = _require_activity_result(
                    "process_all_tasks",
                    await workflow.execute_activity(
                        process_all_tasks,
                        recovery_tasks,
                        start_to_close_timeout=timedelta(
                            hours=TASK_BATCH_TIMEOUT_HOURS
                        ),
                        retry_policy=retry_policy,
                    ),
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

            if not tasks:
                return {
                    "status": "complete",
                    "project_name": project_name,
                    "pm_result": pm_result,
                    "dev_qa_results": [],
                }

            dev_qa_results = _require_activity_result(
                "process_all_tasks",
                await workflow.execute_activity(
                    process_all_tasks,
                    tasks,
                    start_to_close_timeout=timedelta(hours=TASK_BATCH_TIMEOUT_HOURS),
                    retry_policy=retry_policy,
                ),
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

                recovery_results = _require_activity_result(
                    "process_all_tasks",
                    await workflow.execute_activity(
                        process_all_tasks,
                        recovery_tasks,
                        start_to_close_timeout=timedelta(
                            hours=TASK_BATCH_TIMEOUT_HOURS
                        ),
                        retry_policy=retry_policy,
                    ),
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
