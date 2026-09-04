#!/usr/bin/env python3
"""objective-proposer.py — OBJ-16: Autonomous objective proposer.

Analyzes observations.jsonl and kanban.db for recurring patterns (errors,
quota imbalance, stale objectives, missing coverage) and proposes new
objectives in triage, validated against GR1-GR11 guardrails.

Design principles (mirrors verify-task.py, diagnose-crash.py, daily-report.py):
  - Dry-run by default; --execute to create triage tasks.
  - Silent on empty: empty stdout when nothing to propose.
  - Idempotent: objective-proposals.jsonl prevents re-proposal of same
    pattern within the same day (GR6: max 1 proposal/day).
  - No LLM: deterministic pattern matching only. This runs as no_agent cron.
  - All proposals go to triage (GR8): the user decides what to promote.

Usage:
  python3 objective-proposer.py              # dry-run, print what would be proposed
  python3 objective-proposer.py --execute    # create triage task if guardrails pass
  python3 objective-proposer.py --verbose    # show analysis details

Integration:
  - validate-guardrails.py: validates each proposal against GR1-GR11
  - objective-proposals.jsonl: append-only record of all proposals
  - kanban.db: queried for active objectives, stale tasks, crash patterns
  - observations.jsonl: queried for error patterns, quota trends

Exit codes:
  0 — success (proposed something or nothing to propose)
  1 — guardrails blocked the proposal (dry-run: informative, --execute: skipped)
  2 — script error (bad config, missing files)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ── Config ───────────────────────────────────────────────────────────────────

HERMES_HOME = os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))
KANBAN_DB = os.path.expanduser("~/.hermes/kanban.db")
OBSERVATIONS_FILE = os.path.expanduser(
    "~/.hermes/profiles/pr-ollama/quota-governor/observations.jsonl"
)
PROPOSALS_FILE = os.path.expanduser(
    "~/.hermes/quota-governor/objective-proposals.jsonl"
)
LOG_FILE = os.path.expanduser("~/.hermes/logs/objective-proposer.log")

# validate-guardrails.py location: prefer plugin repo, fall back to scripts dir
PLUGIN_REPO = "REPO"
VALIDATE_SCRIPT_CANDIDATES = [
    os.path.join(PLUGIN_REPO, "scripts", "validate-guardrails.py"),
    os.path.expanduser("~/.hermes/scripts/validate-guardrails.py"),
]

# hermes CLI for task creation
HERMES_CLI = os.environ.get("HERMES_CLI", "hermes")

# Analysis window: look back N days of observations
ANALYSIS_WINDOW_DAYS = 7

# Error threshold: if an error appears >= N times in the window, it's "recurring"
ERROR_RECURRENCE_THRESHOLD = 3

# Stale objective threshold: if no tasks completed for an objective in N days
STALE_OBJECTIVE_DAYS = 7

# Quota imbalance: if one provider consistently >80% while another <30%
QUOTA_IMBALANCE_HIGH = 80.0
QUOTA_IMBALANCE_LOW = 30.0
QUOTA_IMBALANCE_MIN_SAMPLES = 5

# Missing test coverage: scripts in plugin repo without corresponding test file
SCRIPT_DIR_PLUGIN = os.path.join(PLUGIN_REPO, "scripts")

VERBOSE = False


# ── Logging ──────────────────────────────────────────────────────────────────

def log(msg: str, level: str = "INFO") -> None:
    """Log to file, optionally print to stderr."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"[{ts}] [{level}] {msg}"
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    if VERBOSE or level in ("WARN", "ERROR"):
        print(line, file=sys.stderr)


# ── Data types ───────────────────────────────────────────────────────────────

@dataclass
class Pattern:
    """A detected pattern in observations or kanban data."""
    kind: str          # "recurring_error", "stale_objective", "quota_imbalance",
                       # "missing_test_coverage", "crash_cluster"
    severity: str      # "high", "medium", "low"
    title: str         # Proposed objective title
    body: str          # Proposed objective body
    evidence: str      # Evidence summary (for the proposal body)
    source: str        # Where the pattern was found


@dataclass
class ProposalResult:
    """Result of proposing an objective."""
    pattern: Pattern
    allowed: bool
    violations: List[Dict[str, str]] = field(default_factory=list)
    warnings: List[Dict[str, str]] = field(default_factory=list)
    task_id: Optional[str] = None
    error: Optional[str] = None


# ── Observations parsing ────────────────────────────────────────────────────

