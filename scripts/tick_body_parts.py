#!/usr/bin/python3.12
"""tick_body_parts.py — OBJ-39-REBELION: the body is the program, the tick
is the interpreter.

Two mechanisms that make the constitution of flight enforceable WITHOUT the
owner watching:

  1. DONE-VERIFICATION AGAINST BODY. When a task whose body declares
     numbered parts (R1/R2/..., Fase N, Paso N, Step N, Round N) closes,
     the tick compares the declared parts against the EVIDENCE the worker
     left behind (tasks.result + task_runs.summary + task comments, plus
     self-declared completions in the body itself). A part counts as
     evidenced only when its mention carries affirmative context; negation
     contexts ("R2-R6 pendientes", "quedan R3 y R4", "R5 skipped") never
     evidence a part.

  2. SUCCESSORS FROM BODIES. Pending parts become a successor task
     (assignee inherited from the parent, header tags inherited, pending
     part lines copied verbatim so the successor body is self-contained).
     The house rule "done is sealed — never reopen" is respected: the
     successor IS the reopen. An audit comment is left on the parent so
     the trail survives.

  3. DRY-QUEUE HONESTY. "Cola seca legitima" is only true when no
     pending parts exist in closed bodies; the cola-viva cascade consults
     this module before saying the drought is real.

Motivating incident (2026-09-11): a multi-part night-marathon task closed
after its first round ("First round of classification completed") while its
body declared R1-R6; the remaining rounds slept in a sealed body for hours
while quota sat free. Doctrine said "never stop on free quota"; nothing
enforced it against premature done. This module is the enforcement point.

Scope decisions (v1, deliberate):
  - NUMBERED parts only. Named-only phase lists ("Fases: a, b, c") need
    semantic matching; a false "pending" there would create a wrong
    successor — worse than the disease. Out of scope.
  - Bare "1. 2. 3." lists are NOT parts (they are step prose in almost
    every task body; matching them would false-positive everywhere).
  - Only class-C parents (body carries 'clase:C') get auto-created
    successors — class A/B are user decisions (house doctrine). For
    non-C parents the tick leaves a one-time audit comment instead.
  - A successor already referencing the parent (open, non-archived)
    blocks creation — idempotency, same rule as the structural successor.
  - Max parts guard: a body declaring > MAX_DECLARED_PARTS parts is
    treated as a parse false-positive and skipped.
  - Escape hatch for legitimate skips (anti-filler rule): a comment
    "[done-verify-skip] R5: <why>" on the closing task WAIVES that part
    (documented rationale wins over blind re-creation, so the chain
    terminates). Without the marker a skipped part stays pending.

Zero tokens (no LLM). Reads the kanban DB read-only; mutates only via the
`hermes kanban` CLI (create/comment). One ledger line per decision in the
shared cola-viva ledger. Exit code 0 always.

Usage (manual inspection; the tick calls cascade_step directly):
  tick_body_parts.py --scan        # findings table against the real DB
  tick_body_parts.py               # dry-run: what would be created
  tick_body_parts.py --execute     # actually create (normally tick-driven)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Config / constants
# ---------------------------------------------------------------------------

DONE_WINDOW_HOURS = 24         # recently closed tasks eligible for the scan
MAX_DECLARED_PARTS = 20        # more than this = parse false-positive, skip
MAX_PART_LINES_IN_BODY = 12    # verbatim part lines copied into the successor
COMPLETION_WINDOW = 40         # chars after a mention where a completion
                               # word rescues it on a negated line / in a body

# Numbered part marker at line start (optionally a markdown heading or a
# bullet, optionally a short parenthetical before the separator):
#   R1 (esta): ...   R2: ...   R6+: ...   Fase 2: ...   Paso 3: ...
#   Step 2: ...      Round 4: ...      (ES and EN)
PART_LINE_RE = re.compile(
    r"^(?:#+\s*)?(?:[-*+]\s*)?"
    r"(?:R(?P<r>\d+)(?P<rplus>\+?)|Fase\s+(?P<f>\d+)|Round\s+(?P<rd>\d+)"
    r"|Paso\s+(?P<p>\d+)|Step\s+(?P<s>\d+))"
    r"\s*(?:\([^)]{0,40}\)\s*)?(?:[:.\-—]|\+?\s|$)",
    re.I)

# Evidence patterns: a part is evidenced when its marker, its "name N"
# form, or its prose ordinal appears in the worker's evidence text.
PART_NUM_WORDS = {
    1: ["first", "primera", "primer"],
    2: ["second", "segunda", "segundo"],
    3: ["third", "tercera", "tercer"],
    4: ["fourth", "cuarta", "cuarto"],
    5: ["fifth", "quinta", "quinto"],
    6: ["sixth", "sexta", "sexto"],
    7: ["seventh", "septima", "séptima"],
    8: ["eighth", "octava", "octavo"],
    9: ["ninth", "novena", "noveno"],
    10: ["tenth", "decima", "décima"],
}
PART_NOUNS = r"(?:ronda|round|fase|phase|paso|step|parte|part)"

# Negation words: a mention on a line carrying one of these needs a
# completion word nearby to count as evidence.
NEG_WORD_RE = re.compile(
    r"(?i)\b(?:pendientes?|pending|remaining|restantes?|faltantes?"
    r"|por\s+hacer|todo\s*:|not\s+yet|a[uú]n\s+no|todav[ií]a\s+no"
    r"|still\s+to|quedan|queda\s+por|skipped|omitid[oa]s?)\b")

# Completion words: affirmative execution signals. On a negated line (or in
# a body self-declaration) a mention counts only with one of these within
# COMPLETION_WINDOW chars after the mention.
COMPLETION_RE = re.compile(
    r"(?i)\b(?:done|completad[oa]s?|hech[oa]s?|ejecutad[oa]s?|finished"
    r"|completed|resolved|pass(?:ed)?|ok|logged|registrad[oa]s?"
    r"|extraid[oa]s?|generad[oa]s?|resumid[oa]s?|clasificad[oa]s?"
    r"|list[oa]s?|verificad[oa]s?|terminad[oa]s?)\b")

# Range mention "R2-R6" / "R2 - R6" (no negation on the line -> all parts
# in the range are evidenced; with negation the range is ignored).
RANGE_RE = re.compile(r"\bR\s*0*(\d+)\s*[-–]\s*R?\s*0*(\d+)\b", re.I)

# Escape hatch: explicit waiver with documented rationale.
WAIVE_RE = re.compile(r"\[done-verify-skip\]", re.I)

# Header tags inherited by the successor (first token only: "cost:tiny
# (todo local...)" -> "tiny").
TAG_RE = {tag: re.compile(rf"\b{tag}\s*:\s*([^\n|]+)", re.I)
          for tag in ("objective", "cost", "privacy", "clase")}
VALID_COSTS = {"micro", "tiny", "small", "medium", "complex"}

# Class-C marker — same convention as tick-cola-viva.py.
CLASE_C_RE = re.compile(r"\bclase\s*:\s*C\b", re.I)

# Audit-comment marker (dedup: one audit comment per parent, ever).
AUDIT_MARKER = "[done-verify]"


def get_hermes_home() -> Path:
    val = os.environ.get("HERMES_HOME", "").strip()
    return Path(val).resolve() if val else (Path.home() / ".hermes").resolve()


def kanban_db_path(hermes_home=None) -> Path:
    env_db = os.environ.get("HERMES_KANBAN_DB", "").strip()
    if env_db:
        return Path(env_db).expanduser().resolve()
    base = Path(hermes_home) if hermes_home else get_hermes_home()
    return base / "kanban.db"


def ledger_path(hermes_home=None) -> Path:
    base = Path(hermes_home) if hermes_home else get_hermes_home()
    return base / "quota-governor" / "cola-viva.jsonl"


# ---------------------------------------------------------------------------
# Parsing: declared parts, evidence, pending
# ---------------------------------------------------------------------------

def parse_parts(body: str) -> dict:
    """Declared numbered parts: {part_number: original_line}. A part line
    starts (after optional heading/bullet) with R<N>, R<N>+, Fase N,
    Round N, Paso N or Step N. First declaration wins for a number."""
    parts: dict[int, str] = {}
    for raw in (body or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        m = PART_LINE_RE.match(line)
        if not m:
            continue
        num = None
        for g in ("r", "f", "rd", "p", "s"):
            if m.group(g) is not None:
                num = int(m.group(g))
                break
        if num is None or num < 1:
            continue
        parts.setdefault(num, line)
    if len(parts) > MAX_DECLARED_PARTS:
        return {}  # parse false-positive guard
    return parts


def evidence_patterns(part: int) -> list:
    """Regexes that count as a mention of a declared part number."""
    pats = [rf"\bR\s*0*{part}\b",                       # R2 / R 2
            rf"(?i)\b{PART_NOUNS}\s*#?\s*0*{part}\b"]   # round 2 / fase 2
    for word in PART_NUM_WORDS.get(part, []):
        pats.append(rf"(?i)\b{word}\s+{PART_NOUNS}\b")  # first round / segunda fase
    return pats


def _mentions(line: str, declared: dict) -> list:
    """[(part_number, match_end)] for every distinct declared part mentioned
    in the line (first pattern hit per part)."""
    out = []
    for num in sorted(declared):
        for pat in evidence_patterns(num):
            m = re.search(pat, line)
            if m:
                out.append((num, m.end()))
                break
    return out


def _range_evidence(line: str, declared: dict) -> set:
    """Parts evidenced by an R<a>-R<b> range mention on a clean line."""
    found = set()
    for m in RANGE_RE.finditer(line):
        lo, hi = int(m.group(1)), int(m.group(2))
        if lo > hi:
            lo, hi = hi, lo
        for num in declared:
            if lo <= num <= hi:
                found.add(num)
    return found


def evidenced_in(text: str, declared: dict) -> set:
    """Parts evidenced in worker OUTPUT (result / run summaries / comments).

    Rules per line:
      - no negation word  -> every mentioned part counts (+ R<a>-R<b> ranges)
      - negation word     -> a mention counts only if a completion word
                             follows within COMPLETION_WINDOW chars
    """
    found: set = set()
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        negated = bool(NEG_WORD_RE.search(line))
        if not negated:
            found |= ({num for num, _ in _mentions(line, declared)}
                      & set(declared))
            found |= (_range_evidence(line, declared) & set(declared))
        else:
            for num, end in _mentions(line, declared):
                window = line[end:end + COMPLETION_WINDOW]
                if COMPLETION_RE.search(window):
                    found.add(num)
    return found & set(declared)


def evidenced_in_body(body: str, declared: dict) -> set:
    """Parts self-declared as executed IN THE PARENT'S OWN BODY (a successor
    often restates 'R1 (ya hecha por el turno anterior)'). Every mention
    needs a completion word nearby — plain program lines ('R2: resumir...')
    never count."""
    found: set = set()
    for line in (body or "").splitlines():
        line = line.strip()
        if not line:
            continue
        for num, end in _mentions(line, declared):
            window = line[end:end + COMPLETION_WINDOW]
            if COMPLETION_RE.search(window):
                found.add(num)
    return found & set(declared)


def waived_parts(comments_text: str, declared: dict) -> set:
    """Parts explicitly waived via '[done-verify-skip] R<N>: <why>'
    comments (documented rationale beats blind re-creation)."""
    waived: set = set()
    for line in (comments_text or "").splitlines():
        if not WAIVE_RE.search(line):
            continue
        for num, _ in _mentions(line, declared):
            waived.add(num)
    return waived & set(declared)


def pending_parts(declared: dict, output_evidence: set, body_evidence: set,
                  waived: set) -> list:
    """Declared parts neither evidenced nor waived, ascending."""
    covered = output_evidence | body_evidence | waived
    return sorted(n for n in declared if n not in covered)


def parts_label(nums: list) -> str:
    """[2,3,4,5,6] -> 'R2-R6'; [2,5] -> 'R2, R5'; [2,3,5] -> 'R2-R3, R5'."""
    if not nums:
        return ""
    runs = []
    start = prev = nums[0]
    for n in nums[1:]:
        if n == prev + 1:
            prev = n
            continue
        runs.append((start, prev))
        start = prev = n
    runs.append((start, prev))
    return ", ".join(f"R{a}" if a == b else f"R{a}-R{b}" for a, b in runs)


def first_tag(body: str, tag: str) -> str:
    m = TAG_RE[tag].search(body or "")
    if not m:
        return ""
    val = m.group(1).strip()
    return val.split()[0] if val else ""


def is_clase_c(body: str) -> bool:
    return bool(CLASE_C_RE.search(body or ""))


# ---------------------------------------------------------------------------
# Board reads (read-only, fail open)
# ---------------------------------------------------------------------------

def _connect(db_path: Path):
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def recent_done_tasks(db_path, hours: int = DONE_WINDOW_HOURS,
                      now: float | None = None) -> list:
    """Done tasks closed inside the window, newest first. `now` injectable
    for deterministic tests (fixed-epoch convention)."""
    db_path = Path(db_path)
    if not db_path.exists():
        return []
    cutoff = (time.time() if now is None else float(now)) - hours * 3600
    try:
        con = _connect(db_path)
        try:
            rows = con.execute(
                "SELECT id, title, assignee, body, result, completed_at "
                "FROM tasks WHERE status='done' AND completed_at > ? "
                "ORDER BY completed_at DESC", (cutoff,)).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return []
    return [dict(r) for r in rows]


def _scalar_lines(db_path: Path, sql: str, args: tuple) -> list:
    try:
        con = _connect(db_path)
        try:
            return [r[0] or "" for r in con.execute(sql, args)]
        finally:
            con.close()
    except sqlite3.Error:
        return []


def evidence_for_parent(db_path: Path, parent_id: str,
                        result_text: str) -> tuple:
    """(runs_text, comments_text) — the worker's output channels."""
    runs = _scalar_lines(db_path,
                         "SELECT summary FROM task_runs "
                         "WHERE task_id=? AND summary IS NOT NULL",
                         (parent_id,))
    comments = _scalar_lines(db_path,
                             "SELECT body FROM task_comments WHERE task_id=?",
                             (parent_id,))
    return "\n".join(r for r in runs if r), "\n".join(c for c in comments if c)


