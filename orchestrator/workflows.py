from datetime import timedelta
from typing import Dict, Any
from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from orchestrator.activities import (
        architect_activity,
        process_all_tasks,
        analyst_activity,
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

        architect_result = await workflow.execute_activity(
            architect_activity,
            initial_task,
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
                "architect_result": architect_result,
                "dev_qa_results": [],
                "analysis": {"status": "skipped", "reason": "no tasks"},
            }

        workflow.logger.info(f"Processing {len(tasks)} tasks in parallel")

        dev_qa_results = await workflow.execute_activity(
            process_all_tasks,
            tasks,
            start_to_close_timeout=timedelta(minutes=30),
            retry_policy=retry_policy,
        )

        workflow.logger.info("All tasks processed, running analyst")

        analysis = await workflow.execute_activity(
            analyst_activity,
            dev_qa_results,
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=retry_policy,
        )

        workflow.logger.info("Workflow completed successfully")

        return {
            "status": "complete",
            "architect_result": architect_result,
            "dev_qa_results": dev_qa_results,
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

        architect_result = await workflow.execute_activity(
            architect_activity,
            initial_task,
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=retry_policy,
        )

        tasks = architect_result.get("tasks", [])

        if not tasks:
            return {
                "status": "complete",
                "project_name": project_name,
                "dev_qa_results": [],
            }

        dev_qa_results = await workflow.execute_activity(
            process_all_tasks,
            tasks,
            start_to_close_timeout=timedelta(minutes=30),
        )

        analysis = await workflow.execute_activity(
            analyst_activity,
            dev_qa_results,
            start_to_close_timeout=timedelta(minutes=5),
        )

        return {
            "status": "complete",
            "project_name": project_name,
            "architect_result": architect_result,
            "dev_qa_results": dev_qa_results,
            "analysis": analysis,
        }
