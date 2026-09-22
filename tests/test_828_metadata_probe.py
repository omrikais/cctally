"""#834 S2 (#828) — the bounded year probe matches the qualifier.

Plan: ``docs/superpowers/plans/2026-09-13-834-s2-source-recovery-read-model.md``.

`_CODEX_PROJECT_METADATA_HEALTH_SQL` counts missing conversation keys, missing
thread joins and refused accounting operands. The qualified reader uses the same
timestamp and integer conversions, so the probe must count every row it refuses.

The second half is the horizon. The generation's accounting window is roughly
thirty calendar days, while both detail routes read a YEAR, so a malformed row
aged thirty-one to three hundred and sixty-five days set no flag at all and the
page rendered in full with that row's cost silently absent from the totals.

The original horizon cases seed rows between thirty-one and three hundred and
sixty-five days old; the #852 cases also cover current rows and a budget period
that reaches beyond the detail year.
"""
from __future__ import annotations

import datetime as dt
import pathlib
import sqlite3
import sys

import pytest

from conftest import load_script, redirect_paths


UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 16, 18, tzinfo=UTC)
#: Outside the ~30-day accounting window, inside the 365-day detail horizon.
BETWEEN = NOW - dt.timedelta(days=200)
ROOT_KEY = "probe-root"
ROOT_PATH = "/synthetic/codex-root"
HEALTHY_CONVERSATION = "probe-conversation"
HEALTHY_PATH = f"{ROOT_PATH}/sessions/healthy.jsonl"


def _iso(value: dt.datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _seed_probe_store(ns, tmp_path, monkeypatch):
    """A minimal single-root store with one qualifying row inside the year."""
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "provider"))
    cache = ns["open_cache_db"]()
    cache.execute(
        "INSERT INTO codex_source_roots (source_root_key, canonical_root_path,"
        " first_seen_utc, last_seen_utc) VALUES (?,?,?,?)",
        (ROOT_KEY, ROOT_PATH, _iso(NOW - dt.timedelta(days=365)), _iso(NOW)),
    )
    cache.execute(
        "INSERT INTO codex_conversation_threads "
        "(conversation_key, source_root_key, native_thread_id, "
        "root_thread_id, source_path, cwd, git_json, first_seen_utc, "
        "last_seen_utc) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            HEALTHY_CONVERSATION, ROOT_KEY, "probe-thread", "probe-thread",
            HEALTHY_PATH, "/synthetic/project-a", None,
            _iso(NOW - dt.timedelta(days=300)), _iso(NOW),
        ),
    )
    _insert_entry(
        cache,
        source_path=HEALTHY_PATH,
        line_offset=1,
        timestamp=_iso(BETWEEN),
        conversation_key=HEALTHY_CONVERSATION,
    )
    # A second healthy row INSIDE the ~30-day accounting window, so the
    # generation publishes a project row a detail route can request. Without
    # it the store is healthy but has nothing to ask about, and the end-to-end
    # cases below could not distinguish a degraded route from an empty one.
    _insert_entry(
        cache,
        source_path=HEALTHY_PATH,
        line_offset=2,
        timestamp=_iso(NOW - dt.timedelta(days=5)),
        conversation_key=HEALTHY_CONVERSATION,
    )
    cache.commit()
    return cache


def _insert_entry(
    cache,
    *,
    source_path,
    line_offset,
    timestamp,
    conversation_key,
    source_root_key=ROOT_KEY,
    input_tokens=100,
    cached_input_tokens=10,
    output_tokens=20,
    reasoning_output_tokens=5,
    total_tokens=130,
):
    cache.execute(
        "INSERT INTO codex_session_entries "
        "(source_path, line_offset, timestamp_utc, session_id, model, "
        "input_tokens, cached_input_tokens, output_tokens, "
        "reasoning_output_tokens, total_tokens, source_root_key, "
        "conversation_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            source_path, line_offset, timestamp, "probe-session", "gpt-5",
            input_tokens, cached_input_tokens, output_tokens,
            reasoning_output_tokens, total_tokens, source_root_key,
            conversation_key,
        ),
    )


#: The four deterministic qualification failures, each seeded on a row aged
#: two hundred days. The last two are the ones the retired health SQL missed
#: entirely: it counted them healthy while the qualified reader raised.
_COUNTEREXAMPLES = {
    "missing_conversation_key": dict(conversation_key=""),
    "missing_thread_join": dict(conversation_key="conversation-with-no-thread"),
    # Malformed but still INSIDE the window's TEXT range: the probe and the
    # qualified reader share those bounds, so a timestamp that sorts outside
    # them is invisible to both and would prove nothing.
    "malformed_timestamp": dict(timestamp="2026-01-01T00:00:99+00:00"),
    "naive_timestamp": dict(timestamp="2026-01-01T00:00:00"),
    "malformed_token_value": dict(input_tokens="not-a-number"),
    # A blob rather than a NULL: the token columns are `INTEGER NOT NULL
    # DEFAULT 0`, so the store cannot hold a NULL at all (asserted below), but
    # SQLite's dynamic typing keeps a blob in an INTEGER-affinity column.
    "blob_token_value": dict(output_tokens=b"\x00\x01"),
}


def _seed_counterexample(cache, name):
    overrides = dict(_COUNTEREXAMPLES[name])
    _insert_entry(
        cache,
        source_path=overrides.pop("source_path", HEALTHY_PATH),
        line_offset=500,
        timestamp=overrides.pop("timestamp", _iso(BETWEEN)),
        conversation_key=overrides.pop("conversation_key", HEALTHY_CONVERSATION),
        **overrides,
    )
    cache.commit()


@pytest.fixture
def probe_store(tmp_path, monkeypatch):
    ns = load_script()
    cache = _seed_probe_store(ns, tmp_path, monkeypatch)
    try:
        yield ns, cache
    finally:
        cache.close()


def test_828_the_probe_reports_a_healthy_store(probe_store):
    """The control. Without it every counterexample below proves nothing."""
    _ns, cache = probe_store
    analytics = sys.modules["_cctally_source_analytics"]
    result = analytics.probe_codex_detail_metadata_health(
        cache_conn=cache, start=NOW - dt.timedelta(days=365), end=NOW,
    )
    assert result.incomplete_rows == 0
    assert result.total_rows == 2


@pytest.mark.parametrize("name", sorted(_COUNTEREXAMPLES))
def test_828_the_probe_counts_every_deterministic_qualification_failure(
    probe_store, name,
):
    """Each row the qualified reader would reject must be counted here.

    `missing_conversation_key` and `missing_thread_join` were already counted.
    The other four are the review's finding: the qualified reader parses the
    timestamp at `_parse_timestamp` and the integer token fields inside
    `_calculate_codex_entry_cost`, and raises on either, while the health SQL
    looked only at the joins.
    """
    _ns, cache = probe_store
    analytics = sys.modules["_cctally_source_analytics"]
    _seed_counterexample(cache, name)
    result = analytics.probe_codex_detail_metadata_health(
        cache_conn=cache, start=NOW - dt.timedelta(days=365), end=NOW,
    )
    assert result.incomplete_rows == 1, (
        f"{name} was not counted as a qualification failure")


@pytest.mark.parametrize("name", sorted(_COUNTEREXAMPLES))
def test_828_the_qualified_reader_agrees_with_the_probe(probe_store, name):
    """The probe claims equivalence with the qualifier, so prove it.

    A probe that counted a row the reader accepts would degrade a page that
    renders, and one that missed a row the reader rejects leaves the defect.
    This drives the REAL reader over the same window and requires it to
    refuse, which is what makes the count above a measurement rather than an
    assertion about its own SQL.
    """
    _ns, cache = probe_store
    analytics = sys.modules["_cctally_source_analytics"]
    _seed_counterexample(cache, name)
    with pytest.raises(analytics.QualifiedMetadataUnavailable):
        analytics.load_qualified_codex_entries(
            NOW - dt.timedelta(days=365), NOW,
            speed="standard", sync=False, cache_conn=cache,
        )


def test_828_a_null_token_value_is_unreachable_in_this_store(probe_store):
    """The probe's `typeof = 'null'` leg is defensive, and this says why.

    The qualified reader catches `TypeError` from `int(None)`, but the token
    columns are `INTEGER NOT NULL DEFAULT 0`, so no writer and no hand repair
    can put a NULL there. Recorded as a test rather than left as an untested
    branch nobody can explain.
    """
    import sqlite3

    _ns, cache = probe_store
    with pytest.raises(sqlite3.IntegrityError):
        _insert_entry(
            cache,
            source_path=HEALTHY_PATH,
            line_offset=777,
            timestamp=_iso(BETWEEN),
            conversation_key=HEALTHY_CONVERSATION,
            output_tokens=None,
        )


