#!/usr/bin/python3.12
"""generate_dashboard.py — native OpenObserve "Hermes Overview" dashboard.

Build a dashboard JSON. The original implementation exceeded 50 lines in build(), so
it has been refactored into several <50‑line private helpers. The public
`build()` now orchestrates the helpers.
"""
import json, sys

# ---------------------------------------------------------------------------
# Lightweight primitives
# ---------------------------------------------------------------------------

def _axis(label, alias):  # <30 lines
    return {
        "label": label,
        "alias": alias,
        "column": None,
        "type": None,
        "color": None,
        "functionName": None,
        "sortBy": None,
        "args": None,
        "isDerived": None,
        "havingConditions": None,
        "treatAsNonTimestamp": None,
        "showFieldAsJson": None,
        "rawQuery": None,
    }


def _fields(stream, stream_type, x, y, breakdown=None):  # <30 lines
    return {
        "stream": stream,
        "stream_type": stream_type,
        "x": x,
        "y": y,
        "z": [],
        "breakdown": breakdown,
        "filter": {
            "filterType": "group",
            "logicalOperator": "AND",
            "conditions": [],
        },
        "promql_labels": [],
        "promql_operations": [],
    }


def _panel(pid, ptype, title, desc, x, y, w, h, i, sql, stream, stream_type, x_axis, y_axis, breakdown=None, unit=None):  # <45 lines
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
        "queries": [
            {
                "query": sql,
                "vrlFunctionQuery": None,
                "customQuery": True,
                "fields": _fields(stream, stream_type, x_axis, y_axis, breakdown),
                "config": {
                    "promql_legend": "",
                    "step_value": None,
                    "layer_type": None,
                    "weight_fixed": None,
                    "limit": None,
                    "min": None,
                    "max": None,
                    "time_shift": None,
                    "query_label": None,
                },
                "joins": None,
                "tabName": None,
            }
        ],
        "layout": {"x": x, "y": y, "w": w, "h": h, "i": i},
    }


def _hist(series_alias, *selects, stream=None, interval="5 minutes", group=None, where=None):  # <35 lines
    cols = ", ".join(selects) if selects else "MAX(value) AS value"
    sql = f"SELECT histogram(_timestamp, '{interval}') AS {series_alias}, {cols} FROM \"{stream}\""
    if where:
        sql += f" WHERE {where}"
    if group:
        sql += f" GROUP BY {series_alias}, {group} ORDER BY {series_alias}"
    else:
        sql += f" GROUP BY {series_alias} ORDER BY {series_alias}"
    return sql

# ---------------------------------------------------------------------------
# Helper that builds each row type. Each helper <50 lines.
# ---------------------------------------------------------------------------

def _build_kpis(panels, i_ref):  # <30 lines
    kpis = [
        ("Tasks done", "hermes_tasks_done", "SELECT MAX(value) AS tasks_done FROM \"hermes_tasks_done\""),
        ("Tasks running", "hermes_tasks_running", "SELECT MAX(value) AS tasks_running FROM \"hermes_tasks_running\""),
        ("Tasks triage", "hermes_tasks_triage", "SELECT MAX(value) AS tasks_triage FROM \"hermes_tasks_triage\""),
        ("Supply ratio", "hermes_supply_ratio", "SELECT MAX(value) AS supply_ratio FROM \"hermes_supply_ratio\""),
        ("Bridge up", "hermes_bridge_up", "SELECT MAX(value) AS bridge_up FROM \"hermes_bridge_up\""),
        ("Session quota %", "hermes_quota_session_percent", "SELECT MAX(value) AS session_quota_pct FROM \"hermes_quota_session_percent\"")
    ]
    for n, (title, stream, sql) in enumerate(kpis):
        panels.append(
            _panel(
                f"p{i_ref[0]+1:02d}", "metric", title, "t_88753960 Hermes Overview", (n % 6) * 4, 0,
                4, 4, i_ref[0], sql, stream, "metrics", [], [ _axis("", title.split()[0].lower())]
            )
        )
        i_ref[0] += 1


def _build_row1(panels, i_ref):  # <30 lines
    panels.append(
        _panel(
            f"p{i_ref[0]+1:02d}", "area", "Tasks by status",
            "Kanban tasks per status over time (bridge metrics)", 0, 4, 6, 7,
            i_ref[0], _hist("t", "status, MAX(value) AS v", stream="hermes_tasks", group="status"),
            "hermes_tasks", "metrics", [ _axis("t", "t") ],
            [ _axis("v", "v") ], [ _axis("status", "status")]
        )
    )
    i_ref[0] += 1
    panels.append(
        _panel(
            f"p{i_ref[0]+1:02d}", "area", "GPU temperature (C)",
            "GPU temp scraped from bridge health", 6, 4, 6, 7,
            i_ref[0], _hist("t", "MAX(value) AS gpu_temp_c", stream="hermes_gpu_temp_c", interval="15 minutes"),
            "hermes_gpu_temp_c", "metrics", [ _axis("t", "t") ],
            [ _axis("gpu_temp_c", "gpu_temp_c") ], unit="celsius"
        )
    )
    i_ref[0] += 1


