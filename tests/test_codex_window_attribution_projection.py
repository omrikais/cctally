"""The projection contract and the lock/ingest handoff (#500 Task 2).

Spec: ``docs/superpowers/specs/2026-08-14-500-codex-window-attribution-design.md``
§8, §8.1, §8.3, §8.4.

This design writes nothing to ``quota_window_snapshots``, so an operator
attribution fires no trigger and appears in no change-ledger entry. Left alone,
the projection certificate returns a confident no-op and the attribution never
reaches ``quota_window_blocks``. These tests pin the four points that close that
hole, and the lock/ingest handoff §8.1 says has to be BUILT rather than assumed.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import importlib
import json
import os
import sqlite3

import pytest

from conftest import load_script, redirect_paths

UTC = dt.timezone.utc

ROOT = "rk"
ACCT_A = "a" * 32
ACCT_B = "b" * 32
WEEK = 10_080

WEEK_KEY = json.dumps({
    "limitId": "codex", "observedSlot": "primary", "source": "codex",
    "sourceRootKey": ROOT, "windowMinutes": WEEK,
}, sort_keys=True, separators=(",", ":"))

RESET = dt.datetime(2026, 8, 5, 4, 35, 6, tzinfo=UTC)
RAWS = (RESET, RESET + dt.timedelta(seconds=9))
NOW = RESET - dt.timedelta(days=1)


def _z(value: dt.datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@pytest.fixture
def env(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    quota = importlib.import_module("_cctally_quota")
    cache_mod = importlib.import_module("_cctally_cache")
    jr = importlib.import_module("_cctally_journal")
    jl = importlib.import_module("_lib_journal")
    return ns, quota, cache_mod, jr, jl


def _write_alert_config(*, actual=(90, 95)):
    """Enable quota alert delivery with real thresholds.

    Both gates default OFF (`alerts.quota.enabled` is `False`), so a test that
    does not write this observes an `_evaluate_quota_alerts` that disarms and
    returns before examining a single threshold — which proves nothing about
    §8.4."""
    import _cctally_core

    _cctally_core.CONFIG_PATH.write_text(json.dumps({"alerts": {
        "enabled": True,
        "quota": {
            "enabled": True,
            "actual_thresholds": list(actual),
            "projected_thresholds": [],
            "rules": [],
        },
    }}) + "\n")


def _threshold_events(ns, account_key):
    conn = ns["open_db"]()
    try:
        return [
            (int(row[0]), str(row[1])) for row in conn.execute(
                "SELECT threshold, disposition FROM quota_threshold_events "
                " WHERE source='codex' AND account_key = ? "
                "   AND orphaned_at IS NULL "
                " ORDER BY threshold", (account_key,))
        ]
    finally:
        conn.close()


def _seed_window(ns, *, reset=RESET, raws=RAWS, account_key=None,
                 key=WEEK_KEY, root=ROOT, offset_base=100, used_percents=None):
    conn = ns["open_cache_db"]()
    try:
        conn.execute(
            "INSERT INTO codex_source_roots "
            "(source_root_key, canonical_root_path, first_seen_utc, "
            " last_seen_utc) VALUES (?,?,?,?) "
            "ON CONFLICT(source_root_key) DO UPDATE SET "
            "  last_seen_utc=excluded.last_seen_utc",
            (root, f"/codex/{root}", _z(NOW), _z(NOW)),
        )
        for index, raw in enumerate(raws):
            conn.execute(
                "INSERT INTO quota_window_snapshots "
                "(source, source_root_key, source_path, line_offset,"
                " captured_at_utc, observed_slot, logical_limit_key, limit_id,"
                " limit_name, window_minutes, used_percent, resets_at_utc,"
                " observed_model, account_key, canonical_resets_at_utc) "
                "VALUES ('codex',?,?,?,?,'primary',?,'codex',NULL,?,?,?,"
                "        'gpt-5',?,?)",
                (root, f"/codex/{root}/rollout.jsonl", offset_base + index,
                 _z(reset - dt.timedelta(days=6, hours=index)), key, WEEK,
                 (float(index + 1) * 5.0 if used_percents is None
                  else float(used_percents[index])),
                 _z(raw), account_key, _z(reset)),
            )
        conn.commit()
    finally:
        conn.close()


def _record_attribution(ns, jr, jl, *, account_key=ACCT_A, witnesses=RAWS,
                        canonical=RESET, key=WEEK_KEY, root=ROOT,
                        at="2026-08-14T00:00:00Z"):
    """Append the real op and replay it, the way Task 3's apply sequence will."""
    op = jl.make_codex_window_attribution(
        at=at, account_key=account_key, source_root_key=root,
        logical_limit_key=key, observed_slot="primary", window_minutes=WEEK,
        raw_resets_at_utc=[_z(w) for w in witnesses],
        canonical_resets_at_utc=_z(canonical),
    )
    conn = ns["open_cache_db"]()
    try:
        conn.execute("BEGIN IMMEDIATE")
        jr._apply_window_attribution_records(conn, [op])
        conn.commit()
    finally:
        conn.close()
    return op


