"""Tests para el comportamiento de tick-cola-viva.py (OBJ‑44)."""

import json
import sqlite3
import tempfile
from pathlib import Path
import pytest

# El módulo bajo prueba
from scripts.tick_cola_viva import (
    run,
    build_successor,
    successor_pattern,
    TITLE_MAX_CHARS,
)

# Helpers -----------------------------------------------------

def create_db(path: Path):
    """Crea una base de datos SQLite con la tabla tasks mínima."""
    con = sqlite3.connect(path)
    cur = con.cursor()
    cur.execute(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT,
            status TEXT,
            assignee TEXT,
            completed_at REAL,
            body TEXT
        );
        """
    )
    con.commit()
    return con

# ---------------------------------------------------------------------
@pytest.fixture
def hermes_home(tmp_path):
    return tmp_path

# ---------------------------------------------------------------------

def insert_task(con, id, title, status, assignee=None, completed_at=None, body=None):
    cur = con.cursor()
    cur.execute(
        "INSERT INTO tasks (id,title,status,assignee,completed_at,body) VALUES (?,?,?,?,?,?);",
        (id, title, status, assignee, completed_at, body),
    )
    con.commit()

# ---------------------------------------------------------------------

def test_stop_file_skips(tmp_path, hermes_home):
    (hermes_home / "quota-governor").mkdir(parents=True)
    (hermes_home / "quota-governor" / "STOP").write_text("\n")
    db = create_db(tmp_path / "kanban.db")
    decisions = run(hermes_home=hermes_home, execute=False, db_path=tmp_path / "kanban.db")
    assert any("STOP signal activo" in d for d in decisions)

# ---------------------------------------------------------------------

def test_idle_board_skips(tmp_path, hermes_home):
    db = create_db(tmp_path / "kanban.db")
    decisions = run(hermes_home=hermes_home, execute=False, live_workers=1, db_path=tmp_path / "kanban.db")
    assert any("cola viva: 1 worker(s) en vuelo" in d for d in decisions)

# ---------------------------------------------------------------------

def test_free_quota_gate(tmp_path, hermes_home):
    db = create_db(tmp_path / "kanban.db")
    decisions = run(hermes_home=hermes_home, execute=False, session_pct=85.0, db_path=tmp_path / "kanban.db")
    assert any("cola seca: sesion al 85.0%" in d for d in decisions)

# ---------------------------------------------------------------------

def test_assign_ready_without_assignee(tmp_path, hermes_home):
    db = create_db(tmp_path / "kanban.db")
    insert_task(db, "tA", "Test algo", "ready")
    decisions = run(hermes_home=hermes_home, execute=False, db_path=tmp_path / "kanban.db")
    assert any("cola viva: asignado tA" in d for d in decisions)

# ---------------------------------------------------------------------

def test_queue_alive(tmp_path, hermes_home):
    db = create_db(tmp_path / "kanban.db")
    insert_task(db, "tA", "Test algo", "ready", assignee="pr-ollama")
    decisions = run(hermes_home=hermes_home, execute=False, db_path=tmp_path / "kanban.db")
    assert any("cola viva: 1 ready con assignee" in d for d in decisions)

# ---------------------------------------------------------------------

def test_create_successor(tmp_path, hermes_home, monkeypatch):
    db = create_db(tmp_path / "kanban.db")
    now = 2000000000.0
    insert_task(
        db,
        "tX",
        "Caso de prueba: clase:C",
        "done",
        completed_at=now - 3600,
        body="objective:OBJ-30\nclase:C\n",
    )
    decisions = run(
        hermes_home=hermes_home,
        execute=False,
        now=now,
        db_path=tmp_path / "kanban.db",
    )
    assert any("cola viva: sucesor estructural de tX" in d for d in decisions)

# ---------------------------------------------------------------------

def test_successor_pattern():
    assert successor_pattern("Log docs production", "") == "docs"
    assert successor_pattern("Test coverage legacy", "") == "test"
    assert successor_pattern("Hardening guardrail", "") == "hardening"
    assert successor_pattern("Unknown project", "") == "test/hardening"
