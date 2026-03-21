import asyncio
import json
import os
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
MAX_SELF_HEALING_ATTEMPTS = int(os.getenv("DEV_QA_MAX_FIX_ATTEMPTS", "2"))


def _ensure_task_list(output: str) -> List[Dict[str, Any]]:
    try:
        parsed = json.loads(output)
    except json.JSONDecodeError:
        return [{"task_id": str(uuid.uuid4()), "description": output}]

    if isinstance(parsed, list):
        return parsed

    return [{"task_id": str(uuid.uuid4()), "description": output}]


def _build_dev_prompt(
    task: Dict[str, Any],
    description: str,
    attempt_number: int,
    qa_feedback: Dict[str, Any] | None = None,
) -> str:
    qa_feedback_text = "No QA feedback yet. Produce the initial implementation."
    if qa_feedback:
        qa_feedback_text = json.dumps(qa_feedback, indent=2, ensure_ascii=True)

    return render_prompt(
        DEV_USER_PROMPT,
        task_description=description,
        task_context=json.dumps(task, indent=2, ensure_ascii=True),
        attempt_number=attempt_number,
        qa_feedback=qa_feedback_text,
    )


def _generate_dev_artifact(
    task: Dict[str, Any],
    task_id: str,
    description: str,
    attempt_number: int,
    qa_feedback: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    code = call_llm(
        DEV_SYSTEM_PROMPT,
        _build_dev_prompt(task, description, attempt_number, qa_feedback),
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
        "attempt": attempt_number,
        "mode": "autofix" if qa_feedback else "initial",
    }


def _summarize_qa_result(task_description: str, qa_logs: str, status: str) -> Dict[str, Any]:
    qa_summary_raw = call_llm(
        QA_SYSTEM_PROMPT,
        render_prompt(
            QA_USER_PROMPT,
            test_logs=qa_logs,
            task_description=task_description,
        ),
    )
    try:
        qa_summary = json.loads(qa_summary_raw)
    except json.JSONDecodeError:
        qa_summary = {
            "status": status,
            "failing_tests": [],
            "error_summary": qa_summary_raw,
            "root_cause": "",
            "fix_suggestion": "",
            "coverage_assessment": {
                "unit_tests": "unknown",
                "integration_tests": "unknown",
                "end_to_end_tests": "unknown",
            },
            "quality_assessment": {
                "confidence": "low",
                "notes": "QA summary was not valid JSON.",
            },
        }
    return qa_summary


def _run_qa_for_artifact(
    task_id: str, description: str, artifact: str | None
) -> Dict[str, Any]:
    artifact_path = Path(artifact) if artifact else None
    if not artifact or not artifact_path or not artifact_path.exists():
        return {
            "task_id": task_id,
            "status": "skipped",
            "logs": "No artifact found",
            "summary": {
                "status": "fail",
                "failing_tests": [],
                "error_summary": "No artifact found",
                "root_cause": "Developer artifact was missing before QA execution.",
                "fix_suggestion": "Ensure dev stage writes the target artifact before QA runs.",
                "coverage_assessment": {
                    "unit_tests": "unknown",
                    "integration_tests": "unknown",
                    "end_to_end_tests": "unknown",
                },
                "quality_assessment": {
                    "confidence": "high",
                    "notes": "QA could not execute because the artifact path was missing.",
                },
            },
        }

    workspace = Path("/workspace")
    result = subprocess.run(
        ["python", "-m", "pytest", str(workspace), "-v"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    qa_logs = result.stdout + result.stderr
    status = "success" if result.returncode == 0 else "fail"

    return {
        "task_id": task_id,
        "status": status,
        "logs": qa_logs,
        "summary": _summarize_qa_result(description, qa_logs, status),
    }


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

    return _generate_dev_artifact(
        task=task,
        task_id=task_id,
        description=description,
        attempt_number=int(task.get("attempt_number", 1)),
        qa_feedback=task.get("qa_feedback"),
    )


@activity.defn
async def qa_activity(task: Dict[str, Any]) -> Dict[str, Any]:
    """Execute QA agent logic - run tests on artifact"""
    task_id = task.get("task_id", str(uuid.uuid4()))
    artifact = task.get("artifact", "")

    workspace = Path("/workspace")
    if not artifact:
        files = list(workspace.glob("*.py"))
        artifact = str(files[0]) if files else None

    return _run_qa_for_artifact(task_id, task.get("description", ""), artifact)


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

    healing_history: List[Dict[str, Any]] = []

    dev_result = _generate_dev_artifact(
        task=task,
        task_id=task_id,
        description=description,
        attempt_number=1,
    )
    qa_result = _run_qa_for_artifact(task_id, description, dev_result["artifact"])
    healing_history.append({"attempt": 1, "dev": dev_result, "qa": qa_result})

    while qa_result.get("status") == "fail" and len(healing_history) <= MAX_SELF_HEALING_ATTEMPTS:
        next_attempt = len(healing_history) + 1
        autofix_feedback = {
            "previous_qa_status": qa_result.get("status"),
            "logs": qa_result.get("logs", ""),
            "summary": qa_result.get("summary", {}),
        }
        dev_result = _generate_dev_artifact(
            task=task,
            task_id=task_id,
            description=description,
            attempt_number=next_attempt,
            qa_feedback=autofix_feedback,
        )
        qa_result = _run_qa_for_artifact(task_id, description, dev_result["artifact"])
        healing_history.append(
            {"attempt": next_attempt, "dev": dev_result, "qa": qa_result}
        )

    return {
        "task_id": task_id,
        "status": "success" if qa_result.get("status") == "success" else "fail",
        "attempts": len(healing_history),
        "self_healing_applied": len(healing_history) > 1,
        "max_self_healing_attempts": MAX_SELF_HEALING_ATTEMPTS,
        "dev": dev_result,
        "qa": qa_result,
        "healing_history": healing_history,
    }


@activity.defn
async def process_all_tasks(tasks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Process all tasks in parallel"""
    results = await asyncio.gather(
        *[process_single_task(task) for task in tasks],
        return_exceptions=True,
    )

    return [{"error": str(r)} if isinstance(r, Exception) else r for r in results]
