from shared.kafka import create_consumer, create_producer
import subprocess

consumer = create_consumer("qa.tasks", "qa")
producer = create_producer()

for msg in consumer:
    task = msg.value

    result = subprocess.run(["pytest", "/workspace"], capture_output=True)

    producer.send("orchestrator.events", {
        "stage": "qa_done",
        "task": task,
        "status": "success" if result.returncode == 0 else "fail",
        "logs": result.stdout.decode()
    })