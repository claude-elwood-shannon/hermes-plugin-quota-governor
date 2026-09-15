#!/usr/bin/python3.12
"""generate_dashboard.py — native OpenObserve "Hermes Overview" dashboard.

Generates the v8 dashboard JSON consumed by the OpenObserve HTTP API
(POST /api/<org>/dashboards to create, PUT /api/<org>/dashboards/<id> to
update; the API returns the server-normalized document with the id).

WIRE FORMAT (verified live against the deployed image, 2026-09-15 — the
serde casing is PER LEVEL, matching openobserve v8 rust structs):
  - Dashboard/Tab/Panel/Query/AxisItem/GroupType: camelCase
    (dashboardId, tabId, queryType, customQuery, functionName, filterType...)
  - PanelFields / PanelConfig / QueryConfig: snake_case, no rename_all
    (stream_type, show_legends, legends_position, promql_legend...)
  - fields.filter: GroupType camelCase; an empty group = no filter:
    {"filterType": "group", "logicalOperator": "AND", "conditions": []}

Panels cover the signal families of the Hermes observability plane:
  board KPIs, tasks by status, objective spend, cron states, efficiency,
  NanoGPT balance, GPU temp, OTLP spans, log streams (watchdog/vllm/eff).

Data sources (org `default`):
  - metrics hermes_*   — Prometheus remote_write (t_9e457672)
  - logs hermes-*      — dual log shipper (t_9e457672)
  - traces `default`   — OBJ-27 F4 OTLP exporter (t_88753960)

Usage:
  python3 generate_dashboard.py [dashboardId] > hermes-overview.json
"""
import json
import sys


def axis(label, alias):
    return {"label": label, "alias": alias, "column": None, "type": None,
            "color": None, "functionName": None, "sortBy": None,
            "args": None, "isDerived": None, "havingConditions": None,
            "treatAsNonTimestamp": None, "showFieldAsJson": None,
            "rawQuery": None}


def fields(stream, stream_type, x, y, breakdown=None):
    return {
        "stream": stream,
        "stream_type": stream_type,
        "x": x,
        "y": y,
        "z": [],
        "breakdown": breakdown,
        "filter": {"filterType": "group", "logicalOperator": "AND",
                   "conditions": []},
        "promql_labels": [],
        "promql_operations": [],
    }


def panel(pid, ptype, title, desc, x, y, w, h, i, sql, stream, stream_type,
          x_axis, y_axis, breakdown=None, unit=None):
    cfg = {
        "show_legends": ptype not in ("metric", "table"),
        "legends_position": "bottom",
        "unit": unit,
        "decimals": 2,
    }
    return {
        "id": pid,
        "type": ptype,
        "title": title,
        "description": desc,
        "config": cfg,
        "query_type": "",
        "queries": [{
            "query": sql,
            "vrlFunctionQuery": None,
            "customQuery": True,
            "fields": fields(stream, stream_type, x_axis, y_axis, breakdown),
            "config": {"promql_legend": "", "step_value": None,
                       "layer_type": None, "weight_fixed": None,
                       "limit": None, "min": None, "max": None,
                       "time_shift": None, "query_label": None},
            "joins": None,
            "tabName": None,
        }],
        "layout": {"x": x, "y": y, "w": w, "h": h, "i": i},
    }


def hist(series_alias, *selects, stream=None, interval="5 minutes",
         group=None, where=None):
    """Histogram-aggregation SQL over a metrics/log stream."""
    cols = ", ".join(selects) if selects else "MAX(value) AS value"
    sql = (f"SELECT histogram(_timestamp, '{interval}') AS {series_alias}, "
           f"{cols} FROM \"{stream}\"")
    if where:
        sql += f" WHERE {where}"
    if group:
        sql += f" GROUP BY {series_alias}, {group} ORDER BY {series_alias}"
    else:
        sql += f" GROUP BY {series_alias} ORDER BY {series_alias}"
    return sql


