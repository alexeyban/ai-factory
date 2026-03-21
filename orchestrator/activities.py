import asyncio
import json
import subprocess
import time
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List

from temporalio import activity
from temporalio.common import RetryPolicy

from shared.llm import call_llm


retry_policy = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=5),
    maximum_attempts=3,
)


@activity.defn
async def architect_activity(task: Dict[str, Any]) -> Dict[str, Any]:
    """Execute architect agent logic - decompose task into subtasks"""
    description = task.get("description", "")

    output = call_llm("Architect", f"Break into tasks: {description}")

    try:
        tasks = json.loads(output)
    except json.JSONDecodeError:
        tasks = [{"task_id": str(uuid.uuid4()), "description": output}]

    return {
        "event_id": str(uuid.uuid4()),
        "task_id": task.get("task_id", str(uuid.uuid4())),
        "stage": "architect_done",
        "timestamp": int(time.time() * 1000),
        "decision": "continue",
        "tasks": tasks,
    }


@activity.defn
async def dev_activity(task: Dict[str, Any]) -> Dict[str, Any]:
    """Execute dev agent logic - generate code from task"""
    task_id = task.get("task_id", str(uuid.uuid4()))
    description = task.get("description", "")

    code = call_llm("Senior dev", f"Implement: {description}")

    workspace = Path("/workspace")
    workspace.mkdir(exist_ok=True)
    file_path = workspace / f"{task_id}.py"
    file_path.write_text(code)

    return {
        "task_id": task_id,
        "status": "success",
        "artifact": str(file_path),
        "code": code,
    }


@activity.defn
async def qa_activity(task: Dict[str, Any]) -> Dict[str, Any]:
    """Execute QA agent logic - run tests on artifact"""
    task_id = task.get("task_id", str(uuid.uuid4()))
    artifact = task.get("artifact", "")

    workspace = Path("/workspace")
    if not artifact:
        files = list(workspace.glob("*.py"))
        artifact = str(files[0]) if files else None

    if not artifact or not Path(artifact).exists():
        return {
            "task_id": task_id,
            "status": "skipped",
            "logs": "No artifact found",
        }

    result = subprocess.run(
        ["python", "-m", "pytest", str(workspace), "-v"],
        capture_output=True,
        text=True,
        timeout=60,
    )

    return {
        "task_id": task_id,
        "status": "success" if result.returncode == 0 else "fail",
        "logs": result.stdout + result.stderr,
    }


@activity.defn
async def analyst_activity(tasks: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Execute analyst agent logic - update project state"""
    state_file = Path("/workspace/project_state.md")
    state = state_file.read_text() if state_file.exists() else ""

    tasks_summary = "\n".join(
        [
            f"- {t.get('description', 'unknown')}: {t.get('status', 'unknown')}"
            for t in tasks
        ]
    )

    new_state = call_llm(
        "Analyst",
        f"Update project state based on these task results:\n{tasks_summary}\n\nCurrent state:\n{state}",
    )

    state_file.write_text(new_state)

    return {
        "status": "complete",
        "state": new_state,
    }


@activity.defn
async def process_single_task(task: Dict[str, Any]) -> Dict[str, Any]:
    """Process one task through dev + QA (sequential)

    Note: This is a simplified version that runs dev and QA sequentially
    in the same activity for simplicity.
    """
    task_id = task.get("task_id", str(uuid.uuid4()))
    description = task.get("description", "")

    # Run dev
    code = call_llm("Senior dev", f"Implement: {description}")

    workspace = Path("/workspace")
    workspace.mkdir(exist_ok=True)
    file_path = workspace / f"{task_id}.py"
    file_path.write_text(code)

    dev_result = {
        "task_id": task_id,
        "status": "success",
        "artifact": str(file_path),
        "code": code,
    }

    # Run QA
    result = subprocess.run(
        ["python", "-m", "pytest", str(workspace), "-v"],
        capture_output=True,
        text=True,
        timeout=60,
    )

    qa_result = {
        "task_id": task_id,
        "status": "success" if result.returncode == 0 else "fail",
        "logs": result.stdout + result.stderr,
    }

    return {
        "task_id": task_id,
        "dev": dev_result,
        "qa": qa_result,
    }


@activity.defn
async def process_all_tasks(tasks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Process all tasks in parallel"""
    results = await asyncio.gather(
        *[process_single_task(task) for task in tasks],
        return_exceptions=True,
    )

    return [{"error": str(r)} if isinstance(r, Exception) else r for r in results]