def test_828_the_probe_does_not_flag_a_row_outside_the_horizon(probe_store):
    """A row older than the detail horizon is not the detail route's problem."""
    _ns, cache = probe_store
    analytics = sys.modules["_cctally_source_analytics"]
    _insert_entry(
        cache,
        source_path=HEALTHY_PATH,
        line_offset=900,
        timestamp=_iso(NOW - dt.timedelta(days=400)),
        conversation_key="",
    )
    cache.commit()
    result = analytics.probe_codex_detail_metadata_health(
        cache_conn=cache, start=NOW - dt.timedelta(days=365), end=NOW,
    )
    assert result.incomplete_rows == 0


def test_828_the_probe_uses_the_timestamp_leading_index(probe_store):
    """A year-bounded probe that scans the table is not bounded at all.

    Direct non-null bounds rather than `(? IS NULL OR ...)`: the nullable form
    the unbounded doctor helper needs is opaque to the planner, so it cannot
    use the timestamp-leading index and reads every row in the table.
    """
    _ns, cache = probe_store
    analytics = sys.modules["_cctally_source_analytics"]
    plan = analytics.explain_codex_detail_metadata_probe(
        cache_conn=cache, start=NOW - dt.timedelta(days=365), end=NOW,
    )
    # The plan names the table by its query alias, `entries`.
    entries_steps = [step for step in plan if " entries " in f" {step} "]
    assert entries_steps, f"the plan never reached the entries table: {plan}"
    assert all(
        step.startswith("SEARCH entries USING INDEX "
                        "idx_codex_entries_ts_root_conversation")
        and "timestamp_utc>?" in step and "timestamp_utc<?" in step
        for step in entries_steps
    ), f"the probe does not seek the timestamp-leading index: {plan}"
    assert not any(step.startswith("SCAN entries") for step in entries_steps), (
        f"the probe full-scans the entries table: {plan}")


def test_828_the_unbounded_doctor_helper_is_preserved(probe_store):
    """`doctor` reads all history through the nullable-bound helper.

    The bounded probe is added BESIDE it rather than replacing its signature,
    because `doctor`'s contract is an all-history read with no bounds at all.
    """
    _ns, cache = probe_store
    analytics = sys.modules["_cctally_source_analytics"]
    _seed_counterexample(cache, "missing_conversation_key")
    _insert_entry(
        cache,
        source_path=HEALTHY_PATH,
        line_offset=901,
        timestamp=_iso(NOW - dt.timedelta(days=400)),
        conversation_key="",
    )
    cache.commit()
    unbounded = analytics.load_codex_project_metadata_health(cache_conn=cache)
    assert unbounded.incomplete_rows == 2
    assert unbounded.total_rows == 4


def test_828_the_accounting_window_health_is_a_separate_result(probe_store):
    """A year-old malformed row must not redefine thirty-day availability.

    The accounting health read bounds the ~30-day window that decides Projects
    availability and the partial generation. The detail probe bounds a year.
    Folding one into the other would withhold the thirty-day project ranking
    over a row no thirty-day figure ever counted.
    """
    _ns, cache = probe_store
    analytics = sys.modules["_cctally_source_analytics"]
    _seed_counterexample(cache, "malformed_timestamp")
    accounting = analytics.load_codex_project_metadata_health(
        cache_conn=cache, start=NOW - dt.timedelta(days=30), end=NOW,
    )
    assert accounting.incomplete_rows == 0
    probe = analytics.probe_codex_detail_metadata_health(
        cache_conn=cache, start=NOW - dt.timedelta(days=365), end=NOW,
    )
    assert probe.incomplete_rows == 1


def test_828_the_probe_refuses_a_naive_or_inverted_bound(probe_store):
    _ns, cache = probe_store
    analytics = sys.modules["_cctally_source_analytics"]
    with pytest.raises(ValueError):
        analytics.probe_codex_detail_metadata_health(
            cache_conn=cache,
            start=dt.datetime(2026, 1, 1),
            end=NOW,
        )
    with pytest.raises(ValueError):
        analytics.probe_codex_detail_metadata_health(
            cache_conn=cache, start=NOW, end=NOW - dt.timedelta(days=365),
        )


# === The generation carries one probe, and both detail routes read it ======


def _build_generation(ns, cache, *, direct=False, codex_budget=None):
    """Build one frozen Codex generation over the probe store."""
    sources = sys.modules["_cctally_dashboard_sources"]
    stats = ns["open_db"]()
    try:
        semantics = sources.resolve_dashboard_source_semantics(
            {}, display_tz_name="UTC",
        )
        context = sources.DashboardReadContext(
            cache_conn=cache,
            stats_conn=stats,
            range_start=NOW - dt.timedelta(days=30),
            now_utc=NOW,
            display_tz_name=semantics.display_tz_name,
            week_start_idx=semantics.week_start_idx,
            week_start_name=semantics.week_start_name,
            speed=semantics.speed,
            codex_budget=codex_budget if codex_budget is not None
            else semantics.codex_budget,
        )
        if direct:
            with sources.codex_path_scope() as path_scope:
                return sources._build_codex_source_state(
                    context, data_version="probe-v1", path_scope=path_scope)
        return sources.build_codex_source_state(context, data_version="probe-v1")
    finally:
        stats.close()


def _snapshot(ns, codex):
    from _lib_dashboard_sources import (
        SOURCE_SCHEMA_VERSION,
        CapabilityRecord,
        SourceDashboardBundle,
        SourceDashboardState,
        compose_all_state,
    )

    claude = SourceDashboardState(
        source="claude",
        availability="ok",
        freshness="fresh",
        warnings=(),
        data_version="claude-v1",
        last_success_at=NOW,
        capabilities={"sessions": CapabilityRecord("supported")},
        data={"sessions": {"rows": ()}, "projects": {"rows": ()},
              "quota": {"blocks": ()}},
    )
    snap = ns["_empty_dashboard_snapshot"]()
    snap.generated_at = NOW
    snap.source_bundle = SourceDashboardBundle(
        source_schema_version=SOURCE_SCHEMA_VERSION,
        default_source="claude",
        source_order=("claude", "codex", "all"),
        sources={"claude": claude, "codex": codex,
                 "all": compose_all_state(claude, codex)},
    )
    return snap


def test_828_a_healthy_store_publishes_a_healthy_generation(probe_store):
    """The control for the two cases below."""
    ns, cache = probe_store
    codex = _build_generation(ns, cache)
    assert codex.metadata_health["state"] == "healthy"
    assert codex.metadata_health["incomplete_rows"] == 0


@pytest.mark.parametrize("name", sorted(_COUNTEREXAMPLES))
def test_828_a_year_old_malformed_row_degrades_both_detail_routes(
    probe_store, name,
):
    """The horizon finding, end to end.

    Each of these rows is aged two hundred days: outside the generation's
    ~30-day accounting window and inside the 365-day window both detail routes
    read. Before this probe the generation reported healthy attribution and
    both pages rendered in full with that row's cost silently absent from the
    totals.
    """
    ns, cache = probe_store
    dashboard = sys.modules["_cctally_dashboard"]
    _seed_counterexample(cache, name)
    codex = _build_generation(ns, cache)

    assert codex.metadata_health["state"] == "malformed_row_partial", name
    assert codex.metadata_health["incomplete_rows"] == 1
    assert codex.metadata_health["retryable"] is False

    snap = _snapshot(ns, codex)
    assert dashboard._codex_generation_metadata_state(snap) == (
        "malformed_row_partial")
    assert dashboard._codex_generation_metadata_partial(snap) is True

    rows = codex.data["projects"]["rows"]
    assert rows, "the generation published no project row to request"
    detail = dashboard.build_source_detail(
        snapshot=snap, source="codex", resource="project",
        key=str(rows[0]["key"]),
    )
    assert detail["metadata_availability"] == "partial"
    # The REASON, not only the availability. Asserting availability alone let
    # the project route serve the transient retry sentence on this malformed
    # carrier: the route manufactures its `QualifiedMetadataUnavailable` from
    # the carrier, and omitting `transient` there defaulted it to True, so the
    # detail promised a retry that `retryable: false` says will never come
    # while the chip beside it advised the cache rebuild. Availability was
    # `partial` throughout, so only the reason distinguishes the two.
    assert detail["metadata_reason"] == (
        "Project metadata is unavailable for this item.")

    # And NO source warning, which is the horizon split stated as a test.
    # This row is aged two hundred days: inside the 365-day window both detail
    # routes read, and outside the ~30-day accounting window the
    # `codex_metadata_incomplete` warning describes. So the detail discloses
    # it and the chip does not, because the chip answers for the window the
    # panels show. An earlier draft of this test asserted the counted rebuild
    # message here and failed for exactly that reason; the counted message is
    # pinned where an IN-window row produces it, in
    # `tests/test_dashboard_source_read_model.py`.
    assert [
        warning.code for warning in codex.warnings
        if warning.code == "codex_metadata_incomplete"
    ] == []


