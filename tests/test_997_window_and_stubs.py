"""#997 T-A3 — the completion window omits admitted inert observations, and
the rederive reader shares one stub per inert Codex observation shape (spec
D3, D4).

85 % of a production journal is Codex `quota_window_snapshot` observations,
and both full replays retained every one of them decoded (≈ 3 KB each) while
reading nothing beyond `t`/`provider`/`payload.kind`. The completion window
now skips an admitted observation the replay provably ignores, and the
rederive reader, which must keep every position, substitutes an interned
read-only stub. Both are checked for exactness against the same run with the
change disabled.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3

import pytest

from conftest import load_script, redirect_paths

UTC = dt.timezone.utc
_W1 = int(dt.datetime(2026, 1, 8, tzinfo=UTC).timestamp())
_FIXED_NOW = "2026-01-05T00:00:00Z"


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


def _mods():
    import _cctally_core
    import _cctally_journal
    import _lib_journal
    return _cctally_journal, _lib_journal, _cctally_core


def _obs(J, at, pct):
    return J.make_obs(at=at, src="record-usage", provider="claude", payload={
        "weekly_percent": pct, "resets_at": _W1, "source": "statusline",
        "captured_at": at})


def _codex(J, at, offset, *, account=None, account_key=None, legacy=False):
    payload = {
        "kind": "quota_window_snapshot", "source": "codex",
        "source_root_key": "root-a", "source_path": "/codex/root-a/r.jsonl",
        "line_offset": offset, "captured_at_utc": at,
        "observed_slot": "primary", "logical_limit_key": "limit-primary",
        "limit_id": "native-primary", "limit_name": "Primary",
        "window_minutes": 300, "used_percent": 10.0 + offset,
        "resets_at_utc": "2026-01-04T15:00:00Z", "plan_type": "pro",
        "individual_limit_json": None, "reached_type": None,
        "observed_model": "gpt-5.3-codex"}
    if account_key is not None:
        payload["account_key"] = account_key
    record = J.make_obs(at=at, src="codex-quota", provider="codex",
                        account=account, payload=payload)
    return J.unstamped_record(record) if legacy else record


def _legacy_snapshot_accept(J, reading):
    """The unstamped decision an old process journals for a current reading
    (excluded by admission after the cutover)."""
    return J.unstamped_record(J.make_evt(
        kind="snapshot_accept", id="sa:" + reading["id"], at=reading["at"],
        payload={
            "captured_at_utc": reading["at"],
            "week_start_date": "2026-01-01",
            "week_end_date": "2026-01-08",
            "weekly_percent": reading["payload"]["weekly_percent"],
            "source": "statusline",
            "payload_json": "{}",
        }))


def _raw_append_tail(records):
    jr, J, core = _mods()
    path = core.JOURNAL_DIR / jr.list_segments()[-1]
    with open(path, "ab") as fh:
        for record in records:
            fh.write(J.encode_line(record))


def _journal_bytes():
    jr, _J, core = _mods()
    return b"".join((core.JOURNAL_DIR / name).read_bytes()
                    for name in jr.list_segments())


#: Tables whose rows describe the build (its time or generation identity),
#: never the replayed content.
_VOLATILE_TABLES = frozenset({"schema_migrations", "stats_publication_stamp"})


def _index_dump():
    _jr, _J, core = _mods()
    conn = sqlite3.connect(core.DB_PATH)
    try:
        out = {}
        for (table,) in conn.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall():
            if table in _VOLATILE_TABLES:
                continue
            out[table] = sorted((tuple(row) for row in conn.execute(
                f"SELECT * FROM {table}")), key=repr)
        return out
    finally:
        conn.close()


# --------------------------------------------------------------------------
# T3 — completion window omission
# --------------------------------------------------------------------------

def _completion_trace(ns, monkeypatch, tmp_path, *, omit=True):
    """A completion from the journal origin over Claude inputs with Codex
    quota observations interleaved and a genuinely excluded record (a
    previous release's decision appended after the cutover). Returns
    ``(prefixes, appended_bytes, index_dump)``."""
    import _cctally_completion
    redirect_paths(ns, monkeypatch, tmp_path)
    jr, J, core = _mods()
    monkeypatch.setattr(_cctally_completion, "_iso_now", lambda: _FIXED_NOW)
    if not omit:
        monkeypatch.setattr(_cctally_completion, "_window_omits",
                            lambda record: False)
    prefixes: list = []
    real = _cctally_completion._read_prefix

    def recording(*args, **kwargs):
        prefix = real(*args, **kwargs)
        prefixes.append(prefix)
        return prefix

    monkeypatch.setattr(_cctally_completion, "_read_prefix", recording)

    jr.append_record(_obs(J, "2026-01-04T09:00:00Z", 7.0))
    jr.run_stats_ingest(mode="authoritative")
    jr.append_record(_codex(J, "2026-01-04T09:30:00Z", 1))
    reading = _obs(J, "2026-01-04T10:00:00Z", 9.0)
    jr.append_record(reading)
    _raw_append_tail([_legacy_snapshot_accept(J, reading)])
    jr.append_record(_codex(J, "2026-01-04T10:30:00Z", 2, account="acct-x"))
    jr.append_record(_codex(J, "2026-01-04T10:40:00Z", 3, legacy=True))
    jr.append_record(_obs(J, "2026-01-04T11:00:00Z", 11.0))
    with open(core.DB_PATH, "r+b") as handle:
        handle.write(b"not a database " * 200)
    before = _journal_bytes()
    assert ns["cmd_db_rebuild"](argparse.Namespace(db="stats", json=True)) == 0
    after = _journal_bytes()
    assert after.startswith(before)
    return prefixes, after[len(before):], _index_dump()


def test_window_has_no_admitted_inert_observation(
        ns, monkeypatch, tmp_path, capsys):
    """Spec T3: the window keeps every Claude input and the excluded
    record's placeholder, and holds no admitted Codex observation. Fails
    before #997: every admitted line was appended to the window."""
    import _lib_account_protocol
    prefixes, appended, _dump = _completion_trace(ns, monkeypatch, tmp_path)
    capsys.readouterr()
    assert prefixes, "no completion window was read"
    for prefix in prefixes:
        assert len(prefix.window) == len(prefix.coordinates)
        kinds = [(r.get("t"), r.get("provider")) for r in prefix.window]
        assert ("obs", "codex") not in kinds, kinds
        assert ("obs", "claude") in kinds, kinds
        assert any(r.get("t") == _lib_account_protocol.EXCLUDED_PLACEHOLDER_TYPE
                   for r in prefix.window), "non-vacuity: an excluded record"
    assert appended, "non-vacuity: the completion appended decisions"


def test_window_omission_output_equal(ns, monkeypatch, tmp_path, capsys):
    """Spec T3: the appended journal bytes and the published index equal
    those of the same completion with the omission disabled."""
    _p, on_bytes, on_dump = _completion_trace(
        ns, monkeypatch, tmp_path / "on")
    off_prefixes, off_bytes, off_dump = _completion_trace(
        ns, monkeypatch, tmp_path / "off", omit=False)
    capsys.readouterr()
    assert any(r.get("provider") == "codex"
               for p in off_prefixes for r in p.window), (
        "the disabled control must retain the observations")
    assert on_bytes and on_bytes == off_bytes
    assert on_dump == off_dump


# --------------------------------------------------------------------------
# T4 — interned rederive stubs
# --------------------------------------------------------------------------

AT = "2026-07-25T12:00:00Z"
_QUOTA_ONLY_KEYS = {"used_percent", "resets_at_utc", "line_offset",
                    "source_path", "limit_id", "captured_at_utc"}


@pytest.fixture
def no_append_path_cutover(monkeypatch):
    """The rederive cases pin their own prefix hashes and high waters, as
    `tests/test_rederive_command.py` does."""
    monkeypatch.setenv("CCTALLY_TEST_NO_ACCOUNT_BOUND_CUTOVER", "1")


def _isolated(tmp_path, monkeypatch):
    from conftest import load_isolated_cctally_module
    return load_isolated_cctally_module(tmp_path, monkeypatch)


def _seed_cache(mod):
    path = "/tmp/claude/projects/repo/session.jsonl"
    conn = mod.open_cache_db()
    conn.execute(
        "INSERT INTO session_files "
        "(path, size_bytes, mtime_ns, last_byte_offset, last_ingested_at, "
        " session_id, project_path) VALUES (?,?,?,?,?,?,?)",
        (path, 100, 1, 100, AT, "session-a", "/repo"))
    conn.execute(
        "INSERT INTO session_entries "
        "(source_path, line_offset, timestamp_utc, model, input_tokens, "
        " output_tokens, cache_create_tokens, cache_read_tokens, "
        " cache_create_1h_tokens, account_key) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (path, 0, "2026-07-25T11:00:00+00:00", "claude-3-5-sonnet-20241022",
         0, 0, 100, 0, 40, "acct-a"))
    conn.commit()
    conn.close()


