"""#825/#832 Task 1.1 — the pure account-bound protocol kernel.

``bin/_lib_account_protocol.py`` owns the writer-generation stamp, the
historical boundary of the ``account_bound_cutover`` op and the admission
predicate of spec section 5.3/5.4. Every row of the admission table is pinned
here against an UNSTAMPED post-boundary record, because exclusion is the
default and an admitted family must be proven Codex-only by an explicit field
its real producer writes.
"""
import pytest

import types

import _lib_account_protocol as P
import _lib_journal as J


def _unstamped_builder(fn):
    """The journal builders stamp every current record; the admission table is
    about UNSTAMPED post-boundary records, so the cases build the previous
    release's shape by dropping the stamp."""
    def build(*args, **kwargs):
        return {k: v for k, v in fn(*args, **kwargs).items() if k != "wpg"}
    return build


LEGACY = types.SimpleNamespace(**{
    name: _unstamped_builder(getattr(J, name))
    for name in ("make_obs", "make_op", "make_evt", "make_account_observe",
                 "make_account_label")
})


BOUNDARY = P.CutoverBoundary(
    segment="observations-2026-10.jsonl",
    end_offset=900,
    frozen_extents={
        "observations-2026-09.jsonl": 5000,
        "observations-2026-10.jsonl": 800,
    },
    legacy_spool_baseline_digest="sha256:" + "0" * 64,
)
POST = P.Coordinate("observations-2026-10.jsonl", 950)
PRE = P.Coordinate("observations-2026-10.jsonl", 10)


def _op(kind, **payload):
    return LEGACY.make_op(at="2026-10-09T00:00:00Z", src="test",
                     payload={"kind": kind, **payload})


def _evt(kind, **payload):
    return LEGACY.make_evt(kind=kind, id=f"{kind}:1", at="2026-10-09T00:00:00Z",
                      payload=payload)


def _obs(provider, **payload):
    return LEGACY.make_obs(at="2026-10-09T00:00:00Z", src="statusline",
                      provider=provider, payload=payload)


# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------

def test_protocol_constants():
    assert P.PROTOCOL_GENERATION == "account-bound-v1"
    assert P.WRITER_GENERATION_KEY == "wpg"
    assert P.CUTOVER_OP_KIND == "account_bound_cutover"
    assert P.CUTOVER_OP_ID == "account-bound-cutover-v1"
    assert P.LEGACY_SPOOL_ACTIVITY_KIND == "legacy_spool_activity"


# --------------------------------------------------------------------------
# stamp / is_stamped
# --------------------------------------------------------------------------

def test_stamp_is_idempotent_and_never_touches_payload():
    rec = _obs("claude", seven_day={"used_percentage": 10})
    payload_before = dict(rec["payload"])
    once = P.stamp(rec)
    twice = P.stamp(once)
    assert once["wpg"] == "account-bound-v1"
    assert once == twice
    assert once["payload"] == payload_before
    assert "wpg" not in once["payload"]
    # The caller's record is not mutated.
    assert "wpg" not in rec
    assert P.is_stamped(once)
    assert not P.is_stamped(rec)


def test_stamp_refuses_a_different_generation():
    rec = dict(_obs("claude"), wpg="account-bound-v0")
    with pytest.raises(ValueError):
        P.stamp(rec)
    assert not P.is_stamped(rec)


# --------------------------------------------------------------------------
# is_historical
# --------------------------------------------------------------------------

def test_is_historical_inside_frozen_extent():
    assert P.is_historical(P.Coordinate("observations-2026-09.jsonl", 0), BOUNDARY)
    assert P.is_historical(P.Coordinate("observations-2026-09.jsonl", 4999), BOUNDARY)
    assert P.is_historical(PRE, BOUNDARY)


def test_is_historical_false_at_or_past_extent():
    assert not P.is_historical(P.Coordinate("observations-2026-09.jsonl", 5000), BOUNDARY)
    assert not P.is_historical(P.Coordinate("observations-2026-10.jsonl", 800), BOUNDARY)
    assert not P.is_historical(POST, BOUNDARY)


def test_is_historical_false_for_segment_absent_from_extents():
    # A bootstrap segment created after the cutover sorts FIRST in canonical
    # order (segment_sort_key) but is still post-boundary.
    late_bootstrap = "bootstrap-20261010T000000Z.jsonl"
    assert J.segment_sort_key(late_bootstrap) < J.segment_sort_key(
        "observations-2026-09.jsonl")
    assert not P.is_historical(P.Coordinate(late_bootstrap, 0), BOUNDARY)
    assert not P.is_historical(
        P.Coordinate("observations-2026-11.jsonl", 0), BOUNDARY)


