import importlib.machinery
import json
import sys
from pathlib import Path

# Load generate_dashboard module via importlib spec
loader = importlib.machinery.SourceFileLoader(
    "generate_dashboard",
    str(Path("scripts/obs/oo-dashboard/generate_dashboard.py").resolve()),
)
module = loader.load_module()

build = module.build


def test_roundtrip_serialization():
    data = build("test-id")
    dumped = json.dumps(data)
    loaded = json.loads(dumped)
    assert loaded == data


def test_unique_panel_ids():
    data = build()
    panels = data["tabs"][0]["panels"]
    ids = [p["id"] for p in panels]
    assert len(ids) == len(set(ids)), "panel ids must be unique"


def test_grid_bounds():
    data = build()
    panels = data["tabs"][0]["panels"]
    for p in panels:
        layout = p["layout"]
        x, y, w, h = layout["x"], layout["y"], layout["w"], layout["h"]
        assert x + w <= 24, f"panel {p['id']} exceeds column width"
        assert y + h <= 50, f"panel {p['id']} exceeds vertical limit"


def test_no_overlapping_panels():
    data = build()
    panels = data["tabs"][0]["panels"]
    def overlaps(a, b):
        ax, ay, aw, ah = a["layout"]["x"], a["layout"]["y"], a["layout"]["w"], a["layout"]["h"]
        bx, by, bw, bh = b["layout"]["x"], b["layout"]["y"], b["layout"]["w"], b["layout"]["h"]
        return not (ax + aw <= bx or bx + bw <= ax or ay + ah <= by or by + bh <= ay)
    for i, p1 in enumerate(panels):
        for p2 in panels[i+1:]:
            assert not overlaps(p1, p2), f"panels {p1['id']} and {p2['id']} overlap"


def test_query_stream_references():
    data = build()
    panels = data["tabs"][0]["panels"]
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
    for p in panels:
        sql = p["queries"][0]["query"]
        assert any(s in sql for s in expected_streams), f"panel {p['id']} query lacks expected streams"


def test_does_propagate_dashboard_id():
    id_ = "abc123"
    data = build(id_)
    assert data["dashboardId"] == id_, "dashboardId not propagated"

if __name__ == "__main__":
    import pytest
    pytest.main([__file__])
