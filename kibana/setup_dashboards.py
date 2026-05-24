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
DASH_ID_MCP        = "shift-mcp-servers-dashboard"
DASH_ID_MCP_ALIAS  = "mcp-dashboards"
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


def upsert(obj_type: str, obj_id: str, attributes: dict, references: list | None = None) -> None:
    url = f"{KIBANA_URL}/api/saved_objects/{obj_type}/{obj_id}"
    body = {"attributes": attributes}
    if references is not None:
        body["references"] = references
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


def _search_source(dv_id: str, kql: str = "") -> str:
    return json.dumps({
        "query": {"language": "kuery", "query": kql},
        "filter": [],
        "indexRefName": "kibanaSavedObjectMeta.searchSourceJSON.index",
    })


def _vis(vis_id: str, title: str, vis_state: dict, dv_id: str, kql: str = "") -> None:
    upsert("visualization", vis_id, {
        "title": title,
        "visState": json.dumps(vis_state),
        "uiStateJSON": "{}",
        "description": "",
        "savedSearchRefName": None,
        "kibanaSavedObjectMeta": {
            "searchSourceJSON": _search_source(dv_id, kql),
        },
    }, references=[
        {"type": "index-pattern", "id": dv_id,
         "name": "kibanaSavedObjectMeta.searchSourceJSON.index"},
    ])


def _panel(vis_id: str, col: int, row: int, w: int, h: int, panel_id: str) -> dict:
    return {
        "panelIndex": panel_id,
        "gridData": {"x": col, "y": row, "w": w, "h": h, "i": panel_id},
        "type": "visualization",
        "version": "8.17.0",
        "panelRefName": f"panel_{panel_id}",
    }


def _dashboard(dash_id: str, title: str, description: str, panels: list,
               refs_extra: list, dv_id: str, refresh_ms: int = 10000) -> None:
    refs = [
        {"type": "index-pattern", "id": dv_id,
         "name": "kibanaSavedObjectMeta.searchSourceJSON.index"},
    ] + refs_extra
    upsert("dashboard", dash_id, {
        "title": title,
        "description": description,
        "panelsJSON": json.dumps(panels),
        "optionsJSON": json.dumps({"hidePanelTitles": False, "useMargins": True}),
        "timeFrom": "now-1h",
        "timeTo": "now",
        "refreshInterval": {"pause": False, "value": refresh_ms},
        "kibanaSavedObjectMeta": {
            "searchSourceJSON": json.dumps({
                "query": {"language": "kuery", "query": ""},
                "filter": [],
            })
        },
    }, references=refs)


# ── Data views ─────────────────────────────────────────────────────────────────

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


# ── Shared vis states ──────────────────────────────────────────────────────────

def _vs_timeseries(title: str, index: str, split_size: int = 20) -> dict:
    """TSVB: heartbeats per minute, optionally split by system."""
    return {
        "title": title,
        "type": "metrics",
        "aggs": [],
        "params": {
            "type": "timeseries",
            "index_pattern": index,
            "time_range_mode": "auto",
            "interval": "1m",
            "axis_min": "0",
            "series": [{
                "id": "series-1",
                "label": "Heartbeats/min",
                "metrics": [{"id": "m1", "type": "count"}],
                "split_mode": "terms",
                "terms_field": "system.keyword",
                "terms_size": split_size,
                "line_width": "2",
                "point_size": "2",
                "fill": "0.2",
                "stacked": "none",
            }],
        },
    }


def _vs_cardinality_metric(title: str, field: str) -> dict:
    return {
        "title": title,
        "type": "metric",
        "aggs": [{
            "id": "1", "type": "cardinality", "schema": "metric",
            "params": {"field": field},
        }],
        "params": {
            "addLegend": False,
            "addTooltip": True,
            "metric": {"colorSchema": "Green to Red", "useRanges": False},
        },
    }


