from shared.kafka import create_consumer, create_producer
from shared.llm import call_llm

consumer = create_consumer("analyst.events", "analyst")
producer = create_producer()

for msg in consumer:
    event = msg.value

    with open("/workspace/project_state.md", "r") as f:
        state = f.read()

    new_state = call_llm(
        "Analyst",
        f"Update state:\n{state}\nEvent:\n{event}"
    )

    with open("/workspace/project_state.md", "w") as f:
        f.write(new_state)

    producer.send("orchestrator.events", {
        "stage": "analysis_done"
    })