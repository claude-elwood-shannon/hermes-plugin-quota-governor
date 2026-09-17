#!/usr/bin/python3.12
"""Tests for objective-budgets.py (OBJ-28 phase 0). Offline fixtures only.

Loads the kebab-named CLI script by path (importlib.util) and exercises
the rollup pure functions against in-memory trace row lists and a
tmp_path HERMES home: no network, no writes to the real trace, no board,
no host state.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_HERE = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "objective_budgets", _HERE / "scripts" / "obs" / "objective-budgets.py")
assert _SPEC is not None and _SPEC.loader is not None  # repo layout is fixed
ob = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ob)

_NOW = 1_789_600_000.0  # pinned wall clock: determinism, no flaky fingerprints


def _task_event(kind="completed", objective: "str | None" = "OBJ-TEST",
                tid=None):
    """One task-events trace row, optionally stamped with the objective tag."""
    r = {"source": "task-events", "cause": kind, "ts_epoch_utc": _NOW}
    if objective is not None:
        r["objective"] = objective
    if tid is not None:
        r["consumer_id"] = tid
    return r


def _cost_event(cost=0.5, objective=None, provider="nanogpt"):
    """One cost-bearing trace row (these carry no task_id today)."""
    r = {"source": "nanogpt-requests", "costUsd": cost, "provider": provider,
         "ts_epoch_utc": _NOW}
    if objective is not None:
        r["objective"] = objective
    return r


# ---------------------------------------------------------------------------
# parse_objective / parse_cost_class
# ---------------------------------------------------------------------------

def test_parse_objective_extracts_tag():
    """Bare header, metadata on the same line, inline [tags:] and absence."""
    assert ob.parse_objective("objective:OBJ-28 | cost:micro") == "OBJ-28"
    assert ob.parse_objective("objective: OBJ-7 , auto_created:true") == "OBJ-7"
    assert ob.parse_objective(
        "[tags: objective:OBJ-AUTODEV | cost:micro]") == "OBJ-AUTODEV"
    assert ob.parse_objective("no stamp in this body") == "unattributed"
    assert ob.parse_objective("") == "unattributed"
    assert ob.parse_objective(None) == "unattributed"


def test_parse_cost_class_defaults():
    """cost:<clase> from the first lines; unknown/absent fall back to tiny."""
    assert ob.parse_cost_class("cost:micro") == "micro"
    assert ob.parse_cost_class("cost: COMPLEX") == "complex"
    assert ob.parse_cost_class("cost:not-a-class") == "tiny"
    assert ob.parse_cost_class("no cost line here") == "tiny"
    assert ob.parse_cost_class("") == "tiny"
    assert ob.parse_cost_class(None) == "tiny"


# ---------------------------------------------------------------------------
# compute_status ladder
# ---------------------------------------------------------------------------

def test_compute_status_ladder():
    """open -> warn -> exhausted -> done over the default fractions."""
    assert ob.compute_status(1.0, 10.0, 5, 1) == "open"
    assert ob.compute_status(6.9, 10.0, 5, 1) == "open"
    assert ob.compute_status(7.0, 10.0, 5, 1) == "warn"       # >= 0.7*budget
    assert ob.compute_status(9.9, 10.0, 5, 1) == "warn"
    assert ob.compute_status(10.0, 10.0, 5, 1) == "exhausted"  # >= 1.0*budget
    assert ob.compute_status(12.0, 10.0, 5, 1) == "exhausted"
    assert ob.compute_status(5.0, 10.0, 5, 5) == "done"       # all tasks done
    assert ob.compute_status(5.0, 10.0, 5, 4) == "open"


def test_compute_status_no_budget_cannot_trip():
    """Phase 0 default: with no ceiling the ladder is armed but silent."""
    assert ob.compute_status(1e9, None, 5, 5) == "open"
    assert ob.compute_status(1e9, 0, 5, 5) == "open"
    assert ob.compute_status(1e9, "not-a-number", 5, 5) == "open"
    assert ob.compute_status(None, None, None, None) == "open"


def test_compute_status_custom_fractions():
    """warn/hard_stop fractions are per-objective overridable."""
    assert ob.compute_status(5.0, 10.0, 5, 1, warn_fraction=0.4) == "warn"
    assert ob.compute_status(5.0, 10.0, 5, 1,
                             hard_stop_fraction=0.4) == "exhausted"


# ---------------------------------------------------------------------------
# variance_breach (armed rule, design §5.2)
# ---------------------------------------------------------------------------

def test_variance_breach_threshold():
    """Breach only when actual > 3x the class estimate (strict)."""
    assert ob.variance_breach(6.3, 2.1, 1.0) is False   # exactly 3x: no breach
    assert ob.variance_breach(6.31, 2.1, 1.0) is True   # just over 3x
    assert ob.variance_breach(0.5, 2.1, 1.0) is False
    assert ob.variance_breach(10.0, 24.8, 1.0) is False


def test_variance_breach_unexercisable_returns_none():
    """Missing inputs never fabricate a verdict: None (unexercisable)."""
    assert ob.variance_breach(None, 2.1, 1.0) is None
    assert ob.variance_breach(1.0, None, 1.0) is None
    assert ob.variance_breach(1.0, 0, 1.0) is None
    assert ob.variance_breach(1.0, 2.1, None) is None
    # Phase 0 never passes a measured window price -> armed but silent.
    assert ob.variance_breach(1.0, 2.1) is None


# ---------------------------------------------------------------------------
# rollup / _process_rows
# ---------------------------------------------------------------------------

def test_rollup_task_flow_counters():
    """task-events rows with objective:OBJ-xx produce correct flow counts."""
    rows = [
        _task_event("created", "OBJ-A"),
        _task_event("claimed", "OBJ-A", "t-1"),
        _task_event("completed", "OBJ-A", "t-1"),
        _task_event("created", "OBJ-A"),
        _task_event("claimed", "OBJ-A", "t-2"),
        _task_event("crashed", "OBJ-B", "t-3"),
    ]
    doc = ob.rollup(rows, now=_NOW)
    a = doc["objectives"]["OBJ-A"]
    assert a["flow"]["created"] == 2
    assert a["flow"]["claimed"] == 2
    assert a["flow"]["completed"] == 1
    assert a["flow"]["crashed"] == 0
    assert a["tasks_total"] == 2
    assert a["tasks_done"] == 1
    assert a["tasks_active"] == 1
    assert doc["objectives"]["OBJ-B"]["flow"]["crashed"] == 1
    assert doc["tagged_events"] == 6
    assert doc["unattributed_events"]["lines"] == 0
    # Status stays open: no budget, ladder cannot trip.
    assert a["status"] == "open"


def test_rollup_unattributed_cost_is_shown_not_hidden():
    """All cost without a task_id/objective lands in the explicit bucket."""
    rows = [
        _cost_event(0.5),
        _cost_event(1.25),
        _cost_event(2.0, provider="usage-audit"),
    ]
    doc = ob.rollup(rows, now=_NOW)
    bucket = doc["unattributed_cost"]
    assert bucket["spent_usd"] == 3.75
    assert bucket["lines"] == 3
    assert bucket["by_provider"] == {"nanogpt": 1.75, "usage-audit": 2.0}
    assert bucket["spent_usd"] > 0
    assert doc["cost_lines_attributed"] == 0
    # Cost rows create no objective entries and no fabricated attribution.
    assert doc["objectives"] == {}


def test_rollup_tagged_cost_and_untagged_events():
    """Tagged cost goes to the objective's spent_usd; untagged events count."""
    rows = [
        _cost_event(0.75, objective="OBJ-A"),
        _task_event("created", objective=None),
        _task_event("claimed", objective=None, tid="t-9"),
    ]
    doc = ob.rollup(rows, now=_NOW)
    assert doc["objectives"]["OBJ-A"]["spent_usd"] == 0.75
    assert doc["cost_lines_attributed"] == 1
    assert doc["tagged_events"] == 0
    assert doc["unattributed_events"]["lines"] == 2
    # Untagged task events still show up under the explicit unattributed line.
    assert doc["objectives"]["unattributed"]["flow"]["created"] == 1


