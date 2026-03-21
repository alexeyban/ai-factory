from shared.kafka import create_producer, create_consumer
import uuid

producer = create_producer()
consumer = create_consumer("orchestrator.events", "orchestrator")

def start_project():
    project = {
        "task_id": str(uuid.uuid4()),
        "description": "Build REST API for todo app"
    }

    producer.send("architect.tasks", project)


def run():
    start_project()

    for msg in consumer:
        event = msg.value

        if event["stage"] == "architect_done":
            for task in event["tasks"]:
                producer.send("dev.tasks", task)

        elif event["stage"] == "dev_done":
            producer.send("qa.tasks", event["task"])

        elif event["stage"] == "qa_done":
            producer.send("analyst.events", event)

        elif event["stage"] == "analysis_done":
            print("Cycle complete")

if __name__ == "__main__":
    run()