def _blocks(ns):
    conn = ns["open_db"]()
    try:
        conn.row_factory = sqlite3.Row
        # `orphaned_at IS NULL` is the read model's own predicate: the sweep
        # MARKS an obsolete block rather than deleting it, so a query without it
        # would report a swept row as live and this file would assert nothing.
        return [
            dict(row) for row in conn.execute(
                "SELECT source_root_key, account_key, resets_at_utc, "
                "       current_percent FROM quota_window_blocks "
                " WHERE source='codex' AND orphaned_at IS NULL "
                " ORDER BY resets_at_utc, account_key")
        ]
    finally:
        conn.close()


def _all_blocks(ns):
    conn = ns["open_db"]()
    try:
        return [
            (row[0], row[1] is not None) for row in conn.execute(
                "SELECT account_key, orphaned_at FROM quota_window_blocks "
                " WHERE source='codex' ORDER BY account_key")
        ]
    finally:
        conn.close()


def _revision(ns):
    import _cctally_cache as cache_mod

    conn = ns["open_cache_db"]()
    try:
        return cache_mod.codex_window_attribution_revision(conn)
    finally:
        conn.close()


# ── the attribution revision ─────────────────────────────────────────────────

def test_replaying_an_attribution_advances_the_revision(env):
    ns, _quota, _cache_mod, jr, jl = env
    assert _revision(ns) == 0
    _record_attribution(ns, jr, jl)
    assert _revision(ns) == 1


def test_a_no_op_replay_does_not_advance_the_revision(env):
    """The rehydration re-reads the ops its own previous sync appended, so a
    revision that moved on every replay would invalidate the quota projection
    on every sync."""
    ns, _quota, _cache_mod, jr, jl = env
    op = _record_attribution(ns, jr, jl)
    assert _revision(ns) == 1
    conn = ns["open_cache_db"]()
    try:
        conn.execute("BEGIN IMMEDIATE")
        jr._apply_window_attribution_records(conn, [op])
        conn.commit()
    finally:
        conn.close()
    assert _revision(ns) == 1


def test_the_replay_cursor_and_the_revision_are_separate(env):
    """§8.3 point four. The journal replay cursor advances on ALL traffic;
    keying the certificate on it would invalidate the quota projection
    continuously and degrade the targeted path into full re-projection."""
    ns, _quota, cache_mod, jr, jl = env
    _record_attribution(ns, jr, jl)
    before = _revision(ns)
    conn = ns["open_cache_db"]()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cache_mod.store_codex_window_attribution_cursor(
            conn, ("observations-2026-08.jsonl", 4096))
        conn.commit()
        cursor = cache_mod.load_codex_window_attribution_cursor(conn)
    finally:
        conn.close()
    assert cursor == ("observations-2026-08.jsonl", 4096)
    assert _revision(ns) == before


# ── the certificate binds the revision ───────────────────────────────────────

