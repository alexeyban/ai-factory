import time
import uuid
import subprocess
from shared.messaging.kafka_producer import KafkaEventProducer
from shared.messaging.kafka_consumer import KafkaEventConsumer

orchestrator_producer = KafkaEventProducer(
    "shared/messaging/schemas/orchestrator_event.avsc"
)
consumer = KafkaEventConsumer("qa.tasks", "qa")

while True:
    event = consumer.poll()
    if not event:
        continue

    result = subprocess.run(["pytest", "/workspace"], capture_output=True)

    orchestrator_producer.send(
        "orchestrator.events",
        {
            "event_id": str(uuid.uuid4()),
            "task_id": event.get("task_id"),
            "stage": "qa_done",
            "timestamp": int(time.time() * 1000),
            "decision": "continue" if result.returncode == 0 else "retry",
            "status": "success" if result.returncode == 0 else "fail",
            "logs": result.stdout.decode(),
        },
    )
