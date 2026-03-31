import argparse
import asyncio
import os
import re
import time
from datetime import timedelta
from pathlib import Path

from temporalio.client import Client

from orchestrator.workflows import OrchestratorWorkflow

GITHUB_PROJECT_URL = "https://github.com/alexeyban/reversi-alpha-zero"
DEFAULT_PROJECT_NAME = "reversi-alpha-zero"


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "project"


def _parse_args():
    parser = argparse.ArgumentParser(
        description="Start an AI Factory OrchestratorWorkflow.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Use built-in reversi-alpha-zero defaults:
  python main.py

  # Start a custom project:
  python main.py --name my-api --github-url https://github.com/owner/repo \\
                 --description "Build a REST API with JWT auth"

  # Load description from a file:
  python main.py --name my-api --github-url https://github.com/owner/repo \\
                 --description-file brief.md
""",
    )
    parser.add_argument("--name", default=None, help="Project name (slug used as directory and workflow prefix)")
    parser.add_argument("--github-url", default=None, help="GitHub repository URL")
    parser.add_argument("--description", default=None, help="Project description / brief (inline text)")
    parser.add_argument("--description-file", default=None, metavar="FILE",
                        help="Path to a file whose contents will be used as the description")
    return parser.parse_args()


async def main():
    args = _parse_args()

    temporal_address = os.getenv("TEMPORAL_ADDRESS", "localhost:7233")
    temporal_namespace = os.getenv("TEMPORAL_NAMESPACE", "default")
    task_queue = os.getenv("TASK_QUEUE", "ai-factory-tasks")

    # Resolve project parameters: CLI args > env vars > built-in defaults
    if args.name or args.github_url or args.description or args.description_file:
        # Custom project mode
        project_name = _slugify(args.name) if args.name else DEFAULT_PROJECT_NAME
        github_url = args.github_url or GITHUB_PROJECT_URL

        if args.description_file:
            description = Path(args.description_file).read_text(encoding="utf-8")
        elif args.description:
            description = args.description
        else:
            # Fallback: use a minimal description
            description = f"Implement the project at: {github_url}"
    else:
        # Default mode: reversi-alpha-zero with README
        project_name = DEFAULT_PROJECT_NAME
        github_url = GITHUB_PROJECT_URL

        readme_path = os.path.join(os.path.dirname(__file__), "workspace/projects/reversi_ai/README.md")
        try:
            with open(readme_path, encoding="utf-8") as f:
                readme_content = f.read()
        except FileNotFoundError:
            readme_content = ""

        description = (
            f"Implement the Reversi AlphaZero AI project at: {github_url}\n\n"
            "This is a self-learning Reversi (Othello) AI using AlphaZero-style reinforcement learning "
            "with Monte Carlo Tree Search (MCTS). Implement ALL 10 tasks from the specification below. "
            "Each task maps to a Python module in the `reversi/` package.\n\n"
            f"IMPORTANT: Clone {github_url} first, then implement all code there. "
            "Commit and push each module as it is completed.\n\n"
            f"Full task specification:\n\n{readme_content}"
        )

    workflow_id = f"{project_name}-{int(time.time())}"

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

    await start_project(
        client,
        task_queue,
        project_name,
        github_url,
        description,
        temporal_namespace,
        workflow_id,
    )


async def start_project(
    client: Client,
    task_queue: str,
    project_name: str,
    github_url: str,
    description: str,
    namespace: str,
    workflow_id: str,
):
    print(f"Starting workflow: {workflow_id}")
    print(f"Project: {github_url}")

    initial_task = {
        "task_id": workflow_id,
        "description": description,
        "project_name": project_name,
        "github_url": github_url,
    }

    handle = await client.start_workflow(
        OrchestratorWorkflow.run,
        initial_task,
        id=workflow_id,
        task_queue=task_queue,
        execution_timeout=timedelta(hours=12),
    )

    print(f"Workflow started: {workflow_id}")
    print(f"Monitor at: http://localhost:8080/namespaces/{namespace}/workflows/{workflow_id}")
    print(f"Web UI: http://localhost:8088/")

    try:
        result = await handle.result(rpc_timeout=timedelta(hours=12))
        print("Workflow completed successfully!")
        print(f"Result: {result}")
    except Exception as e:
        print(f"Workflow failed: {e}")


if __name__ == "__main__":
    asyncio.run(main())
