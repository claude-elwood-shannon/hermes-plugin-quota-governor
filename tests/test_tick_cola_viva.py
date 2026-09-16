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
import importlib.util
import os
import shutil
import sys
import tempfile
import unittest

# ══ Merged from the root lane test_tick_cola_viva.py (t_3fc3e669) ══
# The root copy lived in the repo root; from tests/ the module under test
# is one level deeper. GOV_DIR/SCRIPT are re-derived here for the merged
# home; the frozen-contract suite loads the real hyphen-named module
# through importlib exactly as before.
GOV_DIR = str(Path(__file__).resolve().parent.parent)
SCRIPT = os.path.join(GOV_DIR, "scripts", "tick-cola-viva.py")

_spec = importlib.util.spec_from_file_location("tick_cola_viva", SCRIPT)
cv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cv)

NOW = 1789001175.0  # fixed epoch: deterministic, never wall-clock
CLASE_C_DONE = (
    "objective:OBJ-35 | cost:small | privacy:low | clase:C\n"
    "Sucesor estructural: backfill de la tabla de entrenamiento."
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

def _mk_db(path, tasks, done=None):
    """Create a minimal tasks table. tasks = list of dicts (id,title,status,
    assignee,body). done is a convenience list merged into tasks as done."""
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE tasks (id TEXT, title TEXT, status TEXT, "
                "assignee TEXT, body TEXT, completed_at REAL, created_at REAL)")
    all_tasks = list(tasks)
    for d in (done or []):
        all_tasks.append({**d, "status": "done"})
    for t in all_tasks:
        con.execute(
            "INSERT INTO tasks (id, title, status, assignee, body, completed_at, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (t["id"], t["title"], t["status"], t.get("assignee"),
             t.get("body"), t.get("completed_at"), t.get("created_at", 0)))
    con.commit()
    con.close()


def _ready(id="t_ready", assignee=None, title="OBJ-27: tarea lista",
           body="objective:OBJ-27"):
    return {"id": id, "title": title, "status": "ready",
            "assignee": assignee, "body": body}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="colaviva-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(os.environ.pop, "HERMES_HOME", None)
        self.addCleanup(os.environ.pop, "HERMES_KANBAN_DB", None)
        # Repo-root env leaks (e.g. BRIDGE_PLUGIN_REPO set by sibling test
        # files under pytest) make the objective-lifecycle checkers see the
        # HOST repo/logs -> OBJ-VLLM 'achieved' fires inside fixtures. And
        # approved_objectives hermes_root() falls back to the real
        # Path.home()/.hermes when AO_HERMES_ROOT is merely ABSENT — the
        # fixture must PIN it to the tmp world, not just pop strays.
        for var in ("BRIDGE_PLUGIN_REPO", "AO_HERMES_ROOT", "AO_KANBAN_DB",
                    "AO_TRACE"):
            self.addCleanup(os.environ.pop, var, None)
            os.environ.pop(var, None)
        os.environ["AO_HERMES_ROOT"] = self.tmp
        os.environ.pop("HERMES_KANBAN_DB", None)
        os.environ["HERMES_HOME"] = self.tmp
        self.db = str(Path(self.tmp) / "kanban.db")
        # Record mutations instead of shelling out.
        self.calls = {"create": [], "assign": []}
        cv.create_task = self._fake_create
        cv.assign_task = self._fake_assign

    def _fake_create(self, title, body, assignee):
        self.calls["create"].append((title, body, assignee))
        return f"t_new_{len(self.calls['create'])}"

    def _fake_assign(self, task_id, assignee):
        self.calls["assign"].append((task_id, assignee))
        return True

    def home(self):
        return self.tmp

    def _ledger(self):
        p = cv.ledger_path(self.home())
        try:
            return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()
                    if l.strip()]
        except OSError:
            return []

    def _run(self, **kw):
        kw.setdefault("hermes_home", self.home())
        kw.setdefault("execute", True)
        kw.setdefault("now", NOW)
        kw.setdefault("session_pct", 2.6)
        kw.setdefault("weekly_pct", 54.7)
        kw.setdefault("live_workers", 0)
        return cv.run(**kw)


