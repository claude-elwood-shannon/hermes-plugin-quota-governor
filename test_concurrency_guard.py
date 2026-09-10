#!/usr/bin/env python3
"""Test script for quota-governor concurrency guard (OBJ-06).

Tests:
1. Live worker count: insert fake running tasks with alive PIDs, verify count.
2. Soft cap: live workers >= desired_max → should_spawn=False.
3. Hard cap: live workers > hard_limit → oldest workers marked for kill.
4. Kill worker: SIGTERM is sent (using our own PID as a sacrificial test).
5. Edge cases: no DB, no running tasks, dead PIDs.

The test uses an isolated HERMES_HOME and HERMES_KANBAN_DB to avoid
polluting real state.
"""

import json
import os
import shutil
import signal
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

# Add the plugin directory to the path
PLUGIN_DIR = str(Path(__file__).resolve().parent)
sys.path.insert(0, PLUGIN_DIR)

# Isolate HERMES_HOME and kanban DB
TEST_HOME = tempfile.mkdtemp(prefix="qg-concurrency-test-")
os.environ["HERMES_HOME"] = TEST_HOME
os.environ["HERMES_KANBAN_DB"] = os.path.join(TEST_HOME, "kanban.db")

# Now import
from concurrency_guard import (
    check_concurrency,
    count_live_workers,
    get_live_workers,
    kill_worker,
    sort_by_age,
    WorkerInfo,
    get_kanban_db_path,
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

def setup_kanban_db(tasks):
    """Create a minimal kanban DB with the given task rows.
    Each task: (id, title, assignee, status, worker_pid, started_at, last_heartbeat_at)
    """
    db_path = get_kanban_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT,
            assignee TEXT,
            status TEXT,
            worker_pid INTEGER,
            started_at INTEGER,
            last_heartbeat_at INTEGER
        )
    """)
    for t in tasks:
        conn.execute(
            "INSERT INTO tasks (id, title, assignee, status, worker_pid, started_at, last_heartbeat_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            t,
        )
    conn.commit()
    conn.close()


# Use a sleep subprocess as a sacrificial "alive PID" for testing
def spawn_test_pids(n):
    """Spawn n child processes that sleep, return their PIDs."""
    pids = []
    for i in range(n):
        pid = os.fork()
        if pid == 0:
            # Child: sleep in a Python-level loop so SIGTERM's handler runs
            signal.signal(signal.SIGTERM, lambda s, f: os._exit(0))
            for _ in range(300):  # 30 seconds, 100ms steps
                time.sleep(0.1)
            os._exit(0)
        else:
            pids.append(pid)
    return pids

def cleanup_pids(pids):
    for p in pids:
        try:
            os.kill(p, signal.SIGTERM)
            os.waitpid(p, 0)
        except (ProcessLookupError, OSError):
            pass


# ---------------------------------------------------------------------------
print("=" * 60)
print("OBJ-06 Concurrency Guard Test")
print(f"Test HERMES_HOME: {TEST_HOME}")
print("=" * 60)

# -----------------------------------------------------------------------
# Test 1: Live worker count with alive PIDs
# -----------------------------------------------------------------------
print("\n--- Test 1: Live worker count with alive PIDs ---")

test_pids = spawn_test_pids(3)
time.sleep(0.2)  # let children start

now_epoch = int(time.time())
setup_kanban_db([
    ("t_alive_01", "Alive worker 1", "pr-ollama", "running", test_pids[0], now_epoch - 300, now_epoch - 10),
    ("t_alive_02", "Alive worker 2", "pr-nanogpt", "running", test_pids[1], now_epoch - 200, now_epoch - 5),
    ("t_alive_03", "Alive worker 3", "pr-ollama", "running", test_pids[2], now_epoch - 100, now_epoch - 1),
    ("t_done_01", "Completed task", "pr-ollama", "done", 99999, now_epoch - 500, now_epoch - 400),
    ("t_dead_01", "Dead PID task", "pr-ollama", "running", 999999, now_epoch - 600, now_epoch - 500),
])

workers = get_live_workers()
if len(workers) == 3:
    ok(f"Live worker count: {len(workers)} (expected 3: 3 alive, 1 dead PID, 1 done)")
else:
    fail(f"Live worker count: {len(workers)} (expected 3)")

# Verify dead PID was not counted
worker_ids = [w.task_id for w in workers]
if "t_dead_01" not in worker_ids:
    ok("Dead PID task correctly excluded from live workers")
else:
    fail("Dead PID task incorrectly included in live workers")

if "t_done_01" not in worker_ids:
    ok("Done task correctly excluded from live workers")
else:
    fail("Done task incorrectly included in live workers")

count = count_live_workers()
if count == 3:
    ok(f"count_live_workers() returns {count}")
else:
    fail(f"count_live_workers() returns {count} (expected 3)")

cleanup_pids(test_pids)

# -----------------------------------------------------------------------
# Test 2: Running task with NULL worker_pid is counted as live
# -----------------------------------------------------------------------
print("\n--- Test 2: NULL worker_pid counted as live ---")

setup_kanban_db([
    ("t_null_pid", "No PID task", "pr-ollama", "running", None, now_epoch - 100, now_epoch - 10),
])

workers = get_live_workers()
if len(workers) == 1:
    ok(f"NULL PID task counted as live: {len(workers)}")
else:
    fail(f"NULL PID task count: {len(workers)} (expected 1)")

# -----------------------------------------------------------------------
# Test 3: Soft cap — live >= desired_max → should_spawn=False
# -----------------------------------------------------------------------
print("\n--- Test 3: Soft cap ---")

# 3 live workers, desired_max=2 → should_spawn=False
setup_kanban_db([
    ("t_null_01", "Worker 1", "pr-ollama", "running", None, now_epoch - 300, now_epoch - 10),
    ("t_null_02", "Worker 2", "pr-ollama", "running", None, now_epoch - 200, now_epoch - 5),
    ("t_null_03", "Worker 3", "pr-ollama", "running", None, now_epoch - 100, now_epoch - 1),
])

decision = check_concurrency(desired_max=2)
if not decision.should_spawn:
    ok(f"Soft cap: 3 live >= 2 desired → should_spawn=False ({decision.reason})")
else:
    fail(f"Soft cap: should_spawn={decision.should_spawn} (expected False)")

if decision.live_count == 3:
    ok(f"Live count: {decision.live_count}")
else:
    fail(f"Live count: {decision.live_count} (expected 3)")

if len(decision.workers_to_kill) == 0:
    ok(f"No kills needed (3 <= hard_limit=4)")
else:
    fail(f"Unexpected kills: {len(decision.workers_to_kill)}")

# 1 live worker, desired_max=2 → should_spawn=True
setup_kanban_db([
    ("t_null_01", "Worker 1", "pr-ollama", "running", None, now_epoch - 300, now_epoch - 10),
])
decision = check_concurrency(desired_max=2)
if decision.should_spawn:
    ok(f"Soft cap: 1 live < 2 desired → should_spawn=True")
else:
    fail(f"Soft cap: should_spawn={decision.should_spawn} (expected True)")

# -----------------------------------------------------------------------
# Test 4: Hard cap — live > hard_limit → oldest workers killed
# -----------------------------------------------------------------------
print("\n--- Test 4: Hard cap ---")

# 5 workers, desired_max=2, hard_limit=3 → kill 2 oldest
setup_kanban_db([
    ("t_old_01", "Oldest", "pr-ollama", "running", None, now_epoch - 1000, now_epoch - 900),
    ("t_old_02", "Old", "pr-ollama", "running", None, now_epoch - 800, now_epoch - 700),
    ("t_mid_03", "Mid", "pr-ollama", "running", None, now_epoch - 500, now_epoch - 400),
    ("t_new_04", "New", "pr-ollama", "running", None, now_epoch - 200, now_epoch - 100),
    ("t_new_05", "Newest", "pr-ollama", "running", None, now_epoch - 100, now_epoch - 1),
])

decision = check_concurrency(desired_max=2, hard_limit=3)
if len(decision.workers_to_kill) == 2:
    ok(f"Hard cap: 5 live > 3 hard → kill {len(decision.workers_to_kill)} oldest")
else:
    fail(f"Hard cap: kill count={len(decision.workers_to_kill)} (expected 2)")

# Verify the oldest are selected
kill_ids = [w.task_id for w in decision.workers_to_kill]
if "t_old_01" in kill_ids and "t_old_02" in kill_ids:
    ok(f"Correctly selected oldest: {kill_ids}")
else:
    fail(f"Wrong workers selected for kill: {kill_ids}")

# Verify the newest are NOT selected
if "t_new_04" not in kill_ids and "t_new_05" not in kill_ids:
    ok("Newest workers correctly NOT selected for kill")
else:
    fail("Newest workers incorrectly selected for kill")

# -----------------------------------------------------------------------
# Test 5: Hard limit via env var
# -----------------------------------------------------------------------
print("\n--- Test 5: Hard limit via env var ---")

os.environ["QUOTA_GOVERNOR_HARD_LIMIT"] = "4"
setup_kanban_db([
    ("t_old_01", "Oldest", "pr-ollama", "running", None, now_epoch - 1000, now_epoch - 900),
    ("t_old_02", "Old", "pr-ollama", "running", None, now_epoch - 800, now_epoch - 700),
    ("t_mid_03", "Mid", "pr-ollama", "running", None, now_epoch - 500, now_epoch - 400),
    ("t_new_04", "New", "pr-ollama", "running", None, now_epoch - 200, now_epoch - 100),
    ("t_new_05", "Newest", "pr-ollama", "running", None, now_epoch - 100, now_epoch - 1),
])
decision = check_concurrency(desired_max=2)
if decision.hard_limit == 4:
    ok(f"Hard limit from env: {decision.hard_limit}")
else:
    fail(f"Hard limit from env: {decision.hard_limit} (expected 4)")

if len(decision.workers_to_kill) == 1:
    ok(f"5 live > 4 hard → kill {len(decision.workers_to_kill)}")
else:
    fail(f"Kill count: {len(decision.workers_to_kill)} (expected 1)")

del os.environ["QUOTA_GOVERNOR_HARD_LIMIT"]

# -----------------------------------------------------------------------
# Test 6: Default hard limit = desired_max + 2
# -----------------------------------------------------------------------
print("\n--- Test 6: Default hard limit ---")

setup_kanban_db([
    ("t_01", "Worker 1", "pr-ollama", "running", None, now_epoch - 100, now_epoch - 10),
    ("t_02", "Worker 2", "pr-ollama", "running", None, now_epoch - 90, now_epoch - 5),
])
decision = check_concurrency(desired_max=3)
if decision.hard_limit == 5:
    ok(f"Default hard limit: {decision.hard_limit} (3+2)")
else:
    fail(f"Default hard limit: {decision.hard_limit} (expected 5)")

# -----------------------------------------------------------------------
# Test 7: Kill worker with real PID
# -----------------------------------------------------------------------
print("\n--- Test 7: Kill worker (real PID) ---")

test_pids = spawn_test_pids(1)
time.sleep(0.2)

worker = WorkerInfo(
    task_id="t_sacrifice",
    assignee="pr-ollama",
    pid=test_pids[0],
    started_at=now_epoch - 100,
    last_heartbeat_at=now_epoch - 10,
)
result = kill_worker(worker)
if result:
    ok("kill_worker returned True (SIGTERM sent)")
else:
    fail("kill_worker returned False")

# Verify process is dead — wait for it with os.waitpid (blocking, 2s timeout)
import subprocess
deadline = time.time() + 2
terminated = False
while time.time() < deadline:
    try:
        pid_result, status = os.waitpid(test_pids[0], os.WNOHANG)
        if pid_result != 0:
            terminated = True
            break
    except ChildProcessError:
        terminated = True
        break
    time.sleep(0.1)

if terminated:
    ok("Process terminated after kill_worker")
else:
    # Force kill if still alive
    try:
        os.kill(test_pids[0], signal.SIGKILL)
        os.waitpid(test_pids[0], 0)
    except OSError:
        pass
    fail("Process still alive after kill_worker (SIGTERM may not interrupt sleep)")

# Clean up
try:
    os.waitpid(test_pids[0], 0)
except OSError:
    pass

# -----------------------------------------------------------------------
# Test 8: kill_worker with None PID
# -----------------------------------------------------------------------
print("\n--- Test 8: Kill worker (None PID) ---")

worker = WorkerInfo(
    task_id="t_no_pid",
    assignee="pr-ollama",
    pid=None,
    started_at=now_epoch - 100,
    last_heartbeat_at=now_epoch - 10,
)
result = kill_worker(worker)
if not result:
    ok("kill_worker returned False for None PID")
else:
    fail("kill_worker returned True for None PID (should not)")

# -----------------------------------------------------------------------
# Test 9: Empty DB / no running tasks
# -----------------------------------------------------------------------
print("\n--- Test 9: Empty DB ---")

setup_kanban_db([])
workers = get_live_workers()
if len(workers) == 0:
    ok("Empty DB: 0 live workers")
else:
    fail(f"Empty DB: {len(workers)} workers (expected 0)")

decision = check_concurrency(desired_max=2)
if decision.live_count == 0 and decision.should_spawn:
    ok(f"Empty DB: live={decision.live_count} should_spawn={decision.should_spawn}")
else:
    fail(f"Empty DB: live={decision.live_count} should_spawn={decision.should_spawn}")

# -----------------------------------------------------------------------
# Test 10: Non-existent DB
# -----------------------------------------------------------------------
print("\n--- Test 10: Non-existent DB ---")

os.environ["HERMES_KANBAN_DB"] = "/nonexistent/path/kanban.db"
workers = get_live_workers()
if len(workers) == 0:
    ok("Non-existent DB: 0 live workers (graceful)")
else:
    fail(f"Non-existent DB: {len(workers)} workers (expected 0)")

decision = check_concurrency(desired_max=2)
if decision.live_count == 0 and decision.should_spawn:
    ok(f"Non-existent DB: should_spawn={decision.should_spawn}")
else:
    fail(f"Non-existent DB: should_spawn={decision.should_spawn} (expected True)")

# -----------------------------------------------------------------------
# Test 11: Sort by age
# -----------------------------------------------------------------------
print("\n--- Test 11: Sort by age ---")

os.environ["HERMES_KANBAN_DB"] = os.path.join(TEST_HOME, "kanban.db")
setup_kanban_db([
    ("t_mid", "Mid", "pr-ollama", "running", None, now_epoch - 500, now_epoch - 400),
    ("t_old", "Oldest", "pr-ollama", "running", None, now_epoch - 1000, now_epoch - 900),
    ("t_new", "Newest", "pr-ollama", "running", None, now_epoch - 100, now_epoch - 1),
])
workers = get_live_workers()
sorted_workers = sort_by_age(workers)
if sorted_workers[0].task_id == "t_old" and sorted_workers[-1].task_id == "t_new":
    ok(f"Sort by age: {[w.task_id for w in sorted_workers]}")
else:
    fail(f"Sort by age incorrect: {[w.task_id for w in sorted_workers]}")

# -----------------------------------------------------------------------
# Summary
# -----------------------------------------------------------------------
print("\n" + "=" * 60)
print(f"Results: {PASS} passed, {FAIL} failed")
print("=" * 60)

# Cleanup
shutil.rmtree(TEST_HOME, ignore_errors=True)

sys.exit(0 if FAIL == 0 else 1)