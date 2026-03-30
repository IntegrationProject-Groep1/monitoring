"""
Test producer for the heartbeat pipeline.

Sends a set of heartbeat XML messages to the 'heartbeat' RabbitMQ queue,
covering all code paths in the Logstash pipeline:
  - One valid message per known system (should land in heartbeats-*)
  - One message with an unknown system name (should land in quarantine)
  - One invalid XML message (should land in quarantine)

Exits 0 if all messages are published successfully, non-zero on any error.
"""

import os
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import pika

RABBITMQ_HOST = os.environ.get("RABBITMQ_HOST", "localhost")
RABBITMQ_PORT = int(os.environ.get("RABBITMQ_PORT", "5672"))
QUEUE = "heartbeat"

KNOWN_SYSTEMS = ["planning", "crm", "kassa", "facturatie", "monitoring"]


def build_heartbeat(system: str, uptime: int) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    root = ET.Element("heartbeat")
    ET.SubElement(root, "system").text = system
    ET.SubElement(root, "timestamp").text = timestamp
    ET.SubElement(root, "uptime").text = str(uptime)
    return ET.tostring(root, encoding="unicode")


def connect(retries: int = 10) -> tuple:
    credentials = pika.PlainCredentials("guest", "guest")
    for attempt in range(1, retries + 1):
        try:
            connection = pika.BlockingConnection(
                pika.ConnectionParameters(host=RABBITMQ_HOST, port=RABBITMQ_PORT, credentials=credentials)
            )
            channel = connection.channel()
            channel.queue_declare(queue=QUEUE, durable=True)
            print(f"Connected to RabbitMQ at {RABBITMQ_HOST}:{RABBITMQ_PORT}")
            return connection, channel
        except pika.exceptions.AMQPConnectionError:
            print(f"Connection attempt {attempt}/{retries} failed, retrying in 2s...")
            time.sleep(2)
    print("ERROR: Could not connect to RabbitMQ after all retries.", file=sys.stderr)
    sys.exit(1)


def publish(channel, body: str, label: str) -> None:
    channel.basic_publish(
        exchange="",
        routing_key=QUEUE,
        body=body,
        properties=pika.BasicProperties(delivery_mode=2),
    )
    print(f"  [sent] {label}")


def main() -> None:
    connection, channel = connect()

    print("\nSending valid heartbeats for all known systems:")
    for i, system in enumerate(KNOWN_SYSTEMS, start=1):
        publish(channel, build_heartbeat(system, i * 10), f"system={system}")

    print("\nSending edge-case messages (should be quarantined by Logstash):")
    publish(channel, build_heartbeat("unknown-team", 1), "unknown system name")
    publish(channel, "this is not valid xml <<<", "invalid XML")

    connection.close()
    print(f"\nDone. {len(KNOWN_SYSTEMS) + 2} messages published to queue '{QUEUE}'.")


if __name__ == "__main__":
    main()
