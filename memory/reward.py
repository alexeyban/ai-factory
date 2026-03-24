"""
Reward Engine for the AI Factory self-learning loop.

Computes a scalar reward signal from QA metrics so the learning loop
can compare and rank candidate solutions.

Formula:
    reward = correctness * w_c + performance * w_p - complexity * w_x

where:
    correctness  = tests_passed / tests_total          (0.0 – 1.0)
    performance  = 1 / (1 + exec_time_ms / 1000)      (0.0 – 1.0, lower latency → higher)
    complexity   = cyclomatic_complexity(code) / 10    (normalised; penalises spaghetti code)
"""
from __future__ import annotations

import ast
import logging
import os
from dataclasses import dataclass, field

LOGGER = logging.getLogger(__name__)

# Normalisation denominator for cyclomatic complexity.
# A function with 10 decision points is considered "complex".
_COMPLEXITY_NORM = 10.0


# ---------------------------------------------------------------------------
# QAMetrics
# ---------------------------------------------------------------------------

@dataclass
class QAMetrics:
    """Metrics produced by a single pytest run."""
    tests_passed: int = 0
    tests_failed: int = 0
    tests_total: int = 0
    coverage: float = 0.0           # 0.0 – 1.0 (fraction, not percent)
    execution_time_ms: float = 0.0
    peak_memory_mb: float = 0.0
    error_output: str = ""

    @property
    def pass_rate(self) -> float:
        if self.tests_total == 0:
            return 0.0
        return self.tests_passed / self.tests_total


# ---------------------------------------------------------------------------
# RewardWeights
# ---------------------------------------------------------------------------

@dataclass
class RewardWeights:
    """Configurable weights for the reward formula."""
    correctness: float = 1.0
    performance: float = 0.3
    complexity_penalty: float = 0.2

    @classmethod
    def from_env(cls) -> "RewardWeights":
        return cls(
            correctness=float(os.getenv("REWARD_CORRECTNESS_W", "1.0")),
            performance=float(os.getenv("REWARD_PERF_W", "0.3")),
            complexity_penalty=float(os.getenv("REWARD_COMPLEXITY_W", "0.2")),
        )


# ---------------------------------------------------------------------------
# RewardEngine
# ---------------------------------------------------------------------------

class RewardEngine:
    """
    Compute a scalar reward from QA metrics and candidate source code.

    Usage::

        engine = RewardEngine()
        reward = engine.compute(metrics, code)
    """

    def __init__(self, weights: RewardWeights | None = None) -> None:
        self.weights = weights or RewardWeights.from_env()

    def compute(self, metrics: QAMetrics, code: str) -> float:
        """
        Return a reward scalar in roughly the range [–0.2, 1.3].

        Returns 0.0 immediately when there are no tests (no training signal).
        """
        if metrics.tests_total == 0:
            return 0.0

        correctness = metrics.tests_passed / metrics.tests_total
        performance = 1.0 / (1.0 + metrics.execution_time_ms / 1000.0)
        complexity = self._cyclomatic_complexity(code) / _COMPLEXITY_NORM

        reward = (
            correctness * self.weights.correctness
            + performance * self.weights.performance
            - complexity * self.weights.complexity_penalty
        )
        LOGGER.debug(
            "[reward] correctness=%.3f performance=%.3f complexity=%.3f → reward=%.4f",
            correctness, performance, complexity, reward,
        )
        return reward

    # ------------------------------------------------------------------
    # Static helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _cyclomatic_complexity(code: str) -> float:
        """
        Approximate cyclomatic complexity via AST node counting.

        complexity = 1 + (number of decision points)
        Decision points: if / elif / for / while / ExceptHandler / BoolOp / comprehension

        Returns 1.0 on syntax error (minimum possible complexity).
        """
        try:
            tree = ast.parse(code)
            count = sum(
                1 for node in ast.walk(tree)
                if isinstance(node, (
                    ast.If,
                    ast.For,
                    ast.While,
                    ast.ExceptHandler,
                    ast.BoolOp,
                    ast.comprehension,
                ))
            )
            return float(count + 1)
        except SyntaxError:
            return 1.0

    @staticmethod
    def metrics_from_pytest_result(
        pytest_data: dict,
        execution_time_ms: float = 0.0,
        peak_memory_mb: float = 0.0,
    ) -> QAMetrics:
        """
        Build a QAMetrics from the data dict returned by
        shared.tools.run_pytest_with_coverage().

        Extracts pass/fail counts from the junit data if present,
        otherwise falls back to inferring from returncode.
        """
        junit = pytest_data.get("junit", {})
        tests_passed = int(junit.get("tests_passed", 0))
        tests_failed = int(junit.get("tests_failed", 0))
        tests_total = int(junit.get("tests_total", tests_passed + tests_failed))

        # If no junit counts, infer from returncode
        if tests_total == 0:
            if pytest_data.get("returncode", -1) == 0:
                tests_passed = 1
                tests_total = 1
            else:
                tests_failed = 1
                tests_total = 1

        cov_data = pytest_data.get("coverage") or {}
        coverage_pct = float(cov_data.get("percent", 0.0))
        coverage_frac = coverage_pct / 100.0 if coverage_pct > 1.0 else coverage_pct

        return QAMetrics(
            tests_passed=tests_passed,
            tests_failed=tests_failed,
            tests_total=tests_total,
            coverage=coverage_frac,
            execution_time_ms=execution_time_ms,
            peak_memory_mb=peak_memory_mb,
            error_output=pytest_data.get("stderr", ""),
        )
