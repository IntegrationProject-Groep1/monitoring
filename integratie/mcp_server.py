"""
Monitoring MCP Server — full read access to the ELK stack.

Indices (date-based, queried with wildcards):
  heartbeats-YYYY.MM.dd        fields: system, status, uptime_seconds, @timestamp
  logs-YYYY.MM.dd              fields: system, level, action, log_message, @timestamp
  heartbeats-quarantine-*      malformed heartbeats
  logs-quarantine-*            malformed log entries
  reports-YYYY.MM.dd           fields: report_date, generated_at, overall_health, systems_down

Run standalone:
    python mcp_server.py
or via fastmcp:
    fastmcp run mcp_server.py:mcp --transport streamable-http --port 8005

Environment variables:
    ES_HOST          Elasticsearch URL (default: http://localhost:9200)
    ES_ADMIN_USER    Elasticsearch username (default: elastic)
    ES_ADMIN_PASS    Elasticsearch password
    PORT             HTTP port to listen on (default: 8005)
"""
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

import httpx
from fastmcp import FastMCP
from pydantic import Field

_PAYMENT_AMOUNT_RE = re.compile(
    r"€\s*([0-9]+(?:[.,][0-9]{1,2})?)|([0-9]+(?:[.,][0-9]{1,2})?)\s*(?:EUR|eur)\b"
)

mcp = FastMCP("monitoring")

_ES_URL  = os.getenv("ES_HOST", "http://localhost:9200")
_ES_USER = os.getenv("ES_ADMIN_USER", "elastic")
_ES_PASS = os.getenv("ES_ADMIN_PASS", "")

_auth = (_ES_USER, _ES_PASS) if _ES_PASS else None
_http = httpx.AsyncClient(timeout=15.0, auth=_auth)

# Known systems (from logstash contract)
HEARTBEAT_SYSTEMS = {"planning", "crm", "kassa", "facturatie", "monitoring", "frontend", "mailing", "identity-service"}
LOG_SYSTEMS       = {"crm", "kassa", "facturatie", "frontend", "planning", "mailing", "identity-service"}
LOG_LEVELS        = {"info", "warning", "error"}
LOG_ACTIONS       = {"registration", "user", "payment", "invoice", "session", "calendar",
                     "email", "wallet", "refund", "identity", "xml_validation", "system_error", "badge"}
BUSINESS_ACTIONS  = {"registration", "payment", "invoice", "badge", "email"}

# Heartbeats expected per day (1 per second × 86400 seconds)
EXPECTED_HB_PER_DAY = 86400

HEARTBEATS_IDX  = "heartbeats-*"
LOGS_IDX        = "logs-*"
HB_QUARANTINE   = "heartbeats-quarantine-*"
LOG_QUARANTINE  = "logs-quarantine-*"
REPORTS_IDX     = "reports-*"


# ─────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────

def _err(msg: str, **extra) -> dict:
    return {"error": f"Elasticsearch unavailable: {msg}", **extra}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _search(index: str, body: dict) -> dict:
    resp = await _http.post(f"{_ES_URL}/{index}/_search", json=body)
    resp.raise_for_status()
    return resp.json()


async def _count(index: str, query: dict) -> int:
    resp = await _http.post(f"{_ES_URL}/{index}/_count", json={"query": query})
    resp.raise_for_status()
    return resp.json().get("count", 0)


def _hits(result: dict) -> list[dict]:
    return [h["_source"] for h in result.get("hits", {}).get("hits", [])]


def _buckets(result: dict, *path: str) -> list[dict]:
    node = result.get("aggregations", {})
    for key in path:
        node = node.get(key, {})
    return node.get("buckets", [])


def _compute_health_score(availability: float, error_density: float) -> float:
    availability_component = (availability / 100.0) * 10.0
    error_component        = max(0.0, 10.0 - (error_density / 10.0))
    return round(availability_component * 0.7 + error_component * 0.3, 1)


# ─────────────────────────────────────────────
#  SERVICE HEALTH / HEARTBEATS
# ─────────────────────────────────────────────

