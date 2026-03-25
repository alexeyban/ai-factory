"""Tests for benchmarks/dataset_loader.py."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from benchmarks.dataset_loader import BenchmarkTask, DatasetLoader


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_MINIMAL_DATASET = {
    "dataset_version": "1.0",
    "difficulty": "easy",
    "tasks": [
        {
            "task_id": "t001",
            "title": "Test task",
            "description": "Write a function foo() -> int that returns 42.",
            "difficulty": "easy",
            "type": "dev",
            "tests": ["assert foo() == 42"],
            "hidden_tests": ["assert foo() > 0"],
            "expected_output": {"function_name": "foo", "signature": "def foo() -> int"},
            "time_limit_ms": 100.0,
            "memory_limit_mb": 50.0,
        },
        {
            "task_id": "t002",
            "title": "Another task",
            "description": "Write a function bar() -> str that returns 'hello'.",
            "difficulty": "easy",
            "type": "dev",
            "tests": ["assert bar() == 'hello'"],
            "hidden_tests": [],
            "expected_output": {"function_name": "bar", "signature": "def bar() -> str"},
            "time_limit_ms": 100.0,
            "memory_limit_mb": 50.0,
        },
    ],
}


def _make_loader_with_temp_dir(datasets: dict[str, dict]) -> DatasetLoader:
    """Create a DatasetLoader pointing at a temporary directory."""
    tmp = Path(tempfile.mkdtemp())
    for difficulty, data in datasets.items():
        (tmp / f"{difficulty}.json").write_text(
            json.dumps(data), encoding="utf-8"
        )
    loader = DatasetLoader()
    loader.DATASETS_DIR = tmp
    return loader


# ---------------------------------------------------------------------------
# load()
# ---------------------------------------------------------------------------

def test_load_easy_dataset_returns_benchmark_tasks():
    loader = _make_loader_with_temp_dir({"easy": _MINIMAL_DATASET})
    tasks = loader.load("easy")
    assert len(tasks) == 2
    assert all(isinstance(t, BenchmarkTask) for t in tasks)


def test_dataset_has_required_fields():
    loader = _make_loader_with_temp_dir({"easy": _MINIMAL_DATASET})
    task = loader.load("easy")[0]
    assert task.task_id == "t001"
    assert task.title == "Test task"
    assert task.difficulty == "easy"
    assert task.type == "dev"
    assert isinstance(task.tests, list)
    assert isinstance(task.hidden_tests, list)
    assert isinstance(task.expected_output, dict)
    assert task.time_limit_ms == 100.0
    assert task.memory_limit_mb == 50.0


def test_load_raises_for_missing_file():
    loader = DatasetLoader()
    loader.DATASETS_DIR = Path(tempfile.mkdtemp())  # empty dir
    with pytest.raises(FileNotFoundError):
        loader.load("easy")


def test_load_raises_for_unknown_difficulty():
    loader = DatasetLoader()
    with pytest.raises(ValueError, match="Unknown difficulty"):
        loader.load("legendary")


# ---------------------------------------------------------------------------
# load_all()
# ---------------------------------------------------------------------------

def test_load_all_returns_available_difficulties():
    loader = _make_loader_with_temp_dir({
        "easy": _MINIMAL_DATASET,
        "medium": {**_MINIMAL_DATASET, "difficulty": "medium"},
    })
    all_tasks = loader.load_all()
    assert "easy" in all_tasks
    assert "medium" in all_tasks
    assert "hard" not in all_tasks  # file doesn't exist


def test_load_all_skips_missing_difficulties():
    loader = _make_loader_with_temp_dir({"easy": _MINIMAL_DATASET})
    all_tasks = loader.load_all()
    assert list(all_tasks.keys()) == ["easy"]


# ---------------------------------------------------------------------------
# sample()
# ---------------------------------------------------------------------------

def test_sample_returns_n_tasks():
    loader = _make_loader_with_temp_dir({"easy": _MINIMAL_DATASET})
    samples = loader.sample("easy", n=1)
    assert len(samples) == 1
    assert isinstance(samples[0], BenchmarkTask)


def test_sample_clamps_to_available_tasks():
    loader = _make_loader_with_temp_dir({"easy": _MINIMAL_DATASET})
    samples = loader.sample("easy", n=100)
    assert len(samples) == 2  # only 2 tasks in fixture


def test_sample_is_deterministic_with_seed():
    loader = _make_loader_with_temp_dir({"easy": _MINIMAL_DATASET})
    s1 = loader.sample("easy", n=1, seed=42)
    s2 = loader.sample("easy", n=1, seed=42)
    assert s1[0].task_id == s2[0].task_id


# ---------------------------------------------------------------------------
# to_task_contract()
# ---------------------------------------------------------------------------

def test_to_task_contract_has_required_keys():
    loader = _make_loader_with_temp_dir({"easy": _MINIMAL_DATASET})
    task = loader.load("easy")[0]
    contract = task.to_task_contract()

    required = [
        "task_id", "title", "description", "type", "dependencies",
        "input", "output", "verification", "acceptance_criteria",
        "estimated_size", "can_parallelize",
    ]
    for key in required:
        assert key in contract, f"Missing key: {key}"


def test_to_task_contract_maps_difficulty_to_size():
    loader = _make_loader_with_temp_dir({"easy": _MINIMAL_DATASET})
    task = loader.load("easy")[0]
    contract = task.to_task_contract()
    assert contract["estimated_size"] == "small"


def test_to_task_contract_includes_benchmark_metadata():
    loader = _make_loader_with_temp_dir({"easy": _MINIMAL_DATASET})
    task = loader.load("easy")[0]
    contract = task.to_task_contract()
    meta = contract["benchmark_metadata"]
    assert meta["difficulty"] == "easy"
    assert meta["time_limit_ms"] == 100.0
    assert "hidden_tests" in meta


def test_to_task_contract_tests_in_acceptance_criteria():
    loader = _make_loader_with_temp_dir({"easy": _MINIMAL_DATASET})
    task = loader.load("easy")[0]
    contract = task.to_task_contract()
    assert contract["acceptance_criteria"] == task.tests


# ---------------------------------------------------------------------------
# Real datasets smoke test (only runs if files exist)
# ---------------------------------------------------------------------------

def test_real_easy_dataset_loads():
    loader = DatasetLoader()
    try:
        tasks = loader.load("easy")
        assert len(tasks) >= 10
        for t in tasks:
            assert t.task_id
            assert t.description
    except FileNotFoundError:
        pytest.skip("Real easy.json not present")
