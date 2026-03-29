import socket
import time
import os
import sys
import pika
from datetime import datetime, timezone

SYSTEM_NAME = os.environ.get("SYSTEM_NAME")
TARGETS_RAW = os.environ.get("TARGETS")
RABBITMQ_HOST = os.environ.get("RABBITMQ_HOST")
RABBITMQ_USER = os.environ.get("RABBITMQ_USER")
RABBITMQ_PASS = os.environ.get("RABBITMQ_PASS")

if not all([SYSTEM_NAME, TARGETS_RAW, RABBITMQ_HOST, RABBITMQ_USER, RABBITMQ_PASS]):
    print("FOUT: stel alle environment variables in")
    sys.exit(1)

assert TARGETS_RAW is not None

try:
    TARGETS = [(t.strip().split(":")[0], int(t.strip().split(":")[1]))
               for t in TARGETS_RAW.split(",")]
except (ValueError, IndexError):
    print("FOUT: TARGETS heeft een ongeldig formaat. Verwacht: host:port[,host:port,...]")
    sys.exit(1)

uptime_seconds = 0


def is_alive(host, port, timeout=2):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        return True
    except (socket.timeout, ConnectionRefusedError, OSError):
        return False
    finally:
        sock.close()


def all_alive(targets):
    failed = [f"{host}:{port}" for host, port in targets if not is_alive(host, port)]
    if failed:
        print(f"[DOWN] {SYSTEM_NAME} niet bereikbaar: {', '.join(failed)}")
        return False
    return True


def build_heartbeat_xml(system_name, uptime):
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        f"<heartbeat>"
        f"<system>{system_name}</system>"
        f"<timestamp>{timestamp}</timestamp>"
        f"<uptime>{uptime}</uptime>"
        f"</heartbeat>"
    )


def connect_rabbitmq():
    credentials = pika.PlainCredentials(RABBITMQ_USER, RABBITMQ_PASS)
    while True:
        try:
            connection = pika.BlockingConnection(
                pika.ConnectionParameters(host=RABBITMQ_HOST, credentials=credentials)
            )
            channel = connection.channel()
            channel.queue_declare(queue="heartbeat", durable=True)
            print("Verbonden met RabbitMQ")
            return connection, channel
        except pika.exceptions.AMQPConnectionError:
            print("RabbitMQ niet bereikbaar, opnieuw proberen in 5 sec")
            time.sleep(5)


print(f"Sidecar gestart voor systeem: {SYSTEM_NAME}")
print(f"Controleert: {', '.join(f'{h}:{p}' for h, p in TARGETS)}")

connection, channel = connect_rabbitmq()

while True:
    if all_alive(TARGETS):
        uptime_seconds += 1
        xml = build_heartbeat_xml(SYSTEM_NAME, uptime_seconds)
        try:
            channel.basic_publish(
                exchange="",
                routing_key="heartbeat",
                body=xml,
                properties=pika.BasicProperties(delivery_mode=2)
            )
        except pika.exceptions.AMQPError:
            print("RabbitMQ verbinding verloren, opnieuw verbinden")
            try:
                connection.close()
            except Exception:
                pass
            connection, channel = connect_rabbitmq()
    else:
        uptime_seconds = 0

    time.sleep(1)
