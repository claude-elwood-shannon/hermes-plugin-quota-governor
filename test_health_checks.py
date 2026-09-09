#!/usr/bin/env python3
"""Test script for quota-governor health checks (OBJ-09).

Simulates all three health check conditions and verifies that each
produces a verifiable entry in the alert log:

1. Fast burn: inject two observations 3 min apart with >30% session delta.
2. Zombie workers: insert a fake running task with a heartbeat >2h old.
3. Silent plugin: use a temp HERMES_HOME with no observations file.

The test uses an isolated HERMES_HOME to avoid polluting real state.
"""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Add the plugin directory to the path
PLUGIN_DIR = "REPO"
sys.path.insert(0, PLUGIN_DIR)

# We need to import health_checks with a modified HERMES_HOME
# We'll set the env var before importing
TEST_HOME = tempfile.mkdtemp(prefix="qg-health-test-")
os.environ["HERMES_HOME"] = TEST_HOME

# CRITICAL: isolate the kanban DB too. get_kanban_db_path() resolves to the
# real ~/.hermes/kanban.db when it exists (it does in production), so without
# this override the test's setup_kanban_db() would INSERT fake tasks into the
# real shared DB, causing false zombie_worker alerts on every tick.
os.environ["HERMES_KANBAN_DB"] = os.path.join(TEST_HOME, "kanban.db")

# Now import
from health_checks import (
    check_fast_burn,
    check_zombie_workers,
    check_silent_plugin,
    run_all_health_checks,
    format_alerts_for_stdout,
    get_alert_log_path,
    get_observations_file,
    get_kanban_db_path,
    write_alert,
    FAST_BURN_DELTA_PCT,
    FAST_BURN_WINDOW_MIN,
    ZOMBIE_WORKER_HOURS,
    SILENT_PLUGIN_HOURS,
)

PASS = 0
FAIL = 0

def ok(msg):
    global PASS
    PASS += 1
    print(f"  PASS: {msg}")

def fail(msg):
    global FAIL
    FAIL += 1
    print(f"  FAIL: {msg}")

