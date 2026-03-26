"""
Tests for Phase 9 — Determinism via Random Seed.

Verifies set_global_seed reproducibility, replay buffer seeding,
and that EpisodeRecord stores the seed value.
"""
from __future__ import annotations

import random

import pytest

from shared.episode import set_global_seed
from memory.replay_buffer import ReplayBuffer, BufferedSolution
from memory.episodic import EpisodeRecord
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# set_global_seed
# ---------------------------------------------------------------------------

def test_set_global_seed_reproducible():
    """Two calls with the same seed produce the same sequence."""
    set_global_seed(42)
    seq_a = [random.random() for _ in range(10)]

    set_global_seed(42)
    seq_b = [random.random() for _ in range(10)]

    assert seq_a == seq_b


def test_different_seeds_different_sequences():
    set_global_seed(1)
    seq_a = [random.random() for _ in range(5)]

    set_global_seed(2)
    seq_b = [random.random() for _ in range(5)]

    assert seq_a != seq_b


# ---------------------------------------------------------------------------
# ReplayBuffer — seeded sampling
# ---------------------------------------------------------------------------

def test_same_seed_same_sample():
    """Seeded ReplayBuffer returns same samples on repeated calls."""
    buf_a = ReplayBuffer(max_good=100, random_seed=42)
    buf_b = ReplayBuffer(max_good=100, random_seed=42)

    def _sol(i: int) -> BufferedSolution:
        return BufferedSolution(task_id="T1", episode_id="ep1", iteration=i,
                                reward=0.9, code=f"def f{i}(): pass")

    for i in range(20):
        buf_a.add(_sol(i))
        buf_b.add(_sol(i))

    sample_a = buf_a.sample_good(5)
    sample_b = buf_b.sample_good(5)
    assert sample_a == sample_b


def test_different_seed_may_differ():
    """Different seeds produce different samples (probabilistically)."""
    buf_a = ReplayBuffer(max_good=100, random_seed=1)
    buf_b = ReplayBuffer(max_good=100, random_seed=99)

    def _sol(i: int) -> BufferedSolution:
        return BufferedSolution(task_id="T1", episode_id="ep1", iteration=i,
                                reward=0.9, code=f"def f{i}(): pass")

    for i in range(50):
        buf_a.add(_sol(i))
        buf_b.add(_sol(i))

    # With 50 items and k=10, probability of identical sample is astronomically low
    sample_a = buf_a.sample_good(10)
    sample_b = buf_b.sample_good(10)
    assert sample_a != sample_b


# ---------------------------------------------------------------------------
# EpisodeRecord — stores random_seed
# ---------------------------------------------------------------------------

def test_episode_stores_seed():
    ep = EpisodeRecord(
        id="ep_test",
        workflow_run_id="wf-001",
        started_at=datetime.now(timezone.utc),
        status="running",
        random_seed=42,
    )
    assert ep.random_seed == 42


def test_episode_default_seed():
    ep = EpisodeRecord(
        id="ep_test2",
        workflow_run_id="wf-002",
        started_at=datetime.now(timezone.utc),
        status="running",
    )
    assert isinstance(ep.random_seed, int)
