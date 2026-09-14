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
    successor_signature,
    successor_chain_open,
    _successor_stamp,
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


# ---------------------------------------------- P5 dedup (MEDIATOR 14-sep):
# la cadena recursiva "Sucesor estructural de Sucesor estructural de ..."
# muere con la firma (raiz+patron) y el tope de profundidad.


def test_p5_signature_deterministic_per_root_and_pattern():
    a = successor_signature("tABC123", "docs")
    b = successor_signature("tABC123", "docs")
    c = successor_signature("tXYZ789", "docs")
    d = successor_signature("tABC123", "test")
    assert a == b and len(a) == 16
    assert a != c and a != d  # cambia la raiz o el patron -> cambia la firma


def test_p5_stamp_in_body_and_inheritance():
    root = {"id": "tR", "title": "Tarea raiz", "body": "clase:C"}
    sig = successor_signature("tR", "docs")
    title, body = build_successor(root, sig=sig, depth=1)
    assert f"successor-sig:{sig}" in body
    assert "successor-depth:1" in body
    # hijo de un sucesor estampado: hereda firma, sube profundidad
    child_parent = {"id": "tS1", "title": title, "body": body}
    depth, inherited = _successor_stamp(child_parent, "")
    assert inherited and depth == 2
    # padre legacy (pre-estampa, titulo Sucesor...): depth 1, sin herencia
    legacy = {"id": "tL", "title": "Sucesor estructural de tX: docs de Y",
              "body": "clase:C"}
    depth2, inherited2 = _successor_stamp(legacy, "")
    assert not inherited2 and depth2 == 1


def test_p5_chain_open_detects_any_non_archived_task(tmp_path):
    db = tmp_path / "kanban.db"
    con = create_db(db)
    insert_task(con, "tD", "Sucesor estructural de tR: docs de X", "done",
                completed_at=1.0,
                body=f"successor-sig:{'a' * 16} | successor-depth:1")
    con.commit()
    con.close()
    assert successor_chain_open(db, "a" * 16)
    # archivada ya no bloquea (salio del board)
    con = sqlite3.connect(str(db))
    con.execute("UPDATE tasks SET status='archived' WHERE id='tD'")
    con.commit()
    con.close()
    assert not successor_chain_open(db, "a" * 16)


def test_p5_dedup_blocks_successor_of_existing_chain(tmp_path, hermetic_world):
    """Una cadena ya estampada en el board (running) corta el step 3:
    dedup en vez de un nuevo 'Sucesor estructural de ...'."""
    now = 2000000000.0
    p_title, p_body = "Caso de prueba: clase:C", "objective:OBJ-30\nclase:C\n"
    # la firma se computa EXACTAMENTE como lo hace el tick (raiz + patron)
    sig = successor_signature("tX", successor_pattern(p_title, p_body))
    db = create_db(hermetic_world / "kanban.db")
    insert_task(db, "tX", p_title, "done",
                completed_at=now - 3600, body=p_body)
    insert_task(db, "tCHILD", "Sucesor estructural de tX: test/hardening de Caso",
                "running", body=f"successor-sig:{sig} | successor-depth:1")
    db.close()
    decisions = run(hermes_home=hermetic_world, execute=False, now=now)
    assert any("dedup P5" in d for d in decisions), decisions
    assert not any("created-successor" in d for d in decisions)


def test_p5_legacy_successor_parent_gets_depth_cap(tmp_path, hermetic_world):
    """Padre legacy 'Sucesor estructural de ...' sin estampa: su hijo
    naceria con depth 1 pero el PADRO-PADRE ya es sucesor -> el hijo queda
    a depth 1 y el siguiente de la cadena (hijo de hijo) queda capped.
    Aqui verificamos el corte del antepenultimo eslabon: un padre con
    estampa depth:1 -> hijo depth:2 > MAX -> retenido."""
    now = 2000000000.0
    sig = successor_signature("tR", "docs")
    db = create_db(hermetic_world / "kanban.db")
    insert_task(db, "tS1", "Sucesor estructural de tR: docs de R", "done",
                completed_at=now - 3600,
                body=f"objective:OBJ-30\nclase:C\nsuccessor-sig:{sig} | "
                     f"successor-depth:1")
    db.close()
    decisions = run(hermes_home=hermetic_world, execute=False, now=now)
    # la linea de decision (no la entrada del ledger) nombra el tope
    assert any("tope de profundidad" in d for d in decisions), decisions
    assert not any("sucesor estructural de tS1" in d for d in decisions)


