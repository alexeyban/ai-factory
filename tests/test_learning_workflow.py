"""Integration tests for LearningWorkflow using Temporal's test environment.

Activities are replaced by lightweight mocks so no real LLM/DB/QA is involved.
"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from orchestrator.workflows import (
    LearningWorkflow,
    LearningWorkflowInput,
    LearningWorkflowResult,
)
from orchestrator.activities import (
    dev_activity,
    qa_activity,
    extract_skill_activity,
    policy_update_activity,
)


# ---------------------------------------------------------------------------
# Mock activity factories
# ---------------------------------------------------------------------------

def _dev_result(artifact: str = "/tmp/sol.py", code: str = "x=1") -> dict:
    return {"artifact": artifact, "code": code, "candidates": [], "skills_used": []}


def _qa_result(status: str = "success", reward: float = 0.8) -> dict:
    return {
        "status": status,
        "reward": reward,
        "qa_metrics": {},
        "is_regression": False,
    }


_TASK = {
    "task_id": "T001",
    "description": "Implement a function",
    "project_repo_path": "/tmp/proj",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _run_workflow(
    mock_dev,
    mock_qa,
    mock_extract=None,
    mock_policy=None,
    max_iterations: int = 5,
    stagnation_threshold: int = 3,
):
    """Run LearningWorkflow in Temporal's time-skipping test environment."""
    if mock_extract is None:
        mock_extract = AsyncMock(return_value={"extracted": False, "skill_id": None})
    if mock_policy is None:
        mock_policy = AsyncMock(return_value={"ok": True})

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test-learning",
            workflows=[LearningWorkflow],
            activities=[mock_dev, mock_qa, mock_extract, mock_policy],
        ):
            inp = LearningWorkflowInput(
                task=_TASK,
                max_iterations=max_iterations,
                num_candidates=1,
                exploration_rate=0.3,
                stagnation_threshold=stagnation_threshold,
                episode_id="ep_test",
            )
            result = await env.client.execute_workflow(
                LearningWorkflow.run,
                inp,
                id="test-learning-wf",
                task_queue="test-learning",
                execution_timeout=timedelta(minutes=1),
            )
    return result


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stops_on_stagnation():
    """After stagnation_threshold non-improving iterations → stopped_reason='stagnation'."""
    call_count = 0

    async def mock_dev(task):
        return _dev_result()

    async def mock_qa(task):
        nonlocal call_count
        call_count += 1
        # First iteration improves; subsequent ones don't
        reward = 0.5 if call_count == 1 else 0.4
        return _qa_result(reward=reward)

    result: LearningWorkflowResult = await _run_workflow(
        mock_dev, mock_qa,
        max_iterations=10,
        stagnation_threshold=3,
    )

    assert result.stopped_reason == "stagnation"
    # Stopped after 1 improvement + 3 non-improving = 4 iterations
    assert result.total_iterations == 4


@pytest.mark.asyncio
async def test_stops_on_perfect_score():
    """reward >= 0.99 → stopped_reason='perfect_score'."""

    async def mock_dev(task):
        return _dev_result()

    async def mock_qa(task):
        return _qa_result(reward=1.0)

    result: LearningWorkflowResult = await _run_workflow(
        mock_dev, mock_qa,
        max_iterations=10,
    )

    assert result.stopped_reason == "perfect_score"
    assert result.total_iterations == 1
    assert result.best_reward == 1.0


@pytest.mark.asyncio
async def test_stops_at_max_iterations():
    """Constant mild improvement → runs all iterations."""

    iter_count = 0

    async def mock_dev(task):
        return _dev_result()

    async def mock_qa(task):
        nonlocal iter_count
        iter_count += 1
        return _qa_result(reward=0.1 * iter_count)  # always improving

    result: LearningWorkflowResult = await _run_workflow(
        mock_dev, mock_qa,
        max_iterations=3,
        stagnation_threshold=5,
    )

    assert result.stopped_reason == "max_iterations"
    assert result.total_iterations == 3


@pytest.mark.asyncio
async def test_returns_best_solution():
    """Returns the solution with the highest reward across iterations."""
    rewards = [0.3, 0.9, 0.5]
    idx = 0

    async def mock_dev(task):
        return _dev_result(code=f"code_{idx}")

    async def mock_qa(task):
        nonlocal idx
        r = rewards[idx % len(rewards)]
        idx += 1
        return _qa_result(reward=r)

    result: LearningWorkflowResult = await _run_workflow(
        mock_dev, mock_qa,
        max_iterations=3,
        stagnation_threshold=5,
    )

    assert abs(result.best_reward - 0.9) < 1e-9


@pytest.mark.asyncio
async def test_extracts_skill_on_improvement():
    """extract_skill_activity is called when QA passes with improved reward."""
    extract_calls = []

    async def mock_dev(task):
        return _dev_result()

    async def mock_qa(task):
        return _qa_result(status="success", reward=0.9)

    async def mock_extract(inp):
        extract_calls.append(inp)
        return {"extracted": True, "skill_id": "sk-001"}

    async def mock_policy(inp):
        return {"ok": True}

    result: LearningWorkflowResult = await _run_workflow(
        mock_dev, mock_qa,
        mock_extract=mock_extract,
        mock_policy=mock_policy,
        max_iterations=1,
    )

    assert len(extract_calls) >= 1
    assert result.skills_extracted >= 1


@pytest.mark.asyncio
async def test_no_skill_extraction_when_qa_fails():
    """extract_skill_activity must NOT be called when QA status != 'success'."""
    extract_calls = []

    async def mock_dev(task):
        return _dev_result()

    async def mock_qa(task):
        return _qa_result(status="fail", reward=0.9)  # reward high but status fail

    async def mock_extract(inp):
        extract_calls.append(inp)
        return {"extracted": False}

    result: LearningWorkflowResult = await _run_workflow(
        mock_dev, mock_qa,
        mock_extract=mock_extract,
        max_iterations=1,
    )

    assert len(extract_calls) == 0


@pytest.mark.asyncio
async def test_policy_update_called_once():
    """policy_update_activity is called exactly once at the end."""
    policy_calls = []

    async def mock_dev(task):
        return _dev_result()

    async def mock_qa(task):
        return _qa_result(reward=0.5)

    async def mock_policy(inp):
        policy_calls.append(inp)
        return {"ok": True}

    await _run_workflow(
        mock_dev, mock_qa,
        mock_policy=mock_policy,
        max_iterations=2,
        stagnation_threshold=5,
    )

    assert len(policy_calls) == 1
    assert "episode_id" in policy_calls[0]
    assert "best_reward" in policy_calls[0]
