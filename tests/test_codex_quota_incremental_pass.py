"""Ledger consumption, the watermark, and the scoped sweep, end to end.

Public issue omrikais/cctally#5, Tasks 7-8. Spec:
``docs/superpowers/specs/2026-07-31-codex-hook-incremental-quota-reconcile-design.md``
§1-§2.

``tests/test_codex_quota_ledger_kernel.py`` pins the pure decisions. This module
pins the ones that only exist once the projector, the cache and stats.db are all
in the same room: that a raw SQL mutation converges even though it bumped no
mutation sequence, that replaying a ledger range twice is a no-op, that an
interpretation bump and a reset ledger both fall back to a complete pass, and
that the scoped sweep still reaches every child class the whole-root sweep did.
"""
from __future__ import annotations

import datetime as dt
import importlib
import sqlite3

import pytest

from conftest import load_script, redirect_paths
from helpers.quota_projection_dump import (
    ROOT_KEY,
    append_one_observation_to_live_window,
    build_codex_quota_store,
    canonical_projection_dump,
    count_expanded_groups,
    enable_quota_alerts,
    store_as_of,
)


CLOSED_WINDOWS = 12
PER_WINDOW = 10
SECOND_ROOT_KEY = "root-departing"


def _load(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    quota = importlib.import_module("_cctally_quota")
    return ns, quota


@pytest.fixture()
def primed(tmp_path, monkeypatch):
    """A reconciled store: one full pass done, watermark stamped."""
    ns, quota = _load(tmp_path, monkeypatch)
    build_codex_quota_store(
        ns, closed_windows=CLOSED_WINDOWS, observations_per_window=PER_WINDOW)
    now = store_as_of(ns)
    quota.reconcile_codex_quota_projection(now=now)
    return ns, quota, now


def _ledger_state(ns):
    conn = ns["open_db"]()
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM quota_projection_ledger_state WHERE source='codex'"
        ).fetchone()
        return None if row is None else dict(row)
    finally:
        conn.close()


def _set_watermark(ns, seq):
    conn = ns["open_db"]()
    try:
        conn.execute(
            "UPDATE quota_projection_ledger_state SET watermark_seq=? "
            "WHERE source='codex'", (seq,))
        conn.commit()
    finally:
        conn.close()


def _ledger_rows(ns):
    conn = ns["open_cache_db"]()
    try:
        return conn.execute(
            "SELECT COUNT(*), COALESCE(MAX(seq), 0) "
            "FROM quota_window_change_log").fetchone()
    finally:
        conn.close()


def _dump(ns):
    conn = ns["open_db"]()
    try:
        return canonical_projection_dump(conn)
    finally:
        conn.close()


def _blocks(ns):
    conn = ns["open_db"]()
    try:
        conn.row_factory = sqlite3.Row
        return [
            dict(row) for row in conn.execute(
                "SELECT resets_at_utc, current_percent, orphaned_at, "
                "       physical_group_key, physical_group_digest "
                "  FROM quota_window_blocks WHERE source='codex' "
                " ORDER BY resets_at_utc")
        ]
    finally:
        conn.close()


def _newest_reset(ns):
    conn = ns["open_cache_db"]()
    try:
        return str(conn.execute(
            "SELECT MAX(COALESCE(canonical_resets_at_utc, resets_at_utc)) "
            "FROM quota_window_snapshots WHERE source='codex'").fetchone()[0])
    finally:
        conn.close()


def _oldest_reset(ns):
    conn = ns["open_cache_db"]()
    try:
        return str(conn.execute(
            "SELECT MIN(COALESCE(canonical_resets_at_utc, resets_at_utc)) "
            "FROM quota_window_snapshots WHERE source='codex'").fetchone()[0])
    finally:
        conn.close()


# ── the watermark ──────────────────────────────────────────────────────────

def test_the_first_pass_stamps_the_watermark_at_the_ledger_head(primed):
    ns, _quota, _now = primed
    state = _ledger_state(ns)
    assert state is not None
    # The seeded rows all ledgered, and the full pass consumed all of them.
    assert state["watermark_seq"] > 0
    assert state["interpretation_version"] > 0


def test_consumed_ledger_entries_are_pruned(primed):
    """Nothing else prunes them, and nothing bounds them.

    A `cache-sync --rebuild` on a 211K-observation store writes roughly 422K
    ledger rows (one delete plus one insert each). Entries at or below the
    committed watermark are provably consumed.
    """
    ns, _quota, _now = primed
    count, _high = _ledger_rows(ns)
    assert count == 0, f"{count} consumed ledger rows survived the pass"


def test_pruning_never_lets_a_seq_be_reissued_below_the_watermark(primed):
    """``AUTOINCREMENT`` is what makes the prune safe.

    With a bare rowid alias, deleting the high row hands its seq back to the
    next insert — at or below the watermark, where the projector would skip it
    forever.
    """
    ns, _quota, _now = primed
    watermark = _ledger_state(ns)["watermark_seq"]
    append_one_observation_to_live_window(ns)
    _count, high = _ledger_rows(ns)
    assert high > watermark


