#!/usr/bin/env python3
"""test_assignee_fix.py — tests for OBJ-08 deterministic G1 enforcement.

Tests the assignee-fix.py script that reassigns tasks whose assignee is not
a valid profile (the LLM agent sometimes invents names like 'alice').

Tests:
  1. find_invalid_assignee_tasks: flags 'alice' (invalid) in ready/todo/triage.
  2. find_invalid_assignee_tasks: does NOT flag pr-ollama/pr-nanogpt (valid).
  3. find_invalid_assignee_tasks: ignores unassigned (NULL) tasks.
  4. find_invalid_assignee_tasks: ignores running/done/blocked/archived tasks.
  5. compute_valid_profiles: intersects existing with ALLOWED_PROFILES, orders by preference.
  6. main() --dry-run: prints what would change, reassigns nothing.
  7. main() no invalid tasks: silent (empty stdout, exit 0).
  8. get_existing_profiles: parses 'hermes profile list' output.

The test uses an isolated kanban DB to avoid polluting real state.
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

# Add the plugin directory to the path
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(PLUGIN_DIR, "scripts"))

# Isolate kanban DB
TEST_HOME = tempfile.mkdtemp(prefix="qg-assignee-fix-test-")

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


# ── Test DB setup ────────────────────────────────────────────────────────────

def make_test_db(tasks):
    """Create a minimal kanban DB with the given tasks.

    tasks: list of (id, title, assignee, status) tuples.
    Returns the db path.
    """
    db_path = os.path.join(TEST_HOME, "kanban.db")
    if os.path.exists(db_path):
        os.unlink(db_path)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT,
            assignee TEXT,
            body TEXT,
            status TEXT,
            created_at INTEGER
        )
    """)
    now = int(time.time())
    for tid, title, assignee, status in tasks:
        conn.execute(
            "INSERT INTO tasks (id, title, assignee, body, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (tid, title, assignee, "test body", status, now),
        )
    conn.commit()
    conn.close()
    return db_path


# ── Import the module under test ──────────────────────────────────────────────

# Add the plugin scripts directory to the path and import the module.
# The script is named assignee-fix.py (hyphen, not importable as a module),
# so we load it via importlib with a safe alias.
import importlib.util
_script_path = os.path.join(PLUGIN_DIR, "scripts", "assignee-fix.py")
_spec = importlib.util.spec_from_file_location("assignee_fix", _script_path)
af = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(af)


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_find_invalid_flags_alice():
    print("\n--- Test 1: find_invalid_assignee_tasks flags 'alice' ---")
    db = make_test_db([
        ("t_001", "OBJ-08 task", "alice", "ready"),
        ("t_002", "OBJ-09 task", "bob", "todo"),
        ("t_003", "OBJ-10 task", "charlie", "triage"),
    ])
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    valid_set = {"pr-ollama", "pr-nanogpt"}
    invalid = af.find_invalid_assignee_tasks(conn, valid_set)
    conn.close()
    if len(invalid) == 3:
        ok(f"flagged all 3 invalid assignees: {[t[2] for t in invalid]}")
    else:
        fail(f"expected 3 invalid, got {len(invalid)}: {invalid}")


def test_find_invalid_ignores_valid():
    print("\n--- Test 2: find_invalid_assignee_tasks ignores valid profiles ---")
    db = make_test_db([
        ("t_010", "OBJ-08 task", "pr-ollama", "ready"),
        ("t_011", "OBJ-09 task", "pr-nanogpt", "todo"),
    ])
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    valid_set = {"pr-ollama", "pr-nanogpt"}
    invalid = af.find_invalid_assignee_tasks(conn, valid_set)
    conn.close()
    if len(invalid) == 0:
        ok("no valid-profile tasks flagged")
    else:
        fail(f"expected 0 invalid, got {len(invalid)}: {invalid}")


def test_find_invalid_ignores_unassigned():
    print("\n--- Test 3: find_invalid_assignee_tasks ignores NULL assignee ---")
    db = make_test_db([
        ("t_020", "unassigned triage", None, "triage"),
        ("t_021", "empty assignee", "", "ready"),
        ("t_022", "invalid one", "alice", "ready"),
    ])
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    valid_set = {"pr-ollama", "pr-nanogpt"}
    invalid = af.find_invalid_assignee_tasks(conn, valid_set)
    conn.close()
    ids = [t[0] for t in invalid]
    if len(invalid) == 1 and ids == ["t_022"]:
        ok(f"only flagged the invalid one: {ids}")
    else:
        fail(f"expected only t_022 flagged, got {ids}")


