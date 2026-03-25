"""Curriculum learning state machine for AI Factory benchmarks.

Manages progressive difficulty: starts at 'easy' and promotes to higher levels
when the agent achieves >= PROMOTION_THRESHOLD success rate over MIN_ATTEMPTS.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from benchmarks.dataset_loader import BenchmarkTask, DatasetLoader

LOGGER = logging.getLogger(__name__)

_DEFAULT_STATE_PATH = Path("workspace/.ai_factory/curriculum_state.json")


@dataclass
class CurriculumState:
    current_level: str = "easy"
    level_stats: dict = field(
        default_factory=lambda: {
            "easy":   {"attempts": 0, "successes": 0},
            "medium": {"attempts": 0, "successes": 0},
            "hard":   {"attempts": 0, "successes": 0},
            "expert": {"attempts": 0, "successes": 0},
        }
    )
    state_path: str = str(_DEFAULT_STATE_PATH)

    def to_dict(self) -> dict:
        return {
            "current_level": self.current_level,
            "level_stats": self.level_stats,
            "state_path": self.state_path,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CurriculumState":
        obj = cls()
        obj.current_level = data.get("current_level", "easy")
        obj.level_stats = data.get("level_stats", obj.level_stats)
        obj.state_path = data.get("state_path", str(_DEFAULT_STATE_PATH))
        return obj


class Curriculum:
    LEVELS = ["easy", "medium", "hard", "expert"]
    PROMOTION_THRESHOLD = 0.8   # success_rate required to advance
    MIN_ATTEMPTS = 5            # minimum attempts before promotion is evaluated

    def __init__(
        self,
        loader: DatasetLoader,
        state: CurriculumState | None = None,
    ) -> None:
        self.loader = loader
        self.state = state if state is not None else self._load_state()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_next_task(self) -> BenchmarkTask:
        """Return the next task, promoting level if threshold is met."""
        current = self.state.current_level
        if self._should_promote(current):
            next_level = self._next_level(current)
            if next_level:
                LOGGER.info(
                    "[curriculum] Promoting from %s to %s", current, next_level
                )
                self.state.current_level = next_level
                self._save_state()
                current = next_level
        return self.loader.sample(current, n=1)[0]

    def record_result(self, task: BenchmarkTask, success: bool) -> None:
        """Update statistics after a task attempt."""
        stats = self.state.level_stats[task.difficulty]
        stats["attempts"] += 1
        if success:
            stats["successes"] += 1
        self._save_state()

    def get_success_rate(self, level: str) -> float:
        stats = self.state.level_stats[level]
        if stats["attempts"] == 0:
            return 0.0
        return stats["successes"] / stats["attempts"]

    def current_level(self) -> str:
        return self.state.current_level

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _should_promote(self, level: str) -> bool:
        stats = self.state.level_stats[level]
        if stats["attempts"] < self.MIN_ATTEMPTS:
            return False
        return self.get_success_rate(level) >= self.PROMOTION_THRESHOLD

    def _next_level(self, level: str) -> str | None:
        idx = self.LEVELS.index(level)
        return self.LEVELS[idx + 1] if idx + 1 < len(self.LEVELS) else None

    def _load_state(self) -> CurriculumState:
        path = Path(_DEFAULT_STATE_PATH)
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                return CurriculumState.from_dict(data)
            except Exception as exc:
                LOGGER.warning("[curriculum] Failed to load state: %s", exc)
        return CurriculumState()

    def _save_state(self) -> None:
        path = Path(self.state.state_path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(self.state.to_dict(), indent=2), encoding="utf-8"
            )
        except Exception as exc:
            LOGGER.warning("[curriculum] Failed to save state: %s", exc)
