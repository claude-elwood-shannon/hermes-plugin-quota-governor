"""Offline tests for repo_sync_drift module.

The repo's helper functions are exercised by a small test suite that
creates transient repositories and deploy‑copy directories inside the
temporary directory of each test.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from importlib import util

import pytest

# Import the module under test.
module_path = Path("/data/git/hermes-plugin-quota-governor/repo_sync_drift.py")
spec = util.spec_from_file_location("repo_sync_drift", module_path)
repo_sync_drift = util.module_from_spec(spec)
spec.loader.exec_module(repo_sync_drift)

# ---------------------------------------------------------------------------
# Helper utilities for the tests
# ---------------------------------------------------------------------------

def local_md5(path: Path) -> str | None:
    """Return the MD5 hex digest of *path* or None if unreadable."""
    try:
        m = hashlib.md5()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                m.update(chunk)
        return m.hexdigest()
    except OSError:
        return None

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    """Create a temporary git‑style repository with a `scripts/` subdir."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    # Add a couple of script files.
    (scripts / "foo.py").write_text("print('foo')\n")
    (scripts / "bar.py").write_text("print('bar')\n")
    (scripts / "sub" / "baz.py").parent.mkdir(parents=True)
    (scripts / "sub" / "baz.py").write_text("print('baz')\n")
    return tmp_path

@pytest.fixture
def deploy_dirs(tmp_path: Path) -> list[Path]:
    """Create temporary deploy directories containing copies of some scripts."""
    dir_a = tmp_path / "deploy_a"
    dir_b = tmp_path / "deploy_b"
    dir_a.mkdir()
    dir_b.mkdir()
    return [dir_a, dir_b]

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_md5_of_file_exists(repo_root: Path, deploy_dirs: list[Path]):
    path = repo_root / "scripts" / "foo.py"
    expected = local_md5(path)
    got = repo_sync_drift.md5_of_file(str(path))
    assert got == expected


def test_md5_of_file_missing():
    missing = Path("/nonexistent/file.txt")
    assert repo_sync_drift.md5_of_file(str(missing)) is None


def test_scan_candidates(repo_root: Path):
    candidates = repo_sync_drift._scan_candidates(str(repo_root))
    rels = [c[0] for c in candidates]
    # Expect relative sorted paths
    assert sorted(rels) == rels
    # Exclude __pycache__ and non‑files
    assert all(not p.endswith("__pycache__") for p in rels)


def test_build_name_maps(repo_root: Path):
    cand = repo_sync_drift._scan_candidates(str(repo_root))
    md5_by_name, names_by_base = repo_sync_drift._build_name_maps(cand)
    # md5_by_name keys should match the candidate names
    assert set(md5_by_name.keys()) == {c[0] for c in cand}
    # names_by_base should group by basename
    for base, names in names_by_base.items():
        assert all(Path(n).name == base for n in names)


def test_collect_copies(repo_root: Path, deploy_dirs: list[Path]):
    from shutil import copy2
    # copy one file to each deploy dir
    src = repo_root / "scripts" / "foo.py"
    for d in deploy_dirs:
        copy2(src, d)
    cand = repo_sync_drift._scan_candidates(str(repo_root))
    _, names_by_base = repo_sync_drift._build_name_maps(cand)
    copies = repo_sync_drift._collect_copies([str(d) for d in deploy_dirs], names_by_base)
    base = os.path.basename(str(src))
    assert base in copies
    assert len(copies[base]) == len(deploy_dirs)


def test_detect_drift_no_change(repo_root: Path, deploy_dirs: list[Path]):
    from shutil import copy2
    src = repo_root / "scripts" / "foo.py"
    for d in deploy_dirs:
        copy2(src, d)
    candidates = repo_sync_drift._scan_candidates(str(repo_root))
    md5_by_name, names_by_base = repo_sync_drift._build_name_maps(candidates)
    copies = repo_sync_drift._collect_copies([str(d) for d in deploy_dirs], names_by_base)
    drift = repo_sync_drift._detect_drift(candidates, md5_by_name, names_by_base, copies)
    assert drift == []


def test_detect_drift_with_change(repo_root: Path, deploy_dirs: list[Path]):
    from shutil import copy2
    src = repo_root / "scripts" / "foo.py"
    # deploy_a keeps an identical copy; deploy_b's copy is edited in place,
    # so only deploy_b's copy is stale.
    copy2(src, deploy_dirs[0])
    copy2(src, deploy_dirs[1])
    (deploy_dirs[1] / "foo.py").write_text("print('mod')\n")

    candidates = repo_sync_drift._scan_candidates(str(repo_root))
    md5_by_name, names_by_base = repo_sync_drift._build_name_maps(candidates)
    copies = repo_sync_drift._collect_copies([str(d) for d in deploy_dirs], names_by_base)
    drift = repo_sync_drift._detect_drift(candidates, md5_by_name, names_by_base, copies)
    assert len(drift) == 1
    assert drift[0]["script"] == "foo.py"
    assert len(drift[0]["stale_copies"]) == 1
    assert drift[0]["stale_copies"][0]["dir"] == str(deploy_dirs[1])


def test_detect_drift_ignores_unrelated_basenames(repo_root: Path, deploy_dirs: list[Path]):
    from shutil import copy2
    src = repo_root / "scripts" / "foo.py"
    # Deployed copies keep the repo basename (repo-sync-check contract):
    # a different-named file in a deploy dir is NOT a stale copy of foo.py,
    # even when its stem shares the same prefix.
    copy2(src, deploy_dirs[0])
    (deploy_dirs[1] / "foo_modified.py").write_text("print('mod')\n")

    candidates = repo_sync_drift._scan_candidates(str(repo_root))
    md5_by_name, names_by_base = repo_sync_drift._build_name_maps(candidates)
    copies = repo_sync_drift._collect_copies([str(d) for d in deploy_dirs], names_by_base)
    drift = repo_sync_drift._detect_drift(candidates, md5_by_name, names_by_base, copies)
    assert drift == []


def test_check_deploy_drift(repo_root: Path, deploy_dirs: list[Path]):
    from shutil import copy2
    src = repo_root / "scripts" / "foo.py"
    for d in deploy_dirs:
        copy2(src, d)
    drift = repo_sync_drift.check_deploy_drift(repo_dir=str(repo_root), deploy_dirs=[str(d) for d in deploy_dirs])
    assert drift == []


def test_report_deploy_drift_empty(capsys):
    repo_sync_drift.report_deploy_drift([])
    captured = capsys.readouterr()
    assert captured.out == ""


def test_report_deploy_drift_with_output(capsys):
    drift = [{"script": "foo.py", "repo_md5": "x", "repo_path": "", "stale_copies": [{"dir": "/tmp", "path": "/tmp/foo.py", "md5": "y", "mtime": 0}] }]
    repo_sync_drift.report_deploy_drift(drift)
    out = capsys.readouterr().out
    assert "DEPLOY_DRIFT" in out
    assert "foo.py" in out

# Ensure that _fmt_ts can parse a realistic timestamp

def test_fmt_ts():
    # _fmt_ts renders local time; compute the expectation with the same
    # base so the test is timezone-independent.
    ts = 0
    expected = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    formatted = repo_sync_drift._fmt_ts(ts)
    assert formatted == expected
    assert len(formatted) == 19
