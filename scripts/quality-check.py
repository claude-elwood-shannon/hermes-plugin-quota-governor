#!/usr/bin/python3.12
"""OBJ-CODEQUALITY gauge: deterministic code-quality scan of scripts/.

The OBJ-CODEQUALITY success criterion asks that every versioned function
stays <= 50 lines and that public functions carry docstrings, but no tool
measured it: each worker re-ran ad-hoc AST snippets. This script is the
gauge: it compiles every scanned file and walks its AST, emitting a JSON
report. Zero tokens, stdlib only, read-only inputs.

Usage:
  python3 scripts/quality-check.py [--root PATH] [--json OUTFILE]

Behavior:
  - Scan scope: the ``scripts/`` directory of --root (default: repo root
    derived from this file's location). Excluded: ``attic/`` directories,
    ``test_*`` files under ``scripts/obs/``, ``__pycache__/`` trees, and
    anything that is not a ``.py`` file.
  - ``py_compile_failures`` lists files that fail compilation (their
    AST findings are skipped).
  - ``funcs_gt_50`` lists every module-level or nested function whose
    span ``end_lineno - lineno + 1`` exceeds 50 lines, including nested
    functions (each reports its own span, so a big wrapper also reports
    big inner helpers).
  - ``public_missing_docstrings`` counts module-level functions with a
    name not starting with ``_`` and no docstring.
  - Exit code is 0 whenever the scan completes: findings are data, not
    errors -- this is a gauge, not a gate.

Env overrides: none (pass --root).
"""
from __future__ import annotations

import argparse
import ast
import json
import py_compile
import sys
from datetime import datetime, timezone
from pathlib import Path

MAX_FUNC_LINES = 50
SCAN_DIRNAME = "scripts"

# Directory names never scanned, at any depth under scripts/.
EXCLUDED_DIR_NAMES = {"attic", "__pycache__"}


def iter_py_files(root: Path) -> list[Path]:
    """All .py files under root/scripts/ in sorted order, minus exclusions.

    Excluded: attic/ and __pycache__/ directories at any depth, and
    test_* files directly under scripts/obs/ (they are offline suites,
    already covered by pytest).
    """
    base = root / SCAN_DIRNAME
    if not base.is_dir():
        return []
    found: list[Path] = []
    for path in sorted(base.rglob("*.py")):
        if EXCLUDED_DIR_NAMES & set(path.relative_to(base).parts):
            continue
        if path.parent.name == "obs" and path.name.startswith("test_"):
            continue
        found.append(path)
    return found


def compiles(path: Path) -> bool:
    """Whether the file passes py_compile (syntax valid on this interpreter)."""
    try:
        py_compile.compile(
            str(path), cfile=str(path.with_suffix(path.suffix + ".pyc")),
            doraise=True)
        return True
    except py_compile.PyCompileError:
        return False
    finally:
        Path(str(path) + ".pyc").unlink(missing_ok=True)


def _iter_functions(tree: ast.AST):
    """Yield every FunctionDef / AsyncFunctionDef node in the tree."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _func_lines(node) -> int:
    """Span of a function node in source lines, decorators excluded."""
    return node.end_lineno - node.lineno + 1


def scan_file(path: Path, rel: str) -> tuple[list[dict], int]:
    """AST-scan one file: (entries over 50 lines, public-no-docstring count).

    Counts module-level public functions (name not starting with "_")
    lacking a docstring. Functions whose name is not a plain str (e.g.
    after a broken parse) cannot happen on a compiled-clean file.
    """
    entries: list[dict] = []
    missing_docs = 0
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    for node in _iter_functions(tree):
        lines = _func_lines(node)
        if lines > MAX_FUNC_LINES:
            entries.append({"file": rel, "func": node.name, "lines": lines})
        if (
            isinstance(node, ast.FunctionDef)
            and node.col_offset == 0
            and not node.name.startswith("_")
            and ast.get_docstring(node) is None
        ):
            missing_docs += 1
    return entries, missing_docs


def scan_root(root: Path) -> dict:
    """Run the full OBJ-CODEQUALITY scan and return the JSON-able report.

    Compilation failures are recorded and their AST analysis skipped;
    everything else contributes function-length and docstring findings.
    """
    failures: list[str] = []
    funcs_gt_50: list[dict] = []
    public_missing_docstrings = 0
    for path in iter_py_files(root):
        rel = path.relative_to(root).as_posix()
        if not compiles(path):
            failures.append(rel)
            continue
        entries, missing_docs = scan_file(path, rel)
        funcs_gt_50.extend(entries)
        public_missing_docstrings += missing_docs
    return {
        "py_compile_failures": failures,
        "funcs_gt_50": funcs_gt_50,
        "public_missing_docstrings": public_missing_docstrings,
        "generated_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
    }


def write_json(report: dict, outfile: Path) -> None:
    """Write the report to outfile, creating parent directories as needed."""
    outfile.parent.mkdir(parents=True, exist_ok=True)
    outfile.write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    """CLI entry: print the JSON report; optionally save it via --json."""
    repo_root = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(
        description="OBJ-CODEQUALITY gauge: py_compile failures, functions "
                    "over 50 lines, and public functions missing docstrings "
                    "under scripts/.")
    ap.add_argument("--root", type=Path, default=repo_root,
                    help="repo root whose scripts/ is scanned "
                         "(default: the repo containing this script)")
    ap.add_argument("--json", type=Path, default=None, metavar="OUTFILE",
                    help="also write the report to OUTFILE, creating the "
                         "directory if missing")
    args = ap.parse_args(argv)
    report = scan_root(args.root)
    text = json.dumps(report, indent=2)
    print(text)
    if args.json is not None:
        write_json(report, args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
