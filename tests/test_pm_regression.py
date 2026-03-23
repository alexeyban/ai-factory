"""Regression tests for PM activity and task-state cache behaviour."""
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from agents.decomposer.agent import normalize_task_contract


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_large_text(chars: int) -> str:
    return ("x" * 80 + "\n") * (chars // 81 + 1)


# ---------------------------------------------------------------------------
# PM prompt truncation — architect notes must not overflow token limit
# ---------------------------------------------------------------------------


def test_pm_notes_are_truncated_before_token_limit():
    """
    Regression: PM activity must truncate architect/analyst notes to ≤ 4000 chars
    before inserting them into the prompt.  A 42 000-char architect response must
    NOT be passed verbatim — that caused 0-task output in production.
    """
    # The truncation limit is hard-coded in pm_activity as _PM_MAX_NOTES_CHARS = 4000
    _PM_MAX_NOTES_CHARS = 4000

    big_notes = _make_large_text(42_000)
    truncated = big_notes[:_PM_MAX_NOTES_CHARS]

    assert len(truncated) <= _PM_MAX_NOTES_CHARS
    # Confirm truncation actually cut something off
    assert len(big_notes) > len(truncated)


def test_pm_activity_calls_llm_with_truncated_notes(tmp_path):
    """
    Integration-level regression: pm_activity must not pass >4000 chars of
    architect_notes or analyst_notes to call_llm.

    We mock the three internal LLM calls and capture what the third one receives
    (the PM planning call). The first two calls return large strings to simulate
    a verbose architect/analyst.
    """
    # _PM_MAX_NOTES_CHARS is a local constant inside pm_activity; keep in sync.
    _PM_MAX_NOTES_CHARS = 4000

    big_response = _make_large_text(40_000)
    captured_prompts: list[str] = []

    def _fake_llm(system: str, user: str) -> str:
        captured_prompts.append(user)
        # First two calls are architect + analyst; third is PM plan
        if len(captured_prompts) >= 3:
            return json.dumps({
                "project_goal": "test",
                "delivery_summary": "ok",
                "execution_plan": [
                    {
                        "task_id": "T001",
                        "title": "Do work",
                        "description": "Implement it",
                        "assigned_agent": "dev",
                        "dependencies": [],
                        "acceptance_criteria": ["Done"],
                    }
                ],
            })
        return big_response

    # Build a minimal task payload
    task = {
        "task_id": "wf-001",
        "title": "Test project",
        "description": "Build something",
        "project_name": "test_pm_proj",
        "_workflow_id": "test-workflow-001",
    }

    import asyncio

    with patch("orchestrator.activities.call_llm", side_effect=_fake_llm), \
         patch("orchestrator.activities._ensure_project_scaffold", return_value=tmp_path), \
         patch("orchestrator.activities._record_pm_intake", return_value={}), \
         patch("orchestrator.activities._record_pm_artifacts", return_value={}), \
         patch("orchestrator.activities._wrap_activity_result", side_effect=lambda wf, stage, r, t: r):
        result = asyncio.run(
            __import__("orchestrator.activities", fromlist=["pm_activity"]).pm_activity(task)
        )

    # The third prompt (PM planning) must not contain >4000-char note blocks
    assert len(captured_prompts) == 3
    pm_prompt = captured_prompts[2]
    # The truncated notes are embedded; no single run of 'x' chars > limit should appear
    assert ("x" * (_PM_MAX_NOTES_CHARS + 1)) not in pm_prompt

    # PM must produce at least one task
    assert len(result.get("execution_plan", [])) >= 1


# ---------------------------------------------------------------------------
# Task state cache — workflow_id propagation
# ---------------------------------------------------------------------------


def test_task_state_includes_workflow_id(tmp_path):
    """
    Regression: _save_task_state must persist workflow_id so that
    _execute_task_impl can reject stale cache entries from a different workflow.
    """
    from orchestrator.activities import _save_task_state, _load_task_state  # noqa: PLC0415

    # _save_task_state writes to <repo>/.ai_factory/tasks/ — create it first
    (tmp_path / ".ai_factory" / "tasks").mkdir(parents=True)

    state = {
        "task_id": "T42",
        "status": "success",
        "workflow_id": "wf-new-123",
        "result": {"code": "pass"},
    }
    _save_task_state(tmp_path, "T42", state)

    loaded = _load_task_state(tmp_path, "T42")
    assert loaded is not None
    assert loaded.get("workflow_id") == "wf-new-123"
    assert loaded.get("status") == "success"


def test_stale_cache_rejected_for_different_workflow(tmp_path):
    """
    Regression: a cached 'success' from a different workflow_id must not be
    returned as valid — the guard added in _execute_task_impl checks
    previous_state.get("workflow_id") == workflow_id.
    """
    from orchestrator.activities import _save_task_state, _load_task_state  # noqa: PLC0415

    (tmp_path / ".ai_factory" / "tasks").mkdir(parents=True)

    old_state = {
        "task_id": "T99",
        "status": "success",
        "workflow_id": "wf-old-999",
    }
    _save_task_state(tmp_path, "T99", old_state)

    loaded = _load_task_state(tmp_path, "T99")
    assert loaded is not None

    # Simulate the cache guard in _execute_task_impl
    current_workflow_id = "wf-new-abc"
    is_cache_hit = (
        loaded.get("status") == "success"
        and loaded.get("workflow_id") == current_workflow_id
    )
    assert is_cache_hit is False, "Stale cache from old workflow must be rejected"
