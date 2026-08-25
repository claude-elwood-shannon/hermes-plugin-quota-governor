"""Quota-based decision heuristic.

Translates a ``QuotaSnapshot`` into a concrete ``GovernorDecision``:
how many workers to allow, what task types are safe, and whether to
stop entirely.

Based on the quota-planner document (Aug 2026) with real cost data:
  - ~137 calls fill an Ollama session to 100%
  - ~769 calls fill the weekly cap to 100%
  - Per call: ~0.0073 session, ~0.0013 weekly
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .providers import QuotaSnapshot


Action = Literal["run", "caution", "stop"]


@dataclass
class GovernorDecision:
    """What the governor recommends given current quota."""

    action: Action          # run = normal, caution = limited, stop = halt
    max_workers: int        # 0 when action == stop
    max_task_cost: str       # "micro", "tiny", "small", "medium", "complex", "any"
    reason: str             # human-readable explanation

    @property
    def should_spawn(self) -> bool:
        return self.action != "stop" and self.max_workers > 0


def decide(snapshot: QuotaSnapshot) -> GovernorDecision:
    """Apply the composite heuristic to a quota snapshot.

    Rules (from quota-planner.md, validated Aug 2026):

    Session (primary):
      < 30%   → run, 1-2 workers, any task
      30-60%  → run, 1 worker, medium max
      60-80%  → caution, 1 worker, small max
      > 80%   → stop (only micro tasks manually)

    Weekly (overruling):
      > 75%   → caution, tiny only
      > 90%   → stop entirely
    """
    session = snapshot.session_pct
    weekly = snapshot.weekly_pct

    # --- Weekly override (most restrictive wins) ---
    if weekly > 90:
        return GovernorDecision(
            action="stop",
            max_workers=0,
            max_task_cost="none",
            reason=f"weekly quota critical ({weekly:.0f}%) — wait for weekly reset",
        )

    if weekly > 75:
        if session > 80:
            return GovernorDecision(
                action="stop",
                max_workers=0,
                max_task_cost="none",
                reason=f"both quotas critical (session={session:.0f}%, "
                       f"weekly={weekly:.0f}%)",
            )
        return GovernorDecision(
            action="caution",
            max_workers=1,
            max_task_cost="tiny",
            reason=f"weekly quota high ({weekly:.0f}%) — tiny tasks only",
        )

    # --- Session-based decision ---
    if session > 95:
        return GovernorDecision(
            action="stop",
            max_workers=0,
            max_task_cost="none",
            reason=f"session quota exhausted ({session:.0f}%) — wait for reset",
        )

    if session > 80:
        return GovernorDecision(
            action="caution",
            max_workers=1,
            max_task_cost="micro",
            reason=f"session quota very high ({session:.0f}%) — micro tasks only",
        )

    if session > 60:
        return GovernorDecision(
            action="caution",
            max_workers=1,
            max_task_cost="small",
            reason=f"session quota high ({session:.0f}%) — small tasks only",
        )

    if session > 30:
        return GovernorDecision(
            action="run",
            max_workers=1,
            max_task_cost="medium",
            reason=f"session quota moderate ({session:.0f}%) — medium tasks max",
        )

    # Session < 30%, weekly < 75%
    max_workers = 2 if weekly < 50 else 1
    return GovernorDecision(
        action="run",
        max_workers=max_workers,
        max_task_cost="any",
        reason=f"quota healthy (session={session:.0f}%, weekly={weekly:.0f}%)",
    )