def test_the_certificate_is_declined_once_the_revision_moves(env):
    ns, quota, _cache_mod, jr, jl = env
    _seed_window(ns)
    quota.reconcile_codex_quota_projection(now=NOW)
    conn = ns["open_cache_db"]()
    try:
        assert quota.load_codex_quota_projection_certificate(conn) is not None
    finally:
        conn.close()

    _record_attribution(ns, jr, jl)
    conn = ns["open_cache_db"]()
    try:
        assert quota.load_codex_quota_projection_certificate(conn) is None, (
            "a certificate computed against an older attribution revision "
            "must not read as current")
        payload = quota._codex_quota_projection_certificate_payload(conn)
        assert quota._certificate_attribution_revision(payload) == 0
    finally:
        conn.close()


def test_a_certificate_is_never_stamped_at_a_revision_the_pass_never_saw(env):
    """§8.5's ``recordedPending`` state, and it needs no concurrency at all.

    ``account attribute --yes`` appends its op and dies before the replay. The
    next ordinary pass reads the revision, loads observations that never saw the
    assertion, and only THEN runs the ingest cycle — whose cache leg materializes
    the pending op and advances the revision. Reading the revision at STAMP time
    therefore certified a projection computed against the previous one, every
    later reconcile short-circuited on it, and the attribution never reached
    ``quota_window_blocks`` until an unrelated physical mutation dirtied the
    group. Declining to stamp is the fail-safe direction and matches the
    physical-sequence guard right beside it."""
    ns, quota, cache_mod, jr, jl = env
    _seed_window(ns)
    quota.reconcile_codex_quota_projection(now=NOW)
    assert [row["account_key"] for row in _blocks(ns)] == ["unattributed"]
    assert _revision(ns) == 0

    # Appended and never replayed — exactly `recordedPending`.
    jr.append_record(jl.make_codex_window_attribution(
        at="2026-08-14T00:00:00Z", account_key=ACCT_A, source_root_key=ROOT,
        logical_limit_key=WEEK_KEY, observed_slot="primary",
        window_minutes=WEEK, raw_resets_at_utc=[_z(w) for w in RAWS],
        canonical_resets_at_utc=_z(RESET),
    ))
    # An ordinary hook tick then ingests a new observation, which advances the
    # physical mutation sequence so the pass does NOT short-circuit.
    _seed_window(ns, raws=(RESET + dt.timedelta(seconds=15),), offset_base=300)

    quota.reconcile_codex_quota_projection(now=NOW)
    assert _revision(ns) == 1, "the pass's own ingest cycle replayed the op"

    conn = ns["open_cache_db"]()
    try:
        assert quota.load_codex_quota_projection_certificate(conn) is None, (
            "the pass computed against revision 0 and the revision is now 1, "
            "so its certificate must be declined rather than stamped at 1")
    finally:
        conn.close()

    # And because it was declined, the very next pass applies the attribution
    # instead of short-circuiting on a certificate that never saw it.
    quota.reconcile_codex_quota_projection(now=NOW)
    assert [row["account_key"] for row in _blocks(ns)] == [ACCT_A]


def test_a_pre_500_certificate_still_reads_as_current_without_attributions(env):
    """An install that has never asserted anything keeps its existing
    certificate and its existing short-circuit, byte for byte."""
    ns, quota, _cache_mod, _jr, _jl = env
    _seed_window(ns)
    quota.reconcile_codex_quota_projection(now=NOW)
    conn = ns["open_cache_db"]()
    try:
        payload = quota._codex_quota_projection_certificate_payload(conn)
        payload.pop("attributionRevision", None)
        conn.execute(
            "UPDATE cache_meta SET value=? WHERE key=?",
            (json.dumps(payload, sort_keys=True, separators=(",", ":")),
             quota._DASHBOARD_PROJECTION_CERTIFICATE_KEY))
        conn.commit()
        assert quota.load_codex_quota_projection_certificate(conn) is not None
    finally:
        conn.close()


# ── the dirty-unit scope ─────────────────────────────────────────────────────

def test_the_scope_is_none_without_attribution_records(env):
    ns, quota, _cache_mod, _jr, _jl = env
    _seed_window(ns)
    conn = ns["open_cache_db"]()
    try:
        assert quota.codex_attribution_projection_scope(conn) is None
    finally:
        conn.close()


