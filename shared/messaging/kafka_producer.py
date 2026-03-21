from confluent_kafka.avro import AvroProducer
from confluent_kafka.avro.load import load
from .config import KAFKA_BOOTSTRAP, SCHEMA_REGISTRY_URL


class KafkaEventProducer:
    def __init__(self, schema_path):
        self.default_schema = load(schema_path)
        self._producer = AvroProducer(
            {
                "bootstrap.servers": KAFKA_BOOTSTRAP,
                "schema.registry.url": SCHEMA_REGISTRY_URL,
            },
            default_value_schema=self.default_schema,
        )

    def send(self, topic, value, schema_path=None):
        if schema_path:
            schema = load(schema_path)
            self._producer.produce(topic=topic, value=value, value_schema=schema)
        else:
            self._producer.produce(topic=topic, value=value)
        self._producer.flush()
