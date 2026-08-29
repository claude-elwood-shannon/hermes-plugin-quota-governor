"""Health checks for the quota-governor plugin.

Three detections:

1. **Fast burn** — session_usage rises >30 percentage points in <5 minutes.
   Compares the latest observation's session_pct against one recorded
   ~5 min earlier.  A spike >30 points is anomalous and signals that
   workers are consuming quota far faster than expected.

2. **Zombie workers** — kanban workers with >2 h since their last
   heartbeat.  Uses the kanban SQLite DB to find ``running`` tasks whose
   last heartbeat is older than the threshold.

3. **Silent plugin** — no observations in observations.jsonl in the last
   1 h.  If the plugin were loaded and firing hooks, we would see at
   least periodic samples.  Silence means the plugin is not loaded or
   the hooks are not firing.

All alerts are written to ``~/.hermes/logs/quota-governor-alerts.log``
as JSON lines and returned as a list of dicts for the caller (e.g. the
tick script) to include in stdout.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Thresholds (design spec — task OBJ-09)
# ---------------------------------------------------------------------------

FAST_BURN_DELTA_PCT = 30.0       # >30 percentage-point rise
FAST_BURN_WINDOW_MIN = 5          # in less than 5 minutes

ZOMBIE_WORKER_HOURS = 2           # >2 h without heartbeat

SILENT_PLUGIN_HOURS = 1           # no observations in >1 h


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _get_hermes_home() -> Path:
    """Return the active HERMES_HOME, falling back to ~/.hermes.

    This may be profile-specific (e.g. ``~/.hermes/profiles/pr-ollama``)
    when the plugin runs inside a profile context.  Use this for
    profile-scoped data like ``observations.jsonl``.
    """
    val = os.environ.get("HERMES_HOME", "").strip()
    return Path(val).resolve() if val else (Path.home() / ".hermes").resolve()


def _get_hermes_root() -> Path:
    """Return the root Hermes home directory (always ``~/.hermes``).

    This is used for shared, non-profile-scoped resources like the
    kanban DB and the alert log, which live at the root regardless of
    which profile's ``HERMES_HOME`` is active.

    If ``HERMES_HOME`` is *not* set to a profile subdirectory (i.e. it
    *is* ``~/.hermes`` or unset), this returns the same value as
    ``_get_hermes_home()``.
    """
    hermes_home = _get_hermes_home()
    home = Path.home()

    # Common profile subdirectory pattern: ~/.hermes/profiles/<name>
    profiles_root = (home / ".hermes" / "profiles").resolve()
    try:
        hermes_home.relative_to(profiles_root)
        # HERMES_HOME is inside ~/.hermes/profiles/ — return the root
        return (home / ".hermes").resolve()
    except ValueError:
        pass

    # Not a profile path — return as-is
    return hermes_home


def get_alert_log_path() -> Path:
    """Path to the alert log file.

    The alert log is a shared resource that lives at the root
    ``~/.hermes/logs/quota-governor-alerts.log``, not under a
    profile-specific ``HERMES_HOME``.
    """
    return _get_hermes_root() / "logs" / "quota-governor-alerts.log"


def get_observations_file() -> Path:
    """Path to observations.jsonl.

    Observations are profile-scoped: each profile writes to its own
    ``HERMES_HOME/quota-governor/observations.jsonl``.
    """
    return _get_hermes_home() / "quota-governor" / "observations.jsonl"


def get_kanban_db_path() -> Path:
    """Path to the kanban SQLite DB.

    Resolution order (first existing file wins):

    1. ``HERMES_KANBAN_DB`` env var (explicit override)
    2. ``~/.hermes/kanban.db`` (the real shared location)
    3. ``HERMES_HOME/kanban.db`` (fallback for test/isolated environments)

    The kanban DB is a shared resource at the root ``~/.hermes/kanban.db``,
    not under a profile-specific ``HERMES_HOME``.  When the plugin runs
    with ``HERMES_HOME=~/.hermes/profiles/pr-ollama``, the old code
    resolved to ``~/.hermes/profiles/pr-ollama/kanban.db`` (which doesn't
    exist) and the zombie check silently returned ``[]``.
    """
    # 1. Explicit env var override
    env_db = os.environ.get("HERMES_KANBAN_DB", "").strip()
    if env_db:
        return Path(env_db).expanduser().resolve()

    # 2. Root location (the real shared DB)
    root_db = _get_hermes_root() / "kanban.db"
    if root_db.exists():
        return root_db

    # 3. Fallback: HERMES_HOME/kanban.db (for tests with isolated HERMES_HOME)
    return _get_hermes_home() / "kanban.db"


# ---------------------------------------------------------------------------
# Alert writer
# ---------------------------------------------------------------------------

def write_alert(alert_type: str, message: str, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Append a JSON alert to the alert log file.

    Returns the alert dict so callers can collect it for stdout.
    """
    alert: Dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "type": alert_type,
        "message": message,
    }
    if extra:
        alert.update(extra)

    log_path = get_alert_log_path()
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(alert) + "\n")
    except OSError as exc:
        logger.debug("failed to write alert to %s: %s", log_path, exc)

    return alert