class TestStopSignal(Base):
    def test_stop_signal_skips(self):
        _mk_db(self.db, [_ready(assignee=None)])
        cv.stop_file_path(self.home()).parent.mkdir(parents=True, exist_ok=True)
        cv.stop_file_path(self.home()).write_text("spending limit")
        out = self._run()
        self.assertEqual(len(out), 1)
        self.assertIn("STOP", out[0])
        self.assertEqual(self.calls["assign"], [])
        self.assertEqual(self.calls["create"], [])


class TestQuotaGates(Base):
    def test_live_workers_skips(self):
        """P2: live_workers=1 con ready sin assignee -> backlog bajo (1<3),
        la cascade ACTÚA asignando (ya no skip por tener workers en vuelo)."""
        _mk_db(self.db, [_ready(assignee=None)])
        out = self._run(live_workers=1)
        self.assertEqual(len(out), 1)
        self.assertIn("asignado t_ready", out[0])
        self.assertEqual(self.calls["assign"], [("t_ready", "pr-ollama")])

    def test_backlog_ok_skips(self):
        """P2: backlog_total >= 3 (1 running + 2 ready asignadas) -> skip."""
        _mk_db(self.db, [_ready(assignee="pr-ollama"),
                         _ready(assignee="pr-ollama")])
        out = self._run(live_workers=1)
        self.assertEqual(len(out), 1)
        self.assertIn("backlog OK", out[0])
        self.assertEqual(self.calls["assign"], [])
        self.assertEqual(self.calls["create"], [])

    def test_session_high_skips(self):
        _mk_db(self.db, [_ready(assignee=None)])
        out = self._run(session_pct=85.0)
        self.assertEqual(len(out), 1)
        self.assertIn("sesion al 85.0%", out[0])
        self.assertEqual(self.calls["assign"], [])

    def test_weekly_high_skips(self):
        _mk_db(self.db, [_ready(assignee=None)])
        out = self._run(weekly_pct=90.0)
        self.assertEqual(len(out), 1)
        self.assertIn("weekly al 90.0%", out[0])
        self.assertEqual(self.calls["assign"], [])