def test_828_a_failed_qualified_read_overrides_a_healthy_probe(probe_store):
    """A build whose own read raised has established nothing about the rows.

    The probe can find the store healthy and the qualified accounting read can
    still fail, and reporting a malformed-row partial there would tell the
    reader to rebuild a cache that is not the problem.
    """
    ns, cache = probe_store
    sources = sys.modules["_cctally_dashboard_sources"]
    analytics = sys.modules["_cctally_source_analytics"]

    def _unavailable(*_args, **_kwargs):
        raise analytics.QualifiedMetadataUnavailable(
            "Codex accounting metadata is unavailable")

    original = sources.load_qualified_codex_entries
    sources.load_qualified_codex_entries = _unavailable
    try:
        codex = _build_generation(ns, cache)
    finally:
        sources.load_qualified_codex_entries = original

    assert codex.metadata_health == {
        "state": "transient_read_failure",
        "incomplete_rows": None,
        "retryable": True,
    }

    # The route says "will retry", never "rebuild your cache" — and since
    # #846 §4.6 rule 1 there is no project row to ask about at all, on the
    # parent or on any account child. The route's own disclosure moved to the
    # malformed generation, which is the reproducible degraded state.
    dashboard = sys.modules["_cctally_dashboard"]
    snap = _snapshot(ns, codex)
    assert dashboard._codex_generation_metadata_state(snap) == (
        "transient_read_failure")
    assert codex.data["projects"]["rows"] == ()

    # The degraded generation is never handed back unexamined (#830) ...
    import _lib_dashboard_sources as lds
    assert lds.reuse_coherent_source_state(
        codex, data_version=codex.data_version) is None

    # ... and the next healthy publish replaces the state and clears it.
    recovered = _build_generation(ns, cache)
    assert recovered.metadata_health["state"] == "healthy"
    recovered_snap = _snapshot(ns, recovered)
    assert recovered.data["projects"]["rows"], (
        "the rebuilt healthy generation published no project row")
    recovered_detail = dashboard.build_source_detail(
        snapshot=recovered_snap, source="codex", resource="project",
        key=str(recovered.data["projects"]["rows"][0]["key"]),
    )
    assert recovered_detail["metadata_availability"] is None
    assert recovered_detail["metadata_reason"] is None


# === #845 §4.1–§4.3 — decode tolerance over both columns and all horizons ===

#: The reproduction #845 names: a TEXT value holding bytes that are not valid
#: UTF-8. `sqlite3` raises `OperationalError: Could not decode to UTF-8` on any
#: statement that selects it through the default `str` factory.
BAD_BYTES = b"/synthetic/\xffproject"

DECODE_ROOT_PATH_NAME = "provider"
BAD_CONVERSATION = "bad-conversation"
BAD_NATIVE = "bad-thread"
ALIAS_CONVERSATION = "alias-conversation"

#: The three horizons §4.2 and A2 separate: outside the one-year detail
#: horizon, inside the year and outside the ~30-day accounting window, and
#: inside the accounting window.
HORIZONS = {"outside-year": 400, "inside-year": 200, "inside-accounting": 5}

#: One file alias row. `codex_session_files` is keyed by `path`; the columns
#: this specification reads are `source_root_key` and `last_native_thread_id`.
_ALIAS_FILE_SQL = (
    "INSERT INTO codex_session_files "
    "(path, size_bytes, mtime_ns, last_byte_offset, last_ingested_at, "
    " source_root_key, last_native_thread_id) VALUES (?,0,0,0,?,?,?)"
)


def _seed_decode_store(
    ns, tmp_path, monkeypatch, *, column="cwd", provenance="direct",
    age_days=400, second_bad_age_days=None, root_dir=None,
):
    """A single-root store with one healthy project and one undecodable value.

    The undecodable thread is ALWAYS the ``last_native_thread_id`` of a
    ``codex_session_files`` row, so today's inherited join and both whole-table
    scans reach it whatever the affected entry's age. A direct-only old thread
    would leave the qualified read untouched and prove nothing.

    ``provenance`` selects which half of the merge carries the bad value.
    ``direct`` puts the affected entries on the bad thread's own conversation;
    ``inherited`` puts them on a second conversation whose own metadata is
    empty and whose path's alias winner is the bad thread.
    """
    root = pathlib.Path(root_dir) if root_dir else tmp_path / DECODE_ROOT_PATH_NAME
    root.mkdir(parents=True, exist_ok=True)
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    monkeypatch.setenv("CODEX_HOME", str(root))

    # Codex's own task short names, which `_codex_conversation_metadata` reads
    # per provider root. Without this file "sessions carry their titles" would
    # be vacuous, because every title would be None in both directions.
    import sqlite3 as _sqlite3

    state = _sqlite3.connect(root / "state_5.sqlite")
    try:
        state.execute(
            "CREATE TABLE IF NOT EXISTS threads (id TEXT PRIMARY KEY, title TEXT)")
        state.execute(
            "INSERT OR REPLACE INTO threads VALUES (?,?)",
            ("probe-thread", "Healthy task"))
        state.commit()
    finally:
        state.close()

    bad_path = f"{root}/sessions/bad.jsonl"
    alias_path = f"{root}/sessions/alias.jsonl"
    healthy_path = f"{root}/sessions/healthy.jsonl"

    cache = ns["open_cache_db"]()
    cache.execute(
        "INSERT INTO codex_source_roots (source_root_key, canonical_root_path,"
        " first_seen_utc, last_seen_utc) VALUES (?,?,?,?)",
        (ROOT_KEY, str(root), _iso(NOW - dt.timedelta(days=500)), _iso(NOW)),
    )
    cache.execute(
        "INSERT INTO codex_conversation_threads "
        "(conversation_key, source_root_key, native_thread_id, root_thread_id,"
        " source_path, cwd, git_json, first_seen_utc, last_seen_utc) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            HEALTHY_CONVERSATION, ROOT_KEY, "probe-thread", "probe-thread",
            healthy_path, "/synthetic/project-a", None,
            _iso(NOW - dt.timedelta(days=300)), _iso(NOW),
        ),
    )
    for offset, age in ((1, 5), (2, 200)):
        _insert_entry(
            cache, source_path=healthy_path, line_offset=offset,
            timestamp=_iso(NOW - dt.timedelta(days=age)),
            conversation_key=HEALTHY_CONVERSATION,
        )

    # The undecodable thread. When `git_json` carries the bad bytes the `cwd`
    # must be absent, or the resolver stops before ever consulting it.
    cache.execute(
        "INSERT INTO codex_conversation_threads "
        "(conversation_key, source_root_key, native_thread_id, root_thread_id,"
        " source_path, cwd, git_json, first_seen_utc, last_seen_utc) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            BAD_CONVERSATION, ROOT_KEY, BAD_NATIVE, BAD_NATIVE, bad_path,
            None, None, _iso(NOW - dt.timedelta(days=500)),
            _iso(NOW - dt.timedelta(days=400)),
        ),
    )
    cache.execute(
        f"UPDATE codex_conversation_threads SET {column} = CAST(? AS TEXT) "
        "WHERE conversation_key = ?",
        (BAD_BYTES, BAD_CONVERSATION),
    )
    cache.execute(
        _ALIAS_FILE_SQL, (bad_path, _iso(NOW), ROOT_KEY, BAD_NATIVE),
    )

    affected_path, affected_conversation = bad_path, BAD_CONVERSATION
    if provenance == "inherited":
        cache.execute(
            "INSERT INTO codex_conversation_threads "
            "(conversation_key, source_root_key, native_thread_id,"
            " root_thread_id, source_path, cwd, git_json, first_seen_utc,"
            " last_seen_utc) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                ALIAS_CONVERSATION, ROOT_KEY, "alias-thread", "alias-thread",
                alias_path, "", "", _iso(NOW - dt.timedelta(days=500)),
                _iso(NOW - dt.timedelta(days=401)),
            ),
        )
        cache.execute(
            _ALIAS_FILE_SQL, (alias_path, _iso(NOW), ROOT_KEY, BAD_NATIVE),
        )
        affected_path, affected_conversation = alias_path, ALIAS_CONVERSATION

    ages = [age_days] if second_bad_age_days is None else [
        age_days, second_bad_age_days]
    for index, age in enumerate(ages):
        _insert_entry(
            cache, source_path=affected_path, line_offset=600 + index,
            timestamp=_iso(NOW - dt.timedelta(days=age)),
            conversation_key=affected_conversation,
        )
    cache.commit()
    return cache


