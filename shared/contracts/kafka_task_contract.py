"""
Kafka task contract message.

Used to publish/consume task contracts over the `task.contracts` Kafka topic.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

LOGGER = logging.getLogger(__name__)
TASK_CONTRACTS_TOPIC = "task.contracts"


@dataclass
class TaskContractMessage:
    task_id: str
    episode_id: str
    payload: dict[str, Any]
    schema_version: str = "1.0"
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "episode_id": self.episode_id,
            "payload": self.payload,
            "schema_version": self.schema_version,
            "timestamp": self.timestamp,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskContractMessage":
        return cls(
            task_id=data["task_id"],
            episode_id=data["episode_id"],
            payload=data.get("payload", {}),
            schema_version=data.get("schema_version", "1.0"),
            timestamp=data.get("timestamp", ""),
        )

    @classmethod
    def from_json(cls, raw: str) -> "TaskContractMessage":
        return cls.from_dict(json.loads(raw))


def publish_task_contract(
    task: dict[str, Any],
    episode_id: str,
    producer: Any | None = None,
) -> None:
    """
    Publish a task contract to the `task.contracts` Kafka topic.

    Silently logs a warning if the producer is None or publish fails
    (Kafka unavailability must not break the main pipeline).
    """
    if producer is None:
        LOGGER.debug("No Kafka producer provided; skipping task.contracts publish")
        return

    msg = TaskContractMessage(
        task_id=task.get("task_id", "unknown"),
        episode_id=episode_id,
        payload=task,
    )
    try:
        producer.send(TASK_CONTRACTS_TOPIC, msg.to_dict())
    except Exception as exc:
        LOGGER.warning(f"Failed to publish task contract to Kafka: {exc}")
