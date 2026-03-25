#!/bin/bash
# Create all Kafka topics required by AI Factory.
# Run after the Kafka container is healthy:
#   docker compose exec kafka /infra/kafka_topics.sh
# Or from the host:
#   bash infra/kafka_topics.sh

set -euo pipefail

KAFKA_CONTAINER="${KAFKA_CONTAINER:-kafka}"
KAFKA_BOOTSTRAP="${KAFKA_BOOTSTRAP:-localhost:9092}"
REPLICATION_FACTOR="${REPLICATION_FACTOR:-1}"

create_topic() {
    local topic="$1"
    local partitions="${2:-3}"
    local retention_ms="${3:-604800000}"   # 7 days default

    echo "Creating topic: $topic (partitions=$partitions, retention=${retention_ms}ms)"
    docker exec "$KAFKA_CONTAINER" kafka-topics.sh \
        --bootstrap-server "$KAFKA_BOOTSTRAP" \
        --create --if-not-exists \
        --topic "$topic" \
        --partitions "$partitions" \
        --replication-factor "$REPLICATION_FACTOR" \
        --config "retention.ms=$retention_ms"
}

echo "==> AI Factory Kafka topic setup"
echo "    Container : $KAFKA_CONTAINER"
echo "    Bootstrap : $KAFKA_BOOTSTRAP"
echo ""

# -----------------------------------------------------------------------
# Core pipeline topics
# -----------------------------------------------------------------------
#  Topic                  | Partitions | Retention
create_topic "task.contracts"    3 604800000    # 7d  — Orchestrator → Dev/QA
create_topic "episode.events"    3 2592000000   # 30d — All agents → Memory Worker
create_topic "qa.results"        3 604800000    # 7d  — QA Agent → Reward Worker
create_topic "skill.extracted"   3 2592000000   # 30d — Skill Extractor → VectorDB/Meta
create_topic "memory.events"     3 2592000000   # 30d — Memory Worker → Meta Agent
create_topic "reward.computed"   3 604800000    # 7d  — Reward Worker → Policy Updater

echo ""
echo "==> Done. Listing all topics:"
docker exec "$KAFKA_CONTAINER" kafka-topics.sh \
    --bootstrap-server "$KAFKA_BOOTSTRAP" \
    --list
