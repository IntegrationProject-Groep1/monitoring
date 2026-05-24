import logging
import os
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import pika


class RabbitMQLogHandler(logging.Handler):
    """Forward log records to the RabbitMQ 'logs' queue as contract-compliant XML.

    Reads broker credentials from the same env vars as the rest of the detector
    (RABBITMQ_HOST, RABBITMQ_PORT, RABBITMQMONITORING_USER, RABBITMQMONITORING_PASS,
    RABBITMQ_VHOST). Each emit opens a short-lived connection; failures print an
    OFFLINE fallback so no log is silently lost.
    """

    _VALID_ACTIONS = {
        "registration", "user", "payment", "invoice", "session", "calendar",
        "email", "wallet", "refund", "identity", "xml_validation", "system_error", "badge",
    }

    def __init__(self, source_system: str = "monitoring"):
        super().__init__()
        self.source_system = source_system
        self._recursion_guard = False

    def emit(self, record: logging.LogRecord) -> None:
        if self._recursion_guard:
            return
        if record.name.startswith("pika") or record.name.startswith("urllib3"):
            return

        self._recursion_guard = True
        level = message = action = "system_error"
        try:
            if record.levelno >= logging.ERROR:
                level = "error"
            elif record.levelno >= logging.WARNING:
                level = "warning"
            else:
                level = "info"

            action = getattr(record, "action", "system_error")
            if action not in self._VALID_ACTIONS:
                action = "system_error"

            message = self.format(record)

            root = ET.Element("message")
            header = ET.SubElement(root, "header")
            ET.SubElement(header, "message_id").text = str(uuid.uuid4())
            ET.SubElement(header, "timestamp").text = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            ET.SubElement(header, "source").text = self.source_system
            ET.SubElement(header, "type").text = "log"
            ET.SubElement(header, "version").text = "2.0"
            body_el = ET.SubElement(root, "body")
            ET.SubElement(body_el, "level").text = level
            ET.SubElement(body_el, "action").text = action
            ET.SubElement(body_el, "message").text = message

            xml_bytes = ET.tostring(root, encoding="utf-8")

            credentials = pika.PlainCredentials(
                os.environ.get("RABBITMQMONITORING_USER", "guest"),
                os.environ.get("RABBITMQMONITORING_PASS", "guest"),
            )
            parameters = pika.ConnectionParameters(
                host=os.environ.get("RABBITMQ_HOST", "localhost"),
                port=int(os.environ.get("RABBITMQ_PORT", "5672")),
                virtual_host=os.environ.get("RABBITMQ_VHOST", "/"),
                credentials=credentials,
                connection_attempts=1,
                retry_delay=1,
            )

            connection = pika.BlockingConnection(parameters)
            try:
                channel = connection.channel()
                channel.queue_declare(queue="logs", durable=True)
                channel.basic_publish(
                    exchange="",
                    routing_key="logs",
                    body=xml_bytes,
                    properties=pika.BasicProperties(content_type="application/xml", delivery_mode=2),
                )
                print(f"[Monitoring] Sent log [action={action}, level={level}]: {message}", flush=True)
            finally:
                connection.close()
        except Exception:
            print(f"[Monitoring] OFFLINE [{level.upper()}] [action={action}]: {message}", flush=True)
        finally:
            self._recursion_guard = False
