import argparse
import base64
import json
import logging
import os
import queue
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import NamedTuple
import xml.etree.ElementTree as ET

import pika
from elasticsearch import Elasticsearch, NotFoundError
from jinja2 import Environment, FileSystemLoader, select_autoescape
from weasyprint import HTML

# ... (Configuraties remains the same until ES_HOST)
ES_HOST = os.getenv("ES_HOST", "http://elasticsearch:9200")
ES_USER = os.getenv("ES_ADMIN_USER", "elastic")
ES_PASS = os.getenv("ES_ADMIN_PASS")
RABBITMQ_HOST = os.environ["RABBITMQ_HOST"]
RABBITMQ_PORT = int(os.environ["RABBITMQ_PORT"])
RABBITMQ_USER = os.environ["RABBITMQMONITORING_USER"]
RABBITMQ_PASS = os.environ["RABBITMQMONITORING_PASS"]
RABBITMQ_VHOST = os.environ["RABBITMQ_VHOST"]
THRESHOLD_SECONDS = 3
COOLDOWN_MINUTES = 5
REPORT_QUEUE = os.getenv("REPORT_QUEUE", "monitoring.reports")
ALERTS_QUEUE = os.getenv("ALERTS_QUEUE", "monitoring.alerts")
REPORT_RECIPIENTS = os.getenv(
    "REPORT_RECIPIENTS",
    "admin1@example.com:admin-001:Platform:Admin,client@example.com:client-001:Event:Client",
)
REPORT_TEMPLATE_ID = os.getenv("REPORT_TEMPLATE_ID", "tmpl-monitoring-daily-report")
REPORT_CAMPAIGN_PREFIX = os.getenv("REPORT_CAMPAIGN_PREFIX", "monitoring-daily")
REPORT_SOURCE = os.getenv("REPORT_SOURCE", "monitoring")
REPORT_MAIL_TYPE = os.getenv("REPORT_MAIL_TYPE", "daily_report")
REPORT_TIME_HOUR = int(os.getenv("REPORT_TIME_HOUR", "6"))
REPORT_TIME_MINUTE = int(os.getenv("REPORT_TIME_MINUTE", "0"))

TEMPLATE_ENV = Environment(
    loader=FileSystemLoader(os.path.join(os.path.dirname(__file__), "templates")),
    autoescape=select_autoescape(["html"]),
)

EXPECTED_HEARTBEATS_PER_DAY = 86400
EXPECTED_HEARTBEATS_PER_MINUTE = 60

BUSINESS_ACTIONS = {
    "registration": "Registrations completed",
    "payment": "Payments processed",
    "invoice": "Invoices generated",
    "badge": "Badge scans at entry",
    "email": "Mailings sent",
}

KNOWN_SYSTEMS = {
    "planning",
    "crm",
    "kassa",
    "facturatie",
    "monitoring",
    "frontend",
    "mailing",
    "iot_gateway",
    "identity-service",
}

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("detector")

es = Elasticsearch([ES_HOST], basic_auth=(ES_USER, ES_PASS) if ES_PASS else None)
cooldown_list: dict[str, datetime] = {}

MAX_PUBLISH_RETRIES = 5

class PublishTask(NamedTuple):
    queue: str
    body: str
    retry_count: int = 0

_publish_queue: queue.Queue[PublishTask] = queue.Queue()
_report_thread: threading.Thread | None = None
_report_lock = threading.Lock()

