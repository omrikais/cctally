"""#997 T-C3 — release, compaction and the streamed cache fingerprint
(D14, D15, D16; T14, T15, T16).

After D12 the resident peaks moved to the next consumers (spec §2.4
addendum): the final rebuild still held its whole read-pass state
(`decoded`, `effective`, `stream`, `structural`) while the quota projection
loaded its observations; retained decoded records carried their own copy of
every key and short value, because CPython's decoder shares keys only within
one `loads` call; and `_cache_fingerprint` materialized every cache row and one
JSON document of them before hashing.
"""
from __future__ import annotations

import gc
import hashlib
import importlib
import json
import sqlite3
import tracemalloc
import weakref

import pytest

import journal_fixture_496_s5 as S5
import test_997_window_and_stubs as W
from conftest import load_script, redirect_paths


def _lib():
    return importlib.import_module("_lib_journal")


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


@pytest.fixture
def no_append_path_cutover(monkeypatch):
    monkeypatch.setenv("CCTALLY_TEST_NO_ACCOUNT_BOUND_CUTOVER", "1")


# --------------------------------------------------------------------------
# T14 — the rebuild releases its read-pass state before the quota projection
# bundle loads
# --------------------------------------------------------------------------


class _Probe(dict):
    """A weak-referenceable decoded record, otherwise a plain dict."""


def _tainted(ns):
    core = importlib.import_module("_cctally_core")
    return S5.build_tainted(core.APP_DIR, seed_cache=False)


#: Sept 2026, so appended lines land in the tainted fixture's last segment
#: (`S5.SEG_C`) and the prefix evidence of its resolution ops stays intact.
_SEG_C_NOW = __import__("datetime").datetime(
    2026, 9, 15, 12, 0, tzinfo=__import__("datetime").timezone.utc)


def _five_hour_accept(event_id, captured, weekly, window):
    """A `snapshot_accept` decision that opens a five-hour block, so the
    open-block projection has first observations to receive."""
    return _lib().make_evt("snapshot_accept", event_id, captured, {
        "captured_at_utc": captured,
        "week_start_date": "2026-07-27",
        "week_end_date": "2026-08-03",
        "week_start_at": "2026-07-27T00:00:00+00:00",
        "week_end_at": "2026-08-03T00:00:00+00:00",
        "weekly_percent": weekly,
        "source": "fixture",
        "payload_json": "{}",
        "account_key": "acct-a",
        "five_hour_window_key": window,
        "five_hour_percent": 10.0,
        "five_hour_resets_at": "2026-07-27T11:00:00+00:00",
    })


def _release_fixture(ns, *, quota):
    """The tainted fixture (correction batches, structural events, three
    protocol violations), five-hour decisions with a first writer that is
    not the earliest capture, and optionally Codex quota observations in a
    cache so the quota leg has something to cover."""
    core = importlib.import_module("_cctally_core")
    built = _tainted(ns)
    journal_dir = core.JOURNAL_DIR
    S5._append(journal_dir, S5.SEG_C, [
        _five_hour_accept("sa:5h-later", "2026-07-27T06:20:00Z", 30.0,
                          1785150000),
        _five_hour_accept("sa:5h-earlier", "2026-07-27T06:10:00Z", 31.0,
                          1785150000),
        _five_hour_accept("sa:5h-other", "2026-07-27T06:30:00Z", 32.0,
                          1785168000),
    ])
    if quota:
        import test_quota_journal as Q
        jr = importlib.import_module("_cctally_journal")
        ns["open_cache_db"]().close()
        for index in range(3):
            jr.append_record(Q._codex_quota_obs(
                _lib(), source_root_key="root-a",
                source_path="/codex/root-a/r.jsonl", line_offset=10 + index,
                captured_at_utc=Q._iso(10, index)), now_utc=_SEG_C_NOW)
    return built


