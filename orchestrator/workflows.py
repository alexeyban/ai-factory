from datetime import timedelta
from typing import Dict, Any
from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from orchestrator.activities import (
        pm_activity,
        pm_recovery_activity,
        architect_activity,
        process_all_tasks,
        analyst_activity,
        MAX_PM_RECOVERY_CYCLES,
    )


@workflow.defn
class OrchestratorWorkflow:
    """Main workflow orchestrating the AI factory pipeline"""

    @workflow.run
    async def run(self, initial_task: Dict[str, Any]) -> Dict[str, Any]:
        retry_policy = RetryPolicy(
            initial_interval=timedelta(seconds=1),
            backoff_coefficient=2.0,
            maximum_interval=timedelta(minutes=5),
            maximum_attempts=3,
        )

        workflow.logger.info(
            f"Starting orchestrator workflow for task: {initial_task.get('description', 'unknown')}"
        )

        pm_result = await workflow.execute_activity(
            pm_activity,
            initial_task,
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=retry_policy,
        )

        workflow.logger.info(
            f"PM completed, generated {len(pm_result.get('execution_plan', []))} planned assignments"
        )

        architect_request = {
            **initial_task,
            "project_name": pm_result.get("project_name", initial_task.get("project_name")),
            "project_repo_path": pm_result.get("project_repo_path"),
            "description": (
                f"{initial_task.get('description', '')}\n\n"
                f"PM delivery summary:\n{pm_result.get('delivery_summary', '')}\n\n"
                f"PM architect guidance:\n{pm_result.get('architect_guidance', [])}\n\n"
                f"PM execution plan:\n{pm_result.get('execution_plan', [])}"
            ),
        }

        architect_result = await workflow.execute_activity(
            architect_activity,
            architect_request,
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=retry_policy,
        )

        workflow.logger.info(
            f"Architect completed, got {len(architect_result.get('tasks', []))} tasks"
        )

        tasks = architect_result.get("tasks", [])

        if not tasks:
            workflow.logger.warning("No tasks returned from architect")
            return {
                "status": "complete",
                "pm_result": pm_result,
                "architect_result": architect_result,
                "dev_qa_results": [],
                "analysis": {"status": "skipped", "reason": "no tasks"},
            }

        workflow.logger.info(f"Processing {len(tasks)} tasks in parallel")

        dev_qa_results = await workflow.execute_activity(
            process_all_tasks,
            tasks,
            start_to_close_timeout=timedelta(hours=2),
            retry_policy=retry_policy,
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
            workflow.logger.info(f"Starting PM recovery cycle {recovery_cycle}")
            recovery_request = {
                **initial_task,
                "project_name": pm_result.get("project_name", initial_task.get("project_name")),
                "project_repo_path": pm_result.get("project_repo_path"),
                "recovery_cycle": recovery_cycle,
                "failure_summary": dev_qa_results,
                "description": (
                    f"{initial_task.get('description', '')}\n\n"
                    f"Recovery cycle {recovery_cycle}\n"
                    f"Previous execution results:\n{dev_qa_results}"
                ),
            }
            pm_recovery_result = await workflow.execute_activity(
                pm_recovery_activity,
                recovery_request,
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=retry_policy,
            )
            recovery_architect_result = await workflow.execute_activity(
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
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=retry_policy,
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
            recovery_results = await workflow.execute_activity(
                process_all_tasks,
                recovery_tasks,
                start_to_close_timeout=timedelta(hours=2),
                retry_policy=retry_policy,
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

        workflow.logger.info("All tasks processed, running analyst")

        analysis = await workflow.execute_activity(
            analyst_activity,
            dev_qa_results,
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=retry_policy,
        )

        final_status = "complete"
        if any(
            result.get("status") != "success"
            or result.get("qa", {}).get("status") not in {None, "success"}
            or result.get("error")
            for result in dev_qa_results
        ):
            final_status = "needs_attention"

        workflow.logger.info(f"Workflow completed with status {final_status}")

        return {
            "status": final_status,
            "pm_result": pm_result,
            "architect_result": architect_result,
            "dev_qa_results": dev_qa_results,
            "recovery_rounds": recovery_rounds,
            "analysis": analysis,
        }


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

        retry_policy = RetryPolicy(
            initial_interval=timedelta(seconds=1),
            backoff_coefficient=2.0,
            maximum_interval=timedelta(minutes=5),
            maximum_attempts=3,
        )

        workflow.logger.info(f"Starting project workflow: {project_name}")

        pm_result = await workflow.execute_activity(
            pm_activity,
            initial_task,
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=retry_policy,
        )

        architect_result = await workflow.execute_activity(
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
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=retry_policy,
        )

        tasks = architect_result.get("tasks", [])

        if not tasks:
            return {
                "status": "complete",
                "project_name": project_name,
                "pm_result": pm_result,
                "dev_qa_results": [],
            }

        dev_qa_results = await workflow.execute_activity(
            process_all_tasks,
            tasks,
            start_to_close_timeout=timedelta(hours=2),
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
                "failure_summary": dev_qa_results,
                "description": (
                    f"{description}\n\nRecovery cycle {recovery_cycle}\nPrevious execution results:\n{dev_qa_results}"
                ),
            }
            pm_recovery_result = await workflow.execute_activity(
                pm_recovery_activity,
                recovery_request,
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=retry_policy,
            )
            recovery_architect_result = await workflow.execute_activity(
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
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=retry_policy,
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
            recovery_results = await workflow.execute_activity(
                process_all_tasks,
                recovery_tasks,
                start_to_close_timeout=timedelta(hours=2),
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

        analysis = await workflow.execute_activity(
            analyst_activity,
            dev_qa_results,
            start_to_close_timeout=timedelta(minutes=5),
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