def rabbitmq_worker():
    """Background worker to handle RabbitMQ connection and publishing."""
    connection = None
    channel = None
    credentials = pika.PlainCredentials(RABBITMQ_USER, RABBITMQ_PASS)
    parameters = pika.ConnectionParameters(
        host=RABBITMQ_HOST,
        port=RABBITMQ_PORT,
        virtual_host=RABBITMQ_VHOST,
        credentials=credentials,
        heartbeat=60,
        blocked_connection_timeout=30,
    )

    while True:
        try:
            if connection is None or connection.is_closed:
                logger.info("Connecting to RabbitMQ...")
                connection = pika.BlockingConnection(parameters)
                channel = connection.channel()
                logger.info("RabbitMQ connection established")

            task = _publish_queue.get()
            try:
                channel.queue_declare(queue=task.queue, durable=True)
                channel.basic_publish(
                    exchange="",
                    routing_key=task.queue,
                    body=task.body,
                    properties=pika.BasicProperties(delivery_mode=2),
                )
                logger.debug("Published message to %s", task.queue)
            except Exception as exc:
                if task.retry_count < MAX_PUBLISH_RETRIES:
                    delay = min(2 ** task.retry_count, 30)
                    logger.warning(
                        "Failed to publish message (attempt %d/%d), retrying in %ds: %s",
                        task.retry_count + 1, MAX_PUBLISH_RETRIES, delay, exc,
                    )
                    if connection and not connection.is_closed:
                        connection.close()
                    connection = None
                    time.sleep(delay)
                    _publish_queue.put(task._replace(retry_count=task.retry_count + 1))
                else:
                    logger.error(
                        "Dropping message to queue '%s' after %d failed attempts: %s",
                        task.queue, MAX_PUBLISH_RETRIES, exc,
                    )
            finally:
                _publish_queue.task_done()

        except Exception as exc:
            logger.error("RabbitMQ worker error: %s", exc)
            time.sleep(5) # Backoff

def publish(queue_name: str, body: str) -> None:
    _publish_queue.put(PublishTask(queue_name, body))

# Remove old _get_rabbit_channel and threading.local logic


def send_alert_xml(system_name: str) -> None:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    
    alert_el = ET.Element("alert")
    ET.SubElement(alert_el, "type").text = "HEARTBEAT_CRITICAL"
    ET.SubElement(alert_el, "system").text = system_name
    ET.SubElement(alert_el, "message").text = f"Systeem {system_name} heeft al meer dan {THRESHOLD_SECONDS}s geen heartbeat gestuurd."
    ET.SubElement(alert_el, "timestamp").text = timestamp
    
    xml_payload = ET.tostring(alert_el, encoding="unicode", xml_declaration=True)
    publish(ALERTS_QUEUE, xml_payload)
    logger.info("Alert published for %s", system_name)


def send_log_xml(level: str, action: str, message: str, source: str = "monitoring") -> None:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    
    message_el = ET.Element("message")
    header_el = ET.SubElement(message_el, "header")
    ET.SubElement(header_el, "message_id").text = str(uuid.uuid4())
    ET.SubElement(header_el, "timestamp").text = timestamp
    ET.SubElement(header_el, "source").text = source
    ET.SubElement(header_el, "type").text = "log"
    ET.SubElement(header_el, "version").text = "2.0"
    
    body_el = ET.SubElement(message_el, "body")
    ET.SubElement(body_el, "level").text = level
    ET.SubElement(body_el, "action").text = action
    ET.SubElement(body_el, "message").text = message
    
    xml_payload = ET.tostring(message_el, encoding="unicode", xml_declaration=True)
    publish("logs", xml_payload)
    logger.info("System log published: %s / %s", level, action)


def parse_recipients(raw: str) -> list[dict[str, str]]:
    recipients = []
    for block in (item.strip() for item in raw.split(",") if item.strip()):
        parts = [part.strip() for part in block.split(":")]
        if len(parts) != 4:
            logger.warning("Ignoring malformed recipient entry: %r", block)
            continue
        email, user_id, first_name, last_name = parts
        recipients.append(
            {
                "email": email,
                "user_id": user_id,
                "first_name": first_name,
                "last_name": last_name,
            }
        )
    return recipients


