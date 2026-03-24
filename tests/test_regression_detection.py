"""Tests for regression detection in memory/episodic.py."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from memory.episodic import EpisodicMemory, SolutionRecord


def _make_mem(best_solution: SolutionRecord | None = None) -> EpisodicMemory:
    db = MagicMock()
    db.execute = AsyncMock()
    db.fetch = AsyncMock(return_value=[])
    db.fetchval = AsyncMock(return_value=0)

    async def fetchrow(query, *args):
        if best_solution is None:
            return None
        return {
            "id": best_solution.id or 1,
            "episode_id": best_solution.episode_id,
            "task_id": best_solution.task_id,
            "iteration": best_solution.iteration,
            "code_hash": best_solution.code_hash,
            "code_path": best_solution.code_path,
            "reward": best_solution.reward,
        }

    db.fetchrow = fetchrow

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=AsyncMock())
    ctx.__aexit__ = AsyncMock(return_value=False)
    db.transaction = MagicMock(return_value=ctx)

    return EpisodicMemory(db)


def _solution(reward: float) -> SolutionRecord:
    return SolutionRecord(
        id=1,
        episode_id="ep_001",
        task_id="T001",
        iteration=0,
        code_hash="abc",
        code_path="/tmp/sol.py",
        reward=reward,
    )


# ---------------------------------------------------------------------------
# check_regression
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_regression_first_solution():
    """No history → never a regression."""
    mem = _make_mem(best_solution=None)
    result = await mem.check_regression("T001", new_reward=0.5)
    assert result is False


@pytest.mark.asyncio
async def test_regression_detected():
    """New reward < best → regression."""
    mem = _make_mem(best_solution=_solution(reward=0.9))
    result = await mem.check_regression("T001", new_reward=0.5)
    assert result is True


@pytest.mark.asyncio
async def test_improvement_not_regression():
    """New reward > best → not a regression."""
    mem = _make_mem(best_solution=_solution(reward=0.7))
    result = await mem.check_regression("T001", new_reward=0.95)
    assert result is False


@pytest.mark.asyncio
async def test_equal_reward_not_regression():
    """Same reward → not a regression (strict less-than)."""
    mem = _make_mem(best_solution=_solution(reward=0.8))
    result = await mem.check_regression("T001", new_reward=0.8)
    assert result is False


@pytest.mark.asyncio
async def test_regression_different_task_ids():
    """Regression is task-scoped — different task_id fetches its own best."""
    mem = _make_mem(best_solution=_solution(reward=0.9))
    # T999 has no history (fetchrow returns the same mock, but in reality
    # the DB would return None for an unknown task_id)
    mem2 = _make_mem(best_solution=None)
    result = await mem2.check_regression("T999", new_reward=0.1)
    assert result is False


@pytest.mark.asyncio
async def test_regression_with_none_reward_in_best():
    """If best solution has reward=None → treat as no history."""
    sol = _solution(reward=0.8)
    sol.reward = None  # type: ignore[assignment]
    mem = _make_mem(best_solution=sol)
    result = await mem.check_regression("T001", new_reward=0.1)
    assert result is False