def _readings(J):
    """Six stale-replica readings (60/10 alternating week ends), each
    followed by Codex quota observations of three shapes."""
    out = []
    for minute, (weekly, end_day) in enumerate(
            ((60, 27), (10, 28), (60, 27), (10, 28), (60, 27), (10, 28))):
        captured = f"2026-07-25T12:{minute:02d}:00Z"
        out.append(J.make_obs(
            at=captured, src="record-usage", provider="claude",
            account="acct-a", payload={
                "captured_at": captured, "source": "statusline",
                "weekly_percent": weekly,
                "resets_at": int(dt.datetime(
                    2026, 7, end_day, tzinfo=UTC).timestamp()),
                "five_hour_percent": 20 + minute,
                "five_hour_resets_at": "2026-07-25T16:00:00Z"}))
        out.append(_codex(J, captured, 10 * minute + 1, account="acct-x"))
        out.append(_codex(J, captured, 10 * minute + 2, account="acct-x",
                          account_key="acct-x"))
        out.append(_codex(J, captured, 10 * minute + 3, legacy=True))
    return out


def _store(tmp_path, monkeypatch, *, stubbed):
    mod = _isolated(tmp_path, monkeypatch)
    import _cctally_journal as runtime
    import _cctally_rederive
    import _lib_journal as J
    _seed_cache(mod)
    monkeypatch.setattr(_cctally_rederive, "_iso_now", lambda: AT)
    if not stubbed:
        monkeypatch.setattr(_cctally_rederive, "_inert_codex_stub",
                            lambda record, *args, **kwargs: record)
    records = _readings(J)
    runtime.append_records(records, now_utc=dt.datetime(2026, 7, 25,
                                                        tzinfo=UTC))
    return mod, records


