"""quota-governor — Hermes Agent plugin for self-governance by quota.

Wires five behaviours:

1. ``kanban_task_claimed`` hook — fired by the DISPATCHER just before a
   worker spawns.  Queries quota and records the observation; if quota
   is critically low, writes a stop-signal file that the cron layer reads.

2. ``kanban_task_completed`` hook — fired by the WORKER when it calls
   ``kanban_complete``.  Records the task cost (elapsed time, request
   count if available) and updates the rolling heuristic.

3. ``kanban_task_blocked`` hook — records blocked tasks for audit.

4. ``post_tool_call`` hook — lightweight periodic quota sampling every
   N tool calls (not every call), to keep the governor's state fresh
   without hammering the quota endpoints.

5. ``/quota-governor`` slash command — manual status, decision preview,
   and daemon control.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from . import quota_governor as gov
from . import quota_planner as planner

logger = logging.getLogger(__name__)

# --- state files (under HERMES_HOME) -----------------------------------------

_STATE_DIR = None  # lazily computed in gov.get_state_dir()

# --- sampling throttle for post_tool_call ------------------------------------

_tool_call_counter: int = 0
_SAMPLE_EVERY_N_TOOL_CALLS = 50  # query quota every 50 tool calls


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------

def _on_kanban_task_claimed(
    task_id: str = "",
    board: Optional[str] = None,
    assignee: Optional[str] = None,
    run_id: Optional[int] = None,
    profile_name: str = "",
    **_: Any,
) -> None:
    """Dispatcher fires this just before spawning a worker.

    Observer-only (cannot veto the spawn).  We:
    1. Query current quota.
    2. Record the observation in the state file.
    3. If quota is critically low, write a stop-signal for the cron layer.
    """
    try:
        snapshot = gov.query_quota()
        gov.record_observation(
            event="task_claimed",
            task_id=task_id,
            assignee=assignee,
            snapshot=snapshot,
        )
        decision = planner.decide(snapshot)
        if decision.action == "stop":
            gov.write_stop_signal(
                reason=f"quota critical: session={snapshot.session_pct:.0f}% "
                f"weekly={snapshot.weekly_pct:.0f}%",
            )
    except Exception as exc:
        logger.debug("quota-governor kanban_task_claimed failed: %s", exc)


def _on_kanban_task_completed(
    task_id: str = "",
    board: Optional[str] = None,
    assignee: Optional[str] = None,
    run_id: Optional[int] = None,
    profile_name: str = "",
    summary: Optional[str] = None,
    **_: Any,
) -> None:
    """Worker fires this when it calls kanban_complete.

    Records the completed task and updates the rolling cost estimate.
    """
    try:
        snapshot = gov.query_quota()
        gov.record_observation(
            event="task_completed",
            task_id=task_id,
            assignee=assignee,
            snapshot=snapshot,
            summary=summary,
        )
        # After a task completes, re-evaluate: if quota dropped, write signal
        decision = planner.decide(snapshot)
        if decision.action == "stop":
            gov.write_stop_signal(
                reason=f"quota critical after task: "
                f"session={snapshot.session_pct:.0f}%",
            )
    except Exception as exc:
        logger.debug("quota-governor kanban_task_completed failed: %s", exc)


def _on_kanban_task_blocked(
    task_id: str = "",
    board: Optional[str] = None,
    assignee: Optional[str] = None,
    run_id: Optional[int] = None,
    profile_name: str = "",
    reason: Optional[str] = None,
    **_: Any,
) -> None:
    """Records blocked tasks for audit trail."""
    try:
        gov.record_observation(
            event="task_blocked",
            task_id=task_id,
            assignee=assignee,
            snapshot=None,
            extra={"block_reason": reason},
        )
    except Exception as exc:
        logger.debug("quota-governor kanban_task_blocked failed: %s", exc)


def _on_post_tool_call(
    tool_name: str = "",
    args: Optional[Dict[str, Any]] = None,
    result: Any = None,
    task_id: str = "",
    session_id: str = "",
    tool_call_id: str = "",
    **_: Any,
) -> None:
    """Lightweight periodic quota sampling.

    Not every tool call triggers a quota query — only every Nth call.
    This keeps the governor's state fresh without hammering the quota
    endpoints (which themselves cost requests).
    """
    global _tool_call_counter
    _tool_call_counter += 1
    if _tool_call_counter % _SAMPLE_EVERY_N_TOOL_CALLS != 0:
        return

    try:
        snapshot = gov.query_quota()
        gov.record_observation(
            event="periodic_sample",
            task_id=task_id,
            snapshot=snapshot,
        )
    except Exception as exc:
        logger.debug("quota-governor periodic sample failed: %s", exc)


def _on_session_end(
    session_id: str = "",
    completed: bool = True,
    interrupted: bool = False,
    **_: Any,
) -> None:
    """Final quota check when a session ends.

    Writes a session-end observation so the cron layer has fresh data.
    """
    try:
        snapshot = gov.query_quota()
        gov.record_observation(
            event="session_end",
            snapshot=snapshot,
            extra={"completed": completed, "interrupted": interrupted},
        )
    except Exception as exc:
        logger.debug("quota-governor session_end failed: %s", exc)


# ---------------------------------------------------------------------------
# Slash command: /quota-governor
# ---------------------------------------------------------------------------

_HELP_TEXT = """\
/quota-governor — self-governance by quota

Subcommands:
  status          Current quota snapshot + governor decision
  history         Recent observations (last 20)
  decision        Show what the governor would decide right now
  daemon          Show daemon status (running, max workers)
  daemon-start    Start the kanban daemon with quota-aware --max
  daemon-stop     Stop the kanban daemon (graceful)
  clear-signals   Remove stop-signal files

The governor observes kanban lifecycle and quota state.
It does NOT veto spawns — it records and signals.
The cron layer (no_agent script) reads signals and acts.
"""


def _handle_slash(raw_args: str) -> Optional[str]:
    argv = raw_args.strip().split()
    if not argv or argv[0] in {"help", "-h", "--help"}:
        return _HELP_TEXT

    sub = argv[0]

    if sub == "status":
        return gov.format_status()

    if sub == "history":
        limit = int(argv[1]) if len(argv) > 1 and argv[1].isdigit() else 20
        return gov.format_history(limit=limit)

    if sub == "decision":
        snapshot = gov.query_quota()
        decision = planner.decide(snapshot)
        return gov.format_decision(snapshot, decision)

    if sub == "daemon":
        return gov.daemon_status()

    if sub == "daemon-start":
        snapshot = gov.query_quota()
        decision = planner.decide(snapshot)
        if decision.action == "stop":
            return (
                f"Refusing to start daemon: quota critical "
                f"(session={snapshot.session_pct:.0f}%)."
            )
        return gov.daemon_start(max_workers=decision.max_workers)

    if sub == "daemon-stop":
        return gov.daemon_stop()

    if sub == "clear-signals":
        return gov.clear_stop_signals()

    return f"Unknown subcommand: {sub}\n\n{_HELP_TEXT}"


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    ctx.register_hook("kanban_task_claimed", _on_kanban_task_claimed)
    ctx.register_hook("kanban_task_completed", _on_kanban_task_completed)
    ctx.register_hook("kanban_task_blocked", _on_kanban_task_blocked)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("on_session_end", _on_session_end)
    ctx.register_command(
        "quota-governor",
        handler=_handle_slash,
        description="Self-governance by quota: status, decision, daemon control.",
    )