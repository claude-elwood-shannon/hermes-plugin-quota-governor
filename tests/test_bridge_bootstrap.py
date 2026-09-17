#!/usr/bin/env python3
"""Offline tests for GET /bootstrap (t_a003af3e, bridge v1.9).

Hermetic: the bridge module is imported via spec_from_file_location into an
isolated module object, then every external source is pointed at fixtures —
hermes CLI mocked (HermesBridge._run), kanban.db swapped (bridge._AO_DB),
HERMES_HOME / PLUGIN_REPO redirected to tmp dirs. No real board, logs, SSH
or network is touched. The HTTP server binds 127.0.0.1:0.

Run:  .venv/bin/python -m pytest tests/test_bridge_bootstrap.py -q
"""
import importlib.util
import json
import os
import shutil
import threading
import urllib.error
import urllib.request
from http.server import HTTPServer

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BRIDGE = os.path.join(REPO, "scripts", "bridge", "open-webui-bridge.py")

spec = importlib.util.spec_from_file_location("bridge_bootstrap_under_test", BRIDGE)
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)

srv = HTTPServer(("127.0.0.1", 0), bridge.HermesBridge)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"


def req(path):
    try:
        with urllib.request.urlopen(BASE + path, timeout=15) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}


# ------------------------------------------------------------ fixtures

MINIYAML_SRC = os.path.join(REPO, "capabilities", "_miniyaml.py")

PLUGIN_YAML = """\
name: self-govern
version: 0.2.0
description: "Test plugin fixture."
author: "test + Hermes Agent"
license: MIT
platforms: [linux]
hooks: [kanban_task_claimed]
"""

CAP_MANIFEST = """\
name: selftest-cap
version: 0.1.0
description: "Fixture capability."
objective: OBJ-TEST
permissions: [read]
triggers: [kanban_task_claimed]
"""

STATS_JSON = json.dumps({
    "by_status": {"done": 27, "running": 7, "triage": 60},
    "by_assignee": {"pr-nanogpt": {"done": 26, "running": 7}},
    "oldest_ready_age_seconds": None,
    "now": 1789640037,
})

CRON_SUMMARY = {"ts": "2026-09-17T12:00:01+0200", "checked": 7, "ok": 7,
                "dead": 0, "never_run": 0, "zombie": 0, "actions": []}


def _make_plugin_repo(root):
    """tmp PLUGIN_REPO: plugin.yaml + capabilities/ (_miniyaml + 1 manifest)."""
    capdir = os.path.join(root, "capabilities")
    os.makedirs(capdir, exist_ok=True)
    with open(os.path.join(root, "plugin.yaml"), "w", encoding="utf-8") as f:
        f.write(PLUGIN_YAML)
    shutil.copy(MINIYAML_SRC, os.path.join(capdir, "_miniyaml.py"))
    os.makedirs(os.path.join(capdir, "selftest-cap"), exist_ok=True)
    with open(os.path.join(capdir, "selftest-cap", "manifest.yaml"), "w",
              encoding="utf-8") as f:
        f.write(CAP_MANIFEST)
    return root


def _make_hermes_home(root):
    """tmp HERMES_HOME: logs/ with a cron-health-check.jsonl summary."""
    logdir = os.path.join(root, "logs")
    os.makedirs(logdir, exist_ok=True)
    with open(os.path.join(logdir, "kanban-watchdog.log"), "w") as f:
        f.write("wd line 1\nwd line 2\n")
    with open(os.path.join(logdir, "cron-health-check.jsonl"), "w") as f:
        f.write(json.dumps({"ts": "2026-09-17T11:45:01+0200", "checked": 7,
                            "ok": 6, "dead": 1, "never_run": 0, "zombie": 0,
                            "actions": []}) + "\n")
        f.write(json.dumps(CRON_SUMMARY) + "\n")
    return root


