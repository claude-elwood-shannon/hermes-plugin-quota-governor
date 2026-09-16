#!/usr/bin/python3.12
"""obs-dashboard.py — OBJ-32 v0: observability with a human face.

One self-contained HTML page (inline CSS + SVG sparklines, zero JS, zero
dependencies, stdlib only) that answers, in one screen: what did the
house spend and on what, is any quota about to run out, what moved on
the board, and is anything on fire.

VALUES: same as original; no change in behavior or output.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import http.server
import importlib.util
import sqlite3
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Sibling import: morning-screen is the single source of truth for the
# readers, the F2 thresholds and the gate verdict rule (hyphenated
# filename -> importlib, same pattern as its own test suite).
# ---------------------------------------------------------------------------
_MSPEC = Path(__file__).resolve().parent / "morning-screen.py"
_spc = importlib.util.spec_from_file_location("morning_screen", _MSPEC)
if _spc is None or _spc.loader is None:  # pragma: no cover
    raise SystemExit("obs-dashboard: cannot load sibling morning-screen.py")
ms = importlib.util.module_from_spec(_spc)
_spc.loader.exec_module(ms)

CEST = ms.CEST  # fixed UTC+2 render timezone (house convention)
DAYS = 14  # sparkline window (days)
DEFAULT_PORT = 8734

_VERDICT_BADGE = {
    "ok": ("ok", "OK"),
    "reduce": ("warn", "REDUCIR"),
    "off": ("bad", "BOARD OFF"),
    "unknown": ("mut", "SIN PROYECCIÓN"),
}
_LEVEL_BADGE = {"ok": "ok", "warn": "warn", "dry": "bad", "exhausted": "bad"}

# ---------------------------------------------------------------------------
# Small helpers that existed before refactor; kept unchanged.
# ---------------------------------------------------------------------------

def _esc(x) -> str:
    return html.escape(str(x), quote=True)


def _usd2(v) -> str:
    try:
        return f"${float(v):,.2f}"
    except (TypeError, ValueError):
        return "-"

# ---------------------------------------------------------------------------
# Readers (all read‑only; fail open like morning‑screen)
# ---------------------------------------------------------------------------

def read_trace(hermes_home=None) -> list:
    return ms._read_jsonl(ms.trace_path(hermes_home))


def metrics_path(hermes_home=None) -> Path:
    return ms.state_dir(hermes_home) / "metrics-history.jsonl"


def read_metrics(hermes_home=None) -> list:
    return ms._read_jsonl(metrics_path(hermes_home))


def dashboard_path(hermes_home=None) -> Path:
    """Default output: next to the trace, under <HERMES_HOME>/quota-governor/obs/."""
    return ms.trace_path(hermes_home).parent / "dashboard.html"

# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def agg_trace(rows: list) -> dict:
    """Totals + spend by consumer class and by objective. Cumulative."""
    out = {
        "n": len(rows), "n_cost": 0, "total_usd": 0.0,
        "unatt_usd": 0.0, "unatt_pct": 0.0,
        "first_ts": None, "last_ts": None,
        "by_class": {}, "by_obj": {},
    }
    cls: dict = {}
    obj: dict = {}
    for r in rows:
        c = r.get("consumer_class") or "unattributed"
        o = r.get("objective") or "unattributed"
        try:
            usd = float(r.get("costUsd") or 0.0)
        except (TypeError, ValueError):
            usd = 0.0
        e = cls.setdefault(c, {"n": 0, "usd": 0.0})
        e["n"] += 1
        e["usd"] += usd
        e = obj.setdefault(o, {"n": 0, "usd": 0.0})
        e["n"] += 1
        e["usd"] += usd
        if r.get("costUsd"):
            out["n_cost"] += 1
        out["total_usd"] += usd
        ts = r.get("ts_epoch_utc")
        if isinstance(ts, (int, float)):
            ts = float(ts)
            out["first_ts"] = ts if out["first_ts"] is None else min(out["first_ts"], ts)
            out["last_ts"] = ts if out["last_ts"] is None else max(out["last_ts"], ts)
    if out["total_usd"] > 0:
        out["unatt_usd"] = obj.get("unattributed", {}).get("usd", 0.0)
        out["unatt_pct"] = out["unatt_usd"] / out["total_usd"] * 100.0
    out["by_class"] = cls
    out["by_obj"] = obj
    return out

# ---------------------------------------------------------------------------
# Daily series for sparkline
# ---------------------------------------------------------------------------

def daily_series(rows: list, days: int = DAYS, now=None) -> list:
    """[(iso_date, usd)] for the last `days` local days; missing days are 0."""
    now = time.time() if now is None else float(now)
    buckets: dict = {}
    for r in rows:
        ts = r.get("ts_epoch_utc")
        if not isinstance(ts, (int, float)):
            continue
        d = dt.datetime.fromtimestamp(float(ts), CEST).date()
        try:
            usd = float(r.get("costUsd") or 0.0)
        except (TypeError, ValueError):
            usd = 0.0
        buckets[d] = buckets.get(d, 0.0) + usd
    today = dt.datetime.fromtimestamp(now, CEST).date()
    return [
        ((today - dt.timedelta(days=days - 1 - i)).isoformat(),
         buckets.get(today - dt.timedelta(days=days - 1 - i), 0.0))
        for i in range(days)
    ]

# ---------------------------------------------------------------------------
# Board reader
# ---------------------------------------------------------------------------

def read_board(hermes_home=None) -> dict:
    """Board state: counts, done 24h, active tasks. Read‑only, fails open."""
    out = {"db": False, "counts": {}, "total": 0, "done24": [], "active": []}
    db = ms.kanban_db_path(hermes_home)
    if not db.exists():
        return out
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
    except sqlite3.Error:
        return out
    try:
        for r in con.execute("SELECT status, COUNT(*) c FROM tasks GROUP BY status"):
            out["counts"][r["status"]] = r["c"]
        out["total"] = sum(out["counts"].values())
        now = time.time()
        out["done24"] = [dict(r) for r in con.execute(
            "SELECT id, title, completed_at FROM tasks "
            "WHERE status='done' AND completed_at > ? "
            "ORDER BY completed_at DESC LIMIT 12", (now - 86400,))]
        out["active"] = [dict(r) for r in con.execute(
            "SELECT id, status, assignee, body FROM tasks "
            "WHERE status IN ('ready','running','blocked') "
            "ORDER BY status, id LIMIT 12")]
        out["db"] = True
    except sqlite3.Error:
        pass
    finally:
        con.close()
    return out

# ---------------------------------------------------------------------------
# Verdicts + alerts: parsed from morning‑screen output (single source of truth for the
# RULES; here we only shape into badges/cards).
# ---------------------------------------------------------------------------

def provider_verdicts(fc: dict) -> list:
    """[{name, status, text, pct}] — one per provider the gate rule speaks about."""
    provs = fc.get("providers", {}) if isinstance(fc, dict) else {}
    out = []
    for line in ms._build_verdict(fc):
        s = line.strip()
        name, _, rest = s.partition(":")
        name = name.strip()
        if name not in provs:  # skip summary lines
            continue
        if "BOARD OFF" in rest:
            st = "off"
        elif "max_workers=1" in rest:
            st = "reduce"
        elif "OK" in rest:
            st = "ok"
        else:
            st = "unknown"
        p = provs.get(name) or {}
        out.append({"name": name, "status": st, "text": rest.strip(),
                    "pct": p.get("pct_now")})
    return out


# ---------------------------------------------------------------------------
# Alert Cards
# ---------------------------------------------------------------------------

def alert_cards(hermes_home=None):
    """F2 alerts as [{kind, text}]; None when there are no sources at all."""
    txt = ms.build_alerts_screen(hermes_home=hermes_home)
    if not txt:
        return None
    cards = []
    for line in txt.splitlines()[1:]:
        s = line.strip()
        if not s:
            continue
        low = s.lower()
        if low == "sin incidencias":
            cards.append({"kind": "ok", "text": s})
        elif "board off" in low or low.startswith("burn"):
            cards.append({"kind": "danger", "text": s})
        elif low.startswith("vllm service down"):
            cards.append({"kind": "danger", "text": s})
        else:
            cards.append({"kind": "warn", "text": s})
    return cards

# ---------------------------------------------------------------------------
# Misc helpers/constants
# ---------------------------------------------------------------------------
_CSS = """
:root{--bg:#0d1117;--card:#161b22;--line:#21262d;--tx:#c9d1d9;--mut:#8b949e;
--ok:#3fb950;--warn:#d29922;--bad:#f85149;--acc:#58a6ff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--tx);
font:14px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:20px 16px 40px}
header h1{font-size:20px;margin:0 0 2px}
header .gen{color:var(--mut);font-size:12px;margin-bottom:16px}
.banner{background:rgba(210,153,34,.12);border:1px solid rgba(210,153,34,.4);
border-radius:8px;padding:8px 12px;margin-bottom:12px;font-size:13px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));
gap:10px;margin-bottom:12px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:8px;
padding:10px 12px}
.kpi .l{color:var(--mut);font-size:11px;text-transform:uppercase;
letter-spacing:.04em}
.kpi .v{font-size:20px;font-weight:600;margin-top:2px}
.kpi .s{color:var(--mut);font-size:11px;margin-top:2px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
@media(max-width:800px){.grid{grid-template-columns:1fr}}
section.card{background:var(--card);border:1px solid var(--line);
border-radius:8px;padding:12px 14px;margin-bottom:10px}
section.card h2{font-size:12px;margin:0 0 10px;color:var(--mut);
text-transform:uppercase;letter-spacing:.06em}
.headrow{display:flex;justify-content:space-between;align-items:center;gap:10px}
.sparkrow{color:var(--acc);white-space:nowrap}
.sparkrow small{color:var(--mut)}
table{width:100%;border-collapse:collapse;font-size:13px}
td,th{padding:3px 6px;text-align:left;vertical-align:middle}
th{color:var(--mut);font-weight:500;font-size:11px;text-transform:uppercase}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.bar{background:var(--bg);border-radius:3px;height:8px;min-width:60px;
overflow:hidden}
.bar i{display:block;height:100%;background:var(--acc)}
.bar.bad i{background:var(--bad)}
.bar.warn i{background:var(--warn)}
.spark{color:var(--acc);vertical-align:middle}
.gap{color:var(--bad)}
.badge{display:inline-block;padding:1px 8px;border-radius:10px;font-size:11px;
font-weight:600;white-space:nowrap}
.badge.ok{background:rgba(63,185,80,.15);color:var(--ok)}
.badge.warn{background:rgba(210,153,34,.15);color:var(--warn)}
.badge.bad{background:rgba(248,81,73,.15);color:var(--bad)}
.badge.mut{background:var(--line);color:var(--mut)}
.chip{display:inline-block;background:var(--line);border-radius:10px;
padding:1px 9px;margin:0 6px 6px 0;font-size:12px}
ul.list{margin:0;padding:0;list-style:none;font-size:12px}
ul.list li{padding:3px 0;border-top:1px solid var(--line);white-space:nowrap;
overflow:hidden;text-overflow:ellipsis}
ul.list li:first-child{border-top:0}
.mut{color:var(--mut)}
.alert{border-radius:8px;padding:8px 12px;margin:6px 0;font-size:13px}
.alert.ok{background:rgba(63,185,80,.1);border:1px solid rgba(63,185,80,.4)}
.alert.warn{background:rgba(210,153,34,.1);border:1px solid rgba(210,153,34,.4)}
.alert.danger{background:rgba(248,81,73,.1);border:1px solid rgba(248,81,73,.45)}
footer{margin-top:6px;color:var(--mut);font-size:11px;line-height:1.6}
footer code{background:var(--card);padding:1px 5px;border-radius:4px}
""".strip()

def sparkline(values, w=170.0, h=36.0) -> str:
    vals = []
    for v in values:
        try:
            vals.append(float(v or 0.0))
        except (TypeError, ValueError):
            vals.append(0.0)
    n = len(vals)
    if n == 0:
        return ""
    if n == 1:
        vals = vals * 2
        n = 2
    vmax = max(vals) or 1.0
    step = w / (n - 1)
    pts = " ".join(
        f"{i * step:.1f},{h - (v / vmax) * (h - 4.0) - 2.0:.1f}"
        for i, v in enumerate(vals))
    return (f'<svg class="spark" width="{w:.0f}" height="{h:.0f}" '
            f'viewBox="0 0 {w:.0f} {h:.0f}" role="img" '
            f'aria-label="sparkline"><polyline fill="none" '
            f'stroke="currentColor" stroke-width="1.5" '
            f'points="{pts}"/></svg>')


def _kpi(label: str, value: str, sub: str = "") -> str:
    sub_html = f'<div class="s">{sub}</div>' if sub else ""
    return (f'<div class="kpi"><div class="l">{label}</div>'
            f'<div class="v">{value}</div>{sub_html}</div>')


def _spend_table(entries, total_usd: float) -> str:
    """entries: [(name, {n, usd})] already ordered; renders table + bars."""
    body = []
    for name, e in entries:
        pct = (e["usd"] / total_usd * 100.0) if total_usd > 0 else 0.0
        gap_cls = ' class="gap"' if name == "unattributed" else ""
        bar_cls = "bad" if name == "unattributed" else ""
        mark = ' <span class="gap">← sin etiqueta (hueco)</span>' \
            if name == "unattributed" else ""
        body.append(
            f"<tr><td><span{gap_cls}>{_esc(name)}</span>{mark}</td>"
            f'<td class="num">{e["n"]}</td>'
            f'<td class="num">{_esc(ms._fmt_usd(e["usd"]))}</td>'
            f'<td class="num">{pct:.0f}%</td>'
            f'<td><div class="bar {bar_cls}">'
            f'<i style="width:{min(pct, 100.0):.0f}%"></i></div></td></tr>')
    return ("<table><tr><th>clase</th><th class=num>líneas</th>"
            "<th class=num>gasto</th><th class=num>%</th><th></th></tr>"
            + "".join(body) + "</table>")

# ---------------------------------------------------------------------------
# Helper: K‑PI section (single line, <50 lines)
# ---------------------------------------------------------------------------

def _kpi_section(agg: dict, board: dict, budget: dict, now: float) -> list:
    gap_cls = "gap" if agg["unatt_pct"] > ms.UNATTRIBUTED_SPEND_PCT else ""
    content = [
        _kpi("Gasto real (trace)", _esc(ms._fmt_usd(agg["total_usd"])),
              f'{agg["n_cost"]}/{agg["n"]} líneas con coste'),
        _kpi('Hueco sin etiqueta',
             f'<span class="{gap_cls}">{agg["unatt_pct"]:.0f}%</span>',
             f"umbral &gt; {ms.UNATTRIBUTED_SPEND_PCT:.0f}% del gasto")
    ]
    done_n = len(board["done24"])
    content.append(_kpi("Done 24h", str(done_n),
                       ", ".join(r["id"] for r in board["done24"][:4])
                       + ("…" if done_n > 4 else "")))
    if budget:
        ratio = budget.get("supply_ratio")
        ratio_s = f"{ratio:.2f}" if isinstance(ratio, (int, float)) else "n/d"
        content.append(_kpi(
            "supply_ratio 24h", ratio_s,
            f'{budget.get("supply_created_24h", "?")} creadas / '
            f'{budget.get("supply_closed_24h", "?")} cerradas'))
        bal = budget.get("nanogpt_balance_usd")
        lvl = budget.get("nanogpt_budget_level") or "?"
        lvl_cls = _LEVEL_BADGE.get(str(lvl), "mut")
        content.append(_kpi(
            "Saldo NanoGPT", _usd2(bal) if bal is not None else "n/d",
            f'presupuesto: <span class="badge {lvl_cls}">{_esc(lvl or "?")}</span>'))
    else:
        content.append(_kpi("supply_ratio 24h", "n/d"))
    return ["<section class=\"kpis\">"] + content + ["</section>"]

# ---------------------------------------------------------------------------
# Helper: class spend section
# ---------------------------------------------------------------------------

def _gasto_por_clase_section(cls_sorted: list, agg: dict, series: list) -> list:
    body = [
        f'<div class="headrow"><h2>Gasto — por clase de consumo</h2>'
        f'<span class="sparkrow"><small>gasto $/día, últimos {DAYS} días</small> '
        f'{sparkline(v for _, v in series)}</span></div>'
    ]
    body.append(_spend_table(cls_sorted, agg["total_usd"]))
    return ["<div class=\"grid\">", '<section class=\"card\">' + "".join(body) + '</section>'] + ["</div>"]

# ---------------------------------------------------------------------------
# Helper: objectives section
# ---------------------------------------------------------------------------

def _objetivos_section(obj_sorted: list, agg: dict) -> list:
    body = [
        '<h2>Gasto — por objetivo (top 8)</h2>'
    ]
    body.append(_spend_table(obj_sorted, agg["total_usd"]))
    return [f'<section class=\"card\">' + "".join(body) + '</section>']

# ---------------------------------------------------------------------------
# Helper: forecast section
# ---------------------------------------------------------------------------

def _forecast_section(verdicts: list, fc: dict) -> list:
    prov_rows = []
    for v in verdicts:
        pct = v["pct"]
        pct_s = f"{pct:.1f}%" if isinstance(pct, (int, float)) else "-"
        pv = pct if isinstance(pct, (int, float)) else 0.0
        pcls = "bad" if pv > 90 else ("warn" if pv > 75 else "")
        bcls, label = _VERDICT_BADGE[v["status"]]
        prov_rows.append(
            f'<tr><td>{_esc(v["name"])}</td>'
            f'<td class="num">{_esc(pct_s)}</td>'
            f'<td><div class="bar {pcls}"><i style="width:{min(pv, 100.0):.0f}%"></i></div></td>'
            f'<td><span class="badge {bcls}">{label}</span></td>'
            f'<td class="mut" style="font-size:11px">{_esc(v["text"])}</td></tr>')
    reset_s = ""
    reset_iso = fc.get("next_weekly_reset_iso")
    if reset_iso:
        htr = fc.get("hours_to_reset")
        htr_s = f" en {htr:.1f}h" if isinstance(htr, (int, float)) else ""
        reset_s = (f'<div class="mut" style="font-size:12px;margin-top:8px">'
                   f"reset semanal{htr_s} · {_esc(reset_iso)}</div>")
    if prov_rows:
        title = '<section class="card"><h2>Forecast — burn y veredicto (gate)</h2>'
        title += '<table><tr><th>provider</th>'
        title += '<th class=num>% ahora</th><th>carga</th><th>veredicto</th>'
        title += '<th></th></tr>' + "".join(prov_rows) + "</table>"
        title += reset_s + '</section>'
    else:
        title = '<section class="card"><h2>Forecast — burn y veredicto (gate)</h2>'
        title += '<div class=mut>sin forecast todavía</div></section>'
    return [title]

# ---------------------------------------------------------------------------
# Helper: board section
# ---------------------------------------------------------------------------

def _board_section(board: dict) -> list:
    chips = "".join(
        f'<span class="chip">{_esc(k)}={v}</span>'
        for k, v in sorted(board["counts"].items()))
    done_li = "".join(
        f'<li><span class="mut">{_esc(ms._fmt_ts(r.get("completed_at")))}</span> '
        f'<b>{_esc(r["id"])}</b> {_esc((r.get("title") or "")[:60])}</li>'
        for r in board["done24"][:8])
    act_li = "".join(
        f'<li><b>{_esc(r["id"])}</b> '
        f'<span class="badge mut">{_esc(r["status"])}</span> '
        f'<span class="mut">{_esc(r.get("assignee") or "")}</span> '
        f'{_esc((r.get("body") or "").replace(chr(10), " ").strip()[:70])}</li>'
        for r in board["active"][:8])
    return [f'<section class="card"><h2>Board</h2>'
            f'<div>{chips or "<span class=mut>sin board</span>"}</div>'
            f'<div class=mut style="font-size:11px;margin:2px 0 8px">'
            f'total {board["total"]} tareas</div>'
            f'<ul class="list">{done_li}</ul>'
            f'<ul class="list">{act_li}</ul></section>']

# ---------------------------------------------------------------------------
# Helper: alerts section
# ---------------------------------------------------------------------------

def _alerts_section(alerts: list) -> list:
    cards = "".join(
        f'<div class="alert {c["kind"]}">{_esc(c["text"])}</div>'
        for c in alerts)
    return [f'<section class="card"><h2>Alertas</h2>{cards}</section>']

# ---------------------------------------------------------------------------
# Build the actual HTML
# ---------------------------------------------------------------------------

def _window_suffix(agg: dict) -> str:
    """Trace window fragment for the header ('' when the trace is empty)."""
    if agg["first_ts"] is None:
        return ""
    return (
        f" · ventana trace {ms._fmt_ts(agg['first_ts'])} →"
        f" {ms._fmt_ts(agg['last_ts'])}")


def _top_objetivos(agg: dict) -> list:
    """Top-8 objectives by spend; 'unattributed' is always kept in the cut."""
    obj_sorted = sorted(agg["by_obj"].items(),
                        key=lambda kv: (-kv[1]["usd"], -kv[1]["n"]))
    top = obj_sorted[:8]
    if "unattributed" in agg["by_obj"] and \
            "unattributed" not in {n for n, _ in top} and top:
        top = top[:-1] + [("unattributed", agg["by_obj"]["unattributed"])]
    return top


def _page_header(agg: dict, now: float) -> list:
    """<head> plus page header with the generation stamp and trace window."""
    gen = dt.datetime.fromtimestamp(now, CEST).strftime("%d-%b %H:%M (UTC+2)")
    win = _window_suffix(agg)
    return [
        "<!doctype html>",
        '<html lang="es">', "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>La casa — observabilidad</title>",
        f"<style>{_CSS}</style>", "</head>", "<body>", '<div class="wrap">',
        "<header><h1>La casa — observabilidad</h1>",
        '<div class="gen">generado ' + gen + win + ' · OBJ-32 v0 · solo lectura</div></header>',
    ]


def _page_footer() -> list:
    """Read-only sources footer plus the closing tags."""
    return [
        ' <footer>fuentes (solo lectura): quota-governor/obs/trace.jsonl · ' +
                 'quota-governor/forecast.json · quota-governor/metrics-history.jsonl · ' +
                 'kanban.db<br>regenerar: <code>python3 scripts/obs/obs-dashboard.py</code>' +
                 ' · servir en vivo: <code>--serve</code> (127.0.0.1 ' +
                 'únicamente) · OBJ-32 v0 — stdlib, cero dependencias</footer>',
        "</div>", "</body>", "</html>"]


def _gather_dashboard_data(hermes_home=None, now: float = 0.0) -> dict:
    """Read every read-only source and compute all render inputs."""
    rows = read_trace(hermes_home)
    agg = agg_trace(rows)
    fc = ms._read_json(ms.forecast_path(hermes_home))
    board = read_board(hermes_home)
    budget = read_metrics(hermes_home)
    budget = budget[-1] if budget and isinstance(budget[-1], dict) else {}
    return {
        "rows": rows, "agg": agg,
        "series": daily_series(rows, now=now),
        "fc": fc, "board": board, "budget": budget,
        "verdicts": provider_verdicts(fc),
        "alerts": alert_cards(hermes_home),
    }


def _render_dashboard(d: dict, now: float) -> str:
    """Assemble the page from gathered data (pure string work)."""
    agg, board, fc = d["agg"], d["board"], d["fc"]
    lines = _page_header(agg, now)
    if not bool(d["rows"] or board["db"] or fc.get("providers")):
        lines.append('<div class="banner">sin fuentes todavía — no se '
                     'encontraron trace, forecast, ni kanban.db bajo el '
                     'HERMES_HOME activo; revisa cómo lo fija el cron de '
                     'morning-screen.</div>')
    lines += _kpi_section(agg, board, d["budget"], now)
    cls_sorted = sorted(agg["by_class"].items(),
                        key=lambda kv: (-kv[1]["usd"], -kv[1]["n"]))
    lines += _gasto_por_clase_section(cls_sorted, agg, d["series"])
    lines += _objetivos_section(_top_objetivos(agg), agg)
    lines += _forecast_section(d["verdicts"], fc)
    lines += _board_section(board)
    if d["alerts"]:
        lines += _alerts_section(d["alerts"])
    lines += _page_footer()
    return "\n".join(lines)


def build_html(hermes_home=None, now=None) -> str:
    """The whole page. Pure function of the read‑only sources."""
    now = time.time() if now is None else float(now)
    data = _gather_dashboard_data(hermes_home, now)
    return _render_dashboard(data, now)

# ---------------------------------------------------------------------------
# Write file helper
# ---------------------------------------------------------------------------

def write_dashboard(hermes_home=None, out=None, now=None) -> Path:
    """Build and write the page; returns the path written."""
    page = build_html(hermes_home=hermes_home, now=now)
    path = Path(out) if out else dashboard_path(hermes_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(page, encoding="utf-8")
    return path

# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

def make_server(port: int, hermes_home=None) -> http.server.ThreadingHTTPServer:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            route = self.path.split("?", 1)[0]
            if route not in ("/", "/index.html", "/dashboard.html"):
                self.send_error(404)
                return
            try:
                page = build_html(hermes_home=hermes_home)
            except Exception as exc:  # never leak a stack trace to the page
                page = ("<!doctype html><meta charset=utf-8><pre>error "
                        f"generando: {html.escape(repr(exc))}</pre>")
            data = page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def log_message(self, format, *args):  # noqa: A002
            pass  # quiet: a dashboard shouldn't spam
    return http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--serve", nargs="?", const=DEFAULT_PORT, type=int,
                    metavar="PORT",
                    help=f"serve on 127.0.0.1 (default port {DEFAULT_PORT}) "
                         "and regenerate on every request")
    ap.add_argument("--out", default=None,
                    help="write the page here instead of the default "
                         "obs/dashboard.html")
    args = ap.parse_args(argv)
    try:
        if args.serve is not None:
            srv = make_server(args.serve)
            port = srv.server_address[1]
            print(f"obs dashboard: http://127.0.0.1:{port}  (Ctrl-C para salir)")
            try:
                srv.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                srv.server_close()
            return 0
        path = write_dashboard(out=args.out)
        print(path)
        return 0
    except Exception as exc:
        print(f"obs-dashboard: {exc!r}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
