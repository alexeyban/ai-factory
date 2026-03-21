import time
import uuid
from shared.messaging.kafka_producer import KafkaEventProducer
from shared.messaging.kafka_consumer import KafkaEventConsumer
from shared.llm import call_llm

orchestrator_producer = KafkaEventProducer(
    "shared/messaging/schemas/orchestrator_event.avsc"
)
consumer = KafkaEventConsumer("dev.tasks", "dev-group")

while True:
    event = consumer.poll()
    if not event:
        continue

    task_id = event.get("task_id")

    code = call_llm("Senior dev", f"Implement {event}")

    file_path = f"/workspace/{task_id}.py"
    with open(file_path, "w") as f:
        f.write(code)

    orchestrator_producer.send(
        "orchestrator.events",
        {
            "event_id": str(uuid.uuid4()),
            "task_id": task_id,
            "stage": "dev_done",
            "timestamp": int(time.time() * 1000),
            "decision": "continue",
            "artifact": file_path,
        },
    )
