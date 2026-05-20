"""Verifies the detector publishes a HEARTBEAT_CRITICAL alert to the alerts queue and generates daily reports.

Run after producer.py has sent heartbeats and then stopped — the detector's 3s
threshold will trip within a few seconds, and the alert should land on the queue.
Exits 0 on success, 1 on timeout or wrong payload.
"""

import os
import sys
import time
import subprocess

import pika

RABBITMQ_HOST = os.environ.get("RABBITMQ_HOST", "localhost")
RABBITMQ_PORT = int(os.environ.get("RABBITMQ_PORT", "5672"))
RABBITMQ_USER = os.environ["RABBITMQMONITORING_USER"]
RABBITMQ_PASS = os.environ["RABBITMQMONITORING_PASS"]
REPORT_QUEUE = os.getenv("REPORT_QUEUE", "monitoring.reports")
ALERTS_QUEUE = os.getenv("ALERTS_QUEUE", "monitoring.alerts")
TIMEOUT_SECONDS = 20
POLL_INTERVAL = 0.5


def main() -> None:
    credentials = pika.PlainCredentials(RABBITMQ_USER, RABBITMQ_PASS)
    connection = pika.BlockingConnection(
        pika.ConnectionParameters(host=RABBITMQ_HOST, port=RABBITMQ_PORT, credentials=credentials)
    )
    channel = connection.channel()

    # Test alert publishing
    channel.queue_declare(queue=ALERTS_QUEUE, durable=True)
    alert_found = False
    deadline = time.time() + TIMEOUT_SECONDS
    print(f"Waiting for alert on '{ALERTS_QUEUE}'...")
    while time.time() < deadline:
        method, _props, body = channel.basic_get(queue=ALERTS_QUEUE, auto_ack=True)
        if method is not None:
            payload = body.decode("utf-8", errors="replace")
            print(f"Received alert:\n{payload}")
            if "<type>HEARTBEAT_CRITICAL</type>" not in payload:
                print(f"FAIL: payload missing HEARTBEAT_CRITICAL type tag", file=sys.stderr)
                connection.close()
                sys.exit(1)
            alert_found = True
            break
        time.sleep(POLL_INTERVAL)

    if not alert_found:
        connection.close()
        print(f"FAIL: no alert on '{ALERTS_QUEUE}' within {TIMEOUT_SECONDS}s", file=sys.stderr)
        sys.exit(1)


    # Test report generation
    channel.queue_declare(queue=REPORT_QUEUE, durable=True)
    print("Running detector --run-report to generate a daily report...")
    report_container = os.getenv("DETECTOR_RUN_REPORT_CONTAINER")
    if report_container:
        cmd = [
            "docker",
            "compose",
            "exec",
            "-T",
            report_container,
            "python3",
            "detector.py",
            "--run-report",
        ]
    else:
        cmd = ["python3", "-u", "detector/detector.py", "--run-report"]

    result = subprocess.run(
        cmd,
        cwd=os.path.dirname(os.path.abspath(__file__)) + "/..",
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        print("FAIL: detector --run-report failed", file=sys.stderr)
        print("STDOUT:", result.stdout, file=sys.stderr)
        print("STDERR:", result.stderr, file=sys.stderr)
        connection.close()
        sys.exit(1)

    report_found = False
    deadline = time.time() + TIMEOUT_SECONDS
    while time.time() < deadline:
        method, _props, body = channel.basic_get(queue=REPORT_QUEUE, auto_ack=True)
        if method is not None:
            payload = body.decode("utf-8", errors="replace")
            print(f"Received report message:\n{payload}")
            if "<mail_type>daily_report</mail_type>" not in payload and "<source>monitoring</source>" not in payload:
                print("FAIL: report payload missing expected tags", file=sys.stderr)
                connection.close()
                sys.exit(1)
            report_found = True
            break
        time.sleep(POLL_INTERVAL)

    connection.close()
    if not report_found:
        print(f"FAIL: no report on '{REPORT_QUEUE}' within {TIMEOUT_SECONDS}s", file=sys.stderr)
        sys.exit(1)

    print("OK: detector emitted HEARTBEAT_CRITICAL alert and daily report")
    return


if __name__ == "__main__":
    main()