def test_find_invalid_ignores_non_reassignable():
    print("\n--- Test 4: find_invalid_assignee_tasks ignores running/done/blocked ---")
    db = make_test_db([
        ("t_030", "running alice", "alice", "running"),
        ("t_031", "done alice", "alice", "done"),
        ("t_032", "blocked alice", "alice", "blocked"),
        ("t_033", "archived alice", "alice", "archived"),
        ("t_034", "ready alice", "alice", "ready"),
    ])
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    valid_set = {"pr-ollama", "pr-nanogpt"}
    invalid = af.find_invalid_assignee_tasks(conn, valid_set)
    conn.close()
    ids = [t[0] for t in invalid]
    if len(invalid) == 1 and ids == ["t_034"]:
        ok(f"only flagged the ready one: {ids}")
    else:
        fail(f"expected only t_034 flagged, got {ids}")


def test_compute_valid_profiles():
    print("\n--- Test 5: compute_valid_profiles intersects + orders ---")
    with patch.object(af, "get_existing_profiles", return_value={"pr-ollama", "pr-nanogpt", "pr-openrouter", "pr-vllm"}):
        ordered, valid_set = af.compute_valid_profiles()
    if ordered == ["pr-ollama", "pr-nanogpt"] and valid_set == {"pr-ollama", "pr-nanogpt"}:
        ok(f"ordered={ordered}, set={sorted(valid_set)}")
    else:
        fail(f"expected ordered=['pr-ollama','pr-nanogpt'], got ordered={ordered}, set={sorted(valid_set)}")

    # Edge: no allowed profiles exist on host
    print("  -- edge: no allowed profiles on host --")
    with patch.object(af, "get_existing_profiles", return_value={"pr-openrouter", "pr-vllm"}):
        ordered, valid_set = af.compute_valid_profiles()
    if ordered == [] and valid_set == set():
        ok("empty valid set when no allowed profiles exist")
    else:
        fail(f"expected empty, got ordered={ordered}, set={sorted(valid_set)}")


def test_main_dry_run():
    print("\n--- Test 6: main() --dry-run prints, reassigns nothing ---")
    db = make_test_db([
        ("t_040", "OBJ-08 alice bug", "alice", "ready"),
    ])
    af.KANBAN_DB = db
    with patch.object(af, "get_existing_profiles", return_value={"pr-ollama", "pr-nanogpt"}):
        with patch.object(af, "reassign_task", return_value=True) as mock_reassign:
            result = subprocess.run(
                [sys.executable, os.path.join(PLUGIN_DIR, "scripts", "assignee-fix.py"), "--dry-run"],
                capture_output=True, text=True, timeout=30,
                env={**os.environ, "HOME": TEST_HOME},
            )
    # --dry-run runs in a subprocess so it won't use our patched af.KANBAN_DB;
    # but it will find the real kanban.db unless we set the env.  We verify the
    # script runs and exits 0.  The real reassignment is tested via the
    # in-process path below.
    if result.returncode == 0:
        ok(f"script exited 0 (dry-run mode)")
    else:
        fail(f"script exited {result.returncode}: {result.stderr[:200]}")

    # In-process: call main() directly with --dry-run using our test DB.
    print("  -- in-process main() --dry-run --")
    sys.argv = [sys.argv[0], "--dry-run"]
    with patch.object(af, "get_existing_profiles", return_value={"pr-ollama", "pr-nanogpt"}):
        with patch.object(af, "reassign_task", return_value=True) as mock_reassign:
            old_stdout = sys.stdout
            sys.stdout = captured = MagicMock()
            captured.write = []
            class FakeStdout:
                def __init__(self):
                    self.lines = []
                def write(self, s):
                    if s.strip():
                        self.lines.append(s.strip())
                def flush(self):
                    pass
            fake = FakeStdout()
            sys.stdout = fake
            rc = af.main()
            sys.stdout = old_stdout
    if rc == 0 and mock_reassign.call_count == 0:
        ok(f"dry-run did not reassign (calls={mock_reassign.call_count}), lines={fake.lines}")
    elif rc == 0:
        fail(f"dry-run should not reassign, but got {mock_reassign.call_count} calls")
    else:
        fail(f"main() returned {rc}")


