#!/usr/bin/env python3
"""Live-path e2e: gate against a COPY of the real kanban.db with injected
privacy-tagged tasks — proves the HERMES_KANBAN_DB env path works and the
census counts correctly against a realistic DB.

Read-only for the real DB: copies it to /tmp, inserts test tasks with
privacy tags into the copy, runs the gate (real main(), real API queries)
with HERMES_KANBAN_DB pointed at the copy, prints privacy_summary.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.abspath(__file__))
REAL_DB = "~/.hermes/kanban.db"
GATE = os.path.join(REPO, "scripts", "quota-gate.py")

tmpdir = tempfile.mkdtemp(prefix="privacy-e2e-")
db_copy = os.path.join(tmpdir, "kanban-copy.db")
shutil.copy2(REAL_DB, db_copy)

# Inject tagged tasks into the copy (multiple profiles + a malformed tag)
conn = sqlite3.connect(db_copy)
injected = [
    ("t_e2e_high_ollama", "E2E high (ollama assignee)",
     "objective:OBJ-E2E privacy:high auto_created:true", "ready",
     "pr-ollama"),
    ("t_e2e_low_nanogpt", "E2E low (nanogpt assignee)",
     "objective:OBJ-E2E privacy:low auto_created:true", "ready",
     "pr-nanogpt"),
    ("t_e2e_medium_opencode", "E2E medium (opencode assignee)",
     "objective:OBJ-E2E privacy:medium auto_created:true", "todo",
     "pr-opencode"),
    ("t_e2e_bad", "E2E malformed tag",
     "objective:OBJ-E2E privacy:ultra-secret", "ready", "pr-nanogpt"),
    ("t_e2e_done_high", "E2E done with high tag (must NOT count)",
     "objective:OBJ-E2E privacy:high", "done", "pr-ollama"),
]
for tid, title, body, status, assignee in injected:
    conn.execute(
        "INSERT OR REPLACE INTO tasks "
        "(id, title, body, status, assignee, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (tid, title, body, status, assignee, 0),
    )
conn.commit()
conn.close()

env = dict(os.environ)
env["HERMES_KANBAN_DB"] = db_copy
env.pop("QUOTA_GATE_PRIVACY", None)

# Baseline census of the REAL board (pre-injection), computed with the
# gate's own parser so the expectation tracks board drift instead of a
# frozen Sep-7 snapshot.
import importlib.util
_gspec = importlib.util.spec_from_file_location("qg_for_e2e", GATE)
assert _gspec and _gspec.loader
qg = importlib.util.module_from_spec(_gspec)
_gspec.loader.exec_module(qg)


def bucket_of(body):
    raw = qg._parse_privacy_tag_raw(body or "")
    return qg._PRIVACY_SUMMARY_BUCKETS.get(raw.lower(), "none") if raw else "none"

base_conn = sqlite3.connect(REAL_DB)
base_rows = base_conn.execute(
    "SELECT status, body FROM tasks WHERE status IN ('ready','running',"
    "'blocked','todo','triage')").fetchall()
base_conn.close()
from collections import Counter
baseline = Counter(bucket_of(b) for _, b in base_rows)
print("real-board baseline census:", dict(baseline))

proc = subprocess.run(
    [sys.executable, GATE],
    stdin=subprocess.DEVNULL,
    capture_output=True, text=True, env=env, timeout=120,
)
if proc.returncode != 0:
    print("GATE FAILED:", proc.returncode)
    print(proc.stderr)
    sys.exit(1)

lines = [l for l in proc.stdout.strip().splitlines() if l.strip()]
data = json.loads(lines[-1])
ctx = data.get("context", {})
summary = ctx.get("privacy_summary")
warning = ctx.get("warning")

print("wakeAgent:", data.get("wakeAgent"))
print("recommended_profile:", ctx.get("recommended_profile"))
print("privacy_summary:", json.dumps(summary, sort_keys=True))
print("warning:", warning)

# Expected = real-board baseline + injection delta:
#   +1 high (t_e2e_high_ollama) +1 medium (t_e2e_medium_opencode)
#   +1 low (t_e2e_low_nanogpt) +1 none (t_e2e_bad malformed → none)
#   done-high NOT counted (terminal status)
expected = dict(baseline)
expected["high"] = expected.get("high", 0) + 1
expected["medium"] = expected.get("medium", 0) + 1
expected["low"] = expected.get("low", 0) + 1
expected["none"] = expected.get("none", 0) + 1
if summary == expected:
    print(f"\nE2E PASS: summary {summary} == expected {expected}")
    rc = 0
else:
    print(f"\nE2E FAIL: summary {summary} != expected {expected}")
    rc = 1

# The malformed tag must have produced a warning mentioning the task
if warning and "t_e2e_bad" in warning and "privacy:ultra-secret" in warning:
    print("Malformed-tag warning present: OK")
else:
    print(f"Malformed-tag warning MISSING or wrong: {warning!r}")
    rc = 1

shutil.rmtree(tmpdir, ignore_errors=True)
sys.exit(rc)
