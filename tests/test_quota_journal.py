"""Task 7 — Codex quota journaling.

Covers the Codex-side reroute onto the 6a ingest machinery:

  * Item 1 — ``sync_codex_cache`` appends a Codex quota ``obs`` per newly-read
    observation (the durable truth for the evaporating rollout JSONL), keeping
    the direct cache.db write byte-identical.
  * Item 2 — the ``QUOTA_APPLIER`` cache leg materializes those obs into cache.db
    ``quota_window_snapshots`` under the NON-BLOCKING global cache writer lock
    followed by ``cache.db.codex.lock``, and PREFIX-STOPS on either busy flock
    so the scalar cursor never advances past an unmaterialized obs.
  * Item 3 — ``reconcile_codex_quota_projection`` runs its stats writes through
    the single-flight ingest cycle (covered end-to-end by the existing
    ``test_codex_quota_projection`` suite; this file adds the journaling seam).
  * Item 4 — the on-demand codex budget firing routes through the cycle's
    ``codex_apply`` seam, so its ``budget_milestones`` crossing is journaled as a
    ``budget`` evt and its alert dispatches post-commit.
  * Item 5 — a genuine arming activation journals a ``quota_alert_arming`` evt
    whose ``activated_at_utc`` survives replay; a reconcile over the replayed
    arming honors it (no historical re-fire).

Isolation mirrors tests/test_quota_alerts.py + tests/test_journal_ingest.py:
``load_script()`` drops cached ``_cctally_*`` siblings and reloads fresh, so the
journal/quota siblings are imported AFTER it; ``redirect_paths`` sets the tmp
JOURNAL_DIR / data dir and ``sys.modules["cctally"]``.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import importlib
import json
import os
import shutil
import sqlite3
from pathlib import Path

import pytest

from conftest import load_script, redirect_paths

UTC = dt.timezone.utc
RESET = "2026-07-15T15:00:00+00:00"
FIXED = dt.datetime(2026, 7, 15, 12, 0, 0, tzinfo=UTC)
CODEX_S1_FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures" / "codex-parity" / "v1" / "rollouts" / "modern-full.jsonl"
)


def _load(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    quota = importlib.import_module("_cctally_quota")
    jr = importlib.import_module("_cctally_journal")
    jl = importlib.import_module("_lib_journal")
    return ns, quota, jr, jl


def _iso(hour: int, minute: int = 0) -> str:
    return dt.datetime(2026, 7, 15, hour, minute, tzinfo=UTC).isoformat()


def _journal_lines(jr, jl):
    core = importlib.import_module("_cctally_core")
    out = []
    for seg in jr.list_segments():
        for raw in (core.JOURNAL_DIR / seg).read_bytes().splitlines():
            rec = jl.decode_line(raw)
            if rec is not None:
                out.append(rec)
    return out


def _codex_quota_obs(jl, *, source_root_key, source_path, line_offset,
                     captured_at_utc, used_percent=10.0,
                     logical_limit_key="limit-primary", observed_slot="primary",
                     window_minutes=300, at="2026-07-15T12:00:00Z",
                     resets_at_utc=RESET):
    return jl.make_obs(at=at, src="codex-quota", provider="codex", payload={
        "kind": "quota_window_snapshot",
        "source": "codex", "source_root_key": source_root_key,
        "source_path": source_path, "line_offset": line_offset,
        "captured_at_utc": captured_at_utc, "observed_slot": observed_slot,
        "logical_limit_key": logical_limit_key, "limit_id": "native-primary",
        "limit_name": "Primary", "window_minutes": window_minutes,
        "used_percent": used_percent, "resets_at_utc": resets_at_utc,
        "plan_type": "pro", "individual_limit_json": None, "reached_type": None,
        "observed_model": "gpt-5.3-codex",
    })


def _seed_quota(ns, *, root, observations, limit="limit-primary"):
    conn = ns["open_cache_db"]()
    try:
        conn.execute(
            """INSERT INTO codex_source_roots
               (source_root_key, canonical_root_path, first_seen_utc, last_seen_utc)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(source_root_key) DO UPDATE SET
                 last_seen_utc=excluded.last_seen_utc""",
            (root, f"/codex/{root}", _iso(10), _iso(10)),
        )
        conn.executemany(
            """INSERT INTO quota_window_snapshots
               (source, source_root_key, source_path, line_offset,
                captured_at_utc, observed_slot, logical_limit_key, limit_id,
                limit_name, window_minutes, used_percent, resets_at_utc,
                plan_type, individual_limit_json, reached_type)
               VALUES ('codex', ?, ?, ?, ?, 'primary', ?, 'native-primary',
                       'Primary', 300, ?, ?, 'pro', NULL, NULL)""",
            [(root, f"/codex/{root}/rollout.jsonl", off, captured, limit,
              pct, RESET) for captured, off, pct in observations],
        )
        conn.execute(
            "INSERT INTO cache_meta(key, value) VALUES "
            "('codex_physical_mutation_seq', '1') "
            "ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER) + 1"
        )
        conn.commit()
    finally:
        conn.close()


def _write_quota_config(
    ns, *, actual=(90,), global_enabled=True, quota_enabled=True,
):
    core = importlib.import_module("_cctally_core")
    core.CONFIG_PATH.write_text(json.dumps({"alerts": {
        "enabled": global_enabled,
        "quota": {
            "enabled": quota_enabled,
            "actual_thresholds": list(actual),
            "projected_thresholds": [],
            "rules": [],
        },
    }}) + "\n")


# ==========================================================================
# Item 2 — QUOTA_APPLIER cache leg
# ==========================================================================

def test_quota_applier_materializes_obs_into_cache(tmp_path, monkeypatch):
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    ns["open_cache_db"]().close()  # create the cache.db schema

    obs = _codex_quota_obs(
        jl, source_root_key="root-a", source_path="/codex/root-a/r.jsonl",
        line_offset=10, captured_at_utc=_iso(10), used_percent=42.0)
    jr.append_record(obs, now_utc=FIXED)

    res = jr.run_stats_ingest(mode="authoritative")
    assert res.ran and res.consumed == 1

    conn = ns["open_cache_db"]()
    try:
        rows = conn.execute(
            "SELECT source_root_key, line_offset, used_percent, observed_model "
            "FROM quota_window_snapshots WHERE source='codex'").fetchall()
    finally:
        conn.close()
    assert rows == [("root-a", 10, 42.0, "gpt-5.3-codex")]

    # cursor advanced past the fully-consumed obs
    assert jr.run_stats_ingest(mode="authoritative").consumed == 0


def test_quota_applier_advances_physical_sequence_only_on_row_change(
    tmp_path, monkeypatch,
):
    """The journal cache leg participates in the shared invalidation token.

    A durable observation can be replayed after a cursor recovery, so the
    sequence must advance with the first materialization but stay flat when
    ``INSERT OR IGNORE`` proves the quota row was already current.
    """
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    ns["open_cache_db"]().close()
    obs = _codex_quota_obs(
        jl, source_root_key="root-a", source_path="/codex/root-a/r.jsonl",
        line_offset=10, captured_at_utc=_iso(10), used_percent=42.0)
    decoded = [(obs, "seg", 100)]

    assert jr._quota_applier(decoded) is None
    conn = ns["open_cache_db"]()
    try:
        first = conn.execute(
            "SELECT value FROM cache_meta "
            "WHERE key='codex_physical_mutation_seq'"
        ).fetchone()
    finally:
        conn.close()

    assert first == ("1",)
    assert jr._quota_applier(decoded) is None
    conn = ns["open_cache_db"]()
    try:
        second = conn.execute(
            "SELECT value FROM cache_meta "
            "WHERE key='codex_physical_mutation_seq'"
        ).fetchone()
    finally:
        conn.close()
    assert second == first, "an idempotent replay must not invalidate readers"


# --------------------------------------------------------------------------
# #496 S5b §4.3 — the cache leg advances the coverage certificate
# --------------------------------------------------------------------------

def _coverage(ns):
    cache = importlib.import_module("_cctally_cache")
    conn = ns["open_cache_db"]()
    try:
        return cache.load_codex_journal_coverage_certificate(conn)
    finally:
        conn.close()


def _prime_coverage(ns, jr, covered, applied_through=None):
    """Store a certificate covering exactly ``covered``, as recovery would."""
    cache = importlib.import_module("_cctally_cache")
    kernel = importlib.import_module("_lib_cache_coverage")
    conn = ns["open_cache_db"]()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cache._store_codex_journal_coverage_certificate(
            conn,
            kernel.advance(
                None, covered=covered,
                applied_through=covered if applied_through is None
                else applied_through,
                pinned_vector=jr.coverage_pinned_vector(), physical_seq=0),
        )
        conn.commit()
    finally:
        conn.close()


def _settled_at(ns, jr, jl):
    """A store whose ingest cursor and coverage certificate both sit at the
    journal high water — the state a rebuild's recovery pass leaves behind.

    One obs is appended and consumed first, because a cycle's contiguity check
    compares the stored certificate against the cursor it STARTS from, and a
    fresh index starts from nothing.
    """
    ns["open_cache_db"]().close()
    jr.append_record(_codex_quota_obs(
        jl, source_root_key="root-a", source_path="/codex/root-a/r.jsonl",
        line_offset=10, captured_at_utc=_iso(10)), now_utc=FIXED)
    jr.run_stats_ingest(mode="authoritative")
    settled = jr.journal_high_water()
    _prime_coverage(ns, jr, settled)
    return settled


def test_the_cache_leg_advances_coverage_over_a_contiguous_batch(
    tmp_path, monkeypatch,
):
    """Spec §4.3: the leg owns a contiguous batch, so a predecessor covering
    exactly this cycle's starting cursor extends to its high water.

    This is the writer that keeps the F11 fast path alive between rebuilds. The
    rollout walk appends quota obs but cannot prove contiguity per file, so
    without this every rebuild after any Codex activity replays the whole
    journal into an intact cache.
    """
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    settled = _settled_at(ns, jr, jl)

    jr.append_record(_codex_quota_obs(
        jl, source_root_key="root-a", source_path="/codex/root-a/r.jsonl",
        line_offset=20, captured_at_utc=_iso(11)), now_utc=FIXED)
    grown = jr.journal_high_water()
    assert grown != settled, "the delta must move the high water"

    assert jr.run_stats_ingest(mode="authoritative").ran
    assert _coverage(ns)["coveredHighWater"] == [grown[0], grown[1]]


def test_the_cache_leg_refuses_to_advance_over_a_gap(tmp_path, monkeypatch):
    """"No writer mints a certificate forward over a gapped predecessor."

    A predecessor covering less than this cycle's starting cursor leaves records
    between the two that nobody proved applied. Extending it would assert
    coverage over them.
    """
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    settled = _settled_at(ns, jr, jl)
    _prime_coverage(ns, jr, (settled[0], 0))

    jr.append_record(_codex_quota_obs(
        jl, source_root_key="root-a", source_path="/codex/root-a/r.jsonl",
        line_offset=20, captured_at_utc=_iso(11)), now_utc=FIXED)
    assert jr.run_stats_ingest(mode="authoritative").ran
    assert _coverage(ns)["coveredHighWater"] == [settled[0], 0]


def test_the_cache_leg_mints_nothing_over_an_absent_certificate(
    tmp_path, monkeypatch,
):
    """Establishing coverage requires reading the journal, and only the
    rebuild's recovery pass does that. A writer that minted one here would be
    asserting coverage for every record before its own batch."""
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    ns["open_cache_db"]().close()
    jr.append_record(_codex_quota_obs(
        jl, source_root_key="root-a", source_path="/codex/root-a/r.jsonl",
        line_offset=10, captured_at_utc=_iso(10)), now_utc=FIXED)
    assert _coverage(ns) is None, "the fixture must start with no certificate"
    assert jr.run_stats_ingest(mode="authoritative").ran
    assert _coverage(ns) is None


def test_a_direct_applier_call_advances_nothing(tmp_path, monkeypatch):
    """`cursor`/`covered_to` default to None, so a caller that does not know the
    cycle's range cannot mint coverage it cannot justify."""
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    _settled_at(ns, jr, jl)
    obs = _codex_quota_obs(
        jl, source_root_key="root-a", source_path="/codex/root-a/r.jsonl",
        line_offset=20, captured_at_utc=_iso(11))
    jr.append_record(obs, now_utc=FIXED)
    before = _coverage(ns)
    assert jr._quota_applier([(obs, "seg", 100)]) is None
    assert _coverage(ns) == before


def _append_raw(jr, payload: bytes) -> int:
    """Append raw bytes to the newest segment; return its new size."""
    core = importlib.import_module("_cctally_core")
    path = core.JOURNAL_DIR / jr.list_segments()[-1]
    with open(path, "ab") as handle:
        handle.write(payload)
    return path.stat().st_size


def test_a_short_decode_does_not_freeze_the_certificate_forever(
    tmp_path, monkeypatch,
):
    """The covered boundary and the ingest cursor are DIFFERENT coordinates.

    A newline-terminated line that does not decode moves the cycle's cursor
    without moving the boundary the pass can honestly claim, so the certificate
    stores a `coveredHighWater` deliberately short of `appliedThrough`. Storing
    only the short one made the next cycle read the difference as a gap and
    discard the predecessor, and every later cycle did the same — the mechanism
    froze silently until a rebuild minted a fresh certificate.
    """
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    _settled_at(ns, jr, jl)

    jr.append_record(_codex_quota_obs(
        jl, source_root_key="root-a", source_path="/codex/root-a/r.jsonl",
        line_offset=20, captured_at_utc=_iso(11)), now_utc=FIXED)
    decoded_end = jr.journal_high_water()
    torn = _append_raw(jr, b"this line is not a journal record\n")
    assert torn > decoded_end[1], "the undecodable line must move the cursor"

    assert jr.run_stats_ingest(mode="authoritative").ran
    first = _coverage(ns)
    assert first["coveredHighWater"] == [decoded_end[0], decoded_end[1]], (
        "the claim stops at the last record the pass actually decoded"
    )
    assert first["appliedThrough"] == [decoded_end[0], torn], (
        "the cursor coordinate is carried separately, or the next cycle sees "
        "a gap that is not there"
    )

    jr.append_record(_codex_quota_obs(
        jl, source_root_key="root-a", source_path="/codex/root-a/r.jsonl",
        line_offset=30, captured_at_utc=_iso(12)), now_utc=FIXED)
    grown = jr.journal_high_water()
    assert jr.run_stats_ingest(mode="authoritative").ran
    assert _coverage(ns)["coveredHighWater"] == [grown[0], grown[1]], (
        "the next cycle must still be able to extend it"
    )


def test_an_outdated_interpretation_version_is_never_laundered_forward(
    tmp_path, monkeypatch,
):
    """`advance` re-stamps the CURRENT module constants and discards `prior`.

    Extending a certificate written under an older `interpretationVersion`
    therefore turns it into a current-version one, and the next rebuild skips
    exactly the replay the version bump exists to force. The constant's own
    docstring says such a certificate "is rejected rather than compared".
    """
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    settled = _settled_at(ns, jr, jl)
    kernel = importlib.import_module("_lib_cache_coverage")
    cache = importlib.import_module("_cctally_cache")
    stale = dict(_coverage(ns))
    stale["interpretationVersion"] = kernel.INTERPRETATION_VERSION - 1
    conn = ns["open_cache_db"]()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cache._store_codex_journal_coverage_certificate(conn, stale)
        conn.commit()
    finally:
        conn.close()

    jr.append_record(_codex_quota_obs(
        jl, source_root_key="root-a", source_path="/codex/root-a/r.jsonl",
        line_offset=20, captured_at_utc=_iso(11)), now_utc=FIXED)
    assert jr.run_stats_ingest(mode="authoritative").ran

    after = _coverage(ns)
    assert after["interpretationVersion"] == (
        kernel.INTERPRETATION_VERSION - 1), "it must not be re-stamped"
    assert after["coveredHighWater"] == [settled[0], settled[1]], (
        "and it must not have moved"
    )


def test_the_advanced_certificate_carries_the_post_bump_sequence(
    tmp_path, monkeypatch,
):
    """The sequence is read after the bump, inside the same transaction.

    Storing the pre-bump value would produce a certificate `certificate_is_valid`
    rejects on its very first use, which is indistinguishable from never having
    advanced at all.
    """
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    _settled_at(ns, jr, jl)
    jr.append_record(_codex_quota_obs(
        jl, source_root_key="root-a", source_path="/codex/root-a/r.jsonl",
        line_offset=20, captured_at_utc=_iso(11)), now_utc=FIXED)
    assert jr.run_stats_ingest(mode="authoritative").ran

    kernel = importlib.import_module("_lib_cache_coverage")
    quota_mod = importlib.import_module("_cctally_quota")
    conn = ns["open_cache_db"]()
    try:
        seq = quota_mod.codex_physical_mutation_seq(conn)
    finally:
        conn.close()
    assert seq > 0, "the batch must have changed rows, or the bump never ran"
    ok, why = kernel.certificate_is_valid(
        _coverage(ns), pinned_vector=jr.coverage_pinned_vector(),
        physical_seq=seq)
    assert (ok, why) == (True, kernel.REASON_OK)


# --------------------------------------------------------------------------
# #496 S5b F11 — a rebuild over an intact cache takes no writer flock
# --------------------------------------------------------------------------

