"""Codex adapter for durable provider-neutral quota interpretation.

``quota_window_snapshots`` remains cache.db's physical, re-derivable evidence.
This module reads that committed cache after the S1 ingest lock releases and
reconciles an interpreted index in stats.db.  The two databases are not and do
not pretend to be one atomic transaction: a retry always derives a complete new
stats generation from the physical cache.
"""
from __future__ import annotations

import datetime as dt
import json
import secrets
import sqlite3
import sys
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping, NoReturn, Sequence

import _cctally_core
import _lib_accounts
from _cctally_core import _command_as_of, eprint
from _lib_quota import (
    CODEX_RESET_ANCHOR_TOLERANCE_SECONDS,
    QuotaBlock,
    QuotaForecast,
    QuotaFreshness,
    QuotaHistory,
    QuotaObservation,
    QuotaPercentMilestone,
    QuotaRule,
    QuotaWindowIdentity,
    adopt_unidentified_observations,
    build_blocks,
    build_history,
    forecast_quota,
    quota_freshness,
    quota_rule_fingerprint,
    quota_threshold_decisions,
    percent_milestones,
    physical_order_key,
    resolve_quota_rule,
    select_baseline,
    source_path_key,
)
from _lib_json_envelope import stamp_schema_version
from _lib_jsonl import (
    _codex_logical_limit_key,
    codex_snap_equivalent_limit_keys,
    codex_snap_equivalent_window_minutes,
    snap_codex_window_minutes,
    snap_window_minutes,
)
from _lib_codex_pools import (
    codex_history_is_model_scoped,
    codex_model_scoped_quota_pool,
    is_model_scoped_codex_quota,
)
import _lib_quota_alert_axes as _axes
import _lib_quota_ledger as _ledger


UTC = dt.timezone.utc
_DASHBOARD_PROJECTION_CERTIFICATE_KEY = "codex_quota_projection_certificate"


# --------------------------------------------------------------------------
# the incomplete-quota-projection read gate (#496 S5b §4.7)
# --------------------------------------------------------------------------
#
# The stats quota projection is materialized FROM cache.db, so a rebuild that
# could not fully recover the cache publishes a semantically PARTIAL projection
# inside an otherwise valid generation. Completing the cache later does not by
# itself reconcile that projection.
#
# "The next open reconciles it" is not enforceable: `RebuildResult` is
# process-local, a current-epoch `open_db` returns early without any
# reconciliation gate, and — decisively — in-place publication deliberately
# keeps already-open readers alive, so a connection can finish its old read
# transaction and observe the incomplete new generation without ever calling
# `open_db` again. The gate is therefore durable and PER TRANSACTION.
#
# Three properties decide its shape:
#
# 1. It cannot be an authorizer-style denial, because projection reads are
#    scattered across the dashboard, milestone-history, quota and library
#    modules rather than centralized.
# 2. It must run BEFORE any fallback-catching SQL. Most of those reads sit
#    inside `except sqlite3.Error` handlers that would render a denial as empty
#    data instead of as an error.
# 3. It never acquires a lock. Inside a caller transaction it raises a typed
#    retry signal rather than starting reconciliation, because taking the
#    maintenance and cache locks after a SQLite transaction has opened inverts
#    the repository's lock order. The caller ends its transaction and retries.

#: The one remedy every refusal names. Only two things clear the durable
#: incomplete flag — a reconciliation, which only `cctally cache-sync` and the
#: dashboard server arm, and a later rebuild whose coverage came back complete —
#: so a surface that says only "incomplete" leaves the user with no next step.
#: It is appended to every `QuotaProjectionIncomplete` message rather than left
#: to each caller, because the ten raise sites reach surfaces that render the
#: exception STRING (the TUI's `last_sync_error`, the dashboard's error paths)
#: and would otherwise each have to restate it.
QUOTA_PROJECTION_REMEDY = (
    "run `cctally cache-sync` to reconcile it"
)


class QuotaProjectionIncomplete(Exception):
    """The published quota projection is incomplete — a RETRY signal.

    Not an error verdict: the projection is reconcilable, and the caller's job
    is to end its transaction and retry rather than to report a failure. It
    carries the VERSIONED recovery target rather than a bare coordinate, so a
    target written by one binary is never misread by another.

    The decided contract for a gated caller is: end the transaction, let a
    maintenance-capable process reconcile (`cctally cache-sync`, or the
    dashboard server, which arms
    `_cctally_journal.reconcile_incomplete_quota_projection` at its own opens),
    and retry once — render a "reconciling" state naming that remedy rather than
    empty data if the retry is still refused.

    **Where the contract is implemented, by path rather than by module.** Every
    message carries `QUOTA_PROJECTION_REMEDY`, so any surface that renders the
    exception string names the remedy.

    The three handlers in `bin/_cctally_tui.py` catch this ahead of their
    `except Exception` neighbours, so the refusal is attributed to its own
    `quota-projection` leg instead of being sanitized into a generic
    stats-or-cache failure. **Those handlers cover the dashboard's snapshot
    build too**, and an earlier version of this docstring wrongly said they did
    not: `bin/_cctally_dashboard.py` builds its snapshot by calling
    `_tui_build_snapshot(..., precompute_envelope=True)`, and
    `_tui_build_source_bundle` — which reaches all four
    `bin/_cctally_dashboard_sources.py` sites — is reachable only under that
    flag. `_sync_failure_envelope` turns the resulting attribution into a
    rendered `quota_projection_incomplete` state whose `action` names
    `cctally cache-sync`, on the existing rendered contract, so no
    `dashboard/web/` change and no real-browser QA gate is involved.

    The two `bin/_cctally_dashboard.py` HTTP route handlers — `/api/milestones`
    cycle detail and the `/api/source/…` route that reaches
    `_build_codex_block_detail`, and through it the two
    `bin/_cctally_milestone_history.py` sites — are a separate path with no
    snapshot and no envelope. They answer a typed **503**
    `quota_projection_incomplete` carrying the same `action`, instead of the
    generic 500 and 400 that reported a server fault and named no remedy.
    """

    def __init__(self, message, *, target_version=0, recovery_target=None):
        super().__init__(message)
        self.target_version = target_version
        self.recovery_target = recovery_target


#: Every direct read of `quota_window_blocks` or `quota_projection_state` whose
#: TABLE NAME IS A LITERAL, as `<file>::<function>::<table>`. A static guard
#: keeps this complete, the same discipline `FROZEN_WRITE_SITES` applies to
#: writes — a new site has to be a deliberate act rather than a silent one.
#:
#: The name says "read sites" and one member is a `DELETE FROM
#: quota_projection_state`: the scanner resolves a target after `FROM` or
#: `JOIN`, and a DELETE writes through the same `FROM`. It is classified
#: `projector` so the outcome is identical either way, and it is left in rather
#: than special-cased, because a scanner that skipped `DELETE ... FROM` would
#: also have to decide what to do with every other statement form and would
#: acquire a blind spot doing it.
#:
#: A site whose target is INTERPOLATED is invisible to this set — `FROM {table}`
#: has no literal table name. `PROJECTION_DYNAMIC_READ_SITES` below freezes
#: those by count, which is the only property a static scan can freeze.
PROJECTION_READ_CHOKEPOINTS: "frozenset[str]" = frozenset({
    "_cctally_dashboard.py::_build_codex_block_detail::quota_window_blocks",
    "_cctally_dashboard.py::_handle_get_milestones_week::quota_window_blocks",
    "_cctally_dashboard_sources.py::_codex_weekly_periods::quota_window_blocks",
    "_cctally_dashboard_sources.py::codex_projection_coherence::"
    "quota_projection_state",
    "_cctally_dashboard_sources.py::_quota_wire::quota_window_blocks",
    "_cctally_dashboard_sources.py::_codex_block_account_keys::"
    "quota_window_blocks",
    "_cctally_milestone_history.py::_load_codex_cycles::quota_window_blocks",
    "_cctally_milestone_history.py::_codex_five_hour_rows::quota_window_blocks",
    "_lib_dashboard_sources.py::<module>::quota_projection_state",
    "_lib_dashboard_sources.py::<module>::quota_window_blocks",
    "_cctally_quota.py::_stats_projection_signatures_match::"
    "quota_projection_state",
    "_cctally_quota.py::_blocks_missing_reverse_map::quota_window_blocks",
    "_cctally_quota.py::_root_group_pairs::quota_window_blocks",
    "_cctally_quota.py::_root_accounts::quota_window_blocks",
    "_cctally_quota.py::<module>::quota_window_blocks",
    "_cctally_quota.py::_orphan_unseen::quota_window_blocks",
    "_cctally_quota.py::_orphan_unseen_scoped::quota_window_blocks",
    "_cctally_quota.py::_apply_quota_projection_rows::quota_projection_state",
})

#: What each enumerated site does about the gate.
#:
#: `gate` — `assert_projection_readable` runs in that function before its SQL.
#: `gate_at_caller` — the read is in a pure kernel that may not import this
#:   module, so its callers gate it instead. Every such site MUST name those
#:   callers in `PROJECTION_GATE_CALLERS`, and the guard resolves each named
#:   caller and fails when that function does not call the gate.
#: `projector` — a read by the projection MACHINERY itself. Gating these would
#:   deadlock recovery: they are what re-materializes the projection and clears
#:   the flag, so they must be able to read while it is set.
#: `diagnostic` — a debug surface that reports a raw row COUNT and renders no
#:   projection value. Gating one would replace a diagnostic answer with an
#:   exception at exactly the moment an operator is diagnosing the incomplete
#:   projection, which inverts what the surface is for.
PROJECTION_READ_SITE_ACTIONS: "dict[str, str]" = {
    "_cctally_dashboard.py::_build_codex_block_detail::quota_window_blocks":
        "gate",
    "_cctally_dashboard.py::_handle_get_milestones_week::quota_window_blocks":
        "gate",
    "_cctally_dashboard_sources.py::_codex_weekly_periods::quota_window_blocks":
        "gate",
    "_cctally_dashboard_sources.py::codex_projection_coherence::"
    "quota_projection_state": "gate",
    "_cctally_dashboard_sources.py::_quota_wire::quota_window_blocks": "gate",
    "_cctally_dashboard_sources.py::_codex_block_account_keys::"
    "quota_window_blocks": "gate",
    "_cctally_milestone_history.py::_load_codex_cycles::quota_window_blocks":
        "gate",
    "_cctally_milestone_history.py::_codex_five_hour_rows::quota_window_blocks":
        "gate",
    # `codex_stats_digest`'s relation table is module-level in a pure kernel
    # that must not import this module, so its callers gate it. They are named
    # in `PROJECTION_GATE_CALLERS` and the guard verifies each one.
    "_lib_dashboard_sources.py::<module>::quota_projection_state":
        "gate_at_caller",
    "_lib_dashboard_sources.py::<module>::quota_window_blocks":
        "gate_at_caller",
    "_cctally_quota.py::_stats_projection_signatures_match::"
    "quota_projection_state": "projector",
    "_cctally_quota.py::_blocks_missing_reverse_map::quota_window_blocks":
        "projector",
    "_cctally_quota.py::_root_group_pairs::quota_window_blocks": "projector",
    "_cctally_quota.py::_root_accounts::quota_window_blocks": "projector",
    # A module-level sweep-scoping SQL constant, not a function body.
    "_cctally_quota.py::<module>::quota_window_blocks": "projector",
    "_cctally_quota.py::_orphan_unseen::quota_window_blocks": "projector",
    "_cctally_quota.py::_orphan_unseen_scoped::quota_window_blocks":
        "projector",
    "_cctally_quota.py::_apply_quota_projection_rows::quota_projection_state":
        "projector",
}

#: For each `gate_at_caller` site, the `<file>::<function>` callers that run the
#: gate on its behalf. Naming them is what makes the classification checkable:
#: an unnamed caller reduces `gate_at_caller` to an assertion nothing tests, and
#: that is how the first version of this map came to claim a gating caller that
#: neither called the kernel nor called the gate.
PROJECTION_GATE_CALLERS: "dict[str, tuple[str, ...]]" = {
    "_lib_dashboard_sources.py::<module>::quota_projection_state": (
        "_cctally_tui.py::_tui_build_source_bundle",
        "_cctally_tui.py::_tui_compute_dispatch_signature",
    ),
    "_lib_dashboard_sources.py::<module>::quota_window_blocks": (
        "_cctally_tui.py::_tui_build_source_bundle",
        "_cctally_tui.py::_tui_compute_dispatch_signature",
    ),
}

#: Dynamic-target read sites per file — a `FROM`/`JOIN` whose target is
#: interpolated, so only the COUNT can be frozen. `bin/cctally` carries none.
#:
#: This exists because `PROJECTION_READ_CHOKEPOINTS`' scanner reads string
#: LITERALS, and a read written as `f"SELECT COUNT(*) FROM {table} WHERE
#: {where}"` therefore reaches `quota_window_blocks` while being invisible to
#: it — which is exactly what happened to `_cctally_dashboard._debug_source_
#: counts`. The count cannot say which table a site reaches, but it does make a
#: NEW dynamic read impossible to add silently, and the author then has to
#: classify it in `PROJECTION_DYNAMIC_READ_ACTIONS` if it reaches a projection
#: family. It mirrors `FROZEN_DYNAMIC_SITES` in
#: `tests/test_stats_writer_surface_386.py`, which solved the same problem for
#: the write surface.
PROJECTION_DYNAMIC_READ_SITES: "dict[str, int]" = {
    "_cctally_account.py": 1,
    # Three, all in `_import_legacy_conversation_rows`: `FROM main.{table}`,
    # `FROM cache_db.{table}` and the `SELECT … FROM cache_db.{table}` of the
    # copy. They were invisible to BOTH guards until #496 S5b gave the read
    # patterns the schema-qualifier prefix the write scan already had, which is
    # the hole `PROJECTION_DYNAMIC_READ_SITES` exists to close. `{table}` there
    # iterates a hardcoded conversation-table tuple, so none of them reaches a
    # projection family and none needs an entry below.
    "_cctally_cache.py": 3,
    "_cctally_core.py": 2,
    "_cctally_dashboard.py": 3,
    "_cctally_dashboard_envelope.py": 5,
    "_cctally_db.py": 7,
    "_cctally_doctor.py": 1,
    "_cctally_five_hour.py": 1,
    "_cctally_journal.py": 18,
    "_cctally_pricing_check.py": 1,
    "_cctally_quota.py": 1,
    "_cctally_record.py": 1,
    "_cctally_release.py": 4,
    "_cctally_setup.py": 3,
    "_cctally_tui.py": 1,
    "_lib_conversation_query.py": 1,
    "_lib_conversation_retention.py": 2,
    "_lib_doctor.py": 1,
    "_lib_snapshot_cache.py": 1,
    "_lib_subscription_weeks.py": 1,
}

#: The dynamic-target reads that provably reach a projection family, named by
#: hand as `<file>::<function>`, with the same action vocabulary as
#: `PROJECTION_READ_SITE_ACTIONS`. The scan cannot resolve `{table}`, so this is
#: the human half of the count freeze above.
PROJECTION_DYNAMIC_READ_ACTIONS: "dict[str, str]" = {
    # `_DEBUG_SOURCE_STATS_TABLES` carries `("quota_window_blocks",
    # "source='codex'")` and the query is built as
    # `f"SELECT COUNT(*) FROM {table} WHERE {where}"`. It answers a debug
    # endpoint with a row count and renders no projection value, so it is
    # `diagnostic` rather than `gate` — see the vocabulary above.
    "_cctally_dashboard.py::_debug_source_counts": "diagnostic",
}


def _refuse_unreadable_flag(exc: "sqlite3.Error") -> NoReturn:
    """Fail closed on an unreadable flag — but never OWN a corrupt index.

    Annotated `NoReturn` because both call sites depend on it: each is an
    `except sqlite3.Error` arm followed immediately by a statement reading the
    name the failed query would have bound, so a return here would raise
    `UnboundLocalError` instead of failing closed. The annotation makes "always
    raises" checkable rather than a property a reader has to infer.

    A corrupt `stats.db` fails this probe like any other unreadable one, and
    wrapping it would file it as "the quota projection is incomplete". That is
    the wrong owner and it costs the right one its signal: `_cctally_tui`
    catches `QuotaProjectionIncomplete` ahead of the corruption branch, so a
    wrapped corruption error never became `_StatsSnapshotCorruption` and never
    reached the #407 heal — the index stayed corrupt while every surface
    reported a reconcilable quota view. A corruption error is therefore
    re-raised UNCHANGED, which is still fail-closed: no projection value is
    served either way, and the caller that owns corruption gets to see it.
    """
    import _cctally_db

    if _cctally_db._is_sqlite_corruption_error(exc):
        raise exc
    raise QuotaProjectionIncomplete(
        f"the quota projection flag could not be read ({exc}); "
        + QUOTA_PROJECTION_REMEDY
    ) from exc


def assert_projection_readable(conn) -> None:
    """Refuse a projection read while the published projection is incomplete.

    Raises :class:`QuotaProjectionIncomplete`. Acquires no lock and starts no
    reconciliation, so it is safe inside a caller transaction — which is the
    only placement that covers a connection opened BEFORE the publication.

    Exactly ONE condition is read as "readable": a MISSING
    `stats_quota_projection_state` table. That table arrived with the
    epoch-1009 stats index, so its absence means the index predates the flag
    and there is no incomplete projection for the flag to describe. Every other
    `sqlite3.Error` fails CLOSED and raises, because "I could not read the flag"
    is not "the flag is clear".

    Absence is decided STRUCTURALLY, by asking `sqlite_master`, rather than by
    matching `no such table` against an error message. The message form has no
    constructible false negative, but it does have a false POSITIVE — a message
    embedding that phrase for another reason, such as a view or trigger
    resolving through a missing table — and a false positive here fails OPEN,
    which is the direction this gate exists to prevent. The extra query is one
    cheap lookup, and it still raises in the unreadable-database case because
    `sqlite_master` is unreadable there too.

    The distinction matters because the old justification — that an epoch-1009
    index always carries the table, so a failing probe cannot be serving an
    incomplete projection — is false. A connection can be alive, hold the
    epoch-1009 index open and still fail its reads for a reason that is not
    absence, and that connection is exactly the one §4.7's per-transaction gate
    exists to cover.
    """
    if conn is None:
        return
    try:
        present = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='stats_quota_projection_state'"
        ).fetchone()
    except sqlite3.Error as exc:
        _refuse_unreadable_flag(exc)
    if present is None:
        return
    try:
        row = conn.execute(
            "SELECT incomplete, target_version, recovery_target_json "
            "FROM stats_quota_projection_state WHERE id = 1"
        ).fetchone()
    except sqlite3.Error as exc:
        _refuse_unreadable_flag(exc)
    if row is None or not int(row[0] or 0):
        return
    target = None
    if row[2]:
        try:
            target = json.loads(row[2])
        except ValueError:
            target = None
    raise QuotaProjectionIncomplete(
        "the published quota projection is incomplete; "
        + QUOTA_PROJECTION_REMEDY,
        target_version=int(row[1] or 0),
        recovery_target=target,
    )
# 2 -> 3 (public #5): the root physical signature is now COMPOSED from per-group
# digests instead of digested over a whole root's observation tuples. The
# semantics are unchanged — still an exact-equality function of the physical
# evidence — but the VALUE is not, so every certificate written under the old
# scheme has to be rejected rather than compared. The version also invalidates
# the ledger mechanism itself: a classification change alters interpreted keys
# with no row mutation to observe, so a bump queues one complete pass.
_CODEX_QUOTA_INTERPRETATION_VERSION = 3

#: Above this many dirty loading units, a bounded pass stops being a saving: it
#: is one indexed query per unit (times the snap closure) against a single
#: unbounded scan, and the sweep's ``IN`` list stops fitting comfortably in
#: SQLite's variable budget. A burst that wide is a rebuild or a first ingest,
#: which is exactly what the full path is for.
_MAX_INCREMENTAL_UNITS = 128

#: Loading-unit keys per SQL ``IN`` chunk in the scoped sweep.
_SWEEP_KEY_CHUNK = 400

#: How long a projection may go without a whole-history verification pass
#: (spec §2). The scoped sweep structurally cannot see two classes — a block
#: whose physical group is absent from the cache entirely, and a milestone on a
#: historic root no longer active — so without a deadline they are repairable
#: only by an interpretation bump, a rebuild or a burst overflow, none of which
#: happen on a normal install. One full pass per day against the roughly four
#: seconds this design removes from every turn. EVERY full pass stamps the
#: deadline, whatever triggered it, so it is satisfied by whichever caller
#: reaches it first — a dashboard tick or a `codex quota` invocation pays the
#: cost off the hook path entirely — except on a hook-only install, where no
#: such caller exists. There the deadline would land on the blocking hook path
#: as the one unbounded operation this design otherwise removes, so the hook
#: passes ``full_pass="defer"`` and the pass is handed to the detached
#: ``_codex-quota-verify`` worker instead (see ``_defer_codex_quota_verification``).
CODEX_QUOTA_FULL_VERIFICATION_INTERVAL_SECONDS = 86400

#: The hidden self-subcommand that performs a deferred verification pass.
CODEX_QUOTA_VERIFY_COMMAND = "_codex-quota-verify"

#: Marker whose mtime throttles worker spawns. In ``APP_DIR`` rather than
#: cache.db because the decision is made with no cache write transaction open,
#: and because a spawn is process state, not projection state.
CODEX_QUOTA_VERIFY_MARKER_NAME = "codex-quota-verify.last-attempt"

#: Minimum spacing between deferred-verification spawns. Stamped on ATTEMPT,
#: not on success: ``last_full_pass_at`` moves only when a pass COMPLETES, so
#: every tick between the spawn and the worker's commit still reads as due, and
#: a success-stamped throttle would put one worker per hook tick on the box.
#: Comfortably longer than a measured full pass (~4s locally, ~15s on the
#: reporter's store) while still retrying many times inside the one-day
#: interval if a worker dies.
CODEX_QUOTA_VERIFY_SPAWN_THROTTLE_SECONDS = 600


