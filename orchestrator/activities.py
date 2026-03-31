import json
import logging
import os
import re
import subprocess
import sys
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
REARCHITECT_SYSTEM_PROMPT = load_prompt("rearchitect", "system")
REARCHITECT_USER_PROMPT = load_prompt("rearchitect", "user")
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
MAX_TASK_EXECUTION_SECONDS = int(os.getenv("MAX_TASK_EXECUTION_SECONDS", "3600"))
WORKSPACE_ROOT = Path(os.getenv("WORKSPACE_ROOT", "/workspace"))
PROJECTS_ROOT = Path(os.getenv("PROJECTS_ROOT", str(WORKSPACE_ROOT / "projects")))
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

    _ai_factory_root = Path(os.getenv("AI_FACTORY_ROOT", "/workspace/.ai_factory"))
    output_dir = _ai_factory_root / "contexts" / workflow_id
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
        parsed = json.loads(_extract_json(output))
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
        subprocess.run([sys.executable, "-m", "venv", str(repo_path / ".venv")], check=True)
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
        req_result = subprocess.run(
            [str(python_path), "-m", "pip", "install", "-r", str(requirements_path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=600,
        )
        if req_result.returncode != 0:
            LOGGER.warning(
                "[qa] requirements.txt install failed (continuing): %s",
                req_result.stderr[:300],
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


def _extract_json(text: str) -> str:
    """Extract JSON from LLM output that may be wrapped in markdown code fences.

    Handles ```json...```, ```...```, bare JSON, and truncated (unclosed) fences.
    Returns the raw text unchanged if no JSON block is found.
    """
    import re as _re
    # Strip opening code fence (handles both closed and unclosed/truncated fences)
    stripped = _re.sub(r"^```(?:json)?\s*\n?", "", text.lstrip(), count=1)

    # Try ```json ... ``` or ``` ... ``` fences first (complete fences)
    match = _re.search(r"```(?:json)?\s*\n?([\s\S]*?)```", text, _re.DOTALL)
    if match:
        candidate = match.group(1).strip()
        if candidate.startswith(("{", "[")):
            return candidate

    # Try to find a top-level JSON object or array in the stripped (or raw) text
    for src in (stripped, text):
        for start_char, end_char in (("{", "}"), ("[", "]")):
            start = src.find(start_char)
            if start == -1:
                continue
            # Walk backwards from end to find matching close
            end = src.rfind(end_char)
            if end != -1 and end > start:
                candidate = src[start:end + 1]
                try:
                    json.loads(candidate)
                    return candidate
                except json.JSONDecodeError:
                    pass
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
    """Return the primary output file path for this task.

    Priority:
    1. output.files[0] from the task contract (most accurate)
    2. Module: <file.py> pattern in description (legacy)
    3. Fallback: {package}/{task_slug}.py
    """
    output_files = task.get("output", {}).get("files", [])
    if output_files:
        candidate = str(output_files[0]).strip()
        if candidate:
            return repo_path / candidate
    description = task.get("description", "")
    module_match = re.search(r"Module:\s*([A-Za-z0-9_./-]+\.py)", description)
    if module_match:
        return repo_path / module_match.group(1)
    package_name = _project_package_name(task)
    return repo_path / package_name / f"{_task_slug(task)}.py"


_SAFE_PATH_RE = re.compile(r'^[A-Za-z0-9_\-][A-Za-z0-9_\-./]*$')


def _is_safe_relative_path(path: str) -> bool:
    """Return True only for safe, relative paths with no traversal components."""
    if not path or path.startswith("/"):
        return False
    # Reject any path component that is ".." (traversal)
    parts = Path(path).parts
    if any(p == ".." for p in parts):
        return False
    # Allow only safe characters: letters, digits, underscore, hyphen, dot, slash
    if not _SAFE_PATH_RE.match(path):
        return False
    return True


def _parse_multi_file_output(code: str) -> List[tuple[str, str]]:
    """Parse LLM output that may contain multiple files using === FILE: path === headers.

    Returns list of (relative_path, content) tuples.
    If no FILE headers are found, returns an empty list (caller uses single-file path).
    Skips entries where content is empty after stripping (LLM truncation guard).
    Skips entries with unsafe paths (traversal, absolute, or special characters).
    """
    pattern = re.compile(r"^=== FILE: (.+?) ===\s*$", re.MULTILINE)
    matches = list(pattern.finditer(code))
    if not matches:
        return []
    files = []
    for i, match in enumerate(matches):
        path = match.group(1).strip()
        if not _is_safe_relative_path(path):
            LOGGER.warning("[dev] _parse_multi_file_output: unsafe path rejected: %r", path)
            continue
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(code)
        raw = code[start:end].strip("\n")
        # Strip opening code fence (```python / ```py / ```) and closing fence
        content = re.sub(r"^```(?:python|py)?\n", "", raw, count=1)
        content = re.sub(r"\n?```\s*$", "", content).strip()
        if not content:
            LOGGER.warning("[dev] _parse_multi_file_output: empty content for %s — skipping", path)
            continue
        files.append((path, content))
    return files


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


_PROJECT_NOTES_FILENAME = "project_notes.md"
_PROJECT_NOTES_SECTIONS = [
    "Conventions Discovered",
    "Architecture Decisions",
    "Known Failure Patterns",
    "Completed Tasks Summary",
]
_PROJECT_NOTES_MAX_PROMPT_CHARS = 3000


def _project_notes_path(repo_path: Path) -> Path:
    return repo_path / ".ai_factory" / _PROJECT_NOTES_FILENAME


def _load_project_notes(repo_path: Path) -> str:
    """Load accumulated project notes to inject into agent prompts.

    Returns empty string if the file doesn't exist. Never raises.
    """
    try:
        p = _project_notes_path(repo_path)
        if not p.exists():
            return ""
        text = p.read_text(encoding="utf-8").strip()
        if not text:
            return ""
        # Trim from the top if too long (keep most recent content at the bottom)
        if len(text) > _PROJECT_NOTES_MAX_PROMPT_CHARS:
            text = "...[earlier notes truncated]...\n" + text[-_PROJECT_NOTES_MAX_PROMPT_CHARS:]
        return text
    except Exception:
        return ""


def _append_project_note(repo_path: Path, section: str, entry: str) -> None:
    """Append a timestamped entry under the given section in project_notes.md.

    Creates the file with all section headers if it doesn't exist. Never raises.
    """
    try:
        p = _project_notes_path(repo_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists():
            content = p.read_text(encoding="utf-8")
        else:
            project_name = repo_path.name
            header_lines = [f"# Project Notes: {project_name}", ""]
            for s in _PROJECT_NOTES_SECTIONS:
                header_lines += [f"## {s}", ""]
            content = "\n".join(header_lines)

        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        stamped = f"- [{ts}] {entry}"

        section_header = f"## {section}"
        if section_header in content:
            # Insert after the section header line
            lines = content.splitlines()
            insert_at = None
            for i, line in enumerate(lines):
                if line.strip() == section_header:
                    insert_at = i + 1
                    break
            if insert_at is not None:
                # Skip blank lines right after the header
                while insert_at < len(lines) and lines[insert_at].strip() == "":
                    insert_at += 1
                lines.insert(insert_at, stamped)
                content = "\n".join(lines) + "\n"
        else:
            content = content.rstrip() + f"\n\n{section_header}\n{stamped}\n"

        p.write_text(content, encoding="utf-8")
        LOGGER.debug("[notes] Appended to '%s' | file=%s", section, p)
    except Exception as exc:
        LOGGER.warning("[notes] Failed to append project note: %s", exc)


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
    skills_context: str = "",
    failure_patterns: str = "",
    strategy: str = "explore",
) -> str:
    qa_feedback_text = "No QA feedback yet. Produce the initial implementation."
    if qa_feedback:
        summary = qa_feedback.get("summary") or {}
        if isinstance(summary, str):
            try:
                summary = json.loads(summary)
            except Exception:
                summary = {"error_summary": summary}
        qa_feedback_text = "\n".join(filter(None, [
            "=== QA FAILURE REPORT ===",
            f"Status: {qa_feedback.get('status', 'fail')}",
            f"Error summary: {summary.get('error_summary', '')}",
            f"Root cause: {summary.get('root_cause', '')}",
            f"Fix suggestion: {summary.get('fix_suggestion', '')}",
            "",
            "=== FULL TEST OUTPUT ===",
            (qa_feedback.get("logs") or "")[:4000],
        ]))

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

    output_files = task.get("output", {}).get("files", [])
    if output_files:
        target_files_text = "\n".join(f"  - {f}" for f in output_files)
    else:
        target_files_text = "  (not specified — infer from task description)"

    if strategy == "explore":
        strategy_instruction = "Strategy: EXPLORE — generate a fresh, original solution without relying on the skills above."
    else:
        strategy_instruction = "Strategy: EXPLOIT — leverage the available skills above to compose your solution."

    # Project notes: accumulated cross-task context (conventions, arch decisions, failure patterns)
    project_notes_text = ""
    try:
        repo_path = _project_repo_path(task)
        if repo_path.exists():
            notes = _load_project_notes(repo_path)
            if notes:
                project_notes_text = "=== PROJECT NOTES (accumulated context) ===\n" + notes
    except Exception:
        pass

    # Strip hidden_tests so dev agent never sees them
    task_for_prompt = {k: v for k, v in task.items() if k != "hidden_tests"}
    return render_prompt(
        DEV_USER_PROMPT,
        task_description=description,
        task_context=json.dumps(task_for_prompt, indent=2, ensure_ascii=True),
        target_files=target_files_text,
        attempt_number=attempt_number,
        qa_feedback=qa_feedback_text,
        error_history=error_history_text,
        existing_code=_build_existing_code_context(task),
        project_notes=project_notes_text,
        skills_context=skills_context,
        failure_patterns=failure_patterns,
        strategy_instruction=strategy_instruction,
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

    mode = "autofix" if qa_feedback else "initial"
    dev_prompt = _build_dev_prompt(task, description, attempt_number, qa_feedback)
    LOGGER.info(
        "[dev] LLM call | task_id=%s | attempt=%d | mode=%s | prompt_chars=%d | branch=%s",
        task_id, attempt_number, mode, len(dev_prompt), branch_name,
    )
    llm_start = time.monotonic()
    raw_output = call_llm(DEV_SYSTEM_PROMPT, dev_prompt)
    LOGGER.info(
        "[dev] LLM done | task_id=%s | attempt=%d | response_chars=%d | elapsed=%.1fs",
        task_id, attempt_number, len(raw_output), time.monotonic() - llm_start,
    )

    multi = _parse_multi_file_output(raw_output)
    if multi:
        LOGGER.debug("[dev] Multi-file output detected | task_id=%s | files=%d", task_id, len(multi))
        written_paths = []
        repo_resolved = repo_path.resolve()
        for rel_path, content in multi:
            fp = (repo_path / rel_path).resolve()
            if not str(fp).startswith(str(repo_resolved) + "/") and fp != repo_resolved:
                LOGGER.warning("[dev] Path traversal blocked: %s resolves outside repo", rel_path)
                continue
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(content)
            LOGGER.info("[dev] Code written to %s (%d bytes)", fp, len(content))
            written_paths.append(fp)
        if not written_paths:
            raise ValueError("All LLM-generated paths were rejected (path traversal guard)")
        file_path = written_paths[0]
        code = multi[0][1]
    else:
        code = _strip_code_fences(raw_output)
        file_path = _task_module_path(task, repo_path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(code)
        LOGGER.info("[dev] Code written to %s (%d bytes, %d lines)", file_path, len(code), code.count("\n"))

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
    LOGGER.info(
        "[dev] Committed | task_id=%s | commit=%s | push_ok=%s",
        task_id, commit_sha or "none", push_result.get("ok"),
    )

    return {
        "task_id": task_id,
        "status": "success",
        "artifact": str(file_path),
        "code": code,
        "attempt": attempt_number,
        "mode": mode,
        "branch": branch_name,
        "commit": commit_sha,
        "push": push_result,
        "project_repo_path": str(repo_path),
        "implementation_note": str(task_doc),
        "plan": plan_artifacts,
    }


def _summarize_qa_result(
    task_description: str, qa_logs: str, status: str, attempt_number: int = 1
) -> Dict[str, Any]:
    LOGGER.debug(
        "[qa] Summarizing QA result | status=%s | attempt=%d | logs_chars=%d",
        status, attempt_number, len(qa_logs),
    )
    _sum_start = time.monotonic()
    qa_summary_raw = call_llm(
        QA_SYSTEM_PROMPT,
        render_prompt(
            QA_USER_PROMPT,
            test_logs=qa_logs,
            task_description=task_description,
            attempt_number=attempt_number,
        ),
    )
    LOGGER.debug(
        "[qa] Summary LLM done | status=%s | elapsed=%.1fs | response_chars=%d",
        status, time.monotonic() - _sum_start, len(qa_summary_raw),
    )
    try:
        qa_summary = json.loads(_extract_json(qa_summary_raw))
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


def _run_hidden_tests(
    task: Dict[str, Any],
    repo_path: Path,
    python_path: Path,
    timeout: int = 60,
) -> Dict[str, Any]:
    """Run hidden test cases from task["hidden_tests"] and return a score.

    Hidden tests are inline Python strings.  They are written to a temp directory
    (NOT in repo_path), executed against the installed project, then deleted.
    Results are never surfaced to the dev agent — only the score is returned.

    Returns {"ran": bool, "score": float, "passed": int, "total": int}.
    """
    hidden_tests: List[str] = task.get("hidden_tests", [])
    if not hidden_tests:
        return {"ran": False, "score": 1.0, "passed": 0, "total": 0}

    import tempfile as _tempfile
    tmp_dir = Path(_tempfile.mkdtemp(prefix="hidden_tests_"))
    try:
        test_files: List[str] = []
        for i, test_code in enumerate(hidden_tests):
            tf = tmp_dir / f"hidden_test_{i}.py"
            tf.write_text(test_code)
            test_files.append(str(tf))

        proc = subprocess.run(
            [str(python_path), "-m", "pytest", *test_files, "-v", "--tb=no", "-q"],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(repo_path),
        )
        # Parse pytest summary line: "X passed, Y failed in Zs"
        passed = failed = 0
        for line in proc.stdout.splitlines():
            m = re.search(r"(\d+) passed", line)
            if m:
                passed = int(m.group(1))
            m = re.search(r"(\d+) failed", line)
            if m:
                failed = int(m.group(1))
        total = passed + failed or len(hidden_tests)
        score = passed / total if total > 0 else 0.0
        LOGGER.info(
            "[qa] Hidden tests: %d/%d passed (score=%.2f)", passed, total, score
        )
        return {"ran": True, "score": score, "passed": passed, "total": total}
    except subprocess.TimeoutExpired:
        LOGGER.warning("[qa] Hidden tests timed out")
        return {"ran": True, "score": 0.0, "passed": 0, "total": len(hidden_tests)}
    except Exception as exc:
        LOGGER.warning("[qa] Hidden tests failed to run: %s", exc)
        return {"ran": False, "score": 1.0, "passed": 0, "total": 0}
    finally:
        import shutil as _shutil
        _shutil.rmtree(tmp_dir, ignore_errors=True)


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

    LOGGER.info(
        "[qa] Starting checks | task_id=%s | attempt=%d | branch=%s | artifact=%s",
        task_id, attempt_number, branch_name, artifact or "MISSING",
    )
    qa_start = time.monotonic()

    hidden_result: Dict[str, Any] = {"ran": False, "score": 1.0, "passed": 0, "total": 0}
    artifact_path = Path(artifact) if artifact else None
    if not artifact or not artifact_path or not artifact_path.exists():
        LOGGER.warning(
            "[qa] Artifact missing | task_id=%s | attempt=%d | artifact=%s",
            task_id, attempt_number, artifact or "None",
        )
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
        LOGGER.debug("[qa] Dependencies | task_id=%s | python=%s | ok=%s", task_id, python_path, dependency_info.get("ok", True))

        # 1. Syntax check (stdlib, instant — no subprocess)
        syntax_result = syntax_check(artifact_path)
        LOGGER.info("[qa] Syntax | task_id=%s | ok=%s", task_id, syntax_result.ok)
        if not syntax_result.ok:
            LOGGER.warning(
                "[qa] Syntax FAIL | task_id=%s | error=%.300s",
                task_id, syntax_result.output,
            )
            qa_logs = (
                f"Dependency install: {json.dumps(dependency_info, ensure_ascii=True)}\n\n"
                f"SYNTAX ERROR (pre-pytest):\n{syntax_result.output}\n"
                f"Details: {json.dumps(syntax_result.data, ensure_ascii=True)}\n"
            )
            status = "fail"
        else:
            # 2. Lint (ruff — skipped gracefully if not installed)
            lint_result = run_lint(artifact_path, python_path)
            lint_issues = len(lint_result.data.get("issues", [])) if not lint_result.error else 0
            LOGGER.info(
                "[qa] Lint | task_id=%s | ok=%s | issues=%d | skipped=%s",
                task_id, lint_result.ok, lint_issues, bool(lint_result.error),
            )

            # 3. Type check (mypy — skipped gracefully if not installed)
            type_result = run_typecheck(artifact_path, repo_path, python_path)
            type_errors = len(type_result.data.get("errors", [])) if not type_result.error else 0
            LOGGER.info(
                "[qa] TypeCheck | task_id=%s | ok=%s | errors=%d | skipped=%s",
                task_id, type_result.ok, type_errors, bool(type_result.error),
            )

            # 4. Pytest with coverage (replaces bare _run_pytest)
            LOGGER.info(
                "[qa] Pytest starting | task_id=%s | timeout=%ds | repo=%s",
                task_id, remaining_seconds or 120, repo_path,
            )
            pytest_start = time.monotonic()
            pytest_result = run_pytest_with_coverage(
                repo_path,
                python_path,
                timeout=remaining_seconds or 120,
                module_name=_project_slug(task),
            )
            cov = pytest_result.data.get("coverage")
            cov_pct = f"{cov['percent']:.1f}%" if cov else "n/a"
            LOGGER.info(
                "[qa] Pytest done | task_id=%s | ok=%s | elapsed=%.1fs | coverage=%s | "
                "passed=%s | failed=%s | errors=%s",
                task_id,
                pytest_result.ok,
                time.monotonic() - pytest_start,
                cov_pct,
                pytest_result.data.get("passed", "?"),
                pytest_result.data.get("failed", "?"),
                pytest_result.data.get("errors", "?"),
            )
            if not pytest_result.ok:
                # Log first 500 chars of pytest stdout for quick diagnosis
                stdout_snippet = (pytest_result.data.get("stdout") or "")[:500].strip()
                if stdout_snippet:
                    LOGGER.warning("[qa] Pytest output snippet | task_id=%s |\n%s", task_id, stdout_snippet)

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

        # --- Hidden tests (Phase 9): run only when public tests pass ---
        if status == "success" and task.get("hidden_tests"):
            hidden_result = _run_hidden_tests(task, repo_path, python_path)
            LOGGER.info(
                "[qa] Hidden tests | task_id=%s | ran=%s | score=%.3f | passed=%d/%d",
                task_id, hidden_result.get("ran"), hidden_result.get("score", 1.0),
                hidden_result.get("passed", 0), hidden_result.get("total", 0),
            )

        summary = _summarize_qa_result(description, qa_logs, status, attempt_number=attempt_number)

    LOGGER.info(
        "[qa] Checks done | task_id=%s | status=%s | elapsed=%.1fs | error_summary=%.200s",
        task_id, status, time.monotonic() - qa_start,
        (summary.get("error_summary") or "") if isinstance(summary, dict) else "",
    )

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

        LOGGER.info("[qa] Creating PR | task_id=%s | branch=%s", task_id, branch_name)
        pr_result = create_and_merge_github_pr(
            repo_path,
            branch_name,
            title=f"feat: {_task_slug(task)} (QA approved)",
            body=f"Automated merge after QA approval for task `{task.get('task_id', '')}`.",
        )

        if pr_result.get("ok"):
            pr_url = pr_result.get("pr_url")
            merge_commit = pr_result.get("merge_commit")
            LOGGER.info("[qa] PR merged via GitHub API | task_id=%s | pr=%s | commit=%s", task_id, pr_url, merge_commit)
            run_git(repo_path, ["checkout", "main"], check=False)
            run_git(repo_path, ["pull", "origin", "main"], check=False)
            run_git(repo_path, ["branch", "-d", branch_name], check=False)
            merge_push_result = {"ok": True, "transport": "github_api"}
        else:
            LOGGER.warning(
                "[qa] GitHub PR failed | task_id=%s | error=%s | falling back to local merge",
                task_id, pr_result.get("error"),
            )
            run_git(repo_path, ["checkout", "main"])
            run_git(
                repo_path,
                ["merge", "--no-ff", branch_name, "-m",
                 f"merge: {_task_slug(task)} after qa approval"],
            )
            merge_commit = run_git(repo_path, ["rev-parse", "HEAD"]).stdout.strip()
            merge_push_result = _sync_branch_to_remote(repo_path, "main")
            LOGGER.info(
                "[qa] Local merge done | task_id=%s | merge_commit=%s | push_ok=%s",
                task_id, merge_commit, merge_push_result.get("ok"),
            )
            run_git(repo_path, ["push", "origin", "--delete", branch_name], check=False)
            run_git(repo_path, ["branch", "-d", branch_name], check=False)

        # --- Skill extraction (fire-and-forget) ---
        _try_extract_skill(task, task_id, artifact)

        # --- Project notes: record completed task ---
        _append_project_note(
            repo_path,
            "Completed Tasks Summary",
            f"{task_id}: {_task_title(task)} — {Path(artifact).relative_to(repo_path) if artifact and Path(artifact).exists() else 'no artifact'}",
        )

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
        "hidden_score": hidden_result.get("score", 1.0),
        "hidden_tests_ran": hidden_result.get("ran", False),
    }


def _try_extract_skill(
    task: Dict[str, Any],
    task_id: str,
    artifact: str | None,
) -> None:
    """
    Attempt to extract a reusable skill from the successful dev artifact.

    Runs synchronously but is entirely fire-and-forget — any exception is
    logged as a warning and never propagates to the caller.
    """
    if not artifact:
        return
    artifact_path = Path(artifact)
    if not artifact_path.exists():
        return
    try:
        import asyncio
        from memory.db import MemoryDB
        from memory.vector_store import VectorMemory
        from memory.skill_extractor import SkillExtractor

        code = artifact_path.read_text(encoding="utf-8", errors="replace")
        episode_id = task.get("episode_id", "")

        db = MemoryDB()
        vector = VectorMemory()
        extractor = SkillExtractor(llm_fn=call_llm, vector_memory=vector, db=db)

        async def _run():
            await db.connect()
            try:
                await extractor.extract_from_solution(
                    task_id=task_id,
                    episode_id=episode_id,
                    code=code,
                )
            finally:
                await db.close()

        asyncio.run(_run())
    except Exception as exc:
        LOGGER.warning("[qa] Skill extraction skipped: %s", exc)


def _normalize_task(
    task: Dict[str, Any], project_context: Dict[str, Any]
) -> Dict[str, Any]:
    context = dict(project_context or {})
    context.setdefault("project_name", task.get("project_name") or _project_name(task))
    context.setdefault("project_repo_path", task.get("project_repo_path", ""))
    context.setdefault("project_description", task.get("project_description", ""))

    normalized = normalize_task_contract(task, project_context=context)
    normalized.setdefault("task_id", str(uuid.uuid4()))
    normalized.setdefault("assigned_agent", "dev")
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

    LOGGER.info("[PM AGENT] Step 1/3: Calling Architect LLM for design briefing...")
    architect_start = datetime.now()
    _PM_DESIGN_BRIEFING_SYSTEM = (
        "You are a solution architect. Given a project brief, output a concise technical analysis: "
        "key design constraints, recommended technology choices, major components (max 5 bullets), "
        "and top 3 risks. Be brief. Max 600 words. Plain text, no JSON."
    )
    architect_notes = call_llm(
        _PM_DESIGN_BRIEFING_SYSTEM,
        f"Project brief:\n{description_full[:3000]}",
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
        max_tokens=4096,
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
        plan = json.loads(_extract_json(pm_output))
    except json.JSONDecodeError as _jde:
        LOGGER.warning("[PM AGENT] Plan not valid JSON, using fallback plan — raw output (first 500 chars): %s", pm_output[:500])
        LOGGER.warning("[PM AGENT] JSONDecodeError: %s", str(_jde))
        LOGGER.warning("[PM AGENT] _extract_json result (first 500 chars): %s", _extract_json(pm_output)[:500])
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
        max_tokens=4096,
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
    _TASK_PROJ_DESC_MAX = 1200
    project_context = {
        "project_name": _project_name(task),
        "project_repo_path": artifact_paths["project_repo_path"],
        "project_description": description[:_TASK_PROJ_DESC_MAX],
        "github_url": task.get("github_url", ""),
    }
    normalized_tasks = _normalize_task_list(tasks, project_context)

    # Append architect_guidance to each task's input.context so dev agents see
    # cross-cutting standards (naming, error handling, tech constraints).
    guidance = task.get("architect_guidance", [])
    if guidance:
        guidance_str = "Architect guidelines: " + "; ".join(str(g) for g in guidance[:3])
        for t in normalized_tasks:
            existing = t.get("input", {}).get("context", "")
            t.setdefault("input", {})["context"] = (existing + "\n\n" + guidance_str)[:1400]

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

    recovery_cycle = task.get("recovery_cycle")
    stage = f"architect_recovery_{recovery_cycle}" if recovery_cycle else "architect"
    return _wrap_activity_result(workflow_id, stage, result, start_time)


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
    _PM_DESIGN_BRIEFING_SYSTEM = (
        "You are a solution architect. Given a project brief, output a concise technical analysis: "
        "key design constraints, recommended technology choices, major components (max 5 bullets), "
        "and top 3 risks. Be brief. Max 600 words. Plain text, no JSON."
    )
    architect_notes = call_llm(
        _PM_DESIGN_BRIEFING_SYSTEM,
        f"Project brief:\n{description[:3000]}",
    )
    delivery_summary = task.get("delivery_summary", "")
    failure_summary = task.get("failure_summary", [])
    recovery_current_state = f"Delivery goal: {delivery_summary}" if delivery_summary else ""
    analyst_notes = call_llm(
        ANALYST_SYSTEM_PROMPT,
        render_prompt(
            ANALYST_USER_PROMPT,
            current_state=recovery_current_state,
            event=json.dumps(failure_summary, indent=2, ensure_ascii=True),
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
        max_tokens=4096,
    )

    try:
        plan = json.loads(_extract_json(pm_output))
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


# ---------------------------------------------------------------------------
# Multi-candidate helpers (Phase 3)
# ---------------------------------------------------------------------------

_DEFAULT_NUM_CANDIDATES = int(os.environ.get("NUM_CANDIDATES", "1"))
_DEFAULT_EXPLORATION_RATE = float(os.environ.get("EXPLORATION_RATE", "0.3"))


def _get_strategies(num_candidates: int, exploration_rate: float) -> list[str]:
    """
    Return a list of strategy strings for epsilon-greedy candidate generation.

    Strategies: 'explore' (fresh, no skills) or 'exploit' (use top skills).
    At least one candidate is always 'explore'.
    """
    n_explore = max(1, round(num_candidates * exploration_rate))
    n_exploit = num_candidates - n_explore
    strategies = ["explore"] * n_explore + ["exploit"] * n_exploit
    return strategies


def _build_skill_context_for_candidate(
    task: Dict[str, Any],
    strategy: str,
) -> tuple[str, str]:
    """
    Return (skills_context, failure_patterns) strings for a given strategy.

    In 'explore' mode both are empty so the dev agent works from scratch.
    In 'exploit' mode we try to load skills synchronously from the registry
    (no DB/Qdrant call at dev time — uses local registry.json only).
    """
    if strategy == "explore":
        return "", ""

    # Exploit: pull active skills from local registry
    skills_context = ""
    failure_patterns = ""
    try:
        from skills import SkillRegistry
        registry = SkillRegistry()
        active = registry.list_active_skills()[:3]
        if active:
            lines = ["## Available Skills (use if relevant)"]
            for i, s in enumerate(active, 1):
                tags = ", ".join(s.get("tags", [])) or "—"
                lines.append(
                    f"{i}. **{s.get('name', '?')}** "
                    f"(success_rate: {s.get('success_rate', 0.0):.2f}, tags: {tags})"
                )
                desc = s.get("description", "")
                if desc:
                    lines.append(f"   {desc}")
                if s.get("code_path"):
                    lines.append(f"   Code: {s['code_path']}")
            skills_context = "\n".join(lines)
    except Exception as exc:
        LOGGER.debug("[dev] Could not load skill registry: %s", exc)

    return skills_context, failure_patterns


def _generate_single_candidate(
    task: Dict[str, Any],
    task_id: str,
    description: str,
    attempt_number: int,
    qa_feedback: Dict[str, Any] | None,
    strategy: str,
    candidate_idx: int,
) -> Dict[str, Any]:
    """
    Generate one dev solution with the given strategy.

    Uses CodeComposer in 'exploit' mode to compose skill code with LLM output.
    Returns a result dict identical to _generate_dev_artifact output.
    """
    skills_context, failure_patterns = _build_skill_context_for_candidate(task, strategy)

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
                "strategy": strategy,
                "candidate_idx": candidate_idx,
            },
            indent=2,
            ensure_ascii=True,
        ),
    )

    _cand_prompt = _build_dev_prompt(
        task, description, attempt_number, qa_feedback,
        skills_context=skills_context,
        failure_patterns=failure_patterns,
        strategy=strategy,
    )
    LOGGER.info(
        "[dev] LLM call (candidate %d) | task_id=%s | attempt=%d | strategy=%s | prompt_chars=%d",
        candidate_idx, task_id, attempt_number, strategy, len(_cand_prompt),
    )
    _cand_llm_start = time.monotonic()
    raw_output = call_llm(DEV_SYSTEM_PROMPT, _cand_prompt)
    LOGGER.info(
        "[dev] LLM done (candidate %d) | task_id=%s | elapsed=%.1fs | response_chars=%d",
        candidate_idx, task_id, time.monotonic() - _cand_llm_start, len(raw_output),
    )

    # In exploit mode, compose with relevant skills
    if strategy == "exploit":
        try:
            from skills import SkillRegistry
            from memory.skill import Skill
            from orchestrator.code_composer import CodeComposer
            registry = SkillRegistry()
            active = registry.list_active_skills()[:3]
            skill_objs = [Skill.from_dict({"id": s["id"], **s}) for s in active]
            raw_output = CodeComposer().compose(skill_objs, raw_output)
        except Exception as exc:
            LOGGER.debug("[dev] Skill composition skipped: %s", exc)

    multi = _parse_multi_file_output(raw_output)
    if multi:
        written_paths = []
        repo_resolved = repo_path.resolve()
        for rel_path, content in multi:
            fp = (repo_path / rel_path).resolve()
            if not str(fp).startswith(str(repo_resolved) + "/") and fp != repo_resolved:
                LOGGER.warning("[dev] Path traversal blocked (candidate): %s resolves outside repo", rel_path)
                continue
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(content)
            LOGGER.info("[dev] Candidate %d/%s written to %s (%d bytes)", candidate_idx, strategy, fp, len(content))
            written_paths.append(fp)
        if not written_paths:
            raise ValueError("All LLM-generated paths were rejected (path traversal guard)")
        file_path = written_paths[0]
        code = multi[0][1]
    else:
        code = _strip_code_fences(raw_output)
        file_path = _task_module_path(task, repo_path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(code)
        LOGGER.info(
            "[dev] Candidate %d/%s written to %s (%d bytes)",
            candidate_idx, strategy, file_path, len(code),
        )

    docs_dir = repo_path / "documents" / "pm"
    task_doc = _next_version_path(
        docs_dir, f"task_{_task_slug(task)}_implementation", ".md"
    )
    _write_markdown(
        task_doc,
        f"Implementation: {_task_title(task)}",
        f"## Branch\n`{branch_name}`\n\n## Attempt\n{attempt_number}\n\n"
        f"## Strategy\n{strategy}\n\n"
        f"## Artifact\n`{file_path.relative_to(repo_path)}`\n",
    )

    commit_sha = commit_all(
        repo_path,
        f"dev: implement {_task_slug(task)} attempt {attempt_number} [{strategy}]",
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
        "strategy": strategy,
        "candidate_idx": candidate_idx,
        "mode": "autofix" if qa_feedback else "initial",
        "branch": branch_name,
        "commit": commit_sha,
        "push": push_result,
        "project_repo_path": str(repo_path),
        "implementation_note": str(task_doc),
        "plan": plan_artifacts,
    }


@activity.defn
async def dev_activity(task: Dict[str, Any]) -> Dict[str, Any]:
    start_time = datetime.now()
    task = _load_activity_input(task)
    workflow_id = task.get("_workflow_id", "unknown")
    task_id = task.get("task_id", str(uuid.uuid4()))
    description = task.get("description", "")
    attempt = int(task.get("attempt_number", 1))
    num_candidates = int(task.get("num_candidates", _DEFAULT_NUM_CANDIDATES))
    exploration_rate = float(task.get("exploration_rate", _DEFAULT_EXPLORATION_RATE))

    LOGGER.info(
        "[dev] Starting | workflow=%s | task_id=%s | attempt=%d | candidates=%d",
        workflow_id, task_id, attempt, num_candidates,
    )

    strategies = _get_strategies(num_candidates, exploration_rate)
    qa_feedback = task.get("qa_feedback")

    if num_candidates == 1:
        # Fast path: single candidate, no asyncio overhead
        result = _generate_single_candidate(
            task, task_id, description, attempt, qa_feedback, strategies[0], 0
        )
        candidates = [result]
    else:
        import asyncio as _asyncio
        import functools

        loop = _asyncio.get_event_loop()
        futures = [
            loop.run_in_executor(
                None,
                functools.partial(
                    _generate_single_candidate,
                    task, task_id, description, attempt, qa_feedback, strategy, idx,
                ),
            )
            for idx, strategy in enumerate(strategies)
        ]
        raw = await _asyncio.gather(*futures, return_exceptions=True)
        candidates = []
        for i, outcome in enumerate(raw):
            if isinstance(outcome, BaseException):
                LOGGER.warning("[dev] Candidate %d failed: %s", i, outcome)
            else:
                candidates.append(outcome)

    if not candidates:
        # All candidates failed — return a minimal failure result
        result = {
            "task_id": task_id,
            "status": "error",
            "artifact": "",
            "code": "",
            "attempt": attempt,
            "error": "All candidate generations failed",
        }
    else:
        # Pick first successful candidate as primary result for backward compat
        result = candidates[0]
        result["candidates"] = candidates

    duration_ms = int((datetime.now() - start_time).total_seconds() * 1000)
    LOGGER.info(
        "[dev] Completed | workflow=%s | task_id=%s | duration=%dms | "
        "candidates=%d/%d | artifact=%s",
        workflow_id, task_id, duration_ms,
        len(candidates), num_candidates,
        result.get("artifact", "none"),
    )

    return _wrap_activity_result(workflow_id, f"dev_{task_id}", result, start_time)


# ---------------------------------------------------------------------------
# Phase 4 — Reward helpers
# ---------------------------------------------------------------------------

_QA_RESULTS_TOPIC = "qa.results"
_REWARD_COMPUTED_TOPIC = "reward.computed"


def _attach_reward(qa_result: Dict[str, Any], code: str) -> Dict[str, Any]:
    """
    Compute and attach a reward scalar to a QA result dict.

    Uses pytest junit counts if available (via run_pytest_with_coverage),
    otherwise falls back to returncode-based inference.
    Falls back silently if reward computation fails.
    """
    try:
        from memory.reward import RewardEngine, QAMetrics

        pytest_data = qa_result.get("pytest_data", {})
        execution_time_ms = float(qa_result.get("execution_time_ms", 0.0))

        engine = RewardEngine()
        metrics = RewardEngine.metrics_from_pytest_result(
            pytest_data,
            execution_time_ms=execution_time_ms,
        )
        # Fallback: if no pytest data, infer from status
        if metrics.tests_total == 0:
            if qa_result.get("status") == "success":
                metrics = QAMetrics(tests_passed=1, tests_failed=0, tests_total=1)
            else:
                metrics = QAMetrics(tests_passed=0, tests_failed=1, tests_total=1)

        reward = engine.compute(metrics, code or "")
        qa_result["reward"] = reward
        qa_result["qa_metrics"] = {
            "tests_passed": metrics.tests_passed,
            "tests_failed": metrics.tests_failed,
            "tests_total": metrics.tests_total,
            "coverage": metrics.coverage,
            "execution_time_ms": metrics.execution_time_ms,
            "peak_memory_mb": metrics.peak_memory_mb,
        }
    except Exception as exc:
        LOGGER.warning("[qa] Reward computation skipped: %s", exc)
        qa_result.setdefault("reward", 0.0)
    return qa_result


async def _apply_reward_and_regression(
    result: Dict[str, Any],
    task_id: str,
    episode_id: str,
    iteration: int,
    kafka_producer: Any | None = None,
) -> Dict[str, Any]:
    """
    Run regression detection via EpisodicMemory and publish Kafka events.
    Always returns result (with is_regression flag added).
    """
    reward = result.get("reward", 0.0)
    is_regression = False

    # Regression detection
    try:
        import os as _os
        mem_dsn = _os.environ.get(
            "MEMORY_DB_URL",
            "postgresql://temporal:temporal@localhost:5432/ai_factory_memory",
        )
        from memory.db import MemoryDB
        from memory.episodic import EpisodicMemory
        db = MemoryDB(dsn=mem_dsn)
        await db.connect()
        try:
            mem = EpisodicMemory(db)
            is_regression = await mem.check_regression(task_id, reward)
            if is_regression:
                LOGGER.warning(
                    "[qa] Regression detected for %s: reward=%.4f", task_id, reward
                )
        finally:
            await db.close()
    except Exception as exc:
        LOGGER.debug("[qa] Regression check skipped: %s", exc)

    result["is_regression"] = is_regression

    # Kafka publishing (fire-and-forget)
    _publish_qa_reward_events(
        kafka_producer=kafka_producer,
        episode_id=episode_id,
        task_id=task_id,
        iteration=iteration,
        result=result,
        reward=reward,
        is_regression=is_regression,
    )

    return result


def _publish_qa_reward_events(
    kafka_producer: Any | None,
    episode_id: str,
    task_id: str,
    iteration: int,
    result: Dict[str, Any],
    reward: float,
    is_regression: bool,
) -> None:
    """Publish qa.results and reward.computed events to Kafka (fire-and-forget)."""
    if kafka_producer is None:
        return

    from datetime import timezone
    ts = datetime.now(timezone.utc).isoformat()
    metrics = result.get("qa_metrics", {})

    qa_payload = {
        "episode_id": episode_id,
        "task_id": task_id,
        "iteration": iteration,
        "tests_passed": metrics.get("tests_passed", 0),
        "tests_failed": metrics.get("tests_failed", 0),
        "tests_total": metrics.get("tests_total", 0),
        "coverage": metrics.get("coverage", 0.0),
        "execution_time_ms": metrics.get("execution_time_ms", 0.0),
        "peak_memory_mb": metrics.get("peak_memory_mb", 0.0),
        "reward": reward,
        "timestamp": ts,
    }
    reward_payload = {
        "episode_id": episode_id,
        "task_id": task_id,
        "iteration": iteration,
        "reward": reward,
        "is_regression": is_regression,
        "is_best": not is_regression,
        "timestamp": ts,
    }

    for topic, payload in [
        (_QA_RESULTS_TOPIC, qa_payload),
        (_REWARD_COMPUTED_TOPIC, reward_payload),
    ]:
        try:
            kafka_producer.send(topic, payload)
        except Exception as exc:
            LOGGER.warning("[qa] Kafka publish to %s failed: %s", topic, exc)


@activity.defn
async def qa_activity(task: Dict[str, Any]) -> Dict[str, Any]:
    start_time = datetime.now()
    task = _load_activity_input(task)
    workflow_id = task.get("_workflow_id", "unknown")
    task_id = task.get("task_id", str(uuid.uuid4()))
    attempt = int(task.get("attempt_number", 1))

    # Multi-candidate support (Phase 3): if 'candidates' is present, try each
    # in order and return the first that passes, or the last result if all fail.
    # Backward-compatible: falls back to single 'artifact' path when no candidates.
    candidates: List[Dict[str, Any]] = task.get("candidates", [])
    if not candidates:
        single_artifact = task.get("artifact", "")
        if single_artifact:
            candidates = [{"artifact": single_artifact}]

    if not candidates:
        candidates = [{"artifact": ""}]

    LOGGER.info(
        "[qa] Starting | workflow=%s | task_id=%s | candidates=%d | attempt=%d",
        workflow_id, task_id, len(candidates), attempt,
    )

    result = None
    best_reward = -1.0
    for idx, candidate in enumerate(candidates):
        artifact = candidate.get("artifact", "")
        LOGGER.info(
            "[qa] Trying candidate %d/%d | artifact=%s",
            idx + 1, len(candidates), artifact[:60] if artifact else "none",
        )
        candidate_result = _run_qa_for_artifact(
            task,
            task_id,
            task.get("description", ""),
            artifact,
            attempt,
        )
        candidate_result["candidate_idx"] = idx
        candidate_result["candidate_strategy"] = candidate.get("strategy", "explore")

        # --- Phase 4: compute reward for this candidate ---
        candidate_result = _attach_reward(
            candidate_result,
            candidate.get("code", ""),
        )
        # --- Phase 9: apply hidden test score to reward (hidden tests = 30% weight) ---
        if candidate_result.get("hidden_tests_ran"):
            hidden_score = candidate_result.get("hidden_score", 1.0)
            candidate_result["reward"] = candidate_result.get("reward", 0.0) * (0.7 + 0.3 * hidden_score)
            LOGGER.info("[qa] Reward adjusted for hidden tests: score=%.2f → final_reward=%.4f",
                        hidden_score, candidate_result["reward"])
        candidate_reward = candidate_result.get("reward", 0.0)

        if result is None or candidate_reward > best_reward:
            best_reward = candidate_reward
            result = candidate_result

        if candidate_result.get("status") == "success":
            if len(candidates) == 1:
                LOGGER.info("[qa] Candidate %d passed — using as best result", idx + 1)
                break

    # --- Phase 4: regression detection + Kafka publishing ---
    episode_id = task.get("episode_id", "")
    result = await _apply_reward_and_regression(
        result, task_id, episode_id, attempt,
        kafka_producer=task.get("_kafka_producer"),
    )

    duration_ms = int((datetime.now() - start_time).total_seconds() * 1000)
    LOGGER.info(
        "[qa] Completed | workflow=%s | task_id=%s | duration=%dms | "
        "status=%s | reward=%.4f | regression=%s",
        workflow_id, task_id, duration_ms,
        result.get("status", "unknown"),
        result.get("reward", 0.0),
        result.get("is_regression", False),
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
    project_goal = input_data.get("project_goal", "") if isinstance(input_data, dict) else ""
    delivery_summary = input_data.get("delivery_summary", "") if isinstance(input_data, dict) else ""
    analyst_guidance = input_data.get("analyst_guidance", []) if isinstance(input_data, dict) else []

    current_state_parts = []
    if project_goal:
        current_state_parts.append(f"Project goal: {project_goal}")
    if delivery_summary:
        current_state_parts.append(f"PM delivery summary: {delivery_summary}")
    if analyst_guidance:
        current_state_parts.append("PM analyst guidance:\n" + "\n".join(f"- {g}" for g in analyst_guidance[:4]))
    current_state = "\n\n".join(current_state_parts)

    new_state = call_llm(
        ANALYST_SYSTEM_PROMPT,
        render_prompt(
            ANALYST_USER_PROMPT,
            current_state=current_state,
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
        "[process_task] Running dev | workflow=%s | task_id=%s | attempt=%d | mode=%s",
        workflow_id, task_id, next_attempt,
        "autofix" if qa_feedback else "initial",
    )
    dev_result = _generate_dev_artifact(
        task=task,
        task_id=task_id,
        description=description,
        attempt_number=next_attempt,
        qa_feedback=qa_feedback,
    )
    LOGGER.info(
        "[process_task] Dev done | workflow=%s | task_id=%s | artifact=%s | commit=%s",
        workflow_id, task_id, dev_result.get("artifact"), dev_result.get("commit"),
    )
    LOGGER.info(
        "[process_task] Running QA | workflow=%s | task_id=%s | attempt=%d | remaining=%ss",
        workflow_id, task_id, next_attempt, _remaining_time_seconds(activity_start_time),
    )
    qa_result = _run_qa_for_artifact(
        task,
        task_id,
        description,
        dev_result["artifact"],
        next_attempt,
        remaining_seconds=_remaining_time_seconds(activity_start_time),
    )
    _qa_summary = qa_result.get("summary") or {}
    LOGGER.info(
        "[process_task] QA done | workflow=%s | task_id=%s | status=%s | error=%.200s",
        workflow_id, task_id, qa_result.get("status"),
        (_qa_summary.get("error_summary") or "") if isinstance(_qa_summary, dict) else "",
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
        _prev_summary = qa_result.get("summary") or {}
        _prev_error = (_prev_summary.get("error_summary") or "") if isinstance(_prev_summary, dict) else ""
        LOGGER.info(
            "[process_task] Self-healing triggered | workflow=%s | task_id=%s | attempt=%d/%d | reason=%.200s",
            workflow_id, task_id, next_attempt, MAX_SELF_HEALING_ATTEMPTS + 1, _prev_error,
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
        LOGGER.info(
            "[process_task] Dev done (heal) | workflow=%s | task_id=%s | artifact=%s | commit=%s",
            workflow_id, task_id, dev_result.get("artifact"), dev_result.get("commit"),
        )
        qa_result = _run_qa_for_artifact(
            task,
            task_id,
            description,
            dev_result["artifact"],
            next_attempt,
            remaining_seconds=_remaining_time_seconds(activity_start_time),
        )
        _qa_summary = qa_result.get("summary") or {}
        LOGGER.info(
            "[process_task] QA done (heal) | workflow=%s | task_id=%s | status=%s | error=%.200s",
            workflow_id, task_id, qa_result.get("status"),
            (_qa_summary.get("error_summary") or "") if isinstance(_qa_summary, dict) else "",
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

    # --- Project notes: record failure after all retries exhausted ---
    if final_status == "fail":
        _fail_summary = qa_result.get("summary") or {}
        _fail_error = (_fail_summary.get("error_summary") or "") if isinstance(_fail_summary, dict) else ""
        _fail_fix = (_fail_summary.get("fix_suggestion") or "") if isinstance(_fail_summary, dict) else ""
        _note = f"{task_id}: {_task_title(task)} FAILED after {len(healing_history)} attempt(s)"
        if _fail_error:
            _note += f" — {_fail_error[:200]}"
        if _fail_fix:
            _note += f" | suggested fix: {_fail_fix[:150]}"
        _append_project_note(repo_path, "Known Failure Patterns", _note)

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
    import requests as _requests  # local import keeps activities.py from requiring requests at import time

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


# ---------------------------------------------------------------------------
# Phase 5 — Learning Loop activities
# ---------------------------------------------------------------------------

@activity.defn
async def extract_skill_activity(input_data: Dict[str, Any]) -> Dict[str, Any]:
    """Extract a reusable skill from a successful QA result.

    Wraps SkillExtractor.extract_from_solution() as a proper Temporal activity
    so LearningWorkflow can await it and track the extracted skill count.

    Input keys:
        task_id     — task identifier
        episode_id  — current episode
        artifact    — path to the generated code file
        code        — inline code (fallback if artifact unreadable)

    Returns:
        {"extracted": True/False, "skill_id": str | None}
    """
    task_id = input_data.get("task_id", "")
    episode_id = input_data.get("episode_id", "")
    artifact = input_data.get("artifact", "")
    code = input_data.get("code", "")

    if not code and artifact:
        try:
            code = Path(artifact).read_text(encoding="utf-8", errors="replace")
        except Exception:
            pass

    if not code:
        return {"extracted": False, "skill_id": None}

    try:
        from memory.db import MemoryDB
        from memory.vector_store import VectorMemory
        from memory.skill_extractor import SkillExtractor

        db = MemoryDB()
        vector = VectorMemory()
        extractor = SkillExtractor(llm_fn=call_llm, vector_memory=vector, db=db)

        await db.connect()
        try:
            skill = await extractor.extract_from_solution(
                task_id=task_id,
                episode_id=episode_id,
                code=code,
            )
        finally:
            await db.close()

        skill_id = skill.id if skill is not None else None
        return {"extracted": skill is not None, "skill_id": skill_id}

    except Exception as exc:
        LOGGER.warning("[extract_skill] Skipped: %s", exc)
        return {"extracted": False, "skill_id": None}


@activity.defn
async def policy_update_activity(input_data: Dict[str, Any]) -> Dict[str, Any]:
    """Update Dev agent policy after an episode completes.

    Wraps PolicyUpdater.update() — adjusts prompt examples, skill weights,
    and exploration rate.  Changes apply to the NEXT episode.

    Input keys:
        episode_id      — just-completed episode
        best_solution   — dict with reward, artifact, code, skills_used
        best_reward     — float
        replay_buffer_path — optional override for persistence path
    """
    episode_id = input_data.get("episode_id", "")
    best_solution = input_data.get("best_solution")
    best_reward = float(input_data.get("best_reward", 0.0))
    buffer_path_str = input_data.get(
        "replay_buffer_path",
        str(Path(os.getenv("AI_FACTORY_WORKSPACE", "workspace"))
            / ".ai_factory" / "replay_buffer.json"),
    )

    try:
        from memory.replay_buffer import ReplayBuffer, BufferedSolution
        from memory.policy_updater import PolicyUpdater

        buf_path = Path(buffer_path_str)
        buf = ReplayBuffer.load(buf_path)

        if best_solution is not None:
            buf.add(
                BufferedSolution(
                    task_id=best_solution.get("task_id", ""),
                    episode_id=episode_id,
                    iteration=int(best_solution.get("iteration", 0)),
                    reward=best_reward,
                    artifact=best_solution.get("artifact", ""),
                    code=best_solution.get("code", ""),
                    skills_used=best_solution.get("skills_used", []),
                )
            )
            buf.save(buf_path)

        updater = PolicyUpdater(replay_buffer=buf)
        await updater.update(
            episode_id=episode_id,
            best_solution=best_solution,
            best_reward=best_reward,
        )

        LOGGER.info(
            "[policy_update] episode=%s reward=%.4f buffer=%s",
            episode_id, best_reward, buf.size(),
        )
        return {"ok": True, "buffer_size": buf.size()}

    except Exception as exc:
        LOGGER.warning("[policy_update] Skipped: %s", exc)
        return {"ok": False, "error": str(exc)}


@activity.defn
async def rearchitect_failed_task_activity(task_input: Dict[str, Any]) -> Dict[str, Any]:
    """Re-think the implementation approach for a QA-failed task.

    Called by the workflow when a task fails QA after all dev self-healing retries.
    Uses the architect LLM with the full QA failure context to produce 1–3 revised
    sub-tasks that address the specific root causes.

    Input keys:
        task        — original task dict that failed QA
        qa_failure  — QA result dict: {status, logs, summary{error_summary, root_cause, fix_suggestion}}
        _workflow_id — workflow ID for context file naming
    """
    start_time = time.time()
    original_task = task_input.get("task", {})
    qa_failure = task_input.get("qa_failure", {})
    workflow_id = task_input.get("_workflow_id", "unknown")
    task_id = original_task.get("task_id", "unknown")

    LOGGER.info("[rearchitect] Re-architecting failed task %s", task_id)

    summary = qa_failure.get("summary") or {}
    if isinstance(summary, str):
        try:
            summary = json.loads(summary)
        except Exception:
            summary = {"error_summary": summary}

    user_prompt = render_prompt(
        REARCHITECT_USER_PROMPT,
        task_json=json.dumps(original_task, indent=2, ensure_ascii=True),
        error_summary=summary.get("error_summary", "")[:1000],
        root_cause=summary.get("root_cause", "")[:1000],
        fix_suggestion=summary.get("fix_suggestion", "")[:1000],
        qa_logs=(qa_failure.get("logs") or "")[:4000],
    )

    try:
        llm_response = call_llm(
            system=REARCHITECT_SYSTEM_PROMPT,
            user=user_prompt,
            max_tokens=4096,
        )
    except Exception as exc:
        LOGGER.warning("[rearchitect] LLM call failed for task %s: %s — falling back to original", task_id, exc)
        llm_response = ""

    revised_tasks = _ensure_task_list(llm_response) if llm_response else []
    if not revised_tasks:
        LOGGER.warning("[rearchitect] No revised tasks produced for %s — keeping original", task_id)
        revised_tasks = [original_task]

    project_context = {
        k: original_task.get(k, "")
        for k in ("project_name", "project_repo_path", "github_url", "project_description")
    }
    revised_tasks = _normalize_task_list(revised_tasks, project_context)

    LOGGER.info("[rearchitect] Task %s → %d revised sub-task(s)", task_id, len(revised_tasks))

    result = {
        "original_task_id": task_id,
        "status": "success",
        "tasks": revised_tasks,
        "project_name": original_task.get("project_name"),
        "project_repo_path": original_task.get("project_repo_path"),
    }
    return _wrap_activity_result(workflow_id, f"rearchitect_{task_id}", result, start_time)


@activity.defn
async def skill_optimization_activity(input_data: Dict[str, Any]) -> Dict[str, Any]:
    """Run a skill optimization cycle: refactor + merge + prune.

    Triggered every SKILL_OPTIMIZE_EVERY_N episodes by LearningWorkflow.

    Input keys:
        episode_count — total completed episodes (used for trigger check)

    Returns:
        {"ok": True/False, "refactored": N, "merged": M, "pruned": K}
    """
    episode_count = int(input_data.get("episode_count", 0))
    LOGGER.info("[skill_optimization] Starting cycle (episode_count=%d)", episode_count)

    try:
        from memory.db import MemoryDB
        from memory.vector_store import VectorMemory
        from memory.skill_optimizer import SkillOptimizer
        from skills import SkillRegistry

        db = MemoryDB()
        await db.connect()
        try:
            vector = VectorMemory()
            registry = SkillRegistry()
            optimizer = SkillOptimizer(
                db=db,
                vector_memory=vector,
                llm_fn=call_llm,
                skill_registry=registry,
            )
            stats = await optimizer.run_optimization_cycle(episode_count)
        finally:
            await db.close()

        LOGGER.info("[skill_optimization] Done: %s", stats)
        return {"ok": True, **stats}

    except Exception as exc:
        LOGGER.warning("[skill_optimization] Skipped: %s", exc)
        return {"ok": False, "error": str(exc)}
