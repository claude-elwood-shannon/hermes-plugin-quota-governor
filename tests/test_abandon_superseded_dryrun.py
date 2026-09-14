import os
import sqlite3
import time
import subprocess
import sys
import pathlib

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
SCRIPT_PATH = BASE_DIR / "scripts" / "abandon-superseded.py"


def _mk_db(path, tasks):
    """tasks: list of (id, title, status, body, created_at, completed_at)."""
    conn = sqlite3.connect(str(path))
    conn.execute("""CREATE TABLE tasks (
        id TEXT PRIMARY KEY, title TEXT, status TEXT, body TEXT,
        created_at REAL, completed_at REAL)""")
    for tid, title, status, body, cr, ca in tasks:
        conn.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?)",
                     (tid, title, status, body, cr, ca))
    conn.commit()
    conn.close()


def test_dry_run(tmp_path):
    # Escenario OBJ-16: la tarea perdida (archived, sin completed_at) es
    # referenciada POR id en el body de un sucesor del mismo objective,
    # creado DESPUES (guarda temporal) y terminado en done — la forma en
    # que la regla conservadora del script (relacion + guarda temporal,
    # scripts/abandon-superseded.py §Candidate rule) estampa.
    #
    # Tres defectos del fixture original (t_a8a4418c):
    #   1. sin NINGUNA relacion entre las dos tareas -> 0 matches ->
    #      stdout vacio -> fallo SIEMPRE;
    #   2. el sucesor nacia despues del escaneo de candidatos;
    #   3. los ids no eran hex: el detector de ids del script es
    #      r"\bt_[0-9a-f]{8}\b", asi que ni "t_arch" ni "t_sup" podrian
    #      actuar como referencia aunque el body los mencionara.
    # IDs hex legibles: deadbeef (perdida) / feedface (supersededor).
    now = time.time()
    db_path = tmp_path / "kanban.db"
    log_path = tmp_path / "stamps.jsonl"
    _mk_db(db_path, [
        ("t_deadbeef", "Test Archival", "archived",
         "objective:OBJ-01\ngave_up\n", now - 100, None),
        ("t_feedface", "Supersedes", "done",
         "objective:OBJ-01\nreemplaza a t_deadbeef\n", now - 50, now - 40),
    ])

    # CLI real (proceso hijo) con --db/--log SIEMPRE explicitos apuntando
    # al tmp_path: el dry-run no debe tocar el kanban.db del host ni crear
    # el stamp log (default STAMP_LOG vive bajo ~/.hermes).
    cmd = [sys.executable, str(SCRIPT_PATH), "--db", str(db_path),
           "--log", str(log_path)]
    output = subprocess.check_output(cmd, text=True)
    assert "DRY-RUN: stamp t_deadbeef" in output, \
        "Dry run did not stamp the lost task"
    assert "abandoned:" in output, "Stamp line not present in dry run"
    assert not log_path.exists(), "dry-run must not create the stamp log"
    # Ensure script returns zero exit code
    subprocess.run(cmd, check=True)
