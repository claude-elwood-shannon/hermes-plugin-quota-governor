"""Fixtures/paths compartidos de los tests del repositorio.

Expone al import de tests tanto el paquete `scripts/` (import estilo
`import autoqueue`) como la raiz del repo (import estilo
`from scripts.tick_cola_viva import ...`), sin rutas absolutas de host —
todo relativo a este fichero. Sin esto, esos imports solo resuelven cuando
pytest se invoca como `python -m pytest` desde la raiz (que inserta el CWD
en sys.path); con el binario `pytest` puro fallarian.

Historia: este conftest llego a main con la rama obj44-autoqueue-py2
(5e5b9cb); su merge quedo huerfano en el indice y fue abortado en
t_a8a4418c. Se restaura aqui porque el shim scripts/tick_cola_viva.py y
tests/test_tick_cola_viva.py dependen del import de paquete.
"""
import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _TESTS_DIR.parent
_SCRIPTS_DIR = _REPO_ROOT / "scripts"

for _p in (str(_REPO_ROOT), str(_SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