def test_replaying_the_same_ledger_range_twice_changes_nothing(primed):
    """The crash-consistency claim.

    The watermark is stamped inside the same stats transaction as the
    projection, so a crash replays a range rather than skipping one — which is
    only safe because re-materializing a group is idempotent. Rewinding the
    watermark and reconciling again is that replay.
    """
    ns, quota, now = primed
    append_one_observation_to_live_window(ns)
    before_watermark = _ledger_state(ns)["watermark_seq"]
    quota.reconcile_codex_quota_projection(now=now)
    once = _dump(ns)

    _set_watermark(ns, before_watermark)
    quota.reconcile_codex_quota_projection(now=now)
    twice = _dump(ns)

    assert once == twice


def test_ledger_max_seq_reads_zero_before_the_first_insert(tmp_path, monkeypatch):
    """The case with no ``sqlite_sequence`` row at all.

    ``_ledger_max_seq`` folds ``sqlite_sequence`` in because the projector
    PRUNES consumed entries, and after a prune ``MAX(seq)`` is 0 — which would
    read as "the ledger was reset below the watermark" on every clean tick. But
    ``AUTOINCREMENT`` writes no ``sqlite_sequence`` row until the FIRST insert,
    so a fresh cache has neither source. It must read 0, not ``None``: ``None``
    means "no ledger table" and forces a whole-history pass forever on a store
    that has simply never mutated a quota row.
    """
    ns, quota = _load(tmp_path, monkeypatch)
    conn = ns["open_cache_db"]()
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_sequence "
            " WHERE name='quota_window_change_log'").fetchone()[0] == 0
        assert quota._ledger_max_seq(conn) == 0
    finally:
        conn.close()


# ── the bounded pass ───────────────────────────────────────────────────────

def test_a_clean_tick_expands_nothing(primed):
    ns, quota, now = primed
    # Bust the certificate without changing any observation, so the pass runs
    # rather than short-circuiting.
    conn = ns["open_cache_db"]()
    try:
        conn.execute("DELETE FROM cache_meta WHERE key=?",
                     ("codex_quota_projection_certificate",))
        conn.commit()
    finally:
        conn.close()

    with count_expanded_groups(quota) as counter:
        quota.reconcile_codex_quota_projection(now=now)

    assert counter.observations == 0, (
        f"a tick with nothing dirty loaded {counter.observations} observations")


def test_force_full_bypasses_the_ledger(primed):
    ns, quota, now = primed
    with count_expanded_groups(quota) as counter:
        quota.reconcile_codex_quota_projection(now=now, force_full=True)

    assert counter.groups == CLOSED_WINDOWS + 1
    assert counter.observations == (CLOSED_WINDOWS + 1) * PER_WINDOW


def test_an_interpretation_version_bump_queues_a_complete_pass(primed):
    """A classification change alters interpreted keys with NO row mutation for
    the ledger to observe, so the ledger cannot see it at all."""
    ns, quota, now = primed
    conn = ns["open_db"]()
    try:
        conn.execute(
            "UPDATE quota_projection_ledger_state SET interpretation_version=? "
            "WHERE source='codex'", (9999,))
        conn.commit()
    finally:
        conn.close()

    with count_expanded_groups(quota) as counter:
        quota.reconcile_codex_quota_projection(now=now)

    assert counter.groups == CLOSED_WINDOWS + 1


def test_a_reset_ledger_forces_a_complete_pass(primed):
    """A deleted and recreated cache restarts ``AUTOINCREMENT`` at 1, so the
    stored watermark now points past entries describing different mutations."""
    ns, quota, now = primed
    _set_watermark(ns, 10 ** 9)

    with count_expanded_groups(quota) as counter:
        quota.reconcile_codex_quota_projection(now=now)

    assert counter.groups == CLOSED_WINDOWS + 1


def test_a_block_without_a_reverse_map_forces_a_complete_pass(primed):
    """A scoped sweep matches on ``physical_group_key``; a NULL there would
    silently escape it and the stale block would survive indefinitely."""
    ns, quota, now = primed
    conn = ns["open_db"]()
    try:
        conn.execute(
            "UPDATE quota_window_blocks SET physical_group_key=NULL "
            "WHERE id=(SELECT MIN(id) FROM quota_window_blocks)")
        conn.commit()
    finally:
        conn.close()
    append_one_observation_to_live_window(ns)

    with count_expanded_groups(quota) as counter:
        quota.reconcile_codex_quota_projection(now=now)

    assert counter.groups == CLOSED_WINDOWS + 1


# ── the periodic verification ──────────────────────────────────────────────

def _set_last_full_pass_at(ns, value):
    conn = ns["open_db"]()
    try:
        conn.execute(
            "UPDATE quota_projection_ledger_state SET last_full_pass_at=? "
            "WHERE source='codex'", (value,))
        conn.commit()
    finally:
        conn.close()


