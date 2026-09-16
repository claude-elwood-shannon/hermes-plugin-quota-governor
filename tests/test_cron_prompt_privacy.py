#!/usr/bin/env python3
"""test_cron_prompt_privacy.py — regression test: the ACTIVE cron prompt must
wire OBJ-18 Phase 4 (privacy-aware routing).

History: the PRIVACY-AWARE ROUTING section was added to the
autonomous-task-creator prompt in t_cb74a79f (Sep 2 2026) but a later prompt
rewrite (Sep 7 2026 guardrails/cost-tag work) replaced the whole prompt and
silently dropped the section — the 24-test verification in the matrix ran
against the then-current prompt and was never persisted as a regression test.
Result: end-to-end privacy routing via cron broke for days unnoticed.

This test reads the LIVE jobs.json of the pr-ollama profile (never a
snapshot/copy) and fails if the wiring is lost again.

Checks:
  1. jobs.json exists and parses
  2. autonomous-task-creator job present, enabled, quota-gate.py pre-run script
  3. prompt contains the PRIVACY-AWARE ROUTING (OBJ-18 Phase 4) section
  4. prompt instructs the two-phase gate: privacy-gate.sh re-run
  5. prompt mandates the privacy: tag in the task body (high AND low)
  6. prompt carries the privacy_routed tracking format
  7. prompt handles the confidential/wakeAgent:false case
  8. privacy-gate.sh deployed in the profile scripts dir and executable
  9. no enabled shadow clones of the creator in ANY profile's jobs.json
"""
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
GATE_SH = os.path.join(HOME, ".hermes/profiles/pr-ollama/scripts/privacy-gate.sh")


