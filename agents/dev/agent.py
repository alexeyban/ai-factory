from shared.messaging.kafka_producer import KafkaEventProducer
from shared.messaging.kafka_consumer import KafkaEventConsumer
from shared.llm import call_llm

producer = KafkaEventProducer("shared/messaging/schemas/dev_event.avsc")
consumer = KafkaEventConsumer("dev.tasks", "dev-group")

while True:
    event = consumer.poll()
    if not event:
        continue

    task_id = event["task_id"]

    code = call_llm("Senior dev", f"Implement {event}")

    file_path = f"/workspace/{task_id}.py"
    with open(file_path, "w") as f:
        f.write(code)

    producer.send("orchestrator.events", {
        "event_id": "...",
        "task_id": task_id,
        "stage": "dev_done",
        "artifact": file_path
    })