class TestCascade(Base):
    def test_step1_assigns_ready_without_assignee(self):
        _mk_db(self.db, [_ready(assignee=None)])
        out = self._run()
        self.assertEqual(len(out), 1)
        self.assertIn("asignado t_ready", out[0])
        self.assertEqual(self.calls["assign"], [("t_ready", "pr-ollama")])
        self.assertEqual(self.calls["create"], [])

    def test_step2_queue_alive_when_ready_assigned(self):
        _mk_db(self.db, [_ready(assignee="pr-ollama")])
        out = self._run()
        self.assertEqual(len(out), 1)
        self.assertIn("ready con assignee", out[0])
        self.assertEqual(self.calls["assign"], [])
        self.assertEqual(self.calls["create"], [])

    def test_step3_structural_successor(self):
        _mk_db(self.db, [],
               done=[{"id": "t_done", "title": "OBJ-35 backfill predictor",
                      "assignee": "pr-ollama", "body": CLASE_C_DONE,
                      "completed_at": NOW - 3600}])
        out = self._run()
        # P2: stock de UN padre -> 1 sucesor y pool seco -> sequia legitima
        self.assertEqual(len(out), 2)
        self.assertIn("sucesor estructural de t_done", out[0])
        self.assertIn("cola seca legitima", out[1])
        self.assertEqual(len(self.calls["create"]), 1)
        title, body, assignee = self.calls["create"][0]
        self.assertIn("t_done", title)
        self.assertIn("clase:C", body)
        self.assertEqual(assignee, "pr-ollama")

    def test_step3_skips_parent_with_open_successor(self):
        # t_done already has an open successor referencing it -> skipped.
        _mk_db(self.db, [
            {"id": "t_succ", "title": "Sucesor estructural de t_done",
             "status": "ready", "assignee": "pr-ollama",
             "body": "Sucesor estructural de t_done (ya abierto)"}],
            done=[{"id": "t_done", "title": "OBJ-35 backfill predictor",
                   "assignee": "pr-ollama", "body": CLASE_C_DONE,
                   "completed_at": NOW - 3600}])
        out = self._run()
        # t_succ is ready WITH assignee -> step2 queue-alive fires first.
        self.assertEqual(len(out), 1)
        self.assertIn("ready con assignee", out[0])
        self.assertEqual(self.calls["create"], [])

    def test_step3_skips_parent_with_open_successor_no_ready(self):
        # t_done has an open successor in 'todo' (not ready) -> step3 must
        # skip it (idempotency) and fall to step4.
        _mk_db(self.db, [
            {"id": "t_succ", "title": "Sucesor estructural de t_done",
             "status": "todo", "assignee": "pr-ollama",
             "body": "Sucesor estructural de t_done (ya abierto)"}],
            done=[{"id": "t_done", "title": "OBJ-35 backfill predictor",
                   "assignee": "pr-ollama", "body": CLASE_C_DONE,
                   "completed_at": NOW - 3600}])
        out = self._run()
        self.assertEqual(len(out), 1)
        self.assertIn("cola seca legitima", out[0])
        self.assertEqual(self.calls["create"], [])

    def test_step4_stop_when_nothing_legitimate(self):
        _mk_db(self.db, [])
        out = self._run()
        self.assertEqual(len(out), 1)
        self.assertIn("cola seca legitima", out[0])
        self.assertEqual(self.calls["create"], [])
        self.assertEqual(self.calls["assign"], [])

    def test_max_one_action_per_tick(self):
        # ready-without-assignee AND a done task: only step1 fires.
        _mk_db(self.db, [_ready(assignee=None)],
               done=[{"id": "t_done", "title": "OBJ-35 backfill predictor",
                      "assignee": "pr-ollama", "body": CLASE_C_DONE,
                      "completed_at": NOW - 3600}])
        out = self._run()
        self.assertEqual(len(out), 1)
        self.assertEqual(self.calls["assign"], [("t_ready", "pr-ollama")])
        self.assertEqual(self.calls["create"], [])