def test_845_an_undecodable_value_outside_every_horizon_stays_healthy(
    tmp_path, monkeypatch,
):
    """A1. The provider must not degrade over a value no figure reads.

    The maintainer measured the defect this way: an undecodable `cwd` on a
    conversation whose entries are seven months outside the accounting window
    produced `transient_read_failure` and zero project rows over forty polls.
    The value here is aged four hundred days, so it is outside the one-year
    detail horizon too and no published figure counts it at all.

    It fails on `origin/main` at the carrier: the inherited alias statement is
    whole-table, so the qualified read raises `OperationalError`, the capture
    translates it into a transient refusal and the generation publishes
    `transient_read_failure`.
    """
    ns = load_script()
    cache = _seed_decode_store(ns, tmp_path, monkeypatch, age_days=400)
    try:
        analytics = sys.modules["_cctally_source_analytics"]
        codex = _build_generation(ns, cache)

        assert codex.metadata_health == {
            "state": "healthy", "incomplete_rows": 0, "retryable": False,
        }
        rows = codex.data["projects"]["rows"]
        assert len(rows) == 1, rows
        assert rows[0]["label"] == "project-a"

        sessions = codex.data["sessions"]["rows"]
        titles = [row.get("project") for row in sessions]
        assert "project-a" in titles, sessions
        assert [
            warning.code for warning in codex.warnings
            if warning.code == "codex_metadata_incomplete"
        ] == []

        # The four CLI consumers establish their project section, which is the
        # other half of §4.2's table: the read no longer raises at all.
        entries = analytics.load_qualified_codex_entries(
            NOW - dt.timedelta(days=30), NOW,
            speed="standard", sync=False, cache_conn=cache,
        )
        assert {entry.project_label for entry in entries} == {"project-a"}
    finally:
        cache.close()


@pytest.mark.parametrize("column", ["cwd", "git_json"])
@pytest.mark.parametrize("provenance", ["direct", "inherited"])
@pytest.mark.parametrize("horizon", sorted(HORIZONS))
def test_845_a2_the_matrix_holds_over_both_columns_and_all_three_horizons(
    tmp_path, monkeypatch, column, provenance, horizon,
):
    """A2. Both columns, both provenances, all three horizons.

    Every case is alias-reachable, so `origin/main`'s whole-table inherited
    join raises for every one of them and the generation publishes
    `transient_read_failure` whatever the affected entry's age.
    """
    ns = load_script()
    age = HORIZONS[horizon]
    cache = _seed_decode_store(
        ns, tmp_path, monkeypatch, column=column, provenance=provenance,
        age_days=age,
        # The inside-accounting case seeds a SECOND affected row aged two
        # hundred days, so the carrier's one-year count and the chip's
        # accounting-window count cannot coincide.
        second_bad_age_days=200 if horizon == "inside-accounting" else None,
    )
    try:
        codex = _build_generation(ns, cache)
        warnings = [
            warning for warning in codex.warnings
            if warning.code == "codex_metadata_incomplete"
        ]
        if horizon == "outside-year":
            assert codex.metadata_health == {
                "state": "healthy", "incomplete_rows": 0, "retryable": False,
            }
            assert warnings == []
        elif horizon == "inside-year":
            assert codex.metadata_health == {
                "state": "malformed_row_partial", "incomplete_rows": 1,
                "retryable": False,
            }
            # The row is outside the accounting window, so the chip says
            # nothing and the project rows are complete.
            assert warnings == []
            assert len(codex.data["projects"]["rows"]) == 1
        else:
            assert codex.metadata_health == {
                "state": "malformed_row_partial", "incomplete_rows": 2,
                "retryable": False,
            }
            assert len(warnings) == 1
            assert warnings[0].message.startswith("1 Codex accounting row(s)")
            assert "--rebuild" in warnings[0].message
    finally:
        cache.close()


@pytest.mark.parametrize("column", ["cwd", "git_json"])
@pytest.mark.parametrize("provenance", ["direct", "inherited"])
def test_845_a2_the_year_only_case_is_byte_identical_to_the_healthy_one(
    tmp_path, monkeypatch, column, provenance,
):
    """A2's byte comparison. A malformed row inside the year and outside the
    accounting window leaves `metadata_incomplete` false, so the path map
    serves the session rows, the cache-report payload and every account child
    exactly as it serves them for a generation whose bad row no aggregate
    counts at all."""
    ns = load_script()
    # ONE provider root for both generations: the session key is a digest
    # over the root path, so two roots would differ on the key alone and the
    # comparison would fail for a reason that has nothing to do with §4.4.
    shared_root = tmp_path / "shared-provider"
    healthy_cache = _seed_decode_store(
        ns, tmp_path / "healthy", monkeypatch, column=column,
        provenance=provenance, age_days=400, root_dir=shared_root,
    )
    try:
        healthy = _build_generation(ns, healthy_cache)
    finally:
        healthy_cache.close()
    year_cache = _seed_decode_store(
        ns, tmp_path / "year", monkeypatch, column=column,
        provenance=provenance, age_days=200, root_dir=shared_root,
    )
    try:
        year = _build_generation(ns, year_cache)
    finally:
        year_cache.close()

    assert healthy.metadata_health["state"] == "healthy"
    assert year.metadata_health["state"] == "malformed_row_partial"
    for domain in ("sessions", "cache_report", "projects"):
        assert year.data[domain] == healthy.data[domain], domain
    assert year.data.get("accounts") == healthy.data.get("accounts")


# === A4 — every affected entry is counted exactly once, per population =====


def _seed_overlap_store(ns, tmp_path, monkeypatch):
    """One undecodable thread reachable both directly and through an alias."""
    root = tmp_path / DECODE_ROOT_PATH_NAME
    root.mkdir(parents=True, exist_ok=True)
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    monkeypatch.setenv("CODEX_HOME", str(root))
    bad_path = f"{root}/sessions/bad.jsonl"
    cache = ns["open_cache_db"]()
    cache.execute(
        "INSERT INTO codex_source_roots (source_root_key, canonical_root_path,"
        " first_seen_utc, last_seen_utc) VALUES (?,?,?,?)",
        (ROOT_KEY, str(root), _iso(NOW - dt.timedelta(days=500)), _iso(NOW)),
    )
    cache.execute(
        "INSERT INTO codex_conversation_threads "
        "(conversation_key, source_root_key, native_thread_id, root_thread_id,"
        " source_path, cwd, git_json, first_seen_utc, last_seen_utc) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (BAD_CONVERSATION, ROOT_KEY, BAD_NATIVE, BAD_NATIVE, bad_path,
         None, None, _iso(NOW - dt.timedelta(days=500)), _iso(NOW)),
    )
    cache.execute(
        "UPDATE codex_conversation_threads SET cwd = CAST(? AS TEXT) "
        "WHERE conversation_key = ?", (BAD_BYTES, BAD_CONVERSATION),
    )
    cache.execute(
        _ALIAS_FILE_SQL, (bad_path, _iso(NOW), ROOT_KEY, BAD_NATIVE))
    cache.commit()
    return cache, bad_path


#: Every overlap §4.3 says is constructible, with the count each population
#: must report. A missing-thread-join overlap is NOT constructible: a direct
#: undecodable row proves the direct thread exists, and an inherited one proves
#: the alias `NOT EXISTS` is false.
_OVERLAP_CASES = {
    # Named by both sets at once; the union counts the entry once.
    "direct-and-inherited": ({}, {"probe": 1, "health": 1, "doctor": 1}),
    # The aggregate already counted it under `missing_conversation_key_rows`,
    # in every population, so the decode count adds nothing.
    "missing-conversation-key": (
        {"conversation_key": ""}, {"probe": 0, "health": 0, "doctor": 0},
    ),
    # All three populations now count refused accounting operands directly.
    "unusable-token-operand": (
        {"input_tokens": "not-a-number"},
        {"probe": 0, "health": 0, "doctor": 0},
    ),
    # Same, for a timestamp that is lexically in range and unparseable.
    "unparseable-timestamp": (
        {"timestamp": "2026-07-14T00:00:99+00:00"},
        {"probe": 0, "health": 0, "doctor": 0},
    ),
}