@mcp.tool()
async def get_service_status() -> dict[str, Any]:
    """
    Current online/offline status of every integration service based on their
    most recent heartbeat. Services with no heartbeat in the last 10 seconds
    are marked offline.
    """
    body = {
        "size": 0,
        "query": {"match_all": {}},
        "aggs": {
            "systems": {
                "terms": {"field": "system", "size": 50},
                "aggs": {
                    "latest": {
                        "top_hits": {
                            "size": 1,
                            "sort": [{"@timestamp": {"order": "desc"}}],
                            "_source": ["system", "status", "uptime_seconds", "@timestamp"],
                        }
                    }
                },
            }
        },
    }
    try:
        result = await _search(HEARTBEATS_IDX, body)
        now    = datetime.now(timezone.utc)
        services = []
        seen     = set()
        for bucket in _buckets(result, "systems"):
            hits = bucket["latest"]["hits"]["hits"]
            if not hits:
                continue
            src  = hits[0]["_source"]
            name = src.get("system", bucket["key"])
            seen.add(name)
            ts   = src.get("@timestamp", "")
            try:
                last = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                age  = (now - last).total_seconds()
                live = age <= 10
            except Exception:
                live = False
                age  = None
            services.append({
                "service":         name,
                "status":          src.get("status", "unknown"),
                "live":            live,
                "last_heartbeat":  ts,
                "seconds_since":   round(age, 1) if age is not None else None,
                "uptime_seconds":  src.get("uptime_seconds"),
            })
        # Any known system with no heartbeats at all
        for system in sorted(HEARTBEAT_SYSTEMS - seen):
            services.append({"service": system, "status": "no_data", "live": False,
                             "last_heartbeat": None, "seconds_since": None, "uptime_seconds": None})
        services.sort(key=lambda s: (not s["live"], s["service"]))
        online  = sum(1 for s in services if s["live"])
        return {"services": services, "total": len(services), "online": online, "offline": len(services) - online}
    except Exception as exc:
        return _err(str(exc), services=[])


@mcp.tool()
async def get_offline_services() -> dict[str, Any]:
    """Get all services that are currently offline or haven't sent a heartbeat within the expected interval. Returns service name, last seen timestamp, and expected heartbeat frequency. Use as a quick 'what's down right now?' check before investigating individual services."""
    result = await get_service_status()
    if "error" in result:
        return result
    offline = [s for s in result["services"] if not s["live"]]
    return {"offline_services": offline, "count": len(offline)}


@mcp.tool()
async def get_service_uptime(
    service: Annotated[str, Field(description="Service name. Valid values: 'frontend', 'crm', 'kassa', 'facturatie', 'monitoring', 'planning', 'mailing', 'identity-service'.")],
) -> dict[str, Any]:
    """Get the current continuous uptime (in seconds, hours, and days) for a specific service based on its latest heartbeat. Use to answer 'how long has [service] been running?' after checking get_service_status() for the service name."""
    body = {
        "size": 1,
        "query": {"term": {"system": service}},
        "sort": [{"@timestamp": {"order": "desc"}}],
        "_source": ["system", "status", "uptime_seconds", "@timestamp"],
    }
    try:
        result  = await _search(HEARTBEATS_IDX, body)
        sources = _hits(result)
        if not sources:
            return {"error": f"No heartbeat data found for service '{service}'"}
        src = sources[0]
        uptime = src.get("uptime_seconds", 0)
        hours, rem = divmod(uptime or 0, 3600)
        minutes, seconds = divmod(rem, 60)
        return {
            "service":        service,
            "uptime_seconds": uptime,
            "uptime_human":   f"{hours}h {minutes}m {seconds}s",
            "status":         src.get("status"),
            "last_heartbeat": src.get("@timestamp"),
        }
    except Exception as exc:
        return _err(str(exc), service=service)


@mcp.tool()
async def get_service_availability(
    service: Annotated[str, Field(description="Service name: 'frontend', 'crm', 'kassa', 'facturatie', 'monitoring', 'planning', 'mailing', 'identity-service'.")],
    hours: Annotated[int, Field(description="Lookback window in hours (default 24, max 168).")] = 24,
) -> dict[str, Any]:
    """
    Calculate availability percentage for a service over the last N hours.
    Based on actual heartbeat count vs expected (1 per second).
    """
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    body = {
        "size": 0,
        "query": {
            "bool": {
                "filter": [
                    {"term": {"system": service}},
                    {"range": {"@timestamp": {"gte": since}}},
                ]
            }
        },
    }
    if hours <= 0:
        hours = 1
    try:
        result   = await _search(HEARTBEATS_IDX, body)
        count    = result.get("hits", {}).get("total", {}).get("value", 0)
        expected = hours * 3600
        pct      = round(min(100.0, count / expected * 100.0), 2)
        return {
            "service":           service,
            "window_hours":      hours,
            "heartbeats_received": count,
            "heartbeats_expected": expected,
            "availability_pct":  pct,
        }
    except Exception as exc:
        return _err(str(exc), service=service)