class TestP2Desired3(Base):
    """P2 desired=3 (MEDIATOR t_acf726e6): el backlog-guard rellena HASTA el
    minimo (ready_assigned + running >= BACKLOG_MIN), limitado al pool
    legitimo; el stock P5 (estampado/legacy/stock-muerto) nunca rellena y
    la sequia sigue siendo sequia (regla de oro: no filler)."""

    def _root(self, tid="t_done", **kw):
        d = {"id": tid, "title": "OBJ-35 backfill predictor",
             "assignee": "pr-ollama", "body": CLASE_C_DONE,
             "completed_at": NOW - 3600}
        d.update(kw)
        return d

    def test_1_refillable_true_with_untouched_root(self):
        _mk_db(self.db, [], done=[self._root()])
        self.assertTrue(cv.refillable(Path(self.db), now=NOW))

    def test_2_stamped_done_root_not_refillable(self):
        sig = cv.successor_signature("t_done", "test")
        root = self._root(
            body=CLASE_C_DONE + f"\nsuccessor-sig:{sig} | successor-depth:1")
        _mk_db(self.db, [], done=[root])
        self.assertFalse(cv.refillable(Path(self.db), now=NOW))

    def test_3_legacy_done_root_not_refillable(self):
        root = self._root(
            title="Sucesor estructural de t_origen: docs de OBJ-35 backfill")
        _mk_db(self.db, [], done=[root])
        self.assertFalse(cv.refillable(Path(self.db), now=NOW))

    def test_4_dead_stock_stamped_out_and_not_refillable(self):
        sig = cv.successor_signature("t_done", "test")
        # hijo DONE (stock muerto: lleva la firma de la cadena) y MAS VIEJO
        # que la raiz: el censo alcanza la raiz primero, la sella, y el
        # hijo estampado queda como veredicto capped.
        child = {"id": "t_hijo",
                 "title": "Sucesor estructural de t_done: test de x",
                 "assignee": "pr-ollama",
                 "body": CLASE_C_DONE + f"\nsuccessor-sig:{sig} | "
                                        f"successor-depth:1",
                 "completed_at": NOW - 7200}
        _mk_db(self.db, [], done=[child, self._root()])
        self.assertFalse(cv.refillable(Path(self.db), now=NOW))
        con = sqlite3.connect(self.db)
        try:
            stamp = con.execute(
                "SELECT body FROM tasks WHERE id='t_done'").fetchone()[0]
        finally:
            con.close()
        self.assertIn("P2xP5-no-refill", stamp)

    def test_5_live_chain_via_id_not_refillable(self):
        open_succ = {"id": "t_open", "title": "x", "status": "ready",
                     "assignee": "pr-ollama",
                     "body": "Sucesor estructural de t_done"}
        _mk_db(self.db, [open_succ], done=[self._root()])
        self.assertFalse(cv.refillable(Path(self.db)))

    def test_6_low_guard_sin_pool_se_dry_verdict(self):
        _mk_db(self.db, [])
        out = self._run()
        self.assertEqual(len(out), 1)
        self.assertIn("cola seca legitima", out[0])
        self.assertEqual(self.calls["create"], [])

    def test_7_executes_to_the_minimum_pool_bounded(self):
        _mk_db(self.db, [], done=[self._root(f"t_done{i}")
                                  for i in range(3)])
        out = self._run()
        # rellena exactamente hasta el minimo: 3 sucesores, sin sequia
        # (el ciclo se cierra por backlog_total, no por pool seco)
        self.assertEqual(len(out), 3)
        self.assertEqual(len(self.calls["create"]), 3)
        self.assertEqual(
            sum(1 for t, _, _ in self.calls["create"] if "t_done" in t), 3)
        self.assertNotIn("cola seca legitima", "\n".join(out))

    def test_8_backlog_ok_without_pool_stays_silent(self):
        _mk_db(self.db, [_ready(assignee="pr-ollama"),
                         _ready(assignee="pr-ollama")])
        out = self._run(live_workers=2)
        # backlog_total = 2 live + 2 ready = 4 >= 3 -> guard skip, silencio
        self.assertEqual(out, ["backlog OK (ready=2, running=2)"])
        self.assertEqual(self.calls["create"], [])

    def test_8_low_backlog_dry_pool_alarms_in_step2(self):
        _mk_db(self.db, [_ready(assignee="pr-ollama"),
                         _ready(assignee="pr-ollama")])
        out = self._run(live_workers=0)  # backlog_total = 2 < 3, pool seco
        self.assertEqual(out, ["cola viva: 2 ready con assignee (dispatcher "
                               "los reclama) — backlog 2 < 3, pool seco "
                               "(alarma, sin filler)"])
        self.assertEqual(self.calls["create"], [])

    def test_8b_low_backlog_with_pool_alarms_after_step1(self):
        # LOW (running=0) + stock con 1 raiz limpia: refill 1 y pool seco
        _mk_db(self.db, [], done=[self._root()])
        out = self._run()
        self.assertEqual(len(out), 2)
        self.assertIn("sucesor estructural de t_done", out[0])
        self.assertIn("cola seca legitima", out[1])

    def test_8c_dry_pool_low_backlog_alarms_in_queue_alive(self):
        # LOW (1 ready asignada) + pool seco -> alarma visible, sin filler
        _mk_db(self.db, [{"id": "t_a", "title": "x", "status": "ready",
                          "assignee": "pr-ollama",
                          "body": "sin tag de objetivo"}])
        out = self._run()
        self.assertEqual(len(out), 1)
        self.assertIn("ready con assignee", out[0])
        self.assertIn("backlog 1 < 3, pool seco (alarma, sin filler)", out[0])

    def test_9_quota_gate_outranks_refill(self):
        _mk_db(self.db, [_ready(assignee=None, body="sin tag de objetivo")],
               done=[self._root("t_done0"), self._root("t_done1")])
        out = self._run(weekly_pct=85.0)
        self.assertEqual(len(out), 1)
        self.assertIn("weekly al 85.0%", out[0])
        self.assertEqual(self.calls["create"], [])
        self.assertEqual(self.calls["assign"], [])

    def test_10_p5_dedup_stops_the_tick(self):
        sig = cv.successor_signature("t_done", "test")
        child = {"id": "t_hijo",
                 "title": "Sucesor estructural de t_done: test de x",
                 "assignee": "pr-ollama",
                 "body": CLASE_C_DONE + f"\nsuccessor-sig:{sig} | "
                                        f"successor-depth:1",
                 "status": "running"}
        _mk_db(self.db, [child], done=[self._root()])
        out = self._run()
        # P5: el veredicto dedup CORTA el tick (return) — sin linea extra
        self.assertEqual(len(out), 1)
        self.assertIn("dedup P5", out[0])
        self.assertEqual(self.calls["create"], [])

    def test_11_assignee_inherited_from_parent(self):
        _mk_db(self.db, [], done=[self._root(assignee="pr-nanogpt")])
        self._run()
        self.assertEqual(self.calls["create"][0][2], "pr-nanogpt")