def build_send_mailing_xml(
    report_date: str,
    subject: str,
    template_data: dict,
    attachment: dict | None = None,
) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    correlation_id = f"report-{report_date}"
    campaign_id = f"{REPORT_CAMPAIGN_PREFIX}-{report_date}"
    recipients = parse_recipients(REPORT_RECIPIENTS)
    data_payload = json.dumps(template_data, separators=(",", ":"), ensure_ascii=False)

    message_el = ET.Element("message")
    header_el = ET.SubElement(message_el, "header")
    ET.SubElement(header_el, "message_id").text = str(uuid.uuid4())
    ET.SubElement(header_el, "timestamp").text = timestamp
    ET.SubElement(header_el, "source").text = REPORT_SOURCE
    ET.SubElement(header_el, "type").text = "send_mailing"
    ET.SubElement(header_el, "version").text = "2.0"
    ET.SubElement(header_el, "correlation_id").text = correlation_id

    body_el = ET.SubElement(message_el, "body")
    ET.SubElement(body_el, "campaign_id").text = campaign_id
    ET.SubElement(body_el, "subject").text = subject
    ET.SubElement(body_el, "template_id").text = REPORT_TEMPLATE_ID
    ET.SubElement(body_el, "mail_type").text = REPORT_MAIL_TYPE

    recipients_el = ET.SubElement(body_el, "recipients")
    for recipient in recipients:
        recipient_el = ET.SubElement(recipients_el, "recipient")
        ET.SubElement(recipient_el, "email").text = recipient["email"]
        ET.SubElement(recipient_el, "user_id").text = recipient["user_id"]
        contact_el = ET.SubElement(recipient_el, "contact")
        ET.SubElement(contact_el, "first_name").text = recipient["first_name"]
        ET.SubElement(contact_el, "last_name").text = recipient["last_name"]

    ET.SubElement(body_el, "template_data").text = data_payload

    if attachment is not None:
        attachment_el = ET.SubElement(body_el, "attachment")
        ET.SubElement(attachment_el, "filename").text = attachment["filename"]
        ET.SubElement(attachment_el, "content_type").text = attachment["content_type"]
        ET.SubElement(attachment_el, "base64_data").text = attachment["base64_data"]

    return ET.tostring(message_el, encoding="unicode", xml_declaration=True)


def send_report_message(report_date: str, attachment: dict | None, template_data: dict) -> None:
    subject = f"Daily Platform Report — {report_date}"
    xml_payload = build_send_mailing_xml(report_date, subject, template_data, attachment)
    publish(REPORT_QUEUE, xml_payload)
    logger.info("Daily report message published to %s", REPORT_QUEUE)


def query_aggregations(index: str, search_params: dict) -> dict:
    try:
        return es.search(index=index, allow_no_indices=True, **search_params)
    except NotFoundError:
        return {}


def aggregate_heartbeats(start: datetime, end: datetime) -> dict[str, int]:
    body = {
        "size": 0,
        "query": {
            "bool": {
                "filter": [{"range": {"@timestamp": {"gte": start.isoformat(), "lt": end.isoformat()}}}]
            }
        },
        "aggs": {
            "systems": {
                "terms": {"field": "system.keyword", "size": 500},
                "aggs": {
                    "timeline": {
                        "date_histogram": {
                            "field": "@timestamp",
                            "fixed_interval": "1m",
                            "min_doc_count": 0,
                            "extended_bounds": {"min": start.isoformat(), "max": end.isoformat()},
                        }
                    }
                },
            }
        },
    }
    result = query_aggregations("heartbeats-*", body)
    return {
        bucket["key"]: bucket["doc_count"]
        for bucket in result.get("aggregations", {}).get("systems", {}).get("buckets", [])
    }


def aggregate_logs(start: datetime, end: datetime) -> dict:
    body = {
        "size": 0,
        "query": {
            "bool": {
                "filter": [{"range": {"@timestamp": {"gte": start.isoformat(), "lt": end.isoformat()}}}]
            }
        },
        "aggs": {
            "systems": {
                "terms": {"field": "system.keyword", "size": 500, "order": {"_count": "desc"}},
                "aggs": {
                    "levels": {
                        "terms": {"field": "level.keyword", "size": 10},
                        "aggs": {
                            "actions": {"terms": {"field": "action.keyword", "size": 500}}
                        },
                    },
                    "errors": {
                        "filter": {"term": {"level.keyword": "error"}},
                        "aggs": {"top_errors": {"terms": {"field": "log_message.keyword", "size": 5}}},
                    },
                },
            },
            "info_actions": {
                "filter": {"term": {"level.keyword": "info"}},
                "aggs": {"actions": {"terms": {"field": "action.keyword", "size": 20}}},
            },
            "overall_errors": {
                "filter": {"term": {"level.keyword": "error"}},
                "aggs": {"top_errors": {"terms": {"field": "log_message.keyword", "size": 5}}},
            },
        },
    }
    return query_aggregations("logs-*", body)


