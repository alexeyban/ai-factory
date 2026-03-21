kafka-topics --create --topic dev.tasks --bootstrap-server kafka:9092
kafka-topics --create --topic dev.retry.1 --bootstrap-server kafka:9092
kafka-topics --create --topic dev.retry.2 --bootstrap-server kafka:9092
kafka-topics --create --topic dev.retry.3 --bootstrap-server kafka:9092
kafka-topics --create --topic dev.dlq --bootstrap-server kafka:9092