#!/usr/bin/python3.12
"""budget_check.py — FASE 3 del sistema predictivo de cuota (OBJ-24).

Cruza, para cada tarea elegible (ready/todo/triage con assignee válido),
triple dato:
  (a) calibración OBJ-07 (docs/quota-planner.md §2.4): coste típico del
      task‑cost tag en % de la ventana de su provider,
  (b) cuota libre del provider asignado (del forecast F2 / last‑good),
  (c) forecast F2 (ETA_90 del provider).

Regla: si el coste estimado de la tarea excede el 10 % de la cuota libre restante
de su provider, reasignar al provider con más holgura o dejarla en triage con
la razón.

Por razones de mantenibilidad, la lógica de obra por‑tarea se mueve a
`_evaluate_task`.  La función `evaluate` se queda con la iteración y la vuelta
al listado de decisiones.
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
HERMES_HOME = os.environ.get("HERMES_HOME", "").strip() or os.path.expanduser("~/.hermes/profiles/pr-ollama")
STATE_DIR = Path(HERMES_HOME) / "quota-governor"

# Calibración OBJ‑07 (docs/quota‑planner.md §2.4, calib‑2026‑09‑08):
COST_CLASS_PCT = {
    "micro": 0.25,
    "tiny": 0.5,
    "small": 2.1,
    "medium": 4.0,
    "complex": 24.8,
}
DEFAULT_CLASS = "tiny"
MAX_FREE_FRACTION_PCT = 10.0

PROFILE_PROVIDER = {
    "pr-ollama": "pr-ollama",
    "pr-nanogpt": "pr-nanogpt",
    "pr-opencode": "pr-opencode",
}
ELEGIBLE_STATUSES = ("ready", "todo", "triage")

# ── Estado de balance de pr‑nanogpt ─────────────────────────────────────────────

_NANOGPT_BUDGET = {"loaded": False, "ctx": None}


def _nanogpt_budget_ctx():
    """nanogpt_balance budget context (gate‑compatible, cached per run)."""
    if _NANOGPT_BUDGET["loaded"]:
        return _NANOGPT_BUDGET["ctx"]
    _NANOGPT_BUDGET["loaded"] = True
    try:
        import importlib.util
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "nanogpt-balance-ledger.py")
        spec = importlib.util.spec_from_file_location("nanogpt_balance_ledger", path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load spec for {path}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        ctx, _warn = mod.budget_context()
        _NANOGPT_BUDGET["ctx"] = ctx
    except Exception:
        _NANOGPT_BUDGET["ctx"] = None
    return _NANOGPT_BUDGET["ctx"]


def _nanogpt_budget_level():
    """'ok'|'warn'|'stop'|None (None = budget context unavailable)."""
    ctx = _nanogpt_budget_ctx()
    return ctx.get("level") if ctx else None


def _nanogpt_subscription_free():
    """Weekly subscription remainder (%) for pr‑nanogpt tasks."""
    ctx = _nanogpt_budget_ctx()
    if not ctx:
        return None
    pct = ctx.get("weekly_tokens_pct")
    if pct is None:
        return None
    return max(100.0 - float(pct), 0.0)

# ── Helpers de estado ───────────────────────────────────────────────────────────


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

    free = 100 – pct_now. Provider ausente → None (no veto)."""
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
    """cost:<class> del header del body. None si no lleva."""
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

# ── Núcleo ─────────────────────────────────────────────────────────────────────


def _evaluate_task(row, forecast, enforce):
    """Generate decision for a single task row."""
    profile = row["assignee"]
    if profile not in PROFILE_PROVIDER:
        return None
    cls = parse_cost_tag(row["body"]) or DEFAULT_CLASS
    class_pct = COST_CLASS_PCT.get(cls, COST_CLASS_PCT[DEFAULT_CLASS])
    free = provider_free_pct(forecast, profile)
    if profile == "pr-nanogpt":
        ng_level = _nanogpt_budget_level()
        sub_free = _nanogpt_subscription_free()
        if ng_level == "stop" and sub_free is not None:
            free = sub_free if free is None else min(free, sub_free)
        elif free is None and sub_free is not None:
            free = sub_free
    if free is None:
        return None
    free_needed = free * MAX_FREE_FRACTION_PCT / 100.0
    if class_pct <= free_needed:
        return None
    alt, alt_free = pick_alternative(forecast, profile)
    if alt is not None and alt_free is not None:
        alt_needed = alt_free * MAX_FREE_FRACTION_PCT / 100.0
        if class_pct <= alt_needed:
            decision = {
                "task_id": row["id"],
                "title": row["title"],
                "profile": profile,
                "class": cls,
                "class_pct": class_pct,
                "free_pct": round(free, 1),
                "verdict": "reassign",
                "to_profile": alt,
                "to_free_pct": round(alt_free, 1),
            }
            if enforce:
                _reassign(row["id"], alt)
                decision["executed"] = True
            return decision
    decision = {
        "task_id": row["id"],
        "title": row["title"],
        "profile": profile,
        "class": cls,
        "class_pct": class_pct,
        "free_pct": round(free, 1),
        "verdict": "triage",
        "reason": (
            f"{cls} ({class_pct}%) > 10% de cuota libre ({free:.1f}%) sin alternativa con holgura"
        ),
    }
    if enforce:
        _to_triage(row["id"], decision["reason"])
        decision["executed"] = True
    return decision


def evaluate(db_path=KANBAN_DB, forecast=None, enforce=False, dry_run=True):
    """Evalúa el presupuesto de cada tarea elegible."""
    decisions = []
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT id, title, assignee, body FROM tasks "
            "WHERE status IN ('ready','todo','triage')"
        ).fetchall()
    finally:
        con.close()
    for r in rows:
        d = _evaluate_task(r, forecast, enforce)
        if d:
            decisions.append(d)
    return decisions

# ── Helpers de mutación ───────────────────────────────────────────────────────────


def _reassign(task_id, profile):
    """hermes kanban assign (solo --enforce). Best‑effort, nunca fatal."""
    import subprocess
    try:
        subprocess.run(
            ["hermes", "kanban", "assign", task_id, "--assignee", profile],
            capture_output=True, text=True, timeout=30,
        )
    except Exception:
        pass


def _to_triage(task_id, reason):
    """Devuelve la tarea a triage con la razón (solo --enforce)."""
    import subprocess
    try:
        subprocess.run(
            ["hermes", "kanban", "update", task_id, "--status", "triage", "--note", reason],
            capture_output=True, text=True, timeout=30,
        )
    except Exception:
        pass

# ── CLI ─────────────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="OBJ-24 F3 budget check")
    ap.add_argument("--enforce", action="store_true", help="aplica las decisiones (veto real; default: log)")
    ap.add_argument("--json", action="store_true", help="salida JSON en stdout (siempre log, nunca silencio)")
    args = ap.parse_args()
    forecast = load_forecast()
    decisions = evaluate(forecast=forecast, enforce=args.enforce)
    if args.json:
        print(json.dumps({"mode": "enforce" if args.enforce else "observe", "decisions": decisions}, ensure_ascii=False, indent=1))
    else:
        for d in decisions:
            if d["verdict"] == "reassign":
                print(
                    f"budget[{d['verdict']}]: {d['task_id']} {d['class']} ({d['class_pct']}%) vs free {d['free_pct']}% en {d['profile']} -> {d['to_profile']} (free {d['to_free_pct']}%)"
                )
            else:
                print(
                    f"budget[{d['verdict']}]: {d['task_id']} {d['class']} ({d['class_pct']}%) vs free {d['free_pct']}% en {d['profile']} -> {d['reason']}"
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
