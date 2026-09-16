#!/usr/bin/python3.12
"""Tests for quality-check.py (OBJ-CODEQUALITY). Offline fixtures only.

Loads the kebab-named CLI script by path (importlib.util) and runs the
gauge against temporary repo roots (pytest tmp_path): no network, no
host state, no real scan outside tmp_path except the end-to-end self
test, which only asserts shape keys and exit code.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_HERE = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "quality_check", _HERE / "scripts" / "quality-check.py")
assert _SPEC is not None and _SPEC.loader is not None  # repo layout is fixed
qc = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(qc)

REPORT_KEYS = {"py_compile_failures", "funcs_gt_50",
               "public_missing_docstrings", "generated_at"}


def _big_func(name: str, total: int, doc: str | None = None,
              indent: str = "") -> str:
    """Source of a function spanning exactly `total` lines (doc optional)."""
    body = "\n".join(
        f"{indent}    x{idx} = {idx}" for idx in range(total - 1 - bool(doc)))
    docline = f'{indent}    """{doc}"""\n' if doc else ""
    return f"{indent}def {name}():\n{docline}{body}\n"


def _write(path: Path, text: str) -> Path:
    """Write text to path under the fixture root, creating directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_func_over_50_lines_detected(tmp_path):
    """(a) A public 52-line function is reported with its exact length."""
    _write(tmp_path / "scripts" / "mod.py",
           'def documented():\n    """Short and fine."""\n    return 1\n\n\n'
           + _big_func("huge", 52, doc="Too long."))
    rep = qc.scan_root(tmp_path)
    assert rep["funcs_gt_50"] == [
        {"file": "scripts/mod.py", "func": "huge", "lines": 52}]
    assert rep["py_compile_failures"] == []
    assert rep["public_missing_docstrings"] == 0


def test_public_missing_docstring_counted(tmp_path):
    """(b) Public module-level functions without docstring are counted."""
    _write(tmp_path / "scripts" / "mod.py",
           "def has_docs():\n    """ + '"Docstring."\n' +
           "    return 1\n\n\n"
           + "def no_docs():\n    return 2\n\n\n"
           + "def _private_no_docs():\n    return 3\n\n\n"
           + "def another_no_docs():\n    return 4\n")
    rep = qc.scan_root(tmp_path)
    assert rep["public_missing_docstrings"] == 2


def test_py_compile_failure_listed(tmp_path):
    """(c) A syntactically broken file lands in py_compile_failures."""
    _write(tmp_path / "scripts" / "broken.py", "def oops(:\n")
    _write(tmp_path / "scripts" / "ok.py",
           'def fine():\n    """Compiles."""\n    return 1\n')
    rep = qc.scan_root(tmp_path)
    assert rep["py_compile_failures"] == ["scripts/broken.py"]
    assert rep["funcs_gt_50"] == []


def test_boundary_50_lines_not_flagged(tmp_path):
    """Exactly 50 lines passes; 51 lines is flagged."""
    _write(tmp_path / "scripts" / "mod.py",
           _big_func("at_limit", 50, doc="Boundary.")
           + "\n\n" + _big_func("over_limit", 51, doc="One over."))
    rep = qc.scan_root(tmp_path)
    assert [(e["func"], e["lines"]) for e in rep["funcs_gt_50"]] == [
        ("over_limit", 51)]


def test_excluded_paths_never_scanned(tmp_path):
    """attic/, __pycache__/ and scripts/obs/test_* files are out of scope."""
    _write(tmp_path / "scripts" / "attic" / "old.py",
           _big_func("legacy", 60, doc="Archived."))
    _write(tmp_path / "scripts" / "__pycache__" / "junk.py",
           _big_func("junk", 60, doc="Bytecode."))
    _write(tmp_path / "scripts" / "obs" / "test_module.py",
           _big_func("test_helper", 60, doc="Offline suite helper."))
    _write(tmp_path / "scripts" / "obs" / "keep.py",
           _big_func("observed", 60, doc="Real obs script."))
    rep = qc.scan_root(tmp_path)
    assert [e["file"] for e in rep["funcs_gt_50"]] == ["scripts/obs/keep.py"]


def test_nested_function_reported_with_own_span(tmp_path):
    """A function nested inside a big one reports its own >50 span too."""
    inner = "\n".join(
        "        y%d = %d" % (idx, idx) for idx in range(49))
    source = (
        'def outer():\n'
        '    """Outer spans 55 lines."""\n'
        '    def inner():\n'
        '        """Inner spans 52 lines."""\n'
        f'{inner}\n'
        '        return 1\n'
        '    return inner\n')
    _write(tmp_path / "scripts" / "mod.py", source)
    rep = qc.scan_root(tmp_path)
    got = [(e["func"], e["lines"]) for e in rep["funcs_gt_50"]]
    assert got == [("outer", 55), ("inner", 52)]


def test_main_exit_zero_and_stdout_json_with_findings(tmp_path, capsys):
    """Findings are data, not errors: rc 0, full JSON report on stdout."""
    _write(tmp_path / "scripts" / "broken.py", "def oops(:\n")
    rc = qc.main(["--root", str(tmp_path)])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert set(out) == REPORT_KEYS
    assert out["py_compile_failures"] == ["scripts/broken.py"]


def test_json_outfile_written_missing_dirs_created(tmp_path, capsys):
    """--json writes the report, creating missing parent directories."""
    _write(tmp_path / "scripts" / "mod.py",
           'def fine():\n    """Doc."""\n    return 1\n')
    outfile = tmp_path / "out" / "nested" / "quality-check.json"
    rc = qc.main(["--root", str(tmp_path), "--json", str(outfile)])
    assert rc == 0
    stdout_report = json.loads(capsys.readouterr().out)
    file_report = json.loads(outfile.read_text(encoding="utf-8"))
    assert stdout_report == file_report
    file_report.pop("generated_at")
    assert set(file_report) == REPORT_KEYS - {"generated_at"}


def test_missing_scripts_dir_fails_open(tmp_path, capsys):
    """A root without scripts/ yields a zeroed report and exit 0."""
    rc = qc.main(["--root", str(tmp_path), "--json",
                  str(tmp_path / "out" / "qc.json")])
    out = json.loads((tmp_path / "out" / "qc.json").read_text(encoding="utf-8"))
    assert rc == 0
    assert out["py_compile_failures"] == []
    assert out["funcs_gt_50"] == []
    assert out["public_missing_docstrings"] == 0
    capsys.readouterr()  # drain stdout for cleanliness


def test_generated_at_is_iso_utc(tmp_path):
    """generated_at is an ISO-8601 UTC timestamp (+00:00)."""
    _write(tmp_path / "scripts" / "mod.py",
           'def fine():\n    """Doc."""\n    return 1\n')
    rep = qc.scan_root(tmp_path)
    assert rep["generated_at"].endswith("+00:00")
