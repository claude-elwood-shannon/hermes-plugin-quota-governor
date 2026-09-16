#!/usr/bin/python3.12
"""test_task_cost_seal.py — offline suite for scripts/obs/task-cost-seal.py.

Covers the creation-time seal hook: seal_one writes a kind=estimate row on
an empty ledger ('sin-datos'), the estimator ladder fires once MIN_N (3)
attributed training rows exist, multiple seals append (never overwrite),
main() prints the summary line and exits 0, degrades (never crashes) when
seal_one raises, and honours the `now` timestamp override.

Hermetic: every test points QUOTA_TASK_COST_LEDGER at a tmp file and the
env var is restored afterwards — the real ledger is never touched.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = _load("task_cost_seal_under_test", HERE / "task-cost-seal.py")
seal_one = mod.seal_one
main = mod.main
ledger_path_func = mod.ledger_path

BODY = "objective:OBJ-1\ncost:small\nmodel:m"


@pytest.fixture
def ledger_tmp(tmp_path):
    """Redirect the shared ledger to a tmp file; restore env afterwards."""
    env_path = tmp_path / "ledger.jsonl"
    saved = os.environ.get("QUOTA_TASK_COST_LEDGER")
    os.environ["QUOTA_TASK_COST_LEDGER"] = str(env_path)
    try:
        yield env_path
    finally:
        if saved is None:
            os.environ.pop("QUOTA_TASK_COST_LEDGER", None)
        else:
            os.environ["QUOTA_TASK_COST_LEDGER"] = saved


def read_ledger(path):
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def test_seal_one_empty_ledger(ledger_tmp):
    # No history yet: stage 'sin-datos', but the absence of data is
    # itself recorded as one estimate row.
    res = seal_one("t_empty", BODY, model="m")
    assert res["stage"] == "sin-datos"
    ledger = read_ledger(ledger_tmp)
    assert len(ledger) == 1
    line = ledger[0]
    assert line["kind"] == "estimate"
    assert line["task_id"] == "t_empty"
    est = line["estimate"]
    assert est["p50"] is None
    assert est["p90"] is None
    assert est["n"] == 0
    assert est["stage"] == "sin-datos"
    assert isinstance(line["ts"], float)


def test_seal_one_with_training_rows(ledger_tmp):
    # Seed MIN_N (3) attributed training rows so the exact
    # (objective, cost_class, model) group of the ladder can fire.
    ledger_tmp.parent.mkdir(parents=True, exist_ok=True)
    with open(ledger_tmp, "w", encoding="utf-8") as f:
        for i in range(3):
            f.write(json.dumps({
                "kind": "task",
                "task_id": "t%d" % i,
                "attributed": True,
                "costUsd": 20.0,
                "objective": "OBJ-1",
                "cost_class": "small",
                "model": "m",
            }) + "\n")
    res = seal_one("t2", BODY, model="m")
    assert res["stage"] == "objective+class+model"
    # The estimate is the last (appended) row; training rows precede it.
    est = [r for r in read_ledger(ledger_tmp) if r["kind"] == "estimate"]
    assert len(est) == 1
    est = est[0]["estimate"]
    assert est["p50"] == 20.0
    assert est["p90"] == 20.0
    assert est["n"] == 3


def test_seal_appends_multiple(ledger_tmp):
    seal_one("tA", BODY, model="m")
    seal_one("tB", BODY, model="m")
    ledger = read_ledger(ledger_tmp)
    assert len(ledger) == 2
    assert {l["task_id"] for l in ledger} == {"tA", "tB"}


def test_main_stdout_capture(tmp_path, ledger_tmp, capsys):
    # main() reads --body-file (a path), prints the summary, exits 0.
    body_file = tmp_path / "body.md"
    body_file.write_text(BODY, encoding="utf-8")
    rc = main(["--task-id", "tX", "--body-file", str(body_file),
               "--model", "m"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "task-cost-seal" in out
    assert "stage=" in out


def test_main_degrade_on_error(tmp_path, ledger_tmp, capsys, monkeypatch):
    # A raising seal_one must degrade to a printed line, never crash.
    def bad_seal(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(mod, "seal_one", bad_seal)
    body_file = tmp_path / "body.md"
    body_file.write_text(BODY, encoding="utf-8")
    with contextlib.redirect_stdout(io.StringIO()) as buf:
        rc = main(["--task-id", "t_err", "--body-file", str(body_file),
                   "--model", "m"])
    output = buf.getvalue()
    assert rc == 0
    assert "task-cost-seal: degrade" in output
    assert "RuntimeError" in output


def test_seal_one_time_override(ledger_tmp):
    now = 123456.789
    seal_one("t_now", BODY, model="m", now=now)
    ledger = read_ledger(ledger_tmp)
    assert ledger[0]["ts"] == now
