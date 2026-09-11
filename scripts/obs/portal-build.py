#!/usr/bin/python3.12
"""portal-build.py — OBJ-27 F5b+F5c: the observability hyperespace portal.

A multi-page, self-contained, dark-themed static portal that answers, in
one URL: what the house spent (counted, split real vs estimated), the board
beating (created/closed/crashed 30d, active tasks with their kanban log),
the future projected (per-provider burn forecast, ETA, windows), the past
reconstructed (the F5b backfill gives the trace its history) and the alarms
awake (F2 + trace watchdogs).

PAGES (navigable from the index, shared nav on every page):
  index      KPIs + 30d spend chart + the house at a glance
  consumo    spend by objective/class/model/provider, each with drill-down
             to individual requests; search by requestId/objective/date
             (full search when served by obs-serve.py, anchors when static)
  board      board state, 30d created/closed/crashed, active tasks with
             their kanban event log, supply_ratio history (OBJ-29)
  providers  window state + burn forecast + ETA + closed windows history
             (weekly-reset ledger, calibrations)
  alarms     trace incidents, crash loops, F2 alert state, trace health
  docs       living docs: trace schema (from trace.py itself), report
             format, OTLP, dashboard and retention design (from the repo's
             docs/, resolved RELATIVE to this file — portable)

HOUSE RULES (inherited, not re-invented):
  - single source of truth: readers, thresholds, verdicts and the F2
    alerts are IMPORTED from morning-screen / obs-dashboard; alarms from
    trace-alarms; the canonical schema and prices from trace.py.
  - the gap is shown, not hidden: unattributed in red, estimated cost
    labeled as estimated (model-cost-ledger rows are a conservative UPPER
    bound, never presented as billing truth).
  - no_agent: pure function of existing files, zero tokens, no network.
  - privacy: the server binds 127.0.0.1 ONLY (obs-serve.py); pages carry
    no host paths; works opened as a file (headless mode: --out).
  - watchdog pattern: empty sources render elegant empty states, never
    fake zeros.

USE
  python3 portal-build.py                  # write <home>/quota-governor/obs/portal/
  python3 portal-build.py --out /tmp/p     # headless: static portal anywhere
  python3 obs-serve.py                     # the live URL: http://localhost:8917

Portability: paths resolve through get_hermes_home() (HERMES_HOME env or
~/.hermes). No absolute host paths in this module (the portability test
enforces it). Stdlib only, zero dependencies.
"""
from __future__ import annotations

import datetime as dt
import html
import importlib.util
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Sibling imports — the single-source-of-truth chain:
# morning-screen (readers, thresholds, verdicts) <- obs-dashboard (page
# readers, sparklines, KPI shaping) <- this portal. Alarms: trace-alarms.
# Schema + prices: trace.py.
# ---------------------------------------------------------------------------


