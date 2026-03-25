"""Tests for benchmarks/curriculum.py."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from benchmarks.curriculum import Curriculum, CurriculumState
from benchmarks.dataset_loader import BenchmarkTask


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _task(difficulty: str = "easy", task_id: str = "t001") -> BenchmarkTask:
    return BenchmarkTask(
        task_id=task_id,
        title="Test",
        description="desc",
        difficulty=difficulty,
        type="dev",
        tests=["assert True"],
        hidden_tests=[],
        expected_output={},
    )


def _make_loader(task: BenchmarkTask | None = None) -> MagicMock:
    """Return a DatasetLoader mock that always returns a single task."""
    loader = MagicMock()
    loader.sample.return_value = [task or _task()]
    return loader


def _curriculum(
    state: CurriculumState | None = None,
    task: BenchmarkTask | None = None,
) -> Curriculum:
    """Create a Curriculum with an in-memory state (no file I/O)."""
    loader = _make_loader(task)
    c = Curriculum(loader=loader, state=state or CurriculumState())
    c._save_state = MagicMock()  # prevent file writes
    return c


# ---------------------------------------------------------------------------
# Initial state
# ---------------------------------------------------------------------------

def test_starts_at_easy():
    c = _curriculum()
    assert c.current_level() == "easy"


def test_initial_success_rate_is_zero():
    c = _curriculum()
    assert c.get_success_rate("easy") == 0.0


# ---------------------------------------------------------------------------
# record_result
# ---------------------------------------------------------------------------

def test_record_result_increments_attempts():
    c = _curriculum()
    c.record_result(_task("easy"), success=True)
    assert c.state.level_stats["easy"]["attempts"] == 1
    assert c.state.level_stats["easy"]["successes"] == 1


def test_record_result_failure_no_success_increment():
    c = _curriculum()
    c.record_result(_task("easy"), success=False)
    assert c.state.level_stats["easy"]["attempts"] == 1
    assert c.state.level_stats["easy"]["successes"] == 0


def test_success_rate_calculation():
    c = _curriculum()
    for _ in range(4):
        c.record_result(_task("easy"), success=True)
    c.record_result(_task("easy"), success=False)
    assert c.get_success_rate("easy") == pytest.approx(0.8)


# ---------------------------------------------------------------------------
# Promotion logic
# ---------------------------------------------------------------------------

def test_no_promotion_below_min_attempts():
    """Less than MIN_ATTEMPTS → no promotion regardless of rate."""
    c = _curriculum()
    for _ in range(4):  # MIN_ATTEMPTS is 5
        c.record_result(_task("easy"), success=True)
    c.get_next_task()
    assert c.current_level() == "easy"


def test_no_promotion_below_threshold():
    """5 attempts but < 80 % success → stay at easy."""
    c = _curriculum()
    for _ in range(3):
        c.record_result(_task("easy"), success=True)
    for _ in range(2):
        c.record_result(_task("easy"), success=False)
    # success_rate = 3/5 = 0.6 < 0.8
    c.get_next_task()
    assert c.current_level() == "easy"


def test_promotion_on_threshold():
    """5 attempts with exactly 80 % success → promote to medium."""
    c = _curriculum()
    for _ in range(4):
        c.record_result(_task("easy"), success=True)
    c.record_result(_task("easy"), success=False)
    # success_rate = 4/5 = 0.8 >= 0.8
    c.get_next_task()
    assert c.current_level() == "medium"


def test_promotion_all_success():
    """5/5 successes → promote to medium."""
    c = _curriculum()
    for _ in range(5):
        c.record_result(_task("easy"), success=True)
    c.get_next_task()
    assert c.current_level() == "medium"


def test_no_promotion_beyond_expert():
    """At expert level there is no higher level to promote to."""
    state = CurriculumState(current_level="expert")
    # Give expert enough successes to trigger promotion
    for lvl in ["easy", "medium", "hard", "expert"]:
        state.level_stats[lvl]["attempts"] = 5
        state.level_stats[lvl]["successes"] = 5
    c = _curriculum(state=state)
    c.get_next_task()
    assert c.current_level() == "expert"


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def test_state_to_dict_roundtrip():
    state = CurriculumState(current_level="medium")
    state.level_stats["easy"]["attempts"] = 7
    d = state.to_dict()
    restored = CurriculumState.from_dict(d)
    assert restored.current_level == "medium"
    assert restored.level_stats["easy"]["attempts"] == 7


def test_state_saved_on_record_result():
    c = _curriculum()
    c.record_result(_task("easy"), success=True)
    c._save_state.assert_called()


def test_state_saved_on_promotion():
    c = _curriculum()
    for _ in range(4):
        c.record_result(_task("easy"), success=True)
    c.record_result(_task("easy"), success=False)
    c._save_state.reset_mock()
    c.get_next_task()  # triggers promotion
    c._save_state.assert_called()


def test_state_file_load_and_save(tmp_path: Path):
    state_path = tmp_path / "curriculum_state.json"
    state = CurriculumState(current_level="hard", state_path=str(state_path))

    loader = _make_loader()
    c = Curriculum(loader=loader, state=state)
    c._save_state()  # call real implementation (not mocked)

    # Re-instantiate reading from the file directly
    data = json.loads(state_path.read_text())
    loaded = CurriculumState.from_dict(data)
    assert loaded.current_level == "hard"
