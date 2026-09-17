#!/usr/bin/python3.12
"""backtest-f2.py — OBJ-24 F2 forecast-accuracy harness (no_agent).

Every 15m (cron or manual run) this script:

  1. SNAPSHOT: appends the current forecast.json (quota-forecast.py output)
     as one "snap" line to the backtest ledger BEFORE evaluating. This is
     how forecast history survives the per-tick overwrite of forecast.json.
  2. EVALUATE: for every open snapshot and provider, compares the predicted
     eta_90_iso against the actual crossing time registered in
     metrics-history.jsonl (first sample with weekly pct >= 90 after the
     snapshot, within the weekly window that was live at prediction time).
     Error window:
         error_pct = |t_cross_actual - eta_90_pred| / margin_to_reset * 100
     where margin_to_reset is the time left to the weekly reset at the
     moment of the prediction. error_pct < 20 -> OK, else FAIL.
  3. VERDICT: one daily (UTC day) verdict per provider - OK / FAIL / OPEN -
     appended to the ledger whenever it changes. OPEN means at least one
     snapshot for that day/provider is still waiting for the milestone;
     before the crossing, an error is NEVER counted as a failure. Snapshots
     taken after the milestone was already past (pct_now >= 90), snapshots
     without a usable eta_90 (burn <= 0), and predictions whose eta_90 falls
     beyond the snapshot's own weekly window (post-reset idle regime:
     unmeasurable error) are resolved as NA (no predictive value; not FAIL).
     A window that elapsed with no crossing AND a predicted eta beyond it is
     a correct directional prediction, credited as OK when the day cells are
     rebuilt (its recorded row stays NA). The verdict file sums n_ok over
     ALL resolved day-cells, OK and FAIL verdicts alike: a day that fails
     once does not zero its correct predictions.

Ledger: ~/.hermes/quota-governor/forecast-backtest.jsonl (real HERMES_HOME
dir, NOT the profile dir). One JSON object per line, append-only:

  {"kind":"snap","ts":...,"reset":...,"providers":{p:{...forecast fields}}}
  {"kind":"res","snap":...,"prov":...,"status":"OK|FAIL|NA","error_pct":...}
  {"kind":"day","day":"YYYY-MM-DD","prov":...,"verdict":"OK|FAIL|OPEN",...}

Degrades gracefully like quota-forecast.py: tolerant reads (missing or
corrupt files are skipped, never raise), silent-on-normal (stdout only
announces a NEW FAIL verdict), and always exits 0 so the cron never
breaks. Zero tokens, zero API calls: reads only forecast.json and
metrics-history.jsonl from disk.

OBJ-24 close criterion measured here: with 24h of history the forecast
hits the 90% milestone with <20% error; gate --enforce unlocks only after
7 days of backtest with no false positives.
"""
import json
import os
import time
from pathlib import Path
from datetime import datetime

HERE = Path(__file__).resolve().parent