def has_open_successor(db_path, parent_id: str) -> bool:
    """True if a non-archived task body already references this parent
    (idempotency: never duplicate a successor)."""
    db_path = Path(db_path)
    if not db_path.exists():
        return False
    try:
        con = _connect(db_path)
        try:
            rows = con.execute(
                "SELECT id FROM tasks WHERE status!='archived' AND body LIKE ?",
                (f"%{parent_id}%",)).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return False
    return bool(rows)


def has_audit_comment(db_path: Path, parent_id: str) -> bool:
    """True if the parent already carries a [done-verify] audit comment
    (one-time audit per parent, never comment-spam)."""
    comments = _scalar_lines(db_path,
                             "SELECT body FROM task_comments WHERE task_id=?",
                             (parent_id,))
    return any(AUDIT_MARKER in c for c in comments)


# ---------------------------------------------------------------------------
# Findings + successor generation
# ---------------------------------------------------------------------------

def scan(db_path, hours: int = DONE_WINDOW_HOURS,
         now: float | None = None) -> list:
    """Findings for every recently-done multi-part task: declared parts,
    evidenced parts, waived parts, pending parts, successor state, class."""
    findings = []
    for t in recent_done_tasks(db_path, hours, now=now):
        body = t.get("body") or ""
        declared = parse_parts(body)
        if len(declared) < 2 or max(declared) < 2:
            continue  # not a multi-part program
        result_text = t.get("result") or ""
        runs_text, comments_text = evidence_for_parent(db_path, t["id"],
                                                       result_text)
        out_ev = evidenced_in(f"{result_text}\n{runs_text}\n{comments_text}",
                              declared)
        body_ev = evidenced_in_body(body, declared)
        waived = waived_parts(comments_text, declared)
        pending = pending_parts(declared, out_ev, body_ev, waived)
        findings.append({
            "parent_id": t["id"],
            "title": t.get("title") or "",
            "assignee": (t.get("assignee") or "").strip(),
            "declared": sorted(declared),
            "evidenced": sorted(out_ev | body_ev),
            "waived": sorted(waived),
            "pending": pending,
            "has_successor": has_open_successor(db_path, t["id"]),
            "has_audit": has_audit_comment(db_path, t["id"]),
            "clase_c": is_clase_c(body),
            "part_lines": declared,
            "body": body,
        })
    return findings


