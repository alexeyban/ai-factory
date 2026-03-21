import json
import time
import uuid

from shared.llm import call_llm
from shared.messaging.kafka_consumer import KafkaEventConsumer
from shared.messaging.kafka_producer import KafkaEventProducer
from shared.prompts.loader import load_prompt, render_prompt

orchestrator_producer = KafkaEventProducer(
    "shared/messaging/schemas/orchestrator_event.avsc"
)
consumer = KafkaEventConsumer("pm.tasks", "pm")
ARCHITECT_SYSTEM_PROMPT = load_prompt("architect", "system")
ANALYST_SYSTEM_PROMPT = load_prompt("analyst", "system")
PM_SYSTEM_PROMPT = load_prompt("pm", "system")
PM_USER_PROMPT = load_prompt("pm", "user")

while True:
    event = consumer.poll()
    if not event:
        continue

    description = event.get("description", "")
    architect_input = call_llm(
        ARCHITECT_SYSTEM_PROMPT,
        render_prompt(
            load_prompt("architect", "user"),
            project_description=description,
        ),
    )
    analyst_input = call_llm(
        ANALYST_SYSTEM_PROMPT,
        render_prompt(
            load_prompt("analyst", "user"),
            current_state="",
            event=description,
        ),
    )
    output = call_llm(
        PM_SYSTEM_PROMPT,
        render_prompt(
            PM_USER_PROMPT,
            task_description=description,
            architect_input=architect_input,
            analyst_input=analyst_input,
        ),
    )

    try:
        plan = json.loads(output)
    except json.JSONDecodeError:
        plan = {
            "project_goal": description,
            "delivery_summary": output,
            "architect_guidance": [architect_input],
            "analyst_guidance": [analyst_input],
            "execution_plan": [
                {
                    "task_id": str(uuid.uuid4()),
                    "title": "Implement requested work",
                    "description": description or output,
                    "assigned_agent": "dev",
                    "dependencies": [],
                    "acceptance_criteria": ["Deliver the requested implementation"],
                }
            ],
        }

    orchestrator_producer.send(
        "orchestrator.events",
        {
            "event_id": str(uuid.uuid4()),
            "task_id": event.get("task_id", str(uuid.uuid4())),
            "stage": "pm_done",
            "timestamp": int(time.time() * 1000),
            "decision": "continue",
            "project_goal": plan.get("project_goal", description),
            "delivery_summary": plan.get("delivery_summary", ""),
            "architect_guidance": plan.get("architect_guidance", []),
            "analyst_guidance": plan.get("analyst_guidance", []),
            "execution_plan": plan.get("execution_plan", []),
        },
    )
