#!/usr/bin/python3.12
"""approval_gate.py — P5 kernel: approval-ready tag, dedup, verdict apply.

Deterministic core of the Flujo B light gate (paste 18, 2026-09-13):

  triage task with tag `approval-ready` + full package + [APPROVAL: pending]
  -> the user says si/no/condicion in chat -> THIS module applies the verdict
  with status-guarded writes (mini-CAS) and the house CLI:

    si            -> specify (triage->todo) + promote (todo->ready) +
                     body stamp [APPROVAL: approved <iso>]
    si + condicion-> same as si, body gains the condition line + approved-with
    no            -> body stamp [APPROVAL: rejected <iso>] + archive

Zero states invented (kanban stays canonical); zero LLM. Library module:
the CLI entry (--pending/--dedup/--dry-run) exists for ops and tests, but
the interactive approval flow calls apply_verdict() directly.

Subcommands (read-only by default):
  approval_gate.py --pending            # list triage approval-ready tasks
  approval_gate.py --pending --json     # machine-readable
  approval_gate.py --verify TASK_ID     # package completeness check
  approval_gate.py --dedup-scan         # report near-duplicate proposals
  approval_gate.py --dry-run --apply ...# show what apply_verdict would do
  approval_gate.py --apply TASK_ID --verdict si|no|condicion [--note TEXT]

Body conventions (whole-body search, OBJ-20 lesson):
  tag line:      approval-ready        (header line, exact token)
  stamp:         [APPROVAL: pending] / [APPROVAL: approved <iso>]
                 [APPROVAL: approved-with <iso>: <cond>]
                 [APPROVAL: rejected <iso>]
  package fields (each on its own line, case-insensitive label):
    presupuesto estimado / estimated budget
    modelo asignado / model
    perfil / profile
    fecha estimada / estimated delivery
    success criterion / criterio de exito / criterio de éxito
    clase / class
    objetivo / objective:OBJ-xx  (the objective: tag counts)

Dedup signature (paste §8): sha1[:16] of normalized
(title + clase + objective + success criterion). Same signature in
triage/ready/todo/running = duplicate; the creator must not create it and
--dedup-scan reports existing ones for archival by a human/backstop.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import unicodedata
from pathlib import Path
from typing import Optional

_HERMES_ROOT = Path(os.environ.get("AG_HERMES_ROOT",
                                   os.path.expanduser("~/.hermes")))

APPROVAL_TAG_RE = re.compile(r"(?im)^\s*approval-ready\s*$")
STAMP_RE = re.compile(
    r"\[APPROVAL:\s*(pending|approved|approved-with|rejected)"
    r"(?:[^\]]*)?\]", re.I)

# Package fields: label regexes (bullet/inline-tolerant: the creator writes
# markdown bullets ("- Perfil: x") AND combined tag lines
# ("objective:X | cost:tiny | model:fast") — anchored ^\s* regexes silently
# miss both, so every field reads as "missing"). No line-start anchor;
# value runs to end of line.
PACKAGE_FIELDS = (
    ("presupuesto", re.compile(
        r"(?i)\b(?:presupuesto estimado|estimated budget|presupuesto)"
        r"\s*[:\-]\s*([^\n]+)")),
    ("modelo", re.compile(
        r"(?i)\b(?:modelo asignado|model)\s*[:\-]\s*([^\n]+)")),
    ("perfil", re.compile(
        r"(?i)\b(?:perfil|profile)\s*[:\-]\s*([^\n]+)")),
    ("fecha", re.compile(
        r"(?i)\b(?:fecha estimada(?: de entrega)?|estimated delivery)"
        r"\s*[:\-]\s*([^\n]+)")),
    ("criterio", re.compile(
        r"(?i)\b(?:success criterion|criterio de [ée]xito)"
        r"\s*[:\-]\s*([^\n]+)")),
    ("clase", re.compile(r"(?i)\b(?:clase|class)\s*[:\-]\s*([ABC])\b")),
    ("objetivo", re.compile(
        r"(?i)\bobjective\s*:\s*(OBJ-[A-Za-z0-9._-]+)")),
)

OPEN_STATUSES = {"triage", "todo", "ready", "running", "blocked"}


def kanban_db_path() -> Path:
    env = os.environ.get("AG_KANBAN_DB", "").strip()
    if env:
        return Path(env)
    root = _HERMES_ROOT / "kanban.db"
    return root if root.exists() else \
        _HERMES_ROOT / "profiles" / "pr-ollama" / "kanban.db"


def _norm(text: str) -> str:
    t = (text or "").lower()
    t = unicodedata.normalize("NFKD", t)
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def fingerprint(title: str, clase: str, objective: str, criterion: str) -> str:
    raw = "|".join((_norm(title), _norm(clase), _norm(objective),
                    _norm(criterion)))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Board reads
# ---------------------------------------------------------------------------

def _connect_ro(db: Path):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def fetch_task(db: Path, task_id: str) -> Optional[dict]:
    try:
        con = _connect_ro(db)
        row = con.execute(
            "SELECT id, title, body, status FROM tasks WHERE id=?",
            (task_id,)).fetchone()
        con.close()
        return dict(row) if row else None
    except sqlite3.Error:
        return None


def fetch_pending(db: Path) -> list:
    """Triage tasks stamped [APPROVAL: pending] (tag approval-ready OR the
    stamp itself counts — the tag can be stripped by the backstop but the
    package remains visible)."""
    out = []
    try:
        con = _connect_ro(db)
        rows = con.execute(
            "SELECT id, title, body, status FROM tasks WHERE status='triage' "
            "ORDER BY created_at").fetchall()
        con.close()
    except sqlite3.Error:
        return out
    for r in rows:
        body = r["body"] or ""
        if APPROVAL_TAG_RE.search(body) or \
                re.search(r"\[APPROVAL:\s*pending", body, re.I):
            out.append(dict(r))
    return out


def package_missing(body: str) -> list:
    """Names of the package fields absent from the body."""
    return [name for name, rx in PACKAGE_FIELDS if not rx.search(body or "")]


def stamp_state(body: str) -> str:
    m = STAMP_RE.search(body or "")
    return m.group(1).lower() if m else "none"


# ---------------------------------------------------------------------------
# Dedup
# ---------------------------------------------------------------------------

def proposal_signature(task: dict) -> Optional[str]:
    body = task.get("body") or ""
    m_obj = PACKAGE_FIELDS[6][1].search(body)
    m_cls = PACKAGE_FIELDS[5][1].search(body)
    m_crit = PACKAGE_FIELDS[4][1].search(body)
    obj = m_obj.group(1) if m_obj else ""
    cls = m_cls.group(1) if m_cls else ""
    crit = m_crit.group(1) if m_crit else ""
    if not obj:
        return None  # untagged proposals are not deduped (human refinement)
    return fingerprint(task.get("title") or "", cls, obj, crit)


def dedup_scan(db: Path) -> list:
    """Groups of 2+ OPEN tasks sharing a signature. Returns
    [{signature, ids:[...], keep: oldest, duplicates: [younger...]}]."""
    try:
        con = _connect_ro(db)
        rows = con.execute(
            "SELECT id, title, body, status, created_at FROM tasks "
            "WHERE status IN ('triage','todo','ready','running') "
            "ORDER BY created_at").fetchall()
        con.close()
    except sqlite3.Error:
        return []
    groups: dict = {}
    for r in rows:
        sig = proposal_signature(dict(r))
        if sig:
            groups.setdefault(sig, []).append(dict(r))
    out = []
    for sig, tasks in sorted(groups.items()):
        if len(tasks) < 2:
            continue
        out.append({
            "signature": sig,
            "keep": tasks[0]["id"],
            "duplicates": [t["id"] for t in tasks[1:]],
            "statuses": [t["status"] for t in tasks],
        })
    return out


# ---------------------------------------------------------------------------
# Body mutation (CAS) + CLI moves
# ---------------------------------------------------------------------------

def patch_stamp(body: str, new_stamp: str, extra_line: str = "") -> str:
    """Replace the [APPROVAL: ...] stamp, or append one at the end."""
    lines = (body or "").splitlines()
    replaced = False
    for i, ln in enumerate(lines):
        if STAMP_RE.search(ln):
            lines[i] = new_stamp
            replaced = True
            break
    out = "\n".join(lines)
    if extra_line:
        out = (out.rstrip("\n") + "\n" + extra_line + "\n")
    if not replaced:
        out = (out.rstrip("\n") + "\n" + new_stamp + "\n")
    if (body or "").endswith("\n") and not out.endswith("\n"):
        out += "\n"
    return out


def _update_body_cas(db: Path, task_id: str, new_body: str,
                     expect_status: str) -> bool:
    try:
        con = sqlite3.connect(str(db))
        cur = con.execute(
            "UPDATE tasks SET body=? WHERE id=? AND status=?",
            (new_body, task_id, expect_status))
        con.commit()
        ok = cur.rowcount == 1
        con.close()
        return ok
    except sqlite3.Error:
        return False


def _cli(*args, timeout=30) -> tuple:
    """hermes kanban runner. AG_STUB_CLI=simulate -> moves are simulated for
    fixtures (specify/promote mutate a local map, archive returns ok) so the
    verdict chain is testable without a hermes install."""
    stub = os.environ.get("AG_STUB_CLI", "").strip()
    if stub == "simulate":
        ok_moves = {"specify", "promote", "archive", "comment"}
        if args and args[0] in ok_moves:
            return 0, f"simulated {args[0]}"
        return 0, "simulated"
    try:
        r = subprocess.run(["hermes", "kanban", *args],
                           capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as exc:
        return 1, str(exc)


def _normalize_verdict(verdict: str) -> str:
    """Map free-form user text to a canonical verdict ('si' | 'no' |
    'condicion'). Returns '' when the verdict is not recognized."""
    v = _norm(verdict)
    if v in ("si", "sí", "yes", "arranca", "adelante", "ok"):
        return "si"
    if v in ("no", "archiva", "archivar"):
        return "no"
    if v.startswith("si ") or "condicion" in v or "condición" in v:
        return "condicion"
    return ""


def _validate_or_fail(db: Path, task_id: str,
                      v: str) -> tuple:
    """Validation wrapper returning (task, error_dict).

    On success task is set and error is None.  On failure task is None
    and error holds the audit dict with ``applied: False``.
    """
    task = fetch_task(db, task_id)
    if not task:
        return None, {"task": task_id, "verdict": v, "applied": False,
                      "reason": "task not found"}
    if task["status"] != "triage":
        return None, {"task": task_id, "verdict": v, "applied": False,
                      "reason": f"task is {task['status']}, not triage"}
    st = stamp_state(task["body"] or "")
    if st == "rejected":
        return None, {"task": task_id, "verdict": v, "applied": False,
                      "reason": "already rejected"}
    if st == "none":
        return None, {"task": task_id, "verdict": v, "applied": False,
                      "reason": "no [APPROVAL] stamp — not a Flujo B package"}
    return task, None


def _apply_approved(db: Path, task_id: str, v: str, note: str,
                    iso: str) -> dict:
    """Execute the 'si'/'condicion' path: stamp + CAS + specify + promote.

    Includes race-tolerance retry and CLI-stub mode for tests.
    """
    task = fetch_task(db, task_id)
    new_stamp = f"[APPROVAL: approved {iso}]" if v == "si" else \
        f"[APPROVAL: approved-with {iso}: {note}]"
    new_body = patch_stamp(task["body"], new_stamp,
                           extra_line=(f"[condicion: {note}]"
                                       if v == "condicion" and note
                                       else ""))
    if not _update_body_cas(db, task_id, new_body, "triage"):
        return {"task": task_id, "verdict": v, "applied": False,
                "reason": "CAS body update failed (status changed?)"}
    rc1, out1 = _cli("specify", task_id, "--author", "approval-gate")
    rc2, out2 = _cli("promote", task_id,
                     f"APPROVAL approved {iso}"
                     + (f" condicion: {note}" if note else ""))
    # Race tolerance: between specify and promote the tick may have
    # already promoted the task.  Re-read: in todo -> retry promote
    # once; in ready -> treat as success (the goal state).
    final = fetch_task(db, task_id)
    final_status = final["status"] if final else "?"
    if os.environ.get("AG_STUB_CLI", "").strip() == "simulate":
        ok = rc1 == 0 and rc2 == 0
        return {"task": task_id, "verdict": v, "applied": ok,
                "status": "ready (simulated)",
                "specify_rc": rc1, "promote_rc": rc2,
                "detail": out1.strip()[:120]}
    if rc2 != 0 and final_status == "todo":
        rc2, out2 = _cli("promote", task_id,
                         f"APPROVAL approved {iso} (retry)")
        final = fetch_task(db, task_id)
        final_status = final["status"] if final else "?"
    ok = rc1 == 0 and final_status in ("ready", "todo")
    return {"task": task_id, "verdict": v, "applied": ok,
            "status": final_status,
            "specify_rc": rc1, "promote_rc": rc2,
            "detail": out1.strip()[:120] or out2.strip()[:120]}


def _apply_rejected(db: Path, task_id: str, v: str,
                    iso: str) -> dict:
    """Execute the 'no' path: rejection stamp + CAS + best-effort archive."""
    task = fetch_task(db, task_id)
    new_body = patch_stamp(task["body"], f"[APPROVAL: rejected {iso}]")
    if not _update_body_cas(db, task_id, new_body, "triage"):
        return {"task": task_id, "verdict": v, "applied": False,
                "reason": "CAS body update failed (status changed?)"}
    rc, _ = _cli("archive", task_id)
    _cli("comment", task_id, "rejected by user (approval-gate)")
    return {"task": task_id, "verdict": v, "applied": True,
            "stamp": "rejected", "archive_rc": rc,
            "archived": rc == 0}


def apply_verdict(db: Path, task_id: str, verdict: str,
                  note: str = "", dry: bool = False) -> dict:
    """Apply the user's verdict to a triage approval-ready task.

    verdict: 'si' | 'no' | 'condicion' (note = the condition / context).
    Returns an audit dict; never raises. Status-guarded at every write.
    """
    v = _normalize_verdict(verdict)
    if not v:
        return {"task": task_id, "verdict": verdict,
                "applied": False, "reason": "verdict not recognized"}

    task, err = _validate_or_fail(db, task_id, v)
    if err:
        return err

    iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if dry:
        return {"task": task_id, "verdict": v, "applied": False,
                "dry_run": True, "reason": "would apply", "iso": iso}

    if v in ("si", "condicion"):
        return _apply_approved(db, task_id, v, note, iso)
    return _apply_rejected(db, task_id, v, iso)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_arg_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser for the approval gate."""
    ap = argparse.ArgumentParser(description="P5 approval gate kernel.")
    ap.add_argument("--db", default=None)
    ap.add_argument("--pending", action="store_true")
    ap.add_argument("--verify", metavar="TASK_ID")
    ap.add_argument("--dedup-scan", action="store_true")
    ap.add_argument("--apply", metavar="TASK_ID")
    ap.add_argument("--verdict", default="")
    ap.add_argument("--note", default="")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    return ap


