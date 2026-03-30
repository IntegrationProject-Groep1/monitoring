"""
Verifies that the ELK stack indexed heartbeat documents after the producer ran.

Queries Elasticsearch for today's heartbeats-* and heartbeats-quarantine-* indices
and checks that the expected documents are present.

Exits 0 if all checks pass, 1 on any failure.
"""

import os
import sys
import time
import urllib.request
import urllib.error
import json
from datetime import datetime, timezone

ES_HOST = os.environ.get("ES_HOST", "http://localhost:30060")
ES_USER = os.environ.get("ES_ADMIN_USER", "elastic")
ES_PASS = os.environ.get("ES_ADMIN_PASS", "elk123")

TODAY = datetime.now(timezone.utc).strftime("%Y.%m.%d")
NORMAL_INDEX = f"heartbeats-{TODAY}"
QUARANTINE_INDEX = f"heartbeats-quarantine-{TODAY}"

EXPECTED_TEAMS = ["team-planning", "team-crm", "team-kassa", "team-facturatie", "team-monitoring"]


def es_request(path: str) -> dict:
    url = f"{ES_HOST}{path}"
    req = urllib.request.Request(url)
    import base64
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


def check_teams_present() -> bool:
    try:
        result = es_request(
            f"/{NORMAL_INDEX}/_search?size=100&_source=team"
        )
        hits = result.get("hits", {}).get("hits", [])
        found_teams = {h["_source"].get("team") for h in hits}
        missing = [t for t in EXPECTED_TEAMS if t not in found_teams]
        if missing:
            print(f"  FAIL: missing teams in normal index: {missing}")
            return False
        print(f"  OK: all expected teams present: {sorted(found_teams)}")
        return True
    except Exception as e:
        print(f"  FAIL: could not query teams: {e}")
        return False


def main() -> None:
    print(f"Verifying ELK pipeline against {ES_HOST}\n")
    failures = []

    print(f"Checking normal index ({NORMAL_INDEX}) — expecting >= {len(EXPECTED_TEAMS)} docs:")
    count = wait_for_documents(NORMAL_INDEX, len(EXPECTED_TEAMS))
    if count < len(EXPECTED_TEAMS):
        failures.append(f"Normal index has {count} docs, expected >= {len(EXPECTED_TEAMS)}")
    else:
        print(f"  OK: {count} documents found")

    print(f"\nChecking team mapping in normal index:")
    if not check_teams_present():
        failures.append("Not all expected teams found in normal index")

    print(f"\nChecking quarantine index ({QUARANTINE_INDEX}) — expecting >= 2 docs:")
    q_count = wait_for_documents(QUARANTINE_INDEX, 2)
    if q_count < 2:
        failures.append(f"Quarantine index has {q_count} docs, expected >= 2")
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
