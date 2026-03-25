"""Prometheus metrics exporter for AI Factory benchmarks.

Exposes learning progress metrics via an HTTP endpoint that Prometheus scrapes.
Start with MetricsExporter(port=8080).start() before emitting metrics.
"""
from __future__ import annotations

import logging

LOGGER = logging.getLogger(__name__)

try:
    from prometheus_client import Counter, Gauge, Histogram, start_http_server

    _PROMETHEUS_AVAILABLE = True
except ImportError:  # pragma: no cover
    _PROMETHEUS_AVAILABLE = False
    LOGGER.warning(
        "[metrics_exporter] prometheus_client not installed — metrics disabled"
    )

if _PROMETHEUS_AVAILABLE:
    TASK_ATTEMPTS = Counter(
        "ai_factory_task_attempts_total",
        "Total task attempts",
        ["difficulty"],
    )
    TASK_SUCCESSES = Counter(
        "ai_factory_task_successes_total",
        "Successful task completions",
        ["difficulty"],
    )
    AVG_REWARD = Gauge(
        "ai_factory_avg_reward",
        "Average reward score for the last task",
        ["difficulty"],
    )
    SKILL_COUNT = Gauge(
        "ai_factory_skill_count",
        "Total number of active skills",
    )
    EXPLORATION_RATE = Gauge(
        "ai_factory_exploration_rate",
        "Current exploration rate (epsilon)",
    )
    EPISODE_COUNT = Counter(
        "ai_factory_episodes_total",
        "Total completed learning episodes",
    )
    REWARD_HISTOGRAM = Histogram(
        "ai_factory_reward_histogram",
        "Distribution of reward scores",
        ["difficulty"],
        buckets=[0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0],
    )


class MetricsExporter:
    """Wraps prometheus_client metrics for AI Factory learning progress."""

    def __init__(self, port: int = 8080) -> None:
        self._port = port
        self._available = _PROMETHEUS_AVAILABLE

    def start(self) -> None:
        """Start the HTTP metrics server."""
        if not self._available:
            LOGGER.warning("[metrics_exporter] Skipping start — prometheus_client missing")
            return
        start_http_server(self._port)
        LOGGER.info("[metrics_exporter] Metrics server started on port %d", self._port)

    def record_task_result(
        self, difficulty: str, success: bool, reward: float
    ) -> None:
        """Record the outcome of a single task attempt."""
        if not self._available:
            return
        TASK_ATTEMPTS.labels(difficulty=difficulty).inc()
        if success:
            TASK_SUCCESSES.labels(difficulty=difficulty).inc()
        AVG_REWARD.labels(difficulty=difficulty).set(reward)
        REWARD_HISTOGRAM.labels(difficulty=difficulty).observe(reward)

    def record_episode(self) -> None:
        """Increment the episode counter."""
        if not self._available:
            return
        EPISODE_COUNT.inc()

    def update_skill_count(self, count: int) -> None:
        if not self._available:
            return
        SKILL_COUNT.set(count)

    def update_exploration_rate(self, rate: float) -> None:
        if not self._available:
            return
        EXPLORATION_RATE.set(rate)
