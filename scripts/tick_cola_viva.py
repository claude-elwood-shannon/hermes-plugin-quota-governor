#!/usr/bin/env python3
from typing import Any
"""tick_cola_viva.py — importable alias de scripts/tick-cola-viva.py (OBJ-44).

El modulo de runtime vive en un fichero con guiones (`tick-cola-viva.py`,
invocado por RUTA desde quota-governor-tick.sh fuera del repo), por lo que
Python no puede importarlo como `scripts.tick_cola_viva`. Este shim fino
(t_a8a4418c, reparacion de suite OBJ-44) carga el modulo real via importlib
y reexporta su API publica:

    from scripts.tick_cola_viva import run, build_successor, ...

No ejecuta NINGUNA logica al importar: el bloque `if __name__ == "__main__"`
del modulo con guiones no dispara via importlib, y el shim nunca toca el
sistema de ficheros del host. El intercambio en sys.modules hace que
importaciones posteriores (y monkeypatch de atributos como
`cv.create_task = ...`) resuelvan contra el modulo REAL, cuyos globals son
los que `run()` consulta.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REAL_PATH = _HERE / "tick-cola-viva.py"

_spec = importlib.util.spec_from_file_location("tick_cola_viva", _REAL_PATH)
if _spec is None or _spec.loader is None:  # pragma: no cover
    raise ImportError(f"no se pudo crear spec para {_REAL_PATH}")
_real = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_real)

# Reexport de la API publica (suficiente para `from ... import X` en la
# PRIMERA importacion, cuando el fromlist se resuelve contra este shim).
run = _real.run
main = _real.main
build_successor = _real.build_successor
successor_pattern = _real.successor_pattern
successor_signature = _real.successor_signature
successor_chain_open = _real.successor_chain_open
_successor_stamp = _real._successor_stamp
TITLE_MAX_CHARS = _real.TITLE_MAX_CHARS

# Importaciones posteriores resuelven contra el modulo REAL: `import
# scripts.tick_cola_viva as cv; cv.create_task = fake` parchea los globals
# que run() realmente consulta (CPython >= 3.7: `import X as Y` relee
# sys.modules tras ejecutar el modulo).
sys.modules[__name__] = _real

__all__ = [
    "run",
    "main",
    "build_successor",
    "successor_pattern",
    "successor_signature",
    "successor_chain_open",
    "_successor_stamp",
    "TITLE_MAX_CHARS",
]