def build_successor(parent_id: str, title: str, assignee: str,
                    body: str, pending: list, part_lines: dict) -> tuple:
    """Return (title, body) for the auto-derived successor."""
    obj = first_tag(body, "objective") or "OBJ-30"
    cost = first_tag(body, "cost")
    if cost not in VALID_COSTS:
        cost = "tiny"
    priv = first_tag(body, "privacy") or "low"
    base = (title or "").strip()
    label = parts_label(pending)
    new_title = f"Partes pendientes de {parent_id} ({label}): {base}"[:120]

    verbatim = []
    for n in pending:
        line = part_lines.get(n)
        if line:
            verbatim.append(line)
        if len(verbatim) >= MAX_PART_LINES_IN_BODY:
            break

    new_body = (
        f"objective:{obj} | cost:{cost} | privacy:{priv} | clase:C\n\n"
        f"SUCESOR AUTO-DERIVADO del body de {parent_id} (OBJ-39-REBELION): "
        f"la tarea cerro sin evidenciar en su result/summary/comentarios "
        f"estas partes declaradas. Partes sin evidencia: {label}.\n\n"
        f"Partes pendientes (verbatim del body original):\n"
        + "\n".join(f"- {v}" for v in verbatim) + "\n\n"
        f"Contexto completo (protocolo, metricas, entregable): "
        f"`hermes kanban show {parent_id}`.\n\n"
        f"Convencion de cierre (OBJ-39): el summary del kanban_complete "
        f"debe mencionar CADA parte ejecutada con contexto afirmativo "
        f"(p. ej. 'R2: resumidos los 5 skills. R3: extraidos 50 registros') "
        f"para que la verificacion de done contra body la acepte.\n"
        f"Si una parte NO debe ejecutarse (regla anti-filler), deja ANTES "
        f"de cerrar un comentario en esta tarea: "
        f"'[done-verify-skip] R<N>: <por que>' — la parte queda relevada y "
        f"la cadena termina."
    )
    return new_title, new_body