def build(dashboard_id=""):
    panels = []
    i = 0

    def add(ptype, title, desc, x, y, w, h, sql, stream, stype, xa, ya,
            bd=None, unit=None):
        nonlocal i
        panels.append(panel(f"p{i+1:02d}", ptype, title, desc, x, y, w, h, i,
                            sql, stream, stype, xa, ya, bd, unit))
        i += 1

    # ---- row 0: KPI stats (board / supply / bridge / quota) ----
    kpis = [
        ("Tasks done", "hermes_tasks_done",
         "SELECT MAX(value) AS tasks_done FROM \"hermes_tasks_done\""),
        ("Tasks running", "hermes_tasks_running",
         "SELECT MAX(value) AS tasks_running FROM \"hermes_tasks_running\""),
        ("Tasks triage", "hermes_tasks_triage",
         "SELECT MAX(value) AS tasks_triage FROM \"hermes_tasks_triage\""),
        ("Supply ratio", "hermes_supply_ratio",
         "SELECT MAX(value) AS supply_ratio FROM \"hermes_supply_ratio\""),
        ("Bridge up", "hermes_bridge_up",
         "SELECT MAX(value) AS bridge_up FROM \"hermes_bridge_up\""),
        ("Session quota %", "hermes_quota_session_percent",
         "SELECT MAX(value) AS session_quota_pct FROM \"hermes_quota_session_percent\""),
    ]
    for n, (title, stream, sql) in enumerate(kpis):
        add("metric", title, "t_88753960 Hermes Overview", (n % 6) * 4, 0,
            4, 4, sql, stream, "metrics",
            [], [axis("", title.split()[0].lower())])

    # ---- row 1: tasks by status + GPU temp ----
    add("area", "Tasks by status",
        "Kanban tasks per status over time (bridge metrics)",
        0, 4, 6, 7,
        hist("t", "status, MAX(value) AS v", stream="hermes_tasks",
             group="status"),
        "hermes_tasks", "metrics", [axis("t", "t")],
        [axis("v", "v")], [axis("status", "status")])
    add("area", "GPU temperature (C)",
        "GPU temp scraped from bridge health", 6, 4, 6, 7,
        hist("t", "MAX(value) AS gpu_temp_c", stream="hermes_gpu_temp_c",
             interval="15 minutes"),
        "hermes_gpu_temp_c", "metrics", [axis("t", "t")],
        [axis("gpu_temp_c", "gpu_temp_c")], unit="celsius")

    # ---- row 2: objective spend + cron states ----
    add("area", "Objective spend today (USD)",
        "hermes_objective_spent_today by objective",
        0, 11, 6, 7,
        hist("t", "objective, MAX(value) AS spent_usd",
             stream="hermes_objective_spent_today", group="objective"),
        "hermes_objective_spent_today", "metrics", [axis("t", "t")],
        [axis("spent_usd", "spent_usd")], [axis("objective", "objective")],
        unit="USD")
    add("area", "Cron states",
        "cron-health-check states (ok/dead/zombie)",
        6, 11, 6, 7,
        hist("t", "state, MAX(value) AS crons", stream="hermes_crons",
             interval="15 minutes", group="state"),
        "hermes_crons", "metrics", [axis("t", "t")], [axis("crons", "crons")],
        [axis("state", "state")])

    # ---- row 3: efficiency + nanogpt balance ----
    add("area", "Efficiency ratio (24h)",
        "verified tasks / spend from the bridge",
        0, 18, 6, 6,
        hist("t", "MAX(value) AS efficiency_ratio",
             stream="hermes_efficiency_ratio", interval="15 minutes"),
        "hermes_efficiency_ratio", "metrics", [axis("t", "t")],
        [axis("efficiency_ratio", "efficiency_ratio")])
    add("area", "NanoGPT balance (USD)",
        "account balance scraped by the bridge",
        6, 18, 6, 6,
        hist("t", "MAX(value) AS balance_usd",
             stream="hermes_quota_nanogpt_balance_usd",
             interval="15 minutes"),
        "hermes_quota_nanogpt_balance_usd", "metrics", [axis("t", "t")],
        [axis("balance_usd", "balance_usd")], unit="USD")

    # ---- row 4: traces (OTLP) ----
    add("area", "OTLP spans / 15m",
        "quota-governor consumption spans in OpenObserve (stream `default`)",
        0, 24, 6, 6,
        "SELECT histogram(_timestamp, '15 minutes') AS t, COUNT(*) AS spans "
        "FROM \"default\" GROUP BY t ORDER BY t",
        "default", "traces", [axis("t", "t")], [axis("spans", "spans")])
    add("bar", "Trace events by objective",
        "house.objective attribute of exported spans",
        6, 24, 6, 6,
        "SELECT house_objective, COUNT(*) AS events FROM \"default\" "
        "GROUP BY house_objective",
        "default", "traces", [axis("house_objective", "objective")],
        [axis("events", "events")])

    # ---- row 5: logs ----
    add("area", "Watchdog ticks / 15m",
        "kanban-watchdog ticks shipped by the log shipper",
        0, 30, 4, 6,
        "SELECT histogram(_timestamp, '15 minutes') AS t, COUNT(*) AS ticks "
        "FROM \"hermes_watchdog\" GROUP BY t ORDER BY t",
        "hermes_watchdog", "logs", [axis("t", "t")], [axis("ticks", "ticks")])
    add("area", "vLLM invocations / 15m",
        "vllm-invoke.jsonl lines shipped",
        4, 30, 4, 6,
        "SELECT histogram(_timestamp, '15 minutes') AS t, COUNT(*) AS calls "
        "FROM \"hermes_vllm\" GROUP BY t ORDER BY t",
        "hermes_vllm", "logs", [axis("t", "t")], [axis("calls", "calls")])
    add("table", "Efficiency verdicts (latest)",
        "efficiency-ratio.log tail",
        8, 30, 4, 6,
        "SELECT message FROM \"hermes_efficiency\" ORDER BY _timestamp DESC "
        "LIMIT 20",
        "hermes_efficiency", "logs", [axis("message", "message")],
        [axis("message", "message")])

    return {
        "version": 8,
        "dashboardId": dashboard_id,
        "title": "Hermes Overview",
        "description": ("Unified Hermes plane: board, budget, GPU, cron "
                        "health, OTLP traces (t_88753960)"),
        "role": "",
        "owner": "",
        "created": "2026-09-15T00:00:00Z",
        "tabs": [{"tabId": "tab-1", "name": "Overview", "panels": panels}],
    }


if __name__ == "__main__":
    print(json.dumps(build(sys.argv[1] if len(sys.argv) > 1 else ""),
                     indent=1))
