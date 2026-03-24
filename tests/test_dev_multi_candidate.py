"""
Tests for Phase 3 multi-candidate dev generation.

We test the pure helper functions (strategy selection, skill context building)
without invoking the Temporal activity runtime.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from orchestrator.activities import (
    _get_strategies,
    _build_skill_context_for_candidate,
    _build_dev_prompt,
)


# ---------------------------------------------------------------------------
# _get_strategies
# ---------------------------------------------------------------------------

def test_generates_correct_num_candidates():
    strategies = _get_strategies(3, 0.3)
    assert len(strategies) == 3


def test_single_candidate_backward_compat():
    strategies = _get_strategies(1, 0.3)
    assert len(strategies) == 1


def test_exploration_distribution_rate_1():
    strategies = _get_strategies(5, 1.0)
    assert all(s == "explore" for s in strategies)


def test_exploration_distribution_rate_0():
    strategies = _get_strategies(4, 0.0)
    # At least one explore always
    assert strategies.count("explore") >= 1
    assert strategies.count("exploit") == 3


def test_strategies_only_valid_values():
    for rate in [0.0, 0.3, 0.5, 1.0]:
        for n in [1, 2, 3, 5]:
            strats = _get_strategies(n, rate)
            assert all(s in ("explore", "exploit") for s in strats)


# ---------------------------------------------------------------------------
# _build_skill_context_for_candidate
# ---------------------------------------------------------------------------

def test_explore_strategy_returns_empty_context():
    skills_ctx, failures_ctx = _build_skill_context_for_candidate({}, "explore")
    assert skills_ctx == ""
    assert failures_ctx == ""


def test_exploit_strategy_with_empty_registry():
    """No skills in registry → skills_context is empty (no crash)."""
    with patch("skills.SkillRegistry.list_active_skills", return_value=[]):
        skills_ctx, _ = _build_skill_context_for_candidate({}, "exploit")
    assert skills_ctx == ""


def test_exploit_strategy_with_skills_in_registry():
    fake_skills = [
        {
            "id": "s1", "name": "binary_search",
            "description": "Binary search helper",
            "tags": ["search", "algorithm"],
            "success_rate": 0.9,
            "code_path": "skills/s1.py",
            "is_active": True,
        }
    ]
    with patch("skills.SkillRegistry.list_active_skills", return_value=fake_skills):
        skills_ctx, _ = _build_skill_context_for_candidate({}, "exploit")
    assert "binary_search" in skills_ctx
    assert "## Available Skills" in skills_ctx


def test_exploit_strategy_registry_error_does_not_raise():
    with patch("skills.SkillRegistry.list_active_skills", side_effect=RuntimeError("oops")):
        # Must not raise
        skills_ctx, _ = _build_skill_context_for_candidate({}, "exploit")
    assert skills_ctx == ""


# ---------------------------------------------------------------------------
# _build_dev_prompt — skill context injection
# ---------------------------------------------------------------------------

def test_skill_context_in_exploit_prompt():
    task = {
        "task_id": "T001",
        "description": "implement foo",
        "output": {"files": ["foo.py"]},
        "project_name": "test",
        "project_repo_path": "/tmp/test",
    }
    prompt = _build_dev_prompt(
        task,
        description="implement foo",
        attempt_number=1,
        skills_context="## Available Skills\n1. **binary_search**",
        strategy="exploit",
    )
    assert "binary_search" in prompt
    assert "EXPLOIT" in prompt


def test_no_skill_context_in_explore_prompt():
    task = {
        "task_id": "T001",
        "description": "implement foo",
        "output": {"files": ["foo.py"]},
        "project_name": "test",
        "project_repo_path": "/tmp/test",
    }
    prompt = _build_dev_prompt(
        task,
        description="implement foo",
        attempt_number=1,
        skills_context="",
        strategy="explore",
    )
    assert "EXPLORE" in prompt


# ---------------------------------------------------------------------------
# Partial-failure handling
# ---------------------------------------------------------------------------

def test_handles_partial_failure():
    """_get_strategies still returns all strategies; caller filters failures."""
    strategies = _get_strategies(3, 0.3)
    # Simulate 1 of 3 candidates raising an exception
    results = []
    for i, s in enumerate(strategies):
        if i == 1:
            # This candidate "failed"
            continue
        results.append({"strategy": s, "artifact": f"/tmp/art_{i}.py"})
    # 2 out of 3 succeeded
    assert len(results) == 2
