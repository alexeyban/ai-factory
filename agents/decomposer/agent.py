"""Decomposer agent for breaking large tasks into atomic sub-tasks."""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Mapping

from agents.base.agent_base import BaseAgent
from shared.llm import call_llm
from shared.prompts.loader import load_prompt, render_prompt

TOKEN_LIMIT = 8000
SYSTEM_PROMPT = load_prompt("decomposer", "system")
USER_PROMPT_TEMPLATE = load_prompt("decomposer", "user")

DEFAULT_TASK_TYPES = {"feature", "bugfix", "refactor", "setup", "test", "docs"}


def estimate_tokens(text: str) -> int:
    return max(0, (len(text) + 3) // 4)


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _mapping_to_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _project_context_text(project_context: Any) -> str:
    if project_context is None:
        return ""
    if isinstance(project_context, str):
        return project_context
    if isinstance(project_context, Mapping):
        return json.dumps(project_context, indent=2, ensure_ascii=True)
    return str(project_context)


def normalize_task_contract(
    task: Mapping[str, Any] | Dict[str, Any] | str,
    project_context: Any = None,
) -> Dict[str, Any]:
    normalized: Dict[str, Any]
    if isinstance(task, Mapping):
        normalized = dict(task)
    else:
        normalized = {"description": str(task)}

    for internal_key in ("project_context", "_context_file", "_workflow_id"):
        normalized.pop(internal_key, None)

    description = str(
        normalized.get("description") or normalized.get("title") or ""
    ).strip()
    title = str(
        normalized.get("title")
        or description
        or normalized.get("task_id")
        or "Untitled task"
    ).strip()

    dependencies = _as_list(normalized.get("dependencies"))
    acceptance_criteria = [
        str(item) for item in _as_list(normalized.get("acceptance_criteria"))
    ]

    input_data = _mapping_to_dict(normalized.get("input"))
    output_data = _mapping_to_dict(normalized.get("output"))
    verification_data = _mapping_to_dict(normalized.get("verification"))

    task_description = description or title
    estimated_size = str(normalized.get("estimated_size") or "").lower().strip()
    if estimated_size not in {"small", "medium", "large"}:
        token_estimate = estimate_tokens(task_description)
        if token_estimate <= 400:
            estimated_size = "small"
        elif token_estimate <= 1500:
            estimated_size = "medium"
        else:
            estimated_size = "large"

    context_text = _project_context_text(project_context)
    input_context = str(input_data.get("context") or context_text or task_description)
    input_files = [str(item) for item in _as_list(input_data.get("files"))]
    output_files = [str(item) for item in _as_list(output_data.get("files"))]
    output_artifacts = [str(item) for item in _as_list(output_data.get("artifacts"))]
    verification_criteria = [
        str(item) for item in _as_list(verification_data.get("criteria"))
    ]

    normalized["task_id"] = str(normalized.get("task_id") or normalized.get("id") or "")
    normalized["title"] = title
    normalized["description"] = task_description
    normalized["type"] = str(normalized.get("type") or "feature").lower()
    normalized["dependencies"] = [str(item) for item in dependencies]
    normalized["input"] = {
        "files": input_files,
        "context": input_context,
    }
    normalized["output"] = {
        "files": output_files,
        "artifacts": output_artifacts,
        "expected_result": str(output_data.get("expected_result") or task_description),
    }
    normalized["verification"] = {
        "method": str(verification_data.get("method") or "review"),
        "test_file": verification_data.get("test_file"),
        "criteria": verification_criteria,
    }
    normalized["acceptance_criteria"] = acceptance_criteria
    normalized["estimated_size"] = estimated_size
    if "can_parallelize" in normalized:
        normalized["can_parallelize"] = bool(normalized.get("can_parallelize"))
    else:
        normalized["can_parallelize"] = not dependencies

    if normalized["type"] not in DEFAULT_TASK_TYPES:
        normalized["type"] = "feature"

    if not normalized["task_id"]:
        normalized["task_id"] = str(uuid.uuid4())

    if not normalized["input"]["context"]:
        normalized["input"]["context"] = context_text or task_description

    if not normalized["output"]["expected_result"]:
        normalized["output"]["expected_result"] = task_description

    if normalized["verification"]["test_file"] is not None:
        normalized["verification"]["test_file"] = str(
            normalized["verification"]["test_file"]
        )

    return normalized


def _clean_llm_json(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.removeprefix("```json").removeprefix("```").strip()
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3].strip()
    return cleaned


def _extract_tasks(
    raw_output: str, fallback_task: Dict[str, Any]
) -> List[Dict[str, Any]]:
    cleaned = _clean_llm_json(raw_output)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        return [normalize_task_contract(fallback_task)]

    if isinstance(parsed, dict):
        tasks = parsed.get("tasks") or parsed.get("execution_plan") or []
    else:
        tasks = parsed

    if not isinstance(tasks, list) or not tasks:
        return [normalize_task_contract(fallback_task)]

    normalized_tasks: List[Dict[str, Any]] = []
    fallback_id = str(fallback_task.get("task_id") or "task")
    for index, item in enumerate(tasks, start=1):
        if not isinstance(item, Mapping):
            continue
        candidate = dict(item)
        candidate.setdefault("task_id", f"{fallback_id}-{index:02d}")
        normalized_tasks.append(
            normalize_task_contract(candidate, project_context=fallback_task)
        )

    return normalized_tasks or [normalize_task_contract(fallback_task)]


class DecomposerAgent(BaseAgent):
    def __init__(self, token_limit: int = TOKEN_LIMIT) -> None:
        self.token_limit = token_limit

    def estimate_tokens(self, text: str) -> int:
        return estimate_tokens(text)

    def build_prompt(
        self, task: Mapping[str, Any] | Dict[str, Any], project_context: Any = None
    ) -> str:
        normalized_task = normalize_task_contract(task, project_context=project_context)
        return render_prompt(
            USER_PROMPT_TEMPLATE,
            task_description=json.dumps(normalized_task, indent=2, ensure_ascii=True),
            project_context=_project_context_text(project_context),
        )

    def should_decompose(
        self, task: Mapping[str, Any] | Dict[str, Any], project_context: Any = None
    ) -> bool:
        prompt = self.build_prompt(task, project_context=project_context)
        return estimate_tokens(prompt) > self.token_limit

    def decompose(
        self, task: Mapping[str, Any] | Dict[str, Any], project_context: Any = None
    ) -> List[Dict[str, Any]]:
        prompt = self.build_prompt(task, project_context=project_context)
        raw_output = call_llm(SYSTEM_PROMPT, prompt)
        return _extract_tasks(
            raw_output, normalize_task_contract(task, project_context)
        )

    def handle(self, task: Any):
        if isinstance(task, Mapping):
            project_context = task.get("project_context")
            return self.decompose(task, project_context=project_context)
        return [normalize_task_contract(task)]


if __name__ == "__main__":
    DecomposerAgent().run()
