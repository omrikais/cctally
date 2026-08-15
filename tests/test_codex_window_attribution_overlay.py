"""The fold-time overlay and both axes (#500 Task 2).

Spec: ``docs/superpowers/specs/2026-08-14-500-codex-window-attribution-design.md``
§6.4, §6.4.1, §6.5, §7, §7.1, §8.2.

``load_codex_quota_observations`` applies the operator's active assertions
immediately before the continuity fold, which moves the percentage axis through
``build_blocks`` and the spend axis through the existing adoption kernel from one
insertion point. Nothing is written back to ``quota_window_snapshots``.

The fixtures here are deliberately dense. ``docs/codex-gotchas.md`` records that
the first spend-adoption implementation passed 1,870 tests while stamping zero
rows in production, and the cause was a fixture too thin to tell working from not
working. Every group below carries several jittered raw resets, and the store
carries unattributed neighbours, a natively-identified group, a Spark pool and a
5-hour window that must all stay untouched.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3

import pytest

from conftest import load_script, redirect_paths

UTC = dt.timezone.utc

# Synthetic throughout. A real source_root_key or Codex account_key (a salted
# hash of account id + email) in a test file would be confirmable against the
# operator's own store.
ROOT = "rk"
ACCT_A = "a" * 32
ACCT_B = "b" * 32
ACCT_C = "c" * 32
UNATTRIBUTED = "unattributed"

WEEK = 10_080
FIVE_HOUR = 300


def _key(*, minutes=WEEK, pool=None, root=ROOT):
    payload = {
        "limitId": "codex", "observedSlot": "primary", "source": "codex",
        "sourceRootKey": root, "windowMinutes": minutes,
    }
    if pool is not None:
        payload["modelPool"] = pool
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


WEEK_KEY = _key()
FIVE_HOUR_KEY = _key(minutes=FIVE_HOUR)
SPARK_KEY = _key(pool="gpt-5.3-codex-spark")

MODEL = "gpt-5"

# The target group. Four physical captures whose raw resets are jittered inside
# the anchor tolerance and whose stored canonical anchor is the earliest of them
# — the shape a component that a later observation BRIDGED actually has.
TARGET_RESET = dt.datetime(2026, 8, 5, 4, 35, 6, tzinfo=UTC)
TARGET_RAWS = (
    TARGET_RESET,
    TARGET_RESET + dt.timedelta(seconds=6),
    TARGET_RESET + dt.timedelta(seconds=11),
    TARGET_RESET + dt.timedelta(seconds=17),
)
TARGET_START = TARGET_RESET - dt.timedelta(minutes=WEEK)

# Neighbouring weekly windows on the same root, all unattributed, so the
# adoption pass has to discriminate rather than stamp everything it scans.
NEIGHBOUR_RESETS = tuple(
    TARGET_RESET - dt.timedelta(days=7 * n) for n in range(1, 5)
)
# One weekly window that already carries a real account natively.
NATIVE_RESET = TARGET_RESET + dt.timedelta(days=7)
# The Spark pool and the 5-hour window, neither of which is account weekly quota.
SPARK_RESET = TARGET_RESET + dt.timedelta(days=14)
FIVE_HOUR_RESET = TARGET_RESET + dt.timedelta(hours=3)


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


def _z(value) -> str:
    """Render an instant, passing an already-rendered value through unchanged.

    The passthrough is what lets a test seed a row the loader REFUSES — a blank
    or unparseable ``captured_at_utc`` — without a second insert helper.
    """
    if isinstance(value, str):
        return value
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _seed_snapshot(cache, *, root=ROOT, key=WEEK_KEY, window=WEEK, captured,
                   reset, anchor=None, account_key=None, source_path,
                   line_offset, limit_name=None, observed_model=None,
                   used_percent=1.0, slot="primary"):
    cache.execute(
        "INSERT INTO quota_window_snapshots "
        "(source, source_root_key, source_path, line_offset, captured_at_utc,"
        " observed_slot, logical_limit_key, limit_id, limit_name,"
        " window_minutes, used_percent, resets_at_utc, observed_model,"
        " account_key, canonical_resets_at_utc) "
        "VALUES ('codex',?,?,?,?,?,?,'codex',?,?,?,?,?,?,?)",
        (root, source_path, int(line_offset), _z(captured), slot, key, limit_name,
         window, used_percent, _z(reset), observed_model, account_key,
         _z(anchor if anchor is not None else reset)),
    )


def _seed_entry(cache, *, root=ROOT, timestamp, account_key, source_path,
                line_offset, total_tokens=1_000, session_id="s"):
    cache.execute(
        "INSERT INTO codex_session_entries "
        "(source_path, line_offset, timestamp_utc, session_id, model,"
        " input_tokens, cached_input_tokens, output_tokens,"
        " reasoning_output_tokens, total_tokens, source_root_key,"
        " conversation_key, account_key) "
        "VALUES (?,?,?,?,?,?,0,0,0,?,?,NULL,?)",
        (source_path, int(line_offset), _z(timestamp), session_id, MODEL,
         int(total_tokens), int(total_tokens), root, account_key),
    )


def _seed_group(cache, *, reset, raws=None, account_key=None, key=WEEK_KEY,
                window=WEEK, limit_name=None, observed_model=None,
                path_tag, root=ROOT):
    """One physical window group with several jittered raw captures.

    Every member stores the SAME canonical anchor, which is what makes the
    group one tolerance-connected component regardless of which raw spelling a
    bounded read happens to retain.
    """
    raws = raws or (reset, reset + dt.timedelta(seconds=7))
    for index, raw in enumerate(raws):
        _seed_snapshot(
            cache, root=root, key=key, window=window,
            captured=reset - dt.timedelta(days=6, hours=index),
            reset=raw, anchor=reset, account_key=account_key,
            source_path=f"/s/{path_tag}.jsonl", line_offset=100 + index,
            limit_name=limit_name, observed_model=observed_model,
            used_percent=float(index + 1),
        )


def _seed_store(cache):
    """The dense store every test in this module reads."""
    _seed_group(cache, reset=TARGET_RESET, raws=TARGET_RAWS, path_tag="target")
    for index, reset in enumerate(NEIGHBOUR_RESETS):
        _seed_group(cache, reset=reset, path_tag=f"neighbour{index}")
    _seed_group(cache, reset=NATIVE_RESET, account_key=ACCT_C,
                path_tag="native")
    # One UNATTRIBUTED member inside the natively-identified group. Without it
    # the suppression rule cannot be observed AT ALL: `apply_resolution` never
    # re-stamps an already-identified observation, so a group whose every member
    # already carries the native account reaches the same answer with the
    # SUPPRESSED_NATIVE branch deleted. With it, dropping that branch stamps
    # this row with the asserted account and the group renders as two accounts.
    _seed_snapshot(
        cache, captured=NATIVE_RESET - dt.timedelta(days=5),
        reset=NATIVE_RESET + dt.timedelta(seconds=13), anchor=NATIVE_RESET,
        account_key=None, source_path="/s/native.jsonl", line_offset=150,
        used_percent=4.0)
    _seed_group(cache, reset=SPARK_RESET, key=SPARK_KEY,
                limit_name="gpt-5.3-codex-spark",
                observed_model="gpt-5.3-codex-spark", path_tag="spark")
    _seed_group(cache, reset=FIVE_HOUR_RESET, key=FIVE_HOUR_KEY,
                window=FIVE_HOUR, path_tag="fivehour")
    cache.execute(
        "INSERT INTO codex_source_roots "
        "(source_root_key, canonical_root_path, first_seen_utc, last_seen_utc) "
        "VALUES (?,?,?,?)",
        (ROOT, "/codex", _z(TARGET_START), _z(TARGET_RESET)),
    )
    cache.commit()


def _seed_spend(cache, *, count=6, root=ROOT):
    """Nonzero spend inside the target window, plus decoys outside it."""
    for index in range(count):
        _seed_entry(
            cache, root=root,
            timestamp=TARGET_START + dt.timedelta(hours=index + 1),
            account_key=None, source_path="/s/rollout.jsonl",
            line_offset=200 + index, total_tokens=250_000)
    # Inside a neighbour window nobody identifies — must stay unattributed.
    _seed_entry(cache, root=root,
                timestamp=NEIGHBOUR_RESETS[0] - dt.timedelta(hours=2),
                account_key=None, source_path="/s/rollout.jsonl",
                line_offset=900, total_tokens=1_000)
    # Inside the natively-identified window — its own account, untouched.
    _seed_entry(cache, root=root,
                timestamp=NATIVE_RESET - dt.timedelta(hours=2),
                account_key=ACCT_C, source_path="/s/rollout.jsonl",
                line_offset=910, total_tokens=1_000)
    cache.commit()


def _assert_witnesses(cache, *, account_key=ACCT_A, witnesses, key=WEEK_KEY,
                      canonical, root=ROOT, op_id="o:test",
                      at="2026-08-14T00:00:00.000000Z", slot="primary",
                      minutes=WEEK):
    cache.execute(
        "INSERT INTO codex_window_attributions "
        "(op_id, account_key, source_root_key, logical_limit_key,"
        " observed_slot, window_minutes, raw_resets_at_utc,"
        " canonical_resets_at_utc, asserted_at_utc, retracted_by_op_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,NULL)",
        (op_id, account_key, root, key, slot, minutes,
         json.dumps(sorted(_z(w) for w in witnesses),
                    separators=(",", ":"), sort_keys=True),
         _z(canonical), at),
    )
    cache.commit()


def _accounts_for(observations, reset):
    return {
        o.identity.account_key for o in observations
        if o.canonical_resets_at == reset
    }


def _entry_accounts(cache):
    return {
        int(row[0]): row[1] for row in cache.execute(
            "SELECT line_offset, account_key FROM codex_session_entries "
            "ORDER BY line_offset")
    }


# ── the overlay ──────────────────────────────────────────────────────────────

def test_an_active_assertion_stamps_its_group_at_load_time(ns):
    import _cctally_quota as quota

    cache = ns["open_cache_db"]()
    _seed_store(cache)
    _assert_witnesses(cache, witnesses=(TARGET_RAWS[3],),
                      canonical=TARGET_RESET)

    observations = quota.load_codex_quota_observations(cache_conn=cache)
    assert _accounts_for(observations, TARGET_RESET) == {ACCT_A}
    # Every other window is untouched.
    for reset in NEIGHBOUR_RESETS:
        assert _accounts_for(observations, reset) == {UNATTRIBUTED}
    assert _accounts_for(observations, NATIVE_RESET) == {ACCT_C}
    assert _accounts_for(observations, SPARK_RESET) == {UNATTRIBUTED}
    assert _accounts_for(observations, FIVE_HOUR_RESET) == {UNATTRIBUTED}


def test_no_assertion_leaves_the_load_byte_identical(ns):
    """The overlay costs a store with no assertions exactly one indexed read of
    an empty table, and changes nothing."""
    import _cctally_quota as quota

    cache = ns["open_cache_db"]()
    _seed_store(cache)
    observations = quota.load_codex_quota_observations(cache_conn=cache)
    assert _accounts_for(observations, TARGET_RESET) == {UNATTRIBUTED}


def test_native_evidence_suppresses_the_assertion(ns):
    """The native group carries one identified and one unattributed member, and
    that mix is what makes the assertion DISCRIMINATING.

    Delete the SUPPRESSED_NATIVE branch and the assertion stamps the
    unattributed member with ``ACCT_A``; the continuity fold then sees two
    identified accounts for one physical window, never-combine holds, and the
    group renders as ``{ACCT_A, ACCT_C}``."""
    import _cctally_quota as quota

    cache = ns["open_cache_db"]()
    _seed_store(cache)
    _assert_witnesses(cache, account_key=ACCT_A, witnesses=(NATIVE_RESET,),
                      canonical=NATIVE_RESET)

    observations = quota.load_codex_quota_observations(cache_conn=cache)
    assert _accounts_for(observations, NATIVE_RESET) == {ACCT_C}


class _RowCountingConnection:
    """A ``conn`` proxy counting the ``quota_window_snapshots`` rows read.

    The evidence pass's cost is the rows it interprets, not the statements it
    issues, so a work bound is the honest assertion — and it is independent of
    whichever plan SQLite picks for a fixture this small.
    """

    def __init__(self, conn):
        self._conn = conn
        self.rows_read = 0

    def execute(self, sql, params=()):
        cursor = self._conn.execute(sql, params)
        if ("quota_window_snapshots" in sql
                and not sql.lstrip().upper().startswith("PRAGMA")):
            rows = cursor.fetchall()
            self.rows_read += len(rows)
            return rows
        return cursor

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_the_evidence_pass_never_reads_a_foreign_axis_combination(ns):
    """Both evidence passes are sharded on the assertion's OWN four axes.

    A pass constrained only by root (pass 2) or by root and length (pass 1)
    leaves the interior members of ``idx_qws_physical_group`` unconstrained,
    which SQLite cannot skip — so it reads every row of every other group
    sharing the anchor and pays for the whole root's history per assertion.
    Measured read-only on the maintainer's store at 63 assertions: 4,443 ms for
    one call, of which 3,610 ms is pass 2 at 63 ms per group."""
    import _cctally_quota as quota

    cache = ns["open_cache_db"]()
    _seed_store(cache)
    # A decoy group on the OTHER slot, sharing the target's canonical anchor and
    # dense enough that reading it is unmistakable.
    for index in range(200):
        _seed_snapshot(
            cache, key=_key(), slot="secondary",
            captured=TARGET_RESET - dt.timedelta(days=6, minutes=index),
            reset=TARGET_RAWS[index % len(TARGET_RAWS)], anchor=TARGET_RESET,
            source_path="/s/decoy.jsonl", line_offset=5000 + index,
            used_percent=2.0)
    cache.commit()

    assertions = [{
        "op_id": "o:test", "account_key": ACCT_A, "source_root_key": ROOT,
        "logical_limit_key": WEEK_KEY, "observed_slot": "primary",
        "window_minutes": WEEK, "raw_resets_at_utc": (_z(TARGET_RAWS[3]),),
    }]
    proxy = _RowCountingConnection(cache)
    groups = quota._load_codex_window_group_evidence(proxy, assertions)

    assert {group.observed_slot for group in groups} == {"primary"}
    assert proxy.rows_read < 50, (
        "the 200 foreign-axis rows sharing the anchor must never be read; "
        f"read {proxy.rows_read}")


def test_a_row_the_loader_drops_cannot_supply_native_evidence(ns):
    """The evidence pass's validity predicate must equal the loader's.

    A row the loader drops still reached ``bucket['accounts']`` here, so a
    group the LOADED population sees as cleanly unattributed resolved
    SUPPRESSED_NATIVE and the attribution silently under-applied."""
    import _cctally_quota as quota

    cache = ns["open_cache_db"]()
    _seed_store(cache)
    # Blank `captured_at_utc` — the loader's required-text guard drops it, so it
    # never becomes an observation and never names an account.
    _seed_snapshot(
        cache, captured="", reset=TARGET_RAWS[0], anchor=TARGET_RESET,
        account_key=ACCT_C, source_path="/s/target.jsonl", line_offset=180,
        used_percent=3.0)
    cache.commit()
    _assert_witnesses(cache, witnesses=(TARGET_RAWS[0],),
                      canonical=TARGET_RESET)

    observations = quota.load_codex_quota_observations(cache_conn=cache)
    assert _accounts_for(observations, TARGET_RESET) == {ACCT_A}


def test_an_unparseable_capture_cannot_supply_native_evidence(ns):
    """The same rule for the second half of the loader's predicate. Present but
    unreadable as a time is a separate class from blank, and the loader drops it
    at ``_parse_utc`` rather than at the required-text guard.

    (``used_percent`` is deliberately NOT tested the same way: the column
    carries a ``CHECK (used_percent >= 0 AND used_percent <= 100)``, so a
    non-numeric value cannot be inserted and a test naming that state would
    assert nothing.)"""
    import _cctally_quota as quota

    cache = ns["open_cache_db"]()
    _seed_store(cache)
    _seed_snapshot(
        cache, captured="not-a-timestamp",
        reset=TARGET_RAWS[0], anchor=TARGET_RESET, account_key=ACCT_C,
        source_path="/s/target.jsonl", line_offset=181,
        used_percent=3.0)
    cache.commit()
    _assert_witnesses(cache, witnesses=(TARGET_RAWS[0],),
                      canonical=TARGET_RESET)

    observations = quota.load_codex_quota_observations(cache_conn=cache)
    assert _accounts_for(observations, TARGET_RESET) == {ACCT_A}


def test_a_spark_pool_is_never_filed_as_account_weekly_quota(ns):
    """§6.5/#373. The assertion names the ORDINARY weekly axes, and the group
    has since been re-materialized carrying a Spark ``limit_name`` — the case
    plan-time classification cannot settle, because ``limit_name`` sits outside
    identity equality."""
    import _cctally_quota as quota

    cache = ns["open_cache_db"]()
    _seed_store(cache)
    drifted = TARGET_RESET + dt.timedelta(days=21)
    _seed_group(cache, reset=drifted, key=WEEK_KEY,
                limit_name="gpt-5.3-codex-spark", path_tag="drifted")
    cache.commit()
    _assert_witnesses(cache, witnesses=(drifted,), canonical=drifted)

    observations = quota.load_codex_quota_observations(cache_conn=cache)
    assert _accounts_for(observations, drifted) == {UNATTRIBUTED}


def test_two_assertions_naming_different_accounts_apply_nothing(ns):
    import _cctally_quota as quota

    cache = ns["open_cache_db"]()
    _seed_store(cache)
    _assert_witnesses(cache, op_id="o:1", account_key=ACCT_A,
                      witnesses=(TARGET_RAWS[0],), canonical=TARGET_RESET)
    _assert_witnesses(cache, op_id="o:2", account_key=ACCT_B,
                      witnesses=(TARGET_RAWS[1],), canonical=TARGET_RESET,
                      at="2026-08-14T01:00:00.000000Z")

    observations = quota.load_codex_quota_observations(cache_conn=cache)
    assert _accounts_for(observations, TARGET_RESET) == {UNATTRIBUTED}


def test_a_retracted_assertion_applies_nothing(ns):
    import _cctally_quota as quota

    cache = ns["open_cache_db"]()
    _seed_store(cache)
    _assert_witnesses(cache, witnesses=(TARGET_RAWS[0],),
                      canonical=TARGET_RESET)
    cache.execute(
        "UPDATE codex_window_attributions SET retracted_by_op_id = 'o:gone'")
    cache.commit()

    observations = quota.load_codex_quota_observations(cache_conn=cache)
    assert _accounts_for(observations, TARGET_RESET) == {UNATTRIBUTED}


def test_a_dormant_assertion_applies_nothing_and_raises_nothing(ns):
    import _cctally_quota as quota

    cache = ns["open_cache_db"]()
    _seed_store(cache)
    _assert_witnesses(
        cache, witnesses=(dt.datetime(2020, 1, 1, tzinfo=UTC),),
        canonical=dt.datetime(2020, 1, 1, tzinfo=UTC))

    observations = quota.load_codex_quota_observations(cache_conn=cache)
    assert _accounts_for(observations, TARGET_RESET) == {UNATTRIBUTED}


def test_a_null_canonical_anchor_is_still_attributable(ns):
    """Both sides COALESCE, so a row the anchor backfill never reached is keyed
    on its own raw reset by the evidence pass AND by ``_physical_window_key``.

    The evidence pass reads ``COALESCE(canonical_resets_at_utc,
    resets_at_utc)``; ``QuotaObservation.__post_init__`` fills
    ``canonical_resets_at`` from ``resets_at`` when the loader passes ``None``.
    The two keys are therefore equal rather than mismatched, and no migration
    dependency is being relied on."""
    import _cctally_quota as quota

    cache = ns["open_cache_db"]()
    _seed_store(cache)
    orphan = TARGET_RESET + dt.timedelta(days=28)
    for index in range(2):
        _seed_snapshot(
            cache, captured=orphan - dt.timedelta(days=6, hours=index),
            reset=orphan, anchor=orphan, source_path="/s/orphan.jsonl",
            line_offset=700 + index, used_percent=float(index + 1))
    cache.execute(
        "UPDATE quota_window_snapshots SET canonical_resets_at_utc = NULL "
        " WHERE source_path = '/s/orphan.jsonl'")
    cache.commit()
    _assert_witnesses(cache, witnesses=(orphan,), canonical=orphan)

    observations = quota.load_codex_quota_observations(cache_conn=cache)
    assert _accounts_for(observations, orphan) == {ACCT_A}


def test_the_witness_binding_is_spelling_independent(ns):
    """The cache retains whichever spelling the provider sent, and the journal
    payload carries whichever spelling the command read. Both sides normalize
    before intersecting, so ``…+00:00`` and ``…Z`` are one instant."""
    import _cctally_quota as quota

    cache = ns["open_cache_db"]()
    _seed_store(cache)
    cache.execute(
        "INSERT INTO codex_window_attributions "
        "(op_id, account_key, source_root_key, logical_limit_key,"
        " observed_slot, window_minutes, raw_resets_at_utc,"
        " canonical_resets_at_utc, asserted_at_utc, retracted_by_op_id) "
        "VALUES ('o:offset',?,?,?,'primary',?,?,?,?,NULL)",
        (ACCT_A, ROOT, WEEK_KEY, WEEK,
         json.dumps([TARGET_RAWS[2].isoformat()], separators=(",", ":")),
         TARGET_RESET.isoformat(), "2026-08-14T00:00:00.000000Z"),
    )
    cache.commit()

    observations = quota.load_codex_quota_observations(cache_conn=cache)
    assert _accounts_for(observations, TARGET_RESET) == {ACCT_A}


# ── §6.4.1: resolution against complete evidence ─────────────────────────────

def test_bounded_reads_agree_with_the_full_read_on_a_bridged_component(ns):
    """Witness matching is population-dependent, and a bridged component's
    bounded subset can contain none of the assertion's original witnesses.
    Resolution therefore runs against complete evidence and application against
    the bounded rows. All three read shapes must report the same owner."""
    import _cctally_quota as quota

    cache = ns["open_cache_db"]()
    _seed_store(cache)
    # The assertion witnesses ONLY the oldest capture of the component, which is
    # exactly the row a recency-bounded read drops.
    _assert_witnesses(cache, witnesses=(TARGET_RAWS[3],),
                      canonical=TARGET_RESET)
    oldest_capture = TARGET_RESET - dt.timedelta(days=6, hours=3)

    full = quota.load_codex_quota_observations(cache_conn=cache)
    bounded = quota.load_codex_quota_observations(
        cache_conn=cache,
        captured_at_or_after=oldest_capture + dt.timedelta(minutes=1),
        max_rows=100,
    )
    # The dashboard's own four-argument shape, verbatim: root-scoped, recency-
    # bounded, row-capped, and carrying `active_at` (`bin/_cctally_dashboard.py`
    # always passes it). AC15 names THIS read, so the test has to make it — and
    # the target window's reset is in the past relative to `active_at`, so the
    # still-active retain clause does not save the witness row and the bound
    # genuinely removes it.
    dashboard_active_at = TARGET_RESET + dt.timedelta(days=1)
    dashboard_shaped = quota.load_codex_quota_observations(
        cache_conn=cache,
        source_root_keys=[ROOT],
        captured_at_or_after=oldest_capture + dt.timedelta(minutes=1),
        active_at=dashboard_active_at,
        max_rows=100,
    )
    latest = quota.load_codex_quota_observations(
        cache_conn=cache, latest_per_identity=True)

    assert _accounts_for(full, TARGET_RESET) == {ACCT_A}
    assert _accounts_for(bounded, TARGET_RESET) == {ACCT_A}
    assert _accounts_for(dashboard_shaped, TARGET_RESET) == {ACCT_A}
    assert _accounts_for(latest, TARGET_RESET) == {ACCT_A}
    # The bound really did remove the witness row, or this test proves nothing —
    # asserted for the dashboard's own shape too, which is the one AC15 names.
    target_full = [o for o in full if o.canonical_resets_at == TARGET_RESET]
    assert len([o for o in bounded
                if o.canonical_resets_at == TARGET_RESET]) < len(target_full)
    assert len([o for o in dashboard_shaped
                if o.canonical_resets_at == TARGET_RESET]) < len(target_full)


def test_a_targeted_physical_group_read_reports_the_same_owner(ns):
    """The incremental projector loads exactly one dirty group. Resolution must
    still see the whole component behind it."""
    import _cctally_quota as quota

    cache = ns["open_cache_db"]()
    _seed_store(cache)
    _assert_witnesses(cache, witnesses=(TARGET_RAWS[3],),
                      canonical=TARGET_RESET)

    targeted = quota.load_codex_quota_observations(
        cache_conn=cache,
        physical_groups=[(ROOT, WEEK_KEY, "primary", WEEK, _z(TARGET_RESET))],
    )
    assert targeted
    assert _accounts_for(targeted, TARGET_RESET) == {ACCT_A}


def test_the_public_resolver_reports_every_outcome(ns):
    """The seam ``account attribute`` previews through and ``doctor`` reports
    from. It returns resolutions for EVERY active assertion, including the ones
    that apply nothing."""
    import _cctally_quota as quota
    import _lib_codex_window_attribution as wa

    cache = ns["open_cache_db"]()
    _seed_store(cache)
    _assert_witnesses(cache, op_id="o:live", witnesses=(TARGET_RAWS[0],),
                      canonical=TARGET_RESET)
    _assert_witnesses(cache, op_id="o:dormant",
                      witnesses=(dt.datetime(2020, 1, 1, tzinfo=UTC),),
                      canonical=dt.datetime(2020, 1, 1, tzinfo=UTC),
                      at="2026-08-14T02:00:00.000000Z")
    _assert_witnesses(cache, op_id="o:native", witnesses=(NATIVE_RESET,),
                      canonical=NATIVE_RESET,
                      at="2026-08-14T03:00:00.000000Z")

    resolutions, ownership = quota.resolve_codex_window_attributions(
        cache, source_root_keys=[ROOT])
    outcomes = {r.op_id: r.outcome for r in resolutions}
    assert outcomes == {
        "o:live": wa.RESOLVED,
        "o:dormant": wa.DORMANT,
        "o:native": wa.SUPPRESSED_NATIVE,
    }
    assert list(ownership.values()) == [ACCT_A]


# ── §6.5: the spend axis ─────────────────────────────────────────────────────

def test_the_spend_axis_follows_the_overlay(ns):
    """No new adoption logic: an attributed group becomes a claiming window that
    identifies exactly one account, which is the condition the existing kernel
    already tests."""
    import _cctally_cache as cache_mod

    cache = ns["open_cache_db"]()
    _seed_store(cache)
    _seed_spend(cache)
    _assert_witnesses(cache, witnesses=(TARGET_RAWS[1],),
                      canonical=TARGET_RESET)

    stamped = cache_mod.apply_codex_window_spend_adoption(cache)
    cache.commit()
    assert stamped == 6, "the six rows inside the attributed window, and only those"
    accounts = _entry_accounts(cache)
    assert {accounts[200 + i] for i in range(6)} == {ACCT_A}
    assert accounts[900] is None
    assert accounts[910] == ACCT_C


# ── §7.1: suppression must un-stamp spend ────────────────────────────────────

def test_native_evidence_arriving_later_moves_both_axes(ns):
    """Assert account A, then ingest native evidence naming account B, with NO
    operator retraction. Both axes must end on B.

    Without the standing reconciliation this fails with spend stranded on A: the
    overlay is re-derived per load so the percentage axis reverts by
    construction, but ``codex_session_entries.account_key`` is a stored stamp and
    the adoption kernel never revisits an identified row (spec §7.1)."""
    import _cctally_cache as cache_mod
    import _cctally_quota as quota

    cache = ns["open_cache_db"]()
    _seed_store(cache)
    _seed_spend(cache)
    _assert_witnesses(cache, witnesses=(TARGET_RAWS[1],),
                      canonical=TARGET_RESET)

    assert cache_mod.apply_codex_window_spend_adoption(cache) == 6
    cache.commit()
    assert set(_entry_accounts(cache).values()) >= {ACCT_A}

    # Ingest supplies native evidence for the same physical group.
    _seed_snapshot(
        cache, captured=TARGET_RESET - dt.timedelta(hours=1),
        reset=TARGET_RAWS[0], anchor=TARGET_RESET, account_key=ACCT_B,
        source_path="/s/target.jsonl", line_offset=200, used_percent=9.0)
    cache.commit()

    observations = quota.load_codex_quota_observations(cache_conn=cache)
    assert _accounts_for(observations, TARGET_RESET) == {ACCT_B}, (
        "the percentage axis reverts by construction")

    restored, adopted = cache_mod.reconcile_codex_window_attribution_spend(cache)
    cache.commit()
    assert restored == 6
    assert adopted == 6
    accounts = _entry_accounts(cache)
    assert {accounts[200 + i] for i in range(6)} == {ACCT_B}, (
        "both axes must end on B")
    assert accounts[900] is None
    assert accounts[910] == ACCT_C


def test_a_retraction_restores_the_baseline_before_re_adoption(ns):
    """A group with no surviving evidence of its own goes back to whatever the
    per-file map says — a real key where a durable decision covers those bytes,
    NULL where none does."""
    import _cctally_cache as cache_mod

    cache = ns["open_cache_db"]()
    _seed_store(cache)
    _seed_spend(cache)
    _assert_witnesses(cache, witnesses=(TARGET_RAWS[1],),
                      canonical=TARGET_RESET)
    assert cache_mod.apply_codex_window_spend_adoption(cache) == 6
    cache.commit()

    cache.execute(
        "UPDATE codex_window_attributions SET retracted_by_op_id = 'o:gone'")
    cache.commit()

    restored, adopted = cache_mod.reconcile_codex_window_attribution_spend(cache)
    cache.commit()
    assert restored == 6
    assert adopted == 0
    accounts = _entry_accounts(cache)
    assert {accounts[200 + i] for i in range(6)} == {None}


def test_a_durable_file_decision_is_the_baseline_a_retraction_restores(ns):
    """Not NULL — the per-file decision covering those bytes. Restoring to NULL
    where a real decision exists would destroy #416's attribution.

    The decision here covers the second half of the file only, so a restore that
    resolved by path instead of by ``(identity, incarnation, offset)`` would put
    the wrong key on the first three rows."""
    import _cctally_cache as cache_mod

    cache = ns["open_cache_db"]()
    _seed_store(cache)
    _seed_spend(cache)
    identity = cache_mod.codex_file_key_for_entry_path(ROOT, "/s/rollout.jsonl")
    cache_mod.set_codex_file_incarnation(cache, identity, 1,
                                         at_utc=_z(TARGET_START))
    cache_mod.record_codex_file_account(
        cache, file_identity=identity, incarnation=1, from_offset=203,
        root_scope=ROOT, account_key=ACCT_C, decided_at_utc=_z(TARGET_START))
    cache.commit()
    _assert_witnesses(cache, witnesses=(TARGET_RAWS[1],),
                      canonical=TARGET_RESET)
    assert cache_mod.apply_codex_window_spend_adoption(cache) == 6
    cache.commit()
    assert {_entry_accounts(cache)[200 + i] for i in range(6)} == {ACCT_A}

    cache.execute(
        "UPDATE codex_window_attributions SET retracted_by_op_id = 'o:gone'")
    cache.commit()
    restored, adopted = cache_mod.reconcile_codex_window_attribution_spend(cache)
    cache.commit()
    assert restored == 6
    assert adopted == 0
    accounts = _entry_accounts(cache)
    assert [accounts[200 + i] for i in range(6)] == [
        None, None, None, ACCT_C, ACCT_C, ACCT_C]


def test_a_row_with_no_recoverable_file_identity_is_skipped_not_fatal(ns):
    """Baseline resolution does real Python, and a ``ValueError`` from it is not
    a ``sqlite3.DatabaseError``, so it escaped the best-effort guard in
    ``sync_codex_cache`` and would have failed the WHOLE Codex sync over one
    unrecoverable row.

    ``line_offset`` carries INTEGER AFFINITY, not an integer constraint, so a
    hand-repaired or corrupt row can hold text that ``int()`` refuses. (A blank
    ``source_path`` is NOT such a case and was checked: ``pathlib.Path('')``
    canonicalizes to a non-empty path, so ``codex_file_key`` accepts it.)
    Skipping the row is the same rule the quota loader applies window-by-window;
    ``strict`` still propagates, because the command's all-or-nothing apply must
    not report success over it."""
    import _cctally_cache as cache_mod

    cache = ns["open_cache_db"]()
    _seed_store(cache)
    _seed_spend(cache)
    _assert_witnesses(cache, witnesses=(TARGET_RAWS[1],),
                      canonical=TARGET_RESET)
    assert cache_mod.apply_codex_window_spend_adoption(cache) == 6
    cache.commit()
    # A stamped row inside the same window whose stored offset is not a number.
    _seed_entry(cache, timestamp=TARGET_START + dt.timedelta(hours=2),
                account_key=ACCT_A, source_path="/s/rollout.jsonl",
                line_offset=950)
    cache.execute(
        "UPDATE codex_session_entries SET line_offset = 'not-an-offset' "
        " WHERE line_offset = 950")
    cache.execute(
        "UPDATE codex_window_attributions SET retracted_by_op_id = 'o:gone'")
    cache.commit()

    restored, adopted = cache_mod.reconcile_codex_window_attribution_spend(
        cache)
    cache.commit()
    assert restored == 6, "the six recoverable rows still restore"
    assert adopted == 0
    assert cache.execute(
        "SELECT account_key FROM codex_session_entries "
        " WHERE line_offset = 'not-an-offset'").fetchone()[0] == ACCT_A

    with pytest.raises(ValueError):
        cache_mod.reconcile_codex_window_attribution_spend(cache, strict=True)


def test_the_reconciliation_is_a_no_op_without_attributions(ns):
    import _cctally_cache as cache_mod

    cache = ns["open_cache_db"]()
    _seed_store(cache)
    _seed_spend(cache)
    assert cache_mod.reconcile_codex_window_attribution_spend(cache) == (0, 0)


def test_a_natively_justified_stamp_is_never_restored_and_re_adopted(ns):
    """The skip set is "an account the current world can justify for the group
    containing this instant", not "the account a RESOLVED assertion supplies".

    A group resolving SUPPRESSED_NATIVE contributes no ownership, so a spend row
    stamped with that group's own native account — where the operator also
    asserted that account elsewhere, which is ordinary — was not "already
    correct". Its per-file baseline differs, so it was restored to NULL and
    immediately re-adopted to the same account, on every ``sync_codex_cache``,
    forever, each time printing a line claiming an attribution no longer
    resolves. The end state was right; the write behaviour was not idempotent,
    which contradicts both this function's docstring and the ``--rebuild``
    idempotency rule."""
    import _cctally_cache as cache_mod

    cache = ns["open_cache_db"]()
    _seed_store(cache)
    _seed_spend(cache)
    # One assertion resolves (the target group); a second names the natively
    # identified group and is suppressed. BOTH name ACCT_C, so ACCT_C is an
    # asserted key and row 910's native stamp becomes a restore candidate.
    _assert_witnesses(cache, op_id="o:target", account_key=ACCT_C,
                      witnesses=(TARGET_RAWS[1],), canonical=TARGET_RESET)
    _assert_witnesses(cache, op_id="o:native", account_key=ACCT_C,
                      witnesses=(NATIVE_RESET,), canonical=NATIVE_RESET,
                      at="2026-08-14T01:00:00.000000Z")

    assert cache_mod.apply_codex_window_spend_adoption(cache) == 6
    cache.commit()

    first = cache_mod.reconcile_codex_window_attribution_spend(cache)
    cache.commit()
    second = cache_mod.reconcile_codex_window_attribution_spend(cache)
    cache.commit()
    assert first == (0, 0), "nothing to restore and nothing left to adopt"
    assert second == (0, 0), "and the same again, which is what idempotent means"
    accounts = _entry_accounts(cache)
    assert accounts[910] == ACCT_C
    assert {accounts[200 + i] for i in range(6)} == {ACCT_C}


def test_a_model_scoped_group_never_justifies_a_stale_operator_stamp(ns):
    """A group the kernel refuses to attribute must not authorize KEEPING an
    attribution either.

    ``resolve_window_attributions`` files a model-scoped group as
    ``SUPPRESSED_MODEL_SCOPED``, so such a group can never be the source of a
    stamp. Letting its native accounts justify one is therefore one-directional
    damage: the justification span is a whole week, so a single Spark-labelled
    capture naming the same account the operator asserted covers every stamped
    row in that week and the retraction below silently accomplishes nothing.

    The extra capture here shares the target group's stored axes and anchor, so
    the evidence pass loads it; its Spark ``observed_model`` rewrites the
    INTERPRETED limit key, so it buckets as its own model-scoped group rather
    than joining the target. That is the ordinary shape — a re-materialized
    window carrying pool evidence — not a contrived one.
    """
    import _cctally_cache as cache_mod

    cache = ns["open_cache_db"]()
    _seed_store(cache)
    _seed_spend(cache)
    _seed_snapshot(
        cache, captured=TARGET_RESET - dt.timedelta(hours=2),
        reset=TARGET_RAWS[0], anchor=TARGET_RESET, account_key=ACCT_A,
        source_path="/s/spark-target.jsonl", line_offset=300,
        limit_name="gpt-5.3-codex-spark",
        observed_model="gpt-5.3-codex-spark", used_percent=7.0)
    cache.commit()
    _assert_witnesses(cache, witnesses=(TARGET_RAWS[1],),
                      canonical=TARGET_RESET)

    assert cache_mod.apply_codex_window_spend_adoption(cache) == 6, (
        "the model-scoped capture is not a claiming window and changes nothing")
    cache.commit()
    assert {_entry_accounts(cache)[200 + i] for i in range(6)} == {ACCT_A}

    cache.execute(
        "UPDATE codex_window_attributions SET retracted_by_op_id = 'o:gone'")
    cache.commit()

    restored, adopted = cache_mod.reconcile_codex_window_attribution_spend(cache)
    cache.commit()
    assert restored == 6
    assert adopted == 0
    assert {_entry_accounts(cache)[200 + i] for i in range(6)} == {None}


def test_the_reconciliation_is_idempotent(ns):
    import _cctally_cache as cache_mod

    cache = ns["open_cache_db"]()
    _seed_store(cache)
    _seed_spend(cache)
    _assert_witnesses(cache, witnesses=(TARGET_RAWS[1],),
                      canonical=TARGET_RESET)

    first = cache_mod.reconcile_codex_window_attribution_spend(cache)
    cache.commit()
    second = cache_mod.reconcile_codex_window_attribution_spend(cache)
    cache.commit()
    assert first == (0, 6)
    assert second == (0, 0)
    accounts = _entry_accounts(cache)
    assert {accounts[200 + i] for i in range(6)} == {ACCT_A}


# ── §8.2: the strict adoption mode ───────────────────────────────────────────

def test_the_default_adoption_pass_swallows_a_database_failure(ns):
    """Its ordinary caller is a sync that must not fail a whole ingest over an
    adoption problem, so this behaviour is deliberate and stays."""
    import _cctally_cache as cache_mod

    cache = ns["open_cache_db"]()
    _seed_store(cache)
    _seed_spend(cache)
    cache.execute("DROP TABLE codex_session_entries")
    cache.commit()
    assert cache_mod.apply_codex_window_spend_adoption(cache) == 0


def test_strict_mode_propagates_a_database_failure(ns):
    """Reused unchanged inside the command's transaction, the swallow silently
    defeats the all-or-nothing apply: the journal op lands, the percentage axis
    moves, adoption swallows a failure, and the command reports success with the
    spend axis untouched."""
    import _cctally_cache as cache_mod

    cache = ns["open_cache_db"]()
    _seed_store(cache)
    _seed_spend(cache)
    cache.execute("DROP TABLE codex_session_entries")
    cache.commit()
    with pytest.raises(sqlite3.DatabaseError):
        cache_mod.apply_codex_window_spend_adoption(cache, strict=True)


def test_strict_mode_propagates_from_the_reconciliation_too(ns):
    import _cctally_cache as cache_mod

    cache = ns["open_cache_db"]()
    _seed_store(cache)
    _seed_spend(cache)
    _assert_witnesses(cache, witnesses=(TARGET_RAWS[1],),
                      canonical=TARGET_RESET)
    cache.execute("DROP TABLE codex_session_entries")
    cache.commit()
    with pytest.raises(sqlite3.DatabaseError):
        cache_mod.reconcile_codex_window_attribution_spend(cache, strict=True)