def _cli_verify(db: Path, task_id: str) -> int:
    """Handle --verify: print package completeness for a single task."""
    task = fetch_task(db, task_id)
    if not task:
        print(json.dumps({"task": task_id, "error": "not found"}))
        return 0
    body = task["body"] or ""
    print(json.dumps({
        "task": task["id"], "status": task["status"],
        "tag": bool(APPROVAL_TAG_RE.search(body)),
        "missing_fields": package_missing(body),
        "stamp": stamp_state(body),
        "complete": not package_missing(body),
    }, ensure_ascii=False))
    return 0


def _cli_pending(db: Path, as_json: bool) -> int:
    """Handle --pending (default): list triage approval-ready tasks."""
    rows = []
    for t in fetch_pending(db):
        body = t["body"] or ""
        rows.append({
            "id": t["id"], "title": t["title"],
            "missing": package_missing(body), "stamp": stamp_state(body),
        })
    if as_json:
        print(json.dumps(rows, ensure_ascii=False))
    elif not rows:
        print("sin iniciativas pendientes de aprobacion")
    else:
        for r in rows:
            mark = "OK " if not r["missing"] else \
                f"[falta {', '.join(r['missing'])}]"
            print(f"{r['id']}  {(r['title'] or '')[:70]}  {mark}")
    return 0


def main(argv=None) -> int:
    ap = _build_arg_parser()
    args = ap.parse_args(argv)
    db = Path(args.db) if args.db else kanban_db_path()
    if args.verify:
        return _cli_verify(db, args.verify)
    if args.dedup_scan:
        print(json.dumps(dedup_scan(db), ensure_ascii=False, indent=1))
        return 0
    if args.apply:
        res = apply_verdict(db, args.apply, args.verdict, args.note,
                            dry=args.dry_run)
        print(json.dumps(res, ensure_ascii=False))
        return 0
    return _cli_pending(db, args.json)


if __name__ == "__main__":
    raise SystemExit(main())
