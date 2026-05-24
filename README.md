# Monitoring — ELK Heartbeat & Log Stack

This repository contains the central monitoring stack for the integration project. It runs Elasticsearch, Logstash, and Kibana to ingest XML messages from RabbitMQ, validate them, and index them for operational visibility.

The stack also includes:
- a detector service that inspects heartbeats, generates alerts, and publishes daily report mailings
- a small monitoring MCP service (`integratie/mcp_server.py`) that exposes monitoring queries against Elasticsearch

## What it does

- Logstash consumes RabbitMQ queues and parses XML payloads from participating services
- Valid heartbeat and log messages are indexed into Elasticsearch
- Invalid or misrouted messages are sent to per-pipeline quarantine indices
- Kibana visualizes operational health, service availability, and log activity
- Detector publishes daily report metadata to `monitoring.reports` and can publish alerts to `monitoring.alerts`

## Architecture

- `docker-compose.yml` starts:
  - `elasticsearch`
  - `logstash`
  - `kibana`
  - `detector-service`
  - `monitoring_mcp`
- RabbitMQ is not started by the root compose file; it must already be available at `${RABBITMQ_HOST}:${RABBITMQ_PORT}`.
- `test/docker-compose.yml` includes a local RabbitMQ instance for integration testing.
- Logstash configuration lives in `monitoring_elk/logstash/pipeline/logstash.conf`.

## Startup

1. Copy environment file:

   ```bash
   cp monitoring_elk/.env.example .env
   ```

2. Fill in `ES_ADMIN_PASS`, `KIBANA_SYSTEM_PASS`, `RABBITMQMONITORING_USER`, `RABBITMQMONITORING_PASS`, and any RabbitMQ host details.

3. Start the stack:

   ```bash
   docker compose up -d
   ```

4. For a self-contained test environment with RabbitMQ, use:

   ```bash
   docker compose -f test/docker-compose.yml up -d
   ```

## RabbitMQ queues consumed by Logstash

- `heartbeat`
- `logs`
- `monitoring.user.events`
- `stats.sessions`
- `stats.consumptions`
- `stats.payments`
- `stats.refunds`

## RabbitMQ queues used by detector

- `monitoring.reports` (daily report metadata)
- `monitoring.alerts` (heartbeat alert notifications)

## Monitored teams

The accepted `source` value in the XML header differs per pipeline (per contract v2.3):

| Team | Heartbeat | Log |
|---|:---:|:---:|
| Planning (`planning`) | ✓ | ✓ |
| CRM (`crm`) | ✓ | ✓ |
| Kassa (`kassa`) | ✓ | ✓ |
| Facturatie (`facturatie`) | ✓ | ✓ |
| Frontend (`frontend`) | ✓ | ✓ |
| Mailing (`mailing`) | ✓ | ✓ |
| Monitoring (`monitoring`) | ✓ | ✓ |
| RabbitMQ (`rabbitmq`) | ✓ | — |
| Identity service (`identity-service`) | — | ✓ |

Monitoring sends both heartbeats and logs. Identity service is exempt from the heartbeat sidecar (RPC-only) but does emit logs. Anything outside the per-pipeline whitelist is sent to the quarantine index. Matching is case-insensitive — Logstash lowercases the value. Whitelists live in `monitoring_elk/logstash/pipeline/logstash.conf`.

## Ports

| Service | Host port | Usage |
|---|---|---|
| Elasticsearch REST API | `30060` | Elasticsearch API and ILM management |
| Kibana UI | `30061` | Kibana dashboard |
| Monitoring MCP | `8005` | API exposed by `monitoring_mcp` service |

## Elasticsearch Indices

| Index | Content |
|---|---|
| `heartbeats-YYYY.MM.dd` | Valid, processed heartbeats |
| `heartbeats-quarantine-YYYY.MM.dd` | Invalid XML, unknown source, bad timestamp, unsupported contract version |
| `logs-YYYY.MM.dd` | Valid, processed platform logs |
| `logs-quarantine-YYYY.MM.dd` | Invalid XML, unknown source, bad timestamp, wrong message type, unknown level, unsupported contract version |
| `reports-YYYY.MM.dd` | Daily report metadata |

## Message contracts

Both heartbeat and platform log messages use the same `<message><header><body>` envelope. The header contains:

- `message_id`
- `timestamp` (UTC ISO 8601)
- `source`
- `type` (`heartbeat`, `log`, or `send_mailing`)
- `version`

**Heartbeat body**:
- `status` (`online` / `offline`)
- `uptime` (seconds, integer, required)

**Platform log body**:
- `level` (`info`, `warning`, `error`)
- `action` (closed set of categories — see `monitoring_elk/logstash/pipeline/logstash.conf`)
- `message` (free text)

**Daily report body**:
- `type` = `send_mailing`
- `source` = `monitoring`
- `mail_type` = `daily_report`
- `template_data` contains a JSON preview
- `attachment` carries the generated PDF body

## Authentication

| Account | Usage |
|---|---|
| `elastic` | Elasticsearch admin, Kibana login |
| `kibana_system` | Kibana internal service account |
| `monitoring_rabbitmq` | RabbitMQ user for Logstash and detector publishing |

## Notes

- The `detector` service is built from `detector/detector.py` and publishes report/log XML to RabbitMQ.
- The `monitoring_mcp` service is built from `integratie/mcp_server.py` and provides query tools against Elasticsearch.
- `elastic_agent/` contains an Elastic Agent standalone config, but it is not part of the main `docker-compose.yml` stack.
