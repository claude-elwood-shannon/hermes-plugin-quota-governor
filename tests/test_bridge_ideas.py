#!/usr/bin/env python3
"""Offline tests for the bridge idea parking lot (v1.7, t_0c6a7847).

Covers POST /save-idea + GET /ideas end to end against an ephemeral
HTTPServer (pattern of tests/test_bridge_governance_endpoints.py) plus the
module-level helpers ``_load_idea_file`` / ``_ideas_sorted`` directly.

Hermetic: every test points ``bridge.IDEAS_DIR`` at a pytest tmp directory,
so the real ``~/.hermes/data/ideas`` is never touched. The bridge module is
imported by file path (spec_from_file_location); importing it has no side
effects (no server start — guarded by ``__main__`` —, no network).
"""
import importlib.util
import json
import os
import threading
import urllib.error
import urllib.request
from http.server import HTTPServer

import pytest

BRIDGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir,
                      "scripts", "bridge", "open-webui-bridge.py")

_spec = importlib.util.spec_from_file_location("bridge_ideas_under_test", BRIDGE)
bridge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bridge)

_srv = HTTPServer(("127.0.0.1", 0), bridge.HermesBridge)
threading.Thread(target=_srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{_srv.server_address[1]}"


def req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method)
    if data:
        r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}
    except Exception as e:
        return 0, {"error": str(e)}


@pytest.fixture()
def ideas_dir(tmp_path, monkeypatch):
    """Redirect the parking lot to a tmp dir for the duration of one test."""
    d = tmp_path / "ideas"
    monkeypatch.setattr(bridge, "IDEAS_DIR", str(d))
    return d


# ------------------------------------------------------------ POST /save-idea

def test_save_idea_happy_path_creates_dir_and_file(ideas_dir):
    assert not ideas_dir.exists()  # el directorio se crea con el primer POST
    code, body = req("POST", "/save-idea",
                     {"title": "test", "body": "test idea"})
    assert code == 200, body
    assert body["saved"] is True
    assert body["path"] == os.path.join("data", "ideas", f"{body['idea_id']}.json")
    # success criterion: test -d ~/.hermes/data/ideas
    assert ideas_dir.is_dir()
    files = list(ideas_dir.glob("*.json"))
    assert len(files) == 1
    stored = json.loads(files[0].read_text(encoding="utf-8"))
    assert stored["title"] == "test"
    assert stored["body"] == "test idea"
    assert stored["saved_at"]
    assert files[0].stem == body["idea_id"]


def test_save_idea_id_format(ideas_dir):
    import re
    code, body = req("POST", "/save-idea", {"title": "t", "body": "b"})
    assert code == 200
    assert re.match(r"^idea_[0-9]{8}_[0-9]{6}_[0-9]{3}$", body["idea_id"])


def test_save_idea_missing_title_400(ideas_dir):
    code, body = req("POST", "/save-idea", {"body": "only body"})
    assert code == 400 and body["saved"] is False
    assert not ideas_dir.exists()  # nada escrito, nada creado


def test_save_idea_missing_body_400(ideas_dir):
    code, body = req("POST", "/save-idea", {"title": "only title"})
    assert code == 400 and body["saved"] is False


def test_save_idea_blank_fields_400(ideas_dir):
    code, body = req("POST", "/save-idea", {"title": "   ", "body": "\n\t "})
    assert code == 400 and body["saved"] is False


def test_save_idea_empty_payload_400(ideas_dir):
    # sin Content-Length util, _read_body devuelve {} -> title/body ausentes
    code, body = req("POST", "/save-idea", {})
    assert code == 400 and body["saved"] is False
    assert "title and body are required" in body["error"]


def test_save_idea_with_tags(ideas_dir):
    code, body = req("POST", "/save-idea",
                     {"title": "t", "body": "b", "tags": ["a", 2, " "]})
    assert code == 200
    stored = json.loads(next(ideas_dir.glob("*.json")).read_text(encoding="utf-8"))
    assert stored["tags"] == ["a", "2"]  # coerción a str, blanks fuera


def test_save_idea_tags_not_a_list_ignored(ideas_dir):
    code, _ = req("POST", "/save-idea",
                  {"title": "t", "body": "b", "tags": "no-soy-lista"})
    assert code == 200
    stored = json.loads(next(ideas_dir.glob("*.json")).read_text(encoding="utf-8"))
    assert "tags" not in stored


def test_save_idea_preserves_unicode(ideas_dir):
    code, body = req("POST", "/save-idea",
                     {"title": "Café ñandú", "body": "idea — con em-dash"})
    assert code == 200
    stored = json.loads(next(ideas_dir.glob("*.json")).read_text(encoding="utf-8"))
    assert stored["title"] == "Café ñandú" and stored["body"] == "idea — con em-dash"


