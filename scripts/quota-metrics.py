#!/usr/bin/python3.12
"""quota-metrics.py — FASE 1 del sistema predictivo de cuota (OBJ-24).

Colector observador: cada tick muestrea cuota de todos los providers +
estado del board y APPENDEA una fila JSONL. Cero tokens (no_agent cron).
Base de las fases 2 (prediccion EMA) y 3 (presupuesto por tarea).

Salida:
  - append a ~/.hermes/profiles/pr-ollama/quota-governor/metrics-history.jsonl
    {ts, running, ready, blocked, triage,
     ollama_session_pct, ollama_weekly_pct, ollama_weekly_reqs,
     nanogpt_weekly_pct, opencode_weekly_pct, opencode_rolling_pct,
     providers_ok}
  - stdout SOLO en anomalia (patron watchdog): si no se pudo muestrear
    NINGUN provider o el board esta ilegible. Silencio = muestra OK.

Reutiliza providers.py del plugin (retries + last-good fallback).
Fallo de un provider no rompe la fila: se registra providers_ok y pcts=None.
"""
import json
import sqlite3
import sys
import time
from pathlib import Path

HERMES_SRC = "~/.hermes/hermes-agent"
PLUGIN_DIR = "REPO"
KANBAN_DB = Path("~/.hermes/kanban.db")
OUT = Path("~/.hermes/profiles/pr-ollama/quota-governor/metrics-history.jsonl")
LAST_GOOD_DIR = Path("~/.hermes/profiles/pr-ollama/quota-governor")


def board_counts():
    con = sqlite3.connect(f"file:{KANBAN_DB}?mode=ro", uri=True)
    n = con.execute(
        "SELECT "
        "SUM(status='running'), SUM(status='ready'), SUM(status='blocked'), "
        "SUM(status='triage') FROM tasks").fetchone()
    con.close()
    return {"running": n[0] or 0, "ready": n[1] or 0,
            "blocked": n[2] or 0, "triage": n[3] or 0}


def from_last_good(name):
    try:
        d = json.load(open(LAST_GOOD_DIR / f"{name}-last-good.json"))
        return d
    except Exception:
        return {}


def main():
    row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    # board
    try:
        row.update(board_counts())
    except Exception as e:
        print(f"quota-metrics: board ilegible: {e}")
        return 0

    # providers via last-good (el tick/gate ya los refresco hace <15min;
    # providers.py directo seria una segunda probe — evitamos duplicar
    # llamadas a la API: el last-good del ciclo actual SIRVE como sample)
    ok = 0
    og = from_last_good("ollama")
    if og:
        row["ollama_session_pct"] = og.get("session_pct")
        row["ollama_weekly_pct"] = og.get("weekly_pct")
        row["ollama_weekly_reqs"] = og.get("weekly_requests")
        if og.get("session_pct") is not None or og.get("weekly_pct") is not None:
            ok += 1
    ng = from_last_good("nanogpt")
    if ng:
        row["nanogpt_weekly_pct"] = ng.get("weekly_tokens_pct")
        if ng.get("weekly_tokens_pct") is not None:
            ok += 1
    # OBJ-26: nanogpt balance budget (exact /api/check-balance probe via
    # nanogpt-balance-ledger.py, 60s cache) + level + weekly budget state.
    ng_mod = None
    try:
        sys.path.insert(0, "REPO/scripts")
        import importlib.util as _ilu
        _spec = _ilu.spec_from_file_location(
            "nanogpt_balance_ledger",
            "REPO/scripts/nanogpt-balance-ledger.py")
        if _spec is None or _spec.loader is None:
            raise ImportError("cannot load nanogpt-balance-ledger spec")
        _mod = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        ng_mod = _mod
        ctx, _warn = _mod.budget_context()
        if ctx:
            row["nanogpt_balance_usd"] = ctx.get("usd_balance")
            row["nanogpt_budget_level"] = ctx.get("level")
            row["nanogpt_window_spent_usd"] = ctx.get("window_spent_usd")
    except Exception:
        pass  # F1 must never fail for the balance column
    # OBJ-26a follow-up (t_92d7f0d6): per-request ledger accumulators from
    # nanogpt-requests.jsonl (cross-profile merge — rows land under the
    # CAPTURING process's HERMES_HOME). Independientes de window_spent_usd
    # (probe): acumuladores distintos, se reportan por separado.
    try:
        if ng_mod is not None:
            req = ng_mod.request_window_totals_all_homes()
            # homes_read == 0: no capture data anywhere -> omitir campos
            # (None) en vez de reportar un 0.0 que pareceria "gasto cero".
            if req and req.get("homes_read"):
                row["nanogpt_request_balance_usd"] = req.get(
                    "request_balance_usd")
                row["nanogpt_request_covered_usd"] = req.get(
                    "request_covered_usd")
    except Exception:
        pass  # F1 must never fail for the request columns
    og2 = from_last_good("opencode_go")
    if og2:
        row["opencode_rolling_pct"] = og2.get("rolling_pct")
        row["opencode_weekly_pct"] = og2.get("weekly_pct")
        ok += 1
    row["providers_ok"] = ok

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")

    if ok == 0:
        print(f"quota-metrics: NINGUN provider sampleado — fila con board only: {json.dumps(row)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())