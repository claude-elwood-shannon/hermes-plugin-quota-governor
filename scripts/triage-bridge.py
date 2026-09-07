#!/usr/bin/python3.12
"""triage-bridge.py — OBJ-21 pieza 1: puente triage->todo sin LLM.

Promueve a `todo` las tareas en triage cuyo body ya llega COMPLETO
(objective: y cost: presentes — las crean objective-proposer o el
autonomous-task-creator ya especificadas). Usa la API del nucleo
hermes_cli.kanban_db.specify_triage_task(), que ademas:
  - rechaza tareas con human-gate pendiente (block_loop needs_input)
  - escribe el evento 'specified' auditable
  - recalcula ready inmediatamente (sin esperar tick del dispatcher)

Criterios (todos obligatorios):
  - status = triage
  - body contiene 'objective:' Y 'cost:' (header estandar del ledger)
  - human-gate pendiente lo rechaza (specify_triage_task lo verifica)
  - Tope duro: 3 por tick (anti-flood)

NOTA DE DISENO (8-sep): NO re-ejecuta validate-guardrails.py aqui. Esos
guardrails validan PROPUESTAS y ya corrieron al crear cada tarea
(objective-proposer GR1-GR11, creator via prompt+gate). Re-validar en
specify con GR6 (1 propuesta/dia) haria el puente inutil justo el dia
que ya hubo una propuesta — el estrangulamiento que OBJ-21 elimina.

Kill switch: ~/.hermes/quota-governor/PROMOTE-STOP (mismo que autopromote).
Ledger: ~/.hermes/quota-governor/triage-bridge-ledger.jsonl (append-only).

Despliegue: copia en ~/.hermes/profiles/pr-ollama/scripts/triage-bridge.py,
registrado como cron no_agent 'triage-bridge' cada 30m (coste 0 tokens).

Uso: triage-bridge.py            (ejecuta)
     triage-bridge.py --dry-run  (solo decisiones)
"""
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
KANBAN_DB = HERMES_HOME / "kanban.db"
STATE_DIR = HERMES_HOME / "quota-governor"
LEDGER = STATE_DIR / "triage-bridge-ledger.jsonl"
KILL_SWITCH = STATE_DIR / "PROMOTE-STOP"
HERMES_SRC = HERMES_HOME / "hermes-agent"
MAX_PER_TICK = 3


def log(entry, dry=False):
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with open(LEDGER, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    if dry:
        print("DRY:", json.dumps(entry, ensure_ascii=False))


def main():
    dry = "--dry-run" in sys.argv
    now = datetime.now(timezone.utc).isoformat()

    if KILL_SWITCH.exists():
        log({"ts": now, "action": "skipped", "reason": "PROMOTE-STOP activo"}, dry)
        return

    if not KANBAN_DB.exists():
        return

    conn = sqlite3.connect(f"file:{KANBAN_DB}?mode=ro", uri=True)
    rows = conn.execute(
        "SELECT id, title, body FROM tasks WHERE status='triage' ORDER BY created_at"
    ).fetchall()
    conn.close()

    candidates = []
    for task_id, title, body in rows:
        text = body or ""
        if "objective:" in text and "cost:" in text:
            candidates.append((task_id, title, text))

    if not candidates:
        return

    # sys.path despues de leer el board: el import necesita el nucleo
    sys.path.insert(0, str(HERMES_SRC))
    try:
        from hermes_cli.kanban_db import specify_triage_task
    except ImportError as e:
        log({"ts": now, "action": "error", "reason": f"import fallo: {e}"}, dry)
        return

    promoted = 0
    for task_id, title, body in candidates:
        if promoted >= MAX_PER_TICK:
            break

        if dry:
            log({"ts": now, "task": task_id, "action": "would-promote",
                 "title": title}, dry)
            promoted += 1
            continue

        # conexion de escritura para el nucleo
        wconn = sqlite3.connect(KANBAN_DB)
        wconn.row_factory = sqlite3.Row
        ok = specify_triage_task(wconn, task_id, author="triage-bridge")
        wconn.close()
        if ok:
            promoted += 1
            log({"ts": now, "task": task_id, "action": "promoted",
                 "title": title})
            print(f"TRIAGE-BRIDGE: {task_id} -> todo ({title})")
        else:
            log({"ts": now, "task": task_id, "action": "specify-rejected",
                 "reason": "not in triage o human-gate pendiente"})


if __name__ == "__main__":
    main()