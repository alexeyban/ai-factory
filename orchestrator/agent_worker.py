"""Per-type Temporal agent worker.

Set AGENT_TYPE to one of: dev, qa, refactor, setup, docs
The worker registers only the relevant activity on its dedicated task queue.
"""
import asyncio
import logging
import os

from temporalio.client import Client
from temporalio.worker import Worker

from orchestrator.activities import (
    dev_task,
    docs_task,
    qa_task,
    refactor_task,
    setup_task,
)

logging.basicConfig(level=logging.INFO)

# Maps AGENT_TYPE → (default task queue, [activity functions])
AGENT_CONFIGS: dict[str, tuple[str, list]] = {
    "dev": ("dev-agent-tasks", [dev_task]),
    "qa": ("qa-agent-tasks", [qa_task]),
    "refactor": ("refactor-agent-tasks", [refactor_task]),
    "setup": ("setup-agent-tasks", [setup_task]),
    "docs": ("docs-agent-tasks", [docs_task]),
}


async def run_agent_worker() -> None:
    agent_type = os.getenv("AGENT_TYPE", "dev").lower()
    if agent_type not in AGENT_CONFIGS:
        raise ValueError(
            f"Unknown AGENT_TYPE={agent_type!r}. Must be one of: {sorted(AGENT_CONFIGS)}"
        )

    default_queue, activities = AGENT_CONFIGS[agent_type]
    task_queue = os.getenv("TASK_QUEUE", default_queue)
    temporal_address = os.getenv("TEMPORAL_ADDRESS", "temporal:7233")
    temporal_namespace = os.getenv("TEMPORAL_NAMESPACE", "default")

    logging.info(f"[{agent_type}] Connecting to Temporal at {temporal_address}")

    client: Client | None = None
    for attempt in range(5):
        try:
            client = await Client.connect(temporal_address, namespace=temporal_namespace)
            logging.info(f"[{agent_type}] Connected to namespace {temporal_namespace!r}")
            break
        except Exception as exc:
            logging.warning(f"[{agent_type}] Attempt {attempt + 1}/5 failed: {exc}")
            if attempt < 4:
                await asyncio.sleep(5)
            else:
                raise

    if client is None:
        raise RuntimeError("Temporal client was not initialized")

    worker = Worker(client, task_queue=task_queue, activities=activities)
    logging.info(f"[{agent_type}] Worker started on queue {task_queue!r}")
    await worker.run()


async def main() -> None:
    await run_agent_worker()


if __name__ == "__main__":
    asyncio.run(main())
