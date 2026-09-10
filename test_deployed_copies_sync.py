"""Regression test: every deployed copy of quota-governor-tick.sh must be
byte-identical to the repo copy (the one that is versioned and published).

Why: the tick has up to THREE copies on this host (repo, shared
~/.hermes/scripts/, profile ~/.hermes/profiles/pr-ollama/scripts/). The
multiplex cron ticker resolves the job's `script:` filename against the
PROFILE's scripts dir, so a stale profile copy silently keeps a fixed bug
alive in production (2026-09-10: repo+shared fixed, profile copy still
carried the CONCURRENCY_DESIRED_MAX argv bug — desired stayed pinned at 1).

This test:
  1. hashes the repo copy;
  2. for each candidate deployed path that EXISTS, asserts the md5 matches
     the repo copy and bash -n passes;
  3. if NO deployed copy exists yet (fresh clone / new host), asserts that
     is reported loudly instead of passing silently.
"""
import hashlib
import subprocess
import sys
from pathlib import Path

REPO_SCRIPT = Path(__file__).resolve().parent / "scripts" / "quota-governor-tick.sh"

CANDIDATE_DEPLOYED = [
    Path.home() / ".hermes" / "scripts" / "quota-governor-tick.sh",
    Path.home() / ".hermes" / "profiles" / "pr-ollama" / "scripts" / "quota-governor-tick.sh",
]

PASS = 0
FAIL = 0


def ok(msg):
    global PASS
    PASS += 1
    print(f"  PASS: {msg}")


def bad(msg):
    global FAIL
    FAIL += 1
    print(f"  FAIL: {msg}")


def md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    if not REPO_SCRIPT.exists():
        bad(f"repo script missing: {REPO_SCRIPT}")
        return 1
    ref = md5(REPO_SCRIPT)
    ok(f"repo copy md5 {ref}")

    found = []
    for cand in CANDIDATE_DEPLOYED:
        if not cand.exists():
            continue
        found.append(cand)
        if cand.stat().st_mode & 0o111 == 0:
            bad(f"{cand} is not executable")
        else:
            ok(f"{cand} is executable")
        if md5(cand) == ref:
            ok(f"{cand.name} ({cand.parent.parent.name}) matches repo md5")
        else:
            bad(f"{cand} DRIFTED from repo copy — redeploy: cp {REPO_SCRIPT} {cand}")
        r = subprocess.run(
            ["bash", "-n", str(cand)], capture_output=True, text=True
        )
        if r.returncode == 0:
            ok(f"bash -n {cand}")
        else:
            bad(f"bash -n {cand}: {r.stderr.strip()}")

    if not found:
        bad(
            "no deployed copy found (checked: "
            + ", ".join(str(c) for c in CANDIDATE_DEPLOYED)
            + ") — cron has nothing to run; deploy the repo copy"
        )
    print(f"\nResults: {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