def _install_reachability_probe(monkeypatch):
    """Every decoded record and its payload become weak-referenceable, and
    the effective selection is weakly referenced. `probe()` reports, at the
    moment it is called from inside the rebuild, which of them are still
    reachable other than through the records `tail` retains."""
    import sys

    jr = importlib.import_module("_cctally_journal")
    jl = _lib()
    refs: list = []
    selections: list = []
    real_decode = jl.decode_line
    real_select = jl.resolve_effective_events

    def decode(raw):
        record = real_decode(raw)
        if not isinstance(record, dict):
            return record
        payload = record.get("payload")
        record = _Probe(record)
        refs.append(weakref.ref(record))
        if isinstance(payload, dict):
            payload = _Probe(payload)
            refs.append(weakref.ref(payload))
            record["payload"] = payload
        return record

    def select(*args, **kwargs):
        result = real_select(*args, **kwargs)
        if sys._getframe(1).f_code.co_name == "_rebuild_stats_index_locked":
            selections.append(weakref.ref(result))
        return result

    monkeypatch.setattr(jl, "decode_line", decode)
    # Compaction rebuilds every retained record, which would drop the probe
    # class; this test is about the release, not the compaction.
    monkeypatch.setattr(jl, "compact_retained", lambda value: value,
                        raising=False)
    monkeypatch.setattr(jl, "resolve_effective_events", select)

    def probe():
        gc.collect()
        alive = [obj for obj in (ref() for ref in refs) if obj is not None]
        selection_alive = any(ref() is not None for ref in selections)
        frame = sys._getframe(1)
        while frame is not None and (
                frame.f_code.co_name != "_rebuild_stats_index_locked"):
            frame = frame.f_back
        assert frame is not None, "probe called outside the rebuild"
        allowed: set = set()
        for entry in frame.f_locals["tail"] or ():
            record = entry[3]
            allowed.add(id(record))
            if isinstance(record, dict) and isinstance(
                    record.get("payload"), dict):
                allowed.add(id(record["payload"]))
        leaked = [obj for obj in alive if id(obj) not in allowed]
        # Which of the rebuild's own names still hold a leaked object
        # directly, for the failure message.
        holders = sorted({name for name, value in frame.f_locals.items()
                          for obj in leaked if value is obj})
        del frame
        return {"probed": len(refs), "alive": len(alive),
                "retained_by_tail": len(alive) - len(leaked),
                "leaked": [dict(obj) for obj in leaked],
                "holders": holders, "selection_alive": selection_alive}

    return probe


def _assert_released(report):
    assert report["probed"], "non-vacuity: records were probed"
    leaked_kinds = sorted({
        (rec.get("t"), (rec.get("payload") or {}).get("kind")
         if isinstance(rec.get("payload"), dict) else rec.get("kind"))
        for rec in report["leaked"]}, key=repr)
    assert not report["leaked"], (
        f"{len(report['leaked'])} read-pass objects still reachable at the "
        f"quota bundle load: {leaked_kinds}; held directly by "
        f"{report['holders']}")
    assert not report["selection_alive"], (
        "the effective selection is still reachable at the quota bundle load")


def test_rebuild_releases_read_state_before_bundle_intact_cache(
        ns, monkeypatch, no_append_path_cutover):
    """The intact-cache path reads the bundle from the retained snapshot,
    before the open-block projection. At its entry no decoded record,
    structural event or correction payload of the read pass is reachable
    except the records `tail` retains (fails before revision 10: `decoded`
    and `selector_accumulators` are live there)."""
    jr = importlib.import_module("_cctally_journal")
    _release_fixture(ns, quota=True)
    first = jr.rebuild_stats_index(
        context=jr.RebuildContext(trigger="test-fixture"))
    assert first.quota_cache_coverage["status"] == "recovered"

    probe = _install_reachability_probe(monkeypatch)
    reports: list = []
    real_bundle = jr._read_quota_projection_bundle

    def bundle(snapshot_out):
        # Non-vacuity: this IS the intact path, with a snapshot to consume.
        assert snapshot_out and snapshot_out[0] is not None
        reports.append(probe())
        return real_bundle(snapshot_out)

    monkeypatch.setattr(jr, "_read_quota_projection_bundle", bundle)
    second = jr.rebuild_stats_index(
        context=jr.RebuildContext(trigger="test-fixture"))
    assert second.quota_cache_coverage["status"] == "covered"
    assert len(reports) == 1
    _assert_released(reports[0])


