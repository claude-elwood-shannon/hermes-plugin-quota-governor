#!/usr/bin/env python3
"""e2e_zombie_live.py — OBJ-21 live-path proof against the REAL kanban.db (ro).

Criterion 2 of the card: "Tick con zombie → script output wakeAgent:false →
creator SILENT".  Runs the DEPLOYED gate (the copy the cron actually
executes) against the real board.  Right now (t_e793b2b9 running ~30 min)
there is no zombie, so wakeAgent must be True and zombie_check.has_zombie
False.  A zombie case is then proven by injecting an old RUNNING task into
a COPY of the real DB (never touching the real one) and re-running the
deployed gate pointed at the copy via HERMES_KANBAN_DB.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

HOME = os.path.expanduser("~")
DEPLOYED_GATE = os.path.join(HOME, ".hermes/profiles/pr-ollama/scripts/quota-gate.py")
REAL_DB = os.path.join(HOME, ".hermes/kanban.db")

print("=== e2e part 1: deployed gate against the REAL board (read-only) ===")
env = dict(os.environ)
env.pop("QUOTA_GATE_PRIVACY", None)
env["HERMES_KANBAN_DB"] = REAL_DB
proc = subprocess.run(
    [sys.executable, DEPLOYED_GATE],
    stdin=subprocess.DEVNULL, capture_output=True, text=True,
    env=env, timeout=120,
)
if proc.returncode != 0:
    print("GATE FAILED:", proc.returncode)
    print(proc.stderr[-500:])
    sys.exit(1)
out = json.loads(proc.stdout.strip().splitlines()[-1])
ctx = out.get("context", {})
zc = ctx.get("zombie_check")
print("wakeAgent:", out.get("wakeAgent"))
print("zombie_check:", json.dumps(zc, sort_keys=True))
if zc is None:
    print("FAIL: zombie_check missing from live snapshot")
    sys.exit(1)
if zc.get("has_zombie"):
    print("NOTE: a real zombie exists right now — wakeAgent must be False")
    part1_ok = not out.get("wakeAgent")
else:
    part1_ok = out.get("wakeAgent") is True
print("part1:", "PASS" if part1_ok else "FAIL")

print()
print("=== e2e part 2: injected zombie in a COPY of the real DB ===")
tmpdir = tempfile.mkdtemp(prefix="zombie-e2e-")
db_copy = os.path.join(tmpdir, "kanban-copy.db")
shutil.copy2(REAL_DB, db_copy)
conn = sqlite3.connect(db_copy)
now = int(time.time())
conn.execute(
    "INSERT OR REPLACE INTO tasks (id, title, body, status, assignee, "
    "created_at, started_at, last_heartbeat_at) "
    "VALUES ('t_e2e_zombie', 'OBJ-E2E injected zombie', "
    "'objective:OBJ-E2E cost:micro', 'running', 'pr-ollama', ?, ?, ?)",
    (now - 3700, now - 3700, now - 3500),
)
conn.commit()
conn.close()

env2 = dict(os.environ)
env2.pop("QUOTA_GATE_PRIVACY", None)
env2["HERMES_KANBAN_DB"] = db_copy
proc2 = subprocess.run(
    [sys.executable, DEPLOYED_GATE],
    stdin=subprocess.DEVNULL, capture_output=True, text=True,
    env=env2, timeout=120,
)
if proc2.returncode != 0:
    print("GATE FAILED:", proc2.returncode)
    print(proc2.stderr[-500:])
    sys.exit(1)
out2 = json.loads(proc2.stdout.strip().splitlines()[-1])
ctx2 = out2.get("context", {})
zc2 = ctx2.get("zombie_check")
print("wakeAgent:", out2.get("wakeAgent"))
print("zombie_check:", json.dumps(zc2, sort_keys=True))
print("warning:", (ctx2.get("warning") or "")[:160])

ok2 = (
    out2.get("wakeAgent") is False
    and zc2 is not None
    and zc2.get("has_zombie") is True
    and any(t["id"] == "t_e2e_zombie" for t in zc2.get("tasks", []))
    and "zombie_guard" in (ctx2.get("warning") or "")
)
print("part2:", "PASS" if ok2 else "FAIL")

shutil.rmtree(tmpdir, ignore_errors=True)
sys.exit(0 if (part1_ok and ok2) else 1)