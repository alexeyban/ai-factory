"""Tests for memory/skill_executor.py"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from memory.skill import Skill
from memory.skill_executor import SkillExecutor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_skill(code: str, tmp_dir: Path) -> Skill:
    skill_id = "test-skill-001"
    code_path = tmp_dir / f"{skill_id}.py"
    code_path.write_text(code, encoding="utf-8")
    return Skill(
        id=skill_id,
        name="test_skill",
        code_path=str(code_path),
    )


# ---------------------------------------------------------------------------
# Basic execution
# ---------------------------------------------------------------------------

def test_execute_simple_skill():
    executor = SkillExecutor(timeout_sec=5)
    with tempfile.TemporaryDirectory() as tmp:
        skill = _make_skill('def skill_main(**kw): return "hello"', Path(tmp))
        result = executor.execute(skill, {})
    assert result.success is True
    assert result.error is None
    assert result.execution_time_ms >= 0


def test_sandbox_captures_output():
    executor = SkillExecutor(timeout_sec=5)
    code = "print('{}')\n".format(json.dumps({"success": True, "output": "42"}))
    with tempfile.TemporaryDirectory() as tmp:
        skill = _make_skill(code, Path(tmp))
        result = executor.execute(skill, {})
    assert result.success is True


def test_execute_skill_with_inputs():
    executor = SkillExecutor(timeout_sec=5)
    code = "def skill_main(x, **kw): return x * 2"
    with tempfile.TemporaryDirectory() as tmp:
        skill = _make_skill(code, Path(tmp))
        result = executor.execute(skill, {"x": 21})
    assert result.success is True
    assert result.output == "42"


def test_execute_no_skill_main_still_succeeds():
    """Skill without skill_main should not crash the harness."""
    executor = SkillExecutor(timeout_sec=5)
    code = "x = 1 + 1\n"
    with tempfile.TemporaryDirectory() as tmp:
        skill = _make_skill(code, Path(tmp))
        result = executor.execute(skill, {})
    assert result.success is True


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

def test_execute_python_error_returns_failure():
    executor = SkillExecutor(timeout_sec=5)
    code = "raise ValueError('intentional error')\n"
    with tempfile.TemporaryDirectory() as tmp:
        skill = _make_skill(code, Path(tmp))
        result = executor.execute(skill, {})
    assert result.success is False
    assert result.error is not None


def test_timeout_handling():
    executor = SkillExecutor(timeout_sec=1)
    code = "import time\ntime.sleep(999)\n"
    with tempfile.TemporaryDirectory() as tmp:
        skill = _make_skill(code, Path(tmp))
        result = executor.execute(skill, {})
    assert result.success is False
    assert "timed out" in (result.error or "").lower()


def test_missing_skill_file_returns_failure():
    executor = SkillExecutor(timeout_sec=5)
    skill = Skill(id="ghost", name="ghost", code_path="/nonexistent/ghost.py")
    result = executor.execute(skill, {})
    assert result.success is False
    assert "not found" in (result.error or "").lower()
