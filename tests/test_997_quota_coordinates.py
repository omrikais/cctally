"""#997 T-A5 — the final rebuild's quota leg reads coordinates (spec D7, T7).

The read pass used to retain every Codex quota observation's raw line until
the cache leg ran: 1.64 GB on the maintainer's journal, held across the whole
read pass. It now retains `(segment, offset, length)` per observation and the
leg re-reads one chunk at a time. These tests pin the memory bound, the
equality with the raw-bytes path (including an elided run of adjacent
quota-only segments and a restart after a destructive clear), the failure of
a short read, and the new longest-single-hold field.
"""
from __future__ import annotations

import gc
import importlib
import json
import pathlib
import sqlite3
import time
import tracemalloc

import pytest

import journal_fixture_496_s5b as F
from conftest import load_script, redirect_paths


def _load(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    jr = importlib.import_module("_cctally_journal")
    jl = importlib.import_module("_lib_journal")
    return ns, jr, jl


def _rebuild(jr):
    return jr.rebuild_stats_index(
        context=jr.RebuildContext(trigger="test-fixture"))


def _journal_dir():
    return pathlib.Path(importlib.import_module("_cctally_core").JOURNAL_DIR)


def _write_quota_segments(count, *, segments=4, start=0):
    """``count`` Codex quota observations over ``segments`` files."""
    per = -(-count // segments)
    written = 0
    names = []
    for index in range(segments):
        name = f"observations-2025-{index + 1:02d}.jsonl"
        n = min(per, count - written)
        if n <= 0:
            break
        F._append(_journal_dir(), name, [
            F.codex_quota_obs(line_offset=start + written + k,
                              used_percent=10.0 + (written + k) % 50)
            for k in range(n)])
        written += n
        names.append(name)
    return names


def _originals(jr) -> dict:
    """The unpatched seams of the module set ``_load`` just imported.

    `load_script` re-imports the sibling modules, so each scenario patches its
    own fresh `_cctally_journal` and nothing has to `monkeypatch.undo()`,
    which would also undo the path redirection.
    """
    return {
        "leg": jr._rebuild_quota_cache_leg_raw,
        "apply": jr._apply_quota_records,
        "plan": jr.plan_segment_elision,
        "open": jr._open_segment_for_read,
        "between": jr._RECOVERY_BETWEEN_CHUNKS,
    }


def _measure_leg_entry(jr, monkeypatch, originals):
    """Traced heap at the quota leg's entry, with its input still alive."""
    seen: list = []
    real = originals["leg"]

    def measuring(quota_raw, *args, **kwargs):
        gc.collect()
        seen.append((len(quota_raw), tracemalloc.get_traced_memory()[0]))
        return real(quota_raw, *args, **kwargs)

    monkeypatch.setattr(jr, "_rebuild_quota_cache_leg_raw", measuring)
    return seen


def test_quota_leg_retains_coordinates_only(tmp_path, monkeypatch):
    """Spec T7: what the read pass retains for the leg grows by at most 64
    bytes per quota observation (the raw-bytes path retains a whole line,
    several hundred bytes). Measured as the difference of the traced heap at
    the leg's entry between a 2,000- and a 22,000-observation journal, so the
    fixed cost of a rebuild cancels."""
    small, large = 2_000, 22_000
    readings = {}
    for label, count in (("small", small), ("large", large)):
        ns, jr, _jl = _load(tmp_path / label, monkeypatch)
        originals = _originals(jr)
        ns["open_cache_db"]().close()
        _write_quota_segments(count)
        seen = _measure_leg_entry(jr, monkeypatch, originals)
        # Traced from the rebuild's start only: `load_script` re-imports
        # every module, and an import inside the window would swamp the
        # per-observation difference this measures.
        gc.collect()
        tracemalloc.start()
        try:
            result = _rebuild(jr)
        finally:
            tracemalloc.stop()
        assert result.quota_cache_coverage["status"] == "recovered"
        assert len(seen) == 1 and seen[0][0] == count, seen
        readings[label] = seen[0][1]
    per_observation = (readings["large"] - readings["small"]) / (large - small)
    assert per_observation <= 64, (
        f"the read pass retains {per_observation:.1f} bytes per quota "
        "observation for the leg")


# --------------------------------------------------------------------------
# Equality with the raw-bytes path
# --------------------------------------------------------------------------

#: E E N E E N: two runs of adjacent quota-only segments around a read
#: segment that carries a quota observation beside a retained `evt`.
ADJACENT = tuple(f"observations-2025-{m:02d}.jsonl" for m in range(1, 7))


def _build_adjacent_runs():
    jd = _journal_dir()
    offset = 0

    def quota(n):
        nonlocal offset
        out = [F.codex_quota_obs(line_offset=offset + k,
                                 used_percent=10.0 + offset + k)
               for k in range(n)]
        offset += n
        return out

    F._append(jd, ADJACENT[0], quota(3))
    F._append(jd, ADJACENT[1], quota(2))
    F._append(jd, ADJACENT[2], quota(1) + [F._evt("sa:997-middle", 30.0)]
              + quota(1))
    F._append(jd, ADJACENT[3], quota(2))
    F._append(jd, ADJACENT[4], quota(3))
    F._append(jd, ADJACENT[5], [
        F._claude_obs(F.LATER, 11.0), F._evt("sa:997-tail", 20.0)]
        + quota(2))
    return [ADJACENT[0], ADJACENT[1], ADJACENT[3], ADJACENT[4]]


def _advance_physical_seq(ns):
    conn = ns["open_cache_db"]()
    try:
        conn.execute(
            "INSERT INTO cache_meta (key, value) "
            "VALUES ('codex_physical_mutation_seq', '1000') "
            "ON CONFLICT(key) DO UPDATE SET "
            "value = CAST(cache_meta.value AS INTEGER) + 1000")
        conn.commit()
    finally:
        conn.close()


def _cache_dump(ns, *, across_a_clear=False) -> dict:
    """Every cache.db table, rows sorted.

    ``across_a_clear`` compares a pass a destructive clear interrupted with
    one nothing interrupted: the clear legitimately moves the physical
    mutation sequence (and the certificate's copy of it), appends to the
    trigger-maintained change ledgers, and re-inserted rows get fresh
    surrogate ids, so those, and only those, are set aside. Two #1033 copies
    of the same facts go with them: the clear advances the reset-anchor
    evidence invalidation generation by design, and the evidence summary's
    ``min_id`` is the surrogate id of its minimum row. Its logical
    coordinates (``min_source_path``, ``min_line_offset``) stay compared.
    """
    conn = ns["open_cache_db"]()
    try:
        out = {}
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        for table in tables:
            if table == "schema_migrations":
                continue
            if across_a_clear and table.endswith("_change_log"):
                continue
            cursor = conn.execute(f'SELECT * FROM "{table}"')
            columns = [d[0] for d in cursor.description]
            rows = []
            for row in cursor.fetchall():
                item = dict(zip(columns, row))
                if across_a_clear:
                    item.pop("id", None)
                    if table == "codex_reset_anchor_evidence":
                        item.pop("min_id", None)
                    if table == "cache_meta":
                        if str(item.get("key")).endswith("_mutation_seq"):
                            continue
                        if item.get("key") == (
                                "codex_reset_anchor_evidence_generation"):
                            continue
                        if item.get("key") == (
                                "codex_journal_coverage_certificate"):
                            cert = json.loads(item["value"])
                            cert.pop("physicalMutationSeq", None)
                            item["value"] = cert
                rows.append(item)
            out[table] = sorted(rows, key=repr)
        return out
    finally:
        conn.close()


def _as_raw_path(jr, monkeypatch, originals):
    """Run the leg over materialized bytes, which is the pre-#997 path."""
    real = originals["leg"]

    def raw(quota_raw, *args, **kwargs):
        source = getattr(jr, "QuotaLineSource", None)
        if source is not None and isinstance(quota_raw, source):
            quota_raw = quota_raw.lines(0, len(quota_raw))
        return real(list(quota_raw), *args, **kwargs)

    monkeypatch.setattr(jr, "_rebuild_quota_cache_leg_raw", raw)


def _refill_run(tmp_path, monkeypatch, *, raw, builder):
    ns, jr, _jl = _load(tmp_path, monkeypatch)
    originals = _originals(jr)
    ns["open_cache_db"]().close()
    elidable = builder()
    if raw:
        _as_raw_path(jr, monkeypatch, originals)
    replayed: list = []
    real_apply = originals["apply"]

    def recording(cache, records, *args, **kwargs):
        records = list(records)
        replayed.extend(
            int((rec.get("payload") or {}).get("line_offset", -1))
            for rec in records)
        return real_apply(cache, records, *args, **kwargs)

    monkeypatch.setattr(jr, "_apply_quota_records", recording)
    first = _rebuild(jr)
    assert first.quota_cache_coverage["status"] == "recovered"
    conn = ns["open_cache_db"]()
    try:
        conn.execute("DELETE FROM quota_window_snapshots")
        conn.commit()
    finally:
        conn.close()
    replayed.clear()
    real_plan = originals["plan"]

    def planned(segments, high_water):
        plan = real_plan(segments, high_water)
        _advance_physical_seq(ns)
        return plan

    monkeypatch.setattr(jr, "plan_segment_elision", planned)
    second = _rebuild(jr)
    return {
        "elided": second.traversal["elision"]["elidedSegments"],
        "elidable": len(elidable),
        "coverage": second.quota_cache_coverage,
        "replayed": list(replayed),
        "cache": _cache_dump(ns),
    }


@pytest.mark.parametrize("layout", ["adjacent-runs", "interleaved"])
def test_quota_leg_cache_equal_to_raw_path(tmp_path, monkeypatch, layout):
    """Spec T7: the coordinate path leaves the same quota rows, attribution
    map and coverage certificate as the raw-bytes path, in the same replay
    order, including when the leg refills an elided run of adjacent
    quota-only segments."""
    if layout == "adjacent-runs":
        builder = _build_adjacent_runs
    else:
        def builder():
            core = importlib.import_module("_cctally_core")
            return F.build_interleaved_elision_scenario(
                core.APP_DIR)["elidable"]
    coords = _refill_run(tmp_path / "coords", monkeypatch, raw=False,
                         builder=builder)
    raw = _refill_run(tmp_path / "raw", monkeypatch, raw=True,
                      builder=builder)
    assert coords["elided"] == coords["elidable"] > 0, coords["elided"]
    assert coords["coverage"]["elisionRefill"]["complete"] is True
    assert coords["coverage"]["status"] == "recovered"
    assert coords["replayed"] == sorted(coords["replayed"]), (
        "the refill replayed out of journal order")
    assert coords["replayed"] == raw["replayed"]
    assert coords["coverage"] == raw["coverage"]
    assert coords["cache"] == raw["cache"]


# --------------------------------------------------------------------------
# Restart, short read, longest hold
# --------------------------------------------------------------------------

def _tiny_caps(jr, monkeypatch, records=2):
    monkeypatch.setattr(jr, "_RECOVERY_CHUNK_RECORDS", records)
    monkeypatch.setattr(jr, "_RECOVERY_CHUNK_BYTES", 1 << 30)


def _restart_run(tmp_path, monkeypatch, *, clear):
    ns, jr, _jl = _load(tmp_path, monkeypatch)
    originals = _originals(jr)
    ns["open_cache_db"]().close()
    _write_quota_segments(9, segments=3)
    _tiny_caps(jr, monkeypatch)
    cache_mod = importlib.import_module("_cctally_cache")
    fired: list = []
    reads: list = []
    real_open = originals["open"]

    def counted(seg_path, *args, **kwargs):
        reads.append(pathlib.Path(seg_path).name)
        return real_open(seg_path, *args, **kwargs)

    def clear_once(chunks):
        if not clear or fired or chunks < 2:
            return
        fired.append(chunks)
        reads.clear()
        conn = ns["open_cache_db"]()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cache_mod._clear_codex_derived_rows(conn)
            conn.commit()
        finally:
            conn.close()

    monkeypatch.setattr(jr, "_RECOVERY_BETWEEN_CHUNKS", clear_once)
    monkeypatch.setattr(jr, "_open_segment_for_read", counted)
    result = _rebuild(jr)
    return {
        "fired": fired,
        "reads_after_clear": list(reads),
        "coverage": result.quota_cache_coverage,
        "cache": _cache_dump(ns, across_a_clear=True),
    }


def test_quota_leg_restart_after_destructive_clear(tmp_path, monkeypatch):
    """Spec T7: a destructive clear between two chunks restarts recovery at
    chunk zero, which re-reads the observations from the segments, and the
    result and certificate equal an undisturbed pass."""
    disturbed = _restart_run(tmp_path / "clear", monkeypatch, clear=True)
    calm = _restart_run(tmp_path / "calm", monkeypatch, clear=False)
    assert disturbed["fired"], "the clear never ran between two chunks"
    assert disturbed["coverage"]["restarts"] == 1
    assert disturbed["coverage"]["status"] == "recovered"
    assert disturbed["coverage"]["complete"] is True
    assert "observations-2025-01.jsonl" in disturbed["reads_after_clear"], (
        "the restart did not re-read chunk zero from the journal")
    calm_coverage = dict(calm["coverage"])
    calm_coverage["restarts"] = 1
    calm_coverage["concurrentWriter"] = disturbed["coverage"][
        "concurrentWriter"]
    assert disturbed["coverage"] == calm_coverage
    assert disturbed["cache"] == calm["cache"]


@pytest.mark.parametrize("damage", ["truncated", "reframed"])
def test_quota_leg_truncated_segment_incomplete(tmp_path, monkeypatch,
                                                damage):
    """Spec T7: a segment that no longer holds the bytes its coordinates name
    when the leg re-reads them fails the leg: `complete` is False and no
    certificate is minted over observations nobody applied."""
    ns, jr, _jl = _load(tmp_path, monkeypatch)
    ns["open_cache_db"]().close()
    names = _write_quota_segments(6, segments=2)
    target = _journal_dir() / names[-1]
    real = jr._rebuild_quota_cache_leg_raw

    def damaged(quota_raw, *args, **kwargs):
        original = target.read_bytes()
        if damage == "truncated":
            target.write_bytes(original[:-40])
        else:
            # Same length, one line boundary moved: a newline becomes a space.
            cut = original.index(b"\n")
            target.write_bytes(original[:cut] + b" " + original[cut + 1:])
        try:
            return real(quota_raw, *args, **kwargs)
        finally:
            target.write_bytes(original)

    monkeypatch.setattr(jr, "_rebuild_quota_cache_leg_raw", damaged)
    result = _rebuild(jr)
    coverage = result.quota_cache_coverage
    assert coverage["complete"] is False, coverage
    assert coverage["status"] != "recovered", coverage
    assert coverage.get("lineSourceFailures"), coverage
    cache = importlib.import_module("_cctally_cache")
    conn = ns["open_cache_db"]()
    try:
        assert cache.load_codex_journal_coverage_certificate(conn) is None
    finally:
        conn.close()


def _latest_record():
    core = importlib.import_module("_cctally_core")
    paths = sorted(pathlib.Path(core.LOG_DIR).glob("stats-rebuild-*.json"),
                   key=lambda p: p.stat().st_mtime_ns)
    assert paths, "no rebuild record was written"
    return json.loads(paths[-1].read_text())


def test_quota_lock_max_hold_recorded(tmp_path, monkeypatch):
    """Spec D7: the record keeps the cumulative hold and gains the longest
    single hold across chunks."""
    ns, jr, _jl = _load(tmp_path, monkeypatch)
    ns["open_cache_db"]().close()
    _write_quota_segments(6, segments=2)
    _tiny_caps(jr, monkeypatch)
    calls = {"n": 0}
    real_apply = jr._apply_quota_records

    def slow_second(cache, records, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            time.sleep(0.3)
        return real_apply(cache, records, *args, **kwargs)

    monkeypatch.setattr(jr, "_apply_quota_records", slow_second)
    _rebuild(jr)
    record = _latest_record()
    longest = record["quotaLockMaxHoldSeconds"]
    total = record["quotaLockHoldSeconds"]
    assert calls["n"] == 3, calls
    assert longest >= 0.3, record
    assert total > longest, record


# --------------------------------------------------------------------------
# #1004 T5 / T7 — the rebuild's retained records stream from coordinates
# --------------------------------------------------------------------------

#: The legacy (pre-#341) decisions the journal's own accounts cutover maps
#: to a real account: their payloads carry no `account_key`, so the rebuild
#: must normalize them before any consumer reads them.
LEGACY_IDS = ("sa:1004-legacy-a", "sa:1004-legacy-b")


def _mixed_rebuild_journal(jr, J, monkeypatch):
    """Legacy history under an accounts cutover (two legacy evts, a Codex
    quota observation and a malformed line), the account-bound boundary,
    current records, an admitted current correction batch, and an excluded
    legacy observation and legacy correction batch past the boundary."""
    import test_journal_admission as A
    jd = _journal_dir()
    earlier = "observations-2026-09.jsonl"
    F._append(jd, earlier, [
        F._legacy_evt(LEGACY_IDS[0], 20.0),
        F._cutover_op(F.CUTOVER_ACCOUNT),
        F._legacy_evt(LEGACY_IDS[1], 30.0),
        F.quota_obs(F.AT, 1),
    ])
    with open(jd / earlier, "ab") as handle:
        handle.write(b'{"t":"obs","not json\n')
    monkeypatch.setattr(jr, "_utc_now", lambda: A.FIXED)
    A._seed_with_cutover()
    legacy_obs = J.unstamped_record(
        A._usage_obs(J, 70.0, "2026-10-09T11:00:00Z"))
    legacy_batch = J.legacy_writer_view().make_correction_batch(
        batch_id="rederive:claude-usage:1004-legacy", family="claude-usage",
        at="2026-10-09T11:00:00Z",
        actions=[{"action": "tombstone", "id": LEGACY_IDS[1], "rev": 1,
                  "at": "2026-10-09T11:00:00Z", "payload": None}])
    current_batch = J.make_correction_batch(
        batch_id="rederive:claude-usage:1004-current", family="claude-usage",
        at="2026-10-09T11:30:00Z",
        actions=[{"action": "tombstone", "id": LEGACY_IDS[0], "rev": 1,
                  "at": "2026-10-09T11:30:00Z", "payload": None}])
    F._append(jd, A.SEGMENT, [legacy_obs, *legacy_batch, *current_batch,
                              F.quota_obs("2026-10-09T11:45:00Z", 2)])


def _capture_rebuild_records(jr, monkeypatch, real, sink, check=None):
    """Record the rebuild's retained records at the quota leg's entry (after
    normalization, before any fold): their type, a deep copy of every
    position and which positions are streamed. ``check(decoded)`` runs on
    the live sequence there; ``real`` is the unpatched leg."""
    import copy
    import _lib_retained_records as RR

    def capturing(quota_raw, decoded, *args, **kwargs):
        streamed = None
        if isinstance(decoded, RR.RetainedRecords):
            streamed = [decoded.is_streamed(i) for i in range(len(decoded))]
        sink.append({"type": type(decoded),
                     "records": [copy.deepcopy(r) for r in decoded],
                     "streamed": streamed})
        if check is not None:
            check(decoded)
        return real(quota_raw, decoded, *args, **kwargs)

    monkeypatch.setattr(jr, "_rebuild_quota_cache_leg_raw", capturing)


def _rewrite_same_length(position_line, old: bytes, new: bytes):
    segment, start, length = position_line
    path = _journal_dir() / segment
    data = path.read_bytes()
    line = data[start:start + length]
    assert old in line and len(old) == len(new)
    path.write_bytes(data[:start] + line.replace(old, new, 1)
                     + data[start + length:])
    return path, data


def test_rebuild_retained_records_stream_equal_to_list(tmp_path, monkeypatch):
    """#1004 T5 and T7 (the rebuild reader): every `RETAINED_RECORD_TYPES`
    record is a streamed slot and every other position keeps its `None`
    slot; at the quota leg's entry the sequence equals today's list (the
    `MATERIALIZE_FOR_TESTS` switch) element for element, in iteration,
    indexing, slices and a second pass, including the legacy decisions the
    journal's accounts cutover normalizes to its real account. A same-length
    rewrite of a streamed line after the read pass raises the rebuild's
    `JournalError`, which framing alone would let through. Detects a
    rebuild that folds a record other than the one it read, or that folds a
    legacy decision under `unattributed` because normalization stopped
    reaching re-decoded records."""
    import _lib_retained_records as RR
    _ns, jr, J = _load(tmp_path, monkeypatch)
    _mixed_rebuild_journal(jr, J, monkeypatch)
    checked = {}

    def check(decoded):
        listed = list(decoded)
        assert list(decoded) == listed, "a second pass differs"
        assert [decoded[i] for i in range(len(decoded))] == listed
        for cut in (slice(2, None), slice(None, 5), slice(1, 12, 3),
                    slice(-4, None)):
            assert list(decoded[cut]) == listed[cut], cut
        legacy = [i for i, record in enumerate(listed)
                  if isinstance(record, dict)
                  and record.get("id") == LEGACY_IDS[1]
                  and record.get("t") == "evt"]
        assert len(legacy) == 1, legacy
        [index] = legacy
        assert decoded.is_streamed(index)
        path, data = _rewrite_same_length(
            decoded.coordinates.line(index), b'"weekly_percent":30.0',
            b'"weekly_percent":90.0')
        try:
            with pytest.raises(jr.JournalError, match="changed"):
                decoded[index]
        finally:
            path.write_bytes(data)
        checked["rewrite"] = True

    seen: list = []
    real = jr._rebuild_quota_cache_leg_raw
    _capture_rebuild_records(jr, monkeypatch, real, seen, check)
    _rebuild(jr)
    with monkeypatch.context() as patch:
        patch.setattr(RR, "MATERIALIZE_FOR_TESTS", True)
        _capture_rebuild_records(jr, patch, real, seen)
        _rebuild(jr)
    streamed, listed = seen
    assert checked == {"rewrite": True}
    assert streamed["type"] is RR.RetainedRecords, streamed["type"]
    assert listed["type"] is list, listed["type"]
    assert streamed["records"] == listed["records"]
    retained = jr._lib_journal_router.RETAINED_RECORD_TYPES
    assert streamed["streamed"] == [
        isinstance(record, dict) and record.get("t") in retained
        for record in listed["records"]]
    assert all(record is None for record, is_streamed in zip(
        streamed["records"], streamed["streamed"]) if not is_streamed)
    # T7 non-vacuity: both legacy decisions reach the consumers under the
    # cutover's real account, not unstamped.
    by_id = {record["id"]: record for record in streamed["records"]
             if isinstance(record, dict) and record.get("t") == "evt"}
    for event_id in LEGACY_IDS:
        assert by_id[event_id]["payload"]["account_key"] == (
            F.CUTOVER_ACCOUNT), by_id[event_id]
    assert sum(1 for record in streamed["records"]
               if isinstance(record, dict)
               and record.get("t") == "correction") >= 1