@pytest.mark.parametrize("case", sorted(_OVERLAP_CASES))
def test_845_a4_each_population_subtracts_only_the_overlaps_it_counted(
    tmp_path, monkeypatch, case,
):
    """A4. The three aggregates count different reasons, so the subtraction is
    per population. Counting threads, or running the matrix against the probe
    alone, would not distinguish them."""
    ns = load_script()
    analytics = sys.modules["_cctally_source_analytics"]
    cache, bad_path = _seed_overlap_store(ns, tmp_path, monkeypatch)
    overrides, expected = _OVERLAP_CASES[case]
    try:
        _insert_entry(
            cache, source_path=bad_path, line_offset=10,
            timestamp=overrides.pop("timestamp", _iso(NOW - dt.timedelta(days=2))),
            conversation_key=overrides.pop(
                "conversation_key", BAD_CONVERSATION),
            **overrides,
        )
        cache.commit()

        statements: list[str] = []
        cache.set_trace_callback(statements.append)
        probe = analytics.probe_codex_detail_metadata_health(
            cache_conn=cache, start=NOW - dt.timedelta(days=365), end=NOW)
        health = analytics.load_codex_project_metadata_health(
            cache_conn=cache, start=NOW - dt.timedelta(days=30), end=NOW)
        doctor = analytics.load_codex_project_metadata_health(cache_conn=cache)
        cache.set_trace_callback(None)

        assert probe.undecodable_metadata_rows == expected["probe"], case
        assert health.undecodable_metadata_rows == expected["health"], case
        assert doctor.undecodable_metadata_rows == expected["doctor"], case
        if case in {"unusable-token-operand", "unparseable-timestamp"}:
            assert health.malformed_accounting_rows == 1
            assert doctor.malformed_accounting_rows == 1
            assert health.incomplete_rows == doctor.incomplete_rows == 1

        # The overlap query reproduces the population's complete caller shape,
        # over exactly the affected ids and nothing else.
        overlaps = [
            " ".join(statement.split()) for statement in statements
            if "LEFT JOIN codex_conversation_threads" in statement
            and "entries.id IN" in statement
        ]
        assert len(overlaps) == 3, overlaps
        probe_overlap, health_overlap, doctor_overlap = overlaps
        for statement in overlaps:
            assert "codex_accounting_row_usable(" in statement
        assert "NOT EXISTS" in health_overlap
        # The trace callback expands bound parameters, so the bound predicate
        # is matched without its placeholder.
        assert "entries.timestamp_utc >=" in health_overlap
        assert "entries.timestamp_utc >=" not in doctor_overlap
        assert "entries.timestamp_utc >=" in probe_overlap
        for statement in overlaps:
            ids = statement.split("entries.id IN (")[1].split(")")[0]
            assert len(ids.split(",")) == 1, statement
    finally:
        cache.close()


def test_845_a4_a_clean_store_runs_no_per_identity_query(tmp_path, monkeypatch):
    """§4.3: the count is zero-cost on a clean store, because both undecodable
    sets are empty and no per-identity query runs at all."""
    ns = load_script()
    analytics = sys.modules["_cctally_source_analytics"]
    cache = _seed_probe_store(ns, tmp_path, monkeypatch)
    try:
        statements: list[str] = []
        cache.set_trace_callback(statements.append)
        health = analytics.load_codex_project_metadata_health(
            cache_conn=cache, start=NOW - dt.timedelta(days=30), end=NOW)
        cache.set_trace_callback(None)
        assert health.undecodable_metadata_rows == 0
        assert not [
            statement for statement in statements
            if "idx_codex_entries_conversation" in statement
            or "entries.id IN" in statement
        ]
    finally:
        cache.close()


@pytest.mark.parametrize("bounded", [True, False])
@pytest.mark.parametrize("inherited", [True, False])
def test_845_a7_every_new_counting_statement_seeks(
    tmp_path, monkeypatch, bounded, inherited,
):
    """A7. A year-bounded count that scans the entries table is not bounded at
    all. Doctor's all-history aggregate keeps scanning, by name; every new or
    changed statement must seek."""
    ns = load_script()
    analytics = sys.modules["_cctally_source_analytics"]
    cache = _seed_probe_store(ns, tmp_path, monkeypatch)
    try:
        plan = analytics.explain_undecodable_codex_identity_seek(
            cache_conn=cache, inherited=inherited, bounded=bounded)
        expected_index = (
            "idx_codex_entries_root_path" if inherited
            else "idx_codex_entries_conversation"
        )
        assert any(
            step.startswith(f"SEARCH entries USING INDEX {expected_index}")
            for step in plan
        ), plan
        assert not any(step.startswith("SCAN entries") for step in plan), plan

        overlap = analytics.explain_undecodable_codex_overlap(
            cache_conn=cache, include_probe_reasons=inherited, bounded=bounded)
        assert not any(step.startswith("SCAN entries") for step in overlap), (
            overlap)
    finally:
        cache.close()


# === A21 — `cctally doctor --json` counts undecodable rows all history =====


def _doctor_json(home):
    """Run the real CLI over the store the seeders redirected to ``home``.

    ``redirect_paths`` puts the cache under ``<home>/.local/share/cctally``,
    and the seeders pass ``tmp_path / "data"`` as that home, so this takes the
    same value rather than the test's own ``tmp_path``.
    """
    import json
    import os
    import subprocess

    repo = pathlib.Path(__file__).resolve().parent.parent
    env = os.environ.copy()
    env["TZ"] = "Etc/UTC"
    env["HOME"] = str(home)
    env["CCTALLY_DATA_DIR"] = str(home / ".local" / "share" / "cctally")
    env.pop("CODEX_HOME", None)
    result = subprocess.run(
        [sys.executable, str(repo / "bin" / "cctally"), "doctor", "--json"],
        env=env, capture_output=True, text=True,
    )
    return result, json.loads(result.stdout)


def _codex_metadata_check(payload):
    data = next(
        category for category in payload["categories"]
        if category["id"] == "data"
    )
    return next(
        check for check in data["checks"]
        if check["id"] == "data.codex_project_metadata"
    )


def test_845_a21_doctor_counts_a_two_year_old_undecodable_thread(
    tmp_path, monkeypatch,
):
    """A21. Doctor's read is ALL HISTORY, so a thread whose entries are two
    years old is outside the window-bounded probe and must still be counted
    here. The window-bounded probe would report zero for exactly this store."""
    ns = load_script()
    cache = _seed_decode_store(
        ns, tmp_path, monkeypatch, age_days=730, second_bad_age_days=731)
    try:
        analytics = sys.modules["_cctally_source_analytics"]
        probe = analytics.probe_codex_detail_metadata_health(
            cache_conn=cache, start=NOW - dt.timedelta(days=365), end=NOW)
        assert probe.incomplete_rows == 0, (
            "precondition: the window-bounded probe must not see this store")
    finally:
        cache.close()

    result, payload = _doctor_json(tmp_path / "data")
    check = _codex_metadata_check(payload)
    assert check["severity"] == "warn", (check, result.stderr[-800:])
    assert check["details"]["undecodable_metadata_rows"] == 2
    assert check["details"]["incomplete_rows"] == 2
    assert "cache-sync --source codex --rebuild" in check["remediation"]


def test_845_a21_a_healthy_store_keeps_doctors_established_answer(
    tmp_path, monkeypatch,
):
    """The control: the golden for a healthy store is unchanged apart from the
    additive key, which reads zero."""
    ns = load_script()
    cache = _seed_probe_store(ns, tmp_path, monkeypatch)
    cache.close()
    _result, payload = _doctor_json(tmp_path / "data")
    check = _codex_metadata_check(payload)
    assert check["severity"] == "ok"
    assert check["summary"] == "qualified"
    assert check["details"]["undecodable_metadata_rows"] == 0
    assert check["details"]["incomplete_rows"] == 0


# === #852 — malformed accounting operands retain current good rows =========


