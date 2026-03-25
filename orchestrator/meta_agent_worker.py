"""meta-agent-worker entry point.

Listens for skill optimisation triggers via Kafka (memory.events) and
periodically runs the SkillOptimizer cycle via a Temporal workflow signal
or direct invocation, depending on configuration.

Responsibilities:
  - Track episode count (from episode.events topic)
  - Trigger SkillOptimizer every SKILL_OPTIMIZE_EVERY_N episodes
  - Optionally signal the LearningWorkflow via Temporal SDK
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal

LOGGER = logging.getLogger(__name__)

KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP", "kafka:9092")
SKILL_OPTIMIZE_EVERY_N = int(os.getenv("SKILL_OPTIMIZE_EVERY_N", "10"))
CONSUMER_GROUP = "meta-agent"
TOPICS = ["episode.events", "memory.events"]

_episode_count = 0


# ---------------------------------------------------------------------------
# Message handling
# ---------------------------------------------------------------------------

async def _handle_episode_event(event: dict) -> None:
    global _episode_count
    event_type = event.get("type", "")
    if event_type in ("episode_completed", "episode_finished"):
        _episode_count += 1
        LOGGER.info(
            "[meta-agent] Episode count: %d (optimize every %d)",
            _episode_count,
            SKILL_OPTIMIZE_EVERY_N,
        )
        if _episode_count % SKILL_OPTIMIZE_EVERY_N == 0:
            await _trigger_optimization()


async def _trigger_optimization() -> None:
    """Fire off a SkillOptimizer cycle.

    In a full deployment this would signal a running LearningWorkflow via
    Temporal. Here we invoke the optimizer directly so the worker is useful
    without needing an active workflow.
    """
    LOGGER.info("[meta-agent] Triggering skill optimization (episode=%d)", _episode_count)
    try:
        from memory.db import MemoryDB
        from memory.vector_store import VectorMemory
        from memory.skill_optimizer import SkillOptimizer
        from skills import SkillRegistry

        db = MemoryDB()
        vm = VectorMemory()
        from shared.llm import call_llm

        def llm_fn(system, user):
            return call_llm(system, user)

        registry = SkillRegistry()
        optimizer = SkillOptimizer(
            db=db,
            vector_memory=vm,
            llm_fn=llm_fn,
            skill_registry=registry,
        )
        stats = await optimizer.run_optimization_cycle(_episode_count)
        LOGGER.info("[meta-agent] Optimization complete: %s", stats)
    except Exception as exc:
        LOGGER.warning("[meta-agent] Optimization failed: %s", exc)


# ---------------------------------------------------------------------------
# Kafka consume loop
# ---------------------------------------------------------------------------

async def _consume_loop() -> None:
    try:
        from confluent_kafka import Consumer  # type: ignore
    except ImportError:
        LOGGER.warning(
            "[meta-agent] confluent-kafka not installed — consumer loop disabled"
        )
        return

    consumer = Consumer(
        {
            "bootstrap.servers": KAFKA_BOOTSTRAP,
            "group.id": CONSUMER_GROUP,
            "auto.offset.reset": "earliest",
        }
    )
    consumer.subscribe(TOPICS)
    LOGGER.info("[meta-agent] Subscribed to %s via %s", TOPICS, KAFKA_BOOTSTRAP)

    loop = asyncio.get_event_loop()
    stop_event = asyncio.Event()

    def _handle_signal(*_):
        stop_event.set()

    loop.add_signal_handler(signal.SIGTERM, _handle_signal)
    loop.add_signal_handler(signal.SIGINT, _handle_signal)

    try:
        while not stop_event.is_set():
            msg = await loop.run_in_executor(None, consumer.poll, 1.0)
            if msg is None:
                continue
            if msg.error():
                LOGGER.warning("[meta-agent] Kafka error: %s", msg.error())
                continue
            try:
                value = json.loads(msg.value().decode("utf-8"))
                topic = msg.topic()
                if topic == "episode.events":
                    await _handle_episode_event(value)
            except Exception as exc:
                LOGGER.exception("[meta-agent] Message processing failed: %s", exc)
    finally:
        consumer.close()
        LOGGER.info("[meta-agent] Stopped")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    LOGGER.info("[meta-agent] Starting (optimize_every_n=%d)", SKILL_OPTIMIZE_EVERY_N)
    asyncio.run(_consume_loop())


if __name__ == "__main__":
    main()