@mcp.tool()
async def get_heartbeat_timeline(
    service: Annotated[str, Field(description="Service name: 'frontend', 'crm', 'kassa', 'facturatie', 'monitoring', 'planning', 'mailing', 'identity-service'.")],
    hours: Annotated[int, Field(description="Lookback window in hours (default 6, max 24). Each bucket = 1 minute.")] = 6,
) -> dict[str, Any]:
    """
    Heartbeat count per minute for a service over the last N hours.
    Useful to spot gaps in service availability.
    """
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    body = {
        "size": 0,
        "query": {
            "bool": {
                "filter": [
                    {"term": {"system": service}},
                    {"range": {"@timestamp": {"gte": since}}},
                ]
            }
        },
        "aggs": {
            "timeline": {
                "date_histogram": {
                    "field": "@timestamp",
                    "fixed_interval": "1m",
                    "min_doc_count": 0,
                }
            }
        },
    }
    try:
        result  = await _search(HEARTBEATS_IDX, body)
        buckets = _buckets(result, "timeline")
        points  = [{"timestamp": b["key_as_string"], "count": b["doc_count"]} for b in buckets]
        gaps    = [p for p in points if p["count"] == 0]
        return {
            "service":     service,
            "window_hours": hours,
            "timeline":    points,
            "gap_minutes": len(gaps),
        }
    except Exception as exc:
        return _err(str(exc), service=service)


@mcp.tool()
async def get_health_scores() -> dict[str, Any]:
    """
    Compute a health score (0–20) for every service, same algorithm as the daily report:
    70% availability + 30% inverse error density.
    """
    since_24h = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

    # heartbeat counts
    hb_body = {
        "size": 0,
        "query": {"range": {"@timestamp": {"gte": since_24h}}},
        "aggs": {"systems": {"terms": {"field": "system", "size": 50}}},
    }
    # log counts by system + level
    log_body = {
        "size": 0,
        "query": {"range": {"@timestamp": {"gte": since_24h}}},
        "aggs": {
            "systems": {
                "terms": {"field": "system", "size": 50},
                "aggs": {"levels": {"terms": {"field": "level", "size": 10}}},
            }
        },
    }
    try:
        hb_result  = await _search(HEARTBEATS_IDX, hb_body)
        log_result = await _search(LOGS_IDX, log_body)

        hb_counts: dict[str, int] = {b["key"]: b["doc_count"] for b in _buckets(hb_result, "systems")}

        log_by_system: dict[str, dict[str, int]] = {}
        for bucket in _buckets(log_result, "systems"):
            levels: dict[str, int] = {}
            for lb in _buckets({"aggregations": bucket}, "levels"):
                levels[lb["key"]] = lb["doc_count"]
            log_by_system[bucket["key"]] = levels

        all_systems = HEARTBEAT_SYSTEMS | set(hb_counts) | set(log_by_system)
        scores = []
        for system in sorted(all_systems):
            hb        = hb_counts.get(system, 0)
            avail     = round(min(100.0, hb / EXPECTED_HB_PER_DAY * 100.0), 2)
            levels    = log_by_system.get(system, {})
            total_ev  = sum(levels.values())
            errors    = levels.get("error", 0)
            err_density = round(errors / total_ev * 1000.0, 1) if total_ev > 0 else 0.0
            score     = _compute_health_score(avail, err_density)
            scores.append({
                "service":       system,
                "health_score":  score,
                "availability":  avail,
                "error_density": err_density,
                "heartbeats":    hb,
                "total_logs":    total_ev,
                "errors":        errors,
                "warnings":      levels.get("warning", 0),
            })
        scores.sort(key=lambda x: x["health_score"])
        overall = round(sum(s["health_score"] for s in scores) / len(scores), 1) if scores else 0.0
        return {"services": scores, "overall_health": overall, "window_hours": 24}
    except Exception as exc:
        return _err(str(exc), services=[])


# ─────────────────────────────────────────────
#  LOGS — RETRIEVAL
# ─────────────────────────────────────────────

