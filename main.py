import uuid
from shared.messaging.kafka_producer import KafkaEventProducer
from shared.messaging.kafka_consumer import KafkaEventConsumer

task_producer = KafkaEventProducer("shared/messaging/schemas/task.avsc")
orchestrator_consumer = KafkaEventConsumer("orchestrator.events", "orchestrator")


def start_project():
    task_producer.send(
        "architect.tasks",
        {
            "task_id": str(uuid.uuid4()),
            "description": "Build REST API for todo app",
        },
    )


def run():
    start_project()

    pending_tasks = {}

    while True:
        event = orchestrator_consumer.poll()
        if not event:
            continue

        stage = event.get("stage")

        if stage == "architect_done":
            for task in event.get("tasks", []):
                task_producer.send("dev.tasks", task)
                pending_tasks[task["task_id"]] = task

        elif stage == "dev_done":
            task_id = event.get("task_id")
            task = pending_tasks.get(task_id, {})
            task["artifact"] = event.get("artifact")
            task_producer.send("qa.tasks", task)

        elif stage == "qa_done":
            task_id = event.get("task_id")
            task = pending_tasks.get(task_id, {})
            task["status"] = event.get("status")
            task["logs"] = event.get("logs")
            task_producer.send("analyst.events", task)

        elif stage == "analysis_done":
            print("Cycle complete")


if __name__ == "__main__":
    run()
