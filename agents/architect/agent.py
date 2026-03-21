import time
import uuid
import json
from shared.messaging.kafka_producer import KafkaEventProducer
from shared.messaging.kafka_consumer import KafkaEventConsumer
from shared.llm import call_llm

orchestrator_producer = KafkaEventProducer(
    "shared/messaging/schemas/orchestrator_event.avsc"
)
consumer = KafkaEventConsumer("architect.tasks", "architect")

while True:
    event = consumer.poll()
    if not event:
        continue

    prompt = f"Break into tasks: {event.get('description')}"
    output = call_llm("Architect", prompt)

    try:
        tasks = json.loads(output)
    except json.JSONDecodeError:
        tasks = [{"task_id": str(uuid.uuid4()), "description": output}]

    orchestrator_producer.send(
        "orchestrator.events",
        {
            "event_id": str(uuid.uuid4()),
            "task_id": event.get("task_id"),
            "stage": "architect_done",
            "timestamp": int(time.time() * 1000),
            "decision": "continue",
            "tasks": tasks,
        },
    )
