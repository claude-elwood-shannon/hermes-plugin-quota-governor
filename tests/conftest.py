"""Fixtures/paths compartidos de los tests del repositorio.

Expone el paquete de `scripts/` al import de tests sin rutas absolutas de
host (relativas a este fichero), para que pytest encuentre autoqueue y
otros módulos del repo.
"""
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