def test_main_silent_when_no_invalid():
    print("\n--- Test 7: main() silent when no invalid tasks ---")
    db = make_test_db([
        ("t_050", "valid task", "pr-ollama", "ready"),
        ("t_051", "unassigned", None, "triage"),
    ])
    af.KANBAN_DB = db
    sys.argv = [sys.argv[0]]
    with patch.object(af, "get_existing_profiles", return_value={"pr-ollama", "pr-nanogpt"}):
        with patch.object(af, "reassign_task", return_value=True) as mock_reassign:
            class FakeStdout:
                def __init__(self):
                    self.lines = []
                def write(self, s):
                    if s.strip():
                        self.lines.append(s.strip())
                def flush(self):
                    pass
            fake = FakeStdout()
            old_stdout = sys.stdout
            sys.stdout = fake
            rc = af.main()
            sys.stdout = old_stdout
    if rc == 0 and mock_reassign.call_count == 0 and len(fake.lines) == 0:
        ok("silent (no output, no reassign) when all valid")
    else:
        fail(f"expected silent no-op; rc={rc}, calls={mock_reassign.call_count}, lines={fake.lines}")


def test_get_existing_profiles_parses():
    print("\n--- Test 8: get_existing_profiles parses 'hermes profile list' ---")
    fake_output = (
        "Profiles:\n"
        "──────────────────────────────────\n"
        "◆ pr-ollama\n"
        "  pr-nanogpt\n"
        "  pr-openrouter\n"
        "  pr-vllm\n"
    )
    mock_proc = MagicMock()
    mock_proc.stdout = fake_output
    with patch("subprocess.run", return_value=mock_proc):
        profiles = af.get_existing_profiles()
    if profiles == {"pr-ollama", "pr-nanogpt", "pr-openrouter", "pr-vllm"}:
        ok(f"parsed {sorted(profiles)}")
    else:
        fail(f"expected 4 profiles, got {sorted(profiles)}")

    # Fallback when command fails
    print("  -- edge: command fails, fallback to ALLOWED_PROFILES --")
    with patch("subprocess.run", side_effect=OSError("not found")):
        profiles = af.get_existing_profiles()
    if profiles == af.ALLOWED_PROFILES:
        ok(f"fallback to ALLOWED_PROFILES: {sorted(profiles)}")
    else:
        fail(f"expected fallback {sorted(af.ALLOWED_PROFILES)}, got {sorted(profiles)}")


def test_main_reassigns_invalid():
    print("\n--- Test 9: main() reassigns invalid assignee to fallback ---")
    db = make_test_db([
        ("t_060", "OBJ-08 alice bug", "alice", "ready"),
    ])
    af.KANBAN_DB = db
    sys.argv = [sys.argv[0]]
    reassign_calls = []
    def fake_reassign(task_id, profile):
        reassign_calls.append((task_id, profile))
        return True
    with patch.object(af, "get_existing_profiles", return_value={"pr-ollama", "pr-nanogpt"}):
        with patch.object(af, "reassign_task", side_effect=fake_reassign):
            class FakeStdout:
                def __init__(self):
                    self.lines = []
                def write(self, s):
                    if s.strip():
                        self.lines.append(s.strip())
                def flush(self):
                    pass
            fake = FakeStdout()
            old_stdout = sys.stdout
            sys.stdout = fake
            rc = af.main()
            sys.stdout = old_stdout
    if rc == 0 and len(reassign_calls) == 1 and reassign_calls[0] == ("t_060", "pr-ollama"):
        ok(f"reassigned alice -> pr-ollama: {reassign_calls}, lines={fake.lines}")
    else:
        fail(f"expected 1 reassign to pr-ollama, got {reassign_calls}")


# ── Run all tests ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("test_assignee_fix.py — OBJ-08 G1 deterministic enforcement")
    print("=" * 60)
    test_find_invalid_flags_alice()
    test_find_invalid_ignores_valid()
    test_find_invalid_ignores_unassigned()
    test_find_invalid_ignores_non_reassignable()
    test_compute_valid_profiles()
    test_main_dry_run()
    test_main_silent_when_no_invalid()
    test_get_existing_profiles_parses()
    test_main_reassigns_invalid()
    print("\n" + "=" * 60)
    print(f"Results: {PASS} passed, {FAIL} failed")
    print("=" * 60)
    import shutil
    shutil.rmtree(TEST_HOME, ignore_errors=True)
    sys.exit(1 if FAIL else 0)