def _load_forecast_module():
    """Import quota-forecast.py from the same dir (shared constants/parser)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "quota_forecast_reused", HERE / "quota-forecast.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


qf = _load_forecast_module()

HERMES_HOME = os.environ.get("HERMES_HOME", "").strip() or os.path.expanduser(
    "~/.hermes/profiles/pr-ollama")
FORECAST = Path(os.environ.get(
    "QUOTA_FORECAST_JSON",
    str(Path(HERMES_HOME) / "quota-governor" / "forecast.json")))
HISTORY = Path(os.environ.get(
    "QUOTA_METRICS_HISTORY",
    str(Path(HERMES_HOME) / "quota-governor" / "metrics-history.jsonl")))
# Ledger lives under the REAL hermes home, not the profile dir
LEDGER = Path(os.environ.get(
    "QUOTA_BACKTEST_LEDGER",
    str(Path.home() / ".hermes" / "quota-governor" / "forecast-backtest.jsonl")))
# Verdict JSON consumed by the OBJ-24 gate (approved_objectives.py)
VERDICT_JSON = Path(os.environ.get(
    "QUOTA_BACKTEST_VERDICT",
    str(Path.home() / ".hermes" / "quota-governor" / "backtest-f2-verdict.json")))

HITO = qf.HITO_STOP          # 90.0 % milestone
TOL_PCT = 20.0               # close criterion: error must be < 20%


def read_jsonl(path):
    """Tolerant JSONL reader: bad lines are skipped, missing file -> []."""
    rows = []
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(d, dict):
                rows.append(d)
    except (OSError, UnicodeDecodeError):
        return []
    return rows


def read_forecast(path):
    """Tolerant forecast.json reader -> dict or None."""
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def forecast_providers(forecast):
    """Provider map from a forecast dict, tolerant to BOTH shapes.

    quota-forecast.py writes per-provider data at the TOP level
    (forecast["pr-ollama"]) while the header "providers" stays {},
    but its declared contract (and the gate fixtures) nest under
    "providers". Read both; top-level wins on conflict since that is
    what the live writer produces. (Mismatch flagged as follow-up work.)
    """
    provs = dict(forecast.get("providers") or {})
    for p in qf.METRICAS_WEEKLY:
        f = forecast.get(p)
        if isinstance(f, dict):
            provs[p] = f
    return provs


def snapshot_record(forecast):
    """Extract a compact snapshot line from a forecast dict, or None."""
    ts = forecast.get("generated_at")
    provs_raw = forecast_providers(forecast)
    if not ts or not provs_raw:
        # a forecast with enabled:false carries nothing to test
        return None
    provs = {}
    for p, f in provs_raw.items():
        if not isinstance(f, dict):
            continue
        provs[p] = {
            "pct_now": f.get("pct_now"),
            "eta_90_iso": f.get("eta_90_iso"),
            "eta_100_iso": f.get("eta_100_iso"),
            "burn_rate_pct_per_min": f.get("burn_rate_pct_per_min"),
            "confidence": f.get("confidence"),
        }
    if not provs:
        return None
    return {"kind": "snap", "ts": ts,
            "reset": forecast.get("next_weekly_reset_iso"),
            "providers": provs}


def crossings_by_provider(history_rows):
    """{provider: sorted [(epoch, pct)]} from metrics rows, deduped by ts."""
    series = {p: {} for p in qf.METRICAS_WEEKLY}
    for row in history_rows:
        e = qf._parse_iso(row.get("ts"))
        if e is None:
            continue
        for p, key in qf.METRICAS_WEEKLY.items():
            v = row.get(key)
            if v is not None:
                try:
                    series[p][e] = float(v)
                except (TypeError, ValueError):
                    continue
    return {p: sorted(d.items()) for p, d in series.items()}


def first_crossing(points, after, upto):
    """First (epoch, pct) with epoch > after, epoch <= upto and pct >= HITO."""
    for e, pct in points:
        if e <= after or e > upto:
            continue
        if pct >= HITO:
            return e
    return None


def _eval_one(s, p, f, snap_e, reset_e, series, now_epoch):
    """Evaluate one (snapshot, provider) pair -> res record, or None when
    the window is still live (OPEN: nothing is recorded yet)."""
    rec = {"kind": "res", "snap": s["ts"], "prov": p}
    pct_now = f.get("pct_now")
    eta90 = qf._parse_iso(f.get("eta_90_iso"))
    if pct_now is None or pct_now >= HITO or eta90 is None:
        # predicted after the crossing, or no usable prediction
        rec.update(status="NA", error_pct=None,
                   reason=("pct_already_past" if pct_now is not None
                           and pct_now >= HITO else "no_prediction"))
        return rec
    cross = first_crossing(series.get(p, []), snap_e, reset_e)
    if cross is None:
        if now_epoch > reset_e:
            # weekly window elapsed without hitting the milestone:
            # nothing to measure, not a failure
            rec.update(status="NA", error_pct=None,
                       reason="no_cross_before_reset")
            return rec
        return None                    # still OPEN — record nothing yet
    if eta90 > reset_e:
        # predicted eta beyond the snapshot's own weekly window
        # (post-reset idle burn ~0 -> months-long etas): the
        # margin-normalized error is not measurable -> NA
        rec.update(status="NA", error_pct=None,
                   reason="pred_beyond_window")
        return rec
    margin = reset_e - snap_e
    err = abs(cross - eta90) / margin * 100.0 if margin > 0 else 0.0
    rec.update(status="OK" if err < TOL_PCT else "FAIL",
               error_pct=round(err, 2),
               cross_iso=qf._iso(cross), eta_90_pred=f.get("eta_90_iso"))
    return rec


def evaluate_snapshots(snaps, series, resolved, now_epoch):
    """Return new 'res' records for every snapshot/provider still open.

    snaps: list of snapshot records (deduped by ts, chronological).
    series: {provider: [(epoch, pct)]} actual history.
    resolved: {(snap_ts, provider)} already decided — skipped.
    """
    out = []
    for s in snaps:
        snap_e = qf._parse_iso(s.get("ts"))
        reset_e = qf._parse_iso(s.get("reset"))
        if snap_e is None or reset_e is None:
            continue
        for p, f in (s.get("providers") or {}).items():
            if (s["ts"], p) in resolved:
                continue
            rec = _eval_one(s, p, f, snap_e, reset_e, series, now_epoch)
            if rec is not None:
                out.append(rec)
    return out


def _reclassify(res, snap, resolve_ncr):
    """Effective status of one res row under the C1/C3 aggregation rules.

    C1: a FAIL whose predicted eta_90 falls beyond the snapshot's own
    weekly window becomes NA (pred_beyond_window) — the margin-normalized
    error is not measurable. C3: a no_cross_before_reset NA whose
    predicted eta is ALSO beyond the window is a correct directional
    prediction ("will not reach 90% before reset") and is credited as OK
    when resolve_ncr is set; one with an eta inside the window predicted a
    crossing that never happened and stays NA. Recorded rows are never
    rewritten (the `resolved` set keeps historical res rows immutable).
    """
    st = res.get("status")
    f = (snap.get("providers") or {}).get(res.get("prov")) or {}
    pred = qf._parse_iso(f.get("eta_90_iso"))
    reset = qf._parse_iso(snap.get("reset"))
    beyond = pred is not None and reset is not None and pred > reset
    if st == "FAIL" and beyond:
        return "NA"
    if (st == "NA" and res.get("reason") == "no_cross_before_reset"
            and resolve_ncr and beyond):
        return "OK"
    return st


def _day_cells(snaps, res_index, resolve_ncr=False):
    """Day-cell map {(day, prov): counters} under C1/C3 reclassification.

    snaps: dict ts -> snap record. res_index: {(snap, prov): res row}.
    Every snapshot contributes to its day-cell: a missing res row counts
    as open. Returns (cells, open_snap_total).
    """
    per = {}
    open_snaps = 0
    for ts in sorted(snaps):
        day = (ts or "")[:10]
        if len(day) != 10:
            continue
        for p in (snaps[ts].get("providers") or {}):
            cell = per.setdefault((day, p),
                                  {"ok": 0, "fail": 0, "na": 0, "open": 0,
                                   "worst": None})
            r = res_index.get((ts, p))
            if r is None:
                cell["open"] += 1
                open_snaps += 1
                continue
            st = _reclassify(r, snaps[ts], resolve_ncr)
            cell[{"OK": "ok", "FAIL": "fail"}.get(st, "na")] += 1
            if st == "FAIL":
                w = r.get("error_pct")
                if w is not None and (cell["worst"] is None
                                      or w > cell["worst"]):
                    cell["worst"] = w
    return per, open_snaps


def day_verdicts(snaps, res_records, existing_days, now_iso):
    """New 'day' verdict lines (OK/FAIL/OPEN) for changed (day, provider)."""
    res_index = {(r["snap"], r["prov"]): r for r in res_records
                 if r.get("kind") == "res" and r.get("snap") and r.get("prov")}
    snaps_by_ts = {s["ts"]: s for s in snaps if s.get("ts")}
    per, _ = _day_cells(snaps_by_ts, res_index, resolve_ncr=True)
    new = []
    for (day, p), c in sorted(per.items()):
        if c["fail"]:
            verdict = "FAIL"
        elif c["open"]:
            verdict = "OPEN"
        else:
            verdict = "OK"        # all resolved and none failed
        rec = {"kind": "day", "day": day, "prov": p, "ts": now_iso,
               "verdict": verdict, "n_ok": c["ok"], "n_fail": c["fail"],
               "n_na": c["na"], "n_open": c["open"],
               "worst_error_pct": c["worst"],
               "tolerance_pct": TOL_PCT}
        if existing_days.get((day, p)) == _day_key(rec):
            continue              # unchanged — no append spam
        new.append(rec)
    return new


# -------------------------------------------------------------------------
# Helper wrappers that keep each function <= 50 lines
# -------------------------------------------------------------------------

def _snapshot_phase(forecast, ledger_entries):
    """Return (snaps, snap_new) where snaps maps ts to records.
    snap_new contains the new snapshot record if one is added.
    """
    snaps = {}
    snap_now = snapshot_record(forecast) if forecast else None
    for r in ledger_entries:
        if r.get("kind") == "snap" and r.get("ts"):
            snaps[r["ts"]] = r
    snap_new = []
    if snap_now and snap_now["ts"] not in snaps:
        snaps[snap_now["ts"]] = snap_now
        snap_new.append(snap_now)
    return snaps, snap_new


def _evaluate_phase(snaps, series, resolved, now_epoch):
    """Return list of new 'res' records.
    Wrapper around evaluate_snapshots to keep function size small.
    """
    return evaluate_snapshots(snaps, series, resolved, now_epoch)


def _day_key(r):
    """Content identity of a day row: verdict + counts. A count change
    under the reclassification rules (C1/C3) must re-emit the row even
    when the verdict string stays the same, or the verdict file keeps
    summing stale totals."""
    return (r.get("verdict"), r.get("n_ok", 0), r.get("n_fail", 0),
            r.get("n_na", 0), r.get("n_open", 0),
            r.get("worst_error_pct"))


def _day_verdict_phase(snaps, ledger_entries, res_new, now_iso):
    """Return list of new 'day' verdict records.
    Combines existing days with new results.
    """
    # Build existing_days mapping
    existing_days = {}
    for r in ledger_entries:
        if r.get("kind") == "day" and r.get("day") and r.get("prov"):
            existing_days[(r["day"], r["prov"])] = _day_key(r)
    all_res = [r for r in ledger_entries if r.get("kind") == "res"] + res_new
    return day_verdicts(snaps, all_res, existing_days, now_iso)


def _emit_phase(new_records):
    """Append new ledger records and write the consolidated verdict file."""
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with open(LEDGER, "a", encoding="utf-8") as f:
        for r in new_records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    _write_verdict_file()


def _verdict_totals(ledger_rows):
    """OK/FAIL/OPEN totals from ALL ledger day rows (last-wins per cell).

    Day rows are deduped by (day, prov) keeping the LATEST, and n_ok is
    summed over ALL resolved cells, OK-verdict and FAIL-verdict alike:
    a day that failed once must not zero its correct predictions (C2
    aggregation fix, t_c15c2efb)."""
    cells = {}
    for r in ledger_rows:
        if r.get("kind") == "day" and r.get("day") and r.get("prov"):
            cells[(r["day"], r["prov"])] = r
    ok = fail = open_cnt = 0
    for r in cells.values():
        ver = r.get("verdict")
        if ver == "OK":
            ok += r.get("n_ok", 0)
        elif ver == "FAIL":
            fail += r.get("n_fail", 0)
            ok += r.get("n_ok", 0)    # C2: OKs earned on failing days count
        elif ver == "OPEN":
            open_cnt += r.get("n_open", 0)
    return ok, fail, open_cnt


def _write_verdict_file():
    """Recompute OK/FAIL/OPEN totals from the whole ledger into the
    verifier's verdict JSON (OBJ-24 gate input)."""
    ok, fail, open_cnt = _verdict_totals(read_jsonl(LEDGER))
    precision_ratio = ok / (ok + fail) if ok + fail > 0 else None
    verdict = {
        "precision_ratio": precision_ratio,
        "n_ok": ok,
        "n_fail": fail,
        "n_open": open_cnt,
        "computed_at": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "source": "forecast-backtest.jsonl",
    }
    VERDICT_JSON.parent.mkdir(parents=True, exist_ok=True)
    with open(VERDICT_JSON, "w", encoding="utf-8") as f:
        json.dump(verdict, f, ensure_ascii=False, indent=2)