def test_rollup_is_deterministic_and_idempotent():
    """Same rows -> same fingerprint (timestamps stripped); rows not mutated."""
    rows = [_task_event("completed", "OBJ-A", "t-1"), _cost_event(0.5)]
    snapshot = json.dumps(rows, sort_keys=True)
    d1 = ob.rollup(rows, now=_NOW)
    d2 = ob.rollup(rows, now=_NOW)
    assert ob.doc_fingerprint(d1) == ob.doc_fingerprint(d2)
    assert json.dumps(rows, sort_keys=True) == snapshot  # read-only rollup


# ---------------------------------------------------------------------------
# Trace I/O against a tmp HERMES home (never the real ~/.hermes)
# ---------------------------------------------------------------------------

def test_read_and_write_roundtrip_tmp_home(tmp_path):
    """read_trace parses the tmp trace; write/load roundtrips the doc."""
    rows = [_task_event("completed", "OBJ-A", "t-1"), _cost_event(0.5)]
    trace_file = ob.trace_path(tmp_path)
    trace_file.parent.mkdir(parents=True, exist_ok=True)
    trace_file.write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    assert ob.read_trace(hermes_home=tmp_path) == rows

    doc = ob.rollup(rows, now=_NOW)
    assert ob.write_budgets(doc, hermes_home=tmp_path) is True
    assert ob.load_existing(hermes_home=tmp_path) == doc


