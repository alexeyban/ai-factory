import json
import logging
import os
import re
import subprocess
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List

from temporalio import activity
from temporalio.common import RetryPolicy

from agents.decomposer.agent import DecomposerAgent, normalize_task_contract
from shared.git import (
    _github_api_token,
    _github_repo_slug,
    bootstrap_from_remote,
    branch_exists,
    commit_all,
    create_and_merge_github_pr,
    current_branch,
    ensure_branch,
    ensure_origin_remote,
    ensure_repo,
    push_branch,
    run_git,
    slugify,
    get_or_create_project_path,
)
from shared.llm import call_llm
from shared.tools import (
    ToolResult,
    build_file_tree,
    build_import_map,
    get_task_error_history,
    run_lint,
    run_pytest_with_coverage,
    run_typecheck,
    syntax_check,
)
from shared.prompts.loader import load_prompt, render_prompt

LOGGER = logging.getLogger(__name__)


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
DECOMPOSER_AGENT = DecomposerAgent()
MAX_SELF_HEALING_ATTEMPTS = int(os.getenv("DEV_QA_MAX_FIX_ATTEMPTS", "2"))
MAX_PM_RECOVERY_CYCLES = int(os.getenv("PM_MAX_RECOVERY_CYCLES", "2"))
MAX_TASK_EXECUTION_SECONDS = int(os.getenv("MAX_TASK_EXECUTION_SECONDS", "1800"))
WORKSPACE_ROOT = Path("/workspace")
PROJECTS_ROOT = WORKSPACE_ROOT / "projects"
AGENT_PLAN_PROMPTS = {
    "pm": PM_SYSTEM_PROMPT,
    "architect": ARCHITECT_SYSTEM_PROMPT,
    "dev": DEV_SYSTEM_PROMPT,
    "qa": QA_SYSTEM_PROMPT,
    "analyst": ANALYST_SYSTEM_PROMPT,
}


def _load_activity_input(task: Dict[str, Any]) -> Dict[str, Any]:
    if "_context_file" in task:
        try:
            filepath = Path(task["_context_file"])
            if filepath.exists():
                data = json.loads(filepath.read_text(encoding="utf-8"))
                LOGGER.info(
                    "[activity] Loaded context | file=%s | workflow=%s",
                    filepath.name,
                    task.get("_workflow_id", "unknown"),
                )
                for key in ["_context_file", "_workflow_id"]:
                    data.pop(key, None)
                return data
        except Exception as e:
            LOGGER.warning("[activity] Failed to load context file: %s", e)
    return task


def _wrap_activity_result(
    workflow_id: str,
    stage: str,
    result: Dict[str, Any],
    start_time: datetime | None = None,
) -> Dict[str, Any]:
    """Save full result to disk and return a slim envelope through Temporal.

    The full payload lives in a JSON file under /workspace/.ai_factory/contexts/.
    Only a small routing envelope (< 1 KB) flows through Temporal's event history,
    keeping all messages well under the 512 KB payload limit.

    Activities that receive a result as input call _load_activity_input() which
    transparently reads the context file before the activity body runs.
    Workflows call _load_result_from_file() (via workflow.side_effect) to read
    the file when they need the full data.
    """
    duration_ms = None
    if start_time:
        duration_ms = int((datetime.now() - start_time).total_seconds() * 1000)

    output_dir = Path("/workspace/.ai_factory/contexts") / workflow_id
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / f"output_{stage}.json"

    context_data = {
        "_meta": {
            "workflow_id": workflow_id,
            "stage": stage,
            "saved_at": datetime.now().isoformat(),
            "duration_ms": duration_ms,
            "version": "1.0",
        },
        **result,
    }

    try:
        output_file.write_text(
            json.dumps(context_data, indent=2, ensure_ascii=True), encoding="utf-8"
        )
        LOGGER.info(
            "[activity] Saved result | workflow=%s | stage=%s | duration=%sms | file=%s",
            workflow_id,
            stage,
            duration_ms or "?",
            output_file.name,
        )
    except Exception as e:
        LOGGER.error("[activity] Failed to save result: %s", e)

    # Slim envelope — only routing/status fields pass through Temporal.
    qa = result.get("qa") or {}
    return {
        "_context_file": str(output_file),
        "_workflow_id": workflow_id,
        "task_id": result.get("task_id"),
        "title": result.get("title") or result.get("name"),
        "stage": stage,
        "decision": result.get("decision", "continue"),
        "status": result.get("status"),
        "project_name": result.get("project_name"),
        "project_repo_path": result.get("project_repo_path"),
        # task-level QA status for recovery logic (avoids loading full file)
        "qa_status": qa.get("status") if isinstance(qa, dict) else None,
        "error": result.get("error"),
        # task count for architect/decomposer logging
        "task_count": len(result.get("tasks", [])) if "tasks" in result else None,
    }


def _ensure_task_list(output: str) -> List[Dict[str, Any]]:
    try:
        parsed = json.loads(output)
    except json.JSONDecodeError:
        return [{"task_id": str(uuid.uuid4()), "description": output}]

    if isinstance(parsed, list):
        return parsed

    if isinstance(parsed, dict):
        if isinstance(parsed.get("tasks"), list):
            return parsed["tasks"]
        if isinstance(parsed.get("execution_plan"), list):
            return parsed["execution_plan"]

    return [{"task_id": str(uuid.uuid4()), "description": output}]


def _normalize_task_list(
    tasks: List[Dict[str, Any]], project_context: Dict[str, Any] | None = None
) -> List[Dict[str, Any]]:
    return [
        _normalize_task(task, project_context=project_context or {}) for task in tasks
    ]


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
        task_match = re.match(
            r"^\|\s*TASK\s+(\d+)\s+(.+?)\s*\|", stripped, re.IGNORECASE
        )
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

    github_url = task.get("github_url")
    project_name = task.get("project_name") or _project_slug(task)

    if github_url or project_name:
        return get_or_create_project_path(
            github_url=github_url, project_name=project_name
        )

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
        gitignore_file.write_text("__pycache__/\n*.py[cod]\n.pytest_cache/\n.venv/\n")

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
    subprocess.run(
        [str(python_path), "-m", "pip", "install", "--upgrade", "pip"],
        check=True,
        capture_output=True,
        text=True,
    )
    return python_path


