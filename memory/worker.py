"""memory-worker entry point.

Consumes Kafka topics:
  - memory.events   → store in EpisodicMemory / FailureMemory
  - qa.results      → update solution records
  - reward.computed → persist reward scores

Also serves:
  - GET /health     on METRICS_PORT (default 9091) → {"status": "ok", ...}
  - Prometheus metrics on the same port via prometheus_client
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

LOGGER = logging.getLogger(__name__)

KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP", "kafka:9092")
METRICS_PORT = int(os.getenv("METRICS_PORT", "9091"))
CONSUMER_GROUP = "memory-worker"

TOPICS = ["memory.events", "qa.results", "reward.computed"]

# ---------------------------------------------------------------------------
# Prometheus metrics (optional)
# ---------------------------------------------------------------------------

try:
    from prometheus_client import Counter, start_http_server as _prom_start

    _PROM_AVAILABLE = True
    MESSAGES_CONSUMED = Counter(
        "memory_worker_messages_total",
        "Total Kafka messages consumed",
        ["topic"],
    )
    ERRORS = Counter(
        "memory_worker_errors_total",
        "Total processing errors",
        ["topic"],
    )
except ImportError:
    _PROM_AVAILABLE = False


def _start_metrics_server() -> None:
    if _PROM_AVAILABLE:
        _prom_start(METRICS_PORT)
        LOGGER.info("[memory-worker] Prometheus metrics on port %d", METRICS_PORT)
    else:
        _start_health_only_server()


# ---------------------------------------------------------------------------
# Simple /health HTTP server (used when prometheus_client is absent)
# ---------------------------------------------------------------------------

class _HealthHandler(BaseHTTPRequestHandler):
    """Minimal HTTP handler serving only GET /health."""

    def do_GET(self):  # noqa: N802
        if self.path == "/health":
            body = json.dumps({"status": "ok", "service": "memory-worker"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):  # suppress default access log noise
        pass


def _start_health_only_server() -> None:
    server = HTTPServer(("0.0.0.0", METRICS_PORT), _HealthHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    LOGGER.info("[memory-worker] Health endpoint on port %d/health", METRICS_PORT)


# ---------------------------------------------------------------------------
# Kafka consumer
# ---------------------------------------------------------------------------

async def _handle_message(topic: str, value: dict) -> None:
    """Route a single Kafka message to the appropriate handler."""
    if topic == "memory.events":
        await _handle_memory_event(value)
    elif topic == "qa.results":
        await _handle_qa_result(value)
    elif topic == "reward.computed":
        await _handle_reward_computed(value)


async def _handle_memory_event(event: dict) -> None:
    event_type = event.get("type", "unknown")
    LOGGER.debug("[memory-worker] memory.event type=%s", event_type)
    # Delegate to EpisodicMemory / FailureMemory based on event_type
    # (full implementation wired when DB is available)


async def _handle_qa_result(result: dict) -> None:
    LOGGER.debug("[memory-worker] qa.result episode=%s", result.get("episode_id"))


async def _handle_reward_computed(reward: dict) -> None:
    LOGGER.debug(
        "[memory-worker] reward.computed episode=%s reward=%.4f",
        reward.get("episode_id"),
        float(reward.get("reward", 0.0)),
    )


async def _consume_loop() -> None:
    """Main Kafka consume loop (requires confluent-kafka)."""
    try:
        from confluent_kafka import Consumer, KafkaException  # type: ignore
    except ImportError:
        LOGGER.warning(
            "[memory-worker] confluent-kafka not installed — consumer loop disabled"
        )
        return

    consumer = Consumer(
        {
            "bootstrap.servers": KAFKA_BOOTSTRAP,
            "group.id": CONSUMER_GROUP,
            "auto.offset.reset": "earliest",
            "enable.auto.commit": True,
        }
    )
    consumer.subscribe(TOPICS)
    LOGGER.info(
        "[memory-worker] Subscribed to topics: %s via %s", TOPICS, KAFKA_BOOTSTRAP
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
                LOGGER.warning("[memory-worker] Kafka error: %s", msg.error())
                if _PROM_AVAILABLE:
                    ERRORS.labels(topic=msg.topic() or "unknown").inc()
                continue
            try:
                value = json.loads(msg.value().decode("utf-8"))
                await _handle_message(msg.topic(), value)
                if _PROM_AVAILABLE:
                    MESSAGES_CONSUMED.labels(topic=msg.topic()).inc()
            except Exception as exc:
                LOGGER.exception("[memory-worker] Failed to process message: %s", exc)
                if _PROM_AVAILABLE:
                    ERRORS.labels(topic=msg.topic()).inc()
    finally:
        consumer.close()
        LOGGER.info("[memory-worker] Consumer closed")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    _start_metrics_server()
    LOGGER.info("[memory-worker] Starting")
    asyncio.run(_consume_loop())


if __name__ == "__main__":
    main()
