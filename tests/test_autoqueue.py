"""pytest tests for scripts/autoqueue.py (OBJ-44).

Style follows tests/test_abandon_superseded_dryrun.py: import the script
from the repo, always use a tmp_path fixture for queue/ledger files — NEVER
the real ~/.hermes/data/autoqueue.md. No user data in fixtures.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import autoqueue  # noqa: E402


def _write_queue(tmp_path: Path, *lines: str) -> Path:
    path = tmp_path / "autoqueue.md"
    path.write_text("\n".join(lines) + ("\n" if lines else ""),
                    encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# (a) parsing: valid / invalid format
# --------------------------------------------------------------------------- #

def test_parse_valid_seed(tmp_path):
    f = _write_queue(tmp_path, "- [ ] escribir ficha OBJ-45 | pr-ollama | small")
    seeds = autoqueue.parsear_semillas(f)
    assert len(seeds) == 1
    s = seeds[0]
    assert s["pendiente"] is True
    assert s["desc"] == "escribir ficha OBJ-45"
    assert s["perfil"] == "pr-ollama"
    assert s["coste"] == "small"
    assert s["consumible"] is True


def test_parse_ignores_non_seed_lines(tmp_path):
    f = _write_queue(tmp_path,
                     "# cola de semillas",
                     "texto suelto sin marcador",
                     "- [ ] semilla real | pr-ollama | small")
    seeds = autoqueue.parsear_semillas(f)
    assert len(seeds) == 1
    assert seeds[0]["desc"] == "semilla real"


def test_parse_malformed_seed_not_consumable(tmp_path):
    # Missing the | perfil | coste shape → never consumable.
    f = _write_queue(tmp_path, "- [ ] solo una descripcion")
    seeds = autoqueue.parsear_semillas(f)
    assert len(seeds) == 1
    assert seeds[0]["consumible"] is False


# --------------------------------------------------------------------------- #
# (b) non-consumable by annotations / markers
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("annotation", [
    "duplicada",
    "rota",
    "tarea existente",
])
def test_annotated_seed_not_consumable(tmp_path, annotation):
    f = _write_queue(tmp_path, f"- [ ] semilla ({annotation}) | pr-ollama | small")
    seeds = autoqueue.parsear_semillas(f)
    assert not seeds[0]["consumible"]


def test_already_marked_not_consumable(tmp_path):
    # An idempotent seed (t_... marker) is already consumed → not consumable.
    f = _write_queue(tmp_path, "- [x] semilla | pr-ollama | small (t_abc123)")
    seeds = autoqueue.parsear_semillas(f)
    assert not seeds[0]["consumible"]


# --------------------------------------------------------------------------- #
# (c) consumption: dry-run no mutation; execute marks in place, wc -l stable
# --------------------------------------------------------------------------- #

def test_dry_run_no_mutation(tmp_path):
    f = _write_queue(tmp_path, "- [ ] semilla dry | pr-ollama | small")
    before = f.read_text()
    decision = autoqueue.consumir_semilla(
        execute=False, ruta=f, ledger=tmp_path / "cons.jsonl")
    assert decision is not None
    assert decision.startswith("DRY consume")
    # Queue untouched, no ledger written, no task created.
    assert f.read_text() == before
    assert not (tmp_path / "cons.jsonl").exists()


def test_dry_run_silent_when_nothing_consumable(tmp_path):
    f = _write_queue(tmp_path, "- [x] ya consumida | pr-ollama | small (t_1)")
    # All lines marked → nothing consumable → None → caller stays silent.
    assert autoqueue.consumir_semilla(
        execute=False, ruta=f, ledger=tmp_path / "cons.jsonl") is None


def test_execute_marks_in_place_preserving_line_count(tmp_path):
    f = _write_queue(tmp_path,
                     "- [ ] semilla A | pr-ollama | small",
                     "- [x] semilla B | pr-ollama | small (t_old)")
    n_before = len(f.read_text().splitlines())

    def fake_create(desc, perfil, coste):
        assert perfil == "pr-ollama"
        assert coste == "small"
        return "t_new123"

    decision = autoqueue.consumir_semilla(
        execute=True, ruta=f, ledger=tmp_path / "cons.jsonl",
        crear_tarea=fake_create)

    assert decision is not None
    after = f.read_text()
    # wc -l unchanged (append-only).
    assert len(after.splitlines()) == n_before
    # First seed consumed and idempotently marked.
    assert "- [x] semilla A | pr-ollama | small (t_new123)" in after
    # Second already-consumed seed untouched.
    assert "- [x] semilla B | pr-ollama | small (t_old)" in after
    # Ledger got one append-only entry.
    entries = (tmp_path / "cons.jsonl").read_text().splitlines()
    assert len(entries) == 1
    assert '"task_id": "t_new123"' in entries[0]


def test_execute_consumes_only_first(tmp_path):
    f = _write_queue(tmp_path,
                     "- [ ] primera | pr-ollama | small",
                     "- [ ] segunda | pr-ollama | small")
    n_before = len(f.read_text().splitlines())
    autoqueue.consumir_semilla(
        execute=True, ruta=f, ledger=tmp_path / "cons.jsonl",
        crear_tarea=lambda *a, **k: "t_1")
    after = f.read_text()
    assert len(after.splitlines()) == n_before
    # Only the FIRST seed is consumed; the second stays pending.
    assert "primera | pr-ollama | small (t_1)" in after
    assert "- [ ] segunda | pr-ollama | small" in after


def test_generar_semillas_empty_reservoir():
    assert autoqueue.generar_semillas() == []