@dataclass(frozen=True)
class QuotaProjectionResult:
    """Counts from one completed reconciliation transaction."""

    generation: str | None
    blocks_upserted: int
    milestones_upserted: int
    blocks_orphaned: int
    milestones_orphaned: int
    roots_stamped: int
    alerts_dispatched: int


@dataclass(frozen=True)
class CodexQuotaBreakdownRow:
    """One milestone correlated with root-qualified Codex accounting."""

    percent: int
    captured_at: dt.datetime
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int
    total_tokens: int
    cost_usd: float
    marginal_cost_usd: float


def _parse_utc(value: str, label: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return parsed.astimezone(UTC)


def _utc_iso(value: dt.datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _physical_tuple(observation: QuotaObservation) -> tuple[dt.datetime, str, int]:
    return (observation.captured_at, observation.source_path, observation.line_offset)


def _cache_connection() -> sqlite3.Connection:
    """Open a read-only cache connection without invoking or re-running ingest."""
    path = _cctally_core.CACHE_DB_PATH
    if not path.exists():
        raise FileNotFoundError(path)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _cache_root_keys(conn: sqlite3.Connection) -> set[str]:
    try:
        return {
            str(row[0]) for row in conn.execute(
                "SELECT source_root_key FROM codex_source_roots"
            )
        }
    except sqlite3.OperationalError:
        return set()


def codex_physical_mutation_seq(conn: sqlite3.Connection) -> int:
    """Return the cache-local Codex physical sequence without scanning history."""
    try:
        row = conn.execute(
            "SELECT value FROM cache_meta WHERE key='codex_physical_mutation_seq'"
        ).fetchone()
        return 0 if row is None else int(row[0])
    except (sqlite3.Error, TypeError, ValueError):
        return 0


def load_codex_quota_projection_certificate(
    conn: sqlite3.Connection,
) -> tuple[int, dict[str, str]] | None:
    """Read the post-reconciliation physical-signature certificate in O(1)."""
    try:
        row = conn.execute(
            "SELECT value FROM cache_meta WHERE key=?",
            (_DASHBOARD_PROJECTION_CERTIFICATE_KEY,),
        ).fetchone()
        if row is None:
            return None
        payload = json.loads(str(row[0]))
        if (
            int(payload["interpretationVersion"])
            != _CODEX_QUOTA_INTERPRETATION_VERSION
        ):
            return None
        sequence = int(payload["sequence"])
        signatures = {
            str(root_key): str(signature)
            for root_key, signature in dict(payload["signatures"]).items()
        }
    except (sqlite3.Error, TypeError, ValueError, KeyError, json.JSONDecodeError):
        return None
    if sequence < 0 or any(len(signature) != 64 for signature in signatures.values()):
        return None
    return sequence, signatures


def _store_codex_quota_projection_certificate(
    *,
    sequence: int,
    signatures: Mapping[str, str],
    prune_ledger_through: "int | None" = None,
) -> None:
    """Stamp exact validated signatures only if cache physical state is unchanged.

    The certificate is written after the independent stats transaction commits.
    A later cache mutation necessarily advances ``sequence``, so a dashboard
    reader fails coherence rather than combining new physical cache data with
    the prior projection certificate.

    ``prune_ledger_through`` (public #5) deletes consumed change-ledger entries
    in the same transaction. Nothing else prunes them, and nothing bounds them:
    a ``cache-sync --rebuild`` on a 211K-observation store wipes and re-ingests,
    which the triggers record as roughly 422K rows (one delete plus one insert
    each). Entries at or below the committed watermark are provably consumed —
    the projection that consumed them is already durable — and ``seq`` is
    ``AUTOINCREMENT``, so a pruned high value is never reissued and the
    watermark can never be overtaken from below.

    The prune runs even when the certificate itself is declined: a sequence that
    advanced mid-pass means new evidence landed, not that the old entries are
    unconsumed.
    """
    path = _cctally_core.CACHE_DB_PATH
    if not path.exists():
        return
    try:
        conn = sqlite3.connect(path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            if prune_ledger_through:
                try:
                    conn.execute(
                        "DELETE FROM quota_window_change_log WHERE seq <= ?",
                        (int(prune_ledger_through),),
                    )
                except sqlite3.OperationalError:
                    pass  # a cache too old to carry the ledger
            if codex_physical_mutation_seq(conn) != sequence:
                conn.commit()
                return
            payload = json.dumps({
                "interpretationVersion": _CODEX_QUOTA_INTERPRETATION_VERSION,
                "sequence": sequence,
                "signatures": dict(sorted(signatures.items())),
            }, sort_keys=True, separators=(",", ":"))
            conn.execute(
                "INSERT INTO cache_meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (_DASHBOARD_PROJECTION_CERTIFICATE_KEY, payload),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        return


def _stats_projection_signatures_match(
    stats_conn: sqlite3.Connection,
    active_roots: set[str],
    cert_sigs: Mapping[str, str],
) -> bool:
    """True iff stats.db's projection signature matches the certificate for every root.

    The cache certificate alone does not prove stats.db still holds the
    projection: stats.db can be independently wiped/recovered while cache.db
    persists (F1).  Require an exact ``quota_projection_state.physical_signature``
    match for every active root before the reconcile is allowed to short-circuit.
    A missing row, a mismatch, or any ``sqlite3.Error`` degrades to False, which
    forces the full reconcile (fail-safe).

    ACCOUNT-AWARE and ORDER-INDEPENDENT (public #5 Task 9). The signature is
    per-root by construction, so every ``(root, account)`` row for a root must
    carry the same value; collapsing the rows into one dictionary let whichever
    row happened to come last decide, which is a real answer only when they
    already agree. Requiring the root's rows to agree — and to exist — turns a
    partially-updated projection from a coin flip into a mismatch, and a
    mismatch is the fail-safe direction.
    """
    try:
        rows = stats_conn.execute(
            "SELECT source_root_key, account_key, physical_signature "
            "  FROM quota_projection_state"
        ).fetchall()
    except sqlite3.Error:
        return False
    by_root: dict[str, set[str]] = {}
    for row in rows:
        by_root.setdefault(str(row[0]), set()).add(str(row[2]))
    for root in active_roots:
        stored = by_root.get(root)
        if not stored or len(stored) != 1:
            return False
        if next(iter(stored)) != cert_sigs.get(root):
            return False
    return True


# ── the change ledger's watermark and the state that rides with it ─────────

def _ledger_state(stats_conn: sqlite3.Connection) -> dict | None:
    """Read the incremental projector's own state, or ``None``.

    ``None`` — a missing row, a missing table, any error — means the projector
    has no consumed range it can trust and the next pass must be a complete one.
    Fail-safe by construction: the expensive answer is the correct one.
    """
    try:
        row = stats_conn.execute(
            "SELECT watermark_seq, interpretation_version, alerts_enabled, "
            "       next_evaluation_at_utc, last_full_pass_at, "
            "       next_evaluation_by_root_json "
            "  FROM quota_projection_ledger_state WHERE source='codex'"
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    try:
        schedule_wire = json.loads(str(row[5]))
        if not isinstance(schedule_wire, dict):
            return None
        schedule: dict[str, str] = {}
        for root_key, captured_at in schedule_wire.items():
            if not isinstance(root_key, str) or not root_key:
                return None
            parsed = _parse_utc(str(captured_at), "next_evaluation_by_root_json")
            schedule[root_key] = _utc_iso(parsed)
        scalar_boundary = None if row[3] is None else str(row[3])
        if schedule:
            if scalar_boundary is None:
                return None
            parsed_scalar = _utc_iso(_parse_utc(
                scalar_boundary, "next_evaluation_at_utc"))
            if parsed_scalar != min(schedule.values()):
                return None
        return {
            "watermark": int(row[0]),
            "interpretation_version": int(row[1]),
            "alerts_enabled": (
                None if row[2] is None else bool(int(row[2]))),
            "next_evaluation_at": (
                scalar_boundary),
            "last_full_pass_at": (
                None if row[4] is None else str(row[4])),
            "next_evaluation_by_root": schedule,
        }
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def _full_verification_due(ledger_state: "dict | None", now: dt.datetime) -> bool:
    """True when the projection is overdue for a whole-history pass (spec §2).

    Fail-safe in the expensive direction at every step: no state, no stamp, an
    unparsable stamp and a stamp in the future all read as due. A stamp ahead of
    ``now`` is a clock that moved backwards, and treating it as satisfied would
    suspend the verification until wall time caught up.
    """
    if ledger_state is None:
        return True
    stamp = ledger_state.get("last_full_pass_at")
    if not stamp:
        return True
    try:
        last = _parse_utc(str(stamp), "last_full_pass_at")
    except (TypeError, ValueError):
        return True
    elapsed = (now - last).total_seconds()
    if elapsed < 0:
        return True
    return elapsed >= CODEX_QUOTA_FULL_VERIFICATION_INTERVAL_SECONDS


def _log_codex_worker_outcome(
    op: str, outcome: str, detail: str = "", *, error: str = "",
) -> None:
    """One durable line about a detached Codex worker, in ``hook-tick.log``.

    The workers spawned from the hook path have all three streams on
    ``/dev/null`` and an exit code nobody observes, so this file is the only
    place their outcome can land. Best-effort and never raising: a diagnostic
    must not be able to fail the operation it describes.

    ``detail`` carries the caller's OWN structured ``k=v`` fragments — counts, a
    duration, a fixed reason token — and is emitted verbatim. Anything derived
    from an exception goes through ``error`` instead, which is rendered LAST and
    defused HERE rather than at the call site. That is what makes the privacy
    guarantee a property of the renderer, the way it already is of
    ``_codex_lifecycle_log_line``: no future caller can reintroduce a path, a
    conversation id or a field separator by forgetting to scrub. A caller
    holding the exception should still pass ``_hook_log_error_detail(exc)``,
    which additionally narrows the ``OSError`` family's embedded ``filename``
    away at the source.
    """
    try:
        from _cctally_record import (
            _hook_log_safe_free_text, _hook_tick_log_line,
            _hook_tick_log_rotate_if_needed,
        )
        stamp = _utc_iso(dt.datetime.now(UTC))
        suffix = ""
        if error:
            safe = _hook_log_safe_free_text(error)
            if safe:
                suffix = f" error={safe}"
        _hook_tick_log_line(
            f"{stamp} provider=codex op={op} result={outcome}"
            + (f" {detail}" if detail else "") + suffix)
        _hook_tick_log_rotate_if_needed()
    except Exception:
        pass


def _defer_codex_quota_verification() -> str:
    """Hand the periodic whole-history pass to a detached worker.

    Returns ``"spawned"``, ``"throttled"`` or ``"failed"``, and WRITES the two
    outcomes an operator would act on to ``hook-tick.log``. No caller
    distinguishes them — every one of them skips the pass regardless, because a
    missed pass is bounded staleness the next tick retries while running it
    inline is the ~14-30s reconcile against Codex's 30-second hook timeout that
    this whole change exists to remove. So logging here is what stops a
    permanently failing hand-off from being invisible; the value stays a return
    for tests.

    ``"throttled"`` is deliberately NOT logged: it is the ordinary state between
    a spawn and the worker's commit, and the Codex lifecycle throttle is 15
    seconds, so logging it would bury the two outcomes that matter.

    Marker-first, exactly like ``update-check.last-fetch``: the mtime is stamped
    BEFORE the spawn, so a worker that dies cannot make every following tick
    spawn another one. If the marker itself cannot be written we do not spawn at
    all — without it the spawn rate is unbounded, which is worse than a deferred
    verification.
    """
    marker = _cctally_core.APP_DIR / CODEX_QUOTA_VERIFY_MARKER_NAME
    try:
        age = time.time() - marker.stat().st_mtime
    except OSError:
        age = None
    if age is not None and 0 <= age < CODEX_QUOTA_VERIFY_SPAWN_THROTTLE_SECONDS:
        return "throttled"
    try:
        _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)
        marker.touch()
    except OSError:
        _log_codex_worker_outcome(
            "quota-verify-spawn", "failed", "reason=marker_unwritable")
        return "failed"
    from _cctally_update import _spawn_detached
    if _spawn_detached(CODEX_QUOTA_VERIFY_COMMAND):
        _log_codex_worker_outcome("quota-verify-spawn", "spawned")
        return "spawned"
    _log_codex_worker_outcome("quota-verify-spawn", "failed", "reason=spawn")
    return "failed"


def cmd_codex_quota_verify_internal(args) -> int:
    """Hidden ``_codex-quota-verify`` handler: the deferred whole-history pass.

    Reporting only — no ``alert_eligible_root_keys``, because alert eligibility
    belongs to whoever holds the per-root lifecycle lock and this worker holds
    none. That matches every other off-hook caller of the verification (the
    dashboard tick, ``codex quota``), which is the point: the worker is one of
    those callers, moved off the blocking path.

    ``last_full_pass_at`` is stamped inside the pass's own stats transaction, so
    a worker that is killed leaves the deadline due and the next tick retries.
    Always returns 0 — a detached worker's exit code is observed by nobody, and
    a raised exception would only produce an unread traceback.

    The outcome goes to ``hook-tick.log``, following the ``_update-check``
    precedent of writing ``update.log`` for exactly this reason. All three of
    this worker's streams are ``/dev/null`` and its exit code is unobserved, so
    a bare ``except: pass`` made a persistently failing verification completely
    invisible — and because the deadline only moves when a pass COMMITS, such a
    worker respawns every throttle window forever with nothing to show for it.
    """
    started = time.monotonic()

    def _log(outcome: str, detail: str = "", *, error: str = "") -> None:
        _log_codex_worker_outcome(
            "quota-verify", outcome,
            f"dur_ms={max(0, int((time.monotonic() - started) * 1000))}"
            + (f" {detail}" if detail else ""),
            error=error)

    try:
        result = reconcile_codex_quota_projection(force_full=True)
    except Exception as exc:
        # Same defusing as the lifecycle line, for the same two reasons: the
        # `OSError` family's `str()` carries a rollout path, and the log's
        # reader is a last-wins `k=v` comprehension a free-text `=` can beat.
        # `_log_codex_worker_outcome` performs it; this narrows the `filename`
        # away first, which only the caller holding the exception can do.
        from _cctally_record import _hook_log_error_detail
        _log("error", error=_hook_log_error_detail(exc))
        return 0
    _log(
        "success",
        f"blocks={int(getattr(result, 'blocks_upserted', 0) or 0)} "
        f"milestones={int(getattr(result, 'milestones_upserted', 0) or 0)}")
    return 0


def _store_ledger_state(
    conn: sqlite3.Connection, *, watermark: int,
    alerts_enabled: "bool | None", next_evaluation_at: "str | None",
    last_full_pass_at: "str | None",
    next_evaluation_by_root: Mapping[str, str],
) -> None:
    """Stamp the consumed range, the non-dirtiness alert axes and the deadline.

    Runs on the caller's transaction, alongside the projection it describes.
    ``last_full_pass_at`` is the caller's decision: a full pass passes its own
    ``now``, a bounded pass passes the stored value through unchanged.
    """
    conn.execute(
        """INSERT INTO quota_projection_ledger_state
             (source, watermark_seq, interpretation_version, alerts_enabled,
              next_evaluation_at_utc, last_full_pass_at,
              next_evaluation_by_root_json)
           VALUES ('codex',?,?,?,?,?,?)
           ON CONFLICT(source) DO UPDATE SET
             watermark_seq=excluded.watermark_seq,
             interpretation_version=excluded.interpretation_version,
             alerts_enabled=excluded.alerts_enabled,
             next_evaluation_at_utc=excluded.next_evaluation_at_utc,
             last_full_pass_at=excluded.last_full_pass_at,
             next_evaluation_by_root_json=excluded.next_evaluation_by_root_json""",
        (
            int(watermark), _CODEX_QUOTA_INTERPRETATION_VERSION,
            None if alerts_enabled is None else int(bool(alerts_enabled)),
            next_evaluation_at, last_full_pass_at,
            json.dumps(
                dict(sorted(next_evaluation_by_root.items())),
                sort_keys=True, separators=(",", ":")),
        ),
    )


def _ledger_max_seq(cache_conn: sqlite3.Connection) -> int | None:
    """The ledger's high-water sequence, or ``None`` when unreadable.

    ``None`` (no table — a cache too old to carry the ledger) forces the full
    path, which is exactly today's behaviour and correct, just not incremental.

    Read from ``sqlite_sequence`` and not only from ``MAX(seq)``, because the
    projector PRUNES consumed entries: after a prune the table is empty and
    ``MAX(seq)`` is 0, which would read as "the ledger was reset below the
    watermark" on every single clean tick — and that reset detection is what
    forces a whole-history pass. ``AUTOINCREMENT`` keeps its high-water in
    ``sqlite_sequence`` across a ``DELETE`` and only loses it when the table
    itself is dropped and recreated, which is exactly the event being detected.
    ``MAX(seq)`` is still folded in so a cache whose sequence row is missing but
    whose rows are not degrades to the safe direction.
    """
    try:
        row = cache_conn.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM quota_window_change_log"
        ).fetchone()
    except sqlite3.Error:
        return None
    high = 0 if row is None else int(row[0])
    try:
        row = cache_conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name='quota_window_change_log'"
        ).fetchone()
        if row is not None and row[0] is not None:
            high = max(high, int(row[0]))
    except (sqlite3.Error, TypeError, ValueError):
        pass
    return high


def _ledger_rows_after(
    cache_conn: sqlite3.Connection, low: int, high: int,
) -> list[dict]:
    """Every ledger entry in ``(low, high]``, as plain dicts for the kernel."""
    if high <= low:
        return []
    columns = ", ".join(
        f"{side}{name}"
        for side in ("old_", "new_")
        for name in _ledger.LEDGER_GROUP_SUFFIXES
    )
    previous = cache_conn.row_factory
    try:
        cache_conn.row_factory = sqlite3.Row
        return [
            dict(row) for row in cache_conn.execute(
                f"SELECT op, {columns} FROM quota_window_change_log "
                " WHERE seq > ? AND seq <= ?", (int(low), int(high)),
            )
        ]
    except sqlite3.Error:
        return []
    finally:
        cache_conn.row_factory = previous


def _armed_identities(
    stats_conn: sqlite3.Connection,
) -> dict[tuple, tuple[QuotaWindowIdentity, str]]:
    """Every persisted Codex arming boundary, keyed by its identity tuple.

    This is where the policy axis reads its STORED fingerprints from: the arming
    row already persists the resolved rule's hash per identity, so no second
    store is needed to detect a rule change.
    """
    try:
        rows = stats_conn.execute(
            """SELECT source, source_root_key, account_key, logical_limit_key,
                      observed_slot, window_minutes, rule_fingerprint
                 FROM quota_alert_arming WHERE source='codex'"""
        ).fetchall()
    except sqlite3.Error:
        return {}
    armed: dict[tuple, tuple[QuotaWindowIdentity, str]] = {}
    for row in rows:
        try:
            identity = QuotaWindowIdentity(
                source=str(row[0]), source_root_key=str(row[1]),
                account_key=str(row[2]), logical_limit_key=str(row[3]),
                observed_slot=str(row[4]), window_minutes=int(row[5]),
            )
        except (TypeError, ValueError):
            continue
        key = (
            identity.source, identity.source_root_key, identity.account_key,
            identity.logical_limit_key, identity.observed_slot,
            identity.window_minutes,
        )
        armed[key] = (identity, str(row[6]))
    return armed


def _resolve_alert_scope(
    stats_conn: sqlite3.Connection, *, ledger_scope: str, now: dt.datetime,
    ledger_state: "dict | None", global_enabled: bool, quota_enabled: bool,
    rules, config, eligible_roots: set[str], defer_scheduled: bool = False,
) -> _axes.AlertDirtyScope:
    """Feed the five-axis kernel from stats.db and the resolved configuration."""
    armed = _armed_identities(stats_conn)
    stored = {key: fingerprint for key, (_ident, fingerprint) in armed.items()}
    resolved = {}
    for key, (identity, _fingerprint) in armed.items():
        rule = resolve_quota_rule(
            identity,
            default_actual_thresholds=config["actual_thresholds"],
            default_projected_thresholds=config["projected_thresholds"],
            rules=rules,
        )
        resolved[key] = quota_rule_fingerprint(
            identity, rule, global_enabled=global_enabled,
            quota_enabled=quota_enabled,
        )
    boundary = None
    scheduled_roots: "frozenset[str] | None" = None
    if ledger_state is not None and ledger_state["next_evaluation_at"]:
        try:
            boundary = _parse_utc(
                ledger_state["next_evaluation_at"], "next_evaluation_at_utc")
        except (TypeError, ValueError):
            boundary = None
    if ledger_state is not None:
        schedule = ledger_state["next_evaluation_by_root"]
        if schedule:
            scheduled_roots = frozenset(
                root_key for root_key, captured_at in schedule.items()
                if root_key in eligible_roots
                and now >= _parse_utc(
                    captured_at, "next_evaluation_by_root_json")
            )
    return _axes.alert_dirty_scope(
        ledger_groups=(1,) if ledger_scope == _axes.SCOPE_GROUPS else (),
        stored_fingerprints=stored,
        resolved_fingerprints=resolved,
        gate_before=(
            None if ledger_state is None else ledger_state["alerts_enabled"]),
        gate_after=bool(global_enabled and quota_enabled),
        now=now,
        next_evaluation_at=boundary,
        scheduled_roots=scheduled_roots,
        defer_scheduled=defer_scheduled,
    )


def _blocks_missing_reverse_map(stats_conn: sqlite3.Connection) -> bool:
    """True when any Codex block predates the reverse map.

    A scoped sweep matches on ``physical_group_key``, so a NULL there would
    silently escape it and the stale block would survive indefinitely. The
    epoch rebuild (1005 introduced the column; current epoch 1009) stamps every row, and
    this is the guard that turns the
    one shape it cannot reach — a block written by an older binary against an
    already-current index — into a full pass rather than a missed one.
    """
    try:
        return bool(stats_conn.execute(
            "SELECT 1 FROM quota_window_blocks "
            " WHERE source='codex' AND physical_group_key IS NULL LIMIT 1"
        ).fetchone())
    except sqlite3.Error:
        return True


def _normalized_physical_group(group: object) -> tuple[object, ...]:
    """Coerce one caller-supplied physical group into its bound-parameter form.

    ``window_minutes`` is an INTEGER column, so a string would compare unequal
    under SQLite's type affinity rules and select nothing; the reset is
    normalized to an ISO string because the predicate wraps it in
    ``unixepoch()``, which accepts both the ``Z`` and ``+00:00`` spellings the
    cache retains.
    """
    try:
        root, limit_key, slot, minutes, reset = group  # type: ignore[misc]
    except (TypeError, ValueError):
        raise ValueError(
            "physical_groups entries must be (source_root_key, "
            "logical_limit_key, observed_slot, window_minutes, "
            "canonical_reset) 5-tuples"
        ) from None
    if isinstance(minutes, bool) or not isinstance(minutes, int):
        raise ValueError("physical_groups window_minutes must be an integer")
    if isinstance(reset, dt.datetime):
        if reset.tzinfo is None or reset.utcoffset() is None:
            raise ValueError("physical_groups reset must be timezone-aware")
        reset = _utc_iso(reset)
    return (str(root), str(limit_key), str(slot), minutes, str(reset))


def _iter_shard_rows(conn, shards):
    for shard_sql, shard_params in shards:
        yield from conn.execute(shard_sql, shard_params)


def load_codex_quota_observations(
    *,
    source_root_keys: Iterable[str] | None = None,
    cache_conn: sqlite3.Connection | None = None,
    captured_at_or_after: dt.datetime | None = None,
    active_at: dt.datetime | None = None,
    max_rows: int | None = None,
    physical_signatures: dict[str, str] | None = None,
    canonical_resets_between: "tuple[dt.datetime, dt.datetime] | None" = None,
    physical_groups: "Iterable[tuple[str, str, str, int, str]] | None" = None,
) -> tuple[QuotaObservation, ...]:
    """Load only valid root-qualified S1 physical quota rows.

    Invalid/legacy partial rows remain cache evidence but are not safe enough to
    become a quota identity, so they are skipped window-by-window.  This is a
    projection reader only; it never parses rollout JSONL or mutates cache.db.

    ``cache_conn`` is caller-owned and lets a coordinated dashboard rebuild
    read quota evidence from its exact accounting generation.  Omitting it
    preserves the established independent read-only connection behavior.

    The optional time/cardinality bounds are dashboard read-model controls;
    their defaults preserve the CLI's complete-history behavior.  ``active_at``
    retains reset windows that are still active even when their last capture is
    older than ``captured_at_or_after``.  When ``physical_signatures`` is
    supplied, exact S2 signatures are accumulated from the same cursor before
    presentation bounds are applied, so coherence validation does not require
    a second unbounded observation load.

    ``canonical_resets_between`` is an INCLUSIVE ``(low, high)`` bound on the
    window's CANONICAL reset, applied in SQL.  Unlike ``captured_at_or_after``
    it is a window-IDENTITY bound, so it never fractures a window group: every
    observation of one physical window shares one canonical anchor, and the
    continuity fold below therefore still sees each retained window whole.  That
    is what lets the ingest-side spend-adoption pass bound itself to the windows
    one sync touched instead of materializing all history every hook tick.

    ``physical_groups`` (public #5) is the EXACT group filter the incremental
    projector expands a dirty ledger entry with: an iterable of
    ``(source_root_key, logical_limit_key, observed_slot, window_minutes,
    canonical reset ISO)`` tuples, each matched exactly in SQL by its own
    indexed query. Passing an empty iterable selects nothing and returns ``()``;
    ``None`` is unbounded, i.e. today's behaviour.

    It is deliberately NOT ``canonical_resets_between`` in disguise. That bound
    is an inclusive RANGE over one dimension, so across sparse dirty groups it
    loads everything between the extremes, and even a single reset instant pulls
    in every unrelated limit key and slot that happens to share it. The range
    stays for its spend-adoption caller and is not repurposed here.

    The reset member matches ``COALESCE(canonical_resets_at_utc,
    resets_at_utc)``, never the raw column. Measured on the real store, raw
    grouping yields 4,064 windows where the canonical anchor yields 608 — the
    true block count. Matching the raw column fragments every physical window
    about sevenfold, silently, and nothing fails: the pass simply loads a
    fraction of each group and materializes a wrong block rather than a stale
    one.

    The tuples are RAW stored coordinates. Interpretation (snapping a jittered
    ``window_minutes``, rewriting the limit key from ``observed_model``, folding
    the account over the population) happens below, in Python, exactly as it
    does on the unbounded path — so the caller is responsible for widening a
    dirty group to every raw spelling that snaps onto it before asking for it.
    """
    for name, value in (
        ("captured_at_or_after", captured_at_or_after), ("active_at", active_at),
    ):
        if value is not None:
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} must be timezone-aware")
            if name == "captured_at_or_after":
                captured_at_or_after = value.astimezone(UTC)
            else:
                active_at = value.astimezone(UTC)
    if canonical_resets_between is not None:
        if len(canonical_resets_between) != 2:
            raise ValueError(
                "canonical_resets_between must be a (low, high) pair")
        bounds = []
        for value in canonical_resets_between:
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(
                    "canonical_resets_between must be timezone-aware")
            bounds.append(value.astimezone(UTC))
        if bounds[0] > bounds[1]:
            raise ValueError("canonical_resets_between must be ordered")
        canonical_resets_between = (bounds[0], bounds[1])
    if max_rows is not None:
        if not isinstance(max_rows, int) or isinstance(max_rows, bool) or max_rows <= 0:
            raise ValueError("max_rows must be a positive integer or None")
    group_filter: tuple[tuple[object, ...], ...] | None = None
    if physical_groups is not None:
        # Neither combination has a coherent meaning, and both would fail
        # QUIETLY: `max_rows` appends its ORDER BY/LIMIT parameters after the
        # group disjunction's, and `physical_signatures` must be accumulated
        # from the COMPLETE root history or the certificate it stamps certifies
        # a fraction of the evidence.
        if max_rows is not None:
            raise ValueError("physical_groups cannot be combined with max_rows")
        if physical_signatures is not None:
            raise ValueError(
                "physical_groups cannot be combined with physical_signatures")
        group_filter = tuple(sorted({
            _normalized_physical_group(group) for group in physical_groups
        }))
        if not group_filter:
            return ()
    requested = None if source_root_keys is None else {str(key) for key in source_root_keys}
    owns_conn = cache_conn is None
    if owns_conn:
        try:
            conn = _cache_connection()
        except (FileNotFoundError, sqlite3.Error):
            return ()
    else:
        conn = cache_conn
    previous_row_factory = conn.row_factory
    try:
        conn.row_factory = sqlite3.Row

        def has_columns(table: str, required: set[str]) -> bool:
            columns = {
                str(row[1]) for row in conn.execute(
                    f"PRAGMA table_info({table})"
                )
            }
            return required <= columns

        has_observed_model = has_columns(
            "quota_window_snapshots", {"observed_model"},
        )
        # account_key (#341 Task 2): NULL ≡ unattributed on the read path; a
        # pre-Task-2 cache lacking the column reads every window as unattributed.
        has_account = has_columns("quota_window_snapshots", {"account_key"})
        account_expr = (
            "account_key" if has_account else "NULL AS account_key"
        )
        # canonical_resets_at_utc (#416 §4.2): resolved at ingest over the
        # complete population. A cache that has not yet gained the column (or a
        # row the 032 backfill has not reached) reads NULL, and
        # `QuotaObservation` then falls back to the raw reset — exactly today's
        # behaviour, never a failure.
        has_anchor = has_columns(
            "quota_window_snapshots", {"canonical_resets_at_utc"})
        anchor_expr = (
            "canonical_resets_at_utc" if has_anchor
            else "NULL AS canonical_resets_at_utc"
        )
        # Public #5: the model is read from THIS table alone. There used to be a
        # COALESCE onto the nearest preceding `codex_session_entries.model` at
        # or before the snapshot's byte offset, for rows written before the
        # column existed. That fallback made `quota_window_snapshots` an
        # incomplete dependency set — an accounting row arriving later could
        # move a window into a different model pool with no quota-row mutation
        # for the change ledger to record — so cache migration 039 materialized
        # exactly what it resolved and the fallback was removed. Ingest already
        # stamps the sticky model onto every quota row it emits, so nothing
        # forward-looking depended on it. A row with no determinable model stays
        # NULL and reads as unscoped rather than being fabricated.
        model_expr = (
            "quota_window_snapshots.observed_model AS observed_model"
            if has_observed_model else "NULL AS observed_model"
        )
        sql = """
            SELECT source, source_root_key, source_path, line_offset,
                   captured_at_utc, observed_slot, logical_limit_key, limit_id,
                   limit_name, window_minutes, used_percent, resets_at_utc,
                   plan_type, individual_limit_json, reached_type,
                   {model_expr}, {account_expr}, {anchor_expr}
              FROM quota_window_snapshots
             WHERE source='codex' AND source_root_key IS NOT NULL
        """.format(model_expr=model_expr, account_expr=account_expr,
                   anchor_expr=anchor_expr)
        params: list[object] = []
        if requested is not None:
            if not requested:
                return ()
            sql += " AND source_root_key IN (" + ",".join("?" for _ in requested) + ")"
            params.extend(sorted(requested))
        if canonical_resets_between is not None:
            # COALESCE, not the bare column: a pre-032 row (or one the backfill
            # never reached) carries NULL there and the reader falls back to the
            # raw reset, so the bound has to fall back with it or the row would
            # silently drop out of its own window.
            reset_expr = (
                "COALESCE(canonical_resets_at_utc, resets_at_utc)"
                if has_anchor else "resets_at_utc"
            )
            sql += (
                f" AND unixepoch({reset_expr}) >= unixepoch(?)"
                f" AND unixepoch({reset_expr}) <= unixepoch(?)"
            )
            params.extend((
                _utc_iso(canonical_resets_between[0]),
                _utc_iso(canonical_resets_between[1]),
            ))
        # When exact signatures are requested this first cursor must cover the
        # complete root history.  Otherwise apply dashboard presentation bounds
        # in SQL so only the capped evidence crosses the SQLite/Python boundary.
        sql_bounded = physical_signatures is None
        if sql_bounded and captured_at_or_after is not None:
            if active_at is not None:
                sql += (
                    " AND (unixepoch(captured_at_utc) >= unixepoch(?) "
                    "OR unixepoch(resets_at_utc) > unixepoch(?))"
                )
                params.extend((_utc_iso(captured_at_or_after), _utc_iso(active_at)))
            else:
                sql += " AND unixepoch(captured_at_utc) >= unixepoch(?)"
                params.append(_utc_iso(captured_at_or_after))
        if sql_bounded and max_rows is not None:
            if active_at is not None:
                sql += (
                    " ORDER BY (unixepoch(resets_at_utc) > unixepoch(?)) DESC, "
                    "unixepoch(captured_at_utc) DESC, unixepoch(resets_at_utc) DESC, "
                    "source_path DESC, line_offset DESC"
                )
                params.append(_utc_iso(active_at))
            else:
                sql += (
                    " ORDER BY unixepoch(captured_at_utc) DESC, "
                    "unixepoch(resets_at_utc) DESC, source_path DESC, line_offset DESC"
                )
            sql += " LIMIT ?"
            params.append(max_rows)
        else:
            sql += " ORDER BY source_root_key, captured_at_utc, resets_at_utc, source_path, line_offset"
        # ONE SHARD PER GROUP (public #5), not one disjunction over all of them.
        # Measured on a 211K-row / 608-group store: an OR over the five-member
        # equality gives up and SCANs the table (2 groups 45.7ms, 3 groups
        # 60.3ms, 8 groups 114.3ms), while the same groups as separate queries
        # each seek `idx_qws_physical_group` and cost 0.6ms apiece (2 groups
        # 1.3ms, 3 groups 1.8ms, 8 groups 4.8ms). The disjunction is therefore
        # O(all history) — exactly the property this whole change removes — and
        # the shape that keeps the pass proportional to the change is the boring
        # one. It also keeps the bound-variable count trivially inside SQLite's
        # 999 ceiling however many groups a burst dirties.
        #
        # The unbounded path stays a single shard with no extra predicate, so
        # its SQL and its plan are byte-identical to before.
        reset_group_expr = (
            "COALESCE(canonical_resets_at_utc, resets_at_utc)"
            if has_anchor else "resets_at_utc"
        )
        shards: list[tuple[str, tuple[object, ...]]] = []
        if group_filter is None:
            shards.append((sql, tuple(params)))
        else:
            head, _, tail = sql.partition(" ORDER BY ")
            order_by = " ORDER BY " + tail if tail else ""
            clause = (
                " AND (source_root_key=? AND logical_limit_key=? "
                "AND observed_slot=? AND window_minutes=? "
                f"AND unixepoch({reset_group_expr})=unixepoch(?))"
            )
            shard_sql = f"{head}{clause}{order_by}"
            for group in group_filter:
                shards.append((shard_sql, (*params, *group)))
        result: list[QuotaObservation] = []
        signature_tuples: dict[str, dict[str, list[tuple[object, ...]]]] = {}
        for row in _iter_shard_rows(conn, shards):
            required_text = (
                "source", "source_root_key", "source_path", "captured_at_utc",
                "observed_slot", "logical_limit_key", "resets_at_utc",
            )
            if any(row[name] is None or not str(row[name]).strip() for name in required_text):
                continue
            try:
                # #416 spec §4.3: snap a jittered `window_minutes` (the stray
                # `10081`) onto its native length, in BOTH the column and the
                # key member — an identity carries both, so snapping one alone
                # would still leave two identities for one physical window.
                #
                # This runs on the READ path deliberately. It is a PURE PER-ROW
                # function with no population dependence, so §4.1's argument
                # against read-time canonicalization (a bounded read picks a
                # different first member of a jitter cluster, so the dashboard
                # and CLI disagree) does not apply to it. Snapping at ingest
                # instead would change the journal quota natural key AND the
                # cache UNIQUE key, so `--rebuild` would re-append every
                # already-journalled observation under a new key and materialize
                # BOTH forms — reintroducing the fragmentation being removed.
                window_minutes = snap_codex_window_minutes(
                    int(row["window_minutes"]))
                logical_limit_key = snap_window_minutes(
                    str(row["logical_limit_key"]))
                if codex_model_scoped_quota_pool(row["observed_model"]) is not None:
                    logical_limit_key = _codex_logical_limit_key(
                        str(row["source_root_key"]), row["limit_id"],
                        str(row["observed_slot"]), window_minutes,
                        str(row["observed_model"]),
                    )
                raw_account = row["account_key"]
                account_key = (
                    str(raw_account) if raw_account not in (None, "")
                    else _lib_accounts.UNATTRIBUTED
                )
                identity = QuotaWindowIdentity(
                    source=str(row["source"]),
                    source_root_key=str(row["source_root_key"]),
                    account_key=account_key,
                    logical_limit_key=logical_limit_key,
                    observed_slot=str(row["observed_slot"]),
                    window_minutes=window_minutes,
                    limit_id=row["limit_id"],
                    limit_name=row["limit_name"],
                )
                observation = QuotaObservation(
                    identity=identity,
                    captured_at=_parse_utc(str(row["captured_at_utc"]), "captured_at_utc"),
                    used_percent=float(row["used_percent"]),
                    resets_at=_parse_utc(str(row["resets_at_utc"]), "resets_at_utc"),
                    source_path=str(row["source_path"]),
                    line_offset=int(row["line_offset"]),
                    plan_type=row["plan_type"],
                    individual_limit_json=row["individual_limit_json"],
                    reached_type=row["reached_type"],
                    canonical_resets_at=(
                        None if row["canonical_resets_at_utc"] in (None, "")
                        else _parse_utc(str(row["canonical_resets_at_utc"]),
                                        "canonical_resets_at_utc")
                    ),
                )
            except (TypeError, ValueError, OverflowError):
                # Physical retention is intentionally more permissive than the
                # provider-neutral identity contract.  One malformed window
                # must not suppress unrelated valid windows or accounting.
                continue
            if physical_signatures is not None:
                # Accumulated PER LOADING UNIT, not per root: the root signature
                # is a digest over its sorted (unit key, unit digest) pairs, so
                # a flat per-root list would produce a different value from the
                # one the projector composes and stores — and the certificate
                # compares them with `==`.
                signature_tuples.setdefault(
                    identity.source_root_key, {},
                ).setdefault(
                    _observation_unit_text(observation), [],
                ).append(_signature_tuple(observation))
            if (
                captured_at_or_after is not None
                and observation.captured_at < captured_at_or_after
                and (active_at is None or observation.resets_at <= active_at)
            ):
                continue
            result.append(observation)
        if group_filter is not None and len(shards) > 1:
            # Each shard is ordered internally; their union is not. Restore the
            # unbounded path's total order so a bounded load is byte-comparable
            # with a full one.
            result.sort(key=lambda observation: (
                observation.identity.source_root_key,
                observation.captured_at,
                observation.resets_at,
                observation.source_path,
                observation.line_offset,
            ))
        # Window-account continuity fold (#341 spec §2): adopt unidentified
        # observations into a same-physical-window identified account (exactly
        # one). Physical signatures above are account-independent (computed from
        # the physical tuple), so folding after them is order-invariant; the
        # recursion path below re-fetches + re-folds. Idempotent for an
        # already-identified set — a single-account cache is a no-op.
        result = list(adopt_unidentified_observations(result))
        if physical_signatures is not None:
            physical_signatures.clear()
            roots = requested if requested is not None else set(signature_tuples)
            for root_key in roots:
                physical_signatures[root_key] = _ledger.compose_root_signature(
                    (unit, _ledger.group_digest(tuples))
                    for unit, tuples in signature_tuples.get(
                        root_key, {}).items()
                )
            if captured_at_or_after is not None or max_rows is not None:
                return load_codex_quota_observations(
                    source_root_keys=requested,
                    cache_conn=conn,
                    captured_at_or_after=captured_at_or_after,
                    active_at=active_at,
                    max_rows=max_rows,
                    canonical_resets_between=canonical_resets_between,
                )
        if max_rows is not None and len(result) > max_rows:
            result = sorted(
                result,
                key=lambda observation: (
                    1 if active_at is not None and observation.resets_at > active_at else 0,
                    observation.captured_at,
                    observation.resets_at,
                    observation.source_path,
                    observation.line_offset,
                ),
                reverse=True,
            )[:max_rows]
            result.sort(key=lambda observation: (
                observation.identity.source_root_key,
                observation.captured_at,
                observation.resets_at,
                observation.source_path,
                observation.line_offset,
            ))
        return tuple(result)
    finally:
        if owns_conn:
            conn.close()
        else:
            conn.row_factory = previous_row_factory


def _historic_root_keys(conn: sqlite3.Connection) -> set[str]:
    roots: set[str] = set()
    for table in (
        "quota_window_blocks", "quota_percent_milestones",
        "quota_threshold_events", "quota_projection_state",
    ):
        try:
            roots.update(str(row[0]) for row in conn.execute(
                f"SELECT DISTINCT source_root_key FROM {table}"
            ) if row[0] is not None)
        except sqlite3.OperationalError:
            continue
    return roots


def _signature_tuple(observation: QuotaObservation) -> tuple[object, ...]:
    """The per-observation tuple both the group digest and the old whole-root
    signature are taken over. Unchanged, so a group's digest covers exactly the
    rows it owns."""
    return (
        observation.identity.source_root_key,
        observation.identity.logical_limit_key,
        _utc_iso(observation.captured_at),
        observation.source_path,
        observation.line_offset,
        observation.used_percent,
        _utc_iso(observation.resets_at),
    )


def _observation_unit_text(observation: QuotaObservation) -> str:
    """The serialized loading unit one observation belongs to.

    The unit is the reverse map: a ledger entry names it from RAW coordinates
    while a block stamps it from the INTERPRETED identity, and the two must
    agree or the scoped sweep looks for a key nothing wrote.
    """
    identity = observation.identity
    anchor = observation.canonical_resets_at or observation.resets_at
    return _ledger.physical_group_key_text(_ledger.loading_unit_from_identity(
        source_root_key=identity.source_root_key,
        logical_limit_key=identity.logical_limit_key,
        observed_slot=identity.observed_slot,
        window_minutes=identity.window_minutes,
        canonical_reset_iso=_utc_iso(anchor),
    ))


def _block_unit_text(block: QuotaBlock) -> str:
    identity = block.identity
    return _ledger.physical_group_key_text(_ledger.loading_unit_from_identity(
        source_root_key=identity.source_root_key,
        logical_limit_key=identity.logical_limit_key,
        observed_slot=identity.observed_slot,
        window_minutes=identity.window_minutes,
        # `QuotaBlock.resets_at` IS the canonical anchor (#416 §4.1).
        canonical_reset_iso=_utc_iso(block.resets_at),
    ))


def _group_digests(
    observations: Iterable[QuotaObservation],
) -> dict[str, str]:
    """Digest every loading unit present in ``observations``.

    Only the units the pass LOADED appear, which is exactly right: a bounded
    pass re-derives the dirty units' digests and leaves every clean unit's
    stored value alone, and the root signature is then composed from the union.
    """
    by_unit: dict[str, list[tuple[object, ...]]] = {}
    for observation in observations:
        by_unit.setdefault(_observation_unit_text(observation), []).append(
            _signature_tuple(observation))
    return {
        unit: _ledger.group_digest(tuples) for unit, tuples in by_unit.items()
    }


def _signature(
    observations: Iterable[QuotaObservation], source_root_key: str,
) -> str:
    """One root's physical signature, computed straight from observations.

    The whole-history spelling of the composition: digest each loading unit the
    root's observations fall into, then compose the root value from those pairs.
    For a complete observation set this equals what the projector composes from
    the STORED per-group digests, which is the property that lets a bounded pass
    and a whole-history pass agree on the certificate.

    Retained (rather than folded into the projector) because it is the honest
    way for a caller holding observations — a test, a coherence probe — to ask
    "what should this root's signature be", without going through the blocks.
    """
    digests = _group_digests(
        observation for observation in observations
        if observation.identity.source_root_key == source_root_key
    )
    return _ledger.compose_root_signature(digests.items())


def _root_group_pairs(
    conn: sqlite3.Connection, source_root_key: str,
) -> list[tuple[str, str]]:
    """The live ``(group key, group digest)`` pairs of one root.

    Read from the blocks rather than from the pass's observations, so a bounded
    pass composes over the same complete set a whole-history pass does. Orphaned
    blocks are excluded, which is what makes a swept-to-nothing group drop out
    of the root's signature with no separate bookkeeping.
    """
    return [
        (str(row[0]), str(row[1]))
        for row in conn.execute(
            "SELECT DISTINCT physical_group_key, physical_group_digest "
            "  FROM quota_window_blocks "
            " WHERE source='codex' AND source_root_key=? "
            "   AND orphaned_at IS NULL AND physical_group_key IS NOT NULL "
            "   AND physical_group_digest IS NOT NULL",
            (source_root_key,),
        )
    ]


def _root_accounts(
    conn: sqlite3.Connection, source_root_key: str,
) -> set[str]:
    """The account partitions one root currently projects.

    Derived from the live blocks, not from the pass's observations (#341 +
    public #5 Task 9): under a bounded pass an account whose only evidence lies
    in a CLEAN window contributes no observation, so an observation-derived set
    would drop it and its projection-state row would be left stale. The blocks
    are the materialized truth, and a retired partition loses its blocks to the
    sweep, so this both keeps and retires the right rows.
    """
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT DISTINCT account_key FROM quota_window_blocks "
            " WHERE source='codex' AND source_root_key=? "
            "   AND orphaned_at IS NULL",
            (source_root_key,),
        )
        if row[0] is not None
    }