def _install_project_dependencies(repo_path: Path) -> Dict[str, Any]:
    python_path = _ensure_project_python_env(repo_path)
    install_steps: List[str] = []
    subprocess.run(
        [str(python_path), "-m", "pip", "install",
         "pytest", "requests", "ruff", "mypy", "pytest-cov"],
        check=True,
        capture_output=True,
        text=True,
        timeout=300,
    )
    install_steps.append("pytest requests ruff mypy pytest-cov")

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
    try:
        result = push_branch(repo_path, branch_name)
    except Exception as exc:
        LOGGER.warning("[git] Push raised exception for branch %s: %s", branch_name, exc)
        return {"ok": False, "stderr": str(exc), "transport": "error"}
    if not result.get("ok"):
        LOGGER.warning(
            "[git] Push to GitHub failed for branch %s (%s). "
            "Code is committed locally in %s. "
            "To push, add your SSH key to GitHub or set GITHUB_TOKEN in .env. "
            "stderr: %s",
            branch_name,
            result.get("transport", "?"),
            repo_path,
            result.get("stderr", "")[:200],
        )
    return result


def _strip_code_fences(text: str) -> str:
    """Remove markdown code fences if the LLM wrapped the response."""
    import re as _re
    match = _re.search(r"```(?:python|py)?\n(.*?)```", text, _re.DOTALL)
    if match and match.group(1).strip():
        return match.group(1)
    # Single-backtick fallback
    match = _re.search(r"`{3}(.*?)`{3}", text, _re.DOTALL)
    if match and match.group(1).strip():
        return match.group(1)
    return text


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
    title = task.get("title") or task.get("name") or task.get("task_id") or "task"
    return slugify(title[:60], separator="_")


def _task_branch(task: Dict[str, Any]) -> str:
    # Use task_id as branch name fallback to avoid giant slugs from description fields
    title = task.get("title") or task.get("name")
    if title:
        slug = slugify(title[:60], separator="_")
    else:
        slug = (task.get("task_id") or "task")[:50]
    return f"task/{slug}"


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
    md_path = _next_version_path(
        continuation_dir, f"{role}_{_task_slug(task)}_continuation", ".md"
    )
    json_path = _next_version_path(
        continuation_dir, f"{role}_{_task_slug(task)}_continuation", ".json"
    )
    body = (
        f"## Reason\n{reason}\n\n"
        f"## Task\n{_task_title(task)}\n\n"
        f"## Current State\n{json.dumps(state, indent=2, ensure_ascii=True)}\n\n"
        "## Resume Guidance\nContinue from the last saved branch, artifact, and QA feedback.\n"
    )
    _write_markdown(md_path, f"Continuation Plan: {_task_title(task)}", body)
    _write_json(json_path, {"role": role, "reason": reason, "state": state})
    commit_sha = commit_all(
        repo_path, f"{role}: save continuation plan for {_task_slug(task)}"
    )
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


def _estimate_tokens(text: str) -> int:
    return DECOMPOSER_AGENT.estimate_tokens(text)


def _decompose_task(task: Dict[str, Any], project_context: str) -> List[Dict[str, Any]]:
    return DECOMPOSER_AGENT.decompose(task, project_context=project_context)


@activity.defn
async def decomposer_activity(task: Dict[str, Any]) -> Dict[str, Any]:
    start_time = datetime.now()
    task = _load_activity_input(task)
    workflow_id = task.get("_workflow_id", "unknown")
    project_context_dict = {
        "project_name": task.get("project_name"),
        "project_repo_path": task.get("project_repo_path"),
        "github_url": task.get("github_url"),
    }
    project_context = json.dumps(project_context_dict, ensure_ascii=True)
    tasks = _normalize_task_list(_decompose_task(task, project_context), task)
    result = {
        "task_id": task.get("task_id"),
        "stage": "decomposer_done",
        "status": "success",
        "decision": "continue",
        "tasks": tasks,
        **project_context_dict,
    }
    return _wrap_activity_result(workflow_id, f"decomposer_{task.get('task_id', 'unknown')}", result, start_time)


def _expand_execution_plan(
    tasks: List[Dict[str, Any]], project_context: str
) -> List[Dict[str, Any]]:
    expanded: List[Dict[str, Any]] = []
    for task in tasks:
        desc = json.dumps(task.get("description", task), ensure_ascii=True)
        if _estimate_tokens(desc) > 8000:
            expanded.extend(_decompose_task(task, project_context))
        else:
            expanded.append(task)
    return expanded


def _task_timed_out(start_time: float) -> bool:
    return _remaining_time_seconds(start_time) <= 0


def _record_pm_artifacts(
    task: Dict[str, Any],
    description: str,
    plan: Dict[str, Any],
    architect_notes: str,
    analyst_notes: str,
) -> Dict[str, Any]:
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
    push_result = (
        _sync_branch_to_remote(repo_path, current_branch(repo_path))
        if commit_sha
        else {"ok": False, "stderr": "nothing to push"}
    )
    return {
        "project_repo_path": str(repo_path),
        "pm_brief": str(brief_path),
        "pm_plan_md": str(plan_md_path),
        "pm_plan_json": str(plan_json_path),
        "pm_agent_assignments_md": str(assignments_md_path),
        "pm_commit": commit_sha,
        "pm_push": json.dumps(push_result, ensure_ascii=True),
    }


def _record_pm_intake(task: Dict[str, Any], description: str) -> Dict[str, Any]:
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
    push_result = (
        _sync_branch_to_remote(repo_path, current_branch(repo_path))
        if commit_sha
        else {"ok": False, "stderr": "nothing to push"}
    )
    return {
        "pm_intake": str(intake_path),
        "project_repo_path": str(repo_path),
        "pm_intake_commit": commit_sha,
        "pm_intake_push": json.dumps(push_result, ensure_ascii=True),
    }


