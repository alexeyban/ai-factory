"""
Tests for Phase 9 Step 3 — Hidden Test Cases.

Verifies:
- _run_hidden_tests returns correct score
- hidden_tests field absent → score=1.0, ran=False
- hidden_tests excluded from dev prompt
- reward adjusted when hidden tests ran
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from orchestrator.activities import _run_hidden_tests, _build_dev_prompt


# ---------------------------------------------------------------------------
# _run_hidden_tests
# ---------------------------------------------------------------------------

def _make_task(hidden_tests=None):
    t = {
        "task_id": "T001",
        "type": "feature",
        "title": "Test task",
        "description": "Add two numbers",
    }
    if hidden_tests is not None:
        t["hidden_tests"] = hidden_tests
    return t


def test_no_hidden_tests_returns_default():
    task = _make_task()
    result = _run_hidden_tests(task, Path("/tmp"), Path(sys.executable))
    assert result["ran"] is False
    assert result["score"] == 1.0
    assert result["total"] == 0


def test_passing_hidden_test():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        test_code = "def test_always_passes():\n    assert 1 + 1 == 2\n"
        task = _make_task(hidden_tests=[test_code])
        result = _run_hidden_tests(task, repo, Path(sys.executable))
    assert result["ran"] is True
    assert result["passed"] == 1
    assert result["total"] == 1
    assert result["score"] == pytest.approx(1.0)


def test_failing_hidden_test():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        test_code = "def test_always_fails():\n    assert 1 == 2\n"
        task = _make_task(hidden_tests=[test_code])
        result = _run_hidden_tests(task, repo, Path(sys.executable))
    assert result["ran"] is True
    assert result["passed"] == 0
    assert result["score"] == pytest.approx(0.0)


def test_mixed_hidden_tests():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        pass_test = "def test_pass():\n    assert True\n"
        fail_test = "def test_fail():\n    assert False\n"
        task = _make_task(hidden_tests=[pass_test, fail_test])
        result = _run_hidden_tests(task, repo, Path(sys.executable))
    assert result["ran"] is True
    assert result["passed"] == 1
    assert result["total"] == 2
    assert result["score"] == pytest.approx(0.5)


def test_hidden_tests_not_in_repo(tmp_path):
    """Temp files must NOT be written inside repo_path."""
    test_code = "def test_x():\n    assert 1\n"
    task = _make_task(hidden_tests=[test_code])
    _run_hidden_tests(task, tmp_path, Path(sys.executable))
    # No hidden test file should remain in the repo
    hidden_files = list(tmp_path.glob("hidden_test_*.py"))
    assert hidden_files == [], f"Hidden test files leaked into repo: {hidden_files}"


# ---------------------------------------------------------------------------
# Dev prompt excludes hidden_tests
# ---------------------------------------------------------------------------

def _minimal_task_with_hidden():
    return {
        "task_id": "T002",
        "type": "feature",
        "title": "Calc",
        "description": "Implement add()",
        "input": {"files": [], "context": ""},
        "output": {"files": ["calc.py"], "artifacts": [], "expected_result": ""},
        "hidden_tests": ["def test_secret():\n    assert add(1,2) == 3\n"],
        "project_name": "testproj",
        "project_repo_path": "/tmp/testproj",
    }


def test_hidden_tests_excluded_from_dev_prompt():
    task = _minimal_task_with_hidden()
    with (
        patch("orchestrator.activities.render_prompt") as mock_render,
        patch("orchestrator.activities.get_task_error_history"),
        patch("orchestrator.activities._build_existing_code_context", return_value=""),
    ):
        mock_render.return_value = "prompt"
        _build_dev_prompt(task, "description", attempt_number=1)

    # Inspect the task_context argument passed to render_prompt
    call_kwargs = mock_render.call_args[1]
    task_context_str = call_kwargs.get("task_context", "")
    task_context = json.loads(task_context_str)
    assert "hidden_tests" not in task_context, "hidden_tests must not appear in dev prompt"
