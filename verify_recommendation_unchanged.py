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
  5. Diff: strip the additive privacy_summary and zombie_check keys from
     the new output and compare the full JSON against the old output.
     They must be identical after canonical (sorted-keys) serialisation.

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
import time
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
    side only).  Resolution order:
      1. QUOTA_GATE_BASE_REF env override (explicit, for automation).
      2. merge-base(HEAD, main) — correct while S1 sits on a feature branch.
      3. HEAD~1 — once merged, the parent of the fix commit predates S1.
    """
    override = os.environ.get("QUOTA_GATE_BASE_REF")
    if override:
        return override

    def git(*args):
        r = subprocess.run(["git", "-C", REPO, *args],
                           capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else ""

    # Pickaxe: the first commit that introduced the S1 census function;
    # its parent is the true pre-S1 baseline.  Works on the feature branch
    # AND after the merge onto main, where merge-base(HEAD, main) degenerates
    # to HEAD and HEAD~N counting is positional/fragile.
    #
    # OBJ-21 note: the baseline must predate BOTH additive features.  When
    # verifying the zombie guard, use QUOTA_GATE_BASE_REF=<migration commit>
    # (6d73746, Sep-8 worker-model migration) — the pre-S1 baseline picked
    # below also predates that migration, so the worker_models map
    # legitimately differs (z-ai/glm-5.3-flash) and the default run
    # reports a spurious DIFFERENT hunk.  Both baselines are supported:
    # additive keys are stripped from BOTH sides in the diff (below), so
    # the verifier is baseline-agnostic; only genuinely pre-migration
    # comparisons need the env override.
    introduced = git("log", "--format=%H", "--reverse", "-S",
                     "compute_privacy_summary", "--", "scripts/quota-gate.py")
    first = introduced.splitlines()[0] if introduced else ""
    if first:
        parent = git("rev-parse", f"{first}^")
        if parent:
            return parent

    base = git("merge-base", "HEAD", "main")
    head = git("rev-parse", "HEAD")
    if base and head and base != head:
        return base
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
        ["git", "-C", REPO, "show", f"{ref}:scripts/providers.json"],
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
    are not censused.  Schema carries last_heartbeat_at so the OBJ-21
    zombie guard reads the same column set as the real board.
    """
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY, title TEXT, body TEXT, status TEXT,
            assignee TEXT, created_at INTEGER,
            started_at INTEGER, last_heartbeat_at INTEGER, completed_at INTEGER
        )
    """)
    tasks = [
        # (id, title, body, status, last_heartbeat_at, started_at)
        # hb=0 → epoch 1970.  The OBJ-21 zombie guard treats a running row
        # with heartbeat 0 as age ~56 years... which WOULD fire the guard
        # and change wakeAgent.  So the "running" task gets a RECENT
        # heartbeat (time.time() minus 5 min — a live worker) to keep the
        # recommendation comparison meaningful.  0 is fine for the other
        # (non-running) statuses: the guard only reads running rows.
        ("t_a", "Refactor health checks", "**Goal**\nno privacy tag here",
         "ready", 0, 0),
        ("t_b", "Docs update", "Just documentation work", "running",
         int(time.time() - 5 * 60), int(time.time() - 10 * 60)),
        ("t_c", "Sync repos", "sync repos, no tags", "blocked", 0, 0),
        ("t_d", "Backlog idea", "future idea in triage", "triage", 0, 0),
        ("t_e", "Done task", "privacy:high\nbut terminal status", "done",
         0, 0),
    ]
    for tid, title, body, status, hb, started in tasks:
        conn.execute(
            "INSERT INTO tasks (id, title, body, status, assignee, created_at, "
            "started_at, last_heartbeat_at) "
            "VALUES (?, ?, ?, ?, 'pr-nanogpt', 0, ?, ?)",
            (tid, title, body, status, started, hb),
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
         mock.patch.object(mod, "model_cost_context", ledger_frozen,
                           create=True), \
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

    # The ONLY allowed differences: the additive keys (OBJ-18 S1 census +
    # OBJ-21 zombie guard).  Both are additive context keys; stripping
    # them must leave the OLD recommendation output byte-identical.
    # Stripped from BOTH sides: with a pre-S1 baseline the old output
    # carries none of them (nothing removed), but with a post-S1 baseline
    # (e.g. QUOTA_GATE_BASE_REF=6d73746, the Sep-8 worker-model migration
    # used to verify the zombie guard in isolation) the old output already
    # carries privacy_summary — stripping only from new would show a
    # spurious "key removed" hunk.
    def _strip_additive(out):
        ctx = dict(out.get("context", {}))
        stripped = {}
        for additive_key in ("privacy_summary", "zombie_check"):
            val = ctx.pop(additive_key, None)
            if val is not None:
                stripped[additive_key] = val
        out2 = dict(out)
        out2["context"] = ctx
        return out2, stripped

    old_stripped, old_extra = _strip_additive(old_out)
    new_stripped, new_extra = _strip_additive(new_out)
    stripped = dict(old_extra)
    stripped.update(new_extra)

    old_s = json.dumps(old_stripped, sort_keys=True, indent=2)
    new_s = json.dumps(new_stripped, sort_keys=True, indent=2)

    print("=" * 70)
    print("OLD output (HEAD, keys sorted):")
    print(old_s)
    print("=" * 70)
    print("NEW output (additive keys stripped):")
    print(new_s)
    print("=" * 70)

    for key, val in stripped.items():
        print(f"NEW additive field {key}:", json.dumps(val, sort_keys=True))

    os.unlink(kanban_db)
    os.unlink(old_path)

    if old_s == new_s:
        print("\nVERDICT: IDENTICAL — recommendation output unchanged.")
        # sanity: 4 active untagged tasks → all-none; the done task with
        # privacy:high must NOT be counted (terminal status).
        expected = {"high": 0, "medium": 0, "low": 0, "none": 4}
        summary = stripped.get("privacy_summary")
        if summary != expected:
            print(f"WARNING: privacy_summary unexpected: {summary} "
                  f"(expected {expected})")
            return 1
        print(f"privacy_summary sanity: {summary} == expected {expected} OK")
        # OBJ-21 sanity: the untagged board has NO running tasks →
        # the zombie guard must report zero zombies (fail-open board).
        zombie = stripped.get("zombie_check")
        if zombie is None:
            print("WARNING: zombie_check additive key missing")
            return 1
        if zombie.get("has_zombie") or zombie.get("count") != 0:
            print(f"WARNING: zombie_check unexpected on quiet board: {zombie}")
            return 1
        print(f"zombie_check sanity: no zombies on the quiet board OK "
              f"(threshold {zombie.get('threshold_minutes')} min)")
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
