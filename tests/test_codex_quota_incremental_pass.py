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
    IL_NOW,
    IL_PATH,
    IL_PATH_B,
    ROOT_KEY,
    append_one_observation_to_live_window,
    build_codex_quota_store,
    canonical_projection_dump,
    count_expanded_groups,
    enable_quota_alerts,
    interleave_blocks,
    interleave_capture,
    interleave_reset,
    scope_recorder,
    seed_interleave_root,
    seed_interleave_row,
    store_as_of,
)
from conftest import as_process_owner  # noqa: E402


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


@pytest.fixture()
def empty_store(tmp_path, monkeypatch):
    """A fresh install: both stores exist, neither holds a Codex quota row.
    Requested BEFORE ``spawns``, whose patch the script load would discard."""
    ns, quota = _load(tmp_path, monkeypatch)
    ns["open_cache_db"]().close()
    ns["open_db"]().close()
    return ns, quota


def test_an_empty_store_is_current_on_the_hook_path(empty_store, spawns):
    """Detects a fresh install the hook never acknowledges (#1069 Q4).

    With no projector state yet, the catch-all route handed the whole-history
    pass of an EMPTY store to the worker as ``deferred_verify``, and the hook
    withholds lifecycle success on that status — so a new install's root stayed
    due on every turn and spawned a verification of nothing each throttle
    window. An empty store owes nothing: it is ``current`` and stamps nothing,
    so the first observation is still met by a never-verified store.
    """
    ns, quota = empty_store
    now = IL_NOW

    result = quota.reconcile_codex_quota_projection(
        source_root_keys=[ROOT_KEY], alert_eligible_root_keys=[ROOT_KEY],
        now=now, full_pass="defer")

    assert result.status == quota.PROJECTION_CURRENT, result
    assert spawns == [], "an empty store handed a whole-history pass away"
    assert _ledger_state(ns) is None, "the empty pass stamped projector state"

    # Once observations arrive the very next hook pass is the never-verified
    # non-empty store it was before this rule: handed to the worker.
    build_codex_quota_store(ns, closed_windows=2, observations_per_window=3)
    result = quota.reconcile_codex_quota_projection(
        source_root_keys=[ROOT_KEY], alert_eligible_root_keys=[ROOT_KEY],
        now=store_as_of(ns), full_pass="defer")

    assert result.status == quota.PROJECTION_DEFERRED_VERIFY, result
    assert spawns == [quota.CODEX_QUOTA_VERIFY_COMMAND]


@pytest.mark.parametrize("kwargs", [
    {"full_pass": "inline"},
    {"full_pass": "defer", "force_full": True},
])
def test_an_empty_store_still_runs_inline_and_forced_passes(
    empty_store, spawns, kwargs,
):
    """The empty-store rule is the hook's deferral only: an inline or forced
    pass over an empty store still completes and stamps its deadline."""
    ns, quota = empty_store

    quota.reconcile_codex_quota_projection(
        source_root_keys=[ROOT_KEY], now=IL_NOW, **kwargs)

    state = _ledger_state(ns)
    assert state is not None and state["last_full_pass_at"] == quota._utc_iso(
        IL_NOW)
    assert spawns == []


@pytest.mark.parametrize("retained", [
    "quota_projection_state", "quota_window_blocks",
    "quota_percent_milestones",
])
def test_a_store_whose_observations_vanished_is_not_empty(
    primed, spawns, retained,
):
    """Detects stale projected rows certified as nothing owed: when the cache
    observations are gone but stats still holds a projected row, only a
    complete pass can orphan it, so the hook must still hand that pass to the
    worker rather than report the store ``current``."""
    ns, quota, now = primed
    conn = ns["open_cache_db"]()
    try:
        conn.execute("DELETE FROM quota_window_snapshots")
        conn.commit()
    finally:
        conn.close()
    conn = ns["open_db"]()
    try:
        assert conn.execute(f"SELECT COUNT(*) FROM {retained}").fetchone()[0]
        conn.execute("DELETE FROM quota_projection_ledger_state")
        for table in (
            "quota_projection_state", "quota_window_blocks",
            "quota_percent_milestones", "quota_threshold_events",
        ):
            if table != retained:
                conn.execute(f"DELETE FROM {table}")
        conn.commit()
    finally:
        conn.close()

    result = quota.reconcile_codex_quota_projection(now=now, full_pass="defer")

    assert result.status == quota.PROJECTION_DEFERRED_VERIFY, result
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


@pytest.mark.parametrize("re_prepare", [False, True],
                         ids=["optimistic", "in-lock"])
def test_a_bounded_preparation_reads_no_stats_inside_its_cache_session(
    primed, monkeypatch, re_prepare,
):
    """Detects a stats.db statement run inside the preparation's cache
    snapshot (#787 / #861 R_COVERAGE_USE). That snapshot is one cache query
    session, so for its whole duration the thread's authority is narrowed to
    ``query``; the published block spans the interleave closure needs (#1071
    Q20) were read lazily there, on the OWNER stats connection, which is an
    owner-connection statement under query authority. ``in-lock`` covers the
    re-preparation on the cycle's connection, which repeats both reads."""
    import _cctally_core

    ns, quota, now = primed
    append_one_observation_to_live_window(ns)
    statements: list = []
    original = quota._prepare_codex_quota_pass

    def traced(**kwargs):
        stats_conn = kwargs["stats_conn"]

        def trace(sql):
            authority = _cctally_core.current_authority()
            statements.append((
                bool(kwargs["in_lock"]),
                None if authority is None else authority.kind, sql))

        stats_conn.set_trace_callback(trace)
        try:
            return original(**kwargs)
        finally:
            stats_conn.set_trace_callback(None)

    monkeypatch.setattr(quota, "_prepare_codex_quota_pass", traced)
    if re_prepare:
        monkeypatch.setattr(
            quota, "_codex_prepared_pass_still_valid",
            lambda conn, prepared: False)
    with scope_recorder(quota) as scopes:
        result = quota.reconcile_codex_quota_projection(now=now)
    assert result.status == "published", result
    # SELF-GUARDING: only a bounded pass with seeds reads the spans.
    assert scopes and all(scope is not None for _kw, scope in scopes), scopes
    span_reads = {
        in_lock for in_lock, _kind, sql in statements
        if "quota_window_blocks" in sql}
    assert span_reads >= ({False, True} if re_prepare else {False}), (
        "premise: every preparation reads the published spans on its stats "
        "connection", statements)
    under_query = [entry for entry in statements if entry[1] == "query"]
    assert under_query == [], (
        "a stats statement ran inside the cache query session", under_query)


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


# ── #1069: a pass prepared before a newer publication never publishes over it ──
#
# The seam is the outer ``_cctally_journal.run_stats_ingest`` call: the
# reconcile resolves it through the module at call time, so a wrapper that runs
# H before calling the original runs H after P's preparation and before P holds
# the ingest lock. H is never run while the real lock is held.