def _count_flock_acquisitions(monkeypatch):
    """Every cache-writer flock acquisition, by the paths it locked.

    A STRUCTURAL observable. An elapsed-time ceiling at fixture scale cannot
    fail and would certify nothing; zero acquisitions cannot pass vacuously
    either, which is why the paired recovery case asserts a non-empty list over
    the same fixture.
    """
    import _lib_cache_writer_lock as lock

    real = lock.acquire_cache_writer_flocks
    seen: list = []

    def record(global_path, provider_path, **kwargs):
        seen.append((str(global_path), str(provider_path)))
        return real(global_path, provider_path, **kwargs)

    monkeypatch.setattr(lock, "acquire_cache_writer_flocks", record)
    return seen


def _quota_rows(ns):
    conn = ns["open_cache_db"]()
    try:
        return conn.execute(
            "SELECT source_root_key, line_offset, used_percent "
            "FROM quota_window_snapshots WHERE source='codex' "
            "ORDER BY line_offset").fetchall()
    finally:
        conn.close()


def _rebuild(jr):
    return jr.rebuild_stats_index(context=jr.RebuildContext(trigger="test-fixture"))


def test_a_rebuild_over_an_intact_cache_acquires_zero_writer_flocks(
    tmp_path, monkeypatch,
):
    """The F11 property. Today every rebuild replays every retained observation
    into `cache.db` under both writer flocks whether or not the cache already
    holds them — 1.81 million observations and a 23.0 s hold on the maintainer's
    store, of the lock issue #297 blames for `database is locked`."""
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    ns["open_cache_db"]().close()
    for offset in (10, 20, 30):
        jr.append_record(_codex_quota_obs(
            jl, source_root_key="root-a", source_path="/codex/root-a/r.jsonl",
            line_offset=offset, captured_at_utc=_iso(10)), now_utc=FIXED)

    first = _rebuild(jr)
    assert first.quota_cache_coverage["status"] == "recovered", (
        "the first rebuild must replay, or the second proves nothing"
    )
    assert first.quota_cache_coverage["replayedObservations"] == 3
    expected = _quota_rows(ns)
    assert len(expected) == 3

    acquisitions = _count_flock_acquisitions(monkeypatch)
    second = _rebuild(jr)
    assert acquisitions == []
    assert second.quota_cache_coverage["status"] == "covered"
    assert second.quota_cache_coverage["replayedObservations"] == 0
    assert second.quota_lock_hold_seconds == 0.0
    assert _quota_rows(ns) == expected


def test_a_recovering_rebuild_does_acquire_them(tmp_path, monkeypatch):
    """Non-vacuity for the zero above: the same instrumentation over a rebuild
    that has no certificate must record acquisitions."""
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    ns["open_cache_db"]().close()
    jr.append_record(_codex_quota_obs(
        jl, source_root_key="root-a", source_path="/codex/root-a/r.jsonl",
        line_offset=10, captured_at_utc=_iso(10)), now_utc=FIXED)
    acquisitions = _count_flock_acquisitions(monkeypatch)
    assert _rebuild(jr).quota_cache_coverage["status"] == "recovered"
    assert acquisitions != []


def test_a_late_append_invalidates_coverage_and_replays(tmp_path, monkeypatch):
    """Spec §7 case 4, and the case that establishes the certificate's own
    correctness. An append after certificate creation but before the rebuild
    changes an extent without changing a name, so a name-bound root would stay
    valid while the cache lacked that observation."""
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    ns["open_cache_db"]().close()
    jr.append_record(_codex_quota_obs(
        jl, source_root_key="root-a", source_path="/codex/root-a/r.jsonl",
        line_offset=10, captured_at_utc=_iso(10)), now_utc=FIXED)
    assert _rebuild(jr).quota_cache_coverage["status"] == "recovered"

    jr.append_record(_codex_quota_obs(
        jl, source_root_key="root-a", source_path="/codex/root-a/r.jsonl",
        line_offset=20, captured_at_utc=_iso(11)), now_utc=FIXED)
    acquisitions = _count_flock_acquisitions(monkeypatch)
    result = _rebuild(jr)
    assert acquisitions != [], "the late append must force a recovery pass"
    assert result.quota_cache_coverage["status"] == "recovered"
    assert result.quota_cache_coverage["reason"] == "identityRoot"
    assert [row[1] for row in _quota_rows(ns)] == [10, 20]


def test_a_destructive_clear_forces_the_next_rebuild_to_replay(
    tmp_path, monkeypatch,
):
    """`_clear_codex_derived_rows` deletes the physical quota state the
    certificate describes. Leaving it would make it stale-valid, and the fast
    path would then skip a replay the cache genuinely needs."""
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    ns["open_cache_db"]().close()
    jr.append_record(_codex_quota_obs(
        jl, source_root_key="root-a", source_path="/codex/root-a/r.jsonl",
        line_offset=10, captured_at_utc=_iso(10)), now_utc=FIXED)
    assert _rebuild(jr).quota_cache_coverage["status"] == "recovered"
    assert _rebuild(jr).quota_cache_coverage["status"] == "covered"

    cache = importlib.import_module("_cctally_cache")
    conn = ns["open_cache_db"]()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cache._clear_codex_derived_rows(conn)
        conn.commit()
    finally:
        conn.close()
    assert _quota_rows(ns) == []

    result = _rebuild(jr)
    assert result.quota_cache_coverage["status"] == "recovered"
    assert result.quota_cache_coverage["reason"] == "absent"
    assert [row[1] for row in _quota_rows(ns)] == [10]


def test_the_projection_is_identical_on_the_covered_and_replayed_paths(
    tmp_path, monkeypatch,
):
    """The intact path must still produce the correct quota projection. Skipping
    the replay changes what the leg WRITES to `cache.db`, never what the stats
    projection reads out of it."""
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    # A registered source root, because `_cache_root_keys` is what makes the
    # projection non-empty and the journal obs alone never write one.
    _seed_quota(ns, root="root-a", observations=[(_iso(9), 1, 5.0)])
    for offset in (10, 20):
        jr.append_record(_codex_quota_obs(
            jl, source_root_key="root-a", source_path="/codex/root-a/r.jsonl",
            line_offset=offset, captured_at_utc=_iso(10)), now_utc=FIXED)

    def _projection():
        # Named columns rather than `*`: the row carries a per-rebuild
        # `generation`, which differs between any two rebuilds for reasons that
        # have nothing to do with which coverage path ran.
        conn = __import__("sqlite3").connect(
            str(importlib.import_module("_cctally_core").DB_PATH))
        try:
            return conn.execute(
                "SELECT source, source_root_key, logical_limit_key, "
                "observed_slot, window_minutes, resets_at_utc, "
                "nominal_start_at_utc, first_observed_at_utc, "
                "last_observed_at_utc, first_percent, current_percent, "
                "last_source_path, last_line_offset, account_key, "
                "physical_group_key, physical_group_digest "
                "FROM quota_window_blocks ORDER BY source_root_key, "
                "logical_limit_key, resets_at_utc").fetchall()
        finally:
            conn.close()

    assert _rebuild(jr).quota_cache_coverage["status"] == "recovered"
    replayed = _projection()
    assert replayed, "the projection must be non-empty, or this compares nothing"
    assert _rebuild(jr).quota_cache_coverage["status"] == "covered"
    assert _projection() == replayed


def test_quota_applier_converges_cross_batch_records_by_physical_order(
        tmp_path, monkeypatch):
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    conn = ns["open_cache_db"]()
    try:
        records = (
            _codex_quota_obs(
                jl, source_root_key="root-a",
                source_path="/codex/root-a/z.jsonl", line_offset=20,
                captured_at_utc=_iso(10), resets_at_utc=_iso(19, 20)),
            _codex_quota_obs(
                jl, source_root_key="root-a",
                source_path="/codex/root-a/a.jsonl", line_offset=10,
                captured_at_utc=_iso(10, 1), resets_at_utc=_iso(19, 0)),
            _codex_quota_obs(
                jl, source_root_key="root-a",
                source_path="/codex/root-a/m.jsonl", line_offset=30,
                captured_at_utc=_iso(10, 2), resets_at_utc=_iso(19, 10)),
        )
        for record in records:
            conn.execute("BEGIN IMMEDIATE")
            jr._apply_quota_records(conn, [record])
            conn.commit()

        anchors = {
            row[0] for row in conn.execute(
                "SELECT canonical_resets_at_utc "
                "FROM quota_window_snapshots WHERE source='codex'")
        }
        assert anchors == {"2026-07-15T19:00:00Z"}
    finally:
        conn.close()


def test_quota_applier_prefix_stops_on_busy_codex_flock(tmp_path, monkeypatch):
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    ns["open_cache_db"]().close()
    core = importlib.import_module("_cctally_core")

    # A non-quota obs at index 0, a codex quota obs at index 1.
    non_quota = jl.make_obs(
        at="2026-07-15T12:00:00Z", src="statusline", provider="claude",
        payload={"week_start_date": "2026-07-15", "weekly_percent": 1.0})
    quota_obs = _codex_quota_obs(
        jl, source_root_key="root-a", source_path="/codex/root-a/r.jsonl",
        line_offset=10, captured_at_utc=_iso(10))
    decoded = [(non_quota, "seg", 0), (quota_obs, "seg", 100)]

    # Hold the codex flock (a second open() fd competes even in-process — BSD
    # flock is per-open-file-description) so the applier sees it busy.
    held = os.open(str(core.CACHE_LOCK_CODEX_PATH), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(held, fcntl.LOCK_EX)
    try:
        stop = jr._quota_applier(decoded)
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        os.close(held)
    assert stop == 1, "prefix-stop at the FIRST codex quota obs index"

    conn = ns["open_cache_db"]()
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM quota_window_snapshots "
            "WHERE source='codex'").fetchone()[0] == 0, "nothing materialized"
    finally:
        conn.close()

    # Flock free now -> the remainder materializes, full consumption (None).
    assert jr._quota_applier(decoded) is None
    conn = ns["open_cache_db"]()
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM quota_window_snapshots "
            "WHERE source='codex'").fetchone()[0] == 1
    finally:
        conn.close()


def test_quota_applier_prefix_stops_on_busy_global_writer_flock(
    tmp_path, monkeypatch
):
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    ns["open_cache_db"]().close()
    core = importlib.import_module("_cctally_core")

    quota_obs = _codex_quota_obs(
        jl, source_root_key="root-a", source_path="/codex/root-a/r.jsonl",
        line_offset=10, captured_at_utc=_iso(10))
    decoded = [(quota_obs, "seg", 100)]

    held = os.open(str(core.CACHE_LOCK_PATH), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(held, fcntl.LOCK_EX)
    try:
        assert jr._quota_applier(decoded) == 0
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        os.close(held)

    conn = ns["open_cache_db"]()
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM quota_window_snapshots "
            "WHERE source='codex'").fetchone()[0] == 0
    finally:
        conn.close()


# ==========================================================================
# Item 1 — sync_codex_cache appends a Codex quota obs per observation
# ==========================================================================

def test_sync_codex_cache_appends_quota_obs(tmp_path, monkeypatch):
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    provider_root = tmp_path / "fake-codex-home"
    rollout = provider_root / "sessions" / "2026" / "07" / "15" / "rollout-s1.jsonl"
    rollout.parent.mkdir(parents=True)
    shutil.copyfile(CODEX_S1_FIXTURE, rollout)
    monkeypatch.setenv("CODEX_HOME", str(provider_root))

    # Cut over the (empty) stats.db FIRST — matching production ordering, where
    # the one-time §8 cutover runs before any Codex sync writes cache quota rows,
    # so the bootstrap carries no quota obs. Otherwise the reconcile's own
    # ``open_db`` (invoked at the tail of ``sync_codex_cache``) would cut over a
    # legacy stats.db and re-export the just-written cache quota rows as bootstrap
    # obs, double-counting them here (DB journal redesign §8).
    ns["open_db"]().close()

    cache = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](cache)
    finally:
        cache.close()

    conn = ns["open_cache_db"]()
    try:
        cache_rows = conn.execute(
            "SELECT line_offset, used_percent FROM quota_window_snapshots "
            "WHERE source='codex'").fetchall()
    finally:
        conn.close()
    assert cache_rows, "the S1 fixture must carry quota events"

    obs = [
        line for line in _journal_lines(jr, jl)
        if line.get("t") == "obs" and line.get("provider") == "codex"
        and (line.get("payload") or {}).get("kind") == "quota_window_snapshot"
    ]
    assert len(obs) == len(cache_rows), "one journal obs per materialized row"
    obs_keys = {(o["payload"]["line_offset"], o["payload"]["used_percent"]) for o in obs}
    assert obs_keys == {(r[0], r[1]) for r in cache_rows}


def test_quota_obs_replay_has_stable_identity(tmp_path, monkeypatch):
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    cache_mod = importlib.import_module("_cctally_cache")
    row = (
        "codex", "root-a", "/codex/root-a/r.jsonl", 10, _iso(10),
        "primary", "limit-primary", "native-primary", "Primary", 300,
        42.0, RESET, "pro", None, None, "gpt-5.3-codex",
        None,  # #341 trailing account_key (unattributed -> obs omits `account`)
        # #416 §4.2 trailing canonical_resets_at_utc. Deliberately NOT journaled:
        # the anchor is a property of the observation's POPULATION, not of the
        # observation, so freezing it into the append-only record would both
        # prevent a later ingest from correcting it and change the payload's
        # content id, breaking the natural-key dedup that keeps replay idempotent.
        None,
    )

    monkeypatch.setenv("CCTALLY_AS_OF", "2026-07-15T12:00:00Z")
    cache_mod._append_codex_quota_obs([row])
    core = importlib.import_module("_cctally_core")
    dedupe_index = core.JOURNAL_DIR / ".quota-observation-keys"
    assert dedupe_index.exists()

    # Simulate the next short-lived hook process: it must reload the compact
    # durable index, not rescan the potentially multi-gigabyte journal.
    jr._QUOTA_DEDUP_DIR = None
    jr._QUOTA_DEDUP_KEYS.clear()
    jr._QUOTA_DEDUP_LOADED = False
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-07-15T13:00:00Z")
    cache_mod._append_codex_quota_obs([row])

    obs = [
        line for line in _journal_lines(jr, jl)
        if line.get("t") == "obs"
        and (line.get("payload") or {}).get("kind") == "quota_window_snapshot"
    ]
    assert len(obs) == 1, (
        "re-reading the same rollout bytes must not grow the durable journal"
    )
    assert obs[0]["at"] == _iso(10)


def test_quota_obs_upgrade_dedupes_legacy_command_time_identity(
    tmp_path, monkeypatch,
):
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    cache_mod = importlib.import_module("_cctally_cache")
    row = (
        "codex", "root-a", "/codex/root-a/r.jsonl", 10, _iso(10),
        "primary", "limit-primary", "native-primary", "Primary", 300,
        42.0, RESET, "pro", None, None, "gpt-5.3-codex",
        None,  # #341 trailing account_key (unattributed -> obs omits `account`)
        # #416 §4.2 trailing canonical_resets_at_utc. Deliberately NOT journaled:
        # the anchor is a property of the observation's POPULATION, not of the
        # observation, so freezing it into the append-only record would both
        # prevent a later ingest from correcting it and change the payload's
        # content id, breaking the natural-key dedup that keeps replay idempotent.
        None,
    )

    # v1.80.1 used the later sync command clock as `at`, so the same retained
    # source row had a different content id from the stable-clock form.
    legacy = _codex_quota_obs(
        jl,
        source_root_key="root-a",
        source_path="/codex/root-a/r.jsonl",
        line_offset=10,
        captured_at_utc=_iso(10),
        used_percent=42.0,
        at="2026-07-15T12:00:00Z",
    )
    jr.append_record(legacy, now_utc=FIXED)

    monkeypatch.setenv("CCTALLY_AS_OF", "2026-07-15T13:00:00Z")
    cache_mod._append_codex_quota_obs([row])

    obs = [
        line for line in _journal_lines(jr, jl)
        if line.get("t") == "obs"
        and (line.get("payload") or {}).get("kind") == "quota_window_snapshot"
    ]
    assert len(obs) == 1, (
        "the upgrade must recognize an existing quota row by its table natural "
        "key even though v1.80.1 gave it a command-time content id"
    )


# ==========================================================================
# Item 5 — quota_alert_arming journaled state + replay honored
# ==========================================================================

