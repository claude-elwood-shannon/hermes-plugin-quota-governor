# Helper utilities for deploy-drift checks
import os
import hashlib
import glob
from pathlib import Path
from datetime import datetime, timezone

# Helper functions copied from the main script

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


def check_deploy_drift(repo_dir=None, deploy_dirs=None):
    """Compare each repo scripts/ file against its deployed copies.

    Recursively scans scripts/ (subdirectory scripts count too, e.g.
    scripts/obs/morning-screen.py). Deploy dirs are flat, so a subdir
    script maps to the deployed copy with its bare basename.

    Returns a list of drift dicts; 'script' is the repo-relative name
    with '/', e.g. 'obs/morning-screen.py'. Semantics:
      - file deployed in ≥ 1 DEPLOY_DIRS and every deployed md5 == repo md5
            → in sync, not reported.
      - file deployed but at least one deployed md5 differs from repo md5
            → drift (stale deployed copy).
      - file present ONLY in the repo (no deployed copy anywhere)
            → no-deployed, NOT reported (explicit acceptance criterion).
      - duplicated basename (same name at several repo paths, e.g.
            top-level and obs/ copies): a deployed copy whose md5 matches
            any same-basename repo file exactly is attributed to that file
            (not drift for the rest); a copy matching none is reported
            once, under the first same-basename file in sorted order.
      - repo file unreadable → skipped with a WARN (fail-safe).
    """
    if repo_dir is None:
        repo_dir = str(Path(__file__).resolve().parent.parent)
    if deploy_dirs is None:
        deploy_dirs = ([os.path.expanduser("~/.hermes/scripts")]
                     + sorted(glob.glob(os.path.expanduser("~/.hermes/profiles/*/scripts"))))
    scripts_dir = os.path.join(repo_dir, "scripts")
    if not os.path.isdir(scripts_dir):
        return []
    # Recursive candidate scan
    candidates = []
    for root, dirs, files in os.walk(scripts_dir):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for fname in sorted(files):
            repo_path = os.path.join(root, fname)
            if not os.path.isfile(repo_path):
                continue
            name = os.path.relpath(repo_path, scripts_dir).replace(os.sep, "/")
            repo_md5 = md5_of_file(repo_path)
            if repo_md5 is None:
                continue
            candidates.append((name, repo_path, repo_md5))
    candidates.sort(key=lambda c: c[0])
    md5_by_name = {name: md5 for name, _, md5 in candidates}
    names_by_base = {}
    for name, _, _ in candidates:
        names_by_base.setdefault(os.path.basename(name), []).append(name)
    copies_by_base = {}
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
            dep_md5 = md5_of_file(dep_path)
            if dep_md5 is None:
                continue
            copies_by_base.setdefault(base, []).append({
                "dir": d, "path": dep_path, "md5": dep_md5, "mtime": os.path.getmtime(dep_path)
            })
    drift = []
    for name, repo_path, repo_md5 in candidates:
        base = os.path.basename(name)
        group = names_by_base[base]
        deployed = copies_by_base.get(base, [])
        if not deployed:
            continue
        stale = []
        for dcp in deployed:
            if dcp["md5"] == repo_md5:
                continue
            owners = [n for n in group if md5_by_name[n] == dcp["md5"]]
            owner = owners[0] if owners else group[0]
            if owner != name:
                continue
            stale.append(dcp)
        if stale:
            drift.append({"script": name, "repo_md5": repo_md5, "repo_path": repo_path, "stale_copies": stale,})
    return drift



def report_deploy_drift(drift):
    """Print a human-readable deploy-drift alert to stdout (cron delivery)."""
    if not drift:
        return
    print("DEPLOY_DRIFT: deployed scripts differ from repo — manual deploy needed (no auto-deploy)")
    def _fmt_ts(ts):
        try:
            return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return "unknown"
    for d in drift:
        print(f"  {d['script']}: repo md5 {d['repo_md5']}")
        for s in d["stale_copies"]:
            print(f"    deploy {s['path']} md5 {s['md5']} (mtime {_fmt_ts(s['mtime'])}) — STALE")


# End of helper module