def _record_architecture_artifacts(
    task: Dict[str, Any], raw_output: str, tasks: List[Dict[str, Any]]
) -> Dict[str, Any]:
    repo_path = _ensure_project_scaffold(task, task.get("description", ""))
    run_git(repo_path, ["checkout", "main"])

    architecture_dir = repo_path / "documents" / "architecture"
    architecture_md = _next_version_path(architecture_dir, "architecture", ".md")
    architecture_json = _next_version_path(
        architecture_dir, "architecture_tasks", ".json"
    )
    architecture_drawio = _next_version_path(
        architecture_dir, "architecture", ".drawio"
    )

    _write_markdown(
        architecture_md,
        f"Architecture: {_project_name(task)}",
        f"## Solution Notes\n{raw_output}\n\n## Task Breakdown\n{json.dumps(tasks, indent=2, ensure_ascii=True)}",
    )
    _write_json(architecture_json, tasks)
    architecture_drawio.write_text(
        '<mxfile host="app.diagrams.net">\n'
        f'  <diagram name="{_project_name(task)} architecture">\n'
        "    Placeholder diagram generated by AI Factory.\n"
        "  </diagram>\n"
        "</mxfile>\n"
    )

    commit_sha = commit_all(
        repo_path, "architect: update versioned architecture documents"
    )
    push_result = (
        _sync_branch_to_remote(repo_path, current_branch(repo_path))
        if commit_sha
        else {"ok": False, "stderr": "nothing to push"}
    )
    return {
        "project_repo_path": str(repo_path),
        "architecture_md": str(architecture_md),
        "architecture_json": str(architecture_json),
        "architecture_drawio": str(architecture_drawio),
        "architecture_commit": commit_sha,
        "architecture_push": json.dumps(push_result, ensure_ascii=True),
    }


def _record_architecture_request(task: Dict[str, Any]) -> Dict[str, Any]:
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
    push_result = (
        _sync_branch_to_remote(repo_path, current_branch(repo_path))
        if commit_sha
        else {"ok": False, "stderr": "nothing to push"}
    )
    return {
        "architecture_request": str(request_path),
        "project_repo_path": str(repo_path),
        "architecture_request_commit": commit_sha,
        "architecture_request_push": json.dumps(push_result, ensure_ascii=True),
    }


def _build_existing_code_context(task: Dict[str, Any]) -> str:
    """Build the existing_code block for the dev prompt.

    Returns empty string if the repo does not exist or is empty.
    Never raises — a failure here must not crash the dev loop.
    """
    try:
        repo_path = _project_repo_path(task)
        if not repo_path.exists():
            return ""
        tree = build_file_tree(repo_path)
        imports = build_import_map(repo_path)
        if not tree.data.get("files"):
            return ""
        lines: list[str] = ["Existing project structure:"]
        for rel in tree.data["files"]:
            info = imports.data.get("modules", {}).get(rel, {})
            parts: list[str] = []
            if info.get("classes"):
                parts.append(f"classes: {', '.join(info['classes'])}")
            if info.get("functions"):
                parts.append(f"functions: {', '.join(info['functions'])}")
            lines.append(
                f"  {rel}" + (f" ({'; '.join(parts)})" if parts else "")
            )
        avail = imports.data.get("available_imports", [])
        if avail:
            lines += ["", "Available imports:"] + [f"  {imp}" for imp in avail]
        return "\n".join(lines)
    except Exception:
        return ""  # never crash the dev loop


def _build_dev_prompt(
    task: Dict[str, Any],
    description: str,
    attempt_number: int,
    qa_feedback: Dict[str, Any] | None = None,
) -> str:
    qa_feedback_text = "No QA feedback yet. Produce the initial implementation."
    if qa_feedback:
        qa_feedback_text = json.dumps(qa_feedback, indent=2, ensure_ascii=True)

    error_history_text = ""
    if attempt_number > 1:
        try:
            repo_path = _project_repo_path(task)
            task_id = task.get("task_id", "")
            if task_id and repo_path.exists():
                history = get_task_error_history(repo_path, task_id)
                if history.output and history.output != "No previous attempts recorded.":
                    error_history_text = (
                        "Previous attempt errors — try a DIFFERENT approach:\n"
                        + history.output
                    )
        except Exception:
            pass  # never crash the dev loop

    return render_prompt(
        DEV_USER_PROMPT,
        task_description=description,
        task_context=json.dumps(task, indent=2, ensure_ascii=True),
        attempt_number=attempt_number,
        qa_feedback=qa_feedback_text,
        error_history=error_history_text,
        existing_code=_build_existing_code_context(task),
    )