def test_read_trace_missing_file_fails_open(tmp_path):
    """A missing trace is an empty rollup, never a crash (fails open)."""
    assert ob.read_trace(hermes_home=tmp_path) == []
    doc = ob.rollup(ob.read_trace(hermes_home=tmp_path), now=_NOW)
    assert doc["objectives"] == {}
    assert doc["unattributed_cost"]["spent_usd"] == 0.0


def test_read_trace_skips_corrupt_lines(tmp_path):
    """Corrupt JSONL lines are skipped; dated cutoffs drop old rows."""
    rows = [_task_event("completed", "OBJ-A", "t-1"), _cost_event(0.5)]
    trace_file = ob.trace_path(tmp_path)
    trace_file.parent.mkdir(parents=True, exist_ok=True)
    trace_file.write_text(
        "not json at all\n"
        + json.dumps(rows[0]) + "\n"
        + json.dumps({"source": "task-events", "cause": "created",
                      "objective": "OBJ-OLD", "ts_epoch_utc": 1.0}) + "\n",
        encoding="utf-8")
    got = ob.read_trace(hermes_home=tmp_path, window_days=30)
    assert got == [rows[0]]  # garbage skipped, pre-epoch row outside window


# ---------------------------------------------------------------------------
# preserve_human_decisions (the machine owns the meter, the human the ceiling)
# ---------------------------------------------------------------------------

def test_preserve_human_decisions_keeps_ceilings_recomputes_meter():
    """budget_*/fractions + notes survive; spent_*/status are recomputed."""
    rows = [_task_event("claimed", "OBJ-A", "t-1")]
    prev = ob.rollup(rows, now=_NOW)
    p = prev["objectives"]["OBJ-A"]
    p["budget_usd"] = 5.0
    p["warn_fraction"] = 0.5
    p["currency"] = "USD"
    p["notes"] = [{"epoch": _NOW, "note": "rescoped OBJ-A"}]
    p["spent_usd"] = 999.0          # stale machine field: must be overwritten
    p["status"] = "exhausted"       # stale derivation: must be recomputed

    doc = ob.rollup(rows, now=_NOW)  # spent_usd == 0.0 again
    merged = ob.preserve_human_decisions(doc, prev)
    o = merged["objectives"]["OBJ-A"]
    assert o["budget_usd"] == 5.0
    assert o["warn_fraction"] == 0.5
    assert o["currency"] == "USD"
    assert o["notes"] == [{"epoch": _NOW, "note": "rescoped OBJ-A"}]
    assert o["spent_usd"] == 0.0    # recomputed from the trace, never sticky
    # 0.0 < 0.5*5.0 -> open again, derived from the merged ceiling.
    assert o["status"] == "open"


