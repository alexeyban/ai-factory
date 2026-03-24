"""
Tests for memory/episodic.py

All DB calls are mocked — no real PostgreSQL required.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from memory.episodic import (
    EpisodeRecord,
    EpisodicMemory,
    RewardRecord,
    SolutionRecord,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db(fetch=None, fetchrow=None, fetchval=None, execute=None):
    """Return a mock MemoryDB with configurable return values."""
    db = MagicMock()
    db.execute = AsyncMock(return_value=execute)
    db.fetch = AsyncMock(return_value=fetch or [])
    db.fetchrow = AsyncMock(return_value=fetchrow)
    db.fetchval = AsyncMock(return_value=fetchval)

    # transaction() context manager — yields a mock conn
    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock()
    mock_conn.fetchval = AsyncMock(return_value=1)   # solution id
    mock_conn.fetchrow = AsyncMock(return_value=None)

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    db.transaction = MagicMock(return_value=ctx)

    return db, mock_conn


def _episode(status="running") -> EpisodeRecord:
    return EpisodeRecord(
        id="ep_20260324_000000_aabbccdd",
        workflow_run_id="wf-001",
        started_at=datetime.now(timezone.utc),
        status=status,
        task_count=3,
    )


def _solution(reward=0.9) -> SolutionRecord:
    return SolutionRecord(
        episode_id="ep_20260324_000000_aabbccdd",
        task_id="T001",
        iteration=0,
        code_hash="abc123",
        code_path="/workspace/projects/foo/main.py",
        reward=reward,
    )


# ---------------------------------------------------------------------------
# store_episode
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_store_episode_inserts_row():
    db, _ = _make_db()
    mem = EpisodicMemory(db)
    await mem.store_episode(_episode())
    db.execute.assert_awaited_once()
    call_args = db.execute.call_args[0]
    assert "INSERT INTO episodes" in call_args[0]


@pytest.mark.asyncio
async def test_store_episode_publishes_kafka():
    db, _ = _make_db()
    producer = MagicMock()
    mem = EpisodicMemory(db, kafka_producer=producer)
    await mem.store_episode(_episode())
    producer.send.assert_called_once()
    topic, payload = producer.send.call_args[0]
    assert topic == "memory.events"
    assert payload["event_type"] == "episode_stored"


@pytest.mark.asyncio
async def test_store_episode_kafka_failure_does_not_raise():
    db, _ = _make_db()
    producer = MagicMock()
    producer.send.side_effect = RuntimeError("Kafka down")
    mem = EpisodicMemory(db, kafka_producer=producer)
    # Must not raise
    await mem.store_episode(_episode())


# ---------------------------------------------------------------------------
# store_solution / get_best_solution
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_store_solution_returns_id():
    db, conn = _make_db()
    conn.fetchval.return_value = 42
    mem = EpisodicMemory(db)
    solution_id = await mem.store_solution(_solution())
    assert solution_id == 42


@pytest.mark.asyncio
async def test_store_solution_with_reward():
    db, conn = _make_db()
    conn.fetchval.return_value = 7
    conn.fetchrow.return_value = None  # no existing best
    mem = EpisodicMemory(db)
    reward = RewardRecord(
        solution_id=0,
        correctness=1.0,
        performance=0.8,
        complexity_penalty=0.1,
        total=0.9,
        tests_passed=5,
        tests_total=5,
        execution_time_ms=200.0,
    )
    solution_id = await mem.store_solution(_solution(), reward=reward)
    assert solution_id == 7
    # reward INSERT should be called
    assert conn.execute.await_count >= 1


@pytest.mark.asyncio
async def test_get_best_solution_found():
    row = {
        "id": 1,
        "episode_id": "ep_20260324_000000_aabbccdd",
        "task_id": "T001",
        "iteration": 0,
        "code_hash": "abc123",
        "code_path": "/workspace/projects/foo/main.py",
        "reward": 0.95,
    }
    db, _ = _make_db(fetchrow=row)
    mem = EpisodicMemory(db)
    result = await mem.get_best_solution("T001")
    assert result is not None
    assert result.reward == 0.95
    assert result.task_id == "T001"


@pytest.mark.asyncio
async def test_get_best_solution_not_found():
    db, _ = _make_db(fetchrow=None)
    mem = EpisodicMemory(db)
    result = await mem.get_best_solution("T999")
    assert result is None


# ---------------------------------------------------------------------------
# Fingerprinting
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_solution_fingerprint_detected():
    db, _ = _make_db(fetchval=1)
    mem = EpisodicMemory(db)
    assert await mem.check_solution_fingerprint("abc123", "T001") is True


@pytest.mark.asyncio
async def test_solution_fingerprint_not_detected():
    db, _ = _make_db(fetchval=0)
    mem = EpisodicMemory(db)
    assert await mem.check_solution_fingerprint("newHash", "T001") is False


# ---------------------------------------------------------------------------
# Similar tasks
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_similar_tasks_no_vector_memory():
    db, _ = _make_db()
    mem = EpisodicMemory(db, vector_memory=None)
    result = await mem.get_similar_tasks([0.1] * 1536)
    assert result == []


@pytest.mark.asyncio
async def test_get_similar_tasks_with_vector_memory():
    db, _ = _make_db(fetchrow={
        "id": 1,
        "episode_id": "ep_20260324_000000_aabbccdd",
        "task_id": "T001",
        "iteration": 0,
        "code_hash": "abc",
        "code_path": "/workspace/projects/foo/main.py",
        "reward": 0.8,
    })
    vector = AsyncMock()
    vector.search_similar_episodes = AsyncMock(
        return_value=[{"task_id": "T001", "score": 0.92}]
    )
    mem = EpisodicMemory(db, vector_memory=vector)
    results = await mem.get_similar_tasks([0.0] * 1536, top_k=1)
    assert len(results) == 1
    assert results[0].task_id == "T001"
    assert results[0].similarity == pytest.approx(0.92)
