"""#834 S2 (#828) — the bounded year probe matches the qualifier.

Plan: ``docs/superpowers/plans/2026-09-13-834-s2-source-recovery-read-model.md``.

`_CODEX_PROJECT_METADATA_HEALTH_SQL` counts a missing conversation key and a
missing thread join. The qualified reader rejects more than that: it parses the
timestamp and the integer token fields and raises `QualifiedMetadataUnavailable`
on either. A row with intact joins and a malformed timestamp was therefore
counted HEALTHY by that SQL and rejected by the real reader.

The second half is the horizon. The generation's accounting window is roughly
thirty calendar days, while both detail routes read a YEAR, so a malformed row
aged thirty-one to three hundred and sixty-five days set no flag at all and the
page rendered in full with that row's cost silently absent from the totals.

Every row seeded here is aged between thirty-one and three hundred and
sixty-five days for exactly that reason: a row inside thirty days would be
caught by the accounting-window health read and would prove nothing about the
horizon.
"""
from __future__ import annotations

import datetime as dt
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


def _build_generation(ns, cache):
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
            codex_budget=semantics.codex_budget,
        )
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

    # The route says "will retry", never "rebuild your cache".
    dashboard = sys.modules["_cctally_dashboard"]
    snap = _snapshot(ns, codex)
    assert dashboard._codex_generation_metadata_state(snap) == (
        "transient_read_failure")
    rows = codex.data["projects"]["rows"]
    assert rows, "the degraded generation published no project row"
    detail = dashboard.build_source_detail(
        snapshot=snap, source="codex", resource="project",
        key=str(rows[0]["key"]),
    )
    assert detail["metadata_availability"] == "partial"
    assert detail["metadata_reason"] == (
        "This build could not check project metadata health; it will retry on "
        "the next refresh."
    )

    # The degraded generation is never handed back unexamined (#830) ...
    import _lib_dashboard_sources as lds
    assert lds.reuse_coherent_source_state(
        codex, data_version=codex.data_version) is None

    # ... and the next healthy publish replaces the state and clears it.
    recovered = _build_generation(ns, cache)
    assert recovered.metadata_health["state"] == "healthy"
    recovered_snap = _snapshot(ns, recovered)
    recovered_detail = dashboard.build_source_detail(
        snapshot=recovered_snap, source="codex", resource="project",
        key=str(recovered.data["projects"]["rows"][0]["key"]),
    )
    assert recovered_detail["metadata_availability"] is None
    assert recovered_detail["metadata_reason"] is None
