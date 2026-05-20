"""
Test producer for the heartbeat and platform-log pipelines.

Sends XML messages to the 'heartbeat' and 'logs' RabbitMQ queues, covering
all code paths in the Logstash pipeline (per contract v2.3):
  - heartbeats: one valid per heartbeat source + offline, plus edge cases
    (unknown system, unknown status, missing uptime, identity-service which
    is not in the heartbeat whitelist, malformed XML).
  - logs: one valid info per log source + unknown_action soft-tag, plus
    edge cases (unknown level, wrong type, unsupported version, monitoring
    source which is not in the log whitelist, malformed XML).

Exits 0 if all messages are published successfully, non-zero on any error.
"""

import os
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import pika

RABBITMQ_HOST = os.environ.get("RABBITMQ_HOST", "localhost")
RABBITMQ_PORT = int(os.environ.get("RABBITMQ_PORT", "5672"))
RABBITMQ_USER = os.environ["RABBITMQMONITORING_USER"]
RABBITMQ_PASS = os.environ["RABBITMQMONITORING_PASS"]
HEARTBEAT_QUEUE = "heartbeat"
LOGS_QUEUE = "logs"

HEARTBEAT_SOURCES = ["planning", "crm", "kassa", "facturatie", "monitoring", "frontend", "mailing", "iot_gateway"]
LOG_SOURCES = ["planning", "crm", "kassa", "facturatie", "frontend", "mailing", "identity-service", "iot_gateway"]


def _envelope(source: str, msg_type: str, version: str = "2.0") -> tuple[ET.Element, ET.Element, ET.Element]:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    message = ET.Element("message")
    header = ET.SubElement(message, "header")
    ET.SubElement(header, "message_id").text = str(uuid.uuid4())
    ET.SubElement(header, "timestamp").text = timestamp
    ET.SubElement(header, "source").text = source
    ET.SubElement(header, "type").text = msg_type
    ET.SubElement(header, "version").text = version
    body = ET.SubElement(message, "body")
    return message, header, body


def build_heartbeat(system: str, uptime: int | None, status: str = "online") -> str:
    message, _, body = _envelope(system, "heartbeat")
    ET.SubElement(body, "status").text = status
    if uptime is not None:
        ET.SubElement(body, "uptime").text = str(uptime)
    return ET.tostring(message, encoding="unicode")


def build_log(source: str, level: str, action: str, body_message: str, msg_type: str = "log", version: str = "2.0") -> str:
    message, _, body = _envelope(source, msg_type, version=version)
    ET.SubElement(body, "level").text = level
    ET.SubElement(body, "action").text = action
    ET.SubElement(body, "message").text = body_message
    return ET.tostring(message, encoding="unicode")


def connect(queue: str, retries: int = 10) -> tuple:
    credentials = pika.PlainCredentials(RABBITMQ_USER, RABBITMQ_PASS)
    for attempt in range(1, retries + 1):
        try:
            connection = pika.BlockingConnection(
                pika.ConnectionParameters(host=RABBITMQ_HOST, port=RABBITMQ_PORT, credentials=credentials)
            )
            channel = connection.channel()
            channel.queue_declare(queue=queue, durable=True)
            print(f"Connected to RabbitMQ at {RABBITMQ_HOST}:{RABBITMQ_PORT} (queue={queue})")
            return connection, channel
        except pika.exceptions.AMQPConnectionError:
            print(f"Connection attempt {attempt}/{retries} failed, retrying in 2s...")
            time.sleep(2)
    print("ERROR: Could not connect to RabbitMQ after all retries.", file=sys.stderr)
    sys.exit(1)


def publish(channel, queue: str, body: str, label: str) -> None:
    channel.basic_publish(
        exchange="",
        routing_key=queue,
        body=body,
        properties=pika.BasicProperties(delivery_mode=2),
    )
    print(f"  [sent] {label}")


def send_heartbeats() -> int:
    connection, channel = connect(HEARTBEAT_QUEUE)

    print("\nSending valid heartbeats for all heartbeat sources:")
    for i, system in enumerate(HEARTBEAT_SOURCES, start=1):
        publish(channel, HEARTBEAT_QUEUE, build_heartbeat(system, i * 10), f"system={system}")

    print("\nSending offline heartbeat (status=offline, uptime=0):")
    publish(channel, HEARTBEAT_QUEUE, build_heartbeat(HEARTBEAT_SOURCES[0], 0, "offline"), f"system={HEARTBEAT_SOURCES[0]} offline")

    print("\nSending edge-case heartbeats (should be quarantined by Logstash):")
    publish(channel, HEARTBEAT_QUEUE, build_heartbeat("unknown-team", 1), "unknown system name")
    publish(channel, HEARTBEAT_QUEUE, build_heartbeat("crm", 1, status="banana"), "status=banana (unknown_status)")
    publish(channel, HEARTBEAT_QUEUE, build_heartbeat("crm", None), "missing uptime (missing_uptime)")
    publish(channel, HEARTBEAT_QUEUE, build_heartbeat("identity-service", 1), "source=identity-service (unknown_system for heartbeats)")
    publish(channel, HEARTBEAT_QUEUE, "this is not valid xml <<<", "invalid XML")

    sent = len(HEARTBEAT_SOURCES) + 6
    connection.close()
    return sent


def send_logs() -> int:
    connection, channel = connect(LOGS_QUEUE)

    print("\nSending valid info logs for all log sources:")
    for source in LOG_SOURCES:
        publish(
            channel,
            LOGS_QUEUE,
            build_log(source, "info", "user", f"User flow completed for {source}"),
            f"source={source} level=info action=user",
        )

    print("\nSending unknown-action log (indexed but tagged 'unknown_action'):")
    publish(
        channel,
        LOGS_QUEUE,
        build_log("crm", "info", "totally_made_up_action", "Drift sample for monitoring"),
        "source=crm action=totally_made_up_action",
    )

    print("\nSending edge-case logs (should be quarantined by Logstash):")
    publish(
        channel,
        LOGS_QUEUE,
        build_log("crm", "panic", "user", "Bad level value"),
        "level=panic (unknown_level)",
    )
    publish(
        channel,
        LOGS_QUEUE,
        build_log("crm", "info", "user", "Type field is heartbeat, not log", msg_type="heartbeat"),
        "header.type=heartbeat (wrong_message_type)",
    )
    publish(
        channel,
        LOGS_QUEUE,
        build_log("crm", "info", "user", "Old contract version", version="1.0"),
        "version=1.0 (unsupported_contract_version)",
    )
    publish(
        channel,
        LOGS_QUEUE,
        build_log("monitoring", "info", "user", "Monitoring should not log to itself"),
        "source=monitoring (unknown_system for logs)",
    )
    publish(channel, LOGS_QUEUE, "this is not valid xml <<<", "invalid XML")

    sent = len(LOG_SOURCES) + 6
    connection.close()
    return sent


def main() -> None:
    hb = send_heartbeats()
    lg = send_logs()
    print(f"\nDone. {hb} heartbeats and {lg} logs published.")


if __name__ == "__main__":
    main()
