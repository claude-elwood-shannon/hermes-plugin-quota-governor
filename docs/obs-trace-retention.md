# Trace Retention / Rotation (OBJ-27 F3)

The F0 trace (`quota-governor/obs/trace.jsonl`) is **append-only by design**,
which means it would grow without limit. F3 adds a bounded-window retention
policy: history is never destroyed, only compacted into gzip archives, and
the F0 incremental cursor is never disturbed.

## Policy

| Knob | Default | Env override |
|------|---------|--------------|
| Time window | keep 14 days | `QUOTA_GOVERNOR_TRACE_KEEP_DAYS` |
| Line cap | 100 000 lines in the active file | `QUOTA_GOVERNOR_TRACE_MAX_LINES` |
| Archive GC | keep 12 archive files | `QUOTA_GOVERNOR_TRACE_KEEP_ARCHIVES` |

Eviction order: rows older than the window are archived first; if the
survivors still exceed the line cap, the **oldest** survivors are archived
until the active file holds exactly the newest `max_lines` rows.

## What is never lost

- **The cursor is sacred.** Rotation never reads, writes or removes
  `trace-cursor.json`. The collector cursor is a per-source timestamp
  watermark, independent of the active file's contents, so idempotency
  survives rotation (test: collect → rotate → collect appends nothing twice).
- **Rows without a parseable `ts_epoch_utc` are never window-evicted.**
- **Corrupt (non-JSON) lines stay in the active file** — never silently
  dropped.
- **Write-ahead + atomic replace**: evicted rows are appended to the gzip
  archive FIRST (`obs/archive/trace-<stamp>.jsonl.gz`), then the active file
  is replaced atomically (`os.replace`). A crash between the two leaves a
  superset on disk — nothing is lost.

## Interfaces

- `trace.py retention [--dry-run]` — enforce now; prints a JSON report
  (`rotated`, `reason`, `kept`, `archived`, `archive_path`, ...).
- `trace.py doctor` — extended (F3): `trace_bytes`, `oldest_epoch`,
  `newest_epoch`, `age_days`, effective `retention` policy, `archives`
  count/bytes, and `needs_rotation` (True when policy bounds are already
  exceeded).
- `enforce_retention(hermes_home=..., keep_days=..., max_lines=...,
  keep_archives=..., dry_run=..., now=...)` — library entry point.
  Fail-open like every public helper in this module.
- `trace-retention-cron.sh` — daily no_agent cron (`0 3 * * *`, job
  `trace-retention`), pinned to the profile home where the trace lives.
  Silent stdout when there is nothing rotated (watchdog pattern); emits the
  JSON report only on rotation, archive GC, or error.

## Tests

`scripts/obs/test_trace_retention.py` — 17 tests, fixtures only, no network,
`/usr/bin/python3.12`: no-op inside window, window eviction, line cap,
corrupt-line preservation, ts-less rows kept, archive GC, idempotency,
cursor untouched, collect→rotate→collect without duplication or loss,
doctor size/age/archives/needs_rotation, dry-run, env overrides, privacy.