def _prepare_task_branch(
    repo_path: Path, branch_name: str, attempt_number: int
) -> None:
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
    repo_path = _ensure_project_scaffold(
        task, task.get("project_description", description)
    )
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

    code = _strip_code_fences(
        call_llm(
            DEV_SYSTEM_PROMPT,
            _build_dev_prompt(task, description, attempt_number, qa_feedback),
        )
    )

    file_path = _task_module_path(task, repo_path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(code)
    LOGGER.info("[dev] Code written to %s (%d bytes)", file_path, len(code))

    docs_dir = repo_path / "documents" / "pm"
    task_doc = _next_version_path(
        docs_dir, f"task_{_task_slug(task)}_implementation", ".md"
    )
    _write_markdown(
        task_doc,
        f"Implementation: {_task_title(task)}",
        f"## Branch\n`{branch_name}`\n\n## Attempt\n{attempt_number}\n\n## Artifact\n`{file_path.relative_to(repo_path)}`\n",
    )

    commit_sha = commit_all(
        repo_path,
        f"dev: implement {_task_slug(task)} attempt {attempt_number}",
    )
    push_result = (
        _sync_branch_to_remote(repo_path, branch_name)
        if commit_sha
        else {"ok": False, "stderr": "nothing to push"}
    )

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


def _summarize_qa_result(
    task_description: str, qa_logs: str, status: str
) -> Dict[str, Any]:
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


def _run_pytest(
    repo_path: Path, timeout_seconds: int = 120
) -> subprocess.CompletedProcess:
    python_path = _ensure_project_python_env(repo_path)
    return subprocess.run(
        [str(python_path), "-m", "pytest", str(repo_path), "-v"],
        capture_output=True,
        text=True,
        timeout=max(30, timeout_seconds),
    )


def _run_qa_for_artifact(
    task: Dict[str, Any],
    task_id: str,
    description: str,
    artifact: str | None,
    attempt_number: int,
    remaining_seconds: int | None = None,
) -> Dict[str, Any]:
    repo_path = _ensure_project_scaffold(
        task, task.get("project_description", description)
    )
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
        python_path = Path(dependency_info["python"])

        # 1. Syntax check (stdlib, instant — no subprocess)
        syntax_result = syntax_check(artifact_path)
        if not syntax_result.ok:
            qa_logs = (
                f"Dependency install: {json.dumps(dependency_info, ensure_ascii=True)}\n\n"
                f"SYNTAX ERROR (pre-pytest):\n{syntax_result.output}\n"
                f"Details: {json.dumps(syntax_result.data, ensure_ascii=True)}\n"
            )
            status = "fail"
        else:
            # 2. Lint (ruff — skipped gracefully if not installed)
            lint_result = run_lint(artifact_path, python_path)

            # 3. Type check (mypy — skipped gracefully if not installed)
            type_result = run_typecheck(artifact_path, repo_path, python_path)

            # 4. Pytest with coverage (replaces bare _run_pytest)
            pytest_result = run_pytest_with_coverage(
                repo_path,
                python_path,
                timeout=remaining_seconds or 120,
                module_name=_project_slug(task),
            )

            # 5. Assemble qa_logs for LLM summarizer
            if lint_result.error:
                lint_section = f"LINT (ruff) [unavailable: {lint_result.error}]"
            else:
                lint_section = (
                    f"LINT (ruff): {'OK' if lint_result.ok else 'ISSUES FOUND'}\n"
                    + json.dumps(
                        lint_result.data.get("issues", [])[:20],
                        ensure_ascii=True,
                    )
                )

            if type_result.error:
                type_section = (
                    f"TYPE CHECK (mypy) [unavailable: {type_result.error}]"
                )
            else:
                type_section = (
                    f"TYPE CHECK (mypy): {'OK' if type_result.ok else 'ERRORS FOUND'}\n"
                    + json.dumps(
                        type_result.data.get("errors", [])[:20],
                        ensure_ascii=True,
                    )
                )

            cov = pytest_result.data.get("coverage")
            cov_section = (
                f"Coverage: {cov['percent']:.1f}%"
                f" ({cov['covered_lines']}/{cov['total_lines']} lines)\n"
                if cov
                else "Coverage: not available\n"
            )
            qa_logs = (
                f"Dependency install: {json.dumps(dependency_info, ensure_ascii=True)}\n\n"
                f"SYNTAX: OK\n\n"
                f"{lint_section}\n\n"
                f"{type_section}\n\n"
                f"PYTEST:\n"
                f"{pytest_result.data.get('stdout', '')}"
                f"{pytest_result.data.get('stderr', '')}\n"
                f"{cov_section}"
            )
            status = "success" if pytest_result.ok else "fail"

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
    commit_sha = commit_all(
        repo_path, f"qa: validate {_task_slug(task)} attempt {attempt_number}"
    )
    push_result = (
        _sync_branch_to_remote(repo_path, branch_name)
        if commit_sha
        else {"ok": False, "stderr": "nothing to push"}
    )

    merge_commit = None
    merge_push_result = None
    pr_url = None
    if status == "success":
        # Ensure task branch is on remote before creating PR
        if not push_result.get("ok"):
            push_result = _sync_branch_to_remote(repo_path, branch_name)

        pr_result = create_and_merge_github_pr(
            repo_path,
            branch_name,
            title=f"feat: {_task_slug(task)} (QA approved)",
            body=f"Automated merge after QA approval for task `{task.get('task_id', '')}`.",
        )

        if pr_result.get("ok"):
            pr_url = pr_result.get("pr_url")
            merge_commit = pr_result.get("merge_commit")
            LOGGER.info("[qa] PR merged via GitHub API: %s", pr_url)
            run_git(repo_path, ["checkout", "main"], check=False)
            run_git(repo_path, ["pull", "origin", "main"], check=False)
            run_git(repo_path, ["branch", "-d", branch_name], check=False)
            merge_push_result = {"ok": True, "transport": "github_api"}
        else:
            LOGGER.warning(
                "[qa] GitHub PR failed (%s), falling back to local merge",
                pr_result.get("error"),
            )
            run_git(repo_path, ["checkout", "main"])
            run_git(
                repo_path,
                ["merge", "--no-ff", branch_name, "-m",
                 f"merge: {_task_slug(task)} after qa approval"],
            )
            merge_commit = run_git(repo_path, ["rev-parse", "HEAD"]).stdout.strip()
            merge_push_result = _sync_branch_to_remote(repo_path, "main")
            run_git(repo_path, ["push", "origin", "--delete", branch_name], check=False)
            run_git(repo_path, ["branch", "-d", branch_name], check=False)

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
        "pr_url": pr_url,
        "qa_report_md": str(qa_md_path),
        "qa_report_json": str(qa_json_path),
        "project_repo_path": str(repo_path),
        "plan": plan_artifacts,
    }


def _normalize_task(
    task: Dict[str, Any], project_context: Dict[str, Any]
) -> Dict[str, Any]:
    context = dict(project_context or {})
    context.setdefault("project_name", task.get("project_name") or _project_name(task))
    context.setdefault("project_repo_path", task.get("project_repo_path", ""))
    context.setdefault("project_description", task.get("project_description", ""))

    normalized = normalize_task_contract(task, project_context=context)
    normalized.setdefault("task_id", str(uuid.uuid4()))
    normalized["project_name"] = context.get("project_name", "project")
    normalized["project_repo_path"] = context.get("project_repo_path", "")
    # Store a short description reference rather than the full 40k+ project description
    # to keep Temporal payloads under the 512KB limit.
    desc = context.get("project_description", "")
    normalized["project_description"] = desc[:500] if desc else ""
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


