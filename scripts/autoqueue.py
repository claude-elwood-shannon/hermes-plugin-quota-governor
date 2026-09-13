#!/usr/bin/env python3
"""autoqueue.py — reservorio permanente de semillas (OBJ-44).

Mecanizado de la cola permanente ~/.hermes/data/autoqueue.md: lee las
semillas en formato `- [ ] <desc> | <perfil> | <coste>`, identifica las
consumibles (sin anotaciones) y consume como máximo una por invocación,
marcándola `[x] ... (t_<id>)` in-place sin tocar el `wc -l`. Append-only
JSONL de registro en ~/.hermes/quota-governor/autoqueue-consumes.jsonl.

Contrato futuro de generar_semillas(): el reservorio explícito (la cola)
es la fuente primaria. Cuando exista la semilla de auto-reservorio, esto
generará semillas accionables adicionales; por ahora devuelve [] para
nunca inventar trabajo (regla de oro: no filler).

Patrón: mismo que tick-cola-viva.py — dry-run por defecto, JSONL de
registro, exit 0 siempre, sin rutas absolutas de host en el código.

Uso:
  autoqueue.py                 # dry-run (solo informa, sin mutar)
  autoqueue.py --execute       # consume 1 semilla y la marca
Env:
  AUTOQUEUE_FILE               # ruta de la cola (default ~/.hermes/data/autoqueue.md)
Exit: 0 siempre; stdout no vacío solo si hubo acción.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ANNOTATIONS = ("duplicada", "rota", "tarea existente")


def default_queue_path() -> Path:
    """Ruta de la cola: env AUTOQUEUE_FILE o ~/.hermes/data/autoqueue.md."""
    if val := os.environ.get("AUTOQUEUE_FILE", "").strip():
        return Path(val).expanduser()
    return Path.home() / ".hermes" / "data" / "autoqueue.md"


def default_log_path() -> Path:
    """Registro append-only de consumos (JSONL)."""
    return Path.home() / ".hermes" / "quota-governor" / "autoqueue-consumes.jsonl"


def parsear_semillas(ruta: Path) -> list[dict]:
    """Lee las líneas `- [ ] <desc> | <perfil> | <coste>` de la cola.

    Devuelve un dict por línea pendiente: {'desc','perfil','coste',
    'consumible','line'}. `consumible=False` si la línea lleva
    anotaciones — palabras clave (duplicada/rota/tarea existente) o un
    sufijo `(t_` (ya encargada a una tarea). Las líneas `[x]` hechas se
    ignoran (no son candidatas a consumo).
    """
    semillas: list[dict] = []
    for raw in ruta.read_text(encoding="utf-8").splitlines():
        if not raw.startswith("- [ ]"):
            continue
        parts = raw.split("|")
        if len(parts) < 3:
            continue
        desc = parts[0].replace("- [ ]", "", 1).strip()
        perfil = parts[1].strip()
        coste = parts[2].strip()
        lower = desc.lower()
        anotada = any(nota in lower for nota in ANNOTATIONS) or "(t_" in lower
        semillas.append({
            "desc": desc, "perfil": perfil, "coste": coste,
            "consumible": not anotada, "line": raw,
        })
    return semillas


def generar_semillas() -> list[str]:
    """Reservorio explícito por ahora: genera [].

    Contrato futuro: cuando la cola esté baja, retornar semillas
    accionables adicionales (accionadas desde una semilla de
    auto-reservorio). Nunca inventar trabajo: si no hay fuente legítima,
    devolver [] (regla de oro: no filler).
    """
    return []


def _log(log: Path, entry: dict) -> None:
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        # Fail-open: un fallo de registro nunca rompe el consumo.
        pass


def consumir_semilla(execute: bool = False, crear_tarea=None,
                     ruta: Path | None = None, log: Path | None = None) -> str:
    """Consume la primera semilla consumible (máximo 1).

    - execute=False (dry-run): solo informa y registra, no muta la cola.
    - execute=True y crear_tarea inyectada: crea la tarea, marca la línea
      `- [ ]` -> `- [x] ... (t_<id>)` in-place conservando el `wc -l`, y
      registra en append-only.
    - Crea la tarea vía la función inyectable `crear_tarea()` (mock en
      tests; nunca llama al kanban real directamente).

    Devuelve el mensaje de acción ("" si no hubo acción).
    """
    queue = ruta if ruta is not None else default_queue_path()
    ledger = log if log is not None else default_log_path()
    semillas = [s for s in parsear_semillas(queue) if s["consumible"]]
    if not semillas:
        return ""

    semilla = semillas[0]
    ts = time.time()
    base = {"semilla": semilla["desc"], "perfil": semilla["perfil"],
            "coste": semilla["coste"], "ts": ts, "execute": execute}

    if not execute:
        _log(ledger, {**base, "dry_run": True, "tarea": None})
        return f"dry-run: consumiria {semilla['desc']} | {semilla['perfil']} | {semilla['coste']}"

    if crear_tarea is None:
        _log(ledger, {**base, "action": "execute-sin-crear_tarea"})
        return "execute sin crear_tarea inyectada: no se puede marcar"

    tarea_id = crear_tarea()
    if not tarea_id:
        _log(ledger, {**base, "action": "crear-fallo"})
        return f"execute: fallo al crear tarea para {semilla['desc']}"

    # Marca in-place conservando las demás líneas y el wc -l exacto.
    new_line = semilla["line"].replace("- [ ]", "- [x]", 1) + f" (t_{tarea_id})"
    lines = queue.read_text(encoding="utf-8").splitlines()
    idx = lines.index(semilla["line"])
    lines[idx] = new_line
    queue.write_text("\n".join(lines) + "\n", encoding="utf-8")

    _log(ledger, {**base, "dry_run": False, "tarea": tarea_id})
    return f"consumida: {semilla['desc']} -> t_{tarea_id}"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    p.add_argument("--execute", action="store_true",
                   help="consumir una semilla de verdad (crea tarea y marca)")
    args = p.parse_args(argv)

    crear_tarea = None
    if args.execute:
        # Función inyectada real: crea una tarea kanban vía CLI. Se ejecuta
        # SOLO bajo --execute explícito; los tests la sustituyen por mock.
        def crear_tarea_cli() -> str | None:
            title = "semilla autoqueue consumida"
            body = "origin:autoqueue.py (semilla consumida)."
            r = os.popen(f"hermes kanban create {title} --assignee pr-ollama "
                         f"--workspace scratch --body {body} --json").read()
            try:
                return json.loads(r or "{}").get("id")
            except ValueError:
                return None
        crear_tarea = crear_tarea_cli

    msg = consumir_semilla(execute=args.execute, crear_tarea=crear_tarea)
    if msg:
        print(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