def test_the_scope_carries_the_prior_unit_and_the_current_one(env):
    """§8.3 point two. A retraction, a newly dormant assertion and a component
    split all move a group's rows to a DIFFERENT unit, so a sweep set built from
    the current resolution alone would leave the obsolete rows standing — which
    is exactly the duplicated window this work removes."""
    ns, quota, _cache_mod, jr, jl = env
    _seed_window(ns)
    # The record's stored anchor is one instant; the group's current anchor is
    # another, because a later bridging observation retired the first.
    prior_anchor = RESET - dt.timedelta(seconds=30)
    _record_attribution(ns, jr, jl, canonical=prior_anchor)

    conn = ns["open_cache_db"]()
    try:
        scope = quota.codex_attribution_projection_scope(conn)
    finally:
        conn.close()
    import _lib_quota_ledger as ledger

    def _unit(anchor):
        return ledger.physical_group_key_text(ledger.loading_unit_from_raw(
            (ROOT, WEEK_KEY, "primary", WEEK, _z(anchor))))

    assert _unit(prior_anchor) in scope["units"], "the unit at assertion time"
    assert _unit(RESET) in scope["units"], "the unit it resolves to now"


def test_the_scope_raw_groups_are_snap_expanded(env):
    """§8.3 point one. The loader matches RAW stored coordinates, and one minute
    of weekly jitter lives in BOTH the limit key and the column, so two raw
    groups can interpret into one window."""
    ns, quota, _cache_mod, jr, jl = env
    _seed_window(ns)
    _record_attribution(ns, jr, jl)
    conn = ns["open_cache_db"]()
    try:
        scope = quota.codex_attribution_projection_scope(conn)
    finally:
        conn.close()
    minutes = {group[3] for group in scope["raw_groups"]}
    assert minutes == {WEEK - 1, WEEK, WEEK + 1}


def test_the_scope_includes_a_retracted_record(env):
    ns, quota, _cache_mod, jr, jl = env
    _seed_window(ns)
    _record_attribution(ns, jr, jl)
    conn = ns["open_cache_db"]()
    try:
        conn.execute("UPDATE codex_window_attributions "
                     "SET retracted_by_op_id = 'o:gone'")
        conn.commit()
        scope = quota.codex_attribution_projection_scope(conn)
    finally:
        conn.close()
    assert scope is not None and scope["units"], (
        "a tombstoned record names precisely the unit whose materialized rows "
        "have to be swept")


# ── end to end: the attribution reaches quota_window_blocks ──────────────────

def test_an_attribution_reaches_quota_window_blocks(env):
    """The whole point of §8.3. Without the certificate revision and the
    attribution scope this reconcile is a confident no-op and the block keeps
    the `unattributed` account forever."""
    ns, quota, _cache_mod, jr, jl = env
    _seed_window(ns)
    quota.reconcile_codex_quota_projection(now=NOW)
    assert [row["account_key"] for row in _blocks(ns)] == ["unattributed"]

    _record_attribution(ns, jr, jl)
    quota.reconcile_codex_quota_projection(now=NOW)
    blocks = _blocks(ns)
    assert [row["account_key"] for row in blocks] == [ACCT_A], (
        "one block, owned by the asserted account — not two")
    assert (ACCT_A, False) in _all_blocks(ns)
    assert ("unattributed", True) in _all_blocks(ns), (
        "the obsolete block is swept, which is what stops one window rendering "
        "twice at the same reset instant")


