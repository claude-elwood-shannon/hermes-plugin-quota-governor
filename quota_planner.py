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

Per-call cost data (Aug 2026):
  - ~137 calls fill an Ollama session to 100%
  - ~769 calls fill the weekly cap to 100%
  - Per call: ~0.0073 session, ~0.0013 weekly
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


def decide(snapshot: QuotaSnapshot, prev_activity_cost: float = 0.0,
           spending_limit: float | None = None) -> GovernorDecision:
    """Apply the composite heuristic to a quota snapshot.

    Rules (from pay-as-you-go-design.md, validated Aug 2026):

    Weekly (overruling, hard stop):
      > 90%   → stop entirely

    Weekly (high, throttle):
      > 75%   → run, 1 worker, tiny tasks (session <= 80)
               stop if session > 80 (both critical)

    Session >= 100% (pay-as-you-go detection):
      cost rising AND cost < spend_limit (or limit disabled) → paying (1 worker, small)
      cost rising AND cost >= spend_limit (limit > 0)         → stop (spending limit hit)
      cost not rising                                         → stop (balance exhausted or no balance)

    Session > 95% (but < 100%):
      → run, 1 worker, micro (heavily throttled, not stopped)

    Session 60–95%:
      → run, 1 worker, small

    Session 30–60%:
      → run, 1 worker, medium

    Session < 30%:
      → run, 1-2 workers, any

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

    # Spend limit: explicit override (from sibling callers) or env-read
    if spending_limit is not None:
        spend_limit = spending_limit
    else:
        spend_limit = _read_spend_limit()

    # --- Weekly override (hard stop, unchanged) ---
    if weekly > 90:
        return GovernorDecision(
            action="stop",
            max_workers=0,
            max_task_cost="none",
            mode="stop",
            activity_cost=cost,
            spend_limit=spend_limit,
            reason=f"weekly quota critical ({weekly:.0f}%) — wait for weekly reset",
        )

    # --- Weekly high (throttle to run sub-level) ---
    if weekly > 75:
        if session > 80:
            return GovernorDecision(
                action="stop",
                max_workers=0,
                max_task_cost="none",
                mode="stop",
                activity_cost=cost,
                spend_limit=spend_limit,
                reason=f"both quotas critical (session={session:.0f}%, "
                       f"weekly={weekly:.0f}%)",
            )
        return GovernorDecision(
            action="run",
            max_workers=1,
            max_task_cost="tiny",
            mode="run",
            activity_cost=cost,
            spend_limit=spend_limit,
            reason=f"weekly quota high ({weekly:.0f}%) — tiny tasks only",
        )

    # --- Session >= 100%: pay-as-you-go detection ---
    if session >= 100:
        if cost > 0 and cost > prev_activity_cost:
            # Balance is being consumed → paying
            if spend_limit > 0 and cost >= spend_limit:
                return GovernorDecision(
                    action="stop",
                    max_workers=0,
                    max_task_cost="none",
                    mode="stop",
                    activity_cost=cost,
                    spend_limit=spend_limit,
                    reason=f"spending limit reached (${cost:.2f} >= ${spend_limit:.2f})",
                )
            return GovernorDecision(
                action="paying",
                max_workers=1,
                max_task_cost="small",
                mode="paying",
                activity_cost=cost,
                spend_limit=spend_limit,
                paying_warning=f"⚠ PAY-AS-YOU-GO: spending balance at ${cost:.2f} (limit ${spend_limit:.2f})",
                reason=f"session exhausted, pay-as-you-go active (${cost:.2f} spent)",
            )
        # At 100% but cost not rising: no balance or balance exhausted
        return GovernorDecision(
            action="stop",
            max_workers=0,
            max_task_cost="none",
            mode="stop",
            activity_cost=cost,
            spend_limit=spend_limit,
            reason=f"session quota exhausted ({session:.0f}%) — no pay-as-you-go balance",
        )

    # --- Session > 95% (but < 100%): heavily throttled run ---
    if session > 95:
        return GovernorDecision(
            action="run",
            max_workers=1,
            max_task_cost="micro",
            mode="run",
            activity_cost=cost,
            spend_limit=spend_limit,
            reason=f"session near limit ({session:.0f}%) — micro tasks only",
        )

    # --- Session 60–95%: run cautious ---
    if session > 60:
        return GovernorDecision(
            action="run",
            max_workers=1,
            max_task_cost="small",
            mode="run",
            activity_cost=cost,
            spend_limit=spend_limit,
            reason=f"session quota high ({session:.0f}%) — small tasks only",
        )

    # --- Session 30–60%: run moderate ---
    if session > 30:
        return GovernorDecision(
            action="run",
            max_workers=1,
            max_task_cost="medium",
            mode="run",
            activity_cost=cost,
            spend_limit=spend_limit,
            reason=f"session quota moderate ({session:.0f}%) — medium tasks max",
        )

    # --- Session < 30%, weekly < 75%: run healthy ---
    max_workers = 2 if weekly < 50 else 1
    return GovernorDecision(
        action="run",
        max_workers=max_workers,
        max_task_cost="any",
        mode="run",
        activity_cost=cost,
        spend_limit=spend_limit,
        reason=f"quota healthy (session={session:.0f}%, weekly={weekly:.0f}%)",
    )