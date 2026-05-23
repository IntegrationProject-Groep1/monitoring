"""
Create / update Kibana dashboards for the Shift Festival monitoring stack.

Run after every Kibana restart (data is ephemeral — emptyDir volume):

    python kibana/setup_dashboards.py

Or with explicit credentials:

    KIBANA_URL=https://kibana.desiderius.me \
    KIBANA_USER=elastic \
    KIBANA_PASS=<password> \
    python kibana/setup_dashboards.py
"""

import json
import os
import sys
import time

import requests

KIBANA_URL  = os.getenv("KIBANA_URL",  "https://kibana.desiderius.me")
KIBANA_USER = os.getenv("KIBANA_USER", "elastic")
KIBANA_PASS = os.getenv("KIBANA_PASS", os.getenv("ES_ADMIN_PASS", ""))

HEADERS = {
    "kbn-xsrf": "true",
    "Content-Type": "application/json",
}
AUTH = (KIBANA_USER, KIBANA_PASS)

# Fixed IDs so the chatbot can deep-link to these dashboards
DASH_ID_HEARTBEATS = "shift-mcp-heartbeats-dashboard"
DASH_ID_LOGS       = "shift-service-logs-dashboard"
DV_HEARTBEATS      = "shift-heartbeats-dv"
DV_LOGS            = "shift-logs-dv"

ALL_SYSTEMS = [
    "chatbot", "frontend", "kassa", "facturatie", "crm", "planning",
    "mailing", "identity-service", "monitoring",
    "frontend-mcp", "kassa-mcp", "facturatie-mcp", "crm-mcp", "monitoring-mcp",
]
MCP_SYSTEMS = [s for s in ALL_SYSTEMS if s.endswith("-mcp")]


def wait_for_kibana(max_wait: int = 120) -> None:
    print(f"Waiting for Kibana at {KIBANA_URL} …", flush=True)
    deadline = time.time() + max_wait
    while time.time() < deadline:
        try:
            r = requests.get(f"{KIBANA_URL}/api/status", auth=AUTH, timeout=5, verify=False)
            if r.status_code == 200 and r.json().get("status", {}).get("overall", {}).get("level") in ("available", "green"):
                print("Kibana is ready.", flush=True)
                return
        except Exception:
            pass
        time.sleep(5)
    sys.exit("Kibana did not become ready in time.")


def upsert(obj_type: str, obj_id: str, attributes: dict) -> None:
    url = f"{KIBANA_URL}/api/saved_objects/{obj_type}/{obj_id}"
    body = {"attributes": attributes}
    r = requests.post(url, json=body, headers=HEADERS, auth=AUTH, verify=False)
    if r.status_code in (200, 201):
        print(f"  ✓ {obj_type}/{obj_id}", flush=True)
    elif r.status_code == 409:
        r2 = requests.put(url, json=body, headers=HEADERS, auth=AUTH, verify=False)
        if r2.status_code in (200, 201):
            print(f"  ↑ {obj_type}/{obj_id} (updated)", flush=True)
        else:
            print(f"  ✗ {obj_type}/{obj_id}: {r2.status_code} {r2.text[:120]}", flush=True)
    else:
        print(f"  ✗ {obj_type}/{obj_id}: {r.status_code} {r.text[:120]}", flush=True)


def create_data_views() -> None:
    print("\n── Data views ─────────────────────────────────────────")
    upsert("index-pattern", DV_HEARTBEATS, {
        "title": "heartbeats-*",
        "timeFieldName": "@timestamp",
        "fields": "[]",
    })
    upsert("index-pattern", DV_LOGS, {
        "title": "logs-*",
        "timeFieldName": "@timestamp",
        "fields": "[]",
    })


def _vis_state_heartbeat_timeseries() -> str:
    """TSVB: heartbeats per minute, split by system."""
    return json.dumps({
        "title": "Heartbeats per minute by system",
        "type": "metrics",
        "aggs": [],
        "params": {
            "type": "timeseries",
            "index_pattern": DV_HEARTBEATS,
            "time_range_mode": "auto",
            "interval": "1m",
            "axis_min": "0",
            "series": [
                {
                    "id": "series-hb-all",
                    "label": "Heartbeats/min",
                    "metrics": [{"id": "metric-1", "type": "count"}],
                    "split_mode": "terms",
                    "terms_field": "system.keyword",
                    "terms_size": 20,
                    "line_width": "1",
                    "point_size": "1",
                    "fill": "0.3",
                    "stacked": "none",
                }
            ],
        },
    })