def test_arming_journaled_and_replay_honored(tmp_path, monkeypatch):
    ns, quota, jr, jl = _load(tmp_path, monkeypatch)
    _seed_quota(ns, root="root-a", observations=[(_iso(10), 10, 95.0)])
    _write_quota_config(ns, actual=(90,))
    now = dt.datetime(2026, 7, 15, 12, tzinfo=UTC)

    # Reconcile #1 (eligible): a fresh fingerprint activates -> the arming state
    # is journaled as a `quota_alert_arming` evt (Item 5).
    quota.reconcile_codex_quota_projection(
        source_root_keys={"root-a"}, alert_eligible_root_keys={"root-a"}, now=now)

    arming_evts = [
        line for line in _journal_lines(jr, jl)
        if line.get("t") == "evt"
        and (line.get("payload") or {}).get("kind") == "quota_alert_arming"
    ]
    assert len(arming_evts) == 1
    evt = arming_evts[0]
    assert evt["id"].startswith("qaa:codex:root-a:")
    assert evt["payload"]["activated_at_utc"] == quota._utc_iso(now)
    assert evt["payload"]["rule_fingerprint"]

    conn = ns["open_db"]()
    try:
        arm = conn.execute(
            "SELECT source_root_key, rule_fingerprint, activated_at_utc "
            "FROM quota_alert_arming").fetchall()
    finally:
        conn.close()
    assert len(arm) == 1 and arm[0][2] == quota._utc_iso(now)

    # (b) Fold-applier round-trip: clear the arming table, replay the evt, and
    # the boundary (activated_at) is reproduced verbatim.
    conn = ns["open_db"]()
    try:
        conn.execute("DELETE FROM quota_alert_arming")
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        jr._apply_evt(conn, evt)
        conn.commit()
        replayed = conn.execute(
            "SELECT source, source_root_key, rule_fingerprint, activated_at_utc "
            "FROM quota_alert_arming").fetchone()
    finally:
        conn.close()
    assert replayed is not None
    assert replayed[0] == "codex" and replayed[1] == "root-a"
    assert replayed[2] == evt["payload"]["rule_fingerprint"]
    assert replayed[3] == quota._utc_iso(now)

    # (c) A reconcile over the replayed arming HONORS it: no second arming evt,
    # no historical re-fire.
    result = quota.reconcile_codex_quota_projection(
        source_root_keys={"root-a"}, alert_eligible_root_keys={"root-a"}, now=now)
    arming_evts2 = [
        line for line in _journal_lines(jr, jl)
        if line.get("t") == "evt"
        and (line.get("payload") or {}).get("kind") == "quota_alert_arming"
    ]
    assert len(arming_evts2) == 1, "replayed boundary honored -> no re-arm evt"
    assert result.alerts_dispatched == 0, "no historical re-fire"


@pytest.mark.parametrize("disabled_gate", ("global", "quota"))
def test_disabled_arming_tombstone_survives_rebuild_without_historical_fire(
    tmp_path, monkeypatch, disabled_gate,
):
    """A rebuild must not resurrect the boundary deleted while alerts were off."""
    ns, quota, jr, jl = _load(tmp_path, monkeypatch)
    _seed_quota(ns, root="root-a", observations=[(_iso(10), 10, 80.0)])
    _seed_quota(ns, root="root-b", observations=[(_iso(10), 10, 80.0)])
    _write_quota_config(ns, actual=(90,))
    dispatched = []
    monkeypatch.setattr(jr, "ALERT_DISPATCHER", dispatched.extend)

    quota.reconcile_codex_quota_projection(
        source_root_keys={"root-a", "root-b"},
        alert_eligible_root_keys={"root-a", "root-b"},
        now=dt.datetime(2026, 7, 15, 11, tzinfo=UTC),
    )
    cache = ns["open_cache_db"]()
    try:
        certificate = quota.load_codex_quota_projection_certificate(cache)
        physical_sequence = quota.codex_physical_mutation_seq(cache)
    finally:
        cache.close()
    assert certificate is not None
    assert certificate[0] == physical_sequence

    if disabled_gate == "global":
        disable_args = argparse.Namespace(
            action="set", key="alerts.enabled", value="false", emit_json=False,
        )
    else:
        disable_args = argparse.Namespace(
            action="set",
            key="alerts.quota",
            value=json.dumps({
                "enabled": False,
                "actual_thresholds": [90],
                "projected_thresholds": [],
                "rules": [],
            }),
            emit_json=False,
        )
    assert ns["cmd_config"](disable_args) == 0
    quota.reconcile_codex_quota_projection(
        source_root_keys={"root-a", "root-b"},
        # Read-only report paths deliberately carry no lifecycle eligibility.
        # No cache data changed after the certificate assertion above, so this
        # also proves the valid-certificate fast path cannot bypass disarming.
        alert_eligible_root_keys=set(),
        now=dt.datetime(2026, 7, 15, 11, 20, tzinfo=UTC),
    )

    disarms = [
        line for line in _journal_lines(jr, jl)
        if line.get("t") == "evt"
        and (line.get("payload") or {}).get("kind") == "quota_alert_arming"
        and (line.get("payload") or {}).get("state") == "disarmed"
    ]
    assert len(disarms) == 2, "every disabled delete must be retained in the journal"
    for disarm in disarms:
        payload = disarm["payload"]
        assert set(payload) == {
            "kind", "source", "source_root_key", "account_key",
            "logical_limit_key", "observed_slot", "window_minutes", "state",
            "disarmed_at_utc", "journal_identity_version",
        }
        assert disarm["id"] == jl.evt_id(
            "qaa", payload["source"], payload["source_root_key"],
            payload["account_key"], payload["logical_limit_key"],
            payload["observed_slot"], payload["window_minutes"],
            payload["state"], payload["disarmed_at_utc"],
        )
        assert disarm["at"] == payload["disarmed_at_utc"]

    _seed_quota(ns, root="root-a", observations=[(_iso(11, 10), 20, 95.0)])
    _seed_quota(ns, root="root-b", observations=[(_iso(11, 10), 20, 95.0)])
    jr.rebuild_stats_index(context=jr.RebuildContext(trigger="test-fixture"))
    conn = ns["open_db"]()
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM quota_alert_arming"
        ).fetchone()[0] == 0, "replay must leave the identity disarmed"
    finally:
        conn.close()

    assert ns["cmd_config"](argparse.Namespace(
        action="set", key="alerts.enabled", value="true", emit_json=False,
    )) == 0
    assert ns["cmd_config"](argparse.Namespace(
        action="set",
        key="alerts.quota",
        value=json.dumps({
            "enabled": True,
            "actual_thresholds": [90],
            "projected_thresholds": [],
            "rules": [],
        }),
        emit_json=False,
    )) == 0
    result = quota.reconcile_codex_quota_projection(
        source_root_keys={"root-a", "root-b"},
        alert_eligible_root_keys={"root-a", "root-b"},
        now=dt.datetime(2026, 7, 15, 11, 30, tzinfo=UTC),
    )
    conn = ns["open_db"]()
    try:
        terminal = [
            tuple(row) for row in conn.execute(
                "SELECT threshold, disposition FROM quota_threshold_events "
                "ORDER BY threshold"
            ).fetchall()
        ]
    finally:
        conn.close()
    assert terminal == [
        (90, "suppressed_backfill"),
        (90, "suppressed_backfill"),
    ]
    assert result.alerts_dispatched == 0
    assert dispatched == []


#: Every `qaa` payload field that is ALSO an id component, in the exact order
#: `_cctally_quota._emit_arming` passes them to `evt_id`. `kind` and
#: `journal_identity_version` are family constants, not identity.
_QAA_ID_COMPONENT_KEYS = (
    "source", "source_root_key", "account_key", "logical_limit_key",
    "observed_slot", "window_minutes", "rule_fingerprint", "activated_at_utc",
)
_QAA_CONSTANT_KEYS = frozenset({"kind", "journal_identity_version"})


def test_arming_payload_carries_no_field_outside_its_id(tmp_path, monkeypatch):
    """#374 acceptance 10: the direct quota-arming writer
    (`_cctally_quota.py` `_emit_arming`) appends straight to the journal,
    bypassing BOTH emit paths — so nothing classifies it and nothing can
    quarantine a same-revision divergence it produces. That is only safe while
    divergence is structurally impossible, i.e. while every payload field except
    the family constants is also an id component (and `at` is one of them). This
    test pins that: adding a tenth payload field without extending `evt_id`
    fails HERE rather than silently reopening the #374 defect on a family the
    write boundary deliberately exempts."""
    ns, quota, jr, jl = _load(tmp_path, monkeypatch)
    _seed_quota(ns, root="root-a", observations=[(_iso(10), 10, 95.0)])
    _write_quota_config(ns, actual=(90,))
    now = dt.datetime(2026, 7, 15, 12, tzinfo=UTC)

    quota.reconcile_codex_quota_projection(
        source_root_keys={"root-a"}, alert_eligible_root_keys={"root-a"}, now=now)

    evts = [
        line for line in _journal_lines(jr, jl)
        if line.get("t") == "evt"
        and (line.get("payload") or {}).get("kind") == "quota_alert_arming"
    ]
    assert len(evts) == 1
    evt = evts[0]
    payload = evt["payload"]

    assert set(payload) - _QAA_CONSTANT_KEYS == set(_QAA_ID_COMPONENT_KEYS), (
        "every qaa payload field must be an id component or a family constant — "
        "a field outside the id lets two divergent payloads share one id, which "
        "this writer has no classifier to catch"
    )
    assert payload["journal_identity_version"] == 2
    assert evt["id"] == jl.evt_id(
        "qaa", *(payload[key] for key in _QAA_ID_COMPONENT_KEYS)
    ), "the id must be exactly those fields, in that order"
    assert evt["at"] == payload["activated_at_utc"], (
        "the record's `at` is an id component too, so it cannot drift either")
    assert evt["rev"] == 0


# ==========================================================================
# Item 4 — on-demand codex budget firing routes through the cycle + journals
# ==========================================================================

def test_on_demand_codex_budget_routes_through_cycle_and_journals(
    tmp_path, monkeypatch
):
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    ns["open_db"]().close()
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-06-15T12:00:00Z")
    core = importlib.import_module("_cctally_core")
    core.CONFIG_PATH.write_text(json.dumps({
        "display": {"tz": "utc"},
        "budget": {"codex": {
            "amount_usd": 200.0, "period": "calendar-month",
            "alerts_enabled": True, "alert_thresholds": [90, 100],
        }},
    }) + "\n")
    # Inject deterministic Codex spend crossing BOTH thresholds.
    monkeypatch.setitem(
        ns, "_sum_codex_cost_for_range",
        lambda start, end, *, speed="auto": 200.0)
    captured = []
    monkeypatch.setitem(
        ns, "_dispatch_alert_notification",
        lambda payload, *, mode="real", **k: captured.append((payload, mode)))

    # The production reroute (hook-tick / `cctally budget`) runs the helper on the
    # cycle's conn via the codex_apply seam.
    def _leg(ctx):
        ns["maybe_record_codex_budget_milestone"](
            {}, conn=ctx.conn, alert_sink=ctx.pending_alerts)

    jr.run_stats_ingest(mode="authoritative", codex_apply=_leg)

    budget_evts = [
        line for line in _journal_lines(jr, jl)
        if line.get("t") == "evt"
        and (line.get("payload") or {}).get("kind") == "budget"
        and (line.get("payload") or {}).get("vendor") == "codex"
    ]
    assert {e["payload"]["threshold"] for e in budget_evts} == {90, 100}

    conn = ns["open_db"]()
    try:
        rows = conn.execute(
            "SELECT threshold, journal_id FROM budget_milestones "
            "WHERE vendor='codex' ORDER BY threshold").fetchall()
    finally:
        conn.close()
    assert [r[0] for r in rows] == [90, 100]
    assert all(r[1] is not None for r in rows), "harvested rows stamped journal_id"
    assert {p["threshold"] for p, _ in captured} == {90, 100}
    assert all(p["axis"] == "codex_budget" for p, _ in captured)


# --------------------------------------------------------------------------
# #496 S5b Task 15 — bounded, resumable, revalidating recovery (spec §4.5/§4.6)
# --------------------------------------------------------------------------

def _tiny_caps(jr, monkeypatch, *, records=2):
    """Force chunking at fixture scale.

    A cap a fixture cannot reach is a threshold that cannot fail, so every test
    below shrinks the record cap rather than growing the fixture to 8 MiB.
    """
    monkeypatch.setattr(jr, "_RECOVERY_CHUNK_RECORDS", records)
    monkeypatch.setattr(jr, "_RECOVERY_CHUNK_BYTES", 1 << 30)


def _seed_observations(jr, jl, count, *, root="root-a", percent_ramp=False):
    """Seed `count` quota observations.

    ``percent_ramp`` makes each observation cross a new integer percent, which
    is what `percent_milestones` needs to emit anything: it walks a
    non-decreasing high-water mark, so a run at one fixed percent materializes
    zero milestones and any comparison over that table would be vacuous.
    """
    for index in range(count):
        jr.append_record(_codex_quota_obs(
            jl, source_root_key=root, source_path=f"/codex/{root}/r.jsonl",
            line_offset=10 + index,
            captured_at_utc=_iso(10, index if percent_ramp else 0),
            used_percent=(10.0 + index if percent_ramp else 10.0),
        ), now_utc=FIXED)


class _LockTracker:
    """Every acquire/release of the cache writer flocks, in order.

    Structural: it counts ACQUISITIONS and records what was applied inside each
    one. An elapsed-time bound at fixture scale could not fail.
    """

    def __init__(self):
        self.holds: list = []
        self.held = False
        self.pending_bytes = 0

    def install(self, monkeypatch):
        import _lib_cache_writer_lock as lock

        real_acquire = lock.acquire_cache_writer_flocks
        real_release = lock.release_cache_writer_flocks

        def acquire(global_path, provider_path, **kwargs):
            handle = real_acquire(global_path, provider_path, **kwargs)
            if handle is not None:
                self.held = True
                # The chunk decoded immediately before this acquisition is the
                # one this hold is about to apply, so its encoded size is what
                # the byte cap has to bound. `_track_chunk_bytes` fills
                # `pending_bytes`; without it the field stayed 0 and the byte
                # axis of spec §8 criterion 2 was never observed.
                self.holds.append(
                    {"records": 0, "bytes": self.pending_bytes})
                self.pending_bytes = 0
            return handle

        def release(handle):
            self.held = False
            return real_release(handle)

        monkeypatch.setattr(lock, "acquire_cache_writer_flocks", acquire)
        monkeypatch.setattr(lock, "release_cache_writer_flocks", release)
        return self


class _JournalIOWatch:
    """Journal opens, and which of them happened while a cache flock was held.

    `os.open` is intercepted alongside `builtins.open` because the journal
    readers in `bin/_cctally_journal.py` use it directly — segment append at
    `:446` and `:546`, segment read at `:9868` — and a `builtins.open` patch
    cannot see any of them. `journal_opens` is the count that makes an empty
    `violations` list mean something.
    """

    _SUFFIX = ".jsonl"

    def __init__(self, tracker):
        self.tracker = tracker
        self.journal_opens = 0
        self.violations: list = []
        # A journal segment, and nothing else. Any `.jsonl`, `.ndjson` or `.log`
        # counted as one, so the `journal_opens > 0` reading that makes an empty
        # `violations` list mean something could have been satisfied by a log
        # file the replay never read from the journal at all.
        self.prefix = importlib.import_module("_lib_journal").SEGMENT_PREFIX

    def _note(self, path) -> None:
        try:
            text = os.fsdecode(path)
        except TypeError:
            return  # An open relative to a directory fd, not a path.
        name = os.path.basename(text)
        if not (name.startswith(self.prefix) and name.endswith(self._SUFFIX)):
            return
        self.journal_opens += 1
        if self.tracker.held:
            self.violations.append("opened %s under the flocks" % text)

    def install(self, monkeypatch):
        real_open, real_os_open = open, os.open

        def guarded_open(file, *args, **kwargs):
            self._note(file)
            return real_open(file, *args, **kwargs)

        def guarded_os_open(path, *args, **kwargs):
            self._note(path)
            return real_os_open(path, *args, **kwargs)

        monkeypatch.setattr("builtins.open", guarded_open)
        monkeypatch.setattr(os, "open", guarded_os_open)
        return self


def test_the_journal_io_guard_reports_an_open_taken_under_a_hold(
    tmp_path, monkeypatch,
):
    """The mutation the silence above is only meaningful against.

    Moving a journal open under the flocks is the regression the deleted
    wall-clock ceiling existed to catch, so the guard has to be shown reporting
    it. Both interception arms are exercised, because the readers this is
    guarding use `os.open` and a `builtins.open` patch alone reports nothing at
    all — which is indistinguishable from the property holding.
    """
    tracker = _LockTracker()
    watch = _JournalIOWatch(tracker).install(monkeypatch)
    segment = tmp_path / "observations-000001.jsonl"
    segment.write_bytes(b"{}\n")

    tracker.held = False
    with open(segment, "rb"):
        pass
    os.close(os.open(str(segment), os.O_RDONLY))
    assert watch.journal_opens == 2, watch.journal_opens
    assert watch.violations == []

    tracker.held = True
    with open(segment, "rb"):
        pass
    os.close(os.open(str(segment), os.O_RDONLY))
    assert watch.journal_opens == 4, watch.journal_opens
    assert len(watch.violations) == 2, watch.violations
    assert all("observations-000001.jsonl" in line for line in watch.violations)


def _count_applies(jr, monkeypatch, tracker):
    """Attribute each applied observation to the hold it landed in."""
    real = jr._apply_quota_records

    def wrapper(cache, records, **kwargs):
        records = list(records)
        if tracker.holds:
            tracker.holds[-1]["records"] += len(records)
        return real(cache, records, **kwargs)

    monkeypatch.setattr(jr, "_apply_quota_records", wrapper)


def _track_chunk_bytes(jr, monkeypatch, tracker):
    """Attribute each chunk's ENCODED size to the hold that applies it.

    The decode runs immediately before the acquisition, so the bytes counted
    here are exactly the bytes that hold carries.
    """
    real = jr._decoded_quota_stream

    def stream(raw, cutover, counters=None):
        raw = list(raw)
        tracker.pending_bytes += sum(len(line) + 1 for line in raw)
        return real(raw, cutover, counters)

    monkeypatch.setattr(jr, "_decoded_quota_stream", stream)


