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


def _query_zombie_tasks(db_path: Path) -> List[sqlite3.Row]:
    """Return running tasks with last heartbeat info from the kanban DB."""
    conn: Optional[sqlite3.Connection] = None
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        return conn.execute(
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
    finally:
        if conn:
            conn.close()


def _build_zombie_alert(row, now, threshold):
    """Create a zombie worker alert for a single DB row if stale."""
    hb_epoch = row["last_heartbeat_at"] or row["started_at"]
    if hb_epoch is None:
        last_hb_ts = threshold - timedelta(hours=1)
    else:
        last_hb_ts = datetime.fromtimestamp(int(hb_epoch), tz=timezone.utc)
    if last_hb_ts < threshold:
        hours_stale = (now - last_hb_ts).total_seconds() / 3600
        task_id = row["task_id"]
        assignee = row["assignee"] or "unknown"
        msg = f"Zombie worker: task {task_id} ({assignee}) last heartbeat {hours_stale:.1f}h ago"
        return write_alert(
            "zombie_worker",
            msg,
            extra={
                "task_id": task_id,
                "assignee": assignee,
                "hours_stale": round(hours_stale, 1),
                "title": row["title"],
            },
        )
    return None

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


def _find_earlier_session(observations, latest_ts, window_start):
    earlier_session = None
    earliest_ts = None
    for obs in observations:
        ts = _parse_iso(obs.get("timestamp", ""))
        if ts is None or ts >= latest_ts:
            continue
        if ts >= window_start:
            sp = obs.get("quota", {}).get("ollama_session_pct")
            if sp is None:
                continue
            sp = float(sp)
            if earliest_ts is None or ts < earliest_ts:
                earlier_session = sp
                earliest_ts = ts
    if earlier_session is None:
        for obs in reversed(observations[:-1]):
            ts = _parse_iso(obs.get("timestamp", ""))
            if ts is None or ts >= latest_ts:
                continue
            sp = obs.get("quota", {}).get("ollama_session_pct")
            if sp is None:
                continue
            earlier_session = float(sp)
            break
    return earlier_session


def check_fast_burn() -> Optional[Dict[str, Any]]:
    observations = _read_recent_observations(max_age_minutes=15)
    if len(observations) < 2:
        return None

    observations.sort(key=lambda e: e.get("timestamp", ""))
    latest = observations[-1]
    latest_ts = _parse_iso(latest.get("timestamp", ""))
    if latest_ts is None:
        return None
    latest_session = latest.get("quota", {}).get("ollama_session_pct")
    if latest_session is None:
        return None
    latest_session = float(latest_session)
    window_start = latest_ts - timedelta(minutes=FAST_BURN_WINDOW_MIN)
    earlier_session = _find_earlier_session(observations, latest_ts, window_start)
    if earlier_session is None:
        return None
    delta = latest_session - earlier_session
    if delta > FAST_BURN_DELTA_PCT:
        msg = f"Fast burn: session_usage rose {delta:.1f}pp in <{FAST_BURN_WINDOW_MIN}min ({earlier_session:.1f}% -> {latest_session:.1f}%)"
        return write_alert(
            "fast_burn",
            msg,
            extra={
                "delta_pct": round(delta, 1),
                "from_pct": round(earlier_session, 1),
                "to_pct": round(latest_session, 1),
                "window_minutes": FAST_BURN_WINDOW_MIN,
            }
        )
    return None


# ---------------------------------------------------------------------------
# 2. Zombie worker detection
# ---------------------------------------------------------------------------

def check_zombie_workers() -> List[Dict[str, Any]]:
    """Detect kanban workers with >2h since last heartbeat."""
    db_path = get_kanban_db_path()
    if not db_path.exists():
        return []
    now = datetime.now(timezone.utc)
    threshold = now - timedelta(hours=ZOMBIE_WORKER_HOURS)
    alerts: List[Dict[str, Any]] = []
    for row in _query_zombie_tasks(db_path):
        alert = _build_zombie_alert(row, now, threshold)
        if alert:
            alerts.append(alert)
    return alerts


# ---------------------------------------------------------------------------
# 3. Silent plugin detection
# ---------------------------------------------------------------------------


def _profile_has_any_active_tasks(profile_name: str) -> bool:
    """Check if a profile has any non-terminal task (ready, running, blocked, todo).

    Used to suppress silent_plugin when the board has zero work for this
    profile — if there are no ready/running/todo tasks, silence is expected
    regardless of whether the plugin hooks are firing.
    """
    db_path = get_kanban_db_path()
    if not db_path.exists():
        return False
    conn: Optional[sqlite3.Connection] = None
    try:
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE assignee = ? AND status IN ('ready', 'running', 'blocked', 'todo')",
            (profile_name,),
        ).fetchone()
        return row[0] > 0
    except Exception as exc:
        logger.debug("active-tasks check failed: %s", exc)
        return False
    finally:
        if conn is not None:
            conn.close()