def test_a_retraction_reaches_quota_window_blocks_too(env):
    ns, quota, _cache_mod, jr, jl = env
    _seed_window(ns)
    op = _record_attribution(ns, jr, jl)
    quota.reconcile_codex_quota_projection(now=NOW)
    assert [row["account_key"] for row in _blocks(ns)] == [ACCT_A]

    tombstone = jl.make_codex_window_attribution_retract(
        at="2026-08-15T00:00:00Z", account_key=ACCT_A, source_root_key=ROOT,
        logical_limit_key=WEEK_KEY, observed_slot="primary",
        window_minutes=WEEK, raw_resets_at_utc=[_z(w) for w in RAWS],
        canonical_resets_at_utc=_z(RESET), retracted_assertion_ids=[op["id"]],
    )
    conn = ns["open_cache_db"]()
    try:
        conn.execute("BEGIN IMMEDIATE")
        jr._apply_window_attribution_records(conn, [tombstone])
        conn.commit()
    finally:
        conn.close()

    quota.reconcile_codex_quota_projection(now=NOW)
    blocks = _blocks(ns)
    assert [row["account_key"] for row in blocks] == ["unattributed"], (
        "the obsolete block must be SWEPT, not left beside a new one")


def test_a_second_reconcile_after_the_attribution_short_circuits(env):
    """Once the certificate carries the new revision the pass stops re-running:
    the revision is what invalidates it, and it advances only on an attribution
    change."""
    ns, quota, _cache_mod, jr, jl = env
    _seed_window(ns)
    _record_attribution(ns, jr, jl)
    quota.reconcile_codex_quota_projection(now=NOW)
    result = quota.reconcile_codex_quota_projection(now=NOW)
    assert result.blocks_upserted == 0
    conn = ns["open_cache_db"]()
    try:
        assert quota.load_codex_quota_projection_certificate(conn) is not None
    finally:
        conn.close()


# ── §8.4: no historical alert dispatch ───────────────────────────────────────

def test_re_projecting_history_dispatches_no_historical_alert(env):
    """Re-projecting months of windows under a newly named account must not
    dispatch the crossings those windows satisfied last winter.

    The mechanism is NOT the empty ``alert_eligible_root_keys`` default. The
    caller that matters is the Codex hook, which passes a NON-empty eligible-root
    set, and Task 2's attribution scope is unioned into ``dirty_units`` inside
    the same ``reconcile_codex_quota_projection`` the hook calls — so
    ``_evaluate_quota_alerts`` really does run over re-projected history there.
    What suppresses dispatch is that an attributed group produces a NEW
    ``QuotaWindowIdentity`` (the account key is part of it), so
    ``_activate_quota_rule`` reports ``changed=True`` and every already-satisfied
    crossing is written ``disposition='suppressed_backfill'`` and skipped
    without queueing; on a later tick with ``changed=False`` only observations
    captured after the activation boundary qualify.

    So this test runs the hook's own shape, with delivery enabled and thresholds
    the seeded window has already crossed."""
    ns, quota, _cache_mod, jr, jl = env
    _write_alert_config(actual=(90, 95))
    _seed_window(ns, used_percents=(92.0, 96.0))

    armed = quota.reconcile_codex_quota_projection(
        now=NOW, alert_eligible_root_keys=[ROOT])
    assert armed.alerts_dispatched == 0
    assert _threshold_events(ns, "unattributed") == [
        (90, "suppressed_backfill"), (95, "suppressed_backfill")], (
        "the fixture must actually cross the configured thresholds, or the "
        "assertion below is about an empty table")

    _record_attribution(ns, jr, jl)
    result = quota.reconcile_codex_quota_projection(
        now=NOW, alert_eligible_root_keys=[ROOT])

    assert [row["account_key"] for row in _blocks(ns)] == [ACCT_A], (
        "the re-projection really did run under the newly named account")
    assert _threshold_events(ns, ACCT_A) == [
        (90, "suppressed_backfill"), (95, "suppressed_backfill")], (
        "history re-projected under a new account must be suppressed, never "
        "dispatched")
    assert result.alerts_dispatched == 0, "no historical alert was queued"


# ── §8.1: the lock and ingest handoff ────────────────────────────────────────

