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
      both evidence the criterion and carry a completion word. Evidence is
      anchor-first (see criterion_evidenced): file paths / test names /
      command tokens the summary honestly names, falling back to ~1/3 word
      coverage for anchor-free criteria. A done task with no declaration
      and no evidence NEVER counts (honesty rule).

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

SUCCESS_TAG_RE = re.compile(r"(?i)^\s*\#*\s*success\s*[:\-]\s*(.+?)\s*$")
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


# Typographic fold: NFKD maps U+2011 (non-breaking hyphen) to U+2010, not
# to ASCII '-', so worker summaries carrying typographic characters
# (fondo‑queue‑watch, “quoted”, ellipsis…) never substring-match their
# ASCII criterion anchors. Fold them before tokenizing.
_FOLD = str.maketrans({
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "‒": "-", "–": "-", "—": "-", "―": "-", "‑": "-",
    "﹣": "-", "－": "-", "\u2010": "-", "\u00a0": " ", "…": "...",
})


def _norm(text: str) -> str:
    """lowercase + NFKD + combining-mark strip + typographic fold +
    whitespace collapse.

    OBJ-METRICS t_7aaa897c: NFKD alone leaves combining marks in place, so
    'número' split into the unmatchable ghost token 'mero' on one side while
    the other side may spell it 'numero' — dead weight in the coverage
    denominator. Folding diacritics (and typographic punctuation) makes both
    sides tokenize alike."""
    t = (text or "").lower()
    t = unicodedata.normalize("NFKD", t)
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    t = t.translate(_FOLD)
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


def declared_criterion(body: str) -> tuple[str | None, str | None]:
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
    # bare markdown header ("### Success criterion:") with the
    # criterion text on the next non-empty line
    for i, ln in enumerate(lines[:12]):
        if re.match(r"(?i)^\s*\#*\s*success\s*[:]*\s*$", ln):
            for ln2 in lines[i + 1:]:
                if ln2.strip():
                    return "prose", ln2.strip()
    return None, None


# Anchors: raw criterion runs that embed _ . / - (paths, filenames,
# dotted commands, snake_case test names) — the tokens a worker's honest
# summary quotes nearly verbatim (tested paths, touched files, rc=0 commands),
# unlike process verbs ("reportar", "resumen") that evidence never repeats.
ANCHOR_RE = re.compile(r"[a-z0-9][a-z0-9_./-]*")

# Path-like runs must carry a structural char inside the run: an underscore,
# a slash, or a dot followed by a letter (file extension / dotted command).
def _is_anchor(run: str) -> bool:
    return bool(len(run) >= 4 and ("_" in run or "/" in run or
                                   re.search(r"\.[a-z]", run)))


def _criterion_units(cn: str):
    """(anchors, words) — evidence-bearing units of a normalized criterion:
    path-like anchor runs, and standalone [a-z0-9]{4,} words not inside any
    anchor."""
    anchors = {a for a in ANCHOR_RE.findall(cn) if _is_anchor(a)}
    anchor_parts = set()
    for a in anchors:
        anchor_parts.update(t for t in re.findall(r"[a-z0-9]{4,}", a)
                            if t not in _STOPWORDS)
    words = {t for t in re.findall(r"[a-z0-9]{4,}", cn)
             if t not in _STOPWORDS and t not in anchor_parts}
    return anchors, words


def _anchor_hit(out: str, a: str) -> bool:
    """A criterion anchor counts as hit when the output quotes the full run
    or its basename (>=5 chars)."""
    if a in out:
        return True
    tail = a.rsplit("/", 1)[-1]
    return tail != a and len(tail) >= 5 and tail in out


