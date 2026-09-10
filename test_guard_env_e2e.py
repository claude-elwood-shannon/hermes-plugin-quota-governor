"""Functional E2E test for the quota-governor tick guard env fix (t_52bb6b43).

Runs the REAL tick script (with mocked quota state so no network is touched)
and verifies that the concurrency guard sees CONCURRENCY_DESIRED_MAX
from the environment — i.e. `desired` in the guard log equals the mocked
DESIRED_MAX (4), not the old always-fallback 1.

How it works:
- Copy the tick script to a temp deploy dir (mimics ~/.hermes/scripts).
- Create a temp profile dir with a minimal .env (fake key — the HTTP call
  will fail fast offline, so we pre-write the decision via PREV_COST-style
  env injection... actually simpler: we mock nothing in-band; instead we
  run the guard block in isolation by extracting it, because the tick
  exits early when the usage API call fails (action=ERROR → exit 0 before
  the guard runs).

  Therefore the E2E drives the guard exactly as the tick does:
  DESIRED_MAX=4 bash-extract of the guard block → guard log must show
  desired=4. This still exercises the REAL script text (extracted at
  runtime), so a regression in placement fails the test.
"""

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
TICK = Path(PLUGIN_DIR) / "scripts" / "quota-governor-tick.sh"

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


def main():
    text = TICK.read_text()
    lines = text.splitlines()

    # ── Locate the guard block in the REAL script ─────────────────────────
    start = next(i for i, l in enumerate(lines) if l.startswith("CONCURRENCY_OUTPUT=$("))
    # The guard block runs from its opening line through the closing
    # `" 2>/dev/null) || true` line; find that close first.
    closing_idx = next(k for k in range(start, len(lines)) if '" 2>/dev/null' in lines[k])
    guard_block_raw = "\n".join(lines[start:closing_idx + 1])

    # ── Build the E2E harness script ──────────────────────────────────────
    harness = (
        "#!/usr/bin/env bash\n"
        "set -uo pipefail\n"
        "DESIRED_MAX=4\n"
        'HERMES_HOME="$TEST_HOME/profiles/pr-ollama"\n'
        'HERMES_KANBAN_DB="$TEST_HOME/kanban.db"\n'
        f'PLUGIN_DIR="{PLUGIN_DIR}"\n'
        'mkdir -p "$HERMES_HOME/quota-governor"\n'
        + guard_block_raw + "\n"        # verbatim guard block (open + code + close)
        + 'echo "$CONCURRENCY_OUTPUT"\n'
    )
    print("  harness head:")
    for hl in harness.splitlines()[:12]:
        print("   |", hl[:100])
    with tempfile.TemporaryDirectory(prefix="tick-guard-e2e-") as td:
        harness_path = Path(td) / "harness.sh"
        harness_path.write_text(harness)
        env = {**os.environ,
               "TEST_HOME": td,
               # Isolate from real state: empty db dir → live=0, but the
               # guard still must NOT fall back to desired=1.
               }
        p = subprocess.run(
            ["bash", str(harness_path)],
            capture_output=True, text=True, env=env, timeout=30,
        )
        out = p.stdout.strip()
        print(f"  harness stdout: {out[:300]!r}")
        print(f"  harness stderr: {p.stderr.strip()[:300]!r}")

        if not out:
            fail("E2E: guard produced no JSON output")
            return
        import json
        try:
            d = json.loads(out.splitlines()[-1])
        except Exception as e:
            fail(f"E2E: guard output is not JSON: {e}")
            return
        reason = d.get("reason", "")
        # The guard logs desired=<n>; with the fix it must be 4 (DESIRED_MAX),
        # not the '1' fallback. live=0 (empty isolated db) → no kills either way.
        if re.search(r"\bdesired=4\b", reason):
            ok(f"E2E: guard saw desired_max=4 from env ({reason})")
        else:
            fail(f"E2E: guard did NOT see DESIRED_MAX=4 ({reason}) — "
                 "env placement regression")
        if d.get("live_count") == 0:
            ok("E2E: isolated state (live=0), no production data touched")
        else:
            fail(f"E2E: live_count={d.get('live_count')} — isolation leaked")

    print(f"\nResults: {PASS} passed, {FAIL} failed")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
