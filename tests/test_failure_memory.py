"""
Tests for memory/failures.py

All DB calls are mocked — no real PostgreSQL required.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from memory.failures import FailureMemory, FailurePattern


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db(fetch=None, fetchrow=None, fetchval=None, execute=None):
    db = MagicMock()
    db.execute = AsyncMock(return_value=execute)
    db.fetch = AsyncMock(return_value=fetch or [])
    db.fetchrow = AsyncMock(return_value=fetchrow)
    db.fetchval = AsyncMock(return_value=fetchval)
    return db


# ---------------------------------------------------------------------------
# record_failure
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_record_failure_inserts_row():
    db = _make_db()
    mem = FailureMemory(db)
    await mem.record_failure(
        episode_id="ep_001",
        task_id="T001",
        failure_type="failed_tests",
        error_message="AssertionError: expected 42",
        context={"task_type": "feature"},
    )
    db.execute.assert_awaited_once()
    sql = db.execute.call_args[0][0]
    assert "INSERT INTO failures" in sql


@pytest.mark.asyncio
async def test_record_failure_unknown_type_coerced():
    db = _make_db()
    mem = FailureMemory(db)
    await mem.record_failure(
        episode_id="ep_001",
        task_id="T001",
        failure_type="totally_new_error_type",
        error_message="something",
    )
    # Should store as 'unknown' — check the bound parameter
    args = db.execute.call_args[0]
    assert "unknown" in args  # failure_type arg


@pytest.mark.asyncio
async def test_record_failure_publishes_kafka():
    db = _make_db()
    producer = MagicMock()
    mem = FailureMemory(db, kafka_producer=producer)
    await mem.record_failure("ep_001", "T001", "timeout", "timed out", {})
    producer.send.assert_called_once()
    topic, payload = producer.send.call_args[0]
    assert topic == "memory.events"
    assert payload["event_type"] == "failure_recorded"


@pytest.mark.asyncio
async def test_record_failure_kafka_down_does_not_raise():
    db = _make_db()
    producer = MagicMock()
    producer.send.side_effect = ConnectionError("Kafka unavailable")
    mem = FailureMemory(db, kafka_producer=producer)
    await mem.record_failure("ep_001", "T001", "unknown", "err", {})
    # No exception propagated


# ---------------------------------------------------------------------------
# get_failure_patterns
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_failure_patterns_aggregation():
    rows = [
        {"failure_type": "failed_tests", "count": 5,
         "last_error": "assert fails", "common_context": {}},
        {"failure_type": "timeout", "count": 2,
         "last_error": "exceeded budget", "common_context": {}},
    ]
    db = _make_db(fetch=rows)
    mem = FailureMemory(db)
    patterns = await mem.get_failure_patterns("feature")
    assert len(patterns) == 2
    assert patterns[0].failure_type == "failed_tests"
    assert patterns[0].count == 5
    assert patterns[1].failure_type == "timeout"


@pytest.mark.asyncio
async def test_get_failure_patterns_empty():
    db = _make_db(fetch=[])
    mem = FailureMemory(db)
    patterns = await mem.get_failure_patterns("setup")
    assert patterns == []


# ---------------------------------------------------------------------------
# get_failure_summary
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_failure_summary_format():
    from datetime import datetime, timezone
    rows = [
        {
            "failure_type": "failed_tests",
            "error_message": "AssertionError in test_main",
            "created_at": datetime(2026, 3, 24, 10, 0, tzinfo=timezone.utc),
        }
    ]
    db = _make_db(fetch=rows)
    mem = FailureMemory(db)
    summary = await mem.get_failure_summary("T001")
    assert "T001" in summary
    assert "failed_tests" in summary
    assert "AssertionError" in summary


@pytest.mark.asyncio
async def test_failure_summary_empty_when_no_failures():
    db = _make_db(fetch=[])
    mem = FailureMemory(db)
    summary = await mem.get_failure_summary("T999")
    assert summary == ""
