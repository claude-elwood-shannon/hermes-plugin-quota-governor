#!/usr/bin/env python3
"""verify_recommendation_unchanged.py — OBJ-18 S1 acceptance evidence.

Criterion: "Recommendation existente inalterada (diff del output
antes/después con tareas sin tags = idéntica)".

Method:
  1. Extract the PRE-change quota-gate.py from git HEAD (the worktree's
     unmodified baseline).
  2. Load both versions (old and new) as separate modules.
  3. Freeze the world identically for both:
       - Mock all provider query functions (no live API calls).
       - Same frozen provider raw data → identical providers array.
       - Mock get_existing_profiles (no `hermes` CLI call).
       - Same temp kanban.db, active tasks WITHOUT privacy tags.
  4. Run main() of each version, capture the JSON output.
  5. Diff: strip the additive privacy_summary key from the new output
     and compare the full JSON against the old output.  They must be
     identical after canonical (sorted-keys) serialisation.

Exit 0 = recommendation identical (acceptance met).
Exit 1 = any difference (prints the diff).
"""
import contextlib
import importlib.util
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest.mock as mock

REPO = os.path.dirname(os.path.abspath(__file__))
GATE_PATH = os.path.join(REPO, "scripts", "quota-gate.py")
OLD_GATE = os.path.join(tempfile.gettempdir(), "quota-gate-OLD.py")

# Frozen raw provider data (identical for both runs)
FROZEN_OLLAMA = {"session_pct": 40.0, "weekly_pct": 20.0, "activity_cost": 0.0}
FROZEN_NANOGPT = {"state": "active", "daily_pct": 30.0, "weekly_tokens_pct": 50.0}
FROZEN_OPENROUTER = {"limit": 10.0, "usage": 3.0, "usage_weekly_usd": 1.0,
                     "expires_at": None}
FROZEN_OPENCODE = {
    "rolling_pct": 10.0, "weekly_pct": 20.0, "monthly_pct": 5.0,
    "rolling_status": "ok", "weekly_status": "ok", "monthly_status": "ok",
    "rolling_resets_at": None, "weekly_resets_at": None,
    "monthly_resets_at": None,
}

EXPECTED_PROFILES = {"pr-ollama", "pr-nanogpt", "pr-opencode"}

FAKE_ENV = {
    "NANO_GPT_API_KEY": "k", "OPENROUTER_API_KEY": "k",
    "OPENCODE_GO_API_KEY": "k", "OLLAMA_API_KEY": "k",
}


def old_gate_ref():
    """Commit whose quota-gate.py is the PRE-S1 baseline.

    Hard-coding HEAD was only valid while the branch tip sat directly on
    top of the base commit.  After a rebase onto main, HEAD itself already
    contains the S1 changes, so the "old" side would be the modified gate
    and the diff would be meaningless (privacy_summary stripped from one
    side only).  Anchor on the merge-base instead, falling back to HEAD.
    """
    out = subprocess.run(
        ["git", "-C", REPO, "merge-base", "HEAD", "main"],
        capture_output=True, text=True,
    )
    if out.returncode == 0 and out.stdout.strip():
        return out.stdout.strip()
    return "HEAD~1"


def extract_old_gate():
    """Get quota-gate.py as of the pre-S1 baseline (see old_gate_ref).

    Also copies the baseline's scripts/providers.json next to the extracted
    gate so both versions resolve the SAME providers config: the gate derives
    PROVIDERS_CONFIG_PATH from its own directory, and a /tmp extraction
    would otherwise read no providers.json (parked flags would differ —
    a setup artifact, not a behavioural change).
    """
    ref = old_gate_ref()
    print("baseline ref for old gate:", ref)
    out = subprocess.run(
        ["git", "-C", REPO, "show", f"{ref}:scripts/quota-gate.py"],
        capture_output=True, text=True, check=True,
    )
    with open(OLD_GATE, "w", encoding="utf-8") as f:
        f.write(out.stdout)

    cfg = subprocess.run(
        ["git", "-C", REPO, "show", "HEAD:scripts/providers.json"],
        capture_output=True, text=True,
    )
    if cfg.returncode == 0:
        with open(os.path.join(os.path.dirname(OLD_GATE), "providers.json"),
                  "w", encoding="utf-8") as f:
            f.write(cfg.stdout)
    return OLD_GATE


