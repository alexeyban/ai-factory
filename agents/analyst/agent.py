import time
import uuid
from pathlib import Path
from shared.messaging.kafka_producer import KafkaEventProducer
from shared.messaging.kafka_consumer import KafkaEventConsumer
from shared.llm import call_llm
from shared.prompts.loader import load_prompt, render_prompt

orchestrator_producer = KafkaEventProducer(
    "shared/messaging/schemas/orchestrator_event.avsc"
)
consumer = KafkaEventConsumer("analyst.events", "analyst")

STATE_FILE = Path("/workspace/project_state.md")
SYSTEM_PROMPT = load_prompt("analyst", "system")
USER_PROMPT = load_prompt("analyst", "user")

while True:
    event = consumer.poll()
    if not event:
        continue

    state = ""
    if STATE_FILE.exists():
        state = STATE_FILE.read_text()

    prompt = render_prompt(
        USER_PROMPT,
        current_state=state,
        event=event,
    )
    new_state = call_llm(SYSTEM_PROMPT, prompt)

    STATE_FILE.write_text(new_state)

    orchestrator_producer.send(
        "orchestrator.events",
        {
            "event_id": str(uuid.uuid4()),
            "task_id": event.get("task_id"),
            "stage": "analysis_done",
            "timestamp": int(time.time() * 1000),
            "decision": "complete",
        },
    )
