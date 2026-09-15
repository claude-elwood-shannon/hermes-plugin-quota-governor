"""Tests para ttl_blocked.py — el bucle t_32a71a49 no debe repetirse.

Capas cubiertas (cada una fue una capa real del fallo del 14-sep):
  T1 PATH/binario: HERMES_BIN ausente -> veredicto FAILED, jamás REMEDIATED.
  T2 verificación R2: set-model que no persiste (rc=0 mentiroso) -> FAILED.
  T3 R2 feliz: mutación real -> override distinto en DB + REMEDIATED.
  T4 R6 re-bloqueo misma causa tras remediación ok -> TRIAGED (2 vueltas).
  T5 R6 misma R fallida >= 2 veces -> TRIAGED (sin tercera espera).
  T6 dry-run: veredictos dry-*, ninguna mutación, contador intacto.
  T7 ruta a triage honesta: 1a vuelta recurrences=1 (bloqueado) -> RETRYING;
     2a vuelta -> TRIAGE real (block_loop_detected en eventos).
  T8 human-gate: no se toca.
  T9 DIRECCION-STOP: sin mutaciones.

Nota de conciliación (14-sep, t_bf9be451): la versión inicial de esta suite
parcheaba un simbólico `tb._CLI` y esperaba triage en UNA vuelta; el módulo
real expone `_cli()` y delibera: la ruta a triage es el doble-block oficial
(recurrences 1 -> blocked, 2 -> triage con block_loop_detected), así que T4/T5
simulan DOS ticks del watchdog y la 1a vuelta espera `retrying` (veredicto
honesto), no `triaged`.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import ttl_blocked as tb  # noqa: E402

# El core (hermes_cli.kanban_db) aterriza en triage a BLOCK_RECURRENCE_LIMIT=2;
# el FakeCLI emula ese comportamiento del CLI real.
BLOCK_RECURRENCE_LIMIT = 2


# ── fixtures ────────────────────────────────────────────────────────────────
@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Board SQLite mínimo + root aislado (sin STOP)."""
    db = tmp_path / "kanban.db"
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, title TEXT,
                            body TEXT, assignee TEXT, model_override TEXT,
                            provider_override TEXT, block_kind TEXT,
                            block_recurrences INTEGER DEFAULT 0,
                            created_at INTEGER);
        CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT,
                                  task_id TEXT, kind TEXT, payload TEXT,
                                  created_at INTEGER);
        CREATE TABLE task_runs (id INTEGER PRIMARY KEY AUTOINCREMENT,
                                task_id TEXT, error TEXT, started_at INTEGER,
                                ended_at INTEGER);
        CREATE TABLE task_comments (id INTEGER PRIMARY KEY AUTOINCREMENT,
                                    task_id TEXT, author TEXT, body TEXT,
                                    created_at INTEGER);
        CREATE TABLE task_links (parent_id TEXT, child_id TEXT);
    """)
    con.commit()
    con.close()
    root = tmp_path / "hermes-home"
    (root / "quota-governor").mkdir(parents=True)
    monkeypatch.setattr(tb, "HERMES_BIN", "/nonexistent/hermes-fake")
    return {"db": db, "root": root}


class FakeCLI:
    """Reemplaza tb._cli. Modos por verbo: ok | fail | rc0-liar."""

    def __init__(self, db: Path, mode: str = "ok"):
        self.db = db
        self.mode = mode
        self.calls: list[tuple] = []

    def __call__(self, *args, timeout=60):
        self.calls.append(args)
        argv = list(args)
        verb = argv[0] if argv else ""
        if self.mode == "fail":
            return subprocess.CompletedProcess(argv, 1, "", "boom (simulado)")
        # mode == "rc0-liar": rc=0 pero NO muta nada (el pecado original).
        if self.mode == "rc0-liar":
            return subprocess.CompletedProcess(argv, 0, "ok", "")
        if verb == "set-model":
            tid = argv[1]
            model = argv[2]
            con = sqlite3.connect(self.db)
            con.execute("UPDATE tasks SET model_override=? WHERE id=?", (model, tid))
            con.commit()
            con.close()
            return subprocess.CompletedProcess(argv, 0, "Set", "")
        if verb == "comment":
            tid = argv[1]
            body = argv[2]
            con = sqlite3.connect(self.db)
            con.execute(
                "INSERT INTO task_comments (task_id, body, created_at) VALUES (?,?,0)",
                (tid, body))
            con.commit()
            con.close()
            return subprocess.CompletedProcess(argv, 0, "", "")
        if verb == "unblock":
            # forma usada por el módulo: _cli("unblock", <tid>, "--reason", <r>)
            tid = argv[1]
            con = sqlite3.connect(self.db)
            con.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
            con.commit()
            con.close()
            return subprocess.CompletedProcess(argv, 0, "Unblocked", "")
        if verb == "block":
            # hermes kanban block <id> <reason...>: sin kind cuenta recurrencia;
            # a BLOCK_RECURRENCE_LIMIT aterriza en triage (block_loop_detected).
            if "--kind" in argv:
                kind = argv[argv.index("--kind") + 1]
                rest = argv[argv.index("--kind") + 2:]
            else:
                kind = None
                rest = argv[1:]
            tid = rest[0]
            con = sqlite3.connect(self.db)
            row = con.execute(
                "SELECT block_kind, block_recurrences FROM tasks WHERE id=?",
                (tid,)).fetchone()
            prev_kind, prev_rec = row if row else (None, 0)
            rec = prev_rec + 1 if prev_kind == kind else 1
            if rec >= BLOCK_RECURRENCE_LIMIT:
                new_status = "triage"
                ev = "block_loop_detected"
            else:
                new_status = "blocked"
                ev = "blocked"
            con.execute(
                "UPDATE tasks SET status=?, block_kind=?, block_recurrences=? WHERE id=?",
                (new_status, kind, rec, tid))
            # fidelidad: el core re-bloquea con el motivo ORIGINAL preservado
            # (así fue el bucle real: cada ciclo decía "Merge encountered
            # conflicts" pese a los unblocks intermedios)
            prev = con.execute(
                "SELECT payload FROM task_events WHERE task_id=? AND kind='blocked' "
                "ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
            if prev:
                try:
                    block_reason = json.loads(prev[0]).get("reason", "r")
                except (ValueError, AttributeError):
                    block_reason = "r"
            else:
                block_reason = " ".join(rest[1:]) if len(rest) > 1 else "r"
            con.execute(
                "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?,?,?,0)",
                (tid, ev, json.dumps({"reason": block_reason})))
            con.commit()
            con.close()
            return subprocess.CompletedProcess(argv, 0, "Blocked", "")
        return subprocess.CompletedProcess(argv, 0, "", "")


def make_blocked(db: Path, tid: str = "t_test1", override: str | None = None,
                 reason: str = "Merge encountered conflicts", age_s: int = 6000):
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO tasks (id, status, title, body, model_override, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (tid, "blocked", "t", "b", override, 0))
    con.execute(
        "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?,?,?,?)",
        (tid, "blocked", json.dumps({"reason": reason}), 0))
    con.commit()
    con.close()
    return {"tid": tid, "blocked_at": 0.0, "now": float(age_s)}


def run_one(env, fake, tid, blocked_at, now, execute=True, override=None,
            body="b"):
    tb._cli = fake  # type: ignore[attr-defined]
    return tb.process_blocked(
        env["db"], tid, override if override is not None else "", "",
        body, "t", blocked_at, now, execute=execute, root=env["root"])


@pytest.fixture(autouse=True)
def _wire(monkeypatch):
    # _route_to_triage usa _cli global; FakeCLI se enchufa por test.
    monkeypatch.setattr(tb, "_cli", FakeCLI(Path("/dev/null")))
    yield


# ── T1: binario ausente -> FAILED, jamás REMEDIATED ─────────────────────────
def test_missing_binary_reports_failed_never_rem(env):
    ctx = make_blocked(env["db"], override="glm-5.2")  # glm-5.2 dead for workers
    res = run_one(env, FakeCLI(env["db"], "fail"), ctx["tid"], ctx["blocked_at"],
                  ctx["now"], override="glm-5.2")
    assert res.action == "failed"
    assert "R2" in res.detail
    # y la huella R6 quedó escrita como failed (el CLI falló, no hay comment)
    # — con CLI fail no hay comment posible; el contador vive en comments,
    # así que este test solo fija el veredicto honesto.


# ── T2: set-model rc=0 que no persiste -> FAILED ────────────────────────────
def test_rc0_liar_set_model_detected(env):
    ctx = make_blocked(env["db"], override="glm-5.2")
    res = run_one(env, FakeCLI(env["db"], "rc0-liar"), ctx["tid"], ctx["blocked_at"],
                  ctx["now"], override="glm-5.2")
    assert res.action == "failed"
    assert "override sigue igual" in res.detail or "rc=" in res.detail


# ── T3: R2 feliz ────────────────────────────────────────────────────────────
def test_r2_happy_path(env):
    ctx = make_blocked(env["db"], override="glm-5.2")
    fake = FakeCLI(env["db"], "ok")
    res = run_one(env, fake, ctx["tid"], ctx["blocked_at"], ctx["now"],
                  override="glm-5.2")
    assert res.action == "remediated"
    con = sqlite3.connect(env["db"])
    mo = con.execute("SELECT model_override FROM tasks WHERE id=?",
                     (ctx["tid"],)).fetchone()[0]
    status = con.execute("SELECT status FROM tasks WHERE id=?",
                         (ctx["tid"],)).fetchone()[0]
    con.close()
    assert mo == "gpt-oss:20b"  # canonical worker pin (Sep 15 2026, t_aa18eff8)
    assert status == "ready"
    # huella R6 con ok
    con = sqlite3.connect(env["db"])
    n = con.execute("SELECT COUNT(*) FROM task_comments WHERE body LIKE ?",
                    (f"{tb.R6_MARK} R2 attempt% ok",)).fetchone()[0]
    con.close()
    assert n == 1


# ── T4: re-bloqueo misma causa tras R2 ok -> triage (2 vueltas del watchdog) ─
def test_r6_reblock_same_cause_after_ok_routes_triage(env):
    reason = "Merge encountered conflicts"
    fp = tb.reason_fp(reason)
    ctx = make_blocked(env["db"], override="gpt-oss:20b", reason=reason)
    con = sqlite3.connect(env["db"])
    con.execute(
        "INSERT INTO task_comments (task_id, body, created_at) VALUES (?,?,0)",
        (ctx["tid"], f"{tb.R6_MARK} R2 attempt fp={fp} ok"))
    con.commit()
    con.close()
    fake = FakeCLI(env["db"], "ok")
    # 1a vuelta: veredicto honesto retrying (recurrences=1, aún blocked)
    res = run_one(env, fake, ctx["tid"], ctx["blocked_at"], ctx["now"],
                  override=None)
    assert res.action == "retrying"
    # 2a vuelta: recurrences=2 -> triage real
    res = run_one(env, fake, ctx["tid"], ctx["blocked_at"], ctx["now"],
                  override=None)
    assert res.action == "triaged"
    assert "misma causa" in res.detail
    con = sqlite3.connect(env["db"])
    status = con.execute("SELECT status FROM tasks WHERE id=?",
                         (ctx["tid"],)).fetchone()[0]
    con.close()
    assert status == "triage"


# ── T5: misma R fallida >= 2 veces -> triage ────────────────────────────────
def test_r6_two_failures_same_fp_routes_triage(env):
    reason = "Merge encountered conflicts"
    fp = tb.reason_fp(reason)
    ctx = make_blocked(env["db"], override="gpt-oss:20b", reason=reason)
    con = sqlite3.connect(env["db"])
    for _ in range(2):
        con.execute(
            "INSERT INTO task_comments (task_id, body, created_at) VALUES (?,?,0)",
            (ctx["tid"], f"{tb.R6_MARK} R2 attempt fp={fp} failed"))
    con.commit()
    con.close()
    fake = FakeCLI(env["db"], "ok")
    res = run_one(env, fake, ctx["tid"], ctx["blocked_at"], ctx["now"],
                  override="gpt-oss:20b")
    assert res.action == "retrying"  # 1a vuelta honesta
    res = run_one(env, fake, ctx["tid"], ctx["blocked_at"], ctx["now"],
                  override="gpt-oss:20b")
    assert res.action == "triaged"


# ── T6: dry-run no muta ─────────────────────────────────────────────────────
def test_dry_run_no_mutations(env):
    ctx = make_blocked(env["db"], override="gpt-oss:20b")
    fake = FakeCLI(env["db"], "ok")
    res = run_one(env, fake, ctx["tid"], ctx["blocked_at"], ctx["now"],
                  execute=False, override="gpt-oss:20b")
    assert res.action.startswith("dry-")
    con = sqlite3.connect(env["db"])
    mo = con.execute("SELECT model_override FROM tasks WHERE id=?",
                     (ctx["tid"],)).fetchone()[0]
    n = con.execute("SELECT COUNT(*) FROM task_comments").fetchone()[0]
    con.close()
    assert mo == "gpt-oss:20b"
    assert n == 0


# ── T7: ruta honesta a triage (dos vueltas) ─────────────────────────────────
def test_route_to_triage_honest_two_passes(env):
    ctx = make_blocked(env["db"])
    fake = FakeCLI(env["db"], "ok")
    tb._cli = fake  # type: ignore[attr-defined]
    done, err = tb._route_to_triage(ctx["tid"], "c", env["db"])
    assert done is False
    con = sqlite3.connect(env["db"])
    status = con.execute("SELECT status FROM tasks WHERE id=?",
                         (ctx["tid"],)).fetchone()[0]
    con.close()
    assert status == "blocked"  # recurrences=1, segunda vuelta cae
    done, err = tb._route_to_triage(ctx["tid"], "c", env["db"])
    assert done is True
    con = sqlite3.connect(env["db"])
    status = con.execute("SELECT status FROM tasks WHERE id=?",
                         (ctx["tid"],)).fetchone()[0]
    ev = con.execute(
        "SELECT kind FROM task_events WHERE task_id=? AND kind='block_loop_detected'",
        (ctx["tid"],)).fetchone()
    con.close()
    assert status == "triage"
    assert ev is not None


# ── T8: human-gate intacto ──────────────────────────────────────────────────
def test_human_gate_untouched(env):
    ctx = make_blocked(env["db"])
    gate_body = "[human-gate] espera OK del usuario"
    res = run_one(env, FakeCLI(env["db"], "ok"), ctx["tid"], ctx["blocked_at"],
                  ctx["now"], body=gate_body)
    assert res.action == "skip-human-gate"


# ── T9: DIRECCION-STOP ──────────────────────────────────────────────────────
def test_stop_signal_blocks_mutations(env, monkeypatch):
    (env["root"] / "quota-governor" / "STOP").touch()
    ctx = make_blocked(env["db"], override="gpt-oss:20b")
    results = tb.run(execute=True, now=ctx["now"], db_path=env["db"],
                     root=env["root"])
    assert all(r.action == "skipped-stop" for r in results)
    con = sqlite3.connect(env["db"])
    mo = con.execute("SELECT model_override FROM tasks WHERE id=?",
                     (ctx["tid"],)).fetchone()[0]
    con.close()
    assert mo == "gpt-oss:20b"
