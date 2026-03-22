from __future__ import annotations

from shared.messaging.kafka_consumer import KafkaEventConsumer
from shared.messaging.kafka_producer import KafkaEventProducer
from shared.standalone_dispatcher import build_task_message, dispatch_topic, process_event

consumer = KafkaEventConsumer('orchestrator.events', 'standalone-dispatcher')
producer = KafkaEventProducer('shared/messaging/schemas/task.avsc')

while True:
    event = consumer.poll()
    if not event:
        continue
    tasks = process_event(event)
    if not tasks:
        continue
    for task in tasks:
        topic = dispatch_topic(task.get('assigned_agent'))
        if topic is None:
            continue
        producer.send(topic, build_task_message(task, {'plan_id': task.get('_root_plan_id', ''), 'artifact': event.get('artifact'), 'project_goal': None}))
        print(
            f"standalone_dispatcher: dispatched {task.get('task_id')} to {topic}",
            flush=True,
        )