@mcp.tool()
async def get_recent_logs(
    limit: Annotated[int, Field(description="Max log entries to return (default 50, max 500).")] = 50,
    level: Annotated[str | None, Field(description="Filter by level: 'info', 'warning', or 'error'. Omit for all levels.")] = None,
    service: Annotated[str | None, Field(description="Filter by service: 'frontend', 'crm', 'kassa', 'facturatie', 'monitoring', 'planning', 'mailing', 'identity-service'. Omit for all services.")] = None,
    action: Annotated[str | None, Field(description="Filter by action: 'registration', 'payment', 'invoice', 'session', 'wallet', 'refund', 'badge', 'email', 'identity', 'system_error'. Omit for all.")] = None,
) -> dict[str, Any]:
    """
    Get the most recent log entries from Elasticsearch.
    Returns: system (service name), level, log_message, action, @timestamp.
    """
    filters: list[dict] = []
    if level:
        filters.append({"term": {"level": level.lower()}})
    if service:
        filters.append({"term": {"system": service.lower()}})
    if action:
        filters.append({"term": {"action": action.lower()}})

    body = {
        "size": min(limit, 500),
        "query": {"bool": {"filter": filters}} if filters else {"match_all": {}},
        "sort": [{"@timestamp": {"order": "desc"}}],
        "_source": ["system", "level", "action", "log_message", "@timestamp"],
    }
    try:
        result = await _search(LOGS_IDX, body)
        return {"logs": _hits(result), "count": len(_hits(result))}
    except Exception as exc:
        return _err(str(exc), logs=[])


@mcp.tool()
async def get_error_logs(
    limit: Annotated[int, Field(description="Max entries (default 50).")] = 50,
    service: Annotated[str | None, Field(description="Filter by service: 'frontend', 'crm', 'kassa', 'facturatie', 'monitoring'. Omit for all.")] = None,
) -> dict[str, Any]:
    """Get the most recent error log entries, optionally filtered by service."""
    return await get_recent_logs(limit=limit, level="error", service=service)


@mcp.tool()
async def get_warning_logs(
    limit: Annotated[int, Field(description="Max entries (default 50).")] = 50,
    service: Annotated[str | None, Field(description="Filter by service: 'frontend', 'crm', 'kassa', 'facturatie', 'monitoring'. Omit for all.")] = None,
) -> dict[str, Any]:
    """Get the most recent warning log entries, optionally filtered by service."""
    return await get_recent_logs(limit=limit, level="warning", service=service)


@mcp.tool()
async def get_logs_by_service(
    service: Annotated[str, Field(description="Service name: 'frontend', 'crm', 'kassa', 'facturatie', 'monitoring', 'planning', 'mailing', 'identity-service'.")],
    limit: Annotated[int, Field(description="Max log entries (default 100).")] = 100,
) -> dict[str, Any]:
    """Get all recent log entries for a specific service."""
    return await get_recent_logs(limit=limit, service=service)


@mcp.tool()
async def get_logs_by_action(
    action: Annotated[str, Field(description="Action type to filter by. Valid values: 'registration', 'user', 'payment', 'invoice', 'session', 'calendar', 'email', 'wallet', 'refund', 'identity', 'xml_validation', 'system_error', 'badge'.")],
    limit: Annotated[int, Field(description="Max log entries (default 100).")] = 100,
) -> dict[str, Any]:
    """
    Get log entries for a specific action type.
    Raw event audit trail. For the curated human-readable activity log use crm__get_recent_tasks.
    """
    return await get_recent_logs(limit=limit, action=action)


@mcp.tool()
async def get_logs_in_timerange(
    start: Annotated[str, Field(description="Start timestamp in ISO 8601 format, e.g. '2026-05-15T08:00:00'.")],
    end: Annotated[str, Field(description="End timestamp in ISO 8601 format, e.g. '2026-05-15T18:00:00'.")],
    level: Annotated[str | None, Field(description="Filter by level: 'info', 'warning', 'error'. Omit for all.")] = None,
    service: Annotated[str | None, Field(description="Filter by service: 'frontend', 'crm', 'kassa', 'facturatie', 'monitoring'. Omit for all.")] = None,
    limit: Annotated[int, Field(description="Max log entries (default 200, max 1000).")] = 200,
) -> dict[str, Any]:
    """
    Get log entries between two timestamps.
    Optionally filter by level and/or service.
    """
    filters: list[dict] = [{"range": {"@timestamp": {"gte": start, "lte": end}}}]
    if level:
        filters.append({"term": {"level": level.lower()}})
    if service:
        filters.append({"term": {"system": service.lower()}})

    body = {
        "size": min(limit, 1000),
        "query": {"bool": {"filter": filters}},
        "sort": [{"@timestamp": {"order": "desc"}}],
        "_source": ["system", "level", "action", "log_message", "@timestamp"],
    }
    try:
        result = await _search(LOGS_IDX, body)
        return {"logs": _hits(result), "count": len(_hits(result)), "start": start, "end": end}
    except Exception as exc:
        return _err(str(exc), logs=[])


