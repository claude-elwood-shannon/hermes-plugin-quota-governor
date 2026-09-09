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


def _request_window_fields() -> Dict[str, Any]:
    """OBJ-26a follow-up (t_92d7f0d6): per-request ledger accumulators.

    Reads the cross-profile window totals (request rows land under the
    CAPTURING process's HERMES_HOME, which differs from this observer's) and
    returns quota.request_balance_usd / quota.request_covered_usd. These are
    independent accumulators from the probe-derived window_spent_usd and the
    Ollama activity_cost — reported separately, never merged. Values are
    None when the ledger module or its data is unavailable (schema-stable
    None, fail-open, never raises).
    """
    fields: Dict[str, Any] = {
        "request_balance_usd": None,
        "request_covered_usd": None,
    }
    try:
        import importlib.util

        # quota_governor.py lives at the plugin ROOT: dirname() is the
        # plugin dir, and the ledger copy is next to it under scripts/.
        script = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "scripts", "nanogpt-balance-ledger.py")
        spec = importlib.util.spec_from_file_location(
            "nanogpt_balance_ledger_obs", script)
        if spec is None or spec.loader is None:
            return fields
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        totals = mod.request_window_totals_all_homes()
        if totals and totals.get("homes_read"):
            fields["request_balance_usd"] = totals.get("request_balance_usd")
            fields["request_covered_usd"] = totals.get("request_covered_usd")
    except Exception as exc:  # fail-open: observability never breaks hooks
        logger.debug("request-window fields unavailable: %s", exc)
    return fields


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


def get_spending_limit_file() -> Path:
    """Path to the persisted spending-limit JSON."""
    return get_state_dir() / "spending-limit.json"


# (get_spending_limit / set_spending_limit moved below — see Spending limit persistence section)


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
        # activity_cost is Optional[float] per design §5.4; write it only when
        # present so we don't overwrite the previous observation's value with
        # a misleading 0.0 when the provider returns None.
        quota_entry: Dict[str, Any] = {
            "ollama_session_pct": round(snapshot.ollama_session_pct, 1),
            "ollama_weekly_pct": round(snapshot.ollama_weekly_pct, 1),
            "ollama_session_requests": snapshot.ollama_session_requests,
            "ollama_weekly_requests": snapshot.ollama_weekly_requests,
            "nanogpt_daily_pct": snapshot.nanogpt_daily_pct,
            "nanogpt_weekly_tokens_pct": snapshot.nanogpt_weekly_tokens_pct,
            "openrouter_weekly_usd": snapshot.openrouter_usage_weekly_usd,
            "opencode_go_rolling_pct": snapshot.opencode_go_rolling_pct,
            "opencode_go_weekly_pct": snapshot.opencode_go_weekly_pct,
            "opencode_go_monthly_pct": snapshot.opencode_go_monthly_pct,
            "errors": snapshot.errors,
        }
        # activity_cost: prefer the design-canonical key, keep the legacy
        # alias for backward compatibility with existing observations.jsonl.
        cost = snapshot.ollama_activity_cost
        if cost is not None:
            quota_entry["activity_cost"] = round(cost, 5)
            quota_entry["ollama_activity_cost"] = round(cost, 5)
        # OBJ-26a follow-up: per-request ledger accumulators from
        # nanogpt-requests.jsonl (cross-profile merge). Independent of
        # activity_cost (Ollama probe) and of any spent_usd probe meter —
        # reported separately, values None when the ledger is unavailable.
        quota_entry.update(_request_window_fields())
        entry["quota"] = quota_entry

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
# Spending limit persistence
# ---------------------------------------------------------------------------

_DEFAULT_SPENDING_LIMIT = 5.00


