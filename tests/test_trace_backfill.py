import importlib
import pytest
spec = importlib.util.spec_from_file_location('trace_backfill', '/data/git/hermes-plugin-quota-governor/scripts/obs/trace-backfill.py')
trace_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(trace_module)
tr = trace_module.tr

# helper to write fake source files

def write_jsonl(lines, path):
    with open(path, 'w', encoding='utf-8') as f:
        for line in lines:
            json.dump(line, f)
            f.write('\n')

# fixture for tmp home
@pytest.fixture
def hermes_home(tmp_path):
    # point environment to tmp dir
    os.environ['HERMES_HOME'] = str(tmp_path)
    return tmp_path

# ensure trace file is empty
@pytest.fixture
def empty_trace(tmp_path):
    path = Path(tr.trace_path(tmp_path))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('')
    return path

# test cursor round-trip

def test_cursor_roundtrip(hermes_home):
    cursor = {'usage-audit': 123.0, 'model-cost-ledger': 456.0}
    module._save_cursor(cursor, hermes_home)
    loaded = module._load_cursor(hermes_home)
    assert loaded == cursor

# test dedup_key consistency

def test_dedup_key_equal():
    row1 = {"source": "usage-audit", "consumer_id": "id1", "cause": "c1", "ts_epoch_utc": 100}
    row2 = {"source": "usage-audit", "consumer_id": "id1", "cause": "c1", "ts_epoch_utc": 100}
    row3 = {"source": "model-cost-ledger", "consumer_id": "id1", "cause": "c1", "ts_epoch_utc": 100}
    assert module.dedup_key(row1) == module.dedup_key(row2)
    assert module.dedup_key(row1) != module.dedup_key(row3)

# end-to-end dry run

def test_run_backfill_dry_run(empty_trace, hermes_home):
    # create dummy sources
    usage = [
        {"consumer_id": "id1", "cause": "c1", "consumer_class": "cron-llm", "ts_epoch_utc": 10}
    ]
    ledger = [
        {"session_id": "id2", "profile": "profile1", "ts": 20, "cost": 5.0, "tokens": {"in": 10, "out": 10}}
    ]
    # write source files
    home = Path(hermes_home)
    home.mkdir(parents=True, exist_ok=True)
    (home / 'usage-audit.jsonl').write_text('')
    # but backfill usage_audit loads via trace.collect_usage_audit, which reads from tr._source_homes
    # easier: create a mock _source_homes returning path set containing source dirs
    module.tr._source_homes = lambda hermes_home=None: [home]
    write_jsonl(usage, home / 'usage-audit.jsonl')
    write_jsonl(ledger, home / 'quota-governor' / 'model-cost-ledger.jsonl')
    # create empty kanban.db in home
    db_path = home / 'kanban.db'
    conn = sqlite3.connect(db_path)
    conn.execute('CREATE TABLE task_events(task_id TEXT, kind TEXT, created_at REAL)')
    conn.execute("INSERT INTO task_events VALUES('t1','created', 100)")
    conn.execute('CREATE TABLE tasks(id TEXT, body TEXT)')
    conn.execute("INSERT INTO tasks VALUES('t1','body')")
    conn.commit(); conn.close()
    # run dry run
    report = module.run_backfill(hermes_home=hermes_home, dry_run=True)
    assert report['ok'] is True
    assert report['appended'] > 0
    assert report['duplicates'] == 0
    assert report['sources']['usage-audit'] == 1

# actual run merging and cursor update

def test_run_backfill_exec(tmp_path):
    home = tmp_path
    os.environ['HERMES_HOME'] = str(home)
    module.tr._source_homes = lambda hm=None: [home]
    # create usage-audit.jsonl
    usage = [
        {"consumer_id": "id1", "cause": "c1", "consumer_class": "cron-llm", "ts_epoch_utc": 10}
    ]
    write_jsonl(usage, home / 'usage-audit.jsonl')
    # create ledger
    ledger = [
        {"session_id": "id2", "profile": "profile1", "ts": 20, "cost": 5.0, "tokens": {"in": 10, "out": 10}}
    ]
    (Path(home, 'quota-governor').mkdir(parents=True, exist_ok=True))
    write_jsonl(ledger, Path(home, 'quota-governor', 'model-cost-ledger.jsonl'))
    # create empty trace
    tr_path = Path(tr.trace_path(home))
    tr_path.parent.mkdir(parents=True, exist_ok=True)
    tr_path.write_text('')
    # run
    report = module.run_backfill(hermes_home=home)
    assert report['ok'] and report['appended']>0 and report['error'] is None
    # verify trace file contains our rows
    with tr_path.open() as f:
        lines = [json.loads(line) for line in f if line.strip()]
    dedup_keys = [module.dedup_key(r) for r in lines]
    assert len(set(dedup_keys)) == len(lines)