@mcp.tool()
async def search_logs(
    query: Annotated[str, Field(description="Text to search for in log_message field (full-text match). Example: 'payment failed', 'connection refused'.")],
    limit: Annotated[int, Field(description="Max results (default 50, max 200).")] = 50,
) -> dict[str, Any]:
    """Full-text search in log messages across all services and levels."""
    body = {
        "size": min(limit, 200),
        "query": {"match": {"log_message": query}},
        "sort": [{"@timestamp": {"order": "desc"}}],
        "_source": ["system", "level", "action", "log_message", "@timestamp"],
    }
    try:
        result = await _search(LOGS_IDX, body)
        return {"logs": _hits(result), "count": len(_hits(result)), "query": query}
    except Exception as exc:
        return _err(str(exc), logs=[])


# ─────────────────────────────────────────────
#  LOGS — ANALYTICS
# ─────────────────────────────────────────────

@mcp.tool()
async def get_top_errors(
    limit: Annotated[int, Field(description="Max distinct error types to return (default 20).")] = 20,
    service: Annotated[str | None, Field(description="Filter by service: 'frontend', 'crm', 'kassa', 'facturatie', 'monitoring'. Omit for all.")] = None,
    hours: Annotated[int, Field(description="Lookback window in hours (default 24).")] = 24,
) -> dict[str, Any]:
    """
    Most frequent error messages in the last N hours.
    Optionally filter by service.
    """
    since   = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    filters: list[dict] = [
        {"term":  {"level": "error"}},
        {"range": {"@timestamp": {"gte": since}}},
    ]
    if service:
        filters.append({"term": {"system": service.lower()}})

    body = {
        "size": 0,
        "query": {"bool": {"filter": filters}},
        "aggs": {
            "top_errors": {
                "terms": {"field": "action", "size": limit},
                "aggs": {
                    "sample": {
                        "top_hits": {
                            "size": 1,
                            "_source": ["log_message", "system"],
                        }
                    }
                },
            }
        },
    }
    try:
        result  = await _search(LOGS_IDX, body)
        buckets = _buckets(result, "top_errors")
        errors  = []
        for b in buckets:
            sample_hits = b.get("sample", {}).get("hits", {}).get("hits", [])
            sample_msg  = sample_hits[0]["_source"].get("log_message", "") if sample_hits else ""
            errors.append({"action": b["key"], "count": b["doc_count"], "sample_message": sample_msg})
        return {"errors": errors, "count": len(errors), "window_hours": hours}
    except Exception as exc:
        return _err(str(exc), errors=[])


@mcp.tool()
async def get_log_volume_by_service(
    hours: Annotated[int, Field(description="Lookback window in hours (default 24).")] = 24,
) -> dict[str, Any]:
    """Log count per service for the last N hours, broken down by level."""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    body  = {
        "size": 0,
        "query": {"range": {"@timestamp": {"gte": since}}},
        "aggs": {
            "systems": {
                "terms": {"field": "system", "size": 50},
                "aggs": {"levels": {"terms": {"field": "level", "size": 5}}},
            }
        },
    }
    try:
        result = await _search(LOGS_IDX, body)
        rows   = []
        for b in _buckets(result, "systems"):
            levels: dict[str, int] = {lb["key"]: lb["doc_count"] for lb in _buckets({"aggregations": b}, "levels")}
            rows.append({
                "service": b["key"],
                "total":   b["doc_count"],
                "info":    levels.get("info", 0),
                "warning": levels.get("warning", 0),
                "error":   levels.get("error", 0),
            })
        rows.sort(key=lambda r: r["total"], reverse=True)
        return {"services": rows, "window_hours": hours}
    except Exception as exc:
        return _err(str(exc), services=[])