@pytest.mark.parametrize("overrides", [
    {"input_tokens": "1-2"},
    {"input_tokens": float("inf")},
    {"total_tokens": "1-2"},
    {"timestamp": "2026-07-14TT00:00:00Z"},
    {"conversation_key": "", "input_tokens": "1-2"},
])
@pytest.mark.parametrize("direct", [False, True])
def test_852_malformed_accounting_row_is_counted_and_good_project_survives(
    tmp_path, monkeypatch, overrides, direct,
):
    """Capture must publish valid costs while omitting one refused row."""
    ns = load_script()
    analytics = sys.modules["_cctally_source_analytics"]
    cache = _seed_probe_store(ns, tmp_path, monkeypatch)
    try:
        _insert_entry(
            cache, source_path=HEALTHY_PATH, line_offset=8_100,
            **{
                "timestamp": _iso(NOW - dt.timedelta(days=2)),
                "conversation_key": HEALTHY_CONVERSATION,
                **overrides,
            },
        )
        cache.commit()
        probe = analytics.probe_codex_detail_metadata_health(
            cache_conn=cache, start=NOW - dt.timedelta(days=365), end=NOW)
        assert probe.incomplete_rows == 1
        with pytest.raises(analytics.QualifiedMetadataUnavailable) as refused:
            analytics.load_qualified_codex_entries(
                NOW - dt.timedelta(days=365), NOW, speed="standard",
                sync=False, cache_conn=cache)
        assert refused.value.transient is False
        rooted = analytics.load_cached_rooted_codex_accounting_entries(
            NOW - dt.timedelta(days=30), NOW, speed="standard",
            cache_conn=cache)
        assert len(rooted) == 1
        codex = _build_generation(ns, cache, direct=direct)
        assert codex.metadata_health == {
            "state": "malformed_row_partial", "incomplete_rows": 1,
            "retryable": False,
        }
        rows = codex.data["projects"]["rows"]
        assert len(rows) == 1 and rows[0]["cost_usd"] > 0, rows
    finally:
        cache.close()


@pytest.mark.parametrize("direct", [False, True])
def test_852_budget_accounting_extends_carrier_beyond_detail_year(
    tmp_path, monkeypatch, direct,
):
    ns = load_script()
    sources = sys.modules["_cctally_dashboard_sources"]
    cache = _seed_probe_store(ns, tmp_path, monkeypatch)
    try:
        _insert_entry(
            cache, source_path=HEALTHY_PATH, line_offset=8_101,
            timestamp=_iso(NOW - dt.timedelta(days=400)),
            conversation_key=HEALTHY_CONVERSATION, total_tokens="1-2",
        )
        cache.commit()
        monkeypatch.setattr(
            sources, "_configured_codex_budget_window",
            lambda _context: (
                "year", NOW - dt.timedelta(days=500), NOW + dt.timedelta(days=1)),
        )
        codex = _build_generation(
            ns, cache, direct=direct, codex_budget={"period": "year"})
        assert codex.metadata_health == {
            "state": "malformed_row_partial", "incomplete_rows": 1,
            "retryable": False,
        }
        assert codex.data["projects"]["rows"][0]["cost_usd"] > 0
    finally:
        cache.close()


@pytest.mark.parametrize("direct", [False, True])
def test_852_carrier_includes_accounting_end_instant(tmp_path, monkeypatch, direct):
    ns = load_script()
    cache = _seed_probe_store(ns, tmp_path, monkeypatch)
    try:
        _insert_entry(
            cache, source_path=HEALTHY_PATH, line_offset=8_104,
            timestamp=_iso(NOW), conversation_key=HEALTHY_CONVERSATION,
            input_tokens="1-2",
        )
        cache.commit()
        codex = _build_generation(ns, cache, direct=direct)
        assert codex.metadata_health == {
            "state": "malformed_row_partial", "incomplete_rows": 1,
            "retryable": False,
        }
    finally:
        cache.close()


def test_852_doctor_names_malformed_accounting_all_history(
    tmp_path, monkeypatch,
):
    ns = load_script()
    cache = _seed_probe_store(ns, tmp_path, monkeypatch)
    try:
        _insert_entry(
            cache, source_path=HEALTHY_PATH, line_offset=8_102,
            timestamp=_iso(NOW - dt.timedelta(days=400)),
            conversation_key=HEALTHY_CONVERSATION, total_tokens="1-2",
        )
        cache.commit()
    finally:
        cache.close()
    _result, payload = _doctor_json(tmp_path / "data")
    check = _codex_metadata_check(payload)
    assert check["severity"] == "warn"
    assert check["details"]["malformed_accounting_rows"] == 1
    assert check["details"]["incomplete_rows"] == 1


def test_852_doctor_counts_malformed_newest_timestamp(tmp_path, monkeypatch):
    ns = load_script()
    cache = _seed_probe_store(ns, tmp_path, monkeypatch)
    try:
        _insert_entry(
            cache, source_path=HEALTHY_PATH, line_offset=8_105,
            timestamp="2026-07-14TT00:00:00Z",
            conversation_key=HEALTHY_CONVERSATION,
        )
        cache.commit()
    finally:
        cache.close()

    result, payload = _doctor_json(tmp_path / "data")
    check = _codex_metadata_check(payload)
    assert check["severity"] == "warn", (check, result.stderr[-800:])
    assert check["details"]["malformed_accounting_rows"] == 1
    assert check["details"]["incomplete_rows"] == 1


@pytest.mark.parametrize("direct", [False, True])
def test_852_sqlite_probe_fault_remains_transient(
    tmp_path, monkeypatch, direct,
):
    ns = load_script()
    sources = sys.modules["_cctally_dashboard_sources"]
    cache = _seed_probe_store(ns, tmp_path, monkeypatch)
    try:
        def fail_probe(**_kwargs):
            raise sqlite3.OperationalError("interrupted read")

        monkeypatch.setattr(
            sources, "probe_codex_detail_metadata_health", fail_probe)
        codex = _build_generation(ns, cache, direct=direct)
        assert codex.metadata_health == {
            "state": "transient_read_failure", "incomplete_rows": None,
            "retryable": True,
        }
        assert codex.data["projects"]["rows"] == ()
    finally:
        cache.close()


@pytest.mark.parametrize("direct", [False, True])
def test_852_sqlite_fallback_fault_is_transient_not_a_skipped_row(
    tmp_path, monkeypatch, direct,
):
    ns = load_script()
    sources = sys.modules["_cctally_dashboard_sources"]
    analytics = sys.modules["_cctally_source_analytics"]
    cache = _seed_probe_store(ns, tmp_path, monkeypatch)
    try:
        _insert_entry(
            cache, source_path=HEALTHY_PATH, line_offset=8_103,
            timestamp=_iso(NOW - dt.timedelta(days=2)),
            conversation_key=HEALTHY_CONVERSATION, input_tokens="1-2",
        )
        cache.commit()

        def fail_fallback(*_args, **_kwargs):
            raise analytics.QualifiedMetadataUnavailable(
                "Codex accounting metadata is unavailable") from (
                    sqlite3.OperationalError("interrupted read"))

        monkeypatch.setattr(
            sources, "load_cached_rooted_codex_accounting_entries",
            fail_fallback)
        codex = _build_generation(ns, cache, direct=direct)
        assert codex.metadata_health == {
            "state": "transient_read_failure", "incomplete_rows": None,
            "retryable": True,
        }
        assert codex.data["projects"]["rows"] == ()
    finally:
        cache.close()


