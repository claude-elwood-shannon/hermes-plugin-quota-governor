import os
import sqlite3
import time
import subprocess
import sys
import pathlib

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
SCRIPT_PATH = BASE_DIR / "scripts" / "abandon-superseded.py"

def test_dry_run(tmp_path):
    db_path = tmp_path / "kanban.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT,
            status TEXT,
            body TEXT,
            created_at REAL,
            completed_at REAL
        );
        """
    )
    now = time.time()
    conn.execute(
        "INSERT INTO tasks VALUES (?,?,?,?,?,?)",
        ("t_arch", "Test Archival", "archived", "objective:OBJ-01\n\n", now - 100, None),
    )
    conn.execute(
        "INSERT INTO tasks VALUES (?,?,?,?,?,?)",
        ("t_sup", "Supersedes", "done", "objective:OBJ-01\n\n", now, None),
    )
    conn.commit()
    conn.close()

    cmd = [sys.executable, str(SCRIPT_PATH), "--db", str(db_path)]
    output = subprocess.check_output(cmd, text=True)
    assert "DRY-RUN: stamp t_arch" in output, "Dry run did not stamp the lost task"
    assert "abandoned:" in output, "Stamp line not present in dry run"
    # Ensure script returns zero exit code
    subprocess.run(cmd, check=True)