def _block_params(
    block: QuotaBlock, generation: str, unit: str, digest: str,
) -> tuple[object, ...]:
    latest = block.observations[-1]
    identity = block.identity
    return (
        identity.source, identity.source_root_key, identity.logical_limit_key,
        identity.observed_slot, identity.window_minutes, identity.limit_id,
        identity.limit_name, _utc_iso(block.resets_at), _utc_iso(block.nominal_start_at),
        _utc_iso(block.first_observed_at), _utc_iso(block.last_observed_at),
        block.first_percent, block.current_percent, latest.source_path,
        latest.line_offset, generation, identity.account_key, unit, digest,
    )


# account_key (#341): part of the block/milestone identity — the ON CONFLICT
# target includes it so two accounts sharing one physical window are DISTINCT
# rows (never-combine), and each account's row upserts independently.
_BLOCK_UPSERT = """
    INSERT INTO quota_window_blocks
       (source, source_root_key, logical_limit_key, observed_slot,
        window_minutes, limit_id, limit_name, resets_at_utc, nominal_start_at_utc,
        first_observed_at_utc, last_observed_at_utc, first_percent, current_percent,
        last_source_path, last_line_offset, generation, orphaned_at, account_key,
        physical_group_key, physical_group_digest)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,?,?)
    ON CONFLICT(source, source_root_key, account_key, logical_limit_key,
                observed_slot, window_minutes, resets_at_utc) DO UPDATE SET
      limit_id=excluded.limit_id, limit_name=excluded.limit_name,
      nominal_start_at_utc=excluded.nominal_start_at_utc,
      first_observed_at_utc=excluded.first_observed_at_utc,
      last_observed_at_utc=excluded.last_observed_at_utc,
      first_percent=excluded.first_percent, current_percent=excluded.current_percent,
      last_source_path=excluded.last_source_path, last_line_offset=excluded.last_line_offset,
      generation=excluded.generation, orphaned_at=NULL,
      physical_group_key=excluded.physical_group_key,
      physical_group_digest=excluded.physical_group_digest
"""