def query_trailing_average(start: datetime, system: str) -> float:
    previous_start = start - timedelta(days=7)
    body = {
        "size": 0,
        "query": {
            "bool": {
                "filter": [
                    {"range": {"@timestamp": {"gte": previous_start.isoformat(), "lt": start.isoformat()}}},
                    {"term": {"system.keyword": system}},
                ]
            }
        },
        "aggs": {
            "days": {
                "date_histogram": {
                    "field": "@timestamp",
                    "fixed_interval": "1d",
                    "min_doc_count": 0,
                    "extended_bounds": {
                        "min": previous_start.isoformat(),
                        "max": (start - timedelta(seconds=1)).isoformat(),
                    },
                }
            }
        },
    }
    result = query_aggregations("logs-*", body)
    buckets = result.get("aggregations", {}).get("days", {}).get("buckets", [])
    if not buckets:
        return 0.0
    return sum(bucket["doc_count"] for bucket in buckets) / len(buckets)

REVENUE_PATTERN = re.compile(r"€\s*([0-9]+(?:[.,][0-9]{1,2})?)|([0-9]+(?:[.,][0-9]{1,2})?)\s*(?:EUR|eur)\b")

def parse_revenue_amount(message: str) -> float:
    match = REVENUE_PATTERN.search(message)
    if not match:
        return 0.0
    amount = match.group(1) or match.group(2)
    if not amount:
        return 0.0
    return float(amount.replace(",", "."))

def extract_payment_revenue(start: datetime, end: datetime) -> float:
    search_params = {
        "size": 10000,
        "query": {
            "bool": {
                "filter": [
                    {"range": {"@timestamp": {"gte": start.isoformat(), "lt": end.isoformat()}}},
                    {"term": {"action.keyword": "payment"}},
                ]
            }
        },
        "_source": ["log_message"],
    }
    try:
        result = es.search(index="logs-*", allow_no_indices=True, **search_params)
    except NotFoundError:
        return 0.0

    revenue = 0.0
    for hit in result.get("hits", {}).get("hits", []):
        message = hit.get("_source", {}).get("log_message", "")
        revenue += parse_revenue_amount(message)
    return round(revenue, 2)





def compute_health_score(availability: float, error_density: float) -> float:
    # Scale: 0-100% availability maps to 0-10 availability_component
    # 0 errors maps to 10, increasing errors reduce the score
    availability_component = (availability / 100.0) * 10.0
    error_component = max(0.0, 10.0 - (error_density / 10.0))
    return round((availability_component * 0.7 + error_component * 0.3), 1)


def render_report_pdf(context: dict) -> bytes:
    template = TEMPLATE_ENV.get_template("daily_report.html")
    html = template.render(**context)
    return HTML(string=html).write_pdf()


def archive_report_metadata(report_date: str, overall_health: float, systems_down: int, pdf_path: str) -> None:
    index_name = f"reports-{datetime.strptime(report_date, '%Y-%m-%d').strftime('%Y.%m.%d')}"
    document = {
        "report_date": report_date,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "overall_health": overall_health,
        "systems_down": systems_down,
        "storage_path": pdf_path,
    }
    try:
        es.index(index=index_name, document=document)
        logger.info("Report metadata archived to %s", index_name)
    except Exception as exc:
        logger.warning("Failed to archive report metadata: %s", exc)


