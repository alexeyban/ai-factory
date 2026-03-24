"""Tests for memory/reward.py — RewardEngine and QAMetrics."""
from __future__ import annotations

import os

import pytest

from memory.reward import QAMetrics, RewardEngine, RewardWeights


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _engine(correctness=1.0, performance=0.3, complexity=0.2) -> RewardEngine:
    return RewardEngine(RewardWeights(
        correctness=correctness,
        performance=performance,
        complexity_penalty=complexity,
    ))


def _metrics(passed=5, total=5, exec_ms=100.0) -> QAMetrics:
    return QAMetrics(
        tests_passed=passed,
        tests_failed=total - passed,
        tests_total=total,
        execution_time_ms=exec_ms,
    )


# ---------------------------------------------------------------------------
# RewardEngine.compute
# ---------------------------------------------------------------------------

def test_perfect_solution_reward():
    engine = _engine()
    metrics = _metrics(passed=5, total=5, exec_ms=10.0)
    code = "def f(): return 1"  # complexity = 1
    reward = engine.compute(metrics, code)
    # correctness=1.0*1.0 + perf≈0.99*0.3 - complexity(1/10)*0.2 ≈ 1.27
    assert reward > 1.0


def test_zero_tests_passed_reward():
    engine = _engine()
    metrics = _metrics(passed=0, total=5)
    reward = engine.compute(metrics, "def f(): pass")
    # correctness=0, perf contribution remains, penalty remains → reward < 0.4
    assert reward < 0.4


def test_no_tests_total_returns_zero():
    engine = _engine()
    metrics = QAMetrics(tests_passed=0, tests_total=0)
    assert engine.compute(metrics, "x = 1") == 0.0


def test_complexity_penalty_applied():
    engine = _engine()
    simple_code = "def f(x):\n    return x\n"
    complex_code = "\n".join([
        "def f(x):",
        "    if x > 0:",
        "        for i in range(x):",
        "            if i % 2 == 0:",
        "                while i > 0:",
        "                    if i > 5:",
        "                        pass",
        "                    i -= 1",
        "    elif x < 0:",
        "        try:",
        "            pass",
        "        except Exception:",
        "            pass",
        "    return x",
    ])
    m = _metrics()
    r_simple = engine.compute(m, simple_code)
    r_complex = engine.compute(m, complex_code)
    assert r_simple > r_complex


def test_performance_factor():
    engine = _engine()
    m_fast = QAMetrics(tests_passed=5, tests_total=5, execution_time_ms=1.0)
    m_slow = QAMetrics(tests_passed=5, tests_total=5, execution_time_ms=10000.0)
    code = "def f(): pass"
    assert engine.compute(m_fast, code) > engine.compute(m_slow, code)


def test_partial_pass_reward():
    engine = _engine(correctness=1.0, performance=0.0, complexity=0.0)
    m = QAMetrics(tests_passed=3, tests_total=5)
    reward = engine.compute(m, "x = 1")
    assert abs(reward - 0.6) < 1e-9


# ---------------------------------------------------------------------------
# RewardWeights.from_env
# ---------------------------------------------------------------------------

def test_weights_from_env(monkeypatch):
    monkeypatch.setenv("REWARD_CORRECTNESS_W", "2.0")
    monkeypatch.setenv("REWARD_PERF_W", "0.5")
    monkeypatch.setenv("REWARD_COMPLEXITY_W", "0.1")
    w = RewardWeights.from_env()
    assert w.correctness == 2.0
    assert w.performance == 0.5
    assert w.complexity_penalty == 0.1


def test_weights_defaults_when_env_missing(monkeypatch):
    for var in ("REWARD_CORRECTNESS_W", "REWARD_PERF_W", "REWARD_COMPLEXITY_W"):
        monkeypatch.delenv(var, raising=False)
    w = RewardWeights.from_env()
    assert w.correctness == 1.0
    assert w.performance == 0.3
    assert w.complexity_penalty == 0.2


# ---------------------------------------------------------------------------
# _cyclomatic_complexity
# ---------------------------------------------------------------------------

def test_cyclomatic_complexity_simple():
    code = "def f(x):\n    return x + 1\n"
    c = RewardEngine._cyclomatic_complexity(code)
    assert c == 1.0  # no branches → count=0, complexity=1


def test_cyclomatic_complexity_branchy():
    code = "\n".join([
        "def f(x):",
        "    if x > 0:",
        "        for i in range(x):",
        "            if i % 2 == 0:",
        "                pass",
        "        while x > 0:",
        "            x -= 1",
        "    return x",
    ])
    c = RewardEngine._cyclomatic_complexity(code)
    assert c >= 5.0


def test_cyclomatic_complexity_syntax_error():
    c = RewardEngine._cyclomatic_complexity("def broken(:\n    pass")
    assert c == 1.0


# ---------------------------------------------------------------------------
# metrics_from_pytest_result
# ---------------------------------------------------------------------------

def test_metrics_from_pytest_result_with_junit():
    data = {
        "returncode": 0,
        "stdout": "",
        "stderr": "",
        "coverage": {"percent": 80.0},
        "junit": {"tests_passed": 4, "tests_failed": 1, "tests_total": 5},
    }
    m = RewardEngine.metrics_from_pytest_result(data, execution_time_ms=200.0)
    assert m.tests_passed == 4
    assert m.tests_failed == 1
    assert m.tests_total == 5
    assert abs(m.coverage - 0.8) < 1e-9
    assert m.execution_time_ms == 200.0


def test_metrics_from_pytest_result_fallback_on_success():
    data = {"returncode": 0, "stdout": "", "stderr": "", "coverage": None, "junit": {}}
    m = RewardEngine.metrics_from_pytest_result(data)
    assert m.tests_total == 1
    assert m.tests_passed == 1


def test_metrics_from_pytest_result_fallback_on_failure():
    data = {"returncode": 1, "stdout": "", "stderr": "", "coverage": None, "junit": {}}
    m = RewardEngine.metrics_from_pytest_result(data)
    assert m.tests_total == 1
    assert m.tests_failed == 1