_MILESTONE_UPSERT = """
    INSERT INTO quota_percent_milestones
       (source, source_root_key, logical_limit_key, observed_slot, window_minutes,
        resets_at_utc, percent_threshold, captured_at_utc, source_path,
        line_offset, high_water_percent, generation, orphaned_at, account_key)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,NULL,?)
    ON CONFLICT(source, source_root_key, account_key, logical_limit_key,
                observed_slot, window_minutes, resets_at_utc, percent_threshold)
    DO UPDATE SET
      captured_at_utc=excluded.captured_at_utc, source_path=excluded.source_path,
      line_offset=excluded.line_offset, high_water_percent=excluded.high_water_percent,
      generation=excluded.generation, orphaned_at=NULL
"""


def _milestone_params(
    block: QuotaBlock, milestone: QuotaPercentMilestone, generation: str,
) -> tuple[object, ...]:
    identity = block.identity
    return (
        identity.source, identity.source_root_key, identity.logical_limit_key,
        identity.observed_slot, identity.window_minutes, _utc_iso(block.resets_at),
        milestone.percent, _utc_iso(milestone.captured_at), milestone.observation.source_path,
        milestone.observation.line_offset, milestone.percent, generation,
        identity.account_key,
    )


#: Restricts a sweep statement to blocks whose loading unit is dirty. Chunked by
#: the caller so the ``IN`` list stays inside SQLite's variable budget.
_UNIT_SCOPE_BLOCKS = " AND physical_group_key IN ({placeholders})"

#: The same restriction for the child tables, expressed through the block that
#: owns the row. Blocks are ORPHANED, never deleted, so the join still resolves
#: for a window whose members all disappeared — which is precisely the case the
#: sweep exists to catch.
_UNIT_SCOPE_VIA_BLOCK = """
 AND EXISTS (SELECT 1 FROM quota_window_blocks AS scope
              WHERE scope.source={alias}.source
                AND scope.source_root_key={alias}.source_root_key
                AND scope.account_key={alias}.account_key
                AND scope.logical_limit_key={alias}.logical_limit_key
                AND scope.observed_slot={alias}.observed_slot
                AND scope.window_minutes={alias}.window_minutes
                AND scope.resets_at_utc={alias}.resets_at_utc
                AND scope.physical_group_key IN ({placeholders}))
"""


def _chunk(values: Sequence[str], size: int):
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _orphan_unseen(
    conn: sqlite3.Connection, roots: set[str], generation: str, now_iso: str,
    *, units: "frozenset[str] | None" = None,
) -> tuple[int, int]:
    """Orphan whatever this generation did not re-stamp.

    ``units`` is public #5's scoping (spec §2). ``None`` keeps the whole-root
    sweep, which is what the full path, a rebuild and an interpretation-version
    bump all want. A non-``None`` set runs the SAME SQL over a bounded root set
    instead of a different sweep, which is what preserves the child classes a
    block-only set difference would miss: a milestone threshold that disappears
    inside a still-present window, an account-specific block variant that
    vanishes while the physical window remains, and the account-qualified
    ``quota_threshold_events`` orphan/unorphan reconciliation.

    Two cases a scoped sweep structurally cannot see are handled on the full
    path only, which runs on an interpretation bump, on rebuild and on
    ``force_full``: blocks whose physical group is absent from the cache
    entirely, and milestones on historic roots no longer active.
    ``_historic_root_keys`` continues to feed that path.
    """
    if units is not None:
        return _orphan_unseen_scoped(conn, units, generation, now_iso)
    if not roots:
        return (0, 0)
    placeholders = ",".join("?" for _ in roots)
    args = (now_iso, *sorted(roots), generation)
    blocks = conn.execute(
        "UPDATE quota_window_blocks SET orphaned_at=COALESCE(orphaned_at, ?) "
        "WHERE source='codex' AND source_root_key IN (" + placeholders + ") "
        "AND generation<>?", args,
    ).rowcount
    milestones = conn.execute(
        "UPDATE quota_percent_milestones SET orphaned_at=COALESCE(orphaned_at, ?) "
        "WHERE source='codex' AND source_root_key IN (" + placeholders + ") "
        "AND generation<>?", args,
    ).rowcount
    # Threshold events are terminal evidence and are never recreated here.
    # Their orphan marker tracks whether the stable source block is present in
    # this completed generation, so a cache rebuild that restores the exact
    # block clears a transient prune marker without creating a new terminal
    # claim.
    # account_key (#341): join on the account too so account A's terminal event
    # is never un-orphaned by account B's block sharing the physical window.
    event_sql = f"""UPDATE quota_threshold_events AS events
              SET orphaned_at=CASE WHEN EXISTS (
                  SELECT 1 FROM quota_window_blocks AS blocks
                   WHERE blocks.source=events.source
                     AND blocks.source_root_key=events.source_root_key
                     AND blocks.account_key=events.account_key
                     AND blocks.logical_limit_key=events.logical_limit_key
                     AND blocks.observed_slot=events.observed_slot
                     AND blocks.window_minutes=events.window_minutes
                     AND blocks.resets_at_utc=events.resets_at_utc
                     AND blocks.generation=?
              ) THEN NULL ELSE COALESCE(events.orphaned_at, ?) END
            WHERE events.source='codex'
              AND events.source_root_key IN ({placeholders})
        """
    conn.execute(
        event_sql,
        (generation, now_iso, *sorted(roots)),
    )
    return (int(blocks), int(milestones))


def _orphan_unseen_scoped(
    conn: sqlite3.Connection, units: "frozenset[str]", generation: str,
    now_iso: str,
) -> tuple[int, int]:
    """The whole-root sweep, bounded to a set of dirty loading units.

    Same three statements, same semantics, one extra predicate each. The child
    tables are scoped THROUGH the block that owns them rather than by a column
    of their own: the block's identity columns are the milestone's and the
    event's, blocks are orphaned rather than deleted, so the join keeps
    resolving for a window whose members all disappeared.
    """
    if not units:
        return (0, 0)
    keys = sorted(units)
    blocks = 0
    milestones = 0
    for chunk in _chunk(keys, _SWEEP_KEY_CHUNK):
        placeholders = ",".join("?" for _ in chunk)
        blocks += conn.execute(
            "UPDATE quota_window_blocks SET orphaned_at=COALESCE(orphaned_at, ?) "
            "WHERE source='codex' AND generation<>?"
            + _UNIT_SCOPE_BLOCKS.format(placeholders=placeholders),
            (now_iso, generation, *chunk),
        ).rowcount
        milestones += conn.execute(
            "UPDATE quota_percent_milestones AS milestones "
            "SET orphaned_at=COALESCE(orphaned_at, ?) "
            "WHERE milestones.source='codex' AND milestones.generation<>?"
            + _UNIT_SCOPE_VIA_BLOCK.format(
                alias="milestones", placeholders=placeholders),
            (now_iso, generation, *chunk),
        ).rowcount
        # Terminal evidence: never recreated, only marked. Same CASE as the
        # whole-root sweep — the account-qualified join included — restricted to
        # events whose owning block sits in a dirty unit.
        conn.execute(
            "UPDATE quota_threshold_events AS events "
            "   SET orphaned_at=CASE WHEN EXISTS ("
            "       SELECT 1 FROM quota_window_blocks AS blocks "
            "        WHERE blocks.source=events.source "
            "          AND blocks.source_root_key=events.source_root_key "
            "          AND blocks.account_key=events.account_key "
            "          AND blocks.logical_limit_key=events.logical_limit_key "
            "          AND blocks.observed_slot=events.observed_slot "
            "          AND blocks.window_minutes=events.window_minutes "
            "          AND blocks.resets_at_utc=events.resets_at_utc "
            "          AND blocks.generation=?"
            "   ) THEN NULL ELSE COALESCE(events.orphaned_at, ?) END "
            " WHERE events.source='codex'"
            + _UNIT_SCOPE_VIA_BLOCK.format(
                alias="events", placeholders=placeholders),
            (generation, now_iso, *chunk),
        )
    return (int(blocks), int(milestones))


def _quota_alert_config() -> tuple[bool, bool, tuple[QuotaRule, ...], dict]:
    """Resolve global + quota gates and exact JSON-shaped overrides once."""
    c = _cctally()
    config = c.load_config()
    alerts = _cctally_core._get_alerts_config(config)
    quota = c._get_quota_alerts_config(config)
    rules = tuple(
        QuotaRule(
            source=rule["source"],
            source_root_key=rule["source_root_key"],
            logical_limit_key=rule["logical_limit_key"],
            actual_thresholds=tuple(rule["actual_thresholds"]),
            projected_thresholds=tuple(rule["projected_thresholds"]),
        )
        for rule in quota["rules"]
    )
    return bool(alerts["enabled"]), bool(quota["enabled"]), rules, quota


def _arming_row(conn: sqlite3.Connection, identity: QuotaWindowIdentity) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT rule_fingerprint, activated_at_utc FROM quota_alert_arming
             WHERE source=? AND source_root_key=? AND account_key=?
               AND logical_limit_key=? AND observed_slot=? AND window_minutes=?""",
        (
            identity.source, identity.source_root_key, identity.account_key,
            identity.logical_limit_key, identity.observed_slot, identity.window_minutes,
        ),
    ).fetchone()


def _activate_quota_rule(
    conn: sqlite3.Connection, identity: QuotaWindowIdentity, fingerprint: str, now_iso: str,
    *, journal_emit=None,
) -> tuple[bool, dt.datetime]:
    """Persist one identity's resolved rule boundary, returning (changed, at).

    Task 7 Item 5: on a genuine activation (a new/changed boundary) the arming
    state is journaled — its ``activated_at_utc`` is a forward-only alert
    boundary that must survive a stats.db rebuild so the reconcile honors it
    (no historical re-fires, spec §5.3 "state"). ``journal_emit`` (set only on
    the LIVE ingest-cycle path; ``None`` for a rebuild re-materialization, which
    must not append) appends the ``quota_alert_arming`` evt. The row is still
    written directly here; the evt is the additional durable record, and its
    fold applier's natural-key upsert converges with this write on replay."""
    row = _arming_row(conn, identity)
    if row is not None and row["rule_fingerprint"] == fingerprint:
        return False, _parse_utc(str(row["activated_at_utc"]), "activated_at_utc")
    conn.execute(
        """INSERT INTO quota_alert_arming
               (source, source_root_key, logical_limit_key, observed_slot,
                window_minutes, rule_fingerprint, activated_at_utc, account_key)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(source, source_root_key, account_key, logical_limit_key,
                           observed_slot, window_minutes) DO UPDATE SET
                 rule_fingerprint=excluded.rule_fingerprint,
                 activated_at_utc=excluded.activated_at_utc""",
        (
            identity.source, identity.source_root_key, identity.logical_limit_key,
            identity.observed_slot, identity.window_minutes, fingerprint, now_iso,
            identity.account_key,
        ),
    )
    if journal_emit is not None:
        journal_emit(identity, fingerprint, now_iso)
    return True, _parse_utc(now_iso, "activated_at_utc")