def get_spending_limit() -> float:
    """Return the configured spending limit (USD).

    Precedence (first wins):
      1. State file ``spending-limit.json`` (set via slash command)
      2. ``QUOTA_GOVERNOR_SPENDING_LIMIT`` env var / .env
      3. Default ``$5.00``
    """
    # 1. State file (set via /quota-governor set-limit)
    sf = get_spending_limit_file()
    if sf.exists():
        try:
            data = json.loads(sf.read_text(encoding="utf-8"))
            limit = data.get("limit")
            if limit is not None:
                return float(limit)
        except (json.JSONDecodeError, OSError, ValueError):
            logger.debug("spending-limit.json corrupt, falling through to env")

    # 2. Env var (reuse providers._get_env for .env fallback)
    from .providers import _get_env
    env_val = _get_env("QUOTA_GOVERNOR_SPENDING_LIMIT")
    if env_val is not None:
        try:
            return float(env_val)
        except ValueError:
            logger.warning("QUOTA_GOVERNOR_SPENDING_LIMIT=%r is not a float", env_val)

    # 3. Default
    return _DEFAULT_SPENDING_LIMIT


def set_spending_limit(value: float) -> str:
    """Persist the spending limit to the state file.

    Args:
        value: USD cap. ``0`` means unlimited (no stop on spending).

    Returns:
        Confirmation message.
    """
    if value < 0:
        raise ValueError("Spending limit cannot be negative (use 0 for unlimited)")

    _ensure_state_dir()
    payload = {
        "limit": value,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        get_spending_limit_file().write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        return f"Error: failed to persist spending limit: {exc}"

    if value == 0:
        return "Spending limit set to $0 (unlimited — no stop on spend)."
    return f"Spending limit set to ${value:.2f}."


# ---------------------------------------------------------------------------
# Previous cost tracking (for decide() pay-as-you-go detection)
# ---------------------------------------------------------------------------

def get_previous_cost() -> float:
    """Read the last recorded ``activity_cost`` from observations.

    Returns 0.0 if no observations exist.

    Reads the design-canonical ``activity_cost`` key first (§5.4),
    falling back to the legacy ``ollama_activity_cost`` alias so old
    observations.jsonl entries still work.
    """
    obs = read_observations(limit=1)
    if not obs:
        return 0.0
    quota = obs[0].get("quota", {})
    # Canonical key first, legacy alias as fallback
    cost = quota.get("activity_cost")
    if cost is None:
        cost = quota.get("ollama_activity_cost", 0.0)
    return float(cost)


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
    spending_limit = get_spending_limit()
    previous_cost = get_previous_cost()
    decision = decide(snapshot, prev_activity_cost=previous_cost,
                      spending_limit=spending_limit)

    # Mode display per design §5.2
    mode_label = decision.mode.upper()
    mode_marker = ""
    if decision.mode == "paying":
        mode_marker = "  ⚠ spending pay-as-you-go balance"
    elif decision.mode == "stop" and has_stop_signal():
        mode_marker = "  ⚠ stop signal active"

    # Cost (graceful when activity_cost is None — design §5.4 future-proofs)
    cost_val = snapshot.ollama_activity_cost
    cost_str = f"${cost_val:.2f}" if cost_val is not None else "N/A"

    lines = [
        "=== Quota Governor Status ===",
        "",
        "Ollama Cloud (primary):",
        f"  Session:  {snapshot.ollama_session_pct:5.1f}%  "
        f"({snapshot.ollama_session_requests} requests)",
        f"  Weekly:   {snapshot.ollama_weekly_pct:5.1f}%  "
        f"({snapshot.ollama_weekly_requests} requests)",
        f"  Mode:     {mode_label}{mode_marker}",
    ]

    # Cost + limit combined line (design §5.1)
    if spending_limit > 0:
        lines.append(f"  Cost:     {cost_str}  (limit: ${spending_limit:.2f})")
    else:
        lines.append(f"  Cost:     {cost_str}  (limit: unlimited)")

    if snapshot.nanogpt_daily_pct is not None or snapshot.nanogpt_state is not None:
        lines.append("")
        lines.append("NanoGPT (informational):")
        if snapshot.nanogpt_daily_pct is not None:
            lines.append(f"  Daily:          {snapshot.nanogpt_daily_pct:5.1f}%")
        if snapshot.nanogpt_weekly_tokens_pct is not None:
            lines.append(
                f"  Weekly tokens:  {snapshot.nanogpt_weekly_tokens_pct:5.1f}%"
            )
        if snapshot.nanogpt_state:
            lines.append(f"  State:          {snapshot.nanogpt_state}")
    else:
        lines.append("")
        lines.append("NanoGPT (informational):")
        lines.append("  Not configured — set NANO_GPT_API_KEY in profile .env")

    if snapshot.openrouter_usage_weekly_usd is not None:
        lines.append("")
        lines.append("OpenRouter (informational):")
        lines.append(f"  Weekly:  ${snapshot.openrouter_usage_weekly_usd:.4f}")
        if snapshot.openrouter_usage_monthly_usd is not None:
            lines.append(f"  Monthly: ${snapshot.openrouter_usage_monthly_usd:.4f}")

    # OpenCode Go (informational) — percent fields are already 0-100
    if (
        snapshot.opencode_go_rolling_pct is not None
        or snapshot.opencode_go_weekly_pct is not None
        or snapshot.opencode_go_monthly_pct is not None
    ):
        lines.append("")
        lines.append("OpenCode Go (informational):")
        if snapshot.opencode_go_rolling_pct is not None:
            lines.append(f"  Rolling: {snapshot.opencode_go_rolling_pct:5.1f}%")
        if snapshot.opencode_go_weekly_pct is not None:
            lines.append(f"  Weekly:  {snapshot.opencode_go_weekly_pct:5.1f}%")
        if snapshot.opencode_go_monthly_pct is not None:
            lines.append(f"  Monthly: {snapshot.opencode_go_monthly_pct:5.1f}%")
    else:
        lines.append("")
        lines.append("OpenCode Go (informational):")
        lines.append("  Not configured — set OPENCODE_GO_API_KEY in profile .env")

    lines.append("")
    lines.append(f"Decision: {decision.action.upper()}")
    lines.append(f"  Workers:  {decision.max_workers}")
    lines.append(f"  Max task: {decision.max_task_cost}")
    lines.append(f"  Reason:  {decision.reason}")

    # Detailed cost breakdown when paying (mode/cost already shown above
    # in the Ollama Cloud section — this adds the warning line only).
    if decision.mode == "paying" and decision.paying_warning:
        lines.append(f"  {decision.paying_warning}")

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
        # Prefer canonical activity_cost, fall back to legacy alias
        activity_cost = quota.get("activity_cost")
        if activity_cost is None:
            activity_cost = quota.get("ollama_activity_cost")

        line = f"[{ts}] {event}"
        if task:
            line += f" task={task}"
        if quota:
            line += f"  session={session}% weekly={weekly}%"
            if activity_cost is not None:
                line += f" cost=${activity_cost:.2f}"
        lines.append(line)

    return "\n".join(lines)


def format_decision(snapshot: QuotaSnapshot, decision: GovernorDecision) -> str:
    """Format a decision for /quota-governor decision."""
    # Graceful cost display — activity_cost may be None (design §5.4)
    cost = decision.activity_cost
    cost_str = f"${cost:.2f}" if cost is not None else "N/A"
    lines = [
        f"Decision: {decision.action.upper()}",
        f"  Mode:         {decision.mode}",
        f"  Workers:      {decision.max_workers}",
        f"  Max task:     {decision.max_task_cost}",
        f"  Should spawn: {decision.should_spawn}",
        f"  Reason:       {decision.reason}",
        f"  Session:      {snapshot.ollama_session_pct:.1f}%",
        f"  Weekly:       {snapshot.ollama_weekly_pct:.1f}%",
        f"  Activity cost: {cost_str}",
    ]
    if decision.spending_limit > 0:
        lines.append(f"  Spend limit:  ${decision.spending_limit:.2f}")
    else:
        lines.append(f"  Spend limit:  unlimited (0 = disabled)")
    if decision.paying_warning:
        lines.append(f"  {decision.paying_warning}")
    return "\n".join(lines)