def test_845_a20_an_empty_rooted_identity_escapes_with_the_class_default(
    tmp_path, monkeypatch,
):
    """A20's second fixture. The schema permits an empty `source_root_key` and
    no ingester writes one. The qualified statement loads the entry because it
    has no root filter, `_require_joined_metadata` refuses it BEFORE the key
    check, the health aggregate has no reason that counts it, and the rooted
    fallback loader raises on it.

    §4.6 leaves that translation exactly as it is: the loader translates its
    `ValueError` without a `transient` argument, so the escaping exception
    carries the class default `True`. Nothing reads the attribute on that path,
    because the exception leaves the capture before any carrier is derived — so
    a later reclassification in either direction fails this row."""
    ns = load_script()
    analytics = sys.modules["_cctally_source_analytics"]
    cache = _seed_probe_store(ns, tmp_path, monkeypatch)
    try:
        cache.execute(
            "INSERT INTO codex_conversation_threads "
            "(conversation_key, source_root_key, native_thread_id, "
            "root_thread_id, source_path, cwd, git_json, first_seen_utc, "
            "last_seen_utc) VALUES (?,?,?,?,?,?,?,?,?)",
            ("rootless-conversation", "", "rootless-thread",
             "rootless-thread", "/synthetic/rootless.jsonl",
             "/synthetic/project-rootless", None,
             _iso(NOW - dt.timedelta(days=10)), _iso(NOW)),
        )
        _insert_entry(
            cache, source_path="/synthetic/rootless.jsonl", line_offset=8_200,
            timestamp=_iso(NOW - dt.timedelta(days=2)),
            conversation_key="rootless-conversation", source_root_key="",
        )
        cache.commit()

        health = analytics.load_codex_project_metadata_health(
            cache_conn=cache, start=NOW - dt.timedelta(days=30), end=NOW)
        assert health.incomplete_rows == 0, (
            "precondition: no reason in the health aggregate counts an empty "
            "rooted identity")

        with pytest.raises(analytics.QualifiedMetadataUnavailable) as raised:
            _build_generation(ns, cache)
        assert raised.value.transient is True
        cause = raised.value.__cause__
        assert isinstance(cause, ValueError)
        assert str(cause) == "rooted accounting identity is absent"
    finally:
        cache.close()


# === A6 — one agreement test over the ten fixtures ==========================
#
# A6 requires ONE test in which, for each of the first nine fixtures, the
# qualified reader's outcome, the counter's count and the identity map's item
# are asserted TOGETHER against a hard-coded expectation. The three are
# separate implementations of one rule — the reader refuses, the counter
# counts, the map withholds attribution — and a per-level test cannot show
# that they agree on the same fixture. The tenth fixture is the empty rooted
# identity of §4.6 rule 3, which is not a classification at all: the reader
# refuses before the key check, the counter counts nothing, and the rooted
# fallback loader raises, so the map's population cannot contain the entry.

#: A well-formed Codex git payload. `_git_resolved_key` accepts any non-empty
#: JSON object and the resolver labels every one of them `Git project`.
_A6_VALID_GIT = '{"repository_url":"https://example.invalid/a6.git"}'
_A6_DIRECT_CWD = "/synthetic/a6-project-direct"
_A6_INHERITED_CWD = "/synthetic/a6-project-inherited"
_A6_OWN_PATH = "/synthetic/a6/own.jsonl"
_A6_INHERITED_PATH = "/synthetic/a6/inherited.jsonl"
_A6_ROOT_PATH = "/synthetic/a6-root"

#: Every fixture's affected entry sits INSIDE the ~30-day accounting window,
#: so one window serves the reader, the counter and the map at once.
_A6_WINDOW_START = NOW - dt.timedelta(days=30)
_A6_ENTRY_AGE_DAYS = 5


def _a6_insert_thread(
    cache, *, conversation_key, native, source_path, cwd, git_json,
    source_root_key=ROOT_KEY,
):
    """Insert one thread row, writing undecodable bytes through a CAST.

    `sqlite3` refuses to BIND raw bytes into a TEXT column as text, so a value
    that is not valid UTF-8 is inserted as NULL and then written with
    `CAST(? AS TEXT)`, which is how #845's reproduction stores it.
    """
    cache.execute(
        "INSERT INTO codex_conversation_threads "
        "(conversation_key, source_root_key, native_thread_id, root_thread_id,"
        " source_path, cwd, git_json, first_seen_utc, last_seen_utc) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            conversation_key, source_root_key, native, native, source_path,
            None if isinstance(cwd, bytes) else cwd,
            None if isinstance(git_json, bytes) else git_json,
            _iso(NOW - dt.timedelta(days=120)), _iso(NOW),
        ),
    )
    for column, value in (("cwd", cwd), ("git_json", git_json)):
        if isinstance(value, bytes):
            cache.execute(
                f"UPDATE codex_conversation_threads SET {column} = "
                "CAST(? AS TEXT) WHERE conversation_key = ?",
                (value, conversation_key),
            )


#: The ten fixtures, each stated as the rows it seeds and the outcome all
#: three levels must produce. `reader` is either the literal `"refused"` or the
#: project label the reader must resolve; `count` is the accounting-window
#: population's undecodable count; `label` is the identity map's item label,
#: `None` when the item must carry no attribution at all.
_A6_FIXTURES = {
    # §4.1 E1 — the direct `cwd` is undecodable, so the resolver's first stop
    # is already unreadable. The valid `git_json` beside it is never consulted.
    "E1-direct-cwd": {
        "threads": (
            {"conversation_key": "e1", "native": "e1-native",
             "source_path": _A6_OWN_PATH, "cwd": BAD_BYTES,
             "git_json": _A6_VALID_GIT},
        ),
        "aliases": (),
        "entry_conversation_key": "e1",
        "reader": "refused", "count": 1, "label": None,
    },
    # §4.1 E2 — the direct `cwd` is absent, so the merge reaches the inherited
    # one, which is undecodable.
    "E2-inherited-cwd": {
        "threads": (
            {"conversation_key": "e2-inherited", "native": "e2-shared",
             "source_path": _A6_INHERITED_PATH, "cwd": BAD_BYTES,
             "git_json": None},
            {"conversation_key": "e2", "native": "e2-native",
             "source_path": _A6_OWN_PATH, "cwd": "", "git_json": ""},
        ),
        "aliases": ((_A6_OWN_PATH, "e2-shared"),),
        "entry_conversation_key": "e2",
        "reader": "refused", "count": 1, "label": None,
    },
    # §4.1 E3 — neither `cwd` is usable, so the resolver consults the direct
    # `git_json`, which is undecodable.
    "E3-direct-git-json": {
        "threads": (
            {"conversation_key": "e3", "native": "e3-native",
             "source_path": _A6_OWN_PATH, "cwd": None, "git_json": BAD_BYTES},
        ),
        "aliases": (),
        "entry_conversation_key": "e3",
        "reader": "refused", "count": 1, "label": None,
    },
    # §4.1 E4 — the last field on the path is the inherited `git_json`.
    "E4-inherited-git-json": {
        "threads": (
            {"conversation_key": "e4-inherited", "native": "e4-shared",
             "source_path": _A6_INHERITED_PATH, "cwd": None,
             "git_json": BAD_BYTES},
            {"conversation_key": "e4", "native": "e4-native",
             "source_path": _A6_OWN_PATH, "cwd": None, "git_json": None},
        ),
        "aliases": ((_A6_OWN_PATH, "e4-shared"),),
        "entry_conversation_key": "e4",
        "reader": "refused", "count": 1, "label": None,
    },
    # §4.1 counterexample 1 — a valid direct `cwd` beside an undecodable
    # direct `git_json`. The resolver stops at the `cwd` and never reaches the
    # corrupt field, so the entry is attributed. Revision 1's over-refusing
    # rule failed exactly here.
    "C1-valid-cwd-bad-git-json": {
        "threads": (
            {"conversation_key": "c1", "native": "c1-native",
             "source_path": _A6_OWN_PATH, "cwd": _A6_DIRECT_CWD,
             "git_json": BAD_BYTES},
        ),
        "aliases": (),
        "entry_conversation_key": "c1",
        "reader": "a6-project-direct", "count": 0,
        "label": "a6-project-direct",
    },
    # §4.1 counterexample 2 — both `cwd` values are empty and the direct
    # `git_json` is valid, so the entry is attributed through it and the
    # undecodable inherited `git_json` is never consulted.
    "C2-direct-git-json-wins": {
        "threads": (
            {"conversation_key": "c2-inherited", "native": "c2-shared",
             "source_path": _A6_INHERITED_PATH, "cwd": "",
             "git_json": BAD_BYTES},
            {"conversation_key": "c2", "native": "c2-native",
             "source_path": _A6_OWN_PATH, "cwd": "",
             "git_json": _A6_VALID_GIT},
        ),
        "aliases": ((_A6_OWN_PATH, "c2-shared"),),
        "entry_conversation_key": "c2",
        "reader": "Git project", "count": 0, "label": "Git project",
    },
    # §4.1 counterexample 3 — a valid inherited `cwd` beside an undecodable
    # inherited `git_json`. The merge reaches the `cwd` and stops.
    "C3-inherited-cwd-wins": {
        "threads": (
            {"conversation_key": "c3-inherited", "native": "c3-shared",
             "source_path": _A6_INHERITED_PATH, "cwd": _A6_INHERITED_CWD,
             "git_json": BAD_BYTES},
            {"conversation_key": "c3", "native": "c3-native",
             "source_path": _A6_OWN_PATH, "cwd": "", "git_json": ""},
        ),
        "aliases": ((_A6_OWN_PATH, "c3-shared"),),
        "entry_conversation_key": "c3",
        "reader": "a6-project-inherited", "count": 0,
        "label": "a6-project-inherited",
    },
    # §4.4 refusal 1 — an empty conversation key beside a valid alias winner.
    # The reader refuses before any metadata is consulted, so the map must
    # refuse too rather than attribute the entry through that winner.
    # Revision 8's map attributed it.
    "R1-empty-conversation-key": {
        "threads": (
            {"conversation_key": "r1-inherited", "native": "r1-shared",
             "source_path": _A6_INHERITED_PATH, "cwd": _A6_INHERITED_CWD,
             "git_json": None},
        ),
        "aliases": ((_A6_OWN_PATH, "r1-shared"),),
        "entry_conversation_key": "",
        "reader": "refused", "count": 0, "label": None,
    },
    # §4.4 refusal 2 — a nonempty key with neither a thread row nor an alias
    # winner. The reader refuses; the map must not publish `(unassigned)`.
    "R2-missing-thread-join": {
        "threads": (),
        "aliases": (),
        "entry_conversation_key": "r2-unjoined",
        "reader": "refused", "count": 0, "label": None,
    },
}

