"""Dataset loader for AI Factory benchmarks.

Loads structured benchmark tasks from JSON files and converts them to
TaskContracts compatible with the learning workflow.
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class BenchmarkTask:
    task_id: str
    title: str
    description: str
    difficulty: str          # easy | medium | hard | expert
    type: str
    tests: list[str]
    hidden_tests: list[str]
    expected_output: dict
    time_limit_ms: float = 1000.0
    memory_limit_mb: float = 100.0

    def to_task_contract(self) -> dict:
        """Convert to TaskContract format for the learning workflow."""
        return {
            "task_id": self.task_id,
            "title": self.title,
            "description": self.description,
            "type": "feature",
            "dependencies": [],
            "input": {
                "files": [],
                "context": "\n".join(self.tests),
            },
            "output": {
                "files": [],
                "artifacts": [],
                "expected_result": str(self.expected_output),
            },
            "verification": {
                "method": "pytest",
                "test_file": None,
                "criteria": self.tests,
            },
            "acceptance_criteria": self.tests,
            "estimated_size": _difficulty_to_size(self.difficulty),
            "can_parallelize": True,
            "benchmark_metadata": {
                "difficulty": self.difficulty,
                "time_limit_ms": self.time_limit_ms,
                "memory_limit_mb": self.memory_limit_mb,
                "hidden_tests": self.hidden_tests,
            },
        }


def _difficulty_to_size(difficulty: str) -> str:
    return {
        "easy": "small",
        "medium": "medium",
        "hard": "large",
        "expert": "large",
    }.get(difficulty, "medium")


class DatasetLoader:
    DIFFICULTIES = ["easy", "medium", "hard", "expert"]
    DATASETS_DIR = Path(__file__).parent / "datasets"

    def load(self, difficulty: str) -> list[BenchmarkTask]:
        """Load all tasks for the given difficulty level."""
        if difficulty not in self.DIFFICULTIES:
            raise ValueError(
                f"Unknown difficulty '{difficulty}'. "
                f"Choose from: {self.DIFFICULTIES}"
            )
        path = self.DATASETS_DIR / f"{difficulty}.json"
        if not path.exists():
            raise FileNotFoundError(f"Dataset not found: {path}")
        data = json.loads(path.read_text(encoding="utf-8"))
        return [BenchmarkTask(**t) for t in data["tasks"]]

    def load_all(self) -> dict[str, list[BenchmarkTask]]:
        """Load all available datasets."""
        result: dict[str, list[BenchmarkTask]] = {}
        for d in self.DIFFICULTIES:
            path = self.DATASETS_DIR / f"{d}.json"
            if path.exists():
                result[d] = self.load(d)
        return result

    def sample(
        self,
        difficulty: str,
        n: int = 1,
        seed: int | None = None,
    ) -> list[BenchmarkTask]:
        """Return a random sample of n tasks from the given difficulty level."""
        tasks = self.load(difficulty)
        rng = random.Random(seed)
        return rng.sample(tasks, min(n, len(tasks)))