@mcp.tool()
async def get_log_volume_by_level(
    hours: Annotated[int, Field(description="Lookback window in hours (default 24).")] = 24,
) -> dict[str, Any]:
    """Overall info/warning/error distribution across all services for the last N hours."""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    body  = {
        "size": 0,
        "query": {"range": {"@timestamp": {"gte": since}}},
        "aggs": {"levels": {"terms": {"field": "level", "size": 10}}},
    }
    try:
        result  = await _search(LOGS_IDX, body)
        buckets = _buckets(result, "levels")
        levels  = {b["key"]: b["doc_count"] for b in buckets}
        total   = sum(levels.values())
        return {
            "total":   total,
            "info":    levels.get("info", 0),
            "warning": levels.get("warning", 0),
            "error":   levels.get("error", 0),
            "error_pct": round(levels.get("error", 0) / total * 100, 1) if total else 0,
            "window_hours": hours,
        }
    except Exception as exc:
        return _err(str(exc))


@mcp.tool()
async def get_log_volume_by_action(
    hours: Annotated[int, Field(description="Lookback window in hours (default 24).")] = 24,
) -> dict[str, Any]:
    """Log count per action type (registration, payment, invoice, etc.) for the last N hours."""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    body  = {
        "size": 0,
        "query": {"range": {"@timestamp": {"gte": since}}},
        "aggs": {"actions": {"terms": {"field": "action", "size": 50}}},
    }
    try:
        result  = await _search(LOGS_IDX, body)
        buckets = _buckets(result, "actions")
        actions = [{"action": b["key"], "count": b["doc_count"]} for b in buckets]
        actions.sort(key=lambda a: a["count"], reverse=True)
        return {"actions": actions, "window_hours": hours}
    except Exception as exc:
        return _err(str(exc), actions=[])


@mcp.tool()
async def get_error_rate_per_service(
    hours: Annotated[int, Field(description="Lookback window in hours (default 24, max 168).")] = 24,
) -> dict[str, Any]:
    """
    Error rate (errors / total logs × 100%) per service for the last N hours, sorted highest first.
    Use to identify which service generates the most errors proportionally.
    For raw counts use get_log_volume_by_service; for sudden spikes use get_error_spikes.
    """
    result = await get_log_volume_by_service(hours=hours)
    if "error" in result:
        return result
    rows = []
    for s in result["services"]:
        total = s["total"]
        rate  = round(s["error"] / total * 100.0, 2) if total > 0 else 0.0
        rows.append({**s, "error_rate_pct": rate})
    rows.sort(key=lambda r: r["error_rate_pct"], reverse=True)
    return {"services": rows, "window_hours": hours}


@mcp.tool()
async def get_log_spike_detection(
    hours: Annotated[int, Field(description="Window size in hours to compare against 7-day trailing average (default 1, max 24).")] = 1,
) -> dict[str, Any]:
    """
    Compare total log volume in the last N hours against the 7-day trailing average per service.
    Returns trend percentage per service sorted by largest deviation.
    Use to spot anomalous log bursts from any cause (not just errors).
    For error-only spikes use get_error_spikes instead.
    """
    now        = datetime.now(timezone.utc)
    since_now  = (now - timedelta(hours=hours)).isoformat()
    since_week = (now - timedelta(days=7)).isoformat()

    def _vol_body(gte: str, lte: str | None = None) -> dict:
        rng: dict = {"gte": gte}
        if lte:
            rng["lte"] = lte
        return {
            "size": 0,
            "query": {"range": {"@timestamp": rng}},
            "aggs": {"systems": {"terms": {"field": "system", "size": 50}}},
        }

    try:
        recent_res = await _search(LOGS_IDX, _vol_body(since_now))
        week_res   = await _search(LOGS_IDX, _vol_body(since_week, since_now))

        recent: dict[str, int] = {b["key"]: b["doc_count"] for b in _buckets(recent_res, "systems")}
        week_total: dict[str, int] = {b["key"]: b["doc_count"] for b in _buckets(week_res, "systems")}

        results = []
        for system in set(recent) | set(week_total):
            cur = recent.get(system, 0)
            trail_avg = week_total.get(system, 0) / (7 * 24 / hours) if hours > 0 else 0
            if trail_avg > 0:
                pct = round((cur / trail_avg - 1.0) * 100.0)
                trend = f"+{pct}%" if pct >= 0 else f"{pct}%"
            else:
                trend = "+100%" if cur > 0 else "0%"
            results.append({"service": system, "current": cur, "trailing_avg": round(trail_avg, 1), "trend": trend})
        results.sort(key=lambda r: abs(r["current"] - r["trailing_avg"]), reverse=True)
        return {"services": results, "window_hours": hours}
    except Exception as exc:
        return _err(str(exc), services=[])


