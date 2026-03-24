"""Tests for shared/episode.py — episode ID generation and event logging."""
import json
import re
from unittest.mock import MagicMock, call

import pytest

from shared.episode import (
    new_episode_id,
    log_episode_event,
    episode_event_to_json,
)


# ---------------------------------------------------------------------------
# new_episode_id
# ---------------------------------------------------------------------------

def test_new_episode_id_format():
    ep_id = new_episode_id()
    # Must match: ep_YYYYMMDD_HHMMSS_<8 hex chars>
    pattern = r"^ep_\d{8}_\d{6}_[0-9a-f]{8}$"
    assert re.match(pattern, ep_id), f"ID '{ep_id}' does not match expected format"


def test_new_episode_id_starts_with_ep():
    assert new_episode_id().startswith("ep_")


def test_new_episode_id_uniqueness():
    ids = {new_episode_id() for _ in range(100)}
    assert len(ids) == 100, "Episode IDs must be unique across 100 calls"


def test_new_episode_id_is_string():
    assert isinstance(new_episode_id(), str)


# ---------------------------------------------------------------------------
# log_episode_event — no producer (no-op, must not raise)
# ---------------------------------------------------------------------------

def test_log_episode_event_no_producer_no_error():
    # Must not raise even without a producer
    log_episode_event(
        episode_id="ep_test",
        event_type="workflow_started",
        agent="orchestrator",
        data={"key": "value"},
        producer=None,
    )


def test_log_episode_event_no_data_no_error():
    log_episode_event(
        episode_id="ep_test",
        event_type="task_started",
        agent="dev",
    )


# ---------------------------------------------------------------------------
# log_episode_event — with mock producer
# ---------------------------------------------------------------------------

def test_log_episode_event_publishes_to_kafka():
    mock_producer = MagicMock()
    log_episode_event(
        episode_id="ep_20260324_000000_abcdef01",
        event_type="qa_passed",
        agent="qa",
        data={"task_id": "T001"},
        producer=mock_producer,
    )
    mock_producer.send.assert_called_once()
    topic, payload = mock_producer.send.call_args[0]
    assert topic == "episode.events"
    assert payload["episode_id"] == "ep_20260324_000000_abcdef01"
    assert payload["event_type"] == "qa_passed"
    assert payload["agent"] == "qa"
    assert payload["data"]["task_id"] == "T001"
    assert "timestamp" in payload


def test_log_episode_event_structure():
    mock_producer = MagicMock()
    log_episode_event(
        episode_id="ep_test_123",
        event_type="iteration_started",
        agent="learning_workflow",
        data={"iteration": 2},
        producer=mock_producer,
    )
    _, payload = mock_producer.send.call_args[0]
    required_keys = {"episode_id", "event_type", "agent", "timestamp", "data"}
    assert required_keys.issubset(payload.keys())


def test_log_episode_event_kafka_failure_is_silent():
    """Kafka failures must not propagate — just log a warning."""
    mock_producer = MagicMock()
    mock_producer.send.side_effect = ConnectionError("Kafka down")
    # Must not raise
    log_episode_event(
        episode_id="ep_test",
        event_type="workflow_started",
        agent="orchestrator",
        producer=mock_producer,
    )


# ---------------------------------------------------------------------------
# episode_event_to_json
# ---------------------------------------------------------------------------

def test_episode_event_to_json_is_valid_json():
    raw = episode_event_to_json(
        episode_id="ep_test",
        event_type="dev_generated",
        agent="dev",
        data={"candidates": 3},
    )
    parsed = json.loads(raw)
    assert parsed["episode_id"] == "ep_test"
    assert parsed["event_type"] == "dev_generated"
    assert parsed["agent"] == "dev"
    assert parsed["data"]["candidates"] == 3
    assert "timestamp" in parsed


def test_episode_event_to_json_no_data():
    raw = episode_event_to_json(
        episode_id="ep_test",
        event_type="qa_failed",
        agent="qa",
    )
    parsed = json.loads(raw)
    assert parsed["data"] == {}
