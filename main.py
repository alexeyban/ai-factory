import asyncio
import os
import time
from datetime import timedelta

from temporalio.client import Client

from orchestrator.workflows import OrchestratorWorkflow


async def main():
    temporal_address = os.getenv("TEMPORAL_ADDRESS", "localhost:7233")
    temporal_namespace = os.getenv("TEMPORAL_NAMESPACE", "default")
    task_queue = os.getenv("TASK_QUEUE", "ai-factory-tasks")

    print(f"Connecting to Temporal at {temporal_address}")

    client = await Client.connect(
        temporal_address,
        namespace=temporal_namespace,
    )

    print(f"Connected to Temporal namespace: {temporal_namespace}")

    await start_project(
        client, task_queue, "Build REST API for todo app", temporal_namespace
    )


async def start_project(
    client: Client, task_queue: str, description: str, namespace: str
):
    """Start a new project workflow"""
    project_name = "todo-api"
    timestamp = int(time.time())
    workflow_id = f"project-{project_name}-{timestamp}"

    print(f"Starting project workflow: {workflow_id}")
    print(f"Description: {description}")

    initial_task = {
        "task_id": workflow_id,
        "description": description,
        "project_name": project_name,
    }

    handle = await client.start_workflow(
        OrchestratorWorkflow.run,
        initial_task,
        id=workflow_id,
        task_queue=task_queue,
        execution_timeout=timedelta(minutes=30),
    )

    print(f"Workflow started: {workflow_id}")
    print(
        f"Monitor at: http://localhost:8088/namespaces/{namespace}/workflows/{workflow_id}"
    )

    try:
        result = await handle.result(rpc_timeout=timedelta(minutes=30))
        print("Workflow completed successfully!")
        print(f"Result: {result}")
    except Exception as e:
        print(f"Workflow failed: {e}")


if __name__ == "__main__":
    asyncio.run(main())
