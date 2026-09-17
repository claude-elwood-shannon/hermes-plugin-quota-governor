#!/usr/bin/python3.12
"""OBJ-CODEQUALITY gauge: deterministic code-quality scan of the repo
root and scripts/.

The OBJ-CODEQUALITY success criterion asks that every versioned function
stays <= 50 lines and that public functions carry docstrings, but no tool
measured it: each worker re-ran ad-hoc AST snippets. This script is the
gauge: it compiles every scanned file and walks its AST, emitting a JSON
report. Zero tokens, stdlib only, read-only inputs.

Usage:
  python3 scripts/quality-check.py [--root PATH] [--json OUTFILE]

Behavior:
  - Scan scope: the ``scripts/`` directory of --root (default: repo root
    derived from this file's location), recursively, plus the ``*.py``
    files directly in --root itself (non-recursive: subdirectories of
    the root other than ``scripts/`` are out of scope). Excluded:
    ``attic/`` directories, ``test_*`` files under ``scripts/obs/``,
    ``__pycache__/`` trees, and anything that is not a ``.py`` file.
    Paths in the report are POSIX-relative to --root (e.g. ``quota_governor.py``).
  - ``py_compile_failures`` lists files that fail compilation (their
    AST findings are skipped).
  - ``funcs_gt_50`` lists every module-level or nested function whose
    span ``end_lineno - lineno + 1`` exceeds 50 lines, including nested
    functions (each reports its own span, so a big wrapper also reports
    big inner helpers).
  - ``public_missing_docstrings`` counts module-level functions with a
    name not starting with ``_`` and no docstring.
  - ``public_missing_type_hints`` counts module-level public functions
    (sync ``def`` only, matching the docstring gauge) that lack a
    return annotation or carry at least one parameter (``self`` and
    ``cls`` excluded) without an annotation.
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
    """All .py files under root/scripts/ plus root's own .py files, sorted.

    scripts/ is walked recursively; the repo root contributes only the
    *.py files directly inside it (subdirectories other than scripts/
    are not traversed). Excluded: attic/ and __pycache__/ directories
    at any depth under scripts/, and test_* files directly under
    scripts/obs/ (they are offline suites, already covered by pytest).
    """
    found: list[Path] = []
    for path in sorted(root.glob("*.py")):
        if path.is_file():
            found.append(path)
    base = root / SCAN_DIRNAME
    if not base.is_dir():
        return found
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


def _arg_is_selfish(arg: ast.arg) -> bool:
    """Whether an argument is a plain ``self``/``cls`` (no annotation)."""
    return arg.arg in ("self", "cls") and arg.annotation is None


def _is_unannotated(node: ast.FunctionDef) -> bool:
    """Whether a module-level public function lacks a return annotation
    or has any parameter (self/cls excluded) without an annotation."""
    if node.returns is None:
        return True
    args = node.args
    params = (args.posonlyargs + args.args + args.kwonlyargs
              + [a for a in (args.vararg, args.kwarg) if a is not None])
    return any(
        arg.annotation is None and not _arg_is_selfish(arg)
        for arg in params
    )


def scan_file(path: Path, rel: str) -> tuple[list[dict], int, list[dict]]:
    """AST-scan one file: (entries over 50 lines, no-docstring count,
    no-type-hints count).

    Counts module-level public functions (name not starting with "_")
    lacking a docstring, and module-level public sync functions lacking
    type hints (no return annotation, or any self/cls-excluded parameter
    without an annotation). Functions whose name is not a plain str
    (e.g. after a broken parse) cannot happen on a compiled-clean file.
    """
    entries: list[dict] = []
    missing_docs = 0
    missing_hints: list[dict] = []
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    for node in _iter_functions(tree):
        lines = _func_lines(node)
        if lines > MAX_FUNC_LINES:
            entries.append({"file": rel, "func": node.name, "lines": lines})
        if (
            isinstance(node, ast.FunctionDef)
            and node.col_offset == 0
            and not node.name.startswith("_")
        ):
            if ast.get_docstring(node) is None:
                missing_docs += 1
            if _is_unannotated(node):
                missing_hints.append({"file": rel, "func": node.name,
                                      "lineno": node.lineno})
    return entries, missing_docs, missing_hints


def scan_root(root: Path) -> dict:
    """Run the full OBJ-CODEQUALITY scan and return the JSON-able report.

    Compilation failures are recorded and their AST analysis skipped;
    everything else contributes function-length and docstring findings.
    """
    failures: list[str] = []
    funcs_gt_50: list[dict] = []
    public_missing_docstrings = 0
    missing_hints: list[dict] = []
    for path in iter_py_files(root):
        rel = path.relative_to(root).as_posix()
        if not compiles(path):
            failures.append(rel)
            continue
        entries, missing_docs, hints = scan_file(path, rel)
        funcs_gt_50.extend(entries)
        public_missing_docstrings += missing_docs
        missing_hints.extend(hints)
    return {
        "py_compile_failures": failures,
        "funcs_gt_50": funcs_gt_50,
        "public_missing_docstrings": public_missing_docstrings,
        "public_missing_type_hints": len(missing_hints),
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
                    "over 50 lines, public functions missing docstrings, and "
                    "public functions missing type hints under the repo "
                    "root and scripts/.")
    ap.add_argument("--root", type=Path, default=repo_root,
                    help="repo root whose top-level *.py files and "
                         "scripts/ tree are scanned "
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