def setup_observation_file(entries):
    """Write observation entries to the test observations.jsonl."""
    obs_path = get_observations_file()
    obs_path.parent.mkdir(parents=True, exist_ok=True)
    with open(obs_path, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")

def setup_kanban_db(tasks):
    """Create a minimal kanban DB with the given task rows."""
    db_path = get_kanban_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # Drop existing DB file to support re-runs
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT,
            assignee TEXT,
            status TEXT,
            last_heartbeat_at INTEGER,
            started_at INTEGER
        )
    """)
    for t in tasks:
        conn.execute(
            "INSERT INTO tasks (id, title, assignee, status, last_heartbeat_at, started_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            t,
        )
    conn.commit()
    conn.close()

def read_alert_log():
    """Read the alert log and return list of parsed entries."""
    log_path = get_alert_log_path()
    if not log_path.exists():
        return []
    entries = []
    with open(log_path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return entries


# ---------------------------------------------------------------------------
# Clean up any previous test state
# ---------------------------------------------------------------------------
print("=" * 60)
print("OBJ-09 Health Checks Test")
print(f"Test HERMES_HOME: {TEST_HOME}")
print("=" * 60)

# Clear the alert log if it exists
alert_log = get_alert_log_path()
if alert_log.exists():
    alert_log.unlink()

# ---------------------------------------------------------------------------
# Test 1: Fast burn detection
# ---------------------------------------------------------------------------
print("\n--- Test 1: Fast burn detection ---")

now = datetime.now(timezone.utc)

# Two observations: 3 min apart, session jumps from 20% to 55% (>30pp delta)
obs_entries = [
    {
        "timestamp": (now - timedelta(minutes=3)).isoformat(),
        "event": "periodic_sample",
        "quota": {"ollama_session_pct": 20.0, "ollama_weekly_pct": 10.0},
    },
    {
        "timestamp": now.isoformat(),
        "event": "periodic_sample",
        "quota": {"ollama_session_pct": 55.0, "ollama_weekly_pct": 15.0},
    },
]
setup_observation_file(obs_entries)

# Clear alert log for this test
if alert_log.exists():
    alert_log.unlink()

alert = check_fast_burn()
if alert:
    ok(f"Fast burn alert generated: {alert['type']}")
    ok(f"  Delta: {alert.get('delta_pct')}pp (threshold: {FAST_BURN_DELTA_PCT}pp)")
    ok(f"  Message: {alert['message']}")
else:
    fail("Fast burn: no alert generated (expected one)")

# Verify it's in the log
log_entries = read_alert_log()
fast_burn_in_log = any(e.get("type") == "fast_burn" for e in log_entries)
if fast_burn_in_log:
    ok("Fast burn alert found in log file")
else:
    fail("Fast burn alert NOT found in log file")

# Test negative case: small delta should not alert
print("\n  --- Negative test: small delta (<30pp) ---")
obs_entries_small = [
    {
        "timestamp": (now - timedelta(minutes=3)).isoformat(),
        "event": "periodic_sample",
        "quota": {"ollama_session_pct": 20.0, "ollama_weekly_pct": 10.0},
    },
    {
        "timestamp": now.isoformat(),
        "event": "periodic_sample",
        "quota": {"ollama_session_pct": 35.0, "ollama_weekly_pct": 15.0},
    },
]
setup_observation_file(obs_entries_small)
alert_neg = check_fast_burn()
if alert_neg is None:
    ok("No alert for small delta (15pp < 30pp threshold)")
else:
    fail(f"Unexpected alert for small delta: {alert_neg}")

# ---------------------------------------------------------------------------
# Test 2: Zombie worker detection
# ---------------------------------------------------------------------------
print("\n--- Test 2: Zombie worker detection ---")

# Restore the fast-burn observations (so silent plugin doesn't fire)
setup_observation_file(obs_entries)

# Create a kanban DB with a zombie task (heartbeat >2h ago)
zombie_time = int((now - timedelta(hours=3)).timestamp())  # 3h ago
healthy_time = int(now.timestamp())  # just now

setup_kanban_db([
    ("t_zombie_01", "Zombie task", "pr-nanogpt", "running", zombie_time, zombie_time),
    ("t_healthy_01", "Healthy task", "pr-ollama", "running", healthy_time, healthy_time),
])

# Clear alert log for this test
if alert_log.exists():
    alert_log.unlink()

alerts = check_zombie_workers()
if len(alerts) >= 1:
    ok(f"Zombie worker alert(s) generated: {len(alerts)}")
    for a in alerts:
        ok(f"  {a['type']}: {a['message']}")
        if a.get("task_id") == "t_zombie_01":
            ok(f"  Correctly identified zombie task t_zombie_01")
else:
    fail("Zombie worker: no alerts generated (expected at least 1)")

# Verify the healthy task was NOT flagged
zombie_alerts_for_healthy = [a for a in alerts if a.get("task_id") == "t_healthy_01"]
if not zombie_alerts_for_healthy:
    ok("Healthy task t_healthy_01 was NOT flagged as zombie")
else:
    fail("Healthy task t_healthy_01 was incorrectly flagged as zombie")

# Verify in log
log_entries = read_alert_log()
zombie_in_log = [e for e in log_entries if e.get("type") == "zombie_worker"]
if zombie_in_log:
    ok(f"Zombie worker alert(s) found in log file ({len(zombie_in_log)})")
else:
    fail("Zombie worker alert NOT found in log file")

# ---------------------------------------------------------------------------
# Test 3: Silent plugin detection
# ---------------------------------------------------------------------------
print("\n--- Test 3: Silent plugin detection ---")

# Create an observations file with a single observation >1h old
old_ts = (now - timedelta(hours=2)).isoformat()
setup_observation_file([
    {
        "timestamp": old_ts,
        "event": "periodic_sample",
        "quota": {"ollama_session_pct": 10.0, "ollama_weekly_pct": 5.0},
    },
])

# Clear alert log for this test
if alert_log.exists():
    alert_log.unlink()

alert = check_silent_plugin()
if alert:
    ok(f"Silent plugin alert generated: {alert['type']}")
    ok(f"  Hours silent: {alert.get('hours_silent')} (threshold: {SILENT_PLUGIN_HOURS}h)")
    ok(f"  Message: {alert['message']}")
else:
    fail("Silent plugin: no alert generated (expected one)")

# Verify in log
log_entries = read_alert_log()
silent_in_log = any(e.get("type") == "silent_plugin" for e in log_entries)
if silent_in_log:
    ok("Silent plugin alert found in log file")
else:
    fail("Silent plugin alert NOT found in log file")

# Test negative: recent observation should NOT trigger silent plugin
print("\n  --- Negative test: recent observation (<1h) ---")
recent_ts = (now - timedelta(minutes=10)).isoformat()
setup_observation_file([
    {
        "timestamp": recent_ts,
        "event": "periodic_sample",
        "quota": {"ollama_session_pct": 10.0, "ollama_weekly_pct": 5.0},
    },
])
alert_neg = check_silent_plugin()
if alert_neg is None:
    ok("No alert for recent observation (10min < 1h threshold)")
else:
    fail(f"Unexpected alert for recent observation: {alert_neg}")

# Test: no observations file at all
print("\n  --- Edge case: no observations file ---")
obs_path = get_observations_file()
if obs_path.exists():
    obs_path.unlink()
if alert_log.exists():
    alert_log.unlink()
alert_no_file = check_silent_plugin()
if alert_no_file:
    ok(f"Alert generated for missing observations file: {alert_no_file['type']}")
else:
    fail("No alert for missing observations file (expected one)")

# Test: idle profile suppression (no active tasks)
print("\n  --- Idle profile: no active tasks should suppress silent_plugin ---")
# Setup stale observations but give the profile zero active tasks
old_ts = (now - timedelta(hours=2)).isoformat()
setup_observation_file([
    {
        "timestamp": old_ts,
        "event": "periodic_sample",
        "quota": {"ollama_session_pct": 10.0, "ollama_weekly_pct": 5.0},
    },
])
# Profile name = "pr-idle-dummy", no tasks in DB for it
if alert_log.exists():
    alert_log.unlink()
alert_idle = check_silent_plugin(profile_name="pr-idle-dummy")
if alert_idle is None:
    ok("Silent_plugin suppressed for idle profile (no active tasks)")
else:
    fail(f"Alert fired for idle profile: {alert_idle}")

# Test: active task should NOT suppress
print("\n  --- Active profile: running task should NOT suppress silent_plugin ---")
# Give pr-active-dummy a running task in the kanban DB
setup_kanban_db([
    ("t_active_01", "Active task", "pr-active-dummy", "running", int(now.timestamp()), int(now.timestamp())),
])
if alert_log.exists():
    alert_log.unlink()
alert_active = check_silent_plugin(profile_name="pr-active-dummy")
if alert_active is not None:
    ok("Silent_plugin fires for profile with running tasks + stale obs")
else:
    fail("Silent_plugin suppressed but profile has running tasks")

# Clean up the active task kanban DB so run_all_health_checks doesn't see it
if alert_log.exists():
    alert_log.unlink()

# ---------------------------------------------------------------------------
# Test: Dedup/backoff — same alert type consecutively should suppress
# ---------------------------------------------------------------------------
print("\n--- Test: Dedup/backoff for repeated alerts ---")

# Clear log
if alert_log.exists():
    alert_log.unlink()

# Write first alert
first = write_alert("test_dedup", "First occurrence", extra={"code": "A"})
if first is not None:
    ok("First alert written")
else:
    fail("First alert was deduplicated (should not be)")

# Write same alert again (same type + same extra)
second = write_alert("test_dedup", "Second occurrence (same state)", extra={"code": "A"})
if second is None:
    ok("Second alert suppressed (dedup: same type + same extra)")
else:
    fail("Second alert was written (should be deduplicated)")

# Write different alert type — should NOT be suppressed
third = write_alert("test_dedup_other", "Different type", extra={"code": "B"})
if third is not None:
    ok("Different type alert written (not deduplicated)")
else:
    fail("Different type alert was suppressed (should not be)")

# Write same type but different extra — should NOT be suppressed
fourth = write_alert("test_dedup", "Changed state", extra={"code": "B"})
if fourth is not None:
    ok("Same type with changed extra written (not deduplicated)")
else:
    fail("Same type with changed extra suppressed (should not be)")

# Verify only 3 entries in the log
log_entries = read_alert_log()
if len(log_entries) == 3:
    ok(f"Log has exactly {len(log_entries)} entries (3 written, 1 deduplicated)")
else:
    fail(f"Log has {len(log_entries)} entries (expected 3)")

# Clear log for next test
if alert_log.exists():
    alert_log.unlink()

# ---------------------------------------------------------------------------
print("\n--- Test 4: run_all_health_checks integration ---")

# Setup: fresh observations with fast-burn + zombie + silent
# NOTE: re-setup the zombie kanban DB because the previous test overwrote it
zombie_time = int((now - timedelta(hours=3)).timestamp())  # 3h ago
healthy_time = int(now.timestamp())  # just now

setup_kanban_db([
    ("t_zombie_01", "Zombie task", "pr-nanogpt", "running", zombie_time, zombie_time),
    ("t_healthy_01", "Healthy task", "pr-ollama", "running", healthy_time, healthy_time),
])
setup_observation_file([
    {
        "timestamp": (now - timedelta(minutes=3)).isoformat(),
        "event": "periodic_sample",
        "quota": {"ollama_session_pct": 20.0, "ollama_weekly_pct": 10.0},
    },
    {
        "timestamp": now.isoformat(),
        "event": "periodic_sample",
        "quota": {"ollama_session_pct": 55.0, "ollama_weekly_pct": 15.0},
    },
])
# zombie DB already set up from test 2

if alert_log.exists():
    alert_log.unlink()

all_alerts = run_all_health_checks()
types_found = {a["type"] for a in all_alerts}
print(f"  All alerts: {len(all_alerts)} ({types_found})")

if "fast_burn" in types_found:
    ok("run_all: fast_burn detected")
else:
    fail("run_all: fast_burn NOT detected")

if "zombie_worker" in types_found:
    ok("run_all: zombie_worker detected")
else:
    fail("run_all: zombie_worker NOT detected")

# Note: silent_plugin may or may not fire here depending on whether
# the fast-burn observations are recent enough. The latest observation
# is "now", so silent_plugin should NOT fire (good).
if "silent_plugin" not in types_found:
    ok("run_all: silent_plugin correctly NOT fired (recent obs exists)")
else:
    fail("run_all: silent_plugin unexpectedly fired with recent observations")

# Verify formatted output for stdout
formatted = format_alerts_for_stdout(all_alerts)
if formatted and "[ALERT" in formatted:
    ok(f"format_alerts_for_stdout produces {len(formatted.splitlines())} line(s)")
    print(f"  Sample: {formatted.splitlines()[0]}")
else:
    fail("format_alerts_for_stdout produced no output")

# Verify all alerts are in the log
log_entries = read_alert_log()
log_types = {e.get("type") for e in log_entries}
print(f"  Log types: {log_types}")
if "fast_burn" in log_types and "zombie_worker" in log_types:
    ok("All expected alert types found in log file")
else:
    fail(f"Missing alert types in log. Found: {log_types}")

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print("\n" + "=" * 60)
print(f"Results: {PASS} passed, {FAIL} failed")
print("=" * 60)

# Cleanup
shutil.rmtree(TEST_HOME, ignore_errors=True)

sys.exit(0 if FAIL == 0 else 1)