# -------------------------------------------------------------- GET /ideas

def test_ideas_empty_dir_returns_200_count_0(ideas_dir):
    code, body = req("GET", "/ideas")
    assert code == 200
    assert body["count"] == 0 and body["ideas"] == []
    assert not ideas_dir.exists()  # el GET no crea el directorio


def test_ideas_lists_saved_idea_newest_first(ideas_dir):
    for i, title in enumerate(["primera", "segunda", "tercera"]):
        code, body = req("POST", "/save-idea", {"title": title, "body": f"b{i}"})
        assert code == 200
        # ideas consecutivas en el mismo segundo: ids únicos garantizados
        # por el sufijo de pid — si chocaran, save fallaría con 500.
    code, body = req("GET", "/ideas")
    assert code == 200
    assert body["count"] == 3
    ids = [i["idea_id"] for i in body["ideas"]]
    assert ids == sorted(ids, reverse=True)  # más reciente primero
    titles = [i["title"] for i in body["ideas"]]
    assert titles[-1] == "primera"  # la primera guardada queda al final
    assert body["ideas_dir"] == str(ideas_dir)


def test_ideas_skips_corrupt_files(ideas_dir):
    ideas_dir.mkdir(parents=True)
    (ideas_dir / "idea_20990101_000000_001.json").write_text("{roto", encoding="utf-8")
    (ideas_dir / "idea_20990101_000000_002.json").write_text("[1,2]", encoding="utf-8")
    (ideas_dir / "readme.txt").write_text("no soy idea", encoding="utf-8")
    (ideas_dir / "idea_20990101_000000_003.json").write_text(
        json.dumps({"idea_id": "idea_20990101_000000_003", "title": "buena",
                    "body": "valida", "saved_at": "2099-01-01T00:00:00"}),
        encoding="utf-8")
    code, body = req("GET", "/ideas")
    assert code == 200
    assert body["count"] == 1
    assert body["ideas"][0]["idea_id"] == "idea_20990101_000000_003"


def test_ideas_skips_file_with_bad_embedded_id(ideas_dir):
    ideas_dir.mkdir(parents=True)
    # idea_id dentro del JSON no coincide con idea_<ts>_<pid>: se descarta
    (ideas_dir / "idea_20990101_000000_004.json").write_text(
        json.dumps({"idea_id": "t_ forged", "title": "falsa", "body": "x"}),
        encoding="utf-8")
    code, body = req("GET", "/ideas")
    assert code == 200 and body["count"] == 0


# ---------------------------------------------------- helpers a nivel módulo

def test_load_idea_file_direct(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "IDEAS_DIR", str(tmp_path))
    p = tmp_path / "idea_20990101_000000_009.json"
    p.write_text(json.dumps({"idea_id": "idea_20990101_000000_009",
                             "title": "x", "body": "y"}), encoding="utf-8")
    idea_id, idea = bridge._load_idea_file(str(p))
    assert idea_id == "idea_20990101_000000_009" and idea["title"] == "x"

    p.write_text("no json", encoding="utf-8")
    assert bridge._load_idea_file(str(p)) == (None, None)

    p.write_text(json.dumps(["lista"]), encoding="utf-8")
    assert bridge._load_idea_file(str(p)) == (None, None)

    p.write_text(json.dumps({"title": "sin id", "body": "y"}), encoding="utf-8")
    assert bridge._load_idea_file(str(p)) == (None, None)

    assert bridge._load_idea_file(str(tmp_path / "no-existe.json")) == (None, None)


def test_ideas_sorted_missing_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(bridge, "IDEAS_DIR", str(tmp_path / "vacío"))
    assert bridge._ideas_sorted() == []


# ------------------------------------------------ contrato OpenAPI y routing

def test_openapi_declares_new_endpoints():
    code, spec = req("GET", "/openapi.json")
    assert code == 200
    assert spec["info"]["version"] == "1.11.0"
    paths = spec["paths"]
    assert paths["/save-idea"]["post"]["operationId"] == "save_idea"
    assert paths["/ideas"]["get"]["operationId"] == "list_ideas"
    schema = paths["/save-idea"]["post"]["requestBody"]["required"]
    assert schema is True


def test_get_save_idea_is_404(ideas_dir):
    # /save-idea es POST-only: por GET cae en el else del router -> 404
    code, body = req("GET", "/save-idea")
    assert code == 404 and body == {"error": "not found"}


def test_post_unknown_path_404(ideas_dir):
    code, body = req("POST", "/save-idea-typo", {"title": "t", "body": "b"})
    assert code == 404 and body == {"error": "not found"}


def test_legacy_endpoints_still_routed():
    # regresión mínima: los dispatchers existentes no se rompieron
    code, _ = req("GET", "/board")
    assert code == 200
