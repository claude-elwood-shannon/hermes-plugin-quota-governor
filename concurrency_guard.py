"""Concurrency guard for the quota-governor tick script (OBJ-06).

The Hermes core ``dispatch_once`` already treats ``--max N`` as a live
concurrency cap (counts ``status='running'`` tasks against the budget).
However, the tick script can restart the daemon with a *lower* ``--max``
when quota degrades, and the old daemon's workers keep running until
they finish or get TTL-reclaimed.  This module gives the tick script:

1. **Live worker count** — query the kanban DB for ``running`` tasks whose
   ``worker_pid`` is still alive (``kill(pid, 0)`` succeeds).  This is
   the ground truth, not the daemon's ``--max`` file.

2. **Soft cap** — if ``live_workers <= desired_max`` the tick proceeds
   normally.  If ``live_workers > desired_max`` the tick should *not*
   restart the daemon with a higher ``--max``; the existing daemon already
   prevents new spawns, so workers will drain naturally.

3. **Hard cap** — if ``live_workers > hard_limit`` the oldest workers are
   killed (SIGTERM) to prevent unbounded accumulation.  The hard limit
   defaults to ``desired_max + 2`` (one slow tick of headroom) and can be
   overridden via ``QUOTA_GOVERNOR_HARD_LIMIT`` env var.

All functions are pure-Python with no external deps so the tick script
can call them via an inline ``python3 -c`` block.
"""

from __future__ import annotations

import logging
import os
import signal
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DB path resolution (mirrors health_checks.py logic)
# ---------------------------------------------------------------------------

def _get_hermes_root() -> Path:
    """Return ~/.hermes (root, not profile-scoped)."""
    val = os.environ.get("HERMES_HOME", "").strip()
    hermes_home = Path(val).resolve() if val else (Path.home() / ".hermes").resolve()
    profiles_root = (Path.home() / ".hermes" / "profiles").resolve()
    try:
        hermes_home.relative_to(profiles_root)
        return (Path.home() / ".hermes").resolve()
    except ValueError:
        return hermes_home


def get_kanban_db_path() -> Path:
    """Resolve the kanban DB path (same logic as health_checks.get_kanban_db_path)."""
    env_db = os.environ.get("HERMES_KANBAN_DB", "").strip()
    if env_db:
        return Path(env_db).expanduser().resolve()
    root_db = _get_hermes_root() / "kanban.db"
    if root_db.exists():
        return root_db
    val = os.environ.get("HERMES_HOME", "").strip()
    hermes_home = Path(val).resolve() if val else (Path.home() / ".hermes").resolve()
    return hermes_home / "kanban.db"


# ---------------------------------------------------------------------------
# Live worker detection
# ---------------------------------------------------------------------------

@dataclass
class WorkerInfo:
    """Information about a single live kanban worker."""
    task_id: str
    assignee: str
    pid: Optional[int]
    started_at: Optional[int]   # Unix epoch seconds
    last_heartbeat_at: Optional[int]
    title: str = ""


def _pid_alive(pid: Optional[int]) -> bool:
    """Check if a PID is alive (kill 0)."""
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def count_live_workers() -> int:
    """Count kanban tasks in ``running`` status whose ``worker_pid`` is alive."""
    return len(get_live_workers())


# SQL for _fetch_running_task_rows: every task in ``running`` status;
# liveness (kill(pid, 0)) is decided in Python, never inside SQL.
_SELECT_RUNNING_TASKS_SQL = """
    SELECT id AS task_id,
           title,
           assignee,
           worker_pid,
           started_at,
           last_heartbeat_at
    FROM tasks
    WHERE status = 'running'
    """


def _fetch_running_task_rows(db_path: Path) -> List[sqlite3.Row]:
    """Query the kanban DB for every task in ``running`` status.

    Returns an empty list when the query fails (missing or corrupt DB):
    the caller treats "unknown" as "no live workers" so the guard stays
    fail-open.
    """
    conn: Optional[sqlite3.Connection] = None
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        return conn.execute(_SELECT_RUNNING_TASKS_SQL).fetchall()
    except Exception as exc:
        logger.debug("get_live_workers DB query failed: %s", exc)
        return []
    finally:
        if conn is not None:
            conn.close()


def _row_is_live(row: sqlite3.Row) -> bool:
    """Decide whether a ``running`` task row counts as a live worker.

    A row with no ``worker_pid`` counts as live: the dispatcher may not
    have recorded the PID yet (e.g. goal-mode workers), and a running
    task with no PID shouldn't be invisible to the concurrency guard.
    """
    pid = row["worker_pid"]
    return pid is None or _pid_alive(pid)


def _row_to_worker(row: sqlite3.Row) -> WorkerInfo:
    """Convert one ``running`` task row into a WorkerInfo record."""
    return WorkerInfo(
        task_id=row["task_id"],
        assignee=row["assignee"] or "unknown",
        pid=row["worker_pid"],
        started_at=row["started_at"],
        last_heartbeat_at=row["last_heartbeat_at"],
        title=row["title"] or "",
    )