def test_rebuild_releases_read_state_before_bundle_no_snapshot(
        ns, monkeypatch, no_append_path_cutover):
    """Without a snapshot the projection loads its own bundle inside
    transaction B; at that load the same release holds."""
    jr = importlib.import_module("_cctally_journal")
    quota = importlib.import_module("_cctally_quota")
    _release_fixture(ns, quota=True)
    probe = _install_reachability_probe(monkeypatch)
    reports: list = []
    bundles: list = []
    real_bundle = jr._read_quota_projection_bundle
    real_load = quota.load_quota_projection_bundle

    def bundle(snapshot_out):
        result = real_bundle(snapshot_out)
        bundles.append(result)
        return result

    def load(cache_conn):
        import sys
        if sys._getframe(1).f_code.co_name == (
                "rematerialize_quota_projection_for_rebuild"):
            reports.append(probe())
        return real_load(cache_conn)

    monkeypatch.setattr(jr, "_read_quota_projection_bundle", bundle)
    monkeypatch.setattr(quota, "load_quota_projection_bundle", load)
    result = jr.rebuild_stats_index(
        context=jr.RebuildContext(trigger="test-fixture"))
    assert result.quota_cache_coverage["status"] == "recovered"
    assert bundles == [None], "non-vacuity: no snapshot bundle was consumed"
    assert len(reports) == 1, "the projection's own load was not reached"
    _assert_released(reports[0])


def test_open_block_projection_input_unchanged(
        ns, monkeypatch, no_append_path_cutover):
    """The open-block projection receives the first observations
    `journaled_block_first_observations` derives from the whole retained
    prefix and its effective selection, computed independently here over
    the journal-repair reader's prefix."""
    jr = importlib.import_module("_cctally_journal")
    repair = importlib.import_module("_cctally_journal_repair")
    jl = _lib()
    _release_fixture(ns, quota=False)
    hw = jr.journal_high_water()
    records, evidence, _hash, _ends = repair._read_prefix(hw)
    expected = jr.journaled_block_first_observations(
        records, jl.resolve_effective_events(
            records, protocol_prefix_evidence=evidence).by_id)
    assert expected == {
        ("acct-a", 1785150000): {
            "captured_at_utc": "2026-07-27T06:20:00Z",
            "five_hour_resets_at": "2026-07-27T11:00:00+00:00",
            "weekly_percent": 30.0},
        ("acct-a", 1785168000): {
            "captured_at_utc": "2026-07-27T06:30:00Z",
            "five_hour_resets_at": "2026-07-27T11:00:00+00:00",
            "weekly_percent": 32.0},
    }, "non-vacuity: two windows, the first writer is not the earliest"

    received: list = []
    real_backfill = ns["_backfill_five_hour_blocks"]

    def backfill(conn, **kwargs):
        received.append(kwargs.get("journaled_first"))
        return real_backfill(conn, **kwargs)

    monkeypatch.setitem(ns, "_backfill_five_hour_blocks", backfill)
    jr.rebuild_stats_index(context=jr.RebuildContext(trigger="test-fixture"))
    assert received == [expected]


def test_rebuild_record_fields_unchanged_by_release(ns):
    jr = importlib.import_module("_cctally_journal")
    repair = importlib.import_module("_cctally_journal_repair")
    jl = _lib()
    built = _tainted(ns)
    records, evidence, _hash, _ends = repair._read_prefix(built["high_water"])
    expected = jl.resolve_effective_events(
        records, protocol_prefix_evidence=evidence)
    result = jr.rebuild_stats_index(
        context=jr.RebuildContext(trigger="test-fixture"))
    assert [v.fingerprint for v in result.protocol_violations] == [
        v.fingerprint for v in expected.protocol_violations]
    assert [v.fingerprint for v in result.acknowledged_protocol_violations] \
        == [v.fingerprint for v in expected.acknowledged_protocol_violations]
    assert tuple(result.conflicts) == tuple(expected.conflicts)
    assert len(result.protocol_violations) == 1
    assert len(result.acknowledged_protocol_violations) == 2


# --------------------------------------------------------------------------
# T15 — retained records intern their keys and short string values
# --------------------------------------------------------------------------


_LONG = "x" * 65


def _line():
    return json.dumps({
        "t": "evt", "id": "sa:one", "rev": 0, "at": "2026-01-01T00:00:00Z",
        "payload": {"kind": "snapshot_accept", "weekly_percent": 12.5,
                    "source": "statusline", "note": _LONG, "flag": True,
                    "nothing": None, "count": 7,
                    "nested": [{"inner_key": "short"}, "listed", 3]},
    }).encode("utf-8")


