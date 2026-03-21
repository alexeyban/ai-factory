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
from shared.prompts.loader import load_prompt, render_prompt


retry_policy = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=5),
    maximum_attempts=3,
)

ARCHITECT_SYSTEM_PROMPT = load_prompt("architect", "system")
ARCHITECT_USER_PROMPT = load_prompt("architect", "user")
ANALYST_SYSTEM_PROMPT = load_prompt("analyst", "system")
ANALYST_USER_PROMPT = load_prompt("analyst", "user")
QA_SYSTEM_PROMPT = load_prompt("qa", "system")
QA_USER_PROMPT = load_prompt("qa", "user")
DEV_SYSTEM_PROMPT = load_prompt("dev", "system")
DEV_USER_PROMPT = load_prompt("dev", "user")
PM_SYSTEM_PROMPT = load_prompt("pm", "system")
PM_USER_PROMPT = load_prompt("pm", "user")


def _ensure_task_list(output: str) -> List[Dict[str, Any]]:
    try:
        parsed = json.loads(output)
    except json.JSONDecodeError:
        return [{"task_id": str(uuid.uuid4()), "description": output}]

    if isinstance(parsed, list):
        return parsed

    return [{"task_id": str(uuid.uuid4()), "description": output}]


@activity.defn
async def pm_activity(task: Dict[str, Any]) -> Dict[str, Any]:
    """Create an execution plan as a senior PM using architect and analyst input"""
    description = task.get("description", "")

    architect_notes = call_llm(
        ARCHITECT_SYSTEM_PROMPT,
        render_prompt(
            ARCHITECT_USER_PROMPT,
            project_description=description,
        ),
    )
    analyst_notes = call_llm(
        ANALYST_SYSTEM_PROMPT,
        render_prompt(
            ANALYST_USER_PROMPT,
            current_state="",
            event=description,
        ),
    )

    pm_output = call_llm(
        PM_SYSTEM_PROMPT,
        render_prompt(
            PM_USER_PROMPT,
            task_description=description,
            architect_input=architect_notes,
            analyst_input=analyst_notes,
        ),
    )

    try:
        plan = json.loads(pm_output)
    except json.JSONDecodeError:
        plan = {
            "project_goal": description,
            "delivery_summary": pm_output,
            "architect_guidance": [architect_notes],
            "analyst_guidance": [analyst_notes],
            "execution_plan": [
                {
                    "task_id": str(uuid.uuid4()),
                    "title": "Implement requested work",
                    "description": description or pm_output,
                    "assigned_agent": "dev",
                    "dependencies": [],
                    "acceptance_criteria": ["Deliver the requested implementation"],
                }
            ],
        }

    return {
        "event_id": str(uuid.uuid4()),
        "task_id": task.get("task_id", str(uuid.uuid4())),
        "stage": "pm_done",
        "timestamp": int(time.time() * 1000),
        "decision": "continue",
        "project_goal": plan.get("project_goal", description),
        "delivery_summary": plan.get("delivery_summary", ""),
        "architect_guidance": plan.get("architect_guidance", []),
        "analyst_guidance": plan.get("analyst_guidance", []),
        "execution_plan": plan.get("execution_plan", []),
    }


@activity.defn
async def architect_activity(task: Dict[str, Any]) -> Dict[str, Any]:
    """Execute architect agent logic - decompose task into subtasks"""
    description = task.get("description", "")

    output = call_llm(
        ARCHITECT_SYSTEM_PROMPT,
        render_prompt(
            ARCHITECT_USER_PROMPT,
            project_description=description,
        ),
    )
    tasks = _ensure_task_list(output)

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

    code = call_llm(
        DEV_SYSTEM_PROMPT,
        render_prompt(
            DEV_USER_PROMPT,
            task_description=description,
            task_context=task,
        ),
    )

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
        ANALYST_SYSTEM_PROMPT,
        render_prompt(
            ANALYST_USER_PROMPT,
            current_state=state,
            event=tasks_summary,
        ),
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
    code = call_llm(
        DEV_SYSTEM_PROMPT,
        render_prompt(
            DEV_USER_PROMPT,
            task_description=description,
            task_context=task,
        ),
    )

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

    qa_logs = result.stdout + result.stderr
    qa_summary_raw = call_llm(
        QA_SYSTEM_PROMPT,
        render_prompt(
            QA_USER_PROMPT,
            test_logs=qa_logs,
            task_description=description,
        ),
    )
    try:
        qa_summary = json.loads(qa_summary_raw)
    except json.JSONDecodeError:
        qa_summary = {
            "status": "success" if result.returncode == 0 else "fail",
            "failing_tests": [],
            "error_summary": qa_summary_raw,
            "root_cause": "",
            "fix_suggestion": "",
        }

    qa_result = {
        "task_id": task_id,
        "status": "success" if result.returncode == 0 else "fail",
        "logs": qa_logs,
        "summary": qa_summary,
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