def _observation_line_sizes(core):
    """Encoded sizes of the seeded observation lines, measured not guessed."""
    sizes: list = []
    for path in sorted(core.JOURNAL_DIR.glob("observations-*.jsonl")):
        for line in path.read_bytes().split(b"\n"):
            if line:
                sizes.append(len(line) + 1)
    return sizes


def test_recovery_chunks_and_every_hold_respects_the_record_cap(
    tmp_path, monkeypatch,
):
    """Spec §4.5: each chunk is capped by BOTH encoded bytes and record count.

    The paired assertion that it actually chunked is what stops this passing
    vacuously — one unbounded hold trivially satisfies "every hold is under the
    cap" only if the cap is larger than the fixture.
    """
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    ns["open_cache_db"]().close()
    _tiny_caps(jr, monkeypatch, records=2)
    _seed_observations(jr, jl, 7)

    tracker = _LockTracker().install(monkeypatch)
    _count_applies(jr, monkeypatch, tracker)
    result = _rebuild(jr)

    assert result.quota_cache_coverage["status"] == "recovered"
    assert result.quota_cache_coverage["replayedObservations"] == 7
    assert len(tracker.holds) > 1, "it must actually have chunked"
    assert all(hold["records"] <= 2 for hold in tracker.holds), tracker.holds
    assert [row[1] for row in _quota_rows(ns)] == list(range(10, 17))


def test_recovery_chunks_and_every_hold_respects_the_byte_cap(
    tmp_path, monkeypatch,
):
    """The OTHER axis of spec §8 criterion 2, which nothing observed.

    `_tiny_caps` pins `_RECOVERY_CHUNK_BYTES` at 1 GiB, so no leg-level test
    could reach the byte cap and the shipped assertion covered records only.
    `chunk_spans` is unit-tested on both axes, so what is unobserved is that the
    LEG passes the byte cap through and that a hold respects it. The record cap
    is left at its production value here, so it cannot be the reason this chunks.
    """
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    core = importlib.import_module("_cctally_core")
    ns["open_cache_db"]().close()
    _seed_observations(jr, jl, 7)

    sizes = _observation_line_sizes(core)
    assert len(sizes) == 7, sizes
    cap = 2 * max(sizes)
    monkeypatch.setattr(jr, "_RECOVERY_CHUNK_BYTES", cap)
    assert max(sizes) <= cap, (
        "no single record may exceed the cap, or the oversized-record carve-out "
        "would be what this test observed")

    tracker = _LockTracker().install(monkeypatch)
    _track_chunk_bytes(jr, monkeypatch, tracker)
    _count_applies(jr, monkeypatch, tracker)
    result = _rebuild(jr)

    assert result.quota_cache_coverage["status"] == "recovered"
    assert len(tracker.holds) > 1, "it must actually have chunked on bytes"
    assert all(hold["bytes"] <= cap for hold in tracker.holds), tracker.holds
    assert sum(hold["bytes"] for hold in tracker.holds) == sum(sizes)
    assert all(
        hold["records"] <= jr._RECOVERY_CHUNK_RECORDS
        for hold in tracker.holds), tracker.holds
    assert [row[1] for row in _quota_rows(ns)] == list(range(10, 17))


def test_no_journal_input_or_record_decoding_happens_under_the_flocks(
    tmp_path, monkeypatch,
):
    """The structural replacement for the deleted wall-clock ceiling.

    `tests/test_rebuild_benchmark.py` used to bound the flock hold in seconds.
    That measured the machine: at fixture scale the hold is one to two seconds
    against a production budget of ten microseconds per line, so a bound loose
    enough to survive a loaded runner could not fail a real regression. What the
    ceiling existed to catch is a structural change — file input or a second
    traversal moving inside the flocks — and that is a property, not a duration.

    Two things must never happen while either cache writer flock is held: a
    journal segment must not be opened, and a quota record must not be decoded.
    Both are asserted by observing the calls, not by timing them.
    """
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    ns["open_cache_db"]().close()
    _tiny_caps(jr, monkeypatch)
    _seed_observations(jr, jl, 7)

    tracker = _LockTracker().install(monkeypatch)
    watch = _JournalIOWatch(tracker).install(monkeypatch)

    lib = importlib.import_module("_lib_journal")
    real_decode = lib.decode_line
    decodes = {"total": 0, "under_lock": 0}

    def guarded_decode(raw, *args, **kwargs):
        decodes["total"] += 1
        if tracker.held:
            decodes["under_lock"] += 1
        return real_decode(raw, *args, **kwargs)

    monkeypatch.setattr(lib, "decode_line", guarded_decode)

    result = _rebuild(jr)
    assert result.quota_cache_coverage["status"] == "recovered"
    assert len(tracker.holds) > 1, "the fixture must span more than one chunk"
    assert not watch.violations, watch.violations
    assert decodes["under_lock"] == 0
    # Both guards must have had something to observe, or the two readings above
    # are absences of observation rather than absences of the thing. The open
    # count is the half that was missing: the recovery function receives
    # `quota_raw` already in memory, so a guard that saw zero journal opens
    # anywhere would report exactly the same silence whether the property held
    # or the interception was simply on the wrong function.
    assert decodes["total"] > 0
    assert watch.journal_opens > 0, (
        "no journal file was opened by either `open` or `os.open` during the "
        "replay, so the open guard observed nothing"
    )


def _replay_against_a_competitor(jr, monkeypatch):
    """Replay while an outside party takes both flocks in every released window.

    Returns the tracker, one entry per successful outside acquisition recording
    how many leg holds had happened by then, and the rebuild result.

    The competitor uses the primitives captured BEFORE any tracking is
    installed. Routing it through the tracker instead is what made the earlier
    form unfailable: its acquisition appended a hold of its own, so a replay
    that took the locks exactly once still reported more than one hold.
    """
    import _lib_cache_writer_lock as lock

    raw_acquire = lock.acquire_cache_writer_flocks
    raw_release = lock.release_cache_writer_flocks

    tracker = _LockTracker().install(monkeypatch)
    tracked_acquire = lock.acquire_cache_writer_flocks
    tracked_release = lock.release_cache_writer_flocks

    paths: dict = {}
    contended: list = []

    def remember(global_path, provider_path, **kwargs):
        paths["global"], paths["provider"] = global_path, provider_path
        return tracked_acquire(global_path, provider_path, **kwargs)

    def release_then_contend(handle):
        outcome = tracked_release(handle)
        if paths:
            other = raw_acquire(paths["global"], paths["provider"], timeout=0.5)
            if other is not None:
                contended.append(len(tracker.holds))
                raw_release(other)
        return outcome

    monkeypatch.setattr(lock, "acquire_cache_writer_flocks", remember)
    monkeypatch.setattr(lock, "release_cache_writer_flocks", release_then_contend)
    return tracker, contended, _rebuild(jr)


def test_a_competitor_can_take_both_flocks_between_two_chunks(
    tmp_path, monkeypatch,
):
    """Chunking is only useful if the gap between chunks is a real one.

    A leg that released and immediately re-acquired without ever yielding would
    satisfy every cap while still holding the locks for the whole replay. The
    competitor here takes both flocks itself in the released window, which no
    duration bound could distinguish from a leg that never let go.
    """
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    ns["open_cache_db"]().close()
    _tiny_caps(jr, monkeypatch)
    _seed_observations(jr, jl, 7)

    tracker, contended, result = _replay_against_a_competitor(jr, monkeypatch)

    assert result.quota_cache_coverage["status"] == "recovered"
    assert len(tracker.holds) > 1, "the fixture must span more than one chunk"
    between = [taken for taken in contended if taken < len(tracker.holds)]
    assert between, (
        "no competitor took the flocks in a gap that still had a chunk after "
        "it, so the chunking yields nothing: %r holds, contended after %r"
        % (len(tracker.holds), contended)
    )


def test_the_between_chunks_guard_fails_when_the_replay_never_chunks(
    tmp_path, monkeypatch,
):
    """The single-chunk scaffold the guard above has to be able to fail on.

    Two separate defects made the old form unfailable. The tracker was
    installed twice and the competitor's own acquisition went through it, so
    `len(tracker.holds)` read three on a replay that took the locks once. And
    "a competitor acquired at all" is satisfied by the release at the END of a
    single chunk, which is not a gap between chunks. This pins both: one chunk,
    one hold, and the outside acquisition that follows it is not counted as
    happening between anything.
    """
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    ns["open_cache_db"]().close()
    monkeypatch.setattr(jr, "_RECOVERY_CHUNK_RECORDS", 1000)
    monkeypatch.setattr(jr, "_RECOVERY_CHUNK_BYTES", 1 << 30)
    _seed_observations(jr, jl, 7)

    tracker, contended, result = _replay_against_a_competitor(jr, monkeypatch)

    assert result.quota_cache_coverage["status"] == "recovered"
    assert len(tracker.holds) == 1, tracker.holds
    assert [taken for taken in contended if taken < len(tracker.holds)] == []


def _leave_the_cache_covered(ns, jr):
    """Leave the cache covered, so the next rebuild takes the intact path."""
    assert _rebuild(jr).quota_cache_coverage["status"] == "recovered"
    assert _coverage(ns) is not None


def _force_a_full_replay(ns, jr):
    """A reference rebuild whose CACHE leg actually replays.

    Spec §7 case 7 asks for a forced full replay of both halves. A bare
    `_rebuild(jr)` is not that: the certificate the pass under test minted makes
    the reference take the certified fast path, so the cache half is compared
    against a certificate the pass under test produced rather than against an
    independent replay. Dropping the certificate first is what forces the
    recovery path, and the returned `recovered` status is the proof it took it.

    **Forcing the path is necessary and not sufficient.** The replay then runs
    into an already-complete cache, and spec §2.2 states quota replay is
    `INSERT OR IGNORE` — "a wrong pre-existing row survives a replay from byte
    zero". So a reference replaying into the populated cache cannot DISAGREE
    with the pass under test even if that pass wrote wrong rows, which leaves
    the cache half of the comparison near-convergent. The Codex observation
    rows are therefore deleted too, so the reference replays into an empty
    cache and the comparison can actually fail. This is the technique
    `test_the_reconciliation_path_emits_no_conflict_line` already uses.
    """
    cache = importlib.import_module("_cctally_cache")
    conn = ns["open_cache_db"]()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cache._invalidate_codex_journal_coverage_certificate(conn)
        conn.execute("DELETE FROM quota_window_snapshots WHERE source='codex'")
        conn.commit()
    finally:
        conn.close()
    assert _coverage(ns) is None
    reference = _rebuild(jr)
    assert reference.quota_cache_coverage["status"] == "recovered", (
        "the reference rebuild took the certified fast path, so it replayed "
        "nothing and the cache half of the comparison is not a full replay")
    return reference


def test_the_retained_snapshot_closes_before_the_open_block_projection(
    tmp_path, monkeypatch,
):
    """The pin's DURATION, asserted as CLOSURE and not only as ordering.

    An open WAL read transaction holds a read mark, so a
    `wal_checkpoint(TRUNCATE)` from any other process returns busy for as long
    as the retained snapshot lives — which disables both of issue #297's
    persistent defences. Measured on a 1.6 GB journal, the structural fold is
    0.56 s and the open-block projection is 27.7 s, so reading the bundle
    between them is what bounds the window. Never elapsed time: at fixture
    scale a wall-clock bound could not fail.

    Ordering alone is not the property. The earlier form asserted only that the
    bundle is read before the backfill, which deleting the `finally:
    _close_coverage_snapshot(snapshot)` in `_read_quota_projection_bundle`
    would leave green — the read would still happen first and the connection
    would still be open through the whole projection. So the snapshot connection
    is captured and its closure is observed at the point the backfill runs,
    which is the state the WAL claim actually depends on.
    """
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    ns["open_cache_db"]().close()
    _seed_observations(jr, jl, 3)
    _leave_the_cache_covered(ns, jr)

    events: list = []
    real_leg = jr._rebuild_quota_cache_leg_raw
    real_bundle = jr._read_quota_projection_bundle
    real_backfill = ns["_backfill_five_hour_blocks"]

    def leg(*args, **kwargs):
        events.append("leg")
        return real_leg(*args, **kwargs)

    retained: list = []
    closed_at_backfill: list = []

    def bundle(snapshot_out):
        events.append("bundle")
        # Captured BEFORE the call, because the call is what closes it.
        if snapshot_out and snapshot_out[0] is not None:
            retained.append(snapshot_out[0].conn)
        return real_bundle(snapshot_out)

    def backfill(conn, **kwargs):
        events.append("backfill")
        for snap_conn in retained:
            try:
                snap_conn.execute("SELECT 1")
            except sqlite3.ProgrammingError:
                closed_at_backfill.append(True)
            else:
                closed_at_backfill.append(False)
        return real_backfill(conn, **kwargs)

    monkeypatch.setattr(jr, "_rebuild_quota_cache_leg_raw", leg)
    monkeypatch.setattr(jr, "_read_quota_projection_bundle", bundle)
    monkeypatch.setitem(ns, "_backfill_five_hour_blocks", backfill)

    assert _rebuild(jr).quota_cache_coverage["status"] == "covered"

    tail = events[events.index("leg"):]
    assert tail[:2] == ["leg", "bundle"], tail
    assert "backfill" in tail[2:], (
        "the open-block projection must run AFTER the snapshot is released")
    # Non-vacuity: a retained snapshot really existed on this path, so the
    # closure assertion below is about a real connection rather than an empty
    # list.
    assert retained, "the intact path must have retained a coverage snapshot"
    assert closed_at_backfill == [True] * len(retained), (
        "the retained cache snapshot was still open during the open-block "
        "projection, so it pinned the cache.db WAL across it")


def test_the_intact_path_reads_the_projection_bundle_from_one_snapshot(
    tmp_path, monkeypatch,
):
    """Spec §4.4 and acceptance criterion 1: the certificate, the sequence, the
    source roots, the observations and the ledger state come from ONE read-only
    WAL snapshot.

    The leg used to capture the first two and `rematerialize_quota_projection_
    for_rebuild` opened its own connection later, so a destructive clear landing
    between them published a generation whose quota projection was materialized
    from a cleared cache while the coverage verdict already read `covered`. The
    clear here runs in exactly that window.
    """
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    quota_mod = importlib.import_module("_cctally_quota")
    cache_mod = importlib.import_module("_cctally_cache")
    ns["open_cache_db"]().close()
    _seed_observations(jr, jl, 7)
    # The projection materializes nothing for a root it does not consider
    # active, and `codex_source_roots` is populated by the rollout walk rather
    # than by a journal replay, so the fixture supplies it. Without this the
    # `quota_window_blocks` assertion below would read 0 whatever the snapshot
    # said, which is a threshold that cannot fail.
    conn = ns["open_cache_db"]()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO codex_source_roots"
            "(source_root_key, canonical_root_path, first_seen_utc, "
            " last_seen_utc) VALUES (?,?,?,?)",
            ("root-a", "/codex/root-a", _iso(9), _iso(10)))
        conn.commit()
    finally:
        conn.close()
    _leave_the_cache_covered(ns, jr)

    # Every observation the projection is entitled to see, taken before the
    # clear so the comparison names a real population rather than a convergent
    # empty one.
    conn = ns["open_cache_db"]()
    try:
        expected = len(quota_mod.load_quota_projection_bundle(conn).observations)
    finally:
        conn.close()
    assert expected == 7, expected

    seen: list = []
    real_bundle = quota_mod.load_quota_projection_bundle

    def counting_bundle(cache_conn):
        bundle = real_bundle(cache_conn)
        seen.append(len(bundle.observations))
        return bundle

    monkeypatch.setattr(
        quota_mod, "load_quota_projection_bundle", counting_bundle)

    # Fire the clear the moment the coverage verdict has been decided — which is
    # after the leg's snapshot opened and before the projection reads it.
    real_resolve = jr._resolve_quota_cache_coverage

    def resolve_then_clear(*args, **kwargs):
        result = real_resolve(*args, **kwargs)
        clear_conn = ns["open_cache_db"]()
        try:
            clear_conn.execute("BEGIN IMMEDIATE")
            cache_mod._clear_codex_derived_rows(clear_conn)
            clear_conn.commit()
        finally:
            clear_conn.close()
        return result

    monkeypatch.setattr(
        jr, "_resolve_quota_cache_coverage", resolve_then_clear)
    result = _rebuild(jr)

    assert result.quota_cache_coverage["status"] == "covered"
    assert result.quota_cache_coverage["replayedObservations"] == 0
    conn = ns["open_cache_db"]()
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM quota_window_snapshots "
            "WHERE source='codex'").fetchone()[0] == 0, (
                "the clear must really have emptied the cache, or this test "
                "observes nothing")
    finally:
        conn.close()
    assert seen == [expected], (
        "the projection must have read the pre-clear snapshot the coverage "
        f"verdict was decided against, not a second connection; saw {seen}")
    # The observable consequence, which is what the criterion is actually
    # about: the published generation's quota projection describes the seven
    # observations. Reading a second connection would have materialized it from
    # the cleared cache and published an empty one under a `covered` verdict.
    stats = ns["open_db"]()
    try:
        blocks = stats.execute(
            "SELECT COUNT(*) FROM quota_window_blocks "
            "WHERE source='codex'").fetchone()[0]
    finally:
        stats.close()
    assert blocks > 0, (
        "a generation published under a `covered` verdict must carry the "
        "projection that verdict was decided against")