def _busy(path) -> bool:
    """Whether an EXCLUSIVE flock on ``path`` is unavailable to a new fd.

    A separate ``os.open`` is a separate open file description, so ``flock``
    contends with this process's own hold exactly as another process would.
    """
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def test_the_apply_lock_owner_releases_the_cache_flocks_only(env):
    ns, _quota, _cache_mod, _jr, _jl = env
    import _cctally_core as core
    import _cctally_rederive as rederive

    with rederive.codex_attribution_apply_locks(timeout=5.0) as owner:
        assert _busy(core.CACHE_LOCK_PATH)
        assert _busy(core.CACHE_LOCK_CODEX_PATH)
        assert _busy(core.JOURNAL_INGEST_LOCK_PATH)
        assert _busy(core.STATS_LOCK_MAINTENANCE_PATH)

        owner.release_cache_flocks()

        assert not _busy(core.CACHE_LOCK_PATH)
        assert not _busy(core.CACHE_LOCK_CODEX_PATH)
        assert _busy(core.JOURNAL_INGEST_LOCK_PATH), (
            "the stats transaction still has to run under the ingest lock")
        assert _busy(core.STATS_LOCK_MAINTENANCE_PATH)

    assert not _busy(core.JOURNAL_INGEST_LOCK_PATH)
    assert not _busy(core.STATS_LOCK_MAINTENANCE_PATH)


def test_releasing_the_cache_flocks_twice_is_a_no_op(env):
    ns, _quota, _cache_mod, _jr, _jl = env
    import _cctally_core as core
    import _cctally_rederive as rederive

    with rederive.codex_attribution_apply_locks(timeout=5.0) as owner:
        owner.release_cache_flocks()
        owner.release_cache_flocks()
        assert _busy(core.JOURNAL_INGEST_LOCK_PATH)


def test_the_ordinary_ingest_cannot_run_under_the_apply_lock_set(env):
    """The deadlock §8.1 exists to avoid, stated as an executable fact:
    `run_stats_ingest` unconditionally reacquires maintenance and ingest, so
    calling it while already holding them is a timeout, not a reentrant no-op."""
    ns, _quota, _cache_mod, jr, _jl = env
    import _cctally_rederive as rederive

    with rederive.codex_attribution_apply_locks(timeout=5.0) as owner:
        owner.release_cache_flocks()
        result = jr.run_stats_ingest(mode="opportunistic", timeout_s=0.2)
    assert result.ran is False


def test_the_lock_accepting_ingest_runs_under_the_apply_lock_set(env):
    ns, _quota, _cache_mod, jr, jl = env
    import _cctally_rederive as rederive

    _seed_window(ns)
    op = jl.make_codex_window_attribution(
        at="2026-08-14T00:00:00Z", account_key=ACCT_A, source_root_key=ROOT,
        logical_limit_key=WEEK_KEY, observed_slot="primary",
        window_minutes=WEEK, raw_resets_at_utc=[_z(RAWS[0])],
        canonical_resets_at_utc=_z(RESET),
    )
    jr.append_record(op)

    with rederive.codex_attribution_apply_locks(timeout=5.0) as owner:
        owner.release_cache_flocks()
        result = jr.run_stats_ingest(
            mode="authoritative", timeout_s=5.0, locks_held=True)
    assert result.ran is True
    assert result.consumed >= 1
    # The cycle's own cache leg materialized the op, which is what makes the
    # handoff worth having rather than merely deadlock-free.
    conn = ns["open_cache_db"]()
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM codex_window_attributions"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_the_lock_accepting_ingest_refuses_while_the_cache_flocks_are_held(env):
    """The repository lock-order law, enforced rather than documented.

    All cache work must be committed and unlocked before the stats transaction
    opens (`docs/journal-gotchas.md`), which is the entire reason the ordered
    PARTIAL release exists. A caller that forgets `release_cache_flocks()` would
    otherwise open the stats transaction underneath live cache flocks and
    violate the law silently."""
    ns, _quota, _cache_mod, jr, jl = env
    import _cctally_rederive as rederive

    _seed_window(ns)
    jr.append_record(jl.make_codex_window_attribution(
        at="2026-08-14T00:00:00Z", account_key=ACCT_A, source_root_key=ROOT,
        logical_limit_key=WEEK_KEY, observed_slot="primary",
        window_minutes=WEEK, raw_resets_at_utc=[_z(RAWS[0])],
        canonical_resets_at_utc=_z(RESET),
    ))

    with rederive.codex_attribution_apply_locks(timeout=5.0) as owner:
        with pytest.raises(ValueError, match="cache writer flocks"):
            jr.run_stats_ingest(
                mode="authoritative", timeout_s=5.0, locks_held=True)
        owner.release_cache_flocks()
        # Released, the same call runs — which is what makes the refusal about
        # the flocks rather than about the flag.
        assert jr.run_stats_ingest(
            mode="authoritative", timeout_s=5.0, locks_held=True).ran is True