def test_rederive_reader_interns_codex_stubs(tmp_path, monkeypatch, no_append_path_cutover):
    """Spec T4: every admitted Codex quota position holds the stub for its
    key tuple — one object per `(src, account, account_key, vendor)` — with
    no quota payload keys; Claude positions, record ends and length are
    unchanged. Fails before #997: the reader kept every line decoded."""
    mod, appended = _store(tmp_path, monkeypatch, stubbed=True)
    import _cctally_rederive
    records, _hw, ends, _evidence = (
        _cctally_rederive.read_rederive_journal_prefix())
    assert len(records) == len(ends) == len(appended)
    stubs: dict = {}
    for original, record in zip(appended, records):
        if original.get("provider") != "codex":
            assert record == original
            continue
        assert set(record["payload"]) <= {"kind", "account_key", "vendor"}
        assert not (_QUOTA_ONLY_KEYS & set(record["payload"]))
        assert set(record) <= {"t", "provider", "src", "account", "payload"}
        key = (original.get("account"),
               original["payload"].get("account_key"))
        stubs.setdefault(key, record)
        assert stubs[key] is record, key
    assert len(stubs) == 3
    assert len({id(stub) for stub in stubs.values()}) == 3


def _stable_output(text):
    """A `db rederive --json` document without the fields that describe the
    run's own timing (`generatedAt`, the rebuild's durations and phases)."""
    output = json.loads(text)
    output.pop("generatedAt", None)
    rebuild = output.get("rebuild")
    if isinstance(rebuild, dict):
        output["rebuild"] = {
            key: rebuild.get(key)
            for key in ("segmentsRead", "linesFolded", "malformed",
                        "rowsByTable")}
    return output


