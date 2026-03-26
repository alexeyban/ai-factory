"""
Tests for Phase 9 — Loop Protection.

Validates LearningWorkflow halting conditions via Temporal test environment:
  - max_iterations cap
  - stagnation early stop
  - perfect-score early stop
  - stopped_reason field accuracy
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

_TASK = {
    "task_id": "T-lp",
    "description": "Loop protection test task",
    "project_repo_path": "/tmp/lp_proj",
}

_DEV_RESULT = {
    "artifact": "/tmp/lp.py",
    "code": "x = 1",
    "candidates": [],
    "skills_used": [],
}


def _qa(reward: float, status: str = "success") -> dict:
    return {"status": status, "reward": reward, "qa_metrics": {}, "is_regression": False}


async def _run_workflow(
    env,
    dev_fn,
    qa_fn,
    max_iterations: int = 5,
    stagnation_threshold: int = 3,
    wf_id: str = "wf-lp",
) -> LearningWorkflowResult:
    @activity.defn(name="extract_skill_activity")
    async def _extract(inp):
        return {"extracted": False}

    @activity.defn(name="policy_update_activity")
    async def _policy(inp):
        return {"ok": True}

    async with Worker(
        env.client,
        task_queue="lp-q",
        workflows=[LearningWorkflow],
        activities=[dev_fn, qa_fn, _extract, _policy],
    ):
        return await env.client.execute_workflow(
            LearningWorkflow.run,
            LearningWorkflowInput(
                task=_TASK,
                max_iterations=max_iterations,
                stagnation_threshold=stagnation_threshold,
                episode_id="ep_lp",
            ),
            id=wf_id,
            task_queue="lp-q",
            execution_timeout=timedelta(minutes=1),
        )


@pytest.mark.asyncio
async def test_stops_at_max_iterations():
    """Workflow must not execute more than max_iterations iterations."""
    counter = {"n": 0}

    @activity.defn(name="dev_activity")
    async def _dev(task):
        return _DEV_RESULT

    @activity.defn(name="qa_activity")
    async def _qa_act(task):
        counter["n"] += 1
        return _qa(reward=counter["n"] * 0.1)  # always improving → no stagnation

    async with await WorkflowEnvironment.start_time_skipping() as env:
        result = await _run_workflow(
            env, _dev, _qa_act,
            max_iterations=4, stagnation_threshold=10,
            wf_id="lp-max",
        )

    assert result.total_iterations == 4
    assert result.stopped_reason == "max_iterations"


@pytest.mark.asyncio
async def test_stagnation_stops_early():
    """Workflow stops before max_iterations when reward stops improving."""
    counter = {"n": 0}

    @activity.defn(name="dev_activity")
    async def _dev(task):
        return _DEV_RESULT

    @activity.defn(name="qa_activity")
    async def _qa_act(task):
        counter["n"] += 1
        return _qa(reward=0.8 if counter["n"] == 1 else 0.5)  # 1 improve, then flat

    async with await WorkflowEnvironment.start_time_skipping() as env:
        result = await _run_workflow(
            env, _dev, _qa_act,
            max_iterations=20, stagnation_threshold=2,
            wf_id="lp-stagnation",
        )

    assert result.stopped_reason == "stagnation"
    assert result.total_iterations < 20


@pytest.mark.asyncio
async def test_perfect_score_stops_early():
    """Workflow stops immediately when reward >= 0.99."""

    @activity.defn(name="dev_activity")
    async def _dev(task):
        return _DEV_RESULT

    @activity.defn(name="qa_activity")
    async def _qa_act(task):
        return _qa(reward=1.0)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        result = await _run_workflow(
            env, _dev, _qa_act,
            max_iterations=10, stagnation_threshold=5,
            wf_id="lp-perfect",
        )

    assert result.stopped_reason == "perfect_score"
    assert result.total_iterations == 1
    assert result.best_reward >= 0.99


@pytest.mark.asyncio
async def test_stopped_reason_correct():
    """stopped_reason must be one of the three documented values."""
    counter = {"n": 0}

    @activity.defn(name="dev_activity")
    async def _dev(task):
        return _DEV_RESULT

    @activity.defn(name="qa_activity")
    async def _qa_act(task):
        counter["n"] += 1
        return _qa(reward=0.3)  # constant reward → stagnation

    async with await WorkflowEnvironment.start_time_skipping() as env:
        result = await _run_workflow(
            env, _dev, _qa_act,
            max_iterations=10, stagnation_threshold=3,
            wf_id="lp-reason",
        )

    assert result.stopped_reason in {"max_iterations", "stagnation", "perfect_score"}
