#!/usr/bin/env python3
"""weekly-progress.py — OBJ-08: multi-objective progress tracker + weekly report.

Closes the loop described in
~/.hermes/profiles/pr-ollama/docs/autonomous-objectives.md sections 4 and 7:

  1. Regenerates ~/.hermes/quota-governor/objective-progress.json with the
     documented multi-objective structure (§4): one entry per OBJ-NN with
     status, last_task_created, tasks_completed — derived entirely from
     kanban.db via the `objective:OBJ-NN` header tag on task bodies.
  2. Produces a brief weekly markdown report (§7 "Prioridad semanal") at
     ~/.hermes/profiles/pr-ollama/docs/weekly-reports/YYYY-WW-weekly-summary.md
     with: objectives completed vs in progress, tasks completed this week,
     and quota consumed per provider.

STATUS RULES (derived from §4 "Reglas de completitud" + board reality):
  - not_started     : objective declared in the doc, zero tagged tasks.
  - in_progress     : at least one open task (running|ready|todo|blocked|triage).
  - complete        : all tagged tasks terminal-completed (done, or archived
                      with completed_at — board-cleanup archives done tasks
                      >7d later, same convention as objective-backup.py).
  - awaiting_human  : all done but the objective is on MANUAL_VERIFY_OBJECTIVES
                      (§4: the governor must not self-declare these complete).
  - needs_attention : tasks were lost (archived WITHOUT completed_at = gave_up/
                      crashed/reclaimed) — completion evidence is gone.

Lost-task semantics (OBJ-08 follow-up, t_78acb6dc):
  An archived-without-completed_at task counts as RESOLVED, not lost, when its
  tag header carries an explicit `abandoned: <reason>` line. The convention:
  a human/board-cleanup stamps the note when archiving a gave-up attempt whose
  work was superseded (re-attempted and/or completed by other tasks of the
  same objective). Tasks lost WITHOUT the note keep needs_attention forever —
  an orphan whose work was never replaced must still be visible. The stamp is
  written by abandon-superseded.py (same tag-header convention as cost-tag-fix
  and objective: tags); the detector NEVER trusts prose mentions.
  Known hole (accepted): tasks archived by the pressure-relief auto-archiver
  WITH completed_at set are counted as complete (pre-existing convention);
  tasks force-archived from running WITHOUT a stamp and WITHOUT supersession
  evidence stay lost, which is the intended fail-loud default.

Idempotency: the JSON is fully regenerated from the board on every run and
written atomically (tmp + os.replace). Re-running never duplicates or
corrupts. The weekly report filename is fixed per ISO week, so re-runs
overwrite in place.

Quota sources (read-only):
  - ~/.hermes/quota-governor/model-cost-ledger.jsonl  → USD per profile/model
  - ~/.hermes/quota-governor/burn-ledger.jsonl        → latest window % per
                                                        provider (burn-watchdog)

Usage:
  weekly-progress.py                    # dry-run: print both artifacts, write nothing
  weekly-progress.py --execute          # write objective-progress.json + report
  weekly-progress.py --execute --week 2026-37   # force a specific ISO week
  weekly-progress.py --progress-only / --report-only

Cron registration (Sundays ~23:00 UTC, no_agent wrapper, see
weekly-progress-cron.sh — the cron layer delivers non-empty stdout):
  hermes cron create weekly-progress --name weekly-progress \
      --script weekly-progress-cron.sh --no-agent --deliver local "0 23 * * 0"

All paths are overridable via env for tests:
  WP_KANBAN_DB, WP_STATE_DIR, WP_REPORT_DIR, WP_COST_LEDGER, WP_BURN_LEDGER,
  WP_OBJECTIVES_DOC.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ── Paths (env-overridable for tests) ───────────────────────────────────────
# NB: deliberately NOT reusing HERMES_HOME — in worker/cron contexts it can
# point at a profile dir (~/.hermes/profiles/<p>), while this tracker's data
# lives under the top-level ~/.hermes (same assumption as daily-report.py).
BASE_HOME = Path(os.environ.get("WP_HERMES_HOME",
                                os.path.expanduser("~/.hermes")))
KANBAN_DB = Path(os.environ.get("WP_KANBAN_DB", BASE_HOME / "kanban.db"))
STATE_DIR = Path(os.environ.get("WP_STATE_DIR", BASE_HOME / "quota-governor"))
PROGRESS_FILE = STATE_DIR / "objective-progress.json"
REPORT_DIR = Path(os.environ.get(
    "WP_REPORT_DIR",
    BASE_HOME / "profiles" / "pr-ollama" / "docs" / "weekly-reports"))
COST_LEDGER = Path(os.environ.get(
    "WP_COST_LEDGER", STATE_DIR / "model-cost-ledger.jsonl"))
BURN_LEDGER = Path(os.environ.get("WP_BURN_LEDGER", STATE_DIR / "burn-ledger.jsonl"))
OBJECTIVES_DOC = Path(os.environ.get(
    "WP_OBJECTIVES_DOC",
    BASE_HOME / "profiles" / "pr-ollama" / "docs" / "autonomous-objectives.md"))

PROGRESS_VERSION = "2.1"

# §4: OBJ-01 is the base of everything — human verification required.
MANUAL_VERIFY_OBJECTIVES = {"OBJ-01"}

# Header-tag convention identical to triage-bridge.py / objective-proposer.py:
# tags live in the block before the first blank line. Group 1 = full token
# (e.g. OBJ-08, OBJ-P2-PRIV); group 2 = the numeric core used for sorting.
OBJECTIVE_RE = re.compile(r"\bobjective\s*:\s*(OBJ-([A-Za-z0-9][A-Za-z0-9._-]*))", re.I)
DOC_HEADER_RE = re.compile(r"^###+\s*(OBJ-\d+)\b", re.I)
OPEN_STATUSES = {"running", "ready", "todo", "blocked", "triage"}
# OBJ-08/t_78acb6dc: an archived task WITHOUT completed_at is only "resolved"
# (abandoned attempt superseded by other work of the same objective) when the
# tag header carries this explicit stamp. Header-only: prose never counts.
ABANDONED_TAG_RE = re.compile(r"^abandoned\s*:", re.I)


def header_of(body: str) -> str:
    """Tag header: text up to the first blank line (triage-bridge convention)."""
    lines = []
    for line in (body or "").splitlines():
        if not line.strip():
            break
        lines.append(line)
    return "\n".join(lines)


def header_lines(body: str) -> List[str]:
    """Tag header as a list of lines (same convention as header_of)."""
    return header_of(body).splitlines()


def is_abandoned(body: str) -> bool:
    """True iff the tag HEADER carries an `abandoned:` line (t_78acb6dc)."""
    return any(ABANDONED_TAG_RE.match(ln) for ln in header_lines(body or ""))


def iso(ts: Optional[int]) -> Optional[str]:
    if not ts:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


# ── Kanban data ─────────────────────────────────────────────────────────────

def load_task_objectives(db_path: Path) -> Dict[str, List[Dict[str, Any]]]:
    """Group every tagged task by objective id (uppercase, OBJ-X placeholders
    and malformed ids ignored). Returns {OBJ-ID: [task dicts]}."""
    tasks_by_obj: Dict[str, List[Dict[str, Any]]] = {}
    if not db_path.is_file():
        return tasks_by_obj
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, title, status, body, created_at, started_at, completed_at "
            "FROM tasks").fetchall()
    finally:
        conn.close()
    for r in rows:
        m = OBJECTIVE_RE.search(header_of(r["body"] or ""))
        if not m:
            continue
        obj = m.group(1).upper()
        if obj == "OBJ-X":
            continue
        tasks_by_obj.setdefault(obj, []).append({
            "id": r["id"], "title": r["title"] or "", "status": r["status"],
            "created_at": r["created_at"], "started_at": r["started_at"],
            "completed_at": r["completed_at"],
            # OBJ-08/t_78acb6dc: header carries an explicit abandoned: stamp
            # (see abandon-superseded.py) -> archived-without-completed_at
            # counts as RESOLVED, not lost.
            "abandoned": is_abandoned(r["body"] or ""),
        })
    return tasks_by_obj


def declared_objectives(doc_path: Path) -> List[str]:
    """OBJ ids declared as headers in the autonomous-objectives doc."""
    if not doc_path.is_file():
        return []
    ids = []
    for line in doc_path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = DOC_HEADER_RE.match(line.strip())
        if m:
            obj = m.group(1).upper()
            if obj not in ids:
                ids.append(obj)
    return ids


# ── §4 progress structure ───────────────────────────────────────────────────

def build_progress(tasks_by_obj: Dict[str, List[Dict[str, Any]]],
                   declared: List[str],
                   now: Optional[datetime] = None) -> Dict[str, Any]:
    """Pure function: objective-progress.json document (§4 shape + audit fields)."""
    now = now or datetime.now(timezone.utc)
    all_ids = sorted(set(declared) | set(tasks_by_obj), key=_obj_sort_key)
    doc: Dict[str, Any] = {
        "_meta": {
            "version": PROGRESS_VERSION,
            "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": str(KANBAN_DB),
            "note": ("Auto-generated by weekly-progress.py (OBJ-08). Manual edits "
                     "are overwritten. OBJ-01-class objectives need human "
                     "verification (see autonomous-objectives.md §4)."),
        }
    }
    for obj in all_ids:
        tasks = tasks_by_obj.get(obj, [])
        if not tasks:
            doc[obj] = {
                "status": "not_started",
                "last_task_created": None,
                "tasks_completed": 0,
                "tasks_total": 0,
                "tasks_open": 0,
                "tasks_lost": 0,
            }
            continue
        completed = [t for t in tasks if t["status"] == "done"
                     or (t["status"] == "archived" and t["completed_at"])]
        lost = [t for t in tasks if t["status"] == "archived"
                and not t["completed_at"]
                and not t.get("abandoned")]
        open_t = [t for t in tasks if t["status"] in OPEN_STATUSES]
        if open_t:
            status = "in_progress"
        elif lost:
            status = "needs_attention"
        elif obj in MANUAL_VERIFY_OBJECTIVES:
            status = "awaiting_human_verification"
        else:
            status = "complete"
        last_created = max((t["created_at"] or 0 for t in tasks)) or None
        last_activity = max([t["created_at"] or 0 for t in tasks]
                            + [t["completed_at"] or 0 for t in tasks]
                            + [t["started_at"] or 0 for t in tasks]) or None
        entry: Dict[str, Any] = {
            "status": status,
            "last_task_created": iso(last_created),
            "tasks_completed": len(completed),
            "tasks_total": len(tasks),
            "tasks_open": len(open_t),
            "tasks_lost": len(lost),
        }
        if last_activity:
            days = (now - datetime.fromtimestamp(last_activity, tz=timezone.utc)).days
            entry["days_since_activity"] = max(days, 0)
        doc[obj] = entry
    return doc


def _obj_sort_key(obj_id: str) -> int:
    m = re.search(r"(\d+)$", obj_id)
    return int(m.group(1)) if m else 10**6


# ── Weekly window + report ──────────────────────────────────────────────────

def parse_week(week_arg: Optional[str], now: datetime) -> Tuple[int, int, datetime, datetime]:
    """(iso_year, iso_week, start_utc, end_utc) for 'YYYY-WW' or current week."""
    if week_arg:
        m = re.fullmatch(r"(\d{4})-W?(\d{1,2})", week_arg.strip())
        if not m:
            raise ValueError(f"--week debe ser YYYY-WW, p.ej. 2026-37 (recibido: {week_arg!r})")
        year, week = int(m.group(1)), int(m.group(2))
    else:
        year, week, _ = now.isocalendar()
    start = datetime.fromisoformat(
        date.fromisocalendar(year, week, 1).isoformat()).replace(tzinfo=timezone.utc)
    return year, week, start, start + timedelta(days=7)


def load_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows = []
    if not path.is_file():
        return rows
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # tolerate torn last line, never crash the cron
    return rows


def week_sections(tasks_by_obj: Dict[str, List[Dict[str, Any]]],
                  progress: Dict[str, Any],
                  start: datetime, end: datetime) -> Dict[str, List]:
    """Classify activity for the report: completions in window, status lists."""
    done_window, advanced = [], {}
    for obj, tasks in tasks_by_obj.items():
        for t in tasks:
            ts = t["completed_at"]
            if not ts:
                continue
            if t["status"] != "done" and not (t["status"] == "archived"):
                continue
            when = datetime.fromtimestamp(int(ts), tz=timezone.utc)
            if start <= when < end:
                done_window.append((when, obj, t))
                advanced[obj] = advanced.get(obj, 0) + 1
    done_window.sort(key=lambda x: x[0])
    statuses = {k: v["status"] for k, v in progress.items()
                if isinstance(v, dict) and "status" in v}
    return {
        "done_window": done_window,
        "advanced": sorted(advanced.items()),
        "complete": sorted(o for o, s in statuses.items()
                           if s in ("complete", "awaiting_human_verification")),
        "in_progress": sorted(o for o, s in statuses.items()
                              if s == "in_progress"),
        "attention": sorted(o for o, s in statuses.items()
                            if s == "needs_attention"),
        "not_started": sorted(o for o, s in statuses.items()
                              if s == "not_started"),
    }


def quota_lines(start: datetime, end: datetime) -> List[str]:
    """USD from model-cost-ledger + latest window % per provider (burn-ledger)."""
    out = []
    usd: Dict[Tuple[str, str], float] = {}
    for row in load_jsonl(COST_LEDGER):
        ts = row.get("ts")
        if not ts or not (start <= datetime.fromtimestamp(ts, tz=timezone.utc) < end):
            continue
        key = (row.get("profile") or "?", row.get("model") or "?")
        usd[key] = usd.get(key, 0.0) + float(row.get("cost") or 0.0)
    total = sum(usd.values())
    if usd:
        out.append(f"- Coste medido (model-cost-ledger): **${total:.2f}** en la semana")
        for (profile, model), v in sorted(usd.items(), key=lambda kv: -kv[1]):
            out.append(f"  - {profile} / {model}: ${v:.2f}")
    else:
        out.append("- Coste medido: sin filas del ledger en la ventana")
    latest: Dict[str, Dict[str, Any]] = {}
    for row in load_jsonl(BURN_LEDGER):
        ts = row.get("ts")
        if not ts or not (start <= datetime.fromtimestamp(ts, tz=timezone.utc) < end):
            continue
        prov = row.get("provider") or "?"
        if prov not in latest or ts >= latest[prov].get("ts", 0):
            latest[prov] = row
    if latest:
        out.append("- Cuota fin de semana (burn-ledger, % de ventana):")
        for prov in sorted(latest):
            pct = latest[prov].get("window_pct")
            win = latest[prov].get("window") or "?"
            out.append(f"  - {prov}: {('%.1f%%' % pct) if pct is not None else 'n/d'} "
                       f"(ventana {win})")
    return out


def render_report(year: int, week: int, start: datetime, end: datetime,
                  sections: Dict[str, List], progress: Dict[str, Any],
                  quota: List[str], now: datetime) -> str:
    lines = [
        f"# Resumen semanal de objetivos — {year}-W{week:02d}",
        "",
        f"Periodo (UTC): {start:%Y-%m-%d} → {end - timedelta(microseconds=1):%Y-%m-%d}. "
        f"Generado {now.strftime('%Y-%m-%dT%H:%M:%SZ')} por weekly-progress.py (OBJ-08).",
        "",
        "## Objetivos",
        "",
        f"- Completos (o esperando verificación humana): {len(sections['complete'])} — "
        + (", ".join(sections["complete"]) or "ninguno"),
        f"- En curso: {len(sections['in_progress'])} — "
        + (", ".join(sections["in_progress"]) or "ninguno"),
    ]
    if sections["attention"]:
        lines.append(f"- Necesitan atención (tareas perdidas): "
                     + ", ".join(sections["attention"]))
    if sections["not_started"]:
        lines.append(f"- Sin comenzar: " + ", ".join(sections["not_started"]))
    lines += ["", f"## Actividad completada esta semana ({len(sections['done_window'])} tareas)"]
    lines += ["", f"- Avanzaron: " + ", ".join(f"{o} ({n})" for o, n in sections["advanced"])
              if sections["advanced"] else "- Sin tareas completadas en la ventana."]
    if sections["done_window"]:
        lines.append("")
        lines.append("<details><summary>Tareas (mas reciente primero)</summary>")
        for when, obj, t in reversed(sections["done_window"]):
            title = (t["title"] or "")[:80]
            lines.append(f"- {when:%m-%d %H:%M} [{obj}] {t['id']} — {title}")
        lines.append("</details>")
    lines += ["", "## Cuota consumida", ""] + (quota or ["- Sin datos de cuota."])
    lines.append("")
    return "\n".join(lines)


# ── Writing (atomic) ────────────────────────────────────────────────────────

def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="OBJ-08: regenerate objective-progress.json (§4) and emit "
                    "the weekly progress report (§7). Dry-run by default.")
    ap.add_argument("--execute", action="store_true",
                    help="write objective-progress.json AND the weekly report "
                         "(default: dry-run, prints only)")
    ap.add_argument("--progress-only", action="store_true", help="skip the report")
    ap.add_argument("--report-only", action="store_true", help="skip the JSON")
    ap.add_argument("--week", help="ISO week 'YYYY-WW' for the report (default: current)")
    ap.add_argument("--verbose", action="store_true", help="also print full artifacts on --execute")
    args = ap.parse_args(argv)

    if args.progress_only and args.report_only:
        print("--progress-only y --report-only son excluyentes", file=sys.stderr)
        return 2
    now = datetime.now(timezone.utc)
    try:
        year, week, start, end = parse_week(args.week, now)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2

    tasks_by_obj = load_task_objectives(KANBAN_DB)
    if not tasks_by_obj:
        print(f"weekly-progress: sin tareas etiquetadas en {KANBAN_DB} — "
              f"posible DB inaccesible, no se escribe nada", file=sys.stderr)
        return 1
    progress = build_progress(tasks_by_obj, declared_objectives(OBJECTIVES_DOC), now)
    json_text = json.dumps(progress, indent=2, ensure_ascii=False, sort_keys=False) + "\n"

    report_text = ""
    if not args.progress_only:
        sections = week_sections(tasks_by_obj, progress, start, end)
        report_text = render_report(year, week, start, end, sections, progress,
                                    quota_lines(start, end), now)
    report_path = REPORT_DIR / f"{year}-{week:02d}-weekly-summary.md"

    if not args.execute:
        print(f"[dry-run] escribiria {PROGRESS_FILE} ({len(json_text)} bytes)")
        print(f"[dry-run] escribiria {report_path} ({len(report_text)} bytes)"
              if report_text else "[dry-run] --progress-only, sin reporte")
        if args.verbose:
            print(json_text)
            if report_text:
                print(report_text)
        return 0

    if not args.report_only:
        atomic_write(PROGRESS_FILE, json_text)
    if report_text:
        atomic_write(report_path, report_text)
    statuses = [v["status"] for v in progress.values()
                if isinstance(v, dict) and "status" in v]
    summary = (f"weekly-progress {year}-W{week:02d}: "
               f"{len(statuses)} objetivos "
               f"({statuses.count('complete')} completos, "
               f"{statuses.count('in_progress')} en curso, "
               f"{statuses.count('needs_attention')} atención, "
               f"{statuses.count('awaiting_human_verification')} p/verificar); "
               f"reporte: {report_path.name}" if report_text else
               f"weekly-progress: objective-progress.json regenerado "
               f"({len(statuses)} objetivos)")
    print(summary)
    if args.verbose:
        print(json_text)
        if report_text:
            print(report_text)
    return 0


if __name__ == "__main__":
    sys.exit(main())