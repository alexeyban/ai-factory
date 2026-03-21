import json
import os
import re
import subprocess
import time
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List

from temporalio import activity
from temporalio.common import RetryPolicy

from shared.git import (
    bootstrap_from_remote,
    branch_exists,
    commit_all,
    current_branch,
    ensure_branch,
    ensure_origin_remote,
    ensure_repo,
    push_branch,
    run_git,
    slugify,
)
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
MAX_PM_RECOVERY_CYCLES = int(os.getenv("PM_MAX_RECOVERY_CYCLES", "2"))
MAX_TASK_EXECUTION_SECONDS = int(os.getenv("MAX_TASK_EXECUTION_SECONDS", "900"))
WORKSPACE_ROOT = Path("/workspace")
PROJECTS_ROOT = WORKSPACE_ROOT / "projects"
AGENT_PLAN_PROMPTS = {
    "pm": PM_SYSTEM_PROMPT,
    "architect": ARCHITECT_SYSTEM_PROMPT,
    "dev": DEV_SYSTEM_PROMPT,
    "qa": QA_SYSTEM_PROMPT,
    "analyst": ANALYST_SYSTEM_PROMPT,
}


def _ensure_task_list(output: str) -> List[Dict[str, Any]]:
    try:
        parsed = json.loads(output)
    except json.JSONDecodeError:
        return [{"task_id": str(uuid.uuid4()), "description": output}]

    if isinstance(parsed, list):
        return parsed

    return [{"task_id": str(uuid.uuid4()), "description": output}]


def _extract_tasks_from_spec(description: str) -> List[Dict[str, Any]]:
    lines = description.splitlines()
    extracted: List[Dict[str, Any]] = []
    current_title: str | None = None
    current_lines: List[str] = []
    seen_ids: set[str] = set()

    def flush() -> None:
        nonlocal current_title, current_lines
        if not current_title:
            return
        title_match = re.match(r"TASK\s+(\d+)\s+(.*)", current_title, re.IGNORECASE)
        task_num = title_match.group(1) if title_match else str(len(extracted) + 1)
        title = title_match.group(2).strip() if title_match else current_title.strip()
        task_id = f"task-{task_num}"
        extracted.append(
            {
                "task_id": task_id,
                "title": title,
                "description": "\n".join(current_lines).strip(),
            }
        )
        current_title = None
        current_lines = []

    for line in lines:
        stripped = line.strip()
        task_match = re.match(r"^\|\s*TASK\s+(\d+)\s+(.+?)\s*\|", stripped, re.IGNORECASE)
        if task_match:
            task_num = task_match.group(1)
            if task_num in seen_ids:
                continue
            flush()
            seen_ids.add(task_num)
            current_title = f"TASK {task_num} {task_match.group(2)}"
            current_lines = [current_title]
            continue
        if current_title:
            current_lines.append(line)

    flush()
    return extracted


def _project_name(task: Dict[str, Any]) -> str:
    return (
        task.get("project_name")
        or task.get("project_goal")
        or task.get("title")
        or task.get("task_id")
        or "project"
    )


def _project_slug(task: Dict[str, Any]) -> str:
    return slugify(_project_name(task), separator="_")


def _project_repo_path(task: Dict[str, Any]) -> Path:
    explicit_path = task.get("project_repo_path")
    if explicit_path:
        return Path(explicit_path)
    return PROJECTS_ROOT / _project_slug(task)


def _project_package_name(task: Dict[str, Any]) -> str:
    return _project_slug(task)


def _ensure_project_scaffold(task: Dict[str, Any], description: str) -> Path:
    repo_path = _project_repo_path(task)
    ensure_repo(repo_path)
    ensure_origin_remote(repo_path, _project_name(task))
    bootstrap_from_remote(repo_path)

    package_name = _project_package_name(task)
    package_dir = repo_path / package_name
    tests_dir = repo_path / "tests"
    documents_dir = repo_path / "documents"

    for directory in (
        package_dir,
        tests_dir,
        documents_dir / "pm",
        documents_dir / "architecture",
        documents_dir / "dev",
        documents_dir / "qa",
        documents_dir / "analyst",
        repo_path / ".ai_factory" / "tasks",
        repo_path / ".ai_factory" / "continuations",
    ):
        directory.mkdir(parents=True, exist_ok=True)

    init_file = package_dir / "__init__.py"
    if not init_file.exists():
        init_file.write_text('"""Project package."""\n')

    readme_file = repo_path / "README.md"
    if not readme_file.exists():
        readme_file.write_text(
            f"# {_project_name(task)}\n\n"
            "Generated by AI Factory.\n\n"
            f"## Scope\n{description.strip() or 'No description provided.'}\n"
        )

    gitignore_file = repo_path / ".gitignore"
    if not gitignore_file.exists():
        gitignore_file.write_text(
            "__pycache__/\n"
            "*.py[cod]\n"
            ".pytest_cache/\n"
            ".venv/\n"
        )

    spec_file = repo_path / "TASK_SPEC.md"
    spec_file.write_text(description or "No project description provided.\n")

    commit_all(repo_path, "chore: initialize project scaffold")
    return repo_path


