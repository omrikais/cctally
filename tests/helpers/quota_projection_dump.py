"""Equivalence oracle + synthetic store for the Codex quota projection.

Public issue omrikais/cctally#5. Spec:
``docs/superpowers/specs/2026-07-31-codex-hook-incremental-quota-reconcile-design.md``.

Two things live here, both consumed by ``tests/test_codex_quota_incremental.py``:

``canonical_projection_dump`` — the logical dump that lets an INCREMENTAL pass
be compared against a WHOLE-HISTORY one. A byte compare is impossible by
construction: every full pass mints a random ``generation`` and stamps fresh
completion timestamps, and an incremental pass deliberately leaves clean rows
carrying an older pass's provenance. The dump therefore drops exactly the
per-pass provenance the spec names and keeps everything else, including
``physical_signature`` (a function of the physical evidence, not of the pass —
two passes over the same store must agree on it) and orphan status as a
BOOLEAN (the ``orphaned_at`` instant is per-pass; whether a row is orphaned is
not).

``build_codex_quota_store`` — many closed windows plus one live one, seeded
directly into ``quota_window_snapshots``. Directly, because the point of the
fixture is a store whose observation count dominates the reconcile, and
generating thousands of rollout JSONL bytes to reach it would measure the
ingest path instead.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import sqlite3
from pathlib import Path

import _fixture_builders
import _lib_jsonl
import _lib_quota


UTC = dt.timezone.utc

#: Anchor for every synthetic window. Fixed so a fixture built twice is
#: identical and a failure message names a stable instant.
BASE = dt.datetime(2026, 7, 1, 0, 0, tzinfo=UTC)

#: One synthetic window is one Codex 5-hour slot.
WINDOW_MINUTES = 300

ROOT_KEY = "root-incremental"
SOURCE_PATH = f"/codex/{ROOT_KEY}/rollout.jsonl"
OBSERVED_SLOT = "primary"

#: Percent span walked inside every window. Small on purpose: the milestone
#: ladder writes one row per integer crossing, so a 1..100 span would make the
#: fixture's milestone table an order of magnitude larger than its observation
#: table without exercising anything extra.
FIRST_PERCENT = 1.0
LAST_PERCENT = 6.0


def logical_limit_key(root_key: str = ROOT_KEY) -> str:
    """The interpreted limit key every synthetic window carries.

    Minted through the production helper rather than a literal, so the fixture
    keeps agreeing with ``snap_window_minutes`` / the model-pool classifier
    instead of quietly becoming an opaque string they both fail open on.
    """
    return _lib_jsonl._codex_logical_limit_key(
        root_key, "codex", OBSERVED_SLOT, WINDOW_MINUTES)


def source_path_for(root_key: str = ROOT_KEY) -> str:
    """The rollout path a root's synthetic observations claim to come from."""
    return SOURCE_PATH if root_key == ROOT_KEY else f"/codex/{root_key}/rollout.jsonl"


def _window_reset(index: int) -> dt.datetime:
    return BASE + dt.timedelta(minutes=WINDOW_MINUTES * (index + 1))