# ---------------------------------------------------------------------------
# 1. Fast burn detection
# ---------------------------------------------------------------------------

def _parse_iso(ts: str) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp string (tolerant of trailing 'Z')."""
    if not ts:
        return None
    try:
        # Python <3.11 doesn't handle 'Z' suffix; replace with '+00:00'
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def _read_recent_observations(max_age_minutes: int = 30) -> List[Dict[str, Any]]:
    """Read observations from the last ``max_age_minutes`` minutes."""
    obs_path = get_observations_file()
    if not obs_path.exists():
        return []

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=max_age_minutes)
    results: List[Dict[str, Any]] = []

    try:
        text = obs_path.read_text(encoding="utf-8").strip()
    except OSError:
        return []

    for line in text.split("\n"):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = _parse_iso(entry.get("timestamp", ""))
        if ts and ts >= cutoff:
            results.append(entry)

    return results


def check_fast_burn() -> Optional[Dict[str, Any]]:
    """Detect if session_usage rose >30% in <5 min.

    Reads recent observations and compares the latest session_pct
    against the one recorded ~5 min before it.

    Returns an alert dict if the threshold is exceeded, else None.
    """
    observations = _read_recent_observations(max_age_minutes=15)
    if len(observations) < 2:
        return None

    # Sort by timestamp (oldest first)
    observations.sort(key=lambda e: e.get("timestamp", ""))

    latest = observations[-1]
    latest_ts = _parse_iso(latest.get("timestamp", ""))
    latest_quota = latest.get("quota", {})
    latest_session = latest_quota.get("ollama_session_pct")
    if latest_session is None:
        return None
    latest_session = float(latest_session)

    if latest_ts is None:
        return None

    # Find an observation ~5 min before the latest one
    window_start = latest_ts - timedelta(minutes=FAST_BURN_WINDOW_MIN)

    # Pick the oldest observation within the 5-min window
    earlier_session: Optional[float] = None
    earlier_ts: Optional[datetime] = None
    for obs in observations:
        ts = _parse_iso(obs.get("timestamp", ""))
        if ts is None or ts >= latest_ts:
            continue
        if ts >= window_start:
            quota = obs.get("quota", {})
            sp = quota.get("ollama_session_pct")
            if sp is not None:
                sp = float(sp)
                if earlier_ts is None or ts < earlier_ts:
                    earlier_session = sp
                    earlier_ts = ts

    # If no observation inside the 5-min window, try the closest one before
    if earlier_session is None:
        for obs in reversed(observations[:-1]):
            ts = _parse_iso(obs.get("timestamp", ""))
            if ts is None or ts >= latest_ts:
                continue
            quota = obs.get("quota", {})
            sp = quota.get("ollama_session_pct")
            if sp is not None:
                earlier_session = float(sp)
                earlier_ts = ts
                break

    if earlier_session is None:
        return None

    delta = latest_session - earlier_session
    if delta > FAST_BURN_DELTA_PCT:
        msg = (
            f"Fast burn: session_usage rose {delta:.1f}pp in "
            f"<{FAST_BURN_WINDOW_MIN}min ({earlier_session:.1f}% -> {latest_session:.1f}%)"
        )
        return write_alert(
            "fast_burn",
            msg,
            extra={
                "delta_pct": round(delta, 1),
                "from_pct": round(earlier_session, 1),
                "to_pct": round(latest_session, 1),
                "window_minutes": FAST_BURN_WINDOW_MIN,
            },
        )

    return None


# ---------------------------------------------------------------------------
# 2. Zombie worker detection
# ---------------------------------------------------------------------------

def check_zombie_workers() -> List[Dict[str, Any]]:
    """Detect kanban workers with >2h since last heartbeat.

    Queries the kanban SQLite DB for running tasks and checks their
    last heartbeat timestamp.

    Returns a list of alert dicts (one per zombie worker).
    """
    db_path = get_kanban_db_path()
    if not db_path.exists():
        return []

    now = datetime.now(timezone.utc)
    threshold = now - timedelta(hours=ZOMBIE_WORKER_HOURS)

    # Query running tasks with their last heartbeat.
    # The kanban DB (as of Aug 2026) stores:
    #   tasks.last_heartbeat_at  — INTEGER Unix epoch seconds (nullable)
    #   tasks.started_at         — INTEGER Unix epoch seconds (nullable)
    # We use last_heartbeat_at directly; if NULL, fall back to started_at.

    conn: Optional[sqlite3.Connection] = None
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row

        rows = conn.execute(
            """
            SELECT id AS task_id,
                   title,
                   assignee,
                   last_heartbeat_at,
                   started_at
            FROM tasks
            WHERE status = 'running'
            """,
        ).fetchall()
    except Exception as exc:
        logger.debug("zombie check DB query failed: %s", exc)
        return []
    finally:
        if conn is not None:
            conn.close()

    alerts: List[Dict[str, Any]] = []
    for row in rows:
        # Prefer last_heartbeat_at; fall back to started_at if no heartbeat
        hb_epoch = row["last_heartbeat_at"]
        if hb_epoch is None:
            hb_epoch = row["started_at"]

        if hb_epoch is None:
            # No heartbeat and no started_at — flag as zombie
            last_hb_ts = threshold - timedelta(hours=1)
        else:
            last_hb_ts = datetime.fromtimestamp(int(hb_epoch), tz=timezone.utc)

        if last_hb_ts < threshold:
            hours_stale = (now - last_hb_ts).total_seconds() / 3600
            task_id = row["task_id"]
            assignee = row["assignee"] or "unknown"
            msg = (
                f"Zombie worker: task {task_id} ({assignee}) "
                f"last heartbeat {hours_stale:.1f}h ago"
            )
            alert = write_alert(
                "zombie_worker",
                msg,
                extra={
                    "task_id": task_id,
                    "assignee": assignee,
                    "hours_stale": round(hours_stale, 1),
                    "title": row["title"],
                },
            )
            alerts.append(alert)

    return alerts


# ---------------------------------------------------------------------------
# 3. Silent plugin detection
# ---------------------------------------------------------------------------

def check_silent_plugin() -> Optional[Dict[str, Any]]:
    """Detect if the plugin has been silent (no observations) for >1h.

    If observations.jsonl doesn't exist or the most recent observation
    is older than 1h, emit an alert.

    Returns an alert dict if silent, else None.
    """
    obs_path = get_observations_file()
    if not obs_path.exists():
        return write_alert(
            "silent_plugin",
            "Plugin silent: no observations file found — plugin may not be loaded",
            extra={"hours_silent": None},
        )

    try:
        text = obs_path.read_text(encoding="utf-8").strip()
    except OSError:
        return write_alert(
            "silent_plugin",
            "Plugin silent: cannot read observations file",
            extra={"hours_silent": None},
        )

    if not text:
        return write_alert(
            "silent_plugin",
            "Plugin silent: observations file is empty — plugin may not be loaded",
            extra={"hours_silent": None},
        )

    # Find the most recent observation timestamp
    last_ts: Optional[datetime] = None
    for line in reversed(text.split("\n")):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = _parse_iso(entry.get("timestamp", ""))
        if ts:
            last_ts = ts
            break

    if last_ts is None:
        return write_alert(
            "silent_plugin",
            "Plugin silent: no valid timestamps in observations — plugin may not be loaded",
            extra={"hours_silent": None},
        )

    now = datetime.now(timezone.utc)
    silence = now - last_ts
    hours_silent = silence.total_seconds() / 3600

    if hours_silent > SILENT_PLUGIN_HOURS:
        msg = (
            f"Plugin silent: no observations in {hours_silent:.1f}h "
            f"(threshold: {SILENT_PLUGIN_HOURS}h) — plugin may not be loaded"
        )
        return write_alert(
            "silent_plugin",
            msg,
            extra={"hours_silent": round(hours_silent, 1)},
        )

    return None


# ---------------------------------------------------------------------------
# Run all checks
# ---------------------------------------------------------------------------

def run_all_health_checks() -> List[Dict[str, Any]]:
    """Run all three health checks and return the list of new alerts.

    Each alert is also written to the alert log file.
    """
    alerts: List[Dict[str, Any]] = []

    # 1. Fast burn
    fb = check_fast_burn()
    if fb:
        alerts.append(fb)

    # 2. Zombie workers (can produce multiple alerts)
    zw = check_zombie_workers()
    alerts.extend(zw)

    # 3. Silent plugin
    sp = check_silent_plugin()
    if sp:
        alerts.append(sp)

    return alerts


def format_alerts_for_stdout(alerts: List[Dict[str, Any]]) -> str:
    """Format alerts as human-readable lines for the tick script stdout."""
    if not alerts:
        return ""

    lines: List[str] = []
    for a in alerts:
        ts = a.get("timestamp", "")[:19]
        atype = a.get("type", "?")
        msg = a.get("message", "")
        lines.append(f"[ALERT {ts}] {atype}: {msg}")

    return "\n".join(lines)