def test_compact_retained_interns_keys_and_short_strings():
    jl = _lib()
    a = jl.compact_retained(jl.decode_line(_line()))
    b = jl.compact_retained(jl.decode_line(_line()))
    assert a == b
    for key_a, key_b in zip(a, b):
        assert key_a is key_b
    for key_a, key_b in zip(a["payload"], b["payload"]):
        assert key_a is key_b
    assert a["id"] is b["id"]
    assert a["payload"]["source"] is b["payload"]["source"]
    assert a["payload"]["nested"][1] is b["payload"]["nested"][1]
    inner_a, inner_b = a["payload"]["nested"][0], b["payload"]["nested"][0]
    assert next(iter(inner_a)) is next(iter(inner_b))
    assert inner_a["inner_key"] is inner_b["inner_key"]


def test_compact_retained_equal_and_long_values_unchanged():
    jl = _lib()
    record = jl.decode_line(_line())
    snapshot = json.loads(_line())
    out = jl.compact_retained(record)
    assert out == record == snapshot
    assert jl.encode_line(out) == jl.encode_line(snapshot)
    assert out is not record and out["payload"] is not record["payload"]
    assert out["payload"]["note"] is record["payload"]["note"]
    for key in ("weekly_percent", "flag", "nothing", "count"):
        assert out["payload"][key] is record["payload"][key]
    assert out["payload"]["nested"][2] is record["payload"]["nested"][2]
    out["payload"]["weekly_percent"] = 1.0
    assert record["payload"]["weekly_percent"] == 12.5, (
        "the compacted record shares no mutable container with its input")
    assert jl.compact_retained("y" * 64) is sys_intern("y" * 64)
    assert jl.compact_retained(5) == 5


def sys_intern(value):
    import sys
    return sys.intern(value)


def _journal_lines():
    jr = importlib.import_module("_cctally_journal")
    core = importlib.import_module("_cctally_core")
    lines = []
    for name in jr.list_segments():
        lines.extend((core.JOURNAL_DIR / name).read_bytes().splitlines())
    return lines


def _match_key(record):
    return tuple(record.get(key)
                 for key in ("t", "id", "phase", "batch", "seq", "rev"))


def _shared_with(record, independent, counts):
    """Walk a retained record beside an independent `json.loads` of its
    line: every key and every string of 2..64 characters must be the one
    interned object (`sys.intern` of the independent decode's value). One
    character strings are CPython singletons whether compacted or not, so
    they prove nothing and are skipped."""
    import sys

    limit = _lib().COMPACT_INTERN_MAX_CHARS
    if isinstance(record, dict) and isinstance(independent, dict):
        theirs = {key: key for key in independent}
        for key, value in record.items():
            other = theirs.get(key)
            if other is None:
                continue  # a key the reader added (account normalization)
            if len(key) > 1:
                assert key is sys.intern(other), f"key {key!r} not shared"
                counts["keys"] += 1
            _shared_with(value, independent[other], counts)
    elif isinstance(record, list) and isinstance(independent, list):
        for mine, other in zip(record, independent):
            _shared_with(mine, other, counts)
    elif (isinstance(record, str) and isinstance(independent, str)
            and record == independent and 1 < len(record) <= limit):
        assert record is sys.intern(independent), (
            f"string {record!r} not shared")
        counts["strings"] += 1


def _assert_retained_compacted(retained):
    independent: dict = {}
    for line in _journal_lines():
        try:
            decoded = json.loads(line)
        except ValueError:
            continue
        if isinstance(decoded, dict):
            independent.setdefault(_match_key(decoded), decoded)
    counts = {"records": 0, "keys": 0, "strings": 0}
    for record in retained:
        other = independent.get(_match_key(record))
        if other is None:
            continue
        counts["records"] += 1
        _shared_with(record, other, counts)
    assert counts["records"] and counts["keys"] and counts["strings"], (
        f"non-vacuity: {counts}")
    return counts