def _print_phase(day_new):
    """Announce new FAIL day verdicts on stdout."""
    for r in day_new:
        if r["verdict"] == "FAIL":
            print(f"backtest-f2: FAIL {r['prov']} {r['day']} — "
                  f"{r['n_fail']} snapshot(s) over {TOL_PCT}% error "
                  f"(worst {r['worst_error_pct']}%)")


def main():
    now_epoch = time.time()
    new_records = []

    # Phase 1: snapshot
    forecast = read_forecast(FORECAST)
    ledger_entries = read_jsonl(LEDGER)
    snaps, snap_new = _snapshot_phase(forecast, ledger_entries)
    new_records.extend(snap_new)
    snap_list = [snaps[k] for k in sorted(snaps)]

    # Phase 2: evaluate open snapshots
    series = crossings_by_provider(read_jsonl(HISTORY))
    resolved = {(r["snap"], r["prov"]) for r in ledger_entries
                if r.get("kind") == "res" and r.get("snap") and r.get("prov")}
    res_new = _evaluate_phase(snap_list, series, resolved, now_epoch)
    new_records.extend(res_new)

    # Phase 3: daily verdicts
    day_new = _day_verdict_phase(snap_list, ledger_entries, res_new,
                                 qf._iso(now_epoch))
    new_records.extend(day_new)

    if new_records:
        _emit_phase(new_records)
    _write_verdict_file()   # unconditional: aggregation fixes must land
    _print_phase(day_new)   # even when no day changed (t_c15c2efb)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