def test_without_the_retained_snapshot_the_same_clear_publishes_an_empty_projection(
    tmp_path, monkeypatch,
):
    """Non-vacuity for the criterion above, and a statement of the defect.

    Same fixture, same clear, same window — only the retained snapshot is
    withheld, so the projection opens its own connection the way the pre-change
    leg did. The published generation then carries an EMPTY quota projection
    under a `covered` verdict, which is exactly the outcome §4.4 exists to
    prevent.
    """
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    cache_mod = importlib.import_module("_cctally_cache")
    ns["open_cache_db"]().close()
    _seed_observations(jr, jl, 7)
    conn = ns["open_cache_db"]()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO codex_source_roots"
            "(source_root_key, canonical_root_path, first_seen_utc, "
            " last_seen_utc) VALUES (?,?,?,?)",
            ("root-a", "/codex/root-a", _iso(9), _iso(10)))
        conn.commit()
    finally:
        conn.close()
    _leave_the_cache_covered(ns, jr)

    real_resolve = jr._resolve_quota_cache_coverage

    def resolve_then_clear(*args, **kwargs):
        result = real_resolve(*args, **kwargs)
        clear_conn = ns["open_cache_db"]()
        try:
            clear_conn.execute("BEGIN IMMEDIATE")
            cache_mod._clear_codex_derived_rows(clear_conn)
            clear_conn.commit()
        finally:
            clear_conn.close()
        return result

    monkeypatch.setattr(
        jr, "_resolve_quota_cache_coverage", resolve_then_clear)
    monkeypatch.setattr(
        jr, "_read_quota_projection_bundle", lambda _retained: None)
    result = _rebuild(jr)

    assert result.quota_cache_coverage["status"] == "covered"
    stats = ns["open_db"]()
    try:
        blocks = stats.execute(
            "SELECT COUNT(*) FROM quota_window_blocks "
            "WHERE source='codex'").fetchone()[0]
    finally:
        stats.close()
    assert blocks == 0


def test_the_recovery_path_reads_its_own_snapshot_after_writing(
    tmp_path, monkeypatch,
):
    """Non-vacuity, and the reason the retained snapshot is intact-path only.

    A snapshot taken before recovery's writes would miss exactly the rows
    recovery restored, so the leg closes it and the projection opens its own.
    """
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    quota_mod = importlib.import_module("_cctally_quota")
    ns["open_cache_db"]().close()
    _seed_observations(jr, jl, 7)

    seen: list = []
    real_bundle = quota_mod.load_quota_projection_bundle

    def counting_bundle(cache_conn):
        bundle = real_bundle(cache_conn)
        seen.append(len(bundle.observations))
        return bundle

    monkeypatch.setattr(
        quota_mod, "load_quota_projection_bundle", counting_bundle)
    result = _rebuild(jr)

    assert result.quota_cache_coverage["status"] == "recovered"
    assert seen == [7], (
        "the projection must see the rows recovery just wrote; a retained "
        f"pre-write snapshot would have seen 0. saw {seen}")


def test_a_retained_snapshot_is_closed_when_the_rebuild_raises(
    tmp_path, monkeypatch,
):
    """A retained snapshot holds an open read transaction on `cache.db`, which
    pins the WAL against checkpointing for as long as it lives. The ordinary
    path closes it; this covers the paths that raise before reaching that."""
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    ns["open_cache_db"]().close()
    _seed_observations(jr, jl, 7)
    _leave_the_cache_covered(ns, jr)

    closed: list = []
    real_close = jr._close_coverage_snapshot

    def counted(snapshot):
        if snapshot is not None and snapshot.conn is not None:
            closed.append(id(snapshot.conn))
        return real_close(snapshot)

    monkeypatch.setattr(jr, "_close_coverage_snapshot", counted)

    def boom(*_a, **_k):
        raise RuntimeError("fold exploded")

    monkeypatch.setattr(jr, "_read_quota_projection_bundle", boom)
    with pytest.raises(RuntimeError):
        _rebuild(jr)
    assert closed, "the retained snapshot must have been closed by the finally"


def test_no_journal_segment_is_opened_while_the_flocks_are_held(
    tmp_path, monkeypatch,
):
    """The first half of spec §8 criterion 2, which only the decode half tested.

    Counts PHYSICAL opens through `_open_segment_for_read`, the one chokepoint
    every segment read goes through — not lines, not bytes.
    """
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    ns["open_cache_db"]().close()
    _tiny_caps(jr, monkeypatch, records=2)
    _seed_observations(jr, jl, 7)

    tracker = _LockTracker().install(monkeypatch)
    opened_while_held: list = []
    all_opens: list = []
    real_open = jr._open_segment_for_read

    def counted(seg_path, *args, **kwargs):
        all_opens.append(str(seg_path))
        if tracker.held:
            opened_while_held.append(str(seg_path))
        return real_open(seg_path, *args, **kwargs)

    monkeypatch.setattr(jr, "_open_segment_for_read", counted)
    assert _rebuild(jr).quota_cache_coverage["status"] == "recovered"

    assert opened_while_held == []
    assert all_opens, "the instrumentation must have seen the reads it counts"
    assert len(tracker.holds) > 1, "it must have chunked, or this proves little"


def test_an_unchunked_cap_takes_exactly_one_hold(tmp_path, monkeypatch):
    """Non-vacuity for the count above: with a cap the fixture cannot reach,
    the same instrumentation must record ONE hold."""
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    ns["open_cache_db"]().close()
    _seed_observations(jr, jl, 7)
    tracker = _LockTracker().install(monkeypatch)
    assert _rebuild(jr).quota_cache_coverage["status"] == "recovered"
    assert len(tracker.holds) == 1


def test_nothing_is_decoded_while_the_flocks_are_held(tmp_path, monkeypatch):
    """The decode is per chunk and OUTSIDE the hold.

    S4 moved the JSON decode inside the flocks and that is what lengthened the
    hold; chunking is only a win if each chunk's decode happens before its lock
    is requested.
    """
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    ns["open_cache_db"]().close()
    _tiny_caps(jr, monkeypatch, records=2)
    _seed_observations(jr, jl, 7)

    tracker = _LockTracker().install(monkeypatch)
    decoded_while_held: list = []
    real_stream = jr._decoded_quota_stream

    def stream(raw, cutover, counters=None):
        for record in real_stream(raw, cutover, counters):
            if tracker.held:
                decoded_while_held.append(record)
            yield record

    monkeypatch.setattr(jr, "_decoded_quota_stream", stream)
    assert _rebuild(jr).quota_cache_coverage["status"] == "recovered"
    assert decoded_while_held == []
    assert len(tracker.holds) > 1, "it must have chunked, or this proves little"


def test_peak_decoded_records_stay_at_one_chunk(tmp_path, monkeypatch):
    """Decoding everything before chunking the applies would re-materialize
    roughly six gigabytes of observation dictionaries — exactly what S4
    removed."""
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    ns["open_cache_db"]().close()
    _tiny_caps(jr, monkeypatch, records=2)
    _seed_observations(jr, jl, 7)

    peak: list = [0]
    real = jr._apply_quota_records

    def wrapper(cache, records, **kwargs):
        records = list(records)
        peak[0] = max(peak[0], len(records))
        return real(cache, records, **kwargs)

    monkeypatch.setattr(jr, "_apply_quota_records", wrapper)
    assert _rebuild(jr).quota_cache_coverage["status"] == "recovered"
    assert 0 < peak[0] <= 2


def test_a_competitor_acquires_between_chunks(tmp_path, monkeypatch):
    """This is the assertion that proves the hold was RELEASED, not merely
    shortened. It runs in the seam the loop exposes AFTER releasing both flocks
    and BEFORE requesting them again, and it takes the real locks."""
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    ns["open_cache_db"]().close()
    _tiny_caps(jr, monkeypatch, records=2)
    _seed_observations(jr, jl, 7)

    import _lib_cache_writer_lock as lock
    import _cctally_core as core

    acquired: list = []

    def competitor(_chunks):
        handle = lock.acquire_cache_writer_flocks(
            core.CACHE_LOCK_PATH, core.CACHE_LOCK_CODEX_PATH)
        acquired.append(handle is not None)
        if handle is not None:
            lock.release_cache_writer_flocks(handle)

    monkeypatch.setattr(jr, "_RECOVERY_BETWEEN_CHUNKS", competitor)
    assert _rebuild(jr).quota_cache_coverage["status"] == "recovered"
    assert acquired and all(acquired), acquired


def _reference_rows(ns, jr, jl, count):
    """The cache state an UNCHUNKED recovery leaves, for comparison."""
    return list(range(10, 10 + count))


def test_interleaving_1_a_destructive_clear_restarts_from_zero(
    tmp_path, monkeypatch,
):
    """`cache-sync --rebuild` can take the locks between chunks and clear both
    the materialized state and the certificate. Continuing from an in-memory
    cursor would mint a certificate claiming coverage the cache lacks."""
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    ns["open_cache_db"]().close()
    _tiny_caps(jr, monkeypatch, records=2)
    _seed_observations(jr, jl, 7)

    cache_mod = importlib.import_module("_cctally_cache")
    fired: list = []

    def clear_once(chunks):
        if fired:
            return
        fired.append(chunks)
        conn = ns["open_cache_db"]()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cache_mod._clear_codex_derived_rows(conn)
            conn.commit()
        finally:
            conn.close()

    monkeypatch.setattr(jr, "_RECOVERY_BETWEEN_CHUNKS", clear_once)
    result = _rebuild(jr)

    assert fired, "the destructive clear must have run between two chunks"
    # The convergence assertion comes FIRST, because it is the one that decides
    # whether the chunking may ship: the final cache state must match the
    # unchunked form. The clear removed the rows the first attempt wrote, and
    # the restart re-materialized every one. Without the restart the pass
    # resumes past the hole and leaves the cache SHORT.
    assert [row[1] for row in _quota_rows(ns)] == _reference_rows(ns, jr, jl, 7)
    assert result.quota_cache_coverage["restarts"] == 1
    assert result.quota_cache_coverage["status"] == "recovered"
    assert result.quota_cache_coverage["complete"] is True
    assert _coverage(ns) is not None


def test_interleaving_2_a_file_reset_over_retained_bytes_restarts(
    tmp_path, monkeypatch,
):
    """`_delete_codex_file_derived_rows` deletes `quota_window_snapshots` rows
    for one path while the journal still retains every observation for those
    bytes, and it deletes the progress record with the certificate, so the pass
    restarts rather than resuming over the hole it just made.

    This constructs that function DIRECTLY rather than through
    `_write_codex_file_batch(reset_file=True)`. The batch branch is pinned by
    `test_a_path_labelled_invalidate_actually_invalidates` in
    `tests/test_cache_coverage_496_s5b.py`; what is under test here is the
    interleaving, so the shortest construction of the delete is the honest one
    to name.
    """
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    ns["open_cache_db"]().close()
    _tiny_caps(jr, monkeypatch, records=2)
    _seed_observations(jr, jl, 7)

    cache_mod = importlib.import_module("_cctally_cache")
    fired: list = []

    def reset_once(chunks):
        if fired:
            return
        fired.append(chunks)
        conn = ns["open_cache_db"]()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cache_mod._delete_codex_file_derived_rows(
                conn, "/codex/root-a/r.jsonl")
            conn.commit()
        finally:
            conn.close()

    monkeypatch.setattr(jr, "_RECOVERY_BETWEEN_CHUNKS", reset_once)
    result = _rebuild(jr)

    assert fired
    # Convergence first, for the reason interleaving 1 states.
    assert [row[1] for row in _quota_rows(ns)] == _reference_rows(ns, jr, jl, 7)
    assert result.quota_cache_coverage["restarts"] == 1
    assert result.quota_cache_coverage["status"] == "recovered"


def test_interleaving_3_a_newer_pass_is_not_overwritten(
    tmp_path, monkeypatch,
):
    """Monotonic compare-and-swap: an older worker cannot overwrite a newer
    pass's progress, and it stops rather than restarting — two passes that each
    restarted on seeing the other would make no progress at all."""
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    ns["open_cache_db"]().close()
    _tiny_caps(jr, monkeypatch, records=2)
    _seed_observations(jr, jl, 7)

    cache_mod = importlib.import_module("_cctally_cache")
    kernel = importlib.import_module("_lib_cache_coverage")
    newer: dict = {}
    fired: list = []

    def newer_pass(chunks):
        if fired:
            return
        fired.append(chunks)
        conn = ns["open_cache_db"]()
        try:
            stored = cache_mod.load_codex_recovery_progress(conn)
            assert stored is not None, "the pass must have persisted progress"
            newer.update({
                **stored,
                "passId": "someone-else",
                "startedAt": int(stored["startedAt"]) + 1_000_000,
                "chunks": int(stored["chunks"]) + 5,
            })
            conn.execute("BEGIN IMMEDIATE")
            assert cache_mod._store_codex_recovery_progress(conn, newer)
            conn.commit()
        finally:
            conn.close()

    monkeypatch.setattr(jr, "_RECOVERY_BETWEEN_CHUNKS", newer_pass)
    result = _rebuild(jr)

    assert fired
    assert result.quota_cache_coverage["status"] == "incomplete"
    assert result.quota_cache_coverage["complete"] is False
    assert result.quota_cache_coverage["remainder"]["reason"] == "newerPass"
    conn = ns["open_cache_db"]()
    try:
        assert cache_mod.load_codex_recovery_progress(conn) == newer
    finally:
        conn.close()
    assert _coverage(ns) is None, (
        "a pass that yielded must not have minted a certificate"
    )
    assert kernel.YIELD == "yield"


def _clear_after_first_chunk(ns, jr, monkeypatch, fired):
    """Arm one destructive clear in the between-chunks seam."""
    cache_mod = importlib.import_module("_cctally_cache")

    def clear_once(chunks):
        if fired:
            return
        fired.append(chunks)
        conn = ns["open_cache_db"]()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cache_mod._clear_codex_derived_rows(conn)
            conn.commit()
        finally:
            conn.close()

    monkeypatch.setattr(jr, "_RECOVERY_BETWEEN_CHUNKS", clear_once)


def test_a_restart_does_not_double_count_the_replay_counters(
    tmp_path, monkeypatch,
):
    """Spec §4.6: one recovery reports at most what a single unchunked call
    reports today.

    `_decoded_quota_stream` increments on every pass, so a restart re-decodes
    the prefix and inflates `traversal["quota_replay"]` against the journal it
    actually consumed. The expected numbers are the seeded population exactly,
    which is a value a double count cannot produce.
    """
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    ns["open_cache_db"]().close()
    _tiny_caps(jr, monkeypatch, records=2)
    _seed_observations(jr, jl, 7)

    fired: list = []
    _clear_after_first_chunk(ns, jr, monkeypatch, fired)
    result = _rebuild(jr)

    assert fired, "the clear must have run between two chunks"
    assert result.quota_cache_coverage["restarts"] == 1
    counts = result.traversal["quota_replay"]
    assert counts["lines"] == 7, counts
    assert counts["decodes"] == 7, counts
    assert counts["bytes"] == sum(
        _observation_line_sizes(importlib.import_module("_cctally_core"))
    ), counts


def test_a_restart_does_not_re_emit_the_conflict_line(
    tmp_path, monkeypatch, capsys,
):
    """`reported_conflicts` was cleared on every restart, so a destructive
    writer between two chunks made one recovery report the same conflicting run
    twice — against §4.6's "at most what a single unchunked call reports"."""
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    ns["open_cache_db"]().close()
    _tiny_caps(jr, monkeypatch, records=1)

    import _cctally_cache as cache_mod
    from _lib_source_identity import codex_file_key

    source_path = "/codex/root-a/r.jsonl"
    identity = codex_file_key(
        "root-a", str(cache_mod._canonical_codex_path(Path(source_path))))
    for index in range(4):
        obs = _codex_quota_obs(
            jl, source_root_key="root-a", source_path=source_path,
            line_offset=10 + index, captured_at_utc=_iso(10))
        obs["account"] = "acct-observed"
        jr.append_record(obs, now_utc=FIXED)

    # The decision is seeded straight into the map, and the clear below does NOT
    # remove `codex_file_accounts` — it is the one family a clear deliberately
    # preserves — so the oracle still finds it after the restart.
    conn = ns["open_cache_db"]()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO codex_file_accounts "
            "(file_identity, incarnation, from_offset, root_scope, "
            " account_key, decided_at_utc) VALUES (?,?,?,?,?,?)",
            (identity, 1, 0, "root-a", "acct-decided", _iso(9)))
        conn.commit()
    finally:
        conn.close()

    fired: list = []
    _clear_after_first_chunk(ns, jr, monkeypatch, fired)
    capsys.readouterr()
    result = _rebuild(jr)

    assert fired
    assert result.quota_cache_coverage["restarts"] == 1
    err = capsys.readouterr().err
    assert err.count("codex attribution conflict") == 1, err


