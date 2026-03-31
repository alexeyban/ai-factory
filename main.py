import asyncio
import os
import time
from datetime import timedelta

from temporalio.client import Client

from orchestrator.workflows import OrchestratorWorkflow

GITHUB_PROJECT_URL = "https://github.com/alexeyban/reversi-alpha-zero"


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

    # Guard: warn if another workflow is already running (rate limit contention)
    running = []
    async for wf in client.list_workflows('WorkflowType="OrchestratorWorkflow"'):
        if str(wf.status) in ("WORKFLOW_EXECUTION_STATUS_RUNNING", "Running", "1"):
            running.append(wf.id)
    if running:
        print(
            f"\nWARNING: {len(running)} OrchestratorWorkflow(s) already running: {running}\n"
            "Running multiple workflows simultaneously causes LLM rate limit contention.\n"
            "Set FORCE_START=true to proceed anyway.\n"
        )
        if os.getenv("FORCE_START", "false").lower() != "true":
            print("Aborting. Set FORCE_START=true to override.")
            return
        print("FORCE_START=true — proceeding despite concurrent workflows.")

    readme_path = os.path.join(os.path.dirname(__file__), "workspace/projects/reversi_ai/README.md")
    try:
        with open(readme_path, encoding="utf-8") as f:
            readme_content = f.read()
    except FileNotFoundError:
        readme_content = ""

    description = f"""Implement the Reversi AlphaZero AI project at: {GITHUB_PROJECT_URL}

This is a self-learning Reversi (Othello) AI using AlphaZero-style reinforcement learning with Monte Carlo Tree Search (MCTS). Implement ALL 10 tasks from the specification below. Each task maps to a Python module in the `reversi/` package.

IMPORTANT: Clone {GITHUB_PROJECT_URL} first, then implement all code there. Commit and push each module as it is completed.

Full task specification:

{readme_content}"""

    await start_project(
        client,
        task_queue,
        description,
        temporal_namespace,
        f"reversi-alpha-zero-{int(time.time())}",
    )


async def start_project(
    client: Client, task_queue: str, description: str, namespace: str, workflow_id: str
):
    print(f"Starting workflow: {workflow_id}")
    print(f"Project: {GITHUB_PROJECT_URL}")

    initial_task = {
        "task_id": workflow_id,
        "description": description,
        "project_name": "reversi-alpha-zero",
        "github_url": GITHUB_PROJECT_URL,
    }

    handle = await client.start_workflow(
        OrchestratorWorkflow.run,
        initial_task,
        id=workflow_id,
        task_queue=task_queue,
        execution_timeout=timedelta(hours=12),
    )

    print(f"Workflow started: {workflow_id}")
    print(
        f"Monitor at: http://localhost:8088/namespaces/{namespace}/workflows/{workflow_id}"
    )

    try:
        result = await handle.result(rpc_timeout=timedelta(hours=12))
        print("Workflow completed successfully!")
        print(f"Result: {result}")
    except Exception as e:
        print(f"Workflow failed: {e}")


if __name__ == "__main__":
    asyncio.run(main())