def build_report_context(start: datetime, end: datetime) -> dict:
    heartbeat_counts = aggregate_heartbeats(start, end)
    log_agg = aggregate_logs(start, end)

    systems: list[dict] = []
    known_systems = KNOWN_SYSTEMS | set(heartbeat_counts) | {
        bucket["key"]
        for bucket in log_agg.get("aggregations", {}).get("systems", {}).get("buckets", [])
    }

    overall_errors = log_agg.get("aggregations", {}).get("overall_errors", {}).get("top_errors", {}).get("buckets", [])
    overall_top_alert = "No critical errors detected"
    if overall_errors:
        top = overall_errors[0]
        overall_top_alert = f"{top['key']} ({top['doc_count']})"

    business_summary = {key: 0 for key in BUSINESS_ACTIONS}
    payment_revenue = extract_payment_revenue(start, end)
    for action_bucket in log_agg.get("aggregations", {}).get("info_actions", {}).get("actions", {}).get("buckets", []):
        action = action_bucket["key"]
        count = action_bucket["doc_count"]
        if action in business_summary:
            business_summary[action] = count

    for bucket in log_agg.get("aggregations", {}).get("systems", {}).get("buckets", []):
        system = bucket["key"]
        level_counts = {"info": 0, "warning": 0, "error": 0}
        for level_bucket in bucket.get("levels", {}).get("buckets", []):
            level = level_bucket["key"]
            level_counts[level] = level_bucket["doc_count"]
            if level == "info":
                for action_bucket in level_bucket.get("actions", {}).get("buckets", []):
                    if action_bucket["key"] == "payment":
                        business_summary["payment"] = action_bucket["doc_count"]
        top_errors = [
            {"message": error_bucket["key"], "count": error_bucket["doc_count"]}
            for error_bucket in bucket.get("errors", {}).get("top_errors", {}).get("buckets", [])
        ]
        total_events = sum(level_counts.values())
        error_density = 0.0 if total_events == 0 else round(level_counts["error"] / total_events * 1000.0, 1)
        availability = round(min(100.0, heartbeat_counts.get(system, 0) / EXPECTED_HEARTBEATS_PER_DAY * 100.0), 2)
        health_score = compute_health_score(availability, error_density)
        trail_avg = query_trailing_average(start, system)
        activity_trend = compute_activity_trend(total_events, trail_avg)

        systems.append(
            {
                "name": system,
                "availability": availability,
                "error_density": error_density,
                "health_score": health_score,
                "top_issues": top_errors[:3],
                "activity_trend": activity_trend,
                "total_events": total_events,
                "heartbeats": heartbeat_counts.get(system, 0),
            }
        )

    for system, total in heartbeat_counts.items():
        if system not in {s["name"] for s in systems}:
            availability = round(min(100.0, total / EXPECTED_HEARTBEATS_PER_DAY * 100.0), 2)
            health_score = compute_health_score(availability, 0.0)
            systems.append(
                {
                    "name": system,
                    "availability": availability,
                    "error_density": 0.0,
                    "health_score": health_score,
                    "top_issues": [],
                    "activity_trend": "0%",
                    "total_events": 0,
                    "heartbeats": total,
                }
            )

    for system in sorted(known_systems):
        if system not in {s["name"] for s in systems}:
            systems.append(
                {
                    "name": system,
                    "availability": 0.0,
                    "error_density": 0.0,
                    "health_score": 0.0,
                    "top_issues": [],
                    "activity_trend": "0%",
                    "total_events": 0,
                    "heartbeats": 0,
                }
            )

    systems.sort(key=lambda item: item["health_score"], reverse=True)
    total_events = sum(item["total_events"] for item in systems)
    systems_down = sum(1 for item in systems if item["heartbeats"] < EXPECTED_HEARTBEATS_PER_DAY)
    overall_health = round(sum(item["health_score"] for item in systems) / len(systems), 1) if systems else 0.0

    return {
        "report_date": start.strftime("%Y-%m-%d"),
        "overall_health": overall_health,
        "systems_down": systems_down,
        "top_alert": overall_top_alert,
        "systems": systems,
        "business": {
            "registration": business_summary["registration"],
            "payment": business_summary["payment"],
            "invoice": business_summary["invoice"],
            "badge": business_summary["badge"],
            "email": business_summary["email"],
            "revenue": round(payment_revenue, 2),
        },
        "total_events": total_events,
        "report_sent_to": ", ".join(r["email"] for r in parse_recipients(REPORT_RECIPIENTS)),
    }


def compute_activity_trend(current_count: int, trailing_avg: float) -> str:
    if trailing_avg <= 0:
        return "+100%" if current_count > 0 else "0%"
    percent = round((current_count / trailing_avg - 1.0) * 100.0)
    return f"+{percent}%" if percent >= 0 else f"−{abs(percent)}%"