def test_a_pass_that_can_name_no_boundary_reports_incomplete(
    tmp_path, monkeypatch,
):
    """`covered is None` (verdict `noBoundary`) reported `complete: True` with
    `coveredHighWater: None` and left an orphan progress record behind.

    Nothing false was certified, but "cache recovery complete" was reported for
    a pass that established no coverage at all.
    """
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    cache_mod = importlib.import_module("_cctally_cache")
    kernel = importlib.import_module("_lib_cache_coverage")
    ns["open_cache_db"]().close()
    _tiny_caps(jr, monkeypatch, records=2)
    _seed_observations(jr, jl, 7)

    real_vector = jr.coverage_pinned_vector

    def without_the_high_water_segment():
        return [entry for entry in real_vector()
                if not entry[0].startswith("observations-")]

    monkeypatch.setattr(
        jr, "coverage_pinned_vector", without_the_high_water_segment)
    result = _rebuild(jr)

    coverage = result.quota_cache_coverage
    assert coverage["reason"] == kernel.REASON_NO_BOUNDARY
    assert coverage["status"] == "incomplete"
    assert coverage["complete"] is False
    assert coverage["coveredHighWater"] is None
    assert coverage["remainder"]["reason"] == "noCoverageEstablished"
    # The rows still landed — the duty was performed, only the claim was not.
    assert [row[1] for row in _quota_rows(ns)] == list(range(10, 17))
    conn = ns["open_cache_db"]()
    try:
        assert cache_mod.load_codex_journal_coverage_certificate(conn) is None
        assert cache_mod.load_codex_recovery_progress(conn) is None, (
            "a pass that established nothing must not leave an orphan record")
    finally:
        conn.close()


def test_a_refused_mint_reports_incomplete_and_leaves_the_stored_certificate(
    tmp_path, monkeypatch,
):
    """The mint stored with `prior=None, allow_mint=True` and overwrote whatever
    was present. It now refuses a certificate whose `appliedThrough` is behind
    the stored one, and a refusal is an incomplete pass rather than a
    `complete: True` over a certificate this pass did not write."""
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    cache_mod = importlib.import_module("_cctally_cache")
    kernel = importlib.import_module("_lib_cache_coverage")
    ns["open_cache_db"]().close()
    _tiny_caps(jr, monkeypatch, records=2)
    _seed_observations(jr, jl, 7)

    hw = jr.journal_high_water()
    vector = jr.coverage_pinned_vector()
    ahead = kernel.advance(
        None, covered=(hw[0], int(hw[1])),
        applied_through=(hw[0], int(hw[1]) + 1_000),
        pinned_vector=vector, physical_seq=0)
    # Deliberately unusable as a coverage claim, so the fast path still refuses
    # it and recovery runs — this test is about the MINT, not the fast path.
    ahead["identityRoot"] = "0" * 64
    conn = ns["open_cache_db"]()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cache_mod._store_codex_journal_coverage_certificate(conn, ahead)
        conn.commit()
    finally:
        conn.close()

    result = _rebuild(jr)
    coverage = result.quota_cache_coverage
    assert coverage["status"] == "incomplete"
    assert coverage["complete"] is False
    assert coverage["remainder"]["reason"] == "mintRefused"
    assert [row[1] for row in _quota_rows(ns)] == list(range(10, 17))
    conn = ns["open_cache_db"]()
    try:
        assert cache_mod.load_codex_journal_coverage_certificate(conn) == ahead
        assert cache_mod.load_codex_recovery_progress(conn) is None
    finally:
        conn.close()


def test_the_decision_governs_every_observation_under_chunking(
    tmp_path, monkeypatch,
):
    """Spec §8 criterion 3 under chunking, which nothing observed.

    Chunk 0 carries EVERY file-account decision precisely so §3.5's precedence
    rule survives chunking: the decision must already govern the observations it
    covers, and it is also what makes an interleaved additive
    `_write_codex_file_batch` harmless — so it is load-bearing for the Task 15
    deviation's safety argument. Existing coverage exercised the unchunked leg.
    """
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    ns["open_cache_db"]().close()
    _tiny_caps(jr, monkeypatch, records=2)

    import _cctally_cache as cache_mod
    from _lib_source_identity import codex_file_key

    source_path = "/codex/root-a/r.jsonl"
    identity = codex_file_key(
        "root-a", str(cache_mod._canonical_codex_path(Path(source_path))))
    # The decision is JOURNALED, not seeded into the cache, so chunk 0's
    # `_apply_file_account_records` is what has to restore it.
    jr.append_record(jl.make_codex_file_account(
        at=_iso(9), root_scope="root-a", file_identity=identity,
        incarnation=1, from_offset=0, account_key="d" * 32), now_utc=FIXED)
    for index in range(5):
        obs = _codex_quota_obs(
            jl, source_root_key="root-a", source_path=source_path,
            line_offset=10 + index, captured_at_utc=_iso(10))
        obs["account"] = "o" * 32
        jr.append_record(obs, now_utc=FIXED)

    order: list = []
    real_files = jr._apply_file_account_records
    real_quota = jr._apply_quota_records

    def files(cache, records):
        records = list(records)
        order.append(("decisions", len(records)))
        return real_files(cache, records)

    def quota(cache, records, **kwargs):
        records = list(records)
        order.append(("observations", len(records)))
        return real_quota(cache, records, **kwargs)

    monkeypatch.setattr(jr, "_apply_file_account_records", files)
    monkeypatch.setattr(jr, "_apply_quota_records", quota)
    tracker = _LockTracker().install(monkeypatch)
    result = _rebuild(jr)

    assert result.quota_cache_coverage["status"] == "recovered"
    assert len(tracker.holds) >= 3, tracker.holds
    # Every decision applied in the FIRST transaction, before any observation.
    assert order[0] == ("decisions", 1), order
    assert all(kind == "observations" for kind, _n in order[1:]), order

    conn = ns["open_cache_db"]()
    try:
        assert conn.execute(
            "SELECT account_key FROM codex_file_accounts "
            "WHERE file_identity = ?", (identity,)).fetchall() == [("d" * 32,)]
        accounts = conn.execute(
            "SELECT DISTINCT account_key FROM quota_window_snapshots "
            "WHERE source='codex' AND source_path = ?",
            (source_path,)).fetchall()
    finally:
        conn.close()
    assert accounts == [("d" * 32,)], (
        "the journal's decision must govern the observation stamp, not the "
        "other way round")


# --------------------------------------------------------------------------
# #496 S5b Task 16 — the incomplete-recovery contract (spec §4.7)
# --------------------------------------------------------------------------

def _projection_state(ns):
    """The stored flag, read RAW.

    Deliberately not through `open_db`: that opener now runs the reconciliation
    itself, so reading the flag through it would observe the state after
    reconciliation and could never see the flag this asserts about.
    """
    core = importlib.import_module("_cctally_core")
    conn = sqlite3.connect(str(core.DB_PATH))
    try:
        return conn.execute(
            "SELECT incomplete, target_version, recovery_target_json "
            "FROM stats_quota_projection_state WHERE id = 1").fetchone()
    finally:
        conn.close()


def _yield_to_a_newer_pass(ns, jr, monkeypatch, fired):
    """Stop a recovery mid-pass, leaving an uncovered remainder."""
    cache_mod = importlib.import_module("_cctally_cache")

    def newer_pass(_chunks):
        if fired:
            return
        fired.append(True)
        conn = ns["open_cache_db"]()
        try:
            stored = cache_mod.load_codex_recovery_progress(conn)
            assert stored is not None
            conn.execute("BEGIN IMMEDIATE")
            assert cache_mod._store_codex_recovery_progress(conn, {
                **stored,
                "passId": "someone-else",
                "startedAt": int(stored["startedAt"]) + 1_000,
                "chunks": int(stored["chunks"]) + 5,
            })
            conn.commit()
        finally:
            conn.close()

    monkeypatch.setattr(jr, "_RECOVERY_BETWEEN_CHUNKS", newer_pass)


def test_an_uncovered_remainder_marks_the_projection_incomplete_durably(
    tmp_path, monkeypatch,
):
    """Spec §4.7: "the next open reconciles it" is not enforceable on its own.

    `RebuildResult` is process-local, and in-place publication deliberately
    keeps already-open readers alive, so a connection can observe the incomplete
    generation without ever calling `open_db` again. The flag has to be durable
    and it has to ride into the published index.
    """
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    ns["open_cache_db"]().close()
    _tiny_caps(jr, monkeypatch, records=2)
    _seed_observations(jr, jl, 7)

    fired: list = []
    _yield_to_a_newer_pass(ns, jr, monkeypatch, fired)
    result = _rebuild(jr)

    assert fired
    assert result.quota_cache_coverage["complete"] is False
    assert result.stats_quota_projection_incomplete is True
    row = _projection_state(ns)
    assert row is not None and int(row[0]) == 1
    assert int(row[1]) == jr.PROJECTION_RECOVERY_TARGET_VERSION
    target = json.loads(row[2])
    assert target["remainder"]["reason"] == "newerPass"
    assert target["highWater"] is not None


def test_doctor_reports_the_incomplete_projection_it_can_actually_observe(
    tmp_path, monkeypatch,
):
    """The doctor leg has to read the REAL flag, not a constructed state.

    The kernel arms of `journal.quota_projection` are unit-tested in
    `tests/test_doctor_journal_legs.py`; those pass whether or not the gather
    layer ever populates the field. This runs the real gather against a real
    published generation whose recovery stopped short, so the leg is proven to
    fire on the state a user would actually be in.
    """
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    doctor = importlib.import_module("_cctally_doctor")
    lib_doctor = importlib.import_module("_lib_doctor")

    _gate_an_incomplete_projection(ns, jr, jl, monkeypatch)

    gathered = doctor.doctor_gather_state()
    assert gathered.stats_quota_projection_incomplete is True
    result = lib_doctor._check_journal_quota_projection(gathered)
    assert (result.id, result.severity) == ("journal.quota_projection", "warn")
    assert "cctally cache-sync" in (result.remediation or "")

    # Non-vacuity: a generation whose recovery completed must report the OK arm
    # through the SAME gather, so an always-warn leg cannot satisfy this.
    assert _rebuild(jr).stats_quota_projection_incomplete is False
    clean = doctor.doctor_gather_state()
    assert clean.stats_quota_projection_incomplete is False
    ok = lib_doctor._check_journal_quota_projection(clean)
    assert (ok.id, ok.severity) == ("journal.quota_projection", "ok")


def test_a_complete_recovery_leaves_the_projection_ungated(
    tmp_path, monkeypatch,
):
    """Non-vacuity: the flag has to be a statement about THIS rebuild, not a
    value that is always 1."""
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    ns["open_cache_db"]().close()
    _tiny_caps(jr, monkeypatch, records=2)
    _seed_observations(jr, jl, 7)
    result = _rebuild(jr)

    assert result.quota_cache_coverage["complete"] is True
    assert result.stats_quota_projection_incomplete is False
    row = _projection_state(ns)
    assert row is not None and int(row[0]) == 0
    assert row[2] is None


def test_the_gate_refuses_a_connection_opened_before_the_publication(
    tmp_path, monkeypatch,
):
    """The case that motivated all of this. In-place publication keeps
    already-open readers alive, so this connection never calls `open_db` again
    and only a per-transaction gate can cover it."""
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    quota_mod = importlib.import_module("_cctally_quota")
    ns["open_cache_db"]().close()
    _tiny_caps(jr, monkeypatch, records=2)
    _seed_observations(jr, jl, 7)

    before = ns["open_db"]()
    try:
        quota_mod.assert_projection_readable(before)   # readable to start with
        fired: list = []
        _yield_to_a_newer_pass(ns, jr, monkeypatch, fired)
        assert _rebuild(jr).stats_quota_projection_incomplete is True
        with pytest.raises(quota_mod.QuotaProjectionIncomplete) as excinfo:
            quota_mod.assert_projection_readable(before)
        assert excinfo.value.target_version == (
            jr.PROJECTION_RECOVERY_TARGET_VERSION)
        assert excinfo.value.recovery_target is not None
    finally:
        before.close()


def test_rollback_publication_uses_no_wal_sidecars_and_open_handle_updates(
    tmp_path, monkeypatch,
):
    """#538 removes the WAL/SHM generation from current stats operation."""
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    quota_mod = importlib.import_module("_cctally_quota")
    core = importlib.import_module("_cctally_core")
    ns["open_cache_db"]().close()
    _tiny_caps(jr, monkeypatch, records=2)
    _seed_observations(jr, jl, 7)

    before = ns["open_db"]()
    try:
        quota_mod.assert_projection_readable(before)   # readable to start with
        fired: list = []
        _yield_to_a_newer_pass(ns, jr, monkeypatch, fired)
        assert _rebuild(jr).stats_quota_projection_incomplete is True

        db = Path(str(core.DB_PATH))
        assert before.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert not Path(f"{db}-wal").exists()
        assert not Path(f"{db}-shm").exists()
        assert not Path(f"{db}-journal").exists()

        # An open autocommit handle remains usable and observes the generation
        # published after its prior statement completed.
        row = before.execute(
            "SELECT incomplete FROM stats_quota_projection_state "
            "WHERE id = 1").fetchone()
        assert row is not None and int(row[0]) == 1
        with pytest.raises(quota_mod.QuotaProjectionIncomplete):
            quota_mod.assert_projection_readable(before)
    finally:
        before.close()


def test_a_fresh_open_reconciles_the_projection_before_serving(
    tmp_path, monkeypatch,
):
    """Acceptance criterion 16's second half. The gate has to LIFT, or one
    interrupted rebuild would refuse every quota read until the next rebuild."""
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    quota_mod = importlib.import_module("_cctally_quota")
    ns["open_cache_db"]().close()
    _tiny_caps(jr, monkeypatch, records=2)
    _seed_observations(jr, jl, 7)

    fired: list = []
    _yield_to_a_newer_pass(ns, jr, monkeypatch, fired)
    assert _rebuild(jr).stats_quota_projection_incomplete is True
    assert int(_projection_state(ns)[0]) == 1
    # The interference is gone, and the leftover foreign progress record is what
    # a resumed pass has to overwrite rather than yield to a second time.
    monkeypatch.setattr(jr, "_RECOVERY_BETWEEN_CHUNKS", None)

    # The open-time reconciliation is opt-in per process: a status-line render
    # must never pay a whole-journal read, so it is armed only by
    # `cmd_cache_sync` and `cmd_dashboard`, and by nothing else. `db rebuild`
    # does not arm it (it holds the ingest lock and republishes the flag
    # itself), `doctor` deliberately does not (its report contract is
    # read-only), and the dashboard has no maintenance thread — arming is a
    # process global, so whichever dashboard thread reaches `open_db` first
    # runs the attempt.
    _arm_reconciliation(monkeypatch)
    conn = ns["open_db"]()
    try:
        quota_mod.assert_projection_readable(conn)
        assert int(conn.execute(
            "SELECT incomplete FROM stats_quota_projection_state "
            "WHERE id = 1").fetchone()[0]) == 0
    finally:
        conn.close()
    # The cache really did catch up, which is what the cleared flag asserts.
    assert [row[1] for row in _quota_rows(ns)] == list(range(10, 17))


def test_the_open_time_gate_leaves_the_flag_set_when_recovery_cannot_finish(
    tmp_path, monkeypatch,
):
    """Fail-closed. A reconciliation that cannot complete must not clear the
    flag, because clearing it would serve the partial projection it exists to
    refuse."""
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    quota_mod = importlib.import_module("_cctally_quota")
    ns["open_cache_db"]().close()
    _tiny_caps(jr, monkeypatch, records=2)
    _seed_observations(jr, jl, 7)

    fired: list = []
    _yield_to_a_newer_pass(ns, jr, monkeypatch, fired)
    assert _rebuild(jr).stats_quota_projection_incomplete is True

    monkeypatch.setattr(
        jr, "recover_quota_cache_from_journal",
        lambda *a, **k: {"status": "incomplete", "complete": False,
                         "remainder": {"reason": "locksBusy"}})
    _arm_reconciliation(monkeypatch)
    conn = ns["open_db"]()
    try:
        with pytest.raises(quota_mod.QuotaProjectionIncomplete):
            quota_mod.assert_projection_readable(conn)
    finally:
        conn.close()


def test_the_open_time_gate_is_skipped_inside_the_serialized_writer(
    tmp_path, monkeypatch,
):
    """A context holding the ingest lock is the serialized writer — a rebuild or
    an ingest cycle — and it sets or clears the flag itself. Reconciling
    underneath it would run a second recovery inside its transaction."""
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    store = importlib.import_module("_cctally_store")
    ns["open_cache_db"]().close()
    _tiny_caps(jr, monkeypatch, records=2)
    _seed_observations(jr, jl, 7)

    fired: list = []
    _yield_to_a_newer_pass(ns, jr, monkeypatch, fired)
    assert _rebuild(jr).stats_quota_projection_incomplete is True

    attempts: list = []
    monkeypatch.setattr(
        jr, "recover_quota_cache_from_journal",
        lambda *a, **k: attempts.append(True) or {"complete": True})
    # Connected RAW rather than through `open_db`, because that opener would run
    # a reconciliation of its own and clear the flag before this test made
    # either of its two calls.
    core = importlib.import_module("_cctally_core")
    conn = sqlite3.connect(str(core.DB_PATH))
    try:
        with store.stats_write_scope("test", ingest_lock=True):
            assert jr.reconcile_incomplete_quota_projection(conn) is False
        assert attempts == []
        # Non-vacuity: outside that scope the same call does attempt recovery.
        jr.reconcile_incomplete_quota_projection(conn)
        assert attempts == [True]
    finally:
        conn.close()


