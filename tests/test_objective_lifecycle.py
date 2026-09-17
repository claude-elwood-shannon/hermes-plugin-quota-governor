#!/usr/bin/python3.12
"""Tests for objective-lifecycle.py (Governance P2). Offline fixtures only.

Loads the kebab-named CLI script by path (importlib.util) and exercises the
lifecycle ratchet against temporary DBs/traces/logs (pytest tmp_path): no
network, no real board, no host state. The DB fixture carries ONLY the
columns the engine reads/writes (P1 schema subset) plus a minimal tasks
table for board diagnosis.
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "objective_lifecycle", _HERE / "scripts" / "objective-lifecycle.py")
assert _SPEC is not None and _SPEC.loader is not None  # repo layout is fixed
ol = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ol)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _db(tmp: Path, objectives: list[dict],
        presets: list[str] | None = None) -> Path:
    db = tmp / "kanban.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE approved_objectives ("
        " id TEXT PRIMARY KEY, name TEXT NOT NULL,"
        " budget_daily REAL NOT NULL DEFAULT 0.0,"
        " status TEXT NOT NULL DEFAULT 'active',"
        " nice INTEGER DEFAULT 0, focus_until REAL DEFAULT NULL,"
        " budget_baseline REAL DEFAULT 0, budget_adjustment_pct REAL DEFAULT 0,"
        " governance TEXT DEFAULT 'static', preset_id TEXT DEFAULT 'normal',"
        " updated_at REAL, updated_by TEXT)")
    con.execute("CREATE TABLE adjustment_presets ("
                " id TEXT PRIMARY KEY, is_active INTEGER DEFAULT 1,"
                " nice_step REAL, nice_cap_high REAL, nice_cap_low REAL,"
                " budget_step_pct REAL, cooldown_min INTEGER,"
                " consecutive_low INTEGER, consecutive_high INTEGER)")
    for p in presets or []:
        con.execute("INSERT INTO adjustment_presets (id) VALUES (?)", (p,))
    con.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT,"
                " body TEXT)")
    for o in objectives:
        con.execute(
            "INSERT INTO approved_objectives (id, name, budget_daily, status,"
            " nice, focus_until, budget_baseline, budget_adjustment_pct,"
            " governance, preset_id) VALUES"
            " (:id, :name, :budget_daily, 'active', :nice, :focus_until,"
            "  :budget_baseline, :budget_adjustment_pct, :governance,"
            "  :preset_id)", o)
    con.commit()
    con.close()
    return db


def _trace(tmp: Path, events: list[tuple]) -> Path:
    """events: (ts_epoch, cause, objective) — task-events lines only."""
    p = tmp / "trace.jsonl"
    with open(p, "w", encoding="utf-8") as fh:
        for ts, cause, obj in events:
            fh.write(json.dumps({
                "ts_epoch_utc": ts, "source": "task-events",
                "cause": cause, "objective": obj, "costUsd": None}) + "\n")
    return p


def _run(tmp: Path, dry: bool = False) -> dict:
    return ol.run(dry_run=dry, db=tmp / "kanban.db",
                  trace=tmp / "trace.jsonl",
                  log=tmp / "objective-lifecycle.jsonl",
                  alarms=tmp / "cron-alarms.jsonl")


def _rows(db: Path, oid: str) -> sqlite3.Row:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    r = con.execute("SELECT * FROM approved_objectives WHERE id = ?",
                    (oid,)).fetchone()
    con.close()
    return r


def _obj(oid: str = "OBJ-TEST", gov: str = "responsive",
         preset: str = "normal", **kw) -> dict:
    d = {"id": oid, "name": oid, "budget_daily": 1.0, "nice": 0,
         "focus_until": None, "budget_baseline": 1.0,
         "budget_adjustment_pct": 0.0, "governance": gov, "preset_id": preset}
    d.update(kw)
    return d


def _low_events(n=10, oid="OBJ-TEST"):
    """1 completed / n crashed in the last hour -> efficiency ~0.1 (LOW)."""
    now = time.time()
    ev = [(now - 60, "completed", oid)]
    ev += [(now - 60 - i, "crashed", oid) for i in range(n - 1)]
    return ev


def _high_events(oid="OBJ-TEST"):
    """All completed -> efficiency 1.0 (HIGH)."""
    now = time.time()
    return [(now - 60, "completed", oid) for _ in range(6)]


# ---------------------------------------------------------------------------
# Core ratchet: two consecutive ticks minimum, then adjustment
# ---------------------------------------------------------------------------

def test_no_data_means_no_adjustment(tmp_path):
    _db(tmp_path, [_obj(gov="responsive")])
    _trace(tmp_path, [])  # empty trace
    r = _run(tmp_path)
    e = r["entries"][0]
    assert e["efficiency"] is None
    assert e["state"] is None
    assert "SIN DATOS" in e["note"]
    assert e["actions"] == []
    assert r["updates_applied"] == 1     # one log line (the SIN DATOS entry)
    row = _rows(tmp_path / "kanban.db", "OBJ-TEST")
    assert row["nice"] == 0              # DB untouched


def test_two_low_ticks_trip_nice_adjustment(tmp_path):
    """responsive + LOW: tick1 records, tick2 trips nice 0 -> -1."""
    _db(tmp_path, [_obj(gov="responsive")])
    _trace(tmp_path, _low_events())
    r1 = _run(tmp_path)
    e1 = r1["entries"][0]
    assert e1["state"] == "low" and e1["streak_low"] == 1
    assert e1["actions"] == []          # first tick only records
    assert _rows(tmp_path / "kanban.db", "OBJ-TEST")["nice"] == 0

    r2 = _run(tmp_path)
    e2 = r2["entries"][0]
    assert e2["streak_low"] == 2
    assert e2["actions"] == [{"type": "nice", "from": 0, "to": -1,
                              "reason": e2["actions"][0]["reason"]}]
    assert _rows(tmp_path / "kanban.db", "OBJ-TEST")["nice"] == -1
    # transition alarm fired once, on the trip tick
    alarms = [json.loads(l) for l in
              open(tmp_path / "cron-alarms.jsonl", encoding="utf-8")]
    assert [a["status"] for a in alarms] == ["EFFICIENCY_LOW"]


def test_third_low_tick_in_cooldown_changes_nothing(tmp_path):
    _db(tmp_path, [_obj(gov="responsive")])
    _trace(tmp_path, _low_events())
    _run(tmp_path)
    _run(tmp_path)                       # trips + adjusts (nice=-1)
    r3 = _run(tmp_path)                  # cooldown 240min active
    e3 = r3["entries"][0]
    assert e3["cooldown_ok"] is False
    assert e3["actions"] == []
    assert e3["streak_low"] == 3         # state still carried
    assert _rows(tmp_path / "kanban.db", "OBJ-TEST")["nice"] == -1


def test_high_efficiency_demotes_and_rewards_budget(tmp_path):
    """dynamic + HIGH: nice += 1 (demote), budget +10% vs baseline."""
    _db(tmp_path, [_obj(gov="dynamic", budget_daily=1.0,
                        budget_baseline=1.0)])
    _trace(tmp_path, _high_events())
    _run(tmp_path)                       # record
    _run(tmp_path)                       # adjust
    r = _rows(tmp_path / "kanban.db", "OBJ-TEST")
    assert r["nice"] == 1
    assert r["budget_adjustment_pct"] == 10.0
    assert abs(r["budget_daily"] - 1.1) < 1e-6
    # no low alarm on a high state
    assert not (tmp_path / "cron-alarms.jsonl").exists()


def test_low_efficiency_cuts_budget_for_elastic(tmp_path):
    """elastic: budget moves, nice NEVER does."""
    _db(tmp_path, [_obj(gov="elastic", budget_daily=1.0,
                        budget_baseline=1.0)])
    _trace(tmp_path, _low_events())
    _run(tmp_path)
    _run(tmp_path)
    r = _rows(tmp_path / "kanban.db", "OBJ-TEST")
    assert r["nice"] == 0
    assert r["budget_adjustment_pct"] == -10.0
    assert abs(r["budget_daily"] - 0.9) < 1e-6


def test_budget_baseline_zero_disables_budget(tmp_path):
    _db(tmp_path, [_obj(gov="dynamic", budget_daily=1.0,
                        budget_baseline=0.0)])
    _trace(tmp_path, _high_events())
    _run(tmp_path)
    _run(tmp_path)
    r = _rows(tmp_path / "kanban.db", "OBJ-TEST")
    assert r["nice"] == 1                      # nice still works
    assert r["budget_adjustment_pct"] == 0.0   # budget disabled
    assert r["budget_daily"] == 1.0


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------

def test_conservative_needs_three_ticks(tmp_path):
    _db(tmp_path, [_obj(gov="responsive", preset="conservative")])
    _trace(tmp_path, _low_events())
    _run(tmp_path)
    _run(tmp_path)
    assert _rows(tmp_path / "kanban.db", "OBJ-TEST")["nice"] == 0  # streak 2 < 3
    _run(tmp_path)
    assert _rows(tmp_path / "kanban.db", "OBJ-TEST")["nice"] == -1  # streak 3


def test_preset_db_overrides_builtin(tmp_path):
    _db(tmp_path, [_obj(gov="responsive")], presets=["normal"])
    con = sqlite3.connect(tmp_path / "kanban.db")
    con.execute("UPDATE adjustment_presets SET nice_step = 5,"
                " nice_cap_low = -10, cooldown_min = 0 WHERE id = 'normal'")
    con.commit()
    con.close()
    _trace(tmp_path, _low_events())
    _run(tmp_path)
    _run(tmp_path)
    assert _rows(tmp_path / "kanban.db", "OBJ-TEST")["nice"] == -5


def test_unknown_preset_falls_back_to_normal(tmp_path):
    _db(tmp_path, [_obj(gov="responsive", preset="bogus")])
    _trace(tmp_path, _low_events())
    r = _run(tmp_path)
    assert r["entries"][0]["preset_id"] == "normal"
    assert "bogus" in r["entries"][0]["preset_note"]


def test_nice_cap_clamps_and_alarms(tmp_path):
    _db(tmp_path, [_obj(gov="responsive", nice=4)])  # cap_high=5, low=-5
    _trace(tmp_path, _low_events())
    _run(tmp_path)
    _run(tmp_path)   # nice 4 -> -1
    _run(tmp_path)   # cooldown -> no-op
    time.sleep(0.05)  # not enough to clear a 240min cooldown; use 3rd tick via
    # cooldown: to test clamping deterministically use an aggressive preset
    # (cooldown 120min) is still active — instead rebuild with cooldown 0.
    _db2 = tmp_path / "kanban.db"
    con = sqlite3.connect(_db2)
    con.execute("UPDATE approved_objectives SET preset_id = 'startup',"
                " nice = -9 WHERE id = 'OBJ-TEST'")  # startup: cap_low=-10
    con.commit()
    con.close()
    # clear cooldown by rewriting last_adjusted in the log entry
    log = tmp_path / "objective-lifecycle.jsonl"
    lines = [json.loads(l) for l in open(log, encoding="utf-8")]
    for e in lines:
        e["last_adjusted"] = time.time() - 3600
    log.write_text("".join(json.dumps(e) + "\n" for e in lines),
                   encoding="utf-8")
    r = _run(tmp_path)
    e = r["entries"][0]
    acts = [a for a in e["actions"] if a["type"] == "nice"]
    assert acts and acts[0]["to"] == -10          # clamped at cap
    assert acts[0]["clamped"] is True
    alarms = [json.loads(l) for l in
              open(tmp_path / "cron-alarms.jsonl", encoding="utf-8")]
    assert any(a["status"] == "NICE_CAP" for a in alarms)


# ---------------------------------------------------------------------------
# Focus reset / static / run-level filters
# ---------------------------------------------------------------------------

def test_focus_reset_expired_only(tmp_path):
    now = time.time()
    _db(tmp_path, [
        _obj("OBJ-EXP", focus_until=now - 100, nice=3),
        _obj("OBJ-FOC", focus_until=now + 3600, nice=2),
        _obj("OBJ-NULL", focus_until=None, nice=1),
    ])
    _trace(tmp_path, [])
    _run(tmp_path)
    db = tmp_path / "kanban.db"
    assert _rows(db, "OBJ-EXP")["nice"] == 0
    assert _rows(db, "OBJ-EXP")["focus_until"] is None
    assert _rows(db, "OBJ-FOC")["nice"] == 2      # still focused
    assert _rows(db, "OBJ-FOC")["focus_until"] is not None
    assert _rows(db, "OBJ-NULL")["nice"] == 1
    assert {r["id"] for r in _run(tmp_path)["resets"]} == set()


def test_static_objectives_are_never_evaluated(tmp_path):
    _db(tmp_path, [_obj("OBJ-STATIC", gov="static")])
    _trace(tmp_path, _low_events())
    r = _run(tmp_path)
    assert r["entries"] == []


def test_unknown_governance_treated_as_static(tmp_path):
    _db(tmp_path, [_obj("OBJ-WEIRD", gov="turbo")])
    _trace(tmp_path, _low_events())
    r = _run(tmp_path)
    e = r["entries"][0]
    assert e["governance"] == "turbo"
    assert e["actions"] == []
    assert "unknown governance" in e["note"]
    # nothing was written to the DB row
    r_row = _rows(tmp_path / "kanban.db", "OBJ-WEIRD")
    assert r_row["nice"] == 0 and r_row["budget_adjustment_pct"] == 0.0


# ---------------------------------------------------------------------------
# Stale prior / dry-run safety
# ---------------------------------------------------------------------------

def test_stale_prior_entry_breaks_streak(tmp_path):
    _db(tmp_path, [_obj(gov="responsive")])
    _trace(tmp_path, _low_events())
    log = tmp_path / "objective-lifecycle.jsonl"
    stale = {"ts": "2026-09-16T00:00:00Z", "kind": "objective_lifecycle",
             "objective": "OBJ-TEST", "state": "low", "streak_low": 2,
             "streak_high": 0, "last_adjusted": None,
             "ts_epoch": time.time() - 5 * 3600}
    log.write_text(json.dumps(stale) + "\n", encoding="utf-8")
    r = _run(tmp_path)
    e = r["entries"][0]
    assert e["streak_low"] == 1            # restarted, did not carry 2+1
    assert e["actions"] == []
    assert "streak restarted" in e["stale_note"]


def test_dry_run_writes_nothing(tmp_path):
    _db(tmp_path, [_obj(gov="responsive", nice=0)])
    _trace(tmp_path, _low_events())
    # two recorded priors so the next tick WOULD adjust
    now = time.time()
    log = tmp_path / "objective-lifecycle.jsonl"
    with open(log, "w", encoding="utf-8") as fh:
        for i in range(2):
            fh.write(json.dumps({
                "ts": ol.ts_utc(now - 600 * (2 - i)),
                "kind": "objective_lifecycle", "objective": "OBJ-TEST",
                "state": "low", "streak_low": i + 1, "streak_high": 0,
                "last_adjusted": None, "ts_epoch": now - 600 * (2 - i),
            }) + "\n")
    before = _rows(tmp_path / "kanban.db", "OBJ-TEST")
    log_size = log.stat().st_size
    _run(tmp_path, dry=True)
    after = _rows(tmp_path / "kanban.db", "OBJ-TEST")
    assert dict(before) == dict(after)
    assert log.stat().st_size == log_size          # log untouched
    assert not (tmp_path / "cron-alarms.jsonl").exists()


def test_missing_db_fails_clean(tmp_path):
    r = ol.run(dry_run=False, db=tmp_path / "nope.db",
                trace=tmp_path / "trace.jsonl",
                log=tmp_path / "objective-lifecycle.jsonl",
                alarms=tmp_path / "cron-alarms.jsonl")
    assert r["errors"] and "not found" in r["errors"][0]