def _env(tmp_path, monkeypatch):
    ns, quota = _load(tmp_path, monkeypatch)
    jr = importlib.import_module("_cctally_journal")
    jl = importlib.import_module("_lib_journal")
    return ns, quota, jr, jl


def _interleave_before_publication(monkeypatch, jr, interleave):
    """Run ``interleave`` once, inside the NEXT reconcile's ingest call, before
    the real cycle (and its lock) starts. Fires only for a cycle carrying
    ``codex_apply``, and only once, so a nested reconcile goes straight through."""
    original = jr.run_stats_ingest
    fired = {"done": False}

    def wrapper(*args, **kwargs):
        if kwargs.get("codex_apply") is not None and not fired["done"]:
            fired["done"] = True
            interleave()
        return original(*args, **kwargs)

    monkeypatch.setattr(jr, "run_stats_ingest", wrapper)
    return fired


def _record_preparations(monkeypatch, quota):
    """Every ``_prepare_codex_quota_pass`` call as ``(in_lock, disposition)``."""
    calls: list = []
    original = quota._prepare_codex_quota_pass

    def wrapper(**kwargs):
        prepared = original(**kwargs)
        calls.append((bool(kwargs["in_lock"]), prepared.disposition))
        return prepared

    monkeypatch.setattr(quota, "_prepare_codex_quota_pass", wrapper)
    return calls


def _in_lock_preparations(calls):
    return [disposition for in_lock, disposition in calls if in_lock]


def _watermark(ns):
    state = _ledger_state(ns)
    return None if state is None else int(state["watermark_seq"])


def _raw_projection(ns):
    """Every projection row EXACTLY, generation included: 'H's exact rows'."""
    conn = ns["open_db"]()
    try:
        return {
            table: [tuple(row) for row in conn.execute(
                f"SELECT * FROM {table} ORDER BY {order}")]
            for table, order in (
                ("quota_window_blocks", "id"),
                ("quota_percent_milestones", "id"),
                ("quota_threshold_events", "id"),
                ("quota_projection_state", "source_root_key, account_key"),
                ("quota_projection_ledger_state", "source"),
            )
        }
    finally:
        conn.close()


def _certificate_bytes(ns):
    conn = ns["open_cache_db"]()
    try:
        row = conn.execute(
            "SELECT value FROM cache_meta "
            " WHERE key='codex_quota_projection_certificate'").fetchone()
        return None if row is None else str(row[0])
    finally:
        conn.close()


def _oracle_dump(ns, quota, **kwargs):
    """A NON-wiping ``force_full`` pass over the same store: the serial
    whole-history answer, tombstones of a correct deletion included."""
    quota.reconcile_codex_quota_projection(force_full=True, **kwargs)
    return _dump(ns)


def _live_window_raw_max(ns):
    cache = ns["open_cache_db"]()
    try:
        return cache.execute(
            "SELECT COALESCE(canonical_resets_at_utc, resets_at_utc), "
            "       MAX(used_percent) FROM quota_window_snapshots "
            " WHERE source='codex' GROUP BY 1 ORDER BY 1 DESC LIMIT 1"
        ).fetchone()
    finally:
        cache.close()


def _live_window_percents(ns):
    newest_reset, _raw_max = _live_window_raw_max(ns)
    stats = ns["open_db"]()
    try:
        return [
            float(row[0]) for row in stats.execute(
                "SELECT current_percent FROM quota_window_blocks "
                " WHERE source='codex' AND orphaned_at IS NULL "
                "   AND unixepoch(resets_at_utc)=unixepoch(?)",
                (newest_reset,))
        ]
    finally:
        stats.close()


def _remove_validation(monkeypatch, quota):
    """The mutation of ``test_removing_validation_reintroduces_the_race``:
    the leg's witness comparison always reports 'equal'."""
    monkeypatch.setattr(
        quota, "_codex_witnesses_equal", lambda prepared, live: True)


def _primed_with_owed_work(tmp_path, monkeypatch, *, validation_removed=False):
    """A reconciled store plus one ledgered mutation, so the next pass is a
    real scoped publication rather than a certificate short-circuit."""
    ns, quota, jr, jl = _env(tmp_path, monkeypatch)
    if validation_removed:
        _remove_validation(monkeypatch, quota)
    build_codex_quota_store(ns, closed_windows=3, observations_per_window=10)
    now = store_as_of(ns)
    quota.reconcile_codex_quota_projection(now=now)
    append_one_observation_to_live_window(ns)
    return ns, quota, jr, jl, now


def test_a_slower_pass_does_not_publish_a_stale_group_over_a_newer_one(
    tmp_path, monkeypatch,
):
    """Detects #1069's lower-watermark form: a pass P that prepared group G
    before a newer pass H published G, published its OLDER G and its older
    watermark after H had pruned the ledger entries above it, leaving a stale
    live ``current_percent`` that no later scoped pass can repair.

    Asserted the moment P returns, before any later pass or oracle can repair
    the evidence.
    """
    ns, quota, jr, _jl, now = _primed_with_owed_work(tmp_path, monkeypatch)
    after_h: dict = {}

    def interleave():
        append_one_observation_to_live_window(ns)
        quota.reconcile_codex_quota_projection(now=now)  # H
        after_h["live"] = _live_window_percents(ns)
        after_h["watermark"] = _watermark(ns)
        after_h["ledger"] = _ledger_rows(ns)

    fired = _interleave_before_publication(monkeypatch, jr, interleave)
    quota.reconcile_codex_quota_projection(now=now)  # P
    assert fired["done"], "the interleaving seam never fired"
    _reset, raw_max = _live_window_raw_max(ns)
    # Premises: H published the newer G and pruned what it consumed, and the
    # cache holds a newer percent than P's preparation saw.
    assert after_h["live"] == [float(raw_max)], after_h
    assert after_h["ledger"][0] == 0, ("premise: H pruned the ledger", after_h)

    watermark = _watermark(ns)
    assert watermark >= after_h["watermark"], (
        f"P stamped watermark {watermark} below H's {after_h['watermark']}: "
        f"it published its older preparation after H pruned the entries above it")
    live = _live_window_percents(ns)
    assert live == [float(raw_max)], (
        f"the live window projects current_percent={live} but the cache holds "
        f"{raw_max}: P published its older preparation of G over H's newer one")


ROOT = "rk"
ACCT_A = "a" * 32
WEEK = 10_080
WEEK_KEY = (
    '{"limitId":"codex","observedSlot":"primary","source":"codex",'
    f'"sourceRootKey":"{ROOT}","windowMinutes":{WEEK}}}')
WEEK_RESET = dt.datetime(2026, 8, 5, 4, 35, 6, tzinfo=dt.timezone.utc)
WEEK_RAWS = (WEEK_RESET, WEEK_RESET + dt.timedelta(seconds=9))
WEEK_NOW = WEEK_RESET - dt.timedelta(days=1)


