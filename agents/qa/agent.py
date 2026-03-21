import time
import uuid
import subprocess
import json
from shared.messaging.kafka_producer import KafkaEventProducer
from shared.messaging.kafka_consumer import KafkaEventConsumer
from shared.llm import call_llm
from shared.prompts.loader import load_prompt, render_prompt

orchestrator_producer = KafkaEventProducer(
    "shared/messaging/schemas/orchestrator_event.avsc"
)
consumer = KafkaEventConsumer("qa.tasks", "qa")
SYSTEM_PROMPT = load_prompt("qa", "system")
USER_PROMPT = load_prompt("qa", "user")

while True:
    event = consumer.poll()
    if not event:
        continue

    result = subprocess.run(["pytest", "/workspace"], capture_output=True, text=True)
    logs = result.stdout + result.stderr
    summary_raw = call_llm(
        SYSTEM_PROMPT,
        render_prompt(
            USER_PROMPT,
            test_logs=logs,
            task_description=event.get("description", ""),
        ),
    )

    try:
        summary = json.loads(summary_raw)
    except json.JSONDecodeError:
        summary = {
            "status": "success" if result.returncode == 0 else "fail",
            "failing_tests": [],
            "error_summary": summary_raw,
            "root_cause": "",
            "fix_suggestion": "",
        }

    orchestrator_producer.send(
        "orchestrator.events",
        {
            "event_id": str(uuid.uuid4()),
            "task_id": event.get("task_id"),
            "stage": "qa_done",
            "timestamp": int(time.time() * 1000),
            "decision": "continue" if result.returncode == 0 else "retry",
            "status": "success" if result.returncode == 0 else "fail",
            "logs": logs,
            "summary": summary,
        },
    )
