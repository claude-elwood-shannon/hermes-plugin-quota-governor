#!/usr/bin/env python3
"""Legacy tests for bridge governance endpoints.

Preserve original behavior when run directly; import safe.
"""
import importlib.util
import json
import os
import sys
import threading
import urllib.error
import urllib.request
from http.server import HTTPServer

BRIDGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts", "bridge", "open-webui-bridge.py")
spec = importlib.util.spec_from_file_location("bridge_under_test", BRIDGE)
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)

srv = HTTPServer(("127.0.0.1", 0), bridge.HermesBridge)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{PORT}"


# Helper request function used by tests

def req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method)
    if data:
        r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}
    except Exception as e:
        return 0, {"error": str(e)}


# Legacy test runner preserved for backward compatibility

def _run_legacy_tests():
    passed, failed = [], []

    def check(name, cond, detail=""):
        (passed if cond else failed).append(name)
        print(("PASS" if cond else "FAIL"), name, detail if not cond else "")

    # 1. get_task
    code, body = req("GET", "/task?task_id=t_3cafd196")
    check(
        "1 /task 200 campos",
        code == 200
        and body.get("id") == "t_3cafd196"
        and isinstance(body.get("body"), str)
        and len(body.get("body", "")) > 100
        and body.get("created_by") == "mediator"
        and body.get("tags") == ["mediator-prompt"],
        f"code={code} keys={sorted(body)[:8]}",
    )
    code, body = req("GET", "/task?task_id=../../etc/passwd")
    check("1b /task traversal 400", code == 400, f"code={code}")
    code, body = req("GET", "/task?task_id=t_00000000")
    check("1c /task inexistente 404", code == 404, f"code={code}")
    code, body = req("GET", "/task")
    check("1d /task sin id 400", code == 400, f"code={code}")

    # 2. search_tasks
    code, body = req("GET", "/tasks?tag=mediator-prompt")
    check(
        "2 /tasks tag",
        code == 200
        and body.get("count", 0) >= 1
        and all("mediator-prompt" in (t.get("tags") or []) for t in body.get("tasks", [])),
        f"code={code} count={body.get('count')}",
    )
    code, body = req("GET", "/tasks?created_by=mediator")
    check(
        "2b /tasks created_by",
        code == 200
        and body.get("count", 0) >= 1
        and all(t.get("created_by") == "mediator" for t in body.get("tasks", [])),
        f"count={body.get('count')}",
    )
    code, body = req("GET", "/tasks?status=triage")
    check(
        "2c /tasks status=triage",
        code == 200
        and body.get("count", 0) >= 1
        and all(t["status"] == "triage" for t in body.get("tasks", [])),
        f"count={body.get('count')}",
    )
    code, body = req("GET", "/tasks?tag=mediator-prompt&status=done")
    check(
        "2d /tasks AND tag+status",
        code == 200 and all(t["status"] == "done" for t in body.get("tasks", [])),
        f"count={body.get('count')}",
    )
    code, body = req("GET", "/tasks?objective=OBJ-44&limit=10")
    check(
        "2e /tasks objective",
        code == 200
        and body.get("count", 0) >= 1
        and all("OBJ-44" in (t.get("title") or "").upper() for t in body.get("tasks", [])),
        f"count={body.get('count')}",
    )
    code, body = req("GET", "/tasks?text=bridge&limit=5")
    check(
        "2f /tasks text", code == 200 and body.get("count") <= 5, f"code={code}"
    )
    code, body = req("GET", "/tasks?status=no-existe")
    check("2g /tasks status invalido 400", code == 400, f"code={code}")

    # 3. get_git_log
    code, body = req("GET", "/git-log?limit=3")
    check(
        "3 /git-log",
        code == 200
        and body.get("count") == 3
        and all({"hash", "author", "date", "message"} <= set(c) for c in body.get("commits", [])),
        f"code={code} count={body.get('count')}",
    )
    code, body = req("GET", "/git-log?limit=999")
    check("3b /git-log limit clamp", code == 200 and body.get("count") <= 100, f"code={code}")

    # 4. get_metrics
    code, body = req("GET", "/metrics?kind=efficiency_ratio&days=7")
    check(
        "4 /metrics kind+days",
        code == 200
        and body.get("count", 0) >= 1
        and all(m.get("kind") == "efficiency_ratio" for m in body.get("metrics", [])),
        f"count={body.get('count')}",
    )
    code, body = req("GET", "/metrics")
    check("4b /metrics default 7d", code == 200 and body.get("count", 0) >= 1, f"count={body.get('count')}")
    code, body = req("GET", "/metrics?kind=no-existe&days=365")
    check(
        "4c /metrics kind vacio -> 0",
        code == 200 and body.get("count") == 0,
        f"count={body.get('count')}",
    )

    # 5. verify_task
    code, body = req("POST", "/verify-task", {"task_id": "t_3cafd196"})
    check(
        "5 /verify-task running -> NOT_DONE",
        code == 200 and body.get("verdict") == "NOT_DONE",
        f"{body}",
    )
    code, body = req("POST", "/verify-task", {"task_id": "t_c7e04f98"})
    check(
        "5b /verify-task done criterio -> PASS",
        code == 200 and body.get("verdict") == "PASS",
        f"{str(body)[:120]}",
    )
    code, body = req("POST", "/verify-task", {"task_id": "t_00000000"})
    check("5c /verify-task inexistente 404", code == 404, f"code={code}")
    code, body = req("POST", "/verify-task", {"task_id": "X"})
    check("5d /verify-task id invalido 400", code == 400, f"code={code}")

    # 6. approve/move
    code, body = req("POST", "/approve-task", {"task_id": "t_00000000"})
    check("6 /approve-task inexistente 404", code == 404 and body.get("moved") is False, f"code={code}")
    code, body = req("POST", "/approve-task", {"task_id": "../x"})
    check("6b /approve-task id invalido 400", code == 400, f"code={code}")
    code, body = req("POST", "/move-task", {"task_id": "t_00000000", "status": "done"})
    check("6c /move-task target no soportado 400", code == 400, f"code={code}")
    code, body = req("POST", "/move-task", {"task_id": "t_00000000", "status": "todo"})
    check("6d /move-task id inexistente 409", code == 409, f"code={code} body={body}")

    # 7. OpenAPI
    code, spec_json = req("GET", "/openapi.json")
    paths = spec_json.get("paths", {})
    new_ops = ["/task", "/tasks", "/git-log", "/metrics", "/approve-task", "/verify-task"]
    version_ok = lambda p: all(op in p for op in new_ops)
    check(
        "7 openapi 6 ops + version",
        code == 200 and version_ok(paths),
        f"missing={[op for op in new_ops if op not in paths]}",
    )
    check(
        "7b operationIds correctos",
        all(
            paths.get(op, {}).get("get" if op in ("/task", "/tasks", "/git-log", "/metrics") else "post", {}).get("operationId")
            == name
            for op, name in zip(new_ops, ["get_task", "search_tasks", "get_git_log", "get_metrics", "approve_task", "verify_task"])
        ),
        f"{[(op, paths.get(op, {}).get('get', paths.get(op, {}).get('post', {})).get('operationId')) for op in new_ops]}",
    )

    # 8. Legacy regression
    code, _ = req("GET", "/board")
    check("8 /board vivo", code == 200, f"code={code}")
    code, body = req("GET", "/snapshot")
    check("8b /snapshot vivo", code == 200 and "stats" in body, f"code={code}")

    print(f"\n== {len(passed)} PASS, {len(failed)} FAIL ==")
    if failed:
        print("FAILED:", ", ".join(failed))
    return passed, failed


if __name__ == "__main__":
    _, failed = _run_legacy_tests()
    sys.exit(1 if failed else 0)
