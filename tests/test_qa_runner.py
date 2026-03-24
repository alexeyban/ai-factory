"""
Tests for Phase 4 QA runner additions:
  - _attach_reward wires RewardEngine into qa result dicts
  - _apply_reward_and_regression handles regression detection + Kafka
  - parse_junit_xml extracts pass/fail/total from junit XML
"""
from __future__ import annotations

import textwrap
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.tools import parse_junit_xml
from orchestrator.activities import _attach_reward, _apply_reward_and_regression


# ---------------------------------------------------------------------------
# parse_junit_xml
# ---------------------------------------------------------------------------

def _write_junit(content: str) -> Path:
    tmp = Path(tempfile.mktemp(suffix=".xml"))
    tmp.write_text(content, encoding="utf-8")
    return tmp


def test_parse_junit_xml_basic():
    xml = textwrap.dedent("""\
        <?xml version="1.0" encoding="utf-8"?>
        <testsuite name="pytest" tests="5" errors="0" failures="1" skipped="0">
          <testcase classname="test_foo" name="test_a" time="0.01"/>
          <testcase classname="test_foo" name="test_b" time="0.02"/>
          <testcase classname="test_foo" name="test_c" time="0.01"/>
          <testcase classname="test_foo" name="test_d" time="0.01"/>
          <testcase classname="test_foo" name="test_e" time="0.01">
            <failure message="assert 1 == 2"/>
          </testcase>
        </testsuite>
    """)
    p = _write_junit(xml)
    try:
        result = parse_junit_xml(p)
        assert result["tests_total"] == 5
        assert result["tests_failed"] == 1
        assert result["tests_passed"] == 4
    finally:
        p.unlink(missing_ok=True)


def test_parse_junit_xml_all_pass():
    xml = textwrap.dedent("""\
        <?xml version="1.0" encoding="utf-8"?>
        <testsuite tests="3" failures="0" errors="0" skipped="0">
          <testcase name="a"/><testcase name="b"/><testcase name="c"/>
        </testsuite>
    """)
    p = _write_junit(xml)
    try:
        result = parse_junit_xml(p)
        assert result["tests_passed"] == 3
        assert result["tests_failed"] == 0
    finally:
        p.unlink(missing_ok=True)


def test_parse_junit_xml_missing_file():
    result = parse_junit_xml(Path("/nonexistent/junit.xml"))
    assert result["tests_total"] == 0


def test_parse_junit_xml_invalid_xml():
    p = Path(tempfile.mktemp(suffix=".xml"))
    p.write_text("not xml at all", encoding="utf-8")
    try:
        result = parse_junit_xml(p)
        assert result["tests_total"] == 0
    finally:
        p.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# _attach_reward
# ---------------------------------------------------------------------------

def test_attach_reward_success_result():
    qa_result = {
        "status": "success",
        "pytest_data": {
            "returncode": 0,
            "junit": {"tests_passed": 5, "tests_failed": 0, "tests_total": 5},
            "coverage": {"percent": 80.0},
        },
        "execution_time_ms": 100.0,
    }
    code = "def f(): return 1"
    result = _attach_reward(qa_result, code)
    assert "reward" in result
    assert result["reward"] > 0.0
    assert "qa_metrics" in result
    assert result["qa_metrics"]["tests_passed"] == 5


def test_attach_reward_failure_result():
    qa_result = {"status": "fail"}
    result = _attach_reward(qa_result, "broken code")
    assert "reward" in result
    # Failed QA → correctness=0, some performance contribution, complexity penalty
    assert result["reward"] < 1.0


def test_attach_reward_no_pytest_data_infers_from_status():
    qa_result = {"status": "success"}
    result = _attach_reward(qa_result, "def ok(): pass")
    assert result["reward"] > 0.0


def test_attach_reward_does_not_raise_on_bad_input():
    # Should never raise even with unexpected input
    result = _attach_reward({}, None)
    assert "reward" in result


def test_metrics_time_measured():
    qa_result = {
        "status": "success",
        "execution_time_ms": 500.0,
        "pytest_data": {
            "returncode": 0,
            "junit": {"tests_passed": 1, "tests_total": 1, "tests_failed": 0},
        },
    }
    result = _attach_reward(qa_result, "x = 1")
    assert result["qa_metrics"]["execution_time_ms"] == 500.0


# ---------------------------------------------------------------------------
# _apply_reward_and_regression — Kafka publishing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_publishes_to_kafka_qa_results():
    producer = MagicMock()
    result = {"status": "success", "reward": 0.9, "qa_metrics": {}}
    with patch("memory.db.MemoryDB") as MockDB:
        mock_db_inst = AsyncMock()
        mock_db_inst.connect = AsyncMock()
        mock_db_inst.close = AsyncMock()
        MockDB.return_value = mock_db_inst
        with patch("memory.episodic.EpisodicMemory") as MockMem:
            mock_mem = AsyncMock()
            mock_mem.check_regression = AsyncMock(return_value=False)
            MockMem.return_value = mock_mem
            result = await _apply_reward_and_regression(
                result, "T001", "ep_001", 0,
                kafka_producer=producer,
            )
    # Both qa.results and reward.computed should be published
    assert producer.send.call_count == 2
    topics = [call[0][0] for call in producer.send.call_args_list]
    assert "qa.results" in topics
    assert "reward.computed" in topics


@pytest.mark.asyncio
async def test_kafka_failure_does_not_raise():
    producer = MagicMock()
    producer.send.side_effect = RuntimeError("Kafka down")
    result = {"status": "success", "reward": 0.8, "qa_metrics": {}}
    with patch("memory.db.MemoryDB") as MockDB:
        mock_db_inst = AsyncMock()
        MockDB.return_value = mock_db_inst
        with patch("memory.episodic.EpisodicMemory") as MockMem:
            mock_mem = AsyncMock()
            mock_mem.check_regression = AsyncMock(return_value=False)
            MockMem.return_value = mock_mem
            # Must not raise
            result = await _apply_reward_and_regression(
                result, "T001", "ep_001", 0,
                kafka_producer=producer,
            )
    assert result["is_regression"] is False


@pytest.mark.asyncio
async def test_memory_db_failure_does_not_raise():
    """If DB is unreachable, regression check is skipped silently."""
    result = {"status": "success", "reward": 0.7, "qa_metrics": {}}
    with patch("memory.db.MemoryDB", side_effect=Exception("DB down")):
        result = await _apply_reward_and_regression(
            result, "T001", "ep_001", 0,
            kafka_producer=None,
        )
    # is_regression defaults to False when DB unavailable
    assert result.get("is_regression") is False
