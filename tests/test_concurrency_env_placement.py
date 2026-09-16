"""Regression test: CONCURRENCY_DESIRED_MAX must be an env-prefix assignment
on the same command line as `python3 -c`, never a trailing argv argument.

Root cause (t_52bb6b43): the tick script passed
    CONCURRENCY_DESIRED_MAX="$DESIRED_MAX" AFTER the python3 -c string,
    which made it argv[1] of python instead of an environment variable.
    The guard then always fell back to desired_max=1 → hard_limit=3 and
    SIGTERM'd healthy workers whenever 4-6 accumulated (9 kills on
    2026-09-10 with quota healthy).

The test greps the tick script the same way bash would execute it:
- the assignment must appear in an environment-prefix block that
  terminates in `python3 -c "` (same command, backslash-continued);
- `python3 -c "` must never be followed on the same line by
  CONCURRENCY_DESIRED_MAX= (the argv bug).
"""

import re
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "scripts" / "quota-governor-tick.sh"

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
    print("    (regression: CONCURRENCY_DESIRED_MAX env-prefix placement in "
          "scripts/quota-governor-tick.sh)")


def guard_command_prefix(lines):
    """Collect the env-prefix lines of the guard's CONCURRENCY_OUTPUT=$(...)

    command block: starts at the line that begins with CONCURRENCY_OUTPUT=$(
    and continues through every backslash-continued line (bash logical line).
    Returns list of (line_number, text), last element terminating the prefix.
    """
    start = next(i for i, l in enumerate(lines)
                 if l.startswith("CONCURRENCY_OUTPUT=$("))
    block = []
    j = start
    while True:
        ln = lines[j]
        block.append((j + 1, ln))
        if ln.rstrip().endswith("\\") and j + 1 < len(lines):
            j += 1
            continue
        break
    return block


def main():
    if not SCRIPT.exists():
        fail(f"tick script not found at {SCRIPT}")
        print(f"\nResults: {PASS} passed, {FAIL} failed")
        sys.exit(1)

    text = SCRIPT.read_text()
    lines = text.splitlines()

    # --- Check 1: assignment is part of the env-prefix of python3 -c -------
    # In bash, `VAR=v1 VAR=v2 ... python3 -c "<code>"` puts the vars in the
    # child's environment. The guard block is backslash-continued, so the
    # prefix must set CONCURRENCY_DESIRED_MAX on one of those lines and the
    # prefix must terminate with the `python3 -c "` invocation.
    block = guard_command_prefix(lines)
    last_line = block[-1][1]
    if any("CONCURRENCY_DESIRED_MAX=" in ln for _, ln in block) and \
            "python3 -c" in last_line:
        ok("CONCURRENCY_DESIRED_MAX is set inside the CONCURRENCY_OUTPUT=$(...) "
           "env-prefix, which terminates with python3 -c")
    else:
        fail("CONCURRENCY_DESIRED_MAX is not an env-prefix assignment on the "
             "python3 -c command in the guard block")

    # --- Check 2: never passed as trailing argv after python3 -c ----------
    # The argv bug looked like:   python3 -c "..." CONCURRENCY_DESIRED_MAX=...
    # (closing quote of the -c string, then the assignment on the same line —
    # bash passes it as argv[1] of python, invisible to os.environ).
    argv_bug = re.compile(r'"\s*CONCURRENCY_DESIRED_MAX=')
    offenders = [i + 1 for i, ln in enumerate(lines) if argv_bug.search(ln)]
    if offenders:
        fail(f"CONCURRENCY_DESIRED_MAX passed as trailing argv (python3 -c "
             f"string followed by the assignment) at line(s): {offenders}")
    else:
        ok("No trailing-argv placement of CONCURRENCY_DESIRED_MAX after the "
           "python3 -c string")

    # --- Check 3: inline python reads it from os.environ -------------------
    env_read = re.compile(
        r"os\.environ\.get\(\s*'CONCURRENCY_DESIRED_MAX'", re.M)
    if env_read.search(text):
        ok("Inline guard python reads CONCURRENCY_DESIRED_MAX from os.environ")
    else:
        fail("Inline guard python does not read CONCURRENCY_DESIRED_MAX from "
             "os.environ (contract changed?)")

    # --- Check 4: live functional probe ------------------------------------
    # Prove the bash env-prefix semantics the guard depends on: assignment
    # before the command lands in os.environ; trailing assignment does not.
    import os
    probe_ok = subprocess.run(
        ["bash", "-c",
         'CONCURRENCY_DESIRED_MAX="7" python3 -c "import os; '
         "print(os.environ.get('CONCURRENCY_DESIRED_MAX','MISSING'))\"",
         ],
        capture_output=True, text=True,
    )
    probe_bug = subprocess.run(
        ["bash", "-c",
         'python3 -c "import os; '
         "print(os.environ.get('CONCURRENCY_DESIRED_MAX','MISSING'))\" "
         'CONCURRENCY_DESIRED_MAX="7"',
         ],
        capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": ""},
    )
    if probe_ok.stdout.strip() == "7":
        ok(f"Functional probe: env prefix reaches python os.environ "
           f"(got {probe_ok.stdout.strip()!r})")
    else:
        got = probe_ok.stdout.strip()
        fail(f"Functional probe failed: got {got!r}, expected '7' "
             f"(stderr: {probe_ok.stderr.strip()[:200]})")
    if probe_bug.stdout.strip() == "MISSING":
        ok("Functional probe: trailing argv placement does NOT reach "
           "os.environ (documents the original bug)")
    else:
        got = probe_bug.stdout.strip()
        fail(f"Bug-shape probe unexpectedly visible in env: {got!r}")

    print(f"\nResults: {PASS} passed, {FAIL} failed")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