def _rebuild_retained(ns, monkeypatch, tmp_path):
    """The records the final rebuild's read pass retains (`decoded`, as the
    effective selection receives it)."""
    import sys

    jr = importlib.import_module("_cctally_journal")
    jl = _lib()
    _tainted(ns)
    captured: list = []
    real_select = jl.resolve_effective_events

    def select(records, *args, **kwargs):
        if sys._getframe(1).f_code.co_name == "_rebuild_stats_index_locked":
            captured.append(list(records))
        return real_select(records, *args, **kwargs)

    monkeypatch.setattr(jl, "resolve_effective_events", select)
    jr.rebuild_stats_index(context=jr.RebuildContext(trigger="test-fixture"))
    router = importlib.import_module("_lib_journal_router")
    assert len(captured) == 1
    return [record for record in captured[0] if isinstance(record, dict)
            and record.get("t") in router.RETAINED_RECORD_TYPES]


def _rederive_retained(ns, monkeypatch, tmp_path):
    """The rederive reader's records, less the D4 Codex stubs."""
    # As the rederive cases pin their own prefix (`W.no_append_path_cutover`).
    monkeypatch.setenv("CCTALLY_TEST_NO_ACCOUNT_BOUND_CUTOVER", "1")
    W._store(tmp_path, monkeypatch, stubbed=True)
    rederive = importlib.import_module("_cctally_rederive")
    records, _hw, _ends, _evidence = rederive.read_rederive_journal_prefix()
    return [record for record in records if isinstance(record, dict)
            and record.get("provider") != "codex"]


def _completion_retained(ns, monkeypatch, tmp_path):
    """Completion's window, every prefix it read."""
    prefixes, _appended, _dump = W._completion_trace(
        ns, monkeypatch, tmp_path)
    assert prefixes, "no completion window was read"
    return [record for prefix in prefixes for record in prefix.window
            if isinstance(record, dict)]


@pytest.mark.parametrize("reader", [
    _rebuild_retained, _rederive_retained, _completion_retained,
], ids=["rebuild", "rederive", "completion"])
def test_each_reader_retains_compacted_records(
        ns, monkeypatch, tmp_path, capsys, reader):
    """Each of the three retention points compacts what it keeps: the
    records it retains share their keys and short strings with an
    independent decode of the same journal lines. A reader whose call site
    omits `compact_retained` keeps fresh decoder strings and fails here."""
    retained = reader(ns, monkeypatch, tmp_path / "store")
    capsys.readouterr()
    assert retained, "non-vacuity: the reader retained records"
    _assert_retained_compacted(retained)


def _counting_compaction(monkeypatch):
    jl = _lib()
    real = jl.compact_retained
    calls = {"n": 0}

    def counted(value):
        calls["n"] += 1
        return real(value)

    monkeypatch.setattr(jl, "compact_retained", counted)
    return calls


def _no_compaction(monkeypatch):
    monkeypatch.setattr(_lib(), "compact_retained", lambda value: value)


def test_outputs_equal_with_and_without_compaction_completion_and_index(
        ns, monkeypatch, tmp_path, capsys):
    """The T3 completion and the index it publishes, compaction on and off."""
    calls = _counting_compaction(monkeypatch)
    _p, on_bytes, on_dump = W._completion_trace(
        ns, monkeypatch, tmp_path / "on")
    assert calls["n"], "non-vacuity: compaction ran"
    _no_compaction(monkeypatch)
    _p, off_bytes, off_dump = W._completion_trace(
        ns, monkeypatch, tmp_path / "off")
    capsys.readouterr()
    assert on_bytes and on_bytes == off_bytes
    assert on_dump == off_dump


def test_outputs_equal_with_and_without_compaction_rederive(
        tmp_path, monkeypatch, capsys, no_append_path_cutover):
    """The rederive preview plan and apply (appended corrections and the
    published index), compaction on and off."""
    import argparse

    results = {}
    for compacted in (True, False):
        with monkeypatch.context() as patch:
            mod, _records = W._store(
                tmp_path / ("on" if compacted else "off"), patch,
                stubbed=True)
            if compacted:
                calls = _counting_compaction(patch)
            else:
                _no_compaction(patch)
            plan = W._preview(mod).plan.to_bytes()
            before = W._journal_bytes()
            code = mod.cmd_db_rederive(argparse.Namespace(
                family="claude-usage", yes=True, json=True))
            output = W._stable_output(capsys.readouterr().out)
            results[compacted] = (plan, code, output,
                                  W._journal_bytes()[len(before):],
                                  W._index_dump())
            if compacted:
                assert calls["n"], "non-vacuity: compaction ran"
    on, off = results[True], results[False]
    assert on[0] == off[0], "preview plans differ"
    assert on[1] == off[1] == 0
    assert on[3] and on[3] == off[3], "appended corrections differ"
    assert on[2] == off[2]
    assert on[4] == off[4], "published indexes differ"