def _vis_state_active_systems() -> str:
    """Metric: unique active systems in last 5 min."""
    return json.dumps({
        "title": "Active systems",
        "type": "metric",
        "aggs": [
            {
                "id": "1",
                "type": "cardinality",
                "schema": "metric",
                "params": {"field": "system.keyword"},
            }
        ],
        "params": {
            "addLegend": False,
            "addTooltip": True,
            "metric": {"colorSchema": "Green to Red", "useRanges": False},
        },
    })


def _vis_state_status_table() -> str:
    """Data table: last heartbeat per system with status."""
    return json.dumps({
        "title": "Latest heartbeat per system",
        "type": "table",
        "aggs": [
            {
                "id": "1",
                "type": "terms",
                "schema": "bucket",
                "params": {"field": "system.keyword", "size": 30, "order": "asc"},
            },
            {
                "id": "2",
                "type": "terms",
                "schema": "metric",
                "params": {"field": "status.keyword", "size": 1, "order": "desc"},
            },
            {
                "id": "3",
                "type": "max",
                "schema": "metric",
                "params": {"field": "@timestamp"},
            },
        ],
        "params": {
            "showTotal": False,
            "sort": {"columnIndex": None, "direction": None},
            "totalFunc": "sum",
        },
    })


def _vis_state_mcp_table() -> str:
    """Data table: MCP-only heartbeats."""
    return json.dumps({
        "title": "MCP server heartbeats",
        "type": "table",
        "aggs": [
            {
                "id": "1",
                "type": "terms",
                "schema": "bucket",
                "params": {
                    "field": "system.keyword",
                    "size": 10,
                    "order": "asc",
                    "include": ".*-mcp",
                },
            },
            {
                "id": "2",
                "type": "terms",
                "schema": "metric",
                "params": {"field": "status.keyword", "size": 1, "order": "desc"},
            },
            {
                "id": "3",
                "type": "max",
                "schema": "metric",
                "params": {"field": "uptime_seconds"},
            },
        ],
        "params": {"showTotal": False},
    })


def _vis_state_log_count() -> str:
    """Bar: log count by service + level."""
    return json.dumps({
        "title": "Log count by service & level",
        "type": "histogram",
        "aggs": [
            {
                "id": "1",
                "type": "count",
                "schema": "metric",
                "params": {},
            },
            {
                "id": "2",
                "type": "terms",
                "schema": "segment",
                "params": {"field": "system.keyword", "size": 20, "order": "desc"},
            },
            {
                "id": "3",
                "type": "terms",
                "schema": "group",
                "params": {"field": "level.keyword", "size": 3},
            },
        ],
        "params": {
            "addLegend": True,
            "addTimeMarker": False,
            "addTooltip": True,
            "categoryAxes": [{"position": "bottom", "scale": {"type": "linear"}}],
            "mode": "stacked",
            "scale": "linear",
        },
    })


def _vis_state_log_table() -> str:
    """Table: recent log entries."""
    return json.dumps({
        "title": "Recent log entries",
        "type": "table",
        "aggs": [
            {"id": "1", "type": "count", "schema": "metric", "params": {}},
            {"id": "2", "type": "terms", "schema": "bucket", "params": {"field": "system.keyword", "size": 20}},
            {"id": "3", "type": "terms", "schema": "bucket", "params": {"field": "level.keyword", "size": 3}},
            {"id": "4", "type": "terms", "schema": "bucket", "params": {"field": "action.keyword", "size": 10}},
        ],
        "params": {"showTotal": False},
    })


def _vis_common(vis_id: str, title: str, vis_state_fn, dv_id: str) -> None:
    upsert("visualization", vis_id, {
        "title": title,
        "visState": vis_state_fn(),
        "uiStateJSON": "{}",
        "description": "",
        "savedSearchRefName": None,
        "kibanaSavedObjectMeta": {
            "searchSourceJSON": json.dumps({
                "query": {"language": "kuery", "query": ""},
                "filter": [],
                "indexRefName": "kibanaSavedObjectMeta.searchSourceJSON.index",
            })
        },
    })