def _project_python(repo_path: Path) -> Path:
    return repo_path / ".venv" / "bin" / "python"


def _ensure_project_python_env(repo_path: Path) -> Path:
    python_path = _project_python(repo_path)
    if not python_path.exists():
        subprocess.run(["python", "-m", "venv", str(repo_path / ".venv")], check=True)
    subprocess.run([str(python_path), "-m", "pip", "install", "--upgrade", "pip"], check=True, capture_output=True, text=True)
    return python_path


def _install_project_dependencies(repo_path: Path) -> Dict[str, Any]:
    python_path = _ensure_project_python_env(repo_path)
    install_steps: List[str] = []
    subprocess.run(
        [str(python_path), "-m", "pip", "install", "pytest", "requests"],
        check=True,
        capture_output=True,
        text=True,
        timeout=300,
    )
    install_steps.append("pytest requests")

    requirements_path = repo_path / "requirements.txt"
    if requirements_path.exists() and requirements_path.read_text().strip():
        subprocess.run(
            [str(python_path), "-m", "pip", "install", "-r", str(requirements_path)],
            check=True,
            capture_output=True,
            text=True,
            timeout=600,
        )
        install_steps.append("requirements.txt")

    return {"python": str(python_path), "installed": install_steps}


def _sync_branch_to_remote(repo_path: Path, branch_name: str) -> Dict[str, Any]:
    ensure_origin_remote(repo_path, repo_path.name)
    return push_branch(repo_path, branch_name)


def _next_version_path(directory: Path, prefix: str, suffix: str) -> Path:
    existing = sorted(directory.glob(f"{prefix}_v*{suffix}"))
    next_version = len(existing) + 1
    return directory / f"{prefix}_v{next_version:03d}{suffix}"


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n")


def _task_title(task: Dict[str, Any]) -> str:
    return (
        task.get("title")
        or task.get("name")
        or task.get("description")
        or task.get("task_id")
        or "task"
    )


def _task_slug(task: Dict[str, Any]) -> str:
    return slugify(_task_title(task), separator="_")


def _task_branch(task: Dict[str, Any]) -> str:
    return f"task/{_task_slug(task)}"


def _task_module_path(task: Dict[str, Any], repo_path: Path) -> Path:
    description = task.get("description", "")
    module_match = re.search(r"Module:\s*([A-Za-z0-9_./-]+\.py)", description)
    if module_match:
        return repo_path / module_match.group(1)
    package_name = _project_package_name(task)
    return repo_path / package_name / f"{_task_slug(task)}.py"


def _write_markdown(path: Path, title: str, body: str) -> None:
    path.write_text(f"# {title}\n\n{body.strip()}\n")


def _render_acceptance_criteria(criteria: List[str]) -> str:
    if not criteria:
        return "- Not specified"
    return "\n".join(f"- {item}" for item in criteria)


def _render_dependencies(dependencies: List[str]) -> str:
    if not dependencies:
        return "- None"
    return "\n".join(f"- {item}" for item in dependencies)


def _render_execution_plan_markdown(plan: Dict[str, Any]) -> str:
    assignments = plan.get("execution_plan", [])
    lines = [
        "## Project Goal",
        str(plan.get("project_goal", "Not specified")),
        "",
        "## Delivery Summary",
        str(plan.get("delivery_summary", "Not specified")),
        "",
        "## Architect Guidance",
    ]
    architect_guidance = plan.get("architect_guidance", [])
    lines.extend([f"- {item}" for item in architect_guidance] or ["- None"])
    lines.extend(["", "## Analyst Guidance"])
    analyst_guidance = plan.get("analyst_guidance", [])
    lines.extend([f"- {item}" for item in analyst_guidance] or ["- None"])
    lines.extend(["", "## Execution Plan"])

    if not assignments:
        lines.append("No assignments were produced.")
        return "\n".join(lines)

    for index, item in enumerate(assignments, start=1):
        lines.extend(
            [
                f"### Task {index}: {item.get('title', item.get('task_id', 'Untitled Task'))}",
                f"- Task ID: `{item.get('task_id', 'unknown')}`",
                f"- Assigned Agent: `{item.get('assigned_agent', 'unassigned')}`",
                "",
                "#### Description",
                str(item.get("description", "Not specified")),
                "",
                "#### Dependencies",
                _render_dependencies(item.get("dependencies", [])),
                "",
                "#### Acceptance Criteria",
                _render_acceptance_criteria(item.get("acceptance_criteria", [])),
                "",
            ]
        )
    return "\n".join(lines).strip()