def test_a_full_pass_stamps_the_verification_deadline(primed):
    """Every full pass stamps it, whatever triggered it (spec §2).

    The deadline is therefore satisfied by whichever caller reaches it first —
    a dashboard tick or a `codex quota` invocation pays the cost off the hook
    path entirely.
    """
    ns, _quota, now = primed
    state = _ledger_state(ns)
    assert state["last_full_pass_at"], (
        "the priming full pass left the deadline unstamped, so every later "
        "tick would be due forever")
    from _cctally_quota import _parse_utc
    stamped = _parse_utc(state["last_full_pass_at"], "last_full_pass_at")
    assert abs((stamped - now).total_seconds()) < 1.0


def test_a_stale_deadline_runs_full_with_an_empty_ledger(primed):
    """The time-based full pass, which is what bounds staleness at one interval.

    Two blind spots the scoped sweep structurally cannot see — a block whose
    physical group is absent from the cache entirely, and a milestone on a
    historic root no longer active — are otherwise repairable only by an
    interpretation bump, a rebuild or a burst overflow. On a normal install none
    of those happen, so without this "eventually consistent" degrades to
    "consistent when the operator runs `db rebuild`".

    Nothing is dirty here: the ledger is empty and the certificate is current,
    so the pass has to defeat the short-circuit as well as the ledger — and it
    must, because a skipped pass never stamps the deadline and every subsequent
    tick would be due forever.
    """
    ns, quota, now = primed
    _set_last_full_pass_at(
        ns, quota._utc_iso(now - dt.timedelta(
            seconds=quota.CODEX_QUOTA_FULL_VERIFICATION_INTERVAL_SECONDS + 60)))

    with count_expanded_groups(quota) as counter:
        quota.reconcile_codex_quota_projection(now=now)

    assert counter.groups == CLOSED_WINDOWS + 1, (
        f"the overdue verification pass expanded {counter.groups} groups")
    assert _ledger_state(ns)["last_full_pass_at"] == quota._utc_iso(now)


