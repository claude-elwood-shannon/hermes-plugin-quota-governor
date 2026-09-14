"""Tests de integracion para el mecanismo cola viva (OBJ-44, t_a8a4418c).

Historia: este fichero no colectaba desde que entro a main (7801574/322fdec)
— importaba `scripts.tick_cola_viva` cuando el modulo real es
`scripts/tick-cola-viva.py` (guiones, invocado por RUTA desde
quota-governor-tick.sh) y usaba una API `run(db_path=...)` y mensajes
("1 worker(s) en vuelo") que ya no existen. El WIP se reconcilia AQUI con la
API REAL del modulo; el contrato congelado vive en la suite raiz
`test_tick_cola_viva.py` (27 tests, unittest) y NO se duplica.

Puente de import: `scripts/tick_cola_viva.py` es un shim importlib que
reexporta el modulo con guiones (de paso, estos tests validan el shim).

Hermético: todo el mundo del fixture vive en tmp_path — HERMES_HOME,
HERMES_KANBAN_DB y AO_HERMES_ROOT/AO_KANBAN_DB/AO_TRACE (approved_objectives,
lección del test raiz Base.setUp: el fixture debe PINAR el mundo, no solo
limpiar strays). Sin red, sin CLI real (execute=False), sin kanban.db real.

Run:  python -m pytest tests/test_tick_cola_viva.py -q
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

# El modulo bajo prueba, via el shim (la linea que estuvo rota).
from scripts.tick_cola_viva import (  # noqa: E402
    build_successor,
    run,
    successor_pattern,
    TITLE_MAX_CHARS,
)

# ---------------------------------------------------------------- fixtures


def create_db(path: Path):
    """Base de datos kanban minima (solo la tabla tasks)."""
    con = sqlite3.connect(str(path))
    con.execute(
        """CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT,
            status TEXT,
            assignee TEXT,
            completed_at REAL,
            body TEXT
        )"""
    )
    con.commit()
    return con


def insert_task(con, id, title, status, assignee=None, completed_at=None,
                body=None):
    con.execute(
        "INSERT INTO tasks (id,title,status,assignee,completed_at,body) "
        "VALUES (?,?,?,?,?,?);",
        (id, title, status, assignee, completed_at, body),
    )
    con.commit()


PINNED_ENV = ("HERMES_HOME", "HERMES_KANBAN_DB", "AO_HERMES_ROOT",
              "AO_KANBAN_DB", "AO_TRACE")


@pytest.fixture(autouse=True)
def hermetic_world(tmp_path, monkeypatch):
    """Pin HERMES_HOME/kanban.db/approved_objectives al tmp del test."""
    home = tmp_path / "hermes"
    home.mkdir()
    for var in PINNED_ENV:
        monkeypatch.setenv(var, str(tmp_path / "hermes"))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "hermes" / "kanban.db"))
    yield tmp_path / "hermes"


# ---------------------------------------------------------------- tests


def test_stop_file_skips(tmp_path, hermetic_world):
    (hermetic_world / "quota-governor").mkdir(parents=True, exist_ok=True)
    (hermetic_world / "quota-governor" / "STOP").write_text("\n")
    db = create_db(hermetic_world / "kanban.db")
    db.close()
    decisions = run(hermes_home=hermetic_world, execute=False)
    assert any("STOP signal activo" in d for d in decisions)


def test_backlog_ok_skips(tmp_path, hermetic_world):
    """P2 backlog-guard: 1 running + 2 ready con assignee = 3 >= 3 -> skip."""
    db = create_db(hermetic_world / "kanban.db")
    insert_task(db, "tA", "Test uno", "ready", assignee="pr-ollama")
    insert_task(db, "tB", "Test dos", "ready", assignee="pr-ollama")
    db.close()
    decisions = run(hermes_home=hermetic_world, execute=False, live_workers=1)
    assert any("backlog OK" in d for d in decisions)


def test_free_quota_gate_session(tmp_path, hermetic_world):
    db = create_db(hermetic_world / "kanban.db")
    db.close()
    decisions = run(hermes_home=hermetic_world, execute=False,
                    session_pct=85.0)
    assert any("cola seca: sesion al 85.0%" in d for d in decisions)


def test_free_quota_gate_weekly(tmp_path, hermetic_world):
    db = create_db(hermetic_world / "kanban.db")
    db.close()
    decisions = run(hermes_home=hermetic_world, execute=False,
                    weekly_pct=90.0)
    assert any("cola seca: weekly al 90.0%" in d for d in decisions)


def test_assign_ready_without_assignee(tmp_path, hermetic_world):
    db = create_db(hermetic_world / "kanban.db")
    insert_task(db, "tA", "Test algo", "ready")
    db.close()
    decisions = run(hermes_home=hermetic_world, execute=False)
    assert any("cola viva: asignado tA" in d for d in decisions)


def test_queue_alive(tmp_path, hermetic_world):
    """Reconciliado: el mensaje actual de step 2 es 'N ready con assignee'
    (el antiguo 'N worker(s) en vuelo' murio con el gate P2 backlog)."""
    db = create_db(hermetic_world / "kanban.db")
    insert_task(db, "tA", "Test algo", "ready", assignee="pr-ollama")
    db.close()
    decisions = run(hermes_home=hermetic_world, execute=False, live_workers=0)
    assert any("1 ready con assignee" in d for d in decisions)


def test_create_successor(tmp_path, hermetic_world):
    db = create_db(hermetic_world / "kanban.db")
    now = 2000000000.0
    insert_task(db, "tX", "Caso de prueba: clase:C", "done",
                completed_at=now - 3600, body="objective:OBJ-30\nclase:C\n")
    db.close()
    decisions = run(hermes_home=hermetic_world, execute=False, now=now)
    assert any("cola viva: sucesor estructural de tX" in d for d in decisions)


def test_dry_run_no_mutation(tmp_path, hermetic_world):
    """execute=False: max 1 decision, prefijo 'DRY:', y sin mutaciones —
    ni body cambiado en el board ni salida por stdout."""
    db = create_db(hermetic_world / "kanban.db")
    insert_task(db, "tA", "Test algo", "ready")
    db.close()
    decisions = run(hermes_home=hermetic_world, execute=False)
    assert len(decisions) == 1
    assert decisions[0].startswith("DRY:")
    con = sqlite3.connect(str(hermetic_world / "kanban.db"))
    assignee = con.execute(
        "SELECT assignee FROM tasks WHERE id='tA'").fetchone()[0]
    con.close()
    assert assignee is None, "dry-run no debe asignar"


def test_ledger_written_to_hermes_home(tmp_path, hermetic_world):
    """El ledger cola-viva.jsonl aterriza bajo el HERMES_HOME del fixture
    (no en el host)."""
    db = create_db(hermetic_world / "kanban.db")
    db.close()
    run(hermes_home=hermetic_world, execute=False, session_pct=85.0)
    ledger = hermetic_world / "quota-governor" / "cola-viva.jsonl"
    assert ledger.exists()
    entries = [json.loads(l) for l in
               ledger.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert entries and all("action" in e for e in entries)


def test_successor_pattern():
    assert successor_pattern("Log docs production", "") == "docs"
    assert successor_pattern("Test coverage legacy", "") == "test"
    assert successor_pattern("Hardening guardrail", "") == "hardening"
    assert successor_pattern("Unknown project", "") == "test/hardening"


def test_build_successor_caps_title_and_carries_tags():
    title, body = build_successor({
        "id": "tX", "title": "x" * 300, "body": "clase:C"})
    assert len(title) <= TITLE_MAX_CHARS
    assert "tX" in title
    assert "clase:C" in body
