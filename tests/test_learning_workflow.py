"""Integration tests for LearningWorkflow using Temporal's test environment.

Activities are replaced by lightweight mocks so no real LLM/DB/QA is involved.
Each mock uses @activity.defn with the matching name so Temporal's worker
can dispatch them correctly.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from orchestrator.workflows import (
    LearningWorkflow,
    LearningWorkflowInput,
    LearningWorkflowResult,
)


# ---------------------------------------------------------------------------
# Shared test task
# ---------------------------------------------------------------------------

_TASK = {
    "task_id": "T001",
    "description": "Implement a function",
    "project_repo_path": "/tmp/proj",
}

_DEV_RESULT = {
    "artifact": "/tmp/sol.py",
    "code": "x = 1",
    "candidates": [],
    "skills_used": [],
}


def _qa(status: str = "success", reward: float = 0.8) -> dict:
    return {"status": status, "reward": reward, "qa_metrics": {}, "is_regression": False}


# ---------------------------------------------------------------------------
# test_stops_on_stagnation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stops_on_stagnation():
    _call = {"n": 0}

    @activity.defn(name="dev_activity")
    async def _dev(task): return _DEV_RESULT

    @activity.defn(name="qa_activity")
    async def _qa_act(task):
        _call["n"] += 1
        return _qa(reward=0.5 if _call["n"] == 1 else 0.4)

    @activity.defn(name="extract_skill_activity")
    async def _extract(inp): return {"extracted": False}

    @activity.defn(name="policy_update_activity")
    async def _policy(inp): return {"ok": True}

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client, task_queue="test-q",
            workflows=[LearningWorkflow],
            activities=[_dev, _qa_act, _extract, _policy],
        ):
            result: LearningWorkflowResult = await env.client.execute_workflow(
                LearningWorkflow.run,
                LearningWorkflowInput(
                    task=_TASK, max_iterations=10,
                    stagnation_threshold=3, episode_id="ep_test",
                ),
                id="wf-stagnation", task_queue="test-q",
                execution_timeout=timedelta(minutes=1),
            )

    assert result.stopped_reason == "stagnation"
    assert result.total_iterations == 4  # 1 improve + 3 stagnant


# ---------------------------------------------------------------------------
# test_stops_on_perfect_score
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stops_on_perfect_score():
    @activity.defn(name="dev_activity")
    async def _dev(task): return _DEV_RESULT

    @activity.defn(name="qa_activity")
    async def _qa_act(task): return _qa(reward=1.0)

    @activity.defn(name="extract_skill_activity")
    async def _extract(inp): return {"extracted": True, "skill_id": "sk-1"}

    @activity.defn(name="policy_update_activity")
    async def _policy(inp): return {"ok": True}

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client, task_queue="test-q",
            workflows=[LearningWorkflow],
            activities=[_dev, _qa_act, _extract, _policy],
        ):
            result: LearningWorkflowResult = await env.client.execute_workflow(
                LearningWorkflow.run,
                LearningWorkflowInput(
                    task=_TASK, max_iterations=10,
                    stagnation_threshold=3, episode_id="ep_test",
                ),
                id="wf-perfect", task_queue="test-q",
                execution_timeout=timedelta(minutes=1),
            )

    assert result.stopped_reason == "perfect_score"
    assert result.total_iterations == 1
    assert result.best_reward == 1.0


# ---------------------------------------------------------------------------
# test_stops_at_max_iterations
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stops_at_max_iterations():
    _c = {"n": 0}

    @activity.defn(name="dev_activity")
    async def _dev(task): return _DEV_RESULT

    @activity.defn(name="qa_activity")
    async def _qa_act(task):
        _c["n"] += 1
        return _qa(reward=0.1 * _c["n"])  # always improving

    @activity.defn(name="extract_skill_activity")
    async def _extract(inp): return {"extracted": False}

    @activity.defn(name="policy_update_activity")
    async def _policy(inp): return {"ok": True}

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client, task_queue="test-q",
            workflows=[LearningWorkflow],
            activities=[_dev, _qa_act, _extract, _policy],
        ):
            result: LearningWorkflowResult = await env.client.execute_workflow(
                LearningWorkflow.run,
                LearningWorkflowInput(
                    task=_TASK, max_iterations=3,
                    stagnation_threshold=10, episode_id="ep_test",
                ),
                id="wf-max-iter", task_queue="test-q",
                execution_timeout=timedelta(minutes=1),
            )

    assert result.stopped_reason == "max_iterations"
    assert result.total_iterations == 3


# ---------------------------------------------------------------------------
# test_returns_best_solution
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_returns_best_solution():
    _rewards = [0.3, 0.9, 0.5]
    _c = {"n": 0}

    @activity.defn(name="dev_activity")
    async def _dev(task): return _DEV_RESULT

    @activity.defn(name="qa_activity")
    async def _qa_act(task):
        r = _rewards[_c["n"] % len(_rewards)]
        _c["n"] += 1
        return _qa(reward=r)

    @activity.defn(name="extract_skill_activity")
    async def _extract(inp): return {"extracted": False}

    @activity.defn(name="policy_update_activity")
    async def _policy(inp): return {"ok": True}

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client, task_queue="test-q",
            workflows=[LearningWorkflow],
            activities=[_dev, _qa_act, _extract, _policy],
        ):
            result: LearningWorkflowResult = await env.client.execute_workflow(
                LearningWorkflow.run,
                LearningWorkflowInput(
                    task=_TASK, max_iterations=3,
                    stagnation_threshold=10, episode_id="ep_test",
                ),
                id="wf-best-sol", task_queue="test-q",
                execution_timeout=timedelta(minutes=1),
            )

    assert abs(result.best_reward - 0.9) < 1e-9


# ---------------------------------------------------------------------------
# test_extracts_skill_on_improvement
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_extracts_skill_on_improvement():
    _extract_calls = []

    @activity.defn(name="dev_activity")
    async def _dev(task): return _DEV_RESULT

    @activity.defn(name="qa_activity")
    async def _qa_act(task): return _qa(status="success", reward=0.9)

    @activity.defn(name="extract_skill_activity")
    async def _extract(inp):
        _extract_calls.append(inp)
        return {"extracted": True, "skill_id": "sk-001"}

    @activity.defn(name="policy_update_activity")
    async def _policy(inp): return {"ok": True}

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client, task_queue="test-q",
            workflows=[LearningWorkflow],
            activities=[_dev, _qa_act, _extract, _policy],
        ):
            result: LearningWorkflowResult = await env.client.execute_workflow(
                LearningWorkflow.run,
                LearningWorkflowInput(
                    task=_TASK, max_iterations=1,
                    episode_id="ep_test",
                ),
                id="wf-extract", task_queue="test-q",
                execution_timeout=timedelta(minutes=1),
            )

    assert len(_extract_calls) == 1
    assert result.skills_extracted == 1


# ---------------------------------------------------------------------------
# test_no_skill_extraction_when_qa_fails
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_skill_extraction_when_qa_fails():
    _extract_calls = []

    @activity.defn(name="dev_activity")
    async def _dev(task): return _DEV_RESULT

    @activity.defn(name="qa_activity")
    async def _qa_act(task): return _qa(status="fail", reward=0.9)

    @activity.defn(name="extract_skill_activity")
    async def _extract(inp):
        _extract_calls.append(inp)
        return {"extracted": False}

    @activity.defn(name="policy_update_activity")
    async def _policy(inp): return {"ok": True}

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client, task_queue="test-q",
            workflows=[LearningWorkflow],
            activities=[_dev, _qa_act, _extract, _policy],
        ):
            await env.client.execute_workflow(
                LearningWorkflow.run,
                LearningWorkflowInput(
                    task=_TASK, max_iterations=1,
                    episode_id="ep_test",
                ),
                id="wf-no-extract", task_queue="test-q",
                execution_timeout=timedelta(minutes=1),
            )

    assert len(_extract_calls) == 0


# ---------------------------------------------------------------------------
# test_policy_update_called_once
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_policy_update_called_once():
    _policy_calls = []

    @activity.defn(name="dev_activity")
    async def _dev(task): return _DEV_RESULT

    @activity.defn(name="qa_activity")
    async def _qa_act(task): return _qa(reward=0.5)

    @activity.defn(name="extract_skill_activity")
    async def _extract(inp): return {"extracted": False}

    @activity.defn(name="policy_update_activity")
    async def _policy(inp):
        _policy_calls.append(inp)
        return {"ok": True}

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client, task_queue="test-q",
            workflows=[LearningWorkflow],
            activities=[_dev, _qa_act, _extract, _policy],
        ):
            await env.client.execute_workflow(
                LearningWorkflow.run,
                LearningWorkflowInput(
                    task=_TASK, max_iterations=2,
                    stagnation_threshold=5, episode_id="ep_test",
                ),
                id="wf-policy-once", task_queue="test-q",
                execution_timeout=timedelta(minutes=1),
            )

    assert len(_policy_calls) == 1
    assert "episode_id" in _policy_calls[0]
    assert "best_reward" in _policy_calls[0]