# --------------------------------------------------------------------------
# classify_admission — one case per admission-table row (spec 5.4)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("src", ["statusline", "hook-tick", "refresh-usage",
                                 "record-usage"])
def test_claude_observation_excluded(src):
    rec = LEGACY.make_obs(at="t", src=src, provider="claude",
                     payload={"seven_day": {"used_percentage": 1}})
    adm = P.classify_admission(rec, POST, BOUNDARY)
    assert (adm.admitted, adm.family, adm.reason) == (
        False, "claude-observation", "excluded")


def test_codex_quota_observation_admitted():
    rec = LEGACY.make_obs(at="t", src="codex-quota", provider="codex",
                     payload={"kind": "quota_window_snapshot", "source": "codex"})
    adm = P.classify_admission(rec, POST, BOUNDARY)
    assert (adm.admitted, adm.family, adm.reason) == (True, "codex", "codex-only")


def test_codex_observation_of_unknown_kind_excluded():
    rec = _obs("codex", kind="something_new")
    adm = P.classify_admission(rec, POST, BOUNDARY)
    assert not adm.admitted
    assert adm.family == "unknown"


def test_observation_without_provider_excluded():
    rec = {"v": 1, "t": "obs", "at": "t", "src": "statusline", "payload": {}}
    assert not P.classify_admission(rec, POST, BOUNDARY).admitted


def test_account_observe_codex_admitted():
    rec = LEGACY.make_account_observe(at="t", account_key="k" * 16, provider="codex")
    adm = P.classify_admission(rec, POST, BOUNDARY)
    assert (adm.admitted, adm.reason) == (True, "codex-only")


@pytest.mark.parametrize("provider", ["claude", None])
def test_account_observe_claude_or_missing_excluded(provider):
    rec = LEGACY.make_account_observe(at="t", account_key="k" * 16, provider="claude")
    if provider is None:
        del rec["payload"]["provider"]
    adm = P.classify_admission(rec, POST, BOUNDARY)
    assert (adm.admitted, adm.family) == (False, "claude-op")


def test_account_label_codex_admitted():
    rec = LEGACY.make_account_label(at="t", account_key="k" * 16, label="x",
                               provider="codex")
    assert P.classify_admission(rec, POST, BOUNDARY).admitted


@pytest.mark.parametrize("provider", ["claude", None])
def test_account_label_otherwise_excluded(provider):
    rec = LEGACY.make_account_label(at="t", account_key="k" * 16, label="x",
                               provider=provider)
    adm = P.classify_admission(rec, POST, BOUNDARY)
    assert (adm.admitted, adm.family) == (False, "claude-op")


@pytest.mark.parametrize("kind", ["accounts_cutover", "weekly_credit_floor",
                                  "sync_week"])
def test_claude_ops_excluded(kind):
    adm = P.classify_admission(_op(kind), POST, BOUNDARY)
    assert (adm.admitted, adm.family, adm.reason) == (
        False, "claude-op", "excluded")


@pytest.mark.parametrize("kind", ["codex_file_account", "codex_window_attribution",
                                  "codex_window_attribution_retract"])
def test_codex_ops_admitted(kind):
    adm = P.classify_admission(_op(kind), POST, BOUNDARY)
    assert (adm.admitted, adm.family, adm.reason) == (True, "codex", "codex-only")


@pytest.mark.parametrize("record", [
    {"v": 1, "t": "correction", "id": "c:1", "payload": {}},
    {"v": 1, "t": "correction_batch", "id": "cb:1", "payload": {}},
])
def test_correction_records_excluded(record):
    adm = P.classify_admission(record, POST, BOUNDARY)
    assert (adm.admitted, adm.family) == (False, "administrative")


@pytest.mark.parametrize("kind", [
    "journal_protocol_resolution", "claude_weekly_observation_decision",
    "journal_protocol_resolution_v2", "claude_weekly_observation_decision_v2",
])
def test_administrative_ops_excluded(kind):
    adm = P.classify_admission(_op(kind), POST, BOUNDARY)
    assert (adm.admitted, adm.family) == (False, "administrative")


@pytest.mark.parametrize("kind", [
    "snapshot_accept", "weekly_cost_snapshot", "week_reset", "five_hour_credit",
    "five_hour_block_close", "percent_milestone", "five_hour_milestone",
    "weekly_credit_effects", "projected", "project_budget", "meter_rate_change",
])
def test_claude_events_excluded(kind):
    adm = P.classify_admission(_evt(kind), POST, BOUNDARY)
    assert (adm.admitted, adm.family, adm.reason) == (
        False, "claude-event", "excluded")


