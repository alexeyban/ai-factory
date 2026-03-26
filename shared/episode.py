"""
Episode management for the AI Factory self-learning loop.

An episode represents one complete workflow execution.
Episode IDs are time-ordered and globally unique.
Events are published to the `episode.events` Kafka topic.
"""
from __future__ import annotations

import json
import logging
import random
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

LOGGER = logging.getLogger(__name__)
EPISODE_EVENTS_TOPIC = "episode.events"

# Valid event types
EVENT_TYPES = frozenset({
    "workflow_started",
    "workflow_finished",
    "task_started",
    "task_finished",
    "iteration_started",
    "iteration_finished",
    "dev_generated",
    "qa_passed",
    "qa_failed",
    "stagnation_detected",
    "loop_protected_stop",
    "skill_extracted",
})


def new_episode_id() -> str:
    """
    Generate a unique, time-ordered episode ID.

    Format: ep_YYYYMMDD_HHMMSS_<8 hex chars>
    Example: ep_20260324_112233_a1b2c3d4
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    suffix = uuid4().hex[:8]
    return f"ep_{ts}_{suffix}"


def log_episode_event(
    episode_id: str,
    event_type: str,
    agent: str,
    data: dict[str, Any] | None = None,
    producer: Any | None = None,
) -> None:
    """
    Log an episode lifecycle event.

    Publishes to the `episode.events` Kafka topic when a producer is provided.
    Always logs at DEBUG level regardless of Kafka availability.

    Kafka failures are caught and logged as warnings — they must NOT
    interrupt the main workflow execution.
    """
    event: dict[str, Any] = {
        "episode_id": episode_id,
        "event_type": event_type,
        "agent": agent,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "data": data or {},
    }

    LOGGER.debug(f"[episode] {episode_id} | {event_type} | {agent}")

    if producer is None:
        return

    try:
        producer.send(EPISODE_EVENTS_TOPIC, event)
    except Exception as exc:
        LOGGER.warning(
            f"Failed to publish episode event to Kafka "
            f"(episode={episode_id}, type={event_type}): {exc}"
        )


def episode_event_to_json(
    episode_id: str,
    event_type: str,
    agent: str,
    data: dict[str, Any] | None = None,
) -> str:
    """Return the event as a JSON string (for testing / manual inspection)."""
    return json.dumps(
        {
            "episode_id": episode_id,
            "event_type": event_type,
            "agent": agent,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "data": data or {},
        },
        ensure_ascii=False,
    )