# ─────────────────────────────────────────────
#  BUSINESS METRICS
# ─────────────────────────────────────────────

@mcp.tool()
async def get_business_metrics(
    hours: Annotated[int, Field(description="Lookback window in hours (default 24).")] = 24,
) -> dict[str, Any]:
    """
    Platform-wide business event counts for the last N hours:
    registrations, payments, invoices, badge scans, mailings sent.

    Event COUNTS only (how many payments were logged), not financial totals.
    For invoiced revenue amounts use `facturatie__get_revenue_summary`;
    for live POS sales use `kassa__get_sales_summary`.
    """
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    body  = {
        "size": 0,
        "query": {
            "bool": {
                "filter": [
                    {"terms": {"action": list(BUSINESS_ACTIONS)}},
                    {"term":  {"level": "info"}},
                    {"range": {"@timestamp": {"gte": since}}},
                ]
            }
        },
        "aggs": {"actions": {"terms": {"field": "action", "size": 20}}},
    }
    try:
        result  = await _search(LOGS_IDX, body)
        counts  = {b["key"]: b["doc_count"] for b in _buckets(result, "actions")}
        return {
            "window_hours":  hours,
            "registrations": counts.get("registration", 0),
            "payments":      counts.get("payment", 0),
            "invoices":      counts.get("invoice", 0),
            "badge_scans":   counts.get("badge", 0),
            "emails_sent":   counts.get("email", 0),
        }
    except Exception as exc:
        return _err(str(exc))


@mcp.tool()
async def get_business_metrics_per_service(
    hours: Annotated[int, Field(description="Lookback window in hours (default 24).")] = 24,
) -> dict[str, Any]:
    """
    Business action counts broken down per source service for the last N hours.

    Event COUNTS per system, not financial totals. For revenue use Facturatie or Kassa.
    """
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    body  = {
        "size": 0,
        "query": {
            "bool": {
                "filter": [
                    {"terms": {"action": list(BUSINESS_ACTIONS)}},
                    {"range": {"@timestamp": {"gte": since}}},
                ]
            }
        },
        "aggs": {
            "systems": {
                "terms": {"field": "system", "size": 20},
                "aggs": {"actions": {"terms": {"field": "action", "size": 20}}},
            }
        },
    }
    try:
        result = await _search(LOGS_IDX, body)
        rows   = []
        for b in _buckets(result, "systems"):
            actions = {ab["key"]: ab["doc_count"] for ab in _buckets({"aggregations": b}, "actions")}
            rows.append({"service": b["key"], "total": b["doc_count"], **actions})
        return {"services": rows, "window_hours": hours}
    except Exception as exc:
        return _err(str(exc), services=[])


# ─────────────────────────────────────────────
#  DAILY REPORTS
# ─────────────────────────────────────────────

@mcp.tool()
async def get_report_history(
    limit: Annotated[int, Field(description="Max reports to return (default 30, max 100).")] = 30,
) -> dict[str, Any]:
    """List the most recent daily platform health reports archived in Elasticsearch, newest first. Returns report date, overall health score, number of systems down, and archive path. Use to answer 'how was the platform doing last week?' or to find a specific day's report."""
    body = {
        "size": min(limit, 100),
        "query": {"match_all": {}},
        "sort": [{"report_date": {"order": "desc"}}],
        "_source": ["report_date", "generated_at", "overall_health", "systems_down", "storage_path"],
    }
    try:
        result  = await _search(REPORTS_IDX, body)
        reports = _hits(result)
        return {"reports": reports, "count": len(reports)}
    except Exception as exc:
        return _err(str(exc), reports=[])


@mcp.tool()
async def get_report_for_date(
    date: Annotated[str, Field(description="Date in format 'YYYY-MM-DD', e.g. '2026-05-15'.")],
) -> dict[str, Any]:
    """Get the archived daily platform health report for a specific date. Returns overall health score, systems that were down, and the archive path. Use when an admin asks about platform health on a particular day."""
    body = {
        "size": 1,
        "query": {"term": {"report_date": date}},
        "_source": ["report_date", "generated_at", "overall_health", "systems_down", "storage_path"],
    }
    try:
        result = await _search(REPORTS_IDX, body)
        hits   = _hits(result)
        if not hits:
            return {"error": f"No report found for date '{date}'"}
        return hits[0]
    except Exception as exc:
        return _err(str(exc), date=date)