def _render_agent_assignments_markdown(plan: Dict[str, Any]) -> str:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for item in plan.get("execution_plan", []):
        grouped.setdefault(item.get("assigned_agent", "unassigned"), []).append(item)

    if not grouped:
        return "## Agent Assignments\n\nNo assignments were produced."

    lines = ["## Agent Assignments", ""]
    for agent in sorted(grouped):
        lines.extend([f"### {agent}", ""])
        for item in grouped[agent]:
            lines.extend(
                [
                    f"#### {item.get('title', item.get('task_id', 'Untitled Task'))}",
                    f"- Task ID: `{item.get('task_id', 'unknown')}`",
                    "",
                    "Description:",
                    str(item.get("description", "Not specified")),
                    "",
                    "Dependencies:",
                    _render_dependencies(item.get("dependencies", [])),
                    "",
                    "Acceptance Criteria:",
                    _render_acceptance_criteria(item.get("acceptance_criteria", [])),
                    "",
                ]
            )
    return "\n".join(lines).strip()


def _task_state_path(repo_path: Path, task_id: str) -> Path:
    return repo_path / ".ai_factory" / "tasks" / f"{task_id}.json"


def _load_task_state(repo_path: Path, task_id: str) -> Dict[str, Any] | None:
    path = _task_state_path(repo_path, task_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


def _save_task_state(repo_path: Path, task_id: str, payload: Dict[str, Any]) -> str:
    path = _task_state_path(repo_path, task_id)
    payload = {
        **payload,
        "task_id": task_id,
        "updated_at": int(time.time() * 1000),
    }
    _write_json(path, payload)
    return str(path)


def _role_documents_dir(repo_path: Path, role: str) -> Path:
    role_dir = "architecture" if role == "architect" else role
    path = repo_path / "documents" / role_dir
    path.mkdir(parents=True, exist_ok=True)
    return path


def _record_agent_plan(
    repo_path: Path,
    role: str,
    task: Dict[str, Any],
    objective: str,
    context: str,
) -> Dict[str, str]:
    system_prompt = AGENT_PLAN_PROMPTS[role]
    role_dir = _role_documents_dir(repo_path, role)
    base_name = f"{role}_plan_{_task_slug(task)}"
    md_path = _next_version_path(role_dir, base_name, ".md")
    json_path = _next_version_path(role_dir, base_name, ".json")
    prompt = (
        "Think through the assigned work and produce a concise execution plan in markdown.\n"
        "Include: objective, detailed steps, risks, dependencies, validation, and what should be done next if work is interrupted.\n\n"
        f"Task:\n{objective}\n\nContext:\n{context}"
    )
    plan_text = call_llm(system_prompt, prompt)
    _write_markdown(md_path, f"{role.upper()} Plan: {_task_title(task)}", plan_text)
    _write_json(
        json_path,
        {
            "role": role,
            "task_id": task.get("task_id"),
            "title": _task_title(task),
            "objective": objective,
            "context": context,
            "plan_markdown": plan_text,
        },
    )
    commit_sha = commit_all(repo_path, f"{role}: record plan for {_task_slug(task)}")
    push_result = (
        _sync_branch_to_remote(repo_path, current_branch(repo_path))
        if commit_sha
        else {"ok": False, "stderr": "nothing to push"}
    )
    return {
        "plan_md": str(md_path),
        "plan_json": str(json_path),
        "commit": commit_sha or "",
        "push": json.dumps(push_result, ensure_ascii=True),
    }


def _record_continuation_plan(
    repo_path: Path,
    role: str,
    task: Dict[str, Any],
    reason: str,
    state: Dict[str, Any],
) -> Dict[str, str]:
    continuation_dir = repo_path / ".ai_factory" / "continuations"
    md_path = _next_version_path(continuation_dir, f"{role}_{_task_slug(task)}_continuation", ".md")
    json_path = _next_version_path(continuation_dir, f"{role}_{_task_slug(task)}_continuation", ".json")
    body = (
        f"## Reason\n{reason}\n\n"
        f"## Task\n{_task_title(task)}\n\n"
        f"## Current State\n{json.dumps(state, indent=2, ensure_ascii=True)}\n\n"
        "## Resume Guidance\nContinue from the last saved branch, artifact, and QA feedback.\n"
    )
    _write_markdown(md_path, f"Continuation Plan: {_task_title(task)}", body)
    _write_json(json_path, {"role": role, "reason": reason, "state": state})
    commit_sha = commit_all(repo_path, f"{role}: save continuation plan for {_task_slug(task)}")
    push_result = (
        _sync_branch_to_remote(repo_path, current_branch(repo_path))
        if commit_sha
        else {"ok": False, "stderr": "nothing to push"}
    )
    return {
        "continuation_md": str(md_path),
        "continuation_json": str(json_path),
        "commit": commit_sha or "",
        "push": json.dumps(push_result, ensure_ascii=True),
    }


def _remaining_time_seconds(start_time: float) -> int:
    return max(0, MAX_TASK_EXECUTION_SECONDS - int(time.monotonic() - start_time))


def _task_timed_out(start_time: float) -> bool:
    return _remaining_time_seconds(start_time) <= 0


def _record_pm_artifacts(task: Dict[str, Any], description: str, plan: Dict[str, Any], architect_notes: str, analyst_notes: str) -> Dict[str, str]:
    repo_path = _ensure_project_scaffold(task, description)
    run_git(repo_path, ["checkout", "main"])

    pm_dir = repo_path / "documents" / "pm"
    brief_path = _next_version_path(pm_dir, "project_brief", ".md")
    plan_md_path = _next_version_path(pm_dir, "delivery_plan", ".md")
    plan_json_path = _next_version_path(pm_dir, "delivery_plan", ".json")
    assignments_md_path = _next_version_path(pm_dir, "agent_assignments", ".md")

    _write_markdown(
        brief_path,
        f"Project Brief: {_project_name(task)}",
        f"## Requested Outcome\n{description}\n\n## Architect Input\n{architect_notes}\n\n## Analyst Input\n{analyst_notes}",
    )
    _write_markdown(
        plan_md_path,
        f"Delivery Plan: {_project_name(task)}",
        _render_execution_plan_markdown(plan),
    )
    _write_markdown(
        assignments_md_path,
        f"Agent Assignments: {_project_name(task)}",
        _render_agent_assignments_markdown(plan),
    )
    _write_json(plan_json_path, plan)

    commit_sha = commit_all(repo_path, "pm: create project brief and delivery plan")
    push_result = _sync_branch_to_remote(repo_path, current_branch(repo_path)) if commit_sha else {"ok": False, "stderr": "nothing to push"}
    return {
        "project_repo_path": str(repo_path),
        "pm_brief": str(brief_path),
        "pm_plan_md": str(plan_md_path),
        "pm_plan_json": str(plan_json_path),
        "pm_agent_assignments_md": str(assignments_md_path),
        "pm_commit": commit_sha,
        "pm_push": json.dumps(push_result, ensure_ascii=True),
    }


def _record_pm_intake(task: Dict[str, Any], description: str) -> Dict[str, str]:
    repo_path = _ensure_project_scaffold(task, description)
    run_git(repo_path, ["checkout", "main"])

    pm_dir = repo_path / "documents" / "pm"
    intake_path = _next_version_path(pm_dir, "intake_request", ".md")
    _write_markdown(
        intake_path,
        f"Incoming Request: {_project_name(task)}",
        description or "No request description provided.",
    )
    commit_sha = commit_all(repo_path, "pm: capture incoming project request")
    push_result = _sync_branch_to_remote(repo_path, current_branch(repo_path)) if commit_sha else {"ok": False, "stderr": "nothing to push"}
    return {
        "pm_intake": str(intake_path),
        "project_repo_path": str(repo_path),
        "pm_intake_commit": commit_sha,
        "pm_intake_push": json.dumps(push_result, ensure_ascii=True),
    }


def _record_architecture_artifacts(task: Dict[str, Any], raw_output: str, tasks: List[Dict[str, Any]]) -> Dict[str, str]:
    repo_path = _ensure_project_scaffold(task, task.get("description", ""))
    run_git(repo_path, ["checkout", "main"])

    architecture_dir = repo_path / "documents" / "architecture"
    architecture_md = _next_version_path(architecture_dir, "architecture", ".md")
    architecture_json = _next_version_path(architecture_dir, "architecture_tasks", ".json")
    architecture_drawio = _next_version_path(architecture_dir, "architecture", ".drawio")

    _write_markdown(
        architecture_md,
        f"Architecture: {_project_name(task)}",
        f"## Solution Notes\n{raw_output}\n\n## Task Breakdown\n{json.dumps(tasks, indent=2, ensure_ascii=True)}",
    )
    _write_json(architecture_json, tasks)
    architecture_drawio.write_text(
        "<mxfile host=\"app.diagrams.net\">\n"
        f"  <diagram name=\"{_project_name(task)} architecture\">\n"
        "    Placeholder diagram generated by AI Factory.\n"
        "  </diagram>\n"
        "</mxfile>\n"
    )

    commit_sha = commit_all(repo_path, "architect: update versioned architecture documents")
    push_result = _sync_branch_to_remote(repo_path, current_branch(repo_path)) if commit_sha else {"ok": False, "stderr": "nothing to push"}
    return {
        "project_repo_path": str(repo_path),
        "architecture_md": str(architecture_md),
        "architecture_json": str(architecture_json),
        "architecture_drawio": str(architecture_drawio),
        "architecture_commit": commit_sha,
        "architecture_push": json.dumps(push_result, ensure_ascii=True),
    }


def _record_architecture_request(task: Dict[str, Any]) -> Dict[str, str]:
    repo_path = _ensure_project_scaffold(task, task.get("description", ""))
    run_git(repo_path, ["checkout", "main"])

    architecture_dir = repo_path / "documents" / "architecture"
    request_path = _next_version_path(architecture_dir, "architecture_request", ".md")
    _write_markdown(
        request_path,
        f"Architecture Request: {_project_name(task)}",
        task.get("description", "") or "No architecture request description provided.",
    )
    commit_sha = commit_all(repo_path, "architect: capture architecture request")
    push_result = _sync_branch_to_remote(repo_path, current_branch(repo_path)) if commit_sha else {"ok": False, "stderr": "nothing to push"}
    return {
        "architecture_request": str(request_path),
        "project_repo_path": str(repo_path),
        "architecture_request_commit": commit_sha,
        "architecture_request_push": json.dumps(push_result, ensure_ascii=True),
    }


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


def _prepare_task_branch(repo_path: Path, branch_name: str, attempt_number: int) -> None:
    if attempt_number == 1 or not branch_exists(repo_path, branch_name):
        ensure_branch(repo_path, branch_name, base_branch="main")
        return
    run_git(repo_path, ["checkout", branch_name])


def _generate_dev_artifact(
    task: Dict[str, Any],
    task_id: str,
    description: str,
    attempt_number: int,
    qa_feedback: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    repo_path = _ensure_project_scaffold(task, task.get("project_description", description))
    branch_name = _task_branch(task)
    _prepare_task_branch(repo_path, branch_name, attempt_number)
    plan_artifacts = _record_agent_plan(
        repo_path,
        "dev",
        task,
        description,
        json.dumps(
            {
                "attempt_number": attempt_number,
                "qa_feedback": qa_feedback or {},
                "branch": branch_name,
            },
            indent=2,
            ensure_ascii=True,
        ),
    )

    code = call_llm(
        DEV_SYSTEM_PROMPT,
        _build_dev_prompt(task, description, attempt_number, qa_feedback),
    )

    file_path = _task_module_path(task, repo_path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(code)

    docs_dir = repo_path / "documents" / "pm"
    task_doc = _next_version_path(docs_dir, f"task_{_task_slug(task)}_implementation", ".md")
    _write_markdown(
        task_doc,
        f"Implementation: {_task_title(task)}",
        f"## Branch\n`{branch_name}`\n\n## Attempt\n{attempt_number}\n\n## Artifact\n`{file_path.relative_to(repo_path)}`\n",
    )

    commit_sha = commit_all(
        repo_path,
        f"dev: implement {_task_slug(task)} attempt {attempt_number}",
    )
    push_result = _sync_branch_to_remote(repo_path, branch_name) if commit_sha else {"ok": False, "stderr": "nothing to push"}

    return {
        "task_id": task_id,
        "status": "success",
        "artifact": str(file_path),
        "code": code,
        "attempt": attempt_number,
        "mode": "autofix" if qa_feedback else "initial",
        "branch": branch_name,
        "commit": commit_sha,
        "push": push_result,
        "project_repo_path": str(repo_path),
        "implementation_note": str(task_doc),
        "plan": plan_artifacts,
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


def _run_pytest(repo_path: Path, timeout_seconds: int = 120) -> subprocess.CompletedProcess:
    python_path = _ensure_project_python_env(repo_path)
    return subprocess.run(
        [str(python_path), "-m", "pytest", str(repo_path), "-v"],
        capture_output=True,
        text=True,
        timeout=max(30, timeout_seconds),
    )


def _run_qa_for_artifact(
    task: Dict[str, Any], task_id: str, description: str, artifact: str | None, attempt_number: int, remaining_seconds: int | None = None
) -> Dict[str, Any]:
    repo_path = _ensure_project_scaffold(task, task.get("project_description", description))
    branch_name = _task_branch(task)
    run_git(repo_path, ["checkout", branch_name])
    plan_artifacts = _record_agent_plan(
        repo_path,
        "qa",
        task,
        description,
        json.dumps(
            {
                "attempt_number": attempt_number,
                "artifact": artifact,
                "branch": branch_name,
            },
            indent=2,
            ensure_ascii=True,
        ),
    )

    artifact_path = Path(artifact) if artifact else None
    if not artifact or not artifact_path or not artifact_path.exists():
        summary = {
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
        }
        qa_logs = "No artifact found"
        status = "fail"
    else:
        dependency_info = _install_project_dependencies(repo_path)
        result = _run_pytest(repo_path, timeout_seconds=remaining_seconds or 120)
        qa_logs = (
            f"Dependency install: {json.dumps(dependency_info, ensure_ascii=True)}\n\n"
            + result.stdout
            + result.stderr
        )
        status = "success" if result.returncode == 0 else "fail"
        summary = _summarize_qa_result(description, qa_logs, status)

    qa_dir = repo_path / "documents" / "qa"
    qa_md_path = _next_version_path(qa_dir, f"qa_report_{_task_slug(task)}", ".md")
    qa_json_path = _next_version_path(qa_dir, f"qa_report_{_task_slug(task)}", ".json")
    _write_markdown(
        qa_md_path,
        f"QA Report: {_task_title(task)}",
        f"## Branch\n`{branch_name}`\n\n## Attempt\n{attempt_number}\n\n## Status\n{status}\n\n## Logs\n```\n{qa_logs[:12000]}\n```",
    )
    _write_json(qa_json_path, summary)
    commit_sha = commit_all(repo_path, f"qa: validate {_task_slug(task)} attempt {attempt_number}")
    push_result = _sync_branch_to_remote(repo_path, branch_name) if commit_sha else {"ok": False, "stderr": "nothing to push"}

    merge_commit = None
    merge_push_result = None
    if status == "success":
        run_git(repo_path, ["checkout", "main"])
        run_git(repo_path, ["merge", "--no-ff", branch_name, "-m", f"merge: {_task_slug(task)} after qa approval"])
        merge_commit = run_git(repo_path, ["rev-parse", "HEAD"]).stdout.strip()
        merge_push_result = _sync_branch_to_remote(repo_path, "main")

    return {
        "task_id": task_id,
        "status": status,
        "logs": qa_logs,
        "summary": summary,
        "branch": branch_name,
        "commit": commit_sha,
        "push": push_result,
        "merge_commit": merge_commit,
        "merge_push": merge_push_result,
        "qa_report_md": str(qa_md_path),
        "qa_report_json": str(qa_json_path),
        "project_repo_path": str(repo_path),
        "plan": plan_artifacts,
    }


def _normalize_task(task: Dict[str, Any], project_context: Dict[str, Any]) -> Dict[str, Any]:
    normalized = dict(task)
    normalized.setdefault("task_id", str(uuid.uuid4()))
    normalized["project_name"] = project_context["project_name"]
    normalized["project_repo_path"] = project_context["project_repo_path"]
    normalized["project_description"] = project_context["project_description"]
    return normalized


def _needs_recovery(results: List[Dict[str, Any]]) -> bool:
    for result in results:
        if result.get("status") not in {"success", "complete"}:
            return True
        qa_status = result.get("qa", {}).get("status")
        if qa_status and qa_status != "success":
            return True
        if result.get("error"):
            return True
    return False


def _build_recovery_request(initial_task: Dict[str, Any], results: List[Dict[str, Any]], cycle: int) -> Dict[str, Any]:
    failures = [
        {
            "task_id": result.get("task_id"),
            "status": result.get("status"),
            "qa": result.get("qa", {}),
            "error": result.get("error"),
        }
        for result in results
        if result.get("status") != "success" or result.get("qa", {}).get("status") not in {None, "success"}
    ]
    return {
        **initial_task,
        "recovery_cycle": cycle,
        "failure_summary": failures,
        "description": (
            f"{initial_task.get('description', '')}\n\n"
            f"Recovery cycle: {cycle}\n"
            f"Blocked tasks:\n{json.dumps(failures, indent=2, ensure_ascii=True)}"
        ),
    }


@activity.defn
async def pm_activity(task: Dict[str, Any]) -> Dict[str, Any]:
    description = task.get("description", "")
    project_name = _project_name(task)
    project_repo_path = _ensure_project_scaffold(task, description)
    intake_artifacts = _record_pm_intake(task, description)

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

    artifact_paths = _record_pm_artifacts(task, description, plan, architect_notes, analyst_notes)

    return {
        "event_id": str(uuid.uuid4()),
        "task_id": task.get("task_id", str(uuid.uuid4())),
        "stage": "pm_done",
        "timestamp": int(time.time() * 1000),
        "decision": "continue",
        "project_name": project_name,
        "project_repo_path": str(project_repo_path),
        "project_goal": plan.get("project_goal", description),
        "delivery_summary": plan.get("delivery_summary", ""),
        "architect_guidance": plan.get("architect_guidance", []),
        "analyst_guidance": plan.get("analyst_guidance", []),
        "execution_plan": plan.get("execution_plan", []),
        "artifacts": {**intake_artifacts, **artifact_paths},
    }


@activity.defn
async def architect_activity(task: Dict[str, Any]) -> Dict[str, Any]:
    description = task.get("description", "")
    request_artifacts = _record_architecture_request(task)

    output = call_llm(
        ARCHITECT_SYSTEM_PROMPT,
        render_prompt(
            ARCHITECT_USER_PROMPT,
            project_description=description,
        ),
    )
    tasks = _ensure_task_list(output)
    fallback_tasks = _extract_tasks_from_spec(description)
    if len(fallback_tasks) > len(tasks):
        tasks = fallback_tasks
    artifact_paths = _record_architecture_artifacts(task, output, tasks)
    project_context = {
        "project_name": _project_name(task),
        "project_repo_path": artifact_paths["project_repo_path"],
        "project_description": description,
    }
    normalized_tasks = [_normalize_task(item, project_context) for item in tasks]

    return {
        "event_id": str(uuid.uuid4()),
        "task_id": task.get("task_id", str(uuid.uuid4())),
        "stage": "architect_done",
        "timestamp": int(time.time() * 1000),
        "decision": "continue",
        "tasks": normalized_tasks,
        "artifacts": {**request_artifacts, **artifact_paths},
    }


@activity.defn
async def pm_recovery_activity(task: Dict[str, Any]) -> Dict[str, Any]:
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
            event=json.dumps(task.get("failure_summary", []), indent=2, ensure_ascii=True),
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
            "project_goal": task.get("project_name", "project"),
            "delivery_summary": pm_output,
            "architect_guidance": [architect_notes],
            "analyst_guidance": [analyst_notes],
            "execution_plan": _extract_tasks_from_spec(description),
        }

    repo_path = _ensure_project_scaffold(task, description)
    run_git(repo_path, ["checkout", "main"])
    recovery_dir = repo_path / "documents" / "pm"
    recovery_md = _next_version_path(recovery_dir, "recovery_plan", ".md")
    recovery_json = _next_version_path(recovery_dir, "recovery_plan", ".json")
    _write_markdown(
        recovery_md,
        f"Recovery Plan: {_project_name(task)}",
        json.dumps(plan, indent=2, ensure_ascii=True),
    )
    _write_json(recovery_json, plan)
    commit_sha = commit_all(repo_path, f"pm: recovery plan cycle {task.get('recovery_cycle', 1)}")
    push_result = _sync_branch_to_remote(repo_path, current_branch(repo_path)) if commit_sha else {"ok": False, "stderr": "nothing to push"}

    return {
        "project_goal": plan.get("project_goal", description),
        "delivery_summary": plan.get("delivery_summary", ""),
        "architect_guidance": plan.get("architect_guidance", []),
        "analyst_guidance": plan.get("analyst_guidance", []),
        "execution_plan": plan.get("execution_plan", []),
        "artifacts": {
            "recovery_plan_md": str(recovery_md),
            "recovery_plan_json": str(recovery_json),
            "commit": commit_sha,
            "push": push_result,
        },
    }


@activity.defn
async def dev_activity(task: Dict[str, Any]) -> Dict[str, Any]:
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
    task_id = task.get("task_id", str(uuid.uuid4()))
    artifact = task.get("artifact", "")

    return _run_qa_for_artifact(
        task,
        task_id,
        task.get("description", ""),
        artifact,
        int(task.get("attempt_number", 1)),
    )


@activity.defn
async def analyst_activity(tasks: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not tasks:
        return {"status": "skipped", "reason": "no tasks"}

    first_task = tasks[0]
    repo_path = _ensure_project_scaffold(first_task, first_task.get("project_description", ""))
    run_git(repo_path, ["checkout", "main"])
    plan_artifacts = _record_agent_plan(
        repo_path,
        "analyst",
        {
            "task_id": "project-analysis",
            "title": f"{first_task.get('project_name', 'project')} analysis",
            "description": "Summarize current delivery state and open risks.",
        },
        "Summarize project delivery state and unresolved issues.",
        json.dumps(tasks, indent=2, ensure_ascii=True),
    )

    analyst_dir = repo_path / "documents" / "analyst"
    state_file = _next_version_path(analyst_dir, "project_state", ".md")

    tasks_summary = "\n".join(
        [
            f"- {_task_title(t)}: {t.get('status', 'unknown')} on {t.get('qa', {}).get('branch', t.get('branch', 'n/a'))}"
            for t in tasks
        ]
    )

    new_state = call_llm(
        ANALYST_SYSTEM_PROMPT,
        render_prompt(
            ANALYST_USER_PROMPT,
            current_state="",
            event=tasks_summary,
        ),
    )

    state_file.write_text(new_state)
    commit_sha = commit_all(repo_path, "analyst: update project state")
    push_result = _sync_branch_to_remote(repo_path, current_branch(repo_path)) if commit_sha else {"ok": False, "stderr": "nothing to push"}

    return {
        "status": "complete",
        "state": new_state,
        "artifact": str(state_file),
        "commit": commit_sha,
        "push": push_result,
        "project_repo_path": str(repo_path),
        "plan": plan_artifacts,
    }


@activity.defn
async def process_single_task(task: Dict[str, Any]) -> Dict[str, Any]:
    task_id = task.get("task_id", str(uuid.uuid4()))
    description = task.get("description", "")
    start_time = time.monotonic()
    repo_path = _ensure_project_scaffold(task, task.get("project_description", description))
    previous_state = _load_task_state(repo_path, task_id)

    if previous_state and previous_state.get("status") == "success":
        return previous_state.get("result", previous_state)

    healing_history: List[Dict[str, Any]] = []
    if previous_state and previous_state.get("healing_history"):
        healing_history = previous_state["healing_history"]

    task_plan = _record_agent_plan(
        repo_path,
        "dev",
        task,
        description,
        json.dumps(
            {
                "resume_state": previous_state or {},
                "max_task_seconds": MAX_TASK_EXECUTION_SECONDS,
            },
            indent=2,
            ensure_ascii=True,
        ),
    )
    state_file = _save_task_state(
        repo_path,
        task_id,
        {
            "status": "in_progress",
            "branch": _task_branch(task),
            "task_plan": task_plan,
            "healing_history": healing_history,
        },
    )

    if _task_timed_out(start_time):
        continuation = _record_continuation_plan(
            repo_path,
            "pm",
            task,
            "Task exceeded the 15-minute budget before execution started.",
            {"previous_state": previous_state or {}},
        )
        result = {
            "task_id": task_id,
            "project_name": task.get("project_name"),
            "project_repo_path": task.get("project_repo_path"),
            "status": "needs_continuation",
            "attempts": len(healing_history),
            "self_healing_applied": len(healing_history) > 1,
            "max_self_healing_attempts": MAX_SELF_HEALING_ATTEMPTS,
            "continuation": continuation,
            "task_state_file": state_file,
        }
        _save_task_state(repo_path, task_id, {"status": "needs_continuation", "result": result})
        return result

    next_attempt = 1
    qa_feedback = None
    if previous_state:
        next_attempt = int(previous_state.get("attempts", len(healing_history))) + 1
        qa_feedback = previous_state.get("last_qa_feedback")

    dev_result = _generate_dev_artifact(
        task=task,
        task_id=task_id,
        description=description,
        attempt_number=next_attempt,
        qa_feedback=qa_feedback,
    )
    qa_result = _run_qa_for_artifact(
        task,
        task_id,
        description,
        dev_result["artifact"],
        next_attempt,
        remaining_seconds=_remaining_time_seconds(start_time),
    )
    healing_history.append({"attempt": next_attempt, "dev": dev_result, "qa": qa_result})
    _save_task_state(
        repo_path,
        task_id,
        {
            "status": qa_result.get("status"),
            "attempts": len(healing_history),
            "branch": dev_result.get("branch"),
            "last_dev_result": dev_result,
            "last_qa_result": qa_result,
            "last_qa_feedback": {
                "previous_qa_status": qa_result.get("status"),
                "logs": qa_result.get("logs", ""),
                "summary": qa_result.get("summary", {}),
            },
            "healing_history": healing_history,
            "task_state_file": state_file,
        },
    )

    while (
        qa_result.get("status") == "fail"
        and len(healing_history) <= MAX_SELF_HEALING_ATTEMPTS
        and not _task_timed_out(start_time)
    ):
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
        qa_result = _run_qa_for_artifact(
            task,
            task_id,
            description,
            dev_result["artifact"],
            next_attempt,
            remaining_seconds=_remaining_time_seconds(start_time),
        )
        healing_history.append(
            {"attempt": next_attempt, "dev": dev_result, "qa": qa_result}
        )
        _save_task_state(
            repo_path,
            task_id,
            {
                "status": qa_result.get("status"),
                "attempts": len(healing_history),
                "branch": dev_result.get("branch"),
                "last_dev_result": dev_result,
                "last_qa_result": qa_result,
                "last_qa_feedback": autofix_feedback,
                "healing_history": healing_history,
                "task_state_file": state_file,
            },
        )

    continuation = None
    final_status = "success" if qa_result.get("status") == "success" else "fail"
    if _task_timed_out(start_time):
        continuation = _record_continuation_plan(
            repo_path,
            "pm",
            task,
            "Task exceeded the 15-minute execution budget and must continue later.",
            {
                "attempts": len(healing_history),
                "last_dev_result": dev_result,
                "last_qa_result": qa_result,
            },
        )
        final_status = "needs_continuation"

    result = {
        "task_id": task_id,
        "project_name": task.get("project_name"),
        "project_repo_path": task.get("project_repo_path"),
        "status": final_status,
        "attempts": len(healing_history),
        "self_healing_applied": len(healing_history) > 1,
        "max_self_healing_attempts": MAX_SELF_HEALING_ATTEMPTS,
        "dev": dev_result,
        "qa": qa_result,
        "healing_history": healing_history,
        "task_state_file": state_file,
        "continuation": continuation,
        "task_plan": task_plan,
    }
    _save_task_state(repo_path, task_id, {"status": final_status, "result": result})
    return result


@activity.defn
async def process_all_tasks(tasks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    for task in tasks:
        try:
            results.append(await process_single_task(task))
        except Exception as exc:
            repo_path = _ensure_project_scaffold(task, task.get("project_description", task.get("description", "")))
            continuation = _record_continuation_plan(
                repo_path,
                "pm",
                task,
                f"Task processing failed with exception: {exc}",
                {"error": str(exc)},
            )
            state_file = _save_task_state(
                repo_path,
                task.get("task_id", str(uuid.uuid4())),
                {
                    "status": "error",
                    "error": str(exc),
                    "continuation": continuation,
                },
            )
            results.append(
                {
                    "task_id": task.get("task_id", str(uuid.uuid4())),
                    "project_name": task.get("project_name"),
                    "project_repo_path": task.get("project_repo_path"),
                    "status": "error",
                    "error": str(exc),
                    "continuation": continuation,
                    "task_state_file": state_file,
                }
            )
    return results
