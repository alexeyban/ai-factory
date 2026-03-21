from shared.kafka import create_consumer, create_producer
from shared.llm import call_llm
import json

consumer = create_consumer("architect.tasks", "architect")
producer = create_producer()

for msg in consumer:
    task = msg.value

    prompt = f"Break into tasks: {task['description']}"
    output = call_llm("Architect", prompt)

    tasks = json.loads(output)

    producer.send("orchestrator.events", {
        "stage": "architect_done",
        "tasks": tasks
    })