def test_locks_held_declines_automatic_correction_recovery(env, monkeypatch):
    """The decline BRANCH, exercised rather than asserted from a signature.

    Recovery works by unwinding every lock and re-seeking maintenance EXCLUSIVE
    in total order, which it cannot do while the caller owns the set, so the
    signal is re-raised with its own guidance and `_recover_completed_correction`
    is never reached."""
    _ns, _quota, _cache_mod, jr, _jl = env
    calls = []
    signal = jr.CorrectionRebuildRequired(
        "correction", batch_id="batch:locked", event_id="sa:locked",
        high_water=("observations-2026-08.jsonl", 1),
        expected_metadata=(1, "active", "sha256:x", "batch:locked"),
        recovery_eligible=True,
    )

    def _always_signal(**_kwargs):
        calls.append("attempt")
        raise signal

    monkeypatch.setattr(jr, "_run_stats_ingest_once", _always_signal)
    monkeypatch.setattr(
        jr, "_recover_completed_correction",
        lambda *_args, **_kwargs: calls.append("recovery"))

    with pytest.raises(jr.CorrectionRebuildRequired) as raised:
        jr.run_stats_ingest(mode="authoritative", locks_held=True)
    assert "holds the stats maintenance and ingest locks" in str(raised.value)
    assert raised.value.batch_id == "batch:locked"
    assert calls == ["attempt"], "recovery must never be attempted"

    # Without the flag the very same signal DOES reach recovery, so the
    # assertion above is about `locks_held` and not about the signal.
    calls.clear()
    with pytest.raises(jr.CorrectionRecoveryError):
        jr.run_stats_ingest(mode="authoritative")
    assert calls == ["attempt", "recovery", "attempt"]


def test_a_malformed_cursor_restarts_the_range_from_zero(env):
    """`_journal_cursor_order_key`'s docstring says both functions treat an
    unplaceable cursor the same way — sort it first, restart from the beginning
    — and until now `_iter_range_with_segments` unpacked it outside any guard
    and would have raised instead. Unreachable today because both loaders
    validate, and a from-zero pass is always sound because every apply is
    idempotent on its natural key."""
    import _cctally_journal as journal_mod

    ns, _quota, _cache_mod, jr, jl = env
    jr.append_record(jl.make_codex_window_attribution(
        at="2026-08-14T00:00:00Z", account_key=ACCT_A, source_root_key=ROOT,
        logical_limit_key=WEEK_KEY, observed_slot="primary",
        window_minutes=WEEK, raw_resets_at_utc=[_z(RAWS[0])],
        canonical_resets_at_utc=_z(RESET),
    ))
    segments = journal_mod.list_segments()
    high_water = journal_mod.journal_high_water()
    for malformed in ("not-a-pair", 17, ("seg-that-does-not-exist", 0)):
        seen = list(journal_mod._iter_range_with_segments(
            malformed, high_water, segments))
        assert seen, malformed


def test_the_lock_accepting_ingest_is_the_same_path(env):
    """A second, subtly different ingest path is a worse outcome than the
    deadlock it avoids, so the parameter must reach the SAME cycle."""
    import _cctally_journal as journal_mod
    import inspect

    source = inspect.getsource(journal_mod._run_stats_ingest_once)
    assert source.count("_run_cycle(") == 1
    assert "locks_held" in inspect.signature(
        journal_mod.run_stats_ingest).parameters
    assert "locks_held" in inspect.signature(
        journal_mod._run_stats_ingest_once).parameters