def test_budget_event_codex_admitted():
    adm = P.classify_admission(_evt("budget", vendor="codex"), POST, BOUNDARY)
    assert (adm.admitted, adm.family) == (True, "codex")


@pytest.mark.parametrize("payload", [{}, {"vendor": "claude"}])
def test_budget_event_otherwise_excluded(payload):
    adm = P.classify_admission(_evt("budget", **payload), POST, BOUNDARY)
    assert (adm.admitted, adm.family) == (False, "claude-event")


@pytest.mark.parametrize("kind", ["quota_alert_arming", "quota_threshold_event"])
def test_quota_events_with_codex_source_admitted(kind):
    adm = P.classify_admission(_evt(kind, source="codex"), POST, BOUNDARY)
    assert (adm.admitted, adm.family) == (True, "codex")


@pytest.mark.parametrize("kind", ["quota_alert_arming", "quota_threshold_event"])
@pytest.mark.parametrize("payload", [{}, {"source": "claude"}])
def test_quota_events_otherwise_excluded(kind, payload):
    adm = P.classify_admission(_evt(kind, **payload), POST, BOUNDARY)
    assert not adm.admitted


@pytest.mark.parametrize("kind", ["account_bound_cutover", "legacy_spool_activity"])
def test_unstamped_protocol_ops_after_boundary_excluded(kind):
    adm = P.classify_admission(_op(kind), POST, BOUNDARY)
    assert not adm.admitted
    assert adm.reason == "excluded"


@pytest.mark.parametrize("record", [
    {"v": 1, "t": "mystery", "payload": {}},
    {"v": 1, "t": "op", "at": "t", "src": "x", "payload": {"kind": "brand_new"}},
    {"v": 1, "t": "evt", "id": "x", "payload": {"kind": "brand_new"}},
    {"v": 1, "t": "op", "at": "t", "src": "x", "payload": "not-a-dict"},
])
def test_unknown_records_excluded(record):
    adm = P.classify_admission(record, POST, BOUNDARY)
    assert (adm.admitted, adm.family, adm.reason) == (False, "unknown", "excluded")


@pytest.mark.parametrize("record", [
    _obs("claude"), _op("sync_week"), _evt("week_reset"),
    {"v": 1, "t": "correction", "id": "c:1", "payload": {}},
    {"v": 1, "t": "mystery", "payload": {}},
])
def test_any_stamped_record_admitted(record):
    adm = P.classify_admission(P.stamp(record), POST, BOUNDARY)
    assert (adm.admitted, adm.reason) == (True, "stamped")


def test_any_historical_record_admitted():
    adm = P.classify_admission(_obs("claude"), PRE, BOUNDARY)
    assert (adm.admitted, adm.family, adm.reason) == (
        True, "claude-observation", "historical")


def test_no_cutover_admits_everything():
    adm = P.classify_admission({"v": 1, "t": "mystery"}, POST, None)
    assert (adm.admitted, adm.reason) == (True, "no-cutover")


# --------------------------------------------------------------------------
# boundary payload round trip
# --------------------------------------------------------------------------

def test_boundary_payload_round_trip():
    payload = P.boundary_payload(BOUNDARY)
    assert set(payload) == {"segment", "endOffset", "frozenExtents",
                            "legacySpoolBaselineDigest"}
    op = P.stamp(LEGACY.make_op(at="t", src="account-bound-cutover",
                           payload={"kind": P.CUTOVER_OP_KIND, **payload}))
    assert P.boundary_from_op(op) == BOUNDARY


def test_boundary_from_op_rejects_malformed():
    with pytest.raises(ValueError):
        P.boundary_from_op(_op("sync_week"))
    bad = _op(P.CUTOVER_OP_KIND, segment="s", endOffset=-1, frozenExtents={},
              legacySpoolBaselineDigest="sha256:" + "0" * 64)
    with pytest.raises(ValueError):
        P.boundary_from_op(bad)


def test_empty_boundary_makes_everything_post_boundary():
    empty = P.CutoverBoundary(segment="observations-2026-10.jsonl",
                              end_offset=120, frozen_extents={},
                              legacy_spool_baseline_digest="sha256:" + "1" * 64)
    assert not P.is_historical(P.Coordinate("observations-2026-10.jsonl", 0), empty)
    assert not P.classify_admission(_obs("claude"),
                                    P.Coordinate("observations-2026-10.jsonl", 0),
                                    empty).admitted