def _coverage_ok(total: int, hits: int) -> bool:
    """ceil(total/3) coverage threshold, never below 1."""
    return hits >= max(1, -(-total // 3))


def criterion_evidenced(criterion: str, output_text: str) -> bool:
    """Honest single-part evidence: output carries a completion word AND
    evidences the criterion. Matching is anchor-first (OBJ-METRICS
    t_7aaa897c recalibration; empirical basis measured live against the
    real board, 2026-09-16):

    - The previous rule required >= half of the criterion's unique
      [a-z0-9]{4,} tokens. Real criteria are 17-44-token verification
      recipes; ~60-80% of their tokens are process instructions
      ("reportar", "resumen", "pegar", "termina", imperative framing) that
      an honest 1-3 sentence summary never repeats. Measured distribution
      over the 40 last-7d done_no_verified budget tasks with a declared
      criterion: real hits 0-15 vs a required 8-22 — old_pass was 0/40
      while 13/15 manually audited tasks DID carry the named evidence in
      their output channels. The ratio read 0.0 CRITICO for ~13h purely
      as a verifier artifact.
    - New rule: coverage is computed over evidence-bearing units, not raw
      tokens:
        * anchors (paths/filenames/dotted commands, incl. snake_case and
          test names) — a criterion anchor counts as hit when the output
          quotes the full run or its basename (>=5 chars);
        * words — standalone criterion words not inside any anchor.
      Anchor-bearing criteria need >= ceil(anchors/3) anchor hits: the
      summary must name the recipe's concrete artifacts, and a purely
      prose summary (no paths, no commands) fails. Honest summaries prove
      work through the artifacts they name, not by reusing process
      vocabulary (measured: real evidence summaries carry 0 standalone
      criterion words — e.g. t_b2a0bfa7 "Removed out.txt, tests/tmp.db …
      Commit created" — while never skipping the anchors). Anchor-free
      criteria need >= ceil(words/3) word hits (never < 1).
    - Honesty rule unchanged: no declaration or zero token overlap is
      never enough; no exemption lists, no threshold near zero."""
    out = _norm(output_text)
    if not COMPLETION_RE.search(out):
        return False
    cn = _norm(criterion)
    anchors, words = _criterion_units(cn)
    a_total, w_total = len(anchors), len(words)
    if a_total == 0 and w_total == 0:
        return False
    if a_total:
        a_hits = sum(1 for a in anchors if _anchor_hit(out, a))
        return _coverage_ok(a_total, a_hits)
    w_hits = sum(1 for t in words if t in out)
    return _coverage_ok(w_total, w_hits)


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
                        include_runs: bool = True) -> tuple[list[str], list[str], str]:
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

def read_trace(path: Path) -> list[dict]:
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


def window_spend(rows: list[dict], window_s: int, now: float,
                 db_path: Path) -> tuple[float, float]:
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

def verdict_for(ratio: float | None) -> str:
    if ratio is None:
        return "SIN GASTO"
    if ratio >= 5.0:
        return "EXCELENTE"
    if ratio >= 2.0:
        return "OK"
    if ratio >= 0.5:
        return "BAJO"
    return "CRITICO"


def _window_stats(db: Path, rows: list, w: int, now: float,
                  include_runs: bool = True) -> dict:
    """Verified/budget/spend aggregate for one window (raises on bad sources
    like read_trace/verified_done_tasks)."""
    v_ids, b_ids, verifier = verified_done_tasks(db, w, now, include_runs)
    strict, total = window_spend(rows, w, now, db)
    return {"verified": len(v_ids), "budget_done": len(b_ids),
            "strict_usd": strict, "total_usd": total,
            "verifier": verifier, "verified_ids": v_ids}


def _ratio_base(w: dict):
    """(base_usd, base_mode, ratio, veredicto) for one window aggregate."""
    strict, total = w["strict_usd"], w["total_usd"]
    if strict > 0:
        base, mode = strict, "strict"
    elif total > 0:
        base, mode = total, "proxy-total-24h"
    else:
        base, mode = 0.0, "none"
    ratio = round(w["verified"] / base, 2) if base > 0 else None
    return base, mode, ratio, verdict_for(ratio)


def _window_entry(w: dict) -> dict:
    """Flat output fields for one window aggregate."""
    base, mode, ratio, verdict = _ratio_base(w)
    return {"gasto_usd": round(base, 6),
            "gasto_strict_usd": round(w["strict_usd"], 6),
            "gasto_total_usd": round(w["total_usd"], 6),
            "base_mode": mode, "ratio": ratio, "veredicto": verdict}


def _na_entry(now: float, exc: Exception) -> dict:
    """Fail-open N/A entry for unreadable sources (never aborts the cron)."""
    return {"ts": _iso(now), "kind": "efficiency_ratio", "window": "24h",
            "tareas_verificadas": None, "gasto_usd": None,
            "gasto_strict_usd": None, "gasto_total_usd": None,
            "base_mode": None, "ratio": None, "veredicto": "N/A",
            "error": f"cannot compute: {exc}"}


def compute(now: float | None = None, include_runs: bool = True) -> dict:
    now = time.time() if now is None else float(now)
    db = kanban_db_path()
    trace_file = trace_path()
    try:
        rows = read_trace(trace_file)
        windows = {label: _window_stats(db, rows, w, now, include_runs)
                   for label, w in WINDOWS.items()}
    except (OSError, sqlite3.Error) as exc:
        return _na_entry(now, exc)

    w24, w7 = windows["24h"], windows["7d"]
    e24, e7 = _window_entry(w24), _window_entry(w7)
    out = {"ts": _iso(now), "kind": "efficiency_ratio", "window": "24h",
           "tareas_verificadas": w24["verified"],
           "tareas_budget_done_24h": w24["budget_done"]}
    out.update(e24)
    out["verifier"] = w24["verifier"]
    out["tareas_verificadas_7d"] = w7["verified"]
    out.update(gasto_usd_7d=e7["gasto_usd"], base_mode_7d=e7["base_mode"],
               ratio_7d=e7["ratio"], veredicto_7d=e7["veredicto"])
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

def main(argv: list[str] | None = None) -> int:
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
