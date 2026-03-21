from confluent_kafka.avro import AvroConsumer
from .config import KAFKA_BOOTSTRAP, SCHEMA_REGISTRY_URL

class KafkaEventConsumer:

    def __init__(self, topic, group_id):
        self.consumer = AvroConsumer({
            "bootstrap.servers": KAFKA_BOOTSTRAP,
            "schema.registry.url": SCHEMA_REGISTRY_URL,
            "group.id": group_id,
            "auto.offset.reset": "earliest"
        })

        self.consumer.subscribe([topic])

    def poll(self):
        msg = self.consumer.poll(1.0)
        if msg is None:
            return None
        return msg.value()