def _build_row2(panels, i_ref):  # <35 lines
    panels.append(
        _panel(
            f"p{i_ref[0]+1:02d}", "area", "Objective spend today (USD)",
            "hermes_objective_spent_today by objective", 0, 11, 6, 7,
            i_ref[0], _hist("t", "objective, MAX(value) AS spent_usd", stream="hermes_objective_spent_today", group="objective"),
            "hermes_objective_spent_today", "metrics", [ _axis("t", "t") ],
            [ _axis("spent_usd", "spent_usd") ], [ _axis("objective", "objective") ], unit="USD"
        )
    )
    i_ref[0] += 1
    panels.append(
        _panel(
            f"p{i_ref[0]+1:02d}", "area", "Cron states",
            "cron-health-check states (ok/dead/zombie)", 6, 11, 6, 7,
            i_ref[0], _hist("t", "state, MAX(value) AS crons", stream="hermes_crons", interval="15 minutes", group="state"),
            "hermes_crons", "metrics", [ _axis("t", "t") ], [ _axis("crons", "crons") ], [ _axis("state", "state") ]
        )
    )
    i_ref[0] += 1


def _build_row3(panels, i_ref):  # <30 lines
    panels.append(
        _panel(
            f"p{i_ref[0]+1:02d}", "area", "Efficiency ratio (24h)",
            "verified tasks / spend from the bridge", 0, 18, 6, 6,
            i_ref[0], _hist("t", "MAX(value) AS efficiency_ratio", stream="hermes_efficiency_ratio", interval="15 minutes"),
            "hermes_efficiency_ratio", "metrics", [ _axis("t", "t") ],
            [ _axis("efficiency_ratio", "efficiency_ratio") ]
        )
    )
    i_ref[0] += 1
    panels.append(
        _panel(
            f"p{i_ref[0]+1:02d}", "area", "NanoGPT balance (USD)",
            "account balance scraped by the bridge", 6, 18, 6, 6,
            i_ref[0], _hist("t", "MAX(value) AS balance_usd", stream="hermes_quota_nanogpt_balance_usd", interval="15 minutes"),
            "hermes_quota_nanogpt_balance_usd", "metrics", [ _axis("t", "t") ],
            [ _axis("balance_usd", "balance_usd") ], unit="USD"
        )
    )
    i_ref[0] += 1


def _build_row4(panels, i_ref):  # <35 lines
    panels.append(
        _panel(
            f"p{i_ref[0]+1:02d}", "area", "OTLP spans / 15m",
            "quota-governor consumption spans in OpenObserve (stream `default`)", 0, 24, 6, 6,
            i_ref[0],
            "SELECT histogram(_timestamp, '15 minutes') AS t, COUNT(*) AS spans\nFROM \"default\" GROUP BY t ORDER BY t",
            "default", "traces", [ _axis("t", "t") ], [ _axis("spans", "spans") ]
        )
    )
    i_ref[0] += 1
    panels.append(
        _panel(
            f"p{i_ref[0]+1:02d}", "bar", "Trace events by objective",
            "house.objective attribute of exported spans", 6, 24, 6, 6,
            i_ref[0],
            "SELECT house_objective, COUNT(*) AS events FROM \"default\" \nGROUP BY house_objective",
            "default", "traces", [ _axis("house_objective", "objective") ],
            [ _axis("events", "events") ]
        )
    )
    i_ref[0] += 1


def _build_row5(panels, i_ref):  # <35 lines
    panels.append(
        _panel(
            f"p{i_ref[0]+1:02d}", "area", "Watchdog ticks / 15m",
            "kanban-watchdog ticks shipped by the log shipper", 0, 30, 4, 6,
            i_ref[0],
            "SELECT histogram(_timestamp, '15 minutes') AS t, COUNT(*) AS ticks\nFROM \"hermes_watchdog\" GROUP BY t ORDER BY t",
            "hermes_watchdog", "logs", [ _axis("t", "t") ], [ _axis("ticks", "ticks") ]
        )
    )
    i_ref[0] += 1
    panels.append(
        _panel(
            f"p{i_ref[0]+1:02d}", "area", "vLLM invocations / 15m",
            "vllm-invoke.jsonl lines shipped", 4, 30, 4, 6,
            i_ref[0],
            "SELECT histogram(_timestamp, '15 minutes') AS t, COUNT(*) AS calls\nFROM \"hermes_vllm\" GROUP BY t ORDER BY t",
            "hermes_vllm", "logs", [ _axis("t", "t") ], [ _axis("calls", "calls") ]
        )
    )
    i_ref[0] += 1
    panels.append(
        _panel(
            f"p{i_ref[0]+1:02d}", "table", "Efficiency verdicts (latest)",
            "efficiency-ratio.log tail", 8, 30, 4, 6,
            i_ref[0],
            "SELECT message FROM \"hermes_efficiency\" ORDER BY _timestamp DESC \nLIMIT 20",
            "hermes_efficiency", "logs", [ _axis("message", "message") ], [ _axis("message", "message") ]
        )
    )
    i_ref[0] += 1

# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def build(dashboard_id=""):
    panels = []
    ref = [0]
    _build_kpis(panels, ref)
    _build_row1(panels, ref)
    _build_row2(panels, ref)
    _build_row3(panels, ref)
    _build_row4(panels, ref)
    _build_row5(panels, ref)
    return {
        "version": 8,
        "dashboardId": dashboard_id,
        "title": "Hermes Overview",
        "description": ("Unified Hermes plane: board, budget, GPU, cron "
                         "health, OTLP traces (t_88753960)") ,
        "role": "",
        "owner": "",
        "created": "2026-09-15T00:00:00Z",
        "tabs": [{"tabId": "tab-1", "name": "Overview", "panels": panels}],
    }

if __name__ == "__main__":
    print(json.dumps(build(sys.argv[1] if len(sys.argv) > 1 else ""), indent=1))
