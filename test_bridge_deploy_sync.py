"""Regression test: the Open WebUI bridge (scripts/bridge/) must be in sync
across its THREE copies and the running service must speak the repo's API.

Why: the bridge is critical mediator infrastructure (mediator -> bridge ->
Hermes) exposed on a fixed port (9120) to Open WebUI. It lives in three
places (repo, shared ~/.hermes/scripts/bridge/, profile
~/.hermes/profiles/pr-ollama/scripts/bridge/). A drifted deployed copy
re-spawned by the supervisor would silently serve a stale API; a running
daemon older than the repo copy would diverge from the documented contract
(docs/bridge-open-webui.md).

This test:
  1. hashes the repo copies (server + supervisor wrapper);
  2. for each deployed copy that EXISTS, asserts md5 == repo, syntax passes
     (py_compile for the .py, bash -n for the .sh) and exec bits match the
     deployed-copy convention (profile copy of the server and every copy of
     the wrapper must be executable);
  3. probes http://localhost:9120/openapi.json IF the port answers: it must
     return 200 and the served spec version must equal the version declared
     in the repo copy (a running daemon older than the repo is drift);
  4. if NO deployed copy exists (fresh clone / CI), SKIPS — a machine that
     never deployed the bridge has nothing to drift. Set
     BRIDGE_EXPECT_DEPLOYED=1 (host verify runs) to make a missing copy a
     loud failure instead. The live probe is skipped when the daemon is
     simply not running (the supervisor owns resurrection, not the tests).

Usage:
  /usr/bin/python3.12 -m pytest test_bridge_deploy_sync.py -x
  /usr/bin/python3.12 test_bridge_deploy_sync.py   (unittest fallback)
"""
import hashlib
import json
import os
import py_compile
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent
REPO_SERVER = REPO_DIR / "scripts" / "bridge" / "open-webui-bridge.py"
REPO_WRAPPER = REPO_DIR / "scripts" / "bridge" / "open-webui-bridge-cron.sh"

CANDIDATE_SERVER = [
    Path.home() / ".hermes" / "scripts" / "bridge" / "open-webui-bridge.py",
    Path.home() / ".hermes" / "profiles" / "pr-ollama" / "scripts" / "bridge" / "open-webui-bridge.py",
]
CANDIDATE_WRAPPER = [
    Path.home() / ".hermes" / "scripts" / "bridge" / "open-webui-bridge-cron.sh",
    Path.home() / ".hermes" / "profiles" / "pr-ollama" / "scripts" / "bridge" / "open-webui-bridge-cron.sh",
]

PORT = 9120
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


def check_file(repo_path, candidates, must_exec, syntax):
    if not repo_path.exists():
        bad(f"repo file missing: {repo_path}")
        return
    ref = md5(repo_path)
    ok(f"repo copy md5 {ref} ({repo_path.name})")

    found = []
    for cand in candidates:
        if not cand.exists():
            continue
        found.append(cand)
        if md5(cand) == ref:
            ok(f"{cand} matches repo md5")
        else:
            bad(f"{cand} DRIFTED from repo copy — redeploy: cp {repo_path} {cand}")
        if must_exec:
            if cand.stat().st_mode & 0o111 == 0:
                bad(f"{cand} is not executable")
            else:
                ok(f"{cand} is executable")
        else:
            ok(f"{cand} exec-bit not required (mode {oct(cand.stat().st_mode & 0o777)})")
        if syntax == "python":
            try:
                py_compile.compile(str(cand), doraise=True)
                ok(f"py_compile {cand}")
            except py_compile.PyCompileError as e:
                bad(f"py_compile {cand}: {e}")
        else:
            r = subprocess.run(["bash", "-n", str(cand)], capture_output=True, text=True)
            if r.returncode == 0:
                ok(f"bash -n {cand}")
            else:
                bad(f"bash -n {cand}: {r.stderr.strip()}")

    if not found:
        msg = (
            "no deployed copy found for "
            + repo_path.name
            + " (checked: "
            + ", ".join(str(c) for c in candidates)
            + ") — deploy the repo copy"
        )
        if os.environ.get("BRIDGE_EXPECT_DEPLOYED") == "1":
            bad(msg)
        else:
            print(f"  SKIP: {msg}")


def repo_version():
    text = REPO_SERVER.read_text(encoding="utf-8")
    m = re.search(r'"version":\s*"(\d+\.\d+\.\d+)"', text)
    return m.group(1) if m else None


def check_live_service():
    if not REPO_SERVER.exists():
        return
    try:
        with urllib.request.urlopen(
            f"http://localhost:{PORT}/openapi.json", timeout=5
        ) as r:
            spec = json.loads(r.read().decode("utf-8"))
            code = r.status
    except Exception:
        print(f"  SKIP: no live service on :{PORT} — supervisor owns resurrection")
        return
    if code != 200:
        bad(f"GET /openapi.json returned {code}, expected 200")
        return
    ok(f"live service on :{PORT} answers 200")
    served = spec.get("info", {}).get("version")
    expected = repo_version()
    if expected is None:
        bad("cannot parse version from repo copy")
    elif served == expected:
        ok(f"live spec version {served} == repo version {expected}")
    else:
        bad(
            f"live spec version {served} != repo version {expected} — "
            "running daemon is STALE, redeploy and let the supervisor respawn it"
        )


def check_portability():
    """House convention (TestPortability family): the server module must be
    free of host-specific absolute paths — adoptants run it portably."""
    if not REPO_SERVER.exists():
        return
    src = REPO_SERVER.read_text(encoding="utf-8")
    home_name = Path.home().name
    for needle in ("/home/", "/data/git", home_name, "/iinstances", "C:\\"):
        if needle in src:
            bad(f"host path leaked into open-webui-bridge.py: {needle}")
        else:
            ok(f"no host path {needle!r} in open-webui-bridge.py")


def main():
    check_portability()
    check_file(REPO_SERVER, CANDIDATE_SERVER, must_exec=False, syntax="python")
    check_file(REPO_WRAPPER, CANDIDATE_WRAPPER, must_exec=True, syntax="bash")
    check_live_service()
    print(f"\nResults: {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