@mcp.tool()
async def get_latest_report() -> dict[str, Any]:
    """Get the most recently generated daily platform report metadata."""
    body = {
        "size": 1,
        "query": {"match_all": {}},
        "sort": [{"report_date": {"order": "desc"}}],
        "_source": ["report_date", "generated_at", "overall_health", "systems_down", "storage_path"],
    }
    try:
        result = await _search(REPORTS_IDX, body)
        hits   = _hits(result)
        if not hits:
            return {"error": "No reports found in Elasticsearch"}
        return hits[0]
    except Exception as exc:
        return _err(str(exc))



# ─────────────────────────────────────────────
#  PLATFORM OVERVIEW
# ─────────────────────────────────────────────

@mcp.tool()
async def get_error_spikes(
    hours: Annotated[int, Field(description="Window size in hours to compare (default 1). Compares last N hours vs previous N hours.")] = 1,
    threshold_pct: Annotated[int, Field(description="Minimum % increase to flag as a spike (default 150 = 2.5× more errors than the previous window).")] = 150,
) -> dict[str, Any]:
    """
    Detect services where the error rate in the last `hours` is at least
    `threshold_pct`% higher than the preceding equivalent window.
    Returns only services with a spike — empty list means all clear.
    Useful for catching newly introduced bugs without scanning all logs.
    threshold_pct=150 means 150% higher (2.5× as many errors), not 150% total.
    """
    if hours <= 0:
        hours = 1
    now     = datetime.now(timezone.utc)
    prev_start = (now - timedelta(hours=hours * 2)).isoformat()
    curr_start = (now - timedelta(hours=hours)).isoformat()

    def _err_body(gte: str, lte: str | None = None) -> dict:
        rng: dict = {"gte": gte}
        if lte:
            rng["lte"] = lte
        return {
            "size": 0,
            "query": {"bool": {"filter": [
                {"term": {"level": "error"}},
                {"range": {"@timestamp": rng}},
            ]}},
            "aggs": {"systems": {"terms": {"field": "system", "size": 50}}},
        }

    try:
        import asyncio
        prev_res, curr_res = await asyncio.gather(
            _search(LOGS_IDX, _err_body(prev_start, curr_start)),
            _search(LOGS_IDX, _err_body(curr_start)),
        )
        prev: dict[str, int] = {b["key"]: b["doc_count"] for b in _buckets(prev_res, "systems")}
        curr: dict[str, int] = {b["key"]: b["doc_count"] for b in _buckets(curr_res, "systems")}

        spikes = []
        for system in set(curr) | set(prev):
            c = curr.get(system, 0)
            p = prev.get(system, 0)
            if p == 0:
                pct_increase = 100 if c > 0 else 0
            else:
                pct_increase = round((c - p) / p * 100)
            if pct_increase >= threshold_pct:
                spikes.append({
                    "service": system,
                    "errors_current_window": c,
                    "errors_previous_window": p,
                    "increase_pct": pct_increase,
                })
        spikes.sort(key=lambda s: s["increase_pct"], reverse=True)
        return {
            "spikes": spikes,
            "count": len(spikes),
            "window_hours": hours,
            "threshold_pct": threshold_pct,
            "all_clear": len(spikes) == 0,
        }
    except Exception as exc:
        return _err(str(exc), spikes=[])


@mcp.tool()
async def get_platform_health_overview() -> dict[str, Any]:
    """
    Full admin dashboard: service health scores, top errors, business metrics
    for the last 24 hours. The single most useful tool for a quick platform check.
    """
    health, errors, business, volume = await _gather(
        get_health_scores(),
        get_top_errors(limit=5, hours=24),
        get_business_metrics(hours=24),
        get_log_volume_by_level(hours=24),
    )
    return {
        "timestamp":       _now_iso(),
        "overall_health":  health.get("overall_health"),
        "services":        health.get("services", []),
        "top_errors":      errors.get("errors", []),
        "business":        {k: v for k, v in business.items() if k != "window_hours"},
        "log_volume":      {k: v for k, v in volume.items() if k != "window_hours"},
        "window_hours":    24,
    }


async def _gather(*coros):
    """Run coroutines concurrently; returns results in order, substituting {} on failure."""
    import asyncio
    results = await asyncio.gather(*coros, return_exceptions=True)
    return [r if not isinstance(r, Exception) else {} for r in results]

if __name__ == "__main__":
    mcp.run(
        transport="streamable-http",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8005")),
    )