def generate_daily_report(now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    start = now - timedelta(days=1)
    report_date = start.strftime("%Y-%m-%d")
    logger.info("Generating daily report for %s", report_date)
    try:
        context = build_report_context(start, now)
        pdf_bytes = render_report_pdf(context)
        encoded_pdf = base64.b64encode(pdf_bytes).decode("ascii")
        attachment = {
            "filename": f"platform-report-{report_date}.pdf",
            "content_type": "application/pdf",
            "base64_data": encoded_pdf,
        }
        template_data = {
            "report_date": report_date,
            "overall_health": context["overall_health"],
            "systems_down": context["systems_down"],
            "top_alert": context["top_alert"],
        }
        send_report_message(report_date, attachment, template_data)
        archive_report_metadata(report_date, context["overall_health"], context["systems_down"], f"reports/platform-report-{report_date}.pdf")
    except Exception as exc:
        logger.exception("Failed to generate daily report")
        send_log_xml("error", "system_error", f"Report generation failed for {report_date}: {exc}")
        send_report_message(
            report_date,
            None,
            {
                "report_date": report_date,
                "overall_health": 0,
                "systems_down": 0,
                "top_alert": f"Report generation failed for {report_date}",
            },
        )


def should_run_daily_report(now: datetime) -> bool:
    return now.hour == REPORT_TIME_HOUR and now.minute == REPORT_TIME_MINUTE


def start_report_generation(now: datetime) -> None:
    global _report_thread
    with _report_lock:
        if _report_thread is not None and _report_thread.is_alive():
            logger.warning("Daily report generation already in progress; skipping this run")
            return
        _report_thread = threading.Thread(target=generate_daily_report, args=(now,), daemon=True)
        _report_thread.start()
        logger.info("Started background report thread for %s", now.date())


def main() -> None:
    parser = argparse.ArgumentParser(description="Monitoring detector with daily report generation")
    parser.add_argument("--run-report", action="store_true", help="Generate the daily report once and exit")
    args = parser.parse_args()

    # Start RabbitMQ background worker
    threading.Thread(target=rabbitmq_worker, daemon=True, name="RabbitMQWorker").start()

    if args.run_report:
        generate_daily_report()
        flush_timeout = 30
        joiner = threading.Thread(target=_publish_queue.join, daemon=True)
        joiner.start()
        joiner.join(timeout=flush_timeout)
        if joiner.is_alive():
            logger.warning(
                "Publish queue did not flush within %ds; exiting anyway", flush_timeout
            )
        return

    next_report_date = None
    while True:
        now = datetime.now(timezone.utc)
        try:
            # Query: Zoek de laatste heartbeat per systeem
            heartbeat_query = {
                "size": 0,
                "query": {"match_all": {}},
                "aggs": {
                    "systems": {
                        "terms": {"field": "system.keyword", "size": 500},
                        "aggs": {"last_heartbeat": {"max": {"field": "@timestamp"}}},
                    }
                },
            }
            res = query_aggregations("heartbeats-*", heartbeat_query)
            now = datetime.now(timezone.utc)
            for bucket in res.get("aggregations", {}).get("systems", {}).get("buckets", []):
                system = bucket["key"]
                last_value = bucket["last_heartbeat"].get("value")
                if last_value is None:
                    continue
                last_ts = datetime.fromtimestamp(last_value / 1000.0, tz=timezone.utc)
                diff = (now - last_ts).total_seconds()
                if diff > THRESHOLD_SECONDS:
                    last_alert = cooldown_list.get(system)
                    if not last_alert or (now - last_alert) > timedelta(minutes=COOLDOWN_MINUTES):
                        send_alert_xml(system)
                        cooldown_list[system] = now
                else:
                    if system in cooldown_list:
                        logger.info("%s is back online", system)
                        del cooldown_list[system]

            if should_run_daily_report(now):
                if next_report_date != now.date():
                    start_report_generation(now)
                    next_report_date = now.date()

        except Exception:
            logger.exception("Error in detector")

        time.sleep(1)


if __name__ == "__main__":
    main()