def test_rederive_reader_positions_are_compacted_objects(
        tmp_path, monkeypatch, no_append_path_cutover):
    """Compaction precedes every identity a reader takes: each non-stub
    position is the compacted record (its keys interned), and the D4 stubs
    are still one object per key tuple."""
    import sys

    mod, appended = W._store(tmp_path, monkeypatch, stubbed=True)
    rederive = importlib.import_module("_cctally_rederive")
    records, _hw, _ends, _evidence = rederive.read_rederive_journal_prefix()
    assert len(records) == len(appended)
    stubs = {}
    for original, record in zip(appended, records):
        if original.get("provider") == "codex":
            key = (original.get("account"),
                   original["payload"].get("account_key"))
            assert stubs.setdefault(key, record) is record
            continue
        assert record == original
        for key in record:
            assert key is sys.intern(key)
    assert len(stubs) == 3


# --------------------------------------------------------------------------
# T16 — the cache fingerprint is hashed as it is read
# --------------------------------------------------------------------------


_ENTRY_COLUMNS = (
    "source_path", "line_offset", "timestamp_utc", "model", "input_tokens",
    "output_tokens", "cache_create_tokens", "cache_read_tokens",
    "cache_create_1h_tokens", "cost_usd_raw", "speed", "account_key")
_FILE_COLUMNS = ("path", "session_id", "project_path")
_CODEX_COLUMNS = (
    "source_path", "line_offset", "timestamp_utc", "session_id", "model",
    "input_tokens", "cached_input_tokens", "output_tokens",
    "reasoning_output_tokens", "account_key")


def _cache(rows=(), files=(), codex=()):
    conn = sqlite3.connect(":memory:")
    conn.execute(f"CREATE TABLE session_entries ({', '.join(_ENTRY_COLUMNS)})")
    conn.execute(f"CREATE TABLE session_files ({', '.join(_FILE_COLUMNS)})")
    conn.execute(
        f"CREATE TABLE codex_session_entries ({', '.join(_CODEX_COLUMNS)})")
    conn.executemany(
        f"INSERT INTO session_entries VALUES ({','.join('?' * 12)})", rows)
    conn.executemany("INSERT INTO session_files VALUES (?,?,?)", files)
    conn.executemany(
        f"INSERT INTO codex_session_entries VALUES ({','.join('?' * 10)})",
        codex)
    conn.commit()
    return conn


