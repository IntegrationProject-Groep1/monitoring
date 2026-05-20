"""
Verifies that the ELK stack indexed heartbeat and platform-log documents
after the producer ran.

Queries Elasticsearch for today's heartbeats-*, heartbeats-quarantine-*,
logs-* and logs-quarantine-* indices and checks that the expected documents
are present.

Exits 0 if all checks pass, 1 on any failure.
"""

import os
import sys
import time
import urllib.request
import urllib.error
import json
import base64
from datetime import datetime, timezone

ES_HOST = os.environ.get("ES_HOST", "http://localhost:30060")
ES_USER = os.environ.get("ES_ADMIN_USER", "elastic")
ES_PASS = os.environ.get("ES_ADMIN_PASS", "elk123")

TODAY = datetime.now(timezone.utc).strftime("%Y.%m.%d")
HEARTBEATS_INDEX = f"heartbeats-{TODAY}"
HEARTBEATS_QUARANTINE_INDEX = f"heartbeats-quarantine-{TODAY}"
LOGS_INDEX = f"logs-{TODAY}"
LOGS_QUARANTINE_INDEX = f"logs-quarantine-{TODAY}"

EXPECTED_HEARTBEAT_SYSTEMS = ["planning", "crm", "kassa", "facturatie", "monitoring", "frontend", "mailing", "iot_gateway"]
EXPECTED_LOG_SOURCES = ["planning", "crm", "kassa", "facturatie", "frontend", "mailing", "identity-service", "iot_gateway"]


def es_request(path: str) -> dict:
    url = f"{ES_HOST}{path}"
    req = urllib.request.Request(url)
    token = base64.b64encode(f"{ES_USER}:{ES_PASS}".encode()).decode()
    req.add_header("Authorization", f"Basic {token}")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def wait_for_documents(index: str, min_count: int, retries: int = 12, interval: int = 5) -> int:
    for attempt in range(1, retries + 1):
        try:
            result = es_request(f"/{index}/_count")
            count = result.get("count", 0)
            print(f"  [{index}] count={count} (attempt {attempt}/{retries})")
            if count >= min_count:
                return count
        except urllib.error.HTTPError as e:
            if e.code == 404:
                print(f"  [{index}] index does not exist yet (attempt {attempt}/{retries})")
            else:
                raise
        if attempt < retries:
            time.sleep(interval)
    return 0


def check_field_values(index: str, field: str, expected: list[str], label: str) -> bool:
    try:
        result = es_request(f"/{index}/_search?size=200&_source={field}")
        hits = result.get("hits", {}).get("hits", [])
        found = {h["_source"].get(field) for h in hits}
        missing = [v for v in expected if v not in found]
        if missing:
            print(f"  FAIL: missing {label} in {index}: {missing}")
            return False
        print(f"  OK: all expected {label} present in {index}: {sorted(v for v in found if v)}")
        return True
    except Exception as e:
        print(f"  FAIL: could not query {label} in {index}: {e}")
        return False


def main() -> None:
    print(f"Verifying ELK pipeline against {ES_HOST}\n")
    failures = []

    print(f"Checking heartbeats index ({HEARTBEATS_INDEX}) — expecting >= {len(EXPECTED_HEARTBEAT_SYSTEMS)} docs:")
    count = wait_for_documents(HEARTBEATS_INDEX, len(EXPECTED_HEARTBEAT_SYSTEMS))
    if count < len(EXPECTED_HEARTBEAT_SYSTEMS):
        failures.append(f"{HEARTBEATS_INDEX} has {count} docs, expected >= {len(EXPECTED_HEARTBEAT_SYSTEMS)}")
    else:
        print(f"  OK: {count} documents found")

    print("\nChecking all expected systems present in heartbeats index:")
    if not check_field_values(HEARTBEATS_INDEX, "system", EXPECTED_HEARTBEAT_SYSTEMS, "systems"):
        failures.append("Not all expected systems found in heartbeats index")

    print(f"\nChecking heartbeats quarantine index ({HEARTBEATS_QUARANTINE_INDEX}) — expecting >= 5 docs:")
    q_count = wait_for_documents(HEARTBEATS_QUARANTINE_INDEX, 5)
    if q_count < 5:
        failures.append(f"{HEARTBEATS_QUARANTINE_INDEX} has {q_count} docs, expected >= 5")
    else:
        print(f"  OK: {q_count} documents found")

    print(f"\nChecking logs index ({LOGS_INDEX}) — expecting >= {len(EXPECTED_LOG_SOURCES)} docs:")
    count = wait_for_documents(LOGS_INDEX, len(EXPECTED_LOG_SOURCES))
    if count < len(EXPECTED_LOG_SOURCES):
        failures.append(f"{LOGS_INDEX} has {count} docs, expected >= {len(EXPECTED_LOG_SOURCES)}")
    else:
        print(f"  OK: {count} documents found")

    print("\nChecking all expected sources present in logs index:")
    if not check_field_values(LOGS_INDEX, "system", EXPECTED_LOG_SOURCES, "sources"):
        failures.append("Not all expected sources found in logs index")

    print(f"\nChecking logs quarantine index ({LOGS_QUARANTINE_INDEX}) — expecting >= 5 docs:")
    q_count = wait_for_documents(LOGS_QUARANTINE_INDEX, 5)
    if q_count < 5:
        failures.append(f"{LOGS_QUARANTINE_INDEX} has {q_count} docs, expected >= 5")
    else:
        print(f"  OK: {q_count} documents found")

    print()
    if failures:
        print("FAILED:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    else:
        print("All checks passed.")


if __name__ == "__main__":
    main()
