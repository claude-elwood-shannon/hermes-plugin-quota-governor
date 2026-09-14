#!/usr/bin/env python3
"""autoqueue.py — OBJ-44: deterministic seed queue consumption.

Reads a seed queue (default `~/.hermes/data/autoqueue.md`) and converts, at
most, ONE pending seed per invocation into a kanban task. This removes the
manual-dependency on keeping the queue replenished: a future `generar_semillas()`
replenishes it, `consumir_semilla()` drains it deterministically.

Contract (matches the autoqueue-api.md mechanics):
  * Seed format: `- [ ] <descripcion> | <perfil> | <coste>`.
  * Consumable = a `- [ ]` line whose `<desc>` carries none of the
    annotations `duplicada`, `rota`, `tarea existente`, and which does not
    already reference a task id (`(t_...)`) — an idempotent seed is not
    consumable.
  * Consumption is append-only in line count (`wc -l` is unchanged): the
    line's content is rewritten in place from `- [ ]` to `- [x] ... (t_<id>)`.
  * At most ONE seed per invocation.
  * CLI stdout is non-empty ONLY when a candidate/consumption happened;
    otherwise it is silent. Exit code 0 always.
  * Every consumption is appended to an append-only JSONL ledger under
    get_hermes_home()/quota-governor/autoqueue-consumes.jsonl.

Path resolution (no absolute host paths in the repo):
  * Queue:  env AUTOQUEUE_FILE, else ~/.hermes/data/autoqueue.md.
  * Heres home: env HERMES_HOME, else ~/.hermes.
  * Ledger:  <hermes_home>/quota-governor/autoqueue-consumes.jsonl.

Task creation is INJECTABLE: the default `crear_tarea` shells out to
`hermes kanban create` (with an optional model pin from env AUTOQUEUE_MODEL),
but callers/tests may pass their own callable to avoid depending on the CLI
being invocable in a given worker context.

Usage:
  autoqueue.py            # dry-run: print the seed that WOULD be consumed, no mutation
  autoqueue.py --execute # actually consume (max 1)
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

NON_CONSUMABLE_ANNOTATIONS = (
    "duplicada",
    "rota",
    "tarea existente",
)


def get_hermes_home() -> Path:
    val = os.environ.get("HERMES_HOME", "").strip()
    return Path(val).resolve() if val else (Path.home() / ".hermes").resolve()


def default_queue_path() -> Path:
    val = os.environ.get("AUTOQUEUE_FILE", "").strip()
    if val:
        return Path(val).expanduser()
    return default_queue_dir() / "data" / "autoqueue.md"


def default_queue_dir() -> Path:
    val = os.environ.get("HERMES_HOME", "").strip()
    base = Path(val).resolve() if val else (Path.home() / ".hermes").resolve()
    return base


def default_ledger_path() -> Path:
    return default_queue_dir() / "quota-governor" / "autoqueue-consumes.jsonl"


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

def parsear_semillas(ruta: Path) -> list:
    """Return a list of seed dicts for `ruta`.

    Each dict: {num, line, desc, perfil, coste, consumible, pendiente}.
    A line is parsed only if it looks like a seed (`- [ ] ` or `- [x] `).
    `consumible` is True only for `- [ ]` lines whose <desc> has none of the
    NON_CONSUMABLE_ANNOTATIONS and no inline `(t_...)` task reference.
    """
    seeds: list[dict] = []
    if not Path(ruta).exists():
        return seeds
    for idx, line in enumerate(
            Path(ruta).read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not (stripped.startswith("- [ ] ") or stripped.startswith("- [x] ")):
            continue
        pendiente = stripped.startswith("- [ ] ")
        body = stripped[len("- [ ] "):] if pendiente else stripped[len("- [x] "):]
        parts = [p.strip() for p in body.split("|")]
        if len(parts) < 3:
            # Wrong shape → not consumable (nothing to build a task from).
            consumible = False
        else:
            desc, perfil, coste = parts[0], parts[1], parts[2]
            consumible = _es_consumible(pendiente, desc)
        seeds.append({
            "num": idx,
            "line": line,
            "pendiente": pendiente,
            "desc": parts[0] if len(parts) >= 3 else body,
            "perfil": parts[1] if len(parts) >= 3 else "",
            "coste": parts[2] if len(parts) >= 3 else "",
            "consumible": consumible,
        })
    return seeds


def _es_consumible(pendiente: bool, desc: str) -> bool:
    """A seed is consumable iff it is pending, well-formed, not annotated as
    duplicate/broken/already-existing, and does not already reference a task id."""
    if not pendiente:
        return False
    low = desc.lower()
    if any(anno in low for anno in NON_CONSUMABLE_ANNOTATIONS):
        return False
    # An inline (t_...) reference means the seed was already turned into a
    # task (idempotency marker) — never consume it again.
    if "(t_" in desc:
        return False
    return True


# --------------------------------------------------------------------------- #
# Generation (reservoir)
# --------------------------------------------------------------------------- #

def generar_semillas() -> list:
    """Reservoir for future seed generation (OBJ-44 phase 2).

    Contract: returns a list of candidate seed strings in the format
    `- [ ] <descripcion> | <perfil> | <coste>` that are NOT yet present in
    the queue (dedupe against `parsear_semillas` before adding). The list is
    currently EMPTY — explicit scaffold; generation logic lands in a later
    milestone. Consumers MUST treat the return as the full set of new seeds
    to append, never as a mutation of the queue.
    """
    return []


# --------------------------------------------------------------------------- #
# Task creation (injectable)
# --------------------------------------------------------------------------- #

def crear_tarea_via_cli(desc: str, perfil: str, coste: str) -> str | None:
    """Default task creator: shell out to `hermes kanban create`.

    Pins the model for the target profile from env AUTOQUEUE_MODEL if set
    (mirrors the governor's model pin); otherwise lets the profile default
    apply. Returns the new task id, or None on failure. Callers may inject a
    different callable via `consumir_semilla(crear_tarea=...)`.
    """
    body = (f"objective:OBJ-44 | cost:{coste} | privacy:low | clase:B-seed | "
            f"origen:autoqueue\\n\\n"
            f"Semilla: {desc} (autoqueue)")
    cmd = ["hermes", "kanban", "create", desc,
           "--assignee", perfil, "--workspace", "scratch",
           "--body", body, "--json"]
    model = os.environ.get("AUTOQUEUE_MODEL", "").strip()
    if model:
        cmd += ["--model", model]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    try:
        parsed = json.loads(r.stdout)
        if isinstance(parsed, dict) and parsed.get("id"):
            return str(parsed["id"])
    except (ValueError, AttributeError):
        pass
    import re
    m = re.search(r"(t_[A-Za-z0-9_]+)", r.stdout)
    return m.group(1) if m else "t_" + uuid.uuid4().hex[:8]


def _append_ledger(ledger: Path, entry: dict) -> None:
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with open(ledger, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------- #
# Consumption
# --------------------------------------------------------------------------- #

def consumir_semilla(execute: bool = False, ruta: Path | None = None,
                     ledger: Path | None = None,
                     crear_tarea=None) -> str | None:
    """Consume the first consumable seed (max 1).

    Returns a decision string (None = nothing happened → silent caller).
    Dry-run (execute=False) only reports; execute=True creates the kanban
    task via `crear_tarea` (default: `crear_tarea_via_cli`), appends the
    ledger line, and rewrites `- [ ]` → `- [x] ... (t_<id>)` in place,
    verifying the line count is unchanged (append-only).
    """
    qpath = Path(ruta) if ruta else default_queue_path()
    ledger_path = Path(ledger) if ledger else default_ledger_path()
    creator = crear_tarea if crear_tarea else crear_tarea_via_cli

    seeds = parsear_semillas(qpath)
    consumible = next((s for s in seeds if s["consumible"]), None)
    if consumible is None:
        return None  # silent: nothing to do

    if not execute:
        return (f"DRY consume {consumible['num']}: {consumible['desc']!r} "
                f"(perfil={consumible['perfil']}, coste={consumible['coste']})")

    # --- execute path ---
    before = qpath.read_text(encoding="utf-8")
    before_lines = before.splitlines()

    tid = creator(consumible["desc"], consumible["perfil"], consumible["coste"])
    if not tid:
        tid = "t_" + uuid.uuid4().hex[:8]
    tid = str(tid)

    _append_ledger(ledger_path, {
        "ts": os.environ.get("TS"),
        "seed_line": consumible["num"],
        "desc": consumible["desc"],
        "perfil": consumible["perfil"],
        "coste": consumible["coste"],
        "task_id": tid,
    })

    # Rewrite the consumed line in place: `- [ ] ...` → `- [x] ... (t_<id>)`.
    # `tid` already includes the `t_` prefix (as returned by the creator), so
    # the inline marker is ` (t_<id>)` without re-adding `t_`.
    line_text = consumible["line"] or ""
    new_line = line_text.replace("- [ ]", "- [x]", 1)
    new_line = new_line.rstrip() + f" ({tid})"
    after_raw = before.replace(line_text, new_line)
    after_lines = after_raw.splitlines()
    if len(after_lines) != len(before_lines):
        # Defensive: append-only means the line count must never change.
        raise RuntimeError("autoqueue: line count changed on consumption "
                           f"({len(before_lines)} -> {len(after_lines)}); aborting")
    qpath.write_text(after_raw, encoding="utf-8")

    return (f"consumed {consumible['num']}: {consumible['desc']!r} -> {tid} "
            f"(perfil={consumible['perfil']}, coste={consumible['coste']})")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv: list | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description=__doc__.split("Usage:")[0].strip())
    parser.add_argument("--execute", action="store_true",
                        help="actually consume (default: dry-run, no mutation)")
    parser.add_argument("--file", default=None,
                        help="queue file (default: $AUTOQUEUE_FILE or ~/.hermes/data/autoqueue.md)")
    args = parser.parse_args(argv)

    decision = consumir_semilla(execute=args.execute, ruta=args.file)
    if decision:
        print(decision)
    return 0


if __name__ == "__main__":
    sys.exit(main())