def _last_alert_key() -> Optional[Dict[str, Any]]:
    """Return the ``(type, dedup_key)`` of the last alert in the log, or None.

    The dedup_key is a frozenset of the non-timestamp, non-message fields
    that define whether an alert has "changed state" — same type + same
    extra fields → deduplicate.
    """
    log_path = get_alert_log_path()
    if not log_path.exists():
        return None
    try:
        text = log_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text:
        return None
    last_line = text.split("\n")[-1].strip()
    if not last_line:
        return None
    try:
        entry = json.loads(last_line)
    except json.JSONDecodeError:
        return None
    return entry


def _find_profile_observations() -> List[tuple]:
    """Discover all profiles with observations.jsonl files.

    Returns a list of (profile_name, observations_path) tuples for every
    profile directory under ~/.hermes/profiles/ that has a
    quota-governor/observations.jsonl file.
    """
    home = Path.home()
    profiles_dir = home / ".hermes" / "profiles"
    results: List[tuple] = []

    if not profiles_dir.is_dir():
        return results

    for entry in sorted(profiles_dir.iterdir()):
        if not entry.is_dir():
            continue
        obs_path = entry / "quota-governor" / "observations.jsonl"
        if obs_path.exists():
            results.append((entry.name, obs_path))

    return results


def _get_latest_observation_ts(path: Path) -> Optional[datetime]:
    """Return the timestamp of the most recent observation line."""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text:
        return None
    for line in reversed(text.split("\n")):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = _parse_iso(entry.get("timestamp", ""))
        if ts:
            return ts
    return None


def check_silent_plugin(
    observations_path: Optional[Path] = None,
    profile_name: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Detect if the plugin has been silent (no observations) for >1h.

    If ``observations_path`` is provided, checks that file instead of the
    default HERMES_HOME-scoped one.  If ``profile_name`` is provided, the
    check is suppressed when the profile has no running tasks (idle
    profiles are expected to be silent — this avoids false positives from
    cross-profile detection).

    If observations.jsonl doesn't exist or the most recent observation
    is older than 1h, emit an alert.

    Returns an alert dict if silent, else None.
    """
    obs_path = observations_path if observations_path is not None else get_observations_file()

    if profile_name is not None and not _profile_has_any_active_tasks(profile_name):
        logger.debug(
            "silent_plugin: profile %s has zero active tasks — suppressing alert",
            profile_name,
        )
        return None

    if not obs_path.exists():
        return write_alert(
            "silent_plugin",
            f"Plugin silent ({profile_name or 'unknown'}): no observations file found — plugin may not be loaded",
            extra={"hours_silent": None, "profile": profile_name},
        )

    last_ts = _get_latest_observation_ts(obs_path)
    if last_ts is None:
        return write_alert(
            "silent_plugin",
            f"Plugin silent ({profile_name or 'unknown'}): no valid timestamps in observations — plugin may not be loaded",
            extra={"hours_silent": None, "profile": profile_name},
        )

    now = datetime.now(timezone.utc)
    hours_silent = (now - last_ts).total_seconds() / 3600

    if hours_silent > SILENT_PLUGIN_HOURS:
        msg = (
            f"Plugin silent ({profile_name or 'unknown'}): no observations in {hours_silent:.1f}h "
            f"(threshold: {SILENT_PLUGIN_HOURS}h) — plugin may not be loaded"
        )
        return write_alert(
            "silent_plugin",
            msg,
            extra={"hours_silent": round(hours_silent, 1), "profile": profile_name},
        )

    return None


# ---------------------------------------------------------------------------
# Run all checks
# ---------------------------------------------------------------------------






def _collect_silent_plugin_alerts() -> List[Dict[str, Any]]:
    """Collect silent plugin alerts across profiles."""
    alerts: List[Dict[str, Any]] = []
    default_obs = get_observations_file()
    checked_paths: set = set()
    tick_profile: Optional[str] = None
    hermes_home = _get_hermes_home()
    profiles_root = (Path.home() / ".hermes" / "profiles").resolve()
    try:
        rel = hermes_home.relative_to(profiles_root)
        tick_profile = rel.parts[0]
    except (ValueError, IndexError):
        pass
    sp = check_silent_plugin(observations_path=default_obs, profile_name=tick_profile)
    if sp:
        alerts.append(sp)
    checked_paths.add(str(default_obs.resolve()))
    in_production = False
    try:
        hermes_home.relative_to(profiles_root)
        in_production = True
    except ValueError:
        pass
    if in_production:
        for profile_name, obs_path in _find_profile_observations():
            if str(obs_path.resolve()) in checked_paths:
                continue
            sp = check_silent_plugin(observations_path=obs_path, profile_name=profile_name)
            if sp:
                alerts.append(sp)
            checked_paths.add(str(obs_path.resolve()))
    return alerts

    fb = check_fast_burn()
    if fb:
        alerts.append(fb)
    alerts.extend(check_zombie_workers())
    alerts.extend(_collect_silent_plugin_alerts())
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