def main():
    """Run the OBJ-18 Phase 4 cron-prompt wiring checks. Returns 0 (ok) / 1 (failed).

    pytest collection safety: reads LIVE cron jobs.json and the
    deployed scripts — bails out quietly (rc 0) on fresh clones/CI
    where they do not exist. Runs only on explicit invocation now;
    the module-level sys.exit/SystemExit that killed pytest
    collection with INTERNALERROR is gone.
    """
    print("--- Test: active cron prompt wires OBJ-18 Phase 4 (privacy routing) ---")

    # 1. jobs.json exists and parses
    jobs_path = next((p for p in CANDIDATE_JOBS if os.path.isfile(p)), None)
    check("jobs.json found", jobs_path is not None, f"tried {CANDIDATE_JOBS}")
    if jobs_path is None:
        # the LIVE cron config of this host is the subject under test; on a
        # fresh clone / CI it does not exist — skip instead of failing
        loud = os.environ.get("QUOTA_GOVERNOR_EXPECT_DEPLOYED") == "1"
        print("SKIP: no live cron jobs.json (fresh clone / CI)" + ("" if loud else " — host-only test"))
        return 1 if loud else 0

    try:
        data = json.load(open(jobs_path))
        check("jobs.json parses as JSON", True)
    except Exception as e:  # pragma: no cover
        check("jobs.json parses as JSON", False, str(e))
        print(f"\nResults: {passed} passed, {failed} failed")
        return 1

    jobs = data.get("jobs", [])
    # 2. job present, enabled, script wired
    job = next((j for j in jobs if j.get("name") == "autonomous-task-creator"), None)
    check("autonomous-task-creator job exists", job is not None)
    if job is None:
        print(f"\nResults: {passed} passed, {failed} failed")
        return 1

    check("autonomous-task-creator enabled", bool(job.get("enabled")))
    check("pre-run script is quota-gate.py",
          job.get("script") == "quota-gate.py", f"script={job.get('script')!r}")

    prompt = job.get("prompt") or ""

    # 3-7. prompt markers (exact strings a regression must not drop)
    markers = [
        ("PRIVACY-AWARE ROUTING (OBJ-18 Phase 4", "section header present"),
        ("privacy-gate.sh", "two-phase gate: privacy-gate.sh re-run instructed"),
        ("privacy:high", "privacy:high tag mandated"),
        ("privacy:low", "privacy:low tag mandated"),
        ("privacy:confidential", "confidential case handled"),
        ("wakeAgent:false", "wakeAgent:false guidance present"),
        ("privacy_routed:", "privacy_routed tracking format present"),
        ("LANGUAGE RULE", "language rule present (all task text in Spanish)"),
        ("correct spelling and accents", "Spanish spelling/acents requirement explicit"),
    ]
    for marker, desc in markers:
        check(desc, marker in prompt,
              f"(marker {marker!r} missing from active prompt, len={len(prompt)})")

    # The section must sit near the guardrails, not truncated at the end
    _sec = prompt.find("PRIVACY-AWARE ROUTING")
    _g5 = prompt.find("G5. TASK NAMING")
    check("section is followed by guardrails (prompt not truncated)",
          _sec != -1 and _g5 != -1 and _sec < _g5,
          f"(section idx={_sec}, G5 idx={_g5})")

    # 8. privacy-gate.sh deployed and executable
    check("privacy-gate.sh deployed", os.path.isfile(GATE_SH), GATE_SH)
    check("privacy-gate.sh executable", os.access(GATE_SH, os.X_OK), GATE_SH)

    # 9. no shadow clones of the creator in any profile (t_628ed14a, Sep 8 2026):
    # a prompt-rewrite on Sep 7 programmatically cloned the creator into the
    # pr-nanogpt profile (job 0b9a2b17116f-nanogpt) without the PRIVACY-AWARE
    # section; it created fake "evidence" tasks and duplicated board-feeding.
    # The design is ONE multi-profile creator (pr-ollama); a second creator
    # anywhere is a regression.
    import glob

    clone_jobs = []
    for jobs_path in glob.glob(os.path.join(HOME, ".hermes/profiles/*/cron/jobs.json")):
        try:
            with open(jobs_path) as fh:
                data = json.load(fh)
            for j in data.get("jobs", []):
                name = j.get("name") or ""
                jid = j.get("id") or ""
                if j.get("enabled") and (
                    name.startswith("autonomous-task-creator")
                    and name != "autonomous-task-creator"
                    or jid.startswith("0b9a2b17116f-")
                ):
                    clone_jobs.append(f"{jobs_path}:{jid}")
        except Exception:
            pass
    check("no enabled shadow clones of the creator in any profile",
          not clone_jobs, f"(found: {clone_jobs})")

    print(f"\nResults: {passed} passed, {failed} failed")

    print(f"\nResults: {passed} passed, {failed} failed")
    return 1 if failed else 0


# ── Tests: pytest wrapper (canonical batch) ─────────────────────────────────
def test_full_suite():
    """Run the OBJ-18 Phase 4 cron-prompt wiring checks; assert 0 failed (or skip on fresh clone)."""
    import re as _re
    import subprocess as _sp
    import os as _os
    _here = _os.path.dirname(_os.path.abspath(__file__))
    _self = _os.path.join(_here, "test_cron_prompt_privacy.py")
    result = _sp.run(
        [sys.executable, _self],
        capture_output=True, text=True, timeout=120,
    )
    print(result.stdout[-2000:] if result.stdout else "")
    print(result.stderr[-2000:] if result.stderr else "")
    skip_marker = [ln for ln in result.stdout.splitlines() if ln.startswith("SKIP:")]
    if result.returncode == 0 and skip_marker:
        print(skip_marker[0])
        return  # host-only checks; deployed files absent here
    assert result.returncode == 0, (
        "self-run failed (rc=%d); see captured output above" % result.returncode
    )
    summary = [ln for ln in result.stdout.splitlines() if "passed," in ln]
    assert summary, "summary line with 'passed,' not found in output"
    m = _re.search(r"(\d+) passed, (\d+) failed", summary[-1])
    assert m and m.group(2) == "0", "unexpected: " + summary[-1]


if __name__ == "__main__":
    sys.exit(main())