#: The tenth fixture. Not a classification: §4.6 rule 3 records it as a shape
#: that fails the provider outright before and after this session.
_A6_EMPTY_ROOT_CONVERSATION = "a6-rootless"


def _seed_a6_store(ns, tmp_path, monkeypatch, fixture):
    """One source root, the fixture's threads and aliases, and one entry."""
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "provider"))
    cache = ns["open_cache_db"]()
    cache.execute(
        "INSERT INTO codex_source_roots (source_root_key, canonical_root_path,"
        " first_seen_utc, last_seen_utc) VALUES (?,?,?,?)",
        (ROOT_KEY, _A6_ROOT_PATH, _iso(NOW - dt.timedelta(days=400)),
         _iso(NOW)),
    )
    for thread in fixture["threads"]:
        _a6_insert_thread(cache, **thread)
    for path, native in fixture["aliases"]:
        cache.execute(_ALIAS_FILE_SQL, (path, _iso(NOW), ROOT_KEY, native))
    _insert_entry(
        cache, source_path=_A6_OWN_PATH, line_offset=1,
        timestamp=_iso(NOW - dt.timedelta(days=_A6_ENTRY_AGE_DAYS)),
        conversation_key=fixture["entry_conversation_key"],
    )
    cache.commit()
    return cache


@pytest.mark.parametrize(
    "name", sorted(_A6_FIXTURES) + ["Z-empty-rooted-identity"])
def test_845_a6_the_reader_the_counter_and_the_identity_map_agree(
    tmp_path, monkeypatch, name,
):
    """A6. One rule, three implementations, ten fixtures.

    The qualified reader raises for a malformed entry, the counter counts it
    and the identity map withholds its attribution, and all three call
    `codex_metadata_is_malformed` with the SAME direct row and the SAME
    inherited winner. This test asserts the three outcomes together per
    fixture, which is the only way a disagreement between them is visible: a
    reader that refuses an entry the map attributes publishes a project row for
    a conversation the accounting read refused, and a counter that disagrees
    with either publishes a figure no reader produced.
    """
    ns = load_script()
    analytics = sys.modules["_cctally_source_analytics"]
    sources = sys.modules["_cctally_dashboard_sources"]

    if name == "Z-empty-rooted-identity":
        _a6_assert_empty_rooted_identity(
            ns, analytics, sources, tmp_path, monkeypatch)
        return

    fixture = _A6_FIXTURES[name]
    cache = _seed_a6_store(ns, tmp_path, monkeypatch, fixture)
    try:
        # 1. The reader.
        if fixture["reader"] == "refused":
            with pytest.raises(analytics.QualifiedMetadataUnavailable) as raised:
                analytics.load_qualified_codex_entries(
                    _A6_WINDOW_START, NOW, speed="standard", sync=False,
                    cache_conn=cache,
                )
            assert raised.value.transient is False, (
                "every refusal on this path is a property of the ROW, so the "
                "chip must never promise a retry for it")
            reader_project_key = None
        else:
            entries = analytics.load_qualified_codex_entries(
                _A6_WINDOW_START, NOW, speed="standard", sync=False,
                cache_conn=cache,
            )
            assert [entry.project_label for entry in entries] == [
                fixture["reader"]]
            reader_project_key = entries[0].project_key

        # 2. The counter, over both populations. No fixture carries an
        # unparseable timestamp or an unusable token operand, so the probe's
        # two extra reasons subtract nothing and the two populations agree.
        for include_probe_reasons in (False, True):
            assert analytics.count_undecodable_codex_metadata_rows(
                cache,
                bound_start=_iso(_A6_WINDOW_START),
                bound_end=_iso(NOW),
                include_probe_reasons=include_probe_reasons,
            ) == fixture["count"], include_probe_reasons

        # 3. The identity map, over the rooted fallback loader's own rows —
        # the map's real population in a partial generation.
        rooted = analytics.load_cached_rooted_codex_accounting_entries(
            _A6_WINDOW_START, NOW, speed="standard", cache_conn=cache)
        assert len(rooted) == 1
        identity_map = sources._codex_identity_map(cache, rooted)
        item = identity_map[sources._codex_accounting_identity(rooted[0])]
        assert item["project_label"] == fixture["label"]
        if fixture["label"] is None:
            assert item["project_key"] is None
        else:
            assert item["project_key"] is not None
        # The agreement itself: an attributed entry lands on the key the
        # reader resolved for the very same row.
        assert item["project_key"] == reader_project_key
    finally:
        cache.close()


def _a6_assert_empty_rooted_identity(
    ns, analytics, sources, tmp_path, monkeypatch,
):
    """The tenth fixture: an entry and its thread share an empty root.

    The schema permits an empty `source_root_key` and no ingester writes one.
    The qualified statement loads the entry because it has no root filter,
    `_require_joined_metadata` refuses it BEFORE the key check, no reason in
    the accounting aggregate counts it, and the rooted fallback loader raises
    on it — so the identity map, whose population is that loader's rows, can
    hold no item for it at all.
    """
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "provider"))
    cache = ns["open_cache_db"]()
    try:
        cache.execute(
            "INSERT INTO codex_source_roots (source_root_key,"
            " canonical_root_path, first_seen_utc, last_seen_utc)"
            " VALUES (?,?,?,?)",
            (ROOT_KEY, _A6_ROOT_PATH, _iso(NOW - dt.timedelta(days=400)),
             _iso(NOW)),
        )
        _a6_insert_thread(
            cache, conversation_key=_A6_EMPTY_ROOT_CONVERSATION,
            native="a6-rootless-thread", source_path=_A6_OWN_PATH,
            cwd=_A6_DIRECT_CWD, git_json=None, source_root_key="",
        )
        _insert_entry(
            cache, source_path=_A6_OWN_PATH, line_offset=1,
            timestamp=_iso(NOW - dt.timedelta(days=_A6_ENTRY_AGE_DAYS)),
            conversation_key=_A6_EMPTY_ROOT_CONVERSATION,
            source_root_key="",
        )
        cache.commit()

        with pytest.raises(analytics.QualifiedMetadataUnavailable) as reader:
            analytics.load_qualified_codex_entries(
                _A6_WINDOW_START, NOW, speed="standard", sync=False,
                cache_conn=cache,
            )
        assert reader.value.transient is False

        assert analytics.count_undecodable_codex_metadata_rows(
            cache, bound_start=_iso(_A6_WINDOW_START), bound_end=_iso(NOW),
            include_probe_reasons=False,
        ) == 0

        with pytest.raises(analytics.QualifiedMetadataUnavailable) as loader:
            analytics.load_cached_rooted_codex_accounting_entries(
                _A6_WINDOW_START, NOW, speed="standard", cache_conn=cache)
        # §4.6 leaves this translation exactly as it is: the loader passes no
        # `transient` argument, so the escaping exception carries the class
        # default. Nothing reads the attribute on that path, and a later
        # reclassification in either direction fails this assertion.
        assert loader.value.transient is True
        cause = loader.value.__cause__
        assert isinstance(cause, ValueError)
        assert str(cause) == "rooted accounting identity is absent"

        # The loader admits no such entry, so no population exists that could
        # carry an item for it.
        assert sources._codex_identity_map(cache, ()) == {}
    finally:
        cache.close()
