"""Replay Buffer — accumulates good and bad solutions for policy learning."""
from __future__ import annotations

import json
import random
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class BufferedSolution:
    task_id: str
    episode_id: str
    iteration: int
    reward: float
    artifact: str = ""
    code: str = ""
    skills_used: list[str] = field(default_factory=list)


class ReplayBuffer:
    """Fixed-capacity buffer storing best (good) and worst (bad) solutions.

    Good solutions (reward >= good_threshold) are used as few-shot examples
    for the Dev agent and for skill weight updates.
    Bad solutions are retained to help identify anti-patterns.
    """

    def __init__(
        self,
        max_good: int = 100,
        max_bad: int = 50,
        good_threshold: float = 0.7,
        random_seed: Optional[int] = None,
    ) -> None:
        self._good: deque[BufferedSolution] = deque(maxlen=max_good)
        self._bad: deque[BufferedSolution] = deque(maxlen=max_bad)
        self._threshold = good_threshold
        self._rng = random.Random(random_seed)

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def add(self, solution: BufferedSolution) -> None:
        if solution.reward >= self._threshold:
            self._good.append(solution)
        else:
            self._bad.append(solution)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def sample_good(self, k: int) -> list[BufferedSolution]:
        """Return up to k random good solutions."""
        k = min(k, len(self._good))
        if k == 0:
            return []
        return self._rng.sample(list(self._good), k)

    def sample_bad(self, k: int) -> list[BufferedSolution]:
        """Return up to k random bad solutions."""
        k = min(k, len(self._bad))
        if k == 0:
            return []
        return self._rng.sample(list(self._bad), k)

    def get_best(self, task_id: str) -> Optional[BufferedSolution]:
        """Best (highest reward) good solution for a specific task."""
        relevant = [s for s in self._good if s.task_id == task_id]
        return max(relevant, key=lambda s: s.reward, default=None)

    def size(self) -> dict:
        return {"good": len(self._good), "bad": len(self._bad)}

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_json(self) -> str:
        data = {
            "max_good": self._good.maxlen,
            "max_bad": self._bad.maxlen,
            "good_threshold": self._threshold,
            "good": [asdict(s) for s in self._good],
            "bad": [asdict(s) for s in self._bad],
        }
        return json.dumps(data, indent=2, ensure_ascii=False)

    @classmethod
    def from_json(cls, data: str) -> "ReplayBuffer":
        raw = json.loads(data)
        buf = cls(
            max_good=raw.get("max_good", 100),
            max_bad=raw.get("max_bad", 50),
            good_threshold=raw.get("good_threshold", 0.7),
        )
        for s in raw.get("good", []):
            buf._good.append(BufferedSolution(**s))
        for s in raw.get("bad", []):
            buf._bad.append(BufferedSolution(**s))
        return buf

    # ------------------------------------------------------------------
    # File persistence
    # ------------------------------------------------------------------

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json(), encoding="utf-8")

    @classmethod
    def load(cls, path: Path, **kwargs) -> "ReplayBuffer":
        """Load from file; return a fresh empty buffer if file is missing/corrupt."""
        if not path.exists():
            return cls(**kwargs)
        try:
            return cls.from_json(path.read_text(encoding="utf-8"))
        except Exception:
            return cls(**kwargs)
