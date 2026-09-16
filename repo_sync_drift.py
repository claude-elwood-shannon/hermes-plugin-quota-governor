# Helper utilities for deploy-drift checks
import os
import hashlib
import glob
from pathlib import Path
from datetime import datetime

# --- original md5 helper retained

def md5_of_file(path):
    """Return the md5 hex digest of a file, or None if unreadable."""
    h = hashlib.md5()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()

# --- new split helpers ---

def _scan_candidates(repo_root: str):
    scripts_dir = os.path.join(repo_root, "scripts")
    if not os.path.isdir(scripts_dir):
        return []
    candidates = []
    for root, dirs, files in os.walk(scripts_dir):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for fname in sorted(files):
            path = os.path.join(root, fname)
            if not os.path.isfile(path):
                continue
            rel = os.path.relpath(path, scripts_dir).replace(os.sep, "/")
            md5 = md5_of_file(path)
            if md5 is None:
                continue
            candidates.append((rel, path, md5))
    return sorted(candidates, key=lambda c: c[0])


def _build_name_maps(candidates):
    md5_by_name = {n: m for n, _, m in candidates}
    names_by_base = {}
    for name, _, _ in candidates:
        names_by_base.setdefault(os.path.basename(name), []).append(name)
    return md5_by_name, names_by_base


def _collect_copies(deploy_dirs, names_by_base):
    copies = {}
    for d in deploy_dirs:
        try:
            entries = sorted(os.listdir(d))
        except OSError:
            continue
        for base in entries:
            if base not in names_by_base:
                continue
            dep_path = os.path.join(d, base)
            if not os.path.isfile(dep_path):
                continue
            md5 = md5_of_file(dep_path)
            if md5 is None:
                continue
            copies.setdefault(base, []).append({
                "dir": d,
                "path": dep_path,
                "md5": md5,
                "mtime": os.path.getmtime(dep_path),
            })
    return copies


def _detect_drift(candidates, md5_by_name, names_by_base, copies):
    drift = []
    for name, repo_path, repo_md5 in candidates:
        base = os.path.basename(name)
        group = names_by_base[base]
        deployed = copies.get(base, [])
        if not deployed:
            continue
        stale = []
        for dep in deployed:
            if dep["md5"] == repo_md5:
                continue
            owners = [n for n in group if md5_by_name[n] == dep["md5"]]
            owner = owners[0] if owners else group[0]
            if owner != name:
                continue
            stale.append(dep)
        if stale:
            drift.append({
                "script": name,
                "repo_md5": repo_md5,
                "repo_path": repo_path,
                "stale_copies": stale,
            })
    return drift


def check_deploy_drift(repo_dir=None, deploy_dirs=None):
    """Compare each repo scripts/ file against its deployed copies.

    Recursively scans scripts/ (subdirectory scripts count too). Deploy dirs
    are flat, so a subdir script maps to the deployed copy with its bare
    basename.

    Returns a list of drift dicts; 'script' is the repo-relative name
    with '/', e.g. 'obs/morning-screen.py'.
    """
    if repo_dir is None:
        repo_dir = str(Path(__file__).resolve().parent.parent)
    if deploy_dirs is None:
        deploy_dirs = ([os.path.expanduser("~/.hermes/scripts")]
                       + sorted(glob.glob(os.path.expanduser("~/.hermes/profiles/*/scripts"))))
    candidates = _scan_candidates(repo_dir)
    md5_by_name, names_by_base = _build_name_maps(candidates)
    copies = _collect_copies(deploy_dirs, names_by_base)
    return _detect_drift(candidates, md5_by_name, names_by_base, copies)


def _fmt_ts(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def report_deploy_drift(drift):
    """Print a human‑readable deploy‑drift alert to stdout."""
    if not drift:
        return
    print("DEPLOY_DRIFT: deployed scripts differ from repo — manual deploy needed")
    for d in drift:
        print(f"  {d['script']}: repo md5 {d['repo_md5']}")
        for s in d["stale_copies"]:
            print(f"    deploy {s['path']} md5 {s['md5']} (mtime {_fmt_ts(s['mtime'])}) — STALE")

