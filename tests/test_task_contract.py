"""Tests for shared/contracts/ — task loader and Kafka contract message."""
import json
import pytest

from shared.contracts.task_loader import (
    TaskValidationError,
    load_task,
    validate_task,
    task_to_json,
    task_from_json,
)
from shared.contracts.kafka_task_contract import TaskContractMessage


# ---------------------------------------------------------------------------
# validate_task
# ---------------------------------------------------------------------------

def test_validate_valid_task():
    task = {"task_id": "T001", "type": "dev"}
    assert validate_task(task) is True


def test_validate_valid_task_all_types():
    for t in ("dev", "qa", "refactor", "docs", "feature", "bugfix", "setup", "test"):
        assert validate_task({"task_id": "T001", "type": t}) is True


def test_validate_missing_task_id():
    with pytest.raises(TaskValidationError, match="task_id"):
        validate_task({"type": "dev"})


def test_validate_missing_type():
    with pytest.raises(TaskValidationError, match="type"):
        validate_task({"task_id": "T001"})


def test_validate_missing_both_required():
    with pytest.raises(TaskValidationError):
        validate_task({})


def test_validate_not_a_dict():
    with pytest.raises(TaskValidationError):
        validate_task("not a dict")


def test_validate_invalid_type_value():
    with pytest.raises(TaskValidationError, match="Invalid task type"):
        validate_task({"task_id": "T001", "type": "unknown_type"})


def test_validate_extra_fields_allowed():
    task = {
        "task_id": "T001",
        "type": "dev",
        "custom_field": "value",
        "another": 42,
    }
    assert validate_task(task) is True


# ---------------------------------------------------------------------------
# load_task
# ---------------------------------------------------------------------------

def test_load_task_returns_dict():
    result = load_task({"task_id": "T001", "type": "dev"})
    assert isinstance(result, dict)


def test_load_task_adds_defaults():
    result = load_task({"task_id": "T001", "type": "dev"})
    assert result["dependencies"] == []
    assert result["tests"] == []
    assert result["hidden_tests"] == []
    assert result["input"] == {}
    assert result["output"] == {}
    assert result["iteration"] == 0
    assert result["can_parallelize"] is True


def test_load_task_preserves_existing_values():
    result = load_task({
        "task_id": "T001",
        "type": "feature",
        "title": "My Task",
        "dependencies": ["T000"],
        "iteration": 2,
    })
    assert result["title"] == "My Task"
    assert result["dependencies"] == ["T000"]
    assert result["iteration"] == 2


def test_load_task_invalid_raises():
    with pytest.raises(TaskValidationError):
        load_task({"type": "dev"})  # missing task_id


def test_load_task_does_not_mutate_input():
    original = {"task_id": "T001", "type": "dev"}
    load_task(original)
    assert "dependencies" not in original


# ---------------------------------------------------------------------------
# Backward compatibility with decomposer normalize_task_contract
# ---------------------------------------------------------------------------

def test_backward_compatibility_full_contract():
    """A task produced by normalize_task_contract must pass validation."""
    from agents.decomposer.agent import normalize_task_contract

    task = {
        "task_id": "T001",
        "title": "Setup project",
        "description": "Init repo and dependencies",
        "type": "setup",
        "dependencies": [],
        "input": {"files": [], "context": ""},
        "output": {"files": [], "artifacts": [], "expected_result": ""},
        "verification": {"method": "manual", "test_file": None, "criteria": []},
        "acceptance_criteria": [],
        "estimated_size": "small",
        "can_parallelize": True,
    }
    normalised = normalize_task_contract(task)
    assert validate_task(normalised) is True


def test_backward_compatibility_assigned_agent():
    """Tasks with assigned_agent field pass validation."""
    task = {
        "task_id": "T002",
        "type": "feature",
        "assigned_agent": "dev",
        "title": "Implement feature",
    }
    assert validate_task(task) is True


# ---------------------------------------------------------------------------
# JSON serialization
# ---------------------------------------------------------------------------

def test_task_to_json_roundtrip():
    task = {"task_id": "T001", "type": "dev", "title": "Test"}
    loaded = load_task(task)
    raw = task_to_json(loaded)
    restored = json.loads(raw)
    assert restored["task_id"] == "T001"
    assert restored["type"] == "dev"


def test_task_from_json_roundtrip():
    task = {"task_id": "T001", "type": "qa"}
    raw = json.dumps(task)
    result = task_from_json(raw)
    assert result["task_id"] == "T001"
    assert result["type"] == "qa"


# ---------------------------------------------------------------------------
# TaskContractMessage
# ---------------------------------------------------------------------------

def test_task_contract_message_creation():
    msg = TaskContractMessage(
        task_id="T001",
        episode_id="ep_20260324_112233_a1b2c3d4",
        payload={"task_id": "T001", "type": "dev"},
    )
    assert msg.task_id == "T001"
    assert msg.schema_version == "1.0"
    assert msg.timestamp != ""


def test_task_contract_message_to_json():
    msg = TaskContractMessage(
        task_id="T001",
        episode_id="ep_test",
        payload={"task_id": "T001", "type": "dev"},
    )
    raw = msg.to_json()
    data = json.loads(raw)
    assert data["task_id"] == "T001"
    assert data["episode_id"] == "ep_test"
    assert data["schema_version"] == "1.0"
    assert "payload" in data
    assert "timestamp" in data


def test_task_contract_message_from_json_roundtrip():
    msg = TaskContractMessage(
        task_id="T002",
        episode_id="ep_abc",
        payload={"task_id": "T002", "type": "qa"},
    )
    raw = msg.to_json()
    restored = TaskContractMessage.from_json(raw)
    assert restored.task_id == "T002"
    assert restored.episode_id == "ep_abc"
    assert restored.payload["type"] == "qa"
    assert restored.schema_version == "1.0"


def test_task_contract_message_from_dict():
    data = {
        "task_id": "T003",
        "episode_id": "ep_xyz",
        "payload": {"task_id": "T003", "type": "feature"},
        "schema_version": "2.0",
        "timestamp": "2026-03-24T00:00:00+00:00",
    }
    msg = TaskContractMessage.from_dict(data)
    assert msg.schema_version == "2.0"
    assert msg.timestamp == "2026-03-24T00:00:00+00:00"