# ---------------------------------------------------------------------------
# Mutations via CLI (cron no_agent context is not fenced)
# ---------------------------------------------------------------------------

class _CliResult:
    def __init__(self, returncode: int = 1, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _cli(*args, timeout=60):
    try:
        return subprocess.run(["hermes", "kanban", *args],
                              capture_output=True, text=True, timeout=timeout)
    except Exception as exc:
        return _CliResult(returncode=1, stderr=str(exc))


def create_task(title: str, body: str, assignee: str) -> str | None:
    r = _cli("create", title, "--assignee", assignee, "--workspace", "scratch",
             "--body", body, "--json")
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout).get("id")
    except (ValueError, AttributeError):
        m = re.search(r"(t_\w+)", r.stdout)
        return m.group(1) if m else None


def comment_task(task_id: str, text: str) -> bool:
    return _cli("comment", task_id, text,
                "--author", "tick-body-parts").returncode == 0


def _log(ledger: Path, entry: dict) -> None:
    try:
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with open(ledger, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass  # observability must never break the tick


# ---------------------------------------------------------------------------
# Cascade step (called from tick-cola-viva.py) + CLI
# ---------------------------------------------------------------------------

def pending_without_successor_count(db_path,
                                    hours: int = DONE_WINDOW_HOURS,
                                    now: float | None = None) -> int:
    """How many recently-closed multi-part tasks have pending parts AND no
    open successor. If this is > 0, a 'cola seca' claim is NOT legitima —
    the drought is self-inflicted."""
    return sum(1 for f in scan(db_path, hours, now=now)
               if f["pending"] and not f["has_successor"])


def cascade_step(db_path, ledger, execute: bool = False,
                 now: float | None = None,
                 hours: int = DONE_WINDOW_HOURS) -> str | None:
    """One body-parts step. Returns the stdout message (None = silent).

    Priority order (first actionable finding wins, max 1 action):
      1. pending + open successor already exists -> detect-log only
      2. pending + class C + no successor -> create successor
         (dry-run: report what would be created)
      3. pending + non-C -> one-time audit comment on the parent
         (class A/B are user decisions; the comment surfaces the gap
         without creating work the doctrine forbids)
    Ledger actions: body-parts-detect / body-parts-successor /
    body-parts-audit-non-c / body-parts-create-failed.
    """
    now = time.time() if now is None else float(now)
    for f in scan(db_path, hours, now=now):
        if not f["pending"]:
            continue
        label = parts_label(f["pending"])
        if f["has_successor"]:
            _log(ledger, {"ts": now, "action": "body-parts-detect",
                          "parent": f["parent_id"], "pending": label,
                          "note": "open successor already references parent"})
            continue  # silent on stdout: observation only
        if not f["clase_c"]:
            if not execute:
                _log(ledger, {"ts": now, "action": "body-parts-audit-non-c",
                              "parent": f["parent_id"], "dry": True,
                              "pending": label})
                return (f"cola viva: auditoria body-parts de "
                        f"{f['parent_id']} (no-C, {label} pendientes)")
            if f["has_audit"]:
                continue  # audit already left; never comment-spam
            ok = comment_task(
                f["parent_id"],
                f"{AUDIT_MARKER} Body multi-parte cerrado sin evidenciar: "
                f"{label}. No se crea sucesor automatico (clase no-C "
                f"requiere decision del dueno — OBJ-39-REBELION).")
            _log(ledger, {"ts": now, "action": "body-parts-audit-non-c",
                          "parent": f["parent_id"], "pending": label,
                          "commented": ok})
            if ok:
                return (f"cola viva: auditoria body-parts de "
                        f"{f['parent_id']} (no-C, {label} pendientes)")
            continue
        title, body = build_successor(f["parent_id"], f["title"],
                                      f["assignee"], f["body"],
                                      f["pending"], f["part_lines"])
        assignee = f["assignee"] or "pr-ollama"
        if not execute:
            _log(ledger, {"ts": now, "action": "body-parts-successor",
                          "parent": f["parent_id"], "dry": True,
                          "title": title[:60], "assignee": assignee,
                          "pending": label})
            return (f"cola viva: sucesor body-parts de {f['parent_id']} "
                    f"({label} pendientes) -> {title[:50]}")
        tid = create_task(title, body, assignee)
        if not tid:
            _log(ledger, {"ts": now, "action": "body-parts-create-failed",
                          "parent": f["parent_id"], "pending": label})
            return (f"cola seca: fallo al crear sucesor body-parts de "
                    f"{f['parent_id']}")
        comment_task(f["parent_id"],
                     f"{AUDIT_MARKER} Body multi-parte. Evidencia en "
                     f"result/runs/comentarios: "
                     f"{parts_label(f['evidenced']) or 'ninguna'}. "
                     f"Partes sin evidencia: {label}. "
                     f"Sucesor creado: {tid} (OBJ-39: done sellado no se "
                     f"reabre; el sucesor es la reapertura).")
        _log(ledger, {"ts": now, "action": "body-parts-successor",
                      "parent": f["parent_id"], "task": tid,
                      "title": title[:60], "assignee": assignee,
                      "pending": label})
        return (f"cola viva: sucesor body-parts de {f['parent_id']} -> {tid} "
                f"({label} pendientes)")
    return None


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    p.add_argument("--execute", action="store_true",
                   help="actually create/comment (default: dry-run)")
    p.add_argument("--scan", action="store_true",
                   help="print the findings table and exit")
    p.add_argument("--window-hours", type=int, default=DONE_WINDOW_HOURS)
    args = p.parse_args(argv)

    db = kanban_db_path()
    findings = scan(db, args.window_hours)

    if args.scan:
        if not findings:
            print("no multi-part done tasks in window")
            return 0
        for f in findings:
            print(f"{f['parent_id']}  declared={f['declared']} "
                  f"evidenced={f['evidenced']} waived={f['waived']} "
                  f"pending={f['pending']} "
                  f"successor={'yes' if f['has_successor'] else 'no'} "
                  f"clase:C={f['clase_c']}")
        return 0

    msg = cascade_step(db, ledger_path(), execute=args.execute)
    if msg:
        print(("DRY: " if not args.execute else "") + msg)
    else:
        print("body-parts: nothing pending")
    return 0


if __name__ == "__main__":
    sys.exit(main())