def load_observations(window_days: int = ANALYSIS_WINDOW_DAYS) -> List[Dict[str, Any]]:
    """Load observations from the JSONL file, within the analysis window."""
    if not os.path.exists(OBSERVATIONS_FILE):
        log(f"Observations file not found: {OBSERVATIONS_FILE}", "WARN")
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    observations = []

    try:
        with open(OBSERVATIONS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obs = json.loads(line)
                    ts_str = obs.get("timestamp", "")
                    # Parse ISO timestamp
                    ts = datetime.fromisoformat(
                        ts_str.replace("Z", "+00:00")
                    )
                    if ts >= cutoff:
                        observations.append(obs)
                except (json.JSONDecodeError, ValueError):
                    continue
    except OSError as e:
        log(f"Error reading observations: {e}", "ERROR")
        return []

    log(f"Loaded {len(observations)} observations from last {window_days} days")
    return observations


# ── Kanban DB queries ───────────────────────────────────────────────────────

def query_kanban_db(query: str, params: Tuple = ()) -> List[sqlite3.Row]:
    """Run a SELECT query against kanban.db."""
    if not os.path.exists(KANBAN_DB):
        log(f"Kanban DB not found: {KANBAN_DB}", "WARN")
        return []

    try:
        conn = sqlite3.connect(KANBAN_DB)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(query, params)
        rows = cursor.fetchall()
        conn.close()
        return rows
    except sqlite3.Error as e:
        log(f"Kanban DB error: {e}", "ERROR")
        return []


def get_active_objectives() -> Dict[str, Dict[str, Any]]:
    """Get objectives with non-terminal tasks and their last activity."""
    rows = query_kanban_db(
        "SELECT id, title, body, status, assignee, created_at, completed_at "
        "FROM tasks WHERE status IN ('ready', 'running', 'blocked', 'todo', 'triage')"
    )

    objectives: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        body = row["body"] or ""
        for match in re.finditer(r"objective:(OBJ-\d+)", body, re.IGNORECASE):
            obj_id = match.group(1).upper()
            if obj_id not in objectives:
                objectives[obj_id] = {
                    "task_ids": [],
                    "statuses": [],
                    "titles": [],
                }
            objectives[obj_id]["task_ids"].append(row["id"])
            objectives[obj_id]["statuses"].append(row["status"])
            objectives[obj_id]["titles"].append(row["title"] or "")

    return objectives


def get_completed_objectives_info() -> Dict[str, Dict[str, Any]]:
    """Get info about completed tasks grouped by objective."""
    rows = query_kanban_db(
        "SELECT id, title, body, status, completed_at "
        "FROM tasks WHERE status IN ('done', 'archived') AND completed_at IS NOT NULL "
        "ORDER BY completed_at DESC"
    )

    obj_info: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        body = row["body"] or ""
        for match in re.finditer(r"objective:(OBJ-\d+)", body, re.IGNORECASE):
            obj_id = match.group(1).upper()
            if obj_id not in obj_info:
                obj_info[obj_id] = {
                    "last_completed_at": row["completed_at"],
                    "completed_count": 0,
                }
            obj_info[obj_id]["completed_count"] += 1
            # Track most recent completion
            if row["completed_at"] and (
                "last_completed_at" not in obj_info[obj_id]
                or row["completed_at"] > obj_info[obj_id]["last_completed_at"]
            ):
                obj_info[obj_id]["last_completed_at"] = row["completed_at"]

    return obj_info


def get_crashed_tasks() -> List[Dict[str, Any]]:
    """Get tasks that have crashed (consecutive_failures > 0)."""
    rows = query_kanban_db(
        "SELECT id, title, body, status, consecutive_failures "
        "FROM tasks WHERE consecutive_failures > 0 "
        "AND status IN ('blocked', 'ready', 'todo')"
    )
    return [dict(row) for row in rows]


# ── Pattern detectors ────────────────────────────────────────────────────────

def _is_error_resolved(error_msg: str, observations: List[Dict[str, Any]]) -> bool:
    """Check if a recurring error has already been resolved.

    An error is considered resolved if BOTH:
    1. It has NOT appeared in observations in the last 48h (staleness check)
    2. There exists a done/archived kanban task whose title mentions keywords
       from the error (fix verification check)

    This prevents false-positive proposals for errors that were already fixed
    but whose old occurrences remain in the 7-day observation window.
    """
    # Check 1: Did the error appear in the last 48h?
    recent_cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
    has_recent = False
    for obs in observations:
        ts_str = obs.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if ts < recent_cutoff:
            continue
        quota = obs.get("quota", {})
        errors = quota.get("errors", []) if isinstance(quota, dict) else []
        for err in errors:
            normalized = re.sub(r"\d+", "N", err)
            normalized = re.sub(r"\s+", " ", normalized).strip()
            obs_normalized = re.sub(r"\d+", "N", error_msg)
            obs_normalized = re.sub(r"\s+", " ", obs_normalized).strip()
            if normalized == obs_normalized:
                has_recent = True
                break
        if has_recent:
            break

    if has_recent:
        return False  # Error is still active

    # Check 2: Is there a done task that fixes this error?
    # Extract keywords from the error message for matching
    # e.g. "nanogpt: HTTP Error N: Forbidden" → keywords: "nanogpt", "Forbidden"
    keywords = []
    parts = re.split(r"[:\s]+", error_msg)
    for p in parts:
        p_clean = re.sub(r"[^a-zA-Z]", "", p).lower()
        if len(p_clean) >= 4 and p_clean not in ("http", "error", "urlopen", "tunnel"):
            keywords.append(p_clean)
    if not keywords:
        return False  # Can't extract keywords, don't assume resolved

    done_rows = query_kanban_db(
        "SELECT title, body FROM tasks WHERE status IN ('done', 'archived') "
        "AND completed_at IS NOT NULL ORDER BY completed_at DESC LIMIT 100"
    )
    for row in done_rows:
        text = ((row["title"] or "") + " " + (row["body"] or "")).lower()
        # Match if at least 2 distinctive keywords are found in the task
        matches = sum(1 for kw in keywords if kw in text)
        if matches >= 2:
            log(
                f"Error '{error_msg[:60]}' considered resolved: "
                f"done task matches keywords {keywords}",
            )
            return True

    return False


def detect_recurring_errors(observations: List[Dict[str, Any]]) -> Optional[Pattern]:
    """Detect errors that appear >= ERROR_RECURRENCE_THRESHOLD times in the window.

    Looks at the `errors` field in observations and the `event` field for
    session_end events with errors.

    Skips errors that have already been resolved (no occurrences in last 48h
    AND a done kanban task exists that addresses the error).
    """
    error_counter = Counter()
    error_to_observations: Dict[str, List[Dict[str, Any]]] = {}

    for obs in observations:
        quota = obs.get("quota", {})
        errors = quota.get("errors", []) if isinstance(quota, dict) else []
        for err in errors:
            # Normalize: remove variable parts (timestamps, PIDs)
            normalized = re.sub(r"\d+", "N", err)
            normalized = re.sub(r"\s+", " ", normalized).strip()
            error_counter[normalized] += 1
            error_to_observations.setdefault(normalized, []).append(obs)

    if not error_counter:
        return None

    # Sort by frequency (most common first), then check each for eligibility
    for error_msg, count in error_counter.most_common():
        if count < ERROR_RECURRENCE_THRESHOLD:
            continue

        # Check if this error was already resolved (fix done + no recent occurrences)
        if _is_error_resolved(error_msg, error_to_observations.get(error_msg, observations)):
            log(f"Recurring error already resolved, skipping: {error_msg[:60]}")
            continue

        # Check if this error was already proposed recently
        if _already_proposed(f"recurring_error:{error_msg[:80]}"):
            log(f"Recurring error already proposed recently: {error_msg[:60]}")
            continue

        # Found an eligible error — build the proposal
        break
    else:
        return None  # No eligible errors

    # Build proposal
    severity = "high" if count >= 5 else "medium"

    title = f"OBJ-X: Fix recurring error in quota observations ({count}x in {ANALYSIS_WINDOW_DAYS}d)"
    body = (
        f"objective:OBJ-X\n"
        f"auto_created:true\n"
        f"cost:small\n"
        f"provider:pr-ollama\n\n"
        f"Recurring error detected in observations.jsonl: appears {count} times "
        f"in the last {ANALYSIS_WINDOW_DAYS} days.\n\n"
        f"Error pattern: {error_msg}\n\n"
        f"Evidence:\n"
        f"- Occurrences: {count}\n"
        f"- Window: last {ANALYSIS_WINDOW_DAYS} days\n"
        f"- Source: observations.jsonl\n\n"
        f"Tareas que lo avanzan:\n"
        f"- Investigar la causa raiz del error\n"
        f"- Implementar fix en el codigo del plugin o scripts\n"
        f"- Verificar que el error desaparece en observaciones posteriores\n\n"
        f"Criterio de completitud: el error no aparece en las observaciones "
        f"de las ultimas 48h."
    )
    evidence = f"Error '{error_msg[:100]}' appeared {count} times in {ANALYSIS_WINDOW_DAYS}d"

    return Pattern(
        kind="recurring_error",
        severity=severity,
        title=title,
        body=body,
        evidence=evidence,
        source="observations.jsonl",
    )


def detect_quota_imbalance(observations: List[Dict[str, Any]]) -> Optional[Pattern]:
    """Detect quota imbalance: one provider consistently high while another low.

    If Ollama is consistently >80% while NanoGPT or OpenRouter is <30%, that
    suggests the system is not using the multi-provider routing effectively.
    """
    # Collect quota samples
    ollama_high = 0
    ollama_total = 0
    nanogpt_low = 0
    nanogpt_total = 0
    openrouter_low = 0
    openrouter_total = 0

    for obs in observations:
        quota = obs.get("quota", {})
        if not isinstance(quota, dict):
            continue

        ollama_pct = quota.get("ollama_weekly_pct")
        if ollama_pct is not None:
            ollama_total += 1
            if ollama_pct > QUOTA_IMBALANCE_HIGH:
                ollama_high += 1

        nanogpt_pct = quota.get("nanogpt_weekly_tokens_pct")
        if nanogpt_pct is not None:
            nanogpt_total += 1
            if nanogpt_pct < QUOTA_IMBALANCE_LOW:
                nanogpt_low += 1

        openrouter_usd = quota.get("openrouter_weekly_usd")
        if openrouter_usd is not None:
            openrouter_total += 1
            if openrouter_usd < 1.0:  # Very low usage
                openrouter_low += 1

    # Need enough samples to be meaningful
    if ollama_total < QUOTA_IMBALANCE_MIN_SAMPLES:
        return None

    ollama_high_ratio = ollama_high / ollama_total if ollama_total > 0 else 0
    nanogpt_low_ratio = nanogpt_low / nanogpt_total if nanogpt_total > 0 else 0

    # Ollama consistently high AND NanoGPT consistently low
    if ollama_high_ratio > 0.6 and nanogpt_low_ratio > 0.6 and nanogpt_total >= 3:
        if _already_proposed("quota_imbalance:ollama_high_nanogpt_low"):
            return None

        title = f"OBJ-X: Rebalance quota usage — Ollama consistently high while NanoGPT underutilized"
        body = (
            f"objective:OBJ-X\n"
            f"auto_created:true\n"
            f"cost:small\n"
            f"provider:pr-ollama\n\n"
            f"Quota imbalance detected: Ollama weekly usage is consistently "
            f">{QUOTA_IMBALANCE_HIGH}% while NanoGPT is <{QUOTA_IMBALANCE_LOW}%.\n\n"
            f"Evidence:\n"
            f"- Ollama >{QUOTA_IMBALANCE_HIGH}%: {ollama_high}/{ollama_total} samples "
            f"({ollama_high_ratio:.0%})\n"
            f"- NanoGPT <{QUOTA_IMBALANCE_LOW}%: {nanogpt_low}/{nanogpt_total} samples "
            f"({nanogpt_low_ratio:.0%})\n"
            f"- Window: last {ANALYSIS_WINDOW_DAYS} days\n\n"
            f"Tareas que lo avanzan:\n"
            f"- Investigar por que el multi-provider routing no usa NanoGPT\n"
            f"- Verificar quota-gate.py recommended_profile logic\n"
            f"- Ajustar heuristica si necesario\n\n"
            f"Criterio de completitud: NanoGPT usage >30% en al menos 3 "
            f"observaciones consecutivas tras el fix."
        )
        evidence = (
            f"Ollama high: {ollama_high}/{ollama_total}, "
            f"NanoGPT low: {nanogpt_low}/{nanogpt_total}"
        )

        return Pattern(
            kind="quota_imbalance",
            severity="medium",
            title=title,
            body=body,
            evidence=evidence,
            source="observations.jsonl",
        )

    return None


def detect_stale_objectives() -> Optional[Pattern]:
    """Detect objectives that have been in-progress for too long without progress."""
    active = get_active_objectives()
    completed_info = get_completed_objectives_info()

    now_ts = datetime.now(timezone.utc).timestamp()
    stale_threshold_ts = now_ts - (STALE_OBJECTIVE_DAYS * 86400)

    stale_objectives = []
    for obj_id, info in active.items():
        # Check if this objective has had any recent completions
        comp_info = completed_info.get(obj_id, {})
        last_completed = comp_info.get("last_completed_at", 0)

        if last_completed and last_completed > stale_threshold_ts:
            continue  # Had recent progress

        # Check if it was created recently (still warming up)
        # If all tasks are just 'triage' or 'todo', it might be new
        statuses = info.get("statuses", [])
        if all(s in ("triage", "todo") for s in statuses):
            continue  # Not started yet, not stale

        stale_objectives.append((obj_id, info))

    if not stale_objectives:
        return None

    # Pick the stalest
    stale_objectives.sort(key=lambda x: x[0])  # By OBJ-N number
    obj_id, info = stale_objectives[0]

    if _already_proposed(f"stale_objective:{obj_id}"):
        return None

    title = f"OBJ-X: Diagnose stale objective {obj_id} — no progress in {STALE_OBJECTIVE_DAYS}d"
    body = (
        f"objective:OBJ-X\n"
        f"auto_created:true\n"
        f"cost:tiny\n"
        f"provider:pr-ollama\n\n"
        f"Objective {obj_id} has active tasks but no completions in the last "
        f"{STALE_OBJECTIVE_DAYS} days.\n\n"
        f"Evidence:\n"
        f"- Active tasks: {len(info.get('task_ids', []))}\n"
        f"- Statuses: {', '.join(info.get('statuses', []))}\n"
        f"- Last completion: >{STALE_OBJECTIVE_DAYS} days ago (or never)\n\n"
        f"Tareas que lo avanzan:\n"
        f"- Investigar por que el objetivo esta estancado\n"
        f"- Si las tareas estan bloqueadas, diagnosticar el bloqueo\n"
        f"- Si las tareas son demasiado grandes, dividirlas\n"
        f"- Proponer siguiente paso concreto\n\n"
        f"Criterio de completitud: al menos 1 tarea completada para {obj_id} "
        f"tras la investigacion."
    )
    evidence = f"Objective {obj_id} has {len(info.get('task_ids', []))} active tasks, 0 completions in {STALE_OBJECTIVE_DAYS}d"

    return Pattern(
        kind="stale_objective",
        severity="medium",
        title=title,
        body=body,
        evidence=evidence,
        source="kanban.db",
    )


def detect_missing_test_coverage() -> Optional[Pattern]:
    """Detect plugin scripts without corresponding test files."""
    if not os.path.isdir(SCRIPT_DIR_PLUGIN):
        return None

    scripts = []
    for f in os.listdir(SCRIPT_DIR_PLUGIN):
        if f.endswith(".py") and not f.startswith("__"):
            scripts.append(f)

    if not scripts:
        return None

    # Check for test files in the repo root
    repo_root = PLUGIN_REPO
    test_files = set()
    for f in os.listdir(repo_root):
        if f.startswith("test_") and f.endswith(".py"):
            test_files.add(f)

    # Also check scripts dir for test files
    script_tests = set()
    for f in os.listdir(SCRIPT_DIR_PLUGIN):
        if f.startswith("test_") and f.endswith(".py"):
            script_tests.add(f)

    uncovered = []
    for script in scripts:
        # Convention: foo.py -> test_foo.py
        expected_test = f"test_{script}"
        if expected_test not in test_files and expected_test not in script_tests:
            uncovered.append(script)

    if not uncovered:
        return None

    if _already_proposed("missing_test_coverage"):
        return None

    uncovered_str = ", ".join(uncovered[:5])
    title = f"OBJ-X: Add test coverage for uncovered plugin scripts ({len(uncovered)} scripts)"
    body = (
        f"objective:OBJ-X\n"
        f"auto_created:true\n"
        f"cost:small\n"
        f"provider:pr-ollama\n\n"
        f"Scripts without test coverage detected in the plugin repo.\n\n"
        f"Evidence:\n"
        f"- Uncovered scripts: {uncovered_str}\n"
        f"- Total uncovered: {len(uncovered)}\n"
        f"- Scripts dir: scripts/\n\n"
        f"Tareas que lo avanzan:\n"
        f"- Para cada script sin tests, crear test_<script>.py\n"
        f"- Cubrir casos principales y edge cases\n"
        f"- Verificar que todos los tests pasan\n\n"
        f"Criterio de completitud: cada script en scripts/ tiene un test "
        f"file correspondiente con al menos 3 tests."
    )
    evidence = f"Uncovered scripts: {uncovered_str}"

    return Pattern(
        kind="missing_test_coverage",
        severity="low",
        title=title,
        body=body,
        evidence=evidence,
        source="plugin_repo",
    )


def detect_crash_cluster() -> Optional[Pattern]:
    """Detect multiple tasks that have crashed, suggesting a systemic issue."""
    crashed = get_crashed_tasks()
    if len(crashed) < 2:
        return None

    if _already_proposed("crash_cluster"):
        return None

    titles = [t.get("title", t.get("id", "?")) for t in crashed[:3]]
    titles_str = "; ".join(titles)

    title = f"OBJ-X: Investigate crash cluster — {len(crashed)} tasks with failures"
    body = (
        f"objective:OBJ-X\n"
        f"auto_created:true\n"
        f"cost:tiny\n"
        f"provider:pr-ollama\n\n"
        f"Multiple tasks have consecutive failures, suggesting a systemic issue.\n\n"
        f"Evidence:\n"
        f"- Crashed tasks: {len(crashed)}\n"
        f"- Sample: {titles_str}\n\n"
        f"Tareas que lo avanzan:\n"
        f"- Revisar los logs de cada tarea crasheada\n"
        f"- Identificar patrones comunes (mismo error, mismo tipo de tarea)\n"
        f"- Si hay una causa comun, proponer fix\n\n"
        f"Criterio de completitud: ninguna tarea con consecutive_failures > 0 "
        f"tras el fix."
    )
    evidence = f"{len(crashed)} crashed tasks: {titles_str}"

    return Pattern(
        kind="crash_cluster",
        severity="high",
        title=title,
        body=body,
        evidence=evidence,
        source="kanban.db",
    )


# ── Proposal deduplication ──────────────────────────────────────────────────

def _already_proposed(pattern_key: str, *, cross_day: bool = True) -> bool:
    """Check if a pattern was already proposed (GR6 enforcement + cross-day dedup).

    Checks ALL entries in the proposals file (both allowed and rejected).
    A rejected entry still means "we already saw this pattern and decided
    not to propose it" — re-proposing it every tick wastes the GR6 daily
    quota and clutters the proposals file with duplicates.

    By default (*cross_day=True*), also checks if the same pattern appeared
    on a **previous** day within the analysis window. This prevents the
    proposer from re-proposing the same error every day when stale errors
    remain in the 7-day observation window but have already been addressed
    by a prior task.

    The cross-day check matches on the ``pattern_key`` substring (e.g.
    ``recurring_error:Error 'nanogpt: HTTP Error N: Forbidden'``) appearing
    in the ``evidence`` or ``title`` of any past entry.
    """
    if not os.path.exists(PROPOSALS_FILE):
        return False

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # Extract a short normalized key from pattern_key for fuzzy matching.
    # pattern_key looks like: "recurring_error:Error 'nanogpt: HTTP Error N: Forbidden' appeared 4 times in 7d"
    # We extract the core error signature for cross-day matching.
    short_key = pattern_key.split(":", 1)[-1][:60].lower() if ":" in pattern_key else pattern_key[:60].lower()
    # For bare-kind pattern_keys (no colon, e.g. "missing_test_coverage"),
    # derive the kind for prefix matching against recorded entries.
    # record_proposal() stores pattern_key as "kind:evidence", so a bare
    # kind argument will never exactly match — we need prefix matching.
    bare_kind = pattern_key if ":" not in pattern_key else None

    try:
        with open(PROPOSALS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    entry_date = entry.get("date", "")

                    # Same-day dedup: any entry (allowed OR rejected) with the
                    # same pattern blocks re-proposal today. This prevents the
                    # 2h-cron from re-proposing a rejected pattern every tick.
                    if entry_date == today:
                        if entry.get("pattern_key") == pattern_key:
                            return True
                        # Kind-prefix match: when pattern_key is a bare kind
                        # (e.g. "missing_test_coverage"), match against the
                        # recorded pattern_kind field or pattern_key prefix.
                        # Fixes OBJ-16: detect_missing_test_coverage() and
                        # detect_crash_cluster() pass bare kinds, but
                        # record_proposal() records "kind:evidence".
                        if bare_kind:
                            if entry.get("pattern_kind", "") == bare_kind:
                                return True
                            if entry.get("pattern_key", "").startswith(bare_kind + ":"):
                                return True
                        if pattern_key in entry.get("title", "").lower():
                            return True
                        # Also check evidence field (pattern_key includes evidence)
                        if short_key and short_key in entry.get("evidence", "").lower():
                            return True

                    # Cross-day dedup: if the same pattern was seen on a
                    # previous day (allowed OR rejected), don't re-propose.
                    # This prevents stale errors in the observation window
                    # from generating duplicate tasks across days.
                    if cross_day and entry_date < today:
                        # Kind-prefix match for bare-kind pattern_keys
                        if bare_kind and entry.get("pattern_kind", "") == bare_kind:
                            return True
                        entry_text = (
                            entry.get("evidence", "") + " " + entry.get("title", "")
                        ).lower()
                        if short_key and short_key in entry_text:
                            return True

                except json.JSONDecodeError:
                    continue
    except OSError:
        pass

    return False


# ── Guardrails validation ───────────────────────────────────────────────────

def find_validate_script() -> Optional[str]:
    """Find validate-guardrails.py."""
    for path in VALIDATE_SCRIPT_CANDIDATES:
        if os.path.exists(path):
            return path
    return None


def validate_proposal(title: str, body: str) -> Tuple[bool, List[Dict], List[Dict]]:
    """Validate a proposal against guardrails using validate-guardrails.py.

    Returns (allowed, violations, warnings).
    """
    script = find_validate_script()
    if not script:
        log("validate-guardrails.py not found — skipping validation", "WARN")
        # Can't validate — fail open (return allowed with a warning)
        return True, [], [{"id": "VALIDATOR", "message": "Validator script not found"}]

    cmd = [
        sys.executable, script,
        "--title", title,
        "--body", body,
        "--kanban-db", KANBAN_DB,
        "--state-file", PROPOSALS_FILE,
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            return data.get("allowed", False), data.get("violations", []), data.get("warnings", [])
        else:
            # Exit 1 = guardrails blocked
            try:
                data = json.loads(result.stdout)
                return data.get("allowed", False), data.get("violations", []), data.get("warnings", [])
            except json.JSONDecodeError:
                log(f"Validator output parse error: {result.stdout[:200]}", "ERROR")
                return False, [{"id": "PARSE", "message": "Validator output not parseable"}], []
    except subprocess.TimeoutExpired:
        log("Validator timed out", "ERROR")
        return False, [{"id": "TIMEOUT", "message": "Validator timed out"}], []
    except OSError as e:
        log(f"Validator execution error: {e}", "ERROR")
        return False, [{"id": "EXEC", "message": str(e)}], []


# ── Proposal recording ───────────────────────────────────────────────────────

def record_proposal(
    pattern: Pattern,
    allowed: bool,
    violations: List[Dict],
    warnings: List[Dict],
    task_id: Optional[str] = None,
) -> None:
    """Record a proposal in the proposals file.

    Dedup: if an entry with the same ``pattern_kind`` was already recorded
    today (regardless of allowed/rejected), skip appending a duplicate.
    This is a defense-in-depth measure — ``_already_proposed`` should
    suppress detection before we get here, but if it somehow misses (e.g.,
    a subtle key mismatch), this prevents the proposals file from growing
    with duplicate entries every 2h cron tick.

    Fixes OBJ-16 root cause 3: record_proposal had no dedup, so even when
    _already_proposed failed to suppress, each tick appended a new entry,
    amplifying the spam.
    """
    os.makedirs(os.path.dirname(PROPOSALS_FILE), exist_ok=True)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Dedup: check if an entry with the same pattern_kind already exists today.
    if os.path.exists(PROPOSALS_FILE):
        try:
            with open(PROPOSALS_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        existing = json.loads(line)
                        if (existing.get("date", "") == today
                                and existing.get("pattern_kind", "") == pattern.kind):
                            # Already recorded this pattern_kind today — skip.
                            return
                    except json.JSONDecodeError:
                        continue
        except OSError:
            pass

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "date": today,
        "title": pattern.title,
        "pattern_kind": pattern.kind,
        "pattern_key": f"{pattern.kind}:{pattern.evidence[:80]}",
        "allowed": allowed,
        "violations": violations,
        "warnings": warnings,
        "requires_human_approval": len(warnings) > 0,
        "evidence": pattern.evidence,
        "source": pattern.source,
        "severity": pattern.severity,
    }
    if task_id:
        entry["task_id"] = task_id

    try:
        with open(PROPOSALS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as e:
        log(f"Error recording proposal: {e}", "ERROR")


# ── Task creation ────────────────────────────────────────────────────────────

def create_triage_task(title: str, body: str) -> Optional[str]:
    """Create a task in triage via hermes kanban create.

    Returns the task_id if successful, None otherwise.
    """
    cmd = [
        HERMES_CLI, "kanban", "create",
        title,
        "--body", body,
        "--assignee", "pr-ollama",
        "--triage",
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            # Parse task ID from output
            output = result.stdout.strip()
            # hermes kanban create outputs the task ID
            # Try to find a t_XXXX pattern
            match = re.search(r"(t_[a-f0-9]+)", output)
            if match:
                return match.group(1)
            # If no match, return the raw output as evidence
            log(f"Could not parse task ID from: {output[:200]}", "WARN")
            return None
        else:
            log(f"hermes kanban create failed: {result.stderr[:200]}", "ERROR")
            return None
    except subprocess.TimeoutExpired:
        log("hermes kanban create timed out", "ERROR")
        return None
    except OSError as e:
        log(f"hermes kanban create error: {e}", "ERROR")
        return None


# ── Main analysis ───────────────────────────────────────────────────────────

def run_analysis() -> List[Pattern]:
    """Run all pattern detectors and return findings.

    Detectors run in priority order (high severity first). Returns all
    detected patterns, sorted by severity.
    """
    observations = load_observations()
    patterns: List[Pattern] = []

    # Detector 1: Recurring errors (high severity)
    p = detect_recurring_errors(observations)
    if p:
        patterns.append(p)
        log(f"Detected: {p.kind} — {p.evidence}")

    # Detector 2: Crash cluster (high severity)
    p = detect_crash_cluster()
    if p:
        patterns.append(p)
        log(f"Detected: {p.kind} — {p.evidence}")

    # Detector 3: Quota imbalance (medium severity)
    p = detect_quota_imbalance(observations)
    if p:
        patterns.append(p)
        log(f"Detected: {p.kind} — {p.evidence}")

    # Detector 4: Stale objective (medium severity)
    p = detect_stale_objectives()
    if p:
        patterns.append(p)
        log(f"Detected: {p.kind} — {p.evidence}")

    # Detector 5: Missing test coverage (low severity)
    p = detect_missing_test_coverage()
    if p:
        patterns.append(p)
        log(f"Detected: {p.kind} — {p.evidence}")

    # Sort by severity: high > medium > low
    severity_order = {"high": 0, "medium": 1, "low": 2}
    patterns.sort(key=lambda p: severity_order.get(p.severity, 99))

    return patterns


def propose_objective(
    pattern: Pattern,
    execute: bool = False,
) -> ProposalResult:
    """Validate and optionally create a triage task for a pattern.

    GR6 is enforced by validate-guardrails.py (checks proposals file).
    We also pre-check with _already_proposed to avoid unnecessary validation.
    """
    # Validate against guardrails
    allowed, violations, warnings = validate_proposal(
        pattern.title, pattern.body
    )

    result = ProposalResult(
        pattern=pattern,
        allowed=allowed,
        violations=violations,
        warnings=warnings,
    )

    if not allowed:
        log(
            f"Proposal BLOCKED by guardrails: {pattern.title} — "
            f"violations: {[v['id'] for v in violations]}",
            "WARN",
        )
        # Record the rejected proposal (always, so we don't retry it repeatedly)
        record_proposal(pattern, allowed, violations, warnings)
        return result

    # Guardrails passed — create triage task if --execute
    if execute:
        task_id = create_triage_task(pattern.title, pattern.body)
        result.task_id = task_id
        if task_id:
            log(f"Created triage task {task_id}: {pattern.title}")
            # Record the proposal (only when task was actually created)
            record_proposal(pattern, allowed, violations, warnings, task_id=result.task_id)
        else:
            log(f"Failed to create triage task: {pattern.title}", "ERROR")
            result.error = "Task creation failed"
            # Do NOT record — task wasn't created, so GR6 should not count it
    else:
        log(f"Dry-run — would create triage task: {pattern.title}")
        # Do NOT record in dry-run mode: GR6 counts recorded proposals
        # with allowed=true against the daily limit. Recording in dry-run
        # would block the subsequent --execute run.

    return result


# ── CLI ─────────────────────────────────────────────────────────────────────

def main():
    global VERBOSE

    parser = argparse.ArgumentParser(
        description="OBJ-16: Autonomous objective proposer with guardrails."
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Create triage tasks for proposals that pass guardrails. "
             "Without this flag, runs in dry-run mode.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show detailed analysis output.",
    )
    parser.add_argument(
        "--window-days",
        type=int,
        default=ANALYSIS_WINDOW_DAYS,
        help=f"Analysis window in days (default: {ANALYSIS_WINDOW_DAYS})",
    )
    args = parser.parse_args()
    VERBOSE = args.verbose

    log(f"objective-proposer started (execute={args.execute}, window={args.window_days}d)")

    # Run pattern detection
    patterns = run_analysis()

    if not patterns:
        log("No patterns detected — nothing to propose")
        # Silent exit (empty stdout for no_agent cron)
        sys.exit(0)

    # Process patterns in priority order
    # GR6 limits to 1 proposal per day, so we try patterns in order
    # and stop after the first one that passes guardrails
    results: List[ProposalResult] = []
    proposed = False

    for pattern in patterns:
        if VERBOSE:
            print(f"\n--- Pattern: {pattern.kind} (severity: {pattern.severity}) ---")
            print(f"Title: {pattern.title}")
            print(f"Evidence: {pattern.evidence}")
            print()

        result = propose_objective(pattern, execute=args.execute)
        results.append(result)

        if result.allowed:
            proposed = True
            # Print summary for cron delivery
            if result.task_id:
                print(f"PROPOSED: {result.pattern.title}")
                print(f"  Task: {result.task_id}")
                print(f"  Evidence: {result.pattern.evidence}")
                if result.warnings:
                    print(f"  Warnings: {[w['id'] for w in result.warnings]}")
            else:
                status = "would create" if not args.execute else "FAILED to create"
                print(f"PROPOSED (dry-run): {result.pattern.title}")
                print(f"  Status: {status}")
                print(f"  Evidence: {result.pattern.evidence}")
                if result.warnings:
                    print(f"  Warnings: {[w['id'] for w in result.warnings]}")

            # GR6: max 1 proposal per day — stop after first success
            break
        else:
            if VERBOSE:
                print(f"BLOCKED: {result.pattern.title}")
                print(f"  Violations: {[v['id'] for v in result.violations]}")

            # If blocked by GR6 (daily limit), stop trying
            gr6_blocked = any(v["id"] == "GR6" for v in result.violations)
            if gr6_blocked:
                log("GR6 daily limit reached — stopping", "INFO")
                break

    # Summary
    if not proposed and not VERBOSE:
        # No proposal was made and not verbose — stay silent
        pass

    log(
        f"objective-proposer finished: {len(results)} patterns analyzed, "
        f"{'1 proposed' if proposed else '0 proposed'}"
    )

    # Exit 0 if we proposed something or had nothing to propose
    # Exit 1 if guardrails blocked all proposals (but this is informational)
    sys.exit(0)


if __name__ == "__main__":
    main()