#!/usr/bin/python3.12
"""budget_check.py — FASE 3 del sistema predictivo de cuota (OBJ-24).

Cruza, para cada tarea elegible (ready/todo/triage con assignee válido),
tres datos:
  (a) calibración OBJ-07 (docs/quota-planner.md §2.4): coste típico del
      task-cost tag en % de la ventana de su provider,
  (b) cuota libre del provider asignado (del forecast F2 / last-good),
  (c) forecast F2 (ETA_90 del provider).

Regla (criterio definido): si el coste estimado de la tarea excede el 10%
de la cuota libre restante de su provider, reasignar al provider con más
holgura o dejarla en triage con la razón.

FASE 3.0 — OBSERVADOR: por defecto solo LOG (stdout no vacío = decisiones
tomadas; patrón watchdog). El veto real (--enforce) queda tras una semana
sin falsos positivos; --enforce reasigna vía `hermes kanban assign`.

TODO no_agent: cero tokens. Lee metrics/forecast/kanban.db (read-only).
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────

KANBAN_DB = os.path.expanduser("~/.hermes/kanban.db")

HERMES_HOME = os.environ.get("HERMES_HOME", "").strip() or os.path.expanduser(
    "~/.hermes/profiles/pr-ollama")
STATE_DIR = Path(HERMES_HOME) / "quota-governor"

# Calibración OBJ-07 (docs/quota-planner.md §2.4, calib-2026-09-08):
# % medio de la ventana Ollama/OpenCode que consume cada clase.
# MICRO sin datos en la ventana medida (0.5% asignado por orden de
# magnitud desde TINY). COMPLEX 24.8% medido (el body de OBJ-24 decía
# ~10%; manda el dato medido — decisión registrada en la tarea).
COST_CLASS_PCT = {
    "micro": 0.25,
    "tiny": 0.5,
    "small": 2.1,
    "medium": 4.0,
    "complex": 24.8,
}
# Clases sin tag: el backstop del creator reescribe a tiny; aquí lo mismo.
DEFAULT_CLASS = "tiny"

# Umbral del criterio: la tarea no debe exceder este % de la cuota libre
MAX_FREE_FRACTION_PCT = 10.0

PROFILE_PROVIDER = {
    "pr-ollama": "pr-ollama",
    "pr-nanogpt": "pr-nanogpt",
    "pr-opencode": "pr-opencode",
    # el gate puede recomendar pr-openrouter (parcado); sin datos de
    # calibración -> exento (parked nunca recibe auto-tareas)
}

ELEGIBLE_STATUSES = ("ready", "todo", "triage")


# ── Helpers de estado ─────────────────────────────────────────────────────────

def load_forecast():
    """forecast.json del F2 (ruta del perfil activo). {} si falta."""
    path = STATE_DIR / "forecast.json"
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def provider_free_pct(forecast, profile):
    """Cuota libre % del provider asignado, según forecast F2.

    free = 100 - pct_now. Provider ausente -> None (no veto).
    """
    f = (forecast.get("providers") or {}).get(profile)
    if not isinstance(f, dict):
        return None
    pct = f.get("pct_now")
    if pct is None:
        return None
    try:
        return max(0.0, 100.0 - float(pct))
    except (TypeError, ValueError):
        return None


def eta90_hours(forecast, profile):
    f = (forecast.get("providers") or {}).get(profile)
    if not isinstance(f, dict):
        return None
    raw = f.get("eta_90_hours")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def parse_cost_tag(body):
    """cost:<clase> del header del body. None si no lleva."""
    if not body:
        return None
    for line in body.splitlines()[:8]:
        line = line.strip()
        if line.startswith("cost:"):
            cls = line[5:].strip().lower()
            if cls in COST_CLASS_PCT:
                return cls
            return None
    return None


def pick_alternative(forecast, exclude_profile):
    """Provider con más holgura (mayor cuota libre), excluyendo el actual."""
    best, best_free = None, -1.0
    for profile in (forecast.get("providers") or {}):
        if profile == exclude_profile:
            continue
        free = provider_free_pct(forecast, profile)
        if free is None:
            continue
        if free > best_free:
            best, best_free = profile, free
    return best, best_free


# ── Núcleo ────────────────────────────────────────────────────────────────────

def evaluate(db_path=KANBAN_DB, forecast=None, enforce=False,
             dry_run=True):
    """Evalúa el presupuesto de cada tarea elegible.

    Devuelve lista de decisiones:
      {task_id, title, profile, class_pct, free_pct, verdict, action}
    Con enforce=False nunca muta el board (solo log).
    """
    decisions = []
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT id, title, assignee, body FROM tasks "
            "WHERE status IN ('ready','todo','triage')").fetchall()
    finally:
        con.close()

    for r in rows:
        profile = r["assignee"]
        if profile not in PROFILE_PROVIDER:
            continue  # pr-openrouter / inválidos: fuera del alcance (G1)
        cls = parse_cost_tag(r["body"]) or DEFAULT_CLASS
        class_pct = COST_CLASS_PCT.get(cls, COST_CLASS_PCT[DEFAULT_CLASS])
        free = provider_free_pct(forecast, profile)
        if free is None:
            continue  # sin datos del provider: no veto (falsos positivos=0)
        free_needed = free * MAX_FREE_FRACTION_PCT / 100.0
        if class_pct <= free_needed:
            continue  # cabe
        # excede: ¿reasignar o triage?
        alt, alt_free = pick_alternative(forecast, profile)
        if alt is not None and alt_free is not None:
            alt_needed = alt_free * MAX_FREE_FRACTION_PCT / 100.0
            if class_pct <= alt_needed:
                decision = {"task_id": r["id"], "title": r["title"],
                            "profile": profile, "class": cls,
                            "class_pct": class_pct,
                            "free_pct": round(free, 1),
                            "verdict": "reassign",
                            "to_profile": alt,
                            "to_free_pct": round(alt_free, 1)}
                if enforce:
                    _reassign(r["id"], alt)
                    decision["executed"] = True
                decisions.append(decision)
                continue
        decision = {"task_id": r["id"], "title": r["title"],
                    "profile": profile, "class": cls,
                    "class_pct": class_pct,
                    "free_pct": round(free, 1),
                    "verdict": "triage",
                    "reason": (f"{cls} ({class_pct}%) > 10% de cuota libre "
                               f"({free:.1f}%) sin alternativa con holgura")}
        if enforce:
            _to_triage(r["id"], decision["reason"])
            decision["executed"] = True
        decisions.append(decision)
    return decisions


def _reassign(task_id, profile):
    """hermes kanban assign (solo --enforce). Best-effort, nunca fatal."""
    import subprocess
    try:
        subprocess.run(["hermes", "kanban", "assign", task_id,
                        "--assignee", profile],
                       capture_output=True, text=True, timeout=30)
    except Exception:
        pass


def _to_triage(task_id, reason):
    """Devuelve la tarea a triage con la razón (solo --enforce)."""
    import subprocess
    try:
        subprocess.run(["hermes", "kanban", "update", task_id,
                        "--status", "triage", "--note", reason],
                       capture_output=True, text=True, timeout=30)
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser(description="OBJ-24 F3 budget check")
    ap.add_argument("--enforce", action="store_true",
                    help="aplica las decisiones (veto real; default: log)")
    ap.add_argument("--json", action="store_true",
                    help="salida JSON en stdout (siempre log, nunca silencio)")
    args = ap.parse_args()

    forecast = load_forecast()
    decisions = evaluate(forecast=forecast, enforce=args.enforce)
    if args.json:
        print(json.dumps({"mode": "enforce" if args.enforce else "observe",
                          "decisions": decisions}, ensure_ascii=False, indent=1))
    else:
        for d in decisions:
            if d["verdict"] == "reassign":
                print(f"budget[{d['verdict']}]: {d['task_id']} {d['class']} "
                      f"({d['class_pct']}%) vs free {d['free_pct']}% en "
                      f"{d['profile']} -> {d['to_profile']} "
                      f"(free {d['to_free_pct']}%)")
            else:
                print(f"budget[{d['verdict']}]: {d['task_id']} {d['class']} "
                      f"({d['class_pct']}%) vs free {d['free_pct']}% en "
                      f"{d['profile']} -> {d['reason']}")
    # stdout vacío = sin decisiones (patrón watchdog)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())