def _vs_count_metric(title: str) -> dict:
    return {
        "title": title,
        "type": "metric",
        "aggs": [{"id": "1", "type": "count", "schema": "metric", "params": {}}],
        "params": {
            "addLegend": False,
            "addTooltip": True,
            "metric": {"colorSchema": "Green to Red", "useRanges": False},
        },
    }


def _vs_status_pie(title: str) -> dict:
    return {
        "title": title,
        "type": "pie",
        "aggs": [
            {"id": "1", "type": "count", "schema": "metric", "params": {}},
            {
                "id": "2", "type": "terms", "schema": "segment",
                "params": {"field": "status.keyword", "size": 5, "order": "desc"},
            },
        ],
        "params": {
            "addLegend": True,
            "addTooltip": True,
            "isDonut": True,
            "legendPosition": "right",
        },
    }


def _vs_status_table(title: str, include_pattern: str = None) -> dict:
    """Table: system | latest status | last seen | uptime."""
    bucket_params: dict = {"field": "system.keyword", "size": 30, "order": "asc"}
    if include_pattern:
        bucket_params["include"] = include_pattern
    return {
        "title": title,
        "type": "table",
        "aggs": [
            {"id": "1", "type": "terms",     "schema": "bucket", "params": bucket_params},
            {"id": "2", "type": "top_hits",  "schema": "metric", "params": {"field": "status.keyword", "aggregate": "concat", "size": 1, "sortField": "@timestamp", "sortOrder": "desc"}},
            {"id": "3", "type": "max",       "schema": "metric", "params": {"field": "@timestamp"}},
            {"id": "4", "type": "max",       "schema": "metric", "params": {"field": "uptime_seconds"}},
        ],
        "params": {"showTotal": False, "sort": {"columnIndex": None, "direction": None}, "totalFunc": "sum"},
    }


def _vs_uptime_bar(title: str) -> dict:
    """Horizontal bar: max uptime per MCP server."""
    return {
        "title": title,
        "type": "horizontal_bar",
        "aggs": [
            {"id": "1", "type": "max",   "schema": "metric", "params": {"field": "uptime_seconds"}},
            {"id": "2", "type": "terms", "schema": "group",  "params": {"field": "system.keyword", "size": 10, "order": "desc"}},
        ],
        "params": {
            "addLegend": True,
            "addTooltip": True,
            "addTimeMarker": False,
            "categoryAxes": [{"position": "left", "scale": {"type": "linear"}}],
        },
    }


def _vs_hb_rate_bar(title: str) -> dict:
    """Bar: total heartbeat count per system (proxy for rate)."""
    return {
        "title": title,
        "type": "histogram",
        "aggs": [
            {"id": "1", "type": "count", "schema": "metric", "params": {}},
            {
                "id": "2", "type": "terms", "schema": "segment",
                "params": {"field": "system.keyword", "size": 10, "order": "desc"},
            },
        ],
        "params": {
            "addLegend": True,
            "addTooltip": True,
            "addTimeMarker": True,
            "mode": "stacked",
            "scale": "linear",
            "categoryAxes": [{"position": "bottom", "scale": {"type": "linear"}}],
        },
    }


def _vs_log_count_bar(title: str) -> dict:
    return {
        "title": title,
        "type": "histogram",
        "aggs": [
            {"id": "1", "type": "count",  "schema": "metric",  "params": {}},
            {"id": "2", "type": "terms",  "schema": "segment", "params": {"field": "system.keyword", "size": 20, "order": "desc"}},
            {"id": "3", "type": "terms",  "schema": "group",   "params": {"field": "level.keyword",  "size": 3}},
        ],
        "params": {
            "addLegend": True, "addTimeMarker": False, "addTooltip": True,
            "mode": "stacked", "scale": "linear",
            "categoryAxes": [{"position": "bottom", "scale": {"type": "linear"}}],
        },
    }


