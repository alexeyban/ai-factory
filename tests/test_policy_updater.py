"""Tests for memory/policy_updater.py."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from memory.policy_updater import PolicyUpdater


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tmp_path() -> Path:
    return Path(tempfile.mktemp(suffix=".json"))


def _updater(*, db=None, policy_path: Path | None = None, examples_path: Path | None = None):
    p = policy_path or _tmp_path()
    e = examples_path or _tmp_path()
    return PolicyUpdater(
        replay_buffer=None,
        db=db,
        policy_state_path=p,
        examples_path=e,
    ), p, e


# ---------------------------------------------------------------------------
# _update_skill_weights
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_skill_weight_increases_on_success():
    db = MagicMock()
    db.execute = AsyncMock()
    updater, _, _ = _updater(db=db)
    solution = {"skills_used": ["skill-1"], "reward": 0.9}
    await updater._update_skill_weights(solution)
    db.execute.assert_called_once()
    call_args = db.execute.call_args[0]
    # Second positional arg to execute is skill_id, third is reward
    assert call_args[1] == "skill-1"
    assert call_args[2] == 0.9


@pytest.mark.asyncio
async def test_skill_weight_decreases_on_failure():
    db = MagicMock()
    db.execute = AsyncMock()
    updater, _, _ = _updater(db=db)
    solution = {"skills_used": ["skill-2"], "reward": 0.1}
    await updater._update_skill_weights(solution)
    db.execute.assert_called_once()
    call_args = db.execute.call_args[0]
    assert call_args[2] == 0.1


@pytest.mark.asyncio
async def test_skill_weight_no_db_does_not_raise():
    updater, _, _ = _updater(db=None)
    # Should not raise
    await updater._update_skill_weights({"skills_used": ["s1"], "reward": 0.8})


@pytest.mark.asyncio
async def test_skill_weight_db_error_does_not_raise():
    db = MagicMock()
    db.execute = AsyncMock(side_effect=RuntimeError("DB down"))
    updater, _, _ = _updater(db=db)
    # Must not propagate
    await updater._update_skill_weights({"skills_used": ["s1"], "reward": 0.9})


@pytest.mark.asyncio
async def test_skill_weight_skips_empty_skills_list():
    db = MagicMock()
    db.execute = AsyncMock()
    updater, _, _ = _updater(db=db)
    await updater._update_skill_weights({"skills_used": [], "reward": 0.9})
    db.execute.assert_not_called()


# ---------------------------------------------------------------------------
# _update_exploration_rate
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_exploration_rate_no_decay_below_skill_threshold():
    updater, p, _ = _updater()
    # skills_count=5 < 20 — no decay even with high reward
    p.write_text(json.dumps({"exploration_rate": 0.3, "skills_count": 5, "avg_reward": 0.0}))
    await updater._update_exploration_rate(best_reward=0.9)
    state = json.loads(p.read_text())
    assert abs(state["exploration_rate"] - 0.3) < 1e-9


@pytest.mark.asyncio
async def test_exploration_rate_decreases():
    updater, p, _ = _updater()
    p.write_text(json.dumps({
        "exploration_rate": 0.3,
        "skills_count": 25,
        "avg_reward": 0.8,
        "reward_samples": 10,
    }))
    await updater._update_exploration_rate(best_reward=0.9)
    state = json.loads(p.read_text())
    assert state["exploration_rate"] < 0.3


@pytest.mark.asyncio
async def test_exploration_rate_minimum_floor():
    updater, p, _ = _updater()
    # Start very low — must not go below 0.1
    p.write_text(json.dumps({
        "exploration_rate": 0.101,
        "skills_count": 100,
        "avg_reward": 0.95,
        "reward_samples": 100,
    }))
    for _ in range(50):
        await updater._update_exploration_rate(best_reward=1.0)
    state = json.loads(p.read_text())
    assert state["exploration_rate"] >= 0.1


@pytest.mark.asyncio
async def test_exploration_rate_persists_avg_reward():
    updater, p, _ = _updater()
    await updater._update_exploration_rate(best_reward=0.8)
    state = json.loads(p.read_text())
    assert "avg_reward" in state
    assert state["reward_samples"] == 1


# ---------------------------------------------------------------------------
# _update_prompt_examples
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_prompt_examples_saved_on_high_reward():
    updater, _, e = _updater()
    solution = {"code": "def foo(): return 42", "description": "task desc"}
    await updater._update_prompt_examples(solution, reward=0.9)
    assert e.exists()
    examples = json.loads(e.read_text())
    assert len(examples) == 1
    assert "foo" in examples[0]["code_snippet"]


@pytest.mark.asyncio
async def test_prompt_examples_not_saved_below_threshold():
    updater, _, e = _updater()
    await updater._update_prompt_examples({"code": "x=1"}, reward=0.5)
    assert not e.exists()


@pytest.mark.asyncio
async def test_prompt_examples_no_duplicates():
    updater, _, e = _updater()
    solution = {"code": "def foo(): pass", "description": ""}
    await updater._update_prompt_examples(solution, reward=0.9)
    await updater._update_prompt_examples(solution, reward=0.95)
    examples = json.loads(e.read_text())
    assert len(examples) == 1  # duplicate snippet → skipped


@pytest.mark.asyncio
async def test_prompt_examples_capped_at_max():
    updater, _, e = _updater()
    for i in range(10):
        sol = {"code": f"def f{i}(): return {i}" + " " * i, "description": ""}
        await updater._update_prompt_examples(sol, reward=0.85 + i * 0.001)
    examples = json.loads(e.read_text())
    assert len(examples) <= 3


# ---------------------------------------------------------------------------
# update — full pipeline
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_update_does_not_raise_without_db():
    updater, _, _ = _updater()
    await updater.update(
        episode_id="ep_001",
        best_solution={"code": "x=1", "skills_used": [], "reward": 0.9, "artifact": ""},
        best_reward=0.9,
    )


@pytest.mark.asyncio
async def test_update_none_solution_does_not_raise():
    updater, _, _ = _updater()
    await updater.update(episode_id="ep_001", best_solution=None, best_reward=0.0)