def make_untagged_kanban_db():
    """Temp kanban.db with active tasks WITHOUT privacy tags.

    Includes one done task WITH a privacy tag to prove terminal tasks
    are not censused.
    """
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY, title TEXT, body TEXT, status TEXT,
            assignee TEXT, created_at INTEGER,
            started_at INTEGER, completed_at INTEGER
        )
    """)
    tasks = [
        ("t_a", "Refactor health checks", "**Goal**\nno privacy tag here", "ready"),
        ("t_b", "Docs update", "Just documentation work", "running"),
        ("t_c", "Sync repos", "sync repos, no tags", "blocked"),
        ("t_d", "Backlog idea", "future idea in triage", "triage"),
        ("t_e", "Done task", "privacy:high\nbut terminal status", "done"),
    ]
    for tid, title, body, status in tasks:
        conn.execute(
            "INSERT INTO tasks (id, title, body, status, assignee, created_at) "
            "VALUES (?, ?, ?, ?, 'pr-nanogpt', 0)",
            (tid, title, body, status),
        )
    conn.commit()
    conn.close()
    return path


def load_gate_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def capture_output(mod, kanban_db, scratch):
    """Run mod.main() with all external calls mocked and frozen.

    Returns the parsed JSON output dict.
    """
    env = {
        "HERMES_KANBAN_DB": kanban_db,
        "HERMES_HOME": scratch,  # _cache_dir() → scratch, not real profile
    }
    os.environ.pop("QUOTA_GATE_PRIVACY", None)

    buf = io.StringIO()
    # Freeze the per-model cost ledger (MULTI-PROV-09) on BOTH sides: it is
    # live state (real profile state.db scans + shared cursor throttling) and
    # path-sensitive (the extracted old gate lives in /tmp, where its
    # model-cost-ledger.py sibling is missing → null).  Neither has anything
    # to do with S1; the comparison must isolate the privacy_summary delta.
    ledger_frozen = lambda rolling_resets_at=None: (None, [])
    with mock.patch.dict(os.environ, env, clear=False), \
         mock.patch.object(mod, "query_ollama",
                           lambda: dict(FROZEN_OLLAMA)), \
         mock.patch.object(mod, "query_nanogpt",
                           lambda: dict(FROZEN_NANOGPT)), \
         mock.patch.object(mod, "query_openrouter",
                           lambda: dict(FROZEN_OPENROUTER)), \
         mock.patch.object(mod, "query_opencode_go",
                           lambda: dict(FROZEN_OPENCODE)), \
         mock.patch.object(mod, "get_existing_profiles",
                           lambda: set(EXPECTED_PROFILES)), \
         mock.patch.object(mod, "get_env", lambda key: FAKE_ENV.get(key)), \
         mock.patch.object(mod, "model_cost_context", ledger_frozen), \
         contextlib.redirect_stdout(buf):
        mod.main()
    return json.loads(buf.getvalue().strip())


def main():
    old_path = extract_old_gate()
    kanban_db = make_untagged_kanban_db()

    with tempfile.TemporaryDirectory() as scratch_old, \
         tempfile.TemporaryDirectory() as scratch_new:
        old_mod = load_gate_module("gate_old", old_path)
        new_mod = load_gate_module("gate_new", GATE_PATH)

        old_out = capture_output(old_mod, kanban_db, scratch_old)
        new_out = capture_output(new_mod, kanban_db, scratch_new)

    # The ONLY allowed difference: the additive privacy_summary key.
    ctx = dict(new_out.get("context", {}))
    summary = ctx.pop("privacy_summary", None)
    new_stripped = dict(new_out)
    new_stripped["context"] = ctx

    old_s = json.dumps(old_out, sort_keys=True, indent=2)
    new_s = json.dumps(new_stripped, sort_keys=True, indent=2)

    print("=" * 70)
    print("OLD output (HEAD, keys sorted):")
    print(old_s)
    print("=" * 70)
    print("NEW output (privacy_summary stripped):")
    print(new_s)
    print("=" * 70)

    if summary is not None:
        print("NEW additive field privacy_summary:",
              json.dumps(summary, sort_keys=True))

    os.unlink(kanban_db)
    os.unlink(old_path)

    if old_s == new_s:
        print("\nVERDICT: IDENTICAL — recommendation output unchanged.")
        # sanity: 4 active untagged tasks → all-none; the done task with
        # privacy:high must NOT be counted (terminal status).
        expected = {"high": 0, "medium": 0, "low": 0, "none": 4}
        if summary != expected:
            print(f"WARNING: privacy_summary unexpected: {summary} "
                  f"(expected {expected})")
            return 1
        print(f"privacy_summary sanity: {summary} == expected {expected} OK")
        return 0
    print("\nVERDICT: DIFFERENT — recommendation output CHANGED:")
    import difflib
    for line in difflib.unified_diff(
        old_s.splitlines(), new_s.splitlines(),
        "old(HEAD)", "new(S1)", lineterm="",
    ):
        print(line)
    return 1


if __name__ == "__main__":
    sys.exit(main())
