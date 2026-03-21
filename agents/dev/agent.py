import time
import uuid
from shared.messaging.kafka_producer import KafkaEventProducer
from shared.messaging.kafka_consumer import KafkaEventConsumer
from shared.llm import call_llm
from shared.prompts.loader import load_prompt, render_prompt

orchestrator_producer = KafkaEventProducer(
    "shared/messaging/schemas/orchestrator_event.avsc"
)
consumer = KafkaEventConsumer("dev.tasks", "dev-group")
SYSTEM_PROMPT = load_prompt("dev", "system")
USER_PROMPT = load_prompt("dev", "user")

while True:
    event = consumer.poll()
    if not event:
        continue

    task_id = event.get("task_id")

    prompt = render_prompt(
        USER_PROMPT,
        task_description=event.get("description", ""),
        task_context=event,
    )
    code = call_llm(SYSTEM_PROMPT, prompt)

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
