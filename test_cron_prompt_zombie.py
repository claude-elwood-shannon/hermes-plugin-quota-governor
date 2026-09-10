#!/usr/bin/env python3
"""test_cron_prompt_zombie.py — OBJ-21 regression test: the ACTIVE cron prompt
must document the deterministic zombie guard and keep G3 as defense in depth.

Reads the LIVE jobs.json of the pr-ollama profile (never a snapshot/copy):
  1. jobs.json exists and parses
  2. autonomous-task-creator job present, enabled, quota-gate.py pre-run script
  3. prompt documents the gate-enforced zombie_check (defense in depth)
  4. prompt keeps the manual 45-minute fallback instruction
  5. prompt keeps the wakeAgent:false → [SILENT] rule
  6. the deployed quota-gate.py (repo copy) actually carries the guard
     (compute_zombie_check + ZOMBIE_RUNNING_MINUTES constants)
  7. deployed copies match the repo copy (OBJ-06 deploy-drift, md5)

Run:  /usr/bin/python3.12 test_cron_prompt_zombie.py
"""
import hashlib
import json
import os
import sys

passed = 0
failed = 0


def check(desc, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS: {desc}")
    else:
        failed += 1
        print(f"  FAIL: {desc} {detail}")


HOME = os.path.expanduser("~")
CANDIDATE_JOBS = [
    os.path.join(HOME, ".hermes/profiles/pr-ollama/cron/jobs.json"),
    os.path.join(HOME, ".hermes/cron/jobs.json"),
]
REPO = os.path.dirname(os.path.abspath(__file__))
REPO_GATE = os.path.join(REPO, "scripts", "quota-gate.py")
DEPLOY_CANDIDATES = [
    os.path.join(HOME, ".hermes/scripts/quota-gate.py"),
    os.path.join(HOME, ".hermes/profiles/pr-ollama/scripts/quota-gate.py"),
    os.path.join(HOME, ".hermes/profiles/pr-nanogpt/scripts/quota-gate.py"),
]

print("--- Test: active cron prompt wires OBJ-21 (deterministic zombie guard) ---")

# 1. jobs.json exists and parses
jobs_path = next((p for p in CANDIDATE_JOBS if os.path.isfile(p)), None)
check("jobs.json found", jobs_path is not None, f"tried {CANDIDATE_JOBS}")
if jobs_path is None:
    # the LIVE cron config of this host is the subject under test; on a
    # fresh clone / CI it does not exist — skip instead of failing
    loud = os.environ.get("QUOTA_GOVERNOR_EXPECT_DEPLOYED") == "1"
    print("SKIP: no live cron jobs.json (fresh clone / CI)" + ("" if loud else " — host-only test"))
    sys.exit(1 if loud else 0)

try:
    data = json.load(open(jobs_path))
    check("jobs.json parses as JSON", True)
except Exception as e:  # pragma: no cover
    check("jobs.json parses as JSON", False, str(e))
    print(f"\nResults: {passed} passed, {failed} failed")
    sys.exit(1)

jobs = data.get("jobs", [])
job = next((j for j in jobs if j.get("name") == "autonomous-task-creator"), None)
check("autonomous-task-creator job exists", job is not None)
if job is None:
    print(f"\nResults: {passed} passed, {failed} failed")
    sys.exit(1)

check("autonomous-task-creator enabled", bool(job.get("enabled")))
check("pre-run script is quota-gate.py",
      job.get("script") == "quota-gate.py", f"script={job.get('script')!r}")

prompt = job.get("prompt") or ""

# 3-5. prompt markers (exact strings a regression must not drop)
markers = [
    ("ZOMBIE CHECK", "G3 zombie check section present"),
    ("defense in depth", "prompt documents the deterministic gate enforcement"),
    ("zombie_check.has_zombie", "prompt references the gate's zombie_check field"),
    ("OBJ-21", "prompt cites OBJ-21"),
    ("[SILENT]", "SILENT rule present"),
    ("45 minutes", "manual 45-minute fallback kept"),
]
for marker, desc in markers:
    check(desc, marker in prompt,
          f"(marker {marker!r} missing from active prompt, len={len(prompt)})")

# 6. the gate source carries the guard
repo_src = ""
try:
    with open(REPO_GATE) as fh:
        repo_src = fh.read()
    check("repo quota-gate.py readable", True)
except Exception as e:  # pragma: no cover
    check("repo quota-gate.py readable", False, str(e))
for marker, desc in [
    ("def compute_zombie_check", "guard function present in repo gate"),
    ("ZOMBIE_RUNNING_MINUTES = 45.0", "45-minute threshold constant present"),
    ("zombie_guard:", "gate warning tag present"),
    ('"zombie_check": zombie_check', "context wiring present"),
]:
    check(desc, marker in repo_src)

# 7. deployed copies match the repo copy (deploy-drift style, md5)
def md5_of(path):
    with open(path, "rb") as fh:
        return hashlib.md5(fh.read()).hexdigest()


repo_md5 = hashlib.md5(repo_src.encode()).hexdigest()
deployed = [p for p in DEPLOY_CANDIDATES if os.path.isfile(p)]
check("at least one deployed copy exists", bool(deployed),
      "(expected ~/.hermes/scripts and profile scripts copies)")
for p in deployed:
    check(f"deployed copy matches repo: {p}", md5_of(p) == repo_md5)

print(f"\nResults: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)