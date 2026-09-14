#!/usr/bin/python3.12
"""efficiency-ratio.py — P4: efficiency ratio of the standing budget.

Question answered (zero tokens, stdlib only, read-only inputs):

    efficiency_ratio = tareas_done_con_success_criterion_verificado
                       / gasto_usd_24h(AUTODEV + AUTOREPAIR)

Task side (strict, never inflated):
  Done tasks closed in the window whose body targets the standing budget —
  header/anywhere tag `objective:AUTODEV` or `objective:AUTOREPAIR`
  (case-insensitive, whole body — OBJ-20 lesson: some bodies carry the tag
  only in the footer). A task counts as VERIFIED only when:
    - multi-part body (>=2 declared R1/R2/... parts): every declared part is
      evidenced or waived — the SAME verdict tick_body_parts.scan() produces
      (loaded lazily; if unavailable, multi-part tasks count as NOT verified
      and the output notes verifier=limited), or
    - no parts: the body declares a success criterion (`success:` tag in the
      header block, or a `success criterion:` / `criterio de éxito:` line)
      AND the worker output channels (result + run summaries + comments)
      both mention the criterion tokens and carry a completion word. A done
      task with no declaration and no evidence NEVER counts (honesty rule).

Spend side (documented base):
  trace.jsonl lines in the window with costUsd. STRICT spend = lines whose
  objective is AUTODEV/AUTOREPAIR — either the trace already says so, or the
  line's consumer_id is a task id (t_xxx) we can join to the board body.
  Today the cost-bearing sources (usage-audit, nanogpt-requests) carry NO
  task_id (objective-budgets.json documents cost_lines_attributed=0), so the
  strict base is usually $0.0 — when strict == 0 the ratio falls back to the
  TOTAL window spend and marks base_mode="proxy-total-24h" in the output,
  never hiding the gap. spend == 0 both ways -> ratio null, SIN GASTO.

Output: ONE line appended to the shared metrics-history.jsonl (same file as
supply_ratio), kind="efficiency_ratio", with 24h and 7d aggregates (weekly
ratio = total verified / total spend, never a mean of ratios). Silent stdout
on success (watchdog pattern); on unreadable sources it appends
{"ratio": null, "veredicto": "N/A"} and prints "efficiency-ratio: cannot
compute" — the cron never aborts (fail-open). DIRECCION-STOP is irrelevant
here: the script is read-only observability and always computes.

Usage:
  efficiency-ratio.py              # compute + append + silent (exit 0)
  efficiency-ratio.py --dry-run    # compute + print the JSONL line, no write
  efficiency-ratio.py --verbose    # also print the summary on success

Env overrides (tests / non-standard hosts):
  ER_KANBAN_DB, ER_TRACE, ER_METRICS (input+output metrics-history path),
  ER_RUNS_TABLES=0 to skip task_runs/task_comments evidence in fixtures.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
import unicodedata
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths (explicit profile defaults, like quota-metrics.py; env-overridable)
# ---------------------------------------------------------------------------

_HERMES_ROOT = Path(os.environ.get("ER_HERMES_ROOT",
                                   os.path.expanduser("~/.hermes")))
PROFILE_HOME = _HERMES_ROOT / "profiles" / "pr-ollama"


def kanban_db_path() -> Path:
    env = os.environ.get("ER_KANBAN_DB", "").strip()
    if env:
        return Path(env)
    root_db = _HERMES_ROOT / "kanban.db"          # the shared board (root)
    return root_db if root_db.exists() else \
        PROFILE_HOME / "kanban.db"


def trace_path() -> Path:
    env = os.environ.get("ER_TRACE", "").strip()
    if env:
        return Path(env)
    p = PROFILE_HOME / "quota-governor" / "obs" / "trace.jsonl"
    return p if p.exists() else \
        _HERMES_ROOT / "quota-governor" / "obs" / "trace.jsonl"


def metrics_path() -> Path:
    env = os.environ.get("ER_METRICS", "").strip()
    if env:
        return Path(env)
    return PROFILE_HOME / "quota-governor" / "metrics-history.jsonl"


WINDOWS = {"24h": 86400, "7d": 7 * 86400}

# Standing-budget objectives (the only ones the standing budget pays for).
# MEDIATOR t_4fa0a4b5 (Sep 2026): the budget moved from the legacy
# AUTODEV/AUTOREPAIR namespace to the approved_objectives TABLE
# (~/.hermes/kanban.db).  A task counts when its objective tag names a
# table row — legacy names stay valid (they seeded the table).  The table
# is read once per compute(); missing/unreadable table degrades to the
# legacy pair (fail-open, same spirit as §6's fail-open for untagged).
LEGACY_BUDGET_OBJECTIVES = {"AUTODEV", "AUTOREPAIR"}
_objectives_cache: dict = {}


def budget_objectives(db_path: Path) -> set:
    """Objective ids the standing budget pays for: approved_objectives
    rows (any status — spend before achievement still counts) plus the
    legacy AUTODEV/AUTOREPAIR names."""
    if "ids" in _objectives_cache:
        return _objectives_cache["ids"]
    ids = set(LEGACY_BUDGET_OBJECTIVES)
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            for (oid,) in con.execute(
                    "SELECT id FROM approved_objectives"):
                if oid:
                    ids.add(str(oid).upper())
        finally:
            con.close()
    except sqlite3.Error:
        pass  # table missing / no db — legacy pair only (fail-open)
    _objectives_cache["ids"] = ids
    return ids


_OBJECTIVES_RES_CACHE: dict = {}


def _objective_res(db_path: Path):
    """(BUDGET_OBJECTIVES_set, OBJECTIVE_RE) cached per compute() call —
    the regex alternation must cover every table id, not only the legacy
    pair, or objective:OBJ-CODEQUALITY spend is invisible to the ratio."""
    ids = budget_objectives(db_path)
    cached = _OBJECTIVES_RES_CACHE.get(frozenset(ids))
    if cached is not None:
        return cached
    if ids == LEGACY_BUDGET_OBJECTIVES:
        regex = re.compile(
            r"\bobjective\s*:\s*(AUTODEV|AUTOREPAIR)\b", re.I)
    else:
        alt = "|".join(re.escape(i) for i in sorted(ids))
        regex = re.compile(
            r"\bobjective\s*:\s*(" + alt + r")\b", re.I)
    pair = (ids, regex)
    _OBJECTIVES_RES_CACHE[frozenset(ids)] = pair
    return pair

SUCCESS_TAG_RE = re.compile(r"(?i)^\s*success\s*[:\-]\s*(.+?)\s*$")
SUCCESS_PROSE_RE = re.compile(
    r"(?i)^\s*(?:success criterion|criterio de [ée]xito)\s*[:\-]\s*(.+?)\s*$")

# Same completion vocabulary as tick_body_parts (kept in sync by convention).
COMPLETION_RE = re.compile(
    r"(?i)\b(?:done|completad[oa]s?|hech[oa]s?|ejecutad[oa]s?|finished"
    r"|completed|resolved|pass(?:ed)?|ok|logged|registrad[oa]s?"
    r"|extraid[oa]s?|generad[oa]s?|resumid[oa]s?|clasificad[oa]s?"
    r"|list[oa]s?|verificad[oa]s?|terminad[oa]s?)\b")

TASK_ID_RE = re.compile(r"\bt_[0-9a-f]{8}\b")

_STOPWORDS = {
    "para", "con", "los", "las", "del", "que", "esta", "este", "esto",
    "como", "por", "una", "uno", "unos", "unas", "todos", "todas",
    "sobre", "entre", "sin", "son", "sea", "mas", "muy", "the", "and",
    "for", "with", "from", "this", "that", "have", "must", "should",
}


def _norm(text: str) -> str:
    t = (text or "").lower()
    t = unicodedata.normalize("NFKD", t)
    return re.sub(r"\s+", " ", t).strip()


# ---------------------------------------------------------------------------
# tick_body_parts reuse (lazy; fail-open)
# ---------------------------------------------------------------------------

_tbp = None


def _load_tick_body_parts():
    """Import tick_body_parts from repo/deploy candidates once. None if
    unavailable -> multi-part tasks count as NOT verified (verifier limited)."""
    global _tbp
    if _tbp is not None:
        return _tbp if _tbp is not False else None
    candidates = []
    here = Path(__file__).resolve().parent
    candidates.append(here.parent / "tick_body_parts.py")      # repo layout
    env_plugin = os.environ.get("PLUGIN_DIR", "").strip()
    if env_plugin:
        candidates.append(Path(env_plugin) / "scripts" / "tick_body_parts.py")
    candidates.append(_HERMES_ROOT / "scripts" / "tick_body_parts.py")
    for cand in candidates:
        try:
            if not cand.exists():
                continue
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                f"tick_body_parts_{abs(hash(str(cand)))}", cand)
            if spec is None or spec.loader is None:
                continue
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _tbp = mod
            return mod
        except Exception:
            continue
    _tbp = False
    return None


# ---------------------------------------------------------------------------
# Task-side verification
# ---------------------------------------------------------------------------

def is_budget_task(body: str, db_path: Path | None = None) -> bool:
    """Body carries a budget-objective tag (table-driven; legacy names
    always included)."""
    target = db_path if db_path is not None else kanban_db_path()
    return bool(_objective_res(target)[1].search(body or ""))


def declared_criterion(body: str):
    """(kind, text) — ('tag'|'prose', criterion) or (None, None).

    The `success:` tag is searched in the WHOLE body (house convention:
    bodies are tag-header + blank line + prose, and tasks have carried the
    tag after the blank line; a header-only search would silently drop
    them — same lesson as abandon-superseded.py's objective search)."""
    lines = (body or "").splitlines()
    for ln in lines:
        m = SUCCESS_TAG_RE.match(ln)
        if m:
            return "tag", m.group(1)
    for ln in lines[:12]:
        m = SUCCESS_PROSE_RE.match(ln)
        if m:
            return "prose", m.group(1)
    return None, None


def criterion_evidenced(criterion: str, output_text: str) -> bool:
    """Honest single-part evidence: output mentions the criterion tokens AND
    carries a completion word. Half+ token coverage required (>=1)."""
    toks = [t for t in re.findall(r"[a-z0-9]{4,}", _norm(criterion))
            if t not in _STOPWORDS]
    if not toks:
        return False
    out = _norm(output_text)
    if not COMPLETION_RE.search(out):
        return False
    hits = sum(1 for t in set(toks) if t in out)
    return hits >= max(1, len(set(toks)) // 2)


def collect_output_text(db_path: Path, task_id: str, result_text: str,
                        include_runs: bool = True) -> str:
    parts = [result_text or ""]
    if include_runs:
        try:
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                for sql, in (
                    ("SELECT summary FROM task_runs WHERE task_id=? "
                     "AND summary IS NOT NULL",),
                    ("SELECT body FROM task_comments WHERE task_id=?",),
                ):
                    try:
                        parts.extend(r[0] or "" for r in
                                     con.execute(sql, (task_id,)))
                    except sqlite3.Error:
                        pass
            finally:
                con.close()
        except sqlite3.Error:
            pass
    return "\n".join(p for p in parts if p)


def verified_done_tasks(db_path: Path, window_s: int, now: float,
                        include_runs: bool = True):
    """Return (verified_ids, budget_done_ids, verifier_mode) for the window."""
    cutoff = now - window_s
    verifier = "full" if _load_tick_body_parts() else "limited"
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT id, body, result FROM tasks "
            "WHERE status='done' AND completed_at > ? "
            "ORDER BY completed_at DESC", (cutoff,)).fetchall()
        con.close()
    except sqlite3.Error:
        raise
    verified, budget = [], []
    for r in rows:
        body = r["body"] or ""
        if not is_budget_task(body, db_path):
            continue
        budget.append(r["id"])
        output_text = collect_output_text(db_path, r["id"], r["result"],
                                          include_runs)
        declared = None
        tbp = _load_tick_body_parts()
        parts = tbp.parse_parts(body) if tbp else {}
        if len(parts) >= 2 and verifier == "full":
            # Multi-part: reuse the house verifier (evidence + waivers).
            out_ev = tbp.evidenced_in(output_text, parts)
            body_ev = tbp.evidenced_in_body(body, parts)
            waived = tbp.waived_parts(output_text, parts)
            pending = tbp.pending_parts(parts, out_ev, body_ev, waived)
            declared = "parts"
            ok = not pending
        else:
            kind, crit = declared_criterion(body)
            if kind and crit:
                declared = kind
                ok = criterion_evidenced(crit, output_text)
            else:
                ok = False
        if ok:
            verified.append(r["id"])
    return verified, budget, verifier


# ---------------------------------------------------------------------------
# Spend side
# ---------------------------------------------------------------------------

def read_trace(path: Path):
    rows = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for ln in fh:
                try:
                    rows.append(json.loads(ln))
                except json.JSONDecodeError:
                    continue
    except OSError:
        raise
    return rows


def objective_of_row(row: dict, db_path: Path, body_objective: dict) -> str:
    budget_ids, objective_re = _objective_res(db_path)
    obj = (row.get("objective") or "").strip()
    if obj and obj.upper() in budget_ids:
        return obj.upper()
    cid = row.get("consumer_id") or ""
    if TASK_ID_RE.fullmatch(cid):
        if cid not in body_objective:
            try:
                con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
                row_ = con.execute(
                    "SELECT body FROM tasks WHERE id=?", (cid,)).fetchone()
                con.close()
                body_objective[cid] = (row_[0] or "") if row_ else ""
            except sqlite3.Error:
                body_objective[cid] = ""
        m = objective_re.search(body_objective[cid])
        if m:
            return str(m.group(1)).upper()
    return obj or "unattributed"


def window_spend(rows, window_s: int, now: float, db_path: Path):
    """Return (strict_usd, total_usd) for the window."""
    cutoff = now - window_s
    strict = total = 0.0
    cache: dict = {}
    budget_ids, _ = _objective_res(db_path)
    for r in rows:
        ts = r.get("ts_epoch_utc")
        usd = r.get("costUsd")
        if not isinstance(ts, (int, float)) or ts < cutoff:
            continue
        if not isinstance(usd, (int, float)):
            continue
        total += usd
        obj = objective_of_row(r, db_path, cache)
        if obj in budget_ids:
            strict += usd
    return strict, total


# ---------------------------------------------------------------------------
# Verdicts + emission
# ---------------------------------------------------------------------------

def verdict_for(ratio):
    if ratio is None:
        return "SIN GASTO"
    if ratio >= 5.0:
        return "EXCELENTE"
    if ratio >= 2.0:
        return "OK"
    if ratio >= 0.5:
        return "BAJO"
    return "CRITICO"


def compute(now: float | None = None, include_runs: bool = True) -> dict:
    now = time.time() if now is None else float(now)
    db = kanban_db_path()
    trace_file = trace_path()
    try:
        rows = read_trace(trace_file)
        windows = {}
        for label, w in WINDOWS.items():
            v_ids, b_ids, verifier = verified_done_tasks(db, w, now,
                                                         include_runs)
            strict, total = window_spend(rows, w, now, db)
            windows[label] = {"verified": len(v_ids), "budget_done": len(b_ids),
                              "strict_usd": strict, "total_usd": total,
                              "verifier": verifier, "verified_ids": v_ids}
    except (OSError, sqlite3.Error) as exc:
        return {"ts": _iso(now), "kind": "efficiency_ratio", "window": "24h",
                "tareas_verificadas": None, "gasto_usd": None,
                "gasto_strict_usd": None, "gasto_total_usd": None,
                "base_mode": None, "ratio": None, "veredicto": "N/A",
                "error": f"cannot compute: {exc}"}

    w24, w7 = windows["24h"], windows["7d"]

    def build(w):
        strict, total = w["strict_usd"], w["total_usd"]
        if strict > 0:
            base, mode = strict, "strict"
        elif total > 0:
            base, mode = total, "proxy-total-24h"
        else:
            base, mode = 0.0, "none"
        ratio = round(w["verified"] / base, 2) if base > 0 else None
        return base, mode, ratio, verdict_for(ratio)

    base24, mode24, ratio24, ver24 = build(w24)
    base7, mode7, ratio7, ver7 = build(w7)

    out = {
        "ts": _iso(now), "kind": "efficiency_ratio", "window": "24h",
        "tareas_verificadas": w24["verified"],
        "tareas_budget_done_24h": w24["budget_done"],
        "gasto_usd": round(base24, 6),
        "gasto_strict_usd": round(w24["strict_usd"], 6),
        "gasto_total_usd": round(w24["total_usd"], 6),
        "base_mode": mode24,
        "ratio": ratio24, "veredicto": ver24,
        "verifier": w24["verifier"],
        "tareas_verificadas_7d": w7["verified"],
        "gasto_usd_7d": round(base7, 6),
        "base_mode_7d": mode7,
        "ratio_7d": ratio7, "veredicto_7d": ver7,
    }
    return out


def _iso(now: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))


def append_metrics(entry: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="P4 efficiency ratio (zero tokens, read-only).")
    ap.add_argument("--dry-run", action="store_true",
                    help="compute and print; do not append")
    ap.add_argument("--verbose", action="store_true",
                    help="print the summary line on success too")
    args = ap.parse_args(argv)

    entry = compute()

    if entry.get("ratio") is None and entry.get("error"):
        print(f"efficiency-ratio: {entry['error']}")

    if args.dry_run or args.verbose or entry.get("error"):
        print(json.dumps(entry, ensure_ascii=False))

    if not args.dry_run:
        try:
            append_metrics(entry, metrics_path())
        except OSError as exc:
            print(f"efficiency-ratio: cannot write metrics: {exc}",
                  file=sys.stderr)
            return 0  # never abort the cron
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
