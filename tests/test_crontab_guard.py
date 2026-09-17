#!/usr/bin/env python3
"""test_crontab_guard.py — regression tests for scripts/crontab-guard.sh
(t_da32e7f9: fuse-vs-REPLACE protection for the user crontab).

History: on 2026-09-15 01:25:05 CEST an ad-hoc deploy (obs-shipper, work
under t_a9319828) installed a single-job crontab with `crontab <file>` —
a REPLACE that dropped 9 of 10 user jobs for 32 minutes (watchdog,
obs-serve, cron-health-check all dead together). The deploy step was never
persisted as a script, so there was nothing to fix; this guard is the
persistent protection instead:

  1. `install <file>` must MERGE (never REPLACE): existing lines survive
     verbatim, deploy-file lines are added without duplicates.
  2. `install` always leaves a pre-deploy baseline snapshot.
  3. `canary` alarms (cron-alarms.jsonl) AND restores the previous crontab
     when the active-job count shrinks — the t_da32e7f9 acceptance test.
  4. Additions never trigger the canary (only strict decreases do).
  5. DIRECCION-STOP: alarm yes, restore no.
  6. Comment/blank-only crontabs count as 0 jobs (count = active lines).
  7. Healthy canary runs refresh timestamped snapshots; the pre-deploy
     baseline stays pinned (replay-protection for job-count oscillation).

Design: the test drives the REAL script with a FAKE crontab (a shell
function shim is impossible through subprocess, so a fake `crontab`
binary lives first in PATH; it stores the crontab in a temp file) and a
redirected CRONTAB_BACKUP_DIR / CRONTAB_ALARM_FILE / STOP file — nothing
touches the host crontab. On a fresh clone the script must still exist
(it is versioned), so there is no skip path: CI runs this test too.

Usage:
  pytest tests/test_crontab_guard.py
  python3 tests/test_crontab_guard.py
"""
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "crontab-guard.sh"

FAKE_CRONTAB = r"""#!/usr/bin/env bash
# test fake: crontab state kept in $CRONTAB_FAKE_STATE (one file, plain text)
set -u
STATE="${CRONTAB_FAKE_STATE:?}"
case "${1:-}" in
  -l) cat "$STATE" 2>/dev/null || exit 1 ;;
  "") cat > "$STATE" ;;
  -r) rm -f "$STATE" ;;
  *)  [ -f "$1" ] && cat "$1" > "$STATE" ;;
esac
"""


def _make_fake_crontab(bin_dir: Path) -> Path:
    p = bin_dir / "crontab"
    p.write_text(FAKE_CRONTAB)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return p


class Guard:
    """One isolated crontab-guard sandbox (fake crontab + dirs + env)."""

    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="crontab-guard-test-"))
        self.home = self.tmp / "home"
        self.bin_dir = self.tmp / "bin"
        self.backup = self.tmp / "backups" / "crontab"
        self.alarm_file = self.tmp / "logs" / "cron-alarms.jsonl"
        self.state = self.tmp / "crontab.state"
        self.bin_dir.mkdir(parents=True)
        self.home.mkdir(parents=True)
        self.state.write_text("")
        _make_fake_crontab(self.bin_dir)

    def env(self, **extra):
        e = dict(os.environ)
        e.update({
            "PATH": f"{self.bin_dir}:{os.environ['PATH']}",
            # Pin the guard to the fake binary: the guard defaults its
            # CRONTAB_BIN to /usr/bin/crontab (hardcoded) and must never
            # reach the HOST crontab from a test.
            "CRONTAB_BIN": str(self.bin_dir / "crontab"),
            "CRONTAB_GUARD_HOME": str(self.home),
            "CRONTAB_BACKUP_DIR": str(self.backup),
            "CRONTAB_ALARM_FILE": str(self.alarm_file),
            "CRONTAB_FAKE_STATE": str(self.state),
            "CRONTAB_GUARD_STOP_FILE": str(
                self.home / ".hermes/profiles/pr-ollama/quota-governor/STOP"
            ),
        })
        e.update(extra)
        return e

    def run(self, *args, **extra):
        return subprocess.run(
            ["bash", str(SCRIPT), *args],
            capture_output=True, text=True, env=self.env(**extra), timeout=60,
        )

    def crontab_text(self) -> str:
        return self.state.read_text()

    def active_count(self) -> int:
        return sum(
            1 for ln in self.crontab_text().splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        )

    def read_alarms(self):
        if not self.alarm_file.exists():
            return []
        return [json.loads(ln) for ln in self.alarm_file.read_text().splitlines()]

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