def _materialized(conn, include_codex):
    """The pre-#997 `_cache_fingerprint`, written out: every row, then one
    canonical JSON document of them."""
    basis = {
        "sessionEntries": [list(r) for r in conn.execute(
            "SELECT " + ",".join(_ENTRY_COLUMNS)
            + " FROM session_entries ORDER BY source_path, line_offset")],
        "sessionFiles": [list(r) for r in conn.execute(
            "SELECT " + ",".join(_FILE_COLUMNS)
            + " FROM session_files ORDER BY path")],
    }
    if include_codex:
        basis["codexEntries"] = [list(r) for r in conn.execute(
            "SELECT " + ",".join(_CODEX_COLUMNS)
            + " FROM codex_session_entries ORDER BY source_path,line_offset")]
    body = json.dumps(basis, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _rows():
    entries = [
        ("/p/é-ü.jsonl", 2, "2026-01-01T00:00:00+00:00", "claude-opus-4",
         1, 2, 3, 4, 5, 0.1 + 0.2, None, "acct-✓"),
        ("/p/a.jsonl", 1, "2026-01-01T00:00:01+00:00", None,
         0, 0, 0, 0, 0, None, "fast", None),
        ("/p/a.jsonl", 0, "2026-01-01T00:00:02+00:00", "m\"q\\",
         10**12, -1, 0, 0, 0, 1e-300, "standard", "日本"),
    ]
    files = [("/p/a.jsonl", "s-1", "/repo"), ("/p/é-ü.jsonl", None, None)]
    codex = [("/c/r.jsonl", 3, "2026-01-01T00:00:00Z", "sid", "gpt-5",
              1, 2, 3, 4, None),
             ("/c/r.jsonl", 1, "2026-01-01T00:00:00Z", None, "gpt-5",
              0, 0, 0, 0, "acct")]
    return entries, files, codex


@pytest.mark.parametrize("include_codex", [False, True])
@pytest.mark.parametrize("filled", [False, True])
def test_streamed_fingerprint_equals_materialized(include_codex, filled):
    rederive = importlib.import_module("_cctally_rederive")
    conn = _cache(*_rows()) if filled else _cache()
    try:
        assert rederive._cache_fingerprint(
            conn, include_codex=include_codex) == _materialized(
                conn, include_codex)
    finally:
        conn.close()


def test_streamed_fingerprint_peak_bounded():
    rederive = importlib.import_module("_cctally_rederive")
    count = 200_000
    rows = (
        (f"/claude/projects/repo-{i % 97}/session-{i // 50}.jsonl", i,
         f"2026-01-01T00:{(i // 60) % 60:02d}:{i % 60:02d}+00:00",
         "claude-opus-4-1-20250805", i, i * 2, i * 3, i * 4, i % 7,
         i / 3.0, "standard", f"acct-{i % 5}")
        for i in range(count))
    conn = _cache(rows)
    try:
        expected = _materialized(conn, False)
        gc.collect()
        tracemalloc.start()
        try:
            tracemalloc.reset_peak()
            digest = rederive._cache_fingerprint(conn)
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert digest == expected
        assert peak < 32 * 1024 * 1024, peak
    finally:
        conn.close()


def _published_index_digest(dump) -> str:
    """A stable digest of `W._index_dump()` (volatile tables excluded).

    `journal_selector_state`'s generation identity (`generation_record_path`,
    `generation_stamped_at_utc`, its columns 1 and 2) names the previous
    publication's rebuild record and stamp time, so it describes the build,
    not the replayed content, exactly as `selector_state.comparable` clears it.
    """
    dump = dict(dump)
    if "journal_selector_state" in dump:
        dump["journal_selector_state"] = [
            (row[0], None, None, *row[3:])
            for row in dump["journal_selector_state"]]
    return "sha256:" + hashlib.sha256(
        repr(sorted(dump.items())).encode("utf-8")).hexdigest()


#: The index both rebuilds of `_release_fixture(quota=True)` publish,
#: captured from the implementation before the revision-10 release order
#: (`9282d8b73`), on the recovery path and then on the intact-cache path.
#: Re-pinned for #797/#823/#840 (epoch 1019): every replayed Model-A `evt`
#: line now carries the revision-2 writer stamp `"wpr":2`, which moves the
#: journal cursor, the selector state and the admission summary by the added
#: bytes, and the index gains the empty `claude_oauth_samples` and
#: `claude_weekly_observation_decisions` tables. No row's content otherwise
#: changed. (The previous pin was
#: `sha256:444d70cc633488c366c003610a38ee8823be7c454380447170f466ce993e298d`.)
RELEASE_FIXTURE_INDEX_DIGESTS = (
    "sha256:7ff3eb10b29d15f6e029b3c1fa50ed1ef515e4b30f42af3df0d6681de679865c",
    "sha256:7ff3eb10b29d15f6e029b3c1fa50ed1ef515e4b30f42af3df0d6681de679865c",
)


def test_rebuild_published_index_unchanged_by_release(
        ns, monkeypatch, no_append_path_cutover):
    """The published index of both bundle paths equals the one the
    implementation before the release-order change published."""
    jr = importlib.import_module("_cctally_journal")
    _release_fixture(ns, quota=True)
    digests = []
    for status in ("recovered", "covered"):
        result = jr.rebuild_stats_index(
            context=jr.RebuildContext(trigger="test-fixture"))
        assert result.quota_cache_coverage["status"] == status
        dump = W._index_dump()
        # Non-vacuity: the fixture's decisions and quota projection landed.
        assert dump["weekly_usage_snapshots"], "no snapshot was published"
        digests.append(_published_index_digest(dump))
    assert tuple(digests) == RELEASE_FIXTURE_INDEX_DIGESTS
