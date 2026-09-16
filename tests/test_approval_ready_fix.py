import pytest, sqlite3, time, os
from pathlib import Path

def setup_db(tmp_path):
    db = tmp_path / "db.sqlite"
    con = sqlite3.connect(db)
    con.execute('CREATE TABLE tasks(id TEXT PRIMARY KEY, title TEXT, body TEXT, status TEXT, assignee TEXT, created_at TIMESTAMP)')
    # A valid multiline body using triple‑quoted string
    body = '''approval-ready\n\n[APPROVAL: pending]\n\nobjetivo: OBJ-01'''  # noqa: E501
    con.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?)',('t1','Test',body,'triage','alice',time.time()))
    con.commit(); con.close()
    return db

@pytest.mark.parametrize('dry', [True, False])
def test_main(tmp_path, dry):
    db=setup_db(tmp_path)
    args=['--db', str(db)]
    if dry:
        args.append('--dry-run')
    else:
        args.append('--execute')
    import subprocess
    res=subprocess.run(['python3','scripts/approval-ready-fix.py']+args, capture_output=True,text=True)
    assert res.returncode==0
    # check that a log entry was written when executing
    if not dry:
        log_dir = Path('~/.hermes/quota-governor/approval-fixes.jsonl').expanduser()
        assert log_dir.exists()
