#!/usr/bin/env python3
"""quota-gate.py — pre-run script for the autonomous task creator cron job.

Checks Ollama Cloud quota. If quota is healthy, wakes the agent with
context about available headroom. If quota is critical, stays silent
(zero tokens, no agent run).

Output (last line, JSON):
  {"wakeAgent": false}  — skip this tick, quota too low
  {"wakeAgent": true, "context": {"session_pct": X, "weekly_pct": Y, "max_workers": N}}
    — wake the agent with quota context

Silent watchdog: if quota is fine but there's nothing to do (no tasks
in kanban that need creating), the agent itself will respond [SILENT].
"""
import json
import os
import sys
import urllib.request

def get_env(key):
    """Read from environment or .env file."""
    val = os.environ.get(key)
    if val:
        return val
    for path in (
        os.path.expanduser("~/.hermes/profiles/pr-ollama/.env"),
        os.path.expanduser("~/.hermes/.env"),
    ):
        if os.path.isfile(path):
            try:
                with open(path) as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and line.startswith(f"{key}="):
                            return line.split("=", 1)[1].strip().strip("'\"")
            except OSError:
                pass
    return None

def query_ollama():
    api_key = get_env("OLLAMA_API_KEY")
    if not api_key:
        return None, "no API key"
    req = urllib.request.Request(
        "https://ollama.com/api/usage",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    # Disable proxy — Ollama rejects Tor
    saved = {}
    for var in ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"]:
        if var in os.environ:
            saved[var] = os.environ.pop(var)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    finally:
        os.environ.update(saved)

    session = data.get("limits", {}).get("session", {})
    weekly = data.get("limits", {}).get("weekly", {})
    return {
        "session_pct": float(session.get("usage", 0)) * 100,
        "weekly_pct": float(weekly.get("usage", 0)) * 100,
    }, None

def main():
    quota, err = query_ollama()
    if err:
        # Can't check quota — fail open (wake agent, let it decide)
        print(json.dumps({"wakeAgent": True, "context": {"quota_error": err}}))
        return

    s = quota["session_pct"]
    w = quota["weekly_pct"]

    # Same heuristic as quota_planner.py
    if s > 95 or w > 90:
        print(json.dumps({"wakeAgent": False}))
        return
    if s > 80:
        # Only micro tasks — wake but tell agent to be very conservative
        print(json.dumps({"wakeAgent": True, "context": {
            "session_pct": round(s, 1), "weekly_pct": round(w, 1),
            "max_workers": 0, "max_task_cost": "micro",
            "warning": "session quota very high — only micro tasks"
        }}))
        return
    if w > 75:
        print(json.dumps({"wakeAgent": True, "context": {
            "session_pct": round(s, 1), "weekly_pct": round(w, 1),
            "max_workers": 1, "max_task_cost": "tiny",
            "warning": "weekly quota high — only tiny tasks"
        }}))
        return

    # Healthy — full autonomy
    max_workers = 2 if w < 50 else 1
    print(json.dumps({"wakeAgent": True, "context": {
        "session_pct": round(s, 1), "weekly_pct": round(w, 1),
        "max_workers": max_workers, "max_task_cost": "any"
    }}))

if __name__ == "__main__":
    main()