def _preview(mod):
    return mod.preview_db_rederive("claude-usage")


def test_rederive_preview_equal_full_vs_stubbed(tmp_path, monkeypatch,
                                                capsys,
        no_append_path_cutover):
    """Spec T4: preview and apply over stubbed records equal those over the
    full records: the same plan bytes, the same exit, the same appended
    journal bytes."""
    results = {}
    for stubbed in (True, False):
        mod, _records = _store(
            tmp_path / ("stub" if stubbed else "full"), monkeypatch,
            stubbed=stubbed)
        plan = _preview(mod).plan.to_bytes()
        before = _journal_bytes()
        code = mod.cmd_db_rederive(argparse.Namespace(
            family="claude-usage", yes=True, json=True))
        output = _stable_output(capsys.readouterr().out)
        results[stubbed] = (plan, code, output, _journal_bytes()[len(before):])
    stub, full = results[True], results[False]
    assert stub[0] == full[0], "preview plans differ"
    assert stub[1] == full[1]
    assert stub[3] == full[3], "appended journal bytes differ"
    differing = sorted(key for key in set(stub[2]) | set(full[2])
                       if stub[2].get(key) != full[2].get(key))
    assert not differing, {k: (stub[2].get(k), full[2].get(k))
                           for k in differing}
    assert stub[1] == 0 and stub[3], "non-vacuity"


def test_rederive_apply_equal_full_vs_stubbed(tmp_path, monkeypatch, capsys,
                                              no_append_path_cutover):
    """Spec T4 (plan T-A3): a successful `db rederive --yes` over the
    reader's interned stubs appends byte-identical corrections and publishes
    an index equal to the same apply over the full records."""
    results = {}
    for stubbed in (True, False):
        mod, _records = _store(
            tmp_path / ("stub" if stubbed else "full"), monkeypatch,
            stubbed=stubbed)
        before = _journal_bytes()
        code = mod.cmd_db_rederive(argparse.Namespace(
            family="claude-usage", yes=True, json=True))
        output = _stable_output(capsys.readouterr().out)
        appended = _journal_bytes()[len(before):]
        results[stubbed] = (code, output.get("status"), appended,
                            _index_dump())
    stub, full = results[True], results[False]
    assert stub[0] == full[0] == 0, (stub[0], full[0])
    assert stub[1] == full[1]
    assert stub[2], "non-vacuity: the apply appended corrections"
    assert stub[2] == full[2], "appended corrections differ"
    assert stub[3].get("weekly_usage_snapshots"), (
        "non-vacuity: the rebuilt index holds usage rows")
    assert stub[3] == full[3], "rebuilt indexes differ"