def _seed_source_root(ns, root="root-a"):
    """Make the root ACTIVE so the projection materializes rows for it.

    `codex_source_roots` is populated by the rollout walk, never by a journal
    replay, and the projection materializes nothing for a root it does not
    consider active — so without this every projection comparison below would
    compare two empty sets and could not fail.
    """
    conn = ns["open_cache_db"]()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO codex_source_roots"
            "(source_root_key, canonical_root_path, first_seen_utc, "
            " last_seen_utc) VALUES (?,?,?,?)",
            (root, f"/codex/{root}", _iso(9), _iso(10)))
        conn.commit()
    finally:
        conn.close()


#: Every projection column except the per-pass `generation` token, which is a
#: fresh `secrets.token_hex(16)` on every materialization and so can never be
#: equal across two passes.
_BLOCK_COLUMNS = (
    "source, source_root_key, account_key, logical_limit_key, observed_slot, "
    "window_minutes, limit_id, limit_name, resets_at_utc, nominal_start_at_utc, "
    "first_observed_at_utc, last_observed_at_utc, first_percent, "
    "current_percent, last_source_path, last_line_offset, orphaned_at"
)
_MILESTONE_COLUMNS = (
    "source, source_root_key, account_key, logical_limit_key, observed_slot, "
    "window_minutes, resets_at_utc, percent_threshold, captured_at_utc, "
    "source_path, line_offset, high_water_percent, orphaned_at"
)


def _projection_rows(conn):
    """The materialized stats quota projection, generation token excluded."""
    # `tuple(row)` because `open_db` sets `row_factory = sqlite3.Row` and a raw
    # connection does not, so two equal projections would compare unequal.
    return (
        [tuple(row) for row in conn.execute(
            f"SELECT {_BLOCK_COLUMNS} FROM quota_window_blocks "
            "ORDER BY source_root_key, resets_at_utc, logical_limit_key")],
        [tuple(row) for row in conn.execute(
            f"SELECT {_MILESTONE_COLUMNS} FROM quota_percent_milestones "
            "ORDER BY source_root_key, resets_at_utc, percent_threshold")],
    )


def _projection_rows_raw(ns):
    core = importlib.import_module("_cctally_core")
    conn = sqlite3.connect(str(core.DB_PATH))
    try:
        return _projection_rows(conn)
    finally:
        conn.close()


def _gate_an_incomplete_projection(
    ns, jr, jl, monkeypatch, *, count=7, percent_ramp=False,
):
    """Publish a generation whose cache recovery stopped short.

    Leaves the durable incomplete flag set and removes the interference, so the
    caller is testing the RECONCILIATION rather than the interference.
    """
    ns["open_cache_db"]().close()
    _tiny_caps(jr, monkeypatch, records=2)
    _seed_observations(jr, jl, count, percent_ramp=percent_ramp)
    _seed_source_root(ns)
    fired: list = []
    _yield_to_a_newer_pass(ns, jr, monkeypatch, fired)
    assert _rebuild(jr).stats_quota_projection_incomplete is True
    assert fired
    monkeypatch.setattr(jr, "_RECOVERY_BETWEEN_CHUNKS", None)
    assert int(_projection_state(ns)[0]) == 1


def _arm_reconciliation(monkeypatch):
    """Arm the open-time reconciliation for this test only.

    `monkeypatch` rather than `enable_quota_projection_reconciliation()`
    because `conftest.load_script()` deliberately KEEPS `_cctally_core` across
    reloads, so a permanent set would leak into every later test in the
    process.

    That reasoning covered only the TEST-side setter and missed the production
    one: `cmd_cache_sync` calls `enable_quota_projection_reconciliation()`
    itself, so any test running `cctally cache-sync` armed the whole worker
    process and left `test_an_ordinary_open_never_reads_the_journal_to_
    reconcile` asserting an unarmed open against an armed one. `pytest -n`'s
    default `--dist load` put both on worker gw2 and it failed. The autouse
    `_reset_quota_projection_reconcile_flag` fixture in `tests/conftest.py`
    closes that, and this helper stays as the explicit per-test arming.
    """
    core = importlib.import_module("_cctally_core")
    monkeypatch.setattr(core, "QUOTA_PROJECTION_RECONCILE_ENABLED", True)


def _count_segment_opens(jr, monkeypatch) -> list:
    """Physical opens through `_open_segment_for_read`, the one chokepoint.

    Structural: opens are counted, never lines, bytes or elapsed time.
    """
    opens: list = []
    real_open = jr._open_segment_for_read

    def counted(seg_path, *args, **kwargs):
        opens.append(str(seg_path))
        return real_open(seg_path, *args, **kwargs)

    monkeypatch.setattr(jr, "_open_segment_for_read", counted)
    return opens


def test_a_missing_cache_never_clears_the_projection_gate(
    tmp_path, monkeypatch,
):
    """The gate may only be cleared over a projection that was REWRITTEN.

    `rm cache.db` is documented-safe in this repository and the corruption
    auto-heal recreates the family, so a gated generation meeting an absent
    cache is reachable. The recovery leg then has nothing to write, the
    re-materialization opens no cache connection and applies no row, and
    clearing the flag would serve the partial projection the flag exists to
    refuse.
    """
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    core = importlib.import_module("_cctally_core")
    quota_mod = importlib.import_module("_cctally_quota")
    _gate_an_incomplete_projection(ns, jr, jl, monkeypatch)

    core.CACHE_DB_PATH.unlink()
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(core.CACHE_DB_PATH) + suffix)
        if sidecar.exists():
            sidecar.unlink()

    _arm_reconciliation(monkeypatch)
    conn = ns["open_db"]()
    try:
        with pytest.raises(quota_mod.QuotaProjectionIncomplete):
            quota_mod.assert_projection_readable(conn)
    finally:
        conn.close()
    assert int(_projection_state(ns)[0]) == 1


def test_recovery_over_a_missing_cache_reports_incomplete(
    tmp_path, monkeypatch,
):
    """Step one of the same chain, observed on its own.

    `recover_quota_cache_from_journal` is the reconciliation's entry point, and
    reporting `complete` for a cache it could not write to is what let the
    caller past its own gate.
    """
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    core = importlib.import_module("_cctally_core")
    ns["open_cache_db"]().close()
    _seed_observations(jr, jl, 3)
    core.CACHE_DB_PATH.unlink()

    coverage = jr.recover_quota_cache_from_journal()
    assert coverage["complete"] is False
    assert coverage["remainder"]["reason"] == "cacheAbsent"


def test_a_missing_cache_still_reports_complete_with_nothing_to_recover(
    tmp_path, monkeypatch,
):
    """Non-vacuity for the pair above: `complete` is not a constant `False`.

    A leg with no records to replay has no shortfall, which is the distinction
    §4.7 draws between an absent duty and an uncovered remainder.
    """
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    core = importlib.import_module("_cctally_core")
    ns["open_cache_db"]().close()
    jr.append_record(jl.make_obs(
        at=_iso(10), src="test", provider="claude",
        payload={"kind": "cost_snapshot"}), now_utc=FIXED)
    core.CACHE_DB_PATH.unlink()

    coverage = jr.recover_quota_cache_from_journal()
    assert coverage["complete"] is True
    assert coverage["remainder"] is None


