"""
Episodic memory for the AI Factory self-learning loop.

Stores workflow episodes, dev-generated solutions, and reward scores.
Supports fingerprinting (duplicate detection) and similar-task lookup via Qdrant.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from memory.db import MemoryDB

LOGGER = logging.getLogger(__name__)
MEMORY_EVENTS_TOPIC = "memory.events"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class EpisodeRecord:
    id: str
    workflow_run_id: str
    started_at: datetime
    status: str  # running | success | failed | partial
    task_count: int = 0
    random_seed: int = 42
    metadata: dict = field(default_factory=dict)
    finished_at: datetime | None = None


@dataclass
class SolutionRecord:
    episode_id: str
    task_id: str
    iteration: int
    code_hash: str
    code_path: str
    reward: float
    id: int | None = None  # set after INSERT


@dataclass
class RewardRecord:
    solution_id: int
    correctness: float
    performance: float
    complexity_penalty: float
    total: float
    tests_passed: int = 0
    tests_total: int = 0
    execution_time_ms: float = 0.0
    peak_memory_mb: float = 0.0


@dataclass
class SimilarTask:
    task_id: str
    similarity: float
    best_reward: float
    solution_path: str


# ---------------------------------------------------------------------------
# EpisodicMemory
# ---------------------------------------------------------------------------

class EpisodicMemory:
    """
    Persistence layer for episodes and solutions.

    Kafka publishing is fire-and-forget — failures are logged as warnings
    and never interrupt the caller.
    """

    def __init__(self, db: MemoryDB, vector_memory: Any | None = None,
                 kafka_producer: Any | None = None) -> None:
        self._db = db
        self._vector = vector_memory  # VectorMemory instance (optional)
        self._kafka = kafka_producer

    # ------------------------------------------------------------------
    # Episode operations
    # ------------------------------------------------------------------

    async def store_episode(self, episode: EpisodeRecord) -> None:
        await self._db.execute(
            """
            INSERT INTO episodes (id, workflow_run_id, started_at, status,
                                  task_count, random_seed, metadata)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (id) DO NOTHING
            """,
            episode.id,
            episode.workflow_run_id,
            episode.started_at,
            episode.status,
            episode.task_count,
            episode.random_seed,
            json.dumps(episode.metadata),
        )
        self._publish("episode_stored", episode_id=episode.id)

    async def update_episode_status(
        self,
        episode_id: str,
        status: str,
        finished_at: datetime | None = None,
        task_count: int | None = None,
    ) -> None:
        finished_at = finished_at or datetime.now(timezone.utc)
        if task_count is not None:
            await self._db.execute(
                """
                UPDATE episodes
                SET status = $2, finished_at = $3, task_count = $4
                WHERE id = $1
                """,
                episode_id, status, finished_at, task_count,
            )
        else:
            await self._db.execute(
                """
                UPDATE episodes
                SET status = $2, finished_at = $3
                WHERE id = $1
                """,
                episode_id, status, finished_at,
            )

    async def get_episode(self, episode_id: str) -> EpisodeRecord | None:
        row = await self._db.fetchrow(
            "SELECT * FROM episodes WHERE id = $1", episode_id
        )
        if not row:
            return None
        return EpisodeRecord(
            id=row["id"],
            workflow_run_id=row["workflow_run_id"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            status=row["status"],
            task_count=row["task_count"],
            random_seed=row["random_seed"],
            metadata=row["metadata"] or {},
        )

    # ------------------------------------------------------------------
    # Solution operations
    # ------------------------------------------------------------------

    async def store_solution(self, solution: SolutionRecord,
                             reward: RewardRecord | None = None) -> int:
        """
        Insert solution and optionally its reward.
        Updates is_best flag if this solution has the highest reward for the task.
        Returns the new solution id.
        """
        async with self._db.transaction() as conn:
            solution_id = await conn.fetchval(
                """
                INSERT INTO solutions
                    (episode_id, task_id, iteration, code_hash, code_path, reward)
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING id
                """,
                solution.episode_id,
                solution.task_id,
                solution.iteration,
                solution.code_hash,
                solution.code_path,
                solution.reward,
            )

            # Update is_best: demote previous best if new reward is higher
            best_row = await conn.fetchrow(
                """
                SELECT id, reward FROM solutions
                WHERE task_id = $1 AND is_best = TRUE
                ORDER BY reward DESC
                LIMIT 1
                """,
                solution.task_id,
            )
            if best_row is None or solution.reward > best_row["reward"]:
                if best_row:
                    await conn.execute(
                        "UPDATE solutions SET is_best = FALSE WHERE id = $1",
                        best_row["id"],
                    )
                await conn.execute(
                    "UPDATE solutions SET is_best = TRUE WHERE id = $1",
                    solution_id,
                )

            if reward:
                await conn.execute(
                    """
                    INSERT INTO rewards
                        (solution_id, correctness, performance,
                         complexity_penalty, total,
                         tests_passed, tests_total,
                         execution_time_ms, peak_memory_mb)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                    """,
                    solution_id,
                    reward.correctness,
                    reward.performance,
                    reward.complexity_penalty,
                    reward.total,
                    reward.tests_passed,
                    reward.tests_total,
                    reward.execution_time_ms,
                    reward.peak_memory_mb,
                )

        self._publish("solution_stored",
                      episode_id=solution.episode_id,
                      task_id=solution.task_id,
                      data={"reward": solution.reward})
        return solution_id

    async def get_best_solution(self, task_id: str) -> SolutionRecord | None:
        row = await self._db.fetchrow(
            """
            SELECT * FROM solutions
            WHERE task_id = $1 AND is_best = TRUE
            ORDER BY reward DESC
            LIMIT 1
            """,
            task_id,
        )
        if not row:
            return None
        return SolutionRecord(
            id=row["id"],
            episode_id=row["episode_id"],
            task_id=row["task_id"],
            iteration=row["iteration"],
            code_hash=row["code_hash"],
            code_path=row["code_path"],
            reward=row["reward"],
        )

    async def check_solution_fingerprint(self, code_hash: str,
                                         task_id: str) -> bool:
        """Return True if this exact code_hash has been seen for task_id."""
        val = await self._db.fetchval(
            """
            SELECT COUNT(*) FROM solutions
            WHERE task_id = $1 AND code_hash = $2
            """,
            task_id, code_hash,
        )
        return (val or 0) > 0

    # ------------------------------------------------------------------
    # Vector-backed similar task lookup
    # ------------------------------------------------------------------

    async def get_similar_tasks(
        self,
        task_embedding: list[float],
        top_k: int = 5,
    ) -> list[SimilarTask]:
        """
        Find similar past tasks via Qdrant embedding search.
        Falls back to empty list if VectorMemory is unavailable.
        """
        if self._vector is None:
            return []
        try:
            results = await self._vector.search_similar_episodes(
                task_embedding, top_k=top_k
            )
            similar = []
            for r in results:
                task_id = r.get("task_id", "")
                best = await self.get_best_solution(task_id)
                similar.append(SimilarTask(
                    task_id=task_id,
                    similarity=r.get("score", 0.0),
                    best_reward=best.reward if best else 0.0,
                    solution_path=best.code_path if best else "",
                ))
            return similar
        except Exception as exc:
            LOGGER.warning(f"get_similar_tasks failed: {exc}")
            return []

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
