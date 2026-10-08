"""#901 spec §6.3 tooling: the fail-closed verdict logic of the maintenance-
operation and workload analyzers, the clone-only write guard, and the latency
summary. The tools themselves run only on the maintainer's machine against
copies; these cases pin the rules that decide what counts as evidence."""
import datetime as dt
import os
import importlib.util
import json
import pathlib
import sqlite3

import pytest

TOOLS = pathlib.Path(__file__).resolve().parent.parent / "bench" / "write-attribution"
KiB = 1024
MiB = 1024 * KiB


def _live_inputs(run):
    """The input-mode receipt every revision-15 verdict requires (a live run;
    a catch-up receipt beside it carries its finite-frontier evidence)."""
    run = pathlib.Path(run)
    (run / "inputs.json").write_text(json.dumps(
        {"schema": "write-attribution-inputs/1", "mode": "live"}))
    return run


_FRONTIER = {"mode": "live", "files": 0, "problems": []}


def _load(name):
    spec = importlib.util.spec_from_file_location(f"wa_{name}",
                                                  TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_paths_are_classified_by_role():
    analyze = _load("analyze_op")
    assert analyze.classify({
        "/x/conversations.db": [100, 1], "/x/conversations.db-wal": [200, 2],
        "/var/folders/t/etilqs_abc": [50, 1], "/x/op.json": [7, 1]},
        "conversations.db") == {"db": 100, "wal": 200, "temp": 50, "other": 7}


@pytest.mark.parametrize("bytes_, rows, ok", [
    ({"db": 125 * MiB, "wal": 125 * MiB, "temp": 0, "other": 0}, 27_395, True),
    ({"db": 124 * MiB, "wal": 651 * MiB, "temp": 122 * MiB, "other": 0},
     27_395, False),                                   # 56e66f07a, measured
    ({"db": 100, "wal": 106, "temp": 0, "other": 0}, 1, False),   # > 1.05x
    ({"db": 1, "wal": 1, "temp": 0, "other": 0}, 0, False),
])
def test_the_c_op_verdict(bytes_, rows, ok):
    """Revision 6: C-op's whole-run conditions are no temp bytes and WAL at
    most 1.05x the copy; its I4 bound is the deletion receipt's verdict."""
    analyze = _load("analyze_op")
    assert analyze.verdict_c_op(bytes_, rows)[0] is ok


def test_the_r_cov_verdict_compares_attributable_bytes_with_charges():
    analyze = _load("analyze_op")
    parts = {"db": 300, "wal": 400, "temp": 0, "other": 999}
    assert analyze.verdict_r_cov(parts, 700)[0] is True
    assert analyze.verdict_r_cov(parts, 699)[0] is False


P4K = 4096
FRAME = 2 * P4K + 24
GEOMETRY = {"pageSize": P4K, "usableSize": P4K, "pageCount": 100_000}
COEFFICIENTS = {"fixed": 512 * KiB, "perRow": 15 * KiB,
                "perRowFallback": 50 * KiB, "reclaimFixed": 64 * KiB,
                "reservationVersion": 2}


def _deletion_op(rows=1000, *, mode="spill_free", charge=None, op_id=1):
    analyze = _load("analyze_op")
    charged = (analyze.independent_charge(rows, GEOMETRY, COEFFICIENTS)
               if charge is None else charge)
    return {"kind": "delete", "op_id": op_id, "provider": "codex",
            "outcome": "ok", "mode": mode, "rows": rows,
            "charged_bytes": charged, "began": 10.0, "ended": 20.0,
            "geometry": dict(GEOMETRY), "reservation_version": 2,
            "page_count": 100_000, "usable_size": P4K, "page_size": P4K,
            "pointer_map_cap": analyze.independent_m_cap(
                100_000, P4K, rows, P4K)}


def _frame_log(path, pages, *, t=15.0, wal="/x/conversations.db-wal",
               header=True):
    rows = []
    if header:
        rows.append({"t": t, "path": wal, "frame": 0, "pgno": P4K,
                     "commit": 0, "salt": 1})
    rows += [{"t": t, "path": wal, "frame": i + 1, "pgno": p, "commit": 0,
              "salt": 1} for i, p in enumerate(pages)]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def _run_dir(tmp_path, *, receipt=True, dropped=0, ops=None, paths=None,
             frames=None, charged=None, frames_dropped=0):
    if receipt:
        (tmp_path / "selftest.txt").write_text("ok\nselftest: PASS\n")
    _live_inputs(tmp_path)
    ops = ops if ops is not None else [_deletion_op()]
    (tmp_path / "op.json").write_text(json.dumps({
        "pid": 7, "db": "/x/conversations.db",
        "rows": sum(op.get("rows", 0) for op in ops),
        "chargedBytes": (sum(op.get("charged_bytes", 0) for op in ops)
                         if charged is None else charged),
        "coefficients": COEFFICIENTS, "identities": {"tree": "t"},
        "ops": ops}))
    (tmp_path / "wtrace.7.exit").write_text(json.dumps({
        "t": 1, "pid": 7, "dropped": dropped, "framesDropped": frames_dropped,
        "paths": paths or {"/x/conversations.db": [4 * MiB, 9],
                           "/x/conversations.db-wal": [4 * MiB, 9]}}) + "\n")
    if frames is not False:
        _frame_log(tmp_path / "wtrace.7.frames",
                   frames if frames is not None else list(range(500, 1400)))
    return str(tmp_path)


def test_an_unreceipted_run_is_invalid(tmp_path):
    analyze = _load("analyze_op")
    with pytest.raises(analyze.Invalid, match="self-test"):
        analyze.analyze(_run_dir(tmp_path, receipt=False), "c-op")


def test_a_dropped_snapshot_is_invalid(tmp_path):
    analyze = _load("analyze_op")
    with pytest.raises(analyze.Invalid, match="dropped"):
        analyze.analyze(_run_dir(tmp_path, dropped=5), "c-op")


def test_an_incomplete_operation_is_invalid(tmp_path):
    analyze = _load("analyze_op")
    with pytest.raises(analyze.Invalid, match="did not complete"):
        analyze.analyze(_run_dir(tmp_path, ops=[{"outcome": "gated"}]), "r-cov")


def test_a_clean_c_op_run_passes_and_counts_a_foreign_copier(tmp_path):
    analyze = _load("analyze_op")
    run = _run_dir(tmp_path)
    (tmp_path / "copier.8.exit").write_text(json.dumps({
        "t": 2, "pid": 8, "dropped": 0,
        "paths": {"/x/conversations.db": [MiB, 1]}}) + "\n")
    result = analyze.analyze(run, "c-op")
    assert result["pass"] is True, result["problems"]
    assert result["bytes"]["db"] == 5 * MiB
    assert result["foreignCopier"] is True
    [receipt] = result["receipts"]
    assert (receipt["verdict"], receipt["validity"]["pass"],
            receipt["i4"]["pass"], receipt["coverage"]["pass"]) == (
        "PASS", True, True, True)


# ── deletion receipts: four separate verdicts (901-SR-014) ───────────────

def _receipt(frames, *, op=None, temp=0, copy=None, committed=True,
             dropped=0):
    analyze = _load("analyze_op")
    op = op or _deletion_op()
    return analyze.deletion_receipt(
        op=op, frames=frames, frame_source="frame log", geometry=GEOMETRY,
        coefficients=COEFFICIENTS, wal_header_bytes=32, temp_bytes=temp,
        copy_bytes=(len(set(frames or [])) * P4K if copy is None else copy),
        identities={"tree": "t"}, committed=committed,
        evidence_dropped=dropped)


def test_a_covered_deletion_that_violates_i4_fails():
    """Coverage passes (the 512 KiB + 15 KiB/row + B x M_cap charge holds the
    bytes) but a page reached the WAL twice: the receipt FAILS on I4, and no
    coefficient can change that verdict."""
    frames = list(range(500, 600)) + [550]
    receipt = _receipt(frames)
    assert receipt["coverage"]["pass"] is True
    assert receipt["i4"]["pass"] is False
    assert "repeated" in receipt["i4"]["reasons"][0]
    assert receipt["verdict"] == "FAIL"


def test_a_deletion_over_its_page_bounds_fails_i4_per_component():
    analyze = _load("analyze_op")
    rows = 10
    op = _deletion_op(rows)
    limit_pages = (512 * KiB + 12 * KiB * rows) // FRAME
    others = list(range(10_000, 10_000 + limit_pages + 1))   # one too many
    receipt = _receipt(others, op=op)
    assert receipt["coverage"]["pass"] is True
    assert any("D =" in r for r in receipt["i4"]["reasons"])
    cap = analyze.independent_m_cap(100_000, P4K, rows, P4K)
    maps = [2 + 820 * k for k in range(cap + 1)]               # M_cap + 1
    receipt = _receipt(maps, op=op)
    assert any("M_cap" in r for r in receipt["i4"]["reasons"])


def test_an_under_charged_deletion_fails_coverage_and_its_record_is_checked():
    receipt = _receipt(list(range(500, 900)),
                       op=_deletion_op(1000, charge=4096))
    assert receipt["coverage"]["pass"] is False
    assert any("recomputed" in r for r in receipt["coverage"]["reasons"])
    assert any("attributable" in r for r in receipt["coverage"]["reasons"])


@pytest.mark.parametrize("kw, reason", [
    ({"frames": None}, "no frame identities"),
    ({"frames": []}, "no WAL frame"),
    ({"committed": False}, "committed operation set"),
    ({"dropped": 3}, "dropped"),
])
def test_missing_or_unbound_evidence_is_invalid(kw, reason):
    frames = kw.pop("frames", list(range(500, 600)))
    receipt = _receipt(frames, **kw)
    assert receipt["verdict"] == "INVALID"
    assert any(reason in r for r in receipt["validity"]["reasons"])
    assert receipt["i4"] is None and receipt["coverage"] is None


def test_the_fallback_ceiling_includes_the_final_checkpoint():
    """Q5: all attributable writes through the final checkpoint are at most
    40 KiB per deleted row; the WAL alone fits but the copy breaks it."""
    rows = 61_000
    op = _deletion_op(rows, mode="fallback")
    frames = list(range(2, 2 + 300_000))
    within = _receipt(frames, op=op, copy=0)
    assert within["i4"]["pass"] is True, within["i4"]
    over = _receipt(frames, op=op, copy=40 * KiB * rows)
    assert over["i4"]["pass"] is False
    assert "through the final checkpoint" in over["i4"]["reasons"][0]


def test_a_missing_frame_log_makes_the_run_invalid(tmp_path):
    analyze = _load("analyze_op")
    with pytest.raises(analyze.Invalid, match="no frame identities"):
        analyze.analyze(_run_dir(tmp_path, frames=False), "c-op")


def test_dropped_frame_records_make_the_run_invalid(tmp_path):
    analyze = _load("analyze_op")
    with pytest.raises(analyze.Invalid, match="frame records"):
        analyze.analyze(_run_dir(tmp_path, frames_dropped=2), "r-cov")


def test_an_operation_set_that_disagrees_with_the_ledger_is_invalid(tmp_path):
    analyze = _load("analyze_op")
    with pytest.raises(analyze.Invalid, match="committed operation set"):
        analyze.analyze(_run_dir(tmp_path, charged=1), "r-cov")


def test_calibration_is_a_separate_verdict():
    """210 other pages for 100 rows: inside I4 (1,725,360 <= 1,753,088) and
    inside its charge, but 1.25 x the other bytes exceed F + A x rows
    (2,060,288): the receipt PASSES and calibration FAILS on its own."""
    receipt = _receipt(list(range(10_000, 10_210)), op=_deletion_op(100))
    assert receipt["i4"]["pass"] is True, receipt["i4"]
    assert receipt["coverage"]["pass"] is True, receipt["coverage"]
    assert receipt["calibration"]["pass"] is False
    assert receipt["verdict"] == "PASS"


_PLAN = {"planner_version": 1, "plan_digest": "d" * 16, "steps": 16,
         "identified_pages": 3, "unidentified_pages": 6, "fixed_bytes": 64 * KiB}


def _op(phase, op_id, minute, units, *, charge=None, before=1, written=None):
    charged = 64 * KiB + units * 12 * KiB if charge is None else charge
    return {"phase": phase, "op_id": op_id,
            "started_at": f"2026-10-03T12:{minute:02d}:00Z", "outcome": "ok",
            "rows": units if phase == "delete" else 0,
            "pages_reclaimed": units if phase == "reclaim" else 0,
            "charged_bytes": charged, "duration_s": 1.0,
            "balance_before_bytes": before, "balance_after_bytes": before - charged,
            "process_write_bytes": written,
            "duration_ns": 1_000_000_000, **(_PLAN if phase == "reclaim" else {})}


START = dt.datetime(2026, 10, 3, 12, 0, tzinfo=dt.timezone.utc).timestamp()
END = START + 1800


def _reservation(op):
    units = op["rows"] if op["phase"] == "delete" else op["pages_reclaimed"]
    return 64 * KiB + units * 12 * KiB


def test_c_conditions_pass_on_a_paced_interval():
    workload = _load("workload")
    ops = [_op("delete", 1, 1, 1000, written=9000 * 1000)] + [
        _op("reclaim", n, 2 + n, 16) for n in range(2, 7)] + [
        _op("delete", 99, 0, 5)]
    ops[-1]["started_at"] = "2026-10-03T11:59:00Z"          # before the window
    result = workload.c_conditions(ops, START, END, _reservation)
    assert result["valid"] is True and result["problems"] == []
    assert (result["deletions"], result["reclaims"], result["beforeWindow"]) == (
        1, 5, 1)


def test_c_conditions_without_the_required_operations_are_invalid():
    workload = _load("workload")
    ops = [_op("delete", 1, 1, 10)] + [_op("reclaim", n, 2 + n, 16)
                                       for n in range(2, 5)]
    assert workload.c_conditions(ops, START, END, _reservation)["valid"] is False


def test_c_conditions_reject_a_wrong_charge_and_debt():
    workload = _load("workload")
    ops = [_op("delete", 1, 1, 100, charge=1, before=-5,
               written=100 * 13 * KiB)] + [
        _op("reclaim", n, 2 + n, 16) for n in range(2, 7)]
    problems = workload.c_conditions(ops, START, END, _reservation)["problems"]
    assert any("!= its reservation" in p for p in problems)
    assert any("started in debt" in p for p in problems)
    # Revision 6: the per-row process counter is no longer a write bound.
    assert not any("for 100 rows" in p for p in problems)


def test_the_kernel_statistic_subtracts_marked_deletions():
    """Amendment 19 HR-8: the I2 statistic is the candidate kernel's
    `steady_statistic`, fed the deletion's conservatively bounded counter
    readings: of the 50,000 bytes the deletion wrote, only what the 15 s
    samples prove it wrote inside its interval is excluded."""
    workload = _load("workload")
    budget = workload._candidate("_lib_write_budget")
    S = 1_000_000_000
    samples = [budget.CounterSample(t * S, t * 1000 + (50_000 if t >= 410 else 0),
                                    t // 5) for t in range(0, 601, 15)]
    deletions = workload.kernel_deletions(samples, [
        {"began": 400.0, "ended": 410.0, "process_write_bytes": 50_000,
         "rows": 100}])
    stat = budget.steady_statistic(samples, deletions, now_ns=600 * S,
                                   warm_admitted_ns=0)
    assert stat.status == "qualified"
    assert stat.excluded_bytes == 35_000
    assert stat.bytes_written == 350_000 - 35_000
    assert stat.publications == 60


def test_groups_and_the_batch_read_a_clone(tmp_path):
    workload = _load("workload")
    data = tmp_path / "root" / "data"
    data.mkdir(parents=True)
    db = data / "conversations.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE conversation_messages(session_id TEXT, "
                 "timestamp_utc TEXT, text TEXT, blocks_json TEXT)")
    conn.execute("CREATE TABLE codex_conversation_events(conversation_key "
                 "TEXT, timestamp_utc TEXT, payload_json TEXT)")
    old = "2020-01-01T00:00:00Z"
    conn.executemany("INSERT INTO conversation_messages VALUES (?, ?, ?, '[]')",
                     [("big", old, "x")] * 5 + [("mid", old, "y" * 900)] * 3
                     + [("small", old, "z")])
    conn.commit()
    conn.close()
    assert workload.groups(db, "claude", largest=1) == [{"key": "big", "rows": 5}]
    assert workload.groups(db, "claude", median=1) == [{"key": "mid", "rows": 3}]
    assert workload.groups(db, "claude", smallest=1) == [{"key": "small", "rows": 1}]
    # R-cov (v): at most median size (3 rows), most stored bytes per row.
    assert workload.groups(db, "claude", dense=1)[0]["key"] == "mid"
    batch = workload.batch(tmp_path / "root", 29)
    assert batch["claude"] == {"groups": 3, "rows": 9, "largest": 5}
    assert batch["codex"] == {"groups": 0, "rows": 0, "largest": 0}


def test_the_tools_refuse_to_write_outside_the_clone(tmp_path):
    workload = _load("workload")
    (tmp_path / "root").mkdir()
    with pytest.raises(SystemExit, match="outside the clone"):
        workload._inside(tmp_path / "root", tmp_path / "elsewhere.jsonl")
    assert workload._inside(tmp_path / "root", tmp_path / "root" / "a" / "b") \
        == (tmp_path / "root" / "a" / "b").resolve()


def test_the_latency_summary_reports_median_and_maximum():
    latency = _load("latency")
    assert latency.summarize([0.3, 0.1, 0.2]) == {
        "samplesS": [0.3, 0.1, 0.2], "medianS": 0.2, "maxS": 0.3}


def test_reclaim_eligibility_mirrors_the_start_thresholds():
    """§5.4: reclaim starts only above 2 GiB AND 20% free; A/B's compaction
    is needed only while a store is eligible."""
    workload = _load("workload")
    page = 4096
    gib = 1024 ** 3 // page
    assert workload.reclaim_eligible(page, 10 * gib, 3 * gib) is True
    assert workload.reclaim_eligible(page, 10 * gib, 2 * gib) is False   # not > 2 GiB
    assert workload.reclaim_eligible(page, 20 * gib, 3 * gib) is False   # 15% < 20%
    assert workload.reclaim_eligible(page, 1000, 0) is False


def test_compact_skips_a_store_whose_reclaim_is_not_eligible(tmp_path, monkeypatch, capsys):
    workload = _load("workload")
    data = tmp_path / "root" / "data"
    data.mkdir(parents=True)
    conn = sqlite3.connect(data / "conversations.db")
    conn.execute("CREATE TABLE t(x)")
    conn.commit()
    conn.close()

    def no_vacuum(*_a, **_k):
        raise AssertionError("an ineligible store must not be vacuumed")
    monkeypatch.setattr(workload.subprocess, "run", no_vacuum)
    assert workload.main(["compact", "--tree", str(tmp_path), "--root",
                          str(tmp_path / "root")]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["skipped"] == "reclaim not eligible"


def test_scratch_seed_and_append_write_only_inside_the_clone(tmp_path):
    """Operator decision esc-0db0e25cdfa4: B/D append into an extra scratch
    root beside the real (read-only) roots. The seed is a valid Claude line
    and a Codex rollout with a conversation identity; appends continue it and
    never leave ROOT/scratch."""
    workload = _load("workload")
    root = tmp_path / "root"
    (root / "data").mkdir(parents=True)
    seeded = workload.seed_scratch(root)
    claude = root / "scratch" / "claude" / "projects"
    codex = root / "scratch" / "codex" / "sessions"
    assert seeded == {"claude": str(claude), "codex": str(codex)}
    first = next(codex.rglob("*.jsonl")).read_text().splitlines()[0]
    assert json.loads(first)["type"] == "session_meta"
    out = workload.append(root, 0.2, 60, scratch=True)
    assert out["bytes"] > 0
    for path in root.rglob("*.jsonl"):
        assert (root / "scratch") in path.parents
    assert not (root / "home").exists() and not (root / "codex").exists()


def _perf_run(tmp_path, *, traced=True):
    run = tmp_path / "run"
    run.mkdir()
    _live_inputs(run)
    (run / "pid").write_text("42")
    (run / "window.json").write_text(json.dumps(
        {"traceStart": 0, "warm": 100, "start": 100, "end": 500}))
    (run / "rusage.jsonl").write_text("".join(json.dumps(row) + "\n" for row in (
        {"t": 90, "pid": 42, "rc": 0, "resident": 5 * MiB, "footprint_peak": 9 * MiB},
        {"t": 400, "pid": 42, "rc": 0, "resident": 7 * MiB, "footprint_peak": 11 * MiB},
        {"t": 410, "pid": 42, "rc": 1, "resident": None, "footprint_peak": None})))

    def record(seq, at, ms, *, dispatch="full", cold=False, period=5000):
        return {"seq": seq, "dispatch": dispatch, "cold": cold,
                "duration_ns": ms * 1_000_000, "period_ns": period * 1_000_000,
                "published_at": dt.datetime.fromtimestamp(
                    at, dt.timezone.utc).isoformat()}

    def tree(gather_ms, total_ms):
        return {"name": "build", "elapsed_ms": total_ms, "children": [
            {"name": "doctor.gather", "elapsed_ms": gather_ms, "children": []}]}

    def iso(at):
        return dt.datetime.fromtimestamp(at, dt.timezone.utc).isoformat()

    ring = [record(1, 50, 9000, cold=True), record(2, 150, 1000),
            record(3, 200, 3000), record(4, 250, 2000, dispatch="idle"),
            record(5, 300, 4000), record(6, 600, 8000)]
    # (ring, build-tree instant, doctor.gather ms, ingest-tree instant); the
    # last poll repeats the stored trees of the one before it.
    polls = ((ring[:1], 50, 99.0, 55), (ring[:3], 150, 30.0, 160),
             (ring[:5], 300, 50.0, 310), (ring, 300, 50.0, 310))
    for n, (records, gen, gather, ingest_at) in enumerate(polls):
        diag = {"tick": {"records": records},
                "phases": tree(gather, 900.0) if traced else None,
                "generated_at": iso(gen),
                "ingest_phases": ({"name": "ingest", "elapsed_ms": 200.0 + n,
                                   "children": [{"name": "sync_cache",
                                                 "elapsed_ms": 120.0,
                                                 "children": []}]}
                                  if traced else None),
                "ingest_generated_at": iso(ingest_at)}
        (run / f"perf-{n:05d}.json").write_text(json.dumps({"diagnostic": diag}))
    return run


def test_perf_evidence_reads_distinct_warm_builds_peaks_and_phases(tmp_path):
    workload = _load("workload")
    out = workload.perf_evidence(str(_perf_run(tmp_path)))
    # Distinct full warm builds published inside [warm, end]: seq 2, 3, 5.
    assert out["fullBuildMs"] == [1000.0, 3000.0, 4000.0]
    assert out["fullBuildP50Ms"] == 3000.0 and out["fullBuildP95Ms"] == 4000.0
    assert out["publishPeriodP95Ms"] == 5000.0          # seq 2-5, any dispatch
    assert out["footprintPeakBytes"] == 11 * MiB          # failed sample ignored
    assert out["residentMaxBytes"] == 7 * MiB
    # One value per distinct stored tree built after warm admission (150 and
    # 300), never per poll, and never the cold tree at 50.
    assert out["phases"]["build/doctor.gather"] == {"n": 2, "medianMs": 40.0,
                                                    "maxMs": 50.0}
    assert out["ingestPhases"]["ingest/sync_cache"]["n"] == 2
    assert out["valid"] is True


def test_perf_evidence_without_a_phase_tree_is_invalid(tmp_path):
    workload = _load("workload")
    out = workload.perf_evidence(str(_perf_run(tmp_path, traced=False)))
    assert out["valid"] is False
    assert "no phase tree" in " ".join(out["problems"])


_PROOF_SCHEMA = {
    "cache.db": (
        "CREATE TABLE session_files (path TEXT PRIMARY KEY, last_byte_offset INTEGER)",
        "CREATE TABLE session_entries (id INTEGER PRIMARY KEY, source_path TEXT, line_offset INTEGER,"
        " input_tokens INTEGER, output_tokens INTEGER, cache_create_tokens INTEGER,"
        " cache_read_tokens INTEGER, account_key TEXT)",
        "CREATE TABLE codex_session_files (path TEXT PRIMARY KEY, last_byte_offset INTEGER,"
        " source_root_key TEXT)",
        "CREATE TABLE codex_session_entries (id INTEGER PRIMARY KEY, source_root_key TEXT,"
        " source_path TEXT, line_offset INTEGER, total_tokens INTEGER, account_key TEXT)",
        "CREATE TABLE quota_window_snapshots (id INTEGER PRIMARY KEY, source_path TEXT,"
        " line_offset INTEGER, used_percent REAL, account_key TEXT)",
        "CREATE TABLE codex_source_roots (root_key TEXT, root_path TEXT)",
        "CREATE TABLE quota_window_change_log (x)",
        "CREATE TABLE codex_accounting_change_log (x)"),
    "conversations.db": (
        "CREATE TABLE conversation_source_files (path TEXT PRIMARY KEY, last_byte_offset INTEGER)",
        "CREATE TABLE conversation_messages (id INTEGER PRIMARY KEY, session_id TEXT, uuid TEXT,"
        " source_path TEXT, byte_offset INTEGER, text TEXT, account_key TEXT)",
        "CREATE TABLE codex_conversation_source_files (path TEXT PRIMARY KEY,"
        " last_byte_offset INTEGER, source_root_key TEXT)",
        "CREATE TABLE codex_conversation_messages (id INTEGER PRIMARY KEY, source_path TEXT,"
        " line_offset INTEGER, content_len INTEGER, account_key TEXT)"),
}


def _proof_store(data, files):
    """`files`: [(claude path, codex path)] to track; real ones exist on disk."""
    data.mkdir(parents=True)
    for name, ddl in _PROOF_SCHEMA.items():
        c = sqlite3.connect(data / name)
        for stmt in ddl:
            c.execute(stmt)
        c.commit()
        c.close()
    for claude, codex, root in files:
        add_proof_rows(data, claude, codex, root)


def add_proof_rows(data, claude, codex, root):
    c = sqlite3.connect(data / "cache.db")
    c.execute("INSERT INTO session_files VALUES (?, 200)", (claude,))
    c.executemany("INSERT INTO session_entries (source_path, line_offset, input_tokens,"
                  " output_tokens, cache_create_tokens, cache_read_tokens, account_key)"
                  " VALUES (?, ?, 1, 1, 0, 0, 'a')", [(claude, 0), (claude, 100)])
    c.execute("INSERT INTO codex_session_files VALUES (?, 200, ?)", (codex, root))
    c.execute("INSERT OR IGNORE INTO codex_source_roots VALUES (?, ?)", (root, root))
    c.executemany("INSERT INTO codex_session_entries (source_root_key, source_path,"
                  " line_offset, total_tokens, account_key) VALUES (?, ?, ?, 5, 'a')",
                  [(root, codex, 0), (root, codex, 100)])
    c.commit()
    c.close()
    c = sqlite3.connect(data / "conversations.db")
    c.execute("INSERT INTO conversation_source_files VALUES (?, 200)", (claude,))
    c.executemany("INSERT INTO conversation_messages (session_id, uuid, source_path,"
                  " byte_offset, text, account_key) VALUES (?, ?, ?, ?, 'hi', 'a')",
                  [(claude, f"{claude}-{o}", claude, o) for o in (0, 100)])
    c.execute("INSERT INTO codex_conversation_source_files VALUES (?, 200, ?)", (codex, root))
    c.executemany("INSERT INTO codex_conversation_messages (source_path, line_offset,"
                  " content_len, account_key) VALUES (?, ?, 3, 'a')",
                  [(codex, 0), (codex, 100)])
    c.commit()
    c.close()


def _proof(tmp_path, mutate):
    import shutil
    import subprocess
    import sys
    real = tmp_path / "real"
    real.mkdir()
    claude, codex = real / "s.jsonl", real / "rollout.jsonl"
    claude.write_text("x")
    codex.write_text("x")
    mid = tmp_path / "mid"
    _proof_store(mid, [(str(claude), str(codex), "realkey")])
    after = tmp_path / "after"
    shutil.copytree(mid, after)
    scratch = tmp_path / "clone" / "scratch"
    add_proof_rows(after, f"{scratch}/claude/seed.jsonl", f"{scratch}/codex/seed.jsonl",
                   "scratchkey")
    mutate(after, str(claude))
    proc = subprocess.run([sys.executable, str(TOOLS / "scratch_proof.py"), str(mid),
                           str(after), str(scratch), str(tmp_path / "proof.json")],
                          capture_output=True, text=True)
    return proc.returncode, json.loads((tmp_path / "proof.json").read_text())


def test_scratch_proof_passes_an_added_root_that_leaves_the_real_rows_alone(tmp_path):
    rc, out = _proof(tmp_path, lambda after, claude: None)
    assert (rc, out["verdict"], out["fail"]) == (0, "PASS", [])


def test_scratch_proof_fails_a_replay_that_keeps_counts_equal(tmp_path):
    def replay(after, claude):
        c = sqlite3.connect(after / "cache.db")
        rows = c.execute("SELECT source_path, line_offset, input_tokens, output_tokens,"
                         " cache_create_tokens, cache_read_tokens, account_key FROM"
                         " session_entries WHERE source_path=?", (claude,)).fetchall()
        c.execute("DELETE FROM session_entries WHERE source_path=?", (claude,))
        c.executemany("INSERT INTO session_entries (source_path, line_offset, input_tokens,"
                      " output_tokens, cache_create_tokens, cache_read_tokens, account_key)"
                      " VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
        c.commit()
        c.close()
    rc, out = _proof(tmp_path, replay)
    assert rc == 1
    assert any(f.startswith("cache.session_entries:") for f in out["fail"])


def test_scratch_proof_fails_a_dropped_file_that_is_still_on_disk(tmp_path):
    def purge(after, claude):
        c = sqlite3.connect(after / "cache.db")
        c.execute("DELETE FROM session_files WHERE path=?", (claude,))
        c.commit()
        c.close()
    rc, out = _proof(tmp_path, purge)
    assert rc == 1
    assert any("still on disk were dropped" in f for f in out["fail"])


# ── C: complete capture, completeness and receipts (#901 D4/D5) ─────────

def test_c_completeness_binds_to_the_committed_operations():
    workload = _load("workload")
    ops = [{"op_id": i, "outcome": "ok", "charged_bytes": 100, "ended": 50.0 + i}
           for i in range(1, 4)]
    start = {"t": 40.0, "opSeq": 10, "charged": 1000}
    good = {"t": 60.0, "opSeq": 13, "charged": 1300}
    assert workload.c_completeness(ops, start, good) == []
    missed = {"t": 60.0, "opSeq": 14, "charged": 1400}
    problems = workload.c_completeness(ops, start, missed)
    assert any("op_seq advanced 4, 3" in p for p in problems)
    assert workload.c_completeness(ops, None, good) == [
        "no record snapshots bracket the window"]


def test_the_capture_gives_each_operation_its_precise_bounds(tmp_path):
    workload = _load("workload")
    (tmp_path / "ops.jsonl").write_text("\n".join(json.dumps(r) for r in (
        {"reportedAt": 100.0, "phase": "delete", "op_id": 3,
         "duration_s": 2.5, "outcome": "ok"},
        {"reportedAt": 101.0, "phase": "reclaim", "op_id": 0,
         "outcome": "skipped"})) + "\n")
    [op] = workload.captured_ops(str(tmp_path))
    assert (op["began"], op["ended"], op["duration_ns"]) == (
        97.5, 100.0, 2_500_000_000)
    assert workload.captured_ops(str(tmp_path / "missing")) is None


def _c_run(tmp_path, *, capture=True, frames=True, snapshots=True):
    run = tmp_path / "run"
    run.mkdir()
    _live_inputs(run)
    (run / "pid").write_text("42")
    (run / "window.json").write_text(json.dumps(
        {"traceStart": 0, "warm": 0, "start": 0, "end": 1800}))
    (run / "rusage.jsonl").write_text("")
    analyze = _load("analyze_op")
    rows = 1000
    geometry = {"pageSize": 4096, "usableSize": 4096, "pageCount": 100_000}
    coefficients = {"fixed": 512 * KiB, "perRow": 15 * KiB,
                    "perRowFallback": 50 * KiB}
    charge = analyze.independent_charge(rows, geometry, coefficients)
    frame = 2 * 4096 + 24
    ops = [{"reportedAt": 100.0, "phase": "delete", "op_id": 1,
            "duration_s": 5.0, "outcome": "ok", "rows": rows,
            "mode": "spill_free", "charged_bytes": charge,
            "page_count": 100_000, "usable_size": 4096, "page_size": 4096,
            "pointer_map_cap": analyze.independent_m_cap(
                100_000, 4096, rows, 4096),
            "reservation_version": 2, "balance_before_bytes": 4 * MiB,
            "balance_after_bytes": 4 * MiB - charge,
            "started_at": "1970-01-01T00:01:35Z"}] + [
        {"reportedAt": 200.0 + 60 * n, "phase": "reclaim", "op_id": 1 + n,
         "duration_s": 0.1, "outcome": "ok", "pages_reclaimed": 16,
         "charged_bytes": 10 * frame + 64 * KiB, "balance_before_bytes": 1,
         "balance_after_bytes": 1 - (10 * frame + 64 * KiB),
         **_PLAN, "page_size": 4096, "reservation_version": 2,
         "started_at": f"1970-01-01T00:{3 + n:02d}:20Z"}
        for n in range(1, 6)]
    if capture:
        (run / "ops.jsonl").write_text("".join(json.dumps(o) + "\n"
                                               for o in ops))
    if snapshots:
        (run / "record-start.json").write_text(json.dumps(
            {"t": 10.0, "opSeq": 0, "charged": 0,
             "geometry": {**geometry, "source": "observed"}}))
        (run / "record-end.json").write_text(json.dumps(
            {"t": 1790.0, "opSeq": len(ops),
             "charged": sum(o["charged_bytes"] for o in ops),
             "continuation": _CONTINUATION}))
    _steady_evidence(run, 0.0, 1800.0)
    if frames:
        _frame_log(run / "wtrace.42.frames", list(range(500, 700)), t=98.0)
    _admitted_and_terminal(run)
    return str(run)


_CONTINUATION = {"balance_bytes": 0, "as_of": "1970-01-01T00:30:00Z",
                 "continuation_cutoff": None, "eligible": True,
                 "next_attempt_at": None, "progress": None}


def _steady_evidence(run, start, end, *, pid=42, rate=1000, temp_pid=None,
                     failed_at=(), gap=None, reset_at=None, pubs_per_sample=1,
                     burst=None):
    """The window evidence the analyzer and the candidate kernel read: a
    passing self-test receipt, the dashboard's rusage every 15 s (`rate`
    bytes per second; a failed read at each of `failed_at`; no sample inside
    `gap`; the counter restarting at `reset_at`), its interposer snapshots
    every 5 s, and dashboard-perf samples whose tick_seq counts publications
    (mtime = the sample time). `temp_pid` adds a child trace that writes
    etilqs bytes inside the window."""
    import os as _os
    run = pathlib.Path(run)
    (run / "selftest.txt").write_text("selftest: PASS\n")
    (run / "pid").write_text(str(pid))
    rows, t, seq = [], start - 330, 0
    while t <= end + 15:
        if gap and gap[0] < t < gap[1]:
            t += 15
            continue
        failed = any(abs(t - f) < 1 for f in failed_at)
        dwrite = int(rate * (t - start + 1000))
        if reset_at is not None and t >= reset_at:
            dwrite = int(rate * (t - reset_at))
        rows.append({"t": t, "pid": pid, "rc": 1 if failed else 0,
                     "dwrite": None if failed else dwrite,
                     "resident": 50 * MiB, "footprint_peak": 60 * MiB})
        seq += pubs_per_sample
        perf = run / f"perf-{int(t + 10000):05d}.json"
        perf.write_text(json.dumps({"diagnostic": {"tick": {
            "tick_seq": seq, "dispatch_counts": {"idle": seq, "full": 0,
                                                 "degraded": 0}}}}))
        _os.utime(perf, (t + 0.5, t + 0.5))
        t += 15
    (run / "rusage.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    db = "/clone/data/conversations.db"
    snaps, t = [], start - 330
    while t <= end + 15:
        paths = {db: [int(t - start + 1000), 1]}
        if burst:
            paths["/clone/data/burst"] = [
                int(max(0, min(t, burst[1]) - burst[0]) * burst[2]) + 1, 1]
        snaps.append({"t": t, "pid": pid, "dropped": 0, "framesDropped": 0,
                      "footprintPeak": 60 * MiB, "paths": paths})
        t += 5
    (run / f"wtrace.{pid}").write_text("".join(json.dumps(r) + "\n" for r in snaps))
    if temp_pid is not None:
        child = [{"t": start + 10, "pid": temp_pid, "dropped": 0, "paths": {}},
                 {"t": start + 60, "pid": temp_pid, "dropped": 0,
                  "paths": {"/tmp/etilqs_abc": [8192, 2]}}]
        (run / f"wtrace.{temp_pid}").write_text(
            "".join(json.dumps(r) + "\n" for r in child))
    return run


def _admitted_and_terminal(run, *, admitted=True, alive=True):
    _live_inputs(run)
    (run / "admission.json").write_text(json.dumps({"valid": True}))
    (run / "catchup.json").write_text(json.dumps({"admitted": admitted,
                                                  "frontier": _FRONTIER}))
    (run / "terminal.json").write_text(json.dumps({"t": 1806, "alive": alive}))
    if not (run / "rusage.jsonl").exists() or not (run / "rusage.jsonl").read_text():
        with open(run / "rusage.jsonl", "a") as fh:
            fh.write(json.dumps({"t": 1800, "pid": 42, "rc": 0, "dwrite": 1}) + "\n")
    (run / "perf-99999.json").write_text(json.dumps({"diagnostic": {}}))


def test_c_without_a_complete_capture_is_invalid(tmp_path, capsys):
    workload = _load("workload")
    assert workload.c_verdict(_c_run(tmp_path, capture=False), "tree") == 2
    assert "no complete operation capture" in capsys.readouterr().out


def test_c_without_record_snapshots_is_invalid(tmp_path, capsys):
    workload = _load("workload")
    assert workload.c_verdict(_c_run(tmp_path, snapshots=False), "tree") == 2
    assert "no record snapshots" in capsys.readouterr().out


def test_c_without_a_frame_log_is_invalid(tmp_path, capsys):
    """A deletion without frame-identity evidence makes C invalid."""
    workload = _load("workload")
    assert workload.c_verdict(_c_run(tmp_path, frames=False), "tree") == 2
    assert "no frame identities" in capsys.readouterr().out


def test_c_judges_each_deletion_through_its_receipt(tmp_path, capsys):
    workload = _load("workload")
    workload.c_verdict(_c_run(tmp_path), "tree")
    out = capsys.readouterr().out
    result = _last_json(out)
    [receipt] = result["receipts"]
    assert (receipt["validity"]["pass"], receipt["i4"]["pass"]) == (True, True)
    assert not any("deletion 1" in p for p in result["problems"])


def test_a_synthetic_fallback_group_copies_events_and_derived_rows(tmp_path):
    workload = _load("workload")
    root = tmp_path / "root"
    (root / "data").mkdir(parents=True)
    conn = sqlite3.connect(root / "data" / "conversations.db")
    conn.executescript("""
        CREATE TABLE codex_conversation_events (id INTEGER PRIMARY KEY,
            source_path TEXT NOT NULL, line_offset INTEGER NOT NULL,
            conversation_key TEXT, timestamp_utc TEXT,
            UNIQUE(source_path, line_offset));
        CREATE TABLE codex_conversation_messages (id INTEGER PRIMARY KEY,
            conversation_key TEXT NOT NULL, source_path TEXT NOT NULL,
            line_offset INTEGER NOT NULL, text TEXT,
            UNIQUE(source_path, line_offset));
        CREATE TABLE codex_conversation_file_touches (message_id INTEGER,
            conversation_key TEXT NOT NULL, source_path TEXT NOT NULL,
            file_path TEXT, tool TEXT, UNIQUE(message_id, file_path, tool));
        CREATE TABLE codex_conversation_rollups (
            conversation_key TEXT NOT NULL PRIMARY KEY, n INTEGER);
    """)
    for i in range(30):
        conn.execute("INSERT INTO codex_conversation_events (source_path, "
                     "line_offset, conversation_key, timestamp_utc) "
                     "VALUES ('/r.jsonl', ?, 'big', '2020-01-01T00:00:00Z')",
                     (i,))
        cur = conn.execute("INSERT INTO codex_conversation_messages "
                           "(conversation_key, source_path, line_offset, text)"
                           " VALUES ('big', '/r.jsonl', ?, 'hi')", (i,))
        conn.execute("INSERT INTO codex_conversation_file_touches VALUES "
                     "(?, 'big', '/r.jsonl', '/f', 'edit')", (cur.lastrowid,))
    conn.execute("INSERT INTO codex_conversation_rollups VALUES ('big', 30)")
    conn.commit()
    conn.close()
    counts = workload.synth_group(root, "big", "synthetic", 3)
    assert counts == {"events": 90, "messages": 90, "touches": 90,
                      "rollups": 1}
    conn = sqlite3.connect(root / "data" / "conversations.db")
    assert conn.execute("SELECT COUNT(*) FROM codex_conversation_events "
                        "WHERE conversation_key='synthetic'").fetchone()[0] == 90
    assert conn.execute("SELECT COUNT(*) FROM codex_conversation_events "
                        "WHERE conversation_key='big'").fetchone()[0] == 30
    conn.close()


# ── R-cov (i): per-chunk certification ───────────────────────────────────

def _chunk_op(identified, unid, *, charge=None, fixed=64 * KiB):
    frame = 2 * 4096 + 24
    return {"op_id": 9, "page_size": 4096, "fixed_bytes": fixed,
            "charged_bytes": ((len(identified) + unid + 1) * frame + fixed
                              if charge is None else charge),
            "began": 1.0, "ended": 1.2,
            "plan": {"identified": sorted(identified), "unidentified": unid,
                     "steps": 16}}


def _independent(identified, unid, detail=()):
    return {"identified": sorted(identified), "unidentified": unid,
            "detail": list(detail)}


def test_a_chunk_inside_its_split_bound_is_certified():
    analyze = _load("analyze_op")
    ident = {1, 50, 51}
    out = analyze.certify_chunk(_chunk_op(ident, 4), _independent(ident, 4),
                                [1, 50, 70, 71, 72], repeated=0, temp_bytes=0,
                                wal_header_bytes=0)
    assert out["pass"] is True, out["problems"]


@pytest.mark.parametrize("written, kw, fragment", [
    ([1, 50, 51, 70, 71, 72, 73, 74, 75], {}, "|W| 9"),   # over |ID|+UNID+1
    ([1, 70, 71, 72, 73, 74, 75], {}, "ID| 6"),           # pages outside ID
    ([1, 50], {"repeated": 1}, "repeated"),
    ([1, 50], {"temp_bytes": 4096}, "temp"),
])
def test_a_chunk_outside_its_bound_fails(written, kw, fragment):
    analyze = _load("analyze_op")
    ident = {1, 50, 51}
    args = {"repeated": 0, "temp_bytes": 0, "wal_header_bytes": 0, **kw}
    out = analyze.certify_chunk(_chunk_op(ident, 4), _independent(ident, 4),
                                written, **args)
    assert out["pass"] is False
    assert any(fragment in p for p in out["problems"]), out["problems"]


def test_a_chunk_whose_plan_disagrees_with_its_recomputation_fails():
    analyze = _load("analyze_op")
    out = analyze.certify_chunk(_chunk_op({1, 50}, 4),
                                _independent({1, 50, 51}, 4), [1],
                                repeated=0, temp_bytes=0, wal_header_bytes=0)
    assert any("independent recomputation" in p for p in out["problems"])
    out = analyze.certify_chunk(_chunk_op({1, 50}, 4, charge=1),
                                _independent({1, 50}, 4), [1],
                                repeated=0, temp_bytes=0, wal_header_bytes=0)
    assert any("planned reservation" in p for p in out["problems"])


def test_r_cov_i_requires_its_relocation_evidence():
    analyze = _load("analyze_op")
    ok = [{"detail": [
        {"kind": "interior", "children": 300, "regions": 260},
        {"kind": "leaf", "heads": 3}, {"kind": "overflow", "nonTerminal": True}]}]
    assert analyze.chunk_validity(ok) == []
    thin = [{"detail": [{"kind": "interior", "children": 300, "regions": 40}]}]
    missing = analyze.chunk_validity(thin)
    assert len(missing) == 3 and "298 children" in missing[0]


def test_r_cov_vi_requires_real_fallback_spill_evidence():
    analyze = _load("analyze_op")
    receipt = {"mode": "fallback", "tempBytes": 5 * MiB,
               "frames": {"repeatedPointerMap": 12}}
    assert analyze.fallback_validity(
        receipt, control_distinct_pages=70_000, page_size=4096,
        cache_bytes=256 * MiB) == []
    missing = analyze.fallback_validity(
        {"mode": "spill_free", "tempBytes": 0, "frames": {}},
        control_distinct_pages=1000, page_size=4096, cache_bytes=64 * MiB)
    assert len(missing) == 5


def test_the_independent_recompute_matches_the_product_on_a_pinned_run(
        tmp_path, monkeypatch):
    """R-cov (i) end to end at fixture scale: `maintenance_op.py --pin` runs
    product chunks on a fragmented store and keeps `db.start` and
    `wal.pinned`; `independent_plan` rebuilds each chunk's starting snapshot
    and recomputes its plan with its own code. They must agree, and every
    chunk must be certified on its own pinned frames."""
    import importlib as _il
    import subprocess as _sp
    import sys as _sys

    _sys.path.insert(0, str(TOOLS.parent.parent / "tests"))
    from conftest import load_script, redirect_paths
    import _retention_fixtures as fx

    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    ns["open_cache_db"]().close()
    conn = ns["open_conversations_db"](attach_cache=False)
    retention = _il.import_module("_lib_conversation_retention")
    fx.build_overflow_tail(conn, rows=60, gap_pages=6,
                           normalize=lambda c: retention.normalize_reclaim_record(
                               c, dt.datetime(2026, 10, 3, tzinfo=dt.timezone.utc)))
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db = pathlib.Path(retention._resolve_main_db_path(conn))
    conn.close()
    out = tmp_path / "op.json"
    env = {**os.environ, "HOME": str(tmp_path),
           "CCTALLY_DATA_DIR": str(tmp_path / "data"),
           "CCTALLY_DISABLE_DEV_AUTODETECT": "1"}
    child = _sp.run([_sys.executable, str(TOOLS / "maintenance_op.py"),
                     "--tree", str(TOOLS.parent.parent), "--db", str(db),
                     "--out", str(out), "--start-utc", "2026-10-03T12:00:00Z",
                     "--reclaim-chunks", "8", "--pages", "16", "--pin"],
                    env=env, capture_output=True, text=True, timeout=90)
    assert child.returncode == 0, child.stderr
    report = json.loads(out.read_text())
    assert report["pinned"] is True and len(report["ops"]) == 8
    _sys.path.insert(0, str(TOOLS))
    independent_plan = _il.import_module("independent_plan")
    analyze = _load("analyze_op")
    snaps = independent_plan.PinnedSnapshots(
        f"{db}.start", f"{db}.wal.pinned", 4096)
    cache = independent_plan.FreelistCache()
    try:
        for op in report["ops"]:
            assert op["outcome"] == "ok", op
            snaps.advance(op["walFrom"])
            plan = independent_plan.recompute(snaps, op["plan"]["steps"], cache)
            assert (plan["identified"], plan["unidentified"]) == (
                op["plan"]["identified"], op["plan"]["unidentified"]), op["op_id"]
            pinned = snaps.frames_between(op["walFrom"], op["walTo"])
            header = 32 if op["walFrom"] == 0 else 0
            cert = analyze.certify_chunk(op, plan, pinned, repeated=0,
                                         temp_bytes=0,
                                         wal_header_bytes=header)
            assert cert["pass"] is True, cert["problems"]
    finally:
        snaps.close()




def test_next_start_funds_the_next_operation_when_the_record_is_ahead_of_the_clock(
        monkeypatch):
    """The durable `as_of` never moves backwards (`PacingState.charge` keeps
    `max(now, as_of)`), so a record stamped ahead of the run's clock accrues
    nothing until the clock passes it. `next_start` must fund the next
    operation from the later of the two: advancing from the clock alone left
    production R-cov (i) gated after 19 chunks."""
    import importlib as _il
    import sys as _sys
    _sys.path.insert(0, str(TOOLS.parent.parent / "bin"))
    retention = _il.import_module("_lib_conversation_retention")
    mop = _load("maintenance_op")
    clock = dt.datetime(2026, 10, 4, 1, 21, 7, tzinfo=dt.timezone.utc)
    for ahead_min, balance in ((20, -255120), (0, -255120), (90, -1),
                               (5, -4 * 1024 * 1024)):
        as_of = clock + dt.timedelta(minutes=ahead_min)
        record = {"policy_version": retention.RETENTION_POLICY_VERSION,
                  "balance_bytes": balance,
                  "as_of": as_of.strftime("%Y-%m-%dT%H:%M:%SZ"), "ledger": []}
        monkeypatch.setattr(mop, "ledger_total", lambda _r, _db, rec=record: (0, rec))
        start = mop.next_start(retention, "unused.db", clock)
        state = retention.PacingState.from_record(record, start)
        assert state.available(start) >= 0, (ahead_min, balance, start)
        assert start >= as_of
    funded = {"policy_version": retention.RETENTION_POLICY_VERSION,
              "balance_bytes": 1024, "as_of": "2026-10-04T02:00:00Z", "ledger": []}
    monkeypatch.setattr(mop, "ledger_total", lambda _r, _db: (0, funded))
    assert mop.next_start(retention, "unused.db", clock) == clock

# ── D6: drained admission, the analyzer split, the appender, terminal samples ─

def test_an_undrained_clone_is_invalid(tmp_path, capsys):
    workload = _load("workload")
    run = pathlib.Path(_c_run(tmp_path))
    (run / "catchup.json").write_text(json.dumps({"admitted": False}))
    assert workload.c_verdict(str(run), "tree") == 2
    assert "catch-up receipt" in capsys.readouterr().out
    (run / "admission.json").unlink()
    assert workload.admission_problems(str(run)) == [
        "no admission record (admission.json)"]


def test_a_missing_terminal_sample_is_invalid(tmp_path):
    workload = _load("workload")
    run = pathlib.Path(_c_run(tmp_path))
    (run / "terminal.json").write_text(json.dumps({"t": 1806, "alive": False}))
    (run / "rusage.jsonl").write_text("")
    problems = workload.terminal_problems(str(run), 1800.0)
    assert any("not alive" in p for p in problems)
    assert any("rusage" in p for p in problems)


def test_the_baseline_is_analyzed_through_the_candidate_kernel(tmp_path):
    """The analyzer split (901-PA-002): `--tree` is data, never an import
    path - a baseline tree without the kernel is still judged by this
    tree's limits and reservation formula."""
    workload = _load("workload")
    baseline = tmp_path / "baseline-tree"
    (baseline / "bin").mkdir(parents=True)
    (baseline / "bin" / "_lib_write_budget.py").write_text(
        "raise ImportError('the baseline kernel must never be imported')\n")
    limits = workload._load_limits(str(baseline))
    assert limits.bytes_per_minute == 16 * MiB
    assert str(baseline / "bin") not in __import__("sys").path
    assert workload._candidate("_lib_conversation_retention").__file__.startswith(
        str(TOOLS.parent.parent / "bin"))


def test_ambient_growth_labels_an_a_window(tmp_path):
    workload = _load("workload")
    (tmp_path / "jsonl.jsonl").write_text("".join(json.dumps(r) + "\n" for r in (
        {"t": 100, "claude": 10, "codex": 5}, {"t": 400, "claude": 10, "codex": 9})))
    out = workload.ambient_growth(str(tmp_path), 100, 400)
    assert out["label"] == "ambient" and out["bytes"]["codex"] == 4
    (tmp_path / "jsonl.jsonl").write_text("".join(json.dumps(r) + "\n" for r in (
        {"t": 100, "claude": 10, "codex": 5}, {"t": 400, "claude": 10, "codex": 5})))
    assert workload.ambient_growth(str(tmp_path), 100, 400)["label"] == \
        "append-free"


def test_the_runner_owns_and_reaps_its_appender_group(tmp_path):
    """run-workload.sh's own `reap` (extracted verbatim) kills the
    appender's whole process group, its children included."""
    import re
    import subprocess as _sp
    import time as _time

    script = (TOOLS / "run-workload.sh").read_text()
    # Amendment 19 HR-1: the guard owns the traps and runs `reap` on exit.
    assert "set -m" in script and 'wa_guard "$LIMIT" reap' in script
    reap = re.search(r"^reap\(\) \{.*?^\}", script, re.S | re.M).group(0)
    harness = tmp_path / "harness.sh"
    harness.write_text("\n".join([
        # reap records its kills and ends through _inputs.sh (Amendment 13
        # O3); live mode sends them without a receipts directory
        f"P={TOOLS}", 'WA_LIVE_ONLY=1 . "$P/_inputs.sh"',
        "set -m", "PID=''", reap,
        "( sleep 300 & sleep 300 & wait ) &",
        "APID=$!", "echo $APID > " + str(tmp_path / "pgid"),
        "sleep 0.5", "reap", "sleep 0.5",
        "pgrep -g $(cat " + str(tmp_path / "pgid") + ") && echo ALIVE || echo REAPED"]))
    out = _sp.run(["bash", str(harness)], capture_output=True, text=True,
                  timeout=30, env={**os.environ, "WRITE_ATTRIBUTION_INPUTS": "live"})
    assert out.stdout.strip().endswith("REAPED"), (out.stdout, out.stderr)


def _seed_roots(root):
    workload = _load("workload")
    (root / "data").mkdir(parents=True)
    return workload.seed_scratch(root)


def test_catchup_admits_a_drained_clone_and_refuses_an_expired_deadline(
        tmp_path):
    import subprocess as _sp
    import sys as _sys

    root = tmp_path / "clone"
    _seed_roots(root)
    env = {**os.environ, "HOME": str(tmp_path),
           "CCTALLY_DATA_DIR": str(root / "data"),
           "CLAUDE_CONFIG_DIR": str(root / "scratch" / "claude"),
           "CODEX_HOME": str(root / "scratch" / "codex"),
           "CCTALLY_DISABLE_DEV_AUTODETECT": "1"}
    out = tmp_path / "catchup.json"
    child = _sp.run([_sys.executable, str(TOOLS / "catchup.py"), "--tree",
                     str(TOOLS.parent.parent), "--out", str(out)],
                    env=env, capture_output=True, text=True, timeout=50)
    receipt = json.loads(out.read_text())
    assert child.returncode == 0, (child.stdout, receipt["problems"])
    assert receipt["admitted"] is True
    assert receipt["watermarks"]["claude"]["files"] == 1
    assert receipt["watermarks"]["codex"]["files"] == 1
    assert receipt["phases"]["verify"]["syncs"]["sync_cache"] is not None
    assert receipt["footprintPeakBytes"] is None or receipt["footprintPeakBytes"] > 0
    expired = _sp.run([_sys.executable, str(TOOLS / "catchup.py"), "--tree",
                       str(TOOLS.parent.parent), "--out", str(out),
                       "--deadline-s", "0"],
                      env=env, capture_output=True, text=True, timeout=50)
    assert expired.returncode == 2
    assert "admission deadline" in json.loads(out.read_text())["problems"][-1]


# ── E1: the stable Claude orphan residual (spec §6.3 revision 10, `dc8`) ──

def _catchup(args, env, timeout=240):
    import subprocess as _sp
    import sys as _sys

    return _sp.run([_sys.executable, str(TOOLS / "catchup.py"), *args],
                   env=env, capture_output=True, text=True, timeout=timeout)


def test_catchup_admits_a_stable_claude_residual_only_through_its_preparation(
        tmp_path):
    """The production shape: a tracked transcript that is gone from disk and
    shares its session with a survivor, so `--prune-orphans` leaves it as a
    residual and the Claude full walk never certifies. Revision 9 refuses the
    clone; revision 10 admits it only through the source's preparation
    receipt, and the product certificate stays unminted."""
    root = tmp_path / "clone"
    _seed_roots(root)
    project = (root / "scratch" / "claude" / "projects"
               / "-bench-write-attribution")
    seed = json.loads((project / "bench901-seed.jsonl").read_text())
    resumed = dict(seed, uuid="bench901-seed-resumed-0",
                   requestId="req-bench901-seed-resumed-0",
                   message=dict(seed["message"],
                                id="msg-bench901-seed-resumed-0"))
    gone = project / "bench901-seed-resumed.jsonl"
    gone.write_text(json.dumps(resumed, separators=(",", ":")) + "\n")
    env = {**os.environ, "HOME": str(tmp_path),
           "CCTALLY_DATA_DIR": str(root / "data"),
           "CLAUDE_CONFIG_DIR": str(root / "scratch" / "claude"),
           "CODEX_HOME": str(root / "scratch" / "codex"),
           "CCTALLY_DISABLE_DEV_AUTODETECT": "1"}
    tree = str(TOOLS.parent.parent)
    out = tmp_path / "catchup.json"
    first = _catchup(["--tree", tree, "--out", str(out)], env)
    assert first.returncode == 0, (first.stdout, first.stderr)
    gone.unlink()

    # Revision 9's refusal, reproduced: the residual is the only cause.
    out.unlink()
    plain = _catchup(["--tree", tree, "--out", str(out)], env)
    receipt = json.loads(out.read_text())
    assert plain.returncode == 2
    assert receipt["problems"] == ["sync_cache is not certifiable as a full walk"]

    # A source copy without a preparation receipt stays INVALID.
    out.unlink()
    unprepared = _catchup(["--tree", tree, "--out", str(out),
                           "--source", str(root)], env)
    assert unprepared.returncode == 2, (unprepared.stdout, unprepared.stderr)
    receipt = json.loads(out.read_text())
    assert receipt["problems"][0].startswith(
        "sync_cache is not certifiable as a full walk")
    assert "no preparation receipt" in receipt["problems"][0]
    assert receipt["claudeAdmission"]["route"] == "refused"

    data_files = sorted(p.name for p in (root / "data").iterdir())
    prep = _catchup(["prep", "--source", str(root), "--tree", tree], env,
                    timeout=60)
    assert prep.returncode == 0, (prep.stdout, prep.stderr)
    assert sorted(p.name for p in (root / "data").iterdir()) == data_files
    prepared = json.loads((root / "prep-receipt.json").read_text())
    assert prepared["valid"] is True, prepared["problems"]
    assert len(prepared["residual"]) == 1
    residual = prepared["residual"]
    assert residual[0].endswith("bench901-seed-resumed.jsonl")
    assert set(prepared["source"]["databases"]) == {
        "cache.db", "conversations.db", "stats.db"}

    out.unlink()
    admitted = _catchup(["--tree", tree, "--out", str(out),
                         "--source", str(root)], env)
    receipt = json.loads(out.read_text())
    assert admitted.returncode == 0, (admitted.stdout, receipt["problems"])
    assert receipt["admitted"] is True
    claude = receipt["claudeAdmission"]
    assert claude["route"] == "stable-residual exception"
    assert claude["exception"]["problems"] == []
    assert all(c["ok"] for c in claude["exception"]["checks"])
    assert receipt["preparation"]["receipt"]["residual"] == residual
    for phase in ("catchUp", "verify"):
        # The original certification result is kept, and stays a refusal.
        assert receipt["certification"][phase]["sync_cache"] is False
        assert receipt["certification"][phase]["sync_codex_cache"] is True
        sync = receipt["phases"][phase]["syncs"]["sync_cache"]
        assert sync["full_walk_complete"] is False
        observed = receipt["phases"][phase]["claude"]
        for name in ("walkMissingBefore", "walkMissingAfter",
                     "confirmedAbsentBefore", "confirmedAbsentAfter"):
            assert observed[name] == residual, name
    assert receipt["phases"]["verify"]["syncs"]["sync_cache"][
        "files_processed"] == 0
    prune = receipt["prune"]
    assert prune["ran"] is True and prune["exit"] == 0
    assert prune["result"]["residual_paths"] == residual
    assert (prune["result"]["pruned_files"], prune["result"]["pruned_entries"],
            prune["result"]["pruned_messages"]) == (0, 0, 0)
    assert receipt["pendingState"]["problems"] == []
    conn = sqlite3.connect(root / "data" / "cache.db")
    try:
        # The exception never mints a product certificate.
        assert conn.execute(
            "SELECT 1 FROM cache_meta "
            "WHERE key = 'claude_ingest_walk_complete'").fetchone() is None
    finally:
        conn.close()


_R = "/u/.claude/projects/p/gone.jsonl"
_R2 = "/u/.claude/projects/p/also-gone.jsonl"
_SETS = ("walkMissingBefore", "walkMissingAfter", "confirmedAbsentBefore",
         "confirmedAbsentAfter")


def _residual_case():
    def stats(processed):
        return {"files_total": 6, "files_processed": processed,
                "files_skipped_unchanged": 6 - processed,
                "lock_contended": False, "files_failed": 0,
                "files_deferred_torn": 0, "deferred_reason": None,
                "maintenance_failed": False, "full_walk_complete": False}

    def observed():
        return {"dedupApplied": True,
                "discovery": {"invocations": 1, "complete": True,
                              "errors": [], "count": 6},
                "absenceErrors": [], **{name: [_R] for name in _SETS}}

    versions = {"cache.db": 47, "conversations.db": 5, "stats.db": 1017}
    return {
        "prep": {"kind": "catchup-prep", "valid": True, "problems": [],
                 "residual": [_R],
                 "source": {"root": "/src", "databases": {
                     db: {"userVersion": v} for db, v in versions.items()}},
                 "tree": {"path": "/tree", "gitRev": "a" * 40}},
        "passes": {"catchUp": {"exit": 0, "error": None, "stats": stats(2),
                               "claude": observed()},
                   "verify": {"exit": 0, "error": None, "stats": stats(0),
                              "claude": observed()}},
        "prune": {"ran": True, "exit": 0, "error": None, "result": {
            "pruned_files": 0, "pruned_entries": 0, "pruned_messages": 0,
            "residual_paths": [_R], "contended": False,
            "prune_refused": False, "prune_refused_files": 0,
            "prune_refused_state": None}},
        "identity": {"gitRev": "a" * 40, "sourceRoot": "/src",
                     "userVersions": dict(versions)},
    }


def _judge(case):
    catchup = _load("catchup")
    return catchup.claude_residual_admission(
        case["prep"], case["passes"], case["prune"],
        identity=case["identity"])


def test_the_residual_exception_admits_a_stable_prepared_residual():
    assert _judge(_residual_case()) == []


def _all_sets(case, value):
    for phase in ("catchUp", "verify"):
        for name in _SETS:
            case["passes"][phase]["claude"][name] = list(value)
    case["prune"]["result"]["residual_paths"] = list(value)


def _set(path, value):
    def mutate(case):
        target = case
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
    return mutate


_RESIDUAL_REFUSALS = [
    ("added", lambda c: _all_sets(c, [_R2, _R]), "catchUp.residual"),
    ("removed", lambda c: _all_sets(c, []), "verify.residual"),
    ("replaced", lambda c: _all_sets(c, [_R2]), "catchUp.residual"),
    ("changed-after-verify",
     _set(("passes", "verify", "claude", "walkMissingAfter"), [_R, _R2]),
     "verify.residual"),
    ("wrong-tree", _set(("identity", "gitRev"), "b" * 40),
     "preparation.tree"),
    ("unknown-tree", _set(("prep", "tree", "gitRev"), None),
     "preparation.tree"),
    ("wrong-source", _set(("identity", "sourceRoot"), "/elsewhere"),
     "preparation.source"),
    ("unknown-source", _set(("identity", "sourceRoot"), None),
     "preparation.source"),
    ("wrong-version", _set(("identity", "userVersions", "cache.db"), 46),
     "preparation.userVersions"),
    ("no-identity", _set(("identity",), None), "preparation.tree"),
    ("no-receipt", _set(("prep",), None), "preparation.present"),
    ("invalid-receipt", _set(("prep", "valid"), False), "preparation.valid"),
    ("empty-residual",
     lambda c: (_all_sets(c, []), _set(("prep", "residual"), [])(c)),
     "preparation.residual"),
    ("catch-up-census",
     _set(("passes", "catchUp", "stats", "files_skipped_unchanged"), 3),
     "catchUp.census"),
    ("verify-census",
     _set(("passes", "verify", "stats", "files_total"), 7), "verify.census"),
    ("verify-processed-one",
     lambda c: c["passes"]["verify"]["stats"].update(
         files_processed=1, files_skipped_unchanged=5),
     "verify.claudeProcessed"),
    ("missing-stats", _set(("passes", "verify", "stats"), None),
     "verify.stats"),
    ("missing-catch-up-stats", _set(("passes", "catchUp", "stats"), None),
     "catchUp.stats"),
    ("nonzero-exit", _set(("passes", "catchUp", "exit"), 1), "catchUp.exit"),
    ("raised", _set(("passes", "verify", "exit"), None), "verify.exit"),
    ("dedup-outstanding",
     _set(("passes", "catchUp", "claude", "dedupApplied"), False),
     "catchUp.dedupApplied"),
    ("dedup-unknown",
     _set(("passes", "verify", "claude", "dedupApplied"), None),
     "verify.dedupApplied"),
    ("discovery-disagrees",
     _set(("passes", "catchUp", "claude", "walkMissingBefore"),
          [_R, "/u/.claude/projects/p/unreadable-dir/present.jsonl"]),
     "catchUp.residual"),
    ("absence-error",
     _set(("passes", "verify", "claude", "absenceErrors"),
          [f"{_R2}: PermissionError"]), "verify.absence"),
    ("discovery-incomplete",
     _set(("passes", "catchUp", "claude", "discovery", "complete"), False),
     "catchUp.discovery"),
    ("discovery-error",
     _set(("passes", "verify", "claude", "discovery", "errors"),
          ["OSError(5)"]), "verify.discovery"),
    ("discovery-missing", _set(("passes", "catchUp", "claude"), None),
     "catchUp.discovery"),
    ("prune-files", _set(("prune", "result", "pruned_files"), 1),
     "prune.mutation"),
    ("prune-entries", _set(("prune", "result", "pruned_entries"), 3),
     "prune.mutation"),
    ("prune-messages", _set(("prune", "result", "pruned_messages"), 2),
     "prune.mutation"),
    ("prune-contended", _set(("prune", "result", "contended"), True),
     "prune.contention"),
    ("prune-refused", _set(("prune", "result", "prune_refused"), True),
     "prune.refusal"),
    ("prune-exit", _set(("prune", "exit"), 3), "prune.exit"),
    ("prune-residual", _set(("prune", "result", "residual_paths"), [_R2]),
     "prune.residual"),
    ("prune-missing", _set(("prune",), None), "prune.present"),
    ("prune-not-run", _set(("prune", "result"), None), "prune.present"),
]


@pytest.mark.parametrize("mutate,check",
                         [(m, c) for _n, m, c in _RESIDUAL_REFUSALS],
                         ids=[n for n, _m, _c in _RESIDUAL_REFUSALS])
def test_the_residual_exception_refuses_anything_but_the_stable_residual(
        mutate, check):
    case = _residual_case()
    mutate(case)
    problems = _judge(case)
    assert any(p.startswith(check + ":") for p in problems), problems


_DIRTY = {"lock_contended": True, "files_failed": 1, "files_deferred_torn": 1,
          "deferred_reason": "identity_torn", "prune_refused": True,
          "budget_exhausted": True, "maintenance_failed": True}


@pytest.mark.parametrize("phase", ["catchUp", "verify"])
@pytest.mark.parametrize("field", sorted(_DIRTY))
def test_the_residual_exception_refuses_each_dirty_common_clean_field(
        field, phase):
    case = _residual_case()
    case["passes"][phase]["stats"][field] = _DIRTY[field]
    problems = _judge(case)
    assert any(p.startswith(f"{phase}.commonClean:") and field in p
               for p in problems), problems


def test_the_harness_common_clean_fields_are_the_product_predicates():
    """The exception re-states `provider_sync_certifiable`'s common_clean
    block over serialized statistics; pin the two lists together."""
    import inspect
    import re

    import _lib_ingest_frontier as frontier

    catchup = _load("catchup")
    source = inspect.getsource(frontier.provider_sync_certifiable)
    block = source.split("common_clean = not any((", 1)[1].split("))", 1)[0]
    assert set(re.findall(r'getattr\(stats, "(\w+)"', block)) == set(
        catchup.COMMON_CLEAN_FIELDS) == set(_DIRTY)


def test_admission_keeps_codex_and_conversation_certification_unexcepted():
    """The exception is ONLY the Claude alternative: a failed Codex walk or
    conversation census is refused whatever it says."""
    import types

    import _lib_ingest_frontier as frontier

    catchup = _load("catchup")
    base = dict(_residual_case()["passes"]["verify"]["stats"])
    walk = types.SimpleNamespace(**base)
    clean = types.SimpleNamespace(**dict(base, full_walk_complete=True))
    census = types.SimpleNamespace(files_total=2, files_processed=0,
                                   files_skipped_unchanged=2)
    final = {"sync_cache": walk, "sync_codex_cache": clean,
             "sync_claude_conversations": census,
             "sync_codex_conversations": census}
    drained = {"ran": True, "consumed": 0, "error": None}
    assert catchup.admission_problems(frontier, final, [], drained,
                                      claude_exception=[]) == []
    codex = dict(final, sync_codex_cache=walk)
    assert catchup.admission_problems(frontier, codex, [], drained,
                                      claude_exception=[]) == [
        "sync_codex_cache is not certifiable as a full walk"]
    short = types.SimpleNamespace(files_total=2, files_processed=0,
                                  files_skipped_unchanged=1)
    for name in ("sync_claude_conversations", "sync_codex_conversations"):
        assert catchup.admission_problems(
            frontier, dict(final, **{name: short}), [], drained,
            claude_exception=[]) == [
            f"{name} is not certifiable as a full census"]
    assert catchup.admission_problems(frontier, final, [], drained) == [
        "sync_cache is not certifiable as a full walk"]
    refused = catchup.admission_problems(
        frontier, final, [], drained,
        claude_exception=["prune.mutation: 1 file(s) pruned"])
    assert len(refused) == 1 and refused[0].startswith(
        "sync_cache is not certifiable as a full walk")
    assert "prune.mutation" in refused[0]


def _store(path, *, version=0, applied=(), skipped=(), meta=(),
           projection=None):
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE schema_migrations "
                     "(name TEXT PRIMARY KEY, applied_at_utc TEXT NOT NULL)")
        conn.execute("CREATE TABLE schema_migrations_skipped (name TEXT "
                     "PRIMARY KEY, skipped_at_utc TEXT, reason TEXT)")
        conn.execute("CREATE TABLE cache_meta (key TEXT PRIMARY KEY, value)")
        conn.executemany("INSERT INTO schema_migrations VALUES (?, 't')",
                         [(n,) for n in applied])
        conn.executemany(
            "INSERT INTO schema_migrations_skipped VALUES (?, 't', 'r')",
            [(n,) for n in skipped])
        conn.executemany("INSERT INTO cache_meta VALUES (?, '1')",
                         [(k,) for k in meta])
        if projection is not None:
            conn.execute("CREATE TABLE stats_quota_projection_state "
                         "(id INTEGER PRIMARY KEY, incomplete INTEGER)")
            conn.execute("INSERT INTO stats_quota_projection_state "
                         "VALUES (1, ?)", (projection,))
        conn.execute(f"PRAGMA user_version = {int(version)}")
        conn.commit()
    finally:
        conn.close()


def test_pending_state_names_outstanding_migrations_and_maintenance(tmp_path):
    """`pendingWork` holds only cursor gaps, so migration state and pending
    maintenance are checked explicitly (spec §6.3: no replay, backfill or
    recovery work outstanding)."""
    catchup = _load("catchup")
    registries = {"cache.db": ["001_a", "002_b", "003_c"],
                  "conversations.db": ["001_x"]}
    flags = ("conversation_backfill_pending", "codex_replay_from_zero_pending")
    data = tmp_path / "clean"
    data.mkdir()
    _store(data / "cache.db", version=3, applied=("001_a", "003_c"),
           skipped=("002_b",))
    _store(data / "conversations.db", version=1, applied=("001_x",))
    _store(data / "stats.db", version=1017, projection=0)
    state = catchup.pending_state(data, registries=registries,
                                  stats_epoch=1017, flags=flags)
    assert catchup.pending_problems(state) == []
    assert state["migrations"]["cache.db"]["skipped"] == ["002_b"]

    data = tmp_path / "pending"
    data.mkdir()
    _store(data / "cache.db", version=2, applied=("001_a",),
           skipped=("002_b",), meta=("codex_replay_from_zero_pending",))
    _store(data / "conversations.db", version=1, applied=("001_x",),
           meta=("conversation_backfill_pending",))
    _store(data / "stats.db", version=1016, projection=1)
    state = catchup.pending_state(data, registries=registries,
                                  stats_epoch=1017, flags=flags)
    problems = catchup.pending_problems(state)
    assert any("cache.db" in p and "003_c" in p for p in problems), problems
    assert any("codex_replay_from_zero_pending" in p for p in problems)
    assert any("conversation_backfill_pending" in p for p in problems)
    assert any("stats.db" in p and "1016" in p for p in problems)
    assert any("quota projection" in p for p in problems)
    (data / "conversations.db").unlink()
    assert any("conversations.db" in p for p in catchup.pending_problems(
        catchup.pending_state(data, registries=registries, stats_epoch=1017,
                              flags=flags)))


def _source_copy(tmp_path, *, rows):
    src = tmp_path / "src"
    (src / "data").mkdir(parents=True)
    _store(src / "data" / "conversations.db", version=5)
    _store(src / "data" / "stats.db", version=1017)
    conn = sqlite3.connect(src / "data" / "cache.db")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("CREATE TABLE session_files (path TEXT PRIMARY KEY, "
                     "size_bytes INTEGER, session_id TEXT)")
        conn.executemany("INSERT INTO session_files VALUES (?, ?, 's')",
                         rows)
        conn.execute("PRAGMA user_version = 47")
        conn.commit()
    finally:
        conn.close()
    return src


def test_prep_records_the_confirmed_absent_residual_of_a_closed_copy(tmp_path):
    catchup = _load("catchup")
    present = tmp_path / "roots" / "present.jsonl"
    present.parent.mkdir()
    present.write_text("{}\n")
    gone = str(tmp_path / "roots" / "gone.jsonl")
    src = _source_copy(tmp_path, rows=[
        (str(present), 3), (gone, 40), (str(tmp_path / "roots" / "z.jsonl"), 0)])
    before = sorted(p.name for p in (src / "data").iterdir())
    tree = TOOLS.parent.parent
    assert catchup.main(["prep", "--source", str(src), "--tree",
                         str(tree)]) == 0
    assert sorted(p.name for p in (src / "data").iterdir()) == before
    receipt = json.loads((src / "prep-receipt.json").read_text())
    assert receipt["kind"] == "catchup-prep" and receipt["valid"] is True
    assert receipt["residual"] == [gone]
    assert receipt["source"]["root"] == os.path.realpath(src)
    dbs = receipt["source"]["databases"]
    assert {db: dbs[db]["userVersion"] for db in dbs} == {
        "cache.db": 47, "conversations.db": 5, "stats.db": 1017}
    assert dbs["cache.db"]["bytes"] == (src / "data" / "cache.db").stat().st_size
    assert receipt["tree"]["path"] == str(tree.resolve())
    assert receipt["tree"]["gitRev"]
    assert receipt["trackedPositive"]["count"] == 2
    assert receipt["createdAt"].endswith("Z")


def test_prep_refuses_an_open_copy_and_unconfirmed_absence(tmp_path,
                                                           monkeypatch):
    catchup = _load("catchup")
    gone = str(tmp_path / "gone.jsonl")
    blocked = str(tmp_path / "blocked" / "x.jsonl")
    src = _source_copy(tmp_path, rows=[(gone, 40), (blocked, 40)])
    real_stat = catchup._stat

    def stat(path, *a, **k):
        if str(path) == blocked:
            raise PermissionError(13, "Permission denied", path)
        return real_stat(path, *a, **k)

    monkeypatch.setattr(catchup, "_stat", stat)
    code, receipt = catchup.prepare(src, TOOLS.parent.parent)
    assert code == 2 and receipt["valid"] is False
    assert receipt["residual"] == [gone]
    assert any(blocked in p and "PermissionError" in p
               for p in receipt["problems"]), receipt["problems"]
    monkeypatch.setattr(catchup, "_stat", real_stat)

    (src / "data" / "cache.db-wal").write_bytes(b"\0" * 32)
    code, receipt = catchup.prepare(src, TOOLS.parent.parent)
    assert code == 2 and receipt["valid"] is False
    assert any("not closed" in p for p in receipt["problems"])
    (src / "data" / "cache.db-wal").unlink()
    (src / "data" / "stats.db").unlink()
    code, receipt = catchup.prepare(src, TOOLS.parent.parent)
    assert code == 2 and any("stats.db" in p for p in receipt["problems"])


def test_the_receipt_serializes_lists_explicitly():
    import dataclasses as _dc

    @_dc.dataclass
    class Result:
        pruned_files: int = 0
        residual_paths: list = _dc.field(default_factory=list)
        prune_refused_state: "str | None" = None

    catchup = _load("catchup")
    assert catchup._wire(Result(residual_paths=["/b", "/a"])) == {
        "pruned_files": 0, "residual_paths": ["/a", "/b"],
        "prune_refused_state": None}


def test_run_workload_hands_the_source_copy_to_the_catch_up():
    """A missing receipt on a source whose Claude walk does not certify is
    INVALID; the runner names the source so catchup.py finds its receipt."""
    script = (TOOLS / "run-workload.sh").read_text()
    line = next(l for l in script.splitlines() if "catchup.py" in l
                and "--tree" in l)
    assert '--source "$SRC"' in line


# ── D7: sqlattr completeness, the hook runner, the journal probe, latency ─

_FAKE_CCTALLY = '''
import sqlite3, sys, threading
lib = sys.modules["sqlattr_stub"]
c = sqlite3.connect(":memory:")
for i in range(250):
    c.execute(f"CREATE TABLE t{i}(x)")
lib.add_temp(1000)          # after the main thread's LAST statement started
def worker():
    w = sqlite3.connect(":memory:")
    w.execute("CREATE TABLE worker_table(x)")
    lib.add_temp(500)       # after the worker's last statement started
t = threading.Thread(target=worker)
t.start(); t.join()
lib.add_untracked_temp(300) # outside any statement interval
'''


def test_sqlattr_flushes_final_intervals_and_keeps_complete_totals(tmp_path):
    """901-PA-004: each thread's final interval is charged (the worker's at
    its end, the main thread's at exit), every statement is kept (250+, not
    a top 200), and the unattributed bucket reconciles attributed bytes
    with the interposer's process total."""
    import subprocess as _sp
    import sys as _sys

    tree = tmp_path / "tree"
    (tree / "bin").mkdir(parents=True)
    (tree / "bin" / "cctally").write_text(_FAKE_CCTALLY)
    out = tmp_path / "attr.json"
    env = {**os.environ, "SQLATTR_DYLIB": "stub", "SQLATTR_OUT": str(out),
           "SQLATTR_TREE": str(tree)}
    child = _sp.run([_sys.executable, str(TOOLS / "sqlattr.py")], env=env,
                    capture_output=True, text=True, timeout=60)
    assert child.returncode == 0, child.stderr
    data = json.loads(out.read_text())
    assert data["final"] is True
    assert len(data["stmts"]) >= 251 and len(data["top"]) == 200
    by_sql = {row[0].split("  @@  ")[0]: row for row in data["stmts"]}
    assert by_sql["CREATE TABLE worker_table(x)"][2] == 500
    assert by_sql["CREATE TABLE t249(x)"][2] == 1000
    assert data["attributed"]["temp"] == 1500
    assert data["interposer"]["temp"] == 1800
    assert data["unattributed"]["temp"] == 300
    assert data["flushedThreads"] >= 2


def test_the_journal_probe_reports_family_maxima_of_a_labelled_control():
    probe = _load("journal_probe")
    run = {"control": {"tempStore": "DEFAULT"}, "unattributed": {"temp": 7},
           "footprintPeakBytes": 123, "stmts": [
               ["REPLACE INTO codex_conversation_fts_data(id, block) VALUES(?,?)"
                "  @@  a.py:1:f", 3, 900_000, 0, 310_000, 1.0],
               ["REPLACE INTO conversation_fts_data(id, block) VALUES(?,?)"
                "  @@  b.py:2:g", 2, 300_000, 0, 150_000, 1.0],
               ["INSERT OR IGNORE INTO quota_window_snapshots (a) VALUES (?)"
                "  @@  c.py:3:h", 9, 90_000, 0, 30_000, 1.0]]}
    report = probe.summarize([run], input_bytes=2048)
    fts = report["families"]["fts_segment_write"]
    assert (fts["maxJournalBytes"], fts["calls"], fts["cumulativeTempBytes"]) \
        == (310_000, 5, 1_200_000)
    assert fts["callSite"] == "a.py:1:f"
    assert report["families"]["quota_snapshot_insert"]["maxJournalBytes"] \
        == 30_000
    assert report["method"].startswith("FILE-control")
    with pytest.raises(ValueError, match="FILE-control"):
        probe.summarize([{**run, "control": None}])


def _hook_row(i, **over):
    row = {"i": i, "rc": 0, "start": 100.0 * i, "end": 100.0 * i + 2,
           "wallS": 2.0, "appended": {"bytes": 2048, "events": 4,
                                      "size": 9000},
           "processes": [{"pid": 7, "temp": 0, "other": 4096, "dropped": 0,
                          "framesDropped": 0, "footprintPeak": 50 * MiB}],
           "workers": [], "ingested": True, "ingestProblems": [],
           "attributionComplete": True, "attributionProblems": [],
           "tempBytes": 0, "footprintPeakBytes": 50 * MiB,
           # Revision 11 (dc9 G4): consumed-range evidence and the hook's own
           # lifecycle line ride every receipt.
           "consumedBytes": 2048, "consumedOtherBytes": 0,
           "hookLog": [_lifecycle(backlog=0)]}
    row.update(over)
    return row


def _lifecycle(*, backlog, result="success", sync="ok", dur_ms=6685):
    return (f"2026-10-04T10:00:00Z provider=codex source_root_key=root "
            f"event=Stop sync={sync} blocks=0 milestones=0 "
            f"alert_eligible_roots=1 quota_alerts=0 budget_alerts=0 "
            f"backlog={backlog} dur_ms={dur_ms} result={result}")


_UNSET = object()
#: The source data dir's config.json D's clones copy: no `codex` block, so
#: the hook's ingest budget is the product default (Q22).
_SOURCE_CONFIG = {"display": {"tz": "Etc/UTC"}, "update": {"channel": "beta"}}


def _git(repo, *args) -> str:
    import subprocess
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
         "-c", "commit.gpgsign=false", *args], check=True, env=env,
        capture_output=True, text=True).stdout.strip()


def _d_run(tmp_path, rows, *, markers=True, tree=None, rev=_UNSET,
           config=_UNSET):
    """A D run directory. With `tree`, catchup.json names it, its commit
    (`rev`: default the tree's HEAD, None to omit) and the source root whose
    data/config.json (`config`: a dict, raw text, or None for no file) the
    clone copied."""
    run = tmp_path / "d"
    run.mkdir(parents=True)
    _live_inputs(run)
    (run / "admission.json").write_text(json.dumps({"valid": True}))
    catchup = {"admitted": True, "frontier": _FRONTIER}
    if tree is not None:
        catchup["tree"] = str(tree)
        if rev is _UNSET:
            rev = _git(tree, "rev-parse", "HEAD")
        source = tmp_path / "src"
        (source / "data").mkdir(parents=True)
        if config is _UNSET:
            config = _SOURCE_CONFIG
        if config is not None:
            (source / "data" / "config.json").write_text(
                config if isinstance(config, str) else json.dumps(config))
        catchup["identity"] = {"sourceRoot": str(source)}
        if rev is not None:
            catchup["gitRev"] = rev
            catchup["identity"]["gitRev"] = rev
    (run / "catchup.json").write_text(json.dumps(catchup))
    (run / "hooks.jsonl").write_text("".join(json.dumps(r) + "\n"
                                             for r in rows))
    (run / "hooks-summary.json").write_text(json.dumps(
        {"markersProven": markers,
         "markerProblems": [] if markers else ["cache cursor 9000 short"]}))
    return str(run)


def test_d_requires_ingesting_hooks_with_complete_attribution(tmp_path, capsys):
    workload = _load("workload")
    good = [_hook_row(i) for i in range(1, 21)]
    assert workload.d_verdict(_d_run(tmp_path / "a", good)) == 0
    idle = good[:5] + [_hook_row(6, ingested=False, ingestProblems=[
        "cache cursor 100 short of the appended end 9000"])] + good[6:]
    assert workload.d_verdict(_d_run(tmp_path / "b", idle)) == 2
    assert "hook 6 ingested nothing" in capsys.readouterr().out
    loose = good[:19] + [_hook_row(20, attributionComplete=False,
                                   attributionProblems=["mismatch"])]
    assert workload.d_verdict(_d_run(tmp_path / "c", loose)) == 2
    hot = good[:19] + [_hook_row(20, tempBytes=4096)]
    assert workload.d_verdict(_d_run(tmp_path / "e", hot)) == 1
    short = good[:19]
    assert workload.d_verdict(_d_run(tmp_path / "f", short)) == 2


def test_the_hook_runner_proves_ingest_and_reconciles_attribution():
    runner = _load("hook_runner")
    before = {"cacheCursor": 100, "tokenRows": 5, "quotaRows": 2}
    appended = {"size": 9000, "events": 4}
    assert runner.proof_of_ingest(
        before, {"cacheCursor": 9000, "tokenRows": 9, "quotaRows": 3},
        appended) == []
    problems = runner.proof_of_ingest(
        before, {"cacheCursor": 100, "tokenRows": 5, "quotaRows": 2}, appended)
    assert len(problems) == 3
    sqlattr = {"attributed": {"temp": 100}, "unattributed": {"temp": 20}}
    assert runner.reconcile(sqlattr, {"temp": 120}) == []
    assert runner.reconcile(sqlattr, {"temp": 121})
    assert runner.reconcile(None, {"temp": 0})


def test_the_hook_runners_appended_records_ingest_token_and_quota_rows(
        tmp_path):
    """The proof-of-ingest records are the product's own Codex shape: a real
    `cache-sync --source codex` consumes them through the appended end and
    writes their token AND quota rows."""
    import subprocess as _sp
    import sys as _sys

    workload = _load("workload")
    runner = _load("hook_runner")
    root = tmp_path / "clone"
    (root / "data").mkdir(parents=True)
    workload.seed_scratch(root)
    env = {**os.environ, "HOME": str(tmp_path),
           "CCTALLY_DATA_DIR": str(root / "data"),
           "CLAUDE_CONFIG_DIR": str(root / "scratch" / "claude"),
           "CODEX_HOME": str(root / "scratch" / "codex"),
           "CCTALLY_DISABLE_DEV_AUTODETECT": "1"}
    cctally = str(TOOLS.parent.parent / "bin" / "cctally")

    def sync():
        out = _sp.run([_sys.executable, cctally, "cache-sync", "--source",
                       "codex"], env=env, capture_output=True, text=True,
                      timeout=120)
        assert out.returncode == 0, out.stderr

    sync()
    rollout, _total = workload._codex_target(root, scratch=True)
    data = root / "data"
    before = runner.observe(data, str(rollout))
    appended = runner.append_marked(rollout, "selftest")
    sync()
    after = runner.observe(data, str(rollout))
    assert runner.proof_of_ingest(before, after, appended) == [], (
        before, after, appended)


def test_the_sync_pass_latency_harness_times_each_population(tmp_path):
    import subprocess as _sp
    import sys as _sys

    workload = _load("workload")
    root = tmp_path / "clone"
    (root / "data").mkdir(parents=True)
    workload.seed_scratch(root)
    env = {**os.environ, "HOME": str(tmp_path),
           "CCTALLY_DATA_DIR": str(root / "data"),
           "CLAUDE_CONFIG_DIR": str(root / "scratch" / "claude"),
           "CODEX_HOME": str(root / "scratch" / "codex"),
           "CCTALLY_DISABLE_DEV_AUTODETECT": "1"}
    out = tmp_path / "latency.json"
    child = _sp.run([_sys.executable, str(TOOLS / "latency.py"), "--tree",
                     str(TOOLS.parent.parent), "--out", str(out),
                     "--sync-passes", "2", "--root", str(root)],
                    env=env, capture_output=True, text=True, timeout=90)
    assert child.returncode == 0, child.stderr
    report = json.loads(out.read_text())["syncPasses"]
    for kind in ("conversationSyncClaude", "conversationSyncCodex",
                 "claudeCacheSync", "codexCacheSync", "statsIngest"):
        assert report[kind]["catchUp"]["n"] >= 1, kind
        assert report[kind]["noChange"]["n"] >= 2, kind
        assert report[kind]["smallDelta"]["n"] >= 2, kind
        assert report[kind]["smallDelta"]["p95S"] is not None
    latency = _load("latency")
    assert latency.percentile([3, 1, 2, 4], 0.5) == 2
    assert latency.percentile([3, 1, 2, 4], 0.95) == 4


def test_terminal_footprints_prefer_the_exit_snapshot_and_list_workers(tmp_path):
    workload = _load("workload")
    run = _perf_run(tmp_path)
    (run / "wtrace.42.exit").write_text(json.dumps(
        {"t": 600, "pid": 42, "dropped": 0, "footprintPeak": 77 * MiB,
         "paths": {}}) + "\n")
    (run / "wtrace.43.exit").write_text(json.dumps(
        {"t": 600, "pid": 43, "dropped": 0, "footprintPeak": 5 * MiB,
         "paths": {}}) + "\n")
    (run / "catchup.json").write_text(json.dumps(
        {"footprintPeakBytes": 300 * MiB,
         "setupMigrations": {"cache.db": ["048_x"]}}))
    out = workload.perf_evidence(str(run))
    assert out["terminalFootprintPeakBytes"] == 77 * MiB
    assert out["workerFootprintPeakBytes"] == {"43": 5 * MiB}
    assert out["catchUp"]["label"] == "setup"
    assert out["catchUp"]["setupMigrations"] == {"cache.db": ["048_x"]}
    (run / "wtrace.42.exit").unlink()
    (run / "rusage.jsonl").write_text("")
    invalid = workload.perf_evidence(str(run))
    assert invalid["valid"] is False
    assert "terminal lifetime peak" in " ".join(invalid["problems"])



def test_a_baseline_run_is_judged_on_the_whole_run_without_receipts(tmp_path):
    """The C-op RED proof on 56e66f07a: shipped statements carry no charge
    or geometry, so the run is judged on its temp bytes and WAL ratio and
    FAILS there (122 MB of temp, WAL 5.2x the copy), never INVALID."""
    analyze = _load("analyze_op")
    run = _run_dir(tmp_path, ops=[{"kind": "delete", "outcome": "ok",
                                   "rows": 27_395, "charged_bytes": 0,
                                   "began": 10.0, "ended": 20.0,
                                   "mode": "shipped"}], charged=0,
                   paths={"/x/conversations.db": [124 * MiB, 9],
                          "/x/conversations.db-wal": [651 * MiB, 9],
                          "/t/etilqs_1": [122 * MiB, 9]})
    report = json.loads((tmp_path / "op.json").read_text())
    report["baseline"] = True
    (tmp_path / "op.json").write_text(json.dumps(report))
    result = analyze.analyze(run, "c-op")
    assert result["pass"] is False and result["receipts"] == []
    assert any("temp bytes" in p for p in result["problems"])


# ── #901 revision 11 (Q12): P, D's deferral rule, idle A, segmented R-cov ──


def _p1_pass(case, rep, **over):
    record = {"case": case, "rep": rep, "rowsDeleted": 0, "rowsInserted": 0,
              "rowsUpdated": 0, "rowsChanged": 0, "generationBefore": 10,
              "generationAfter": 10, "walBytes": 0, "checkpointBytes": 0,
              "tempBytes": 0, "wallS": 0.2, "cpuS": 0.15, "constructionS": 0.1,
              "comparisonS": 0.01, "commitS": 0.005, "flockHoldS": 0.21,
              "footprintPeakBytes": 90 * MiB, "filesProcessed": 0,
              "ambient": False}
    if case in ("append", "late-anchor"):
        record.update(rowsInserted=4, rowsChanged=4, generationAfter=11,
                      walBytes=60_000, checkpointBytes=40_000, filesProcessed=1)
    if case == "late-anchor":
        record["equalsRebuild"] = True
    record.update(over)
    return record


def _p1_receipt(role, source, *, reps=5, late=True, **per_case):
    passes = []
    for rep in range(1, reps + 1):
        for case in ("forced", "no-append", "append") + (
                ("late-anchor",) if late else ()):
            passes.append(_p1_pass(case, rep, **per_case.get(case, {})))
    return {"schema": "p1/1", "role": role, "sourceConversation": source,
            "treeRev": "a" * 40 if role == "candidate" else "b" * 40,
            "conversation": f"scratch-{source}", "messages": 600,
            "projectionRows": 580, "lateAnchor": late, "reps": reps,
            "passes": passes}


def _control(source, rows):
    """The candidate before revision 11: every pass rewrites the whole
    conversation and advances the generation."""
    full = {"rowsDeleted": rows, "rowsInserted": rows, "rowsChanged": 2 * rows,
            "generationAfter": 11}
    return _p1_receipt("control", source, late=False, forced={
        **full, "walBytes": rows * 5000, "checkpointBytes": rows * 4000},
        append={**full, "rowsInserted": rows + 4, "rowsChanged": 2 * rows + 4,
                "walBytes": rows * 5000, "checkpointBytes": rows * 4000})


def _p1_set(**candidate_over):
    return [_p1_receipt("candidate", "mid", **candidate_over.get("mid", {})),
            _p1_receipt("candidate", "large", **candidate_over.get("large", {})),
            _control("mid", 580), _control("large", 9000)]


def test_p1_verdict_passes_a_differential_candidate_against_its_control():
    replay = _load("projection_replay")
    code, result = replay.p1_verdict(_p1_set())
    assert code == 0, result
    assert result["candidateTwoSize"]["ratio"] == 1.0
    assert result["controlTwoSize"]["sameRows"] is False


def test_p1_verdict_missing_field_makes_the_receipt_invalid():
    replay = _load("projection_replay")
    receipts = _p1_set()
    receipts[0]["passes"][0]["walBytes"] = None
    code, result = replay.p1_verdict(receipts)
    assert code == 2
    assert any("missing walBytes" in p for p in result["invalid"])
    receipts = _p1_set()
    del receipts[1]["passes"][3]["equalsRebuild"]
    code, result = replay.p1_verdict(receipts)
    assert code == 2 and any("equalsRebuild" in p for p in result["invalid"])


def test_p1_verdict_is_invalid_when_the_control_passes_the_forced_reprojection():
    replay = _load("projection_replay")
    receipts = _p1_set()
    for receipt in receipts[2:]:
        for record in receipt["passes"]:
            if record["case"] == "forced":
                record.update(rowsDeleted=0, rowsInserted=0, rowsChanged=0,
                              generationAfter=record["generationBefore"])
    code, result = replay.p1_verdict(receipts)
    assert code == 2
    assert "the control passed the forced re-projection" in result["invalid"]


def test_p1_verdict_fails_a_26_percent_two_size_difference():
    replay = _load("projection_replay")
    receipts = _p1_set(large={"append": {"walBytes": 66_000,
                                         "checkpointBytes": 60_000}})
    code, result = replay.p1_verdict(receipts)
    assert code == 1, result
    assert abs(result["candidateTwoSize"]["ratio"] - 1.26) < 1e-9
    receipts = _p1_set(large={"append": {"walBytes": 65_000,
                                         "checkpointBytes": 60_000}})
    assert replay.p1_verdict(receipts)[0] == 0          # exactly 1.25 passes


def test_p1_verdict_fails_unequal_rows_a_noop_write_and_a_rebuild_mismatch():
    replay = _load("projection_replay")
    rows = _p1_set(large={"append": {"rowsInserted": 5, "rowsChanged": 5}})
    assert replay.p1_verdict(rows)[0] == 1
    noop = _p1_set(mid={"forced": {"rowsUpdated": 1, "rowsChanged": 1}})
    assert replay.p1_verdict(noop)[0] == 1
    rebuild = _p1_set(mid={"late-anchor": {"equalsRebuild": False}})
    code, result = replay.p1_verdict(rebuild)
    assert code == 1 and any("rebuild" in p for p in result["problems"])
    ambient = _p1_set(mid={"no-append": {"ambient": True}})
    assert replay.p1_verdict(ambient)[0] == 2


def _rollout_line(at, kind, payload):
    return json.dumps({"timestamp": at, "type": kind, "payload": payload}) + "\n"


def _touch_window(directory):
    """Rollouts written after the window, whatever the runner's clock says."""
    at = dt.datetime(2026, 10, 3, 10, 30, tzinfo=dt.timezone.utc).timestamp()
    for path in directory.glob("*.jsonl"):
        os.utime(path, (at, at))


def test_p2_extract_keeps_the_window_and_truncates_at_its_start(tmp_path):
    replay = _load("projection_replay")
    root = tmp_path / "codex"
    day = root / "sessions" / "2026" / "10" / "03"
    day.mkdir(parents=True)
    old = (_rollout_line("2026-10-03T09:59:00Z", "session_meta", {"id": "a"})
           + _rollout_line("2026-10-03T09:59:59Z", "event_msg", {"n": 1}))
    inside = (_rollout_line("2026-10-03T10:00:30Z", "event_msg", {"n": 2})
              + "not json, keeps the previous time\n"
              + _rollout_line("2026-10-03T10:04:00Z", "event_msg", {"n": 3}))
    after = _rollout_line("2026-10-03T10:06:00Z", "event_msg", {"n": 4})
    (day / "rollout-a.jsonl").write_text(old + inside + after)
    (day / "rollout-b.jsonl").write_text(
        _rollout_line("2026-10-03T10:02:00Z", "session_meta", {"id": "b"})
        + '{"timestamp":"2026-10-03T10:02:30Z","partial":')
    (day / "rollout-c.jsonl").write_text(
        _rollout_line("2026-10-03T08:00:00Z", "session_meta", {"id": "c"}))
    _touch_window(day)
    out = tmp_path / "slice"
    manifest = replay.p2_extract(
        replay._parse_instant("2026-10-03T10:00:00Z"),
        replay._parse_instant("2026-10-03T10:05:00Z"), out, [root])
    by_name = {pathlib.Path(f["relative"]).name: f for f in manifest["files"]}
    assert set(by_name) == {"rollout-a.jsonl", "rollout-b.jsonl"}
    a, b = by_name["rollout-a.jsonl"], by_name["rollout-b.jsonl"]
    assert (out / "prefix" / f"{a['id']}.jsonl").read_text() == old
    assert a["prefixBytes"] == len(old.encode()) and not a["createdInWindow"]
    index = json.loads((out / "window" / f"{a['id']}.json").read_text())
    assert [row["dt"] for row in index] == [30.0, 30.0, 240.0]
    assert (out / "window" / f"{a['id']}.bin").read_text() == inside
    assert b["createdInWindow"] and b["lines"] == 1      # the partial line waits
    with pytest.raises(SystemExit):
        replay.p2_extract(0.0, 1.0, root / "slice", [root])


def test_p2_place_and_replay_rebuild_the_window_inside_the_clone(tmp_path):
    replay = _load("projection_replay")
    root = tmp_path / "codex"
    day = root / "sessions" / "2026" / "10" / "03"
    day.mkdir(parents=True)
    old = _rollout_line("2026-10-03T09:59:00Z", "session_meta", {"id": "a"})
    new = (_rollout_line("2026-10-03T10:00:01Z", "event_msg", {"n": 1})
           + _rollout_line("2026-10-03T10:00:03Z", "event_msg", {"n": 2}))
    (day / "rollout-a.jsonl").write_text(old + new)
    (day / "rollout-b.jsonl").write_text(
        _rollout_line("2026-10-03T10:00:02Z", "session_meta", {"id": "b"}))
    _touch_window(day)
    slice_dir = tmp_path / "slice"
    replay.p2_extract(replay._parse_instant("2026-10-03T10:00:00Z"),
                      replay._parse_instant("2026-10-03T10:05:00Z"),
                      slice_dir, [root])
    clone = tmp_path / "clone"
    clone.mkdir()
    assert replay.p2_place(slice_dir, clone) == {"placed": 1, "files": 2}
    placed = sorted((clone / "scratch").rglob("*.jsonl"))
    assert [p.read_text() for p in placed] == [old]
    out = replay.p2_replay(slice_dir, clone, 60.0, time_scale=100.0)
    assert out["lines"] == 3 and out["bytes"] == len((new + (
        day / "rollout-b.jsonl").read_text()).encode())
    replayed = {p.name: p.read_text()
                for p in (clone / "scratch").rglob("*.jsonl")}
    assert replayed["rollout-a.jsonl"] == old + new
    assert replayed["rollout-b.jsonl"] == (day / "rollout-b.jsonl").read_text()
    assert (root / "sessions" / "2026" / "10" / "03" / "rollout-a.jsonl"
            ).read_text() == old + new                # the source is untouched
    with pytest.raises(SystemExit):
        replay._inside(clone, tmp_path / "elsewhere.jsonl")


def _p1_records(turns):
    def at(seconds):
        return (dt.datetime(2026, 7, 20, tzinfo=dt.timezone.utc)
                + dt.timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
    records = [{"type": "session_meta", "timestamp": at(0), "payload": {
        "id": "p1-thread", "session_id": "90190190-1901-4901-8901-90190190p1aa",
        "cwd": "/synthetic/p1", "model": "gpt-5", "model_provider": "openai",
        "source": "codex", "thread_source": "p1-source"}}]
    for n in range(turns):
        t = 1 + 10 * n
        records += [
            {"type": "turn_context", "timestamp": at(t),
             "payload": {"model": "gpt-5", "turn_id": f"turn-{n}"}},
            {"type": "response_item", "timestamp": at(t + 1), "payload": {
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": f"prompt {n}"}]}},
            {"type": "response_item", "timestamp": at(t + 2), "payload": {
                "type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": f"answer {n}"}]}},
            {"type": "event_msg", "timestamp": at(t + 3), "payload": {
                "type": "token_count", "info": {
                    "last_token_usage": {"input_tokens": 10,
                                         "cached_input_tokens": 0,
                                         "output_tokens": 5,
                                         "reasoning_output_tokens": 0,
                                         "total_tokens": 15},
                    "total_token_usage": {"total_tokens": 15 * (n + 1)}}}},
        ]
    return records


def test_the_p1_runner_measures_a_synthetic_conversation_end_to_end(tmp_path):
    """The P1 harness on this tree, without the interposer: the forced
    re-projection and the no-append pass change nothing, the small append
    changes only its own rows, the late anchor equals the from-zero rebuild,
    and the receipt is INVALID only for the bytes the interposer would supply."""
    import subprocess as _sp
    import sys as _sys

    real = tmp_path / "real-codex"
    day = real / "sessions" / "2026" / "07" / "20"
    day.mkdir(parents=True)
    (day / "rollout-p1.jsonl").write_text("".join(
        json.dumps(r) + "\n" for r in _p1_records(6)))
    clone = tmp_path / "clone"
    (clone / "data").mkdir(parents=True)
    (clone / "data" / "config.json").write_text(
        '{"conversation":{"retention_days":0}}\n')
    env = {**os.environ, "HOME": str(tmp_path),
           "CCTALLY_DATA_DIR": str(clone / "data"), "CODEX_HOME": str(real),
           "CLAUDE_CONFIG_DIR": str(tmp_path / "no-claude"),
           "CCTALLY_DISABLE_DEV_AUTODETECT": "1"}
    cctally = str(TOOLS.parent.parent / "bin" / "cctally")
    sync = _sp.run([_sys.executable, cctally, "cache-sync", "--source", "codex"],
                   env=env, capture_output=True, text=True, timeout=30)
    assert sync.returncode == 0, sync.stderr
    conn = sqlite3.connect(f"file:{clone / 'data' / 'conversations.db'}?mode=ro",
                           uri=True)
    key = conn.execute("SELECT DISTINCT conversation_key FROM "
                       "codex_conversation_messages").fetchone()[0]
    conn.close()
    out = tmp_path / "p1.json"
    child = _sp.run([_sys.executable, str(TOOLS / "projection_replay.py"), "p1",
                     "--tree", str(TOOLS.parent.parent), "--db",
                     str(clone / "data"), "--conversation", key, "--reps", "1",
                     "--late-anchor", "--real-codex-home", str(real),
                     "--out", str(out)],
                    env=env, capture_output=True, text=True, timeout=75)
    assert child.returncode == 2, child.stderr       # no interposer bytes
    receipt = json.loads(out.read_text())
    assert receipt["conversation"] != key and receipt["messages"] >= 12
    cases = {r["case"]: r for r in receipt["passes"]}
    assert set(cases) == {"forced", "no-append", "append", "late-anchor"}
    forced = cases["forced"]
    assert forced["rowsChanged"] == 0
    assert forced["generationAfter"] == forced["generationBefore"]
    assert forced["flockHoldS"] is not None and forced["flockHoldS"] >= 0
    assert cases["no-append"]["rowsChanged"] == 0
    append = cases["append"]
    assert append["rowsDeleted"] == append["rowsUpdated"] == 0
    assert append["rowsInserted"] >= 3 and append["filesProcessed"] == 1
    assert append["generationAfter"] == append["generationBefore"] + 1
    late = cases["late-anchor"]
    assert late["equalsRebuild"] is True and late["rowsUpdated"] > 0, late
    assert receipt["rebuilds"][0]["equal"] is True
    for record in receipt["passes"]:
        assert record["ambient"] is False
        assert record["footprintPeakBytes"] and record["cpuS"] is not None
        assert record["constructionS"] is not None and record["commitS"] >= 0
    problems = _load("projection_replay")._receipt_problems(receipt, 1)
    assert problems and all("missing walBytes, checkpointBytes, tempBytes"
                            in p for p in problems), problems
    # The real root was only read.
    assert (day / "rollout-p1.jsonl").read_text() == "".join(
        json.dumps(r) + "\n" for r in _p1_records(6))


def test_d_admits_a_budgeted_deferral_and_classifies_each_invocation(
        tmp_path, capsys):
    workload = _load("workload")
    deferred = _hook_row(5, ingested=False, consumedBytes=50_000,
                         consumedOtherBytes=50_000, hookLog=[
                             _lifecycle(backlog=0), _lifecycle(backlog=4)],
                         ingestProblems=["cache cursor short"])
    rows = [_hook_row(i) for i in range(1, 5)] + [deferred] + [
        _hook_row(i) for i in range(6, 22)]
    assert workload.d_classify(deferred) == "deferred"
    assert workload.d_verdict(_d_run(tmp_path / "a", rows)) == 0
    out = json.loads(capsys.readouterr().out.rsplit("\n", 2)[0])
    assert out["starvation"] == {"deferred": 1, "invocations": 21,
                                 "share": 1 / 21, "ingested": 20}
    assert [p["class"] for p in out["invocations"]][4] == "deferred"
    # A positive backlog with nothing else consumed is not a deferral.
    starved = dict(deferred, consumedOtherBytes=0, consumedBytes=0)
    assert workload.d_classify(starved) == "error"
    rows_b = rows[:4] + [starved] + rows[5:]
    assert workload.d_verdict(_d_run(tmp_path / "b", rows_b)) == 2
    assert "hook 5 ingested nothing" in capsys.readouterr().out
    # A failed lifecycle result is not a deferral either.
    failed = dict(deferred, hookLog=[_lifecycle(backlog=4, result="error")])
    assert workload.d_classify(failed) == "error"


def test_d_is_invalid_above_20_percent_deferred_or_25_invocations(
        tmp_path, capsys):
    workload = _load("workload")

    def deferral(i):
        return _hook_row(i, ingested=False, consumedOtherBytes=4096,
                         consumedBytes=4096,
                         hookLog=[_lifecycle(backlog=3)])
    rows = [_hook_row(i) for i in range(1, 20)] + [
        deferral(i) for i in range(20, 25)]
    assert workload.d_verdict(_d_run(tmp_path / "a", rows)) == 2
    text = capsys.readouterr().out
    assert "5 of 24 invocations deferred (21% > 20%)" in text
    assert "19 invocations ingested their own append (need 20)" in text
    many = [_hook_row(i) for i in range(1, 27)]
    assert workload.d_verdict(_d_run(tmp_path / "b", many)) == 2
    assert "26 invocations (at most 25)" in capsys.readouterr().out


def test_d_fails_an_envelope_breach_even_in_an_incomplete_run(tmp_path, capsys):
    workload = _load("workload")
    limit = 4 * MiB + 8 * 2048
    over = _hook_row(7, processes=[{"pid": 7, "temp": 0, "other": limit + 1,
                                    "dropped": 0, "framesDropped": 0,
                                    "footprintPeak": 50 * MiB}])
    rows = [_hook_row(i) for i in range(1, 7)] + [over] + [
        _hook_row(i) for i in range(8, 21)]
    assert workload.d_envelope(over) == (limit + 1, limit)
    assert workload.d_verdict(_d_run(tmp_path / "a", rows)) == 1
    assert "hook 7 (ingested) wrote" in capsys.readouterr().out
    at_limit = dict(over, processes=[dict(over["processes"][0], other=limit)])
    rows[6] = at_limit
    assert workload.d_verdict(_d_run(tmp_path / "b", rows)) == 0
    capsys.readouterr()
    rows[6] = over
    assert workload.d_verdict(_d_run(tmp_path / "c", rows[:10])) == 1
    out = capsys.readouterr().out
    assert "need 20" in out                       # still reported as incomplete


def test_d_requires_every_appended_marker_proven(tmp_path, capsys):
    workload = _load("workload")
    rows = [_hook_row(i) for i in range(1, 21)]
    assert workload.d_verdict(_d_run(tmp_path / "a", rows, markers=False)) == 2
    assert "not every appended marker is proven ingested" in capsys.readouterr().out
    missing = rows[:3] + [_hook_row(4, consumedBytes=None)] + rows[4:]
    assert workload.d_verdict(_d_run(tmp_path / "b", missing)) == 2
    assert "no consumed-range evidence" in capsys.readouterr().out


def test_the_hook_runner_sums_consumed_bytes_across_roots_and_proves_markers():
    runner = _load("hook_runner")
    before = {"r1\x00/real/a.jsonl": 100, "r2\x00/scratch/s.jsonl": 500}
    after = {"r1\x00/real/a.jsonl": 400, "r2\x00/scratch/s.jsonl": 900,
             "r1\x00/real/new.jsonl": 50}
    out = runner.consumed(before, after, "/scratch/s.jsonl")
    assert out["consumedBytes"] == 300 + 400 + 50
    assert out["consumedOtherBytes"] == 350
    assert runner.consumed(None, after, "/x")["consumedBytes"] is None
    first = {"cacheCursor": 100, "tokenRows": 5, "quotaRows": 2}
    appends = [{"size": 5000, "events": 4}, {"size": 9000, "events": 4}]
    assert runner.marker_proof(
        first, {"cacheCursor": 9000, "tokenRows": 13, "quotaRows": 4},
        appends) == []
    assert len(runner.marker_proof(
        first, {"cacheCursor": 5000, "tokenRows": 9, "quotaRows": 3},
        appends)) == 3
    workload = _load("workload")
    assert workload.d_lifecycle([_lifecycle(backlog=4)]) == [
        {"sync": "ok", "backlog": 4, "result": "success"}]


# Spec §6.3 D, before-walk budget deferral (Q22): a hook whose ingest budget,
# measured from function entry, expired before the walk opened any file.

def _budget_tree(path, seconds=5.0, *, text=None):
    """A git tree under test whose commit carries only the product constant
    D's runner leaves in force (the runner records no budget; D's clones
    configure none). `text` replaces the whole committed config module."""
    (path / "bin").mkdir(parents=True)
    (path / "bin" / "_cctally_config.py").write_text(
        text if text is not None else
        "#: Wall-clock budget the native Codex hook gives its ingest leg.\n"
        f"CODEX_HOOK_INGEST_BUDGET_DEFAULT_SECONDS = {seconds}\n"
        "CODEX_HOOK_INGEST_BUDGET_MAX_SECONDS = 20.0\n")
    _git(path, "init", "-q")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "tree under test")
    return path


def _q22_chain(n, *, deferred=(), size0=748, nbytes=3428, events=4,
               quota_per_append=8, wall=7.94):
    """Coherent D receipts over one scratch rollout: every hook appends
    `nbytes` (`events` token counts carrying rate limits). A hook in
    `deferred` exits 0 with `backlog=1 result=success` on both due roots,
    consumes nothing and runs `wall` seconds; every other hook consumes the
    whole pending range, its own append and any deferred one before it."""
    rows = []
    cursor, tokens, quota, size, pending = size0, 1, 0, size0, 0
    for i in range(1, n + 1):
        size += nbytes
        pending += 1
        before = {"cacheCursor": cursor, "tokenRows": tokens,
                  "quotaRows": quota}
        appended = {"bytes": nbytes, "events": events, "size": size}
        if i in deferred:
            after = dict(before)
            row = _hook_row(
                i, end=100.0 * i + wall, wallS=wall, ingested=False,
                ingestProblems=[f"cache cursor {cursor} short of the "
                                f"appended end {size}"],
                consumedBytes=0, consumedOtherBytes=0, advances=[],
                hookLog=[_lifecycle(backlog=1), _lifecycle(backlog=1)])
        else:
            after = {"cacheCursor": size,
                     "tokenRows": tokens + events * pending,
                     "quotaRows": quota + quota_per_append * pending}
            row = _hook_row(i, consumedBytes=size - cursor,
                            consumedOtherBytes=0)
            pending = 0
        row.update(appended=appended, before=before, after=after)
        cursor, tokens, quota = (after["cacheCursor"], after["tokenRows"],
                                 after["quotaRows"])
        rows.append(row)
    return rows


def test_d_admits_a_before_walk_budget_deferral_proven_by_its_successor(
        tmp_path, capsys):
    workload = _load("workload")
    rows = _q22_chain(21, deferred={3})
    deferral, successor = rows[2], rows[3]
    assert successor["consumedBytes"] == 2 * 3428
    assert workload.d_classify(deferral, successor, 5.0) == "deferred"
    judged = workload.d_classify_all(rows, 5.0)
    assert judged[3]["class"] == "deferred" and judged[3]["rule"] == "Q22"
    assert judged[3]["problems"] == []
    assert judged[4] == {"class": "ingested", "rule": "own-append",
                         "problems": []}
    # Without its successor (or its budget) the hook proves nothing.
    assert workload.d_classify(deferral) == "error"
    assert workload.d_classify(deferral, successor) == "error"
    tree = _budget_tree(tmp_path / "tree")
    assert workload.d_verdict(_d_run(tmp_path / "a", rows, tree=tree)) == 0
    out = json.loads(capsys.readouterr().out.rsplit("\n", 2)[0])
    assert out["valid"] is True
    assert out["starvation"] == {"deferred": 1, "invocations": 21,
                                 "share": 1 / 21, "ingested": 20}
    assert out["deferredByRule"] == {"Q12": 0, "Q22": 1}
    third = out["invocations"][2]
    assert (third["class"], third["rule"]) == ("deferred", "Q22")
    assert out["invocations"][3]["rule"] == "own-append"
    assert out["ingestBudget"]["seconds"] == 5.0
    assert "CODEX_HOOK_INGEST_BUDGET_DEFAULT_SECONDS" in \
        out["ingestBudget"]["basis"]
    assert "not recorded" in out["ingestBudget"]["basis"]


def test_d_rejects_a_before_walk_deferral_without_its_complete_proof(
        tmp_path, capsys):
    workload = _load("workload")
    rows = _q22_chain(6, deferred={3})
    d, nxt = rows[2], rows[3]
    assert workload.d_classify(d, nxt, 5.0) == "deferred"
    cases = {
        "a: under budget": (dict(d, end=d["start"] + 4.99, wallS=4.99), nxt),
        "b: no positive backlog": (
            dict(d, hookLog=[_lifecycle(backlog=0), _lifecycle(backlog=0)]),
            nxt),
        "b: no lifecycle line": (dict(d, hookLog=[]), nxt),
        "c: an unsuccessful lifecycle line": (
            dict(d, hookLog=[_lifecycle(backlog=1),
                             _lifecycle(backlog=1, result="error")]), nxt),
        "d: nonzero exit": (dict(d, rc=1), nxt),
        "d: killed": (dict(d, rc=None), nxt),
        "e: consumed part of its own range": (
            dict(d, consumedBytes=1000, consumedOtherBytes=0), nxt),
        "e: consumed other data, uncounted": (
            dict(d, consumedBytes=1000, consumedOtherBytes=None), nxt),
        "f: the next hook stops short": (
            d, dict(nxt, after=dict(nxt["after"],
                                    cacheCursor=d["appended"]["size"] - 1))),
        "f: the next hook started past the range": (
            d, dict(nxt, before=dict(nxt["before"],
                                     cacheCursor=d["appended"]["size"]))),
        "g: a token row short": (
            d, dict(nxt, after=dict(nxt["after"],
                                    tokenRows=nxt["after"]["tokenRows"] - 1))),
        "g: one quota row for two appends": (
            d, dict(nxt, after=dict(nxt["after"],
                                    quotaRows=nxt["before"]["quotaRows"] + 1))),
        "h: no successor": (d, None),
        "h: not the immediate successor": (d, dict(nxt, i=5)),
    }
    for field in ("rc", "start", "end", "consumedBytes", "consumedOtherBytes",
                  "hookLog", "appended"):
        cases[f"h: missing {field}"] = (
            {k: v for k, v in d.items() if k != field}, nxt)
    for field in ("before", "after", "appended"):
        cases[f"h: successor missing {field}"] = (
            d, {k: v for k, v in nxt.items() if k != field})
    for field in ("cacheCursor", "tokenRows", "quotaRows"):
        cases[f"h: successor after.{field} unread"] = (
            d, dict(nxt, after=dict(nxt["after"], **{field: None})))
    cases["h: appended size missing"] = (
        dict(d, appended={"bytes": 3428, "events": 4}), nxt)
    for name, (row, successor) in cases.items():
        assert workload.d_classify(row, successor, 5.0) == "error", name
    assert workload.d_classify(d, nxt, None) == "error"
    # (f) Only a later invocation than the next one consumed the append:
    # hook 3's range waits for hook 5, while hook 4 (itself a before-walk
    # deferral that hook 5 proves) is admitted.
    late = _q22_chain(6, deferred={3, 4})
    judged = workload.d_classify_all(late, 5.0)
    assert judged[3]["class"] == "error"
    assert any("next invocation" in p for p in judged[3]["problems"])
    assert judged[4]["class"] == "deferred" and judged[4]["rule"] == "Q22"
    tree = _budget_tree(tmp_path / "tree")
    rows_l = _q22_chain(21, deferred={3, 4})
    assert workload.d_verdict(_d_run(tmp_path / "l", rows_l, tree=tree)) == 2
    text = capsys.readouterr().out
    assert "hook 3 ingested nothing and is not an admitted deferral" in text
    assert "before-walk deferral (Q22) not proven" in text
    # Without a readable budget no before-walk deferral is admitted.
    rows_n = _q22_chain(21, deferred={3})
    assert workload.d_verdict(_d_run(tmp_path / "n", rows_n)) == 2
    out = capsys.readouterr().out
    assert "hook 3 ingested nothing" in out
    assert json.loads(out.rsplit("\n", 2)[0])["ingestBudget"]["seconds"] is None


def test_d_judges_a_before_walk_deferral_identically_on_both_trees(
        tmp_path, capsys):
    workload = _load("workload")
    rows = _q22_chain(22, deferred={3, 20})
    verdicts = []
    for label in ("base", "cand"):
        tree = _budget_tree(tmp_path / f"tree-{label}")
        code = workload.d_verdict(_d_run(tmp_path / label, rows, tree=tree))
        out = json.loads(capsys.readouterr().out.rsplit("\n", 2)[0])
        verdicts.append((code, [(p["i"], p["class"], p["rule"])
                                for p in out["invocations"]],
                         out["starvation"], out["deferredByRule"]))
    assert verdicts[0] == verdicts[1]
    assert verdicts[0][0] == 0
    assert verdicts[0][3] == {"Q12": 0, "Q22": 2}


def test_d_counts_before_walk_deferrals_toward_the_limits_and_the_mean(
        tmp_path, capsys):
    workload = _load("workload")
    import statistics
    tree = _budget_tree(tmp_path / "tree")
    rows = _q22_chain(24, deferred={2, 6, 10, 14, 18})
    assert workload.d_verdict(_d_run(tmp_path / "a", rows, tree=tree)) == 2
    text = capsys.readouterr().out
    assert "5 of 24 invocations deferred (21% > 20%)" in text
    assert "19 invocations ingested their own append (need 20)" in text
    rows = _q22_chain(26, deferred={3})
    assert workload.d_verdict(_d_run(tmp_path / "b", rows, tree=tree)) == 2
    assert "26 invocations (at most 25)" in capsys.readouterr().out
    rows = _q22_chain(21, deferred={3})
    assert workload.d_verdict(_d_run(tmp_path / "c", rows, tree=tree)) == 0
    out = json.loads(capsys.readouterr().out.rsplit("\n", 2)[0])
    assert out["meanWallS"] == statistics.mean(r["wallS"] for r in rows)
    assert out["meanWallS"] > 2.0          # the 7.94 s deferral is included


def _retained(i, *, start, wall, rc=0, consumed, appended, before, after,
              lines):
    """A faithful copy of a retained hooks.jsonl receipt's judged fields."""
    row = _hook_row(i, rc=rc, start=start, end=start + wall, wallS=wall,
                    consumedBytes=consumed, consumedOtherBytes=0,
                    appended=appended, hookLog=lines,
                    before=dict(zip(("cacheCursor", "tokenRows", "quotaRows"),
                                    before)),
                    after=dict(zip(("cacheCursor", "tokenRows", "quotaRows"),
                                   after)))
    if after[0] < appended["size"]:
        row.update(ingested=False, ingestProblems=[
            f"cache cursor {after[0]} short of the appended end "
            f"{appended['size']}"])
    return row


def _retained_line(root, backlog, dur_ms, blocks):
    return (f"2026-10-07T09:49:06Z provider=codex source_root_key={root} "
            f"event=Stop sync=ok blocks={blocks} milestones="
            f"{3 * blocks} alert_eligible_roots=2 quota_alerts=0 "
            f"budget_alerts=0 backlog={backlog} dur_ms={dur_ms} "
            "result=success")


def _retained_pair(i, *, wall, dur_ms, size, cursor, tokens, quota,
                   nbytes=3428):
    """Hook i (zero-progress, both roots `backlog=1 result=success`) and
    hook i+1 (consumed both appends), as the retained runs record them."""
    roots = ("af80e3c29629cbdc103a8c78c8fd8df3",
             "d9afad72218ba9725b7bd579d945b366")
    stalled = _retained(
        i, start=1000.0, wall=wall, consumed=0,
        appended={"bytes": nbytes, "events": 4, "size": size},
        before=(cursor, tokens, quota), after=(cursor, tokens, quota),
        lines=[_retained_line(r, 1, dur_ms, 0) for r in roots])
    successor = _retained(
        i + 1, start=1045.0, wall=2.25, consumed=2 * nbytes,
        appended={"bytes": nbytes, "events": 4, "size": size + nbytes},
        before=(cursor, tokens, quota),
        after=(size + nbytes, tokens + 8, quota + 16),
        lines=[_retained_line(r, 0, 1411, 2) for r in roots])
    return stalled, successor


@pytest.mark.parametrize("name, pair", [
    # run-x4-D-p3-cand (7379c674a) hooks 3/4 and 20/21.
    ("x4-p3-cand hook 3", dict(i=3, wall=8.07, dur_ms=7145, size=11029,
                               cursor=7601, tokens=9, quota=16)),
    ("x4-p3-cand hook 20", dict(i=20, wall=7.94, dur_ms=7053, size=69396,
                                cursor=65960, tokens=77, quota=152,
                                nbytes=3436)),
    # run-x4-D-p3-base and run-x4-D-p1-base (56e66f07a) hooks 19 and 7.
    ("x4-p3-base hook 19", dict(i=19, wall=7.94, dur_ms=7574, size=65960,
                                cursor=62524, tokens=73, quota=144,
                                nbytes=3436)),
    ("x4-p1-base hook 7", dict(i=7, wall=7.69, dur_ms=7332, size=24741,
                               cursor=21313, tokens=25, quota=48)),
    # run-x-D-p3-cand (3ba2508b6) hook 5, successor hook 6.
    ("x-p3-cand hook 5", dict(i=5, wall=7.86, dur_ms=6970, size=17885,
                              cursor=14457, tokens=17, quota=32)),
])
def test_d_admits_the_retained_before_walk_deferrals(tmp_path, name, pair):
    workload = _load("workload")
    stalled, successor = _retained_pair(**pair)
    assert stalled["ingested"] is False and successor["ingested"] is True
    assert workload.d_classify(stalled) == "error", name      # before Q22
    # The budget as d_verdict sources it: the constant at the run's commit,
    # with a source config that sets no codex block.
    run = _d_run(tmp_path, [stalled, successor],
                 tree=_budget_tree(tmp_path / "tree"))
    budget = workload.d_ingest_budget(run)["seconds"]
    assert budget == 5.0, name
    assert workload.d_classify(stalled, successor, budget) == "deferred", name
    judged = workload.d_classify_all([stalled, successor], budget)
    assert judged[pair["i"]]["rule"] == "Q22", name
    # The hook's own timer, not only the wall clock, must reach the budget:
    # a budget just above its dur_ms but below its wall time rejects it.
    above = pair["dur_ms"] / 1000 + 0.01
    assert above < pair["wall"], name
    assert workload.d_classify(stalled, successor, above) == "error", name
    # Lookalikes: the same receipt under a budget it did not reach, with the
    # successor's proof cut to one append, or one root reporting an error.
    assert workload.d_classify(stalled, successor, pair["wall"] + 0.01) \
        == "error", name
    short = dict(successor, after=dict(successor["after"],
                                       tokenRows=successor["after"]
                                       ["tokenRows"] - 4))
    assert workload.d_classify(stalled, short, 5.0) == "error", name
    failed = dict(stalled, hookLog=[stalled["hookLog"][0],
                                    stalled["hookLog"][1].replace(
                                        "result=success", "result=error")])
    assert workload.d_classify(failed, successor, 5.0) == "error", name


def test_d_ingest_budget_fails_closed_on_a_config_override(tmp_path, capsys):
    """The hook's budget is `resolve_codex_hook_ingest_budget(load_config())`:
    a source config that sets `codex.hook.ingest_budget_seconds` at all (even
    to the default) leaves no budget the harness will vouch for."""
    workload = _load("workload")
    tree = _budget_tree(tmp_path / "tree")
    rows = _q22_chain(21, deferred={3})
    plain = workload.d_ingest_budget(_d_run(tmp_path / "plain", rows,
                                            tree=tree))
    assert plain["seconds"] == 5.0
    config_path = str(tmp_path / "plain" / "src" / "data" / "config.json")
    assert config_path in plain["basis"]
    # A codex block without a hook block leaves the default in force.
    other = dict(_SOURCE_CONFIG, codex={"quota": {"alerts": True}})
    assert workload.d_ingest_budget(_d_run(
        tmp_path / "other", rows, tree=tree, config=other))["seconds"] == 5.0
    for label, value in (("same", 5.0), ("lower", 2.0), ("junk", "x")):
        config = dict(_SOURCE_CONFIG,
                      codex={"hook": {"ingest_budget_seconds": value}})
        run = _d_run(tmp_path / label, rows, tree=tree, config=config)
        budget = workload.d_ingest_budget(run)
        assert budget["seconds"] is None, label
        assert "codex.hook.ingest_budget_seconds" in budget["basis"], label
        assert str(tmp_path / label / "src" / "data" / "config.json") \
            in budget["basis"], label
        assert workload.d_verdict(run) == 2, label
        out = capsys.readouterr().out
        assert "hook 3 ingested nothing" in out, label
    # A codex or hook block of the wrong shape is not re-resolved either.
    for label, codex in (("codex-list", [1]), ("hook-str", {"hook": "5"})):
        run = _d_run(tmp_path / label, rows, tree=tree,
                     config=dict(_SOURCE_CONFIG, codex=codex))
        assert workload.d_ingest_budget(run)["seconds"] is None, label


def test_d_ingest_budget_fails_closed_without_a_readable_source_config(
        tmp_path):
    workload = _load("workload")
    tree = _budget_tree(tmp_path / "tree")
    rows = _q22_chain(21, deferred={3})
    cases = {"missing": None, "not json": "{\"display\": ", "array": "[1, 2]"}
    for label, config in cases.items():
        budget = workload.d_ingest_budget(
            _d_run(tmp_path / label, rows, tree=tree, config=config))
        assert budget["seconds"] is None, label
        assert "config.json" in budget["basis"], label
    # Unreadable: a directory where the file should be.
    run = _d_run(tmp_path / "dir", rows, tree=tree, config=None)
    (tmp_path / "dir" / "src" / "data" / "config.json").mkdir()
    assert workload.d_ingest_budget(run)["seconds"] is None
    # No source root recorded: the config cannot be located.
    run = _d_run(tmp_path / "nosrc", rows, tree=tree)
    receipt = json.loads((pathlib.Path(run) / "catchup.json").read_text())
    del receipt["identity"]["sourceRoot"]
    (pathlib.Path(run) / "catchup.json").write_text(json.dumps(receipt))
    budget = workload.d_ingest_budget(run)
    assert budget["seconds"] is None and "source" in budget["basis"]


def test_d_ingest_budget_requires_exactly_one_plain_constant(tmp_path):
    workload = _load("workload")
    rows = _q22_chain(21, deferred={3})
    name = "CODEX_HOOK_INGEST_BUDGET_DEFAULT_SECONDS"
    texts = {
        "missing": "CODEX_HOOK_INGEST_BUDGET_MAX_SECONDS = 20.0\n",
        "duplicated": f"{name} = 5.0\nX = 1\n{name} = 9.0\n",
        "duplicated, same value": f"{name} = 5.0\n{name} = 5.0\n",
        "annotated": f"{name}: float = 5.0\n",
        "trailing comment": f"{name} = 5.0  # seconds\n",
        "commented out": f"# {name} = 5.0\n",
        "commented out, then redefined": f"# {name} = 5.0\n{name} = 7.0\n"
                                         f"{name} += 1\n",
        "expression": f"{name} = 2.5 * 2\n",
    }
    for n, (label, text) in enumerate(texts.items()):
        tree = _budget_tree(tmp_path / f"tree{n}", text=text)
        budget = workload.d_ingest_budget(
            _d_run(tmp_path / f"run{n}", rows, tree=tree))
        assert budget["seconds"] is None, label
        assert name in budget["basis"], label
    # A comment that only mentions it does not count as a definition.
    tree = _budget_tree(tmp_path / "ok", text=f"#: {name} is the default.\n"
                                               f"{name} = 5.0\n")
    assert workload.d_ingest_budget(
        _d_run(tmp_path / "run-ok", rows, tree=tree))["seconds"] == 5.0


def test_d_ingest_budget_reads_the_constant_at_the_runs_commit(tmp_path):
    workload = _load("workload")
    rows = _q22_chain(21, deferred={3})
    tree = _budget_tree(tmp_path / "tree")
    rev = _git(tree, "rev-parse", "HEAD")
    config = tree / "bin" / "_cctally_config.py"
    # The tree moved on after the run: a new commit and an uncommitted edit.
    config.write_text("CODEX_HOOK_INGEST_BUDGET_DEFAULT_SECONDS = 9.0\n")
    _git(tree, "commit", "-q", "-am", "later")
    config.write_text("CODEX_HOOK_INGEST_BUDGET_DEFAULT_SECONDS = 12.0\n")
    budget = workload.d_ingest_budget(_d_run(tmp_path / "a", rows, tree=tree,
                                             rev=rev))
    assert budget["seconds"] == 5.0
    assert rev in budget["basis"]
    assert workload.d_ingest_budget(_d_run(
        tmp_path / "head", rows, tree=tree))["seconds"] == 9.0
    # No recorded commit, an unknown one, a malformed one, or no repository.
    cases = {"no rev": (tree, None), "unknown rev": (tree, "0" * 40),
             "option-like rev": (tree, "--output=x"),
             "not a repository": (tmp_path / "plain", rev)}
    (tmp_path / "plain" / "bin").mkdir(parents=True)
    (tmp_path / "plain" / "bin" / "_cctally_config.py").write_text(
        "CODEX_HOOK_INGEST_BUDGET_DEFAULT_SECONDS = 5.0\n")
    for n, (label, (where, value)) in enumerate(cases.items()):
        budget = workload.d_ingest_budget(
            _d_run(tmp_path / f"r{n}", rows, tree=where, rev=value))
        assert budget["seconds"] is None, label
    # A receipt whose two commit records disagree names no single commit.
    run = _d_run(tmp_path / "split", rows, tree=tree, rev=rev)
    receipt = json.loads((pathlib.Path(run) / "catchup.json").read_text())
    receipt["identity"]["gitRev"] = _git(tree, "rev-parse", "HEAD")
    (pathlib.Path(run) / "catchup.json").write_text(json.dumps(receipt))
    assert workload.d_ingest_budget(run)["seconds"] is None


def test_d_judges_the_same_receipts_by_each_trees_own_budget(tmp_path, capsys):
    workload = _load("workload")
    rows = _q22_chain(21, deferred={3})          # 7.94 s wall, dur_ms 6685
    results = {}
    for seconds in (5.0, 9.0):
        tree = _budget_tree(tmp_path / f"tree-{seconds:g}", seconds)
        code = workload.d_verdict(_d_run(tmp_path / f"{seconds:g}", rows,
                                         tree=tree))
        out = json.loads(capsys.readouterr().out.rsplit("\n", 2)[0])
        results[seconds] = (code, out["ingestBudget"]["seconds"],
                            out["invocations"][2]["class"])
    assert results[5.0] == (0, 5.0, "deferred")
    assert results[9.0] == (2, 9.0, "error")


def test_d_requires_the_hooks_own_timer_to_reach_the_budget():
    workload = _load("workload")
    rows = _q22_chain(6, deferred={3})
    d, nxt = rows[2], rows[3]
    assert d["end"] - d["start"] >= 5.0
    assert workload.d_classify(d, nxt, 5.0) == "deferred"
    # Wall time above the budget, the hook's own timer below it.
    fast = dict(d, hookLog=[_lifecycle(backlog=1, dur_ms=4999),
                            _lifecycle(backlog=1, dur_ms=4200)])
    problems = workload.d_before_walk_deferral(fast, nxt, 5.0)
    assert any("dur_ms 4999" in p for p in problems), problems
    assert workload.d_classify(fast, nxt, 5.0) == "error"
    # The largest line decides.
    mixed = dict(d, hookLog=[_lifecycle(backlog=1, dur_ms=4200),
                             _lifecycle(backlog=1, dur_ms=5000)])
    assert workload.d_before_walk_deferral(mixed, nxt, 5.0) == []
    # A lifecycle line without dur_ms proves no duration.
    bare = dict(d, hookLog=[_lifecycle(backlog=1),
                            _lifecycle(backlog=1).replace(" dur_ms=6685", "")])
    problems = workload.d_before_walk_deferral(bare, nxt, 5.0)
    assert any("dur_ms" in p for p in problems), problems
    junk = dict(d, hookLog=[_lifecycle(backlog=1, dur_ms="x")])
    assert workload.d_classify(junk, nxt, 5.0) == "error"
    # The other lifecycle outputs are unchanged.
    assert workload.d_lifecycle([_lifecycle(backlog=4)]) == [
        {"sync": "ok", "backlog": 4, "result": "success"}]


def test_d_admits_a_successor_that_consumed_only_the_deferred_append():
    """The next invocation consumed this deferral's range but not its own
    append: it is proven by this append's token rows and one quota row."""
    workload = _load("workload")
    rows = _q22_chain(6, deferred={3})
    d, nxt = rows[2], rows[3]
    first_end = d["appended"]["size"]
    assert nxt["appended"]["size"] > first_end
    partial = dict(nxt, after={
        "cacheCursor": first_end,
        "tokenRows": nxt["before"]["tokenRows"] + d["appended"]["events"],
        "quotaRows": nxt["before"]["quotaRows"] + 1})
    assert workload.d_before_walk_deferral(d, partial, 5.0) == []
    assert workload.d_judge(d, partial, 5.0) == {
        "class": "deferred", "rule": "Q22", "problems": []}
    short = dict(partial, after=dict(partial["after"],
                                     tokenRows=partial["after"]["tokenRows"]
                                     - 1))
    assert any("token rows (expected 4)" in p
               for p in workload.d_before_walk_deferral(d, short, 5.0))
    no_quota = dict(partial, after=dict(partial["after"],
                                        quotaRows=nxt["before"]["quotaRows"]))
    assert any("quota rows (expected at least 1)" in p
               for p in workload.d_before_walk_deferral(d, no_quota, 5.0))


def test_d_before_walk_deferral_reports_a_nonzero_exit():
    workload = _load("workload")
    rows = _q22_chain(6, deferred={3})
    d, nxt = rows[2], rows[3]
    assert workload.d_before_walk_deferral(d, nxt, 5.0) == []
    assert workload.d_before_walk_deferral(dict(d, rc=1), nxt, 5.0) == [
        "exit 1"]
    assert workload.d_before_walk_deferral(dict(d, rc=None), nxt, 5.0) == [
        "exit None"]


def _idle_run(tmp_path, *, workload_label="A", dispatches=None, gap=False,
              growth=0, admitted=True):
    run = tmp_path / "idle"
    run.mkdir(parents=True)
    _live_inputs(run)
    (run / "pid").write_text("42")
    window = {"traceStart": 0, "warm": 100, "start": 100, "end": 400}
    if workload_label is not None:
        window["workload"] = workload_label
    (run / "window.json").write_text(json.dumps(window))
    (run / "admission.json").write_text(json.dumps({"valid": True}))
    (run / "catchup.json").write_text(json.dumps({"admitted": admitted,
                                                  "frontier": _FRONTIER}))
    (run / "jsonl.jsonl").write_text(
        json.dumps({"t": 110, "claude": 1000, "codex": 2000}) + "\n"
        + json.dumps({"t": 395, "claude": 1000, "codex": 2000 + growth}) + "\n")
    (run / "rusage.jsonl").write_text(json.dumps(
        {"t": 390, "pid": 42, "rc": 0, "resident": 5 * MiB,
         "footprint_peak": 9 * MiB}) + "\n")
    # Amendment 19 HR-20: a predecessor published before the window and a
    # last record within one publish period (40 s) of its end.
    dispatches = dispatches or ["idle"] * 8
    seqs = [s for s in range(2, len(dispatches) + 3) if not (gap and s == 3)]
    stamp = lambda at: dt.datetime.fromtimestamp(at, dt.timezone.utc).isoformat()
    records = [{"seq": 1, "dispatch": "idle", "cold": False,
                "duration_ns": 3_000_000, "period_ns": 40_000_000_000,
                "published_at": stamp(60)}] + [
        {"seq": seq, "dispatch": dispatch, "cold": False,
         "duration_ns": 3_000_000, "period_ns": 40_000_000_000,
         "published_at": stamp(110 + 40 * i)}
        for i, (seq, dispatch) in enumerate(zip(seqs, dispatches))]
    (run / "perf-00000.json").write_text(json.dumps(
        {"diagnostic": {"tick": {"records": records}, "phases": None}}))
    return str(run)


def test_perf_evidence_reports_an_idle_drained_a_as_not_applicable(tmp_path):
    workload = _load("workload")
    out = workload.perf_evidence(_idle_run(tmp_path))
    assert out["valid"] is True, out["problems"]
    assert out["fullBuild"]["applicable"] is False and out["fullBuild"]["n"] == 0
    assert out["fullBuildP50Ms"] is None              # never zero latency
    assert out["publishPeriodP95Ms"] == 40000.0       # periods still measured


@pytest.mark.parametrize("kw, fragment", [
    ({"workload_label": "B"}, "workload B"),
    ({"dispatches": ["idle", "idle", "degraded", "idle"]}, "non-idle"),
    ({"gap": True}, "gaps"),
    ({"growth": 512}, "append-free"),
    ({"admitted": False}, "catch-up receipt"),
])
def test_anything_but_an_idle_drained_a_still_needs_a_full_build(
        tmp_path, kw, fragment):
    workload = _load("workload")
    out = workload.perf_evidence(_idle_run(tmp_path, **kw))
    assert out["valid"] is False
    assert "no warm full build in the window" in out["problems"]
    assert fragment in " ".join(out["idleA"]["reasons"])


def test_an_idle_a_still_requires_its_terminal_footprint(tmp_path):
    workload = _load("workload")
    run = _idle_run(tmp_path)
    pathlib.Path(run, "rusage.jsonl").write_text("")
    out = workload.perf_evidence(run)
    assert out["valid"] is False
    assert "terminal lifetime peak" in " ".join(out["problems"])


def _b_run(tmp_path, name, full_ms, *, label="B", rev=None):
    run = tmp_path / name
    run.mkdir(parents=True)
    _live_inputs(run)
    (run / "pid").write_text("42")
    # Amendment 19 HR-9: drained admission on both sides and the tree each
    # side measured (a run named base* is the 56e66f07a baseline).
    (run / "admission.json").write_text(json.dumps({"valid": True}))
    (run / "catchup.json").write_text(json.dumps(
        {"admitted": True, "frontier": _FRONTIER,
         "gitRev": rev or (_BASE_REV if name.startswith("base") else _CAND_REV)}))
    (run / "window.json").write_text(json.dumps(
        {"workload": label, "traceStart": 0, "warm": 100, "start": 100,
         "end": 400}))
    (run / "rusage.jsonl").write_text(json.dumps(
        {"t": 390, "pid": 42, "rc": 0, "resident": 5 * MiB,
         "footprint_peak": 9 * MiB}) + "\n")
    records = [{"seq": i + 1, "dispatch": "full", "cold": False,
                "duration_ns": int(ms * 1e6), "period_ns": 5_000_000_000,
                "published_at": dt.datetime.fromtimestamp(
                    120 + 20 * i, dt.timezone.utc).isoformat()}
               for i, ms in enumerate(full_ms)]
    (run / "perf-00000.json").write_text(json.dumps({"diagnostic": {
        "tick": {"records": records},
        "phases": {"name": "build", "elapsed_ms": 1.0, "children": []},
        "generated_at": dt.datetime.fromtimestamp(
            150, dt.timezone.utc).isoformat()}}))
    return str(run)


def test_b_pair_compares_b_against_b_with_sample_counts(tmp_path, capsys):
    workload = _load("workload")
    base = _b_run(tmp_path, "base", [1000, 2000, 3000, 4000])
    good = _b_run(tmp_path, "good", [1100, 2200, 3300, 4400])
    assert workload.b_pair(good, base) == 0
    out = json.loads(capsys.readouterr().out.rsplit("\n", 2)[0])
    assert out["candidate"]["n"] == 4 and out["baseline"]["n"] == 4
    assert abs(out["ratios"]["p95Ms"] - 1.1) < 1e-9
    slow = _b_run(tmp_path, "slow", [1300, 2600, 3900, 5200])
    assert workload.b_pair(slow, base) == 1
    assert "1.2 x baseline" in capsys.readouterr().out
    idle = _idle_run(tmp_path / "x")
    assert workload.b_pair(idle, base) == 2
    assert "is not B" in capsys.readouterr().out


def _chunk(op_id, *, ok=True, ms=200.0, residual=16, detail=()):
    return {"opId": op_id, "pass": ok, "problems": [] if ok else ["x"],
            "durationMs": ms, "fixedResidual": residual,
            "attributable": 100, "charge": 200, "detail": list(detail)}


def _segment(tmp_path, name, chunks, *, attributable=1000, charged=2000,
             temp=0, verdict="PASS"):
    seg = tmp_path / name
    seg.mkdir()
    # Amendment 19 HR-21: each segment's operation record, chained on one
    # clone (page count and freelist continue across op ids).
    (seg / "op.json").write_text(json.dumps({
        "identities": {"gitRev": _CAND_REV},
        "ops": [{"op_id": c["opId"], "page_count": 1_000_000 - 16 * c["opId"],
                 "freelist_count": 500_000 - 16 * c["opId"],
                 "page_count_reduction": 16, "freelist_reduction": 16}
                for c in chunks]}))
    result = {"mode": "r-cov", "pass": verdict == "PASS",
              "problems": [] if verdict == "PASS" else ["x"],
              "operations": len(chunks), "chunks": chunks,
              "attributable": attributable, "chargedBytes": charged,
              "bytes": {"db": 0, "wal": attributable, "temp": temp, "other": 0}}
    (seg / "verdict.txt").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n" + verdict + "\n")
    return str(seg)


def test_the_segmented_r_cov_aggregate_reconciles_its_segments(tmp_path, capsys):
    analyze = _load("analyze_op")
    interior = {"kind": "interior", "children": 300, "regions": 260}
    leaf = {"kind": "leaf", "heads": [5]}
    overflow = {"kind": "overflow", "nonTerminal": True}
    first = _segment(tmp_path, "s1", [_chunk(1, detail=[interior]),
                                      _chunk(2, ms=1065.0)])
    second = _segment(tmp_path, "s2", [_chunk(3, detail=[leaf, overflow],
                                              residual=32)],
                      attributable=900, charged=1000)
    result = analyze.aggregate([first, second], min_chunks=3)
    assert result["pass"] is True and result["chunks"] == 3
    assert result["chunkValidity"]["complete"] is True   # only over all chunks
    assert result["maxChunkDurationMs"] == 1065.0
    assert result["maxChunkFixedResidual"] == 32
    assert (result["attributable"], result["charged"]) == (1900, 3000)
    assert result["worstSegmentRatio"] == 0.9
    assert analyze.main(["--aggregate", first, second, "--min-chunks", "3"]) == 0
    capsys.readouterr()
    with pytest.raises(analyze.Invalid, match="relocated leaf"):
        analyze.aggregate([first], min_chunks=1)
    with pytest.raises(analyze.Invalid, match="need 5000"):
        analyze.aggregate([first, second])


def test_the_aggregate_refuses_unparsable_or_failing_segments(tmp_path, capsys):
    analyze = _load("analyze_op")
    detail = [{"kind": "interior", "children": 300, "regions": 260},
              {"kind": "leaf", "heads": [5]},
              {"kind": "overflow", "nonTerminal": True}]
    good = _segment(tmp_path, "good", [_chunk(1, detail=detail)])
    bare = tmp_path / "bare"
    bare.mkdir()
    (bare / "verdict.txt").write_text('{"mode": "r-cov"}\n')
    with pytest.raises(analyze.Invalid, match="no verdict line"):
        analyze.aggregate([good, str(bare)], min_chunks=1)
    invalid = tmp_path / "inv"
    invalid.mkdir()
    (invalid / "verdict.txt").write_text("INVALID: no op.json\n")
    assert analyze.main(["--aggregate", good, str(invalid),
                         "--min-chunks", "1"]) == 2
    assert "INVALID" in capsys.readouterr().out
    failing = _segment(tmp_path, "fail", [_chunk(2, ok=False, detail=detail)],
                       verdict="FAIL: chunk 2: x")
    result = analyze.aggregate([good, failing], min_chunks=1)
    assert result["pass"] is False and result["failedChunks"] == 1
    hot = _segment(tmp_path, "hot", [_chunk(2, detail=detail)], temp=4096)
    assert analyze.aggregate([good, hot], min_chunks=1)["tempBytes"] == 4096
    with pytest.raises(analyze.Invalid, match="more than one chunk"):
        analyze.aggregate([good, good], min_chunks=1)
    over = _segment(tmp_path, "over", [_chunk(2, detail=detail)],
                    attributable=5000, charged=1000)
    assert analyze.aggregate([good, over], min_chunks=1)["pass"] is False


# ── revision 12 (Q13): the terminal drain, drain-verdict, P3 and its runner ──

_KILLED_WRITER = r"""
import os, sqlite3, sys
db = sys.argv[1]
conn = sqlite3.connect(db)
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA wal_autocheckpoint=0")
conn.execute("CREATE TABLE IF NOT EXISTS t(b BLOB)")
conn.commit()
for _ in range(int(sys.argv[2])):
    conn.execute("INSERT INTO t VALUES (randomblob(8000))")
    conn.commit()
if sys.argv[3] == "partial":
    # A reader pins a snapshot, so the checkpoint copies only older frames.
    reader = sqlite3.connect(db)
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM t").fetchone()
    for _ in range(int(sys.argv[2])):
        conn.execute("INSERT INTO t VALUES (randomblob(8000))")
        conn.commit()
    conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
os._exit(0)
"""


def _killed_store(tmp_path, name="conversations.db", rows=20, mode="partial"):
    import subprocess
    import sys

    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    db = data / name
    subprocess.run([sys.executable, "-c", _KILLED_WRITER, str(db), str(rows),
                    mode], check=True, timeout=60)
    return data, db


def test_the_copier_reads_the_wal_index_before_it_drains_a_killed_store(
    tmp_path,
):
    drain = _load("terminal_drain")
    data, db = _killed_store(tmp_path)
    assert pathlib.Path(f"{db}-wal").stat().st_size > 0, "the kill left a WAL"
    result = drain.drain(data, ("conversations.db",),
                         rusage=lambda: {"rc": 0, "dwrite": 4096})
    reading = result["stores"]["conversations.db"]["walIndex"]
    assert reading["valid"] and not reading["walAbsent"]
    assert 0 < reading["nBackfill"] < reading["mxFrame"], reading
    checkpoint = result["stores"]["conversations.db"]["checkpoint"]
    assert checkpoint["ok"] and checkpoint["busy"] == 0
    assert checkpoint["walBytesAfter"] == 0
    assert result["rusage"] == {"rc": 0, "dwrite": 4096}
    terminal = tmp_path / "terminal.json"
    terminal.write_text(json.dumps({"t": 5, "alive": 1}))
    assert drain.main(["--data", str(data), "--terminal", str(terminal),
                       "--stores", "conversations.db"]) == 0
    record = json.loads(terminal.read_text())
    assert record["alive"] == 1 and record["drain"]["stores"]


def test_the_copier_refuses_an_inconsistent_or_foreign_wal_index(tmp_path):
    drain = _load("terminal_drain")
    data, db = _killed_store(tmp_path, mode="plain")
    shm = pathlib.Path(f"{db}-shm")
    raw = bytearray(shm.read_bytes())
    raw[50] ^= 0xFF  # the second header copy no longer equals the first
    shm.write_bytes(bytes(raw))
    reading = drain.read_wal_index(db)
    assert not reading["valid"] and "header copies differ" in reading["reason"]
    raw[50] ^= 0xFF
    shm.write_bytes(bytes(raw))
    wal = pathlib.Path(f"{db}-wal")
    header = bytearray(wal.read_bytes())
    header[16] ^= 0xFF  # a different WAL generation's salt
    wal.write_bytes(bytes(header))
    reading = drain.read_wal_index(db)
    assert not reading["valid"] and "salt" in reading["reason"]
    pathlib.Path(f"{db}-shm").unlink()
    reading = drain.read_wal_index(db)
    assert not reading["valid"] and "no -shm" in reading["reason"]


FRAME = 4096 + 24


def _drain_run(tmp_path, *, appended, main_writes, mx_frame, n_backfill,
               pages=None, copier=True, rusage_rc=0, frames_dropped=0,
               checkpoint_ok=True, valid_index=True, start=1000.0,
               end=1300.0):
    """A synthetic window: dashboard snapshots every 5 s, WAL frame records at
    `appended` times, main-file writes at `main_writes` times, and the
    copier's terminal record."""
    run = tmp_path / "run"
    run.mkdir()
    _live_inputs(run)
    (run / "window.json").write_text(json.dumps(
        {"traceStart": start - 100, "warm": start - 50, "start": start,
         "end": end}))
    db = "/clone/data/conversations.db"
    rows, main = [], 0
    t = start - 50
    writes = sorted(main_writes)
    while t <= end + 10:
        main += 4096 * sum(1 for w in writes if t - 5 < w <= t)
        rows.append({"t": t, "pid": 42, "dropped": 0,
                     "framesDropped": frames_dropped,
                     "paths": {db: [main, 1], f"{db}-wal": [0, 1]}})
        t += 5
    (run / "wtrace.42").write_text("".join(json.dumps(r) + "\n" for r in rows))
    pages = pages or [2 + i % 400 for i in range(len(appended))]
    (run / "wtrace.42.frames").write_text("".join(
        json.dumps({"t": at, "path": f"{db}-wal", "frame": i + 1, "pgno": pgno,
                    "commit": 1 if i % 10 == 9 else 0, "salt": 7}) + "\n"
        for i, (at, pgno) in enumerate(zip(appended, pages))))
    terminal = {"t": end + 6, "alive": 1}
    if copier:
        terminal["drain"] = {
            "pid": 77, "rusage": {"rc": rusage_rc,
                                  "dwrite": 8192 if rusage_rc == 0 else None},
            "stores": {"conversations.db": {
                "walIndex": ({"valid": True, "walAbsent": False,
                              "mxFrame": mx_frame, "nBackfill": n_backfill,
                              "pageSize": 4096, "walBytes": 1}
                             if valid_index else
                             {"valid": False, "reason": "header checksum"}),
                "checkpoint": {"ok": checkpoint_ok, "busy": 0,
                               "walBytesAfter": 0, "row": [0, 0, 0]}},
                "cache.db": {
                    "walIndex": {"valid": True, "walAbsent": True, "walBytes": 0,
                                 "mxFrame": 0, "nBackfill": 0, "pageSize": None},
                    "checkpoint": {"ok": True, "busy": 0, "walBytesAfter": 0,
                                   "row": [0, 0, 0]}}}}
        (run / "drain.77.exit").write_text(json.dumps(
            {"t": end + 8, "pid": 77, "dropped": 0, "framesDropped": 0,
             "paths": {db: [mx_frame * 4096, 3], f"{db}-wal": [0, 1]}}) + "\n")
    (run / "terminal.json").write_text(json.dumps(terminal))
    return str(run)


def _steady(start=1000.0, end=1300.0, per_second=2):
    return [start + i / per_second for i in range(int((end - start) * per_second))]


def test_drain_verdict_passes_a_bounded_deferral(tmp_path, capsys):
    workload = _load("workload")
    run = _drain_run(tmp_path, appended=_steady(),
                     main_writes=[1050, 1110, 1170, 1230, 1290],
                     mx_frame=900, n_backfill=880)
    result = workload.drain_check(run)
    assert result["status"] == "PASS", result
    store = result["stores"]["conversations.db"]
    assert store["appendedFrames"] == 600 and store["mainWrites"] == 5
    assert workload.main(["drain-verdict", run]) == 0
    assert capsys.readouterr().out.rstrip().endswith("PASS")


def test_drain_verdict_fails_frames_left_uncheckpointed_for_over_120_s(tmp_path):
    workload = _load("workload")
    run = _drain_run(tmp_path, appended=_steady(),
                     main_writes=[1050, 1240, 1290], mx_frame=900, n_backfill=880)
    result = workload.drain_check(run)
    assert result["status"] == "FAIL"
    assert any("no main-file write for 190 s" in p for p in result["problems"])


def test_drain_verdict_fails_a_repeated_page_backlog_above_the_limit(tmp_path):
    """Repeated versions of a few pages collapse into one copy each, so the
    copier's own output would look small; the wal-index frontier counts every
    committed frame, and it is above 16 MiB plus one minute's frames."""
    workload = _load("workload")
    appended = _steady(per_second=10)               # 3,000 frames, 600/min
    run = _drain_run(tmp_path, appended=appended,
                     pages=[2 + i % 4 for i in range(len(appended))],
                     main_writes=[1050, 1110, 1170, 1230, 1290],
                     mx_frame=5_000, n_backfill=200)
    result = workload.drain_check(run)
    assert result["status"] == "FAIL", result
    store = result["stores"]["conversations.db"]
    limit = 16 * MiB + 600 * FRAME
    assert store["backlogBytes"] == 4_800 * FRAME > limit
    assert store["backlogLimitBytes"] == pytest.approx(limit)


def test_drain_verdict_fails_partial_progress_over_an_older_backlog(tmp_path):
    """Periodic checkpoint writes satisfy (i), yet the backfill frontier lags
    the committed frontier by an older backlog above the limit."""
    workload = _load("workload")
    run = _drain_run(tmp_path, appended=_steady(per_second=1),
                     main_writes=[1020 + 60 * k for k in range(5)],
                     mx_frame=20_000, n_backfill=15_500)
    result = workload.drain_check(run)
    assert result["status"] == "FAIL", result
    assert not any("no main-file write" in p for p in result["problems"])
    assert any("unbackfilled" in p for p in result["problems"])


@pytest.mark.parametrize("kw, reason", [
    ({"copier": False}, "no terminal drain record"),
    ({"rusage_rc": 1}, "no kernel write sample of the copier"),
    ({"frames_dropped": 3}, "dropped bytes or frame records"),
    ({"valid_index": False}, "invalid wal-index reading"),
    ({"checkpoint_ok": False}, "did not succeed"),
])
def test_drain_verdict_is_invalid_without_complete_evidence(tmp_path, kw, reason):
    workload = _load("workload")
    run = _drain_run(tmp_path, appended=_steady(),
                     main_writes=[1050, 1110, 1170, 1230, 1290],
                     mx_frame=900, n_backfill=880, **kw)
    result = workload.drain_check(run)
    assert result["status"] == "INVALID"
    assert any(reason in p for p in result["problems"]), result


def test_q19_drain_judges_each_frame_against_the_next_write(tmp_path):
    """Spec §6.3 rule (i) (Q19): the C-c1 pattern. Writes 180 s apart around
    a sparse append that was copied 57 s after it arrived: the old interval
    reading failed it; each frame's own deferral is what is bounded."""
    workload = _load("workload")
    run = _drain_run(tmp_path, appended=[1055.0, 1183.0, 1320.0],
                     main_writes=[1060, 1240, 1360], mx_frame=3, n_backfill=3,
                     end=1400.0)
    result = workload.drain_check(run)
    assert result["status"] == "PASS", result
    store = result["stores"]["conversations.db"]
    assert store["worstDeferralS"] == pytest.approx(57.0)
    assert store["overdueFrames"] == 0 and store["censoredFrames"] == 0


@pytest.mark.parametrize("at, status", [(1180.0, "PASS"), (1179.9, "FAIL")])
def test_q19_drain_leaves_only_the_final_120_s_to_rule_ii(tmp_path, at, status):
    """An append with no later write in the window is left to (ii) only if
    it lies in the window's final 120 s (no sampling allowance there)."""
    workload = _load("workload")
    run = _drain_run(tmp_path, appended=[at], main_writes=[1050, 1110, 1170],
                     mx_frame=1, n_backfill=0)
    result = workload.drain_check(run)
    assert result["status"] == status, result
    store = result["stores"]["conversations.db"]
    assert store["censoredFrames"] == (1 if status == "PASS" else 0)
    if status == "FAIL":
        assert any("before the window's end" in p for p in result["problems"])


def test_q19_drain_counts_an_append_at_the_window_start(tmp_path):
    """The window's start is not a write event: a frame appended at the
    start and first copied 130 s later is overdue."""
    workload = _load("workload")
    run = _drain_run(tmp_path, appended=[1000.0], main_writes=[1130],
                     mx_frame=1, n_backfill=1)
    result = workload.drain_check(run)
    assert result["status"] == "FAIL", result
    assert any("appended at 0 s had no main-file write for 130 s" in p
               for p in result["problems"]), result["problems"]


def test_q19_drain_merges_every_process_and_its_exit_snapshot(tmp_path):
    """Write observations come from each process's own counters, its exit
    snapshot included; another process's unchanged pre-window bytes are not
    a write in the window."""
    workload = _load("workload")
    run = pathlib.Path(_drain_run(tmp_path, appended=[1183.0],
                                  main_writes=[1060, 1360], mx_frame=1,
                                  n_backfill=1, end=1400.0))
    db = "/clone/data/conversations.db"
    (run / "wtrace.44").write_text("".join(json.dumps(
        {"t": 950.0 + 5 * k, "pid": 44, "dropped": 0, "framesDropped": 0,
         "paths": {db: [8192, 2]}}) + "\n" for k in range(80)))
    stale = workload.drain_check(str(run))
    assert stale["status"] == "FAIL", stale
    assert any("for 177 s" in p for p in stale["problems"]), stale["problems"]
    (run / "wtrace.43.exit").write_text(json.dumps(
        {"t": 1240.0, "pid": 43, "dropped": 0, "framesDropped": 0,
         "paths": {db: [4096, 1]}}) + "\n")
    merged = workload.drain_check(str(run))
    assert merged["status"] == "PASS", merged
    assert merged["stores"]["conversations.db"]["worstDeferralS"] == \
        pytest.approx(57.0)


def test_the_window_verdicts_carry_the_drain_as_their_own_section(
    tmp_path, capsys,
):
    workload = _load("workload")
    run = pathlib.Path(_c_run(tmp_path))
    workload.c_verdict(str(run), "tree")
    out = capsys.readouterr().out
    result = _last_json(out)
    assert result["drain"]["status"] == "INVALID"
    assert out.rstrip().endswith("INVALID"), "no copier: the run is invalid"
    assert not any("drain" in p for p in result["problems"]), (
        "the drain is its own section, never folded into the window's")


def test_p3_report_compares_the_two_runs_store_by_store(tmp_path):
    workload = _load("workload")
    off_dir, on_dir = tmp_path / "off", tmp_path / "on"
    off_dir.mkdir()
    on_dir.mkdir()
    off = _drain_run(off_dir, appended=_steady(per_second=4),
                     main_writes=[1000 + 5 * k for k in range(1, 60)],
                     mx_frame=10, n_backfill=10)
    on = _drain_run(on_dir, appended=_steady(per_second=1),
                    main_writes=[1060, 1120, 1180, 1240, 1300],
                    mx_frame=300, n_backfill=200)
    for run, dwrite in ((off, (100, 900_100)), (on, (100, 400_100))):
        (pathlib.Path(run) / "pid").write_text("42")
        (pathlib.Path(run) / "rusage.jsonl").write_text(
            json.dumps({"t": 990, "pid": 42, "rc": 0, "dwrite": dwrite[0]}) + "\n"
            + json.dumps({"t": 1310, "pid": 42, "rc": 0, "dwrite": dwrite[1]})
            + "\n")
    report = workload.p3_report(off, on)
    w9_off = report["w9Off"]["stores"]["conversations.db"]
    shipped = report["shipped"]["stores"]["conversations.db"]
    assert (w9_off["walFrames"], shipped["walFrames"]) == (1200, 300)
    assert w9_off["commits"] == 120 and shipped["commits"] == 30
    assert w9_off["checkpointCopyBytes"] == 59 * 4096
    assert shipped["checkpointCopyBytes"] == 5 * 4096
    assert shipped["drainInterposerBytes"] == 300 * 4096
    assert shipped["windowPlusDrainBytes"] == (
        shipped["interposerBytes"] + shipped["drainInterposerBytes"])
    assert report["shipped"]["rusageBytes"] == 400_000
    assert report["shipped"]["windowPlusDrainRusageBytes"] == 400_000 + 8192
    assert report["shipped"]["rusageToInterposer"] == pytest.approx(
        400_000 / report["shipped"]["interposerBytes"])
    assert any("walFrames" in line for line in report["table"])


def test_the_runner_kills_the_dashboard_then_drains_under_the_interposer():
    script = (TOOLS / "run-workload.sh").read_text()
    drain_line = next(l for l in script.splitlines()
                      if 'exec $PY "$P/terminal_drain.py"' in l)
    # the drain section's own kill (reap's teardown kills the group too)
    kill_line = next(l for l in script.splitlines()
                     if l.startswith('wa_kill 9 "-$PID"'))
    assert script.rindex(kill_line) < script.index(drain_line)
    assert script.index('> "$OUT/terminal.json"') < script.rindex(kill_line)
    env_line = script.splitlines()[script.splitlines().index(drain_line) - 1]
    assert "WTRACE_OUT=$OUT/drain" in env_line
    assert "DYLD_INSERT_LIBRARIES=$DYLIB" in env_line
    p3 = (TOOLS / "run-p3.sh").read_text()
    assert "w9-off)  export CCTALLY_TEST_W9_DISABLE=1" in p3
    assert '"$P/run-p2.sh"' in p3
    assert os.access(TOOLS / "run-p3.sh", os.X_OK)


# ── revision 13 (Q14): the lifecycle proof, P1's repetition rule, P3's fit ──


_LIFECYCLE_STORES = ("conversations.db", "cache.db")


def _lifecycle_run(tmp_path, *, generations, start=1000.0, end=1150.0,
                   page1_commits=(), missing_checkpoints=(),
                   rusage_per_byte=None, rusage_per_generation=0,
                   frames=True, dash_log=""):
    """A synthetic P2-style run. For each store, every WAL generation starts
    with its header write (frame 0, a new salt) at the listed time, appends one
    one-frame commit per second until two seconds before the
    next generation, and ends with a main-file write one second before it (a
    completed checkpoint) unless its index is in `missing_checkpoints`.
    `page1_commits` adds one-frame commits of page 1 alone at those times. The
    dashboard's interposer snapshots (every 5 s) and, when asked, its rusage
    samples (every 15 s, `rusage_per_byte` x interposer bytes plus
    `rusage_per_generation` per new generation) follow from the frames.
    `dash_log` is the dashboard's stderr log (`dash.log`); None writes none."""
    run = tmp_path / "lifecycle-run"
    run.mkdir()
    _live_inputs(run)
    if dash_log is not None:
        (run / "dash.log").write_text(dash_log)
    (run / "window.json").write_text(json.dumps(
        {"traceStart": start - 100, "warm": start - 50, "start": start,
         "end": end}))
    (run / "pid").write_text("42")
    records, main_writes = [], {name: [] for name in _LIFECYCLE_STORES}
    starts = list(generations) + [end + 2]
    for name in _LIFECYCLE_STORES:
        wal = f"/clone/data/{name}-wal"
        for index, (begin, nxt) in enumerate(zip(starts, starts[1:])):
            salt = 1000 + index
            records.append({"t": begin, "path": wal, "frame": 0, "pgno": 0,
                            "commit": 0, "salt": salt})
            frame, at = 1, begin + 1
            while at <= nxt - 2:
                # Each frame its own commit, so a page-1 commit placed
                # between two of them stands alone, as SQLite writes it.
                records.append({"t": at, "path": wal, "frame": frame,
                                "pgno": 2 + frame % 50, "commit": 9,
                                "salt": salt})
                frame += 1
                if any(at < when <= at + 1 for when in page1_commits):
                    records.append({"t": at + 0.5, "path": wal,
                                    "frame": frame, "pgno": 1, "commit": 9,
                                    "salt": salt})
                    frame += 1
                at += 1
            if nxt <= end and index not in missing_checkpoints:
                main_writes[name].append(nxt - 1)
    records.sort(key=lambda r: r["t"])
    if frames:
        (run / "wtrace.42.frames").write_text(
            "".join(json.dumps(r) + "\n" for r in records))
    snaps, rusage = [], []
    t = start - 50
    while t <= end + 10:
        paths = {}
        for name in _LIFECYCLE_STORES:
            db = f"/clone/data/{name}"
            wal_bytes = FRAME * sum(
                1 for r in records if r["path"] == f"{db}-wal"
                and r["frame"] >= 1 and r["t"] <= t)
            main = 4096 * 10 * sum(1 for w in main_writes[name] if w <= t)
            paths[db] = [main, 1]
            paths[f"{db}-wal"] = [wal_bytes, 1]
        snaps.append({"t": t, "pid": 42, "dropped": 0, "framesDropped": 0,
                      "paths": paths})
        t += 5
    (run / "wtrace.42").write_text("".join(json.dumps(s) + "\n" for s in snaps))
    if rusage_per_byte is not None:
        rows = []
        for snap in snaps:
            if (snap["t"] - (start - 50)) % 15:
                continue
            interposer = sum(v[0] for v in snap["paths"].values())
            # Every store's WAL starts a generation at each listed time.
            new_generations = len(_LIFECYCLE_STORES) * sum(
                1 for g in generations if g <= snap["t"])
            rows.append({"t": snap["t"], "pid": 42, "rc": 0, "dwrite": int(
                rusage_per_byte * interposer
                + rusage_per_generation * new_generations)})
        (run / "rusage.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows))
    return str(run)


def test_lifecycle_verdict_fails_a_revision_12_frame_log(tmp_path, capsys):
    """A new WAL generation per pass, as the close-time checkpoint produced:
    far more completed checkpoints than one per 60 s, and page-1 commits."""
    workload = _load("workload")
    run = _lifecycle_run(
        tmp_path, generations=[1000 + 6 * k for k in range(25)],
        page1_commits=[1003 + 6 * k for k in range(25)])
    result = workload.lifecycle_check(run)
    assert result["status"] == "FAIL", result
    store = result["stores"]["conversations.db"]
    assert store["walGenerations"] == 25
    assert store["completedCheckpoints"] >= 24 > store["allowedCheckpoints"]
    assert store["page1OnlyCommits"] == 25
    assert any("page 1" in p for p in result["problems"])
    assert any("completed checkpoints" in p for p in result["problems"])
    assert workload.main(["lifecycle-verdict", run]) == 1
    assert capsys.readouterr().out.rstrip().endswith("FAIL")


def test_lifecycle_verdict_passes_a_deferred_frame_log(tmp_path, capsys):
    workload = _load("workload")
    run = _lifecycle_run(tmp_path, generations=[1000, 1060, 1120])
    result = workload.lifecycle_check(run)
    assert result["status"] == "PASS", result
    store = result["stores"]["cache.db"]
    assert store["walGenerations"] == 3
    assert store["completedCheckpoints"] == 2 <= store["allowedCheckpoints"]
    assert store["page1OnlyCommits"] == 0
    assert workload.main(["lifecycle-verdict", run]) == 0
    assert capsys.readouterr().out.rstrip().endswith("PASS")


def test_lifecycle_verdict_fails_a_generation_without_a_checkpoint(tmp_path):
    workload = _load("workload")
    run = _lifecycle_run(tmp_path, generations=[1000, 1060, 1120],
                         missing_checkpoints=(1,))
    result = workload.lifecycle_check(run)
    assert result["status"] == "FAIL", result
    assert any("without a completed checkpoint" in p
               for p in result["problems"]), result


def test_lifecycle_verdict_is_invalid_without_a_frame_log(tmp_path):
    workload = _load("workload")
    run = _lifecycle_run(tmp_path, generations=[1000], frames=False)
    result = workload.lifecycle_check(run)
    assert result["status"] == "INVALID"
    assert any("frame log" in p for p in result["problems"])


# ── revision 14 (Q15): no keeper yield in ordinary operation ────────────────

#: The line `bin/_lib_wal_checkpoint.py` writes to stderr at every keeper
#: yield, as a dashboard log carries it.
_YIELD_LINE = ("cctally: [w9] keeper-yield: conversations.db released its "
               "checkpoint keeper (keeper-yield request)\n")
_ORDINARY_LOG = "cctally dashboard on http://127.0.0.1:8789\n[cache] synced\n"


def test_lifecycle_verdict_fails_on_a_keeper_yield_line(tmp_path, capsys):
    workload = _load("workload")
    run = _lifecycle_run(tmp_path, generations=[1000, 1060, 1120],
                         dash_log=_ORDINARY_LOG + _YIELD_LINE)
    result = workload.lifecycle_check(run)
    assert result["status"] == "FAIL", result
    assert result["keeperYieldLines"] == 1
    assert any("keeper-yield" in p for p in result["problems"]), result
    assert workload.main(["lifecycle-verdict", run]) == 1
    assert capsys.readouterr().out.rstrip().endswith("FAIL")


def test_lifecycle_verdict_passes_a_log_without_a_keeper_yield_line(tmp_path):
    workload = _load("workload")
    run = _lifecycle_run(tmp_path, generations=[1000, 1060, 1120],
                         dash_log=_ORDINARY_LOG)
    result = workload.lifecycle_check(run)
    assert result["status"] == "PASS", result
    assert result["keeperYieldLines"] == 0


def test_lifecycle_verdict_is_invalid_without_a_dashboard_log(tmp_path):
    workload = _load("workload")
    run = _lifecycle_run(tmp_path, generations=[1000, 1060, 1120],
                         dash_log=None)
    result = workload.lifecycle_check(run)
    assert result["status"] == "INVALID", result
    assert any("dash.log" in p for p in result["problems"]), result


def _p2_run(tmp_path, monkeypatch, workload, dash_log):
    """A P2 run whose window, caps and drain all pass, so only its dashboard
    log can change the ab-verdict."""
    import subprocess
    import types

    run = tmp_path / "p2-run"
    run.mkdir()
    (run / "window.json").write_text(json.dumps(
        {"traceStart": 900.0, "warm": 950.0, "start": 1000.0, "end": 1330.0}))
    if dash_log is not None:
        (run / "dash.log").write_text(dash_log)
    monkeypatch.setattr(workload, "_window", lambda _run: (1000.0, 1330.0))
    monkeypatch.setattr(workload, "admission_problems", lambda _run: [])
    monkeypatch.setattr(workload, "terminal_problems", lambda _run, _end: [])
    monkeypatch.setattr(workload, "subprocess", types.SimpleNamespace(
        run=lambda *a, **k: subprocess.CompletedProcess(a, 0, "", "")))
    monkeypatch.setattr(workload, "_load_limits", lambda _tree: types.SimpleNamespace(
        bytes_per_minute=16 * 1024 * 1024, bytes_per_publication=8 * 1024 * 1024))
    monkeypatch.setattr(workload, "kernel_steady", lambda *_a, **_k: {
        "valid": True, "problems": [], "worstBytesPerMinute": 13e6,
        "worstBytesPerPublication": 3e6, "qualified": 2, "evaluations": 2})
    monkeypatch.setattr(workload, "_etilqs_in_window", lambda *_a: 0)
    monkeypatch.setattr(workload, "ambient_growth", lambda *_a: {})
    monkeypatch.setattr(workload, "drain_check",
                        lambda _run: {"status": "PASS", "problems": []})
    return str(run)


@pytest.mark.parametrize("dash_log, code", [
    (_ORDINARY_LOG, 0),
    (_ORDINARY_LOG + _YIELD_LINE, 1),
    (None, 2),
], ids=("ordinary-log", "yield-line", "no-log"))
def test_p2_ab_verdict_fails_on_a_keeper_yield_line(
    tmp_path, monkeypatch, capsys, dash_log, code,
):
    workload = _load("workload")
    run = _p2_run(tmp_path, monkeypatch, workload, dash_log)
    assert workload.main(["ab-verdict", run, "--tree", str(tmp_path)]) == code
    out = capsys.readouterr().out
    if code == 1:
        assert "keeper-yield" in out, out
    if code == 2:
        assert "dash.log" in out, out


def test_the_lifecycle_runner_replays_p2_for_at_least_two_minutes():
    runner = (TOOLS / "run-lifecycle.sh").read_text()
    assert os.access(TOOLS / "run-lifecycle.sh", os.X_OK)
    assert '"$P/run-p2.sh"' in runner
    assert "lifecycle-verdict" in runner
    assert "-lt 120" in runner, "the replay may not be shorter than 120 s"
    p2 = (TOOLS / "run-p2.sh").read_text()
    assert "P2_MEASURE_SECONDS" in p2
    workload = (TOOLS / "run-workload.sh").read_text()
    assert "P2_MEASURE_SECONDS" in workload


def test_p1_reruns_an_ambient_repetition_at_most_three_more_times():
    replay = _load("projection_replay")
    outcomes = iter([True, True, False])
    calls = []

    def measure():
        record = {"case": "append", "rep": 2, "ambient": next(outcomes)}
        calls.append(record)
        return record

    budget = {"append": 3}
    refused: list = []
    record = replay.measure_with_retries("append", 2, measure, budget, refused)
    assert record["ambient"] is False and len(calls) == 3
    assert [r["attempt"] for r in refused] == [1, 2]
    assert all(r["case"] == "append" and r["rep"] == 2
               and "ambient activity" in r["reason"] for r in refused)
    assert budget["append"] == 1

    always = {"case": "forced", "rep": 1, "ambient": True}
    budget = {"forced": 3}
    refused = []
    calls = []

    def ambient():
        calls.append(dict(always))
        return calls[-1]

    record = replay.measure_with_retries("forced", 1, ambient, budget, refused)
    assert len(calls) == 4 and len(refused) == 3 and budget["forced"] == 0
    assert record["ambient"] is True, "the fourth attempt is kept as measured"
    assert replay.measure_with_retries(
        "forced", 2, ambient, budget, refused)["ambient"] is True
    assert len(refused) == 3, "no retry once the case's three are spent"


def test_p1_verdict_accepts_refused_repetitions_listed_in_the_receipt():
    replay = _load("projection_replay")
    receipts = _p1_set()
    receipts[0]["refusedRepetitions"] = [
        {"case": "append", "rep": 3, "attempt": 1,
         "reason": "ambient activity in the pass (another file or "
                   "conversation changed)"}]
    code, result = replay.p1_verdict(receipts)
    assert code == 0, result


def test_p1_verdict_refuses_a_case_without_five_ambient_free_repetitions():
    replay = _load("projection_replay")
    receipts = _p1_set()
    passes = receipts[0]["passes"]
    last = next(p for p in reversed(passes) if p["case"] == "no-append")
    last["ambient"] = True
    receipts[0]["refusedRepetitions"] = [
        {"case": "no-append", "rep": 5, "attempt": attempt,
         "reason": "ambient activity in the pass (another file or "
                   "conversation changed)"} for attempt in (1, 2, 3)]
    code, result = replay.p1_verdict(receipts)
    assert code == 2
    assert any("4 ambient-free no-append" in p for p in result["invalid"]), (
        result["invalid"])
    receipts[0]["refusedRepetitions"].append(
        {"case": "no-append", "rep": 5, "attempt": 4, "reason": "ambient"})
    code, result = replay.p1_verdict(receipts)
    assert code == 2
    assert any("more than three" in p for p in result["invalid"])


def test_p3_report_counts_generations_and_fits_rusage_per_generation(tmp_path):
    workload = _load("workload")
    run = _lifecycle_run(tmp_path, generations=[1000 + 10 * k for k in range(15)],
                         rusage_per_byte=0.5, rusage_per_generation=1_200_000)
    result = workload.p3_run(run)
    store = result["stores"]["conversations.db"]
    assert store["walGenerations"] == 15
    assert store["completedCheckpoints"] == 14
    fit = result["perGenerationFit"]
    assert fit["intervals"] >= 8
    assert fit["bytesPerInterposerByte"] == pytest.approx(0.5, rel=1e-6)
    assert fit["bytesPerGeneration"] == pytest.approx(1_200_000, rel=1e-6)
    assert result["rusageToInterposer"] is not None


# ── revision 15 (Q16, `dc13` L2, 901-SR-037): the frozen input set ───────────


def _frozen_store(root, *, rows=(), wal=b""):
    """A closed store copy at ROOT/data whose four cursor tables retain
    `rows` = [(table, path, last_byte_offset, size_bytes, inode)]."""
    data = root / "data"
    data.mkdir(parents=True)
    schema = {
        "cache.db": {"session_files": False, "codex_session_files": True},
        "conversations.db": {"conversation_source_files": True,
                             "codex_conversation_source_files": True},
    }
    for db, tables in schema.items():
        conn = sqlite3.connect(data / db)
        for table, ident in tables.items():
            extra = ", device_id INTEGER, inode INTEGER" if ident else ""
            conn.execute(f"CREATE TABLE {table} (path TEXT PRIMARY KEY, "
                         f"size_bytes INTEGER, last_byte_offset INTEGER{extra})")
        for table, path, offset, size, inode in rows:
            if table not in tables:
                continue
            if tables[table]:
                conn.execute(f"INSERT INTO {table} (path, size_bytes, "
                             "last_byte_offset, device_id, inode) VALUES "
                             "(?, ?, ?, 1, ?)", (str(path), size, offset, inode))
            else:
                conn.execute(f"INSERT INTO {table} (path, size_bytes, "
                             "last_byte_offset) VALUES (?, ?, ?)",
                             (str(path), size, offset))
        conn.commit()
        conn.close()
        if wal and db == "cache.db":
            (data / f"{db}-wal").write_bytes(wal)
    old = 1_700_000_000
    for path in data.iterdir():
        os.utime(path, (old, old))
    return root


def _frozen_home(tmp_path):
    home = pathlib.Path(os.path.realpath(tmp_path)) / "home"
    sessions = home / ".codex" / "sessions" / "2026" / "10" / "05"
    sessions.mkdir(parents=True)
    rollout = sessions / "rollout-a.jsonl"
    rollout.write_bytes(b'{"a":1}\n{"b":2}\n')
    projects = home / ".claude" / "projects" / "-p"
    projects.mkdir(parents=True)
    session = projects / "s.jsonl"
    session.write_bytes(b'{"c":3}\n')
    auth = home / ".codex" / "auth.json"
    auth.write_bytes(b'{\n  "tokens": {\n    "account_id": "acct"\n  }\n}')
    (home / ".claude.json").write_bytes(b'{\n  "oauthAccount": {}\n}')
    return {"home": home, "rollout": rollout, "session": session, "auth": auth}


class _Ticks:
    """A fake monotonic clock: every call advances one second."""

    def __init__(self):
        self.t = 0.0

    def __call__(self):
        self.t += 1.0
        return self.t


def _capture(fr, tmp_path, tree, store=None, **kw):
    store = store or _frozen_store(tmp_path / "store")
    return fr.capture(store, tmp_path / "freeze", home=str(tree["home"]),
                      codex_home=None, claude_config_dir=None,
                      clock=kw.pop("clock", _Ticks()), **kw)


def _drop_before_seal(monkeypatch, fr, names):
    """Create files inside the freeze after the copy and before the seal, as
    macOS's Finder metadata (`.DS_Store`) appeared in the first real freeze."""
    original = fr._seal

    def seal(freeze):
        for rel in names:
            (pathlib.Path(freeze) / rel).write_bytes(b"\0" * 16)
        return original(freeze)
    monkeypatch.setattr(fr, "_seal", seal)


def test_seal_removes_finder_metadata_created_during_capture(tmp_path, monkeypatch):
    fr = _load("frozen_roots")
    tree = _frozen_home(tmp_path)
    _drop_before_seal(monkeypatch, fr, ["roots/0/.DS_Store",
                                        "roots/0/sessions/.DS_Store"])
    manifest = _capture(fr, tmp_path, tree)
    freeze = tmp_path / "freeze"
    assert not list(freeze.rglob(".DS_Store"))
    assert manifest["seal"]["removedFinderMetadata"] == 2
    assert fr.verify(freeze, full=True)["problems"] == []


def test_seal_refuses_an_unmanifested_file_in_the_freeze(tmp_path, monkeypatch):
    fr = _load("frozen_roots")
    tree = _frozen_home(tmp_path)
    _drop_before_seal(monkeypatch, fr, ["roots/0/sessions/stray.jsonl"])
    with pytest.raises(fr.Refused, match="unmanifested file"):
        _capture(fr, tmp_path, tree)


def test_selftest_rows_ignore_the_ingest_clock_of_either_tree(tmp_path):
    """The control tree (before W8) rewrites claude_ingest_walk_complete and
    codex_file_incarnations.updated_at_utc on every walk; both are the
    ingest's own clock, never input content, so the frozen-versus-control
    comparison must not see them, while a real content change still shows."""
    st = _load("frozen_selftest")

    def store(base, stamp, incarnation):
        (base / "data").mkdir(parents=True)
        conn = sqlite3.connect(base / "data" / "cache.db")
        conn.executescript(
            "CREATE TABLE cache_meta (key TEXT PRIMARY KEY, value TEXT);"
            "CREATE TABLE codex_file_incarnations (file_identity TEXT PRIMARY "
            "KEY, incarnation INTEGER NOT NULL, updated_at_utc TEXT);")
        conn.execute("INSERT INTO cache_meta VALUES "
                     "('claude_ingest_walk_complete', ?)", (stamp,))
        conn.execute("INSERT INTO cache_meta VALUES "
                     "('codex_accounting_mutation_seq', '301')")
        conn.execute("INSERT INTO codex_file_incarnations VALUES ('f', ?, ?)",
                     (incarnation, stamp))
        conn.commit()
        conn.close()
        return base / "data"
    a = st.table_rows(store(tmp_path / "a", "2026-10-05T10:29:43Z", 1))
    b = st.table_rows(store(tmp_path / "b", "2026-10-05T10:29:42Z", 1))
    c = st.table_rows(store(tmp_path / "c", "2026-10-05T10:29:42Z", 2))
    assert a == b
    assert a != c


def _entry(manifest, path):
    return next(e for e in manifest["entries"] if e["logical"] == str(path))


def test_capture_copies_a_record_boundary_prefix_without_a_torn_line(tmp_path):
    fr = _load("frozen_roots")
    tree = _frozen_home(tmp_path)
    with open(tree["rollout"], "ab") as fh:
        fh.write(b'{"torn": tr')
    manifest = _capture(fr, tmp_path, tree)
    entry = _entry(manifest, tree["rollout"])
    assert entry["admittedLength"] == len(b'{"a":1}\n{"b":2}\n')
    assert entry["sourceSize"] > entry["admittedLength"]
    frozen = (tmp_path / "freeze" / entry["physical"]).read_bytes()
    assert frozen == b'{"a":1}\n{"b":2}\n'
    assert entry["ino"] == os.stat(tree["rollout"]).st_ino
    assert entry["sha256"] == __import__("hashlib").sha256(frozen).hexdigest()


def test_capture_admits_suffix_growth_during_the_copy(tmp_path):
    fr = _load("frozen_roots")
    tree = _frozen_home(tmp_path)

    def grow(src, attempt):
        if src == str(tree["rollout"]) and attempt == 1:
            with open(src, "ab") as fh:
                fh.write(b'{"later":1}\n')
    manifest = _capture(fr, tmp_path, tree, on_copy=grow)
    entry = _entry(manifest, tree["rollout"])
    assert entry["grewDuringCopy"] is True and entry["attempts"] == 1
    assert entry["admittedLength"] == len(b'{"a":1}\n{"b":2}\n')


def test_capture_refuses_a_retained_cursor_beyond_the_last_record(tmp_path):
    fr = _load("frozen_roots")
    tree = _frozen_home(tmp_path)
    store = _frozen_store(tmp_path / "store", rows=[
        ("codex_session_files", tree["rollout"], 40, 40, 1)])
    with pytest.raises(fr.Refused, match="retained cursor/scan target 40"):
        _capture(fr, tmp_path, tree, store=store, retry_deadline_s=3)


@pytest.mark.parametrize("change, reason", [
    ("replace", "replaced during the copy"),
    ("shrink", "shrank during the copy"),
    ("rewrite", "in-place rewrite"),
])
def test_capture_retries_then_refuses_a_transcript_that_never_settles(
        tmp_path, change, reason):
    fr = _load("frozen_roots")
    tree = _frozen_home(tmp_path)
    full = b'{"a":1}\n{"b":2}\n'
    src = str(tree["rollout"])

    def mutate(path, attempt):
        if path != src:
            return
        if change == "replace":          # a new inode every attempt
            pathlib.Path(src + ".new").write_bytes(full)
            os.replace(src + ".new", src)
        elif change == "shrink":         # cut below the copied boundary
            with open(src, "r+b") as fh:
                fh.truncate(4)
        else:                            # the copied prefix rewritten in place
            with open(src, "r+b") as fh:
                fh.write(b'{"%d"' % (attempt % 10))
            with open(src, "ab") as fh:
                fh.write(b"\n")

    class Restoring(_Ticks):
        """Each retry starts from the full file again (the clock is read at
        the top of every retry), so only `change` can refuse it."""

        def __call__(self):
            if change == "shrink":
                pathlib.Path(src).write_bytes(full)
            return super().__call__()
    with pytest.raises(fr.Refused, match=reason):
        _capture(fr, tmp_path, tree, on_copy=mutate, retry_deadline_s=4,
                 clock=Restoring())


def test_capture_retries_a_replacement_once_and_records_the_new_identity(tmp_path):
    fr = _load("frozen_roots")
    tree = _frozen_home(tmp_path)

    def replace_once(src, attempt):
        if src == str(tree["rollout"]) and attempt == 1:
            tmp = src + ".new"
            pathlib.Path(tmp).write_bytes(b'{"a":1}\n{"b":2}\n{"c":3}\n')
            os.replace(tmp, src)
    manifest = _capture(fr, tmp_path, tree, on_copy=replace_once)
    entry = _entry(manifest, tree["rollout"])
    assert entry["attempts"] == 2
    assert entry["ino"] == os.stat(tree["rollout"]).st_ino
    assert entry["admittedLength"] == 24


def test_capture_copies_multiline_json_metadata_whole(tmp_path):
    fr = _load("frozen_roots")
    tree = _frozen_home(tmp_path)
    manifest = _capture(fr, tmp_path, tree)
    for path in (tree["auth"], tree["home"] / ".claude.json"):
        entry = _entry(manifest, path)
        assert entry["class"] == "metadata"
        frozen = (tmp_path / "freeze" / entry["physical"]).read_bytes()
        assert frozen == path.read_bytes()          # never cut at a newline
        assert entry["admittedLength"] == path.stat().st_size


def test_capture_retries_an_in_place_metadata_rewrite_then_refuses(tmp_path):
    fr = _load("frozen_roots")
    tree = _frozen_home(tmp_path)
    auth = str(tree["auth"])

    def once(src, attempt):
        if src == auth and attempt == 1:
            with open(src, "r+b") as fh:
                fh.write(b'{\n  "TOKENS"')
    manifest = _capture(fr, tmp_path, tree, on_copy=once)
    assert _entry(manifest, tree["auth"])["attempts"] == 2

    def always(src, attempt):
        if src == auth:
            with open(src, "r+b") as fh:
                fh.write(b'{\n  "T%04d"' % attempt)
    with pytest.raises(fr.Refused, match="in-place rewrite"):
        fr.capture(_frozen_store(tmp_path / "store2"), tmp_path / "freeze2",
                   home=str(tree["home"]), clock=_Ticks(), on_copy=always,
                   retry_deadline_s=4)


def test_capture_covers_a_file_symlink_inside_a_root(tmp_path):
    fr = _load("frozen_roots")
    tree = _frozen_home(tmp_path)
    link = tree["rollout"].parent.parent / "latest"
    os.symlink("05/rollout-a.jsonl", link)
    manifest = _capture(fr, tmp_path, tree, extras=[str(link)])
    entry = _entry(manifest, link)
    assert entry["kind"] == "symlink" and entry["target"] == "05/rollout-a.jsonl"
    assert entry["resolved"] == str(tree["rollout"])
    tsv = (tmp_path / "freeze" / "rootmap.tsv").read_text().splitlines()
    line = next(l for l in tsv if l.startswith("E\tL\t"))
    assert len(line.split("\t")) == 21
    assert line.split("\t")[19:] == ["05/rollout-a.jsonl", str(tree["rollout"])]


# ── Amendment 10: symlinks in the real roots (§6.3 "Frozen input set":
# "symlinks and canonical aliases are covered or refused") ──────────────────


def _link_tree(tmp_path):
    """_frozen_home plus the two link shapes the real roots hold: an absolute
    single-hop link from the Codex walk to a rollout under another
    repository's .codex/sessions (outside every root), and a Claude project's
    `memory` directory link to a directory outside every root that holds a
    .jsonl the product walk must never reach."""
    tree = _frozen_home(tmp_path)
    outside = pathlib.Path(os.path.realpath(tmp_path)) / "outside" / "repo"
    target = outside / ".codex" / "sessions" / "2026" / "10" / "04" / "rollout-ext.jsonl"
    target.parent.mkdir(parents=True)
    target.write_bytes(b'{"x":1}\n{"y":2}\n{"torn')
    link = tree["rollout"].parent / "rollout-ext.jsonl"
    os.symlink(str(target), link)
    memory_target = outside / ".agentmem"
    memory_target.mkdir()
    (memory_target / "m.jsonl").write_bytes(b'{"memory":1}\n')
    memory = tree["session"].parent / "memory"
    os.symlink(str(memory_target), memory)
    tree.update(target=target, link=link, memory=memory, memory_target=memory_target)
    return tree


def _tsv(freeze):
    return (freeze / "rootmap.tsv").read_text().splitlines()


def test_capture_covers_an_absolute_link_to_a_file_outside_every_root(tmp_path):
    fr = _load("frozen_roots")
    tree = _link_tree(tmp_path)
    st = os.stat(tree["target"])
    store = _frozen_store(tmp_path / "store", rows=[
        ("codex_session_files", tree["target"], 8, 8, st.st_ino)])
    manifest = _capture(fr, tmp_path, tree, store=store)
    freeze = tmp_path / "freeze"
    link = _entry(manifest, tree["link"])
    assert link["kind"] == "symlink" and link["class"] == "symlink"
    assert link["target"] == link["resolved"] == str(tree["target"])
    assert link["targetScope"] == "outside-roots"
    assert link["ino"] == os.lstat(tree["link"]).st_ino
    target = _entry(manifest, tree["target"])
    assert target["kind"] == "file" and target["class"] == "transcript"
    assert (target["dev"], target["ino"]) == (st.st_dev, st.st_ino)
    assert target["admittedLength"] == len(b'{"x":1}\n{"y":2}\n')
    assert target["linkedFrom"] == [str(tree["link"])]
    root = next(r for r in manifest["roots"] if r["logical"] == str(tree["target"]))
    assert root["kind"] == "file" and root["origin"] == "link-target"
    assert root["links"] == [str(tree["link"])] and root["walk"] is None
    assert link["targetRoot"] == root["index"] == target["root"]
    assert target["physical"] == root["frozen"]
    # inside the freeze the link is relative and resolves to the frozen copy
    frozen_link = freeze / link["physical"]
    assert os.path.islink(frozen_link) and not os.path.isabs(os.readlink(frozen_link))
    assert os.path.realpath(frozen_link) == os.path.realpath(freeze / target["physical"])
    assert (freeze / target["physical"]).read_bytes() == b'{"x":1}\n{"y":2}\n'
    tsv = _tsv(freeze)
    assert "\t".join(["R", str(tree["target"]),
                      os.path.join(os.path.realpath(freeze), root["frozen"]),
                      "1", "1"]) in tsv
    line = next(l.split("\t") for l in tsv if l.startswith(f"E\tL\t{tree['link']}\t"))
    assert line[19:] == [str(tree["target"]), str(tree["target"])]
    line = next(l.split("\t") for l in tsv if l.startswith(f"E\tF\t{tree['target']}\t"))
    assert (int(line[3]), int(line[4]), int(line[5])) == (st.st_dev, st.st_ino, 16)
    profile = (freeze / "frozen.sb").read_text()
    assert (f'(deny file-read* file-write* (literal "{tree["target"]}") '
            "(with send-signal SIGKILL))") in profile
    assert fr.verify(freeze, full=True)["valid"] is True


@pytest.mark.parametrize("shape, reason", [
    ("relative", "relative symlink"),
    ("chain", "chain of links"),
    ("parent-link", "chain of links"),
    ("directory", "directory used as a file"),
    ("fifo", "not a regular file"),
    ("dangling", "not a regular file"),
])
def test_capture_refuses_an_outside_link_it_cannot_cover(tmp_path, shape, reason):
    fr = _load("frozen_roots")
    tree = _frozen_home(tmp_path)
    outside = pathlib.Path(os.path.realpath(tmp_path)) / "outside"
    outside.mkdir()
    real = outside / "real.jsonl"
    real.write_bytes(b'{"x":1}\n')
    link = tree["rollout"].parent / "rollout-out.jsonl"
    if shape == "relative":
        os.symlink(os.path.relpath(real, link.parent), link)
    elif shape == "chain":
        os.symlink(str(real), outside / "hop.jsonl")
        os.symlink(str(outside / "hop.jsonl"), link)
    elif shape == "parent-link":             # single hop, through a linked dir
        os.symlink(str(outside), pathlib.Path(os.path.realpath(tmp_path)) / "alias")
        os.symlink(str(pathlib.Path(os.path.realpath(tmp_path)) / "alias" / "real.jsonl"),
                   link)
    elif shape == "directory":
        os.symlink(str(outside), link)
    elif shape == "fifo":
        os.mkfifo(outside / "pipe.jsonl")
        os.symlink(str(outside / "pipe.jsonl"), link)
    else:
        os.symlink(str(outside / "gone.jsonl"), link)
    with pytest.raises(fr.Refused, match=reason):
        _capture(fr, tmp_path, tree)


def test_capture_covers_a_directory_link_the_walk_does_not_traverse(tmp_path):
    fr = _load("frozen_roots")
    tree = _link_tree(tmp_path)
    manifest = _capture(fr, tmp_path, tree)
    freeze = tmp_path / "freeze"
    entry = _entry(manifest, tree["memory"])
    assert entry["kind"] == "symlink" and entry["class"] == "dir-link"
    assert entry["traversed"] is False
    assert entry["target"] == entry["resolved"] == str(tree["memory_target"])
    assert entry["ino"] == os.lstat(tree["memory"]).st_ino
    under = [e["logical"] for e in manifest["entries"]
             if e["logical"].startswith((str(tree["memory"]) + "/",
                                         str(tree["memory_target"])))]
    assert under == []                       # nothing under the link is captured
    assert [{k: v for k, v in d.items() if k != "metadata"}
            for d in manifest["deniedTargets"]] == [
        {"logical": str(tree["memory_target"]), "kind": "dir",
         "links": [str(tree["memory"])]}]
    frozen_link = freeze / entry["physical"]
    assert os.path.islink(frozen_link)
    assert os.readlink(frozen_link) == str(tree["memory_target"])
    tsv = _tsv(freeze)
    line = next(l.split("\t") for l in tsv if l.startswith(f"E\tL\t{tree['memory']}\t"))
    assert line[19:] == [str(tree["memory_target"])] * 2
    assert any(l.split("\t")[:2] == ["X", str(tree["memory_target"])] for l in tsv)
    profile = (freeze / "frozen.sb").read_text()
    assert (f'(deny file-read* file-write* (subpath "{tree["memory_target"]}") '
            "(with send-signal SIGKILL))") in profile
    assert fr.verify(freeze, full=True)["valid"] is True


def _tsv_fields(tsv, kind, path):
    return next(l.split("\t") for l in tsv if l.split("\t")[:2] == [kind, str(path)])


def test_capture_records_a_directory_links_target_metadata_never_its_contents(
        tmp_path):
    """Amendment 13 O1: `os.walk` (DirEntry.is_dir) and `os.stat` FOLLOW the
    non-traversed link, and the frozen link points at its live target, which
    the sandbox kills. The capture records the target directory's own `lstat`
    (type, device, inode, mode, timestamps) so the namespace answers a
    following stat with no kernel call on the live target; nothing under the
    target is captured or opened."""
    fr = _load("frozen_roots")
    tree = _link_tree(tmp_path)
    st = os.lstat(tree["memory_target"])
    manifest = _capture(fr, tmp_path, tree)
    freeze = tmp_path / "freeze"
    (denied,) = manifest["deniedTargets"]
    meta = denied["metadata"]
    assert meta["type"] == "dir"
    assert (meta["dev"], meta["ino"], meta["mode"], meta["nlink"], meta["size"]) == (
        st.st_dev, st.st_ino, st.st_mode, st.st_nlink, st.st_size)
    assert (meta["uid"], meta["gid"], meta["mtimeNs"], meta["ctimeNs"]) == (
        st.st_uid, st.st_gid, st.st_mtime_ns, st.st_ctime_ns)
    tsv = _tsv(freeze)
    assert tsv[0] == "ROOTMAP\t2"
    line = _tsv_fields(tsv, "X", tree["memory_target"])
    assert len(line) == 18
    assert [int(v) for v in line[2:9]] == [st.st_dev, st.st_ino, st.st_size, st.st_mode,
                                           st.st_nlink, st.st_uid, st.st_gid]
    assert (int(line[11]), int(line[12])) == divmod(st.st_mtime_ns, 1_000_000_000)
    assert not any(e["logical"].startswith(str(tree["memory_target"]))
                   for e in manifest["entries"])
    assert fr.verify(freeze, full=True)["valid"] is True


def test_capture_refuses_a_directory_link_whose_target_vanished(tmp_path, monkeypatch):
    fr = _load("frozen_roots")
    tree = _link_tree(tmp_path)
    real_lstat = fr.os.lstat

    def lstat(path, *a, **k):
        if str(path) == str(tree["memory_target"]):
            raise FileNotFoundError(2, "No such file or directory", str(path))
        return real_lstat(path, *a, **k)
    monkeypatch.setattr(fr.os, "lstat", lstat)
    with pytest.raises(fr.Refused, match="target of a non-traversed directory link"):
        _capture(fr, tmp_path, tree)


def test_capture_covers_an_in_root_directory_link_and_refuses_one_used_as_a_file(
        tmp_path):
    fr = _load("frozen_roots")
    tree = _frozen_home(tmp_path)
    alias = tree["rollout"].parent.parent / "alias-dir"
    os.symlink(tree["rollout"].parent, alias)
    manifest = _capture(fr, tmp_path, tree)
    entry = _entry(manifest, alias)
    assert entry["class"] == "dir-link" and entry["traversed"] is False
    assert not any(e["logical"].startswith(str(alias) + "/")
                   for e in manifest["entries"])
    os.symlink(tree["rollout"].parent, tree["rollout"].parent.parent / "dir.jsonl")
    with pytest.raises(fr.Refused, match="directory used as a file"):
        fr.capture(_frozen_store(tmp_path / "store2"), tmp_path / "freeze2",
                   home=str(tree["home"]), clock=_Ticks())


def test_qualify_checks_a_retained_file_reached_through_a_link(tmp_path):
    fr = _load("frozen_roots")
    tree = _link_tree(tmp_path)
    ino = os.stat(tree["target"]).st_ino
    store = _frozen_store(tmp_path / "store", rows=[
        ("codex_session_files", tree["target"], 16, 16, ino),       # canonical
        ("conversation_source_files", tree["link"], 8, 16, ino)])   # link path
    _capture(fr, tmp_path, tree, store=store)
    freeze = tmp_path / "freeze"
    result = fr.qualify(store, freeze)
    assert result["qualified"] is True, result["problems"]
    assert result["retainedChecked"] == 2

    def tweak(sql, *params):
        conn = sqlite3.connect(store / "data" / "cache.db")
        conn.execute(sql, params)
        conn.commit()
        conn.close()
        os.utime(store / "data" / "cache.db", (1_700_000_000, 1_700_000_000))
    tweak("UPDATE codex_session_files SET inode = ?", ino + 1)
    result = fr.qualify(store, freeze)
    assert any(f"{tree['target']}: replaced" in p for p in result["problems"]), \
        result["problems"]
    tweak("UPDATE codex_session_files SET inode = ?, last_byte_offset = 99", ino)
    result = fr.qualify(store, freeze)
    assert any("last_byte_offset 99 is beyond the frozen file (16 bytes)" in p
               for p in result["problems"]), result["problems"]


def test_the_launcher_closes_and_checks_denied_link_targets_too(tmp_path):
    launch = _load("frozen_launch")
    (tmp_path / "rootmap.tsv").write_text(
        "ROOTMAP\t1\nR\t/h/.codex\t/f/roots/0\t0\t1\nR\t/r/t.jsonl\t/f/roots/1\t1\t1\n"
        "E\tL\t/h/.codex/sessions/m\t1\t2\t3\t4\t5\t6\t7\t0\t0\t0\t0\t0\t0\t0\t0\t0"
        "\t/r/mem\t/r/mem\nX\t/r/mem\n")
    assert launch._live_paths(str(tmp_path)) == ["/h/.codex", "/r/t.jsonl", "/r/mem"]


# ── Amendment 13 O2: probe-only entries for paths the family only checks ────


def _codex_cwd_lines(cwd, turn_cwd=None):
    return [json.dumps({"type": "session_meta", "payload": {"id": "t", "cwd": cwd}}),
            json.dumps({"type": "turn_context", "payload": {"cwd": turn_cwd or cwd}}),
            json.dumps({"type": "response_item", "payload": {"cwd": "/ignored"}})]


def _probe_tree(tmp_path):
    """_frozen_home plus a Codex rollout whose session_meta and turn_context
    `cwd` lie in a Codex worktree under the Codex home (each ancestor and its
    `.git` is what the product's git-root walk-up checks), a Claude record
    whose `cwd` names a worktree already removed, a torn last line whose cwd
    lies BEYOND the captured prefix, and a cwd outside every root."""
    tree = _frozen_home(tmp_path)
    codex = tree["home"] / ".codex"
    repo = codex / "worktrees" / "abc" / "repo"
    (repo / "sub").mkdir(parents=True)
    (repo / ".git").write_text("gitdir: /elsewhere/.git/worktrees/abc\n")
    rollout = tree["rollout"].parent / "rollout-w.jsonl"
    lines = _codex_cwd_lines(str(repo / "sub"), str(repo))
    lines.append(json.dumps({"type": "session_meta", "payload": {"cwd": "/outside/proj"}}))
    rollout.write_text("\n".join(lines) + "\n" + json.dumps(
        {"type": "turn_context", "payload": {"cwd": str(codex / "worktrees" / "torn")}}))
    (codex / "worktrees" / "torn").mkdir()
    with open(tree["session"], "a") as fh:
        fh.write(json.dumps({"type": "user", "cwd": str(codex / "worktrees" / "gone" / "r2"),
                             "message": {"content": "x"}}) + "\n")
    tree.update(codex=codex, repo=repo, wt=codex / "worktrees", rollout_w=rollout)
    return tree


def test_capture_derives_probe_entries_from_the_frozen_transcripts_cwds(tmp_path):
    fr = _load("frozen_roots")
    tree = _probe_tree(tmp_path)
    manifest = _capture(fr, tmp_path, tree)
    freeze = tmp_path / "freeze"
    probes = {e["logical"]: e for e in manifest["entries"] if e.get("class") == "probe"}
    repo, wt = tree["repo"], tree["wt"]
    assert sorted(probes) == sorted(str(p) for p in (
        wt, wt / "abc", repo, repo / "sub", repo / ".git"))
    for path, e in probes.items():
        st = os.lstat(path)
        assert e["kind"] == "probe" and "physical" not in e
        assert (e["dev"], e["ino"], e["mode"], e["size"], e["mtimeNs"]) == (
            st.st_dev, st.st_ino, st.st_mode, st.st_size, st.st_mtime_ns), path
    assert probes[str(repo / ".git")]["type"] == "file"
    assert probes[str(repo / "sub")]["type"] == "dir"
    absences = {a["logical"]: a["reason"] for a in manifest["absences"]}
    for gone in (tree["codex"] / ".git", wt / ".git", wt / "abc" / ".git",
                 repo / "sub" / ".git", wt / "gone"):
        assert absences.get(str(gone)) == "probe-absent", gone
    # nothing below a recorded absence, nothing beyond the captured prefix,
    # nothing outside every root
    assert not any(k.startswith((str(wt / "gone") + "/", str(wt / "torn"), "/outside"))
                   for k in list(probes) + list(absences))
    summary = manifest["probes"]
    assert summary["cwds"] == 3 and summary["entries"] == 5
    assert summary["absences"] == 5 and summary["scan"]["files"] >= 3
    tsv = _tsv(freeze)
    lines = {l.split("\t")[2]: l.split("\t") for l in tsv if l.startswith("E\tP\t")}
    assert sorted(lines) == sorted(probes)
    assert int(lines[str(repo / ".git")][4]) == os.lstat(repo / ".git").st_ino
    assert all(len(f) == 19 for f in lines.values())
    assert f"A\t{wt / 'gone'}" in tsv
    assert fr.verify(freeze, full=True)["valid"] is True


def test_capture_probe_presents_a_links_lstat_stat_and_target(tmp_path):
    fr = _load("frozen_roots")
    tree = _frozen_home(tmp_path)
    outside = pathlib.Path(os.path.realpath(tmp_path)) / "outside" / "wt"
    (outside / "sub").mkdir(parents=True)
    wt = tree["home"] / ".codex" / "worktrees"
    wt.mkdir()
    os.symlink(str(outside), wt / "lnk")
    (tree["rollout"].parent / "rollout-l.jsonl").write_text(
        "\n".join(_codex_cwd_lines(str(wt / "lnk" / "sub"))) + "\n")
    manifest = _capture(fr, tmp_path, tree)
    e = _entry(manifest, wt / "lnk")
    assert e["class"] == "probe" and e["type"] == "symlink"
    assert e["ino"] == os.lstat(wt / "lnk").st_ino
    assert e["target"] == str(outside) and e["resolved"] == str(outside)
    assert e["followed"]["ino"] == os.stat(outside).st_ino
    assert e["followed"]["type"] == "dir"
    # nothing is probed below a link (the walk-up resolves it first)
    assert not any(x["logical"].startswith(str(wt / "lnk") + "/")
                   for x in manifest["entries"])
    tsv = _tsv(tmp_path / "freeze")
    line = next(l.split("\t") for l in tsv if l.startswith(f"E\tP\t{wt / 'lnk'}\t"))
    assert line[19:] == [str(outside), str(outside)]
    s = _tsv_fields(tsv, "S", wt / "lnk")
    assert len(s) == 18 and int(s[3]) == os.stat(outside).st_ino


def test_capture_takes_explicit_probes_and_refuses_one_outside_every_root(
        tmp_path, capsys):
    fr = _load("frozen_roots")
    tree = _frozen_home(tmp_path)
    thing = tree["home"] / ".codex" / "extra" / "thing"
    thing.parent.mkdir()
    thing.write_text("x")
    missing = tree["home"] / ".codex" / "nope" / "x"
    store = _frozen_store(tmp_path / "store")
    code = fr.main(["capture", "--store", str(store), "--out", str(tmp_path / "freeze"),
                    "--home", str(tree["home"]), "--codex-home", "",
                    "--claude-config-dir", "", "--probe", str(thing),
                    "--probe", str(missing)])
    assert code == 0, capsys.readouterr().out
    manifest = json.loads((tmp_path / "freeze" / "manifest.json").read_text())
    assert _entry(manifest, thing)["class"] == "probe"
    assert _entry(manifest, thing.parent)["type"] == "dir"
    absences = {a["logical"]: a["reason"] for a in manifest["absences"]}
    assert absences[str(missing.parent)] == "probe-absent"
    assert str(missing) not in absences                  # under a recorded absence
    assert manifest["probes"]["explicit"] == [str(thing), str(missing)]
    with pytest.raises(fr.Refused, match="probe .* outside every directory root"):
        _capture(fr, tmp_path / "x", tree, store=_frozen_store(tmp_path / "x" / "store"),
                 probes=["/elsewhere/thing"])


# ── Amendment 14 P1: probe entries at and below a denied directory-link target ─


def _target_tree(tmp_path):
    """_link_tree plus records whose working directory is the memory link's
    live target itself (a `.git` file there), a directory below it (a `.git`
    directory at one component, none at the next) and a path below it whose
    first component is gone: the real transcripts record a project's
    `.claude-memory` as their cwd, and the product's git-root walk-up checks
    `<target>/.git` (run-s15b-A-c1's three `traversal` problems)."""
    tree = _link_tree(tmp_path)
    t = tree["memory_target"]
    (t / ".git").write_text("gitdir: /elsewhere/.git/worktrees/mem\n")
    (t / "notes" / "deep").mkdir(parents=True)
    (t / "notes" / ".git").mkdir()
    with open(tree["session"], "a") as fh:
        for cwd in (t, t / "notes" / "deep"):
            fh.write(json.dumps({"type": "user", "cwd": str(cwd),
                                 "message": {"content": "x"}}) + "\n")
    (tree["rollout"].parent / "rollout-m.jsonl").write_text(
        "\n".join(_codex_cwd_lines(str(t / "gone" / "x"))) + "\n")
    tree.update(t=t)
    return tree


def test_capture_probes_cwds_at_and_below_a_denied_directory_link_target(tmp_path):
    fr = _load("frozen_roots")
    tree = _target_tree(tmp_path)
    t = tree["t"]
    manifest = _capture(fr, tmp_path, tree)
    freeze = tmp_path / "freeze"
    probes = {e["logical"]: e for e in manifest["entries"] if e.get("class") == "probe"}
    assert sorted(probes) == sorted(str(p) for p in (
        t / ".git", t / "notes", t / "notes" / ".git", t / "notes" / "deep"))
    for path, e in probes.items():
        st = os.lstat(path)
        assert e["kind"] == "probe" and "physical" not in e
        assert e["root"] is None and e["deniedTarget"] == str(t), e
        assert (e["dev"], e["ino"], e["mode"], e["size"], e["mtimeNs"]) == (
            st.st_dev, st.st_ino, st.st_mode, st.st_size, st.st_mtime_ns), path
    assert probes[str(t / ".git")]["type"] == "file"
    assert probes[str(t / "notes" / ".git")]["type"] == "dir"
    assert probes[str(t / "notes" / "deep")]["type"] == "dir"
    absences = {a["logical"]: a["reason"] for a in manifest["absences"]}
    assert absences.get(str(t / "notes" / "deep" / ".git")) == "probe-absent"
    assert absences.get(str(t / "gone")) == "probe-absent"
    # the target itself keeps O1's presentation; nothing below an absence or a
    # `.git`, nothing copied, the target still denied
    assert str(t) not in probes and str(t) not in absences
    assert not any(k.startswith((str(t / "gone") + "/", str(t / "notes" / ".git") + "/"))
                   for k in list(probes) + list(absences))
    assert [d["logical"] for d in manifest["deniedTargets"]] == [str(t)]
    summary = manifest["probes"]
    assert summary["targetCwds"] == 3 and summary["cwds"] == 0, summary
    assert summary["targetEntries"] == 4 and summary["targetAbsences"] == 2, summary
    assert summary["entries"] == 4 and summary["absences"] == 2, summary
    tsv = _tsv(freeze)
    lines = {l.split("\t")[2]: l.split("\t") for l in tsv if l.startswith("E\tP\t")}
    assert sorted(lines) == sorted(probes)
    assert all(len(f) == 19 for f in lines.values())
    assert int(lines[str(t / ".git")][4]) == os.lstat(t / ".git").st_ino
    assert f"A\t{t / 'gone'}" in tsv and f"A\t{t / 'notes' / 'deep' / '.git'}" in tsv
    assert len(_tsv_fields(tsv, "X", t)) == 18
    assert fr.verify(freeze, full=True)["valid"] is True


def test_capture_takes_an_explicit_probe_below_a_denied_target_and_stops_at_a_link(
        tmp_path, capsys):
    fr = _load("frozen_roots")
    tree = _link_tree(tmp_path)
    t = tree["memory_target"]
    (t / "a" / "b").mkdir(parents=True)
    elsewhere = pathlib.Path(os.path.realpath(tmp_path)) / "elsewhere" / "wt"
    (elsewhere / "sub").mkdir(parents=True)
    os.symlink(str(elsewhere), t / "lnk")
    with open(tree["session"], "a") as fh:
        fh.write(json.dumps({"type": "user", "cwd": str(t / "lnk" / "sub")}) + "\n")
    store = _frozen_store(tmp_path / "store")
    code = fr.main(["capture", "--store", str(store), "--out", str(tmp_path / "freeze"),
                    "--home", str(tree["home"]), "--codex-home", "",
                    "--claude-config-dir", "", "--probe", str(t / "a" / "b"),
                    "--probe", str(t)])
    assert code == 0, capsys.readouterr().out
    manifest = json.loads((tmp_path / "freeze" / "manifest.json").read_text())
    probes = {e["logical"]: e for e in manifest["entries"] if e.get("class") == "probe"}
    absences = {a["logical"]: a["reason"] for a in manifest["absences"]}
    # an explicit probe: its components below the target, no `.git`
    assert probes[str(t / "a")]["type"] == "dir" and probes[str(t / "a" / "b")]["type"] == "dir"
    assert str(t / "a" / ".git") not in probes and str(t / "a" / ".git") not in absences
    assert manifest["probes"]["explicit"] == [str(t / "a" / "b"), str(t)]
    # the cwd walk inside the target: its `.git` absent, a link stops it
    assert absences.get(str(t / ".git")) == "probe-absent"
    e = probes[str(t / "lnk")]
    assert e["type"] == "symlink" and e["deniedTarget"] == str(t)
    assert e["target"] == str(elsewhere) and e["resolved"] == str(elsewhere)
    assert e["followed"]["ino"] == os.stat(elsewhere).st_ino
    assert not any(k.startswith(str(t / "lnk") + "/") for k in list(probes) + list(absences))
    assert manifest["probes"]["targetCwds"] == 1
    tsv = _tsv(tmp_path / "freeze")
    line = next(l.split("\t") for l in tsv if l.startswith(f"E\tP\t{t / 'lnk'}\t"))
    assert line[19:] == [str(elsewhere), str(elsewhere)]
    s = _tsv_fields(tsv, "S", t / "lnk")
    assert len(s) == 18 and int(s[3]) == os.stat(elsewhere).st_ino


def test_capture_records_nothing_else_below_a_denied_target_and_refuses_an_unreadable_probe(
        tmp_path, monkeypatch):
    """Below a denied target the namespace answers only a probe entry or a
    path under a probe absence; every other path there - the target's own
    files - has no record at all, so rootmap.c refuses it as a `traversal`.
    A probe the capture cannot stat is refused rather than guessed."""
    fr = _load("frozen_roots")
    tree = _target_tree(tmp_path)
    t = tree["t"]
    manifest = _capture(fr, tmp_path, tree)
    recorded = set()
    for line in _tsv(tmp_path / "freeze"):
        f = line.split("\t")
        path = f[2] if f[0] == "E" else f[1]
        if f[0] in ("E", "A", "S") and path.startswith(str(t) + "/"):
            assert f[0] != "E" or f[1] == "P", line
            recorded.add(path)
    probes = {e["logical"] for e in manifest["entries"] if e.get("class") == "probe"}
    absent = {a["logical"] for a in manifest["absences"]}
    assert recorded == {p for p in probes | absent if p.startswith(str(t) + "/")}
    assert str(t / "m.jsonl") not in recorded
    real_lstat = fr.os.lstat

    def lstat(path, *a, **k):
        if str(path) == str(t / "notes"):
            raise PermissionError(13, "Permission denied", str(path))
        return real_lstat(path, *a, **k)
    monkeypatch.setattr(fr.os, "lstat", lstat)
    with pytest.raises(fr.Refused, match="probe path cannot be stat'ed"):
        _capture(fr, tmp_path / "x", tree, store=_frozen_store(tmp_path / "x" / "store"))


def test_capture_records_absences_and_refuses_an_open_or_newer_store(tmp_path):
    fr = _load("frozen_roots")
    tree = _frozen_home(tmp_path)
    gone = tree["rollout"].parent / "gone.jsonl"
    store = _frozen_store(tmp_path / "store", rows=[
        ("session_files", gone, 10, 10, None)])
    manifest = _capture(fr, tmp_path, tree, store=store,
                        extras=[str(tree["home"] / ".codex" / "hooks.json")])
    absences = {a["logical"]: a["reason"] for a in manifest["absences"]}
    assert absences[str(gone)] == "retained-missing"
    assert absences[str(tree["home"] / ".codex" / "config.toml")] == "metadata-absent"
    assert absences[str(tree["home"] / ".codex" / "hooks.json")] == "extra-absent"
    assert absences[str(tree["home"] / ".config" / "claude" / "projects")] == "root-absent"
    tsv = (tmp_path / "freeze" / "rootmap.tsv").read_text()
    assert f"A\t{gone}\n" in tsv
    with pytest.raises(fr.Refused, match="not closed"):
        fr.capture(_frozen_store(tmp_path / "s2", wal=b"x" * 32), tmp_path / "f2",
                   home=str(tree["home"]), clock=_Ticks())
    with pytest.raises(fr.Refused, match="not older"):
        fr.capture(_frozen_store(tmp_path / "s3"), tmp_path / "f3",
                   home=str(tree["home"]), clock=_Ticks(), now=lambda: 1.0)


def test_the_manifest_feeds_the_namespace_and_the_sandbox_profile(tmp_path):
    fr = _load("frozen_roots")
    tree = _frozen_home(tmp_path)
    _capture(fr, tmp_path, tree)
    tsv = (tmp_path / "freeze" / "rootmap.tsv").read_text().splitlines()
    assert tsv[0] == "ROOTMAP\t2"
    roots = [l.split("\t") for l in tsv if l.startswith("R\t")]
    assert {r[1] for r in roots} >= {str(tree["home"] / ".codex"),
                                     str(tree["home"] / ".claude" / "projects"),
                                     str(tree["home"] / ".claude.json")}
    assert all(len(r) == 5 for r in roots)
    files = [l.split("\t") for l in tsv if l.startswith("E\tF\t")]
    assert all(len(f) == 19 for f in files)
    rollout = next(f for f in files if f[2] == str(tree["rollout"]))
    assert int(rollout[4]) == os.stat(tree["rollout"]).st_ino
    profile = (tmp_path / "freeze" / "frozen.sb").read_text()
    assert (f'(deny file-read* file-write* (subpath "{tree["home"] / ".codex"}") '
            "(with send-signal SIGKILL))") in profile
    assert (f'(literal "{tree["home"] / ".claude.json"}")') in profile
    assert f'(deny file-write* (subpath "{os.path.realpath(tmp_path / "freeze")}")' \
        in profile


def test_verify_detects_a_seal_mutation(tmp_path):
    fr = _load("frozen_roots")
    tree = _frozen_home(tmp_path)
    manifest = _capture(fr, tmp_path, tree)
    freeze = tmp_path / "freeze"
    assert fr.verify(freeze, full=True)["valid"] is True
    assert not os.access(freeze, os.W_OK)
    victim = freeze / _entry(manifest, tree["rollout"])["physical"]
    st = victim.stat()
    os.chmod(victim, 0o644)
    with open(victim, "r+b") as fh:
        fh.write(b"X")
    os.chmod(victim, 0o444)
    os.utime(victim, ns=(st.st_atime_ns, st.st_mtime_ns))
    result = fr.verify(freeze, full=True)
    assert result["valid"] is False
    assert any("contentDigest" in p for p in result["problems"])
    os.chmod(freeze / "roots" / "0", 0o755)
    (freeze / "roots" / "0" / "extra.jsonl").write_bytes(b"{}\n")
    assert any("metaDigest" in p for p in fr.verify(freeze)["problems"])
    os.chmod(freeze, 0o755)
    os.chmod(freeze / "rootmap.tsv", 0o644)
    (freeze / "rootmap.tsv").write_text("ROOTMAP\t1\n")
    assert any("rootmap.tsv changed" in p for p in fr.verify(freeze)["problems"])


def test_qualify_refuses_a_cursor_beyond_a_replacement_and_a_missing_file(tmp_path):
    fr = _load("frozen_roots")
    tree = _frozen_home(tmp_path)
    ino = os.stat(tree["rollout"]).st_ino
    gone = tree["rollout"].parent / "gone.jsonl"
    rows = [("codex_session_files", tree["rollout"], 16, 16, ino),
            ("session_files", gone, 5, 5, None)]
    store = _frozen_store(tmp_path / "store", rows=rows)
    _capture(fr, tmp_path, tree, store=store)
    freeze = tmp_path / "freeze"
    result = fr.qualify(store, freeze)
    assert result["qualified"] is False
    assert any("gone.jsonl is retained but absent" in p for p in result["problems"])
    (store / "prep-receipt.json").write_text(json.dumps(
        {"valid": True, "residual": [str(gone)]}))
    result = fr.qualify(store, freeze)
    assert result["qualified"] is True, result["problems"]
    assert result["residualAdmitted"] == 1
    assert result["freeze"]["sealSha256"]

    def tweak(sql, *params):
        for db in ("cache.db", "conversations.db"):
            conn = sqlite3.connect(store / "data" / db)
            try:
                conn.execute(sql, params)
                conn.commit()
            except sqlite3.OperationalError:
                pass
            conn.close()
            os.utime(store / "data" / db, (1_700_000_000, 1_700_000_000))
    tweak("UPDATE codex_session_files SET last_byte_offset = 99")
    result = fr.qualify(store, freeze)
    assert any("last_byte_offset 99 is beyond the frozen file" in p
               for p in result["problems"]), result["problems"]
    tweak("UPDATE codex_session_files SET last_byte_offset = 16, inode = ?",
          ino + 1)
    result = fr.qualify(store, freeze)
    assert any("replaced (stored inode" in p for p in result["problems"])
    tweak("UPDATE codex_session_files SET size_bytes = 50, inode = ?", ino)
    result = fr.qualify(store, freeze)
    assert any("size_bytes 50 is beyond" in p for p in result["problems"])


def test_qualify_admits_an_absent_row_that_retains_no_bytes(tmp_path):
    """A tracked row with zero size and a zero cursor (a transcript recorded
    while empty, its file later deleted) retains nothing the freeze could
    contradict, which is why catchup.py's preparation counts only positive
    sizes as tracked. Recorded absent by the capture, it is admitted and
    counted, not refused; an absent row with any nonzero value still is."""
    fr = _load("frozen_roots")
    tree = _frozen_home(tmp_path)
    empty = tree["rollout"].parent / "empty-gone.jsonl"
    store = _frozen_store(tmp_path / "store",
                          rows=[("session_files", empty, 0, 0, None)])
    _capture(fr, tmp_path, tree, store=store)
    result = fr.qualify(store, tmp_path / "freeze")
    assert result["qualified"] is True, result["problems"]
    assert result["retainedEmptyAbsent"] == 1
    assert result["residualAdmitted"] == 0
    (tmp_path / "b").mkdir()
    tree_b = _frozen_home(tmp_path / "b")
    cursor = tree_b["rollout"].parent / "cursor-gone.jsonl"
    store_b = _frozen_store(tmp_path / "b" / "store",
                            rows=[("session_files", cursor, 5, 0, None)])
    _capture(fr, tmp_path / "b", tree_b, store=store_b)
    result = fr.qualify(store_b, tmp_path / "b" / "freeze")
    assert result["qualified"] is False
    assert any("cursor-gone.jsonl is retained but absent" in p
               for p in result["problems"]), result["problems"]


def test_qualify_refuses_a_store_copy_the_freeze_was_not_captured_against(tmp_path):
    fr = _load("frozen_roots")
    tree = _frozen_home(tmp_path)
    store = _frozen_store(tmp_path / "store")
    _capture(fr, tmp_path, tree, store=store)
    conn = sqlite3.connect(store / "data" / "cache.db")
    conn.execute("CREATE TABLE later (x)")
    conn.commit()
    conn.close()
    result = fr.qualify(store, tmp_path / "freeze")
    assert result["qualified"] is False
    assert any("cache.db is not the store copy" in p for p in result["problems"])


# ── Amendment 11: one freeze per measurement session, bound to every copy ────


def _record(i):
    """One 64-byte JSONL record, so cursors at multiples of 64 are boundaries."""
    line = b'{"n":%04d,"p":"%s"}\n' % (i, b"x" * 46)
    assert len(line) == 64
    return line


def _session_home(tmp_path):
    """`_frozen_home` whose rollout (160 records, 10,240 bytes) is longer than
    either copy's cursor."""
    tree = _frozen_home(tmp_path)
    tree["rollout"].write_bytes(b"".join(_record(i) for i in range(160)))
    return tree


def _two_copies(tmp_path, tree, cursors=(4096, 6144)):
    """Two closed store copies of the same roots whose cursors on one rollout
    differ (the candidate's and the baseline's copies of one session). A
    retains a missing file its preparation receipt confirmed absent (a
    residual); B an empty row whose file is gone."""
    ino = os.stat(tree["rollout"]).st_ino
    gone = tree["rollout"].parent / "gone-a.jsonl"
    empty = tree["rollout"].parent / "empty-b.jsonl"
    a = _frozen_store(tmp_path / "copy-a", rows=[
        ("codex_session_files", tree["rollout"], cursors[0], cursors[0], ino),
        ("session_files", gone, 64, 64, None)])
    (a / "prep-receipt.json").write_text(json.dumps(
        {"valid": True, "residual": [str(gone)]}))
    b = _frozen_store(tmp_path / "copy-b", rows=[
        ("codex_session_files", tree["rollout"], cursors[1], cursors[1], ino),
        ("session_files", empty, 0, 0, None)])
    return a, b, gone, empty


def _capture_copies(fr, tmp_path, tree, stores, name="freeze"):
    return fr.capture(stores, tmp_path / name, home=str(tree["home"]),
                      codex_home=None, claude_config_dir=None, clock=_Ticks())


def test_one_freeze_bound_to_two_store_copies_qualifies_each_copy(tmp_path):
    fr = _load("frozen_roots")
    tree = _session_home(tmp_path)
    a, b, gone, empty = _two_copies(tmp_path, tree)
    manifest = _capture_copies(fr, tmp_path, tree, [a, b])
    freeze = tmp_path / "freeze"
    qa, qb = fr.qualify(a, freeze), fr.qualify(b, freeze)
    assert qa["qualified"] is True, qa["problems"]
    assert qb["qualified"] is True, qb["problems"]
    # each receipt names its own copy and the one shared freeze
    assert qa["store"]["root"] == os.path.realpath(a)
    assert qb["store"]["root"] == os.path.realpath(b)
    assert qa["freeze"] == qb["freeze"] == fr.seal_identity(freeze)
    assert qa["residualAdmitted"] == 1 and qb["retainedEmptyAbsent"] == 1
    # the manifest records every bound copy's identity, in --store order
    assert "store" not in manifest
    assert [s["root"] for s in manifest["stores"]] == [os.path.realpath(a),
                                                       os.path.realpath(b)]
    assert all(s["closed"] for s in manifest["stores"])
    # the prefix reaches the largest cursor any bound copy retains, and the
    # absent rows of every copy are recorded
    entry = next(e for e in manifest["entries"]
                 if e["logical"] == str(tree["rollout"]))
    assert entry["requiredOffset"] == 6144 and entry["admittedLength"] == 10240
    absences = {x["logical"]: x["reason"] for x in manifest["absences"]}
    assert absences[str(gone)] == absences[str(empty)] == "retained-missing"


def test_capture_takes_the_largest_cursor_of_any_bound_copy(tmp_path):
    """The union rule: B retains a cursor beyond the live rollout's last
    complete record, so a freeze bound to A alone captures, and one bound to
    A and B is refused naming B's cursor."""
    fr = _load("frozen_roots")
    tree = _session_home(tmp_path)
    a, b, _, _ = _two_copies(tmp_path, tree, cursors=(4096, 12800))
    _capture_copies(fr, tmp_path, tree, [a], name="freeze-a")
    with pytest.raises(fr.Refused, match="retained cursor/scan target 12800"):
        _capture_copies(fr, tmp_path, tree, [a, b], name="freeze-ab")


def test_capture_cli_binds_every_store_flag(tmp_path, capsys):
    fr = _load("frozen_roots")
    tree = _session_home(tmp_path)
    a, b, _, _ = _two_copies(tmp_path, tree)
    freeze = tmp_path / "freeze"
    code = fr.main(["capture", "--store", str(a), "--store", str(b),
                    "--out", str(freeze), "--home", str(tree["home"]),
                    "--codex-home", "", "--claude-config-dir", ""])
    assert code == 0, capsys.readouterr().out
    manifest = json.loads((freeze / "manifest.json").read_text())
    assert [s["root"] for s in manifest.get("stores") or []] == [
        os.path.realpath(a), os.path.realpath(b)]
    assert "2 store copies" in capsys.readouterr().out


def test_qualify_refuses_a_store_copy_the_freeze_is_not_bound_to(tmp_path):
    fr = _load("frozen_roots")
    tree = _session_home(tmp_path)
    a, b, _, _ = _two_copies(tmp_path, tree)
    _capture_copies(fr, tmp_path, tree, [a, b])
    # byte-identical databases to a bound copy, at another root: not bound
    import shutil
    c = tmp_path / "copy-c"
    shutil.copytree(a, c)
    result = fr.qualify(c, tmp_path / "freeze")
    assert result["qualified"] is False
    assert any("is not a store copy this freeze is bound to" in p
               for p in result["problems"]), result["problems"]
    assert fr.qualify(a, tmp_path / "freeze")["qualified"] is True
    # every bound copy must be closed, and a copy is bound once
    with pytest.raises(fr.Refused, match="copy-open.*not closed"):
        _capture_copies(fr, tmp_path, tree,
                        [a, _frozen_store(tmp_path / "copy-open", wal=b"x" * 32)],
                        name="freeze-open")
    with pytest.raises(fr.Refused, match="bound twice"):
        _capture_copies(fr, tmp_path, tree, [a, tmp_path / "copy-a"],
                        name="freeze-twice")


def test_a_copy_drained_after_the_capture_is_refused_for_that_copy_only(tmp_path):
    """A freeze captured before one copy's later drain: that copy's cursor is
    beyond the frozen file, so its qualification is refused; the other copy
    still qualifies against the same freeze."""
    fr = _load("frozen_roots")
    tree = _session_home(tmp_path)
    a, b, _, _ = _two_copies(tmp_path, tree)
    _capture_copies(fr, tmp_path, tree, [a, b])
    with open(tree["rollout"], "ab") as fh:
        fh.write(b"".join(_record(i) for i in range(160, 200)))
    conn = sqlite3.connect(b / "data" / "cache.db")
    conn.execute("UPDATE codex_session_files SET last_byte_offset = 12800, "
                 "size_bytes = 12800")
    conn.commit()
    conn.close()
    os.utime(b / "data" / "cache.db", (1_700_000_000, 1_700_000_000))
    qa, qb = fr.qualify(a, tmp_path / "freeze"), fr.qualify(b, tmp_path / "freeze")
    assert qa["qualified"] is True, qa["problems"]
    assert qb["qualified"] is False
    assert any("last_byte_offset 12800 is beyond the frozen file (10240 bytes)"
               in p for p in qb["problems"]), qb["problems"]


def test_b_pair_accepts_runs_on_two_copies_of_one_freeze_and_refuses_two_freezes(
        tmp_path, capsys):
    """The candidate's run uses copy A and the baseline's copy B: one freeze
    bound to both admits each copy's frozen catch-up and carries one label,
    so b-pair compares them; a second freeze is still mixed evidence."""
    fr, catchup, workload = (_load("frozen_roots"), _load("catchup"),
                             _load("workload"))
    tree = _session_home(tmp_path)
    a, b, _, _ = _two_copies(tmp_path, tree)
    manifest = _capture_copies(fr, tmp_path, tree, [a, b])
    seal = fr.seal_identity(tmp_path / "freeze")
    nothing = {"roots": {}, "rows": {}, "incarnations": {}}
    for copy in (a, b):
        qualification = fr.qualify(copy, tmp_path / "freeze")
        assert catchup.frozen_identity_problems(
            manifest, nothing, root_key=lambda path: "?",
            qualification=qualification, seal=seal,
            store_root=os.path.realpath(copy)) == [], copy
    cand = _frozen_evidence(_b_run(tmp_path, "cand-a", [1100, 2200, 3300, 4400]),
                            seal=seal["sealSha256"])
    base = _frozen_evidence(_b_run(tmp_path, "base-b", [1000, 2000, 3000, 4000]),
                            seal=seal["sealSha256"])
    assert workload.b_pair(str(cand), str(base)) == 0
    out = json.loads(capsys.readouterr().out.rsplit("\n", 2)[0])
    assert out["candidate"]["inputs"] == out["baseline"]["inputs"] == {
        "mode": "frozen", "freeze": seal["sealSha256"]}
    _capture_copies(fr, tmp_path, tree, [b], name="freeze-2")
    seal2 = fr.seal_identity(tmp_path / "freeze-2")["sealSha256"]
    assert seal2 != seal["sealSha256"]
    other = _frozen_evidence(_b_run(tmp_path, "base-b2", [1000, 2000, 3000, 4000]),
                             seal=seal2)
    assert workload.b_pair(str(cand), str(other)) == 2
    assert "mixed input evidence" in capsys.readouterr().out


def test_frozen_admission_needs_the_qualification_of_its_own_copy():
    """A shared freeze is qualified once per copy; a catch-up admits only the
    receipt that names the copy it was cloned from."""
    catchup = _load("catchup")
    mine = {"qualified": True, "freeze": {"sealSha256": "seal"},
            "store": {"root": "/copies/a"}}
    assert _identity(catchup, qualification=mine, store_root="/copies/a") == []
    problems = _identity(catchup, qualification=mine, store_root="/copies/b")
    assert any("names another store copy (/copies/a)" in p
               for p in problems), problems


def _receipt_line(**kw):
    base = {"event": "activate", "schema": "rootmap/1", "image": "load",
            "pid": 100, "ok": True, "manifestSha256": "abc",
            "enforced": [{"root": "/h/.codex", "denied": True}]}
    base.update(kw)
    return json.dumps(base)


def _toplevel(pid, *, status=0, step="step"):
    """One RECEIPTS/toplevel.jsonl row (Amendment 13 O3): how a top-level
    family process the runner waited on ended."""
    signaled = status > 128
    return json.dumps({"schema": "toplevel/1", "pid": pid, "status": status,
                       "exited": not signaled, "code": None if signaled else status,
                       "signaled": signaled,
                       "signal": status - 128 if signaled else None,
                       "runner": "run-x.sh", "step": step, "t": 1})


def _receipts(tmp_path, files, launches=(), ends=None):
    """A receipts directory; every launched pid ends normally unless `ends`
    (a list of toplevel.jsonl rows, or [] for none) says otherwise."""
    d = tmp_path / "receipts"
    d.mkdir()
    for name, lines in files.items():
        (d / name).write_text("\n".join(lines) + "\n")
    if launches:
        (d / "launches.jsonl").write_text("".join(
            json.dumps({"pid": p, "t": 1.0, "argv": ["x"]}) + "\n"
            for p in launches))
    rows = [_toplevel(p) for p in launches] if ends is None else ends
    if rows:
        (d / "toplevel.jsonl").write_text("\n".join(rows) + "\n")
    return d


@pytest.mark.parametrize("lines, launches, fragment", [
    ([_receipt_line()], [100], None),
    ([_receipt_line(manifestSha256="other")], [100], "different manifest"),
    ([_receipt_line(ok=False, error="manifest unreadable")], [100], "did not load"),
    ([_receipt_line(enforced=[{"root": "/h/.codex", "denied": False}])], [100],
     "absent enforcement"),
    ([_receipt_line(), json.dumps({"event": "unmapped", "pid": 100,
                                   "call": "stat", "path": "/h/.codex/x"})],
     [100], "unmapped stat /h/.codex/x"),
    ([_receipt_line(), json.dumps({"event": "write-denied", "pid": 100,
                                   "call": "open", "path": "/h/.codex/x"})],
     [100], "write-denied"),
    ([_receipt_line(), json.dumps({"event": "exec", "pid": 100,
                                   "target": "/opt/homebrew/bin/python3"})],
     [100], "without a later activation"),
    ([_receipt_line(), json.dumps({"event": "exec", "pid": 100,
                                   "target": "/bin/sh"})], [100],
     "without a complete launch record"),
    ([_receipt_line(), json.dumps({"event": "exec", "pid": 100,
                                   "target": "/opt/homebrew/bin/npm",
                                   "interpreter": "/usr/bin/env"})], [100],
     "without a complete launch record"),
    ([_receipt_line(), json.dumps({"event": "spawn", "pid": 100, "child": 7,
                                   "target": "/opt/homebrew/bin/python3"})],
     [100], "spawned /opt/homebrew/bin/python3 (pid 7)"),
    ([_receipt_line()], [100, 101], "pid 101 was launched"),
    # Amendment 13: an access of a non-traversed link's target or anything
    # under it, and an open or listing of a metadata-only probe entry
    ([_receipt_line(), json.dumps({"event": "traversal", "pid": 100,
                                   "call": "opendir", "path": "/h/.claude/p/memory"})],
     [100], "traversal opendir /h/.claude/p/memory"),
    ([_receipt_line(), json.dumps({"event": "unmapped-open", "pid": 100,
                                   "call": "open", "path": "/h/.codex/w/r/.git"})],
     [100], "unmapped-open open /h/.codex/w/r/.git"),
])
def test_the_family_verdict_requires_complete_enforced_receipts(
        tmp_path, lines, launches, fragment):
    fr = _load("frozen_roots")
    d = _receipts(tmp_path, {"100.1.2.jsonl": lines}, launches)
    result = fr.family_verdict(d, "abc")
    if fragment is None:
        assert result["valid"] is True, result["problems"]
    else:
        assert result["valid"] is False
        assert any(fragment in p for p in result["problems"]), result["problems"]


def test_the_family_verdict_refuses_an_expected_pid_and_an_empty_directory(tmp_path):
    fr = _load("frozen_roots")
    assert fr.family_verdict(tmp_path / "none", "abc")["valid"] is False
    d = _receipts(tmp_path, {"100.1.2.jsonl": [_receipt_line()]}, [100])
    assert fr.family_verdict(d, "abc", expect_pids=[100])["valid"] is True
    assert fr.family_verdict(d, "abc", expect_pids=[555])["valid"] is False


# ── revision 15 precision correction (Q17, `dc14` N2/D7): the system-program
# rule and swallowed kills ──────────────────────────────────────────────────


def _event(event, pid=100, **kw):
    return json.dumps({"event": event, "pid": pid, **kw})


def _reaped(child, *, parent=100, code=0, signal=None):
    return _event("reaped", parent, call="waitpid", child=child,
                  status=(signal if signal is not None else code << 8),
                  exited=signal is None, code=None if signal else code,
                  signaled=signal is not None, signal=signal)


_ENV_SPAWN = dict(target="/opt/homebrew/bin/npm", interpreter="/usr/bin/env",
                  argv=["npm", "prefix", "-g"], child=7, reinjected=False)


def test_a_listed_system_program_child_is_admitted(tmp_path):
    """Q17: a process started through a protected system program (here npm's
    `#!/usr/bin/env` shebang) cannot carry the namespace; it is admitted only
    with its parent's launch record and its recorded termination, and listed.
    The exec form (a forked, activated child that execs the program) is
    admitted the same way from its own exec record and its parent's wait."""
    fr = _load("frozen_roots")
    d = _receipts(tmp_path, {
        "100.1.2.jsonl": [_receipt_line(), _event("spawn", **_ENV_SPAWN),
                          _reaped(7), _reaped(101)],
        "101.1.3.jsonl": [_receipt_line(pid=101, image="fork", ppid=100),
                          _event("exec", 101, target="/usr/bin/env", ppid=100,
                                 argv=["/usr/bin/env", "true"])]}, [100])
    result = fr.family_verdict(d, "abc")
    assert result["valid"] is True, result["problems"]
    assert result["admittedSystemPrograms"] == [
        {"program": "/opt/homebrew/bin/npm", "interpreter": "/usr/bin/env",
         "argv": ["npm", "prefix", "-g"], "parent": 100, "child": 7,
         "via": "spawn", "termination": {"exited": True, "code": 0,
                                         "signaled": False, "signal": None}},
        {"program": "/usr/bin/env", "interpreter": None,
         "argv": ["/usr/bin/env", "true"], "parent": 100, "child": 101,
         "via": "exec", "termination": {"exited": True, "code": 0,
                                        "signaled": False, "signal": None}}]
    assert result["unloadedSystemImages"] == 2


@pytest.mark.parametrize("lines, fragment", [
    # a non-system image that never activated: the admitted list never
    # discharges it, even beside an admitted system program
    ([_event("spawn", target="/opt/homebrew/bin/python3", argv=["python3"],
             child=8), _reaped(8), _event("spawn", **_ENV_SPAWN), _reaped(7)],
     "spawned /opt/homebrew/bin/python3 (pid 8) without an activation receipt"),
    # a system program without its launch record
    ([_event("spawn", target="/usr/bin/env", child=7), _reaped(7)],
     "without a complete launch record"),
    # ... or without its termination status
    ([_event("spawn", **_ENV_SPAWN)], "without a recorded termination status"),
    # a library prefix is not a protected system program
    ([_event("spawn", target="/usr/lib/dyld", argv=["dyld"], child=7),
      _reaped(7)], "spawned /usr/lib/dyld (pid 7) without an activation receipt"),
])
def test_an_uncovered_or_unrecorded_process_is_invalid(tmp_path, lines, fragment):
    fr = _load("frozen_roots")
    d = _receipts(tmp_path, {"100.1.2.jsonl": [_receipt_line()] + lines}, [100])
    result = fr.family_verdict(d, "abc")
    assert result["valid"] is False
    assert any(fragment in p for p in result["problems"]), result["problems"]


def test_a_swallowed_signal_termination_invalidates_the_family(tmp_path):
    """The sandbox kills a violating process with SIGKILL and logs nothing; a
    parent that ignores its child's failure must not hide it. The parent's
    recorded wait shows the signal, covered child or admitted program alike."""
    fr = _load("frozen_roots")
    for sub in "abc":
        (tmp_path / sub).mkdir()
    covered = _receipts(tmp_path / "a", {
        "100.1.2.jsonl": [_receipt_line(), _event(
            "spawn", target="/opt/homebrew/bin/python3", argv=["python3"],
            child=7), _reaped(7, signal=9), _event("exit", how="exit",
                                                    counters={})],
        "7.1.3.jsonl": [_receipt_line(pid=7)]}, [100])
    result = fr.family_verdict(covered, "abc")
    assert result["valid"] is False
    assert any("pid 7" in p and "signal 9" in p and "no family member sent it"
               in p for p in result["problems"]), result["problems"]
    admitted = _receipts(tmp_path / "b", {"100.1.2.jsonl": [
        _receipt_line(), _event("spawn", **_ENV_SPAWN), _reaped(7, signal=9)]},
        [100])
    result = fr.family_verdict(admitted, "abc")
    assert result["valid"] is False
    assert any("signal 9" in p for p in result["problems"]), result["problems"]
    sent = _receipts(tmp_path / "c", {"100.1.2.jsonl": [
        _receipt_line(), _event("spawn", **_ENV_SPAWN),
        _event("signal-sent", call="kill", target=7, signal=15),
        _reaped(7, signal=15)]}, [100])
    result = fr.family_verdict(sent, "abc")
    # A termination a recorded family member sent itself (the product's own
    # subprocess timeout, for example `npm prefix -g` after 2 s) is not a
    # sandbox kill: it is listed, and the family stays valid.
    assert result["valid"] is True, result["problems"]
    assert result["familyKills"] == [
        {"pid": 7, "signal": 15, "reapedBy": 100, "sentBy": 100}]
    # A signal no family member sent stays fatal even when a family member
    # sent the same child a different one (a sandbox SIGKILL is not hidden).
    (tmp_path / "d").mkdir()
    masked = _receipts(tmp_path / "d", {"100.1.2.jsonl": [
        _receipt_line(), _event("spawn", **_ENV_SPAWN),
        _event("signal-sent", call="kill", target=7, signal=15),
        _reaped(7, signal=9)]}, [100])
    result = fr.family_verdict(masked, "abc")
    assert result["valid"] is False
    assert any("signal 9" in p and "no family member sent it" in p
               for p in result["problems"]), result["problems"]


def test_a_recorded_teardown_kill_is_distinguished(tmp_path):
    fr = _load("frozen_roots")
    d = _receipts(tmp_path, {
        "100.1.2.jsonl": [_receipt_line(), _event(
            "spawn", target="/opt/homebrew/bin/python3", argv=["python3"],
            child=7), _reaped(7, signal=9)],
        "7.1.3.jsonl": [_receipt_line(pid=7)]}, [100])
    (d / "teardown.jsonl").write_text(json.dumps(
        {"pid": 7, "start": 1, "signal": 9, "t": 2.0,
         "by": "frozen_roots.reap"}) + "\n")
    result = fr.family_verdict(d, "abc")
    assert result["valid"] is True, result["problems"]
    assert result["teardownKills"] == 1
    (d / "teardown.jsonl").write_text(json.dumps(
        {"pid": 7, "start": 1, "signal": 15, "t": 2.0,
         "by": "frozen_roots.reap"}) + "\n")            # another signal
    assert fr.family_verdict(d, "abc")["valid"] is False


# ── Amendment 13 O3: how each top-level family process ended ────────────────


def test_a_killed_top_level_process_invalidates_the_family(tmp_path):
    """A top-level family process's parent is the runner's shell, which the
    namespace does not load: before O3 a catch-up the sandbox killed with
    SIGKILL left frozen-family.json valid. The runner's record of how it
    ended (RECEIPTS/toplevel.jsonl) is judged like a member's termination."""
    fr = _load("frozen_roots")
    for sub in "abcde":
        (tmp_path / sub).mkdir()
    files = {"100.1.2.jsonl": [_receipt_line()]}
    killed = _receipts(tmp_path / "a", files, [100],
                       ends=[_toplevel(100, status=137, step="catchup")])
    result = fr.family_verdict(killed, "abc")
    assert result["valid"] is False
    assert any("top-level pid 100 (run-x.sh catchup)" in p and "signal 9" in p
               and "unexpectedly" in p for p in result["problems"]), result["problems"]
    assert result["signalTerminations"][0]["topLevel"] == {"runner": "run-x.sh",
                                                           "step": "catchup"}
    assert result["topLevel"] == {"records": 1, "signaled": 1, "unrecorded": []}
    # a kill the harness recorded at teardown is not unexpected
    torn = _receipts(tmp_path / "b", files, [100],
                     ends=[_toplevel(100, status=137, step="dashboard")])
    (torn / "teardown.jsonl").write_text(json.dumps(
        {"pid": 100, "signal": 9, "group": True, "t": 2, "by": "run-x.sh"}) + "\n")
    result = fr.family_verdict(torn, "abc")
    assert result["valid"] is True, result["problems"]
    assert result["teardownKills"] == 1
    # ... but only that signal
    (torn / "teardown.jsonl").write_text(json.dumps(
        {"pid": 100, "signal": 15, "t": 2, "by": "run-x.sh"}) + "\n")
    assert fr.family_verdict(torn, "abc")["valid"] is False
    # a normal exit, whatever its code, is the runner's business
    normal = _receipts(tmp_path / "c", files, [100], ends=[_toplevel(100, status=2)])
    assert fr.family_verdict(normal, "abc")["valid"] is True
    # a signal a recorded family member sent it is listed under familyKills
    sent = _receipts(tmp_path / "d", {
        **files, "200.1.2.jsonl": [_receipt_line(pid=200), _event(
            "signal-sent", 200, call="kill", target=100, signal=15)]}, [100, 200],
        ends=[_toplevel(100, status=143), _toplevel(200)])
    result = fr.family_verdict(sent, "abc")
    assert result["valid"] is True, result["problems"]
    assert [(k["pid"], k["signal"], k["sentBy"]) for k in result["familyKills"]] == [
        (100, 15, 200)]
    malformed = _receipts(tmp_path / "e", files, [100], ends=["{not json"])
    assert any("toplevel.jsonl" in p for p in
               fr.family_verdict(malformed, "abc")["problems"])


def test_a_launched_top_level_process_without_a_recorded_end_is_invalid(tmp_path):
    """Every process the runner launched through the launch contract
    (launches.jsonl) must have a record of how it ended, so a runner path
    that forgets to wait on one cannot hide a kill again."""
    fr = _load("frozen_roots")
    d = _receipts(tmp_path, {"100.1.2.jsonl": [_receipt_line()]}, [100], ends=[])
    result = fr.family_verdict(d, "abc")
    assert result["valid"] is False
    assert any("pid 100 was launched as a top-level family process but how it "
               "ended was not recorded" in p for p in result["problems"]), result["problems"]
    assert result["topLevel"]["unrecorded"] == [100]


def _wa_script(tmp_path, body, mode="frozen"):
    """Run BODY after sourcing _inputs.sh (frozen mode on a stand-in sealed
    freeze); returns (completed process, receipts directory)."""
    import subprocess as _sp
    freeze = tmp_path / "freeze"
    freeze.mkdir()
    for name in ("seal.json", "frozen.sb", "rootmap.tsv"):
        (freeze / name).write_text("{}")
    rec = tmp_path / "rec"
    script = tmp_path / "runner-x.sh"
    script.write_text(f'set -u\nP={TOOLS}\n. "$P/_inputs.sh"\nWA_RECEIPTS={rec}\n{body}\n')
    env = {**os.environ, "WRITE_ATTRIBUTION_INPUTS":
           f"frozen:{freeze}" if mode == "frozen" else "live", "WA_ALLOW_LIVE": "1"}
    out = _sp.run(["bash", str(script)], env=env, capture_output=True, text=True,
                  timeout=60)
    return out, rec


def _jsonl(path):
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def test_wa_wait_and_wa_kill_record_top_level_ends(tmp_path):
    out, rec = _wa_script(tmp_path, "\n".join([
        "( exec sleep 30 ) & p=$!",
        "wa_kill 9 $p; echo kill=$?",
        "wa_wait killed $p; echo killed=$?",
        "wa_kill 9 $p; echo again=$?",                     # gone: not recorded
        "( exit 3 ) & wa_wait plain $!; echo plain=$?",
        "( exec sleep 0.2 ) & q=$!; sleep 1; wa_wait late $q; echo late=$?",
        "set -m; ( exec sleep 30 ) & g=$!; set +m",
        "wa_kill 15 -$g; wa_wait group $g; echo group=$?",
        "( exec sleep 30 ) & z=$!; ( exec sleep 30 ) & y=$!",
        "kill -9 $z; sleep 0.5; wa_kill 9 $z; echo zombie=$?",   # dead: not recorded
        "wa_wait zombie $z; wa_kill 9 $y; wa_wait other $y; true",
    ]))
    assert out.returncode == 0, (out.stdout, out.stderr)
    assert out.stdout.split() == ["kill=0", "killed=137", "again=1", "plain=3",
                                  "late=0", "group=143", "zombie=1"], out.stdout
    ends = {r["step"]: r for r in _jsonl(rec / "toplevel.jsonl")}
    assert ends["killed"]["signaled"] is True and ends["killed"]["signal"] == 9
    assert ends["killed"]["status"] == 137 and ends["killed"]["runner"] == "runner-x.sh"
    assert (ends["plain"]["exited"], ends["plain"]["code"], ends["plain"]["signal"]) == (
        True, 3, None)
    assert ends["late"]["code"] == 0
    assert ends["group"]["signal"] == 15 and ends["zombie"]["signal"] == 9
    kills = [(r["pid"], r["signal"], r["group"]) for r in _jsonl(rec / "teardown.jsonl")]
    assert kills == [(ends["killed"]["pid"], 9, False), (ends["group"]["pid"], 15, True),
                     (ends["other"]["pid"], 9, False)]
    # live mode records nothing (no family verdict)
    (tmp_path / "live").mkdir()
    out, rec = _wa_script(tmp_path / "live", "( exit 4 ) & wa_wait x $!; echo rc=$?",
                          mode="live")
    assert out.stdout.strip() == "rc=4" and not rec.exists()


_FAMILY_RUNNERS = ("run-clone.sh", "run-latency.sh", "run-op.sh", "run-p1.sh",
                   "run-statements.sh", "run-workload.sh")


@pytest.mark.parametrize("name", _FAMILY_RUNNERS)
def test_every_runner_waits_on_and_kills_its_family_through_the_recorders(name):
    """O3: every top-level family process a runner starts (a `wa_exec`
    subshell) runs in the background and is waited on by `wa_wait`, and the
    runner's own kills go through `wa_kill`; a bare `wait` or `kill` on a
    family process would end it unrecorded (only the watchdogs' own
    subshells and `kill -0` liveness probes are exempt)."""
    import re
    text = (TOOLS / name).read_text()
    code = "\n".join(l.split(" #")[0] for l in text.splitlines()
                     if not l.lstrip().startswith("#"))
    code = re.sub(r"\\\n\s*", " ", code)            # join continued lines
    for m in re.finditer(r"\bwait\b[^\n]*", code):
        assert m.group(0).startswith(("wait \"$WATCH\"", "wait \"$watch\"")), m.group(0)
    for m in re.finditer(r"(?<![-\w])kill\b[^\n]*", code):
        assert m.group(0).startswith("kill -0"), m.group(0)
    assert "wa_exec" in code
    for m in re.finditer(r"wa_exec", code):
        depth, i = 0, m.start()
        while True:                                  # the closing ) of its subshell
            if code[i] == "(":
                depth += 1
            elif code[i] == ")":
                if depth == 0:
                    break
                depth -= 1
            i += 1
        tail = code[i + 1:code.index("\n", i + 1) if "\n" in code[i + 1:] else None]
        assert re.search(r"&\s*($|wa_wait)", tail.split("&&")[0]) or \
            tail.rstrip().endswith("&"), (name, code[m.start():i + 1 + len(tail)])
    assert code.count("wa_wait ") >= 1


def test_reap_records_its_teardown_kills(tmp_path):
    import subprocess
    fr = _load("frozen_roots")
    proc = subprocess.Popen(["/bin/sleep", "30"])
    try:
        start = fr._start_second(proc.pid)
        assert start is not None
        d = tmp_path / "receipts"
        d.mkdir()
        (d / f"{proc.pid}.{start}.0.jsonl").write_text(_receipt_line(pid=proc.pid) + "\n")
        assert fr.reap(d)["killed"] == [proc.pid]
        assert proc.wait(timeout=10) == -9
        records = [json.loads(l) for l in (d / "teardown.jsonl").read_text().splitlines()]
        assert [(r["pid"], r["start"], r["signal"], r["by"]) for r in records] == [
            (proc.pid, start, 9, "frozen_roots.reap")]
        assert fr.family_verdict(d, "abc")["valid"] is True   # not a receipt file
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


# ── revision 15 (Q16, `dc13` L4): one launch contract for every runner ──────


def test_p1_measured_children_keep_the_namespace_libraries(tmp_path, monkeypatch):
    """projection_replay.py ~540 replaced DYLD_INSERT_LIBRARIES with the write
    interposer alone, so a namespace a parent composed was lost in every
    measured child. The interposer must be appended to the composed list."""
    replay = _load("projection_replay")
    p1 = replay.P1.__new__(replay.P1)
    p1.args = type("A", (), {"dylib": "/x/wtrace.dylib"})()
    p1.tree = tmp_path
    p1.work = tmp_path
    p1.env = {"DYLD_INSERT_LIBRARIES": "/x/rootmap.dylib",
              "ROOTMAP_MANIFEST": "/f/rootmap.tsv"}
    seen = {}

    def fake_run(argv, env=None, **kw):
        seen["env"] = env
        result = pathlib.Path(argv[argv.index("--result") + 1])
        result.write_text(json.dumps({"pid": 1}))
        return type("R", (), {"returncode": 0, "stderr": ""})()
    monkeypatch.setattr(replay.subprocess, "run", fake_run)
    p1._step("conversations", case="no-append", rep=1, measured=True)
    libs = seen["env"]["DYLD_INSERT_LIBRARIES"].split(":")
    assert libs == ["/x/rootmap.dylib", "/x/wtrace.dylib"], libs
    assert seen["env"]["ROOTMAP_MANIFEST"] == "/f/rootmap.tsv"
    p1._step("cache", case="setup", rep=0, measured=False)
    assert seen["env"]["DYLD_INSERT_LIBRARIES"] == "/x/rootmap.dylib"


class _FakeHook:
    """subprocess.Popen stand-in for one hook-tick child."""
    seen = {}
    returncode_after = 0

    def __init__(self, argv, env=None, **kw):
        _FakeHook.seen["env"] = env
        self.pid, self.returncode = 77, None

    def communicate(self, data=None, timeout=None):
        self.returncode = _FakeHook.returncode_after
        return "", ""

    def kill(self):
        pass


def _hook_stubs(hooks, monkeypatch, returncode=0):
    _FakeHook.seen, _FakeHook.returncode_after = {}, returncode
    monkeypatch.setattr(hooks.subprocess, "Popen", _FakeHook)
    monkeypatch.setattr(hooks, "observe", lambda *a, **k: {"logBytes": 0})
    monkeypatch.setattr(hooks, "cursors", lambda *a, **k: {})
    monkeypatch.setattr(hooks, "traced_processes", lambda *a, **k: [])
    monkeypatch.setattr(hooks, "append_marked", lambda *a: {"bytes": 1})
    monkeypatch.setattr(hooks, "proof_of_ingest", lambda *a: [])
    monkeypatch.setattr(hooks, "reconcile", lambda *a: [])
    monkeypatch.setattr(hooks, "consumed", lambda *a: {})
    monkeypatch.setattr(hooks, "new_log_lines", lambda *a: [])


def test_the_hook_runner_records_how_each_hook_ended(tmp_path, monkeypatch):
    """O3: each hook-tick child's end goes to RECEIPTS/toplevel.jsonl too (a
    negative returncode is the signal that ended it)."""
    hooks = _load("hook_runner")
    _hook_stubs(hooks, monkeypatch, returncode=-9)
    rec = tmp_path / "rec"
    args = type("A", (), {"out": str(tmp_path), "dylib": "/x/wtrace.dylib",
                          "tree": tmp_path})()
    receipt = hooks.run_hook(3, args, {"DYLD_INSERT_LIBRARIES": "/x/rootmap.dylib",
                                       "ROOTMAP_RECEIPTS": str(rec)},
                             tmp_path, tmp_path / "r.jsonl")
    assert receipt["rc"] == -9
    (row,) = _jsonl(rec / "toplevel.jsonl")
    assert (row["pid"], row["signaled"], row["signal"], row["runner"], row["step"]) == (
        77, True, 9, "hook_runner.py", "hook-3")
    _hook_stubs(hooks, monkeypatch, returncode=0)
    hooks.run_hook(4, args, {"ROOTMAP_RECEIPTS": str(rec)}, tmp_path, tmp_path / "r.jsonl")
    row = _jsonl(rec / "toplevel.jsonl")[-1]
    assert (row["step"], row["exited"], row["code"]) == ("hook-4", True, 0)


def test_the_hook_runner_appends_the_interposer_to_the_namespace(tmp_path, monkeypatch):
    hooks = _load("hook_runner")
    _hook_stubs(hooks, monkeypatch)
    seen = _FakeHook.seen
    args = type("A", (), {"out": str(tmp_path), "dylib": "/x/wtrace.dylib",
                          "tree": tmp_path})()
    hooks.run_hook(1, args, {"DYLD_INSERT_LIBRARIES": "/x/rootmap.dylib"},
                   tmp_path, tmp_path / "r.jsonl")
    assert seen["env"]["DYLD_INSERT_LIBRARIES"] == "/x/rootmap.dylib:/x/wtrace.dylib"


_RUNNERS = {
    "run-p1.sh": ["/nonexistent-src", "tag", "/nonexistent-tree", "key", "candidate"],
    "run-workload.sh": ["A", "/nonexistent-src", "tag", "/nonexistent-tree", "9"],
    "run-clone.sh": ["/nonexistent-store", "tag", "1", "9", "/nonexistent-tree"],
    "run-op.sh": ["/nonexistent.db", "tag", "/nonexistent-tree", "--"],
    "run-p2.sh": ["/nonexistent-slice", "/nonexistent-src", "tag",
                  "/nonexistent-tree", "9"],
    "run-p3.sh": ["/nonexistent-slice", "/nonexistent-src", "tag",
                  "/nonexistent-tree", "9", "shipped"],
    "run-lifecycle.sh": ["/nonexistent-slice", "/nonexistent-src", "tag",
                         "/nonexistent-tree", "9"],
    "run-statements.sh": ["/nonexistent-store", "tag", "1", "9", "/nonexistent-tree"],
    "run-scratch-proof.sh": ["/nonexistent-src", "tag", "/nonexistent-tree", "9"],
    "run-latency.sh": ["/nonexistent-src", "tag", "/nonexistent-tree",
                       "--sync-passes", "5"],
}


def _runner(tmp_path, name, args, **env):
    import subprocess as _sp
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    base = {k: v for k, v in os.environ.items()
            if k not in ("WRITE_ATTRIBUTION_INPUTS", "WA_ALLOW_LIVE",
                         "WA_LIVE_ONLY")}
    base.update({"WRITE_ATTRIBUTION_SCRATCH": str(scratch), **env})
    out = _sp.run(["bash", str(TOOLS / name), *args], env=base,
                  capture_output=True, text=True, timeout=120)
    return out, scratch


@pytest.mark.parametrize("name", sorted(_RUNNERS))
def test_every_runner_refuses_to_start_without_an_input_mode(tmp_path, name):
    """Revision 15: a runner started without WRITE_ATTRIBUTION_INPUTS read
    whatever roots its environment named. It must refuse before it creates,
    builds or launches anything."""
    out, scratch = _runner(tmp_path, name, _RUNNERS[name])
    assert out.returncode == 2, (out.stdout, out.stderr)
    assert "WRITE_ATTRIBUTION_INPUTS" in out.stderr, out.stderr
    assert list(scratch.iterdir()) == []


@pytest.mark.parametrize("name", sorted(set(_RUNNERS) - {"run-scratch-proof.sh"}))
def test_a_frozen_capable_runner_refuses_live_inputs_without_the_flag(tmp_path, name):
    out, scratch = _runner(tmp_path, name, _RUNNERS[name],
                           WRITE_ATTRIBUTION_INPUTS="live")
    assert out.returncode == 2, (out.stdout, out.stderr)
    assert "--live" in out.stderr, out.stderr
    assert list(scratch.iterdir()) == []


@pytest.mark.parametrize("name, args", [
    ("run-workload.sh", ["L", "/nonexistent-src", "tag", "/nonexistent-tree", "9"]),
    ("run-scratch-proof.sh", _RUNNERS["run-scratch-proof.sh"]),
])
def test_a_live_only_runner_refuses_frozen_inputs(tmp_path, name, args):
    out, scratch = _runner(tmp_path, name, args,
                           WRITE_ATTRIBUTION_INPUTS=f"frozen:{tmp_path}")
    assert out.returncode == 2, (out.stdout, out.stderr)
    assert "live" in out.stderr, out.stderr
    assert list(scratch.iterdir()) == []


def test_run_live_takes_the_live_only_contract_before_anything_else():
    """run-live.sh measures the INSTALLED dashboard on the real store, so the
    test never executes it: its first command after P must be the live-only
    input contract, which the next test executes on its own."""
    lines = [l for l in (TOOLS / "run-live.sh").read_text().splitlines()
             if l.strip() and not l.lstrip().startswith("#")]
    first = next(i for i, l in enumerate(lines) if l.startswith("P="))
    assert lines[first + 1] == 'WA_LIVE_ONLY=1 . "$P/_inputs.sh"', lines[first + 1]


def test_the_live_only_contract_refuses_frozen_inputs(tmp_path):
    import subprocess as _sp
    script = tmp_path / "x.sh"
    script.write_text(f'P={TOOLS}\nWA_LIVE_ONLY=1 . "$P/_inputs.sh"\necho STARTED\n')
    env = {**os.environ, "WRITE_ATTRIBUTION_INPUTS": f"frozen:{tmp_path}"}
    out = _sp.run(["bash", str(script)], env=env, capture_output=True, text=True,
                  timeout=30)
    assert out.returncode == 2 and "STARTED" not in out.stdout
    assert "live roots only" in out.stderr
    env["WRITE_ATTRIBUTION_INPUTS"] = "live"
    out = _sp.run(["bash", str(script)], env=env, capture_output=True, text=True,
                  timeout=30)
    assert out.returncode == 0 and "STARTED" in out.stdout


def test_a_frozen_runner_refuses_a_directory_that_is_not_a_sealed_freeze(tmp_path):
    out, scratch = _runner(tmp_path, "run-p1.sh", _RUNNERS["run-p1.sh"],
                           WRITE_ATTRIBUTION_INPUTS=f"frozen:{tmp_path}")
    assert out.returncode == 2
    assert "not a sealed freeze" in out.stderr, out.stderr
    assert list(scratch.iterdir()) == []


@pytest.mark.parametrize("tool, argv", [
    ("catchup.py", ["--tree", "/nonexistent", "--out", "OUT"]),
    ("hook_runner.py", ["--tree", "/n", "--root", "/n", "--out", "/n",
                        "--dylib", "/n"]),
    ("terminal_drain.py", ["--data", "/n", "--terminal", "OUT"]),
    ("projection_replay.py", ["p1", "--tree", "/n", "--db", "/n",
                              "--conversation", "k", "--out", "OUT"]),
    ("maintenance_op.py", ["--tree", "/n", "--db", "/n"]),
    ("latency.py", ["--tree", "/n", "--out", "OUT", "--sync-passes", "1",
                    "--root", "/n"]),
])
def test_a_harness_process_refuses_frozen_inputs_without_the_namespace(
        tmp_path, tool, argv):
    """An omitted namespace in a catch-up, a hook runner, a copier or P1
    makes the family invalid: the process refuses before reading anything."""
    import subprocess as _sp
    import sys as _sys
    argv = [str(tmp_path / "out.json") if a == "OUT" else a for a in argv]
    env = {**os.environ, "WRITE_ATTRIBUTION_INPUTS": f"frozen:{tmp_path}"}
    env.pop("DYLD_INSERT_LIBRARIES", None)
    out = _sp.run([_sys.executable, str(TOOLS / tool), *argv], env=env,
                  capture_output=True, text=True, timeout=60)
    assert out.returncode == 2, (out.stdout, out.stderr)
    assert "namespace" in (out.stdout + out.stderr), (out.stdout, out.stderr)


def test_the_ambient_predicate_still_refuses_in_frozen_mode(monkeypatch):
    """Frozen inputs cannot grow, so an ambient pass means the namespace
    leaked; the P1 predicate (~560) is unchanged and still trips."""
    monkeypatch.setenv("WRITE_ATTRIBUTION_INPUTS", "frozen:/f")
    replay = _load("projection_replay")
    p1 = replay.P1.__new__(replay.P1)
    p1.passes = []
    result = {"interposer": None, "stats": {"files_processed": 0},
              "ops": {"deleted": 0, "inserted": 0, "updated": 0},
              "opsAll": {"deleted": 0, "inserted": 3, "updated": 0},
              "generationBefore": 1, "generationAfter": 1, "wallS": 1,
              "cpuS": 1, "constructionS": 0, "comparisonS": 0, "commitS": 0,
              "timing": {}, "flockHoldS": 0, "footprintPeakBytes": 1}
    record = p1._record("no-append", 1, result, expected_files=0)
    assert record["ambient"] is True


# ── revision 15 precision correction (Q17, `dc14` N1): the derived Codex ────
# state database. Its main file and -wal are one stability unit read raw; only
# an external scratch copy of the admitted pair is opened by SQLite, recovered,
# checkpointed to a single rollback-mode file and sealed.


class _CodexState:
    """A synthetic Codex state_5.sqlite written the way Codex writes it: a WAL
    database whose writer never checkpoints on its own, optionally with a
    reader that pins the WAL (so a PASSIVE checkpoint stays incomplete)."""

    def __init__(self, path):
        self.path = pathlib.Path(path)
        self.w = sqlite3.connect(self.path, isolation_level=None)
        self.w.execute("PRAGMA journal_mode=WAL")
        self.w.execute("PRAGMA wal_autocheckpoint=0")
        self.w.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT)")
        self.w.execute("CREATE TABLE other (v TEXT)")
        self.w.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.reader = None

    def commit(self, sql, *args):
        self.w.execute(sql, args)

    def checkpoint(self, mode):
        return tuple(self.w.execute(f"PRAGMA wal_checkpoint({mode})").fetchone())

    def pin(self):
        self.reader = sqlite3.connect(self.path, isolation_level=None)
        self.reader.execute("BEGIN")
        self.reader.execute("SELECT count(*) FROM threads").fetchone()

    def unpin(self):
        if self.reader is not None:
            self.reader.execute("COMMIT")
            self.reader.close()
            self.reader = None

    def close(self):
        self.unpin()
        self.w.close()

    @property
    def wal(self):
        return pathlib.Path(f"{self.path}-wal")


def _state_path(tree):
    return tree["home"] / ".codex" / "state_5.sqlite"


def _read_derived(path, sql="SELECT id, title FROM threads ORDER BY id", params=()):
    """The product's read (bin/_cctally_dashboard_sources.py ~3096): a raw
    `?mode=ro` connection with a 50 ms timeout."""
    ro = sqlite3.connect(f"{pathlib.Path(path).as_uri()}?mode=ro", uri=True,
                         timeout=0.05)
    try:
        return ro.execute(sql, params).fetchall()
    finally:
        ro.close()


def _sha(data):
    return __import__("hashlib").sha256(data).hexdigest()


def test_a_codex_state_title_committed_only_in_the_wal_enters_the_freeze(tmp_path):
    """dc14 N1's RED: a title committed only in the WAL (a reader pins it, so
    a PASSIVE checkpoint stays incomplete) must survive the capture; a later
    live change must not. The header patch kept only the main file."""
    fr = _load("frozen_roots")
    tree = _frozen_home(tmp_path)
    state = _state_path(tree)
    db = _CodexState(state)
    db.commit("INSERT INTO threads VALUES ('t1', 'Checkpointed title')")
    assert db.checkpoint("TRUNCATE")[0] == 0
    db.pin()
    db.commit("UPDATE threads SET title = 'WAL title' WHERE id = 't1'")
    busy, log, done = db.checkpoint("PASSIVE")
    assert busy == 0 and log > 0 and done < log        # incomplete: WAL-only commit
    live = os.stat(state)
    wal_bytes = db.wal.read_bytes()
    try:
        manifest = _capture(fr, tmp_path, tree)
    finally:
        db.unpin()
    db.commit("UPDATE threads SET title = 'LIVE-SENTINEL' WHERE id = 't1'")
    db.close()
    entry = _entry(manifest, state)
    frozen = tmp_path / "freeze" / entry["physical"]
    assert _read_derived(frozen, "SELECT id, title FROM threads WHERE id IN (?)",
                         ("t1",)) == [("t1", "WAL title")]
    # the namespace identity is the source main file's; length and bytes the
    # derived file's
    assert entry["class"] == "sqlite-derived"
    assert (entry["dev"], entry["ino"], entry["mtimeNs"], entry["ctimeNs"]) == (
        live.st_dev, live.st_ino, live.st_mtime_ns, live.st_ctime_ns)
    raw = frozen.read_bytes()
    assert entry["admittedLength"] == len(raw) == entry["derived"]["length"]
    assert entry["sha256"] == _sha(raw) == entry["derived"]["sha256"]
    assert raw[18:20] == b"\x01\x01"                    # a rollback-mode file
    # source provenance, separate from the derived file's
    source = entry["source"]
    assert source["main"]["logical"] == str(state)
    assert source["main"]["ino"] == live.st_ino
    assert source["main"]["size"] == live.st_size == entry["sourceSize"]
    assert source["wal"]["present"] is True
    assert source["wal"]["size"] == len(wal_bytes)
    assert source["wal"]["sha256"] == _sha(wal_bytes)
    assert source["shm"]["copied"] is False and source["shm"]["used"] is False
    assert source["journal"]["present"] is False
    assert source["attempts"] == 1 and source["captureInterval"]["seconds"] >= 0
    derived = entry["derived"]
    assert derived["path"] == entry["physical"]
    recovery = derived["recovery"]
    assert recovery["integrityCheck"] == "ok"
    assert recovery["journalMode"] == "delete"
    assert recovery["walCheckpoint"][0] == 0
    assert recovery["walCheckpoint"][1] == recovery["walCheckpoint"][2]
    assert recovery["sqlite"] == sqlite3.sqlite_version
    # derived sidecar absences, recorded apart from the source observations
    absences = {a["logical"]: a["reason"] for a in manifest["absences"]}
    sidecars = {f"{state}{side}" for side in ("-wal", "-shm", "-journal")}
    assert {p: absences.get(p) for p in sidecars} == dict.fromkeys(
        sidecars, "derived-sidecar")
    assert set(derived["sidecarsAbsent"]) == sidecars
    assert sorted(p.name for p in frozen.parent.iterdir()
                  if p.name.startswith("state_5")) == ["state_5.sqlite"]
    assert not (tmp_path / "freeze" / ".sqlite-derive").exists()
    # the namespace manifest presents that identity with the derived length
    line = next(l.split("\t") for l in (tmp_path / "freeze" / "rootmap.tsv")
                .read_text().splitlines() if l.startswith("E\tF\t")
                and l.split("\t")[2] == str(state))
    assert [int(x) for x in line[3:6]] == [live.st_dev, live.st_ino, len(raw)]
    for side in ("-wal", "-shm", "-journal"):
        assert f"A\t{state}{side}" in (tmp_path / "freeze" / "rootmap.tsv").read_text()


def test_a_partially_backfilled_state_database_keeps_its_latest_commit(tmp_path):
    fr = _load("frozen_roots")
    tree = _frozen_home(tmp_path)
    state = _state_path(tree)
    db = _CodexState(state)
    db.commit("INSERT INTO threads VALUES ('t1', 'one')")
    db.pin()                                   # the reader's snapshot: 'one'
    db.commit("UPDATE threads SET title = 'two' WHERE id = 't1'")
    busy, log, done = db.checkpoint("PASSIVE")
    assert busy == 0 and 0 < done < log        # 'one' backfilled, 'two' only in the WAL
    try:
        manifest = _capture(fr, tmp_path, tree)
    finally:
        db.close()
    entry = _entry(manifest, state)
    assert _read_derived(tmp_path / "freeze" / entry["physical"]) == [("t1", "two")]


def _independent_capture(fr, state, out, mutate):
    """The per-file method a pair needs more than: each file copied by its own
    stable whole read, with `mutate` between the two reads."""
    out.mkdir()
    deadline = 1e9
    main = fr.copy_whole(str(state), str(out / "state_5.sqlite"), deadline=deadline)
    mutate()
    wal = fr.copy_whole(f"{state}-wal", str(out / "state_5.sqlite-wal"),
                        deadline=deadline)
    conn = sqlite3.connect(out / "state_5.sqlite")
    try:
        rows = conn.execute("SELECT v FROM other ORDER BY v").fetchall()
        titles = conn.execute("SELECT title FROM threads").fetchall()
    finally:
        conn.close()
    return main["attempts"], wal["attempts"], rows, titles


def _committed_history(db):
    db.commit("INSERT INTO other VALUES ('kept')")
    db.commit("INSERT INTO threads VALUES ('t1', 'one')")
    for i in range(6):                     # a WAL high-water mark above one update
        db.commit("INSERT INTO other VALUES (?)", f"pad{i}")


def _checkpointed_between_reads(db):
    def mutate():
        assert db.checkpoint("TRUNCATE")[0] == 0     # main written, WAL emptied
    return mutate


def _wal_reused_at_constant_length(db):
    def mutate():
        size, salts = db.wal.stat().st_size, db.wal.read_bytes()[16:24]
        assert db.checkpoint("RESTART")[0] == 0      # main written, WAL restarts
        db.commit("UPDATE threads SET title = 'two' WHERE id = 't1'")
        assert db.wal.stat().st_size == size             # same length ...
        assert db.wal.read_bytes()[16:24] != salts       # ... new salts
    return mutate


@pytest.mark.parametrize("scenario", [_checkpointed_between_reads,
                                      _wal_reused_at_constant_length])
def test_independent_file_captures_cannot_certify_the_state_pair(tmp_path, scenario):
    """dc14 N1's mutation seams: each file is stable during its own read, so a
    per-file capture certifies both, yet the pair it took loses a committed
    row; the pair capture sees the main file change across its interval and
    retries the whole pair."""
    fr = _load("frozen_roots")
    tree = _frozen_home(tmp_path / "a")
    state = _state_path(tree)
    db = _CodexState(state)
    _committed_history(db)
    try:
        main_attempts, wal_attempts, rows, _ = _independent_capture(
            fr, state, tmp_path / "independent", scenario(db))
    finally:
        db.close()
    assert (main_attempts, wal_attempts) == (1, 1)     # each file "certified"
    assert ("kept",) not in rows                       # a committed row lost

    tree = _frozen_home(tmp_path / "b")
    state = _state_path(tree)
    db = _CodexState(state)
    _committed_history(db)
    mutate, fired = scenario(db), []

    def seam(path, stage, attempt):
        if path == str(state) and stage == "main" and attempt == 1:
            fired.append(stage)
            mutate()
    try:
        manifest = _capture(fr, tmp_path / "b", tree, on_pair=seam)
    finally:
        db.close()
    entry = _entry(manifest, state)
    assert fired == ["main"] and entry["source"]["attempts"] == 2
    frozen = tmp_path / "b" / "freeze" / entry["physical"]
    rows = _read_derived(frozen, "SELECT v FROM other ORDER BY v")
    assert ("kept",) in rows and len(rows) == 7


def test_the_state_pair_retries_a_replaced_main_file(tmp_path):
    fr = _load("frozen_roots")
    tree = _frozen_home(tmp_path)
    state = _state_path(tree)
    db = _CodexState(state)
    db.commit("INSERT INTO threads VALUES ('t1', 'one')")
    db.close()
    before = os.stat(state).st_ino

    def seam(path, stage, attempt):
        if stage == "main" and attempt == 1:
            copy = tmp_path / "replacement"
            copy.write_bytes(state.read_bytes())
            os.replace(copy, state)
    manifest = _capture(fr, tmp_path, tree, on_pair=seam)
    entry = _entry(manifest, state)
    assert entry["source"]["attempts"] == 2
    assert entry["ino"] == os.stat(state).st_ino != before
    assert "replaced" in " ".join(entry["source"]["retries"])


def test_the_state_pair_retries_a_wal_that_appears_or_disappears(tmp_path):
    fr = _load("frozen_roots")
    tree = _frozen_home(tmp_path / "a")
    state = _state_path(tree)
    db = _CodexState(state)
    db.commit("INSERT INTO threads VALUES ('t1', 'one')")
    db.close()                                # the last close removes the WAL
    assert not os.path.exists(f"{state}-wal")

    def appear(path, stage, attempt):
        if stage == "main" and attempt == 1:
            pathlib.Path(f"{state}-wal").write_bytes(b"")
    manifest = _capture(fr, tmp_path / "a", tree, on_pair=appear)
    entry = _entry(manifest, state)
    assert entry["source"]["attempts"] == 2
    assert entry["source"]["wal"]["present"] is True
    assert _read_derived(tmp_path / "a" / "freeze" / entry["physical"]) == [("t1", "one")]

    tree = _frozen_home(tmp_path / "b")
    state = _state_path(tree)
    db = _CodexState(state)
    db.commit("INSERT INTO threads VALUES ('t1', 'one')")
    assert db.checkpoint("TRUNCATE")[0] == 0

    def vanish(path, stage, attempt):
        if stage == "copied" and attempt == 1:
            os.unlink(f"{state}-wal")
    try:
        manifest = _capture(fr, tmp_path / "b", tree, on_pair=vanish)
    finally:
        db.close()
    entry = _entry(manifest, state)
    assert entry["source"]["attempts"] == 2
    assert entry["source"]["wal"] == {"logical": f"{state}-wal", "present": False,
                                      "stableAbsence": True}


def test_the_state_pair_refuses_when_it_never_holds_still(tmp_path):
    fr = _load("frozen_roots")
    tree = _frozen_home(tmp_path)
    state = _state_path(tree)
    db = _CodexState(state)
    db.commit("INSERT INTO threads VALUES ('t1', 'one')")

    def churn(path, stage, attempt):
        if stage == "copied":
            with open(f"{state}-wal", "ab") as fh:
                fh.write(b"\0" * 8)
    try:
        with pytest.raises(fr.Refused, match="retry deadline expired"):
            fr.capture(_frozen_store(tmp_path / "store"), tmp_path / "freeze",
                       home=str(tree["home"]), clock=_Ticks(), on_pair=churn,
                       retry_deadline_s=4)
    finally:
        db.close()
    assert not (tmp_path / "freeze" / ".sqlite-derive").exists()


def test_the_state_pair_refuses_a_failed_recovery_and_a_nonempty_journal(tmp_path):
    fr = _load("frozen_roots")
    tree = _frozen_home(tmp_path / "a")
    _state_path(tree).write_bytes(b"this is not a database\n" * 64)
    with pytest.raises(fr.Refused, match="recovery"):
        _capture(fr, tmp_path / "a", tree)

    tree = _frozen_home(tmp_path / "b")
    state = _state_path(tree)
    db = _CodexState(state)
    db.commit("INSERT INTO threads VALUES ('t1', 'one')")
    db.close()
    journal = pathlib.Path(f"{state}-journal")
    journal.write_bytes(b"")                  # an empty journal is no evidence
    manifest = _capture(fr, tmp_path / "b", tree)
    assert _entry(manifest, state)["source"]["journal"] == {
        "logical": str(journal), "present": True, "size": 0}
    journal.write_bytes(b"a hot rollback journal")
    with pytest.raises(fr.Refused, match="rollback journal"):
        fr.capture(_frozen_store(tmp_path / "b" / "store2"),
                   tmp_path / "b" / "freeze2", home=str(tree["home"]),
                   clock=_Ticks())


def test_a_derived_state_database_may_differ_in_size_from_its_source(tmp_path):
    fr = _load("frozen_roots")
    tree = _frozen_home(tmp_path)
    state = _state_path(tree)
    db = _CodexState(state)
    db.commit("BEGIN")                         # new pages only in the WAL
    for i in range(300):
        db.commit("INSERT INTO threads VALUES (?, ?)", f"t{i:03d}", "x" * 500)
    db.commit("COMMIT")
    source_size = os.stat(state).st_size
    try:
        manifest = _capture(fr, tmp_path, tree)
    finally:
        db.close()
    entry = _entry(manifest, state)
    frozen = tmp_path / "freeze" / entry["physical"]
    assert entry["source"]["main"]["size"] == source_size == entry["sourceSize"]
    assert entry["admittedLength"] == frozen.stat().st_size > source_size
    assert len(_read_derived(frozen)) == 300
    line = next(l.split("\t") for l in (tmp_path / "freeze" / "rootmap.tsv")
                .read_text().splitlines() if l.startswith("E\tF\t")
                and l.split("\t")[2] == str(state))
    assert int(line[5]) == frozen.stat().st_size


# ── revision 15 (Q16, `dc13` L1/L3): frozen admission and the live frontier ──

_FA = "/h/.codex/sessions/2026/10/05/a.jsonl"
_FB = "/h/.claude/projects/-p/b.jsonl"
_FNEW = "/h/.codex/sessions/2026/10/05/new.jsonl"


def _row(table, path, **cols):
    return {f"{table}\t{path}": {"table": table, "path": path, **cols}}


def _state(*rows, roots=None, incarnations=None):
    merged = {}
    for r in rows:
        merged.update(r)
    return {"rows": merged, "roots": dict(roots or {}),
            "incarnations": dict(incarnations or {})}


def _frozen_manifest(**over):
    entries = [
        {"logical": _FA, "kind": "file", "class": "transcript", "ino": 11,
         "admittedLength": 300},
        {"logical": _FB, "kind": "file", "class": "transcript", "ino": 12,
         "admittedLength": 200},
        {"logical": _FNEW, "kind": "file", "class": "transcript", "ino": 13,
         "admittedLength": 50},
    ]
    manifest = {"roots": [{"logical": "/h/.codex", "kind": "codex-home"},
                          {"logical": "/h/.claude/projects",
                           "kind": "claude-projects"}],
                "entries": entries, "absences": []}
    manifest.update(over)
    return manifest


def _frozen_before():
    return _state(
        _row("codex_session_files", _FA, last_byte_offset=200, size_bytes=200,
             inode=11, source_root_key="rk", account_key="acct"),
        _row("session_files", _FB, last_byte_offset=200, size_bytes=200),
        _row("conversation_source_files", _FB, last_byte_offset=200,
             size_bytes=200, inode=12, source_incarnation_id="inc-b",
             committed_prefix_sha256="d"),
        _row("codex_session_files", "/clone/scratch/codex/s.jsonl",
             last_byte_offset=5, size_bytes=5, inode=99, source_root_key="sk"),
        roots={"rk": "/h/.codex/sessions", "sk": "/clone/scratch/codex/sessions"},
        incarnations={"fk-a": 1})


_QUALIFIED = {"qualified": True, "freeze": {"sealSha256": "seal"}}


def _identity(catchup, before=None, manifest=None, qualification=_QUALIFIED, **kw):
    return catchup.frozen_identity_problems(
        manifest or _frozen_manifest(), before or _frozen_before(),
        root_key=lambda path: "rk" if path == "/h/.codex/sessions" else "?",
        qualification=qualification, seal={"sealSha256": "seal"}, **kw)


def test_frozen_admission_accepts_identities_that_match_the_freeze():
    catchup = _load("catchup")
    assert _identity(catchup) == []
    expected = catchup.expected_consumption(_frozen_manifest(), _frozen_before())
    assert expected == {_FA: 100, _FB: 0, _FNEW: 50}


@pytest.mark.parametrize("mutate, fragment", [
    (lambda s, m: s["rows"][f"codex_session_files\t{_FA}"].update(inode=77),
     "identity"),
    (lambda s, m: s["rows"].update(_row("session_files", "/h/.claude/projects/-p/x.jsonl",
                                        last_byte_offset=1, size_bytes=1)),
     "not in the freeze"),
    (lambda s, m: s["rows"][f"codex_session_files\t{_FA}"].update(
        last_byte_offset=400), "beyond the frozen file"),
    (lambda s, m: s["roots"].update(rk="/elsewhere/.codex/sessions"),
     "root key"),
    (lambda s, m: s["rows"][f"codex_session_files\t{_FA}"].update(
        source_root_key="relocated"), "root key"),
])
def test_frozen_admission_refuses_an_identity_mismatch(mutate, fragment):
    catchup = _load("catchup")
    state, manifest = _frozen_before(), _frozen_manifest()
    mutate(state, manifest)
    problems = _identity(catchup, before=state, manifest=manifest)
    assert any(fragment in p for p in problems), problems


_FT = "/repos/x/.codex/sessions/2026/10/04/t.jsonl"   # outside every root
_FL = "/h/.codex/sessions/2026/10/04/t.jsonl"         # the covered link


def _linked_manifest(link=_FL):
    manifest = _frozen_manifest()
    manifest["roots"].append({"logical": _FT, "kind": "file",
                              "origin": "link-target", "links": [link]})
    manifest["entries"] += [
        {"logical": link, "kind": "symlink", "class": "symlink", "target": _FT,
         "resolved": _FT, "targetScope": "outside-roots"},
        {"logical": _FT, "kind": "file", "class": "transcript", "ino": 14,
         "admittedLength": 40}]
    return manifest


def test_frozen_admission_checks_a_linked_rollout_under_its_link_spelling():
    """Amendment 10: the tree keeps the WALKED link path as a linked rollout's
    cursor row (its file identity key alone uses the canonical target), with
    the target's inode; admission checks that row against the frozen target,
    and the drain consumes the target's suffix under the link's spelling, so
    the out-of-root target is never counted again as a file of its own."""
    catchup = _load("catchup")
    state = _frozen_before()
    state["rows"].update(_row("codex_session_files", _FL, last_byte_offset=30,
                              size_bytes=30, inode=14, source_root_key="rk",
                              account_key="acct"))
    assert _identity(catchup, before=state, manifest=_linked_manifest()) == []
    expected = catchup.expected_consumption(_linked_manifest(), state)
    assert expected[_FL] == 10 and _FT not in expected, expected
    state["rows"][f"codex_session_files\t{_FL}"].update(inode=15)
    problems = _identity(catchup, before=state, manifest=_linked_manifest())
    assert any(f"{_FL} file identity mismatch" in p for p in problems), problems
    state["rows"][f"codex_session_files\t{_FL}"].update(inode=14,
                                                         last_byte_offset=41)
    problems = _identity(catchup, before=state, manifest=_linked_manifest())
    assert any("last_byte_offset 41 is beyond the frozen file (40 bytes)" in p
               for p in problems), problems


def test_frozen_admission_needs_a_qualification_of_this_pair():
    catchup = _load("catchup")
    assert any("qualification" in p for p in _identity(catchup, qualification=None))
    other = {"qualified": True, "freeze": {"sealSha256": "another"}}
    assert any("another freeze" in p for p in _identity(catchup, qualification=other))
    refused = {"qualified": False, "freeze": {"sealSha256": "seal"},
               "problems": ["x"]}
    assert any("not qualified" in p for p in _identity(catchup, qualification=refused))


def _drained(before):
    import copy
    after = copy.deepcopy(before)
    after["rows"][f"codex_session_files\t{_FA}"].update(
        last_byte_offset=300, size_bytes=300)
    after["rows"].update(_row("codex_session_files", _FNEW, last_byte_offset=50,
                              size_bytes=50, inode=13, source_root_key="rk"))
    return after


def test_frozen_drain_consumes_only_the_frozen_suffixes():
    catchup = _load("catchup")
    before = _frozen_before()
    suffixes = catchup.row_suffixes(_frozen_manifest(), before)
    assert catchup.relocation_problems(before, _drained(before), suffixes) == []


@pytest.mark.parametrize("mutate, fragment", [
    (lambda a: a["rows"].pop(f"session_files\t{_FB}"), "pruned"),
    (lambda a: a["rows"][f"conversation_source_files\t{_FB}"].update(
        source_incarnation_id="inc-new", last_byte_offset=200),
     "replay"),
    (lambda a: a["incarnations"].update({"fk-a": 2}), "replay"),
    (lambda a: a["rows"][f"session_files\t{_FB}"].update(last_byte_offset=0),
     "reset"),
    (lambda a: a["rows"][f"codex_session_files\t{_FA}"].update(
        account_key="other"), "reattribution"),
    (lambda a: a["rows"][f"session_files\t{_FB}"].update(
        last_byte_offset=200, size_bytes=200, mtime_ns=5), None),
    (lambda a: a["rows"][f"conversation_source_files\t{_FB}"].update(
        committed_prefix_sha256="e"), "unexplained work"),
])
def test_frozen_drain_refuses_relocation_effects(mutate, fragment):
    catchup = _load("catchup")
    before = _frozen_before()
    suffixes = catchup.row_suffixes(_frozen_manifest(), before)
    after = _drained(before)
    mutate(after)
    problems = catchup.relocation_problems(before, after, suffixes)
    if fragment is None:
        assert problems == []
    else:
        assert any(fragment in p for p in problems), problems


def _two_table_before():
    """Amendment 14 P2 (run-s15b-disc-C): one frozen rollout retained by two
    tables whose cursors differ - the cache's `codex_session_files` row
    behind (1,261,223 of 1,554,115 bytes there), the conversations row
    already at the admitted length."""
    return _state(
        _row("codex_session_files", _FA, last_byte_offset=120, size_bytes=120,
             inode=11, source_root_key="rk", account_key="acct"),
        _row("codex_conversation_source_files", _FA, last_byte_offset=300,
             size_bytes=300, inode=11, source_root_key="rk"),
        roots={"rk": "/h/.codex/sessions"})


def _two_table_drained(before):
    import copy
    after = copy.deepcopy(before)
    after["rows"][f"codex_session_files\t{_FA}"].update(
        last_byte_offset=300, size_bytes=300)
    return after


def test_frozen_drain_judges_each_rows_work_against_its_own_cursor():
    """The cache row's catch-up to the admitted length is explained by ITS
    OWN cursor's frozen suffix, though the furthest cursor across tables
    (the conversations row's) has none; expected_consumption's byte
    accounting (the suffix beyond the furthest cursor) is unchanged."""
    catchup = _load("catchup")
    before = _two_table_before()
    assert _identity(catchup, before=before) == []
    assert catchup.expected_consumption(_frozen_manifest(), before)[_FA] == 0
    suffixes = catchup.row_suffixes(_frozen_manifest(), before)
    assert suffixes == {f"codex_session_files\t{_FA}": 180,
                        f"codex_conversation_source_files\t{_FA}": 0}
    after = _two_table_drained(before)
    assert catchup.relocation_problems(before, after, suffixes) == []


@pytest.mark.parametrize("mutate, fragment", [
    (lambda a: a["rows"][f"codex_conversation_source_files\t{_FA}"].update(
        size_bytes=310), "unexplained work"),
    (lambda a: a["rows"][f"codex_session_files\t{_FA}"].update(
        last_byte_offset=0, size_bytes=0), "reset"),
    (lambda a: a["rows"][f"codex_session_files\t{_FA}"].update(inode=12),
     "replay"),
    (lambda a: a["rows"][f"codex_session_files\t{_FA}"].update(
        account_key="other"), "reattribution"),
    (lambda a: a["rows"].pop(f"codex_conversation_source_files\t{_FA}"), "pruned"),
])
def test_frozen_drain_still_refuses_other_work_on_a_two_table_file(mutate, fragment):
    """A row whose own cursor already sits at the admitted length has no
    frozen suffix, so any work on it stays unexplained; a cursor reset, a
    replay, a re-attribution and a prune stay refused whatever the suffix."""
    catchup = _load("catchup")
    before = _two_table_before()
    after = _two_table_drained(before)
    mutate(after)
    problems = catchup.relocation_problems(
        before, after, catchup.row_suffixes(_frozen_manifest(), before))
    assert any(fragment in p for p in problems), problems


_LA = "/h/.codex/sessions/live/a.jsonl"
_LB = "/h/.claude/projects/-p/live-b.jsonl"


def _frontier():
    return {"mode": "live", "files": {
        _LA: {"provider": "codex", "dev": 1, "ino": 21, "size": 100,
              "torn": False},
        _LB: {"provider": "claude", "dev": 1, "ino": 22, "size": 80,
              "torn": False}}}


def _live_rows(a, b, *, inc="i"):
    return _state(
        _row("codex_session_files", _LA, last_byte_offset=a, size_bytes=a,
             inode=21),
        _row("session_files", _LB, last_byte_offset=b, size_bytes=b),
        _row("conversation_source_files", _LB, last_byte_offset=b,
             size_bytes=b, inode=22, source_incarnation_id=inc))


def _now(a=100, b=80, ino_a=21):
    return {_LA: {"dev": 1, "ino": ino_a, "size": a},
            _LB: {"dev": 1, "ino": 22, "size": b}}


def test_live_admission_admits_a_continuous_clean_append():
    catchup = _load("catchup")
    problems, justified = catchup.frontier_problems(
        _frontier(), _live_rows(100, 80), _live_rows(160, 95), _now(160, 95))
    assert problems == [] and justified == {"codex": 1, "claude": 1}
    case = _residual_case()
    case["passes"]["verify"]["stats"].update(files_processed=1,
                                            files_skipped_unchanged=5)
    catchup_ = _load("catchup")
    assert catchup_.claude_residual_admission(
        case["prep"], case["passes"], case["prune"], identity=case["identity"],
        justified_claude=1) == []
    assert any(p.startswith("verify.claudeProcessed:") for p in
               catchup_.claude_residual_admission(
                   case["prep"], case["passes"], case["prune"],
                   identity=case["identity"], justified_claude=0))


def test_live_admission_admits_a_file_first_discovered_after_the_frontier():
    catchup = _load("catchup")
    late = "/h/.codex/sessions/live/late.jsonl"
    after = _live_rows(100, 80)
    after["rows"].update(_row("codex_session_files", late, last_byte_offset=10,
                              size_bytes=10, inode=30))
    now = dict(_now(), **{late: {"dev": 1, "ino": 30, "size": 10}})
    problems, justified = catchup.frontier_problems(
        _frontier(), _live_rows(100, 80), after, now)
    assert problems == [] and justified["codex"] == 1


@pytest.mark.parametrize("s1, s2, now, frontier_edit, fragment", [
    ((90, 80), (100, 80), (100, 80, 21), None, "not consumed"),
    ((100, 80), (40, 80), (100, 80, 21), None, "reset"),
    ((100, 80), (100, 90), (100, 80, 21), None, "unexplained work"),
    ((100, 80), (100, 80), (100, 80, 99), None, "discontinuity"),
    ((100, 80), (100, 80), (70, 80, 21), None, "discontinuity"),
    ((90, 80), (90, 80), (100, 80, 21), {"torn": True}, "torn"),
])
def test_live_admission_refuses_anything_but_clean_growth(
        s1, s2, now, frontier_edit, fragment):
    catchup = _load("catchup")
    frontier = _frontier()
    if frontier_edit:
        frontier["files"][_LA].update(frontier_edit)
    problems, _ = catchup.frontier_problems(
        frontier, _live_rows(*s1), _live_rows(*s2), _now(*now))
    assert any(fragment in p for p in problems), problems


def test_live_admission_refuses_a_replayed_or_pruned_frontier_file():
    catchup = _load("catchup")
    problems, _ = catchup.frontier_problems(
        _frontier(), _live_rows(100, 80), _live_rows(100, 80, inc="new"), _now())
    assert any("replay" in p for p in problems), problems
    pruned = _live_rows(100, 80)
    pruned["rows"].pop(f"session_files\t{_LB}")
    problems, _ = catchup.frontier_problems(
        _frontier(), _live_rows(100, 80), pruned, _now())
    assert any("pruned" in p for p in problems), problems


def test_the_live_residual_route_still_refuses_a_changed_residual():
    catchup = _load("catchup")
    case = _residual_case()
    _all_sets(case, [_R2])
    problems = catchup.claude_residual_admission(
        case["prep"], case["passes"], case["prune"], identity=case["identity"],
        justified_claude=5)
    assert any(p.startswith("catchUp.residual:") for p in problems), problems


def test_jsonl_bytes_reports_per_file_growth_against_its_baseline(tmp_path):
    import subprocess as _sp
    import sys as _sys
    claude = tmp_path / "claude" / "projects" / "p"
    codex = tmp_path / "codex" / "sessions"
    claude.mkdir(parents=True)
    codex.mkdir(parents=True)
    grow, gone, swap = claude / "g.jsonl", claude / "d.jsonl", codex / "r.jsonl"
    for p in (grow, gone, swap):
        p.write_text("{}\n")
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(tmp_path / "claude"),
           "CODEX_HOME": str(tmp_path / "codex")}
    env.pop("WRITE_ATTRIBUTION_INPUTS", None)
    base = tmp_path / "baseline.json"

    def sample():
        out = _sp.run([_sys.executable, str(TOOLS / "jsonl_bytes.py"),
                       "--baseline", str(base)], env=env, capture_output=True,
                      text=True, timeout=30)
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout)
    first = sample()
    assert first["growth"] == {"claude": 0, "codex": 0} and first["changes"] == {}
    with open(grow, "a") as fh:
        fh.write('{"x": 1}\n')
    gone.unlink()
    swap.unlink()
    swap.write_text("{}\n{}\n")
    (codex / "new.jsonl").write_text("{}\n")
    second = sample()
    kinds = {os.path.basename(p): c["kind"] for p, c in second["changes"].items()}
    assert kinds == {"g.jsonl": "grown", "d.jsonl": "deleted",
                     "r.jsonl": "replaced", "new.jsonl": "new"}, kinds
    assert second["growth"]["claude"] == len('{"x": 1}\n')
    assert second["discontinuities"] == 2
    # the aggregate totals stay for the existing readers
    assert second["claude"] == grow.stat().st_size


def test_ambient_growth_reads_per_file_discontinuities(tmp_path):
    workload = _load("workload")
    run = tmp_path
    rows = [{"t": 100.0, "claude": 10, "codex": 10, "growth": {"claude": 0, "codex": 0},
             "discontinuities": 0, "changes": {}},
            {"t": 200.0, "claude": 10, "codex": 10, "growth": {"claude": 0, "codex": 0},
             "discontinuities": 1, "changes": {"/x": {"kind": "replaced"}}}]
    (run / "jsonl.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    result = workload.ambient_growth(str(run), 100.0, 200.0)
    assert result["label"] == "ambient" and result["discontinuities"] == 1


def test_catchup_records_the_live_frontier_it_drained(tmp_path):
    """Finite-frontier admission end to end on a seeded clone: the receipt
    names the live mode, the frontier file lists every discovered file, and
    a clone whose verification processed nothing is admitted."""
    root = tmp_path / "clone"
    _seed_roots(root)
    env = {**os.environ, "HOME": str(tmp_path),
           "CCTALLY_DATA_DIR": str(root / "data"),
           "CLAUDE_CONFIG_DIR": str(root / "scratch" / "claude"),
           "CODEX_HOME": str(root / "scratch" / "codex"),
           "CCTALLY_DISABLE_DEV_AUTODETECT": "1",
           "WRITE_ATTRIBUTION_INPUTS": "live"}
    out = tmp_path / "catchup.json"
    child = _catchup(["--tree", str(TOOLS.parent.parent), "--out", str(out)], env)
    receipt = json.loads(out.read_text())
    assert child.returncode == 0, (child.stdout, receipt["problems"])
    assert receipt["inputMode"] == "live"
    frontier = receipt["frontier"]
    assert frontier["mode"] == "live" and frontier["files"] == 2
    assert frontier["problems"] == [] and frontier["torn"] == 0
    saved = json.loads(pathlib.Path(frontier["path"]).read_text())
    assert len(saved["files"]) == 2
    assert all({"dev", "ino", "size", "torn"} <= set(f) for f in saved["files"].values())


# ── revision 15 (Q16): every verdict needs and labels its input evidence ────


def _frozen_evidence(run, *, seal="s" * 64, unmapped=False, pid=42,
                     tripwire=True):
    """A frozen run's inputs.json and the activation receipts of its family;
    with `tripwire`, a window without leak-tripwire samples gets two flat
    ones (Amendment 19 HR-14 refuses a window without them)."""
    run = pathlib.Path(run)
    window = run / "window.json"
    if tripwire and window.exists() and not (run / "jsonl.jsonl").exists():
        w = json.loads(window.read_text())
        (run / "jsonl.jsonl").write_text("".join(json.dumps(
            {"t": t, "claude": 1000, "codex": 2000}) + "\n" for t in (
                float(w["start"]) + 1, float(w["end"]))))
    ident = {"freeze": "/f", "sealSha256": seal, "manifestSha256": "m" * 64,
             "rootmapSha256": "r" * 64, "profileSha256": "p" * 64}
    (run / "inputs.json").write_text(json.dumps({
        "schema": "write-attribution-inputs/1", "mode": "frozen",
        "runtime": {"python": "/opt/homebrew/bin/python3", "version": "3.14.7"},
        "libraries": {"wtrace": {"path": "/x/w", "sha256": "w" * 64},
                      "rootmap": {"path": "/x/r", "sha256": "l" * 64}},
        "freeze": ident,
        "frozenSelftest": {"passed": True, "rootmapSha256": "l" * 64,
                           "wtraceSha256": "w" * 64, "python": "3.14.7"},
        "verifyBefore": {"valid": True, "seal": ident},
        "verifyAfter": {"valid": True, "seal": ident}}))
    receipts = run / "rootmap"
    receipts.mkdir(exist_ok=True)
    lines = [{"event": "activate", "schema": "rootmap/1", "image": "load",
              "pid": pid, "ok": True, "manifestSha256": "r" * 64,
              "library": "/x/r", "enforced": [{"root": "/h/.codex",
                                               "denied": True}]}]
    if unmapped:
        lines.append({"event": "unmapped", "pid": pid, "call": "stat",
                      "path": "/h/.codex/hooks.json"})
    (receipts / f"{pid}.1.2.jsonl").write_text(
        "".join(json.dumps(l) + "\n" for l in lines))
    (receipts / "launches.jsonl").write_text(
        json.dumps({"pid": pid, "t": 1.0, "argv": ["x"]}) + "\n")
    (receipts / "toplevel.jsonl").write_text(_toplevel(pid) + "\n")   # its runner's record
    catchup = run / "catchup.json"
    if catchup.exists():
        data = json.loads(catchup.read_text())
        data["frontier"] = {"mode": "frozen",
                            "verification": {"checked": True, "problems": []}}
        catchup.write_text(json.dumps(data))
    return run


def test_a_verdict_without_input_evidence_is_invalid(tmp_path, capsys):
    workload = _load("workload")
    run = pathlib.Path(_c_run(tmp_path))
    (run / "inputs.json").unlink()
    assert workload.c_verdict(str(run), "tree") == 2
    assert "inputs.json" in capsys.readouterr().out


def test_a_live_admission_without_its_frontier_receipt_is_invalid(tmp_path):
    workload = _load("workload")
    run = pathlib.Path(_idle_run(tmp_path))
    (run / "catchup.json").write_text(json.dumps({"admitted": True}))
    assert any("finite-frontier" in p for p in workload.admission_problems(str(run)))


def test_a_frozen_window_that_grew_is_a_namespace_leak_never_an_ambient_run(tmp_path):
    workload = _load("workload")
    clean = _frozen_evidence(_idle_run(tmp_path / "a"))
    assert workload.admission_problems(str(clean)) == []
    assert workload.input_evidence(str(clean)) == {"mode": "frozen",
                                                   "freeze": "s" * 64}
    grew = _frozen_evidence(_idle_run(tmp_path / "b", growth=4096))
    problems = workload.admission_problems(str(grew))
    assert any("namespace leaked" in p for p in problems), problems


def _frozen_window(tmp_path, tag, changes, *, manifest=True):
    """A frozen idle run whose window's last sample carries per-file
    `changes` (jsonl_bytes.py --baseline) against a freeze whose manifest
    names the roots /h/.claude/projects, /h/.codex and one link-target file."""
    freeze = tmp_path / f"freeze-{tag}"
    freeze.mkdir()
    if manifest:
        (freeze / "manifest.json").write_text(json.dumps({"roots": [
            {"kind": "claude-projects", "logical": "/h/.claude/projects"},
            {"kind": "codex-home", "logical": "/h/.codex"},
            {"kind": "file", "origin": "link-target",
             "logical": "/r/x/.codex/sessions/a.jsonl"}]}))
    run = _frozen_evidence(_idle_run(tmp_path / tag))
    inputs = json.loads((run / "inputs.json").read_text())
    inputs["freeze"]["freeze"] = str(freeze)
    (run / "inputs.json").write_text(json.dumps(inputs))
    growth = {"claude": 0, "codex": 0}
    for change in changes.values():
        growth[change["provider"]] += change["size"] - change["base"]
    rows = [{"t": 110, "claude": 1000, "codex": 2000, "discontinuities": 0,
             "growth": {"claude": 0, "codex": 0}, "changes": {}},
            {"t": 395, "claude": 1000 + growth["claude"],
             "codex": 2000 + growth["codex"], "discontinuities": 0,
             "growth": growth, "changes": changes}]
    (run / "jsonl.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    return run


_SCRATCH_CHANGES = {
    "/c/clone-t/scratch/claude/projects/-b/seed.jsonl":
        {"provider": "claude", "kind": "new", "base": 0, "size": 4096},
    "/c/clone-t/scratch/codex/sessions/2026/10/05/rollout-b.jsonl":
        {"provider": "codex", "kind": "new", "base": 0, "size": 2048}}


def test_a_frozen_window_counts_only_frozen_inputs_not_the_controlled_scratch(tmp_path):
    """Amendment 15 Q1: B appends to its controlled scratch root, outside
    every freeze root; only a change at or under a freeze root is a leak."""
    workload = _load("workload")
    scratch = _frozen_window(tmp_path, "scratch", dict(_SCRATCH_CHANGES))
    assert workload.input_problems(str(scratch)) == []
    grown = dict(_SCRATCH_CHANGES)
    grown["/h/.claude/projects/p/a.jsonl"] = {
        "provider": "claude", "kind": "grown", "base": 10, "size": 30}
    problems = workload.input_problems(str(_frozen_window(tmp_path, "grown", grown)))
    assert any("namespace leaked" in p and "/h/.claude/projects/p/a.jsonl" in p
               for p in problems), problems
    linked = {"/r/x/.codex/sessions/a.jsonl": {
        "provider": "codex", "kind": "replaced", "base": 10, "size": 10}}
    problems = workload.input_problems(str(_frozen_window(tmp_path, "linked", linked)))
    assert any("namespace leaked" in p for p in problems), problems
    deleted = {"/h/.codex/sessions/2026/b.jsonl": {
        "provider": "codex", "kind": "deleted", "base": 10, "size": 0}}
    problems = workload.input_problems(str(_frozen_window(tmp_path, "deleted", deleted)))
    assert any("namespace leaked" in p for p in problems), problems


def test_a_frozen_window_without_a_readable_manifest_counts_every_change(tmp_path):
    """Fail closed: without the freeze's roots, scratch growth is a leak."""
    workload = _load("workload")
    run = _frozen_window(tmp_path, "nomanifest", dict(_SCRATCH_CHANGES),
                         manifest=False)
    problems = workload.input_problems(str(run))
    assert any("namespace leaked" in p for p in problems), problems


def test_a_frozen_family_with_an_unmapped_read_is_invalid(tmp_path):
    workload = _load("workload")
    run = _frozen_evidence(_idle_run(tmp_path), unmapped=True)
    problems = workload.admission_problems(str(run))
    assert any("unmapped stat /h/.codex/hooks.json" in p for p in problems), problems


def test_b_pair_refuses_mixed_input_evidence(tmp_path, capsys):
    workload = _load("workload")
    base = _frozen_evidence(_b_run(tmp_path, "base", [1000, 2000, 3000, 4000]),
                            seal="1" * 64)
    same = _frozen_evidence(_b_run(tmp_path, "same", [1100, 2200, 3300, 4400]),
                            seal="1" * 64)
    assert workload.b_pair(str(same), str(base)) == 0
    other = _frozen_evidence(_b_run(tmp_path, "other", [1100, 2200, 3300, 4400]),
                             seal="2" * 64)
    assert workload.b_pair(str(other), str(base)) == 2
    assert "mixed input evidence" in capsys.readouterr().out
    live = _b_run(tmp_path, "live", [1100, 2200, 3300, 4400])
    assert workload.b_pair(live, str(base)) == 2
    assert "mixed input evidence" in capsys.readouterr().out


def test_the_op_analyzer_requires_input_evidence_and_one_freeze(tmp_path):
    analyze = _load("analyze_op")
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    run = _run_dir(tmp_path / "a")
    assert analyze.analyze(run, "c-op")["inputs"] == {"mode": "live",
                                                      "freeze": None}
    (pathlib.Path(run) / "inputs.json").unlink()
    with pytest.raises(analyze.Invalid, match="inputs.json"):
        analyze.analyze(run, "c-op")
    _live_inputs(run)
    control = _frozen_evidence(_run_dir(tmp_path / "b"), pid=7)
    with pytest.raises(analyze.Invalid, match="mixed input evidence"):
        analyze.analyze(run, "c-op", control=str(control))


def test_p1_verdict_refuses_mixed_freezes_and_frozen_ambient_passes():
    replay = _load("projection_replay")
    receipts = _p1_set()
    frozen = {"problems": [], "label": {"mode": "frozen", "freeze": "s1"}}
    code, result = replay.p1_verdict(receipts, inputs=[frozen] * 4)
    assert code == 0, result
    assert result["inputs"] == [frozen["label"]] * 4
    mixed = [frozen] * 3 + [{"problems": [], "label": {"mode": "frozen",
                                                       "freeze": "s2"}}]
    code, result = replay.p1_verdict(receipts, inputs=mixed)
    assert code == 2 and any("mixed input evidence" in p
                             for p in result["invalid"])
    leaked = _p1_set()
    leaked[0]["refusedRepetitions"] = [
        {"case": "no-append", "rep": 1, "attempt": 1, "reason": "ambient"}]
    code, result = replay.p1_verdict(leaked, inputs=[frozen] * 4)
    assert code == 2 and any("namespace leaked" in p for p in result["invalid"])
    bad = [frozen] * 3 + [{"problems": ["family: pid 9 never activated"],
                           "label": frozen["label"]}]
    code, result = replay.p1_verdict(receipts, inputs=bad)
    assert code == 2 and any("never activated" in p for p in result["invalid"])


def test_the_d_verdict_labels_its_evidence(tmp_path, capsys):
    workload = _load("workload")
    good = [_hook_row(i) for i in range(1, 21)]
    assert workload.d_verdict(_d_run(tmp_path / "a", good)) == 0
    out = json.loads(capsys.readouterr().out.rsplit("\n", 2)[0])
    assert out["inputs"] == {"mode": "live", "freeze": None}


def test_qualify_verifies_every_committed_prefix_against_the_freeze(tmp_path):
    """A frozen catch-up trusts the qualification for the committed-prefix
    digests (#777 continuity), so qualify recomputes each one from the
    frozen bytes and refuses a mismatch."""
    import hashlib
    fr = _load("frozen_roots")
    tree = _frozen_home(tmp_path)
    session = tree["session"]
    ino = os.stat(session).st_ino
    good = hashlib.sha256(session.read_bytes()[:8]).hexdigest()
    store = _frozen_store(tmp_path / "store", rows=[
        ("conversation_source_files", session, 8, 8, ino)])
    conn = sqlite3.connect(store / "data" / "conversations.db")
    conn.execute("ALTER TABLE conversation_source_files ADD COLUMN "
                 "committed_prefix_sha256 TEXT")
    conn.execute("UPDATE conversation_source_files SET committed_prefix_sha256 = ?",
                 (good,))
    conn.commit()
    conn.close()
    os.utime(store / "data" / "conversations.db", (1_700_000_000, 1_700_000_000))
    _capture(fr, tmp_path, tree, store=store)
    result = fr.qualify(store, tmp_path / "freeze")
    assert result["qualified"] is True, result["problems"]
    assert result["committedPrefixesVerified"] == 1
    conn = sqlite3.connect(store / "data" / "conversations.db")
    conn.execute("UPDATE conversation_source_files SET committed_prefix_sha256 = ?",
                 ("0" * 64,))
    conn.commit()
    conn.close()
    os.utime(store / "data" / "conversations.db", (1_700_000_000, 1_700_000_000))
    result = fr.qualify(store, tmp_path / "freeze")
    assert any("committed prefix" in p for p in result["problems"]), result["problems"]


# ── Amendment 12: the frozen runner for the latency evidence ──────────────

_LATENCY_PREP_STUB = r'''# _prep.sh stub for the runner's own tests: no build, no self-test; every
# wa_exec and wa_finish call is recorded, and a family process started with
# the pinned interpreter runs on this test's interpreter instead.
DYLIB=$X/wtrace.dylib
ROOTMAP=$X/rootmap.dylib
WA_RECEIPTS=$OUT/rootmap
mkdir -p "$X/tmp"
echo "selftest: PASS (stub)" > "$OUT/selftest.txt"
wa_exec() {
  printf 'WA_EXEC\t%s\tTMPDIR=%s\tDATA=%s\tCLAUDE=%s\tCODEX=%s\n' "$*" \
    "${TMPDIR-}" "${CCTALLY_DATA_DIR-}" "${CLAUDE_CONFIG_DIR-<unset>}" \
    "${CODEX_HOME-<unset>}" >> "$WA_TEST_LOG"
  if [ "$1" = /opt/homebrew/bin/python3 ]; then shift; exec "$WA_TEST_PY" "$@"; fi
  exec "$@"
}
wa_finish() { printf 'WA_FINISH\t%s\n' "$*" >> "$WA_TEST_LOG"; return 0; }
'''

_LATENCY_STUB = r'''import json, os, pathlib, sys
argv = sys.argv[1:]
out = pathlib.Path(argv[argv.index("--out") + 1])
root = argv[argv.index("--root") + 1] if "--root" in argv else None
seeded = None if root is None else sorted(
    str(p.relative_to(root)) for p in pathlib.Path(root, "scratch").rglob("*.jsonl"))
out.write_text(json.dumps({
    "argv": argv, "pid": os.getpid(), "cwd": os.getcwd(), "seeded": seeded,
    "data": sorted(os.listdir(os.environ["CCTALLY_DATA_DIR"])),
    "env": {k: os.environ.get(k) for k in (
        "TMPDIR", "CCTALLY_DATA_DIR", "CLAUDE_CONFIG_DIR", "CODEX_HOME",
        "WRITE_ATTRIBUTION_INPUTS", "PYTHONDONTWRITEBYTECODE")}}))
'''

_LATENCY_HANGS = r'''import os, pathlib, subprocess, sys, time
argv = sys.argv[1:]
out = pathlib.Path(argv[argv.index("--out") + 1])
child = subprocess.Popen(["sleep", "300"])
(out.parent / "child.pid").write_text(str(child.pid))
time.sleep(300)
'''


def _latency_runner(tmp_path, stub, *args, timeout_s=None):
    """run-latency.sh, copied beside the real _inputs.sh and workload.py with
    a recording _prep.sh stub and a latency.py stub, started in live mode
    (--live) on a synthetic store copy, HOME and scratch directory. A `cp`
    shim drops `-c` so the APFS clone also works off APFS."""
    import shutil
    import stat
    import subprocess as _sp
    import sys as _sys
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in ("run-latency.sh", "_inputs.sh", "workload.py"):
        shutil.copy2(TOOLS / name, tools / name)
    (tools / "_prep.sh").write_text(_LATENCY_PREP_STUB)
    (tools / "latency.py").write_text(stub)
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "cp").write_text(
        '#!/bin/bash\nargs=()\nfor a in "$@"; do case $a in -cR) args+=(-R) ;; '
        '-c) ;; *) args+=("$a") ;; esac; done\nexec /bin/cp "${args[@]}"\n')
    (shim / "cp").chmod(stat.S_IRWXU)
    src = tmp_path / "src"
    (src / "data").mkdir(parents=True)
    (src / "data" / "cache.db").write_bytes(b"synthetic")
    tree = tmp_path / "tree"
    tree.mkdir()
    scratch = tmp_path / "x"
    scratch.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    log = tmp_path / "wa.log"
    env = {k: v for k, v in os.environ.items()
           if k not in ("WA_ALLOW_LIVE", "WA_LIVE_ONLY", "CLAUDE_CONFIG_DIR",
                        "CODEX_HOME", "CCTALLY_DATA_DIR", "DYLD_INSERT_LIBRARIES")}
    env.update({"WRITE_ATTRIBUTION_INPUTS": "live",
                "WRITE_ATTRIBUTION_SCRATCH": str(scratch), "HOME": str(home),
                "PATH": f"{shim}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                "WA_TEST_LOG": str(log), "WA_TEST_PY": _sys.executable})
    if timeout_s is not None:
        env["LATENCY_TIMEOUT_S"] = str(timeout_s)
    out = _sp.run(["bash", str(tools / "run-latency.sh"), str(src), "t1",
                   str(tree), *args, "--live"], env=env, capture_output=True,
                  text=True, timeout=120)
    return out, {"src": src, "tree": tree, "x": scratch, "home": home,
                 "log": log, "run": scratch / "run-t1",
                 "clone": scratch / "clone-t1"}


def _wa_lines(log, kind):
    return [line.split("\t") for line in log.read_text().splitlines()
            if line.startswith(kind + "\t")]


@pytest.mark.parametrize("sync", [False, True], ids=["sites", "sync-passes"])
def test_the_latency_runner_starts_its_family_through_wa_exec(tmp_path, sync):
    """Amendment 12: run-latency.sh starts every family process - B's
    scratch seed and latency.py - through _inputs.sh's `wa_exec`, with TMPDIR
    on the scratch drive and CCTALLY_DATA_DIR on its own APFS clone of
    SRC_ROOT/data. `--sync-passes N` seeds the clone's scratch roots exactly
    as B does (`workload.py seed-scratch`, with CLAUDE_CONFIG_DIR and
    CODEX_HOME naming the real roots plus ROOT/scratch) and passes --root;
    wa_finish judges the family with latency.py's pid; the clone is removed."""
    args = ["--sync-passes", "3"] if sync else []
    out, p = _latency_runner(tmp_path, _LATENCY_STUB, *args)
    assert out.returncode == 0, (out.stdout, out.stderr)
    assert out.stdout.strip().splitlines()[-1] == "__exit_status=0", out.stdout
    run, root = p["run"], p["clone"]
    receipt = json.loads((run / "latency.json").read_text())
    execs = _wa_lines(p["log"], "WA_EXEC")
    commands = [e[1] for e in execs]
    if sync:
        assert len(execs) == 2, execs
        assert commands[0] == (f"/opt/homebrew/bin/python3 {p['x'].parent}/tools/"
                               f"workload.py seed-scratch --root {root}")
    else:
        assert len(execs) == 1, execs
    latency = commands[-1].split()
    assert latency[:2] == ["/opt/homebrew/bin/python3",
                           f"{p['x'].parent}/tools/latency.py"], latency
    assert receipt["argv"][:4] == ["--tree", str(p["tree"]), "--out",
                                   str(run / "latency.json")]
    assert receipt["argv"][receipt["argv"].index("--source") + 1] == str(p["src"])
    claude = f"{p['home']}/.claude,{root}/scratch/claude"
    codex = f"{p['home']}/.codex,{root}/scratch/codex"
    for e in execs:
        assert e[2] == f"TMPDIR={p['x']}/tmp/" and e[3] == f"DATA={root}/data", e
        assert e[4:] == ([f"CLAUDE={claude}", f"CODEX={codex}"] if sync
                         else ["CLAUDE=<unset>", "CODEX=<unset>"]), e
    if sync:
        assert receipt["argv"][receipt["argv"].index("--sync-passes") + 1] == "3"
        assert receipt["argv"][receipt["argv"].index("--root") + 1] == str(root)
        assert any(s.startswith("scratch/claude/projects/") for s in receipt["seeded"])
        assert any(s.startswith("scratch/codex/sessions/") for s in receipt["seeded"])
    else:
        assert "--sync-passes" not in receipt["argv"] and "--root" not in receipt["argv"]
    assert receipt["env"]["WRITE_ATTRIBUTION_INPUTS"] == "live"
    assert receipt["data"] == ["cache.db"]
    assert int((run / "pid").read_text()) == receipt["pid"]
    finish = _wa_lines(p["log"], "WA_FINISH")
    assert len(finish) == 1 and str(receipt["pid"]) in finish[0][1].split(), finish
    assert not root.exists()
    assert (p["src"] / "data" / "cache.db").read_bytes() == b"synthetic"


def test_the_latency_runner_bounds_and_reaps_its_family_process(tmp_path):
    """A latency.py that never finishes is killed with its whole process
    group at LATENCY_TIMEOUT_S; the run is INVALID (exit 2), names the step
    that timed out, still judges the family, and removes the clone."""
    import time as _time
    # timing-budget: the 2 s LATENCY_TIMEOUT_S bound IS the claim - the hung latency step must be killed at it and its family reaped
    out, p = _latency_runner(tmp_path, _LATENCY_HANGS, timeout_s=2)
    assert out.returncode == 2, (out.stdout, out.stderr)
    assert out.stdout.strip().splitlines()[-1] == "__exit_status=2", out.stdout
    timeout = json.loads((p["run"] / "timeout.json").read_text())
    assert timeout == {"step": "latency", "limitS": 2}
    child = int((p["run"] / "child.pid").read_text())
    deadline = _time.monotonic() + 10
    while _time.monotonic() < deadline:
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        _time.sleep(0.2)
    else:
        os.kill(child, 9)
        raise AssertionError("the latency step's child outlived its timeout")
    assert len(_wa_lines(p["log"], "WA_FINISH")) == 1
    assert not p["clone"].exists()


@pytest.mark.parametrize("args", [["--sync-passes"], ["--sync-passes", "x"],
                                  ["--sync-passes", "0"], ["extra"]])
def test_the_latency_runner_refuses_a_malformed_command_line(tmp_path, args):
    out, scratch = _runner(tmp_path, "run-latency.sh",
                           ["/nonexistent-src", "tag", "/nonexistent-tree", *args,
                            "--live"], WRITE_ATTRIBUTION_INPUTS="live")
    assert out.returncode == 2, (out.stdout, out.stderr)
    assert "usage: " in out.stderr and "run-latency.sh" in out.stderr, out.stderr
    assert list(scratch.iterdir()) == []


def test_the_recipe_runs_the_latency_evidence_through_its_runner():
    readme = (TOOLS / "README.md").read_text()
    lines = readme.splitlines()
    step = next(l for l in lines if l.startswith("1. Latency:"))
    assert "WRITE_ATTRIBUTION_INPUTS=frozen:FREEZE" in step, step
    assert "run-latency.sh SRC_ROOT TAG TREE" in step, step
    assert "--sync-passes 5" in step and "latency.py pair" in step, step
    modes = next(l for l in lines if l.startswith("Input mode by runner:"))
    frozen = modes.split("— `frozen:FREEZE`")[0]
    assert "`run-latency.sh`" in frozen, modes
    assert any(l.startswith("- `WRITE_ATTRIBUTION_INPUTS=frozen:FREEZE run-latency.sh")
               for l in lines)
    assert os.access(TOOLS / "run-latency.sh", os.X_OK)


def _latency_run(tmp_path, name, receipt, *, seal="s" * 64, live=False):
    run = tmp_path / name
    run.mkdir()
    receipt = {"gitRev": _BASE_REV if name.startswith("base") else _CAND_REV,
               **receipt}
    (run / "latency.json").write_text(json.dumps(receipt))
    return str(_live_inputs(run) if live else _frozen_evidence(run, seal=seal))


def _site_receipt(**medians):
    names = {"w1": "W1.doctor_latest_quota", "w3": "W3b.pricing_models_all",
             "pa": "PA001a.reconcile_full_pass"}
    return {"tree": "/t", "calls": 5, "sites": {
        names[k]: {"module": "m", "function": "f", "args": {},
                   "samplesS": [v] * 5, "medianS": v, "maxS": v * 1.5}
        for k, v in medians.items()}}


_SYNC_KINDS = ("conversationSyncClaude", "conversationSyncCodex",
               "claudeCacheSync", "codexCacheSync", "statsIngest")


def _sync_receipt(scale=1.0, *, p95_scale=None, passes=5, empty=None):
    out = {}
    for kind in _SYNC_KINDS:
        for population in ("catchUp", "noChange", "smallDelta"):
            n = 0 if (kind, population) == empty else (
                1 if population == "catchUp" else passes)
            p95 = 0.2 * (p95_scale if p95_scale is not None else scale)
            out.setdefault(kind, {})[population] = {
                "n": n, "p50S": 0.1 * scale if n else None,
                "p95S": p95 if n else None, "maxS": p95 if n else None,
                "inputs": [{}] * n}
    return {"tree": "/t", "passes": passes, "syncPasses": out}


def _pair(latency, cand, base, capsys):
    code = latency.pair(cand, base)
    text = capsys.readouterr().out
    return code, json.loads(text.rsplit("\n", 2)[0]), text


def test_the_latency_pair_requires_input_receipts_and_one_freeze(tmp_path, capsys):
    """Amendment 12: the candidate/baseline latency comparison requires each
    run's inputs.json and refuses mixed input evidence, as b-pair does."""
    latency = _load("latency")
    base = _latency_run(tmp_path, "base", _site_receipt(w1=1.0, w3=1.0, pa=1.0),
                        seal="1" * 64)
    same = _latency_run(tmp_path, "same", _site_receipt(w1=0.5, w3=0.9, pa=0.2),
                        seal="1" * 64)
    code, result, _ = _pair(latency, same, base, capsys)
    assert code == 0, result
    assert result["candidate"]["inputs"] == result["baseline"]["inputs"] == {
        "mode": "frozen", "freeze": "1" * 64}
    other = _latency_run(tmp_path, "other", _site_receipt(w1=0.5, w3=0.9, pa=0.2),
                         seal="2" * 64)
    code, result, text = _pair(latency, other, base, capsys)
    assert code == 2 and "mixed input evidence" in text, result
    live = _latency_run(tmp_path, "live", _site_receipt(w1=0.5, w3=0.9, pa=0.2),
                        live=True)
    code, result, text = _pair(latency, live, base, capsys)
    assert code == 2 and "mixed input evidence" in text, result
    (pathlib.Path(same) / "inputs.json").unlink()
    code, result, text = _pair(latency, same, base, capsys)
    assert code == 2 and "inputs.json" in text, result
    (pathlib.Path(other) / "latency.json").unlink()
    _frozen_evidence(other, seal="1" * 64)
    code, result, text = _pair(latency, other, base, capsys)
    assert code == 2 and "latency.json" in text, result


def test_the_latency_pair_holds_each_w_site_to_the_baseline_median(tmp_path, capsys):
    """Spec §6.3: each W1-W3 call site <= the baseline median; the PA-001
    sites are measured and reported (the spec states no limit for them)."""
    latency = _load("latency")
    base = _latency_run(tmp_path, "base", _site_receipt(w1=1.0, w3=1.0, pa=1.0))
    pa_slower = _latency_run(tmp_path, "pa", _site_receipt(w1=1.0, w3=0.5, pa=3.0))
    code, result, _ = _pair(latency, pa_slower, base, capsys)
    assert code == 0, result
    row = result["sites"]["PA001a.reconcile_full_pass"]
    assert row["limit"] is None and row["ratio"] == 3.0
    assert result["sites"]["W1.doctor_latest_quota"]["limit"] == "<= baseline median"
    w_slower = _latency_run(tmp_path, "w", _site_receipt(w1=1.01, w3=0.5, pa=0.5))
    code, result, _ = _pair(latency, w_slower, base, capsys)
    assert code == 1 and any("W1.doctor_latest_quota" in p
                             for p in result["problems"]), result
    missing = _latency_run(tmp_path, "missing", _site_receipt(w1=0.5, w3=0.5))
    code, result, _ = _pair(latency, missing, base, capsys)
    assert code == 2 and any("PA001a.reconcile_full_pass" in p
                             for p in result["problems"]), result
    fewer = _site_receipt(w1=0.5, w3=0.5, pa=0.5)
    fewer["calls"] = 3
    code, result, _ = _pair(latency, _latency_run(tmp_path, "fewer", fewer), base,
                            capsys)
    assert code == 2 and any("calls" in p for p in result["problems"]), result


def test_the_latency_pair_applies_the_ingest_factor_to_matched_sync_passes(
        tmp_path, capsys):
    """Spec §6.3 revision 9: the 1.2x ingest factor applies to matched
    sync-pass p50/p95 per kind and population; an unmatched population, a
    different pass count or a sites receipt against a sync receipt is
    INVALID."""
    latency = _load("latency")
    base = _latency_run(tmp_path, "base", _sync_receipt(1.0))
    ok = _latency_run(tmp_path, "ok", _sync_receipt(1.15))
    code, result, _ = _pair(latency, ok, base, capsys)
    assert code == 0, result
    row = result["syncPasses"]["statsIngest"]["smallDelta"]
    assert row["candidate"]["n"] == row["baseline"]["n"] == 5
    assert row["limit"] == "<= 1.2 x baseline p50 and p95"
    slow = _latency_run(tmp_path, "slow", _sync_receipt(1.0, p95_scale=1.3))
    code, result, _ = _pair(latency, slow, base, capsys)
    assert code == 1 and any("p95" in p for p in result["problems"]), result
    unmatched = _latency_run(tmp_path, "unmatched", _sync_receipt(
        1.0, empty=("codexCacheSync", "noChange")))
    code, result, _ = _pair(latency, unmatched, base, capsys)
    assert code == 2 and any("codexCacheSync" in p and "noChange" in p
                             for p in result["problems"]), result
    both_empty = _latency_run(tmp_path, "be", _sync_receipt(
        1.0, empty=("statsIngest", "catchUp")))
    base_empty = _latency_run(tmp_path, "base-e", _sync_receipt(
        1.0, empty=("statsIngest", "catchUp")))
    code, result, _ = _pair(latency, both_empty, base_empty, capsys)
    assert code == 0, result
    assert result["syncPasses"]["statsIngest"]["catchUp"]["applicable"] is False
    passes = _latency_run(tmp_path, "passes", _sync_receipt(1.0, passes=3))
    code, result, _ = _pair(latency, passes, base, capsys)
    assert code == 2 and any("passes" in p for p in result["problems"]), result
    sites = _latency_run(tmp_path, "sites", _site_receipt(w1=1.0, w3=1.0, pa=1.0))
    code, result, _ = _pair(latency, sites, base, capsys)
    assert code == 2 and any("not the same kind" in p
                             for p in result["problems"]), result


def test_a_frozen_latency_run_needs_its_copys_qualification(tmp_path):
    """README "Frozen inputs" step 2: a frozen catch-up refuses a copy whose
    qualification is missing or names another freeze or copy. latency.py's
    first sync pass is that catch-up (the labelled `catchUp` population), so
    a frozen latency run names its copy (--source) and is refused the same
    way."""
    fr, latency = _load("frozen_roots"), _load("latency")
    tree = _session_home(tmp_path)
    a, b, _, _ = _two_copies(tmp_path, tree)
    _capture_copies(fr, tmp_path, tree, [a, b])
    freeze = tmp_path / "freeze"
    assert any("no qualification" in p
               for p in latency.source_problems(str(a), str(freeze)))
    assert any("--source" in p for p in latency.source_problems(None, str(freeze)))
    for copy in (a, b):
        (copy / "frozen-qualification.json").write_text(
            json.dumps(fr.qualify(copy, freeze)))
    assert latency.source_problems(str(a), str(freeze)) == []
    assert latency.source_problems(str(b), str(freeze)) == []
    _capture_copies(fr, tmp_path, tree, [b], name="freeze-2")
    assert any("another freeze" in p
               for p in latency.source_problems(str(b), str(tmp_path / "freeze-2")))
    (b / "frozen-qualification.json").write_text(
        (a / "frozen-qualification.json").read_text())
    assert any("another store copy" in p
               for p in latency.source_problems(str(b), str(freeze)))


def _prep_in(tmp_path, *, with_bin):
    """Source a copy of _prep.sh from a harness snapshot laid out as the
    certification copies it (bench/write-attribution), with or without the
    candidate's bin/ beside it; nothing is built when the guard refuses."""
    import shutil
    import subprocess
    root = tmp_path / ("with" if with_bin else "without")
    harness = root / "bench" / "write-attribution"
    harness.mkdir(parents=True)
    shutil.copy(TOOLS / "_prep.sh", harness / "_prep.sh")
    if with_bin:
        (root / "bin").mkdir()
        for name in ("_lib_write_budget.py", "_lib_reclaim_planner.py",
                     "_lib_conversation_retention.py"):
            (root / "bin" / name).write_text("")
    (root / "x").mkdir()
    (root / "o").mkdir()
    script = (f'P={harness}; X={root}/x; OUT={root}/o; WA_MODE=live; '
              '. "$P/_prep.sh"; echo reached')
    return subprocess.run(["bash", "-c", script], capture_output=True,
                          text=True, timeout=60)


def test_prep_refuses_a_harness_without_the_candidate_bin(tmp_path):
    """Amendment 15 Q2: workload.py's verdicts and terminal_drain.py's copier
    import the candidate's limits and reclaim planner from the harness's
    ../../bin; a snapshot of bench/ alone lost every phase-1 drain and A
    verdict, so the run must refuse before anything starts."""
    out = _prep_in(tmp_path, with_bin=False)
    assert out.returncode == 2, (out.stdout, out.stderr)
    assert "candidate bin/_lib_write_budget.py" in out.stderr
    assert "reached" not in out.stdout
    ok = _prep_in(tmp_path, with_bin=True)
    assert "candidate bin/" not in ok.stderr, ok.stderr


# ── Amendment 16 (Q18, dc15 F2): the §6.3 full-build gate is non-regression ──

def _pair_out(capsys):
    """The JSON a pair verdict printed, and its verdict word."""
    text = capsys.readouterr().out
    body, word = text.rstrip("\n").rsplit("\n", 1)
    return json.loads(body), word


def test_b_pair_q18_gates_the_ratios_only_and_reports_absolute_values(
        tmp_path, capsys):
    """Q18: a candidate whose B full builds exceed the retired 5 s / 10 s
    ceilings while staying within 1.2 x the matched baseline passes; the
    absolute percentiles, the sample counts and the ratios are reported."""
    workload = _load("workload")
    cand = _b_run(tmp_path, "cand", [6000, 6000, 11000, 11000])
    base = _b_run(tmp_path, "base", [6500, 6500, 12000, 12000])
    assert workload.b_pair(cand, base) == 0
    out, word = _pair_out(capsys)
    assert word == "PASS" and out["valid"] is True and out["problems"] == []
    assert (out["candidate"]["p50Ms"], out["candidate"]["p95Ms"]) == (6000, 11000)
    assert (out["baseline"]["p50Ms"], out["baseline"]["p95Ms"]) == (6500, 12000)
    assert out["candidate"]["n"] == out["baseline"]["n"] == 4
    assert out["ratios"]["p50Ms"] == pytest.approx(6000 / 6500)
    assert out["ratios"]["p95Ms"] == pytest.approx(11000 / 12000)
    assert out["factor"] == 1.2


@pytest.mark.parametrize("cand_ms, failing", [
    ([1300, 1300, 2000, 2000], ["p50Ms"]),
    ([1000, 1000, 2500, 2500], ["p95Ms"]),
    ([1300, 1300, 2500, 2500], ["p50Ms", "p95Ms"]),
    ([1200, 1200, 2400, 2400], []),
])
def test_b_pair_q18_fails_each_percentile_ratio_on_its_own(
        tmp_path, capsys, cand_ms, failing):
    """p50 and p95 are gated independently at <= 1.2 x the baseline;
    equality at exactly 1.2 passes."""
    workload = _load("workload")
    base = _b_run(tmp_path, "base", [1000, 1000, 2000, 2000])
    cand = _b_run(tmp_path, "cand", cand_ms)
    assert workload.b_pair(cand, base) == (1 if failing else 0)
    out, _word = _pair_out(capsys)
    assert out["valid"] is True
    assert sorted(q for q in ("p50Ms", "p95Ms")
                  if any(p.startswith(f"candidate {q} ")
                         for p in out["problems"])) == failing
    assert len(out["problems"]) == len(failing)


@pytest.mark.parametrize("side, full_ms, fragment", [
    ("baseline", [], "no warm full-build population"),
    ("candidate", [], "no warm full-build population"),
    ("baseline", [0, 0, 0, 0], "nonpositive"),
    ("candidate", [0, 0, 0, 0], "nonpositive"),
])
def test_b_pair_q18_an_empty_or_nonpositive_population_is_invalid(
        tmp_path, capsys, side, full_ms, fragment):
    workload = _load("workload")
    runs = {"candidate": [1000, 1000, 2000, 2000],
            "baseline": [1000, 1000, 2000, 2000]}
    runs[side] = full_ms
    cand = _b_run(tmp_path, "cand", runs["candidate"])
    base = _b_run(tmp_path, "base", runs["baseline"])
    assert workload.b_pair(cand, base) == 2
    out, word = _pair_out(capsys)
    assert word == "INVALID" and out["valid"] is False
    assert any(p.startswith(f"{side}: ") and fragment in p
               for p in out["problems"]), out["problems"]


def _c_pair_run(tmp_path, name, full_ms, *, label="C", python="3.14.8",
           sqlite="3.53.4", wtrace="w" * 64, rewound=True, batch=True,
           measured=1830, admitted=True):
    """A synthetic C run: the B helper's warm full builds, C's label and its
    thirty-minute window, a drained admission, the runtime and the write
    interposer it ran with, and the ten-minute batch and rewind receipts."""
    run = pathlib.Path(_b_run(tmp_path, name, full_ms, label=label))
    (run / "window.json").write_text(json.dumps(
        {"workload": label, "traceStart": 0, "warm": 100, "start": 100,
         "end": 100 + measured}))
    (run / "inputs.json").write_text(json.dumps(
        {"schema": "write-attribution-inputs/1", "mode": "live",
         "runtime": {"python": "/opt/homebrew/bin/python3", "version": python},
         "libraries": {"wtrace": {"path": "/x/w", "sha256": wtrace}}}))
    (run / "admission.json").write_text(json.dumps({"valid": True}))
    (run / "catchup.json").write_text(json.dumps(
        {"admitted": admitted, "frontier": _FRONTIER, "python": python,
         "sqlite": sqlite,
         "gitRev": _BASE_REV if name.startswith("base") else _CAND_REV}))
    import os as _os
    written = 100.0 + 650.0                     # the rewind, 650 s into the window
    if batch:
        (run / "batch.json").write_text(json.dumps(
            {"cutoff": "2026-09-06T00:00:00Z",
             "claude": {"groups": 1, "rows": 10, "largest": 10},
             "codex": {"groups": 1, "rows": 5, "largest": 5}}))
        _os.utime(run / "batch.json", (written - 1, written - 1))
    if rewound:
        (run / "rewind.txt").write_text(dt.datetime.fromtimestamp(
            written - 25 * 3600, dt.timezone.utc).isoformat() + "\n")
        _os.utime(run / "rewind.txt", (written, written))
    return str(run)


def test_c_pair_compares_one_candidate_c_with_the_baseline_c(tmp_path, capsys):
    """dc15 F2: each candidate C repetition is compared on its own with the
    recipe's one 56e66f07a C; absolute values, counts and ratios are
    reported, and the identity the comparison matched is in the receipt."""
    workload = _load("workload")
    base = _c_pair_run(tmp_path, "base", [6500, 6500, 12000, 12000])
    cand = _c_pair_run(tmp_path, "cand", [6000, 6000, 11000, 11000])
    assert workload.main(["c-pair", cand, base]) == 0
    out, word = _pair_out(capsys)
    assert word == "PASS" and out["problems"] == []
    assert out["workload"] == "C" and out["factor"] == 1.2
    assert (out["candidate"]["p50Ms"], out["baseline"]["p95Ms"]) == (6000, 12000)
    assert out["candidate"]["n"] == out["baseline"]["n"] == 4
    assert out["ratios"]["p95Ms"] == pytest.approx(11000 / 12000)
    assert out["identity"]["runtime"] == {"python": "3.14.8", "sqlite": "3.53.4"}
    assert out["identity"]["instrumentation"]["wtrace"] == "w" * 64


@pytest.mark.parametrize("cand_ms, code", [
    ([1300, 1300, 2000, 2000], 1),
    ([1000, 1000, 2500, 2500], 1),
    ([1200, 1200, 2400, 2400], 0),
])
def test_c_pair_gates_each_percentile_ratio_with_equality_passing(
        tmp_path, capsys, cand_ms, code):
    workload = _load("workload")
    base = _c_pair_run(tmp_path, "base", [1000, 1000, 2000, 2000])
    cand = _c_pair_run(tmp_path, "cand", cand_ms)
    assert workload.c_pair(cand, base) == code
    out, _word = _pair_out(capsys)
    assert out["valid"] is True and bool(out["problems"]) == bool(code)


def test_c_pair_never_uses_b_as_c_s_denominator(tmp_path, capsys):
    workload = _load("workload")
    c_run = _c_pair_run(tmp_path, "c", [1000, 1000, 2000, 2000])
    b_run = _c_pair_run(tmp_path, "b", [1000, 1000, 2000, 2000], label="B")
    assert workload.c_pair(c_run, b_run) == 2
    out, word = _pair_out(capsys)
    assert word == "INVALID"
    assert any("baseline: workload B" in p and "is not C" in p
               for p in out["problems"]), out["problems"]
    assert workload.c_pair(b_run, c_run) == 2
    assert "candidate: workload B" in capsys.readouterr().out
    # ... and b-pair still refuses a C on either side.
    assert workload.b_pair(c_run, b_run) == 2
    assert "is not B" in capsys.readouterr().out


@pytest.mark.parametrize("kw, fragment", [
    ({"python": "3.14.7"}, "runtime"),
    ({"sqlite": "3.53.3"}, "runtime"),
    ({"wtrace": "v" * 64}, "instrumentation"),
    ({"rewound": False}, "workload settings"),
    ({"batch": False}, "workload settings"),
    ({"measured": 900}, "workload settings"),
    ({"admitted": False}, "catch-up receipt"),
    ({"full_ms": []}, "no warm full-build population"),
    ({"full_ms": [0, 0, 0, 0]}, "nonpositive"),
])
def test_c_pair_mismatched_or_missing_evidence_is_invalid(
        tmp_path, capsys, kw, fragment):
    workload = _load("workload")
    full_ms = kw.pop("full_ms", [1000, 1000, 2000, 2000])
    base = _c_pair_run(tmp_path, "base", full_ms, **kw)
    cand = _c_pair_run(tmp_path, "cand", [1000, 1000, 2000, 2000])
    assert workload.c_pair(cand, base) == 2
    out, word = _pair_out(capsys)
    assert word == "INVALID" and out["valid"] is False
    assert any(fragment in p for p in out["problems"]), out["problems"]


def test_c_pair_refuses_mixed_input_evidence_and_missing_identity(
        tmp_path, capsys):
    workload = _load("workload")
    base = _frozen_evidence(_c_pair_run(tmp_path, "base", [1000, 1000, 2000, 2000]),
                            seal="1" * 64)
    cand = _c_pair_run(tmp_path, "cand", [1000, 1000, 2000, 2000])
    assert workload.c_pair(cand, str(base)) == 2
    assert "mixed input evidence" in capsys.readouterr().out
    bare = _c_pair_run(tmp_path, "bare", [1000, 1000, 2000, 2000])
    receipt = json.loads(pathlib.Path(bare, "catchup.json").read_text())
    del receipt["sqlite"]
    pathlib.Path(bare, "catchup.json").write_text(json.dumps(receipt))
    assert workload.c_pair(cand, bare) == 2
    out, _word = _pair_out(capsys)
    assert any("baseline: runtime" in p and "not recorded" in p
               for p in out["problems"]), out["problems"]


def _frozen_c_run(tmp_path, name, *, full_ms=(), seal="s" * 64,
                  dispatches=None, cold=False, gap=False, start_edge=True,
                  tail_s=2.0, growth=0, frozen=True, python="3.14.8"):
    """A frozen, drained C run (spec §6.3, Q19): the C pair helper's identity
    and settings, the freeze's input evidence, append-free roots, and tick
    records every 7 s from a predecessor published before the window to
    `tail_s` before its end. `full_ms` turns the first records into warm
    full builds; `dispatches` overrides the window's decisions."""
    run = pathlib.Path(_c_pair_run(tmp_path, name, list(full_ms),
                                   python=python))
    if frozen:
        _frozen_evidence(run, seal=seal)
    start, end = 100.0, 1930.0
    (run / "jsonl.jsonl").write_text(
        json.dumps({"t": 110, "claude": 1000, "codex": 2000}) + "\n"
        + json.dumps({"t": 1925, "claude": 1000, "codex": 2000 + growth})
        + "\n")
    times = [start + 5 + 7 * k for k in range(int((end - start) / 7) + 1)
             if start + 5 + 7 * k <= end - tail_s]
    decisions = list(dispatches or ["idle"] * len(times))
    decisions += ["idle"] * (len(times) - len(decisions))
    for i, ms in enumerate(full_ms):
        decisions[i] = "full"
    stamp = lambda s: dt.datetime.fromtimestamp(s, dt.timezone.utc).isoformat()
    records = ([{"seq": 1, "dispatch": "idle", "cold": True,
                 "duration_ns": 3_000_000, "period_ns": 7_000_000_000,
                 "published_at": stamp(start - 30)}] if start_edge else [])
    for i, (at, decision) in enumerate(zip(times, decisions)):
        seq = i + 2
        if gap and i == 10:
            continue
        records.append({
            "seq": seq, "dispatch": decision, "cold": bool(cold and i == 3),
            "duration_ns": int((full_ms[i] if i < len(full_ms) else 3) * 1e6),
            "period_ns": 7_000_000_000, "published_at": stamp(at)})
    (run / "perf-00000.json").write_text(json.dumps({"diagnostic": {
        "tick": {"records": records}, "phases": None}}))
    return str(run)


def test_q19_a_frozen_idle_only_c_pair_is_not_applicable(tmp_path, capsys):
    """dc16 G2: both sides frozen, drained, append-free and idle-only over
    the whole window: zero samples, null percentiles and ratios, exit 0."""
    workload = _load("workload")
    base = _frozen_c_run(tmp_path, "base")
    cand = _frozen_c_run(tmp_path, "cand")
    side = workload.perf_evidence(cand)
    assert side["valid"] is True, side["problems"]
    assert side["fullBuild"] == {
        "applicable": False, "n": 0, "p50Ms": None, "p95Ms": None,
        "exception": "frozen-C (Q19)", "pairRequired": True,
        "idleTicks": side["frozenC"]["ticks"]}
    assert side["fullBuildP50Ms"] is None             # never zero latency
    assert side["publishPeriodP95Ms"] == 7000.0       # periods still measured
    assert workload.main(["c-pair", cand, base]) == 0
    out, word = _pair_out(capsys)
    assert word == "NOT APPLICABLE" and out["valid"] is True
    assert out["applicable"] is False and out["problems"] == []
    assert out["ratios"] == {"p50Ms": None, "p95Ms": None}
    assert out["candidate"]["n"] == out["baseline"]["n"] == 0
    assert out["candidate"]["p50Ms"] is None and out["baseline"]["p95Ms"] is None


@pytest.mark.parametrize("kw, fragment", [
    ({"dispatches": ["idle"] * 5 + ["degraded"]}, "non-idle"),
    ({"cold": True}, "cold ticks"),
    ({"gap": True}, "gaps"),
    ({"start_edge": False}, "incomplete window coverage"),
    ({"tail_s": 20.0}, "incomplete window coverage"),
    ({"growth": 512}, "append-free"),
    ({"frozen": False}, "not a frozen run"),
])
def test_q19_a_c_side_that_does_not_qualify_needs_a_full_build(
        tmp_path, capsys, kw, fragment):
    workload = _load("workload")
    side = workload.perf_evidence(_frozen_c_run(tmp_path, "cand", **kw))
    assert side["valid"] is False
    assert "no warm full build in the window" in side["problems"]
    assert fragment in " ".join(side["frozenC"]["reasons"]), side["frozenC"]
    base = _frozen_c_run(tmp_path, "base")
    assert workload.c_pair(_frozen_c_run(tmp_path, "c2", **kw), base) == 2
    out, word = _pair_out(capsys)
    assert word == "INVALID" and out["valid"] is False


@pytest.mark.parametrize("idle_side", ["candidate", "baseline"])
def test_q19_a_one_sided_full_build_population_is_invalid(
        tmp_path, capsys, idle_side):
    workload = _load("workload")
    idle = _frozen_c_run(tmp_path, "idle")
    built = _frozen_c_run(tmp_path, "built", full_ms=[1000, 1000, 2000, 2000])
    assert workload.perf_evidence(built)["fullBuild"]["n"] == 4
    cand, base = (idle, built) if idle_side == "candidate" else (built, idle)
    assert workload.c_pair(cand, base) == 2
    out, word = _pair_out(capsys)
    assert word == "INVALID"
    assert any(p.startswith(f"{idle_side}: a one-sided full-build population")
               for p in out["problems"]), out["problems"]


def test_q19_the_frozen_c_pair_still_needs_one_freeze_and_identity(
        tmp_path, capsys):
    workload = _load("workload")
    base = _frozen_c_run(tmp_path, "base")
    other = _frozen_c_run(tmp_path, "other", seal="1" * 64)
    assert workload.c_pair(_frozen_c_run(tmp_path, "cand"), other) == 2
    assert "mixed input evidence" in capsys.readouterr().out
    slow = _frozen_c_run(tmp_path, "slow", python="3.14.7")
    receipt = json.loads(pathlib.Path(slow, "catchup.json").read_text())
    receipt["sqlite"] = "3.53.3"
    pathlib.Path(slow, "catchup.json").write_text(json.dumps(receipt))
    assert workload.c_pair(slow, base) == 2
    out, _word = _pair_out(capsys)
    assert any("mismatched runtime" in p for p in out["problems"]), out


def test_q19_the_exception_never_covers_b(tmp_path, capsys):
    """B keeps Q18's matched nonempty populations: idle-only frozen B runs
    are no warm full-build population, never not applicable."""
    workload = _load("workload")
    base = _frozen_c_run(tmp_path, "base")
    cand = _frozen_c_run(tmp_path, "cand")
    for run in (base, cand):
        window = json.loads(pathlib.Path(run, "window.json").read_text())
        window["workload"] = "B"
        pathlib.Path(run, "window.json").write_text(json.dumps(window))
        assert workload.perf_evidence(run)["frozenC"] is None
    assert workload.b_pair(cand, base) == 2
    out, word = _pair_out(capsys)
    assert word == "INVALID"
    assert any("no warm full-build population" in p for p in out["problems"])


# ── Amendment 19 Part U (sr5 HR-1, HR-2, HR-7, HR-17, HR-19, HR-21): every
# runner guards its lifecycle, so an interrupted, overdue or killed runner
# never leaves a process of its family running ─────────────────────────────

_LIFECYCLE_PREP_STUB = r'''# _prep.sh stub for the lifecycle tests: no build and no self-test; every
# wa_exec and wa_finish call is recorded, and a family process started with
# the pinned interpreter runs on this test's interpreter instead.
mkdir -p "$X/tmp"
export TMPDIR=$X/tmp/
DYLIB=
ROOTMAP=
WA_RECEIPTS=$OUT/rootmap
echo "selftest: PASS (stub)" > "$OUT/selftest.txt"
wa_exec() {
  printf 'WA_EXEC\t%s\n' "$*" >> "$WA_TEST_LOG"
  unset DYLD_INSERT_LIBRARIES
  if [ "$1" = /opt/homebrew/bin/python3 ]; then shift; exec "$WA_TEST_PY" "$@"; fi
  exec "$@"
}
wa_finish() { printf 'WA_FINISH\t%s\n' "$*" >> "$WA_TEST_LOG"; return 0; }
'''

#: Every family tool the runners start, as one stub: a long-running role
#: records its pid, a child's and a setsid worker's (the worker a process
#: group kill misses) and sleeps; every other role succeeds at once.
_FAMILY_STUB = r'''import json, os, pathlib, subprocess, sys, time
name = pathlib.Path(sys.argv[0]).name
argv = sys.argv[1:]
first = argv[0] if argv else ""
marks = pathlib.Path(os.environ["WA_TEST_MARKS"])


def value(flag):
    return argv[argv.index(flag) + 1] if flag in argv else None


def linger(label):
    child = subprocess.Popen(["sleep", "300"])
    worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"],
                              start_new_session=True)
    (marks / f"{label}.{os.getpid()}.pids").write_text(
        f"{os.getpid()} {child.pid} {worker.pid}\n")
    time.sleep(300)


if name == "cctally" and first == "dashboard-perf":
    if "--trace" in argv:
        raise SystemExit(0)
    cold = os.environ.get("WA_TEST_WARM") != "1"
    print(json.dumps({"diagnostic": {"tick": {"records": [{"seq": 1, "cold": cold}]}}}))
    raise SystemExit(0)
if name == "cctally" and first == "dashboard":
    linger("dashboard")
if name == "catchup.py":
    pathlib.Path(value("--out")).write_text(json.dumps({"admitted": True}))
    raise SystemExit(0)
if name == "maintenance_op.py" and "--normalize-only" not in argv:
    linger("op")
if name == "projection_replay.py" and first in ("p1", "p2-replay"):
    linger(first)
if name == "workload.py" and first == "append":
    linger("append")
if name in ("sqlattr.py", "latency.py", "hook_runner.py"):
    linger(name.split(".")[0])
raise SystemExit(0)
'''


def _lifecycle_box(tmp_path):
    """A copy of the harness - every runner, _inputs.sh, wa_procs.py and the
    real helpers - under a path with a space in it, with the recording
    _prep.sh stub, the family stub in place of every tool that would run a
    real workload, a stub tree whose bin/cctally plays the dashboard, closed
    store copies and an empty HOME. A `cp` shim drops `-c` off APFS."""
    import shutil
    import stat
    import sys as _sys
    import types
    base = tmp_path / "s p"
    tools = base / "tools"
    shutil.copytree(TOOLS, tools, ignore=shutil.ignore_patterns(
        "__pycache__", "*.dylib", "*.pyc"))
    (tools / "_prep.sh").write_text(_LIFECYCLE_PREP_STUB)
    for name in ("catchup.py", "maintenance_op.py", "projection_replay.py",
                 "sqlattr.py", "latency.py", "hook_runner.py", "workload.py"):
        (tools / name).write_text(_FAMILY_STUB)
    tree = base / "tree"
    (tree / "bin").mkdir(parents=True)
    (tree / "bin" / "cctally").write_text(_FAMILY_STUB)
    shim = base / "shim"
    shim.mkdir()
    (shim / "cp").write_text(
        '#!/bin/bash\nargs=()\nfor a in "$@"; do case $a in -cR) args+=(-R) ;; '
        '-c) ;; *) args+=("$a") ;; esac; done\nexec /bin/cp "${args[@]}"\n')
    (shim / "cp").chmod(stat.S_IRWXU)
    src = base / "src"
    (src / "data").mkdir(parents=True)
    for db in (src / "data" / "conversations.db", src / "data" / "cache.db",
               src / "op.db"):
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE cache_meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.commit()
        conn.close()
    slice_dir = base / "slice"
    slice_dir.mkdir()
    (slice_dir / "manifest.json").write_text("{}")
    home = base / "home"
    home.mkdir()
    data = base / "data"
    data.mkdir()
    marks = base / "marks"
    marks.mkdir()
    scratch = base / "x"
    scratch.mkdir()
    log = base / "wa.log"
    env = {k: v for k, v in os.environ.items()
           if k not in ("WA_ALLOW_LIVE", "WA_LIVE_ONLY", "CLAUDE_CONFIG_DIR",
                        "CODEX_HOME", "DYLD_INSERT_LIBRARIES", "WA_RUN_TOKEN",
                        "WA_DEADLINE_S")}
    env.update({"WRITE_ATTRIBUTION_INPUTS": "live",
                "WRITE_ATTRIBUTION_SCRATCH": str(scratch), "HOME": str(home),
                "CCTALLY_DATA_DIR": str(data),
                "PATH": f"{shim}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                "WA_TEST_LOG": str(log), "WA_TEST_PY": _sys.executable,
                "WA_TEST_MARKS": str(marks)})
    return types.SimpleNamespace(base=base, tools=tools, tree=tree, src=src,
                                 slice=slice_dir, home=home, marks=marks,
                                 scratch=scratch, log=log, env=env)


#: name -> the arguments that start it into a long-running family process.
def _lifecycle_args(box):
    t = str(box.tree)
    return {
        "run-workload.sh": ["A", str(box.src), "t1", t, "9", "--live"],
        "run-latency.sh": [str(box.src), "t1", t, "--live"],
        "run-clone.sh": [str(box.src / "data"), "t1", "300", "9", t, "--live"],
        "run-statements.sh": [str(box.src / "data"), "t1", "300", "9", t, "--live"],
        "run-op.sh": [str(box.src / "op.db"), "t1", t, "--live", "--"],
        "run-p1.sh": [str(box.src), "t1", t, "key", "candidate", "--live"],
        "run-live.sh": ["t1", "300", "9", t],
        "run-scratch-proof.sh": [str(box.src), "t1", t, "9"],
        "run-p2.sh": [str(box.slice), str(box.src), "t1", t, "9", "--live"],
    }


def _start_runner(box, name, args, **env):
    import subprocess as _sp
    out = open(box.base / f"{name}.out", "w")
    return _sp.Popen(["bash", str(box.tools / name), *args],
                     env={**box.env, **env}, stdout=out, stderr=_sp.STDOUT,
                     start_new_session=True)


def _marked_pids(box) -> "list[int]":
    pids = []
    for path in box.marks.glob("*.pids"):
        pids.extend(int(p) for p in path.read_text().split())
    return pids


def _await_family(box):
    """Until a long-running family process has recorded its pids."""
    import time as _time
    deadline = _time.monotonic() + 40
    while _time.monotonic() < deadline:
        if list(box.marks.glob("*.pids")):
            _time.sleep(0.5)                    # its children are started
            return
        _time.sleep(0.2)
    raise AssertionError("no family process started: "
                         + "".join(p.read_text()[-2000:]
                                   for p in box.base.glob("*.out")))


def _alive(pid) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _survivors(pids) -> "list[int]":
    """The pids still alive once every one has had its chance to die."""
    import time as _time
    deadline = _time.monotonic() + 20
    while _time.monotonic() < deadline:
        left = [p for p in pids if _alive(p)]
        if not left:
            return []
        _time.sleep(0.2)
    return [p for p in pids if _alive(p)]


def _kill_everything(box, proc):
    """Test cleanup: nothing a failing test started may outlive it."""
    import signal as _signal
    for pid in _marked_pids(box):
        try:
            os.kill(pid, _signal.SIGKILL)
        except OSError:
            pass
    try:
        os.killpg(proc.pid, _signal.SIGKILL)
    except OSError:
        pass
    if proc.poll() is None:
        proc.kill()


def _diagnose(run, pids) -> dict:
    """What a failed lifecycle assertion needs: the runner's records and
    the survivors' process rows."""
    import subprocess as _sp
    out = {}
    for name in ("orphaned.json", "deadline.json", "interrupted.jsonl",
                 "finish.jsonl", "token-teardown.jsonl", "token-teardown.log"):
        path = run / name
        out[name] = path.read_text()[-1500:] if path.exists() else None
    if pids:
        out["ps"] = _sp.run(["ps", "-o", "pid,ppid,pgid,stat,command", "-p",
                             ",".join(str(p) for p in pids)],
                            capture_output=True, text=True, timeout=10).stdout
    return out


def _wa_log(box, kind):
    if not box.log.exists():
        return []
    return [line for line in box.log.read_text().splitlines()
            if line.startswith(kind + "\t")]


def _run_out(box, name="run-t1"):
    return box.scratch / name


@pytest.mark.parametrize("name", [
    "run-workload.sh", "run-latency.sh", "run-clone.sh", "run-statements.sh",
    "run-op.sh", "run-p1.sh", "run-live.sh", "run-scratch-proof.sh",
    "run-p2.sh"])
def test_an_interrupted_runner_leaves_nothing_running_and_judges_its_family(
        tmp_path, name):
    """HR-1/HR-2/HR-7: SIGTERM to a runner while its family runs. Its own
    trap must kill the family's process group - the family process, its
    child and the worker it started in another session (which a group kill
    misses) - run wa_finish exactly once, record the interrupt, and exit
    143, all under a path with a space in it (HR-21)."""
    import signal as _signal
    box = _lifecycle_box(tmp_path)
    proc = _start_runner(box, name, _lifecycle_args(box)[name])
    try:
        _await_family(box)
        pids = _marked_pids(box)
        assert len(pids) == 3, pids
        os.kill(proc.pid, _signal.SIGTERM)
        rc = proc.wait(timeout=40)
        out = (box.base / f"{name}.out").read_text()
        assert rc == 143, (rc, out)
        assert _survivors(pids) == [], (pids, out)
        finished = _wa_log(box, "WA_FINISH")
        # run-scratch-proof.sh has no _prep.sh (no stub wa_finish to log);
        # finish.jsonl below records its judging all the same.
        assert len(finished) == (0 if name == "run-scratch-proof.sh" else 1), (
            finished, out)
        run = (_run_out(box, "run-live-t1") if name == "run-live.sh"
               else _run_out(box))
        judged = _jsonl(run / "finish.jsonl")
        assert [j["by"] for j in judged] == ["trap"], judged
        interrupts = _jsonl(run / "interrupted.jsonl")
        assert [i["signal"] for i in interrupts][:1] == ["TERM"], interrupts
    finally:
        _kill_everything(box, proc)


@pytest.mark.parametrize("sig, status", [("SIGINT", 130), ("SIGHUP", 129)])
def test_every_trapped_signal_ends_the_run_the_same_way(tmp_path, sig, status):
    import signal as _signal
    box = _lifecycle_box(tmp_path)
    proc = _start_runner(box, "run-clone.sh", _lifecycle_args(box)["run-clone.sh"])
    try:
        _await_family(box)
        pids = _marked_pids(box)
        os.kill(proc.pid, getattr(_signal, sig))
        assert proc.wait(timeout=40) == status
        assert _survivors(pids) == []
        assert len(_wa_log(box, "WA_FINISH")) == 1
    finally:
        _kill_everything(box, proc)


def test_the_hard_deadline_stops_the_runner_and_its_family(tmp_path):
    """The watchdog's hard deadline (WA_DEADLINE_S): a runner whose family
    would run for 300 s is stopped at 8 s, its family killed, the deadline
    recorded and the family judged once."""
    box = _lifecycle_box(tmp_path)
    proc = _start_runner(box, "run-clone.sh", _lifecycle_args(box)["run-clone.sh"],
                         WA_DEADLINE_S="8")
    try:
        _await_family(box)
        pids = _marked_pids(box)
        assert proc.wait(timeout=40) == 143
        assert _survivors(pids) == []
        deadline = json.loads((_run_out(box) / "deadline.json").read_text())
        assert deadline["limitS"] == 8
        assert len(_wa_log(box, "WA_FINISH")) == 1
    finally:
        _kill_everything(box, proc)


def test_a_killed_runner_is_cleaned_up_by_its_watchdog(tmp_path):
    """SIGKILL cannot be trapped: the watchdog notices the runner is gone,
    kills the process groups it last saw the runner lead and every process
    carrying the run token, runs the runner's cleanup and wa_finish, and
    records the orphaned run."""
    import signal as _signal
    box = _lifecycle_box(tmp_path)
    proc = _start_runner(box, "run-clone.sh", _lifecycle_args(box)["run-clone.sh"])
    try:
        _await_family(box)
        pids = _marked_pids(box)
        os.kill(proc.pid, _signal.SIGKILL)
        proc.wait(timeout=20)
        left = _survivors(pids)
        run = _run_out(box)
        assert left == [], (pids, left, _diagnose(run, left))
        import time as _time
        deadline = _time.monotonic() + 10       # the cleanup ends with finish.jsonl
        while not (run / "finish.jsonl").exists() and _time.monotonic() < deadline:
            _time.sleep(0.2)
        assert (run / "orphaned.json").exists()
        assert not (box.scratch / "clone-t1").exists()
        judged = _jsonl(_run_out(box) / "finish.jsonl")
        assert [j["by"] for j in judged] == ["watchdog"], judged
    finally:
        _kill_everything(box, proc)


def test_the_live_runner_writes_its_records_and_reaps_its_workers(tmp_path):
    """HR-17: run-live.sh writes a fresh run-live-TAG with the frontier,
    admission, window and terminal records and the terminal drain, and
    refuses an existing one; HR-7: the installed dashboard's setsid worker
    is reaped by run token, recorded, before the copier runs."""
    box = _lifecycle_box(tmp_path)
    args = ["t1", "1", "9", str(box.tree)]
    proc = _start_runner(box, "run-live.sh", args, WA_TEST_WARM="1")
    try:
        rc = proc.wait(timeout=60)
        out = (box.base / "run-live.sh.out").read_text()
        assert rc == 0, out
        run = _run_out(box, "run-live-t1")
        window = json.loads((run / "window.json").read_text())
        assert window["workload"] == "L-post"
        assert window["warm"] <= window["start"] < window["end"]
        assert json.loads((run / "admission.json").read_text())["valid"] is True
        frontier = json.loads((run / "frontier.json").read_text())
        assert frontier["mode"] == "live" and "files" in frontier
        terminal = json.loads((run / "terminal.json").read_text())
        assert terminal["alive"] == 1 and "drain" in terminal
        pids = _marked_pids(box)
        assert len(pids) == 3 and _survivors(pids) == []
        reaped = [json.loads(l) for l in
                  (run / "token-teardown.jsonl").read_text().splitlines()]
        assert pids[2] in {r["pid"] for r in reaped}, reaped
        again = _start_runner(box, "run-live.sh", args, WA_TEST_WARM="1")
        assert again.wait(timeout=20) == 2
        assert "exists" in (box.base / "run-live.sh.out").read_text()
    finally:
        _kill_everything(box, proc)


def test_the_token_reap_kills_a_setsid_worker_whose_parent_is_gone(tmp_path):
    """HR-7: a worker that called setsid and outlived its parent is in no
    group the runner can kill; the run token in its environment finds it.
    A process without the token is never touched."""
    import subprocess as _sp
    import sys as _sys
    import uuid
    wa = _load("wa_procs")
    token = f"wa-test-{uuid.uuid4().hex}"
    started = _sp.run(
        [_sys.executable, "-c",
         "import subprocess, sys; p = subprocess.Popen([sys.executable, '-c', "
         "'import time; time.sleep(300)'], start_new_session=True, "
         "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, "
         "stderr=subprocess.DEVNULL); print(p.pid)"],
        env={**os.environ, "WA_RUN_TOKEN": token}, capture_output=True,
        text=True, timeout=30)
    worker = int(started.stdout.strip())
    bystander = _sp.Popen(["sleep", "300"])
    try:
        assert [r["pid"] for r in wa.token_processes(token)] == [worker]
        result = wa.reap(token, str(tmp_path / "rec.jsonl"),
                         teardown=str(tmp_path / "teardown.jsonl"), by="test")
        assert result["killed"] == [worker] and result["survivors"] == []
        assert _survivors([worker]) == []
        assert bystander.poll() is None
        record = [json.loads(l) for l in (tmp_path / "rec.jsonl").read_text().splitlines()]
        assert [(r["pid"], r["signal"], r["matched"]) for r in record] == [
            (worker, 9, "WA_RUN_TOKEN")]
        teardown = [json.loads(l) for l in
                    (tmp_path / "teardown.jsonl").read_text().splitlines()]
        assert [(r["pid"], r["signal"]) for r in teardown] == [(worker, 9)]
    finally:
        for pid in (worker,):
            try:
                os.kill(pid, 9)
            except OSError:
                pass
        bystander.kill()
        bystander.wait(timeout=30)


def test_a_group_teardown_records_every_member(tmp_path):
    """HR-21: a group kill records each live member, not only its leader,
    so a system program a member started has its kill on record."""
    out, rec = _wa_script(tmp_path, "\n".join([
        "set -m; ( sleep 30 & sleep 30 & wait ) & g=$!; set +m",
        "sleep 0.5",
        "wa_kill 9 -$g; wa_wait group $g; echo group=$?",
    ]))
    assert out.returncode == 0, (out.stdout, out.stderr)
    kills = _jsonl(rec / "teardown.jsonl")
    assert len(kills) == 3, kills
    assert {k["leader"] for k in kills} == {kills[0]["leader"]}
    assert all(k["group"] is True and k["signal"] == 9 for k in kills)


def _live_store():
    import pwd
    return os.path.join(pwd.getpwuid(os.getuid()).pw_dir, ".local", "share",
                        "cctally")


@pytest.mark.parametrize("name, args", [
    ("run-clone.sh", ["{live}", "t", "1", "9", "/nonexistent-tree", "--live"]),
    ("run-statements.sh", ["{live}/nonexistent-copy", "t", "1", "9",
                           "/nonexistent-tree", "--live"]),
    ("run-op.sh", ["{live}/nonexistent-copy/conversations.db", "t",
                   "/nonexistent-tree", "--live", "--"]),
])
def test_a_probe_refuses_the_live_data_directory(tmp_path, name, args):
    """HR-19: a store at or under the live data directory (resolved through
    the password database, never $HOME) is refused before anything is
    created; nothing is read or written there."""
    args = [a.replace("{live}", _live_store()) for a in args]
    out, scratch = _runner(tmp_path, name, args, WRITE_ATTRIBUTION_INPUTS="live")
    assert out.returncode == 2, (out.stdout, out.stderr)
    assert "live data directory" in out.stderr, out.stderr
    assert list(scratch.iterdir()) == []


def test_prep_points_tmpdir_at_the_external_drive_before_it_builds(tmp_path):
    """HR-21: _prep.sh built the libraries and ran its self-tests before
    TMPDIR named the external drive, so their temporary files landed on the
    internal disk. clang is the first thing it runs: it must already see
    TMPDIR=$X/tmp/."""
    import subprocess
    import stat
    root = tmp_path / "r"
    harness = root / "bench" / "write-attribution"
    harness.mkdir(parents=True)
    import shutil
    shutil.copy(TOOLS / "_prep.sh", harness / "_prep.sh")
    (root / "bin").mkdir()
    for name in ("_lib_write_budget.py", "_lib_reclaim_planner.py",
                 "_lib_conversation_retention.py"):
        (root / "bin" / name).write_text("")
    for d in ("x", "o", "shim"):
        (root / d).mkdir()
    seen = root / "seen-tmpdir"
    (root / "shim" / "clang").write_text(
        f'#!/bin/bash\nprintf "%s" "${{TMPDIR-}}" > "{seen}"\nexit 1\n')
    (root / "shim" / "clang").chmod(stat.S_IRWXU)
    script = (f'P={harness}; X={root}/x; OUT={root}/o; WA_MODE=live; '
              '. "$P/_prep.sh"; echo reached')
    env = {**os.environ, "PATH": f"{root / 'shim'}:{os.environ.get('PATH', '')}",
           "TMPDIR": "/private/var/folders/internal/"}
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                         timeout=60, env=env)
    assert out.returncode == 2 and "wtrace build failed" in out.stderr
    assert seen.read_text() == f"{root}/x/tmp/"


_GUARDED_RUNNERS = ("run-clone.sh", "run-latency.sh", "run-live.sh", "run-op.sh",
                    "run-p1.sh", "run-scratch-proof.sh", "run-statements.sh",
                    "run-workload.sh")


@pytest.mark.parametrize("name", _GUARDED_RUNNERS)
def test_every_runner_guards_its_lifecycle_before_it_builds(name):
    """HR-1/HR-2: the guard (job control, the traps, the hard deadline, the
    token) is installed before _prep.sh builds anything, and no runner sets
    a trap of its own that could bypass it."""
    text = (TOOLS / name).read_text()
    code = [l for l in text.splitlines() if not l.lstrip().startswith("#")]
    guard = next(i for i, l in enumerate(code) if l.startswith("wa_guard "))
    first_launch = min(i for i, l in enumerate(code)
                       if '"$P/_prep.sh"' in l or "cp -c" in l)
    assert guard < first_launch, name
    assert not [l for l in code if l.lstrip().startswith("trap ")], name


@pytest.mark.parametrize("name", ("run-p2.sh", "run-p3.sh", "run-lifecycle.sh"))
def test_a_wrapper_runs_the_runner_it_wraps_through_wa_child(name):
    text = (TOOLS / name).read_text()
    assert "wa_child \"$P/run-" in text, name


@pytest.mark.parametrize("name", ("run-workload.sh", "run-live.sh"))
def test_the_drain_runs_after_the_detached_workers_are_reaped(name):
    """HR-21: the copier must have the clone to itself, so the dashboard's
    detached workers are reaped (recorded) before terminal_drain.py starts."""
    script = (TOOLS / name).read_text()
    reap = script.index("wa_reap_detached")
    drain = script.index('"$P/terminal_drain.py"')
    kill = max(script.rfind('wa_kill 9 "-$PID"', 0, reap),
               script.rfind("\nstop\n", 0, reap))
    assert 0 <= kill < reap < drain, name


# ── Amendment 19 Part U (sr5 HR-3, HR-10, HR-21): every receipt's end is on
# record, and an activation line is written whole ─────────────────────────


def test_a_receipt_without_termination_evidence_invalidates_the_family(tmp_path):
    """HR-3: a detached worker the sandbox kills is reaped by launchd, so
    its receipt holds only its activation. Every receipt needs evidence of
    how its process ended: its own exit line, a family member's reaped
    record, a teardown record or the runner's top-level record."""
    fr = _load("frozen_roots")
    worker = [_receipt_line(pid=300, image="load", ppid=100)]
    for sub in "abcde":
        (tmp_path / sub).mkdir()
    bare = _receipts(tmp_path / "a", {"100.1.2.jsonl": [_receipt_line()],
                                      "300.1.4.jsonl": worker}, [100])
    result = fr.family_verdict(bare, "abc")
    assert result["valid"] is False
    assert any("300.1.4.jsonl" in p and "no termination evidence" in p
               for p in result["problems"]), result["problems"]
    assert result["unterminated"] == [300]
    exited = _receipts(tmp_path / "b", {
        "100.1.2.jsonl": [_receipt_line()],
        "300.1.4.jsonl": worker + [_event("exit", 300, how="exit", counters={})]},
        [100])
    assert fr.family_verdict(exited, "abc")["valid"] is True
    reaped = _receipts(tmp_path / "c", {
        "100.1.2.jsonl": [_receipt_line(), _reaped(300)],
        "300.1.4.jsonl": worker}, [100])
    assert fr.family_verdict(reaped, "abc")["valid"] is True
    torn = _receipts(tmp_path / "d", {"100.1.2.jsonl": [_receipt_line()],
                                      "300.1.4.jsonl": worker}, [100])
    (torn / "teardown.jsonl").write_text(json.dumps(
        {"pid": 300, "signal": 9, "t": 2, "by": "frozen_roots.reap"}) + "\n")
    assert fr.family_verdict(torn, "abc")["valid"] is True
    top = _receipts(tmp_path / "e", {"100.1.2.jsonl": [_receipt_line()],
                                     "300.1.4.jsonl": worker}, [100, 300])
    assert fr.family_verdict(top, "abc")["valid"] is True


def test_an_exec_into_an_admitted_system_program_ends_through_its_parent(tmp_path):
    """Q17: `/usr/bin/security` or `/bin/cp` cannot write an exit line; the
    parent's reaped record of that pid is its termination evidence, and
    nothing else is demanded."""
    fr = _load("frozen_roots")
    d = _receipts(tmp_path, {
        "100.1.2.jsonl": [_receipt_line(), _reaped(101)],
        "101.1.3.jsonl": [_receipt_line(pid=101, image="fork", ppid=100),
                          _event("exec", 101, target="/usr/bin/security",
                                 ppid=100, argv=["security", "find"])]}, [100])
    result = fr.family_verdict(d, "abc")
    assert result["valid"] is True, result["problems"]
    assert result["unterminated"] == []


def test_a_group_teardown_discharges_a_running_system_program(tmp_path):
    """HR-21: a system program still running at teardown is killed with its
    group; the parent never reaps it, but the group kill's per-member
    teardown record is its termination evidence (not a spurious INVALID)."""
    fr = _load("frozen_roots")
    d = _receipts(tmp_path, {"100.1.2.jsonl": [
        _receipt_line(), _event("spawn", **_ENV_SPAWN)]}, [100])
    (d / "teardown.jsonl").write_text("".join(json.dumps(r) + "\n" for r in (
        {"pid": 100, "signal": 9, "group": True, "leader": 100, "t": 2,
         "by": "run-x.sh"},
        {"pid": 7, "signal": 9, "group": True, "leader": 100, "t": 2,
         "by": "run-x.sh"})))
    result = fr.family_verdict(d, "abc")
    assert result["valid"] is True, result["problems"]
    [admitted] = result["admittedSystemPrograms"]
    assert admitted["child"] == 7
    assert admitted["termination"] == {"teardown": True, "signal": 9}


@pytest.mark.parametrize("text, ready", [
    ("", False),
    (_receipt_line() + "\n", True),
    (_receipt_line()[:40], False),                                 # torn
    (_receipt_line() + "\n" + _event("exec", target="/x") + "\n", False),
    (_receipt_line() + "\n" + _event("exec", target="/x") + "\n"
     + _receipt_line()[:30], False),                               # torn re-activation
    (_receipt_line() + "\n" + _event("exec", target="/x") + "\n"
     + _receipt_line() + "\n", True),
    (_receipt_line() + "\n" + _event("exec", target="/x") + "\n"
     + _event("exec-failed", target="/x") + "\n", True),
])
def test_an_activation_is_ready_only_when_complete_and_after_the_last_exec(
        text, ready):
    """HR-10: the self-test's teardown kill must wait for a complete
    `activate` line written after the last `exec` (the framework image of
    Homebrew's python3 stub), never for the receipt file merely existing."""
    fr = _load("frozen_roots")
    assert fr.activation_ready(text) is ready


def test_rootmap_writes_its_activation_line_in_one_write():
    """HR-10: write_activation wrote its line in several raw writes, so a
    reader (the self-test, a kill) could see it torn."""
    import re
    source = (TOOLS / "rootmap.c").read_text()
    body = re.search(r"static void write_activation\(const char \*image\) \{"
                     r"(.*?)\n\}\n", source, re.S).group(1)
    assert len(re.findall(r"\braw_write\(", body)) == 1, body


def test_the_self_test_kills_only_a_ready_top_level_process():
    """HR-10: the top-level teardown case waits for the process's own
    readiness file and a complete activation (`frozen_roots.py ready`),
    never for the receipt file to exist."""
    text = (TOOLS / "frozen_selftest.py").read_text()
    start = text.index("def toplevel_ends(self)")
    body = text[start:text.index("def run(self)", start)]
    assert "frozen_roots.py ready" in body or "frozen_roots.py', 'ready'" in body \
        or "{HERE}/frozen_roots.py ready" in body, body
    assert ".*.jsonl" not in body, body


# ── Amendment 19 Part U (sr5 HR-4/5/6/8/9/11-16/20/21): the verdicts judge
# with the candidate kernel, complete evidence and the sr5 comparators ─────


def _ab_run(tmp_path, name="ab", *, start=1000.0, end=1300.0, **evidence):
    """An admitted live A/B-shaped run with complete window evidence and a
    passing drain, so the I2 statistic decides its verdict."""
    run = tmp_path / name
    run.mkdir()
    _live_inputs(run)
    (run / "window.json").write_text(json.dumps(
        {"workload": "B", "traceStart": start - 100, "warm": start - 50,
         "start": start, "end": end}))
    (run / "dash.log").write_text("ordinary\n")
    (run / "admission.json").write_text(json.dumps({"valid": True}))
    (run / "catchup.json").write_text(json.dumps({"admitted": True,
                                                  "frontier": _FRONTIER}))
    (run / "terminal.json").write_text(json.dumps({"t": end + 6, "alive": 1}))
    _steady_evidence(run, start, end, **evidence)
    return run


def _last_json(text):
    """The verdict's JSON in its output: the last top-level object (the
    analyzer's report may precede it)."""
    import re as _re
    starts = [m.start() for m in _re.finditer(r"(?m)^\{$", text)]
    return json.loads(text[starts[-1]:text.rindex("}") + 1])


def _verdict_out(capsys):
    text = capsys.readouterr().out
    return _last_json(text), text.rstrip().rsplit("\n", 1)[-1]


@pytest.fixture
def _no_drain(monkeypatch):
    def install(workload):
        monkeypatch.setattr(workload, "drain_check",
                            lambda _run: {"status": "PASS", "problems": []})
    return install


def test_the_ab_verdict_judges_with_the_candidate_kernel(tmp_path, capsys, _no_drain):
    workload = _load("workload")
    _no_drain(workload)
    run = _ab_run(tmp_path)
    assert workload.ab_verdict(str(run), "tree") == 0, capsys.readouterr().out
    run = _ab_run(tmp_path, "heavy", rate=17 * MiB // 60)
    assert workload.ab_verdict(str(run), "tree") == 1
    out, word = _verdict_out(capsys)
    assert word == "FAIL" and any("B/min" in p for p in out["problems"])
    assert out["steady"]["kernel"] == "bin/_lib_write_budget.steady_statistic"


@pytest.mark.parametrize("evidence, reason", [
    ({"failed_at": (1150.0,)}, "invalid_samples"),
    ({"gap": (1100.0, 1200.0)}, "insufficient_coverage"),
    ({"reset_at": 1200.0}, "counter_reset"),
])
def test_a_window_the_kernel_cannot_qualify_is_invalid(tmp_path, evidence, reason):
    """HR-8: rolling_rates dropped a failed sample, bridged a gap and
    ignored a counter reset; the candidate kernel's rules make each one
    evidence the window cannot rest on."""
    workload = _load("workload")
    run = _ab_run(tmp_path, **evidence)
    steady = workload.kernel_steady(str(run), 1000.0, 1300.0, 950.0)
    assert steady["valid"] is False
    assert reason in " ".join(steady["problems"]), steady


def test_a_failed_sample_makes_the_ab_verdict_invalid(tmp_path, capsys, _no_drain):
    workload = _load("workload")
    _no_drain(workload)
    run = _ab_run(tmp_path, failed_at=(1150.0,))
    assert workload.ab_verdict(str(run), "tree") == 2
    out, word = _verdict_out(capsys)
    assert word == "INVALID" and "invalid_samples" in " ".join(out["problems"])


def test_the_kernel_bounds_a_deletion_conservatively_from_coarse_samples():
    """The harness samples the counter every 15 s, not at BEGIN and COMMIT:
    a deletion interval inside one sampling interval excludes no more than
    the bytes the operation can have written there (other writes in the
    interval reduce the exclusion, never the window's count)."""
    workload = _load("workload")
    budget = workload._candidate("_lib_write_budget")
    S = 1_000_000_000
    samples = [budget.CounterSample(t * S, t * 1000) for t in range(0, 601, 15)]
    samples[30] = budget.CounterSample(450 * S, 450 * 1000 + 50_000)
    samples = [budget.CounterSample(s.t_ns, s.bytes + (50_000 if s.t_ns > 450 * S else 0))
               if i != 30 else s for i, s in enumerate(samples)]
    [d] = workload.kernel_deletions(samples, [
        {"began": 440.0, "ended": 445.0, "process_write_bytes": 50_000,
         "rows": 10}])
    assert d.end_bytes - d.start_bytes <= 50_000
    assert d.start_bytes >= samples[29].bytes and d.end_bytes <= samples[30].bytes
    [none] = workload.kernel_deletions(samples, [
        {"began": 440.0, "ended": 445.0, "process_write_bytes": None}])
    assert (none.start_bytes, none.end_bytes) == (None, None)


def test_the_c_verdict_checks_the_per_publication_cap(tmp_path, capsys, monkeypatch):
    """HR-5: C condition 3 holds C to both I2 caps; c_verdict checked only
    the B/min one. Ten or more publications with 8 MiB + 1 each fails."""
    workload = _load("workload")
    run = pathlib.Path(_c_run(tmp_path))
    _steady_evidence(run, 0.0, 1800.0, rate=(8 * MiB + 1) * 2 // 15 + 1,
                     pubs_per_sample=1)
    monkeypatch.setattr(workload, "drain_check",
                        lambda _run: {"status": "PASS", "problems": []})
    monkeypatch.setattr(workload, "_load_limits", lambda _t: __import__(
        "types").SimpleNamespace(bytes_per_minute=10 ** 12,
                                 bytes_per_publication=8 * MiB))
    workload.c_verdict(str(run), "tree")
    out, word = _verdict_out(capsys)
    assert any("B/publication" in p for p in out["problems"]), out


def test_etilqs_bytes_count_across_the_whole_traced_family(tmp_path, capsys, _no_drain):
    """HR-11: a child traced under the dashboard's prefix that writes
    etilqs bytes in the window fails the zero-temp condition."""
    workload = _load("workload")
    _no_drain(workload)
    run = _ab_run(tmp_path, temp_pid=77)
    assert workload.ab_verdict(str(run), "tree") == 1
    out, _word = _verdict_out(capsys)
    assert out["etilqsBytes"] == 8192
    assert any("etilqs" in p for p in out["problems"]), out


def test_the_analyzer_measures_from_the_windows_trace_start(tmp_path):
    """HR-21: analyze.py measured its offsets from its first snapshot minus
    5 s while the verdicts passed offsets from traceStart."""
    import subprocess as _sp
    import sys as _sys
    run = _ab_run(tmp_path, burst=(1000.0, 1300.0, 100_000))
    out = _sp.run([_sys.executable, str(TOOLS / "analyze.py"), str(run),
                   "--origin", "900", "--skip", "100", "--until", "400"],
                  capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "window 100s..400s" in out.stdout, out.stdout
    # the whole 30 MB burst of [1000, 1300], not the 6.5 MB a window
    # measured from the first snapshot (665 + 100 s) would catch
    assert "interposer total 30.0 MB" in out.stdout, out.stdout


def test_the_analyzer_skips_a_child_trace_that_holds_no_snapshot(tmp_path):
    """Task X H-1: HR-10's activation line leaves an empty wtrace file for a
    child that exits before its first snapshot; the analyzer's child section
    indexed rows[-1] of it and crashed, so every C window was INVALID. A
    child with snapshots is still reported."""
    import subprocess as _sp
    import sys as _sys
    run = _ab_run(tmp_path)
    (run / "wtrace.4242").write_text("")
    (run / "wtrace.4343").write_text(json.dumps(
        {"t": 1100.0, "pid": 4343, "dropped": 0, "framesDropped": 0,
         "footprintPeak": MiB, "paths": {"/clone/data/x": [2_000_000, 3]}})
        + "\n")
    out = _sp.run([_sys.executable, str(TOOLS / "analyze.py"), str(run),
                   "--origin", "900", "--skip", "100", "--until", "400"],
                  capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "child pid 4242" not in out.stdout
    assert "child pid 4343: 2.0 MB lifetime" in out.stdout, out.stdout


def _measure_loop(script: str) -> str:
    """run-workload.sh's measured-interval loop, from its START line to the
    END line after it."""
    begin = script.index("START_S=$SECONDS")
    return script[begin:script.index("END=$(", begin)]


def test_the_c_rewind_never_blocks_the_sampler():
    """Task X H-2: C's due batch and the 25 h rewind ran synchronously inside
    the sampling loop (about 80 s), leaving a rusage gap past the kernel's
    60 s coverage rule in every C window, baseline and candidate. They run as
    one background family step that the loop waits on after the interval
    and `reap` kills on every exit path."""
    script = (TOOLS / "run-workload.sh").read_text()
    loop = _measure_loop(script)
    assert "rewind_c &" in loop and "RPID=$!" in loop
    assert "run_env" not in loop.split("rewind_c &")[0].split("rewound = 0")[1]
    body = script[script.index("rewind_c() {"):]
    body = body[:body.index("\n}\n")]
    assert body.index("workload.py\" batch") < body.index("--at rewind-25h")
    after = script[script.index("END=$("):]
    assert 'wa_wait rewind "$RPID"' in after.split("sample\n")[0]
    reap = script[script.index("reap() {"):]
    reap = reap[:reap.index("\n}\n")]
    assert 'wa_kill 9 "-$RPID"' in reap and 'wa_wait rewind "$RPID"' in reap


def test_the_measured_samples_are_jittered():
    """Task X H-3: a fixed 15 s poll locks onto the dashboard's tick period,
    so the stored trees it reads can systematically miss the builds that
    carry doctor.gather (the candidate's A runs sampled none of the ~10
    gathers their tick records show). The measured loop sleeps 10-20 s,
    mean 15 s, so no tick phase is skipped by construction."""
    loop = _measure_loop((TOOLS / "run-workload.sh").read_text())
    assert "wa_sleep $((10 + RANDOM % 11))" in loop
    assert "wa_sleep 15" not in loop


def test_the_drain_is_invalid_for_a_missing_store_or_unlogged_frames(tmp_path):
    """HR-13: a store missing from the copier's readings was skipped; -wal
    bytes in the window with no frame records passed rule (i) vacuously; a
    WAL past the interposer's 32-path table is never logged."""
    workload = _load("workload")
    for sub in "abc":
        (tmp_path / sub).mkdir()
    run = pathlib.Path(_drain_run(tmp_path / "a", appended=_steady(),
                                  main_writes=_steady()[::20], mx_frame=0,
                                  n_backfill=0))
    terminal = json.loads((run / "terminal.json").read_text())
    del terminal["drain"]["stores"]["cache.db"]
    (run / "terminal.json").write_text(json.dumps(terminal))
    result = workload.drain_check(str(run))
    assert result["status"] == "INVALID"
    assert any("cache.db: no drain reading" in p for p in result["problems"])
    run = pathlib.Path(_drain_run(tmp_path / "b", appended=[],
                                  main_writes=_steady()[::20], mx_frame=0,
                                  n_backfill=0))
    rows = [json.loads(l) for l in (run / "wtrace.42").read_text().splitlines()]
    for r in rows:
        r["paths"]["/clone/data/conversations.db-wal"] = [
            int(max(0, r["t"] - 1000) * 4120), 1]
    (run / "wtrace.42").write_text("".join(json.dumps(r) + "\n" for r in rows))
    result = workload.drain_check(str(run))
    assert result["status"] == "INVALID"
    assert any("no frame records" in p for p in result["problems"]), result
    run = pathlib.Path(_drain_run(tmp_path / "c", appended=_steady(),
                                  main_writes=_steady()[::20], mx_frame=0,
                                  n_backfill=0))
    with open(run / "wtrace.42.frames", "a") as fh:
        for n in range(32):
            fh.write(json.dumps({"t": 1001.0, "path": f"/x/s{n}.db-wal",
                                 "frame": 0, "pgno": 4096, "commit": 0,
                                 "salt": 1}) + "\n")
    result = workload.drain_check(str(run))
    assert result["status"] == "INVALID"
    assert any("32-path table" in p for p in result["problems"]), result


def test_the_copier_records_a_missing_store():
    drain = _load("terminal_drain")
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        result = drain.drain(pathlib.Path(d), ("conversations.db", "cache.db"),
                             rusage=lambda: {"rc": 0, "dwrite": 0})
    assert result["stores"] == {"conversations.db": {"missing": True},
                                "cache.db": {"missing": True}}


def test_a_frozen_window_without_tripwire_samples_is_invalid(tmp_path):
    """HR-14: with no jsonl.jsonl the frozen leak tripwire reported no
    leak; missing samples are no evidence."""
    workload = _load("workload")
    run = tmp_path / "run"
    run.mkdir()
    (run / "window.json").write_text(json.dumps(
        {"traceStart": 0, "warm": 0, "start": 100, "end": 400}))
    _frozen_evidence(run, tripwire=False)
    problems = workload.input_problems(str(run))
    assert any("leak tripwire has no samples" in p for p in problems), problems


def test_c_condition_one_requires_complete_operation_records():
    """HR-15: a missing balance counted as 0; plans and balances after were
    never checked; a balance after must be the balance before less the
    charge."""
    workload = _load("workload")
    ops = [_op("delete", 1, 1, 1000)] + [_op("reclaim", n, 2 + n, 16)
                                         for n in range(2, 7)]
    assert workload.c_conditions(ops, START, END, _reservation)["valid"] is True
    for field in ("balance_before_bytes", "balance_after_bytes", "plan_digest"):
        broken = [dict(o) for o in ops]
        broken[1 if field == "plan_digest" else 0].pop(field)
        result = workload.c_conditions(broken, START, END, _reservation)
        assert result["valid"] is False and field in " ".join(result["problems"])
    wrong = [dict(o) for o in ops]
    wrong[0]["balance_after_bytes"] += 1
    result = workload.c_conditions(wrong, START, END, _reservation)
    assert result["valid"] is False
    assert "less its charge" in " ".join(result["problems"])
    assert workload.c_continuation_problems({"opSeq": 1}) == [
        "no continuation state at the end (record-end.json has no `continuation`)"]
    assert workload.c_continuation_problems({"continuation": _CONTINUATION}) == []


def test_c_without_the_end_continuation_state_is_invalid(tmp_path, capsys):
    workload = _load("workload")
    run = pathlib.Path(_c_run(tmp_path))
    end = json.loads((run / "record-end.json").read_text())
    del end["continuation"]
    (run / "record-end.json").write_text(json.dumps(end))
    assert workload.c_verdict(str(run), "tree") == 2
    assert "continuation" in capsys.readouterr().out


def test_c_receipts_take_their_geometry_from_an_independent_observation(
        tmp_path, capsys):
    """HR-16: the receipts' geometry came from the product's own record, so
    the charge recomputation was circular. It is now the database header
    read at the window's start plus the WAL's last commit frame before the
    operation; without it the receipt says so and is INVALID."""
    workload = _load("workload")
    run = pathlib.Path(_c_run(tmp_path))
    with open(run / "wtrace.42.frames", "a") as fh:
        fh.write(json.dumps({"t": 50.0, "path": "/x/conversations.db-wal",
                             "frame": 9, "pgno": 7, "commit": 100_000,
                             "salt": 1}) + "\n")
    [receipt] = workload.c_deletion_receipts(
        str(run), [op for op in workload.captured_ops(str(run))
                   if op["phase"] == "delete"],
        workload._candidate("_lib_conversation_retention"))
    assert receipt["geometry"]["pageCount"] == 100_000
    assert receipt["geometrySource"].startswith("observed")
    assert "last WAL commit frame" in receipt["geometrySource"]
    assert receipt["copySource"].startswith("derived")
    start = json.loads((run / "record-start.json").read_text())
    del start["geometry"]
    (run / "record-start.json").write_text(json.dumps(start))
    [receipt] = workload.c_deletion_receipts(
        str(run), [op for op in workload.captured_ops(str(run))
                   if op["phase"] == "delete"],
        workload._candidate("_lib_conversation_retention"))
    assert receipt["verdict"] == "INVALID"
    assert receipt["geometrySource"].startswith("not captured")


def test_the_record_snapshot_observes_geometry_and_continuation(tmp_path):
    workload = _load("workload")
    data = tmp_path / "data"
    data.mkdir()
    conn = sqlite3.connect(data / "conversations.db")
    conn.execute("PRAGMA page_size = 8192")
    conn.execute("CREATE TABLE cache_meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO cache_meta VALUES (?, ?)", (
        "conversation_retention_reclaim_pending",
        json.dumps({"op_seq": 4, "ledger": [{"charged": 10}],
                    "continuation_cutoff": "2026-09-06T00:00:00Z",
                    "balance_bytes": -5, "as_of": "x", "eligible": True,
                    "next_attempt_at": None, "progress": None})))
    conn.commit()
    conn.close()
    snap = workload.record_snapshot(str(tmp_path))
    assert snap["opSeq"] == 4 and snap["charged"] == 10
    assert snap["continuation"]["continuation_cutoff"] == "2026-09-06T00:00:00Z"
    assert snap["geometry"]["pageSize"] == 8192
    assert snap["geometry"]["usableSize"] == 8192
    assert snap["geometry"]["pageCount"] >= 2


def test_the_idle_a_exception_needs_both_window_edges(tmp_path):
    """HR-20: idle-A lacked frozen-C's coverage checks, so an outage at a
    window edge could hide full builds."""
    workload = _load("workload")

    def rec(seq, at, period=30_000):
        return {"seq": seq, "dispatch": "idle", "cold": False,
                "period_ns": period * 1_000_000,
                "published_at": dt.datetime.fromtimestamp(
                    at, dt.timezone.utc).isoformat()}
    full = {n: rec(n, 100 + 30 * n) for n in range(0, 12)}       # 100..430
    rows = lambda records: [r for _s, r in sorted(records.items())
                            if 120 <= workload._epoch(r["published_at"]) <= 400]
    assert workload.window_coverage_problems(full, rows(full), 120, 400) == []
    no_start = {k: v for k, v in full.items() if k != 0}
    assert any("before the window" in p for p in workload.window_coverage_problems(
        no_start, rows(no_start), 120, 400))
    late_end = {k: v for k, v in full.items() if k <= 8}         # last at 340
    assert any("before the end" in p for p in workload.window_coverage_problems(
        late_end, rows(late_end), 120, 400))
    import inspect
    assert "window_coverage_problems" in inspect.getsource(workload.idle_a_exception)


def _settings_run(tmp_path, *, at=650.0, ago=25 * 3600, batch=None):
    import os as _os
    run = tmp_path / "settings"
    run.mkdir(exist_ok=True)
    (run / "window.json").write_text(json.dumps(
        {"workload": "C", "traceStart": 0, "warm": 0, "start": 1000.0,
         "end": 2800.0}))
    (run / "batch.json").write_text(json.dumps(batch if batch is not None else {
        "cutoff": "2026-09-06T00:00:00Z",
        "claude": {"groups": 1, "rows": 10, "largest": 10},
        "codex": {"groups": 1, "rows": 5, "largest": 5}}))
    written = 1000.0 + at
    (run / "rewind.txt").write_text(dt.datetime.fromtimestamp(
        written - ago, dt.timezone.utc).isoformat() + "\n")
    _os.utime(run / "batch.json", (written - 1, written - 1))
    _os.utime(run / "rewind.txt", (written, written))
    return run


@pytest.mark.parametrize("kw, fragment", [
    ({}, None),
    ({"at": 300.0}, "not 600 s"),
    ({"ago": 3600}, "not 25 h"),
    ({"batch": {"cutoff": "x"}}, "not an expiry batch"),
])
def test_c_settings_are_checked_by_content_and_time(tmp_path, kw, fragment):
    """HR-21: c_identity checked the batch and rewind receipts for presence
    only."""
    workload = _load("workload")
    run = _settings_run(tmp_path, **kw)
    _identity, problems = workload.c_identity(str(run))
    settings = [p for p in problems if p.startswith("workload settings")]
    if fragment is None:
        assert settings == [], settings
    else:
        assert any(fragment in p for p in settings), settings


def test_the_input_receipt_records_the_host(tmp_path):
    fr = _load("frozen_roots")
    host = fr.host_identity()
    assert host["name"] and host["machine"]
    receipt = fr.inputs_begin(tmp_path, wtrace=None, rootmap=None)
    assert receipt["host"]["name"] == host["name"]


_BASE_REV = "56e66f07aacacc4b2d90b378190e99724064c1b4"
_CAND_REV = "c" * 40


def test_a_pair_needs_the_baseline_tree_and_another_candidate():
    """HR-9: no pair checked that its baseline run measured 56e66f07a and
    its candidate another tree; a swapped pair or one tree twice passed."""
    workload = _load("workload")
    assert workload.tree_identity_problems(_CAND_REV, _BASE_REV) == []
    assert any("is not 56e66f07a" in p for p in
               workload.tree_identity_problems(_BASE_REV, _CAND_REV))
    assert any("is the baseline" in p for p in
               workload.tree_identity_problems(_BASE_REV, _BASE_REV))
    assert any("no tree revision" in p for p in
               workload.tree_identity_problems(None, _BASE_REV))


def test_a_runner_that_did_not_finish_is_never_admitted(tmp_path):
    workload = _load("workload")
    run = pathlib.Path(_c_run(tmp_path))
    assert workload.admission_problems(str(run)) == []
    for marker in ("interrupted.jsonl", "deadline.json", "orphaned.json"):
        (run / marker).write_text("{}\n")
        assert any(marker in p for p in workload.admission_problems(str(run)))
        (run / marker).unlink()


def test_a_frozen_catch_up_must_carry_its_verification_comparison(tmp_path):
    """HR-12: the frozen catch-up never compared after_verify with
    after_catchup; a receipt without the comparison is not an admission."""
    workload = _load("workload")
    run = pathlib.Path(_c_run(tmp_path))
    _frozen_evidence(run)
    receipt = json.loads((run / "catchup.json").read_text())
    del receipt["frontier"]["verification"]
    (run / "catchup.json").write_text(json.dumps(receipt))
    assert any("verification" in p for p in workload.admission_problems(str(run)))


def test_the_frozen_verification_pass_may_do_no_work():
    catchup = _load("catchup")
    row = {"table": "codex_session_files", "path": "/r/a.jsonl",
           "last_byte_offset": 100, "size_bytes": 100, "inode": 7}
    base = {"rows": {"codex_session_files\t/r/a.jsonl": row},
            "incarnations": {"k": 1}}
    assert catchup.frozen_verification_problems(base, base) == []
    moved = {"rows": {"codex_session_files\t/r/a.jsonl":
                      dict(row, last_byte_offset=120)}, "incarnations": {"k": 1}}
    assert any("changed in verification" in p for p in
               catchup.frozen_verification_problems(base, moved))
    assert any("pruned in verification" in p for p in
               catchup.frozen_verification_problems(base, {"rows": {},
                                                           "incarnations": {"k": 1}}))
    grew = {"rows": {**base["rows"], "session_files\t/r/b.jsonl": {
        "table": "session_files", "path": "/r/b.jsonl"}}, "incarnations": {"k": 1}}
    assert any("first tracked in verification" in p for p in
               catchup.frozen_verification_problems(base, grew))
    assert any("incarnation" in p for p in catchup.frozen_verification_problems(
        base, dict(base, incarnations={"k": 2})))


def test_the_live_post_release_run_is_admitted_on_its_frontier(tmp_path):
    """HR-17: run-live.sh's run (workload L-post) has no catch-up; it is
    admitted on its warm admission and live frontier receipt."""
    workload = _load("workload")
    run = tmp_path / "live"
    run.mkdir()
    _live_inputs(run)
    (run / "window.json").write_text(json.dumps({"workload": "L-post"}))
    (run / "admission.json").write_text(json.dumps({"valid": True}))
    assert any("frontier.json" in p for p in workload.admission_problems(str(run)))
    (run / "frontier.json").write_text(json.dumps({"mode": "live", "files": {}}))
    assert workload.admission_problems(str(run)) == []


# HR-4: the hook runner waits for its workers, and the D verdict re-reads them


def test_the_hook_runner_settles_its_workers_before_reading_them(tmp_path):
    hr = _load("hook_runner")
    (tmp_path / "hook-1.4242").write_text("{}\n")
    clock = iter([0.0, 0.0, 1.0, 999.0])
    import os as _os
    alive = {"n": 0}

    def sleep(_s):
        alive["n"] += 1
        (tmp_path / "hook-1.4242.exit").write_text("{}\n")
    hr._alive = lambda pid: True
    assert hr.settle(tmp_path, "hook-1", 10.0, clock=lambda: next(clock),
                     sleep=sleep) == []
    (tmp_path / "hook-2.4343").write_text("{}\n")
    clock = iter([0.0, 11.0])
    assert hr.settle(tmp_path, "hook-2", 10.0, clock=lambda: next(clock),
                     sleep=lambda _s: None) == [4343]


def test_d_is_invalid_while_a_hook_worker_trace_has_no_exit_snapshot(
        tmp_path, capsys):
    workload = _load("workload")
    rows = [_hook_row(i) for i in range(1, 21)]
    run = pathlib.Path(_d_run(tmp_path, rows))
    (run / "hook-3.9999").write_text(json.dumps(
        {"t": 1.0, "pid": 9999, "dropped": 0, "paths": {}}) + "\n")
    assert workload.d_verdict(str(run)) == 2
    assert "without an exit" in capsys.readouterr().out


def test_b_pair_requires_drained_admission_on_both_sides(tmp_path, capsys):
    """HR-9: b_pair checked input evidence only, never drained admission."""
    workload = _load("workload")
    base = _b_run(tmp_path, "base", [1000, 2000, 3000, 4000])
    cand = _b_run(tmp_path, "cand", [1000, 2000, 3000, 4000])
    assert workload.b_pair(cand, base) == 0
    capsys.readouterr()
    (pathlib.Path(cand) / "catchup.json").write_text(json.dumps(
        {"admitted": False, "gitRev": _CAND_REV}))
    assert workload.b_pair(cand, base) == 2
    out, _w = _verdict_out(capsys)
    assert any("candidate: no catch-up receipt" in p for p in out["problems"])
    assert workload.b_pair(base, cand) == 2                     # swapped
    out, _w = _verdict_out(capsys)
    assert any("is not 56e66f07a" in p for p in out["problems"]), out


def test_the_latency_pair_checks_the_two_trees(tmp_path, capsys):
    latency = _load("latency")
    receipt = _site_receipt(w1=1.0, w3=1.0)
    cand = _latency_run(tmp_path, "cand", receipt)
    base = _latency_run(tmp_path, "base", receipt)
    assert latency.pair(cand, base) == 0
    capsys.readouterr()
    assert latency.pair(base, cand) == 2
    assert "is not 56e66f07a" in capsys.readouterr().out


def test_p1_roles_are_checked_against_the_trees_measured():
    """HR-21: p1-verdict trusted the self-declared --role."""
    replay = _load("projection_replay")
    receipts = _p1_set()
    assert replay.p1_role_problems(receipts[:2], receipts[2:]) == []
    same = [dict(r, treeRev="a" * 40) for r in receipts]
    assert any("same tree" in p for p in replay.p1_role_problems(same[:2], same[2:]))
    mixed = [dict(receipts[0], treeRev="z" * 40)] + receipts[1:]
    assert any("different trees" in p
               for p in replay.p1_role_problems(mixed[:2], mixed[2:]))
    code, result = replay.p1_verdict(same)
    assert code == 2 and any("same tree" in p for p in result["invalid"])


def test_the_r_cov_aggregate_proves_its_segments_chain_on_one_clone(
        tmp_path, capsys):
    """HR-21: the aggregate never checked that its segments were one clone's
    consecutive chunks on one input label."""
    analyze = _load("analyze_op")
    first = _segment(tmp_path, "s1", [_chunk(1), _chunk(2)])
    second = _segment(tmp_path, "s2", [_chunk(3)])
    assert analyze.chain_problems([first, second]) == []
    gap = _segment(tmp_path, "s3", [_chunk(5)])
    assert any("do not continue" in p for p in analyze.chain_problems([first, gap]))
    op = json.loads((pathlib.Path(second) / "op.json").read_text())
    op["ops"][0]["page_count"] += 1
    (pathlib.Path(second) / "op.json").write_text(json.dumps(op))
    assert any("page_count" in p for p in analyze.chain_problems([first, second]))
    (pathlib.Path(second) / "source.json").write_text(json.dumps(
        {"src": "/elsewhere/conversations.db"}))
    assert any("cloned /elsewhere" in p for p in
               analyze.chain_problems([first, second]))
    with pytest.raises(analyze.Invalid):
        analyze.aggregate([first, second], min_chunks=3)


# HR-6: the perf/D pair comparator with sr5's fixed definitions


def _perf_side(run, *, peak, gather=(100.0,), ingest=(2000.0,),
               store_open=(3.0,), regime="doctor", workers=None,
               catchup=400 * MiB, label="B", rev=None):
    """An admitted dashboard run for perf-pair: its terminal footprint, and
    phase trees (one per stored build) with doctor.gather in `regime` and
    ingest with its children."""
    run = pathlib.Path(run)
    run.mkdir(parents=True, exist_ok=True)
    _live_inputs(run)
    (run / "pid").write_text("42")
    (run / "window.json").write_text(json.dumps(
        {"workload": label, "traceStart": 0, "warm": 100, "start": 100,
         "end": 400}))
    (run / "admission.json").write_text(json.dumps({"valid": True}))
    (run / "catchup.json").write_text(json.dumps(
        {"admitted": True, "frontier": _FRONTIER,
         "gitRev": rev or (_BASE_REV if run.name.startswith("base") else _CAND_REV),
         "footprintPeakBytes": catchup}))
    (run / "rusage.jsonl").write_text(json.dumps(
        {"t": 390, "pid": 42, "rc": 0, "resident": peak,
         "footprint_peak": peak}) + "\n")
    (run / "terminal.json").write_text(json.dumps({"t": 395, "alive": 1}))
    for pid, value in (workers or {}).items():
        (run / f"wtrace.{pid}.exit").write_text(json.dumps(
            {"t": 300, "pid": pid, "footprintPeak": value, "paths": {}}) + "\n")
    stamp = lambda at: dt.datetime.fromtimestamp(at, dt.timezone.utc).isoformat()
    records = [{"seq": i + 1, "dispatch": "full", "cold": False,
                "duration_ns": 1_000_000_000, "period_ns": 5_000_000_000,
                "published_at": stamp(120 + 20 * i)} for i in range(4)]
    for n, (g, i, so) in enumerate(zip(gather, ingest, store_open)):
        (run / f"perf-{n:05d}.json").write_text(json.dumps({"diagnostic": {
            "tick": {"records": records},
            "phases": {"name": "snapshot", "elapsed_ms": 900.0, "children": [
                {"name": regime, "elapsed_ms": g + 1, "children": [
                    {"name": "doctor.gather", "elapsed_ms": g, "children": []}]}]},
            "generated_at": stamp(150 + n),
            "ingest_phases": {"name": "ingest", "elapsed_ms": i, "children": [
                {"name": "ingest.claude", "elapsed_ms": i / 4, "children": []},
                {"name": "ingest.store_open", "elapsed_ms": so, "children": []}]},
            "ingest_generated_at": stamp(160 + n)}}))
    return str(run)


def _perf_pair(workload, capsys, cand, base):
    code = workload.perf_pair(cand, base)
    out, word = _verdict_out(capsys)
    return code, out, word


def test_perf_pair_passes_a_matched_candidate(tmp_path, capsys):
    workload = _load("workload")
    base = _perf_side(tmp_path / "base", peak=900 * MiB)
    cand = _perf_side(tmp_path / "cand", peak=960 * MiB, catchup=400 * MiB)
    code, out, word = _perf_pair(workload, capsys, cand, base)
    assert (code, word) == (0, "PASS"), out
    assert out["footprint"]["deltaBytes"] == 64 * MiB
    assert set(out["ingest"]) == {"ingest", "ingest/ingest.claude",
                                  "ingest/ingest.store_open"}
    assert set(out["doctorGather"]) == {"snapshot/doctor/doctor.gather"}


@pytest.mark.parametrize("cand_kw, base_kw, label, fragment", [
    # footprint: the A/B delta and the ceiling, each on its own
    ({"peak": 965 * MiB}, {"peak": 900 * MiB}, "B", "+ 67108864"),
    ({"peak": 1537 * MiB}, {"peak": 1500 * MiB}, "B", "process ceiling"),
    ({"peak": 900 * MiB, "workers": {77: 1537 * MiB}}, {"peak": 900 * MiB}, "B",
     "worker 77"),
    ({"peak": 1413 * MiB}, {"peak": 900 * MiB}, "C", "+ 536870912"),
    # doctor.gather: median <= the baseline's
    ({"peak": 900 * MiB, "gather": (101.0,)}, {"peak": 900 * MiB}, "B",
     "doctor.gather"),
    # ingest: 1.2 x, parent and every ingest.* child, store_open included,
    # no small-millisecond exemption
    ({"peak": 900 * MiB, "store_open": (3.7,)}, {"peak": 900 * MiB}, "B",
     "ingest.store_open"),
    ({"peak": 900 * MiB, "ingest": (2401.0,)}, {"peak": 900 * MiB}, "B",
     "ingest candidate median"),
])
def test_perf_pair_fails_each_limit_on_its_own(tmp_path, capsys, cand_kw,
                                              base_kw, label, fragment):
    workload = _load("workload")
    base = _perf_side(tmp_path / "base", label=label, **base_kw)
    cand = _perf_side(tmp_path / "cand", label=label, **cand_kw)
    code, out, word = _perf_pair(workload, capsys, cand, base)
    assert (code, word) == (1, "FAIL"), out
    assert any(fragment in p for p in out["problems"]), out["problems"]


@pytest.mark.parametrize("mutate, fragment", [
    (lambda c, b: (pathlib.Path(c) / "rusage.jsonl").write_text(""),
     "no terminal lifetime peak"),
    (lambda c, b: _perf_side(c, peak=900 * MiB, regime="idle-decision"),
     "measured on the"),
    (lambda c, b: (pathlib.Path(c) / "admission.json").write_text(
        json.dumps({"valid": False, "reason": "x"})), "admission refused"),
    (lambda c, b: (pathlib.Path(b) / "catchup.json").write_text(json.dumps(
        {"admitted": True, "frontier": _FRONTIER, "gitRev": _CAND_REV})),
     "is not 56e66f07a"),
])
def test_perf_pair_missing_evidence_is_invalid(tmp_path, capsys, mutate, fragment):
    workload = _load("workload")
    base = _perf_side(tmp_path / "base", peak=900 * MiB)
    cand = _perf_side(tmp_path / "cand", peak=900 * MiB)
    mutate(cand, base)
    code, out, word = _perf_pair(workload, capsys, cand, base)
    assert (code, word) == (2, "INVALID"), out
    assert any(fragment in p for p in out["problems"]), out["problems"]


def _hook_wall_run(tmp_path, name, walls, *, worker_late=0.0, open_trace=False,
                   peak=50 * MiB):
    rows = [_hook_row(i, start=100.0 * i, end=100.0 * i + w, wallS=w)
            for i, w in enumerate(walls, start=1)]
    run = pathlib.Path(_d_run(tmp_path / name, rows))
    receipt = json.loads((run / "catchup.json").read_text())
    receipt["gitRev"] = _BASE_REV if name.startswith("base") else _CAND_REV
    (run / "catchup.json").write_text(json.dumps(receipt))
    for i, w in enumerate(walls, start=1):
        (run / f"hook-{i}.{1000 + i}").write_text("{}\n")
        (run / f"hook-{i}.{1000 + i}.exit").write_text(json.dumps(
            {"t": 100.0 * i + w + (worker_late if i == 1 else 0.0),
             "pid": 1000 + i, "footprintPeak": peak, "paths": {}}) + "\n")
    if open_trace:
        (run / "hook-2.5555").write_text("{}\n")
    return str(run)


def test_perf_pair_judges_d_on_the_mean_end_to_end_hook_time(tmp_path, capsys):
    """sr5 HR-6: the arithmetic mean end-to-end wall time per scheduled
    invocation, a detached worker's tail included; median and max are
    diagnostics only (the candidate's median here is lower, its mean
    higher)."""
    workload = _load("workload")
    base = _hook_wall_run(tmp_path, "base", [2.0] * 20)
    cand = _hook_wall_run(tmp_path, "cand", [1.9] * 19 + [4.67])
    code, out, word = _perf_pair(workload, capsys, cand, base)
    assert (code, word) == (1, "FAIL"), out
    assert out["hookWall"]["candidate"]["medianS"] < 2.0
    assert any("hook wall time" in p for p in out["problems"])
    late = _hook_wall_run(tmp_path, "cand2", [1.9] * 20, worker_late=10.0)
    code, out, _w = _perf_pair(workload, capsys, late, base)
    assert out["hookWall"]["candidate"]["maxS"] == pytest.approx(11.9)
    fast = _hook_wall_run(tmp_path, "cand3", [1.9] * 20)
    assert _perf_pair(workload, capsys, fast, base)[0] == 0
    open_ = _hook_wall_run(tmp_path, "cand4", [1.9] * 20, open_trace=True)
    code, out, word = _perf_pair(workload, capsys, open_, base)
    assert (code, word) == (2, "INVALID") and any(
        "without an exit snapshot" in p for p in out["problems"])
    heavy = _hook_wall_run(tmp_path, "cand5", [1.9] * 20, peak=1537 * MiB)
    code, out, _w = _perf_pair(workload, capsys, heavy, base)
    assert code == 1 and any("process ceiling" in p for p in out["problems"])


def test_the_perf_pair_is_on_the_workload_cli(tmp_path, capsys):
    workload = _load("workload")
    base = _perf_side(tmp_path / "base", peak=900 * MiB)
    cand = _perf_side(tmp_path / "cand", peak=900 * MiB)
    assert workload.main(["perf-pair", cand, base]) == 0


def test_the_analyzer_self_test_passes_with_the_family_termination_rule(tmp_path):
    """_prep.sh runs analyze_selftest.py before every run and refuses to
    measure when it fails. Its frozen fixtures must carry what the family
    judge now requires (HR-3: termination evidence per receipt), and it
    proves a receipt without any is INVALID."""
    import subprocess as _sp
    import sys as _sys
    out = _sp.run([_sys.executable, str(TOOLS / "analyze_selftest.py"),
                   str(tmp_path / "ast")], capture_output=True, text=True,
                  timeout=100)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "analyze-selftest: PASS" in out.stdout
    assert "frozen-unterminated" in (TOOLS / "analyze_selftest.py").read_text()