def _reviewed_manifest(mod, records, tmp_path, capsys):
    """Preview a reviewed weekly decision holding the stale replicas, then
    pin its plan hashes, as an operator does."""
    import _cctally_journal as runtime
    high_water = runtime.journal_high_water()
    claude = [r for r in records if r.get("provider") == "claude"]
    manifest = {
        "schemaVersion": 2,
        "journalHighWater": {"segment": high_water[0],
                             "offset": high_water[1]},
        "journalPrefixHash": runtime.journal_prefix_hash(high_water),
        "reviewedAt": "2026-07-25T13:00:00Z",
        "reason": "Reviewed stale replica captures against retained evidence",
        "weeklyAxisDecisions": [
            {"observationId": record["id"], "disposition": "hold"}
            for record in claude[2:]],
        "snapshotIdentityDecisions": [],
    }
    path = tmp_path / "reviewed.json"
    path.write_text(json.dumps(manifest))
    parser = mod.build_parser()
    args = parser.parse_args([
        "db", "rederive", "--family", "claude-usage",
        "--reviewed-weekly-decisions", str(path), "--json"])
    assert mod.cmd_db_rederive(args) == 0
    preview = json.loads(capsys.readouterr().out)
    manifest.update({
        "expectedBaselinePlanHash": preview["baselinePlanHash"],
        "expectedDecisionPlanHash": preview["decisionPlanHash"],
    })
    path.write_text(json.dumps(manifest))
    return parser.parse_args([
        "db", "rederive", "--family", "claude-usage",
        "--reviewed-weekly-decisions", str(path), "--yes", "--json"])


class _StopAfterDecision(Exception):
    pass


def test_rederive_suffix_conflict_with_stubs_raises(tmp_path, monkeypatch,
                                                    capsys,
        no_append_path_cutover):
    """Spec T4: an unfinished reviewed decision followed by Codex
    observations still refuses ("new journal records after it") over
    stubbed records, exactly as over full records."""
    outcomes = {}
    for stubbed in (True, False):
        mod, records = _store(
            tmp_path / ("stub" if stubbed else "full"), monkeypatch,
            stubbed=stubbed)
        # Imported after `_store`: loading the isolated module re-imports
        # the siblings, so an earlier reference would be a stale module.
        import _cctally_rederive
        import _lib_journal as J
        import _lib_rederive
        apply_args = _reviewed_manifest(
            mod, records, tmp_path / ("stub" if stubbed else "full"), capsys)

        def stop(stage):
            if stage == "after-reviewed-decision-op":
                raise _StopAfterDecision(stage)

        monkeypatch.setattr(_cctally_rederive, "_REDERIVE_CRASH_HOOK", stop)
        with pytest.raises(_StopAfterDecision):
            mod.cmd_db_rederive(apply_args)
        monkeypatch.setattr(_cctally_rederive, "_REDERIVE_CRASH_HOOK", None)
        capsys.readouterr()
        import _cctally_journal as runtime
        runtime.append_record(_codex(J, "2026-07-25T14:00:00Z", 99,
                                     account="acct-x"))
        with pytest.raises(_lib_rederive.RederiveConflict) as caught:
            _preview(mod)
        outcomes[stubbed] = str(caught.value)
    assert outcomes[True] == outcomes[False]
    assert "new journal records after it" in outcomes[True]


def test_rederive_exact_retry_with_stub_suffix_rejected(tmp_path, monkeypatch,
                                                        capsys,
        no_append_path_cutover):
    """Spec T4: once the reviewed decision has applied, a retry of the same
    apply after Codex observations landed is rejected exactly as it is over
    full records: same exit, same output, no journal bytes."""
    import _lib_journal as J
    outcomes = {}
    for stubbed in (True, False):
        mod, records = _store(
            tmp_path / ("stub" if stubbed else "full"), monkeypatch,
            stubbed=stubbed)
        apply_args = _reviewed_manifest(
            mod, records, tmp_path / ("stub" if stubbed else "full"), capsys)
        assert mod.cmd_db_rederive(apply_args) == 0
        capsys.readouterr()
        import _cctally_journal as runtime
        runtime.append_record(_codex(J, "2026-07-25T14:00:00Z", 99,
                                     account="acct-x"))
        before = _journal_bytes()
        code = mod.cmd_db_rederive(apply_args)
        output = _stable_output(capsys.readouterr().out)
        outcomes[stubbed] = (code, output, _journal_bytes() == before)
    assert outcomes[True] == outcomes[False]
    code, output, unchanged = outcomes[True]
    assert code != 0 and output.get("status") != "applied", output
    assert unchanged
