"""
Failure memory for the AI Factory self-learning loop.

Records dev/QA failures and surfaces aggregated patterns so the Dev agent
can avoid repeating known mistakes.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from memory.db import MemoryDB

LOGGER = logging.getLogger(__name__)
MEMORY_EVENTS_TOPIC = "memory.events"

VALID_FAILURE_TYPES = frozenset({
    "timeout",
    "wrong_complexity",
    "failed_tests",
    "llm_error",
    "git_conflict",
    "unknown",
})


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class FailurePattern:
    failure_type: str
    count: int
    last_error: str
    common_context: dict


# ---------------------------------------------------------------------------
# FailureMemory
# ---------------------------------------------------------------------------

class FailureMemory:
    """
    Persistence layer for failure records.

    Kafka publishing is fire-and-forget.
    """

    def __init__(self, db: MemoryDB,
                 kafka_producer: Any | None = None) -> None:
        self._db = db
        self._kafka = kafka_producer

    async def record_failure(
        self,
        episode_id: str,
        task_id: str,
        failure_type: str,
        error_message: str,
        context: dict | None = None,
    ) -> None:
        """
        Insert a failure record.

        Unknown failure_type values are coerced to 'unknown' to satisfy the
        database CHECK constraint.
        """
        if failure_type not in VALID_FAILURE_TYPES:
            LOGGER.warning(
                f"Unknown failure_type '{failure_type}', storing as 'unknown'"
            )
            failure_type = "unknown"

        await self._db.execute(
            """
            INSERT INTO failures
                (episode_id, task_id, failure_type, error_message, context)
            VALUES ($1, $2, $3, $4, $5)
            """,
            episode_id,
            task_id,
            failure_type,
            error_message,
            json.dumps(context or {}),
        )
        self._publish("failure_recorded",
                      episode_id=episode_id,
                      task_id=task_id,
                      data={"failure_type": failure_type})

    async def get_failure_patterns(
        self,
        task_type: str,
        limit: int = 5,
    ) -> list[FailurePattern]:
        """
        Return the most common failure patterns for a given task type.

        task_type is matched against the `context->>'task_type'` JSON field.
        """
        rows = await self._db.fetch(
            """
            SELECT
                failure_type,
                COUNT(*)            AS count,
                MAX(error_message)  AS last_error,
                MAX(context)        AS common_context
            FROM failures
            WHERE context->>'task_type' = $1
            GROUP BY failure_type
            ORDER BY count DESC
            LIMIT $2
            """,
            task_type,
            limit,
        )
        return [
            FailurePattern(
                failure_type=r["failure_type"],
                count=r["count"],
                last_error=r["last_error"] or "",
                common_context=r["common_context"] or {},
            )
            for r in rows
        ]

    async def get_failure_summary(self, task_id: str) -> str:
        """
        Return a formatted string describing past failures for this task_id.
        Intended for inclusion in a Dev agent prompt.
        """
        rows = await self._db.fetch(
            """
            SELECT failure_type, error_message, created_at
            FROM failures
            WHERE task_id = $1
            ORDER BY created_at DESC
            LIMIT 10
            """,
            task_id,
        )
        if not rows:
            return ""

        lines = [f"Past failures for task {task_id}:"]
        for r in rows:
            ts = r["created_at"].strftime("%Y-%m-%d %H:%M") if r["created_at"] else "?"
            lines.append(f"  [{ts}] {r['failure_type']}: {r['error_message'] or '(no message)'}")
        return "\n".join(lines)

    async def get_recent_failures(
        self,
        task_id: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        """Return raw recent failure rows, optionally filtered by task_id."""
        if task_id:
            rows = await self._db.fetch(
                """
                SELECT * FROM failures
                WHERE task_id = $1
                ORDER BY created_at DESC
                LIMIT $2
                """,
                task_id, limit,
            )
        else:
            rows = await self._db.fetch(
                """
                SELECT * FROM failures
                ORDER BY created_at DESC
                LIMIT $1
                """,
                limit,
            )
        return rows

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _publish(self, event_type: str, episode_id: str = "",
                 task_id: str = "", data: dict | None = None) -> None:
        if self._kafka is None:
            return
        payload = {
            "event_type": event_type,
            "episode_id": episode_id,
            "task_id": task_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "data": data or {},
        }
        try:
            self._kafka.send(MEMORY_EVENTS_TOPIC, payload)
        except Exception as exc:
            LOGGER.warning(f"Failed to publish memory event ({event_type}): {exc}")