def get_live_workers() -> List[WorkerInfo]:
    """Return all live kanban workers (status='running' + alive PID).

    If ``worker_pid`` is NULL but the task is ``running``, we count it
    as live — the dispatcher may not have recorded the PID yet (e.g.
    goal-mode workers), and a running task with no PID shouldn't be
    invisible to the concurrency guard.
    """
    db_path = get_kanban_db_path()
    if not db_path.exists():
        return []

    rows = _fetch_running_task_rows(db_path)
    return [_row_to_worker(row) for row in rows if _row_is_live(row)]


def sort_by_age(workers: List[WorkerInfo]) -> List[WorkerInfo]:
    """Sort workers oldest-first (by started_at ascending).

    Workers with no ``started_at`` are treated as oldest (epoch 0)
    so they get killed first under the hard cap — a worker we can't
    age is suspicious and should be culled preferentially.
    """
    return sorted(workers, key=lambda w: w.started_at or 0)


# ---------------------------------------------------------------------------
# Concurrency decision
# ---------------------------------------------------------------------------

@dataclass
class ConcurrencyDecision:
    """Result of the concurrency check."""
    live_count: int
    desired_max: int
    hard_limit: int
    should_spawn: bool            # False if live_count >= desired_max
    workers_to_kill: List[WorkerInfo] = field(default_factory=list)
    reason: str = ""


def _resolve_hard_limit(desired_max: int, hard_limit: Optional[int]) -> int:
    """Resolve the effective hard limit for the concurrency check.

    Precedence: explicit argument > ``QUOTA_GOVERNOR_HARD_LIMIT`` env var >
    ``desired_max + 2`` (one tick of headroom).  A garbage or empty env
    value falls back to the default rather than raising.
    """
    if hard_limit is not None:
        return hard_limit
    env_val = os.environ.get("QUOTA_GOVERNOR_HARD_LIMIT", "").strip()
    if env_val:
        try:
            return int(env_val)
        except ValueError:
            pass
    return desired_max + 2


def _build_decision_reason(
    live_count: int, desired_max: int, hard_limit: int,
    should_spawn: bool, kill_count: int,
) -> str:
    """Build the one-line decision reason consumed by format_decision_for_log."""
    parts = [f"live={live_count}", f"desired={desired_max}", f"hard={hard_limit}"]
    if should_spawn:
        parts.append("spawn=ok")
    else:
        parts.append("spawn=skip(soft_cap)")
    if kill_count:
        parts.append(f"kill={kill_count}")
    return " | ".join(parts)


def check_concurrency(desired_max: int, hard_limit: Optional[int] = None) -> ConcurrencyDecision:
    """Decide whether the tick should spawn and which workers to kill.

    Args:
        desired_max: The ``--max N`` the tick wants to set on the daemon.
            This is the soft cap — if live workers already exceed it,
            the tick should NOT restart the daemon (the running daemon
            already enforces this cap on new spawns).
        hard_limit: The absolute maximum workers allowed.  If live
            workers exceed this, the oldest are killed via SIGTERM.
            Defaults to ``desired_max + 2`` (one tick of headroom) or
            the ``QUOTA_GOVERNOR_HARD_LIMIT`` env var.

    Returns:
        ConcurrencyDecision with the live count, kill list, and reason.
    """
    hard_limit = _resolve_hard_limit(desired_max, hard_limit)

    workers = get_live_workers()
    live_count = len(workers)
    workers_sorted = sort_by_age(workers)

    # Soft cap: if live workers >= desired_max, don't spawn
    should_spawn = live_count < desired_max

    # Hard cap: if live workers > hard_limit, kill the oldest
    to_kill: List[WorkerInfo] = []
    if live_count > hard_limit:
        excess = live_count - hard_limit
        to_kill = workers_sorted[:excess]

    reason = _build_decision_reason(
        live_count, desired_max, hard_limit,
        should_spawn, kill_count=len(to_kill),
    )

    return ConcurrencyDecision(
        live_count=live_count,
        desired_max=desired_max,
        hard_limit=hard_limit,
        should_spawn=should_spawn,
        workers_to_kill=to_kill,
        reason=reason,
    )


def kill_worker(worker: WorkerInfo) -> bool:
    """SIGTERM a worker. Returns True if signal was sent successfully."""
    if worker.pid is None:
        logger.warning(
            "kill_worker: task %s has no PID — cannot kill (will be "
            "TTL-reclaimed by the dispatcher)", worker.task_id,
        )
        return False
    try:
        os.kill(worker.pid, signal.SIGTERM)
        return True
    except (ProcessLookupError, PermissionError, OSError) as exc:
        logger.warning(
            "kill_worker: failed to SIGTERM PID %s (task %s): %s",
            worker.pid, worker.task_id, exc,
        )
        return False


def format_decision_for_log(decision: ConcurrencyDecision) -> str:
    """One-line summary for the tick log."""
    return decision.reason


def format_kill_notice(killed: List[Tuple[WorkerInfo, bool]]) -> str:
    """Format killed-worker notice for tick stdout (shown to operator).

    Args:
        killed: list of (worker, success) tuples.
    """
    if not killed:
        return ""
    lines = []
    for worker, success in killed:
        status = "killed" if success else "kill_failed"
        pid_str = str(worker.pid) if worker.pid else "no_pid"
        lines.append(
            f"[CONCURRENCY] {status}: task={worker.task_id} pid={pid_str} "
            f"assignee={worker.assignee} (hard cap exceeded)"
        )
    return "\n".join(lines)