def test_an_in_interval_tick_stays_incremental(primed):
    """The other half: a fresh stamp must NOT force a full pass.

    Without this the interval would be indistinguishable from "always full",
    which is the behaviour the whole change removes.
    """
    ns, quota, now = primed
    _set_last_full_pass_at(
        ns, quota._utc_iso(now - dt.timedelta(
            seconds=quota.CODEX_QUOTA_FULL_VERIFICATION_INTERVAL_SECONDS // 2)))
    append_one_observation_to_live_window(ns)

    with count_expanded_groups(quota) as counter:
        quota.reconcile_codex_quota_projection(now=now)

    assert counter.groups == 1, (
        f"an in-interval tick expanded {counter.groups} groups")


def test_a_full_pass_from_any_cause_resets_the_deadline(primed):
    """`force_full` is not the only trigger, and none of them may skip the stamp.

    A rebuild, an interpretation bump and a burst overflow all take the full
    path; if only the interval-triggered pass stamped, an install whose ledger
    bursts daily would still pay a second full pass for the deadline.
    """
    ns, quota, now = primed
    _set_last_full_pass_at(
        ns, quota._utc_iso(now - dt.timedelta(days=30)))

    quota.reconcile_codex_quota_projection(now=now, force_full=True)
    assert _ledger_state(ns)["last_full_pass_at"] == quota._utc_iso(now)

    append_one_observation_to_live_window(ns)
    with count_expanded_groups(quota) as counter:
        quota.reconcile_codex_quota_projection(now=now)
    assert counter.groups == 1, (
        "the forced full pass did not satisfy the deadline")


# ── the deferred verification worker ───────────────────────────────────────

@pytest.fixture()
def spawns(monkeypatch):
    """Record every detached-worker spawn instead of forking one."""
    import _cctally_update

    seen = []
    monkeypatch.setattr(
        _cctally_update, "_spawn_detached",
        lambda command: seen.append(command) or True)
    return seen


def _overdue(ns, quota, now, *, days=2):
    _set_last_full_pass_at(
        ns, quota._utc_iso(now - dt.timedelta(days=days)))


def test_the_hook_hands_an_overdue_verification_to_a_worker(primed, spawns):
    """Acceptance criterion 3, for the install that filed the bug.

    "Whichever caller reaches the deadline first" assumes a caller other than
    the hook exists. On a hook-only install — the reporter's shape — none does,
    so once a day the whole-history reconcile would land on the blocking hook
    path: 14-30 seconds against Codex's 30-second timeout, which is exactly the
    stall this change removes from every other turn.
    """
    ns, quota, now = primed
    _overdue(ns, quota, now)
    stale = _ledger_state(ns)["last_full_pass_at"]
    append_one_observation_to_live_window(ns)

    with count_expanded_groups(quota) as counter:
        quota.reconcile_codex_quota_projection(now=now, full_pass="defer")

    assert spawns == [quota.CODEX_QUOTA_VERIFY_COMMAND]
    assert counter.groups == 1, (
        f"the deferring tick still expanded {counter.groups} groups — the "
        f"whole-history pass ran on the hook path anyway")
    assert _ledger_state(ns)["last_full_pass_at"] == stale, (
        "the deadline was stamped by a pass that never ran, so the worker's "
        "verification is now skipped for a whole interval")


def test_a_failed_spawn_skips_the_verification_rather_than_running_it_inline(
    primed, monkeypatch,
):
    """A missed daily verification is bounded staleness; a 30s hook stall is not.

    The deadline is stamped only by a pass that completes, so nothing is lost
    permanently: this tick skips, the next one is still due and retries.
    """
    ns, quota, now = primed
    import _cctally_update
    monkeypatch.setattr(_cctally_update, "_spawn_detached", lambda command: False)
    _overdue(ns, quota, now)
    stale = _ledger_state(ns)["last_full_pass_at"]
    append_one_observation_to_live_window(ns)

    with count_expanded_groups(quota) as counter:
        quota.reconcile_codex_quota_projection(now=now, full_pass="defer")

    assert counter.groups == 1, (
        "a failed spawn fell back to running the unbounded pass inline")
    assert _ledger_state(ns)["last_full_pass_at"] == stale


def test_the_spawn_is_throttled_on_attempt_not_on_success(primed, spawns):
    """One worker per window, however many ticks read the deadline as due.

    `last_full_pass_at` moves only when a pass COMMITS, so every tick between
    the spawn and the worker's commit still reads as due. Throttling on success
    would therefore put one worker per hook tick on the box — and the Codex
    lifecycle throttle is 15 seconds.
    """
    ns, quota, now = primed
    _overdue(ns, quota, now)

    for _ in range(4):
        quota.reconcile_codex_quota_projection(now=now, full_pass="defer")

    assert spawns == [quota.CODEX_QUOTA_VERIFY_COMMAND]
    marker = (
        ns["APP_DIR"] / quota.CODEX_QUOTA_VERIFY_MARKER_NAME)
    assert marker.exists(), "the throttle marker was never stamped"


def test_an_unwritable_throttle_marker_does_not_spawn(primed, spawns, monkeypatch):
    """Without the marker the spawn rate is unbounded, which is worse.

    Failing closed here costs one deferred verification; failing open costs a
    worker per hook tick with no way to stop.
    """
    ns, quota, now = primed
    _overdue(ns, quota, now)

    import pathlib
    real_touch = pathlib.Path.touch

    def _refuse(self, *args, **kwargs):
        if self.name == quota.CODEX_QUOTA_VERIFY_MARKER_NAME:
            raise OSError("read-only")
        return real_touch(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "touch", _refuse)
    quota.reconcile_codex_quota_projection(now=now, full_pass="defer")

    assert spawns == []


def test_the_worker_runs_the_full_pass_and_stamps_the_deadline(primed):
    """What the hook handed off has to actually happen.

    Reporting only — the worker holds no per-root lifecycle lock, so it passes
    no alert-eligible roots, exactly like the dashboard tick and `codex quota`.
    """
    ns, quota, now = primed
    _overdue(ns, quota, now)
    stale = _ledger_state(ns)["last_full_pass_at"]

    with count_expanded_groups(quota) as counter:
        assert quota.cmd_codex_quota_verify_internal(None) == 0

    assert counter.groups == CLOSED_WINDOWS + 1, (
        f"the worker expanded {counter.groups} groups — it did not run a "
        f"whole-history pass")
    stamped = _ledger_state(ns)["last_full_pass_at"]
    assert stamped and stamped != stale


def test_a_failed_hand_off_is_logged_and_a_throttled_one_is_not(primed, monkeypatch):
    """The return value was documented "for the log" and never logged, and no
    caller distinguishes the three outcomes — every one of them skips the pass.
    So a hand-off that can never succeed was invisible. `throttled` stays quiet:
    it is the ordinary state between a spawn and the worker's commit, at a
    15-second lifecycle cadence."""
    ns, quota, now = primed
    import _cctally_core
    import _cctally_update
    log = _cctally_core.HOOK_TICK_LOG_PATH

    monkeypatch.setattr(_cctally_update, "_spawn_detached", lambda command: False)
    assert quota._defer_codex_quota_verification() == "failed"
    assert "op=quota-verify-spawn result=failed reason=spawn" in log.read_text()

    monkeypatch.setattr(_cctally_update, "_spawn_detached", lambda command: True)
    assert quota._defer_codex_quota_verification() == "throttled"
    assert log.read_text().count("op=quota-verify-spawn") == 1

    (_cctally_core.APP_DIR / quota.CODEX_QUOTA_VERIFY_MARKER_NAME).unlink()
    assert quota._defer_codex_quota_verification() == "spawned"
    assert "op=quota-verify-spawn result=spawned" in log.read_text()


def test_the_worker_records_its_outcome_instead_of_swallowing_it(primed):
    """All three of the worker's streams are `/dev/null` and its exit code is
    unobserved, so a bare `except: pass` made a persistently failing
    verification completely invisible — and because the deadline only moves
    when a pass COMMITS, such a worker respawns every throttle window forever
    with nothing to show for it. Follow the `_update-check` precedent."""
    ns, quota, now = primed
    import _cctally_core
    log = _cctally_core.HOOK_TICK_LOG_PATH

    assert quota.cmd_codex_quota_verify_internal(None) == 0
    assert "op=quota-verify result=success" in log.read_text()

    def _boom(**kwargs):
        raise sqlite3.OperationalError("database is locked")

    real = quota.reconcile_codex_quota_projection
    quota.reconcile_codex_quota_projection = _boom
    try:
        assert quota.cmd_codex_quota_verify_internal(None) == 0
    finally:
        quota.reconcile_codex_quota_projection = real

    text = log.read_text()
    assert "op=quota-verify result=error" in text
    assert "error=OperationalError: database is locked" in text
    assert "Traceback" not in text


def test_the_worker_line_is_defused_by_its_renderer_not_by_its_caller(primed):
    """Where the scrub lives decides whether the guarantee survives the next
    caller.

    `_codex_lifecycle_log_line` defuses its own `error` field, so no caller can
    reintroduce a path or a field separator by forgetting to. The detached
    workers' lines were the exception — they appended an already-scrubbed
    `detail` verbatim, which reads as a chokepoint but is really a convention.
    Hand the renderer raw free text and it must still come out safe.
    """
    _ns, quota, _now = primed
    import _cctally_core
    log = _cctally_core.HOOK_TICK_LOG_PATH

    quota._log_codex_worker_outcome(
        "quota-verify", "error", "dur_ms=9",
        error="boom at /Users/someone/.codex/sessions/"
              "rollout-0199aa11-bb22-cc33-dd44-ee55ff667788.jsonl\n"
              "result=success provider=claude")

    line = [
        entry for entry in log.read_text().splitlines()
        if "op=quota-verify result=error" in entry and "boom" in entry
    ][-1]
    assert "someone" not in line and "/Users/" not in line, (
        "the renderer emitted a caller's raw free text, so the path scrub is a "
        "convention every future call site has to remember")
    assert "0199aa11" not in line, "the conversation id reached a durable log"
    assert "result=success" not in line and "provider=claude" not in line, (
        "raw free text impersonated a fixed field, which the last-wins `k=v` "
        "reader resolves in the FREE TEXT's favour")
    assert line.count("\n") == 0 and "dur_ms=9" in line


def test_every_other_caller_still_verifies_inline(primed):
    """`inline` is the default, and it must stay the behaviour it names.

    The dashboard, `cache-sync` and every `codex quota` invocation pay the
    verification on their own thread; deferring for them would move an
    already-off-hook cost into a process nobody is waiting on and give the
    deadline two owners.
    """
    ns, quota, now = primed
    _overdue(ns, quota, now)
    append_one_observation_to_live_window(ns)

    with count_expanded_groups(quota) as counter:
        quota.reconcile_codex_quota_projection(now=now)

    assert counter.groups == CLOSED_WINDOWS + 1
    assert _ledger_state(ns)["last_full_pass_at"] == quota._utc_iso(now)


def test_an_explicit_force_full_still_runs_inline_under_defer(primed, spawns):
    """`force_full` is a programmatic "do it now", not a schedule.

    The hook never passes it — every hook caller relies on the rule above — so
    honouring it inline cannot put a whole-history pass on the blocking path,
    and the equivalence oracle and the rebuild path both need it to mean what
    it says.
    """
    ns, quota, now = primed
    _overdue(ns, quota, now)

    with count_expanded_groups(quota) as counter:
        quota.reconcile_codex_quota_projection(
            now=now, force_full=True, full_pass="defer")

    assert counter.groups == CLOSED_WINDOWS + 1
    assert _ledger_state(ns)["last_full_pass_at"] == quota._utc_iso(now)


@pytest.mark.parametrize("route", [
    "no_projector_state", "interpretation_bump", "reset_ledger",
    "stale_reverse_map",
])
def test_the_hook_never_runs_a_whole_history_pass_inline(primed, spawns, route):
    """The rule, across every route `_resolve_pass_scope` has into a full pass.

    The old gate deferred ONLY the interval, and only when the projector state
    was already current — so a rebuilt stats index (which this feature forces on
    every upgrading install, by bumping `STATS_INDEX_EPOCH`) ran the entire
    whole-history pass on the blocking hook path, on the very tick where the
    index was also being rebuilt. If that tick exceeds Codex's 30-second kill,
    `run_stats_ingest` commits nothing, `last_full_pass_at` is never stamped,
    and the next tick repeats it: the reported defect, delivered by the fix.

    Deferring leaves the projection transiently missing rather than merely
    stale, which is accepted — the worker converges it, every non-hook caller
    still runs inline, and a 30-second blocking tick is not an option.
    """
    ns, quota, now = primed
    conn = ns["open_db"]()
    try:
        if route == "no_projector_state":
            conn.execute("DELETE FROM quota_projection_ledger_state")
        elif route == "interpretation_bump":
            conn.execute(
                "UPDATE quota_projection_ledger_state "
                "SET interpretation_version=9999 WHERE source='codex'")
        elif route == "reset_ledger":
            conn.execute(
                "UPDATE quota_projection_ledger_state SET watermark_seq=? "
                "WHERE source='codex'", (10 ** 9,))
        else:
            conn.execute(
                "UPDATE quota_window_blocks SET physical_group_key=NULL "
                "WHERE id=(SELECT MIN(id) FROM quota_window_blocks)")
        conn.commit()
    finally:
        conn.close()
    append_one_observation_to_live_window(ns)

    with count_expanded_groups(quota) as counter:
        quota.reconcile_codex_quota_projection(now=now, full_pass="defer")

    assert counter.groups == 0, (
        f"the {route} route ran a {counter.groups}-group whole-history pass on "
        f"the blocking hook path")
    assert spawns == [quota.CODEX_QUOTA_VERIFY_COMMAND], (
        f"the {route} route deferred the pass to nobody")


def test_a_ledgerless_cache_also_defers_on_the_hook_path(primed, spawns):
    """`ledger_high is None` is the fifth route, and it is unconditional: a
    cache too old to carry the change log makes EVERY pass whole-history."""
    ns, quota, now = primed
    conn = ns["open_cache_db"]()
    try:
        conn.execute("DROP TABLE quota_window_change_log")
        conn.commit()
    finally:
        conn.close()

    with count_expanded_groups(quota) as counter:
        quota.reconcile_codex_quota_projection(now=now, full_pass="defer")

    assert counter.groups == 0
    assert spawns == [quota.CODEX_QUOTA_VERIFY_COMMAND]


def test_a_cache_with_no_change_log_still_stamps_the_deadline(primed):
    """A ledgerless cache must not read as permanently overdue.

    `_ledger_max_seq` returns None when the change log table is absent, and the
    stamp used to be guarded on that value — so such a store wrote no state row
    at all and `_full_verification_due` answered True forever, for a pass it had
    just run. The outcome was harmless (no ledger means every pass is full) but
    the deadline stopped being a usable signal.
    """
    ns, quota, now = primed
    conn = ns["open_cache_db"]()
    try:
        conn.execute("DROP TABLE quota_window_change_log")
        conn.commit()
    finally:
        conn.close()
    conn = ns["open_db"]()
    try:
        conn.execute("DELETE FROM quota_projection_ledger_state")
        conn.commit()
    finally:
        conn.close()

    quota.reconcile_codex_quota_projection(now=now)

    state = _ledger_state(ns)
    assert state is not None, "a ledgerless cache stamped no state row at all"
    assert state["last_full_pass_at"] == quota._utc_iso(now)
    assert state["watermark_seq"] == 0
    assert not quota._full_verification_due(
        {"last_full_pass_at": state["last_full_pass_at"]}, now)


def test_reconcile_rejects_an_unknown_full_pass_mode(primed):
    ns, quota, now = primed
    with pytest.raises(ValueError):
        quota.reconcile_codex_quota_projection(now=now, full_pass="sometimes")


# ── the composable signature ───────────────────────────────────────────────

def test_the_stored_signature_equals_the_one_computed_from_observations(primed):
    """The composition has to agree with itself from either side.

    The projector composes a root's signature from the per-group digests stored
    on its blocks; a caller holding observations composes it from those. If the
    two ever disagree, the cache certificate and
    ``_stats_projection_signatures_match`` start comparing different functions
    and the reconcile either short-circuits when it should not or never
    short-circuits at all.
    """
    ns, quota, _now = primed
    observations = quota.load_codex_quota_observations(
        source_root_keys={ROOT_KEY})
    expected = quota._signature(observations, ROOT_KEY)

    conn = ns["open_db"]()
    try:
        stored = {
            str(row[0]) for row in conn.execute(
                "SELECT physical_signature FROM quota_projection_state "
                " WHERE source_root_key=?", (ROOT_KEY,))
        }
    finally:
        conn.close()

    assert stored == {expected}


def test_a_bounded_pass_reproduces_the_whole_history_signature(primed):
    """The property the plan actually needs.

    A whole-root digest is something a bounded pass cannot recompute; the point
    of making it associative is that it does not have to. Append one
    observation, let the ledger bound the pass, and the root value must equal
    what a complete pass over the same store produces.
    """
    ns, quota, now = primed
    append_one_observation_to_live_window(ns)
    with count_expanded_groups(quota) as counter:
        quota.reconcile_codex_quota_projection(now=now)
    # SELF-GUARDING. The equality below holds trivially if this arm quietly
    # loaded everything, so the test could not otherwise catch the regression
    # it is named for.
    assert counter.groups == 1, (
        f"the 'bounded' arm expanded {counter.groups} groups, so it proves "
        f"nothing about composing a root signature from stored digests")
    conn = ns["open_db"]()
    try:
        bounded = conn.execute(
            "SELECT DISTINCT physical_signature FROM quota_projection_state "
            " WHERE source_root_key=?", (ROOT_KEY,)).fetchall()
    finally:
        conn.close()

    quota.reconcile_codex_quota_projection(now=now, force_full=True)
    conn = ns["open_db"]()
    try:
        whole = conn.execute(
            "SELECT DISTINCT physical_signature FROM quota_projection_state "
            " WHERE source_root_key=?", (ROOT_KEY,)).fetchall()
    finally:
        conn.close()

    assert len(bounded) == 1
    assert bounded == whole


# ── the ledger beats the certificate ───────────────────────────────────────

def test_a_raw_sql_mutation_converges_even_without_a_sequence_bump(primed):
    """The property that makes this a mechanism rather than a discipline rule.

    Migration 028 rewrites ``observed_model`` and bumps no mutation sequence at
    all, and the journal cache applier commits quota rows without one either.
    The certificate therefore reads as CURRENT while real interpretation drift
    sits unprocessed — unless the short-circuit also refuses to fire while the
    ledger has unconsumed entries, which the triggers guarantee it knows about.
    """
    ns, quota, now = primed
    newest = _newest_reset(ns)
    conn = ns["open_cache_db"]()
    try:
        conn.execute(
            "UPDATE quota_window_snapshots SET used_percent=88.0 "
            " WHERE source='codex' "
            "   AND COALESCE(canonical_resets_at_utc, resets_at_utc)=? "
            "   AND line_offset=(SELECT MAX(line_offset) "
            "                      FROM quota_window_snapshots "
            "                     WHERE COALESCE(canonical_resets_at_utc, "
            "                                    resets_at_utc)=?)",
            (newest, newest))
        conn.commit()
        # Deliberately NOT bumping codex_physical_mutation_seq — that is the
        # whole point.
    finally:
        conn.close()

    quota.reconcile_codex_quota_projection(now=now)

    live = [b for b in _blocks(ns) if b["resets_at_utc"].startswith(newest[:19])]
    assert live, "the live block disappeared"
    assert any(b["current_percent"] == 88.0 for b in live), (
        f"the raw mutation never reached the projection: {live!r}")


# ── the scoped sweep ───────────────────────────────────────────────────────

def test_a_vanished_window_is_swept_and_its_neighbours_are_not(primed):
    """A group that has lost all its members is swept to nothing.

    That only works because the ledger records the OLD coordinates of a delete;
    the group contributes no observation, so nothing else could name it.
    """
    ns, quota, now = primed
    oldest = _oldest_reset(ns)
    conn = ns["open_cache_db"]()
    try:
        conn.execute(
            "DELETE FROM quota_window_snapshots WHERE source='codex' "
            " AND COALESCE(canonical_resets_at_utc, resets_at_utc)=?", (oldest,))
        conn.commit()
    finally:
        conn.close()

    quota.reconcile_codex_quota_projection(now=now)

    blocks = _blocks(ns)
    swept = [b for b in blocks if b["resets_at_utc"].startswith(oldest[:19])]
    survivors = [b for b in blocks if not b["resets_at_utc"].startswith(oldest[:19])]
    assert swept and all(b["orphaned_at"] is not None for b in swept)
    assert survivors and all(b["orphaned_at"] is None for b in survivors), (
        "the scoped sweep orphaned windows outside the dirty groups")


def test_a_milestone_threshold_disappearing_inside_a_live_window_is_swept(primed):
    """The child class a block-only set difference would miss entirely.

    The window is still present and its block is still re-materialized, so no
    block-level difference exists — but its ladder lost a rung, and only the
    generation sweep notices.
    """
    ns, quota, now = primed
    newest = _newest_reset(ns)
    conn = ns["open_cache_db"]()
    try:
        # Drop the window's high-percent tail so its top milestones no longer
        # have evidence behind them.
        conn.execute(
            "DELETE FROM quota_window_snapshots WHERE source='codex' "
            " AND COALESCE(canonical_resets_at_utc, resets_at_utc)=? "
            " AND used_percent > 3.0", (newest,))
        conn.commit()
    finally:
        conn.close()

    quota.reconcile_codex_quota_projection(now=now)

    conn = ns["open_db"]()
    try:
        rows = list(conn.execute(
            "SELECT percent_threshold, orphaned_at FROM quota_percent_milestones "
            " WHERE source='codex' AND resets_at_utc LIKE ? "
            " ORDER BY percent_threshold", (newest[:19] + "%",)))
    finally:
        conn.close()

    assert rows, "the window lost its whole ladder, which is not this scenario"
    orphaned = [r[0] for r in rows if r[1] is not None]
    assert orphaned, "a milestone whose evidence vanished was not swept"


def test_a_departed_roots_blocks_are_still_swept(tmp_path, monkeypatch):
    """Liveness narrows the LOADER, never the sweep (spec §2).

    A dirty unit names a group to sweep even when its root has left
    ``codex_source_roots`` — that is precisely the case where its blocks must be
    orphaned. Filtering the unit set by liveness drops the removed root's
    ledgered deletions while still pruning their ledger entries, which strands
    those blocks permanently in ``_historic_root_keys``, the projection state and
    the dashboard. That is a regression against the pre-change
    ``_orphan_unseen``, which swept every historic root on every pass.
    """
    ns, quota = _load(tmp_path, monkeypatch)
    build_codex_quota_store(
        ns, closed_windows=3, observations_per_window=PER_WINDOW)
    build_codex_quota_store(
        ns, closed_windows=3, observations_per_window=PER_WINDOW,
        root_key=SECOND_ROOT_KEY)
    now = store_as_of(ns)
    quota.reconcile_codex_quota_projection(now=now)

    def _root_blocks(root_key):
        conn = ns["open_db"]()
        try:
            return [
                (str(row[0]), row[1]) for row in conn.execute(
                    "SELECT resets_at_utc, orphaned_at FROM quota_window_blocks "
                    " WHERE source='codex' AND source_root_key=?", (root_key,))
            ]
        finally:
            conn.close()

    assert _root_blocks(SECOND_ROOT_KEY), "the second root materialized nothing"

    # The root leaves: its rollouts are gone from the tree, so the orphan prune
    # removed both its observations and its `codex_source_roots` row. Deleting
    # the snapshots is what the ledger records; deleting the root row is what
    # takes it out of `active_roots`.
    conn = ns["open_cache_db"]()
    try:
        conn.execute(
            "DELETE FROM quota_window_snapshots WHERE source_root_key=?",
            (SECOND_ROOT_KEY,))
        conn.execute(
            "DELETE FROM codex_source_roots WHERE source_root_key=?",
            (SECOND_ROOT_KEY,))
        conn.commit()
    finally:
        conn.close()

    quota.reconcile_codex_quota_projection(now=now)

    departed = _root_blocks(SECOND_ROOT_KEY)
    survivors = _root_blocks(ROOT_KEY)
    assert departed, "the departed root's blocks vanished; this tests orphaning"
    assert all(orphaned is not None for _reset, orphaned in departed), (
        "a departed root's blocks were never swept — the liveness filter "
        "narrowed the sweep set, not just the loader request")
    assert survivors and all(
        orphaned is None for _reset, orphaned in survivors), (
        "the surviving root's blocks were orphaned by the departure")


def test_a_threshold_event_is_unorphaned_when_its_block_returns(tmp_path, monkeypatch):
    """Terminal evidence is marked, never recreated.

    Its orphan marker tracks whether the stable source block is present in the
    completed generation, so restoring the exact window clears a transient prune
    marker without minting a new terminal claim.
    """
    ns, quota = _load(tmp_path, monkeypatch)
    build_codex_quota_store(
        ns, closed_windows=4, observations_per_window=PER_WINDOW)
    enable_quota_alerts(ns, actual=(2,))
    now = store_as_of(ns, before_reset=dt.timedelta(minutes=10))
    eligible = {ROOT_KEY}
    quota.reconcile_codex_quota_projection(
        now=now, alert_eligible_root_keys=eligible)

    conn = ns["open_db"]()
    try:
        events = conn.execute(
            "SELECT COUNT(*) FROM quota_threshold_events").fetchone()[0]
    finally:
        conn.close()
    assert events, "the fixture produced no terminal evidence to sweep"

    oldest = _oldest_reset(ns)
    removed = []
    conn = ns["open_cache_db"]()
    try:
        conn.row_factory = sqlite3.Row
        removed = [dict(r) for r in conn.execute(
            "SELECT * FROM quota_window_snapshots WHERE source='codex' "
            " AND COALESCE(canonical_resets_at_utc, resets_at_utc)=?", (oldest,))]
        conn.execute(
            "DELETE FROM quota_window_snapshots WHERE source='codex' "
            " AND COALESCE(canonical_resets_at_utc, resets_at_utc)=?", (oldest,))
        conn.commit()
    finally:
        conn.close()
    quota.reconcile_codex_quota_projection(
        now=now, alert_eligible_root_keys=eligible)

    conn = ns["open_db"]()
    try:
        after_delete = list(conn.execute(
            "SELECT threshold, orphaned_at FROM quota_threshold_events "
            " WHERE resets_at_utc LIKE ?", (oldest[:19] + "%",)))
    finally:
        conn.close()

    # Restore the exact rows and reconcile again.
    conn = ns["open_cache_db"]()
    try:
        for row in removed:
            columns = [k for k in row if k != "id"]
            conn.execute(
                "INSERT INTO quota_window_snapshots (" + ",".join(columns) + ") "
                "VALUES (" + ",".join("?" for _ in columns) + ")",
                tuple(row[c] for c in columns))
        conn.commit()
    finally:
        conn.close()
    quota.reconcile_codex_quota_projection(
        now=now, alert_eligible_root_keys=eligible)

    conn = ns["open_db"]()
    try:
        after_restore = list(conn.execute(
            "SELECT threshold, orphaned_at FROM quota_threshold_events "
            " WHERE resets_at_utc LIKE ?", (oldest[:19] + "%",)))
        total = conn.execute(
            "SELECT COUNT(*) FROM quota_threshold_events").fetchone()[0]
    finally:
        conn.close()

    assert after_delete and all(row[1] is not None for row in after_delete), (
        "a terminal event whose block vanished kept a clean marker")
    assert after_restore and all(row[1] is None for row in after_restore), (
        "restoring the exact block did not clear the transient prune marker")
    assert total == events, "a terminal event was recreated rather than marked"


if __name__ == "__main__":  # pragma: no cover - convenience
    raise SystemExit(pytest.main([__file__, "-v"]))