def test_preserve_human_decisions_recomputes_with_merged_fractions():
    """Custom hard_stop_fraction from prev drives the recomputed status."""
    prev = {"objectives": {"OBJ-A": {
        "budget_usd": 5.0, "hard_stop_fraction": 0.5, "notes": []}}}
    doc = ob.rollup([_cost_event(3.0, objective="OBJ-A")], now=_NOW)
    merged = ob.preserve_human_decisions(doc, prev)
    o = merged["objectives"]["OBJ-A"]
    assert o["spent_usd"] == 3.0
    assert o["hard_stop_fraction"] == 0.5
    assert o["status"] == "exhausted"  # 3.0 >= 0.5*5.0 under the merged ceiling


def test_preserve_human_decisions_dedupes_notes():
    """Notes are append-only history: same note is not duplicated."""
    note = {"epoch": _NOW, "note": "approved default"}
    prev = {"objectives": {"OBJ-A": {"budget_usd": None, "notes": [note]}}}
    doc = ob.rollup([_task_event("created", "OBJ-A")], now=_NOW)
    doc["objectives"]["OBJ-A"]["notes"] = [note]
    merged = ob.preserve_human_decisions(doc, prev)
    assert merged["objectives"]["OBJ-A"]["notes"] == [note]


def test_preserve_human_decisions_bad_prev_is_noop():
    """Malformed prev {} / non-dict / missing objectives never corrupt doc."""
    doc = ob.rollup([_task_event("created", "OBJ-A")], now=_NOW)
    assert ob.preserve_human_decisions(doc, {}) is doc
    assert ob.preserve_human_decisions(doc, None) is doc
    assert ob.preserve_human_decisions(doc, {"objectives": []}) is doc
    # Unknown objectives in prev are ignored; doc objectives stay intact.
    prev = {"objectives": {"OBJ-GONE": {"budget_usd": 1.0}}}
    merged = ob.preserve_human_decisions(ob.rollup([], now=_NOW), prev)
    assert merged["objectives"] == {}


# ---------------------------------------------------------------------------
# CLI main() end-to-end on the tmp home
# ---------------------------------------------------------------------------

def test_main_writes_state_and_prints_json(tmp_path, capsys, monkeypatch):
    """main() rolls the tmp trace up, writes the state file, --json prints."""
    rows = [_task_event("completed", "OBJ-A", "t-1"), _cost_event(0.5)]
    trace_file = ob.trace_path(tmp_path)
    trace_file.parent.mkdir(parents=True, exist_ok=True)
    trace_file.write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    # trace_path/state_dir are borrowed from trace.py's namespace: patching
    # them on `ob` is what actually redirects main()'s I/O (a patched
    # get_hermes_home would not be seen through the borrowed functions).
    monkeypatch.setattr(ob, "trace_path",
                        lambda hermes_home=None: trace_file)
    monkeypatch.setattr(ob, "state_dir",
                        lambda hermes_home=None: tmp_path / "quota-governor")

    rc = ob.main(["--window-days", "30", "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["objectives"]["OBJ-A"]["tasks_done"] == 1
    assert out["unattributed_cost"]["spent_usd"] == 0.5
    # State file landed under the tmp home, not the real ~/.hermes.
    assert ob.budgets_path(tmp_path).exists()


def test_main_missing_trace_fails_open(tmp_path, capsys, monkeypatch):
    """No trace at all: exit 0, zeroed rollup still written (watchdog shape)."""
    monkeypatch.setattr(ob, "trace_path",
                        lambda hermes_home=None: tmp_path / "obs" / "none.jsonl")
    monkeypatch.setattr(ob, "state_dir",
                        lambda hermes_home=None: tmp_path / "quota-governor")
    assert ob.main(["--window-days", "30", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["objectives"] == {}
    assert out["unattributed_cost"]["spent_usd"] == 0.0