def _load_sibling(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover
        raise SystemExit(f"portal-build: cannot load sibling {path.name}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_HERE = Path(__file__).resolve().parent
ms = _load_sibling("morning_screen", _HERE / "morning-screen.py")
od = _load_sibling("obs_dashboard", _HERE / "obs-dashboard.py")
ta = _load_sibling("trace_alarms", _HERE / "trace-alarms.py")
tr = _load_sibling("obs_trace", _HERE / "trace.py")

CEST = ms.CEST
DAYS = 30                                   # the portal's history window
DEFAULT_PORTAL_SUBDIR = "portal"
PAGES = ("index", "consumo", "board", "providers", "alarms", "docs")
PAGE_TITLES = {
    "index": "Overview",
    "consumo": "Consumo",
    "board": "Board",
    "providers": "Providers",
    "alarms": "Alarmas & Salud",
    "docs": "Docs vivos",
}
REPO_DOCS = ("obs-report-format.md", "obs-otlp.md", "obs-dashboard.md",
             "obs-trace-retention.md", "calibration-2026-09-08.md")

# sources whose costUsd is REAL money charged to a balance; everything else
# is a shadow estimate. The split is always shown (never merged silently).
REAL_SOURCES = frozenset({"nanogpt-requests"})


def portal_dir(hermes_home=None, out=None) -> Path:
    if out:
        return Path(out)
    return od.dashboard_path(hermes_home).parent / DEFAULT_PORTAL_SUBDIR


# ---------------------------------------------------------------------------
# Small render helpers
# ---------------------------------------------------------------------------

def _esc(x) -> str:
    return html.escape(str(x if x is not None else "-"), quote=True)


def _usd(v) -> str:
    try:
        return f"${float(v):,.4f}"
    except (TypeError, ValueError):
        return "-"


def _usd2(v) -> str:
    try:
        return f"${float(v):,.2f}"
    except (TypeError, ValueError):
        return "-"


def _fmt_ts(epoch) -> str:
    if not epoch:
        return "-"
    try:
        return dt.datetime.fromtimestamp(float(epoch), CEST).strftime(
            "%d-%b %H:%M")
    except (ValueError, OSError, OverflowError):
        return "-"


def _fmt_day(epoch) -> str:
    try:
        return dt.datetime.fromtimestamp(float(epoch), CEST).strftime("%d-%b")
    except (ValueError, OSError, OverflowError, TypeError):
        return "-"


def _slug(v) -> str:
    """Safe anchor id for a dimension value."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(v))[:60] or "x"


def _badge(kind: str, text: str) -> str:
    return f'<span class="badge {_esc(kind)}">{_esc(text)}</span>'


def _bar(pct: float, cls: str = "") -> str:
    return (f'<div class="bar {_esc(cls)}">'
            f'<i style="width:{min(max(pct, 0.0), 100.0):.0f}%"></i></div>')


def empty_state(text: str) -> str:
    return (f'<div class="empty"><span class="empty-ico">·</span>'
            f'{_esc(text)}</div>')


def _kpi(label: str, value: str, sub: str = "") -> str:
    sub_html = f'<div class="s">{sub}</div>' if sub else ""
    return (f'<div class="kpi"><div class="l">{label}</div>'
            f'<div class="v">{value}</div>{sub_html}</div>')


# ---------------------------------------------------------------------------
# Charts (inline SVG, no JS, no CDN — works as file://)
# ---------------------------------------------------------------------------

def line_chart(values, w=640.0, h=120.0, color="#58a6ff", fill=True,
               label_every=0) -> str:
    """Daily series -> SVG polyline (+ area, max label, optional x labels)."""
    vals = []
    for v in values:
        try:
            vals.append(float(v or 0.0))
        except (TypeError, ValueError):
            vals.append(0.0)
    n = len(vals)
    if n < 2:
        return empty_state("serie demasiado corta para dibujar")
    vmax = max(vals) or 1.0
    step = w / (n - 1)
    pts = [(i * step, h - 14 - (v / vmax) * (h - 34.0))
           for i, v in enumerate(vals)]
    poly = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    area = (f'<polygon fill="{color}" opacity="0.12" points='
            f'"{pts[0][0]:.1f},{h - 14:.1f} {poly} '
            f'{pts[-1][0]:.1f},{h - 14:.1f}"/>') if fill else ""
    vmax_s = f"{vmax:,.4f}".rstrip("0").rstrip(".")
    out = [f'<svg class="chart" width="{w:.0f}" height="{h:.0f}" '
           f'viewBox="0 0 {w:.0f} {h:.0f}" role="img">',
           f'<line x1="0" y1="{h - 14:.1f}" x2="{w:.0f}" '
           f'y2="{h - 14:.1f}" stroke="#21262d"/>', area,
           f'<polyline fill="none" stroke="{color}" stroke-width="1.6" '
           f'points="{poly}"/>',
           f'<text x="4" y="12" class="chart-max">max {vmax_s}</text>']
    if label_every:
        step_n = max(1, n // label_every)
        for i in range(0, n, step_n):
            out.append(f'<text x="{pts[i][0]:.1f}" y="{h - 3:.1f}" '
                       f'class="chart-lab" text-anchor="middle">'
                       f'{vals[i] and ""}{"" }</text>')
    out.append("</svg>")
    return "".join(out)


def bars_chart(series, w=640.0, h=130.0) -> str:
    """[(label, v1, v2, v3)] 30d -> grouped bars: creadas/cerradas/crashes."""
    n = len(series)
    if n == 0:
        return empty_state("sin eventos todavía")
    vmax = max(max(v1, v2, v3) for _, v1, v2, v3 in series) or 1.0
    slot = w / n
    bw = max(2.0, min(8.0, slot * 0.24))
    colors = ("#58a6ff", "#3fb950", "#f85149")
    out = [f'<svg class="chart" width="{w:.0f}" height="{h:.0f}" '
           f'viewBox="0 0 {w:.0f} {h:.0f}" role="img">',
           f'<line x1="0" y1="{h - 16:.1f}" x2="{w:.0f}" '
           f'y2="{h - 16:.1f}" stroke="#21262d"/>']
    for i, (lab, v1, v2, v3) in enumerate(series):
        x0 = i * slot + slot / 2
        for j, v in enumerate((v1, v2, v3)):
            bh = (v / vmax) * (h - 36.0)
            if v > 0:
                out.append(
                    f'<rect x="{x0 - bw * 1.6 + j * bw:.1f}" '
                    f'y="{h - 16 - bh:.1f}" width="{bw:.1f}" '
                    f'height="{bh:.1f}" fill="{colors[j]}" opacity="0.85"/>')
    for i in range(0, n, max(1, n // 6)):
        out.append(f'<text x="{i * slot + slot / 2:.1f}" y="{h - 4:.1f}" '
                   f'class="chart-lab" text-anchor="middle">'
                   f'{_esc(series[i][0])}</text>')
    out.append("</svg>")
    out.append('<div class="legend">'
               f'<span style="color:{colors[0]}">■</span> creadas '
               f'<span style="color:{colors[1]}">■</span> cerradas '
               f'<span style="color:{colors[2]}">■</span> crashes</div>')
    return "".join(out)


def spark_row(label: str, values, value_s: str) -> str:
    return (f'<tr><td>{_esc(label)}</td><td class="num">{_esc(value_s)}</td>'
            f'<td class="sparkcell">{od.sparkline(values)}</td></tr>')


# ---------------------------------------------------------------------------
# Data: board with events (the kanban log, rendered)
# ---------------------------------------------------------------------------

def read_board_full(hermes_home=None, now=None, window_days=DAYS) -> dict:
    """Board counts + 30d daily events + active tasks with their event log.

    Read-only (mode=ro), fails open like every reader in this family.
    """
    now = time.time() if now is None else float(now)
    lo = now - window_days * 86400.0
    out = {"db": False, "counts": {}, "total": 0, "done24": [], "active": [],
           "daily": [], "recent_events": []}
    db = ms.kanban_db_path(hermes_home)
    if not db.exists():
        return out
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
    except sqlite3.Error:
        return out
    try:
        for r in con.execute(
                "SELECT status, COUNT(*) c FROM tasks GROUP BY status"):
            out["counts"][r["status"]] = r["c"]
        out["total"] = sum(out["counts"].values())
        out["done24"] = [dict(r) for r in con.execute(
            "SELECT id, title, completed_at, assignee FROM tasks "
            "WHERE status='done' AND completed_at > ? "
            "ORDER BY completed_at DESC LIMIT 20", (now - 86400,))]
        out["active"] = [dict(r) for r in con.execute(
            "SELECT id, status, assignee, title FROM tasks "
            "WHERE status IN ('ready','running','blocked','triage') "
            "ORDER BY status, id LIMIT 40")]
        buckets = {}
        for r in con.execute(
                "SELECT kind, created_at FROM task_events "
                "WHERE created_at > ? AND kind IN "
                "('created','completed','crashed')", (lo,)):
            d = dt.datetime.fromtimestamp(r["created_at"], CEST).date()
            b = buckets.setdefault(d, {"created": 0, "completed": 0,
                                       "crashed": 0})
            if r["kind"] in b:
                b[r["kind"]] += 1
        today = dt.datetime.fromtimestamp(now, CEST).date()
        out["daily"] = [
            ((today - dt.timedelta(days=window_days - 1 - i)).isoformat(),
             buckets.get(today - dt.timedelta(days=window_days - 1 - i),
                         {}).get("created", 0),
             buckets.get(today - dt.timedelta(days=window_days - 1 - i),
                         {}).get("completed", 0),
             buckets.get(today - dt.timedelta(days=window_days - 1 - i),
                         {}).get("crashed", 0))
            for i in range(window_days)]
        ev_by_task = {}
        for r in con.execute(
                "SELECT task_id, kind, created_at FROM task_events "
                "ORDER BY created_at DESC LIMIT 400"):
            ev_by_task.setdefault(r["task_id"], []).append(
                {"kind": r["kind"], "created_at": r["created_at"]})
        out["events"] = ev_by_task
        for r in con.execute(
                "SELECT task_id, status, outcome, started_at, ended_at, "
                "error FROM task_runs ORDER BY id DESC LIMIT 120"):
            out["recent_events"].append({
                "task_id": r["task_id"], "status": r["status"],
                "outcome": r["outcome"], "started_at": r["started_at"],
                "ended_at": r["ended_at"],
                "error": (r["error"] or "")[:160]})
        out["db"] = True
    except sqlite3.Error:
        pass
    finally:
        con.close()
    return out


# ---------------------------------------------------------------------------
# Data: trace with real/estimated split + requests + dimension aggregates
# ---------------------------------------------------------------------------

def read_requests(rows: list) -> list:
    """Consumption rows (costUsd present) sorted newest-first, tagged."""
    out = []
    for r in rows:
        try:
            usd = float(r.get("costUsd") or 0.0)
        except (TypeError, ValueError):
            usd = 0.0
        if not r.get("costUsd"):
            continue
        d = dict(r)
        d["kind_cost"] = "real" if r.get("source") in REAL_SOURCES else "est"
        d["usd"] = usd
        out.append(d)
    out.sort(key=lambda r: (r.get("ts_epoch_utc") is None,
                            -(r.get("ts_epoch_utc") or 0.0)))
    return out


def agg_dims(rows: list) -> dict:
    """Spend by objective / class / model / provider (full trace)."""
    dims = {"obj": {}, "cls": {}, "model": {}, "prov": {}}
    keys = {"obj": "objective", "cls": "consumer_class",
            "model": "model", "prov": "provider"}
    for r in rows:
        try:
            usd = float(r.get("costUsd") or 0.0)
        except (TypeError, ValueError):
            usd = 0.0
        for dim, key in keys.items():
            v = r.get(key) or "unattributed"
            e = dims[dim].setdefault(v, {"n": 0, "usd": 0.0})
            e["n"] += 1
            e["usd"] += usd
    for dim in dims:
        dims[dim] = sorted(dims[dim].items(),
                           key=lambda kv: (-kv[1]["usd"], -kv[1]["n"]))
    return dims


def apply_query(rows: list, query: dict) -> tuple:
    """Live filters: q (substring over ids/model/objective), date (CEST
    YYYY-MM-DD), dim+val (objective/class/model/provider filter)."""
    if not query:
        return rows, ""
    out = rows
    notes = []
    q = (query.get("q") or "").strip().lower()
    if q:
        def _match(r):
            hay = " ".join(str(r.get(k) or "") for k in
                           ("requestId", "consumer_id", "model", "objective",
                            "source", "provider", "consumer_class")).lower()
            return q in hay
        out = [r for r in out if _match(r)]
        notes.append(f"q={q}")
    date = (query.get("date") or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        def _day(r):
            ts = r.get("ts_epoch_utc")
            if not isinstance(ts, (int, float)):
                return False
            return dt.datetime.fromtimestamp(ts, CEST).date().isoformat() \
                == date
        out = [r for r in out if _day(r)]
        notes.append(f"fecha={date}")
    dim = (query.get("dim") or "").strip()
    val = (query.get("val") or "").strip()
    if dim in ("obj", "cls", "model", "prov") and val:
        key = {"obj": "objective", "cls": "consumer_class",
               "model": "model", "prov": "provider"}[dim]
        out = [r for r in out if (r.get(key) or "unattributed") == val]
        notes.append(f"{dim}={val}")
    note = " · filtro: " + ", ".join(notes) if notes else ""
    return out, note


# ---------------------------------------------------------------------------
# Data: providers history (metrics + weekly ledger)
# ---------------------------------------------------------------------------

def _metrics_series(metrics: list, key: str) -> list:
    out = []
    for m in metrics:
        ts = tr._parse_ts(m.get("ts"))
        v = m.get(key)
        if ts is not None and isinstance(v, (int, float)):
            out.append((ts, float(v)))
    return out


def read_weekly_ledger(hermes_home=None) -> list:
    """Weekly-reset ledger rows (window history / calibrations)."""
    path = ms.state_dir(hermes_home) / "weekly-reset-ledger.jsonl"
    return ms._read_jsonl(path)


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

_CSS = """
:root{--bg:#0d1117;--card:#161b22;--line:#21262d;--tx:#c9d1d9;--mut:#8b949e;
--ok:#3fb950;--warn:#d29922;--bad:#f85149;--acc:#58a6ff;--mono:ui-monospace,
SFMono-Regular,Menlo,Consolas,monospace}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--tx);
font:14px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
a{color:var(--acc);text-decoration:none}
a:hover{text-decoration:underline}
.wrap{max-width:1160px;margin:0 auto;padding:16px 16px 40px}
nav.top{position:sticky;top:0;background:rgba(13,17,23,.94);border-bottom:
1px solid var(--line);z-index:9}
nav.top .in{max-width:1160px;margin:0 auto;display:flex;gap:4px;
align-items:center;padding:8px 16px;flex-wrap:wrap}
nav.top .brand{font-weight:700;margin-right:10px;white-space:nowrap}
nav.top a.tab{color:var(--mut);padding:3px 10px;border-radius:6px;
font-size:13px}
nav.top a.tab:hover{color:var(--tx);text-decoration:none;
background:var(--card)}
nav.top a.tab.on{color:var(--tx);background:var(--card)}
nav.top .url{margin-left:auto;color:var(--mut);font-size:11px;
font-family:var(--mono)}
header .gen{color:var(--mut);font-size:12px;margin:10px 0 14px}
h1{font-size:20px;margin:14px 0 0}
h2{font-size:12px;margin:0 0 10px;color:var(--mut);text-transform:uppercase;
letter-spacing:.06em}
h3{font-size:13px;margin:14px 0 6px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));
gap:10px;margin-bottom:12px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:8px;
padding:10px 12px}
.kpi .l{color:var(--mut);font-size:11px;text-transform:uppercase;
letter-spacing:.04em}
.kpi .v{font-size:20px;font-weight:600;margin-top:2px}
.kpi .s{color:var(--mut);font-size:11px;margin-top:2px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
@media(max-width:860px){.grid{grid-template-columns:1fr}}
section.card{background:var(--card);border:1px solid var(--line);
border-radius:8px;padding:12px 14px;margin-bottom:10px}
.headrow{display:flex;justify-content:space-between;align-items:center;
gap:10px;flex-wrap:wrap}
table{width:100%;border-collapse:collapse;font-size:13px}
td,th{padding:3px 6px;text-align:left;vertical-align:middle}
th{color:var(--mut);font-weight:500;font-size:11px;text-transform:uppercase}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.mono{font-family:var(--mono);font-size:12px}
.bar{background:var(--bg);border-radius:3px;height:8px;min-width:60px;
overflow:hidden}
.bar i{display:block;height:100%;background:var(--acc)}
.bar.bad i{background:var(--bad)}.bar.warn i{background:var(--warn)}
.spark{color:var(--acc);vertical-align:middle}
.sparkcell{width:180px}
.chart{width:100%;height:auto;color:var(--acc)}
.chart-max{fill:var(--mut);font-size:10px}
.chart-lab{fill:var(--mut);font-size:9px}
.legend{color:var(--mut);font-size:11px;margin-top:4px}
.badge{display:inline-block;padding:1px 8px;border-radius:10px;font-size:11px;
font-weight:600;white-space:nowrap}
.badge.ok{background:rgba(63,185,80,.15);color:var(--ok)}
.badge.warn{background:rgba(210,153,34,.15);color:var(--warn)}
.badge.bad{background:rgba(248,81,73,.15);color:var(--bad)}
.badge.mut{background:var(--line);color:var(--mut)}
.badge.acc{background:rgba(88,166,255,.15);color:var(--acc)}
.chip{display:inline-block;background:var(--line);border-radius:10px;
padding:1px 9px;margin:0 6px 6px 0;font-size:12px}
ul.list{margin:0;padding:0;list-style:none;font-size:12px}
ul.list li{padding:3px 0;border-top:1px solid var(--line);white-space:nowrap;
overflow:hidden;text-overflow:ellipsis}
ul.list li:first-child{border-top:0}
.mut{color:var(--mut)}.gap{color:var(--bad)}
.alert{border-radius:8px;padding:8px 12px;margin:6px 0;font-size:13px}
.alert.ok{background:rgba(63,185,80,.1);border:1px solid rgba(63,185,80,.4)}
.alert.warn{background:rgba(210,153,34,.1);border:1px solid
rgba(210,153,34,.4)}
.alert.danger{background:rgba(248,81,73,.1);border:1px solid
rgba(248,81,73,.45)}
.empty{color:var(--mut);padding:14px 8px;font-size:13px;border:1px dashed
var(--line);border-radius:8px;text-align:center}
.empty-ico{color:var(--acc);font-weight:700;margin-right:6px}
.detail-block{border:1px solid var(--line);border-radius:8px;
padding:10px 12px;margin:10px 0;background:rgba(22,27,34,.6)}
.detail-block:target{border-color:var(--acc)}
.toc{font-size:12px;line-height:1.9}
.toc a{margin-right:10px;white-space:nowrap}
form.search{display:flex;gap:6px;margin:8px 0;flex-wrap:wrap}
form.search input[type=text]{background:var(--bg);border:1px solid
var(--line);color:var(--tx);border-radius:6px;padding:5px 9px;font-size:13px;
min-width:220px}
form.search button{background:var(--card);border:1px solid var(--line);
color:var(--tx);border-radius:6px;padding:5px 12px;font-size:13px;cursor:
pointer}
pre.doc{white-space:pre-wrap;word-break:break-word;font-family:var(--mono);
font-size:11.5px;line-height:1.55;color:var(--tx);background:var(--bg);
border:1px solid var(--line);border-radius:8px;padding:12px;margin:8px 0;
max-height:440px;overflow:auto}
footer{margin-top:10px;color:var(--mut);font-size:11px;line-height:1.6}
footer code{background:var(--card);padding:1px 5px;border-radius:4px}
""".strip()


def _shell(page: str, body: str, gen: str, sub: str = "") -> str:
    nav = "".join(
        f'<a class="tab{" on" if p == page else ""}" '
        f'href="{p}.html">{_esc(PAGE_TITLES[p])}</a>'
        for p in PAGES)
    title = PAGE_TITLES.get(page, page)
    return "\n".join([
        "<!doctype html>", '<html lang="es">', "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>hiperespacio — {title}</title>",
        f"<style>{_CSS}</style>", "</head>", "<body>",
        '<nav class="top"><div class="in">',
        '<span class="brand">◉ hiperespacio</span>',
        nav,
        '<span class="url">http://localhost:8917</span>',
        "</div></nav>", '<div class="wrap">',
        f"<header><h1>{_esc(title)}</h1>",
        f'<div class="gen">{_esc(gen)}{_esc(sub)}</div></header>',
        body,
        '<footer>casa portable — ficheros estáticos, 0 dependencias, '
        "solo lectura<br>",
        "fuentes: <code>quota-governor/obs/trace.jsonl</code> · "
        "<code>forecast.json</code> · <code>metrics-history.jsonl</code> · "
        "<code>kanban.db</code> · <code>weekly-reset-ledger.jsonl</code><br>",
        "regenerar estático: <code>portal-build.py --out DIR</code> · "
        "servir: <code>obs-serve.py</code> (127.0.0.1 únicamente) · "
        "OBJ-27 F5 — stdlib</footer>",
        "</div>", "</body>", "</html>"])


# --- index ------------------------------------------------------------------

def page_index(data: dict, query: dict = None) -> str:
    agg, board, series = data["agg"], data["board"], data["series"]
    total = agg["total_usd"]
    real = agg["real_usd"]
    est = total - real
    unatt = agg["by_obj"].get("unattributed", {}).get("usd", 0.0)
    unatt_pct = (unatt / total * 100.0) if total > 0 else 0.0
    gap_cls = "gap" if unatt_pct > ms.UNATTRIBUTED_SPEND_PCT else ""
    last = data["metrics"][-1] if data["metrics"] else {}
    n_alarms = len(data["alarm_lines"])
    out = ['<section class="kpis">']
    out.append(_kpi(
        "Gasto (trace)",
        f'{_esc(_usd(total)) if total else "$0"}',
        f'{_esc(_usd(real))} real · {_esc(_usd(est))} estimado'
        if total else "sin coste todavía"))
    out.append(_kpi("Hueco sin etiqueta",
                    f'<span class="{gap_cls}">{unatt_pct:.0f}%</span>',
                    f"umbral &gt; {ms.UNATTRIBUTED_SPEND_PCT:.0f}% del gasto"))
    if last:
        ratio = last.get("supply_ratio")
        ratio_s = f"{ratio:.2f}" if isinstance(ratio, (int, float)) else "n/d"
        out.append(_kpi("supply_ratio 24h", ratio_s,
                        f'{last.get("supply_created_24h", "?")} creadas / '
                        f'{last.get("supply_closed_24h", "?")} cerradas'))
        bal = last.get("nanogpt_balance_usd")
        lvl = last.get("nanogpt_budget_level") or "?"
        out.append(_kpi("Saldo NanoGPT",
                        _usd2(bal) if bal is not None else "n/d",
                        _badge(od._LEVEL_BADGE.get(str(lvl), "mut"), lvl)))
    out.append(_kpi("Done 24h", str(len(board["done24"])),
                    ", ".join(r["id"] for r in board["done24"][:4])
                    + ("…" if len(board["done24"]) > 4 else "")))
    out.append(_kpi("Alarmas activas",
                    f'<span class="{"gap" if n_alarms else ""}">'
                    f'{n_alarms}</span>',
                    "ver Alarmas & Salud" if n_alarms else "sin incidencias"))
    out.append("</section>")

    out.append('<div class="grid">')
    out.append('<section class="card"><h2>Gasto $/día — 30 días</h2>'
               + line_chart([v for _, v in data["series"]["spend"]],
                            label_every=6)
               + f'<div class="mut" style="font-size:11px;margin-top:6px">'
                 f'ventana trace: {_esc(data["window_s"])}</div>'
               "</section>")
    out.append('<section class="card"><h2>Board 30d — creadas / cerradas / '
               "crashes</h2>" + bars_chart(data["board"]["daily"])
               + "</section>")
    out.append("</div>")

    out.append('<div class="grid">')
    cls_sorted = sorted(agg["by_class"].items(),
                        key=lambda kv: (-kv[1]["usd"], -kv[1]["n"]))
    cls_rows = []
    for name, e in cls_sorted[:6]:
        pct = (e["usd"] / total * 100.0) if total > 0 else 0.0
        cls_rows.append(
            f'<tr><td><a href="consumo.html#d-cls-{_slug(name)}">'
            f'{_esc(name)}</a></td><td class="num">{e["n"]}</td>'
            f'<td class="num">{_esc(_usd(e["usd"]))}</td>'
            f'<td class="num">{pct:.0f}%</td>'
            f"<td>{_bar(pct, 'bad' if name == 'unattributed' else '')}"
            "</td></tr>")
    out.append('<section class="card"><h2>Por clase de consumo</h2>'
               + (("<table><tr><th>clase</th><th class=num>líneas</th>"
                   "<th class=num>gasto</th><th class=num>%</th><th></th>"
                   "</tr>" + "".join(cls_rows) + "</table>")
                  if cls_rows else empty_state("sin trace todavía"))
               + "</section>")
    obj_sorted = sorted(agg["by_obj"].items(),
                        key=lambda kv: (-kv[1]["usd"], -kv[1]["n"]))
    obj_rows = []
    for name, e in obj_sorted[:6]:
        pct = (e["usd"] / total * 100.0) if total > 0 else 0.0
        mark = ' <span class="gap">← hueco</span>' \
            if name == "unattributed" else ""
        obj_rows.append(
            f'<tr><td><a href="consumo.html#d-obj-{_slug(name)}">'
            f'<span{" class=gap" if name == "unattributed" else ""}>'
            f'{_esc(name)}</span></a>{mark}</td>'
            f'<td class="num">{e["n"]}</td>'
            f'<td class="num">{_esc(_usd(e["usd"]))}</td>'
            f"<td>{_bar(pct, 'bad' if name == 'unattributed' else '')}"
            "</td></tr>")
    out.append('<section class="card"><h2>Por objetivo (top 6)</h2>'
               + (("<table><tr><th>objetivo</th><th class=num>líneas</th>"
                   "<th class=num>gasto</th><th></th></tr>"
                   + "".join(obj_rows) + "</table>")
                  if obj_rows else empty_state("sin trace todavía"))
               + "</section>")
    out.append("</div>")

    out.append('<section class="card"><h2>La casa, en una línea por '
               "sección</h2><div class=\"toc\">"
               '<a href="consumo.html">→ Consumo: cada dólar contado, con '
               "su request individual</a><br>"
               '<a href="board.html">→ Board: la cola respirando, crashes '
               "a la vista</a><br>"
               '<a href="providers.html">→ Providers: ventanas, burn y '
               "cuándo se agotan</a><br>"
               '<a href="alarms.html">→ Alarmas: lo que despierta de '
               "noche</a><br>"
               '<a href="docs.html">→ Docs vivos: el schema y las reglas '
               "de la casa</a></div></section>")
    return "\n".join(out)


# --- consumo ----------------------------------------------------------------

def _dim_table(dim: str, entries, total: float) -> str:
    labels = {"obj": "objetivo", "cls": "clase", "model": "modelo",
              "prov": "proveedor"}
    rows = []
    for name, e in entries:
        pct = (e["usd"] / total * 100.0) if total > 0 else 0.0
        gap = name == "unattributed"
        rows.append(
            f'<tr><td><a href="#d-{dim}-{_slug(name)}">'
            f'<span{" class=gap" if gap else ""}>{_esc(name)}</span></a>'
            f'{" <span class=gap>← hueco</span>" if gap else ""}</td>'
            f'<td class="num">{e["n"]}</td>'
            f'<td class="num">{_esc(_usd(e["usd"]))}</td>'
            f'<td class="num">{pct:.0f}%</td>'
            f"<td>{_bar(pct, 'bad' if gap else '')}</td></tr>")
    if not rows:
        return empty_state("sin datos para esta dimensión")
    return (f'<table><tr><th>{labels[dim]}</th>'
            "<th class=num>líneas</th><th class=num>gasto</th>"
            "<th class=num>%</th><th></th></tr>" + "".join(rows) + "</table>")


def _request_table(reqs: list, limit: int = 40, anchor: bool = True) -> str:
    rows = []
    for r in reqs[:limit]:
        rid = r.get("requestId") or ""
        cid = (r.get("consumer_id") or "-")[:24]
        rid_cell = (f'<a href="#req-{_slug(rid)}" class="mono">'
                    f'{_esc(rid[:26])}</a>') if rid and anchor else \
            (f'<span class="mono">{_esc(rid[:26])}</span>' if rid else
             f'<span class="mono mut">{_esc(cid)}</span>')
        kind = r.get("kind_cost")
        kind_badge = _badge("acc" if kind == "real" else "mut",
                            "real" if kind == "real" else "est")
        tin = r.get("tokens_in")
        tout = r.get("tokens_out")
        rows.append(
            f'<tr id="req-{_slug(rid or r.get("consumer_id") or "")}">'
            f'<td class="mono mut">{_esc(_fmt_ts(r.get("ts_epoch_utc")))}</td>'
            f"<td>{rid_cell}</td><td>{_esc(r.get('model') or '-')}</td>"
            f"<td>{_esc(r.get('provider') or '-')}</td>"
            f"<td>{_esc(r.get('consumer_class') or '-')}</td>"
            f"<td>{_esc(r.get('objective') or '-')}</td>"
            f'<td class="num">{_esc(tin if tin is not None else "-")}/'
            f'{_esc(tout if tout is not None else "-")}</td>'
            f'<td class="num">{_esc(_usd(r.get("usd")))}</td>'
            f"<td>{kind_badge}</td></tr>")
    if not rows:
        return empty_state("sin requests con coste en este filtro")
    return ('<table><tr><th>fecha</th><th>requestId</th><th>modelo</th>'
            "<th>prov</th><th>clase</th><th>objetivo</th><th class=num>"
            "tok in/out</th><th class=num>coste</th><th></th></tr>"
            + "".join(rows) + "</table>")


def page_consumo(data: dict, query: dict = None) -> str:
    agg = data["agg"]
    total = agg["total_usd"]
    dims = data["dims"]
    reqs_all = data["requests"]
    filtered, note = apply_query(reqs_all, query)
    q_exact = (query or {}).get("q", "").strip()

    out = []
    out.append('<section class="card"><h2>Búsqueda</h2>'
               '<form class="search" method="get" action="consumo.html">'
               '<input type="text" name="q" placeholder="requestId, modelo, '
               'objetivo…" value="">'
               '<input type="text" name="date" placeholder="YYYY-MM-DD" '
               'style="min-width:120px">'
               "<button type=\"submit\">buscar</button></form>"
               f'<div class="mut" style="font-size:11px">búsqueda completa '
               f"en el servidor local (http://localhost:8917/consumo); en el "
               f"fichero estático usa el buscador del navegador sobre la "
               f"tabla.{_esc(note)}</div></section>")

    if q_exact:
        exact = [r for r in reqs_all
                 if (r.get("requestId") or "").lower() == q_exact.lower()]
        if exact:
            r = exact[0]
            otel = r.get("otel") or {}
            otel_s = "".join(f'<div class="mono mut">{_esc(k)}: '
                             f"{_esc(v)}</div>" for k, v in sorted(
                                 otel.items()))
            out.append('<section class="card"><h2>Request individual — '
                       f"{_esc(q_exact)}</h2>"
                       '<table>'
                       f"<tr><th>fecha</th><td class=mono>"
                       f"{_esc(_fmt_ts(r.get('ts_epoch_utc')))}</td></tr>"
                       f"<tr><th>modelo</th><td>{_esc(r.get('model'))}</td>"
                       f"</tr>"
                       f"<tr><th>proveedor</th><td>"
                       f"{_esc(r.get('provider') or '-')}</td></tr>"
                       f"<tr><th>clase</th><td>"
                       f"{_esc(r.get('consumer_class'))}</td></tr>"
                       f"<tr><th>objetivo</th><td>"
                       f"{_esc(r.get('objective'))}</td></tr>"
                       f"<tr><th>tokens in/out</th><td class=num>"
                       f"{_esc(r.get('tokens_in'))} / "
                       f"{_esc(r.get('tokens_out'))}</td></tr>"
                       f"<tr><th>coste</th><td class=num>"
                       f"{_esc(_usd(r.get('usd')))}</td></tr>"
                       f"<tr><th>fuente</th><td>"
                       f"{_esc(r.get('source'))}</td></tr>"
                       "</table>" + otel_s + "</section>")

    out.append('<div class="grid">')
    labels = {"obj": "objetivo", "cls": "clase", "model": "modelo",
              "prov": "proveedor"}
    for i, dim in enumerate(("obj", "cls", "model", "prov")):
        out.append(f'<section class="card" id="agg-{dim}"><h2>Por '
                   f"{labels[dim]}</h2>"
                   + _dim_table(dim, dims[dim][:12], total) + "</section>")
    out.append("</div>")

    out.append('<section class="card"><h2>Drill-down — requests por '
               "objetivo</h2>")
    if not data["requests"]:
        out.append(empty_state("el trace no tiene líneas con coste todavía — "
                               "el backfill le dará el pasado"))
    else:
        toc = []
        sections = []
        # every DIMENSION gets drill-down blocks (the index/consumo tables
        # link to them); objetivo first, then clase/modelo/proveedor.
        dim_specs = (("obj", "objective", "objetivo"), ("cls",
                      "consumer_class", "clase"), ("model", "model",
                                                   "modelo"),
                     ("prov", "provider", "proveedor"))
        for dim, key, cap in dim_specs:
            picked = [n for n, _ in dims[dim][:12]]
            if "unattributed" in dict(dims[dim]) and \
                    "unattributed" not in picked:
                picked.append("unattributed")
            for name in picked:
                sid = f"d-{dim}-{_slug(name)}"
                sub_reqs = [r for r in reqs_all
                            if (r.get(key) or "unattributed") == name]
                usd = dict(dims[dim]).get(name, {}).get("usd", 0.0)
                toc.append(f'<a href="#{sid}">{_esc(cap)}:{_esc(name)}</a>')
                sections.append(
                    f'<div class="detail-block" id="{sid}">'
                    f'<h3>{_esc(cap)} {_esc(name)} · {_esc(_usd(usd))} · '
                    f'{len(sub_reqs)} requests</h3>'
                    + _request_table(sub_reqs, limit=30) + "</div>")
        out.append('<div class="toc">' + "".join(toc) + "</div>")
        out.extend(sections)
    out.append("</section>")

    out.append('<section class="card"><h2>Requests individuales '
               f'(últimos {min(len(filtered), 60)} de {len(filtered)} en '
               "el filtro)</h2>" + _request_table(filtered, limit=60)
               + "</section>")
    out.append('<div class="mut" style="font-size:11px">coste '
               '<span class="badge acc">real</span> = USD cobrado a saldo '
               "(nanogpt-requests) · <span class=\"badge mut\">est</span> = "
               "estimación (precios catálogo / ledger; cota superior "
               "conservadora, nunca verdad de facturación)</div>")
    return "\n".join(out)


# --- board ------------------------------------------------------------------

def page_board(data: dict, query: dict = None) -> str:
    board = data["board"]
    series = data["series"]
    out = ['<section class="kpis">']
    chips = "".join(f'<span class="chip">{_esc(k)}={v}</span>'
                    for k, v in sorted(board["counts"].items()))
    out.append(_kpi("Tareas", str(board["total"]),
                    chips or "sin board"))
    out.append(_kpi("Done 24h", str(len(board["done24"])),
                    ", ".join(r["id"] for r in board["done24"][:4])
                    + ("…" if len(board["done24"]) > 4 else "")))
    crashed30 = sum(v[3] for v in board["daily"])
    out.append(_kpi("Crashes 30d", str(crashed30),
                    "task_events crashed" if crashed30 else "sin crashes"))
    out.append("</section>")

    out.append('<section class="card"><h2>Creadas / cerradas / crashes — '
               "30 días</h2>" + bars_chart(board["daily"]) + "</section>")

    out.append('<div class="grid">')
    done_li = "".join(
        f'<li><span class="mut">{_esc(_fmt_ts(r.get("completed_at")))}</span> '
        f'<b>{_esc(r["id"])}</b> {_esc((r.get("title") or "")[:56])}</li>'
        for r in board["done24"][:12])
    out.append('<section class="card"><h2>Cerradas en 24h</h2>'
               + (f'<ul class="list">{done_li}</ul>' if done_li else
                  empty_state("nada cerrado en 24h")) + "</section>")

    act_li = []
    for r in board["active"]:
        nid = _esc(r["id"])
        st = _badge("acc" if r["status"] == "running" else "mut",
                    r["status"])
        title = _esc((r.get("title") or "")[:44])
        act_li.append(
            f'<li><a href="#log-{_slug(r["id"])}"><b>{nid}</b></a> '
            f'{st} <span class="mut">'
            f'{_esc(r.get("assignee") or "")}</span> '
            f'{title}</li>')
    out.append('<section class="card"><h2>Tareas activas (link a su '
               "kanban log)</h2>"
               + (f'<ul class="list">{"".join(act_li)}</ul>' if act_li else
                  empty_state("cola vacía — la casa descansa")) + "</section>")
    out.append("</div>")

    sup = series["supply"]
    out.append('<section class="card"><h2>supply_ratio — histórico '
               "(OBJ-29)</h2>")
    if sup:
        vals = [v for _, v in sup]
        last_v = vals[-1]
        out.append(line_chart(vals, color="#3fb950", label_every=6))
        out.append(f'<div class="mut" style="font-size:11px;margin-top:4px">'
                   f"último: {last_v:.2f} · "
                   f"media: {sum(vals) / len(vals):.2f} · "
                   f"{len(vals)} muestras desde "
                   f"{_esc(_fmt_day(sup[0][0]))}</div>")
    else:
        out.append(empty_state("sin muestras de supply_ratio todavía"))
    out.append("</section>")

    out.append('<section class="card"><h2>Kanban log — eventos recientes '
               "por tarea activa</h2>")
    evs = board.get("events") or {}
    if not board["active"]:
        out.append(empty_state("sin tareas activas"))
    else:
        for r in board["active"][:14]:
            sid = f"log-{_slug(r['id'])}"
            log = evs.get(r["id"], [])
            log_li = "".join(
                f'<li><span class="mut mono">'
                f'{_esc(_fmt_ts(e["created_at"]))}</span> '
                f"{_esc(e['kind'])}</li>" for e in log[:12]) or \
                '<li class="mut">sin eventos registrados</li>'
            out.append(f'<div class="detail-block" id="{sid}">'
                       f"<h3>{_esc(r['id'])} "
                       f'{_badge("mut", r["status"])} '
                       f'{_esc((r.get("title") or "")[:60])}</h3>'
                       f'<ul class="list">{log_li}</ul></div>')
    out.append("</section>")
    return "\n".join(out)


# --- providers ----------------------------------------------------------------

_VERDICT_LABEL = {"ok": ("ok", "OK"), "reduce": ("warn", "REDUCIR"),
                  "off": ("bad", "BOARD OFF"),
                  "unknown": ("mut", "SIN PROYECCIÓN")}


def page_providers(data: dict, query: dict = None) -> str:
    fc = data["forecast"]
    series = data["series"]
    out = ['<section class="kpis">']
    n_ok = fc.get("providers_ok")
    out.append(_kpi("Providers OK", str(n_ok if n_ok is not None else "n/d"),
                    fc.get("enabled") and "forecast habilitado"
                    or "forecast (estado según gate)"))
    reset_iso = fc.get("next_weekly_reset_iso")
    htr = fc.get("hours_to_reset")
    htr_s = f"{htr:.1f}h" if isinstance(htr, (int, float)) else "-"
    out.append(_kpi("Reset semanal", htr_s, _esc(reset_iso or "-")))
    out.append(_kpi("Ventana forecast",
                    f"{fc.get('window_hours', '-')}h",
                    f"EMA α={fc.get('alpha', '-')}"))
    out.append("</section>")

    out.append('<section class="card"><h2>Ventanas — burn y veredicto '
               "(gate)</h2>")
    verdicts = data["verdicts"]
    if verdicts:
        rows = []
        for v in verdicts:
            pct = v["pct"]
            pct_s = f"{pct:.1f}%" if isinstance(pct, (int, float)) else "-"
            pv = pct if isinstance(pct, (int, float)) else 0.0
            pcls = "bad" if pv > 90 else ("warn" if pv > 75 else "")
            bcls, label = _VERDICT_LABEL[v["status"]]
            rows.append(
                f'<tr><td>{_esc(v["name"])}</td>'
                f'<td class="num">{_esc(pct_s)}</td>'
                f"<td>{_bar(pv, pcls)}</td>"
                f'<td>{_badge(bcls, label)}</td>'
                f'<td class="mut" style="font-size:11px">'
                f'{_esc(v["text"])}</td></tr>')
        out.append('<table><tr><th>provider</th><th class=num>% ahora</th>'
                   "<th>carga</th><th>veredicto</th><th></th></tr>"
                   + "".join(rows) + "</table>")
    else:
        out.append(empty_state("sin forecast todavía — el EMA necesita "
                               "algunas muestras de burn"))
    out.append("</section>")

    out.append('<div class="grid">')
    out.append('<section class="card"><h2>Burn por provider — '
               "% semanal histórico</h2>")
    prov_series = [
        ("ollama", "ollama_weekly_pct", "ollama-cloud"),
        ("nanogpt", "nanogpt_weekly_pct", "nanogpt"),
        ("opencode go", "opencode_rolling_pct", "opencode-go (5h)"),
        ("opencode sem", "opencode_weekly_pct", "opencode-go (sem)"),
    ]
    rows = []
    for label, key, _cap in prov_series:
        pts = series["provider"].get(key) or []
        if not pts:
            continue
        last_v = pts[-1][1]
        rows.append(spark_row(label, [v for _, v in pts],
                              f"{last_v:.1f}%"))
    out.append(("<table><tr><th>provider</th><th class=num>último</th>"
                "<th>30d</th></tr>" + "".join(rows) + "</table>")
               if rows else empty_state("sin métricas de providers todavía"))
    out.append("</section>")

    bal = series["balance"]
    out.append('<section class="card"><h2>Saldo NanoGPT — histórico</h2>')
    if bal:
        out.append(line_chart([v for _, v in bal], color="#d29922",
                              label_every=6))
        out.append(f'<div class="mut" style="font-size:11px">último: '
                   f"{_usd2(bal[-1][1])} · {len(bal)} muestras</div>")
    else:
        out.append(empty_state("sin muestras de saldo"))
    out.append("</section>")
    out.append("</div>")

    ledger = data["weekly_ledger"]
    out.append('<section class="card"><h2>Histórico de ventanas cerradas / '
               "calibraciones</h2>")
    if ledger:
        rows = []
        for row in reversed(ledger[-30:]):
            for p in row.get("providers", []):
                reset = p.get("resets_at") or "-"
                parked = bool(p.get("parked"))
                state = _badge("mut" if parked else "ok",
                               "aparcado" if parked else "activo")
                rows.append(
                    f'<tr><td class="mono mut">'
                    f'{_esc(str(row.get("ts", ""))[:16])}</td>'
                    f"<td>{_esc(p.get('provider'))}</td>"
                    f'<td class="num">{p.get("weekly_pct", 0):.1f}%</td>'
                    f"<td>{state}</td>"
                    f'<td class="mono mut">{_esc(str(reset)[:19])}</td></tr>')
        out.append('<table><tr><th>ts</th><th>provider</th>'
                   "<th class=num>% semanal</th><th>estado</th>"
                   "<th>reset</th></tr>" + "".join(rows) + "</table>")
    else:
        out.append(empty_state("sin ledger de resets todavía"))
    out.append("</section>")
    return "\n".join(out)


# --- alarms -------------------------------------------------------------------

def page_alarms(data: dict, query: dict = None) -> str:
    alarm_lines = data["alarm_lines"]
    cards = data["alert_cards"]
    out = ['<section class="card"><h2>Alarmas del trace (F2 watchdog)'
           "</h2>"]
    if alarm_lines:
        for line in alarm_lines:
            kind = "danger" if "crash-loop" in line else "warn"
            out.append(f'<div class="alert {kind}">{_esc(line)}</div>')
    else:
        out.append('<div class="alert ok">sin incidencias — el trace está '
                   "limpio</div>")
    out.append("</section>")

    out.append('<section class="card"><h2>Estado de alertas F2 '
               "(regla del gate)</h2>")
    if cards:
        for c in cards:
            out.append(f'<div class="alert {_esc(c["kind"])}">'
                       f'{_esc(c["text"])}</div>')
    else:
        out.append(empty_state("sin fuentes de alertas (trace/forecast/"
                               "board ausentes)"))
    out.append("</section>")

    doctor = data["doctor"]
    parseable = bool(doctor.get("parseable"))
    parse_badge = _badge("ok" if parseable else "bad",
                         "sí" if parseable else "NO — líneas corruptas")
    out.append('<section class="card"><h2>Salud del trace</h2><table>'
               f"<tr><th>líneas</th><td class=num>{doctor.get('lines', 0)}"
               "</td></tr>"
               f"<tr><th>ventana</th><td>{_esc(data['window_s'])}</td></tr>"
               f'<tr><th>parseable</th><td>{parse_badge}</td></tr>'
               f"<tr><th>archivo activo</th><td class=mono>"
               f"{doctor.get('trace_bytes', 0):,} bytes</td></tr>"
               f"<tr><th>archivos rotados</th><td class=num>"
               f"{doctor.get('archives', {}).get('count', 0)} "
               f"({doctor.get('archives', {}).get('bytes', 0):,} bytes)"
               "</td></tr>"
               f"<tr><th>retención</th><td class=mono>"
               f"{doctor.get('retention', {}).get('keep_days', '-')} días / "
               f"{doctor.get('retention', {}).get('max_lines', '-')} líneas"
               "</td></tr></table>"
               '<div class="mut" style="font-size:11px;margin-top:6px">'
               "fuentes vivas: trace.jsonl (nanogpt-requests · usage-audit · "
               "task-events · model-cost-ledger) — el backfill reconstruye "
               "el pasado desde ellas</div></section>")

    loops = [a for a in alarm_lines if "crash-loop" in a]
    out.append('<section class="card"><h2>Loops de crash</h2>')
    if loops:
        for line in loops:
            tid = line.split(":")[1].strip().split(" ")[0] \
                if ":" in line else ""
            out.append(f'<div class="alert danger">{_esc(line)} — '
                       f'<a href="board.html#log-{_slug(tid)}">ver kanban '
                       "log</a></div>")
    else:
        out.append('<div class="alert ok">ningún loop de crash en 24h'
                   "</div>")
    out.append("</section>")
    return "\n".join(out)


# --- docs ---------------------------------------------------------------------

def _doc_block(title: str, text: str) -> str:
    return (f'<h3>{_esc(title)}</h3><pre class="doc">{_esc(text)}</pre>')


def page_docs(data: dict, query: dict = None) -> str:
    out = ['<section class="card"><h2>Trace schema — canónico (vivo, del '
           "código)</h2>"]
    doc = (tr.__doc__ or "").strip()
    if doc:
        out.append(_doc_block("scripts/obs/trace.py", doc))
    else:
        out.append(empty_state("schema no disponible"))
    out.append("</section>")

    out.append('<section class="card"><h2>Docs del repo (render crudo, '
               "fuentes vivas)</h2>")
    docs_dir = _HERE.resolve().parents[2] / "docs"
    any_doc = False
    for name in REPO_DOCS:
        try:
            text = (docs_dir / name).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            text = ""
        if text:
            any_doc = True
            out.append(_doc_block(name, text))
    if not any_doc:
        out.append(empty_state("docs del repo no encontradas junto al "
                               "módulo (instalación sin repo)"))
    out.append("</section>")

    out.append('<section class="card"><h2>Contratos de la casa</h2>'
               "<ul class=\"list\">"
               "<li>el hueco se muestra, nunca se esconde (unattributed en "
               "rojo, estimaciones etiquetadas)</li>"
               "<li>una sola fuente de verdad: umbrales y veredictos "
               "importados, no re-implementados</li>"
               "<li>watchdog: silencio = sin incidencias; estados vacíos "
               "elegantes, ceros falsos jamás</li>"
               "<li>solo lectura: el portal nunca escribe en sus "
               "fuentes</li>"
               "<li>127.0.0.1 únicamente: el hiperespacio es de la casa y "
               "en la casa</li></ul></section>")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Orchestration: one read pass -> all pages
# ---------------------------------------------------------------------------

def gather(hermes_home=None, now=None) -> dict:
    """Read every source once; everything downstream is pure shaping."""
    now = time.time() if now is None else float(now)
    rows = od.read_trace(hermes_home)
    agg = od.agg_trace(rows)
    real = sum(float(r.get("costUsd") or 0.0) for r in rows
               if r.get("source") in REAL_SOURCES)
    agg["real_usd"] = real
    dims = agg_dims(rows)
    requests = read_requests(rows)
    board = read_board_full(hermes_home, now=now)
    metrics = od.read_metrics(hermes_home)
    fc = ms._read_json(ms.forecast_path(hermes_home))
    series = {
        "spend": od.daily_series(rows, days=DAYS, now=now),
        "supply": [
            (tr._parse_ts(m.get("ts")), m.get("supply_ratio"))
            for m in metrics
            if tr._parse_ts(m.get("ts")) is not None
            and isinstance(m.get("supply_ratio"), (int, float))],
        "provider": {k: _metrics_series(metrics, k) for k in
                     ("ollama_weekly_pct", "nanogpt_weekly_pct",
                      "opencode_rolling_pct", "opencode_weekly_pct")},
        "balance": _metrics_series(metrics, "nanogpt_balance_usd"),
    }
    doctor = tr.doctor(hermes_home)
    window_s = ""
    if agg.get("first_ts"):
        window_s = (f"{_fmt_ts(agg['first_ts'])} → "
                    f"{_fmt_ts(agg['last_ts'])}")
    return {
        "now": now, "rows": rows, "agg": agg, "dims": dims,
        "requests": requests, "board": board, "metrics": metrics,
        "forecast": fc, "verdicts": od.provider_verdicts(fc),
        "series": series, "doctor": doctor,
        "alarm_lines": ta.run_checks(hermes_home, now=now),
        "alert_cards": od.alert_cards(hermes_home),
        "weekly_ledger": read_weekly_ledger(hermes_home),
        "window_s": window_s,
    }


_PAGE_FN = {
    "index": page_index, "consumo": page_consumo, "board": page_board,
    "providers": page_providers, "alarms": page_alarms, "docs": page_docs,
}


def portal_build(hermes_home=None, now=None, query=None) -> dict:
    """{page_name: html} — pure function of the read-only sources."""
    data = gather(hermes_home=hermes_home, now=now)
    gen = dt.datetime.fromtimestamp(data["now"], CEST).strftime(
        "%d-%b %H:%M (UTC+2)")
    return {p: _shell(p, _PAGE_FN[p](data, query), gen,
                      f" · ventana trace {data['window_s']}"
                      if data["window_s"] else "")
            for p in PAGES}


def write_portal(hermes_home=None, out=None, now=None) -> Path:
    """Write every page under obs/portal/ (or --out). Returns the dir."""
    pages = portal_build(hermes_home=hermes_home, now=now)
    target = portal_dir(hermes_home, out)
    target.mkdir(parents=True, exist_ok=True)
    for name, page in pages.items():
        (target / f"{name}.html").write_text(page, encoding="utf-8")
    return target


# ---------------------------------------------------------------------------
# CLI (headless mode: static generation without server — see obs-serve.py
# for the live URL)
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    p.add_argument("--out", default=None,
                   help="write the portal here instead of the default "
                        "obs/portal/")
    args = p.parse_args(argv)
    try:
        target = write_portal(out=args.out)
        print(target)
        return 0
    except Exception as exc:
        print(f"portal-build: {exc!r}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
