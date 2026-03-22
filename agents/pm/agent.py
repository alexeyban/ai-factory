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


def _trim_text(value: str, limit: int = 6000) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "\n...[truncated by pm_agent]"


while True:
    event = consumer.poll()
    if not event:
        continue

    print(f"pm_agent: received task {event.get('task_id')}", flush=True)

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
            task_description=_trim_text(description, limit=12000),
            architect_input=_trim_text(architect_input),
            analyst_input=_trim_text(analyst_input),
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

    if isinstance(plan, list):
        plan = {
            "project_goal": description,
            "delivery_summary": description,
            "architect_guidance": [architect_input],
            "analyst_guidance": [analyst_input],
            "execution_plan": plan,
        }

    execution_plan = plan.get("execution_plan", [])
    orchestrator_producer.send(
        "orchestrator.events",
        {
            "event_id": str(uuid.uuid4()),
            "task_id": event.get("task_id", str(uuid.uuid4())),
            "stage": "pm_done",
            "timestamp": int(time.time() * 1000),
            "decision": "continue",
            "reason": plan.get("delivery_summary", ""),
            "artifact": event.get("artifact"),
            "status": "planned",
            "logs": json.dumps(plan),
            "tasks": [
                {
                    "task_id": task.get("task_id", str(uuid.uuid4())),
                    "description": task.get("description", task.get("title", "")),
                }
                for task in execution_plan
            ],
        },
    )
    print(
        f"pm_agent: published plan for {event.get('task_id')} with {len(execution_plan)} tasks",
        flush=True,
    )