BASE = (
    "# Monitoreo de espacio\n"
    "*/30 * * * * /home/iinstances/space_monitor.sh\n"
    "*/5 * * * * /home/iinstances/.hermes/scripts/kanban-watchdog.sh\n"
    "*/15 * * * * /home/iinstances/.hermes/scripts/cron-health-check.sh\n"
)

NEW_JOB = (
    "# t_9e457672: shipper dual ES+OO de logs del sistema\n"
    "*/5 * * * * /usr/bin/python3.12 $HOME/.hermes/obs-shipper/log-shipper.py\n"
)


def test_install_merges_instead_of_replace():
    g = Guard()
    try:
        g.state.write_text(BASE)
        deploy = g.tmp / "deploy.crontab"
        deploy.write_text(NEW_JOB)
        r = g.run("install", str(deploy))
        assert r.returncode == 0, r.stderr
        text = g.crontab_text()
        for ln in BASE.splitlines():
            assert ln in text, f"existing line lost: {ln!r}"
        assert "log-shipper.py" in text, "new deploy line missing"
        # no duplication of the pre-existing lines
        assert text.count("kanban-watchdog.sh") == 1
        # pre-deploy baseline written with the OLD content
        pre = g.backup / "pre-deploy.crontab"
        assert pre.is_file()
        assert "log-shipper.py" not in pre.read_text()
        assert "kanban-watchdog.sh" in pre.read_text()
        assert (g.backup / "pre-deploy.count").read_text().strip() == "3"
        assert re.search(r"\d{8}-\d{6}\.crontab", "\n".join(
            p.name for p in g.backup.glob("*.crontab")))
        assert "merged install ok (4 active jobs" in r.stdout
    finally:
        g.cleanup()


def test_canary_restores_after_destructive_replace():
    """THE t_da32e7f9 acceptance test: simulate the 2026-09-15 REPLACE deploy
    (crontab collapsed to a single job) and verify the canary alarms AND
    restores the previous crontab."""
    g = Guard()
    try:
        g.state.write_text(BASE)
        deploy = g.tmp / "deploy.crontab"
        deploy.write_text(NEW_JOB)
        assert g.run("install", str(deploy)).returncode == 0
        assert g.active_count() == 4
        # destructive REPLACE deploy: 1 job survives, 3 vanish
        g.state.write_text(
            "*/5 * * * * /usr/bin/python3.12 $HOME/.hermes/obs-shipper/log-shipper.py\n"
        )
        r = g.run("canary")
        assert r.returncode == 0, r.stderr
        restored = g.crontab_text()
        for marker in ("space_monitor.sh", "kanban-watchdog.sh",
                       "cron-health-check.sh", "log-shipper.py"):
            assert marker in restored, f"restore lost: {marker}"
        kinds = [a["status"] for a in g.read_alarms()]
        assert "JOBS_LOST" in kinds, g.read_alarms()
        assert "RESTORED" in kinds, g.read_alarms()
        entry = next(a for a in g.read_alarms() if a["status"] == "JOBS_LOST")
        assert entry["cron"] == "crontab-guard"
        assert "3 -> 1" in entry["detail"]
    finally:
        g.cleanup()


