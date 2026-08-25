"""Quota Governor — core logic: state, observations, daemon control.

This module is the glue between the hooks (in __init__.py), the provider
queries (in providers.py), and the decision heuristic (in quota_planner.py).

State is persisted as JSON under ``$HERMES_HOME/quota-governor/`` so the
cron layer and the plugin share the same picture.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .providers import QuotaSnapshot, query_all
from .quota_planner import GovernorDecision, decide

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _get_hermes_home() -> Path:
    val = os.environ.get("HERMES_HOME", "").strip()
    return Path(val).resolve() if val else (Path.home() / ".hermes").resolve()


def get_state_dir() -> Path:
    return _get_hermes_home() / "quota-governor"


def get_state_file() -> Path:
    return get_state_dir() / "observations.jsonl"


def get_stop_signal_file() -> Path:
    return get_state_dir() / "STOP"


def get_daemon_pidfile() -> Path:
    return _get_hermes_home() / "quota-governor-daemon.pid"


# ---------------------------------------------------------------------------
# State persistence (JSON Lines — one observation per line)
# ---------------------------------------------------------------------------

def _ensure_state_dir() -> None:
    get_state_dir().mkdir(parents=True, exist_ok=True)


def record_observation(
    event: str,
    task_id: str = "",
    assignee: Optional[str] = None,
    snapshot: Optional[QuotaSnapshot] = None,
    summary: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """Append a JSON observation to the state file."""
    _ensure_state_dir()
    entry: Dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event,
    }
    if task_id:
        entry["task_id"] = task_id
    if assignee:
        entry["assignee"] = assignee
    if summary:
        entry["summary"] = summary[:200]  # truncate long summaries
    if extra:
        entry.update(extra)
    if snapshot:
        entry["quota"] = {
            "ollama_session_pct": round(snapshot.ollama_session_pct, 1),
            "ollama_weekly_pct": round(snapshot.ollama_weekly_pct, 1),
            "ollama_session_requests": snapshot.ollama_session_requests,
            "ollama_weekly_requests": snapshot.ollama_weekly_requests,
            "nanogpt_daily_pct": snapshot.nanogpt_daily_pct,
            "nanogpt_weekly_tokens_pct": snapshot.nanogpt_weekly_tokens_pct,
            "openrouter_weekly_usd": snapshot.openrouter_usage_weekly_usd,
            "errors": snapshot.errors,
        }

    try:
        with open(get_state_file(), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as exc:
        logger.debug("failed to write observation: %s", exc)


def read_observations(limit: int = 20) -> List[Dict[str, Any]]:
    """Read the last N observations from the state file."""
    state_file = get_state_file()
    if not state_file.exists():
        return []
    try:
        lines = state_file.read_text(encoding="utf-8").strip().split("\n")
        entries = []
        for line in lines[-limit:]:
            if line.strip():
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return entries
    except OSError:
        return []


# ---------------------------------------------------------------------------
# Stop signals (read by the cron no_agent script)
# ---------------------------------------------------------------------------

def write_stop_signal(reason: str = "") -> None:
    """Write a STOP signal file that the cron layer reads.

    The cron script checks for this file before starting the daemon.
    If present, it skips the tick. The file includes the reason and
    timestamp so the user can see why the governor stopped.
    """
    _ensure_state_dir()
    payload = {
        "reason": reason,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    try:
        get_stop_signal_file().write_text(json.dumps(payload), encoding="utf-8")
        logger.info("stop signal written: %s", reason)
    except OSError as exc:
        logger.debug("failed to write stop signal: %s", exc)


def clear_stop_signals() -> str:
    """Remove stop signal files."""
    sf = get_stop_signal_file()
    if sf.exists():
        try:
            sf.unlink()
            return "Stop signal cleared."
        except OSError:
            return "Error clearing stop signal."
    return "No stop signal present."


def has_stop_signal() -> bool:
    return get_stop_signal_file().exists()


# ---------------------------------------------------------------------------
# Daemon control
# ---------------------------------------------------------------------------

def daemon_status() -> str:
    """Check if the kanban daemon is running."""
    pidfile = get_daemon_pidfile()
    if not pidfile.exists():
        return "Daemon: not running (no pidfile)"

    try:
        pid = int(pidfile.read_text().strip())
    except (ValueError, OSError):
        return "Daemon: stale pidfile"

    # Check if process is alive
    try:
        os.kill(pid, 0)
        return f"Daemon: running (PID {pid})"
    except ProcessLookupError:
        return f"Daemon: dead (stale PID {pid})"
    except PermissionError:
        return f"Daemon: running (PID {pid}) — permission check"


def daemon_start(max_workers: int = 1) -> str:
    """Start the kanban daemon with quota-aware --max."""
    if has_stop_signal():
        return "Cannot start: stop signal is active. Run /quota-governor clear-signals first."

    cmd = [
        "hermes", "kanban", "daemon",
        "--interval", "60",
        "--max", str(max_workers),
        "--pidfile", str(get_daemon_pidfile()),
        "--verbose",
    ]
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return f"Daemon started (PID {proc.pid}, max_workers={max_workers})"
    except Exception as exc:
        return f"Failed to start daemon: {exc}"


def daemon_stop() -> str:
    """Stop the kanban daemon gracefully."""
    pidfile = get_daemon_pidfile()
    if not pidfile.exists():
        return "Daemon: not running (no pidfile)"

    try:
        pid = int(pidfile.read_text().strip())
    except (ValueError, OSError):
        return "Daemon: stale pidfile, removing"
        pidfile.unlink(missing_ok=True)
        return "Stale pidfile removed"

    try:
        os.kill(pid, 15)  # SIGTERM
        pidfile.unlink(missing_ok=True)
        return f"Daemon stopped (SIGTERM to PID {pid})"
    except ProcessLookupError:
        pidfile.unlink(missing_ok=True)
        return f"Daemon PID {pid} not found, pidfile cleaned"
    except Exception as exc:
        return f"Failed to stop daemon: {exc}"


# ---------------------------------------------------------------------------
# Query + format helpers
# ---------------------------------------------------------------------------

def query_quota() -> QuotaSnapshot:
    """Convenience wrapper — query all providers."""
    return query_all()


def format_status() -> str:
    """Human-readable status for /quota-governor status."""
    snapshot = query_quota()
    decision = decide(snapshot)

    lines = [
        "=== Quota Governor Status ===",
        "",
        "Ollama Cloud (primary):",
        f"  Session:  {snapshot.ollama_session_pct:5.1f}%  "
        f"({snapshot.ollama_session_requests} requests)",
        f"  Weekly:   {snapshot.ollama_weekly_pct:5.1f}%  "
        f"({snapshot.ollama_weekly_requests} requests)",
    ]

    if snapshot.nanogpt_daily_pct is not None:
        lines.append("")
        lines.append("NanoGPT (informational):")
        lines.append(f"  Daily:          {snapshot.nanogpt_daily_pct:5.1f}%")
        if snapshot.nanogpt_weekly_tokens_pct is not None:
            lines.append(
                f"  Weekly tokens:  {snapshot.nanogpt_weekly_tokens_pct:5.1f}%"
            )
        if snapshot.nanogpt_state:
            lines.append(f"  State:          {snapshot.nanogpt_state}")

    if snapshot.openrouter_usage_weekly_usd is not None:
        lines.append("")
        lines.append("OpenRouter (informational):")
        lines.append(f"  Weekly:  ${snapshot.openrouter_usage_weekly_usd:.4f}")
        if snapshot.openrouter_usage_monthly_usd is not None:
            lines.append(f"  Monthly: ${snapshot.openrouter_usage_monthly_usd:.4f}")

    lines.append("")
    lines.append(f"Decision: {decision.action.upper()}")
    lines.append(f"  Workers: {decision.max_workers}")
    lines.append(f"  Max task: {decision.max_task_cost}")
    lines.append(f"  Reason:  {decision.reason}")

    if has_stop_signal():
        lines.append("")
        lines.append("⚠ STOP SIGNAL ACTIVE — daemon will not start")

    if snapshot.errors:
        lines.append("")
        lines.append("Provider errors:")
        for err in snapshot.errors:
            lines.append(f"  - {err}")

    return "\n".join(lines)


def format_history(limit: int = 20) -> str:
    """Recent observations for /quota-governor history."""
    obs = read_observations(limit=limit)
    if not obs:
        return "No observations recorded yet."

    lines = [f"=== Last {len(obs)} observations ===", ""]
    for entry in reversed(obs):
        ts = entry.get("timestamp", "?")[:19]
        event = entry.get("event", "?")
        task = entry.get("task_id", "")
        quota = entry.get("quota", {})
        session = quota.get("ollama_session_pct", "?")
        weekly = quota.get("ollama_weekly_pct", "?")

        line = f"[{ts}] {event}"
        if task:
            line += f" task={task}"
        if quota:
            line += f"  session={session}% weekly={weekly}%"
        lines.append(line)

    return "\n".join(lines)


def format_decision(snapshot: QuotaSnapshot, decision: GovernorDecision) -> str:
    """Format a decision for /quota-governor decision."""
    return (
        f"Decision: {decision.action.upper()}\n"
        f"  Workers:     {decision.max_workers}\n"
        f"  Max task:    {decision.max_task_cost}\n"
        f"  Should spawn: {decision.should_spawn}\n"
        f"  Reason:      {decision.reason}\n"
        f"  Session:     {snapshot.ollama_session_pct:.1f}%\n"
        f"  Weekly:      {snapshot.ollama_weekly_pct:.1f}%"
    )