def test_an_ordinary_open_never_reads_the_journal_to_reconcile(
    tmp_path, monkeypatch,
):
    """The status-line path.

    `open_db` runs on every `cctally statusline` render and every hook tick.
    While the flag is set, an unconditional reconciliation reads the journal
    from zero to the current high water on each of them — the 1.64 GB working
    set the S4 measurements describe, on the interactive path. Only a context
    that opted in may pay it.

    The segment-open count alone cannot observe the OTHER half of the claim.
    `_reconcile_incomplete_quota_projection` documents that an unarmed process
    returns "before the `_cctally_journal` import", and in a test process that
    module is already imported, so a broken arming gate that imported it and
    then failed would still open no segment. The `import_module` call is counted
    separately, which is a physical observation of the reach itself.
    """
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    core = importlib.import_module("_cctally_core")
    # `_cctally_core` intentionally survives `load_script()` and the arming
    # flag intentionally lives for the process. Pin the premise so a prior
    # cache-sync test on the same xdist worker cannot turn this unarmed-open
    # assertion into a maintenance-capable-open assertion.
    monkeypatch.setattr(core, "QUOTA_PROJECTION_RECONCILE_ENABLED", False)
    _gate_an_incomplete_projection(ns, jr, jl, monkeypatch)

    reached: list = []
    real_import = importlib.import_module

    def counting_import(name, *args, **kwargs):
        if name == "_cctally_journal":
            reached.append(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", counting_import)

    opens = _count_segment_opens(jr, monkeypatch)
    conn = ns["open_db"]()
    conn.close()
    assert opens == []
    assert reached == [], "an unarmed open must not even reach the journal module"
    assert int(_projection_state(ns)[0]) == 1


def test_a_maintenance_capable_open_does_reconcile(tmp_path, monkeypatch):
    """Non-vacuity for the count above, and acceptance criterion 16's second
    half: the gate still has to LIFT somewhere."""
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    quota_mod = importlib.import_module("_cctally_quota")
    _gate_an_incomplete_projection(ns, jr, jl, monkeypatch)

    reached: list = []
    real_import = importlib.import_module

    def counting_import(name, *args, **kwargs):
        if name == "_cctally_journal":
            reached.append(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", counting_import)

    opens = _count_segment_opens(jr, monkeypatch)
    _arm_reconciliation(monkeypatch)
    conn = ns["open_db"]()
    try:
        quota_mod.assert_projection_readable(conn)
    finally:
        conn.close()
    assert opens, "the enabled context must have read the journal"
    # Non-vacuity for the unarmed test's import count: the reach really is
    # observable through this counter when arming lets it through.
    assert reached, "the enabled context must have reached the journal module"
    assert int(_projection_state(ns)[0]) == 0
    assert [row[1] for row in _quota_rows(ns)] == list(range(10, 17))


def test_an_armed_reconciliation_prints_nothing_at_all(
    tmp_path, monkeypatch, capsys,
):
    """Acceptance criterion 10 — "ordinary stderr produces no new line" — as a
    BLANKET assertion over the whole armed reconciliation.

    The sibling that pins the conflict line asserts one known string is absent,
    which a print added anywhere else in the recovery, the re-materialization
    or the flag clear would walk straight past. This asserts the stream is
    EMPTY, so a later print fails here whatever it says. The setup's own
    rebuild output is drained first, because a rebuild is allowed to speak and
    this is about what an ordinary open does.
    """
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    quota_mod = importlib.import_module("_cctally_quota")
    _gate_an_incomplete_projection(ns, jr, jl, monkeypatch)
    capsys.readouterr()          # discard the setup rebuild's own output

    _arm_reconciliation(monkeypatch)
    conn = ns["open_db"]()
    try:
        quota_mod.assert_projection_readable(conn)
    finally:
        conn.close()

    captured = capsys.readouterr()
    assert captured.err == "", captured.err
    assert captured.out == "", captured.out
    # Non-vacuity: the reconciliation really ran, so the silence is a property
    # of a path that did work rather than of one that returned early.
    assert int(_projection_state(ns)[0]) == 0
    assert [row[1] for row in _quota_rows(ns)] == list(range(10, 17))


def test_cache_sync_is_the_remedy_it_is_documented_as(tmp_path, monkeypatch):
    """`cctally cache-sync` is named as the remedy by `doctor`, by every refusal
    message and by `docs/commands/db.md`, and nothing proved it works.

    `cmd_cache_sync` arms the reconciliation, but arming does nothing unless the
    command then reaches `open_db()`. That reach was established by reading the
    source, and this repository's own guidance says a statically inferred cause
    is a hypothesis. This runs the real command and asserts the flag clears.
    """
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    core = importlib.import_module("_cctally_core")
    _gate_an_incomplete_projection(ns, jr, jl, monkeypatch)
    # Arming is a process global and `_arm_reconciliation` is not called here on
    # purpose: the command has to arm itself, which is half of what this proves.
    monkeypatch.setattr(core, "QUOTA_PROJECTION_RECONCILE_ENABLED", False)

    # Through the REAL parser rather than a hand-built Namespace. A hand-built
    # one freezes today's attribute set, so a new `cache-sync` flag that
    # `cmd_cache_sync` reads would make this test raise `AttributeError`
    # instead of exercising the command — it would break rather than cover.
    args = ns["build_parser"]().parse_args(["cache-sync"])
    assert ns["cmd_cache_sync"](args) == 0

    assert int(_projection_state(ns)[0]) == 0, (
        "the documented remedy did not reconcile the projection")
    assert [row[1] for row in _quota_rows(ns)] == list(range(10, 17))


def test_a_successful_reconciliation_leaves_no_throttle_behind(
    tmp_path, monkeypatch,
):
    """The marker bounds the cost of a FAILING attempt.

    A success leaves nothing to bound, and keeping the marker made the throttle
    punish the next genuine incompleteness: a flag set again within the interval
    — a second interrupted rebuild, which is exactly what an upgrade under lock
    contention produces — would have waited the interval out for no reason.
    """
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    _gate_an_incomplete_projection(ns, jr, jl, monkeypatch)
    _arm_reconciliation(monkeypatch)

    ns["open_db"]().close()
    assert int(_projection_state(ns)[0]) == 0, "the reconciliation must succeed"
    assert not jr._projection_reconcile_marker_path().exists()
    assert jr._projection_reconcile_throttled() is False

    # Non-vacuity: a FAILING attempt still leaves the marker, so the removal
    # above is a property of success rather than of the marker never appearing.
    monkeypatch.setattr(
        jr, "recover_quota_cache_from_journal",
        lambda *a, **k: {"status": "incomplete", "complete": False,
                         "remainder": {"reason": "locksBusy"}})
    _gate_an_incomplete_projection(ns, jr, jl, monkeypatch, count=9)
    ns["open_db"]().close()
    assert int(_projection_state(ns)[0]) == 1
    assert jr._projection_reconcile_marker_path().exists()
    assert jr._projection_reconcile_throttled() is True


def test_a_failed_reconciliation_is_not_retried_on_every_open(
    tmp_path, monkeypatch,
):
    """Nothing bounded the repetition.

    The states that leave the flag set are ordinary — `locksBusy`,
    `restartLimit`, `noCoverageEstablished`, `mintRefused` — and in any of them
    every subsequent invocation re-read the whole journal and failed again.
    """
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    _gate_an_incomplete_projection(ns, jr, jl, monkeypatch)
    _arm_reconciliation(monkeypatch)

    monkeypatch.setattr(
        jr, "_rematerialize_and_clear_projection_gate", lambda *a, **k: False)
    first = _count_segment_opens(jr, monkeypatch)
    ns["open_db"]().close()
    assert first, "the first attempt must have read the journal"

    second = _count_segment_opens(jr, monkeypatch)
    ns["open_db"]().close()
    assert second == [], "a second attempt inside the interval must not re-read"
    assert int(_projection_state(ns)[0]) == 1

    # Non-vacuity: the throttle is an interval, not a one-shot latch.
    monkeypatch.setattr(jr, "_PROJECTION_RECONCILE_RETRY_SECONDS", 0.0)
    third = _count_segment_opens(jr, monkeypatch)
    ns["open_db"]().close()
    assert third, "an elapsed interval must let the next attempt through"


def test_busy_cache_flocks_skip_the_journal_read_entirely(
    tmp_path, monkeypatch,
):
    """The journal read is the expensive half and was paid FIRST.

    A recovery that cannot take the cache writer flocks cannot apply a single
    row, so the pre-check has to run ahead of the read rather than after it.
    """
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    core = importlib.import_module("_cctally_core")
    from _lib_cache_writer_lock import (
        acquire_cache_writer_flocks, release_cache_writer_flocks,
    )
    _gate_an_incomplete_projection(ns, jr, jl, monkeypatch)
    _arm_reconciliation(monkeypatch)

    held = acquire_cache_writer_flocks(
        core.CACHE_LOCK_PATH, core.CACHE_LOCK_CODEX_PATH, timeout=None)
    assert held is not None
    try:
        opens = _count_segment_opens(jr, monkeypatch)
        ns["open_db"]().close()
        assert opens == []
    finally:
        release_cache_writer_flocks(held)
    assert int(_projection_state(ns)[0]) == 1


def test_the_reconciled_projection_matches_a_full_replay(tmp_path, monkeypatch):
    """Criterion 13e's missing half.

    `tests/test_quota_journal.py::test_the_gate_refuses_a_connection_opened_
    before_the_publication` asserts only that the refusal fires; a gate that
    never lifted would satisfy it, and nothing observed the projection ever
    being SERVED correctly. This reconciles and then compares both materialized
    populations against a forced full replay, which is the reference the plan's
    own sketch named.

    **The comparison is read on a connection opened after the reconciliation.**
    Publication used to unlink the live `-wal` and `-shm` (issue #516), which
    put a connection opened BEFORE it on unlinked inodes; that unlink is gone,
    so the choice is now conservatism rather than a limitation. The gate's own
    response to an unreadable flag is asserted separately and deterministically
    by `tests/test_selector_state_496_s5b.py::
    test_the_gate_fails_closed_when_the_flag_cannot_be_read`, and
    `test_a_publication_never_unlinks_the_sidecars_of_an_open_reader` pins the
    publisher side.

    Two earlier versions of this docstring were wrong about the hazard. The
    first claimed it "does not reproduce in THIS process, because the unix VFS
    keys its shared-memory node by inode WITHIN a process"; that reasoning is
    backwards, since sharing one node within a process is what PROPAGATES the
    fault. The second, written after thirteen arrangements produced no error,
    concluded the unlink was harmless. It is not: the fault needs a WRITE after
    the unlink AND a reader that had READ before it, and the thirteen
    arrangements never combined the two. Re-measured on both LAN runners
    (macOS, Python 3.13.14, SQLite 3.53.4, byte-identical on each): a
    same-process later writer breaks such a reader with `OperationalError: disk
    I/O error`, and a child-process later writer leaves it — and a freshly
    opened connection in the parent — silently reading a STALE generation. Do
    not restore either argument, and do not read a green single-process test as
    evidence either way.
    """
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    quota_mod = importlib.import_module("_cctally_quota")
    _gate_an_incomplete_projection(
        ns, jr, jl, monkeypatch, percent_ramp=True)

    _arm_reconciliation(monkeypatch)
    conn = ns["open_db"]()
    try:
        quota_mod.assert_projection_readable(conn)
        served = _projection_rows(conn)
    finally:
        conn.close()

    assert served[0], "no blocks materialized — the comparison would be vacuous"
    assert served[1], "no milestones materialized — same"

    # The reference: a FORCED full replay of both halves. The certificate the
    # reconciliation just minted is dropped first, so the reference's cache leg
    # takes the recovery path rather than being handed the pass under test's own
    # conclusion.
    reference = _force_a_full_replay(ns, jr)
    assert reference.stats_quota_projection_incomplete is False
    assert served == _projection_rows_raw(ns)


def test_a_pre_publication_connection_reads_the_reconciled_projection(
    tmp_path, monkeypatch,
):
    """Criterion 13e's SECOND clause, which nothing asserted.

    13e requires the projection to be reconciled from a complete cache snapshot
    before it is served, "including for a connection opened BEFORE that
    publication". What shipped asserts only that such a connection sees the flag
    and is refused; every test that reads the reconciled projection reads it on a
    connection opened afterwards, so a mechanism that served the reconciled rows
    only to later openers would have satisfied the whole set.

    The reconciliation is armed at `open_db`, so a pre-publication connection is
    never itself the reconciler. What it must do is OBSERVE the cleared flag and
    the rewritten rows once another opener reconciles, which is what this
    asserts on the original connection.
    """
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    quota_mod = importlib.import_module("_cctally_quota")

    before = ns["open_db"]()
    try:
        quota_mod.assert_projection_readable(before)   # readable to start with
        _gate_an_incomplete_projection(
            ns, jr, jl, monkeypatch, percent_ramp=True)
        # The refusal reaches the pre-publication connection — the half that
        # already shipped, kept here so the lift below is not vacuous.
        with pytest.raises(quota_mod.QuotaProjectionIncomplete):
            quota_mod.assert_projection_readable(before)

        _arm_reconciliation(monkeypatch)
        reconciler = ns["open_db"]()
        try:
            quota_mod.assert_projection_readable(reconciler)
        finally:
            reconciler.close()

        # The ORIGINAL connection now reads the reconciled projection.
        quota_mod.assert_projection_readable(before)
        assert int(before.execute(
            "SELECT incomplete FROM stats_quota_projection_state "
            "WHERE id = 1").fetchone()[0]) == 0
        served = _projection_rows(before)
        assert served[0], "no blocks materialized — the comparison is vacuous"
        assert served[1], "no milestones materialized — same"
        assert served == _projection_rows_raw(ns)
    finally:
        before.close()


def test_a_resumed_recovery_converges_the_projection_with_a_full_replay(
    tmp_path, monkeypatch,
):
    """Spec §7 case 7: cache AND stats-projection convergence after an
    incomplete recovery resumes, compared against a forced full replay of both.

    The shipped test compared only the cache rows. The stats projection is
    materialized FROM those rows, so a converged cache does not by itself mean a
    converged projection — that gap is the whole reason §4.7 exists.
    """
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    quota_mod = importlib.import_module("_cctally_quota")
    _gate_an_incomplete_projection(
        ns, jr, jl, monkeypatch, percent_ramp=True)

    _arm_reconciliation(monkeypatch)
    conn = ns["open_db"]()
    try:
        quota_mod.assert_projection_readable(conn)
        assert int(conn.execute(
            "SELECT incomplete FROM stats_quota_projection_state "
            "WHERE id = 1").fetchone()[0]) == 0
    finally:
        conn.close()

    resumed_cache = [row[1] for row in _quota_rows(ns)]
    resumed = _projection_rows_raw(ns)
    assert resumed[0] and resumed[1], "the comparison must name real rows"

    # A FORCED full replay of both halves, per spec §7 case 7. The cache half is
    # compared against what that replay produced rather than against a
    # hardcoded range plus a certificate the resumed pass minted itself.
    _force_a_full_replay(ns, jr)
    assert resumed_cache == [row[1] for row in _quota_rows(ns)]
    assert resumed_cache == list(range(10, 17)), (
        "belt and braces: the replay and the resumed pass agree, and they agree "
        "on the full seeded range rather than on a shared shortfall")
    assert resumed == _projection_rows_raw(ns)


def test_the_cache_recovery_payload_never_reports_a_null_complete(
    tmp_path, monkeypatch,
):
    """The JSON and the durable flag must not disagree about one state.

    A rebuild with `update_quota_cache=False` leaves a coverage record with no
    `complete` key. `_write_quota_projection_state` reads that absence as
    COMPLETE; the payload emitted `null` for it, so
    `if not payload["cacheRecovery"]["complete"]` got the wrong answer.
    """
    ns, _quota, jr, _jl = _load(tmp_path, monkeypatch)
    db = importlib.import_module("_cctally_db")
    core = importlib.import_module("_cctally_core")
    # The schema has to exist first. Without this the writer hits `no such
    # table`, reports the failure on stderr and returns None for EVERY
    # coverage record — which is how the shipped form of this test came to
    # assert its one agreement against a writer that never ran.
    ns["open_db"]().close()
    never_ran = {"status": "skipped", "reason": None, "replayedObservations": 0}
    fell_short = {"status": "incomplete", "complete": False,
                  "remainder": {"reason": "locksBusy"}}
    covered = {"status": "covered", "complete": True}

    def flag_writer_says_incomplete(coverage) -> bool:
        conn = sqlite3.connect(str(core.DB_PATH))
        try:
            # Non-vacuity: a writer that cannot see the table returns None
            # for every record, and `None is not True` / `None is not False`
            # are BOTH true — so without this guard the comparison below would
            # pass for a writer that never ran.
            assert conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND "
                "name='stats_quota_projection_state'").fetchone() is not None
            conn.execute("BEGIN IMMEDIATE")
            written = jr._write_quota_projection_state(
                conn, coverage=coverage, high_water=None)
            conn.rollback()
        finally:
            conn.close()
        return written

    # The agreement is asserted against `_write_quota_projection_state` for ALL
    # THREE states rather than restated as literals for two of them. Restating
    # is what let the original disagreement stand: the payload said `null` for
    # the never-ran record while the flag writer read the same record as
    # complete, and only a comparison between the two could have caught it.
    for coverage, expected_complete, expected_phase in (
        (never_ran, True, "notRun"),
        (fell_short, False, "incomplete"),
        (covered, True, "covered"),
    ):
        payload = db._cache_recovery_payload(coverage)
        assert payload["complete"] is expected_complete, coverage
        assert payload["phase"] == expected_phase, coverage
        assert flag_writer_says_incomplete(coverage) is not payload["complete"], (
            "the JSON and the durable flag disagree about "
            f"{expected_phase}: {coverage}"
        )

    assert db._cache_recovery_payload(None)["phase"] == "notRun"


def test_db_rebuild_reports_publication_and_cache_recovery_distinctly(
    tmp_path, monkeypatch, capsys,
):
    """Spec §4.7 and acceptance criteria 10 and 16.

    Publication success and cache-recovery completeness are two different
    questions, so `db rebuild --json` states them as two fields a consumer
    cannot read as one another. Exit code 0, and no new stderr line.
    """
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    ns["open_cache_db"]().close()
    _tiny_caps(jr, monkeypatch, records=2)
    _seed_observations(jr, jl, 7)

    fired: list = []
    _yield_to_a_newer_pass(ns, jr, monkeypatch, fired)
    capsys.readouterr()
    assert ns["cmd_db_rebuild"](
        argparse.Namespace(db="stats", json=True)) == 0
    captured = capsys.readouterr()
    assert fired

    payload = json.loads(captured.out)
    assert payload["publication"]["ok"] is True
    assert payload["publication"]["statsQuotaProjectionIncomplete"] is True
    assert payload["cacheRecovery"]["complete"] is False
    assert payload["cacheRecovery"]["remainder"] is not None
    assert payload["cacheRecovery"]["remainder"]["reason"] == "newerPass"
    assert payload["cacheRecovery"]["phase"] == "incomplete"
    assert "cacheRecovery" not in payload["publication"]


def test_a_complete_rebuild_reports_both_outcomes_as_satisfied(
    tmp_path, monkeypatch, capsys,
):
    """Non-vacuity for the pair above: neither field is a constant."""
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    ns["open_cache_db"]().close()
    _tiny_caps(jr, monkeypatch, records=2)
    _seed_observations(jr, jl, 7)

    capsys.readouterr()
    assert ns["cmd_db_rebuild"](
        argparse.Namespace(db="stats", json=True)) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["publication"]["ok"] is True
    assert payload["publication"]["statsQuotaProjectionIncomplete"] is False
    assert payload["cacheRecovery"]["complete"] is True
    assert payload["cacheRecovery"]["remainder"] is None


def test_an_uncovered_remainder_prints_the_existing_success_line_and_no_more(
    tmp_path, monkeypatch, capsys,
):
    """Acceptance criterion 16: exit 0, the existing success line, and NO new
    stderr line. Ordinary stderr stays quiet (§6.3)."""
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    ns["open_cache_db"]().close()
    _tiny_caps(jr, monkeypatch, records=2)
    _seed_observations(jr, jl, 7)

    fired: list = []
    _yield_to_a_newer_pass(ns, jr, monkeypatch, fired)
    capsys.readouterr()
    assert ns["cmd_db_rebuild"](
        argparse.Namespace(db="stats", json=False)) == 0
    captured = capsys.readouterr()
    assert fired
    assert "rebuilt stats.db" in captured.out
    for word in ("remainder", "incomplete", "uncovered", "coverage"):
        assert word not in captured.out.lower(), captured.out
        assert word not in captured.err.lower(), captured.err


def test_recovery_from_the_journal_matches_a_rebuild_leg(
    tmp_path, monkeypatch,
):
    """`recover_quota_cache_from_journal` is the cache half of a rebuild
    reachable on its own, and it must classify the journal the same way the
    rebuild's streaming pass does — the same observations, the same file-account
    decisions, the same cutover."""
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    cache_mod = importlib.import_module("_cctally_cache")
    ns["open_cache_db"]().close()
    _tiny_caps(jr, monkeypatch, records=2)

    from _lib_source_identity import codex_file_key
    source_path = "/codex/root-a/r.jsonl"
    identity = codex_file_key(
        "root-a", str(cache_mod._canonical_codex_path(Path(source_path))))
    jr.append_record(jl.make_codex_file_account(
        at=_iso(9), root_scope="root-a", file_identity=identity,
        incarnation=1, from_offset=0, account_key="d" * 32), now_utc=FIXED)
    _seed_observations(jr, jl, 5)

    coverage = jr.recover_quota_cache_from_journal()
    assert coverage["status"] == "recovered"
    assert coverage["complete"] is True
    assert coverage["replayedObservations"] == 5
    assert [row[1] for row in _quota_rows(ns)] == list(range(10, 15))
    conn = ns["open_cache_db"]()
    try:
        assert conn.execute(
            "SELECT account_key FROM codex_file_accounts "
            "WHERE file_identity = ?", (identity,)).fetchall() == [("d" * 32,)]
        assert cache_mod.load_codex_journal_coverage_certificate(
            conn) is not None
    finally:
        conn.close()


def test_the_anchor_resolver_is_reconstructed_per_chunk(
    tmp_path, monkeypatch,
):
    """Releasing the flock between chunks lets another writer mutate under a
    resolver that caches group state bound to its connection, so each chunk's
    anchor decisions must be made against the state committed at that moment."""
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    ns["open_cache_db"]().close()
    _tiny_caps(jr, monkeypatch, records=2)
    _seed_observations(jr, jl, 7)

    cache_mod = importlib.import_module("_cctally_cache")
    constructions: list = []
    real = cache_mod.CodexResetAnchorResolver.__init__

    def counted(self, conn):
        constructions.append(conn)
        return real(self, conn)

    monkeypatch.setattr(
        cache_mod.CodexResetAnchorResolver, "__init__", counted)
    tracker = _LockTracker().install(monkeypatch)
    assert _rebuild(jr).quota_cache_coverage["status"] == "recovered"

    # Chunk 0 applies the decisions AND the first observation span in one
    # transaction, so every hold builds exactly one resolver.
    assert len(tracker.holds) > 1, "it must have chunked"
    assert len(constructions) == len(tracker.holds)


def test_conflict_reports_are_threaded_across_chunks(
    tmp_path, monkeypatch, capsys,
):
    """`reported_conflicts` is deliberately local so a conflicting run spanning
    several ingest batches reports once per batch. Naive chunking would emit one
    line per chunk for what a single unchunked call reports once."""
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    ns["open_cache_db"]().close()
    _tiny_caps(jr, monkeypatch, records=1)

    # A durable decision naming one account, seeded straight into the map so
    # the oracle finds it, then observations stamped with a DIFFERENT one —
    # the exact shape `_apply_quota_records` reports on.
    import _cctally_cache as cache_mod
    from _lib_source_identity import codex_file_key

    source_path = "/codex/root-a/r.jsonl"
    identity = codex_file_key(
        "root-a",
        str(cache_mod._canonical_codex_path(Path(source_path))))
    conn = ns["open_cache_db"]()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO codex_file_accounts "
            "(file_identity, incarnation, from_offset, root_scope, "
            " account_key, decided_at_utc) VALUES (?,?,?,?,?,?)",
            (identity, 1, 0, "root-a", "acct-decided", _iso(9)))
        conn.commit()
    finally:
        conn.close()

    for index in range(4):
        obs = _codex_quota_obs(
            jl, source_root_key="root-a", source_path=source_path,
            line_offset=10 + index, captured_at_utc=_iso(10))
        obs["account"] = "acct-observed"
        jr.append_record(obs, now_utc=FIXED)

    capsys.readouterr()
    result = _rebuild(jr)
    assert result.quota_cache_coverage["status"] == "recovered"
    assert result.quota_cache_coverage["chunks"] > 2, "it must have chunked"
    err = capsys.readouterr().err
    assert err.count("codex attribution conflict") == 1, err


def test_the_reconciliation_path_emits_no_conflict_line(
    tmp_path, monkeypatch, capsys,
):
    """Acceptance criterion 10: no new stderr on an ordinary command.

    Routing `recover_quota_cache_from_journal` through the shared leg made this
    line reachable from the open-time reconciliation, so `cache-sync` and the
    dashboard would emit a line they never emitted before. Its sibling,
    `_report_file_account_conflicts`, was given `quiet` and this one was not.
    """
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    ns["open_cache_db"]().close()
    _tiny_caps(jr, monkeypatch, records=1)

    import _cctally_cache as cache_mod
    from _lib_source_identity import codex_file_key

    source_path = "/codex/root-a/r.jsonl"
    identity = codex_file_key(
        "root-a",
        str(cache_mod._canonical_codex_path(Path(source_path))))
    conn = ns["open_cache_db"]()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO codex_file_accounts "
            "(file_identity, incarnation, from_offset, root_scope, "
            " account_key, decided_at_utc) VALUES (?,?,?,?,?,?)",
            (identity, 1, 0, "root-a", "acct-decided", _iso(9)))
        conn.commit()
    finally:
        conn.close()

    for index in range(4):
        obs = _codex_quota_obs(
            jl, source_root_key="root-a", source_path=source_path,
            line_offset=10 + index, captured_at_utc=_iso(10))
        obs["account"] = "acct-observed"
        jr.append_record(obs, now_utc=FIXED)

    capsys.readouterr()
    quiet_result = jr.recover_quota_cache_from_journal(quiet=True)
    assert quiet_result["status"] == "recovered", quiet_result
    assert "codex attribution conflict" not in capsys.readouterr().err

    # Non-vacuity: the SAME records still produce the line on the loud path, so
    # the silence above is the guard rather than an absent condition. The
    # certificate is dropped first, or the second pass replays nothing.
    cache_conn = ns["open_cache_db"]()
    try:
        cache_conn.execute("BEGIN IMMEDIATE")
        cache_mod._invalidate_codex_journal_coverage_certificate(cache_conn)
        cache_conn.execute(
            "DELETE FROM quota_window_snapshots WHERE source='codex'")
        cache_conn.commit()
    finally:
        cache_conn.close()
    capsys.readouterr()
    loud_result = jr.recover_quota_cache_from_journal(quiet=False)
    assert loud_result["status"] == "recovered", loud_result
    assert "codex attribution conflict" in capsys.readouterr().err
