import asyncio
import os
import logging
from temporalio.client import Client
from temporalio.worker import Worker

from orchestrator.workflows import OrchestratorWorkflow, ProjectWorkflow
from orchestrator.activities import (
    architect_activity,
    dev_activity,
    qa_activity,
    analyst_activity,
    process_single_task,
    process_all_tasks,
)

logging.basicConfig(level=logging.INFO)


async def run_worker():
    temporal_address = os.getenv("TEMPORAL_ADDRESS", "temporal:7233")
    temporal_namespace = os.getenv("TEMPORAL_NAMESPACE", "default")
    task_queue = os.getenv("TASK_QUEUE", "ai-factory-tasks")

    logging.info(f"Connecting to Temporal at {temporal_address}")

    max_retries = 5
    retry_delay = 5

    for attempt in range(max_retries):
        try:
            client = await Client.connect(
                temporal_address,
                namespace=temporal_namespace,
            )
            logging.info(f"Connected to Temporal namespace: {temporal_namespace}")
            break
        except Exception as e:
            logging.warning(f"Attempt {attempt + 1}/{max_retries} failed: {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(retry_delay)
            else:
                raise

    logging.info(f"Starting worker on task queue: {task_queue}")

    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[OrchestratorWorkflow, ProjectWorkflow],
        activities=[
            architect_activity,
            dev_activity,
            qa_activity,
            analyst_activity,
            process_single_task,
            process_all_tasks,
        ],
    )

    logging.info("Worker started, waiting for tasks...")
    await worker.run()


async def main():
    await run_worker()


if __name__ == "__main__":
    asyncio.run(main())
