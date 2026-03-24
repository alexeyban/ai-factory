"""Tests for memory/replay_buffer.py."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from memory.replay_buffer import BufferedSolution, ReplayBuffer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sol(task_id: str = "T001", reward: float = 0.8, iteration: int = 0) -> BufferedSolution:
    return BufferedSolution(
        task_id=task_id,
        episode_id="ep_001",
        iteration=iteration,
        reward=reward,
        artifact="",
        code="def f(): pass",
        skills_used=[],
    )


def _buf(**kwargs) -> ReplayBuffer:
    return ReplayBuffer(random_seed=42, **kwargs)


# ---------------------------------------------------------------------------
# add — routing by threshold
# ---------------------------------------------------------------------------

def test_add_good_solution():
    buf = _buf(good_threshold=0.7)
    buf.add(_sol(reward=0.9))
    assert buf.size()["good"] == 1
    assert buf.size()["bad"] == 0


def test_add_bad_solution():
    buf = _buf(good_threshold=0.7)
    buf.add(_sol(reward=0.5))
    assert buf.size()["good"] == 0
    assert buf.size()["bad"] == 1


def test_add_at_threshold_is_good():
    buf = _buf(good_threshold=0.7)
    buf.add(_sol(reward=0.7))
    assert buf.size()["good"] == 1


# ---------------------------------------------------------------------------
# maxlen eviction
# ---------------------------------------------------------------------------

def test_maxlen_eviction_good():
    buf = _buf(max_good=3, good_threshold=0.0)
    for i in range(5):
        buf.add(_sol(reward=0.9, iteration=i))
    # deque evicts oldest — only 3 remain
    assert buf.size()["good"] == 3


def test_maxlen_eviction_bad():
    buf = _buf(max_bad=2, good_threshold=1.0)  # threshold=1.0 → all go to bad
    for i in range(4):
        buf.add(_sol(reward=0.5, iteration=i))
    assert buf.size()["bad"] == 2


# ---------------------------------------------------------------------------
# sample_good / sample_bad
# ---------------------------------------------------------------------------

def test_sample_good_returns_k():
    buf = _buf(good_threshold=0.0)
    for i in range(5):
        buf.add(_sol(reward=0.9, iteration=i))
    samples = buf.sample_good(3)
    assert len(samples) == 3


def test_sample_good_capped_at_size():
    buf = _buf(good_threshold=0.0)
    buf.add(_sol(reward=0.9))
    samples = buf.sample_good(10)
    assert len(samples) == 1


def test_sample_good_empty():
    buf = _buf()
    assert buf.sample_good(5) == []


def test_sample_bad_returns_k():
    buf = _buf(good_threshold=1.0)  # all → bad
    for i in range(4):
        buf.add(_sol(reward=0.2, iteration=i))
    samples = buf.sample_bad(2)
    assert len(samples) == 2


# ---------------------------------------------------------------------------
# get_best
# ---------------------------------------------------------------------------

def test_get_best_for_task():
    buf = _buf(good_threshold=0.0)
    buf.add(_sol(task_id="T001", reward=0.6))
    buf.add(_sol(task_id="T001", reward=0.9))
    buf.add(_sol(task_id="T002", reward=0.95))
    best = buf.get_best("T001")
    assert best is not None
    assert abs(best.reward - 0.9) < 1e-9


def test_get_best_returns_none_when_no_match():
    buf = _buf(good_threshold=0.0)
    buf.add(_sol(task_id="T001", reward=0.8))
    assert buf.get_best("T999") is None


# ---------------------------------------------------------------------------
# to_json / from_json (persistence roundtrip)
# ---------------------------------------------------------------------------

def test_persistence_roundtrip():
    buf = _buf(max_good=50, max_bad=25, good_threshold=0.6)
    buf.add(_sol(task_id="T001", reward=0.9))
    buf.add(_sol(task_id="T002", reward=0.4))

    restored = ReplayBuffer.from_json(buf.to_json())

    assert restored.size()["good"] == 1
    assert restored.size()["bad"] == 1
    good = restored.sample_good(1)[0]
    assert good.task_id == "T001"
    bad = restored.sample_bad(1)[0]
    assert bad.task_id == "T002"


def test_roundtrip_preserves_config():
    buf = _buf(max_good=7, max_bad=3, good_threshold=0.5)
    raw = json.loads(buf.to_json())
    assert raw["max_good"] == 7
    assert raw["max_bad"] == 3
    assert raw["good_threshold"] == 0.5


# ---------------------------------------------------------------------------
# save / load (file persistence)
# ---------------------------------------------------------------------------

def test_save_and_load_file():
    buf = _buf(good_threshold=0.0)
    buf.add(_sol(task_id="T001", reward=0.85))

    tmp = Path(tempfile.mktemp(suffix=".json"))
    try:
        buf.save(tmp)
        loaded = ReplayBuffer.load(tmp)
        assert loaded.size()["good"] == 1
    finally:
        tmp.unlink(missing_ok=True)


def test_load_missing_file_returns_empty():
    buf = ReplayBuffer.load(Path("/nonexistent/replay.json"))
    assert buf.size() == {"good": 0, "bad": 0}


def test_load_corrupt_file_returns_empty():
    tmp = Path(tempfile.mktemp(suffix=".json"))
    tmp.write_text("not json", encoding="utf-8")
    try:
        buf = ReplayBuffer.load(tmp)
        assert buf.size() == {"good": 0, "bad": 0}
    finally:
        tmp.unlink(missing_ok=True)