def _make_kanban_db(path):
    import sqlite3
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE approved_objectives ("
                "id TEXT PRIMARY KEY, name TEXT, budget_daily REAL, status TEXT)")
    con.execute("INSERT INTO approved_objectives VALUES "
                "('OBJ-A', 'Alpha', 0.5, 'active')")
    con.execute("INSERT INTO approved_objectives VALUES "
                "('OBJ-B', 'Beta', 0.25, 'achieved')")
    con.commit()
    con.close()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Full offline bootstrap environment + server-ready module state."""
    plugin_repo = _make_plugin_repo(str(tmp_path / "plugin_repo"))
    hermes_home = _make_hermes_home(str(tmp_path / "hermes_home"))
    db = str(tmp_path / "kanban.db")
    _make_kanban_db(db)
    monkeypatch.setattr(bridge, "PLUGIN_REPO", plugin_repo)
    monkeypatch.setattr(bridge, "HERMES_HOME", hermes_home)
    monkeypatch.setattr(bridge, "_AO_DB", db)
    # capabilities cache lives across tests — force re-scan per request.
    monkeypatch.setattr(bridge, "_CAP_MANIFEST_CACHE",
                        {"ts": 0.0, "caps": None, "errors": []})
    monkeypatch.setattr(bridge.HermesBridge, "_run",
                        lambda self, cmd, timeout=15: STATS_JSON)
    return {"plugin_repo": plugin_repo, "hermes_home": hermes_home, "db": db}


# ------------------------------------------------- unit: _bs_cron_health

def test_bs_cron_health_reads_last_summary_line(env):
    out = bridge._bs_cron_health()
    assert out == CRON_SUMMARY


def test_bs_cron_health_missing_file(env, tmp_path, monkeypatch):
    empty = str(tmp_path / "nohome")
    os.makedirs(empty, exist_ok=True)
    monkeypatch.setattr(bridge, "HERMES_HOME", empty)
    out = bridge._bs_cron_health()
    assert "error" in out and "cron-health-check.jsonl" in out["error"]


def test_bs_cron_health_empty_file(env, tmp_path, monkeypatch):
    empty = str(tmp_path / "nohome2")
    os.makedirs(os.path.join(empty, "logs"), exist_ok=True)
    open(os.path.join(empty, "logs", "cron-health-check.jsonl"), "w").close()
    monkeypatch.setattr(bridge, "HERMES_HOME", empty)
    out = bridge._bs_cron_health()
    assert "error" in out and "no summary line" in out["error"]


def test_bs_cron_health_corrupt_last_line(env, tmp_path, monkeypatch):
    home = str(tmp_path / "nohome3")
    os.makedirs(os.path.join(home, "logs"), exist_ok=True)
    p = os.path.join(home, "logs", "cron-health-check.jsonl")
    with open(p, "w") as f:
        f.write(json.dumps(CRON_SUMMARY) + "\n{not json\n")
    monkeypatch.setattr(bridge, "HERMES_HOME", home)
    out = bridge._bs_cron_health()
    assert "error" in out


# --------------------------------------- unit: _bs_capabilities_block

def test_bs_capabilities_block_maps_payload(env):
    out = bridge._bs_capabilities_block()
    assert out["count"] == 1
    cap = out["capabilities"][0]
    assert cap["name"] == "selftest-cap"
    assert cap["objective"] == "OBJ-TEST"
    assert out["load_errors"] == []


def test_bs_capabilities_block_exception_degrades(env, monkeypatch):
    def boom(force=False):
        raise RuntimeError("capscan failed")
    monkeypatch.setattr(bridge, "_load_capabilities", boom)
    out = bridge._bs_capabilities_block()
    assert out == {"error": "capscan failed"}


# ------------------------------------------- endpoint GET /bootstrap

def test_bootstrap_200_all_blocks_present(env):
    code, body = req("/bootstrap")
    assert code == 200, body
    for key in ("bootstrap_version", "plugin", "board", "objectives",
                "capabilities", "health", "endpoints", "endpoint_count",
                "logs_tail"):
        assert key in body, key
    assert body["bootstrap_version"] == 1
    assert body["plugin"]["name"] == "self-govern"
    assert body["plugin"]["version"] == "0.2.0"
    assert body["board"]["stats"]["by_status"]["running"] == 7
    assert body["objectives"]["count"] == 2
    assert {o["id"] for o in body["objectives"]["objectives"]} == {"OBJ-A", "OBJ-B"}
    assert body["capabilities"]["count"] == 1
    assert body["health"]["bridge"]["alive"] is True
    assert body["health"]["bridge"]["port"] == 9120
    assert body["health"]["crons"] == CRON_SUMMARY


def test_bootstrap_endpoints_list_matches_spec(env):
    code, body = req("/bootstrap")
    assert code == 200
    expected = sorted(
        f"{'POST' if 'post' in ops else 'GET'} {p}"
        for p, ops in bridge.OPENAPI_SPEC["paths"].items())
    assert body["endpoints"] == expected
    assert body["endpoint_count"] == len(expected)
    assert "GET /bootstrap" in body["endpoints"]
    assert "POST /save-idea" in body["endpoints"]


def test_bootstrap_logs_tail_embedded(env):
    code, body = req("/bootstrap")
    assert code == 200
    assert set(body["logs_tail"]) == {"watchdog", "tick", "health"}
    assert "wd line 1" in body["logs_tail"]["watchdog"]
    # tick log does not exist in the fixture -> graceful placeholder
    assert "no log" in body["logs_tail"]["tick"]


def test_bootstrap_plugin_yaml_broken_rest_survives(env, monkeypatch):
    with open(os.path.join(env["plugin_repo"], "plugin.yaml"), "w") as f:
        f.write(" ::: [not: parseable\n")
    code, body = req("/bootstrap")
    assert code == 200
    assert "error" in body["plugin"]
    assert body["objectives"]["count"] == 2
    assert body["capabilities"]["count"] == 1


def test_bootstrap_stats_error_degrades(env, monkeypatch):
    monkeypatch.setattr(bridge.HermesBridge, "_run",
                        lambda self, cmd, timeout=15: "ERROR: cli down")
    code, body = req("/bootstrap")
    assert code == 200
    assert "error" in body["board"]["stats"]
    assert body["plugin"]["name"] == "self-govern"


def test_bootstrap_objectives_missing_degrades(env, monkeypatch):
    monkeypatch.setattr(bridge, "_AO_DB", str(env["db"] + ".missing"))
    code, body = req("/bootstrap")
    assert code == 200
    assert "error" in body["objectives"]
    assert "approved_objectives unavailable" in body["objectives"]["error"]


def test_bootstrap_ideas_explicitly_excluded(env):
    code, body = req("/bootstrap")
    assert code == 200
    assert "ideas" not in body
    assert "GET /ideas" in body["endpoints"]


# ------------------------------------------------- OpenAPI contract

def test_openapi_declares_bootstrap():
    assert bridge.OPENAPI_SPEC["info"]["version"] == "1.9.0"
    entry = bridge.OPENAPI_SPEC["paths"]["/bootstrap"]["get"]
    assert entry["operationId"] == "get_bootstrap"
    assert entry["responses"]["200"]


def test_dispatcher_routes_bootstrap(env):
    code, body = req("/bootstrap")
    assert code == 200
    assert body["health"]["bridge"]["spec_version"] == "1.9.0"