def _vs_log_table(title: str) -> dict:
    return {
        "title": title,
        "type": "table",
        "aggs": [
            {"id": "1", "type": "count", "schema": "metric",  "params": {}},
            {"id": "2", "type": "terms", "schema": "bucket",  "params": {"field": "system.keyword", "size": 20}},
            {"id": "3", "type": "terms", "schema": "bucket",  "params": {"field": "level.keyword",  "size": 3}},
            {"id": "4", "type": "terms", "schema": "bucket",  "params": {"field": "action.keyword", "size": 10}},
        ],
        "params": {"showTotal": False},
    }


# ── Dashboard 1: All-services heartbeat overview ───────────────────────────────

def create_heartbeat_dashboard() -> None:
    print("\n── All-services heartbeat dashboard ────────────────────")
    _vis("vis-hb-active",       "Active services",          _vs_cardinality_metric("Active services", "system.keyword"), DV_HEARTBEATS)
    _vis("vis-hb-timeseries",   "Heartbeats/min by service", _vs_timeseries("Heartbeats/min by service", DV_HEARTBEATS), DV_HEARTBEATS)
    _vis("vis-hb-status-table", "Latest status per service", _vs_status_table("Latest status per service"), DV_HEARTBEATS)
    _vis("vis-hb-mcp-table",    "MCP server status",        _vs_status_table("MCP server status", ".*-mcp"), DV_HEARTBEATS)

    panels = [
        _panel("vis-hb-active",       0,  0, 10, 8,  "p1"),
        _panel("vis-hb-timeseries",   10, 0, 38, 16, "p2"),
        _panel("vis-hb-status-table", 0,  8, 24, 20, "p3"),
        _panel("vis-hb-mcp-table",    24, 8, 24, 20, "p4"),
    ]
    refs = [
        {"type": "visualization", "id": "vis-hb-active",       "name": "panel_p1"},
        {"type": "visualization", "id": "vis-hb-timeseries",   "name": "panel_p2"},
        {"type": "visualization", "id": "vis-hb-status-table", "name": "panel_p3"},
        {"type": "visualization", "id": "vis-hb-mcp-table",    "name": "panel_p4"},
    ]
    _dashboard(DASH_ID_HEARTBEATS, "All Services — Heartbeat Monitor",
               "Live heartbeat status for all services and MCP servers",
               panels, refs, DV_HEARTBEATS, refresh_ms=10000)


# ── Dashboard 2: Dedicated MCP servers ────────────────────────────────────────

