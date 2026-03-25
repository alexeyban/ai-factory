"""reward-worker entry point.

Consumes:  qa.results
Computes:  reward via RewardEngine
Publishes: reward.computed

Message contract
----------------
qa.results message (input):
{
  "episode_id": "...",
  "task_id": "...",
  "iteration": 1,
  "tests_passed": 3,
  "tests_failed": 1,
  "tests_total": 4,
  "execution_time_ms": 120.0,
  "peak_memory_mb": 32.0,
  "code": "def foo(): ..."
}

reward.computed message (output):
{
  "episode_id": "...",
  "task_id": "...",
  "iteration": 1,
  "reward": 0.87
}
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal

from memory.reward import QAMetrics, RewardEngine

LOGGER = logging.getLogger(__name__)

KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP", "kafka:9092")
INPUT_TOPIC = "qa.results"
OUTPUT_TOPIC = "reward.computed"
CONSUMER_GROUP = "reward-worker"

_engine = RewardEngine()


# ---------------------------------------------------------------------------
# Message processing
# ---------------------------------------------------------------------------

def _qa_result_to_metrics(msg: dict) -> QAMetrics:
    return QAMetrics(
        tests_passed=int(msg.get("tests_passed", 0)),
        tests_failed=int(msg.get("tests_failed", 0)),
        tests_total=int(msg.get("tests_total", 0)),
        execution_time_ms=float(msg.get("execution_time_ms", 0.0)),
        peak_memory_mb=float(msg.get("peak_memory_mb", 0.0)),
    )


def _compute_and_publish(msg: dict, producer) -> None:
    metrics = _qa_result_to_metrics(msg)
    code = msg.get("code", "")
    reward = _engine.compute(metrics, code)

    output = {
        "episode_id": msg.get("episode_id"),
        "task_id": msg.get("task_id"),
        "iteration": msg.get("iteration", 0),
        "reward": reward,
    }
    producer.produce(
        OUTPUT_TOPIC,
        key=str(msg.get("episode_id", "")),
        value=json.dumps(output).encode("utf-8"),
    )
    producer.flush()
    LOGGER.info(
        "[reward-worker] episode=%s task=%s reward=%.4f",
        output["episode_id"],
        output["task_id"],
        reward,
    )


# ---------------------------------------------------------------------------
# Kafka consume loop
# ---------------------------------------------------------------------------

async def _consume_loop() -> None:
    try:
        from confluent_kafka import Consumer, Producer, KafkaException  # type: ignore
    except ImportError:
        LOGGER.warning(
            "[reward-worker] confluent-kafka not installed — consumer loop disabled"
        )
        return

    consumer = Consumer(
        {
            "bootstrap.servers": KAFKA_BOOTSTRAP,
            "group.id": CONSUMER_GROUP,
            "auto.offset.reset": "earliest",
        }
    )
    producer = Producer({"bootstrap.servers": KAFKA_BOOTSTRAP})
    consumer.subscribe([INPUT_TOPIC])
    LOGGER.info(
        "[reward-worker] Subscribed to %s via %s", INPUT_TOPIC, KAFKA_BOOTSTRAP
    )

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
                LOGGER.warning("[reward-worker] Kafka error: %s", msg.error())
                continue
            try:
                value = json.loads(msg.value().decode("utf-8"))
                _compute_and_publish(value, producer)
            except Exception as exc:
                LOGGER.exception("[reward-worker] Processing failed: %s", exc)
    finally:
        consumer.close()
        LOGGER.info("[reward-worker] Stopped")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    LOGGER.info("[reward-worker] Starting")
    asyncio.run(_consume_loop())


if __name__ == "__main__":
    main()