def _insert_quota_terminal_event(
    conn: sqlite3.Connection,
    *, identity: QuotaWindowIdentity, resets_at: dt.datetime,
    threshold: int, kind: str, qualifying_percent: float | None,
    projected_percent: float | None, disposition: str, now_iso: str,
    journal_emit=None,
) -> bool:
    """Claim one durable threshold lifecycle row; unique-key races converge.

    #416 spec §7.2 (review F13): a claimed row is TERMINAL alert evidence and
    must survive a stats.db rebuild, so a genuine claim is journaled through
    ``journal_emit``. Without it a rebuild could not recreate an ``alerted`` row
    at all — `rematerialize_quota_projection_for_rebuild` runs with no
    alert-eligible roots — and the crossing would be free to fire again.

    ``journal_emit`` is set only on the LIVE ingest-cycle path and is ``None``
    for the rebuild re-materialization, which must never append. It fires only
    when ``rowcount == 1``, i.e. on a genuinely NEW claim: re-emitting on a
    converged race would append a duplicate record for a fact already journaled.
    Same shape as ``_activate_quota_rule``'s arming emitter.
    """
    alerted_at = now_iso if disposition == "alerted" else None
    suppressed_at = now_iso if disposition == "suppressed_backfill" else None
    cur = conn.execute(
        """INSERT OR IGNORE INTO quota_threshold_events
               (source, source_root_key, logical_limit_key, observed_slot,
                window_minutes, resets_at_utc, threshold, qualifying_kind,
                qualifying_percent, projected_percent, severity, created_at_utc,
                disposition, alerted_at, suppressed_at, account_key)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            identity.source, identity.source_root_key, identity.logical_limit_key,
            identity.observed_slot, identity.window_minutes, _utc_iso(resets_at),
            threshold, kind, qualifying_percent, projected_percent,
            _cctally().severity_for(threshold), now_iso, disposition,
            alerted_at, suppressed_at, identity.account_key,
        ),
    )
    claimed = cur.rowcount == 1
    if claimed and journal_emit is not None:
        journal_emit({
            "source": identity.source,
            "source_root_key": identity.source_root_key,
            "account_key": identity.account_key,
            "logical_limit_key": identity.logical_limit_key,
            "observed_slot": identity.observed_slot,
            "window_minutes": identity.window_minutes,
            "resets_at_utc": _utc_iso(resets_at),
            "threshold": threshold,
            "qualifying_kind": kind,
            "qualifying_percent": qualifying_percent,
            "projected_percent": projected_percent,
            "severity": _cctally().severity_for(threshold),
            "created_at_utc": now_iso,
            "disposition": disposition,
            "alerted_at": alerted_at,
            "suppressed_at": suppressed_at,
        })
    return claimed


def _block_observations_at_or_before(
    block: QuotaBlock, at: dt.datetime,
) -> tuple[QuotaObservation, ...]:
    return tuple(point for point in block.observations if point.captured_at <= at)


def _quota_projection_for_block(
    history: QuotaHistory, block: QuotaBlock, now: dt.datetime,
) -> float | None:
    """Return a fresh projected percent only for the current native block."""
    forecast = forecast_quota(history.physical_observations, now)
    if forecast.status != "ok" or forecast.resets_at != block.resets_at:
        return None
    return forecast.projected_percent


def _quota_alert_payload(
    *, identity: QuotaWindowIdentity, resets_at: dt.datetime, threshold: int,
    kind: str, now_iso: str, qualifying_percent: float | None,
    projected_percent: float | None,
) -> dict:
    return _cctally()._build_alert_payload_quota(
        source=identity.source, source_root_key=identity.source_root_key,
        logical_limit_key=identity.logical_limit_key,
        observed_slot=identity.observed_slot, window_minutes=identity.window_minutes,
        resets_at_utc=_utc_iso(resets_at), threshold=threshold, kind=kind,
        crossed_at_utc=now_iso, qualifying_percent=qualifying_percent,
        projected_percent=projected_percent,
        account_key=identity.account_key,
    )


def _evaluate_quota_alerts(
    conn: sqlite3.Connection,
    *, observations: tuple[QuotaObservation, ...], alert_eligible_roots: set[str],
    now: dt.datetime, now_iso: str, journal_emit=None, journal_disarm=None,
    journal_terminal=None,
) -> list[dict]:
    """Arm or claim quota alerts within the caller's stats transaction.

    A fresh fingerprint writes only terminal backfill suppressions. Later
    eligible observations can claim an alerted row. No non-terminal state is
    stored: the arming boundary plus unique terminal event key is sufficient.
    """
    global_enabled, quota_enabled, rules, config = _quota_alert_config()
    # Disabled delivery is entirely inert: it must not leave an arming
    # boundary that could turn disabled-period evidence into a later alert.
    # This gate deliberately precedes the lifecycle-eligibility fast path:
    # read-only report reconciles carry no eligible roots, but they must still
    # durably disarm state after either user-facing switch is turned off.
    if not (global_enabled and quota_enabled):
        # Scratch rebuild/rederive replay has no alert sink and must preserve
        # the provider-owned state selected from the journal verbatim. Only the
        # live single-flight reconcile supplies the durable disarm emitter.
        if journal_disarm is None:
            return []
        rows = conn.execute(
            """SELECT source, source_root_key, account_key,
                       logical_limit_key, observed_slot, window_minutes
                  FROM quota_alert_arming
                 WHERE source='codex'"""
        ).fetchall()
        conn.execute(
            "DELETE FROM quota_alert_arming WHERE source='codex'"
        )
        if journal_disarm is not None:
            for row in rows:
                journal_disarm(
                    QuotaWindowIdentity(
                        source=str(row["source"]),
                        source_root_key=str(row["source_root_key"]),
                        account_key=str(row["account_key"]),
                        logical_limit_key=str(row["logical_limit_key"]),
                        observed_slot=str(row["observed_slot"]),
                        window_minutes=int(row["window_minutes"]),
                    ),
                    now_iso,
                )
        return []
    if not alert_eligible_roots:
        return []
    histories = build_history(observations)
    queued: list[dict] = []
    for history in histories:
        identity = history.identity
        if identity.source_root_key not in alert_eligible_roots:
            continue
        resolved = resolve_quota_rule(
            identity,
            default_actual_thresholds=config["actual_thresholds"],
            default_projected_thresholds=config["projected_thresholds"],
            rules=rules,
        )
        fingerprint = quota_rule_fingerprint(
            identity, resolved, global_enabled=global_enabled,
            quota_enabled=quota_enabled,
        )
        changed, activated_at = _activate_quota_rule(
            conn, identity, fingerprint, now_iso, journal_emit=journal_emit)

        # Future evidence is never a threshold qualifier (including a first
        # activation backfill). A later well-clocked observation creates the
        # appropriate normal activation/claim path.
        freshness = quota_freshness(history.physical_observations, now)
        if freshness.state == "future":
            continue
        blocks = tuple(
            block for block in build_blocks(history.observations)
            if block.identity == identity
        )
        for block in blocks:
            present = _block_observations_at_or_before(block, now)
            if not present:
                continue
            if changed:
                actual_percent = max(point.used_percent for point in present)
                projected_percent = _quota_projection_for_block(history, block, now)
                for decision in quota_threshold_decisions(
                    current_percent=actual_percent,
                    projected_percent=projected_percent,
                    actual_thresholds=resolved.actual_thresholds,
                    projected_thresholds=resolved.projected_thresholds,
                ):
                    _insert_quota_terminal_event(
                        conn, identity=identity, resets_at=block.resets_at,
                        threshold=decision.threshold, kind=decision.kind,
                        qualifying_percent=(actual_percent if decision.kind == "actual" else None),
                        projected_percent=(
                            projected_percent if decision.kind == "projected" else None
                        ),
                        disposition="suppressed_backfill", now_iso=now_iso,
                        journal_emit=journal_terminal,
                    )
                continue
            later = tuple(point for point in present if point.captured_at > activated_at)
            if not later:
                continue
            actual_percent = max(point.used_percent for point in later)
            projected_percent = None
            baseline = select_baseline(history.observations, now)
            if (
                freshness.state != "stale" and baseline is not None
                # CANONICAL on both sides (#416 §4.1): `block.resets_at` is
                # now the anchor, so comparing it to the baseline's RAW reset
                # would never match for a jittered window.
                and baseline.canonical_resets_at == block.resets_at
                and baseline.captured_at > activated_at
            ):
                projected_percent = _quota_projection_for_block(history, block, now)
            for decision in quota_threshold_decisions(
                current_percent=actual_percent,
                projected_percent=projected_percent,
                actual_thresholds=resolved.actual_thresholds,
                projected_thresholds=resolved.projected_thresholds,
            ):
                qualifying = actual_percent if decision.kind == "actual" else None
                projected = projected_percent if decision.kind == "projected" else None
                if _insert_quota_terminal_event(
                    conn, identity=identity, resets_at=block.resets_at,
                    threshold=decision.threshold, kind=decision.kind,
                    qualifying_percent=qualifying, projected_percent=projected,
                    disposition="alerted", now_iso=now_iso,
                    journal_emit=journal_terminal,
                ):
                    queued.append(_quota_alert_payload(
                        identity=identity, resets_at=block.resets_at,
                        threshold=decision.threshold, kind=decision.kind,
                        now_iso=now_iso, qualifying_percent=qualifying,
                        projected_percent=projected,
                    ))
    return queued


def _reanchor_terminal_events_sql(
    key_slots: int, minute_slots: int, reset_slots: int,
) -> str:
    # `UPDATE OR IGNORE`, not a plain UPDATE: if this identity already carries an
    # anchored row at the same threshold, moving the jittered twin onto it would
    # violate the UNIQUE key. OR IGNORE SKIPS that move (it does not delete the
    # twin), which is the right trade: the anchored row survives with its
    # evidence intact and is the one every future evaluation keys against, so
    # the re-fire is prevented either way — while deleting historical alert
    # evidence to tidy the display would be irreversible and is not this pass's
    # mandate. A plain UPDATE would raise and abort the whole projection
    # transaction.
    keys = ",".join(f":key{i}" for i in range(key_slots))
    minutes = ",".join(f":min{i}" for i in range(minute_slots))
    member_epochs = ",".join(f":reset{i}" for i in range(reset_slots))
    member_clause = (
        f" OR unixepoch(resets_at_utc) IN ({member_epochs})"
        if member_epochs else ""
    )
    return (
        "UPDATE OR IGNORE quota_threshold_events "
        "   SET resets_at_utc = :anchor, "
        "       logical_limit_key = :limit_key, "
        "       window_minutes = :minutes "
        " WHERE source = :source AND source_root_key = :root "
        "   AND account_key = :account "
        f"   AND logical_limit_key IN ({keys}) "
        f"   AND observed_slot = :slot AND window_minutes IN ({minutes}) "
        "   AND (resets_at_utc <> :anchor OR logical_limit_key <> :limit_key "
        "        OR window_minutes <> :minutes) "
        "   AND ("
        "       abs(unixepoch(resets_at_utc) - unixepoch(:anchor)) <= :tolerance"
        f"{member_clause})"
    )


def _reanchor_terminal_events(conn: sqlite3.Connection, block) -> None:
    """Move terminal alert evidence for one window onto its canonical identity.

    #416 spec §4.1 made `QuotaBlock.resets_at` the tolerance-anchored reset, but
    `quota_threshold_events.resets_at_utc` is part of that table's UNIQUE key and
    existing rows were written under whichever RAW spelling the block carried at
    the time. Without this, the very next reconcile after the canonicalization
    ships would look up an already-alerted threshold under the anchor, find
    nothing, claim it again, and DISPATCH A DUPLICATE ALERT for a crossing the
    user was already told about. The window would also appear twice in the
    dashboard's alert list.

    The reset is not the only axis §4.3 moved. `window_minutes` is snapped too,
    and it lives in BOTH the logical limit key and a column of its own — so a
    terminal row written before the snap under the stray `10081` spelling is not
    reachable by an identity match on the snapped value at all. Matching only the
    snapped spelling would leave exactly the rows the canonicalization merged
    stranded under their old identity, which is the same duplicate-alert hazard
    one axis over. The match therefore enumerates the RAW spellings that snap
    onto this identity (`codex_snap_equivalent_limit_keys` /
    `codex_snap_equivalent_window_minutes`) and the UPDATE re-keys them, not just
    re-anchors them.

    Runs inside the caller's transaction, on both the live leg and the rebuild
    re-materialization (they share this body), and is idempotent: a row already
    on the canonical identity is excluded by the three-way `<>` guard.

    The reset match accepts either the original 600-second anchor neighbourhood
    or an exact raw reset retained by this block. The latter is required by
    #425's transitive component closure: an endpoint can be farther than 600s
    from the first-sight anchor while still joining it through retained bridge
    observations. Exact membership keeps the widened reach evidence-bound; a
    genuinely different cycle is never inferred from distance alone. The length
    axis remains bounded by its ±1 minute snap.
    """
    identity = block.identity
    keys = codex_snap_equivalent_limit_keys(identity.logical_limit_key)
    minutes = codex_snap_equivalent_window_minutes(identity.window_minutes)
    membership_evidence = (
        block.physical_observations or block.observations
    )
    reset_epochs = sorted({
        int(observation.resets_at.timestamp())
        for observation in membership_evidence
    })
    params: dict[str, object] = {
        "anchor": _utc_iso(block.resets_at),
        "source": identity.source,
        "root": identity.source_root_key,
        "account": identity.account_key,
        "limit_key": identity.logical_limit_key,
        "slot": identity.observed_slot,
        "minutes": identity.window_minutes,
        "tolerance": CODEX_RESET_ANCHOR_TOLERANCE_SECONDS,
    }
    params.update({f"key{i}": value for i, value in enumerate(keys)})
    params.update({f"min{i}": value for i, value in enumerate(minutes)})
    params.update({
        f"reset{i}": value for i, value in enumerate(reset_epochs)})
    conn.execute(
        _reanchor_terminal_events_sql(
            len(keys), len(minutes), len(reset_epochs)),
        params,
    )


def _apply_quota_projection_rows(
    conn, *, observations, active_roots, now, now_iso,
    sink, alert_eligible_roots, journal_emit=None, journal_disarm=None,
    journal_terminal=None, holder=None, dirty_units=None,
    ledger_watermark=None, alerts_enabled=None,
    stored_next_evaluation_at=None, stored_last_full_pass_at=None,
    stored_alerts_enabled=None, stored_next_evaluation_by_root=None,
    consume_alert_axes=True, reconcile_roots=None,
    schedule_evaluated_roots=frozenset(),
    prune_inactive_schedule=False,
):
    """Transaction-neutral quota projection apply (spec §5.3 "projection").

    Materializes the interpreted ``quota_*`` rows on ``conn`` (no ``BEGIN`` /
    ``commit`` — the caller owns the transaction), queues alerts into ``sink``
    (``None`` → none), and journals arming state changes via ``journal_emit``
    (``None`` → none). ONE shared body drives both the live ingest ``codex_apply``
    leg (``sink`` = the cycle's pending-alerts, ``journal_emit`` = the arming evt
    emitter) AND the rebuild re-materialization pass (``sink=None``,
    ``journal_emit=None``, ``alert_eligible_roots`` empty) so the two never drift.
    ``holder`` (optional) captures the certificate signatures + result for the
    live caller; rebuild passes ``None``.

    ``dirty_units`` (public #5) is the BOUNDED pass: a set of serialized loading
    units whose complete current membership ``observations`` carries.
    ``reconcile_roots`` distinguishes a complete root-scoped pass from the
    genuine whole-history ``None``/``None`` shape. It changes exactly two
    things: the sweep is scoped to those units, and the root signature is
    composed from the stored per-group digests rather than recomputed from
    scratch. Everything else runs identically, which is what keeps the two paths
    from drifting.

    ``consume_alert_axes`` says whether this pass may advance the global
    non-dirtiness axes or must carry the stored values through untouched,
    the way ``last_full_pass_at`` is carried through by a bounded pass. False
    means "this pass did not do the work those axes exist to trigger", and there
    are two such passes. A REPORTING-ONLY pass (no alert-eligible roots — the
    ``_codex-quota-verify`` worker, the dashboard tick, every ``codex quota``
    invocation) returns from ``_evaluate_quota_alerts`` at
    ``if not alert_eligible_roots`` before a single threshold is examined, so
    stamping the gate would retire a delivery-gate ENABLE with no arming row and
    no ``suppressed_backfill`` written, and the axis could never re-fire because
    ``gate_before`` now reads True. A hook tick that DEFERRED axis 4 is the
    other. Scheduled ownership is narrower: ``schedule_evaluated_roots`` names
    only complete root histories this alert-eligible pass may replace. Group
    passes and reporting-only passes can merge new minima but retire none.

    ``ledger_watermark`` is stamped INSIDE this transaction. That is deliberate:
    ``run_stats_ingest`` is the sole stats writer, so advancing it after the
    commit would need a second cycle — and atomicity is the stronger guarantee
    anyway. A crash replays the range, which is safe because re-materializing a
    group is idempotent.
    """
    historic_roots = _historic_root_keys(conn)
    roots_to_reconcile = (
        active_roots | historic_roots
        if reconcile_roots is None else set(reconcile_roots)
    )
    if not roots_to_reconcile:
        return
    generation = secrets.token_hex(16)
    blocks = build_blocks(observations)
    digests = _group_digests(observations)
    for block in blocks:
        _reanchor_terminal_events(conn, block)
        unit = _block_unit_text(block)
        conn.execute(
            _BLOCK_UPSERT,
            _block_params(block, generation, unit, digests.get(unit, "")),
        )
        for milestone in percent_milestones(block):
            conn.execute(
                _MILESTONE_UPSERT,
                _milestone_params(block, milestone, generation),
            )
    blocks_orphaned, milestones_orphaned = _orphan_unseen(
        conn, roots_to_reconcile, generation, now_iso, units=dirty_units,
    )
    queued = _evaluate_quota_alerts(
        conn, observations=observations,
        alert_eligible_roots=alert_eligible_roots & active_roots,
        now=now, now_iso=now_iso, journal_emit=journal_emit,
        journal_disarm=journal_disarm, journal_terminal=journal_terminal,
    )
    # The completion stamp is intentionally the final DML in the stats
    # transaction.  A pre-commit failure rolls all projection updates back;
    # a retry sees the prior complete generation or rederives it.
    #
    # projection_state is (source_root_key, account_key)-keyed (#341 spec §2).
    # The account partitions and the root signature are both derived from the
    # LIVE BLOCKS rather than from this pass's observations (public #5 Task 9):
    # under a bounded pass an account whose only evidence lies in a clean window
    # contributes nothing to `observations`, so an observation-derived set would
    # drop it and leave its row carrying a stale signature. A retired partition
    # loses its blocks to the sweep above, so the same read both keeps and
    # retires the right rows — and the DELETE below removes what it no longer
    # names. A root with no blocks at all still stamps one `unattributed` row,
    # byte-stable with the prior behaviour.
    roots_to_stamp = active_roots & roots_to_reconcile
    for root_key in sorted(roots_to_stamp):
        accounts = _root_accounts(conn, root_key) or {_lib_accounts.UNATTRIBUTED}
        root_signature = _ledger.compose_root_signature(
            _root_group_pairs(conn, root_key))
        placeholders = ",".join("?" for _ in accounts)
        conn.execute(
            "DELETE FROM quota_projection_state WHERE source_root_key=? "
            "AND account_key NOT IN (" + placeholders + ")",
            (root_key, *sorted(accounts)),
        )
        for account_key in sorted(accounts):
            conn.execute(
                """INSERT INTO quota_projection_state
                   (source_root_key, account_key, generation, physical_signature,
                    completed_at_utc)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(source_root_key, account_key) DO UPDATE SET
                     generation=excluded.generation,
                     physical_signature=excluded.physical_signature,
                     completed_at_utc=excluded.completed_at_utc""",
                (root_key, account_key, generation, root_signature, now_iso),
            )
    # The cache-side certificate remains whole-store even when this pass was
    # root-scoped. Root signatures are composable from the stored group digests,
    # so collecting every active root here is O(groups), not O(observations),
    # and prevents a scoped pass from erasing untouched roots from the proof.
    signatures = {
        root_key: _ledger.compose_root_signature(
            _root_group_pairs(conn, root_key))
        for root_key in sorted(active_roots)
    }
    # The state row is stamped even when ``ledger_watermark`` is ``None`` — a
    # cache too old to carry the change log, where ``_ledger_max_seq`` cannot
    # report a sequence. Guarding this whole block on it left such a store with
    # no row at all, so ``_full_verification_due`` read True forever and it was
    # permanently "overdue" for a pass it had just run. Harmless in outcome (no
    # ledger means every pass is full anyway) but dishonest, and it makes the
    # deadline unusable as a signal. ``0`` is the right watermark there: it is
    # the only sequence a ledgerless cache can claim to have consumed, and if
    # the log later appears the pass replays from its first entry, which is
    # idempotent.
    #
    # Axis 4 (spec §3): an observation captured in the FUTURE is skipped as
    # a threshold qualifier and becomes eligible when wall time passes it,
    # with no row mutation for the ledger to record. Persisting the earliest
    # such instant is what turns that into a dirtiness signal.
    stored_boundary = None
    if stored_next_evaluation_at:
        try:
            stored_boundary = _parse_utc(
                stored_next_evaluation_at, "next_evaluation_at_utc")
        except (TypeError, ValueError):
            stored_boundary = None
    #
    # Recording a NEWLY seen future capture is safe from any pass, so that side
    # stays unconditional; only RETIRING a matured instant is gated, because
    # that is the half that claims an evaluation happened.
    stored_schedule = dict(stored_next_evaluation_by_root or {})
    # Retire only roots whose COMPLETE history this alert-eligible pass
    # evaluated. A group-bounded pass may discover an earlier future capture,
    # but it cannot prove the absence of another capture in a clean group, so
    # it only merges minima and never removes a stored root deadline.
    for root_key in schedule_evaluated_roots:
        stored_schedule.pop(root_key, None)
    future_by_root: dict[str, dt.datetime] = {}
    for observation in observations:
        if observation.captured_at <= now:
            continue
        root_key = observation.identity.source_root_key
        current = future_by_root.get(root_key)
        if current is None or observation.captured_at < current:
            future_by_root[root_key] = observation.captured_at
    for root_key, captured_at in future_by_root.items():
        current_wire = stored_schedule.get(root_key)
        current = (
            None if current_wire is None
            else _parse_utc(current_wire, "next_evaluation_by_root_json")
        )
        if root_key in schedule_evaluated_roots or current is None or captured_at < current:
            stored_schedule[root_key] = _utc_iso(captured_at)
    # Inactive roots have no lifecycle owner and cannot dispatch. Keeping their
    # deadlines would create an unretirable scheduled axis.
    if prune_inactive_schedule:
        stored_schedule = {
            root_key: captured_at
            for root_key, captured_at in stored_schedule.items()
            if root_key in active_roots
        }
    schedule_boundary = min(stored_schedule.values(), default=None)
    # A scalar-only boundary is a legacy/fail-safe state with unknown ownership.
    # Keep the prior behavior until a qualifying whole-history pass can retire
    # it; current-epoch indexes normally never enter this branch.
    legacy_boundary = stored_boundary if not stored_next_evaluation_by_root else None
    boundary = (
        None if schedule_boundary is None
        else _parse_utc(schedule_boundary, "next_evaluation_by_root_json")
    )
    if legacy_boundary is not None:
        retained_legacy = _axes.next_evaluation_boundary(
            capture_times=(), now=now, stored=legacy_boundary,
            retain_due=not consume_alert_axes,
        )
        if retained_legacy is not None and (
            boundary is None or retained_legacy < boundary
        ):
            boundary = retained_legacy
    _store_ledger_state(
        conn, watermark=0 if ledger_watermark is None else ledger_watermark,
        alerts_enabled=(
            alerts_enabled if consume_alert_axes else stored_alerts_enabled),
        next_evaluation_at=None if boundary is None else _utc_iso(boundary),
        next_evaluation_by_root=stored_schedule,
        # Spec §2: EVERY full pass stamps the verification deadline,
        # whatever triggered it — the interval itself, a rebuild, an
        # interpretation bump, `force_full`, or a dirty-unit burst
        # overflow. `dirty_units is None` is exactly "this pass was
        # whole-history", so the stamp cannot drift from the thing it
        # certifies. A bounded pass carries the stored value through
        # untouched; overwriting it would let an install that never
        # bursts postpone verification forever.
        last_full_pass_at=(
            now_iso
            if dirty_units is None and reconcile_roots is None
            else stored_last_full_pass_at),
    )
    if sink is not None:
        # Set-then-dispatch: all claims committed with the cycle before the
        # cycle's post-commit ALERT_DISPATCHER fires them (spec §5.2 step 6).
        sink.extend(queued)
    if holder is not None:
        holder["signatures"] = signatures
        holder["result"] = QuotaProjectionResult(
            generation=generation,
            blocks_upserted=len(blocks),
            milestones_upserted=sum(len(percent_milestones(b)) for b in blocks),
            blocks_orphaned=blocks_orphaned,
            milestones_orphaned=milestones_orphaned,
            roots_stamped=len(roots_to_stamp),
            alerts_dispatched=len(queued),
        )


@dataclass(frozen=True)
class QuotaProjectionBundle:
    """Everything the rebuild's projection pass reads out of `cache.db`.

    Spec §4.4 requires the rebuild to capture the coverage certificate, the
    physical mutation sequence, the source roots, the observations and the
    ledger state from ONE read-only WAL snapshot. Before this existed the leg
    captured the first two and the projection opened its own connection later,
    so a destructive clear landing between them published a generation whose
    quota projection was materialized from a cleared cache while the coverage
    verdict already read `covered`.

    The projection CERTIFICATE §4.4 also names is not carried here, and that is
    not an omission: the rebuild pass runs `_apply_quota_projection_rows` with
    `holder=None`, so it neither reads nor writes that certificate. Adding a
    field nothing consumes would be the same defect the recovery progress
    record's applied-count was.
    """

    active_roots: "set[str]"
    observations: object
    watermark: "int | None"


def load_quota_projection_bundle(cache_conn) -> QuotaProjectionBundle:
    """Read the bundle from ``cache_conn``, inside whatever transaction it holds.

    The caller owns the transaction, which is the whole point: on the rebuild's
    intact path the connection is the one that already read the coverage
    certificate, and under WAL its read snapshot is fixed at that first read, so
    these three reads and that certificate describe one cache state.
    """
    active_roots = _cache_root_keys(cache_conn)
    observations = load_codex_quota_observations(
        source_root_keys=None, cache_conn=cache_conn,
    )
    # A rebuild is a whole-history pass by definition, so it also initializes
    # the watermark: every ledger entry up to here is already reflected in what
    # it materialized, and leaving the watermark at zero would make the next
    # tick replay the entire ledger for nothing.
    watermark = _ledger_max_seq(cache_conn)
    return QuotaProjectionBundle(
        active_roots=active_roots, observations=observations,
        watermark=watermark,
    )


def rematerialize_quota_projection_for_rebuild(
    stats_conn, *, now=None, bundle=None,
) -> None:
    """Rebuild path (spec §5.4 / §5.3 "projection"): re-run the Codex quota
    projection over the materialized cache.db ``quota_window_snapshots`` directly
    onto the fresh rebuilt ``stats_conn``, side-effect-free.

    Called by ``rebuild_stats_index`` AFTER the ``quota_alert_arming`` evts have
    folded (order 45), so ``_evaluate_quota_alerts`` honors the replayed
    activation boundary and re-fires nothing. NO alerts (``sink=None``), NO arming
    journaling (``journal_emit=None``), NO alert-eligible roots. Opens no stats
    connection of its own — writes on the caller's rebuild transaction. Uses the
    SAME ``active_roots`` source as the live reconcile (``_cache_root_keys`` over
    the cache) so the materialized projection matches live. A missing cache.db is
    a clean no-op (the journal quota obs remain the durable source; a later
    ``cache-sync`` + reconcile re-materializes).

    ``bundle`` is the §4.4 single snapshot. When the caller supplies one this
    function opens NO cache connection of its own, so the projection is
    materialized from the same cache state the coverage verdict was decided
    against. When it is absent — the recovery path, where the leg wrote to the
    cache and a snapshot taken before those writes would miss them — the
    function reads its own, which is what it always did."""
    if now is None:
        now = dt.datetime.now(UTC)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    now_iso = _utc_iso(now)
    if bundle is None:
        try:
            cache = _cache_connection()
        except (FileNotFoundError, sqlite3.Error):
            return
        try:
            bundle = load_quota_projection_bundle(cache)
        finally:
            cache.close()
    active_roots = bundle.active_roots
    observations = bundle.observations
    watermark = bundle.watermark
    _apply_quota_projection_rows(
        stats_conn, observations=observations, active_roots=active_roots,
        now=now, now_iso=now_iso, sink=None,
        alert_eligible_roots=frozenset(), journal_emit=None, holder=None,
        dirty_units=None, ledger_watermark=watermark,
        # Reporting-only, but NOT a carry-through: this writes onto a freshly
        # rebuilt index where there is no stored axis state to preserve, and
        # recording the boundary from complete evidence is exactly what the
        # rebuilt row should start life with. A gate of NULL is the fail-safe
        # value — `gate_before is not True` makes the next alert-eligible pass
        # widen and do the activation.
        consume_alert_axes=True,
        prune_inactive_schedule=True,
    )


def _resolve_pass_scope(
    cache_conn: sqlite3.Connection, *, force_full: bool,
    ledger_state: "dict | None", ledger_high: "int | None", watermark: int,
    active_roots: set[str], stale_reverse_map: bool,
    verification_due: bool = False,
) -> "tuple[dict | None, int | None]":
    """Decide between a bounded pass and a whole-history one.

    Returns ``(scope, watermark_target)``. ``scope`` is ``None`` for a full pass
    or a dict carrying the exact raw groups to load and the loading units to
    sweep. ``watermark_target`` is the sequence the pass will stamp, or ``None``
    when there is no ledger to stamp against.

    Every branch that is not provably safe takes the full path. In order:

    * ``force_full`` and a cache with no ledger table are explicit requests.
    * No stored state at all — a first run, or a rebuilt index — has no consumed
      range to trust.
    * A stored interpretation version that is not the current one means the
      interpreted KEYS may have moved with no row mutation to observe, which is
      exactly what the ledger cannot see.
    * ``ledger_high < watermark`` means the ledger was reset under us (a deleted
      and recreated cache restarts ``AUTOINCREMENT`` at 1), so the stored
      watermark now points past entries that describe different mutations.
    * A block with no reverse map cannot be reached by a scoped sweep.
    * ``verification_due`` is the periodic whole-history pass (spec §2): the
      scoped sweep cannot see a block whose physical group left the cache
      entirely, nor a milestone on a historic root, so the interval is what
      bounds how long either may survive.
    * More dirty units than ``_MAX_INCREMENTAL_UNITS`` is a burst — a rebuild or
      a first ingest — where one unbounded scan beats N indexed seeks.
    """
    if force_full or ledger_high is None or verification_due:
        return None, ledger_high
    if (
        ledger_state is None
        or ledger_state["interpretation_version"]
        != _CODEX_QUOTA_INTERPRETATION_VERSION
        or ledger_high < watermark
        or stale_reverse_map
    ):
        return None, ledger_high
    rows = _ledger_rows_after(cache_conn, watermark, ledger_high)
    raw_groups = _ledger.expand_dirty_groups(rows)
    # Spec §2: "Liveness may narrow what the LOADER is asked to fetch, because
    # an inactive root's shard returns nothing anyway; it must never narrow what
    # the SWEEP is asked to reconcile." A dirty unit names a group to sweep even
    # when its root has left `codex_source_roots` — that is precisely the case
    # where its blocks must be orphaned. Deriving `units` from the FILTERED set
    # dropped the departed root's ledgered deletions while still pruning their
    # ledger entries, stranding those blocks permanently in
    # `_historic_root_keys`, the projection state and the dashboard, which is a
    # regression against the pre-change whole-root `_orphan_unseen`.
    units = {
        _ledger.physical_group_key_text(_ledger.loading_unit_from_raw(group))
        for group in raw_groups
    }
    if len(units) > _MAX_INCREMENTAL_UNITS:
        return None, ledger_high
    load_groups = raw_groups
    if active_roots:
        load_groups = frozenset(
            group for group in raw_groups if group[0] in active_roots)
    return (
        {
            # The loader matches RAW stored coordinates, so the request has to
            # enumerate every spelling that snaps onto a dirty group. One minute
            # of weekly jitter lives in BOTH the limit key and a column, so two
            # raw groups can interpret into one window and loading only the
            # mutated one would hand the fold a PARTIAL population.
            "raw_groups": _ledger.snap_equivalent_raw_groups(load_groups),
            "units": units,
        },
        ledger_high,
    )


def reconcile_codex_quota_projection(
    *,
    source_root_keys: Iterable[str] | None = None,
    alert_eligible_root_keys: Iterable[str] = (),
    now: dt.datetime | None = None,
    force_full: bool = False,
    full_pass: str = "inline",
    _before_stats_commit: Callable[[], None] | None = None,
    _after_stats_commit: Callable[[], None] | None = None,
) -> QuotaProjectionResult:
    """Reconcile every active Codex root into one stats transaction.

    Reporting reconciles every configured root. Threshold evaluation is limited
    to explicitly lifecycle-eligible roots, so read-only quota commands pass an
    empty set and never create an alert claim or activation boundary.

    By default the pass is BOUNDED to what the change ledger says moved since
    the stored watermark (public #5). ``force_full=True`` bypasses the ledger
    entirely and re-materializes everything, which is what the equivalence
    oracle compares against and what an operator gets from a rebuild.

    ``full_pass`` decides where a WHOLE-HISTORY pass runs. ``"inline"`` is every
    caller but the hook: the pass happens on this call, which is what makes the
    verification deadline "satisfied by whichever caller reaches it first".
    ``"defer"`` is the hook's, and the rule there is absolute — the hook path
    never runs a whole-history pass inline, whatever put it there. Every route
    ``_resolve_pass_scope`` has into one (an absent or rebuilt projector state,
    an interpretation-version bump, a reset ledger, a block missing its reverse
    map, a dirty-unit burst, a ledgerless cache, and the periodic interval) is
    handed to the detached ``_codex-quota-verify`` worker instead.

    Two deliberate carve-outs stay inline. Only the first is unreachable from
    the hook; the second is reachable and stays inline anyway, which is a
    different claim and the honest one.

    ``force_full`` still runs inline, and no hook caller passes it. It is a
    programmatic "do it now" — the equivalence oracle and the rebuild path both
    need it to mean that.

    The spec §3 alert axes also still widen inline, and they ARE reachable here:
    every hook tick that clears the 15-second lifecycle throttle passes a
    non-empty ``alert_eligible_root_keys``, so ``alert_scope`` is resolved and
    the widening branch is live on all of them. That is accepted for axes 2 and
    3 — a rule change or a delivery-gate ENABLE must write ``suppressed_backfill``
    terminal rows for already-satisfied blocks rather than dispatch history, and
    it can only do that by SEEING those blocks; the worker runs reporting-only
    with no alert-eligible roots, so deferring would break the pass rather than
    delay it. Both fire on a config change the user just made, so the cost is
    bounded and attributable.

    Axis 4 fires on wall clock. Epoch 1007 persists its owning roots, so a due
    hook tick performs complete passes only for the matured roots. A legacy or
    inconsistent scalar-only boundary still defers under ``"defer"`` because
    its only honest fallback is whole history.

    A tick that widened to whole-history for axis 2 or 3 anyway does retire it,
    because it did look: at every observation of every active root. Withholding
    that was an absorbing state rather than a conservative one — the widening
    routes are exactly the ones that also need to stamp ``alerts_enabled``, so
    declining both left the same scope standing and repeated the whole-history
    pass inline on every subsequent tick.

    A quiet owned boundary is therefore consumed without broadening the hook to
    unrelated roots; the following quiet tick returns to the zero-group path.
    """
    if full_pass not in ("inline", "defer"):
        raise ValueError(
            "reconcile_codex_quota_projection: full_pass must be "
            "'inline' or 'defer'")
    if now is None:
        now = dt.datetime.now(UTC)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    now_iso = _utc_iso(now)

    alert_eligible_roots = {str(key) for key in alert_eligible_root_keys}
    global_alerts_enabled, quota_alerts_enabled, _rules, _config = (
        _quota_alert_config()
    )
    delivery_enabled = global_alerts_enabled and quota_alerts_enabled

    try:
        cache = _cache_connection()
    except (FileNotFoundError, sqlite3.Error):
        return QuotaProjectionResult(None, 0, 0, 0, 0, 0, 0)
    try:
        # F2: read active_roots, the physical sequence, and the certificate
        # inside ONE WAL read snapshot so a concurrent commit cannot interleave
        # a stale sequence with a fresh certificate.
        cache.execute("BEGIN")
        try:
            active_roots = (
                _cache_root_keys(cache)
                if source_root_keys is None else {str(key) for key in source_root_keys}
            )
            physical_sequence = codex_physical_mutation_seq(cache)
            certificate = load_codex_quota_projection_certificate(cache)
            ledger_high = _ledger_max_seq(cache)
        finally:
            cache.commit()
        # ONE stats read decides the whole shape of the pass: whether the
        # short-circuit fires, and if not, whether the pass is bounded.
        ledger_state = None
        has_arming = False
        stale_reverse_map = True
        alert_scope = None
        stats_conn = _cctally_core.open_db()
        try:
            ledger_state = _ledger_state(stats_conn)
            has_arming = bool(stats_conn.execute(
                "SELECT 1 FROM quota_alert_arming "
                "WHERE source='codex' LIMIT 1"
            ).fetchone())
            stale_reverse_map = _blocks_missing_reverse_map(stats_conn)
            signatures_match = (
                certificate is not None
                and _stats_projection_signatures_match(
                    stats_conn, active_roots, certificate[1])
            )
            if alert_eligible_roots:
                alert_scope = _resolve_alert_scope(
                    stats_conn, ledger_scope=_axes.SCOPE_GROUPS, now=now,
                    ledger_state=ledger_state,
                    global_enabled=global_alerts_enabled,
                    quota_enabled=quota_alerts_enabled,
                    rules=_rules, config=_config,
                    eligible_roots=alert_eligible_roots,
                    defer_scheduled=(full_pass == "defer"),
                )
        finally:
            stats_conn.close()

        watermark = 0 if ledger_state is None else ledger_state["watermark"]
        # Short-circuit: when nothing is alert-eligible and the certificate
        # proves the cache physical state is current AND the stats-side
        # projection still matches it (F1), the ~2.9 s observation load and the
        # whole reconcile are provably a no-op. Any missed concurrent write
        # leaves cur_seq != cert_seq (or a stats-signature mismatch) on the next
        # call, so the scheme is self-healing.
        #
        # The unconsumed-ledger clause is the third leg, and it closes a hole
        # the certificate alone cannot see: a writer that mutates
        # `quota_window_snapshots` WITHOUT advancing the physical mutation
        # sequence (migration 028 does exactly this, and the journal cache
        # applier did too) leaves the certificate reading as current while real
        # interpretation drift sits unprocessed. The triggers recorded it, so
        # the ledger knows even when the sequence does not.
        #
        # The projector's own state has to be current too. A stale
        # interpretation version or a block with no reverse map means the next
        # pass MUST do work, and skipping on the certificate alone would defer
        # that repair forever — the certificate cannot see either condition.
        #
        # The periodic verification joins them for the same reason. A skipped
        # pass never stamps the deadline, so letting the short-circuit fire
        # while it is overdue would leave every subsequent tick overdue too —
        # the interval would never elapse into anything.
        verification_due = _full_verification_due(ledger_state, now)
        projector_state_current = (
            ledger_state is not None
            and ledger_state["interpretation_version"]
            == _CODEX_QUOTA_INTERPRETATION_VERSION
            and not stale_reverse_map
        )
        if (
            verification_due
            and full_pass == "defer"
            # The interval alone. When the projector state is otherwise current
            # this tick can still do its ordinary BOUNDED work after handing the
            # verification off, which is why this gate is separate from the
            # catch-all below: deferring here costs nothing, deferring there
            # costs this tick's incremental progress.
            and projector_state_current
            and ledger_high is not None
            and ledger_high >= watermark
        ):
            # The bounded ingest leg made every part of the hook tick bounded
            # except this one. The spec's escape hatch — "whichever caller
            # reaches the deadline first, a dashboard tick or a `codex quota`
            # invocation" — does not exist for a hook-only install, which is
            # precisely the reporter's shape, so once a day the whole-history
            # reconcile would land on the blocking hook path against Codex's
            # 30-second timeout and fail acceptance criterion 3 for the very
            # user who reported the bug. Hand it to a detached worker.
            #
            # Skipping it on a failed spawn is deliberate: the deadline is only
            # stamped by a pass that COMPLETES, so the next tick is still due
            # and retries (throttled). One missed daily verification is bounded
            # staleness; a 30-second hook stall is the reported defect.
            _defer_codex_quota_verification()
            verification_due = False
        ledger_state_current = projector_state_current and not verification_due
        if (
            not force_full
            and not alert_eligible_roots
            and certificate is not None
            and ledger_high is not None
            and ledger_high == watermark
            and ledger_state_current
        ):
            cert_seq, cert_sigs = certificate
            if physical_sequence == cert_seq and active_roots <= set(cert_sigs):
                can_skip_delivery = delivery_enabled or not has_arming
                if can_skip_delivery and signatures_match:
                    return QuotaProjectionResult(None, 0, 0, 0, 0, 0, 0)

        dirty_units, watermark_target = _resolve_pass_scope(
            cache,
            force_full=force_full,
            ledger_state=ledger_state,
            ledger_high=ledger_high,
            watermark=watermark,
            active_roots=active_roots,
            stale_reverse_map=stale_reverse_map,
            verification_due=verification_due,
        )
        # ``None`` means the genuine whole-history path. A set means complete
        # histories for only those roots — axis 2 and epoch-1007 axis 4 can both
        # be satisfied without scanning unrelated roots.
        reconcile_roots: "set[str] | None" = None
        if dirty_units is None and full_pass == "defer" and not force_full:
            # The rule, for every OTHER route into a whole-history pass: an
            # absent or freshly rebuilt projector state, an interpretation-
            # version bump, a reset ledger, a block missing its reverse map, a
            # dirty-unit burst, and a cache too old to carry the change log.
            #
            # The rebuilt-state route is the one that matters, and it is not
            # hypothetical: this feature bumps `STATS_INDEX_EPOCH`, so every
            # upgrading install rebuilds stats.db from the journal on first
            # open. Measured on a real 211K-observation store that rebuild alone
            # cost 76.45s of an 82.05s tick. Running the whole-history pass
            # inline on top of it, on a path Codex kills at 30 seconds, means
            # `run_stats_ingest` commits nothing, `last_full_pass_at` is never
            # stamped, and the next tick repeats it — a non-converging
            # 30-second-per-turn loop, which is the reported defect delivered by
            # the fix.
            #
            # Unlike the interval gate above, this one performs NO projection
            # work at all: without a trustworthy watermark there is no bounded
            # pass to fall back to. That leaves the projection transiently
            # missing rather than merely stale, and it is accepted — the worker
            # converges it (throttled retry on failure), every non-hook caller
            # still runs inline, and a 30-second blocking tick is not an option.
            _defer_codex_quota_verification()
            return QuotaProjectionResult(None, 0, 0, 0, 0, 0, 0)
        # Axis 2/3/4 (spec §3): alert state is not a function of window
        # dirtiness. A rule change and a delivery-gate ENABLE each require a
        # semantic pass over the affected identities with no observation having
        # moved — and activation must write `suppressed_backfill` terminal rows
        # rather than dispatch history, which it can only do if it actually sees
        # the blocks. Widening a bounded pass is always safe; missing an
        # identity is not. Both are driven by a configuration change the user
        # just made, so the widening is bounded and expected, and it stays
        # inline even under `defer`.
        #
        # Axis 4 is root-scoped when epoch-1007 ownership is available. A
        # scalar-only legacy boundary still records `REASON_SCHEDULED_DEFERRED`
        # under `full_pass="defer"`, because whole history remains its only
        # honest fallback.
        if dirty_units is not None and alert_scope is not None:
            if alert_scope.scope == _axes.SCOPE_ALL:
                dirty_units = None
            elif alert_scope.scope == _axes.SCOPE_ROOTS:
                # Reconcile every alert-invalidated root in full. If physical
                # ledger work landed on the same tick, promote its roots too so
                # the watermark can advance without skipping those mutations.
                reconcile_roots = set(alert_scope.roots)
                reconcile_roots.update(
                    str(group[0]) for group in dirty_units["raw_groups"])
                dirty_units = None
        if dirty_units is None:
            observations = load_codex_quota_observations(
                source_root_keys=(
                    active_roots if reconcile_roots is None
                    else active_roots & reconcile_roots
                ),
                cache_conn=cache,
            )
        else:
            observations = load_codex_quota_observations(
                source_root_keys=active_roots, cache_conn=cache,
                physical_groups=dirty_units["raw_groups"],
            )
            # A unit whose members ALL disappeared loads nothing, so the loaded
            # observations alone would not name it and its blocks would never be
            # swept. The ledger-derived set is authoritative for the sweep; the
            # loaded set is unioned in only to cover a stored limit key the raw
            # snap and the interpreted strip disagree on (reachable by hand
            # repair, not by ingest).
            dirty_units = frozenset(
                dirty_units["units"]
                | {_observation_unit_text(o) for o in observations}
            )
    finally:
        cache.close()

    # A pass may only advance the two non-dirtiness alert axes it stores if it
    # actually did the work they exist to trigger.
    #
    # A REPORTING-ONLY pass (the `_codex-quota-verify` worker, the dashboard
    # tick, `codex quota`) never reaches a threshold decision —
    # `_evaluate_quota_alerts` returns at `if not alert_eligible_roots` — yet it
    # used to stamp both, so a delivery-gate ENABLE landing on the same tick as
    # a full pass was consumed with no arming row and no `suppressed_backfill`,
    # and could not re-fire because `gate_before` then read True. Every
    # upgrading install is in exactly that state right after the epoch rebuild,
    # and the daily verification puts installs there routinely. Carrying
    # eligible roots is what excludes it, and nothing else does.
    #
    # The second condition asks whether this pass LOOKED, not whether it
    # intended to. A bounded tick that deferred axis 4 declined to look, so it
    # must not retire the boundary. A tick that widened to whole-history for
    # some OTHER reason did look — at every observation of every active root,
    # and applied over all of them — so it evaluated the matured instant as
    # surely as a widening for axis 4 itself would have, and retiring it is
    # honest. Gating on the deferral alone instead was an absorbing state, not a
    # conservative one: `alert_dirty_scope` widens whenever `gate_before is not
    # True` (the NULL a rebuild leaves, the False a disable leaves), the hook is
    # the only production caller that carries eligible roots AND it always
    # defers, so a due boundary froze `alerts_enabled` at its stored value and
    # every following tick re-resolved the identical SCOPE_ALL — the
    # whole-history reconcile inline on the blocking path, on every turn.
    #
    # Reaching this line with `dirty_units is None` under `defer` means
    # precisely "the alert axes widened it": every other route into a
    # whole-history pass returned at the catch-all above, and no hook caller
    # passes `force_full`.
    consume_alert_axes = bool(alert_eligible_roots & active_roots) and (
        dirty_units is None
        or alert_scope is None
        or _axes.REASON_SCHEDULED_DEFERRED not in alert_scope.reasons
    )
    complete_roots = (
        (active_roots if reconcile_roots is None else active_roots & reconcile_roots)
        if dirty_units is None else set()
    )
    schedule_evaluated_roots = frozenset(
        complete_roots & alert_eligible_roots)

    # ── Apply phase (Task 7 Item 3) ─────────────────────────────────────────
    # The stats.db writes route through the single-flight ingest cycle instead
    # of this function opening its own stats connection + BEGIN IMMEDIATE. The
    # nested `_apply_projection(conn, sink)` is the transaction-neutral chokepoint
    # (writes on `conn`, no BEGIN/commit; alerts to `sink`) — the ingest cycle's
    # `codex_apply` seam drives it on the cycle's conn, AFTER the codex flock has
    # released (the existing after-flock-release rule). The interpreted `quota_*`
    # tables are mutable projections re-materialized here (spec §5.3), and each
    # genuine arming activation journals a `quota_alert_arming` evt so the
    # forward-only boundary survives a stats.db rebuild (Item 5).
    holder: dict = {
        "result": QuotaProjectionResult(None, 0, 0, 0, 0, 0, 0),
        "signatures": None,
    }

    def _apply_projection(
        conn, sink, *, journal_emit=None, journal_disarm=None,
        journal_terminal=None,
    ):
        # No configured roots and no existing interpreted history means there is
        # no stats work.  This preserves the existing empty-Codex fast path.
        # Delegates to the shared module-level apply so the live leg and the
        # rebuild re-materialization pass (Task 8) never drift.
        _apply_quota_projection_rows(
            conn, observations=observations, active_roots=active_roots,
            now=now, now_iso=now_iso, sink=sink,
            alert_eligible_roots=alert_eligible_roots,
            journal_emit=journal_emit, journal_disarm=journal_disarm,
            journal_terminal=journal_terminal,
            holder=holder, dirty_units=dirty_units,
            ledger_watermark=watermark_target,
            alerts_enabled=delivery_enabled,
            stored_next_evaluation_at=(
                None if ledger_state is None
                else ledger_state["next_evaluation_at"]),
            stored_last_full_pass_at=(
                None if ledger_state is None
                else ledger_state["last_full_pass_at"]),
            stored_alerts_enabled=(
                None if ledger_state is None
                else ledger_state["alerts_enabled"]),
            stored_next_evaluation_by_root=(
                {} if ledger_state is None
                else ledger_state["next_evaluation_by_root"]),
            consume_alert_axes=consume_alert_axes,
            reconcile_roots=reconcile_roots,
            schedule_evaluated_roots=schedule_evaluated_roots,
            prune_inactive_schedule=(
                source_root_keys is None
                and dirty_units is None
                and reconcile_roots is None),
        )

    import _cctally_journal as _jr
    import _lib_journal as _jl

    def _codex_leg(ctx):
        def _emit_arming(identity, fingerprint, activated_at):
            # Item 5: journal the arming state change (`quota_alert_arming` evt).
            # account_key (#341) is part of the qaa state identity (after the
            # root), while fingerprint + activation boundary make each real
            # state transition a distinct rev-0 event (#372). Exact re-emission
            # of one state still converges on one id. This order MUST match the
            # cutover export's natural_key_id.
            eid = _jl.evt_id(
                "qaa", identity.source, identity.source_root_key,
                identity.account_key, identity.logical_limit_key,
                identity.observed_slot, identity.window_minutes,
                fingerprint, activated_at,
            )
            _jr.append_record(_jl.make_evt(
                kind="quota_alert_arming", id=eid, at=activated_at,
                payload={
                    "source": identity.source,
                    "source_root_key": identity.source_root_key,
                    "account_key": identity.account_key,
                    "logical_limit_key": identity.logical_limit_key,
                    "observed_slot": identity.observed_slot,
                    "window_minutes": identity.window_minutes,
                    "rule_fingerprint": fingerprint,
                    "activated_at_utc": activated_at,
                    "journal_identity_version": 2,
                },
            ))
            ctx.events_emitted += 1

        def _emit_disarm(identity, disarmed_at):
            eid = _jl.evt_id(
                "qaa", identity.source, identity.source_root_key,
                identity.account_key, identity.logical_limit_key,
                identity.observed_slot, identity.window_minutes,
                "disarmed", disarmed_at,
            )
            _jr.append_record(_jl.make_evt(
                kind="quota_alert_arming", id=eid, at=disarmed_at,
                payload={
                    "source": identity.source,
                    "source_root_key": identity.source_root_key,
                    "account_key": identity.account_key,
                    "logical_limit_key": identity.logical_limit_key,
                    "observed_slot": identity.observed_slot,
                    "window_minutes": identity.window_minutes,
                    "state": "disarmed",
                    "disarmed_at_utc": disarmed_at,
                    "journal_identity_version": 2,
                },
            ))
            ctx.events_emitted += 1

        def _emit_terminal_event(payload):
            # #416 spec §7.2: journal one TERMINAL threshold fact. The `qte:` id
            # mirrors the table's UNIQUE key, so one crossing is one event
            # forever and a replay converges instead of duplicating. This order
            # MUST match the cutover export's `natural_key_id`.
            eid = _jl.evt_id(
                "qte", payload["source"], payload["source_root_key"],
                payload["account_key"], payload["logical_limit_key"],
                payload["observed_slot"], payload["window_minutes"],
                payload["resets_at_utc"], payload["threshold"],
            )
            _jr.append_record(_jl.make_evt(
                kind="quota_threshold_event", id=eid,
                at=payload["created_at_utc"],
                payload={**payload, "journal_identity_version": 2},
            ))
            ctx.events_emitted += 1

        _apply_projection(
            ctx.conn,
            ctx.pending_alerts,
            journal_emit=_emit_arming,
            journal_disarm=_emit_disarm,
            journal_terminal=_emit_terminal_event,
        )
        # `_before_stats_commit` fires INSIDE the cycle txn, before COMMIT — a
        # raise rolls the whole cycle back, so the projection updates undo
        # together (the crash-consistency contract the callers test).
        if _before_stats_commit is not None:
            _before_stats_commit()

    # AUTHORITATIVE: the reconcile must observe its own write + return the
    # materialized result synchronously; the cycle dispatches the queued alerts
    # post-commit via the ALERT_DISPATCHER, and `post_commit` runs the
    # `_after_stats_commit` seam after the commit (before dispatch).
    _jr.run_stats_ingest(
        mode="authoritative", codex_apply=_codex_leg,
        post_commit=_after_stats_commit,
    )

    # Finalize: the cache-side certificate (self-healing no-op optimization) is
    # stored only when the apply actually materialized a projection. Ledger
    # pruning rides the same connection and transaction — it is the only other
    # cache write this function makes, and folding it in avoids adding a second
    # unflocked mutator.
    if holder["signatures"] is not None:
        _store_codex_quota_projection_certificate(
            sequence=physical_sequence, signatures=holder["signatures"],
            prune_ledger_through=watermark_target,
        )
    return holder["result"]


def _load_active_milestones(
    identity: QuotaWindowIdentity, resets_at: dt.datetime,
    *, stats_conn: sqlite3.Connection | None = None,
) -> list[sqlite3.Row | tuple[object, ...]]:
    owns_conn = stats_conn is None
    stats = _cctally_core.open_db() if stats_conn is None else stats_conn
    try:
        return list(stats.execute(
            """SELECT percent_threshold, captured_at_utc, source_path, line_offset
                 FROM quota_percent_milestones
                WHERE source=? AND source_root_key=? AND account_key=?
                  AND logical_limit_key=? AND observed_slot=? AND window_minutes=?
                  AND resets_at_utc=? AND orphaned_at IS NULL
                ORDER BY percent_threshold""",
            (
                identity.source, identity.source_root_key, identity.account_key,
                identity.logical_limit_key, identity.observed_slot,
                identity.window_minutes, _utc_iso(resets_at),
            ),
        ))
    finally:
        if owns_conn:
            stats.close()


def _codex_cache_account_predicate(
    account_key: str | None, *, admit_unattributed: bool = False,
) -> tuple[str, tuple]:
    """SQL fragment scoping a CACHE table to one account (#416 B2).

    The cache columns are nullable ``TEXT``, so ``NULL ≡ unattributed`` — the
    established cache-read rule (``load_cached_rooted_codex_accounting_entries``
    uses the identical pair). ``None`` yields an empty fragment, i.e. today's
    merged read.

    ``admit_unattributed`` selects the ONE-DIRECTIONAL WIDENING flavour — the
    SQL twin of ``_codex_account_admits``: a REAL account admits its own rows
    PLUS the unattributed sentinel, while an ``unattributed`` scope still admits
    only unattributed, so no REAL account's rows ever reach another's read.

    Which flavour a read wants is decided by the STAMPING MECHANISM behind its
    scope key, never by taste, and it is settled per read INSIDE this module —
    no caller elects it (#416 closeout F1/F3). The rule, in full:

    * **Widen** iff a row genuinely belonging to the focused account can still
      carry the ``unattributed`` sentinel IN THE TABLE BEING FILTERED — i.e. the
      scope key and the rows were stamped by DIFFERENT mechanisms, or by the
      same mechanism over a different population/window-group.
    * **Strict** iff the scope key was derived from the very column being
      filtered, over the same population: the read is then a partition of the
      rows the key came from, and the children must sum to the parent.
    * Corollary: **selection/boundary reads widen; cost- and percentage-
      adoption reads stay strict.** A boundary read answers "where did this
      block open" — widening it is not attribution. A cost read answers "whose
      dollars are these" — widening it IS attribution, which D1 forbids, and it
      puts one row in two scopes.

    FOUR stamping mechanisms exist and must never be conflated: the
    quota-observation fold (``adopt_unidentified_observations``, per physical-
    window group, landing post-fold in ``quota_window_blocks`` /
    ``quota_percent_milestones`` and NEVER written back to
    ``quota_window_snapshots``); per-file-range attribution
    (``codex_file_accounts`` -> ``codex_session_entries.account_key``,
    ``stably_absent`` -> NULL); WINDOW-SCOPED SPEND ADOPTION (the 2026-07-30
    spec — ``_lib_codex_account_adoption`` +
    ``_cctally_cache.apply_codex_window_spend_adoption``, which stamps that same
    ``codex_session_entries.account_key`` column at ingest from the window's
    single identified account); and the stats ``accounts`` registry.

    So ``codex_session_entries.account_key`` now carries window-derived
    attribution IN ADDITION to per-file decisions, and that is precisely what
    keeps the cost read strict rather than forcing it to widen. The rule above
    genuinely pointed both ways for that read: the scope key comes from the
    observation fold while the rows came from per-file attribution — DIFFERENT
    mechanisms, which reads as "widen" — yet widening a cost read is attribution
    D1 forbids and puts one row in two scopes. The resolution is to make the ROW
    carry the window's answer durably, so scope key and row now agree by
    construction and the strict flavour is correct rather than merely safe. A
    row the adoption pass declined to stamp (zero or ambiguous identified
    accounts, an overlap whose windows disagree) is genuinely nobody's and
    stays in the ``unattributed`` scope, which is the honest answer.
    """
    if account_key is None:
        return "", ()
    if account_key == _lib_accounts.UNATTRIBUTED:
        return "AND (account_key IS NULL OR account_key = ?)", (
            _lib_accounts.UNATTRIBUTED,)
    if admit_unattributed:
        return (
            "AND (account_key = ? OR account_key IS NULL OR account_key = ?)",
            (account_key, _lib_accounts.UNATTRIBUTED),
        )
    return "AND account_key = ?", (account_key,)


def _first_block_physical_tuple(
    identity: QuotaWindowIdentity, resets_at: dt.datetime,
    *, cache_conn: sqlite3.Connection | None = None,
    account_key: str | None = None,
) -> tuple[dt.datetime, str, int] | None:
    """Read the first physical tuple for one exact projected block.

    The prior implementation reconstructed every retained quota observation
    for the root in Python just to discover this boundary.  Keep the same
    physical ordering while letting SQLite filter the exact identity/reset.
    ``unixepoch`` deliberately accepts retained ``Z`` and ``+00:00`` spellings.

    ``account_key`` (#416 Slice 3A review B2) scopes the boundary to one
    account: two accounts sharing one root and one canonical reset otherwise
    hand the focused account the OTHER account's earlier start, so its first
    milestone segment absorbs spend from before its own block opened.
    ``NULL ≡ unattributed`` on this cache column; ``None`` keeps the merged
    read, which is byte-stable.

    A non-``None`` scope ALWAYS takes the widening flavour here, unconditionally
    and with no caller say in it (#416 closeout F1). Every scope key that can
    reach this read is stamped by a different mechanism than
    ``quota_window_snapshots.account_key``: the durable projection and the
    in-memory observation partition are both POST-fold
    (``load_codex_quota_observations`` runs ``adopt_unidentified_observations``
    before returning), while these snapshot rows are the PRE-fold raw cache the
    fold never writes back to. And this is a BOUNDARY read — "where did the
    block open" — so widening it attributes nothing.

    Getting it wrong here is maximally destructive: ``codex_quota_breakdown``
    returns ``()`` outright when this read finds no row, so a post-fold key read
    strictly against pre-fold snapshots does not shrink the ladder, it DELETES
    it, while the cycle INDEX (reading the post-fold
    ``quota_percent_milestones``) still counts the crossings — the #373
    root-cause-3 symptom exactly. The widening stays one-directional, so an
    ``unattributed`` scope never adopts a REAL account's boundary.
    """
    owns_conn = cache_conn is None
    if cache_conn is None:
        try:
            cache = _cache_connection()
        except (FileNotFoundError, sqlite3.Error):
            return None
    else:
        cache = cache_conn
    try:
        account_predicate, account_params = _codex_cache_account_predicate(
            account_key, admit_unattributed=True)
        row = cache.execute(
            """SELECT captured_at_utc, source_path, line_offset
                 FROM quota_window_snapshots
                WHERE source='codex' AND source_root_key=?
                  AND logical_limit_key=? AND observed_slot=?
                  AND window_minutes=?
                  AND unixepoch(resets_at_utc)=unixepoch(?)
                  """ + account_predicate + """
                ORDER BY unixepoch(captured_at_utc), unixepoch(resets_at_utc),
                         source_path, line_offset
                LIMIT 1""",
            (
                identity.source_root_key, identity.logical_limit_key,
                identity.observed_slot, identity.window_minutes,
                _utc_iso(resets_at), *account_params,
            ),
        ).fetchone()
    finally:
        if owns_conn:
            cache.close()
    if row is None:
        return None
    try:
        return (_parse_utc(str(row[0]), "captured_at_utc"), str(row[1]), int(row[2]))
    except (TypeError, ValueError):
        return None


def codex_quota_breakdown(
    identity: QuotaWindowIdentity,
    resets_at: str | dt.datetime,
    *, speed: str = "auto", cache_conn: sqlite3.Connection | None = None,
    stats_conn: sqlite3.Connection | None = None,
    account_key: str | None = None,
) -> tuple[CodexQuotaBreakdownRow, ...]:
    """Correlate durable milestone boundaries with live-priced cache accounting.

    Each comparison is the full physical tuple ``(timestamp, path, offset)`` so
    same-timestamp records stay deterministic.  Pricing is deliberately read
    now rather than materialized in stats.db, keeping a pricing refresh
    immediately effective for historical quota breakdowns.

    ``account_key`` (#416 Slice 3A review B2) scopes BOTH cache reads below —
    the block-start boundary and the accounting rows — to one account. The
    milestone read is already account-scoped through ``identity.account_key``,
    so without this the durable ladder mixed one account's crossings with every
    account's spend on that root. ``None`` is today's merged read and is
    byte-stable, which is what every CLI caller keeps.

    The two reads take DIFFERENT flavours, and neither is a caller's choice
    (#416 closeout F1/F3) — the single flag they used to share conflated two
    reads with opposite correctness requirements:

    * the boundary (``_first_block_physical_tuple``, pre-fold
      ``quota_window_snapshots``) always WIDENS. It is a selection read whose
      scope key comes from another mechanism entirely, and strict equality
      there blanks the whole ladder rather than trimming it.
    * the accounting read below (``codex_session_entries``) always stays
      STRICT. It is a COST read: widening it would file one unattributed row
      under a real account AND under the ``unattributed`` scope, which is
      inference D1 forbids and double-counting the children-sum-to-parent
      invariant forbids. A crossing whose spend is unattributed therefore
      renders an honest ``$0.00``; the dollars stay visible in the
      ``unattributed`` scope, which owns them.

    That honest ``$0.00`` used to fire for spend that was NOT nobody's. The
    crossing carries the window's account (the observation fold put it there)
    while the rows behind it carried none, because per-file attribution had no
    decision covering those bytes. ``codex_session_entries.account_key`` now also
    carries WINDOW-DERIVED attribution — stamped durably at ingest by
    ``apply_codex_window_spend_adoption`` under the same window key and the same
    single-identified-account guard the fold uses — so the ladder and the
    dollars agree by construction. The read stays strict on top of it precisely
    BECAUSE the inference is now durable: doing it here instead would re-file one
    row under two scopes on every read, while doing it once at ingest moves the
    row out of ``unattributed`` and into exactly one owner.

    The full rule, and the four stamping mechanisms it turns on, are in
    ``_codex_cache_account_predicate``.
    """
    reset = _parse_utc(resets_at, "resets_at") if isinstance(resets_at, str) else resets_at
    if reset.tzinfo is None or reset.utcoffset() is None:
        raise ValueError("resets_at must be timezone-aware")
    reset = reset.astimezone(UTC)
    milestones = _load_active_milestones(identity, reset, stats_conn=stats_conn)
    if not milestones:
        return ()
    owns_cache = cache_conn is None
    if cache_conn is None:
        try:
            cache = _cache_connection()
        except (FileNotFoundError, sqlite3.Error):
            return ()
    else:
        cache = cache_conn
    start = _first_block_physical_tuple(
        identity, reset, cache_conn=cache, account_key=account_key)
    if start is None:
        if owns_cache:
            cache.close()
        return ()
    # ``timestamp_utc`` is stored in canonical ``Z`` form, while retained
    # quota observations may have arrived as ``+00:00``.  Keep this SQL bound
    # deliberately one second wider and let the physical-tuple comparison
    # below enforce the exact inclusive endpoint.  This preserves same-second
    # path/offset ordering without relying on mixed-spelling text equality.
    query_start = (
        start[0] - dt.timedelta(seconds=1)
    ).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    query_end = (
        _parse_utc(str(milestones[-1][1]), "captured_at_utc")
        + dt.timedelta(seconds=1)
    ).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    try:
        entries = []
        # Cost read: strict, always. See the docstring above.
        entry_predicate, entry_params = _codex_cache_account_predicate(
            account_key)
        for row in cache.execute(
            """SELECT timestamp_utc, source_path, line_offset, model,
                      input_tokens, cached_input_tokens, output_tokens,
                      reasoning_output_tokens, total_tokens
                 FROM codex_session_entries
                WHERE source_root_key=?
                  AND timestamp_utc>=? AND timestamp_utc<=?
                  """ + entry_predicate,
            (
                identity.source_root_key,
                query_start,
                query_end,
                *entry_params,
            ),
        ):
            try:
                physical = (
                    _parse_utc(str(row[0]), "timestamp_utc"),
                    str(row[1]), int(row[2]),
                )
            except (TypeError, ValueError):
                continue
            entries.append((physical, row))
    finally:
        if owns_cache:
            cache.close()
    entries.sort(key=lambda pair: pair[0])
    resolved_speed = sys.modules["cctally"]._resolve_codex_speed(speed)
    calculate_cost = sys.modules["cctally"]._calculate_codex_entry_cost
    prior_cumulative = 0.0
    cumulative_input = 0
    cumulative_cached = 0
    cumulative_output = 0
    cumulative_reasoning = 0
    cumulative_total = 0
    result: list[CodexQuotaBreakdownRow] = []
    for milestone in milestones:
        end = (
            _parse_utc(str(milestone[1]), "captured_at_utc"),
            str(milestone[2]), int(milestone[3]),
        )
        selected = [row for physical, row in entries if start < physical <= end]
        input_tokens = sum(int(row[4]) for row in selected)
        cached = sum(int(row[5]) for row in selected)
        output = sum(int(row[6]) for row in selected)
        reasoning = sum(int(row[7]) for row in selected)
        total = sum(int(row[8]) for row in selected)
        marginal = sum(
            calculate_cost(
                str(row[3]), int(row[4]), int(row[5]), int(row[6]),
                int(row[7]), speed=resolved_speed,
            )
            for row in selected
        )
        cumulative = prior_cumulative + marginal
        cumulative_input += input_tokens
        cumulative_cached += cached
        cumulative_output += output
        cumulative_reasoning += reasoning
        cumulative_total += total
        result.append(CodexQuotaBreakdownRow(
            percent=int(milestone[0]), captured_at=end[0],
            input_tokens=cumulative_input, cached_input_tokens=cumulative_cached,
            output_tokens=cumulative_output, reasoning_output_tokens=cumulative_reasoning,
            total_tokens=cumulative_total, cost_usd=cumulative,
            marginal_cost_usd=marginal,
        ))
        prior_cumulative = cumulative
        start = end
    return tuple(result)


def codex_five_hour_percent_at_crossing(
    identity: QuotaWindowIdentity,
    captured_at: dt.datetime,
    observations: Iterable[QuotaObservation] | None = None,
    *, cache_conn: sqlite3.Connection | None = None,
) -> float | None:
    """Return the latest matching native five-hour percent at one crossing.

    #373: the 5h window must share the target identity's MODEL SCOPE, not just
    its ``limit_id``. A separate model pool can reuse ``limit_id="codex"`` and
    spell itself only in the interpreted key's ``modelPool`` or in
    ``limit_name``, so the pre-existing ``limit_id`` equality lets it through
    and a standard weekly crossing gets annotated with a foreign pool's 5h
    percent. The rule is symmetric: a Spark weekly still correlates with Spark
    5h rows, and never with standard ones.
    """
    target_model_scoped = is_model_scoped_codex_quota(
        identity.logical_limit_key, identity.limit_name,
    )
    if observations is not None:
        eligible = tuple(
            observation for observation in observations
            if observation.identity.source_root_key == identity.source_root_key
            and observation.identity.window_minutes == 300
            and observation.identity.observed_slot == identity.observed_slot
            and observation.identity.limit_id == identity.limit_id
            and is_model_scoped_codex_quota(
                observation.identity.logical_limit_key,
                observation.identity.limit_name,
            ) == target_model_scoped
            and observation.captured_at <= captured_at < observation.resets_at
        )
        if not eligible:
            return None
        return float(max(eligible, key=physical_order_key).used_percent)

    owns_conn = cache_conn is None
    if cache_conn is None:
        try:
            cache = _cache_connection()
        except (FileNotFoundError, sqlite3.Error):
            return None
    else:
        cache = cache_conn
    # A valid 300-minute observation that still covers the crossing must have
    # been captured within the preceding native window.  The extra hour keeps
    # seconds-level reset jitter and shortened/re-anchored blocks in range
    # while avoiding a root-wide history reconstruction.
    lower = (captured_at - dt.timedelta(hours=6)).astimezone(UTC).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )
    upper = (captured_at + dt.timedelta(seconds=1)).astimezone(UTC).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )
    try:
        rows = cache.execute(
            """SELECT captured_at_utc, resets_at_utc, source_path, line_offset,
                      used_percent, logical_limit_key, limit_name
                 FROM quota_window_snapshots
                WHERE source='codex' AND source_root_key=?
                  AND window_minutes=300 AND observed_slot=? AND limit_id IS ?
                  AND captured_at_utc>=? AND captured_at_utc<?""",
            (
                identity.source_root_key, identity.observed_slot,
                identity.limit_id, lower, upper,
            ),
        ).fetchall()
    finally:
        if owns_conn:
            cache.close()
    eligible: list[tuple[tuple[dt.datetime, dt.datetime, str, int], float]] = []
    for row in rows:
        try:
            observed_at = _parse_utc(str(row[0]), "captured_at_utc")
            resets_at = _parse_utc(str(row[1]), "resets_at_utc")
            physical = (observed_at, resets_at, str(row[2]), int(row[3]))
            used_percent = float(row[4])
        except (TypeError, ValueError, OverflowError):
            continue
        # Same model-scope rule as the in-memory branch above; the two must not
        # disagree about which 5h window belongs to this identity's pool.
        if is_model_scoped_codex_quota(row[5], row[6]) != target_model_scoped:
            continue
        if observed_at <= captured_at < resets_at:
            eligible.append((physical, used_percent))
    if not eligible:
        return None
    return max(eligible, key=lambda item: item[0])[1]


# === Canonical nested `cctally codex quota` CLI ===========================


class QuotaCLIError(ValueError):
    """A cctally-native quota CLI validation failure (exit 2)."""


def _cctally():
    """Resolve the current main module at call time for test isolation."""
    return sys.modules["cctally"]


def _iso_z(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _identity_wire(identity: QuotaWindowIdentity) -> dict[str, object]:
    return {
        "source": identity.source,
        "sourceRootKey": identity.source_root_key,
        "logicalLimitKey": identity.logical_limit_key,
        "observedSlot": identity.observed_slot,
        "windowMinutes": identity.window_minutes,
        "limitId": identity.limit_id,
        "limitName": identity.limit_name,
    }


def _freshness_wire(freshness: QuotaFreshness) -> dict[str, object]:
    return {
        "state": freshness.state,
        "source": "local-rollout",
        "capturedAt": _iso_z(freshness.captured_at),
        "ageSeconds": freshness.age_seconds,
        "staleAfterSeconds": freshness.stale_after_seconds,
    }


def _observation_wire(observation: QuotaObservation) -> dict[str, object]:
    return {
        "capturedAt": _iso_z(observation.captured_at),
        "usedPercent": observation.used_percent,
        "resetsAt": _iso_z(observation.resets_at),
        "sourcePathKey": source_path_key(observation.source_path),
        "lineOffset": observation.line_offset,
    }


def _duration_label(minutes: int) -> str:
    hours, mins = divmod(minutes, 60)
    if hours and mins:
        return f"{hours}h {mins}m"
    if hours:
        return f"{hours}h"
    return f"{mins}m"


def _identity_label(identity: QuotaWindowIdentity) -> str:
    return (
        f"{identity.observed_slot} · {_duration_label(identity.window_minutes)}"
        f" · root={identity.source_root_key} · limit={identity.logical_limit_key}"
    )


def _parse_as_of(value: str | None) -> dt.datetime:
    if value is None:
        return _command_as_of().astimezone(UTC)
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise QuotaCLIError(f"invalid --as-of timestamp: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_reset_at(value: str) -> dt.datetime:
    if "T" not in value and "t" not in value:
        raise QuotaCLIError("--reset-at rejects date-only input; include HH:MM")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise QuotaCLIError(f"invalid --reset-at timestamp: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_range_bound(value: str | None, *, display_tz, option: str) -> dt.datetime | None:
    if value is None:
        return None
    try:
        if "T" not in value and "t" not in value:
            date = dt.date.fromisoformat(value)
            return dt.datetime.combine(date, dt.time.min, tzinfo=display_tz).astimezone(UTC)
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise QuotaCLIError(f"invalid {option} timestamp: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise QuotaCLIError(f"{option} datetime must include an offset (or use a date-only value)")
    return parsed.astimezone(UTC)


def _history_in_range(
    history: QuotaHistory, *, since: dt.datetime | None, until: dt.datetime | None,
) -> tuple[QuotaObservation, ...]:
    return tuple(
        observation for observation in history.physical_observations
        if (since is None or observation.captured_at >= since)
        and (until is None or observation.captured_at < until)
    )


def _candidate_text(histories: tuple[QuotaHistory, ...]) -> str:
    if not histories:
        return "  (no active Codex quota identities)"
    return "\n".join(
        "  root-key={root} limit-key={limit}".format(
            root=history.identity.source_root_key,
            limit=history.identity.logical_limit_key,
        )
        for history in histories
    )


def _select_histories(
    histories: tuple[QuotaHistory, ...], *, root_key: str | None, limit_key: str | None,
) -> tuple[QuotaHistory, ...]:
    selected = tuple(
        history for history in histories
        if (root_key is None or history.identity.source_root_key == root_key)
        and (limit_key is None or history.identity.logical_limit_key == limit_key)
    )
    if (root_key is not None or limit_key is not None) and not selected:
        raise QuotaCLIError(
            "no quota identity matches the exact selectors; candidates:\n"
            + _candidate_text(histories)
        )
    return selected


def _sync_and_load(
    args, as_of: dt.datetime, *, current_fresh_only: bool = False,
    reconcile_projection: bool = True,
) -> tuple[QuotaHistory, ...]:
    c = _cctally()
    sync_requested = getattr(args, "sync", None)
    should_sync = (
        bool(sync_requested)
        if sync_requested is not None
        else not getattr(args, "no_sync", False)
    )
    if should_sync:
        cache = c.open_cache_db()
        try:
            cache_mod = c._load_sibling("_cctally_cache")
            _, cache = cache_mod._run_cache_operation_with_recovery(
                cache,
                lambda active_conn: c.sync_codex_cache(active_conn),
                origin="codex.quota.sync",
            )
        finally:
            cache.close()
    # The nested quota leaves heal the durable projection on every read.  The
    # peer percent-breakdown command instead mirrors Claude's materialized-read
    # default; its explicit --sync path still reconciles before rendering.
    if reconcile_projection:
        reconcile_codex_quota_projection(now=as_of)
    observations = load_codex_quota_observations(
        captured_at_or_after=(
            as_of - dt.timedelta(hours=1) if current_fresh_only else None
        ),
    )
    return build_history(observations)


def _command_context(
    args, *, range_args: bool = False, current_fresh_only: bool = False,
    reconcile_projection: bool = True,
):
    c = _cctally()
    config = c._load_claude_config_for_args(args)
    display_tz = c._resolve_display_tz_obj(config)
    as_of = _parse_as_of(getattr(args, "as_of", None))
    since = until = None
    if range_args:
        since = _parse_range_bound(getattr(args, "since", None), display_tz=display_tz, option="--since")
        until = _parse_range_bound(getattr(args, "until", None), display_tz=display_tz, option="--until")
        if since is not None and until is not None and until <= since:
            raise QuotaCLIError("--until must be after --since")
    histories = _sync_and_load(
        args, as_of, current_fresh_only=current_fresh_only,
        reconcile_projection=reconcile_projection,
    )
    selected = _select_histories(
        histories,
        root_key=getattr(args, "root_key", None),
        limit_key=getattr(args, "limit_key", None),
    )
    return as_of, since, until, selected


def _resolve_account_and_scope(args, histories):
    """(#341, spec §3) Resolve ``--account`` (provider=codex) and scope histories.

    Returns ``(account_key | None, exit_code | None, filtered_histories)``:
    ``None`` key = the merged view (byte-identical to today, R8); a resolved ref
    filters to that account's quota identities; a bad ref yields exit 2 (the
    ``account: …`` diagnostic is printed by ``resolve_account_filter``).
    The native quota views read the projection, not the entry cache, so
    ``needs_cache=False``.
    """
    c = _cctally()
    account_key, acct_exit = c.resolve_account_filter(
        args, "codex", needs_cache=False)
    if acct_exit is not None:
        return None, acct_exit, histories
    if account_key is not None:
        histories = type(histories)(
            h for h in histories
            if getattr(h.identity, "account_key", _lib_accounts.UNATTRIBUTED)
            == account_key
        )
    return account_key, None, histories


def _decorate_account(payload: dict[str, object], account_key: "str | None") -> dict:
    """(#341 R8) Add ``accountKey``/``accountLabel`` to a quota payload under an
    explicit ``--account`` invocation; a no-flag render stays byte-identical."""
    if account_key is not None:
        payload.update(_cctally().account_json_fields(account_key))
    return payload


def _emit(args, payload: dict[str, object], text: str) -> int:
    if getattr(args, "json", False):
        print(json.dumps(stamp_schema_version(payload), ensure_ascii=False))
    else:
        print(text)
    return 0


def _command_error(exc: QuotaCLIError) -> int:
    eprint(f"cctally codex quota: {exc}")
    return 2


def cmd_codex_quota_history(args) -> int:
    """Render root-qualified physical local-rollout quota history."""
    try:
        as_of, since, until, histories = _command_context(args, range_args=True)
    except QuotaCLIError as exc:
        return _command_error(exc)
    acct_key, acct_exit, histories = _resolve_account_and_scope(args, histories)
    if acct_exit is not None:
        return acct_exit
    windows = []
    text_rows = ["Codex quota history · local-rollout"]
    for history in histories:
        shown = _history_in_range(history, since=since, until=until)
        if not shown:
            continue
        freshness = quota_freshness(history.physical_observations, as_of)
        windows.append({
            "identity": _identity_wire(history.identity),
            "freshness": _freshness_wire(freshness),
            "orphaned": False,
            "observations": [_observation_wire(observation) for observation in shown],
        })
        text_rows.append(_identity_label(history.identity))
        text_rows.extend(
            "  {at}  {percent:.1f}%  reset {reset}  path {path}".format(
                at=_iso_z(observation.captured_at), percent=observation.used_percent,
                reset=_iso_z(observation.resets_at), path=source_path_key(observation.source_path),
            )
            for observation in shown
        )
    payload = {
        "source": "codex", "generatedAt": _iso_z(as_of),
        "freshnessSource": "local-rollout", "windows": windows,
    }
    _decorate_account(payload, acct_key)  # #341 R8
    if len(text_rows) == 1:
        text_rows.append("No Codex quota history.")
    return _emit(args, payload, "\n".join(text_rows))


def _statusline_status(history: QuotaHistory, as_of: dt.datetime) -> tuple[str, QuotaObservation | None, QuotaFreshness]:
    freshness = quota_freshness(history.physical_observations, as_of)
    current = select_baseline(history.observations, as_of)
    if freshness.state == "future":
        return "future", current, freshness
    if current is None:
        return "unavailable", None, freshness
    if freshness.state == "stale":
        return "stale", current, freshness
    return "ok", current, freshness


def cmd_codex_quota_statusline(args) -> int:
    """Render one truthful native status segment for every selected identity."""
    try:
        as_of, _since, _until, histories = _command_context(args)
    except QuotaCLIError as exc:
        return _command_error(exc)
    acct_key, acct_exit, histories = _resolve_account_and_scope(args, histories)
    if acct_exit is not None:
        return acct_exit
    windows = []
    text_rows = []
    for history in histories:
        status, current, freshness = _statusline_status(history, as_of)
        label = _identity_label(history.identity)
        windows.append({
            "identity": _identity_wire(history.identity),
            "freshness": _freshness_wire(freshness),
            "status": status,
            "current": None if current is None else {
                "usedPercent": current.used_percent, "resetsAt": _iso_z(current.resets_at),
            },
            "label": label,
        })
        if current is None:
            row = f"{label} · unavailable"
        else:
            row = f"{label} · {current.used_percent:.1f}% · resets {_iso_z(current.resets_at)}"
        if status == "future":
            row += " · FUTURE DATA"
        elif status == "stale":
            row += " · STALE"
        text_rows.append(row)
    payload = {
        "source": "codex", "generatedAt": _iso_z(as_of),
        "freshnessSource": "local-rollout", "windows": windows,
    }
    _decorate_account(payload, acct_key)  # #341 R8
    return _emit(args, payload, "\n".join(text_rows) if text_rows else "Codex quota unavailable.")


def _forecast_wire(history: QuotaHistory, as_of: dt.datetime) -> dict[str, object]:
    forecast: QuotaForecast = forecast_quota(history.physical_observations, as_of)
    freshness = quota_freshness(history.physical_observations, as_of)
    return {
        "identity": _identity_wire(history.identity),
        "freshness": _freshness_wire(freshness), "status": forecast.status,
        "currentPercent": forecast.current_percent,
        "ratePercentPerHour": forecast.rate_percent_per_hour,
        "projectedPercent": forecast.projected_percent,
        "resetsAt": _iso_z(forecast.resets_at),
        "remainingSeconds": forecast.remaining_seconds,
        "sampleCount": forecast.sample_count,
        "sampleSpanSeconds": forecast.sample_span_seconds,
        "confidence": forecast.confidence,
    }


def cmd_codex_quota_forecast(args) -> int:
    """Render independent native-window forecasts without quota blending."""
    try:
        as_of, _since, _until, histories = _command_context(args)
    except QuotaCLIError as exc:
        return _command_error(exc)
    acct_key, acct_exit, histories = _resolve_account_and_scope(args, histories)
    if acct_exit is not None:
        return acct_exit
    forecasts = [_forecast_wire(history, as_of) for history in histories]
    text_rows = ["Codex quota forecast · local-rollout"]
    for history, forecast in zip(histories, forecasts):
        label = _identity_label(history.identity)
        current = forecast["currentPercent"]
        projected = forecast["projectedPercent"]
        row = f"{label} · {forecast['status']}"
        if current is not None:
            row += f" · current {float(current):.1f}%"
        if projected is not None:
            row += f" · projected {float(projected):.1f}%"
        text_rows.append(row)
    payload = {
        "source": "codex", "generatedAt": _iso_z(as_of),
        "freshnessSource": "local-rollout", "forecasts": forecasts,
    }
    _decorate_account(payload, acct_key)  # #341 R8
    return _emit(args, payload, "\n".join(text_rows))


def cmd_codex_quota_blocks(args) -> int:
    """Render reset-native quota blocks from the provider-neutral kernel."""
    try:
        as_of, since, until, histories = _command_context(args, range_args=True)
    except QuotaCLIError as exc:
        return _command_error(exc)
    acct_key, acct_exit, histories = _resolve_account_and_scope(args, histories)
    if acct_exit is not None:
        return acct_exit
    blocks = []
    text_rows = ["Codex quota blocks · local-rollout"]
    for block in build_blocks(
        observation for history in histories for observation in history.physical_observations
    ):
        if since is not None and block.last_observed_at < since:
            continue
        if until is not None and block.first_observed_at >= until:
            continue
        blocks.append({
            "identity": _identity_wire(block.identity), "resetAt": _iso_z(block.resets_at),
            "nominalStartAt": _iso_z(block.nominal_start_at),
            "firstObservedAt": _iso_z(block.first_observed_at),
            "lastObservedAt": _iso_z(block.last_observed_at),
            "firstPercent": block.first_percent, "currentPercent": block.current_percent,
            "orphaned": False,
        })
        text_rows.append(
            f"{_identity_label(block.identity)} · {block.first_percent:.1f}% → "
            f"{block.current_percent:.1f}% · reset {_iso_z(block.resets_at)}"
        )
    payload = {
        "source": "codex", "generatedAt": _iso_z(as_of),
        "freshnessSource": "local-rollout", "blocks": blocks,
    }
    _decorate_account(payload, acct_key)  # #341 R8
    if len(text_rows) == 1:
        text_rows.append("No Codex quota blocks.")
    return _emit(args, payload, "\n".join(text_rows))


def cmd_codex_quota_breakdown(args) -> int:
    """Render root-qualified, live-priced milestone deltas for one block."""
    try:
        as_of, _since, _until, histories = _command_context(args)
        acct_key, acct_exit, histories = _resolve_account_and_scope(args, histories)
        if acct_exit is not None:
            return acct_exit
        if len(histories) != 1:
            raise QuotaCLIError(
                "breakdown requires selectors resolving to exactly one quota identity; candidates:\n"
                + _candidate_text(histories)
            )
        reset_at = _parse_reset_at(args.reset_at)
        identity = histories[0].identity
        matching = [
            block for block in build_blocks(histories[0].physical_observations)
            if block.resets_at == reset_at
        ]
        if len(matching) != 1:
            raise QuotaCLIError(
                "--reset-at matches no unique native quota block; candidates:\n"
                + _candidate_text(histories)
            )
    except QuotaCLIError as exc:
        return _command_error(exc)
    c = _cctally()
    speed = c._resolve_codex_speed(args.speed)
    rows = codex_quota_breakdown(identity, reset_at, speed=speed)
    block = matching[0]
    milestones = [
        {
            "percent": row.percent, "capturedAt": _iso_z(row.captured_at),
            "inputTokens": row.input_tokens, "cachedInputTokens": row.cached_input_tokens,
            "outputTokens": row.output_tokens, "reasoningOutputTokens": row.reasoning_output_tokens,
            "totalTokens": row.total_tokens, "costUSD": row.cost_usd,
            "marginalCostUSD": row.marginal_cost_usd,
        }
        for row in rows
    ]
    payload = {
        "source": "codex", "generatedAt": _iso_z(as_of),
        "freshnessSource": "local-rollout", "identity": _identity_wire(identity),
        "block": {"resetAt": _iso_z(block.resets_at), "nominalStartAt": _iso_z(block.nominal_start_at)},
        "speed": speed, "milestones": milestones,
    }
    _decorate_account(payload, acct_key)  # #341 R8
    text_rows = [
        f"Codex quota breakdown · {_identity_label(identity)}",
        f"reset {_iso_z(block.resets_at)} · speed {speed}",
    ]
    text_rows.extend(
        f"{row.percent:>3}%  {row.total_tokens:>8} tokens  ${row.cost_usd:.6f}  Δ${row.marginal_cost_usd:.6f}"
        for row in rows
    )
    if not rows:
        text_rows.append("No percent milestones.")
    return _emit(args, payload, "\n".join(text_rows))


def cmd_codex_percent_breakdown(args) -> int:
    """Render one native seven-day Codex cycle in the Claude table design."""
    try:
        reset_value = getattr(args, "reset_at", None)
        as_of, _since, _until, selected = _command_context(
            args, current_fresh_only=not bool(reset_value),
            reconcile_projection=bool(getattr(args, "sync", False)),
        )
        histories = tuple(
            history for history in selected
            if history.identity.window_minutes == 10_080
        )
        if reset_value:
            reset_at = _parse_reset_at(reset_value)
            matching = tuple(
                (history, block)
                for history in histories
                for block in build_blocks(history.physical_observations)
                if block.resets_at == reset_at
            )
        else:
            # #373: a separate model pool is not account-level standard quota,
            # so it must not make the DEFAULT (no-selector) command ambiguous.
            # Scoped to this branch only — `--reset-at` still reaches a foreign
            # pool's retained history — and it falls back to the unfiltered set
            # when no standard candidate survives, so a lone foreign pool (or an
            # explicit `--limit-key` naming one) still renders rather than
            # turning into an exit-2 for a window the user genuinely has.
            # The baseline is resolved first because it is the §7.1 label
            # authority for classification.
            candidates = tuple(
                (history, select_baseline(history.observations, as_of))
                for history in histories
            )
            standard = tuple(
                (history, baseline) for history, baseline in candidates
                if not codex_history_is_model_scoped(history, baseline=baseline)
            )
            matching = tuple(
                (history, block)
                for history, baseline in (standard or candidates)
                if (
                    baseline is not None
                    and baseline.resets_at > as_of
                    and quota_freshness(
                        history.physical_observations, as_of,
                    ).state == "fresh"
                )
                for block in build_blocks(history.physical_observations)
                # CANONICAL on both sides (#416 §4.1) — see above.
                if block.resets_at == baseline.canonical_resets_at
            )
        if len(matching) != 1:
            raise QuotaCLIError(
                "percent-breakdown matches no unique native 7-day quota cycle; "
                "use exact selectors or --reset-at for retained history; candidates:\n"
                + _candidate_text(histories)
            )
    except QuotaCLIError as exc:
        eprint(f"cctally codex percent-breakdown: {exc}")
        return 2

    c = _cctally()
    speed = c._resolve_codex_speed(args.speed)
    history, block = matching[0]
    try:
        cache = _cache_connection()
    except (FileNotFoundError, sqlite3.Error):
        cache = None
    try:
        rows = codex_quota_breakdown(
            history.identity, block.resets_at, speed=speed, cache_conn=cache,
        )
        five_hour_observations = load_codex_quota_observations(
            source_root_keys={history.identity.source_root_key},
            cache_conn=cache,
            captured_at_or_after=(
                rows[0].captured_at - dt.timedelta(hours=6) if rows else as_of
            ),
        )
        milestone_list = [
            {
                "percentThreshold": row.percent,
                "cumulativeCostUSD": round(row.cost_usd, 9),
                "marginalCostUSD": round(row.marginal_cost_usd, 9),
                "capturedAt": _iso_z(row.captured_at),
                "fiveHourPercentAtCrossing": (
                    round(five_hour_percent, 1)
                    if (five_hour_percent := codex_five_hour_percent_at_crossing(
                        history.identity, row.captured_at, five_hour_observations,
                    )) is not None
                    else None
                ),
            }
            for row in rows
        ]
    finally:
        if cache is not None:
            cache.close()
    week_start = block.nominal_start_at.astimezone(UTC)
    week_end = block.resets_at.astimezone(UTC)
    output = {
        "source": "codex",
        "identity": _identity_wire(history.identity),
        "weekStartDate": week_start.date().isoformat(),
        "weekEndDate": (week_end - dt.timedelta(microseconds=1)).date().isoformat(),
        "weekStartAt": _iso_z(week_start),
        "weekEndAt": _iso_z(week_end),
        "milestones": milestone_list,
        "generatedAt": _iso_z(as_of),
    }
    if args.json:
        print(json.dumps(stamp_schema_version(output), indent=2))
        return 0

    config = c._load_claude_config_for_args(args)
    tz = c.resolve_display_tz(args, config)
    print(c._render_percent_breakdown_terminal(
        week_start_date=output["weekStartDate"],
        week_end_date=output["weekEndDate"],
        display_start_iso=output["weekStartAt"],
        display_end_iso=output["weekEndAt"],
        milestone_list=milestone_list,
        tz=tz,
    ))
    return 0