def _build_recovery_request(
    initial_task: Dict[str, Any], results: List[Dict[str, Any]], cycle: int
) -> Dict[str, Any]:
    failures = [
        {
            "task_id": result.get("task_id"),
            "status": result.get("status"),
            "qa": result.get("qa", {}),
            "error": result.get("error"),
        }
        for result in results
        if result.get("status") != "success"
        or result.get("qa", {}).get("status") not in {None, "success"}
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
    start_time = datetime.now()
    task = _load_activity_input(task)
    workflow_id = task.get("_workflow_id", "unknown")

    description = task.get("description", "")[:200]
    project_name = _project_name(task)
    github_url = task.get("github_url", "N/A")

    LOGGER.info("=" * 80)
    LOGGER.info("[PM AGENT] Starting project planning")
    LOGGER.info("[PM AGENT] Workflow ID: %s", workflow_id)
    LOGGER.info("[PM AGENT] Project: %s", project_name)
    LOGGER.info("[PM AGENT] GitHub URL: %s", github_url)
    LOGGER.info(
        "[PM AGENT] Task description: %s...",
        description[:100] if description else "N/A",
    )
    LOGGER.info("=" * 80)

    description_full = task.get("description", "")
    project_repo_path = _ensure_project_scaffold(task, description_full)
    LOGGER.info("[PM AGENT] Project repo ready at: %s", project_repo_path)

    intake_artifacts = _record_pm_intake(task, description_full)
    LOGGER.info(
        "[PM AGENT] Intake artifacts recorded: %s", list(intake_artifacts.keys())
    )

    LOGGER.info("[PM AGENT] Step 1/3: Calling Architect LLM to analyze requirements...")
    architect_start = datetime.now()
    architect_notes = call_llm(
        ARCHITECT_SYSTEM_PROMPT,
        render_prompt(
            ARCHITECT_USER_PROMPT,
            project_description=description_full,
        ),
    )
    LOGGER.info(
        "[PM AGENT] Architect LLM completed in %ds | response length: %d chars",
        (datetime.now() - architect_start).total_seconds(),
        len(architect_notes),
    )

    LOGGER.info(
        "[PM AGENT] Step 2/3: Calling Analyst LLM for current state analysis..."
    )
    analyst_start = datetime.now()
    analyst_notes = call_llm(
        ANALYST_SYSTEM_PROMPT,
        render_prompt(
            ANALYST_USER_PROMPT,
            current_state="",
            event=description_full,
        ),
    )
    LOGGER.info(
        "[PM AGENT] Analyst LLM completed in %ds | response length: %d chars",
        (datetime.now() - analyst_start).total_seconds(),
        len(analyst_notes),
    )

    LOGGER.info("[PM AGENT] Step 3/3: Generating execution plan...")
    plan_start = datetime.now()
    # Truncate notes to prevent PM prompt from exceeding token limits.
    # Architect output can be 40-50k chars; the PM prompt template appends
    # both notes plus the full description, which overwhelms LLM_MAX_PROMPT_TOKENS
    # and causes the JSON execution_plan to be silently truncated to 0 tasks.
    _PM_MAX_NOTES_CHARS = 4000
    pm_output = call_llm(
        PM_SYSTEM_PROMPT,
        render_prompt(
            PM_USER_PROMPT,
            task_description=description_full,
            architect_input=architect_notes[:_PM_MAX_NOTES_CHARS],
            analyst_input=analyst_notes[:_PM_MAX_NOTES_CHARS],
        ),
    )
    LOGGER.info(
        "[PM AGENT] PM LLM completed in %ds | response length: %d chars",
        (datetime.now() - plan_start).total_seconds(),
        len(pm_output),
    )

    project_context = {
        "project_name": project_name,
        "github_url": github_url,
        "project_repo_path": str(project_repo_path),
        "project_description": description_full,
    }

    try:
        plan = json.loads(pm_output)
    except json.JSONDecodeError:
        LOGGER.warning("[PM AGENT] Plan not valid JSON, using fallback plan")
        plan = {
            "project_goal": description_full,
            "delivery_summary": pm_output,
            "architect_guidance": [architect_notes],
            "analyst_guidance": [analyst_notes],
            "execution_plan": [
                {
                    "task_id": str(uuid.uuid4()),
                    "title": "Implement requested work",
                    "description": description_full or pm_output,
                    "assigned_agent": "dev",
                    "dependencies": [],
                    "acceptance_criteria": ["Deliver the requested implementation"],
                }
            ],
        }

    execution_plan = plan.get("execution_plan", [])
    execution_plan = _expand_execution_plan(
        execution_plan,
        json.dumps(project_context, ensure_ascii=True),
    )
    plan["execution_plan"] = _normalize_task_list(execution_plan, project_context)
    LOGGER.info(
        "[PM AGENT] Plan parsed successfully | %d tasks planned",
        len(execution_plan),
    )
    for i, t in enumerate(execution_plan[:5]):
        LOGGER.info(
            "[PM AGENT]   Task %d: [%s] %s",
            i + 1,
            t.get("assigned_agent", "?"),
            t.get("title", "?")[:60],
        )
    if len(execution_plan) > 5:
        LOGGER.info("[PM AGENT]   ... and %d more tasks", len(execution_plan) - 5)

    artifact_paths = _record_pm_artifacts(
        task, description, plan, architect_notes, analyst_notes
    )

    # Truncate architect/analyst guidance to avoid exceeding Temporal's 512KB payload limit
    _GUIDANCE_MAX = 2000
    truncated_architect = [g[:_GUIDANCE_MAX] for g in plan.get("architect_guidance", [])]
    truncated_analyst = [g[:_GUIDANCE_MAX] for g in plan.get("analyst_guidance", [])]

    result = {
        "event_id": str(uuid.uuid4()),
        "task_id": task.get("task_id", str(uuid.uuid4())),
        "stage": "pm_done",
        "status": "success",
        "timestamp": int(time.time() * 1000),
        "decision": "continue",
        "project_name": project_name,
        "project_repo_path": str(project_repo_path),
        "project_goal": plan.get("project_goal", description)[:2000],
        "delivery_summary": plan.get("delivery_summary", "")[:2000],
        "architect_guidance": truncated_architect,
        "analyst_guidance": truncated_analyst,
        "execution_plan": plan.get("execution_plan", []),
        "artifacts": {**intake_artifacts, **artifact_paths},
    }

    duration_ms = int((datetime.now() - start_time).total_seconds() * 1000)
    LOGGER.info(
        "[pm] Completed | workflow=%s | duration=%dms | tasks_created=%d",
        workflow_id,
        duration_ms,
        len(result.get("execution_plan", [])),
    )

    return _wrap_activity_result(workflow_id, "pm", result, start_time)


@activity.defn
async def architect_activity(task: Dict[str, Any]) -> Dict[str, Any]:
    start_time = datetime.now()
    task = _load_activity_input(task)
    workflow_id = task.get("_workflow_id", "unknown")

    LOGGER.info(
        "[architect] Starting | workflow=%s | task_id=%s",
        workflow_id,
        task.get("task_id"),
    )

    description = task.get("description", "")
    request_artifacts = _record_architecture_request(task)

    LOGGER.info(
        "[architect] Generating task breakdown | workflow=%s | desc_len=%d",
        workflow_id,
        len(description),
    )
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
        LOGGER.info(
            "[architect] Using fallback tasks | workflow=%s | count=%d",
            workflow_id,
            len(tasks),
        )
    else:
        LOGGER.info(
            "[architect] Tasks defined | workflow=%s | count=%d",
            workflow_id,
            len(tasks),
        )

    artifact_paths = _record_architecture_artifacts(task, output, tasks)
    # Use a short project_description in task contexts to prevent the full PM plan
    # (which can be 30+ tasks with full objects) from being embedded into every task
    # and overflowing the decomposer's Temporal message payload.
    _TASK_PROJ_DESC_MAX = 800
    project_context = {
        "project_name": _project_name(task),
        "project_repo_path": artifact_paths["project_repo_path"],
        "project_description": description[:_TASK_PROJ_DESC_MAX],
        "github_url": task.get("github_url", ""),
    }
    normalized_tasks = _normalize_task_list(tasks, project_context)

    result = {
        "event_id": str(uuid.uuid4()),
        "task_id": task.get("task_id", str(uuid.uuid4())),
        "stage": "architect_done",
        "status": "success",
        "timestamp": int(time.time() * 1000),
        "decision": "continue",
        "tasks": normalized_tasks,
        "artifacts": {**request_artifacts, **artifact_paths},
    }

    duration_ms = int((datetime.now() - start_time).total_seconds() * 1000)
    LOGGER.info(
        "[architect] Completed | workflow=%s | duration=%dms | tasks=%d",
        workflow_id,
        duration_ms,
        len(normalized_tasks),
    )

    return _wrap_activity_result(workflow_id, "architect", result, start_time)


@activity.defn
async def pm_recovery_activity(task: Dict[str, Any]) -> Dict[str, Any]:
    start_time = datetime.now()
    task = _load_activity_input(task)
    workflow_id = task.get("_workflow_id", "unknown")
    recovery_cycle = task.get("recovery_cycle", 1)

    LOGGER.info(
        "[pm_recovery] Starting | workflow=%s | cycle=%d",
        workflow_id,
        recovery_cycle,
    )

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
            event=json.dumps(
                task.get("failure_summary", []), indent=2, ensure_ascii=True
            ),
        ),
    )
    _PM_MAX_NOTES_CHARS = 4000
    pm_output = call_llm(
        PM_SYSTEM_PROMPT,
        render_prompt(
            PM_USER_PROMPT,
            task_description=description,
            architect_input=architect_notes[:_PM_MAX_NOTES_CHARS],
            analyst_input=analyst_notes[:_PM_MAX_NOTES_CHARS],
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

    project_context = {
        "project_name": _project_name(task),
        "project_repo_path": str(_project_repo_path(task)),
        "project_description": description,
        "github_url": task.get("github_url", ""),
    }
    plan["execution_plan"] = _normalize_task_list(
        plan.get("execution_plan", []), project_context
    )

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
    commit_sha = commit_all(
        repo_path, f"pm: recovery plan cycle {task.get('recovery_cycle', 1)}"
    )
    push_result = (
        _sync_branch_to_remote(repo_path, current_branch(repo_path))
        if commit_sha
        else {"ok": False, "stderr": "nothing to push"}
    )

    result = {
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

    duration_ms = int((datetime.now() - start_time).total_seconds() * 1000)
    LOGGER.info(
        "[pm_recovery] Completed | workflow=%s | cycle=%d | duration=%dms | tasks=%d",
        workflow_id,
        recovery_cycle,
        duration_ms,
        len(result.get("execution_plan", [])),
    )

    return _wrap_activity_result(
        workflow_id, f"pm_recovery_{recovery_cycle}", result, start_time
    )


@activity.defn
async def dev_activity(task: Dict[str, Any]) -> Dict[str, Any]:
    start_time = datetime.now()
    task = _load_activity_input(task)
    workflow_id = task.get("_workflow_id", "unknown")
    task_id = task.get("task_id", str(uuid.uuid4()))
    description = task.get("description", "")
    attempt = int(task.get("attempt_number", 1))

    LOGGER.info(
        "[dev] Starting | workflow=%s | task_id=%s | attempt=%d",
        workflow_id,
        task_id,
        attempt,
    )

    result = _generate_dev_artifact(
        task=task,
        task_id=task_id,
        description=description,
        attempt_number=attempt,
        qa_feedback=task.get("qa_feedback"),
    )

    duration_ms = int((datetime.now() - start_time).total_seconds() * 1000)
    LOGGER.info(
        "[dev] Completed | workflow=%s | task_id=%s | duration=%dms | artifact=%s",
        workflow_id,
        task_id,
        duration_ms,
        result.get("artifact", "none"),
    )

    return _wrap_activity_result(workflow_id, f"dev_{task_id}", result, start_time)


@activity.defn
async def qa_activity(task: Dict[str, Any]) -> Dict[str, Any]:
    start_time = datetime.now()
    task = _load_activity_input(task)
    workflow_id = task.get("_workflow_id", "unknown")
    task_id = task.get("task_id", str(uuid.uuid4()))
    artifact = task.get("artifact", "")
    attempt = int(task.get("attempt_number", 1))

    LOGGER.info(
        "[qa] Starting | workflow=%s | task_id=%s | artifact=%s | attempt=%d",
        workflow_id,
        task_id,
        artifact[:50] if artifact else "none",
        attempt,
    )

    result = _run_qa_for_artifact(
        task,
        task_id,
        task.get("description", ""),
        artifact,
        attempt,
    )

    duration_ms = int((datetime.now() - start_time).total_seconds() * 1000)
    LOGGER.info(
        "[qa] Completed | workflow=%s | task_id=%s | duration=%dms | status=%s",
        workflow_id,
        task_id,
        duration_ms,
        result.get("status", "unknown"),
    )

    return _wrap_activity_result(workflow_id, f"qa_{task_id}", result, start_time)


@activity.defn
async def analyst_activity(input_data: Any) -> Dict[str, Any]:
    start_time = datetime.now()
    workflow_id = "unknown"
    tasks: List[Dict[str, Any]] = []

    if isinstance(input_data, dict):
        tasks = input_data.get("dev_qa_results", input_data.get("_tasks", []))
        workflow_id = input_data.get("_workflow_id", "unknown")
        loaded_input = _load_activity_input(input_data)
        tasks = loaded_input.get("dev_qa_results", loaded_input.get("_tasks", tasks))
    elif isinstance(input_data, list):
        tasks = input_data

    LOGGER.info(
        "[analyst] Starting | workflow=%s | tasks=%d",
        workflow_id,
        len(tasks) if tasks else 0,
    )

    if not tasks:
        LOGGER.info("[analyst] No tasks to analyze | workflow=%s", workflow_id)
        return _wrap_activity_result(
            workflow_id,
            "analyst",
            {"status": "skipped", "reason": "no tasks"},
            start_time,
        )

    first_task = tasks[0]
    repo_path = _ensure_project_scaffold(
        first_task, first_task.get("project_description", "")
    )
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

    LOGGER.info(
        "[analyst] Generating project state | workflow=%s | tasks_summary_len=%d",
        workflow_id,
        len(tasks_summary),
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
    push_result = (
        _sync_branch_to_remote(repo_path, current_branch(repo_path))
        if commit_sha
        else {"ok": False, "stderr": "nothing to push"}
    )

    result = {
        "status": "complete",
        "state": new_state,
        "artifact": str(state_file),
        "commit": commit_sha,
        "push": push_result,
        "project_repo_path": str(repo_path),
        "plan": plan_artifacts,
    }

    duration_ms = int((datetime.now() - start_time).total_seconds() * 1000)
    LOGGER.info(
        "[analyst] Completed | workflow=%s | duration=%dms",
        workflow_id,
        duration_ms,
    )

    return _wrap_activity_result(workflow_id, "analyst", result, start_time)


async def _execute_task_impl(task: Dict[str, Any]) -> Dict[str, Any]:
    """Core task execution: dev artifact generation + QA self-healing loop.

    All named task activities (DEV_Task, QA_Task, etc.) delegate here.
    """
    start_time = datetime.now()
    task = _load_activity_input(task)
    workflow_id = task.get("_workflow_id", "unknown")

    task = _normalize_task(
        task,
        {
            "project_name": task.get("project_name") or _project_name(task),
            "project_repo_path": str(_project_repo_path(task)),
            "project_description": task.get(
                "project_description", task.get("description", "")
            ),
            "github_url": task.get("github_url", ""),
        },
    )

    task_id = task.get("task_id", str(uuid.uuid4()))
    description = task.get("description", "")
    activity_start_time = time.monotonic()

    LOGGER.info(
        "[process_task] Starting | workflow=%s | task_id=%s",
        workflow_id,
        task_id,
    )

    repo_path = _ensure_project_scaffold(
        task, task.get("project_description", description)
    )
    previous_state = _load_task_state(repo_path, task_id)

    if (
        previous_state
        and previous_state.get("status") == "success"
        and previous_state.get("workflow_id") == workflow_id
    ):
        LOGGER.info(
            "[process_task] Resuming from previous success | workflow=%s | task_id=%s",
            workflow_id,
            task_id,
        )
        result = previous_state.get("result", previous_state)
        return _wrap_activity_result(workflow_id, f"task_{task_id}", result, start_time)

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

    if _task_timed_out(activity_start_time):
        LOGGER.warning(
            "[process_task] Timed out before start | workflow=%s | task_id=%s",
            workflow_id,
            task_id,
        )
        continuation = _record_continuation_plan(
            repo_path,
            "pm",
            task,
            f"Task exceeded the {MAX_TASK_EXECUTION_SECONDS // 60}-minute budget before execution started.",
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
        _save_task_state(
            repo_path, task_id, {"status": "needs_continuation", "result": result}
        )
        return _wrap_activity_result(workflow_id, f"task_{task_id}", result, start_time)

    next_attempt = 1
    qa_feedback = None
    if previous_state:
        next_attempt = int(previous_state.get("attempts", len(healing_history))) + 1
        qa_feedback = previous_state.get("last_qa_feedback")

    LOGGER.info(
        "[process_task] Running dev | workflow=%s | task_id=%s | attempt=%d",
        workflow_id,
        task_id,
        next_attempt,
    )
    dev_result = _generate_dev_artifact(
        task=task,
        task_id=task_id,
        description=description,
        attempt_number=next_attempt,
        qa_feedback=qa_feedback,
    )
    LOGGER.info(
        "[process_task] Running QA | workflow=%s | task_id=%s | attempt=%d",
        workflow_id,
        task_id,
        next_attempt,
    )
    qa_result = _run_qa_for_artifact(
        task,
        task_id,
        description,
        dev_result["artifact"],
        next_attempt,
        remaining_seconds=_remaining_time_seconds(activity_start_time),
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
        and not _task_timed_out(activity_start_time)
    ):
        next_attempt = len(healing_history) + 1
        LOGGER.info(
            "[process_task] Self-healing | workflow=%s | task_id=%s | attempt=%d",
            workflow_id,
            task_id,
            next_attempt,
        )
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
            remaining_seconds=_remaining_time_seconds(activity_start_time),
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
    if _task_timed_out(activity_start_time):
        LOGGER.warning(
            "[process_task] Timed out | workflow=%s | task_id=%s", workflow_id, task_id
        )
        continuation = _record_continuation_plan(
            repo_path,
            "pm",
            task,
            f"Task exceeded the {MAX_TASK_EXECUTION_SECONDS // 60}-minute execution budget and must continue later.",
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
    _save_task_state(repo_path, task_id, {"status": final_status, "workflow_id": workflow_id, "result": result})

    duration_ms = int((datetime.now() - start_time).total_seconds() * 1000)
    LOGGER.info(
        "[process_task] Completed | workflow=%s | task_id=%s | status=%s | attempts=%d | duration=%dms",
        workflow_id,
        task_id,
        final_status,
        len(healing_history),
        duration_ms,
    )

    return _wrap_activity_result(workflow_id, f"task_{task_id}", result, start_time)


@activity.defn
async def process_single_task(task: Dict[str, Any]) -> Dict[str, Any]:
    """Generic task activity — kept for backward compatibility."""
    return await _execute_task_impl(task)


@activity.defn(name="DEV_Task")
async def dev_task(task: Dict[str, Any]) -> Dict[str, Any]:
    """Feature and bugfix implementation tasks dispatched to the dev agent."""
    return await _execute_task_impl(task)


@activity.defn(name="QA_Task")
async def qa_task(task: Dict[str, Any]) -> Dict[str, Any]:
    """Test and validation tasks dispatched to the QA agent."""
    return await _execute_task_impl(task)


@activity.defn(name="REFACTOR_Task")
async def refactor_task(task: Dict[str, Any]) -> Dict[str, Any]:
    """Refactoring tasks dispatched to the dev agent."""
    return await _execute_task_impl(task)


@activity.defn(name="SETUP_Task")
async def setup_task(task: Dict[str, Any]) -> Dict[str, Any]:
    """Project setup and configuration tasks."""
    return await _execute_task_impl(task)


@activity.defn(name="DOCS_Task")
async def docs_task(task: Dict[str, Any]) -> Dict[str, Any]:
    """Documentation tasks dispatched to the dev agent."""
    return await _execute_task_impl(task)


@activity.defn
async def cleanup_stale_branches_activity(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Delete remote task-* branches whose tasks completed (success or error).

    Called at the end of OrchestratorWorkflow after all tasks are processed.
    Uses the GitHub REST API when GITHUB_TOKEN is available; falls back to
    local `git push origin --delete` for non-GitHub remotes.

    payload keys:
      project_repo_path  – local path to the generated project repo
      completed_task_ids – list of task IDs that finished (any status)
    """
    import requests as _requests

    repo_path_str = payload.get("project_repo_path", "")
    completed_ids: list[str] = list(payload.get("completed_task_ids", []))
    if not repo_path_str:
        return {"ok": False, "deleted": [], "error": "no project_repo_path"}

    repo_path = Path(repo_path_str)
    if not repo_path.exists():
        return {"ok": False, "deleted": [], "error": f"repo_path not found: {repo_path_str}"}

    # Build set of task-branch names that belong to completed tasks
    completed_branches = {f"task-{tid}" for tid in completed_ids}

    deleted: list[str] = []
    errors: list[str] = []

    # List all remote branches via git ls-remote
    ls_result = run_git(repo_path, ["ls-remote", "--heads", "origin"], check=False)
    if ls_result.returncode != 0:
        return {"ok": False, "deleted": [], "error": f"ls-remote failed: {ls_result.stderr[:200]}"}

    remote_task_branches: list[str] = []
    for line in ls_result.stdout.splitlines():
        # format: "<sha>\trefs/heads/<branch>"
        parts = line.strip().split("\t")
        if len(parts) == 2 and parts[1].startswith("refs/heads/task-"):
            remote_task_branches.append(parts[1].removeprefix("refs/heads/"))

    if not remote_task_branches:
        LOGGER.info("[cleanup] No task-* branches found on remote")
        return {"ok": True, "deleted": [], "error": None}

    # Try GitHub API first for a clean delete
    token = _github_api_token()
    slug = _github_repo_slug(repo_path)

    for branch in remote_task_branches:
        if branch not in completed_branches:
            LOGGER.info("[cleanup] Skipping branch %s (task not in completed set)", branch)
            continue

        if token and slug:
            owner, repo = slug
            del_resp = _requests.delete(
                f"https://api.github.com/repos/{owner}/{repo}/git/refs/heads/{branch}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                timeout=15,
            )
            if del_resp.status_code in (204, 422):  # 422 = already gone
                LOGGER.info("[cleanup] Deleted remote branch %s via GitHub API", branch)
                deleted.append(branch)
            else:
                LOGGER.warning(
                    "[cleanup] GitHub API delete failed for %s (HTTP %d): %s",
                    branch, del_resp.status_code, del_resp.text[:100],
                )
                errors.append(f"{branch}: HTTP {del_resp.status_code}")
        else:
            # Fall back to git push --delete
            del_result = run_git(
                repo_path, ["push", "origin", "--delete", branch], check=False
            )
            if del_result.returncode == 0:
                LOGGER.info("[cleanup] Deleted remote branch %s via git push", branch)
                deleted.append(branch)
            else:
                LOGGER.warning(
                    "[cleanup] git push --delete failed for %s: %s",
                    branch, del_result.stderr[:100],
                )
                errors.append(f"{branch}: {del_result.stderr[:100]}")

    LOGGER.info(
        "[cleanup] Branch cleanup done: %d deleted, %d errors", len(deleted), len(errors)
    )
    return {"ok": len(errors) == 0, "deleted": deleted, "error": "; ".join(errors) or None}


@activity.defn
async def process_all_tasks(input_data: Any) -> List[Dict[str, Any]]:
    """Legacy stub — no longer called by OrchestratorWorkflow or ProjectWorkflow.

    Task dispatch has been promoted to the workflow tier: the workflow now calls
    workflow.execute_activity(process_single_task, task) for each task in
    parallel via asyncio.gather (see _dispatch_tasks in workflows.py).

    This activity remains registered so in-flight workflow histories that
    reference it can still replay without errors.
    """
    LOGGER.warning(
        "[process_batch] process_all_tasks is a legacy stub and should not be "
        "invoked by new workflow executions — dispatch is now handled by the workflow."
    )
    return []