def test_canary_ignores_additions_and_idempotent_after_restore():
    g = Guard()
    try:
        g.state.write_text(BASE)
        deploy = g.tmp / "deploy.crontab"
        deploy.write_text(NEW_JOB)
        assert g.run("install", str(deploy)).returncode == 0
        # a benign growth (manual addition): must NOT alarm
        assert g.run("canary").returncode == 0
        assert g.read_alarms() == []
        # restore path is idempotent under oscillation: two successive
        # destructive REPLACEs each lose jobs, each restore re-fuses the
        # surviving lines (baseline never moves, so it re-alarms and
        # re-restores — that is the designed behaviour).
        g.state.write_text("*/5 * * * * /bin/only-one\n")
        assert g.run("canary").returncode == 0
        # union restore: pre-deploy baseline (3) + the surviving deployer line
        assert g.active_count() == 4, g.crontab_text()
        g.state.write_text("")  # second loss: nothing survives
        assert g.run("canary").returncode == 0
        # union restore: pre-deploy baseline (3) + no survivors
        assert g.active_count() == 3, g.crontab_text()
        # healthy tick after restoration: OK, baseline untouched
        assert g.run("canary").returncode == 0
        assert "canary OK (3 >= 3" in g.run("canary").stdout
        kinds = [a["status"] for a in g.read_alarms()]
        assert kinds.count("JOBS_LOST") == 2 and "RESTORED" in kinds
    finally:
        g.cleanup()


def test_canary_stop_file_alarms_without_restore():
    g = Guard()
    try:
        g.state.write_text(BASE)
        assert g.run("snapshot").returncode == 0
        g.state.write_text("*/5 * * * * /bin/only-one\n")
        stop = g.home / ".hermes/profiles/pr-ollama/quota-governor/STOP"
        stop.parent.mkdir(parents=True, exist_ok=True)
        stop.write_text("")
        r = g.run("canary")
        assert r.returncode == 0, r.stderr
        assert "DIRECCION-STOP" in r.stdout
        assert g.active_count() == 1, "restore must NOT happen under STOP"
        kinds = [a["status"] for a in g.read_alarms()]
        assert "JOBS_LOST" in kinds and "RESTORED" not in kinds
    finally:
        g.cleanup()


def test_canary_bootstraps_and_counts_only_active_lines():
    g = Guard()
    try:
        g.state.write_text("# only comments\n\n")
        r = g.run("canary")  # no baseline, no backups -> bootstrap snapshot
        assert r.returncode == 0, r.stderr
        assert "first snapshot" in r.stdout
        assert g.read_alarms() == []
        assert (g.backup / "pre-deploy.count").read_text().strip() == "0"
        # comments/blanks added later still count as 0: no false alarm
        g.state.write_text("# only comments\n\n# more noise\n")
        assert g.run("canary").returncode == 0
        assert g.read_alarms() == []
    finally:
        g.cleanup()


def test_canary_falls_back_to_newest_backup_without_baseline():
    g = Guard()
    try:
        g.state.write_text(BASE)
        assert g.run("snapshot").returncode == 0
        os.remove(g.backup / "pre-deploy.crontab")
        os.remove(g.backup / "pre-deploy.count")
        g.state.write_text("*/5 * * * * /bin/only-one\n")
        r = g.run("canary")
        assert r.returncode == 0, r.stderr
        # union restore: baseline 3 jobs + the 1 surviving deployer line
        assert g.active_count() == 4, g.crontab_text()
        kinds = [a["status"] for a in g.read_alarms()]
        assert "JOBS_LOST" in kinds and "RESTORED" in kinds
    finally:
        g.cleanup()


def test_script_is_executable_and_deploys_match_repo():
    """Guard script exists in repo AND every deployed copy is byte-identical
    (same drift rule the bridge and quota-governor-tick enforce)."""
    assert SCRIPT.is_file()
    mode = SCRIPT.stat().st_mode
    assert mode & stat.S_IXUSR, "crontab-guard.sh must be executable"
    for deployed in (
        Path.home() / ".hermes/scripts/crontab-guard.sh",
        Path.home() / ".hermes/profiles/pr-ollama/scripts/crontab-guard.sh",
    ):
        if not deployed.exists():
            continue
        assert deployed.read_bytes() == SCRIPT.read_bytes(), (
            f"{deployed} DRIFTED from repo copy — redeploy: "
            f"cp {SCRIPT} {deployed}"
        )
        assert deployed.stat().st_mode & stat.S_IXUSR


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"--- {name}")
            try:
                fn()
                print("    PASS")
            except AssertionError as exc:
                failures += 1
                print(f"    FAIL: {exc}")
    print(f"\nResults: {7 - failures} passed, {failures} failed")
    sys.exit(1 if failures else 0)