class TestDryRun(Base):
    def test_dry_run_prints_but_does_not_mutate(self):
        _mk_db(self.db, [_ready(assignee=None)])
        out = self._run(execute=False)
        self.assertEqual(len(out), 1)
        self.assertIn("DRY:", out[0])
        self.assertEqual(self.calls["assign"], [])
        self.assertEqual(self.calls["create"], [])


class TestObjectiveBalanceRoute(Base):
    """MEDIATOR 14-sep (t_7626791f): ruta A — objetivos aprobados se
    despachan CONTRA BALANCE aunque la cuota gratis esté agotada.

    Seeds are trace-level (the tick's housekeeping recomputes spent_today
    from the trace; a direct table seed would be wiped before the fork)."""

    def _seed_objectives(self, spent=0.0, status="active", budget=3.0):
        ao_spec = importlib.util.spec_from_file_location(
            "approved_objectives_fx",
            Path(__file__).resolve().parent.parent / "scripts" / "approved_objectives.py")
        ao = importlib.util.module_from_spec(ao_spec)
        ao_spec.loader.exec_module(ao)
        ao.ensure_table(self.db)
        con = sqlite3.connect(self.db)
        con.execute("UPDATE approved_objectives SET status=?, budget_daily=? "
                    "WHERE id='OBJ-AUTODEV'", (status, budget))
        con.commit(); con.close()
        if spent > 0:
            day0 = ao.day_start_epoch_cest(ao.cest_day_of(NOW))
            obs = Path(self.tmp) / "profiles/pr-ollama/quota-governor/obs"
            obs.mkdir(parents=True, exist_ok=True)
            with open(obs / "trace.jsonl", "w") as fh:
                fh.write(json.dumps({"ts_epoch_utc": day0 + 3600,
                                     "objective": "OBJ-AUTODEV",
                                     "costUsd": spent}) + "\n")

    def test_1_tagged_task_dispatches_against_balance_at_85pct(self):
        self._seed_objectives()
        _mk_db(self.db, [_ready(body="objective:OBJ-AUTODEV | cost:small")])
        out = self._run(weekly_pct=85.0)
        self.assertEqual(len(out), 1)
        self.assertIn("asignado t_ready", out[0])
        self.assertIn("ruta A: OBJ-AUTODEV", out[0])
        self.assertIn("85.0%", out[0])
        self.assertEqual(self.calls["assign"], [("t_ready", "pr-ollama")])

    def test_2_exhausted_budget_retains(self):
        self._seed_objectives(spent=3.0)
        _mk_db(self.db, [_ready(body="objective:OBJ-AUTODEV")])
        out = self._run(weekly_pct=85.0)
        self.assertTrue(any("retain: t_ready" in l for l in out), out)
        self.assertTrue(any("budget exhausted" in l for l in out), out)
        self.assertEqual(self.calls["assign"], [])

    def test_3_paused_objective_retains(self):
        self._seed_objectives(spent=3.0, status="paused")
        _mk_db(self.db, [_ready(body="objective:OBJ-AUTODEV")])
        out = self._run(weekly_pct=85.0)
        self.assertTrue(any("retain: t_ready" in l for l in out), out)
        self.assertTrue(any("not active" in l for l in out), out)
        self.assertEqual(self.calls["assign"], [])

    def test_4_unknown_objective_is_route_b(self):
        self._seed_objectives()
        _mk_db(self.db, [_ready(body="objective:OBJ-GHOST")])
        out = self._run(weekly_pct=85.0)
        self.assertEqual(len(out), 1)
        self.assertIn("weekly al 85.0%", out[0])
        self.assertEqual(self.calls["assign"], [])
        # con cuota libre el desconocido se despacha (Ruta B, sin tag)
        out2 = self._run(weekly_pct=50.0)
        self.assertIn("asignado t_ready", out2[0])
        self.assertEqual(self.calls["assign"], [("t_ready", "pr-ollama")])

    def test_5_untagged_task_at_85pct_not_dispatched(self):
        self._seed_objectives()
        _mk_db(self.db, [_ready(body="sin tag de objetivo")])
        out = self._run(weekly_pct=85.0)
        self.assertEqual(len(out), 1)
        self.assertIn("weekly al 85.0%", out[0])
        self.assertEqual(self.calls["assign"], [])

    def test_6_untagged_task_at_50pct_dispatched_with_free_quota(self):
        self._seed_objectives()
        _mk_db(self.db, [_ready(body="sin tag de objetivo")])
        out = self._run(weekly_pct=50.0)
        self.assertEqual(len(out), 1)
        self.assertIn("asignado t_ready", out[0])
        self.assertNotIn("ruta A", out[0])
        self.assertEqual(self.calls["assign"], [("t_ready", "pr-ollama")])

    def test_7_direccion_stop_blocks_balance_route_too(self):
        self._seed_objectives()
        _mk_db(self.db, [_ready(body="objective:OBJ-AUTODEV")])
        stop = cv.stop_file_path(self.home())
        stop.parent.mkdir(parents=True, exist_ok=True)
        stop.write_text("DIRECCION-STOP test fixture")
        out = self._run(weekly_pct=85.0)
        self.assertEqual(len(out), 1)
        self.assertIn("STOP", out[0])
        self.assertEqual(self.calls["assign"], [])

    def test_8_table_inaccessible_fail_safe_to_route_b(self):
        # 'missing' is not a reachable state (the tick's housekeeping
        # auto-creates the table on every pass) — the fail-safe the mandate
        # names is the INACCESSIBLE inventory (loader returns None):
        # tagged task must then fall to Route B (no dispatch at 85%).
        self._seed_objectives()
        _mk_db(self.db, [_ready(body="objective:OBJ-AUTODEV")])
        saved = cv._load_approved_objectives
        cv._load_approved_objectives = lambda: None
        try:
            out = self._run(weekly_pct=85.0)
        finally:
            cv._load_approved_objectives = saved
        self.assertTrue(any("weekly al 85.0%" in l for l in out), out)
        self.assertEqual(self.calls["assign"], [])

    def test_10_spend_line_after_balance_dispatch_trace(self):
        self._seed_objectives(spent=1.20)
        _mk_db(self.db, [])
        out = self._run(weekly_pct=85.0)
        self.assertTrue(any("gasto hoy OBJ-AUTODEV $1.20" in l for l in out),
                        out)


class TestPortability(unittest.TestCase):
    def test_no_absolute_host_paths_in_module(self):
        src = Path(SCRIPT).read_text(encoding="utf-8")
        for needle in ("/home/", "/data", Path.home().name):
            self.assertNotIn(needle, src,
                             f"host path leaked into tick-cola-viva.py: {needle}")


