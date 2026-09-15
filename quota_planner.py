"""Quota-based decision heuristic.

Translates a ``QuotaSnapshot`` into a concrete ``GovernorDecision``:
how many workers to allow, what task types are safe, and whether to
stop entirely.

Three-state model (Aug 2026, design pay-as-you-go-design.md):

  run    — included quota available (free).  Sub-levels throttle workers
           and max-task-cost but the top-level state is still ``run``.
  paying — included quota exhausted (session 100%), spending pay-as-you-go
           balance.  Warn but allow (1 worker, small tasks).
  stop   — balance exhausted, spending limit hit, or weekly quota critical.

Per-call cost data (calibrated 2026-09-03 from 132 task_runs + 768 observations):
  - ~510 calls fill an Ollama session to 100% (was 137 — 272% deviation)
  - ~2988 calls fill the weekly cap to 100% (was 769 — 289% deviation)
  - Per call: ~0.0020 session, ~0.00034 weekly
  - Real per-task calls: tiny median 19, small median 27, medium median 19.5
  - Real wall-clock: micro 168s, tiny 242s, small 270s, medium 420s (medians)
  - decide() thresholds operate on percentages, not absolute counts,
    so no threshold changes needed — only documentation updated.
  - See docs/quota-planner.md §"Real cost calibration" for full table.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .providers import QuotaSnapshot, _get_env


Action = Literal["run", "paying", "stop"]


@dataclass
class GovernorDecision:
    """What the governor recommends given current quota."""

    action: Action          # run = normal/throttled, paying = spending balance, stop = halt
    max_workers: int        # 0 when action == stop
    max_task_cost: str       # "micro", "tiny", "small", "medium", "complex", "any"
    reason: str             # human-readable explanation
    mode: str = "run"       # user-facing label: "run" | "paying" | "stop"
    activity_cost: float = 0.0    # current activity.cost (USD)
    spend_limit: float = 5.0      # configured QUOTA_GOVERNOR_MAX_SPEND (USD)
    paying_warning: str = ""      # human-readable warning if in paying mode

    @property
    def should_spawn(self) -> bool:
        return self.action not in ("stop",) and self.max_workers > 0

    # Compatibility alias: sibling task t_58e412e0 uses `spending_limit` as the
    # field name in quota_governor.py / __init__.py. Design §5.3 names it
    # `spend_limit`. Expose both so callers on either convention work until
    # the naming is unified.
    @property
    def spending_limit(self) -> float:
        return self.spend_limit


def _read_spend_limit() -> float:
    """Read QUOTA_GOVERNOR_MAX_SPEND from env (default 5.00).

    0 disables the spend limit (warn-only, never stop on cost).
    Also checks .env files via the shared _get_env helper for cron/plugin
    contexts where the shell env may not be loaded.
    """
    raw = _get_env("QUOTA_GOVERNOR_MAX_SPEND")
    if raw is None:
        return 5.0
    try:
        return float(raw)
    except (ValueError, TypeError):
        return 5.0


def _resolve_spend_limit(spending_limit: float | None) -> float:
    """Return the effective spend limit: explicit override or env-read default."""
    return spending_limit if spending_limit is not None else _read_spend_limit()


def _decision(cost: float, spend_limit: float, *, action: Action,
              max_workers: int, max_task_cost: str, reason: str,
              paying_warning: str = "") -> GovernorDecision:
    """Build a GovernorDecision with the standard fields pre-filled."""
    return GovernorDecision(
        action=action,
        max_workers=max_workers,
        max_task_cost=max_task_cost,
        mode=action,  # user-facing label mirrors the action in all current paths
        activity_cost=cost,
        spend_limit=spend_limit,
        reason=reason,
        paying_warning=paying_warning,
    )


def _weekly_override_decision(cost: float, spend_limit: float,
                              weekly: float) -> GovernorDecision:
    """Hard stop when the weekly quota is critical (>90%)."""
    return _decision(
        cost, spend_limit, action="stop", max_workers=0, max_task_cost="none",
        reason=f"weekly quota critical ({weekly:.0f}%) — wait for weekly reset",
    )


def _weekly_high_decision(session: float, weekly: float, cost: float,
                          spend_limit: float) -> GovernorDecision:
    """Weekly high (75–90%): throttle to tiny, stop if session is also critical."""
    if session > 80:
        return _decision(
            cost, spend_limit, action="stop", max_workers=0, max_task_cost="none",
            reason=f"both quotas critical (session={session:.0f}%, "
                   f"weekly={weekly:.0f}%)",
        )
    return _decision(
        cost, spend_limit, action="run", max_workers=1, max_task_cost="tiny",
        reason=f"weekly quota high ({weekly:.0f}%) — tiny tasks only",
    )


def _session_exhausted_decision(session: float, cost: float,
                                prev_activity_cost: float,
                                spend_limit: float) -> GovernorDecision:
    """Session >=100%: pay-as-you-go detection when cost is rising."""
    if cost > 0 and cost > prev_activity_cost:
        # Balance is being consumed → paying
        if spend_limit > 0 and cost >= spend_limit:
            return _decision(
                cost, spend_limit, action="stop", max_workers=0,
                max_task_cost="none",
                reason=f"spending limit reached (${cost:.2f} >= ${spend_limit:.2f})",
            )
        return _decision(
            cost, spend_limit, action="paying", max_workers=1, max_task_cost="small",
            paying_warning=(
                f"⚠ PAY-AS-YOU-GO: spending balance at ${cost:.2f} "
                f"(limit ${spend_limit:.2f})"
            ),
            reason=f"session exhausted, pay-as-you-go active (${cost:.2f} spent)",
        )
    # At 100% but cost not rising: no balance or balance exhausted
    return _decision(
        cost, spend_limit, action="stop", max_workers=0, max_task_cost="none",
        reason=f"session quota exhausted ({session:.0f}%) — no pay-as-you-go balance",
    )


def _session_run_decision(session: float, weekly: float, cost: float,
                          spend_limit: float) -> GovernorDecision:
    """Throttled run sub-levels by session, or healthy run when quota is free.

    Maps session 95%→30% to progressively lighter task limits, and defaults
    to the healthy run level below 30%.
    """
    if session > 95:
        return _decision(
            cost, spend_limit, action="run", max_workers=1, max_task_cost="micro",
            reason=f"session near limit ({session:.0f}%) — micro tasks only",
        )
    if session > 60:
        return _decision(
            cost, spend_limit, action="run", max_workers=1, max_task_cost="small",
            reason=f"session quota high ({session:.0f}%) — small tasks only",
        )
    if session > 30:
        return _decision(
            cost, spend_limit, action="run", max_workers=1, max_task_cost="medium",
            reason=f"session quota moderate ({session:.0f}%) — medium tasks max",
        )
    # --- Session < 30%, weekly < 75%: run healthy ---
    # P2 desired=3 (MEDIATOR t_acf726e6): the healthy run level equals the
    # operational minimum backlog (ready_assigned + running >= 3), so the
    # concurrency guard holds the board at the minimum while quota is free.
    # weekly < 50% keeps 3; a higher weekly falls back to 1 (throttle
    # before burn). Mirrored inline in scripts/quota-governor-tick.sh.
    max_workers = 3 if weekly < 50 else 1
    return _decision(
        cost, spend_limit, action="run", max_workers=max_workers,
        max_task_cost="any",
        reason=f"quota healthy (session={session:.0f}%, weekly={weekly:.0f}%)",
    )


def decide(snapshot: QuotaSnapshot, prev_activity_cost: float = 0.0,
           spending_limit: float | None = None) -> GovernorDecision:
    """Apply the composite quota heuristic to a snapshot.

    Entry point that branches on weekly and session thresholds; each stage's
    logic lives in the private ``_*`` helpers below. See the module docstring
    for the three-state model and the pay-as-you-go rules.

    Args:
      snapshot: current quota state from providers.
      prev_activity_cost: the previous observation's activity.cost (USD).
        Used to detect whether cost is rising (pay-as-you-go active).
      spending_limit: optional override for QUOTA_GOVERNOR_MAX_SPEND.
        When None (default), reads from env. Sibling callers pass this
        explicitly from the file-based persistence in quota_governor.py.
    """
    session = snapshot.session_pct
    weekly = snapshot.weekly_pct
    cost = snapshot.ollama_activity_cost  # actual field on QuotaSnapshot
    spend_limit = _resolve_spend_limit(spending_limit)

    if weekly > 90:
        return _weekly_override_decision(cost, spend_limit, weekly)
    if weekly > 75:
        return _weekly_high_decision(session, weekly, cost, spend_limit)
    if session >= 100:
        return _session_exhausted_decision(
            session, cost, prev_activity_cost, spend_limit)
    return _session_run_decision(session, weekly, cost, spend_limit)