def _z(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _seed_week_window(ns, *, raws=WEEK_RAWS, offset_base=100):
    """The ``test_codex_window_attribution_projection`` window."""
    conn = ns["open_cache_db"]()
    try:
        conn.execute(
            "INSERT INTO codex_source_roots "
            "(source_root_key, canonical_root_path, first_seen_utc, "
            " last_seen_utc) VALUES (?,?,?,?) "
            "ON CONFLICT(source_root_key) DO UPDATE SET "
            "  last_seen_utc=excluded.last_seen_utc",
            (ROOT, f"/codex/{ROOT}", _z(WEEK_NOW), _z(WEEK_NOW)),
        )
        for index, raw in enumerate(raws):
            conn.execute(
                "INSERT INTO quota_window_snapshots "
                "(source, source_root_key, source_path, line_offset,"
                " captured_at_utc, observed_slot, logical_limit_key, limit_id,"
                " limit_name, window_minutes, used_percent, resets_at_utc,"
                " observed_model, account_key, canonical_resets_at_utc) "
                "VALUES ('codex',?,?,?,?,'primary',?,'codex',NULL,?,?,?,"
                "        'gpt-5',NULL,?)",
                (ROOT, f"/codex/{ROOT}/rollout.jsonl", offset_base + index,
                 _z(WEEK_RESET - dt.timedelta(days=6, hours=index)), WEEK_KEY,
                 WEEK, float(index + 1) * 5.0, _z(raw), _z(WEEK_RESET)),
            )
        conn.commit()
    finally:
        conn.close()


def _attribution_op(jl):
    return jl.make_codex_window_attribution(
        at="2026-08-14T00:00:00Z", account_key=ACCT_A, source_root_key=ROOT,
        logical_limit_key=WEEK_KEY, observed_slot="primary", window_minutes=WEEK,
        raw_resets_at_utc=[_z(w) for w in WEEK_RAWS],
        canonical_resets_at_utc=_z(WEEK_RESET),
    )


def _record_attribution_in_cache(ns, jr, jl):
    conn = ns["open_cache_db"]()
    try:
        conn.execute("BEGIN IMMEDIATE")
        jr._apply_window_attribution_records(conn, [_attribution_op(jl)])
        conn.commit()
    finally:
        conn.close()


def _live_block_accounts(ns):
    conn = ns["open_db"]()
    try:
        return [
            str(row[0]) for row in conn.execute(
                "SELECT account_key FROM quota_window_blocks "
                " WHERE source='codex' AND orphaned_at IS NULL "
                " ORDER BY account_key")
        ]
    finally:
        conn.close()


def _certified_and_live_revision(ns, quota):
    cache = ns["open_cache_db"]()
    try:
        payload = quota._codex_quota_projection_certificate_payload(cache)
        certified = (
            None if payload is None
            else quota._certificate_attribution_revision(payload))
        live = quota._cache_module().codex_window_attribution_revision(cache)
        return certified, live
    finally:
        cache.close()


def _week_window_with_owed_work(tmp_path, monkeypatch):
    ns, quota, jr, jl = _env(tmp_path, monkeypatch)
    _seed_week_window(ns)
    quota.reconcile_codex_quota_projection(now=WEEK_NOW)  # prime, revision 0
    assert _live_block_accounts(ns) == ["unattributed"]
    # One more raw row in the same window: a ledger entry P owes.
    _seed_week_window(
        ns, raws=(WEEK_RESET + dt.timedelta(seconds=15),), offset_base=300)
    return ns, quota, jr, jl


def test_a_stale_attribution_pass_does_not_mask_a_newer_certificate(
    tmp_path, monkeypatch,
):
    """Detects #1069's same-watermark attribution form: P, prepared under
    attribution revision 0, published at the same watermark after H published
    revision 1 and stored its revision-1 certificate. P's own certificate was
    declined, H's survived, and it certified owners P had just reverted to
    ``unattributed``, so every later pass trusted it and never reloaded.
    """
    ns, quota, jr, jl = _week_window_with_owed_work(tmp_path, monkeypatch)
    after_h: dict = {}

    def interleave():
        _record_attribution_in_cache(ns, jr, jl)
        quota.reconcile_codex_quota_projection(now=WEEK_NOW)  # H, revision 1
        after_h["accounts"] = _live_block_accounts(ns)
        after_h["watermark"] = _watermark(ns)
        after_h["revisions"] = _certified_and_live_revision(ns, quota)

    fired = _interleave_before_publication(monkeypatch, jr, interleave)
    quota.reconcile_codex_quota_projection(now=WEEK_NOW)  # P, revision 0
    assert fired["done"], "the interleaving seam never fired"
    # Premises: H published the asserted account under the advanced revision,
    # and P ended at the SAME watermark.
    assert after_h["accounts"] == [ACCT_A], after_h
    assert after_h["revisions"] == (1, 1), after_h
    assert _watermark(ns) == after_h["watermark"], after_h

    accounts = _live_block_accounts(ns)
    certified, live = _certified_and_live_revision(ns, quota)
    assert accounts == [ACCT_A], (
        f"live blocks are owned by {accounts} after P: its revision-0 apply at "
        f"the same watermark overwrote H's revision-1 projection")
    assert certified == live, (
        f"the stored certificate claims revision {certified} against the live "
        f"{live}")


def _identical_publication(tmp_path, monkeypatch, *, validation_removed=False):
    """P prepares owed work; H then publishes the SAME cache state (equal
    watermark and rows, a new generation) before P enters the ingest."""
    ns, quota, jr, _jl, now = _primed_with_owed_work(
        tmp_path, monkeypatch, validation_removed=validation_removed)
    calls = _record_preparations(monkeypatch, quota)
    snap: dict = {}

    def interleave():
        snap["h"] = quota.reconcile_codex_quota_projection(now=now)  # H
        snap["rows"] = _raw_projection(ns)
        snap["certificate"] = _certificate_bytes(ns)
        snap["ledger"] = _ledger_rows(ns)

    fired = _interleave_before_publication(monkeypatch, jr, interleave)
    result = quota.reconcile_codex_quota_projection(now=now)  # P
    assert fired["done"], "the interleaving seam never fired"
    assert snap["h"].status == "published", ("premise: H published", snap["h"])
    return ns, quota, calls, snap, result


def _case_identical_publication(tmp_path, monkeypatch, *,
                                validation_removed=False):
    ns, quota, calls, snap, result = _identical_publication(
        tmp_path, monkeypatch, validation_removed=validation_removed)
    assert _in_lock_preparations(calls) == ["current"], (
        f"a semantically identical publication by H went unnoticed: P's "
        f"in-lock preparations were {_in_lock_preparations(calls)}")
    assert result.status == "current", result


def test_an_identical_publication_between_preparation_and_apply_forces_re_preparation(
    tmp_path, monkeypatch,
):
    """Detects a witness that compares VALUES that an identical republication
    leaves equal: H publishing the same rows under a new generation is still a
    publication P's preparation never saw, so P must re-prepare."""
    _case_identical_publication(tmp_path, monkeypatch)


def test_a_re_preparation_to_current_writes_nothing(tmp_path, monkeypatch):
    """Detects a superseded pass that still writes: P re-prepared to
    ``current`` must leave H's exact rows (generation included), H's
    certificate and the ledger untouched."""
    ns, _quota, _calls, snap, result = _identical_publication(
        tmp_path, monkeypatch)
    assert result.status == "current", result
    assert _raw_projection(ns) == snap["rows"], "P rewrote H's projection"
    assert _certificate_bytes(ns) == snap["certificate"], (
        "P replaced or deleted H's certificate")
    assert _ledger_rows(ns) == snap["ledger"], "P pruned or changed the ledger"


@pytest.mark.parametrize("hook_pass_first", [True, False],
                         ids=["h_first", "p_first"])
def test_a_re_preparation_with_owed_work_is_serial_equivalent(
    tmp_path, monkeypatch, hook_pass_first,
):
    """Detects a re-prepared pass that applies something other than what a
    serial run would: after H publishes, a further mutation lands, so the
    superseded pass re-prepares to real owed work and must publish exactly the
    serial result. Both orders: ``h_first`` has an alert-eligible pass publish
    over a reporting pass's earlier preparation, ``p_first`` the reverse."""
    ns, quota, jr, _jl, now = _primed_with_owed_work(tmp_path, monkeypatch)
    calls = _record_preparations(monkeypatch, quota)
    eligible = {"alert_eligible_root_keys": {ROOT_KEY}}
    inner = eligible if hook_pass_first else {}
    outer = {} if hook_pass_first else eligible

    def interleave():
        assert quota.reconcile_codex_quota_projection(
            now=now, **inner).status == "published"
        append_one_observation_to_live_window(ns)

    _interleave_before_publication(monkeypatch, jr, interleave)
    result = quota.reconcile_codex_quota_projection(now=now, **outer)

    assert _in_lock_preparations(calls) == ["apply"], calls
    assert result.status == "published", result
    _reset, raw_max = _live_window_raw_max(ns)
    assert _live_window_percents(ns) == [float(raw_max)]
    incremental = _dump(ns)
    assert incremental == _oracle_dump(ns, quota, now=now)


def _interleave_endpoint(ns, window):
    (block,) = interleave_blocks(ns, window)
    return (
        dt.datetime.fromisoformat(block["last_observed_at_utc"]).timestamp(),
        block["last_source_path"], int(block["last_line_offset"]))


def test_a_re_preparation_recomputes_the_interleave_bounds_with_the_closure(
    tmp_path, monkeypatch,
):
    """Detects an in-lock re-preparation that reuses the optimistic
    preparation's published block spans (#1071 Q20). H (a complete pass)
    publishes W's
    interleaved reading W30 (W's span becomes [0, 30]) and then W30 is
    deleted: what connects W to D — whose readings W30 separated — is now only
    the span H published, so a closure computed from the spans read before H
    leaves D's endpoint where H put it, while the serial result moves it."""
    ns, quota, jr, _jl = _env(tmp_path, monkeypatch)
    conn = ns["open_cache_db"]()
    try:
        seed_interleave_root(conn)
        seed_interleave_row(conn, window=0, minute=0, percent=10.0, offset=1)
        seed_interleave_row(conn, window=1, minute=25, percent=5.0, offset=101)
        seed_interleave_row(conn, window=1, minute=35, percent=5.0, offset=102)
        conn.commit()
    finally:
        conn.close()

    def mutate(apply):
        conn = ns["open_cache_db"]()
        try:
            apply(conn)
            conn.execute(
                "INSERT INTO cache_meta(key, value) VALUES "
                "('codex_physical_mutation_seq', '1') ON CONFLICT(key) DO "
                "UPDATE SET value=CAST(value AS INTEGER) + 1")
            conn.commit()
        finally:
            conn.close()

    quota.reconcile_codex_quota_projection(now=IL_NOW)  # prime, whole history
    d25 = (interleave_capture(25).timestamp(), IL_PATH, 101)
    assert _interleave_endpoint(ns, 1) == d25, "fixture premise"
    mutate(lambda conn: seed_interleave_row(
        conn, window=0, minute=30, percent=10.0, offset=3, path=IL_PATH_B))
    after_h: dict = {}

    def interleave():
        # H is a complete pass, so what it publishes does not depend on the
        # closure under test.
        assert quota.reconcile_codex_quota_projection(
            now=IL_NOW, force_full=True).status == "published"  # H
        after_h["d"] = _interleave_endpoint(ns, 1)
        mutate(lambda conn: conn.execute(
            "DELETE FROM quota_window_snapshots "
            " WHERE source_path=? AND line_offset=3", (IL_PATH_B,)))

    calls = _record_preparations(monkeypatch, quota)
    fired = _interleave_before_publication(monkeypatch, jr, interleave)
    span_reads: list = []
    read_spans = quota._codex_historical_block_spans
    monkeypatch.setattr(
        quota, "_codex_historical_block_spans",
        lambda conn: span_reads.append(read_spans(conn)) or span_reads[-1])
    with scope_recorder(quota) as scopes:
        result = quota.reconcile_codex_quota_projection(now=IL_NOW)  # P
    assert fired["done"], "the interleaving seam never fired"
    assert _in_lock_preparations(calls) == ["apply"], calls
    assert result.status == "published", result
    assert after_h["d"] == (interleave_capture(35).timestamp(), IL_PATH, 102), (
        "premise: H's publication made D35 an interpreted point", after_h)
    assert _interleave_endpoint(ns, 1) == d25, (
        f"D kept {_interleave_endpoint(ns, 1)}: the re-prepared closure did not "
        f"reach D through the span H published")
    _in_lock_kwargs, in_lock_scope = scopes[-1]
    w_node = quota._reset_anchor_node(quota._utc_iso(interleave_reset(0)))
    # H is a complete pass and reads no spans, so the last read is the
    # in-lock re-preparation's.
    w_spans = [
        spans for nodes in span_reads[-1].by_family.values()
        for node, spans in nodes.items() if node == w_node]
    assert w_spans == [((interleave_capture(0).timestamp(),
                         interleave_capture(30).timestamp()),)], (
        "the re-preparation did not read the spans H published", w_spans)
    assert in_lock_scope["closure_added_unit_count"] == 1, in_lock_scope
    incremental = _dump(ns)
    assert incremental == _oracle_dump(ns, quota, now=IL_NOW)


def test_a_historic_only_publication_is_detected(tmp_path, monkeypatch):
    """Detects the no-active-root hole in the witness: a publication over a
    store whose only root is historic stamped no ``quota_projection_state``
    row, so it changed nothing the witness reads and P applied over it. Every
    reconciled root, historic ones included, is restamped now."""
    ns, quota, jr, _jl = _env(tmp_path, monkeypatch)
    build_codex_quota_store(ns, closed_windows=2, observations_per_window=5)
    now = store_as_of(ns)
    quota.reconcile_codex_quota_projection(now=now)
    conn = ns["open_cache_db"]()
    try:
        conn.execute("DELETE FROM codex_source_roots")
        conn.commit()
    finally:
        conn.close()
    eligible = {ROOT_KEY}
    # Reach the fixed point, so H changes nothing but a generation.
    for _ in range(2):
        quota.reconcile_codex_quota_projection(
            now=now, alert_eligible_root_keys=eligible)
    state_before = _ledger_state(ns)
    calls = _record_preparations(monkeypatch, quota)
    snap: dict = {}

    def interleave():
        snap["h"] = quota.reconcile_codex_quota_projection(
            now=now, alert_eligible_root_keys=eligible)
        snap["state"] = _ledger_state(ns)

    _interleave_before_publication(monkeypatch, jr, interleave)
    result = quota.reconcile_codex_quota_projection(
        now=now, alert_eligible_root_keys=eligible)

    assert snap["h"].status == "published", snap["h"]
    assert snap["state"] == state_before, (
        "premise: H changed only the historic root's completion generation")
    assert _in_lock_preparations(calls) == ["apply"], (
        f"P never noticed H's historic-only publication: {calls}")
    assert result.status == "published", result


def test_a_stats_rebuild_between_preparation_and_apply_re_prepares(
    tmp_path, monkeypatch,
):
    """Detects a witness blind to a replaced index: a stats rebuild between
    P's preparation and its apply publishes a new stats identity, and P must
    re-prepare against it rather than apply over the rebuilt projection."""
    ns, quota, jr, _jl, now = _primed_with_owed_work(tmp_path, monkeypatch)
    calls = _record_preparations(monkeypatch, quota)

    def interleave():
        jr.rebuild_stats_index(context=jr.RebuildContext(trigger="test-fixture"))

    fired = _interleave_before_publication(monkeypatch, jr, interleave)
    result = quota.reconcile_codex_quota_projection(now=now)
    assert fired["done"]
    assert len(_in_lock_preparations(calls)) == 1, calls
    assert result.status in ("published", "current"), result
    incremental = _dump(ns)
    assert incremental == _oracle_dump(ns, quota, now=now)


@pytest.mark.parametrize("change", ["attribution", "model_debt"])
def test_attribution_or_model_debt_from_the_cycles_step_3_re_prepares(
    tmp_path, monkeypatch, change,
):
    """Detects a validation that trusts preparation-time cache facts: the
    cycle's OWN step-3 cache leg materializes a journaled attribution (moving
    the revision) or leaves model-resolution debt, after P's preparation and
    before its apply. P must re-prepare: publish the attributed owners, or
    refuse publication while model debt stands."""
    ns, quota, jr, jl = _week_window_with_owed_work(tmp_path, monkeypatch)
    import _cctally_cache

    if change == "model_debt":
        # The attribution is already materialized and consumed; the seam's
        # re-appended identical op lands no row and moves no revision, so the
        # debt is the only change the cycle makes.
        jr.append_record(_attribution_op(jl))
        for _ in range(2):  # materialize, then converge
            quota.reconcile_codex_quota_projection(now=WEEK_NOW)
        assert _live_block_accounts(ns) == [ACCT_A]
        _seed_week_window(
            ns, raws=(WEEK_RESET + dt.timedelta(seconds=21),), offset_base=400)
        original_apply = jr._apply_window_attribution_records

        def apply_and_owe_models(cache, records):
            outcome = original_apply(cache, records)
            _cctally_cache.add_codex_finalization_debt(cache, model=True)
            return outcome

        monkeypatch.setattr(
            jr, "_apply_window_attribution_records", apply_and_owe_models)
    revision_before = _certified_and_live_revision(ns, quota)[1]
    rows_before = _raw_projection(ns)
    requested: list = []
    monkeypatch.setattr(
        quota, "_request_codex_model_resolution",
        lambda: requested.append("model"))
    calls = _record_preparations(monkeypatch, quota)
    fired = _interleave_before_publication(
        monkeypatch, jr, lambda: jr.append_record(_attribution_op(jl)))

    result = quota.reconcile_codex_quota_projection(now=WEEK_NOW)
    assert fired["done"]
    certified, live = _certified_and_live_revision(ns, quota)
    if change == "attribution":
        assert live == revision_before + 1, "premise: step 3 moved the revision"
        assert _in_lock_preparations(calls) == ["apply"], calls
        assert result.status == "published", result
        assert _live_block_accounts(ns) == [ACCT_A], (
            "P applied its revision-0 preparation over the attribution its own "
            "cycle materialized")
        assert certified == live
    else:
        assert live == revision_before, "premise: the revision did not move"
        assert _in_lock_preparations(calls) == ["deferred_model"], calls
        assert result.status == "deferred_model", result
        assert _raw_projection(ns) == rows_before, (
            "P published while model resolution was owed")
        assert requested == ["model"], requested


def _case_unreadable_witness(tmp_path, monkeypatch, *,
                             validation_removed=False):
    ns, quota, jr, _jl, now = _primed_with_owed_work(
        tmp_path, monkeypatch, validation_removed=validation_removed)
    calls = _record_preparations(monkeypatch, quota)
    monkeypatch.setattr(
        quota, "_codex_projection_witness",
        lambda stats_conn: quota._UnreadableWitness())
    result = quota.reconcile_codex_quota_projection(now=now)
    assert _in_lock_preparations(calls) == ["apply"], (
        f"an unreadable witness was trusted: {calls}")
    assert result.status == "published", result
    incremental = _dump(ns)
    assert incremental == _oracle_dump(ns, quota, now=now)


def test_an_unreadable_witness_re_prepares(tmp_path, monkeypatch):
    """Detects a witness read error treated as 'nothing changed': an
    unreadable witness must never compare equal, so the leg re-prepares."""
    _case_unreadable_witness(tmp_path, monkeypatch)


class _AbortCycle(Exception):
    """Rolls back one ingest attempt, as a failed first cycle does."""


def _case_recovery_retry(tmp_path, monkeypatch, *, validation_removed=False):
    ns, quota, jr, _jl, now = _primed_with_owed_work(
        tmp_path, monkeypatch, validation_removed=validation_removed)
    calls = _record_preparations(monkeypatch, quota)
    original = jr.run_stats_ingest
    seen = {"legs": 0, "done": False}
    after_h: dict = {}

    def retrying(*args, **kwargs):
        leg = kwargs.get("codex_apply")
        if leg is None or seen["done"]:
            return original(*args, **kwargs)
        seen["done"] = True

        def first_attempt(ctx):
            seen["legs"] += 1
            leg(ctx)
            raise _AbortCycle()

        with pytest.raises(_AbortCycle):
            original(*args, **{**kwargs, "codex_apply": first_attempt})
        # Between the attempts H publishes the newer G (a rebuild would
        # publish a new identity the same way).
        append_one_observation_to_live_window(ns)
        quota.reconcile_codex_quota_projection(now=now)
        after_h["watermark"] = _watermark(ns)

        def second_attempt(ctx):
            seen["legs"] += 1
            leg(ctx)

        return original(*args, **{**kwargs, "codex_apply": second_attempt})

    monkeypatch.setattr(jr, "run_stats_ingest", retrying)
    quota.reconcile_codex_quota_projection(now=now)
    assert seen["legs"] == 2, seen
    _reset, raw_max = _live_window_raw_max(ns)
    assert _in_lock_preparations(calls) == ["current"], (
        f"the retried leg reused the first attempt's validation: {calls}")
    assert _watermark(ns) >= after_h["watermark"]
    assert _live_window_percents(ns) == [float(raw_max)], (
        "the retried leg applied P's stale preparation over H's publication")


def test_a_recovery_retry_validates_the_leg_again(tmp_path, monkeypatch):
    """Detects a holder carried across ``run_stats_ingest``'s recovery retry:
    the cycle can invoke ``codex_apply`` twice, and a publication between the
    attempts must be caught by the second invocation's own validation."""
    _case_recovery_retry(tmp_path, monkeypatch)


def test_a_cache_advance_after_preparation_stays_owed(tmp_path, monkeypatch):
    """Detects a finalization that certifies or prunes past what P loaded: a
    cache mutation landing after P's preparation snapshot is not P's to
    consume. P still publishes what it prepared, but its finalization fences
    fail, so it prunes nothing, deletes the certificate it cannot substantiate,
    and the next pass consumes the advance."""
    ns, quota, jr, _jl, now = _primed_with_owed_work(tmp_path, monkeypatch)
    _interleave_before_publication(
        monkeypatch, jr, lambda: append_one_observation_to_live_window(ns))
    watermark_before = _watermark(ns)

    result = quota.reconcile_codex_quota_projection(now=now)
    assert result.status == "published", result
    count, head = _ledger_rows(ns)
    watermark = _watermark(ns)
    assert watermark < head, "premise: the advance is above P's watermark"
    assert count == head - watermark_before, (
        "P pruned ledger entries although its finalization fences failed")
    assert _certificate_bytes(ns) is None, (
        "a certificate survived that describes no published state")

    assert quota.reconcile_codex_quota_projection(
        now=now).status == "published"
    _reset, raw_max = _live_window_raw_max(ns)
    assert _live_window_percents(ns) == [float(raw_max)]
    assert _ledger_rows(ns)[0] == 0
    assert _certificate_bytes(ns) is not None


def test_contention_returns_contended_and_writes_nothing(tmp_path, monkeypatch):
    """Detects a silent no-op reported as published: when the ingest lock
    cannot be had, the pass applied nothing and must say so."""
    ns, quota, jr, _jl, now = _primed_with_owed_work(tmp_path, monkeypatch)
    rows = _raw_projection(ns)
    certificate = _certificate_bytes(ns)
    ledger = _ledger_rows(ns)
    monkeypatch.setattr(jr, "_acquire_ingest_lock", lambda mode, timeout_s: None)

    result = quota.reconcile_codex_quota_projection(now=now)
    assert result.status == "contended", result
    assert _raw_projection(ns) == rows
    assert _certificate_bytes(ns) == certificate
    assert _ledger_rows(ns) == ledger


_OBLIGATION_THRESHOLDS = (2, 25)


def _append_live_at(ns, root_key, *, offset, captured_at, used_percent):
    conn = ns["open_cache_db"]()
    try:
        row = conn.execute(
            "SELECT logical_limit_key, observed_slot, window_minutes, "
            "       resets_at_utc, "
            "       COALESCE(canonical_resets_at_utc, resets_at_utc) "
            "  FROM quota_window_snapshots "
            " WHERE source='codex' AND source_root_key=? "
            " ORDER BY 5 DESC LIMIT 1", (root_key,)).fetchone()
        import _fixture_builders
        from helpers.quota_projection_dump import source_path_for
        _fixture_builders.seed_codex_quota_snapshot(
            conn, source_root_key=root_key,
            source_path=source_path_for(root_key), line_offset=offset,
            captured_at_utc=captured_at.astimezone(dt.timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"),
            observed_slot=str(row[1]), logical_limit_key=str(row[0]),
            window_minutes=int(row[2]), used_percent=used_percent,
            resets_at_utc=str(row[3]), canonical_resets_at_utc=str(row[4]),
            plan_type="pro",
        )
        _fixture_builders.bump_codex_physical_mutation_seq(conn)
        conn.commit()
    finally:
        conn.close()


def _obligation_run(tmp_path, monkeypatch, *, h_roots, interleaved):
    """One store, P eligible for ROOT_KEY, H eligible for ``h_roots``; either
    H inside P's seam (interleaved) or H then P (serial). Returns the terminal
    claims and the dispatched notifications."""
    ns, quota, jr, _jl = _env(tmp_path, monkeypatch)
    dispatched: list = []
    monkeypatch.setattr(jr, "ALERT_DISPATCHER", dispatched.extend)
    build_codex_quota_store(ns, closed_windows=2, observations_per_window=5)
    build_codex_quota_store(
        ns, closed_windows=2, observations_per_window=5,
        root_key=SECOND_ROOT_KEY)
    enable_quota_alerts(ns, actual=_OBLIGATION_THRESHOLDS)
    now = store_as_of(ns, before_reset=dt.timedelta(minutes=20))
    quota.reconcile_codex_quota_projection(
        now=now, alert_eligible_root_keys={ROOT_KEY, SECOND_ROOT_KEY})
    _append_live_at(ns, ROOT_KEY, offset=10_000,
                    captured_at=now + dt.timedelta(minutes=1),
                    used_percent=30.0)
    later = now + dt.timedelta(minutes=2)

    def hook_h():
        quota.reconcile_codex_quota_projection(
            now=later, alert_eligible_root_keys=h_roots, full_pass="defer")

    if interleaved:
        _interleave_before_publication(monkeypatch, jr, hook_h)
    else:
        hook_h()
    quota.reconcile_codex_quota_projection(
        now=later, alert_eligible_root_keys={ROOT_KEY}, full_pass="defer")
    conn = ns["open_db"]()
    try:
        claims = sorted(
            (str(row[0]), int(row[1]), str(row[2])) for row in conn.execute(
                "SELECT source_root_key, threshold, disposition "
                "  FROM quota_threshold_events WHERE source='codex'"))
    finally:
        conn.close()
    sent = sorted(
        (str(alert.get("source_root_key")), alert.get("threshold"))
        for alert in dispatched)
    return claims, sent


@pytest.mark.parametrize("h_roots", [
    {ROOT_KEY}, {SECOND_ROOT_KEY},
], ids=["same", "disjoint"])
def test_eligible_obligations_match_the_serial_order(
    tmp_path, monkeypatch, h_roots,
):
    """Detects a re-prepared eligible pass whose alert obligations differ from
    the serial order 'H, then P': a duplicated or replayed claim when H was
    eligible for the same root, or a claim P makes from a preparation H already
    consumed when H was eligible for a disjoint root."""
    serial = _obligation_run(
        tmp_path / "serial", monkeypatch, h_roots=h_roots, interleaved=False)
    interleaved = _obligation_run(
        tmp_path / "interleaved", monkeypatch, h_roots=h_roots,
        interleaved=True)
    assert interleaved == serial


@pytest.mark.parametrize("case", ["identical", "unreadable", "retry"])
def test_removing_validation_reintroduces_the_race(tmp_path, monkeypatch, case):
    """Non-vacuity: with the leg's witness comparison forced to 'equal', each
    re-preparation case must FAIL, which proves those cases exercise the
    validation rather than pass around it. The mutation is installed after
    the store's modules are loaded, on the module the reconcile runs."""
    body = {
        "identical": _case_identical_publication,
        "unreadable": _case_unreadable_witness,
        "retry": _case_recovery_retry,
    }[case]
    with pytest.raises(AssertionError):
        body(tmp_path, monkeypatch, validation_removed=True)


# ── #1034: the drain settlement among concurrent reconcilers (spec §7) ─────

def _post_prune_settlement_estate(tmp_path, monkeypatch, *, obligation=False):
    """A real hook-enabled Codex root after a committed prune and before its
    settlement: the deletion is owed ledger work. ``obligation`` first leaves
    one pending deferred-activation request through the real path (delivery
    off, a disarming reporting pass, delivery on, an eligible hook pass).
    Returns ``(ns, quota, jr, cache, home)`` on the modules the estate
    loaded."""
    import sys

    import test_codex_quota_incremental as inc
    ns, quota, home, paths = inc.build_settlement_estate(
        tmp_path, monkeypatch)
    if obligation:
        import json as _json

        import _cctally_core
        config = _json.loads(_cctally_core.CONFIG_PATH.read_text())
        config["alerts"]["enabled"] = False
        _cctally_core.CONFIG_PATH.write_text(_json.dumps(config) + "\n")
        quota.reconcile_codex_quota_projection(
            now=dt.datetime.now(dt.timezone.utc))
        enable_quota_alerts(ns, actual=(15, 45))
        keys = tuple(root.source_root_key for root in
                     sys.modules["_cctally_record"]._codex_lifecycle_roots())
        quota.reconcile_codex_quota_projection(
            now=dt.datetime.now(dt.timezone.utc), source_root_keys=keys,
            alert_eligible_root_keys=keys, full_pass="defer")
        assert len(_obligation_requests()) == 1, "premise: no pending request"
    inc.prune_settlement_victim(ns, paths["v_interleaved"])
    return (ns, quota, sys.modules["_cctally_journal"],
            sys.modules["_cctally_cache"], home)


def _obligation_requests():
    import _cctally_quota
    return list(_cctally_quota._read_codex_alert_obligations().requests)


def _obligation_bytes():
    import _cctally_quota
    try:
        return _cctally_quota._alert_obligations_path().read_bytes()
    except FileNotFoundError:
        return None


def _settle(cache):
    return cache._settle_codex_quota_after_prune()


def _reporter(quota):
    return quota.reconcile_codex_quota_projection(
        now=dt.datetime.now(dt.timezone.utc))


def _verify_worker(quota):
    assert quota.cmd_codex_quota_verify_internal(None) == 0
    return None


def _record_prepared(monkeypatch, quota):
    """Every ``_prepare_codex_quota_pass`` as ``(in_lock, disposition,
    witness)``: an in-lock preparation exists only when the leg found its
    witness superseded, so it is the re-preparation itself."""
    calls: list = []
    original = quota._prepare_codex_quota_pass

    def wrapper(**kwargs):
        prepared = original(**kwargs)
        calls.append((bool(kwargs["in_lock"]), prepared.disposition,
                      prepared.witness))
        return prepared

    monkeypatch.setattr(quota, "_prepare_codex_quota_pass", wrapper)
    return calls


def _overtake(monkeypatch, quota, jr, *, outer, inner):
    """Run ``outer``; ``inner`` runs inside it after its optimistic
    preparation and before its ingest cycle. Returns ``(outer result, inner
    result, outer's own preparations)`` and asserts the seam fired and the
    outer pass re-prepared against a materially different witness."""
    calls = _record_prepared(monkeypatch, quota)
    seen: dict = {}

    def interleave():
        seen["before"] = len(calls)
        seen["inner"] = inner()
        seen["after"] = len(calls)

    fired = _interleave_before_publication(monkeypatch, jr, interleave)
    result = outer()
    assert fired["done"], "the interleaving seam never fired"
    own = calls[:seen["before"]] + calls[seen["after"]:]
    assert [in_lock for in_lock, _d, _w in own] == [False, True], (
        f"the overtaken pass never re-prepared: {own}")
    assert not quota._codex_witnesses_equal(own[0][2], own[1][2]), (
        "the overtaken pass re-prepared against an unchanged witness")
    return result, seen["inner"], [(in_lock, d) for in_lock, d, _w in own]


def _serial_dump(ns, copy, *steps):
    import test_codex_quota_incremental as inc
    inc.restore_data_dir(ns, copy)
    for step in steps:
        step()
    return inc._stats_dump(ns)


def test_a_reporter_prepared_before_the_settlement_re_prepares(
    tmp_path, monkeypatch,
):
    """Detects a stale reporter preparation overwriting the settlement's
    publication: a reporting-only pass R prepares the prune's owed deletion,
    the settlement publishes it before R enters the ingest lock, and R must
    re-prepare against the settlement's publication and write nothing."""
    ns, quota, jr, cache, _home = _post_prune_settlement_estate(
        tmp_path, monkeypatch)
    rows: dict = {}

    def settle():
        report = _settle(cache)
        rows["after"] = _raw_projection(ns)
        return report

    result, report, own = _overtake(
        monkeypatch, quota, jr, outer=lambda: _reporter(quota), inner=settle)
    assert report.token == "published", report
    assert own == [(False, "apply"), (True, "current")], own
    assert result.status == "current", result
    assert _raw_projection(ns) == rows["after"], (
        "R's stale preparation overwrote the settlement's projection")


def test_a_settlement_prepared_before_a_reporter_publication_re_prepares(
    tmp_path, monkeypatch,
):
    """Detects the reverse: the settlement's preparation overwriting a
    reporter's newer publication. R publishes the prune's deletion after the
    settlement prepared it and before the settlement enters the ingest lock.
    The settlement re-prepares; it still applies, because R was reporting-only
    and left the eligible alert evaluation to it, and the result must equal
    the serial order R, then the settlement, on a copy of the same state."""
    import test_codex_quota_incremental as inc
    ns, quota, jr, cache, _home = _post_prune_settlement_estate(
        tmp_path, monkeypatch)
    inc.snapshot_data_dir(ns, tmp_path / "post-prune")
    report, reporter, own = _overtake(
        monkeypatch, quota, jr, outer=lambda: _settle(cache),
        inner=lambda: _reporter(quota))
    assert reporter.status == "published", reporter
    assert own == [(False, "apply"), (True, "apply")], own
    assert report.token == "published", report
    interleaved = inc._stats_dump(ns)
    assert interleaved == _serial_dump(
        ns, tmp_path / "post-prune", lambda: _reporter(quota),
        lambda: _settle(cache)), (
        "the re-prepared settlement differs from the serial order")


@pytest.mark.parametrize("verify_first", [True, False],
                         ids=["verify_prepared_first",
                              "settlement_prepared_first"])
def test_the_verify_reporting_pass_and_the_settlement_are_serial_in_both_orders(
    tmp_path, monkeypatch, verify_first,
):
    """Detects an interleaving of the ``_codex-quota-verify`` worker's
    reporting pass and the settlement that publishes something no serial
    order would: whichever prepared first is overtaken by the other's
    publication, re-prepares, and the result must equal the same two runs
    made serially (overtaker first) on a copy of the same post-prune state."""
    import test_codex_quota_incremental as inc
    ns, quota, jr, cache, _home = _post_prune_settlement_estate(
        tmp_path, monkeypatch)
    inc.snapshot_data_dir(ns, tmp_path / "post-prune")
    verify, settle = (lambda: _verify_worker(quota)), (lambda: _settle(cache))
    outer, inner = (verify, settle) if verify_first else (settle, verify)
    outer_result, inner_result, _own = _overtake(
        monkeypatch, quota, jr, outer=outer, inner=inner)
    report = inner_result if verify_first else outer_result
    assert report.token in ("published", "current"), report
    interleaved = inc._stats_dump(ns)
    assert interleaved == _serial_dump(
        ns, tmp_path / "post-prune", inner, outer), (
        "the interleaved verify pass and settlement differ from the serial "
        "order")


def _worker_log_lines(ns):
    path = ns["APP_DIR"] / "logs" / "hook-tick.log"
    return ([line for line in path.read_text().splitlines()
             if " op=quota-verify " in line] if path.exists() else [])


def _count_settlement_lock_attempts(monkeypatch, flag):
    """Count the lifecycle-lock acquisitions made while ``flag["on"]``."""
    import _lib_codex_hooks
    attempts = {"n": 0}
    real = _lib_codex_hooks.acquire_due_lifecycle_locks

    def acquire(*args, **kwargs):
        if flag["on"]:
            attempts["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(_lib_codex_hooks, "acquire_due_lifecycle_locks",
                        acquire)
    return attempts


@pytest.mark.parametrize("ending", ["released", "exhausted"])
def test_an_activation_holding_the_locks_delays_the_settlement(
    tmp_path, monkeypatch, ending,
):
    """Detects a settlement that runs beside the deferred alert activation or
    disturbs its obligations: while the ``_codex-quota-verify`` activation
    holds every root's lifecycle lock (after its commit, before its release),
    the settlement only retries. ``released``: it settles once the activation
    releases, publishing an observation that landed meanwhile. ``exhausted``:
    it reports ``locks_busy``. Either way the obligation sidecar is byte-for-
    byte what the activation left while the settlement waited."""
    import threading
    import time

    import _lib_codex_hooks
    from _codex_zstd_fixtures import token_records, write_rollout

    import test_codex_quota_incremental as inc
    ns, quota, _jr, cache, home = _post_prune_settlement_estate(
        tmp_path, monkeypatch, obligation=True)
    monkeypatch.setattr(cache, "_SETTLEMENT_LOCK_RETRY_SECONDS", 0.01)
    if ending == "exhausted":
        monkeypatch.setattr(cache, "_SETTLEMENT_LOCK_ATTEMPTS", 3)
    flag = {"on": False}
    attempts = _count_settlement_lock_attempts(monkeypatch, flag)
    seen: dict = {}
    real_release = _lib_codex_hooks.release_lifecycle_locks
    # ``released``: a hand-off, not a race. Once the settlement has retried,
    # its next retry sleep blocks until the activation has released, so a
    # loaded runner cannot exhaust the attempts before the release.
    handed_off = threading.Event()
    real_sleep = time.sleep

    def retry_sleep(seconds):
        if (ending == "released" and attempts["n"] >= 2
                and threading.current_thread() is seen.get("thread")
                and not handed_off.is_set()):
            assert handed_off.wait(60), "the activation never released"
            return
        real_sleep(seconds)
    monkeypatch.setattr(time, "sleep", retry_sleep)

    def settle():
        flag["on"] = True
        try:
            seen["report"] = _settle(cache)
        finally:
            flag["on"] = False

    def release(locks):
        if "held" in seen or not locks:
            return real_release(locks)
        seen["held"] = _obligation_bytes()
        if ending == "exhausted":
            settle()
        else:
            seen["thread"] = threading.Thread(target=as_process_owner(settle))
            seen["thread"].start()
            deadline = dt.datetime.now() + dt.timedelta(seconds=30)
            while attempts["n"] < 2 and dt.datetime.now() < deadline:
                threading.Event().wait(0.01)
            write_rollout(
                inc._settle_rollout_path(home, 8, "meanwhile"),
                token_records(2, session_id="meanwhile")
                + [inc.settle_rate_limit_event(inc.SETTLE_BASE, 40, 30, 1)])
        seen["waited"] = _obligation_bytes()
        try:
            return real_release(locks)
        finally:
            handed_off.set()

    monkeypatch.setattr(_lib_codex_hooks, "release_lifecycle_locks", release)
    _verify_worker(quota)  # the activation leg: there is a pending request
    if ending == "released":
        seen["thread"].join(timeout=60)
        assert not seen["thread"].is_alive(), "the settlement never returned"
    assert "held" in seen, "the activation never held the lifecycle locks"
    assert " mode=activation " in _worker_log_lines(ns)[-1], (
        _worker_log_lines(ns))
    assert seen["waited"] == seen["held"], (
        "the waiting settlement changed the activation's obligations")
    report = seen["report"]
    if ending == "exhausted":
        assert report.token == "locks_busy", report
        assert attempts["n"] == 3, attempts
    else:
        assert attempts["n"] >= 2, ("the settlement never retried", attempts)
        assert report.token == "published", report


def test_a_settlement_holding_the_locks_leaves_activation_obligations_pending(
    tmp_path, monkeypatch,
):
    """Detects an activation that runs, or loses its obligation, while the
    settlement holds the lifecycle locks: the ``_codex-quota-verify``
    activation started inside the settlement's reconcile acquires nothing,
    logs ``alert_lifecycle_contended`` and leaves its request pending, and a
    later worker run still completes it."""
    ns, quota, jr, cache, _home = _post_prune_settlement_estate(
        tmp_path, monkeypatch, obligation=True)
    [request] = _obligation_requests()
    seen: dict = {}

    def interleave():
        _verify_worker(quota)
        seen["line"] = _worker_log_lines(ns)[-1]
        seen["requests"] = _obligation_requests()

    fired = _interleave_before_publication(monkeypatch, jr, interleave)
    report = _settle(cache)
    assert fired["done"], "the activation never ran inside the settlement"
    assert report.core is not None and report.core.reconciled, report
    assert "projection=alert_lifecycle_contended" in seen["line"], seen["line"]
    assert seen["requests"] == [request], seen["requests"]
    assert _obligation_requests() == [request], (
        "the settlement completed or dropped the activation's obligation")
    _verify_worker(quota)
    assert _obligation_requests() == [], "the later activation never completed"


if __name__ == "__main__":  # pragma: no cover - convenience
    raise SystemExit(pytest.main([__file__, "-v"]))