def build_codex_quota_store(
    ns,
    *,
    closed_windows: int,
    live_windows: int = 1,
    observations_per_window: int = 50,
    root_key: str = ROOT_KEY,
) -> Path:
    """Seed a Codex quota store of ``closed_windows`` + ``live_windows`` windows.

    Returns the ``cache.db`` path. ``ns`` is the ``load_script()`` namespace the
    caller has already pointed at a tmp data dir with ``redirect_paths`` — the
    plan sketched a bare ``tmp_path`` argument driven by ``CCTALLY_DATA_DIR``,
    but every other test in this repo redirects through ``_cctally_core`` (which
    is what also pins the migration-error-log guard in ``conftest``), so the
    fixture takes the namespace and stays on the established path.

    Every window is a distinct physical group: same root, same limit key, same
    slot, same duration, different canonical reset. That is the shape the
    incremental projector has to exploit — one appended observation must dirty
    exactly one of them.

    ``root_key`` seeds a SECOND (or third) root into the same store when called
    again — the shape the liveness rule needs, where one root leaves
    ``codex_source_roots`` while its blocks remain.
    """
    if closed_windows < 0 or live_windows < 1:
        raise ValueError("closed_windows must be >= 0 and live_windows >= 1")
    if observations_per_window < 1:
        raise ValueError("observations_per_window must be >= 1")
    limit_key = logical_limit_key(root_key)
    source_path = source_path_for(root_key)
    conn = ns["open_cache_db"]()
    try:
        _fixture_builders.seed_codex_source_root(
            conn,
            source_root_key=root_key,
            canonical_root_path=f"/codex/{root_key}",
            first_seen_utc=_iso(BASE),
            last_seen_utc=_iso(_window_reset(closed_windows + live_windows)),
        )
        line_offset = 0
        span = LAST_PERCENT - FIRST_PERCENT
        for index in range(closed_windows + live_windows):
            reset_at = _window_reset(index)
            start_at = reset_at - dt.timedelta(minutes=WINDOW_MINUTES)
            step = dt.timedelta(
                minutes=WINDOW_MINUTES / (observations_per_window + 1))
            for k in range(observations_per_window):
                fraction = (
                    0.0 if observations_per_window == 1
                    else k / (observations_per_window - 1)
                )
                _fixture_builders.seed_codex_quota_snapshot(
                    conn,
                    source_root_key=root_key,
                    source_path=source_path,
                    line_offset=line_offset,
                    captured_at_utc=_iso(start_at + step * (k + 1)),
                    observed_slot=OBSERVED_SLOT,
                    logical_limit_key=limit_key,
                    window_minutes=WINDOW_MINUTES,
                    used_percent=FIRST_PERCENT + span * fraction,
                    resets_at_utc=_iso(reset_at),
                    plan_type="pro",
                )
                line_offset += 1
        _fixture_builders.bump_codex_physical_mutation_seq(conn)
        conn.commit()
    finally:
        conn.close()
    return Path(ns["CACHE_DB_PATH"])


def append_one_observation_to_live_window(ns) -> tuple[object, ...]:
    """Append exactly one observation to the newest (live) window.

    Returns that window's physical group key, so a caller can assert WHICH
    group the projector expanded rather than only how many. Bumps the Codex
    physical mutation sequence, because a real ingest commits the row and the
    sequence together — without it the reconcile's certificate short-circuit
    fires and the pass measures nothing.
    """
    conn = ns["open_cache_db"]()
    try:
        # cache.db connections deliberately carry no row_factory.
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT source_root_key, logical_limit_key, observed_slot, "
            "       window_minutes, resets_at_utc, "
            "       COALESCE(canonical_resets_at_utc, resets_at_utc) AS anchor, "
            "       MAX(unixepoch(captured_at_utc)) AS last_capture, "
            "       MAX(used_percent) AS last_percent "
            "  FROM quota_window_snapshots "
            " WHERE source='codex' "
            "   AND COALESCE(canonical_resets_at_utc, resets_at_utc) = ("
            "        SELECT MAX(COALESCE(canonical_resets_at_utc, resets_at_utc)) "
            "          FROM quota_window_snapshots WHERE source='codex')"
        ).fetchone()
        if row is None or row[0] is None:
            raise AssertionError("store has no Codex quota windows to append to")
        next_offset = int(conn.execute(
            "SELECT MAX(line_offset) FROM quota_window_snapshots "
            "WHERE source='codex' AND source_path=?", (SOURCE_PATH,),
        ).fetchone()[0]) + 1
        captured_at = dt.datetime.fromtimestamp(
            int(row["last_capture"]), UTC) + dt.timedelta(minutes=1)
        _fixture_builders.seed_codex_quota_snapshot(
            conn,
            source_root_key=str(row["source_root_key"]),
            source_path=SOURCE_PATH,
            line_offset=next_offset,
            captured_at_utc=_iso(captured_at),
            observed_slot=str(row["observed_slot"]),
            logical_limit_key=str(row["logical_limit_key"]),
            window_minutes=int(row["window_minutes"]),
            used_percent=min(100.0, float(row["last_percent"]) + 1.0),
            resets_at_utc=str(row["resets_at_utc"]),
            canonical_resets_at_utc=str(row["anchor"]),
            plan_type="pro",
        )
        _fixture_builders.bump_codex_physical_mutation_seq(conn)
        conn.commit()
        return (
            "codex",
            str(row["source_root_key"]),
            str(row["logical_limit_key"]),
            str(row["observed_slot"]),
            int(row["window_minutes"]),
            _parse(str(row["anchor"])),
        )
    finally:
        conn.close()


