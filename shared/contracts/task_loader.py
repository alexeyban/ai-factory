"""
Task contract loader and validator.

Validates task dicts against task_schema.yaml.
Maintains backward compatibility with the existing normalize_task_contract
used in agents/decomposer/agent.py.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import yaml

LOGGER = logging.getLogger(__name__)

_SCHEMA_PATH = Path(__file__).parent / "task_schema.yaml"

# Required fields for a minimal valid task
_REQUIRED_FIELDS = {"task_id", "type"}

# Fields that must be present in backward-compat mode (may be empty)
_LEGACY_FIELDS = {"title", "description", "dependencies", "input", "output"}


class TaskValidationError(ValueError):
    """Raised when a task dict fails validation."""


def _load_schema() -> dict:
    """Load YAML schema once and cache."""
    if not hasattr(_load_schema, "_cache"):
        _load_schema._cache = yaml.safe_load(_SCHEMA_PATH.read_text(encoding="utf-8"))
    return _load_schema._cache


def validate_task(task: dict[str, Any]) -> bool:
    """
    Validate task against the schema.

    Returns True if valid. Raises TaskValidationError on failure.
    Tries jsonschema first; falls back to manual required-field check.
    """
    if not isinstance(task, dict):
        raise TaskValidationError(f"Task must be a dict, got {type(task).__name__}")

    missing = _REQUIRED_FIELDS - task.keys()
    if missing:
        raise TaskValidationError(f"Missing required fields: {missing}")

    task_type = task.get("type", "")
    allowed_types = {
        "dev", "qa", "refactor", "docs",
        "feature", "bugfix", "setup", "test",
    }
    if task_type and task_type not in allowed_types:
        raise TaskValidationError(
            f"Invalid task type '{task_type}'. Allowed: {allowed_types}"
        )

    try:
        import jsonschema
        schema = _load_schema()
        jsonschema.validate(instance=task, schema=schema)
    except ImportError:
        # jsonschema not installed — basic check already done above
        pass
    except Exception as exc:
        raise TaskValidationError(f"Schema validation failed: {exc}") from exc

    return True


def load_task(data: dict[str, Any]) -> dict[str, Any]:
    """
    Validate and normalise a task dict.

    Adds missing optional fields with defaults so downstream code can
    always access them without KeyError.  Does NOT modify the original dict.
    """
    validate_task(data)

    defaults: dict[str, Any] = {
        "title": data.get("task_id", ""),
        "description": "",
        "input_spec": {},
        "tests": [],
        "hidden_tests": [],
        "metrics": {},
        "constraints": {
            "max_tokens": 8000,
            "timeout_sec": 900,
            "max_fix_attempts": 2,
        },
        "dependencies": [],
        "input": {},
        "output": {},
        "verification": {},
        "acceptance_criteria": [],
        "can_parallelize": True,
        "iteration": 0,
    }

    result = {**defaults, **data}
    return result


def task_to_json(task: dict[str, Any]) -> str:
    """Serialise a task dict to JSON string."""
    return json.dumps(task, ensure_ascii=False, indent=2)


def task_from_json(raw: str) -> dict[str, Any]:
    """Deserialise a task from JSON string and validate."""
    data = json.loads(raw)
    return load_task(data)