def test_p5_first_successor_still_created(tmp_path, hermetic_world):
    """El dedup no debe matar la cola viva: un done clase:C sin sucesor
    sigue generando su primer sucesor, ahora con estampa."""
    now = 2000000000.0
    db = create_db(hermetic_world / "kanban.db")
    insert_task(db, "tX", "Caso de prueba: clase:C", "done",
                completed_at=now - 3600, body="objective:OBJ-30\nclase:C\n")
    db.close()
    decisions = run(hermes_home=hermetic_world, execute=False, now=now)
    assert any("sucesor estructural de tX" in d for d in decisions), decisions


# ------------------------------------------------------- hueco OBJ-44 (tarea
# t_5ae86c2d): paso 3.6 anti-sequia (dry-run) e incidente de sequia con >=5
# en triage (rama del commit 8f4870a). Nadie los cubria.


def _read_ledger(hermetic_world):
    p = hermetic_world / "quota-governor" / "cola-viva.jsonl"
    return [json.loads(l) for l in
            p.read_text(encoding="utf-8").splitlines() if l.strip()]


def test_step36_drought_check_dry_run(hermetic_world):
    """Paso 3.6, execute=False, tablero sin candidatos (0 ready, sin cierres
    clase:C <24h): la decision es 'paso 3.6 anti-sequia evaluado (dry-run)'
    con accion drought-check, y no se crea ni asigna nada."""
    db = create_db(hermetic_world / "kanban.db")
    db.close()
    now = 1789001175.0
    decisions = run(hermes_home=hermetic_world, execute=False, now=now,
                    live_workers=0)
    assert decisions == ["DRY: cola viva: paso 3.6 anti-sequia evaluado "
                         "(dry-run)"], decisions
    entries = _read_ledger(hermetic_world)
    assert any(e.get("action") == "drought-check" for e in entries)
    # sin mutaciones: el board sigue sin tareas y sin assignees
    con = sqlite3.connect(str(hermetic_world / "kanban.db"))
    n = con.execute("SELECT count(*) FROM tasks").fetchone()[0]
    con.close()
    assert n == 0, "dry-run del paso 3.6 no debe crear tareas"


def test_step4_triage_incident_vs_legit_drought(hermetic_world):
    """Paso 4, execute=True, tablero seco: >=5 en triage -> INCIDENTE
    (waiting-user); <5 triage -> cola seca legitima. Con execute=True los
    unicos writes del tick pasan por el CLI hermes (create/assign), que aqui
    no se invoca porque no hay ready ni clase:C recientes: sin red real."""
    now = 1789001175.0
    db = create_db(hermetic_world / "kanban.db")
    for i in range(5):
        insert_task(db, f"tr{i}", f"Triage {i}", "triage")
    db.close()
    decisions = run(hermes_home=hermetic_world, execute=True, now=now,
                    live_workers=0)
    assert decisions == ["INCIDENTE: sequia con 5 tareas en triage — el board "
                         "espera al usuario, no esta seco"], decisions
    assert any(e.get("action") == "waiting-user" and e.get("triage") == 5
               for e in _read_ledger(hermetic_world))

    # rama contraria: <5 en triage (borramos 3 -> quedan 2)
    con = sqlite3.connect(str(hermetic_world / "kanban.db"))
    con.execute("DELETE FROM tasks WHERE id IN ('tr2','tr3','tr4')")
    con.commit()
    con.close()
    decisions = run(hermes_home=hermetic_world, execute=True, now=now,
                    live_workers=0)
    assert decisions == ["cola seca legitima: sin trabajo legitimo "
                         "(regla de oro: no filler)"], decisions
    assert any(e.get("action") == "cola-seca-legitima"
               for e in _read_ledger(hermetic_world))