def create_mcp_dashboard() -> None:
    print("\n── Dedicated MCP servers dashboard ────────────────────")
    MCP_KQL = "system.keyword: *-mcp"

    # KPIs (heartbeats-*)
    _vis("vis-mcp-active",    "Active MCP servers",
         _vs_cardinality_metric("Active MCP servers", "system.keyword"), DV_HEARTBEATS, MCP_KQL)
    _vis("vis-mcp-hb-total",  "Total MCP heartbeats",
         _vs_count_metric("Total MCP heartbeats"), DV_HEARTBEATS, MCP_KQL)
    _vis("vis-mcp-hb-pie",    "MCP heartbeat status split",
         _vs_status_pie("MCP heartbeat status split"), DV_HEARTBEATS, MCP_KQL)

    # Heartbeat timeseries per MCP server
    _vis("vis-mcp-timeseries", "MCP heartbeats/min per server",
         _vs_timeseries("MCP heartbeats/min per server", DV_HEARTBEATS, split_size=10),
         DV_HEARTBEATS, MCP_KQL)

    # Status + uptime table
    _vis("vis-mcp-status-table", "MCP server status & uptime",
         _vs_status_table("MCP server status & uptime", ".*-mcp"), DV_HEARTBEATS, MCP_KQL)

    # Uptime horizontal bar
    _vis("vis-mcp-uptime-bar", "MCP server uptime (seconds)",
         _vs_uptime_bar("MCP server uptime (seconds)"), DV_HEARTBEATS, MCP_KQL)

    # Heartbeat rate bar
    _vis("vis-mcp-hb-rate", "MCP heartbeat volume per server",
         _vs_hb_rate_bar("MCP heartbeat volume per server"), DV_HEARTBEATS, MCP_KQL)

    # Layout (48-wide grid):
    # Row 0  h=8 : [active: 12] [total hb: 12] [pie: 24]
    # Row 8  h=16: [timeseries: 48]
    # Row 24 h=18: [status+uptime table: 24] [uptime bar: 24]
    # Row 42 h=16: [hb rate bar: 48]
    panels = [
        _panel("vis-mcp-active",       0,  0, 12,  8, "m1"),
        _panel("vis-mcp-hb-total",     12, 0, 12,  8, "m2"),
        _panel("vis-mcp-hb-pie",       24, 0, 24,  8, "m4"),
        _panel("vis-mcp-timeseries",   0,  8, 48, 16, "m5"),
        _panel("vis-mcp-status-table", 0,  24, 24, 18, "m6"),
        _panel("vis-mcp-uptime-bar",   24, 24, 24, 18, "m7"),
        _panel("vis-mcp-hb-rate",      0,  42, 48, 16, "m8"),
    ]
    refs = [
        {"type": "visualization", "id": "vis-mcp-active",       "name": "panel_m1"},
        {"type": "visualization", "id": "vis-mcp-hb-total",     "name": "panel_m2"},
        {"type": "visualization", "id": "vis-mcp-hb-pie",       "name": "panel_m4"},
        {"type": "visualization", "id": "vis-mcp-timeseries",   "name": "panel_m5"},
        {"type": "visualization", "id": "vis-mcp-status-table", "name": "panel_m6"},
        {"type": "visualization", "id": "vis-mcp-uptime-bar",   "name": "panel_m7"},
        {"type": "visualization", "id": "vis-mcp-hb-rate",      "name": "panel_m8"},
    ]
    _dashboard(DASH_ID_MCP, "MCP Dashboards",
               "Dedicated dashboard: heartbeat health, uptime, and status for all MCP servers",
               panels, refs, DV_HEARTBEATS, refresh_ms=10000)
    _dashboard(DASH_ID_MCP_ALIAS, "MCP Dashboards",
               "Dedicated dashboard: heartbeat health, uptime, and status for all MCP servers",
               panels, refs, DV_HEARTBEATS, refresh_ms=10000)


# ── Dashboard 3: Service logs ──────────────────────────────────────────────────

def create_logs_dashboard() -> None:
    print("\n── Service logs dashboard ──────────────────────────────")
    _vis("vis-log-count", "Log count by service & level", _vs_log_count_bar("Log count by service & level"), DV_LOGS)
    _vis("vis-log-table", "Recent log entries",           _vs_log_table("Recent log entries"),               DV_LOGS)

    panels = [
        _panel("vis-log-count", 0,  0, 48, 20, "q1"),
        _panel("vis-log-table", 0, 20, 48, 20, "q2"),
    ]
    refs = [
        {"type": "visualization", "id": "vis-log-count", "name": "panel_q1"},
        {"type": "visualization", "id": "vis-log-table", "name": "panel_q2"},
    ]
    _dashboard(DASH_ID_LOGS, "Service Logs",
               "Log volume and recent entries per service",
               panels, refs, DV_LOGS, refresh_ms=30000)


if __name__ == "__main__":
    import urllib3
    urllib3.disable_warnings()

    wait_for_kibana()
    create_data_views()
    create_heartbeat_dashboard()
    create_mcp_dashboard()
    create_logs_dashboard()

    print(f"""
Done. Dashboard URLs:
  All Services Heartbeats : {KIBANA_URL}/app/dashboards#/view/{DASH_ID_HEARTBEATS}
  MCP Servers (dedicated) : {KIBANA_URL}/app/dashboards#/view/{DASH_ID_MCP}
  MCP Dashboards          : {KIBANA_URL}/app/dashboards#/view/{DASH_ID_MCP_ALIAS}
  Service Logs            : {KIBANA_URL}/app/dashboards#/view/{DASH_ID_LOGS}
""", flush=True)
