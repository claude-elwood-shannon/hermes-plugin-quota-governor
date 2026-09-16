import json
from pathlib import Path
import importlib.util

# Resolve dashboard module
ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "obs" / "oo-dashboard" / "generate_dashboard.py"

spec = importlib.util.spec_from_file_location("generate_dashboard", MODULE_PATH)
mod = importlib.util.module_from_spec(spec)
# Dynamically import the dashboard module in a robust way
try:
    spec = importlib.util.spec_from_file_location("generate_dashboard", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to create spec for generate_dashboard")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    build = mod.build
except Exception as e:
    raise RuntimeError(f"Failed to load generate_dashboard module: {e}")


def test_build_valid_json_serializable():
    dashboard = build()
    # Round-trip JSON
    dumped = json.dumps(dashboard, indent=1)
    loaded = json.loads(dumped)
    assert dashboard == loaded, "JSON round-trip mismatch"


def test_unique_panel_ids():
    dashboard = build()
    panel_ids = {panel["id"] for panel in dashboard["tabs"][0]["panels"]}
    assert len(panel_ids) == len(dashboard["tabs"][0]["panels"]), "Duplicate panel ids found"


def test_grid_limits_and_no_overlap():
    dashboard = build()
    panels = dashboard["tabs"][0]["panels"]
    max_xw = max(p["layout"]["x"] + p["layout"]["w"] for p in panels)
    max_yh = max(p["layout"]["y"] + p["layout"]["h"] for p in panels)
    assert max_xw <= 24, f"Grid width exceeds 24 columns: {max_xw}"
    # simple overlap check: for every pair ensure not overlapping
    for i, a in enumerate(panels):
        ax, ay, aw, ah = a["layout"]["x"], a["layout"]["y"], a["layout"]["w"], a["layout"]["h"]
        for b in panels[i+1:]:
            bx, by, bw, bh = b["layout"]["x"], b["layout"]["y"], b["layout"]["w"], b["layout"]["h"]
            overlap_x = (ax < bx + bw) and (bx < ax + aw)
            overlap_y = (ay < by + bh) and (by < ay + ah)
            assert not (overlap_x and overlap_y), f"Panels {a['id']} and {b['id']} overlap"
    assert max_yh <= 36, f"Grid height exceeds reasonable limit: {max_yh}"


def test_build_includes_expected_streams():
    dashboard = build()
    panels = dashboard["tabs"][0]["panels"]
    expected_streams = {
        "hermes_tasks_done",
        "hermes_tasks_running",
        "hermes_tasks_triage",
        "hermes_supply_ratio",
        "hermes_bridge_up",
        "hermes_quota_session_percent",
        "hermes_tasks",
        "hermes_gpu_temp_c",
        "hermes_objective_spent_today",
        "hermes_crons",
        "hermes_efficiency_ratio",
        "hermes_quota_nanogpt_balance_usd",
        "default",
        "hermes_watchdog",
        "hermes_vllm",
        "hermes_efficiency",
    }
    for panel in panels:
        q = panel["queries"][0]["query"]
        found = any(stream in q for stream in expected_streams)
        assert found, f"Panel {panel['id']} query missing expected stream"


def test_id_propagation():
    id_val = "TESTID123"
    dashboard = build(dashboard_id=id_val)
    assert dashboard["dashboardId"] == id_val, "Passed dashboard_id not set"
