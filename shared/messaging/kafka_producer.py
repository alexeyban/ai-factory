from confluent_kafka.avro import AvroProducer
from confluent_kafka.avro.load import load
from .config import KAFKA_BOOTSTRAP, SCHEMA_REGISTRY_URL

class KafkaEventProducer:

    def __init__(self, schema_path):
        self.value_schema = load(schema_path)

        self.producer = AvroProducer(
            {
                "bootstrap.servers": KAFKA_BOOTSTRAP,
                "schema.registry.url": SCHEMA_REGISTRY_URL
            },
            default_value_schema=self.value_schema
        )

    def send(self, topic, value):
        self.producer.produce(topic=topic, value=value)
        self.producer.flush()