def _panel(vis_id: str, col: int, row: int, w: int, h: int, panel_id: str) -> dict:
    return {
        "panelIndex": panel_id,
        "gridData": {"x": col, "y": row, "w": w, "h": h, "i": panel_id},
        "type": "visualization",
        "version": "8.17.0",
        "panelRefName": f"panel_{panel_id}",
    }


def create_heartbeat_dashboard() -> None:
    print("\n── Heartbeats dashboard ────────────────────────────────")
    _vis_common("vis-hb-timeseries",  "Heartbeats per minute", _vis_state_heartbeat_timeseries, DV_HEARTBEATS)
    _vis_common("vis-hb-active",      "Active systems",         _vis_state_active_systems,       DV_HEARTBEATS)
    _vis_common("vis-hb-status-table","Latest status per system",_vis_state_status_table,        DV_HEARTBEATS)
    _vis_common("vis-hb-mcp-table",   "MCP server heartbeats",  _vis_state_mcp_table,            DV_HEARTBEATS)

    panels = [
        _panel("vis-hb-active",       0,  0, 12, 8,  "p1"),
        _panel("vis-hb-timeseries",   12, 0, 36, 15, "p2"),
        _panel("vis-hb-status-table", 0,  8, 24, 20, "p3"),
        _panel("vis-hb-mcp-table",    24, 8, 24, 20, "p4"),
    ]
    refs = [
        {"type": "index-pattern", "id": DV_HEARTBEATS, "name": "kibanaSavedObjectMeta.searchSourceJSON.index"},
        {"type": "visualization", "id": "vis-hb-active",        "name": "panel_p1"},
        {"type": "visualization", "id": "vis-hb-timeseries",    "name": "panel_p2"},
        {"type": "visualization", "id": "vis-hb-status-table",  "name": "panel_p3"},
        {"type": "visualization", "id": "vis-hb-mcp-table",     "name": "panel_p4"},
    ]

    upsert("dashboard", DASH_ID_HEARTBEATS, {
        "title": "MCP & Heartbeat Monitor",
        "description": "Live heartbeat status for all services and MCP servers",
        "panelsJSON": json.dumps(panels),
        "optionsJSON": json.dumps({"hidePanelTitles": False, "useMargins": True}),
        "timeFrom": "now-1h",
        "timeTo": "now",
        "refreshInterval": {"pause": False, "value": 10000},
        "kibanaSavedObjectMeta": {
            "searchSourceJSON": json.dumps({"query": {"language": "kuery", "query": ""}, "filter": []})
        },
    })


def create_logs_dashboard() -> None:
    print("\n── Logs dashboard ──────────────────────────────────────")
    _vis_common("vis-log-count", "Log count by service & level", _vis_state_log_count, DV_LOGS)
    _vis_common("vis-log-table", "Recent log entries",           _vis_state_log_table, DV_LOGS)

    panels = [
        _panel("vis-log-count",  0,  0, 48, 20, "q1"),
        _panel("vis-log-table",  0, 20, 48, 20, "q2"),
    ]
    refs = [
        {"type": "index-pattern", "id": DV_LOGS, "name": "kibanaSavedObjectMeta.searchSourceJSON.index"},
        {"type": "visualization", "id": "vis-log-count", "name": "panel_q1"},
        {"type": "visualization", "id": "vis-log-table", "name": "panel_q2"},
    ]

    upsert("dashboard", DASH_ID_LOGS, {
        "title": "Service Logs",
        "description": "Log volume and recent entries per service",
        "panelsJSON": json.dumps(panels),
        "optionsJSON": json.dumps({"hidePanelTitles": False, "useMargins": True}),
        "timeFrom": "now-1h",
        "timeTo": "now",
        "refreshInterval": {"pause": False, "value": 30000},
        "kibanaSavedObjectMeta": {
            "searchSourceJSON": json.dumps({"query": {"language": "kuery", "query": ""}, "filter": []})
        },
    })


if __name__ == "__main__":
    import urllib3
    urllib3.disable_warnings()

    wait_for_kibana()
    create_data_views()
    create_heartbeat_dashboard()
    create_logs_dashboard()

    print(f"""
Done. Dashboard URLs:
  MCP & Heartbeats : {KIBANA_URL}/app/dashboards#/view/{DASH_ID_HEARTBEATS}
  Service Logs     : {KIBANA_URL}/app/dashboards#/view/{DASH_ID_LOGS}
""", flush=True)