def store_as_of(
    ns, *, before_reset: dt.timedelta = dt.timedelta(hours=1),
) -> dt.datetime:
    """A deterministic ``now`` that sits INSIDE the store's live window.

    Derived from the data rather than hard-coded so it stays correct for any
    ``closed_windows`` the caller asks for.

    ``before_reset`` moves it relative to the live window's reset. The default
    hour leaves the newest captures in the FUTURE relative to ``now``, which is
    what the reproduction test wants (a genuinely live window). An alert-bearing
    test needs the opposite — ``quota_freshness`` classifies a history whose
    latest physical capture is ahead of ``now`` as ``future`` and skips every
    threshold decision for that identity — so it passes a smaller value and gets
    a ``now`` that is after the last capture but still inside the window.
    """
    conn = ns["open_cache_db"]()
    try:
        newest = conn.execute(
            "SELECT MAX(COALESCE(canonical_resets_at_utc, resets_at_utc)) "
            "FROM quota_window_snapshots WHERE source='codex'"
        ).fetchone()[0]
    finally:
        conn.close()
    if newest is None:
        raise AssertionError("store has no Codex quota windows")
    return _parse(str(newest)) - before_reset


def enable_quota_alerts(ns, *, actual=(2,), projected=()) -> None:
    """Turn on Codex quota alerting for the redirected data dir.

    Both switches default OFF, so without this the equivalence oracle reads
    ``quota_threshold_events`` and ``quota_alert_arming`` as empty in BOTH arms
    and covers them vacuously — and those are exactly the tables the scoped
    sweep and the alert axes put at risk.

    ``notifier: none`` is not cosmetic: a queued alert is dispatched post-commit
    by the ingest cycle, and the default backend shells out to ``osascript``.
    """
    import json as _json

    import _cctally_core

    _cctally_core.CONFIG_PATH.write_text(_json.dumps({"alerts": {
        "enabled": True,
        "notifier": "none",
        "quota": {
            "enabled": True,
            "actual_thresholds": list(actual),
            "projected_thresholds": list(projected),
            "rules": [],
        },
    }}) + "\n")


# ── the equivalence oracle ────────────────────────────────────────────────

#: Columns a pass re-stamps on every row it touches. They are the reason a byte
#: (or naive row) compare cannot work, and they are the ONLY things dropped.
_PER_PASS_PROVENANCE = {
    "id",                 # AUTOINCREMENT — assignment order, not content
    "generation",         # secrets.token_hex per pass
    "completed_at_utc",   # projection-state stamp, = the pass's `now`
    "created_at_utc",     # terminal-event stamp, = the creating pass's `now`
    "alerted_at",         # dispatch stamp
    "suppressed_at",      # backfill-suppression stamp
    "activated_at_utc",   # arming boundary, = the activating pass's `now`
    "orphaned_at",        # replaced by the `orphaned` boolean below
}

_DUMP_TABLES = {
    "blocks": ("quota_window_blocks", (
        "source", "source_root_key", "account_key", "logical_limit_key",
        "observed_slot", "window_minutes", "resets_at_utc")),
    "milestones": ("quota_percent_milestones", (
        "source", "source_root_key", "account_key", "logical_limit_key",
        "observed_slot", "window_minutes", "resets_at_utc",
        "percent_threshold")),
    "threshold_events": ("quota_threshold_events", (
        "source", "source_root_key", "account_key", "logical_limit_key",
        "observed_slot", "window_minutes", "resets_at_utc", "threshold")),
    "alert_arming": ("quota_alert_arming", (
        "source", "source_root_key", "account_key", "logical_limit_key",
        "observed_slot", "window_minutes")),
    "projection_state": ("quota_projection_state", (
        "source_root_key", "account_key")),
}


