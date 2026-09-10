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
     providers_ok,
     supply_created_24h, supply_closed_24h, supply_ratio}
  - stdout SOLO en anomalia (patron watchdog): si no se pudo muestrear
    NINGUN provider o el board esta ilegible. Silencio = muestra OK.

OBJ-29 (t_a821194b; diseno en triage t_99e3b849, capa 1 EXTRACCION):
supply_ratio = tareas_creadas / tareas_cerradas en la ventana rodante de
24h UTC (ventana "por dia" rodante; los buckets diarios se reconstruyen
con la ultima fila de cada dia UTC).
  created = task_events kind='created' — exactamente una por tarea.
  closed  = task_events kind='completed' — done sellado. 'archived' NO
            cuenta: es limpieza posterior de tareas ya completadas y
            duplicaria el denominador.
  supply_ratio = created/closed (3 decimales). None cuando closed==0:
            indefinido, NO deficit — consumidores (alarma OBJ-27 F2)
            deben mirar supply_created_24h/supply_closed_24h crudos.
  Ratio < 1.0 sostenido con cuota libre = deficit de suministro.

Reutiliza providers.py del plugin (retries + last-good fallback).
Fallo de un provider no rompe la fila: se registra providers_ok y pcts=None.
"""
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

HERMES_SRC = os.environ.get(
    "HERMES_SRC", os.path.expanduser("~/.hermes/hermes-agent"))
_PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERMES_ROOT = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
PLUGIN_DIR = _PLUGIN_ROOT
KANBAN_DB = Path(HERMES_ROOT) / "kanban.db"
OUT = Path(HERMES_ROOT) / "profiles" / "pr-ollama" / "quota-governor" / "metrics-history.jsonl"
LAST_GOOD_DIR = Path(HERMES_ROOT) / "profiles" / "pr-ollama" / "quota-governor"
# OBJ-26: ledger de balance NanoGPT (constante inyectable para tests —
# MainRow la apunta a un path inexistente para que el bloque degrade a
# campos ausentes sin tocar el perfil real ni sondear la API).
NANOGPT_LEDGER = os.path.join(_PLUGIN_ROOT, "scripts",
                              "nanogpt-balance-ledger.py")


def board_counts():
    con = sqlite3.connect(f"file:{KANBAN_DB}?mode=ro", uri=True)
    n = con.execute(
        "SELECT "
        "SUM(status='running'), SUM(status='ready'), SUM(status='blocked'), "
        "SUM(status='triage') FROM tasks").fetchone()
    con.close()
    return {"running": n[0] or 0, "ready": n[1] or 0,
            "blocked": n[2] or 0, "triage": n[3] or 0}


VENTANA_SUPPLY_SEG = 86400  # OBJ-29: ventana rodante 24h (UTC)


def supply_counts(now_epoch=None, db_path=None):
    """OBJ-29: (creadas, cerradas) en la ventana rodante de 24h UTC.

    created: task_events kind='created' — exactamente una por tarea
    (verificado live: COUNT(created) == COUNT(tasks) en el board completo).
    closed:  kind='completed' — done sellado. 'archived' NO cuenta (limpieza
    posterior de tareas ya completadas; contarlas duplicaria el denominador).
    """
    now = time.time() if now_epoch is None else now_epoch
    lo = now - VENTANA_SUPPLY_SEG
    con = sqlite3.connect(f"file:{db_path or KANBAN_DB}?mode=ro", uri=True)
    try:
        created, closed = con.execute(
            "SELECT "
            "(SELECT COUNT(*) FROM task_events "
            " WHERE kind='created' AND created_at >= ?), "
            "(SELECT COUNT(*) FROM task_events "
            " WHERE kind='completed' AND created_at >= ?)",
            (lo, lo)).fetchone()
    finally:
        con.close()
    return int(created), int(closed)


def supply_ratio(created, closed):
    """created/closed redondeado a 3 decimales; None si closed <= 0.

    None NO significa deficit: sin cierres el ratio es indefinido — los
    consumidores (alarma OBJ-27 F2) miran los componentes crudos.
    """
    if not closed or closed <= 0:
        return None
    return round(created / closed, 3)


def from_last_good(name):
    try:
        d = json.load(open(LAST_GOOD_DIR / f"{name}-last-good.json"))
        return d
    except Exception:
        return {}


def main():
    row: dict = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    # board
    try:
        row.update(board_counts())
    except Exception as e:
        print(f"quota-metrics: board ilegible: {e}")
        return 0

    # OBJ-29: supply_ratio (suministro de objetivos, t_99e3b849 capa 1).
    # Fallo de lectura no rompe la fila: campos a None (known-unknown).
    try:
        created, closed = supply_counts()
        row["supply_created_24h"] = created
        row["supply_closed_24h"] = closed
        row["supply_ratio"] = supply_ratio(created, closed)
    except Exception:
        row["supply_created_24h"] = None
        row["supply_closed_24h"] = None
        row["supply_ratio"] = None

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
        sys.path.insert(0, os.path.join(_PLUGIN_ROOT, "scripts"))
        import importlib.util as _ilu
        _spec = _ilu.spec_from_file_location("nanogpt_balance_ledger",
                                             NANOGPT_LEDGER)
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