def canonical_projection_dump(stats_conn, *, source: str = "codex") -> dict:
    """A logical, order-stable dump of the interpreted quota projection.

    Covers ``quota_window_blocks``, ``quota_percent_milestones``,
    ``quota_projection_state``, ``quota_threshold_events`` and
    ``quota_alert_arming``, each with orphan status reduced to a boolean.
    Suitable for ``assert incremental == whole_history``: two passes over the
    same physical evidence must produce the same dump even though neither
    produces the same bytes.

    ``physical_signature`` is deliberately KEPT. It is a digest of the physical
    cache evidence, not of the pass that wrote it, so an incremental pass that
    cannot reproduce it is telling us something real about the projection state
    rather than about provenance.
    """
    previous = stats_conn.row_factory
    try:
        stats_conn.row_factory = sqlite3.Row
        dump: dict = {}
        for label, (table, order_by) in _DUMP_TABLES.items():
            try:
                cursor = stats_conn.execute(f"SELECT * FROM {table}")
            except sqlite3.OperationalError:
                dump[label] = []
                continue
            names = [c[0] for c in cursor.description]
            rows = []
            for row in cursor:
                if "source" in names and str(row["source"]) != source:
                    continue
                record = {
                    name: row[name] for name in names
                    if name not in _PER_PASS_PROVENANCE
                }
                if "orphaned_at" in names:
                    record["orphaned"] = row["orphaned_at"] is not None
                rows.append(record)
            rows.sort(key=lambda record: tuple(
                _sort_key(record.get(column)) for column in order_by))
            dump[label] = [
                tuple(sorted(record.items())) for record in rows
            ]
        return dump
    finally:
        stats_conn.row_factory = previous


def wipe_projection(stats_conn) -> None:
    """Clear the re-derivable projection, keeping terminal evidence.

    Blocks, milestones and projection state are mutable projections the
    reconcile re-materializes. ``quota_threshold_events`` / ``quota_alert_arming``
    are terminal/forward-only evidence a rebuild explicitly preserves, so a
    "whole-history" comparison arm must preserve them too or it would be
    comparing two different questions.
    """
    for table in (
        "quota_window_blocks", "quota_percent_milestones",
        "quota_projection_state",
    ):
        stats_conn.execute(f"DELETE FROM {table}")
    stats_conn.commit()


class _GroupCounter:
    """Records which physical groups a reconcile pass loaded evidence for."""

    def __init__(self) -> None:
        self.group_keys: set[tuple[object, ...]] = set()
        self.observations = 0
        self.calls = 0

    @property
    def groups(self) -> int:
        return len(self.group_keys)


@contextlib.contextmanager
def count_expanded_groups(quota):
    """Count the physical groups a reconcile expands.

    Measured at ``load_codex_quota_observations``: the union, across every call
    the pass makes, of ``_lib_quota._physical_window_key`` over the returned
    observations. That is the same quantity as "groups expanded" — the
    projector must load a dirty group's COMPLETE membership before interpreting
    it, so a group it expands is exactly a group whose observations it loads —
    and it needs no implementation-specific hook, so it keeps measuring the
    right thing after the incremental projector lands.

    Counting groups rather than blocks is deliberate: a bridge re-anchor can
    legitimately move more than one block, so a block-count assertion would be
    wrong rather than strict.
    """
    counter = _GroupCounter()
    original = quota.load_codex_quota_observations

    def instrumented(*args, **kwargs):
        result = original(*args, **kwargs)
        counter.calls += 1
        counter.observations += len(result)
        for observation in result:
            counter.group_keys.add(
                _lib_quota._physical_window_key(observation))
        return result

    quota.load_codex_quota_observations = instrumented
    try:
        yield counter
    finally:
        quota.load_codex_quota_observations = original


def _iso(value: dt.datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _sort_key(value):
    """Total order across the mixed column types a dump row can hold."""
    if value is None:
        return (0, "")
    if isinstance(value, bool):
        return (1, int(value))
    if isinstance(value, (int, float)):
        return (2, float(value))
    return (3, str(value))
