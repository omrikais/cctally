"""Codex adapter for durable provider-neutral quota interpretation.

``quota_window_snapshots`` remains cache.db's physical, re-derivable evidence.
This module reads that committed cache after the S1 ingest lock releases and
reconciles an interpreted index in stats.db.  The two databases are not and do
not pretend to be one atomic transaction: a retry always derives a complete new
stats generation from the physical cache.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import math
import os
import secrets
import sqlite3
import sys
import time
from dataclasses import dataclass, replace
from typing import Callable, Iterable, Mapping, NoReturn, Sequence

import _cctally_core
import _lib_accounts
import _lib_codex_window_attribution as _wa
import _lib_quota
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
    "_cctally_quota.py::_codex_projection_witness::quota_projection_state",
    "_cctally_quota.py::_codex_historical_block_spans::quota_window_blocks",
    "_cctally_quota.py::_codex_projected_rows_present::quota_window_blocks",
    "_cctally_diagnosis_sources.py::<module>::quota_window_blocks",
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
    # #1069/#1071: the publication witness and the interleave closure's
    # historical spans are read by the Codex pass while it PREPARES a
    # publication, so like the reads above they must work while the flag is
    # set. Neither renders a projection value, and neither reads a failure as
    # empty: an unreadable witness never compares equal (the leg re-prepares),
    # and unreadable spans are `readable=False`, which brings every affected
    # family whole instead of narrowing the closure.
    "_cctally_quota.py::_codex_projection_witness::quota_projection_state":
        "projector",
    "_cctally_quota.py::_codex_historical_block_spans::quota_window_blocks":
        "projector",
    # #1069 Q4: the empty-store probe reads while the pass prepares, so it is
    # projector machinery too; a denied read reports rows present, which keeps
    # the pass on its ordinary route rather than certifying nothing owed.
    "_cctally_quota.py::_codex_projected_rows_present::quota_window_blocks":
        "projector",
    # `cctally explain` holds its Codex block SQL as a module-level constant,
    # so the function that executes it gates instead.
    "_cctally_diagnosis_sources.py::<module>::quota_window_blocks":
        "gate_at_caller",
}

#: For each `gate_at_caller` site, the `<file>::<function>` callers that run the
#: gate on its behalf. Naming them is what makes the classification checkable:
#: an unnamed caller reduces `gate_at_caller` to an assertion nothing tests, and
#: that is how the first version of this map came to claim a gating caller that
#: neither called the kernel nor called the gate.
#:
#: `_cctally_dashboard_sources.py::_codex_quota_reuse_identity` joined the list
#: with #583 S5. It derives `codex_stats_digest` when its caller cannot supply
#: one, which is the share path and the per-request cycle-detail route — neither
#: of which computes a build signature, so neither had already gated. It gates
#: and, uniquely among the named callers, returns `None` on
#: `QuotaProjectionIncomplete` rather than propagating: it produces a cache KEY,
#: and the fail-safe for an identity that cannot be established is a cold read.
#: The retry signal is not lost, because its callers reach a `gate`-classified
#: projection read within a few statements.
PROJECTION_GATE_CALLERS: "dict[str, tuple[str, ...]]" = {
    "_lib_dashboard_sources.py::<module>::quota_projection_state": (
        "_cctally_tui.py::_tui_build_source_bundle",
        "_cctally_tui.py::_tui_compute_dispatch_signature",
        "_cctally_dashboard_sources.py::_codex_quota_reuse_identity",
    ),
    "_lib_dashboard_sources.py::<module>::quota_window_blocks": (
        "_cctally_tui.py::_tui_build_source_bundle",
        "_cctally_tui.py::_tui_compute_dispatch_signature",
        "_cctally_dashboard_sources.py::_codex_quota_reuse_identity",
    ),
    # Two functions in that module execute module-level SQL against
    # `quota_window_blocks` — the block read and the observation-horizon read —
    # and the scanner sees both as the one `<module>` site. Both are named,
    # because "the horizon read is safe today because of call order" is not a
    # property this guard can check, and a reordering would not fail it.
    "_cctally_diagnosis_sources.py::<module>::quota_window_blocks": (
        "_cctally_diagnosis_sources.py::_read_codex_blocks",
        "_cctally_diagnosis_sources.py::_read_store_horizon",
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
#:
#: THE CENSUS DROPPED FROM 64 TO 50 IN #746. The scanner matched `FROM {` with
#: no SQL context at all, so it counted ordinary English — `f"moved from {was}
#: to {now}"` — and fourteen of the sixty-four entries recorded nothing but
#: prose. Seven whole rows were prose only and are gone; `_cctally_db.py` lost
#: one and `_cctally_journal.py` lost two. The scanner now reconstructs the
#: enclosing SQL expression, following `+` concatenation and module constants,
#: and requires a statement verb earlier in that text than the match. Nothing
#: about which reads are covered changed; what changed is that the count now
#: means what its name says.
# #968 adds eight cache cursor/thread reads, one doctor prune-debt read,
# and two frontier cursor/debt reads. Fixed cursor pairs, thread tuples/TEMP
# views and debt enums cannot reach either quota projection table. This
# census is consumed only by test_selector_state_496_s5b.py; see its audit.
PROJECTION_DYNAMIC_READ_SITES: "dict[str, int]" = {
    "_cctally_account.py": 1,
    # Three, all in `_import_legacy_conversation_rows`: `FROM main.{table}`,
    # `FROM cache_db.{table}` and the `SELECT … FROM cache_db.{table}` of the
    # copy. They were invisible to BOTH guards until #496 S5b gave the read
    # patterns the schema-qualifier prefix the write scan already had, which is
    # the hole `PROJECTION_DYNAMIC_READ_SITES` exists to close. `{table}` there
    # iterates a hardcoded conversation-table tuple, so none of them reaches a
    # projection family and none needs an entry below.
    #
    # Seven, not three, since #769 S3. The four new reads all name the browse
    # rollup or its staging twin — `_recompute_conversation_sessions`' two
    # `SELECT … FROM {target}` derivations plus
    # `_fill_conversation_sessions_filter_columns`' `PRAGMA table_info({target})`
    # and its session-id scan — so a rebuild can derive its replacement
    # generation into `conversation_sessions_staging` without touching what
    # readers are reading (#752). `{target}` substitutes one of exactly two
    # names chosen inside `_cctally_cache`, neither of which is a projection
    # table, so none of them needs an entry below either.
    #
    # Nine, not seven, since #769 S3 tranche 3. `conversation_legacy_bridge_pending`
    # adds `SELECT 1 FROM main.{table}` and `SELECT 1 FROM cache_db.{table}`,
    # which are `_import_legacy_conversation_rows`' own condition read back as
    # a pure predicate so the read-only reader can DETECT that the bridge still
    # has work without performing it (#780). `{table}` iterates the same
    # hardcoded `_LEGACY_BRIDGE_TABLES` tuple as the three reads above, so
    # neither reaches a projection family and neither needs an entry below.
    # #944's `_ClaudeAttributionRun` probe reads `{schema}.sqlite_master` over
    # the hardcoded ("main", "temp") pair: the schema catalog, never a
    # projection family, so it needs no entry below either.
    # Twenty since #927: `_codex_inventory_bootstrap_chunk`'s range probe and
    # its `INSERT … SELECT … FROM {table}` copy iterate the hardcoded
    # `CODEX_IDENTITY_INVENTORY_FAMILIES` tuple (Codex cursors, accounting
    # rows, Codex quota observations, Codex threads), none a projection family.
    # Twenty-five since #970: `_codex_archive_spelled_names` range-seeks the
    # fixed `_CODEX_HOLD_CACHE_FAMILIES` / `_CODEX_HOLD_CONVERSATION_FAMILIES`
    # tuples (Codex cursors, accounting rows, Codex quota observations, Codex
    # threads; the four conversations source families);
    # `_codex_archive_spelled_source_pairs` (the generator
    # `_codex_cursorless_archive_spelled_conversation_sources` consumes, #1046)
    # the fixed messages/events/file-touches triple; `_codex_deletion_witness`
    # one of the two `_VERIFY_TABLES` cursor tables; and
    # `_next_codex_debt_row`'s two keyset reads one of the two literal
    # prune-debt tables. None reaches a projection family, so none needs an
    # entry below.
    # Thirty-three since #1068: the account-scoped rollup reuse reads the
    # `schema_migrations`, `schema_migrations_skipped` and `cache_meta` of
    # `main` or `cache_db` (`_conversation_rollup_epoch`,
    # `_rollup_certification_allowed`, `_scoped_reuse_schema_gate`), the
    # fingerprint stamp reads one row of `{target}`, the browse rollup or its
    # staging twin (`_fill_conversation_sessions_filter_columns`), and
    # `_rewrite_temp_rollup_in_key_order` copies a fixed TEMP rollup table
    # (`conversation_sessions` or `codex_conversation_rollups`) through its
    # temporary twin. None reaches a projection family, so none needs an
    # entry below.
    # Thirty-four since #1041: `_write_conversation_fts_merge_options` reads
    # each present conversation FTS index's `automerge`/`crisismerge` from
    # `main.{table}_config`, where `{table}` iterates the hardcoded
    # `CONVERSATION_FTS_TABLES` triple (the two message indexes and the title
    # index). An FTS5 config shadow is never a projection family, so it needs
    # no entry below.
    # Thirty-five since #787 / #861: `_request_meta_present` (the request
    # writer's ingest-evidence probe) reads `{schema}.cache_meta`, where
    # `{schema}` is `main` or the attached `cache_db` alias: a meta table,
    # never a projection family, so it needs no entry below.
    "_cctally_cache.py": 35,
    # One, not two, since #750 S4: `_get_latest_row_for_week` composed
    # its `WHERE` clauses instead of branching on `as_of_utc`, so its
    # two near-identical `FROM {table_name}` reads became one. The
    # target is still `weekly_usage_snapshots` or
    # `weekly_cost_snapshots`; neither is a projection table.
    "_cctally_core.py": 1,
    "_cctally_dashboard.py": 3,
    "_cctally_dashboard_envelope.py": 5,
    # Six, not seven: `cctally: rebuilt stats.db from {} journal segment(s)` is
    # an operator line, not a statement, and no longer counts. Seven since
    # #927: `_codex_identity_families_empty` probes the same hardcoded
    # `CODEX_IDENTITY_INVENTORY_FAMILIES` tuple, none a projection family.
    # Nine since #1068: `migration_fence_present` probes the `sqlite_master`
    # and `cache_meta` of `main` or `cache_db` for a migration fence row.
    "_cctally_db.py": 9,
    "_cctally_doctor.py": 2,
    "_cctally_five_hour.py": 1,
    # Sixteen, not eighteen: the two `event {} rev {} conflicts with effective
    # rev {} from {}` refusals are prose and no longer count.
    #
    # Seventeen since #869: `latch_crossing_was_retired` reads a latch row's
    # natural-key columns with `SELECT … FROM {spec.table} WHERE id = ?` so a
    # live writer can ask whether harvest will journal it as a re-fire. It
    # accepts only the `budget`, `projected` and `project_budget` harvest
    # specs (`_LATCH_REFIRE_ID_PREFIXES`), so `{spec.table}` is always
    # `budget_milestones`, `projected_milestones` or
    # `project_budget_milestones`. None is a projection table, so it needs no
    # entry below.
    #
    # Nineteen since #825/#832 fix round E: `_rematerialize_scratch_row`'s
    # row lookup by journal id and `_delete_scratch_row`'s removal by row id
    # touch only a derivation-completion SCRATCH projection, and the table
    # they interpolate is always an `_EVT_SPECS` table (the snapshot, reset,
    # block and milestone families). None is a projection table, so neither
    # needs an entry below.
    #
    # Twenty since #956 class 2: `_remove_opened_block` deletes a tombstoned
    # opening's open block's child rows `FROM {child_table}`, iterating the
    # fixed `_BLOCK_CHILDREN` pair (`five_hour_block_models`,
    # `five_hour_block_projects`) on a derivation-completion scratch. Neither
    # is a projection table, so it needs no entry below.
    "_cctally_journal.py": 20,
    "_cctally_pricing_check.py": 1,
    # One, in `_read_stats_component`: a single `SELECT {column} FROM
    # {table}` shared by `week_reset_events` and `weekly_credit_floors`,
    # the two authoritative credit tables. Neither is a projection table,
    # so no `PROJECTION_DYNAMIC_READ_ACTIONS` classification applies.
    "_cctally_quota_model.py": 1,
    "_cctally_quota.py": 1,
    # Two, both #825/#832 revision 5 per-account row counts:
    # `_account_rows`' `SELECT COUNT(*) FROM {table}` behind the projection's
    # classification evidence, and `_tokens_from`'s `SELECT COUNT(*),
    # MAX(rowid) FROM {table}` behind a waiting head's dependency tokens
    # (`_wait_tokens` before fix round K13, which shares it with the
    # activating event's non-waiting read).
    # `{table}` is always one of the hardcoded `weekly_usage_snapshots`,
    # `week_reset_events`, `weekly_credit_floors`, `five_hour_reset_events`
    # or `five_hour_blocks`; neither site reaches a quota projection table, so
    # no `PROJECTION_DYNAMIC_READ_ACTIONS` classification applies.
    "_cctally_statusline.py": 2,
    # Eight, all new with #787 / #861: the query session's readiness reads.
    # `_migration_state` reads `{schema}.schema_migrations` and
    # `{schema}.schema_migrations_skipped`, `_applied_names`
    # `{schema}.schema_migrations`, `_cache_meta_value` and
    # `_codex_maintenance_debt` `{schema}.cache_meta`, `_codex_maintenance_debt`
    # `{schema}.sqlite_master` and `{schema}.codex_prune_debt`, and
    # `_read_store` `{schema}.sqlite_master`. `{schema}` is `main` or one
    # fixed attachment alias. None is a projection family, so none needs an
    # entry below.
    "_cctally_store_access.py": 8,
    # One, in `_codex_rollup_revision`: the table is selected from a hardcoded
    # pair of Codex conversation rollups. Neither is a quota projection table,
    # so no `PROJECTION_DYNAMIC_READ_ACTIONS` classification applies.
    "_lib_codex_conversation_query.py": 1,
    # Two since #1094: `_any_content`'s `SELECT 1 FROM main.{content}` and
    # `_source_bytes`'s `SELECT coalesce(sum(…)) FROM main.{content}`, where
    # `{content}` iterates the hardcoded `CONTENT_TABLES` triple
    # (`conversation_messages`, `codex_conversation_messages`,
    # `conversation_ai_titles`) of `conversations.db`. None is a projection
    # table, so no `PROJECTION_DYNAMIC_READ_ACTIONS` classification applies.
    "_lib_conversation_fts_integrity.py": 2,
    # Four since #1093: `_search_title` names each path's title source once
    # and interpolates it into the shared matched-title page template
    # (`FROM {source}`) and into that path's count (`SELECT COUNT(*) FROM
    # {source}`). `{source}` is one of two hardcoded strings, the FTS path's
    # `conversation_title_fts f JOIN conversation_ai_titles t` or the LIKE
    # path's `conversation_ai_titles t`. Neither is a projection table, so no
    # `PROJECTION_DYNAMIC_READ_ACTIONS` classification applies.
    "_lib_conversation_query.py": 4,
    # Four, not two, since #901's paced retention (`f2077468a`, spec §5.4):
    # `group_row_count` counts the rows a deletion's reservation charges with
    # `SELECT COUNT(*) FROM {table}` in its two shapes (by identity key, and by
    # source path for a NULL-identity group). `{table}` comes from
    # `_GROUP_TABLES`, a hardcoded pair (`conversation_messages`,
    # `codex_conversation_events`); neither is a quota projection table, so no
    # `PROJECTION_DYNAMIC_READ_ACTIONS` classification applies.
    "_lib_conversation_retention.py": 4,
    # Five: `_target_has_cursor_gap` selects one of the two accounting source
    # tables, `_conversation_source_paths` and `_conversation_target_risk`
    # select one of the two transcript source tables, and #769 S6's
    # `select_recently_active_codex_paths` reads whichever Codex cursor table
    # its caller owns — twice, once for the recency predicate and once for the
    # ticket-named paths that predicate missed. That choice is confined to
    # `_CODEX_CURSOR_TABLES` and validated against it before the query is
    # built. Six, not five, since #769 S6 T2a's `32b8eaa0a`: `_source_paths`
    # now reads `f"SELECT path FROM {table}"`, where `_ingest_source_table`
    # returns `session_files` or `codex_session_files` in place of the literal
    # pair the planner used to spell at its own call site. That function is
    # total over those two names and raises on anything else. Every choice here
    # is hardcoded; none is a quota projection table, so no
    # `PROJECTION_DYNAMIC_READ_ACTIONS` classification applies.
    "_lib_ingest_frontier.py": 8,
    "_lib_snapshot_cache.py": 1,
    # Three (#1050), each over a hardcoded Claude stats table set:
    # `_trigger_sql`'s below-max INSERT predicate `SELECT 1 FROM {table}` and
    # `trigger_lookup_queries`' `EXPLAIN` twin of it range over the kernel's
    # `TRACKED_TABLES`, and `child_set_unchanged`'s `SELECT * FROM {table}`
    # over `CHILD_TABLES` (the two five-hour block child tables), checked
    # before the query is built. None is a quota projection table, so no
    # `PROJECTION_DYNAMIC_READ_ACTIONS` classification applies.
    "_lib_stats_content_revision.py": 3,
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

#: #1070 (spec §5.6, Q16): the non-blocking flock serializing the marker's
#: check and stamp, so concurrent hooks that each observe pending alert work
#: reserve ONE spawn between them — a hook that loses it is ``throttled``.
CODEX_QUOTA_VERIFY_RESERVATION_NAME = "codex-quota-verify.spawn.lock"

#: #1070 (spec §5.6, Q12): the durable pending-work sidecar of the deferred
#: Codex quota alert activation. An activation that needs complete root
#: histories (a delivery-gate enable, a rule change, an owned or legacy
#: scheduled evaluation) never runs on the hook; the hook records it here with
#: its own ``now`` as the per-root cutoff and the ``_codex-quota-verify``
#: worker completes it. In ``APP_DIR`` rather than stats.db so a stats rebuild
#: preserves it, and separate from the spawn marker because pending work is
#: independent of spawn attempts. Mutated only under ``journal.ingest.lock``.
CODEX_QUOTA_ALERT_OBLIGATIONS_NAME = "codex-quota-alert-obligations.json"
CODEX_QUOTA_ALERT_OBLIGATIONS_VERSION = 1
_ALERT_OBLIGATION_REASONS = frozenset({
    "gate_enabled", "rule_changed", "scheduled", "scheduled_legacy"})


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
    #: What the reconcile actually did (#1069). Every return of
    #: ``reconcile_codex_quota_projection`` sets it explicitly; the default
    #: stays ``"published"`` only for the dataclass's other constructors.
    #:
    #: * ``"published"`` — the leg applied and the cycle committed (scoped,
    #:   root-complete or whole-history), with its generation and counts.
    #: * ``"current"`` — nothing was owed: a certificate-backed short-circuit,
    #:   a superseded preparation that re-prepared to nothing, or an empty store.
    #: * ``"deferred_verify"`` — whole-history work was handed to the
    #:   ``_codex-quota-verify`` worker and nothing was applied.
    #: * ``"deferred_model"`` — the #980 model-resolution fence refused it (D6):
    #:   no projection rows and no certificate, the drain executor requested.
    #: * ``"contended"`` — the ingest never ran: its lock was refused
    #:   (``run_stats_ingest`` returned ``ran=False``), or the call was nested
    #:   inside a held ingest (#1076 D4).
    #: * ``"unavailable"`` — the cache could not be read, or (#1070) the pass
    #:   was withheld because its alert obligation could not be recorded.
    status: str = "published"
    #: #1070 (spec §5.6, Q13): complete-history alert work is still owed — a
    #: recorded or merged obligation, or a damaged obligation sidecar. Separate
    #: from ``status`` so a committed bounded publication keeps its real status
    #: and counts; the hook never acknowledges lifecycle success while it is
    #: set and requests the verify worker once its lifecycle locks are released.
    alert_work_pending: bool = False
    #: Why alert work is still pending when the obligation sidecar itself is
    #: the blocker: ``"alert_obligations_unwritable"`` (a recording, a
    #: supersession or a removal failed) or ``"alert_obligations_unreadable"``
    #: (a damaged sidecar); empty otherwise. The worker's log token.
    alert_work_issue: str = ""


#: #980 D6: the status a refused projection publisher returns.
PROJECTION_DEFERRED_MODEL = "deferred_model"
#: #1069 (spec §5.1): the remaining explicit outcomes.
PROJECTION_PUBLISHED = "published"
PROJECTION_CURRENT = "current"
PROJECTION_DEFERRED_VERIFY = "deferred_verify"
PROJECTION_CONTENDED = "contended"
PROJECTION_UNAVAILABLE = "unavailable"


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


def _cache_connection():
    """A read-only cache reader without invoking or re-running ingest.

    #787 / #861 AD21: every statement is one query session (an explicit
    ``BEGIN`` ... ``COMMIT`` bracket is one), never the startup phase: a
    missing store is still ``FileNotFoundError`` and a store that is not
    readable is a typed `StoreUnavailable` (a ``sqlite3.DatabaseError``)."""
    path = _cctally_core.CACHE_DB_PATH
    if not path.exists():
        raise FileNotFoundError(path)
    import _cctally_store_access as _store_access
    return _store_access.statement_reader(
        "cache", row_factory=sqlite3.Row, startup=False)


def _cache_connection_owner() -> sqlite3.Connection:
    """The maintenance owner's raw read-only cache connection (the rebuild's
    rematerialization runs under its own maintenance authority)."""
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


def _codex_quota_projection_certificate_payload(
    conn: sqlite3.Connection,
) -> "dict | None":
    """The stored certificate as written, with no revision gate applied.

    Separate from the reader below because the reconcile needs to know WHY a
    certificate is not usable: an attribution-stale one has to widen the pass to
    the attribution's own groups, while an absent one is an ordinary full pass.
    """
    try:
        row = conn.execute(
            "SELECT value FROM cache_meta WHERE key=?",
            (_DASHBOARD_PROJECTION_CERTIFICATE_KEY,),
        ).fetchone()
        if row is None:
            return None
        payload = json.loads(str(row[0]))
    except (sqlite3.Error, TypeError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _certificate_attribution_revision(payload: "Mapping[str, object] | None") -> int:
    """The attribution revision a stored certificate was computed against.

    A certificate written before #500 carries no such member and reads as ``0``,
    which is also the revision of a store where nothing has ever been asserted —
    so an install with no attributions keeps its existing certificate and its
    existing short-circuit unchanged.
    """
    if not payload:
        return 0
    try:
        return int(payload.get("attributionRevision", 0))
    except (TypeError, ValueError):
        return 0


def load_codex_quota_projection_certificate(
    conn: sqlite3.Connection,
) -> tuple[int, dict[str, str]] | None:
    """Read the post-reconciliation physical-signature certificate in O(1).

    Also gated on the ATTRIBUTION REVISION (#500 spec §8.3). This design writes
    nothing to ``quota_window_snapshots``, so an operator attribution fires no
    trigger and appears in no change-ledger entry; left alone, every consumer of
    this certificate would read a confident no-op and the attribution would never
    reach ``quota_window_blocks`` until some unrelated future ingest happened to
    dirty the same group. Returning ``None`` when the stored revision is not the
    live one puts every consumer — the reconcile short-circuit, the dashboard
    source coherence check, the deferred cache-sync gate — on the fail-safe side
    of that question at once.
    """
    payload = _codex_quota_projection_certificate_payload(conn)
    if payload is None:
        return None
    try:
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
    except (sqlite3.Error, TypeError, ValueError, KeyError):
        return None
    if sequence < 0 or any(len(signature) != 64 for signature in signatures.values()):
        return None
    if _certificate_attribution_revision(payload) != (
        _cache_module().codex_window_attribution_revision(conn)
    ):
        return None
    return sequence, signatures


def _store_codex_quota_projection_certificate(
    *,
    sequence: int,
    signatures: Mapping[str, str],
    prune_ledger_through: "int | None" = None,
    attribution_revision: "int | None" = None,
    expected_ledger_high: "int | None" = None,
    delete_on_mismatch: bool = False,
) -> str:
    """Prune the consumed ledger and stamp the certificate, behind fences.

    Returns ``"stored"``, ``"fenced"`` or ``"skipped"``.

    The fences are checked FIRST, in the one ``BEGIN IMMEDIATE`` (#1069): the
    live physical sequence must equal ``sequence``; the live attribution
    revision must equal ``attribution_revision`` (#500 §8.3, the semantic twin
    of the sequence, which needs no concurrency to move — the pass's OWN ingest
    cycle materializes a pending ``codex_window_attribution`` op after the
    observations are loaded); when ``expected_ledger_high`` is given, the live
    ledger head must equal it; and no model-resolution debt may stand (#980
    D6: no certificate while resolution is owed). ``None`` means no gate for
    the first two, for callers that are not a projection pass.

    All equal: ``prune_ledger_through`` (public #5) deletes the consumed
    change-ledger entries and the certificate is stored — ``"stored"``.
    Entries at or below the committed watermark are provably consumed, and
    ``seq`` is ``AUTOINCREMENT``, so a pruned high value is never reissued.

    Any mismatch: nothing is pruned — the entries above a moved head are not
    this publication's, and pruning before checking let a superseded pass
    delete the entries a newer one still owed — and, with
    ``delete_on_mismatch``, the stored certificate is deleted, because the
    publication just committed is the newest and no stored certificate
    describes it. ``"fenced"``.

    A missing cache or any ``sqlite3.Error`` rolls back and returns
    ``"skipped"``; pruning and certification stay recoverable work for the
    next finalization.
    """
    path = _cctally_core.CACHE_DB_PATH
    if not path.exists():
        return "skipped"
    try:
        conn = sqlite3.connect(path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            live_revision = _cache_module().codex_window_attribution_revision(
                conn)
            fenced = (
                codex_physical_mutation_seq(conn) != sequence
                or (attribution_revision is not None
                    and live_revision != int(attribution_revision))
                or (expected_ledger_high is not None
                    and _ledger_max_seq(conn) != int(expected_ledger_high))
                or _cache_module().codex_model_resolution_pending(conn)
            )
            if fenced:
                if delete_on_mismatch:
                    conn.execute(
                        "DELETE FROM cache_meta WHERE key=?",
                        (_DASHBOARD_PROJECTION_CERTIFICATE_KEY,))
                conn.commit()
                return "fenced"
            if prune_ledger_through:
                try:
                    conn.execute(
                        "DELETE FROM quota_window_change_log WHERE seq <= ?",
                        (int(prune_ledger_through),),
                    )
                except sqlite3.OperationalError:
                    pass  # a cache too old to carry the ledger
            payload = json.dumps({
                "interpretationVersion": _CODEX_QUOTA_INTERPRETATION_VERSION,
                "sequence": sequence,
                "signatures": dict(sorted(signatures.items())),
                # #500 §8.3: the semantic revision this projection was computed
                # against — the caller's, verified equal to the live one above.
                "attributionRevision": live_revision,
            }, sort_keys=True, separators=(",", ":"))
            conn.execute(
                "INSERT INTO cache_meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (_DASHBOARD_PROJECTION_CERTIFICATE_KEY, payload),
            )
            conn.commit()
            return "stored"
        except BaseException:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()
    except sqlite3.Error:
        return "skipped"


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
    return _projection_signatures_match_rows(
        ((row[0], row[2]) for row in rows), active_roots, cert_sigs)


def _projection_signatures_match_rows(
    rows: "Iterable[tuple[object, object]]",
    active_roots: "Iterable[str]",
    cert_sigs: Mapping[str, str],
) -> bool:
    """The decision of ``_stats_projection_signatures_match`` over rows already
    read as ``(source_root_key, physical_signature)`` — the preparation computes
    it from its witness, so the verdict and the witness are one stats read."""
    by_root: dict[str, set[str]] = {}
    for root_key, signature in rows:
        by_root.setdefault(str(root_key), set()).add(str(signature))
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


@dataclass(frozen=True)
class _AlertObligations:
    """One read of the alert-obligation sidecar (#1070, spec §5.6).

    ``state`` is ``"absent"`` (no request), ``"readable"``, ``"unreadable"``
    (an ``OSError`` on read, invalid JSON or an invalid request) or
    ``"unsupported"`` (a ``version`` this binary does not know). The last two
    are DAMAGED: their bytes are preserved and their contents count as unknown
    pending work for every applicable root, because stats cannot prove the
    absence of an obligation — journal replay restores an arming row and its
    fingerprint even when the activation that wrote them crashed before its
    historical suppressions (Appendix B round 4).
    """

    state: str
    requests: "tuple[dict, ...]" = ()

    @property
    def damaged(self) -> bool:
        return self.state in ("unreadable", "unsupported")


def _alert_obligations_path():
    return _cctally_core.APP_DIR / CODEX_QUOTA_ALERT_OBLIGATIONS_NAME


def _valid_alert_obligation(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    try:
        if not isinstance(value["id"], str) or not value["id"]:
            return False
        if value["reason"] not in _ALERT_OBLIGATION_REASONS:
            return False
        if not isinstance(value["config_fingerprint"], str):
            return False
        roots = value["roots"]
        if not isinstance(roots, list) or not all(
                isinstance(root, str) and root for root in roots):
            return False
        cutoffs = value["cutoffs"]
        if not isinstance(cutoffs, dict):
            return False
        for root, cutoff in cutoffs.items():
            if not isinstance(root, str) or not root:
                return False
            _parse_utc(str(cutoff), "alert obligation cutoff")
        _parse_utc(str(value["created_at"]), "alert obligation created_at")
    except (KeyError, TypeError, ValueError):
        return False
    return True


def _read_codex_alert_obligations() -> _AlertObligations:
    """Read the sidecar into one of its three distinct states; never raises."""
    try:
        raw = _alert_obligations_path().read_bytes()
    except FileNotFoundError:
        return _AlertObligations("absent")
    except OSError:
        return _AlertObligations("unreadable")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return _AlertObligations("unreadable")
    if not isinstance(document, dict):
        return _AlertObligations("unreadable")
    version = document.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        return _AlertObligations("unreadable")
    if version != CODEX_QUOTA_ALERT_OBLIGATIONS_VERSION:
        return _AlertObligations("unsupported")
    requests = document.get("requests")
    if not isinstance(requests, list) or not all(
            _valid_alert_obligation(request) for request in requests):
        return _AlertObligations("unreadable")
    return _AlertObligations(
        "readable", tuple(dict(request) for request in requests))


def _write_codex_alert_obligations(requests: "Iterable[dict]") -> None:
    """Durably replace the sidecar's request set; raises ``OSError``.

    The order of ``_cctally_journal._atomic_write_json`` — a temporary file in
    ``APP_DIR``, its fsync, ``os.replace``, then the fsync of ``APP_DIR`` —
    through the journal's own ``_fsync_dir``. That writer is tied to the
    account-bound directory, so it is not called. A request is the hand-off's
    only record of its cutoff: a machine crash after the bounded publication
    commits must not lose it, so a caller treats ANY failure here as "not
    recorded" (spec §5.6, Appendix B round 4 P2).
    """
    import _cctally_journal as _jr

    app_dir = _cctally_core.APP_DIR
    app_dir.mkdir(parents=True, exist_ok=True)
    target = app_dir / CODEX_QUOTA_ALERT_OBLIGATIONS_NAME
    data = (json.dumps(
        {"version": CODEX_QUOTA_ALERT_OBLIGATIONS_VERSION,
         "requests": list(requests)},
        sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    temporary = app_dir / (
        f".{CODEX_QUOTA_ALERT_OBLIGATIONS_NAME}.{os.getpid()}.tmp")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("short write to the alert obligation sidecar")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, target)
    except OSError:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    _jr._fsync_dir(app_dir)


def _cancel_codex_alert_obligations() -> None:
    """End the delivery generation (a live disarm executed); raises ``OSError``.

    A damaged file is first moved aside to ``<name>.unreadable-<UTC stamp>``
    (its bytes kept for diagnosis), then the empty request set is written. An
    absent file stays absent.
    """
    state = _read_codex_alert_obligations()
    if state.state == "absent":
        return
    if state.damaged:
        source = _alert_obligations_path()
        stamp = dt.datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        os.replace(source, source.with_name(f"{source.name}.unreadable-{stamp}"))
    elif not state.requests:
        return
    _write_codex_alert_obligations(())


def _codex_alert_config_fingerprint(alert_config: "QuotaAlertConfig") -> str:
    """A digest of one resolved alert-configuration snapshot (§5.2): a request
    belongs to the configuration generation it was recorded under."""
    global_enabled, quota_enabled, _rules, config = alert_config
    payload = json.dumps(
        [bool(global_enabled), bool(quota_enabled), config],
        sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _alert_obligation_owed_roots(request: dict, applicable) -> "set[str]":
    """The roots a request owes among ``applicable``: its own roots, or every
    applicable root for a global request (empty ``roots``)."""
    roots = request["roots"]
    return set(applicable) if not roots else set(roots) & set(applicable)


def _alert_obligations_newer_than(
    requests: "Sequence[dict]", fingerprint: str, now_iso: str,
) -> bool:
    """Whether a request under another fingerprint was created after
    ``now_iso``: the leg holding ``now_iso`` is then stale (spec §5.6, Q5).
    An unparsable instant compares as not newer, which keeps the ordinary
    supersession (the reader already rejects such a sidecar as damaged)."""
    try:
        now = _parse_utc(str(now_iso), "alert obligation now")
    except (TypeError, ValueError):
        return False
    for request in requests:
        if request["config_fingerprint"] == fingerprint:
            continue
        try:
            created = _parse_utc(
                str(request["created_at"]), "alert obligation created_at")
        except (TypeError, ValueError):
            continue
        if created > now:
            return True
    return False


def _merge_codex_alert_obligations(
    requests: "Sequence[dict]", *, derived, fingerprint: str,
    observed_roots, now_iso: str,
) -> "tuple[list[dict], bool]":
    """Merge one eligible pass's view into the pending requests (spec §5.6).

    ``observed_roots`` are the roots this pass is the eligible observer of.
    A request recorded under another configuration fingerprint is superseded:
    a new id, this fingerprint and this pass's ``now`` as every cutoff — a
    configuration change never borrows the old generation's boundary. An owed
    root without a cutoff gets this pass's ``now``; an existing cutoff never
    moves. Each derived ``(reason, roots)`` keeps a matching request (same
    reason and fingerprint; new roots join it with this cutoff) or creates
    one. Returns the merged list and whether anything changed.

    A STALE leg changes nothing: one whose ``now`` precedes the creation of
    a request recorded under another fingerprint was prepared before that
    configuration existed (Q5 re-reads no configuration in the lock), so its
    view is older than the request's. Superseding there would replace the
    newer generation's first validated hand-off with this earlier leg's
    fingerprint, and the next current hook would supersede again at its own
    later ``now``: the activation cutoff would move past the first hand-off.
    The comparison is between parsed instants, because the writers' spellings
    (``…Z`` against ``….500000+00:00``) do not sort in time order.
    """
    observed = set(observed_roots)
    if _alert_obligations_newer_than(requests, fingerprint, now_iso):
        return [dict(request) for request in requests], False
    merged: "list[dict]" = []
    changed = False
    for request in requests:
        request = {**request, "cutoffs": dict(request["cutoffs"])}
        if request["config_fingerprint"] != fingerprint:
            request = {
                **request, "id": secrets.token_hex(16),
                "config_fingerprint": fingerprint,
                "cutoffs": {
                    root: now_iso for root in (
                        set(request["cutoffs"])
                        | _alert_obligation_owed_roots(request, observed))},
                "created_at": now_iso,
            }
            changed = True
        merged.append(request)
    for reason, roots in derived:
        roots = sorted(set(roots))
        target = next(
            (request for request in merged
             if request["reason"] == reason
             and request["config_fingerprint"] == fingerprint
             and (not request["roots"]) == (not roots)), None)
        if target is None:
            merged.append({
                "id": secrets.token_hex(16), "reason": reason,
                "config_fingerprint": fingerprint, "roots": roots,
                "cutoffs": {}, "created_at": now_iso,
            })
            changed = True
        elif roots and not set(roots) <= set(target["roots"]):
            target["roots"] = sorted(set(target["roots"]) | set(roots))
            changed = True
    for request in merged:
        for root in sorted(_alert_obligation_owed_roots(request, observed)):
            if root not in request["cutoffs"]:
                request["cutoffs"][root] = now_iso
                changed = True
    return merged, changed


def _deferred_alert_obligations(
    alert_scope, detail: dict, applicable: "frozenset[str]",
) -> "tuple[tuple[str, frozenset[str]], ...]":
    """The complete-history alert work a ``"defer"`` pass hands to the worker
    (spec §5.6, Q15): a delivery-gate enable (including a rebuild's NULL gate),
    a rule change, an owned scheduled instant and a legacy scalar boundary
    (``REASON_SCHEDULED_DEFERRED``, which a reporting-only verifier can never
    discharge). Root-scoped work is narrowed to the roots this pass is the
    eligible observer of, so a historic root's stale fingerprint can never
    become an obligation nobody holds a lifecycle lock for."""
    if alert_scope is None or alert_scope.disarm_all or not applicable:
        return ()
    reasons = set(alert_scope.reasons)
    obligations: "list[tuple[str, frozenset[str]]]" = []
    if "gate_enabled" in reasons:
        obligations.append(("gate_enabled", frozenset()))
    changed = frozenset(detail.get("changed_roots", ())) & applicable
    if "rule_changed" in reasons and changed:
        obligations.append(("rule_changed", changed))
    due = frozenset(detail.get("scheduled_roots") or ()) & applicable
    if "scheduled" in reasons and due:
        obligations.append(("scheduled", due))
    if _axes.REASON_SCHEDULED_DEFERRED in reasons:
        obligations.append(("scheduled_legacy", frozenset()))
    return tuple(obligations)


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
    # #1070 (spec §5.6, Q16): every hook that observes pending alert work
    # requests the worker, so the marker's check and stamp are serialized by a
    # non-blocking flock — concurrent hooks reserve ONE spawn, and the loser is
    # `throttled` without stamping or spawning.
    try:
        _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)
        reservation = os.open(
            _cctally_core.APP_DIR / CODEX_QUOTA_VERIFY_RESERVATION_NAME,
            os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        _log_codex_worker_outcome(
            "quota-verify-spawn", "failed", "reason=marker_unwritable")
        return "failed"
    try:
        try:
            fcntl.flock(reservation, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return "throttled"
        try:
            age = time.time() - marker.stat().st_mtime
        except OSError:
            age = None
        if age is not None and 0 <= age < CODEX_QUOTA_VERIFY_SPAWN_THROTTLE_SECONDS:
            return "throttled"
        try:
            marker.touch()
        except OSError:
            _log_codex_worker_outcome(
                "quota-verify-spawn", "failed", "reason=marker_unwritable")
            return "failed"
    finally:
        os.close(reservation)  # closing the descriptor releases the flock
    from _cctally_update import _spawn_detached
    if _spawn_detached(CODEX_QUOTA_VERIFY_COMMAND):
        _log_codex_worker_outcome("quota-verify-spawn", "spawned")
        return "spawned"
    _log_codex_worker_outcome("quota-verify-spawn", "failed", "reason=spawn")
    return "failed"


def _request_codex_model_resolution() -> None:
    """A refused publisher asks the drain executor to resolve models (D6).

    Best effort and attempt-throttled by the executor's own spawn marker; the
    executor requests ``_codex-quota-verify`` once it has cleared the flag, so
    the refused projection is republished."""
    try:
        cache_module = _cache_module()
        cache_module._request_codex_drain_worker(
            cache_module.CODEX_PRUNE_DRAIN_COMMAND)
    except Exception:
        pass


def cmd_codex_quota_verify_internal(args) -> int:
    """Hidden ``_codex-quota-verify`` handler: the deferred whole-history pass,
    and (#1070, spec §5.6) the deferred Codex quota alert activation.

    It reads the alert-obligation sidecar first. With no request it is
    reporting only — no ``alert_eligible_root_keys``, because alert eligibility
    belongs to whoever holds the per-root lifecycle lock — which matches every
    other off-hook caller of the verification (the dashboard tick,
    ``codex quota``): the worker is one of those callers, moved off the
    blocking path.

    With pending requests it is the activation the hook could not afford (a
    complete-history pass took 32 s against Codex's 30 s hook kill, §4.2 M1):
    it resolves the configured roots exactly as the hook does, takes the owed
    roots' REAL lifecycle locks non-blocking and all or nothing
    (``acquire_due_lifecycle_locks(throttle_seconds=0)``), holds them through
    evaluation, commit and dispatch, and never calls ``mark_lifecycle_success``
    (it ran neither the hook's sync nor its budget leg). The pass applies each
    request's cutoff, and the completed requests are removed after the commit.
    A partial acquisition releases everything and leaves the requests pending.
    A damaged sidecar keeps the worker reporting-only, so the projection stays
    fresh while the activation waits for the disarm that ends its generation.

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
    One aggregate line per invocation (Q16): ``result=success`` only when the
    verification and every applicable requested alert work item completed.
    """
    started = time.monotonic()
    # #1076 D5: every reconcile this invocation runs reports the Codex budget
    # reads its off-hook ingest deferred; the line names the first one.
    deferrals: "list[str]" = []

    def _log(outcome: str, detail: str = "", *, error: str = "") -> None:
        _log_codex_worker_outcome(
            "quota-verify", outcome,
            f"dur_ms={max(0, int((time.monotonic() - started) * 1000))}"
            + (f" {detail}" if detail else "")
            + (f" budget_deferral={deferrals[0]}" if deferrals else ""),
            error=error)

    def _counts(result) -> str:
        return (
            f"blocks={int(getattr(result, 'blocks_upserted', 0) or 0)} "
            f"milestones={int(getattr(result, 'milestones_upserted', 0) or 0)}")

    now = _command_as_of().astimezone(UTC)
    obligations = _read_codex_alert_obligations()
    if obligations.damaged:
        # Reporting-only, and a failed hand-off by definition: the requested
        # activation cannot even be read, so it cannot have completed.
        #
        # NOT `force_full`: the damage keeps alert work pending until a
        # disarm, so every hook requests this worker again (throttled to
        # `CODEX_QUOTA_VERIFY_SPAWN_THROTTLE_SECONDS`), and a forced pass
        # re-materialized the whole history (about 30 s of CPU natively) on
        # every run. An ordinary inline pass keeps every reason this worker
        # must run a complete one: the periodic verification once
        # `_full_verification_due` says so, and every catch-all route of
        # `_resolve_pass_scope` (an absent, rebuilt or version-stale projector
        # state, a reset ledger, a missing reverse map, a burst); otherwise it
        # is bounded or short-circuits. A disarm needs no observations, so it
        # is never short-circuited away (`has_arming`).
        try:
            result = reconcile_codex_quota_projection(
                now=now, _read_deferrals=deferrals)
        except Exception as exc:
            from _cctally_record import _hook_log_error_detail
            _log("error", "mode=verify projection=alert_obligations_unreadable",
                 error=_hook_log_error_detail(exc))
            return 0
        status = getattr(result, "status", None) or PROJECTION_PUBLISHED
        after = _read_codex_alert_obligations()
        if after.damaged or after.requests:
            _log("error",
                 f"mode=verify projection=alert_obligations_unreadable "
                 f"status={status} {_counts(result)} pending=1")
            return 0
        # The pass executed a live disarm, which ended the damaged
        # generation (the file moved aside, an empty request set written):
        # nothing is owed any more, so this is an ordinary verification.
        detail = f"mode=verify {_counts(result)} pending=0"
        if status == PROJECTION_DEFERRED_MODEL:
            _log("deferred", f"projection=deferred {detail}")
        elif status not in (PROJECTION_PUBLISHED, PROJECTION_CURRENT):
            _log("error", f"projection={status} {detail}")
        else:
            _log("success", f"projection={status} {detail}")
        return 0
    if obligations.requests:
        return _run_codex_alert_activation(
            obligations, now=now, log=_log, counts=_counts,
            read_deferrals=deferrals)

    try:
        result = reconcile_codex_quota_projection(
            force_full=True, now=now, _read_deferrals=deferrals)
    except Exception as exc:
        # Same defusing as the lifecycle line, for the same two reasons: the
        # `OSError` family's `str()` carries a rollout path, and the log's
        # reader is a last-wins `k=v` comprehension a free-text `=` can beat.
        # `_log_codex_worker_outcome` performs it; this narrows the `filename`
        # away first, which only the caller holding the exception can do.
        from _cctally_record import _hook_log_error_detail
        _log("error", error=_hook_log_error_detail(exc))
        return 0
    status = getattr(result, "status", None) or PROJECTION_PUBLISHED
    if status == PROJECTION_DEFERRED_MODEL:
        # #980 D6: refused until model resolution is established; the drain
        # executor was requested and requests this worker again afterwards.
        _log("deferred", "projection=deferred")
        return 0
    if status not in (PROJECTION_PUBLISHED, PROJECTION_CURRENT):
        # #1069 spec §5.4: a contended or unavailable pass (or a deferral a
        # forced pass should never produce) verified nothing. `success` here
        # would let Doctor read a failing hand-off as healthy, so it is a
        # failed hand-off with the status as a fixed token.
        _log("error", f"projection={status}")
        return 0
    _log("success", _counts(result))
    return 0


def _run_codex_alert_activation(obligations, *, now, log, counts,
                                read_deferrals: "list[str]") -> int:
    """The worker's activation leg (#1070, spec §5.6, Q14).

    The requests name the owed scope, not eligibility: eligibility is the real
    lifecycle locks of the roots the HOOK's root resolution configures. A
    global request owes every configured root; a removed root gets no lock and
    therefore no dispatch, and a newly configured root inherits nothing a
    request completed. Locks are held through evaluation, commit and dispatch,
    which all happen inside the reconcile call.

    ``read_deferrals`` is the invocation's collector (#1076 D5): the Codex
    budget reads the reconcile's off-hook ingest deferred. The obligation and
    the budget axes are independent (D6): a recorded deferral never turns a
    completed activation into anything but ``result=success``.
    """
    import _cctally_record
    from _lib_codex_hooks import (
        acquire_due_lifecycle_locks,
        release_lifecycle_locks,
    )

    try:
        roots = list(_cctally_record._codex_lifecycle_roots())
        configured = frozenset(root.source_root_key for root in roots)
        owed: "set[str]" = set()
        for request in obligations.requests:
            owed |= _alert_obligation_owed_roots(request, configured)
        owed_roots = [root for root in roots if root.source_root_key in owed]
        locks = acquire_due_lifecycle_locks(
            _cctally_core.APP_DIR, owed_roots, now=time.time(),
            throttle_seconds=0)
    except OSError as exc:
        # An unreadable root configuration or a lifecycle directory that
        # cannot be created (an ejected or read-only volume): nothing was
        # locked or evaluated, so the obligation stays exactly as recorded
        # and this run still leaves its outcome line for Doctor.
        from _cctally_record import _hook_log_error_detail
        log("error",
            "mode=activation projection=alert_lifecycle_unavailable pending=1",
            error=_hook_log_error_detail(exc))
        return 0
    if len(locks) != len(owed_roots):
        # All or nothing: a partial set cannot retire a global obligation, and
        # publishing over part of it would activate the rest late.
        #
        # Unlike the damaged-sidecar branch, this returns without the due
        # verification on purpose: contention is transient (a hook or a
        # sibling worker holds the locks right now), the obligation stays
        # pending, and every hook that observes pending work requests this
        # worker again, so the next spawn retries the activation, whose
        # complete pass is also the verification.
        release_lifecycle_locks(locks)
        log("error",
            f"mode=activation projection=alert_lifecycle_contended "
            f"roots={len(owed_roots)} locked={len(locks)} pending=1")
        return 0
    try:
        result = reconcile_codex_quota_projection(
            force_full=True, now=now,
            alert_eligible_root_keys=frozenset(
                lock.root.source_root_key for lock in locks),
            _alert_activation_roots=configured,
            _read_deferrals=read_deferrals,
        )
    except Exception as exc:
        from _cctally_record import _hook_log_error_detail
        log("error", "mode=activation projection=alert_activation_failed "
            "pending=1", error=_hook_log_error_detail(exc))
        return 0
    finally:
        release_lifecycle_locks(locks)
    status = getattr(result, "status", None) or PROJECTION_PUBLISHED
    pending = bool(getattr(result, "alert_work_pending", False))
    detail = (
        f"mode=activation {counts(result)} roots={len(locks)} "
        f"alerts={int(getattr(result, 'alerts_dispatched', 0) or 0)} "
        f"pending={int(pending)}")
    if status == PROJECTION_DEFERRED_MODEL:
        log("deferred", f"projection=deferred {detail}")
        return 0
    if status not in (PROJECTION_PUBLISHED, PROJECTION_CURRENT):
        token = getattr(result, "alert_work_issue", "") or status
        log("error", f"projection={token} status={status} {detail}")
        return 0
    if pending:
        token = (
            getattr(result, "alert_work_issue", "") or "alert_activation_failed")
        log("error", f"projection={token} status={status} {detail}")
        return 0
    log("success", f"projection={status} {detail}")
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
    detail: "dict | None" = None,
) -> _axes.AlertDirtyScope:
    """Feed the five-axis kernel from stats.db and the resolved configuration.

    ``detail``, when supplied, receives the roots behind each root-scoped
    reason — ``changed_roots`` (axis 2) and ``scheduled_roots`` (axis 4's
    matured owners, ``None`` for legacy ownership) — which the kernel's merged
    ``roots`` cannot separate (#1070: each becomes its own deferred obligation).
    """
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
    if detail is not None:
        detail["changed_roots"] = frozenset(
            key[1] for key, fingerprint in resolved.items()
            if stored.get(key) != fingerprint)
        detail["scheduled_roots"] = scheduled_roots
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


def _codex_projected_rows_present(stats_conn: sqlite3.Connection) -> bool:
    """True when stats.db holds any Codex block, milestone or threshold event.

    Half of the empty-store test (#1069 Q4); the projection-state half is the
    witness's own rows. Each probe is one ``source='codex' LIMIT 1`` seek on
    the table's ``(source, …)`` index, and the scope is the whole Codex
    projection, every root included: a departed root's rows are exactly what
    only a complete pass can orphan. Fail-safe toward more work — a read it
    cannot complete reads as rows present.
    """
    try:
        return (
            stats_conn.execute(
                "SELECT 1 FROM quota_window_blocks "
                " WHERE source='codex' LIMIT 1").fetchone() is not None
            or stats_conn.execute(
                "SELECT 1 FROM quota_percent_milestones "
                " WHERE source='codex' LIMIT 1").fetchone() is not None
            or stats_conn.execute(
                "SELECT 1 FROM quota_threshold_events "
                " WHERE source='codex' LIMIT 1").fetchone() is not None
        )
    except sqlite3.Error:
        return True


def _codex_observations_present(
    cache_conn: sqlite3.Connection, roots: "frozenset[str]",
) -> bool:
    """True when the cache holds a Codex quota observation for any of
    ``roots``: one ``idx_quota_window_source_root`` seek per root. The cache
    half of the empty-store test (#1069 Q4); fail-safe toward more work."""
    try:
        return any(
            cache_conn.execute(
                "SELECT 1 FROM quota_window_snapshots "
                " WHERE source_root_key=? AND source='codex' LIMIT 1",
                (root,),
            ).fetchone() is not None
            for root in sorted(roots))
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


#: Every ``quota_window_snapshots`` column that must be present and non-blank
#: before ``load_codex_quota_observations`` will interpret the row.
#:
#: Named once because TWO populations have to agree on it: the loader's own, and
#: the #500 attribution evidence pass's. A row one of them keeps and the other
#: drops is a silent divergence in BOTH directions — an evidence row the loader
#: discarded can name an account no loaded observation carries, turning a group
#: the fold sees as cleanly unattributed into a suppressed one.
#:
#: One divergence between those two populations is KNOWN and is not on this
#: list, because it is not about a required column. Both sides derive the group
#: anchor as ``COALESCE(canonical_resets_at_utc, resets_at_utc)``, but SQL
#: ``COALESCE`` only replaces NULL, while the loader's per-row interpretation
#: treats ``''`` as absent too (``in (None, "")``) and falls back to the raw
#: reset. So a row storing an EMPTY canonical anchor is kept by the loader and
#: dropped by the evidence pass, which discards one witness for that group. The
#: append path never writes ``''`` — this shape comes from a hand-repaired row —
#: and the effect is one-directional (a narrower witness set can only make an
#: assertion dormant, never make it claim a group it does not own), so it is
#: recorded here rather than repaired. It predates #500.
_CODEX_QUOTA_REQUIRED_TEXT = (
    "source", "source_root_key", "source_path", "captured_at_utc",
    "observed_slot", "logical_limit_key", "resets_at_utc",
)


def _codex_quota_required_text_present(values: Iterable[object]) -> bool:
    """True iff every required text value is present and not blank."""
    return not any(
        value is None or not str(value).strip() for value in values)


# ── #901 W1: reading the maintained raw-partition maxima (spec §5.1, T1) ─────

#: The objects the latest-only read and the general loader's plan pins depend
#: on, probed together once per load.
_CODEX_QUOTA_READ_OBJECTS = (
    "codex_quota_partition_latest", "idx_qws_partition_capture",
    "trg_qws_latest_ins", "trg_qws_latest_del", "trg_qws_latest_upd",
    "idx_qws_codex_load_order", "idx_qws_physical_group",
)
_CODEX_QUOTA_SUMMARY_OBJECTS = frozenset(_CODEX_QUOTA_READ_OBJECTS[:5])

#: The latest path's SELECT list over the summary join, aliased so every name
#: the interpretation loop reads resolves exactly as it does on the window path.
_CODEX_QUOTA_LATEST_SELECT = ", ".join(
    f"q.{column} AS {column}" for column in (
        "source", "source_root_key", "source_path", "line_offset",
        "captured_at_utc", "observed_slot", "logical_limit_key", "limit_id",
        "limit_name", "window_minutes", "used_percent", "resets_at_utc",
        "plan_type", "individual_limit_json", "reached_type",
        "observed_model", "account_key", "canonical_resets_at_utc",
    ))


def _codex_quota_read_objects(conn: sqlite3.Connection) -> "frozenset[str]":
    """Which of the read-path objects this store carries (one probe)."""
    placeholders = ",".join("?" for _ in _CODEX_QUOTA_READ_OBJECTS)
    return frozenset(
        str(row[0]) for row in conn.execute(
            f"SELECT name FROM sqlite_master WHERE name IN ({placeholders})",
            _CODEX_QUOTA_READ_OBJECTS)
    )


def _codex_quota_latest_summary_ready(read_objects: "frozenset[str]") -> bool:
    """True when the summary and everything that maintains it are present.

    The five objects are created and bootstrapped in one transaction
    (``_cctally_db._apply_codex_quota_latest_summary``), and SQLite drops a
    table's triggers and indexes only together with the table, so their joint
    presence means the summary has been maintained since its bootstrap.
    Anything less — a read-only open of a store migration 047 has not reached,
    or a legacy shape — keeps the window query, which selects the same rows.
    """
    return _CODEX_QUOTA_SUMMARY_OBJECTS <= read_objects


def _sqlite_order_value(value) -> tuple:
    """Rank a stored value the way SQLite's BINARY ORDER BY ranks it.

    NULL first, then numbers (integers and reals compared numerically), then
    text in code-point order (which is UTF-8 byte order), then blobs.
    """
    if value is None:
        return (0, 0)
    if isinstance(value, (int, float)):
        return (1, value)
    if isinstance(value, str):
        return (2, value)
    return (3, bytes(value))


def _codex_quota_latest_rows(
    conn: sqlite3.Connection, requested: "set[str] | None",
) -> "list[sqlite3.Row]":
    """Every valid physical row tied at its raw partition's maximum second.

    One scan of the summary (one row per retained raw partition) and one
    ``idx_qws_partition_capture`` seek per partition. ``CROSS JOIN`` fixes the
    summary as the outer loop and ``INDEXED BY`` the inner seek, so the plan
    cannot drift into walking the snapshots. The window path's ORDER BY
    (``source_root_key, captured_at_utc, resets_at_utc, source_path,
    line_offset``) is reproduced here in Python over this bounded set, so the
    rows reach the interpretation loop in exactly the order they did before.
    """
    import _cctally_db

    on = " AND ".join(
        f"{expr} = s.{key}" for key, expr in zip(
            _cctally_db._CODEX_QUOTA_LATEST_KEY_COLUMNS,
            _cctally_db._codex_quota_latest_key_exprs("q.")))
    sql = (
        f"SELECT {_CODEX_QUOTA_LATEST_SELECT}"
        " FROM codex_quota_partition_latest AS s"
        " CROSS JOIN quota_window_snapshots AS q"
        " INDEXED BY idx_qws_partition_capture"
        f" ON {on} AND unixepoch(q.captured_at_utc) = s.latest_capture_epoch"
        f" WHERE {_cctally_db._codex_quota_latest_validity_sql('q.')}"
    )
    params: "list[object]" = []
    if requested is not None:
        sql += (" AND s.k_source_root_key IN ("
                + ",".join("quote(?)" for _ in requested) + ")")
        params.extend(sorted(requested))
    rows = conn.execute(sql, params).fetchall()
    rows.sort(key=lambda row: tuple(
        _sqlite_order_value(row[name]) for name in (
            "source_root_key", "captured_at_utc", "resets_at_utc",
            "source_path", "line_offset")))
    return rows


# --------------------------------------------------------------------------
# #500 §6.4 — the fold-time operator attribution overlay
# --------------------------------------------------------------------------
#
# The operator's assertions live in the append-only journal and are indexed into
# cache.db's `codex_window_attributions` (#500 Task 1). This is where they are
# APPLIED: immediately before the continuity fold, so an attributed group is
# genuinely identified by the time `adopt_unidentified_observations`,
# `build_blocks` and the spend-adoption pass see it. Nothing is written back to
# `quota_window_snapshots`, which stays raw pre-fold provider evidence.
#
# Resolution and application are separate phases and are NOT interchangeable
# (spec §6.4.1). Witness matching is population-dependent, and several callers
# deliberately hand the loader a reduced population — the dashboard bounds by
# recent days and a row cap, doctor asks for the latest row per identity. If
# matching ran against the reduced set, a component a later observation bridged
# could present a subset carrying none of the assertion's original witnesses and
# the attribution would vanish from the dashboard alone. So resolution always
# reads its own COMPLETE evidence for the roots in play, and only application
# touches the caller's rows.


def _cache_module():
    """`_cctally_cache`, imported lazily.

    That module imports THIS one inside its own functions for the same reason:
    the cache leg reads quota observations and the quota leg reads the cache's
    derived attribution index, so a module-level import either way is a cycle.
    """
    import _cctally_cache

    return _cctally_cache


def _codex_quota_instant_is_valid(value: object) -> bool:
    """``_parse_utc`` succeeds on ``value``, without paying for the conversion.

    The evidence pass asks this question about every row's capture and reset
    only to agree with the loader on which rows exist, and never uses the
    resulting datetime — while ``astimezone`` is the expensive half of
    ``_parse_utc``.

    Same acceptance set, and the last branch is what makes that true rather than
    approximately true. ``_parse_utc`` ends in ``astimezone(UTC)``, which raises
    ``OverflowError`` when shifting the instant leaves the representable range —
    a year-9999 instant with a negative offset, or a year-1 instant with a
    positive one. Skipping the conversion outright would accept a row the loader
    rejects, so it is performed on exactly the two years where it can fail, and
    nowhere else.
    """
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return False
    if parsed.year in (dt.MINYEAR, dt.MAXYEAR):
        try:
            parsed.astimezone(UTC)
        except (OverflowError, OSError, ValueError):
            return False
    return True


def _codex_attribution_witness(value: object) -> str:
    """One spelling for one reset instant, applied to BOTH sides of the binding.

    The cache retains whichever spelling the provider sent and the journal
    payload carries whichever spelling the command read, so a `Z` witness and a
    `+00:00` group member are the same instant and must intersect. This is the
    same fail-open normalizer the loading-unit key uses.
    """
    return _ledger.normalize_reset(value)


def _codex_attribution_table_present(conn: sqlite3.Connection) -> bool:
    """Whether this cache carries the derived index at all.

    A cache too old to have it degrades to "no assertions", exactly as every
    other column probe in this loader degrades. The fail-LOUD requirement of
    §6.2 is about the rebuild's publication gate, which is where a partial index
    would be certified as complete; a read against a pre-#500 cache has no
    assertion to under-apply.
    """
    try:
        return bool(conn.execute(
            "SELECT 1 FROM sqlite_master "
            " WHERE type='table' AND name='codex_window_attributions' LIMIT 1"
        ).fetchone())
    except sqlite3.DatabaseError:
        return False


def _codex_attribution_axis_shards(
    assertions: "Sequence[Mapping[str, object]]",
) -> "dict[tuple, set[str]]":
    """Assertions grouped by their four normalized axes, witnesses unioned.

    The shard, not the assertion, is the unit both evidence passes are issued
    per — because the kernel binds on ``group.axes == assertion.axes`` and every
    assertion sharing one axis combination therefore reaches the same candidate
    groups. One query per combination is what keeps the pass proportional to the
    axes in play instead of to the number of assertions.
    """
    shards: "dict[tuple, set[str]]" = {}
    for assertion in assertions:
        try:
            axes = (
                str(assertion["source_root_key"]),
                str(assertion["logical_limit_key"]),
                str(assertion["observed_slot"]),
                int(assertion["window_minutes"]),
            )
            witnesses = {
                str(witness) for witness in assertion["raw_resets_at_utc"]}
        except (KeyError, TypeError, ValueError):
            continue
        if not all(str(member).strip() for member in axes[:3]) or not witnesses:
            continue
        shards.setdefault(axes, set()).update(witnesses)
    return shards


def _codex_attribution_stored_limit_keys(logical_limit_key: str) -> "list[str]":
    """Every spelling ``quota_window_snapshots`` can hold for one axis key.

    Two closures, because the stored key and the INTERPRETED key are not the
    same value. The snap closure covers the weekly jitter that lives in the key
    as well as in the column, and the model-pool strip covers the read-path
    rewrite: a row the interpretation moves INTO a model-scoped bucket is stored
    under the ordinary key, so an axis key carrying a ``modelPool`` member has to
    ask for the stripped spellings too or its own members become unreachable.
    For an ordinary key ``strip_model_pool`` is the identity and this is exactly
    the three snap spellings.
    """
    return sorted(
        set(codex_snap_equivalent_limit_keys(logical_limit_key))
        | set(codex_snap_equivalent_limit_keys(
            _ledger.strip_model_pool(logical_limit_key)))
    )


def _codex_shard_axis_filter(axes: tuple) -> "tuple[str, tuple]":
    """The WHERE clause and parameters selecting one attribution axis shard.

    Rows are STORED under whichever length and key spelling the provider sent,
    so the filter enumerates every equivalent rather than snapping in SQL —
    which it could not do and still seek the index.
    """
    root_key, limit_key, slot, window_minutes = axes
    limit_keys = _codex_attribution_stored_limit_keys(limit_key)
    minutes = sorted({
        int(value)
        for value in codex_snap_equivalent_window_minutes(window_minutes)
    })
    axis_clause = (
        " WHERE source='codex' AND source_root_key = ?"
        "   AND logical_limit_key IN ("
        + ",".join("?" * len(limit_keys)) + ")"
        "   AND observed_slot = ?"
        "   AND window_minutes IN (" + ",".join("?" * len(minutes)) + ")"
    )
    return axis_clause, (root_key, *limit_keys, slot, *minutes)


def _codex_witness_anchors(
    conn: sqlite3.Connection, anchor_expr: str, axis_clause: str,
    axis_params: tuple, witnesses: "Sequence[str]",
) -> "set[str]":
    """Pass 1: the canonical anchors of the groups a witness reset reaches.

    #901 Amendment 19 T1 (spec I1): the set is built in Python from the
    streamed rows. ``SELECT DISTINCT`` sorted the anchors of every capture
    carrying a witness reset through a temp b-tree — a window group's whole
    activity — and one attribution axis shard is not a bound. The result is
    the same set; the Python set holds one entry per distinct anchor.
    """
    pass_one = (
        f"SELECT {anchor_expr} AS anchor"
        "  FROM quota_window_snapshots"
        + axis_clause
        + "   AND unixepoch(resets_at_utc) IN ("
        + ",".join("unixepoch(?)" for _ in witnesses) + ")"
    )
    anchors: "set[str]" = set()
    for row in conn.execute(pass_one, (*axis_params, *witnesses)):
        if row[0] is None or not str(row[0]).strip():
            continue
        anchors.add(str(row[0]))
    return anchors


def _load_codex_window_group_evidence(
    conn: sqlite3.Connection, assertions: "Sequence[Mapping[str, object]]",
) -> "tuple[_wa.WindowGroup, ...]":
    """Complete current evidence for every group a stored witness can reach.

    Two indexed passes rather than one interpretation of all history, and the
    two are exactly equivalent to a full read for RESOLUTION's purposes:

    * Pass 1 finds the canonical anchors of every group holding a row whose RAW
      reset is one of the stored witnesses. The kernel binds on an intersection
      against those witnesses, so a group holding none of them cannot match and
      its absence here changes no verdict — the assertion is dormant either way.
    * Pass 2 loads the COMPLETE membership of exactly those groups. Every member
      of a group shares its axes and its canonical anchor by definition of the
      physical window key, so the shard is a superset of the group and the
      identified accounts, the model-pool verdict and the full witness set are
      all read over the whole population.

    BOTH passes are sharded on the assertion's four axes, and that is the whole
    performance story. ``idx_qws_physical_group`` is ``(source_root_key,
    logical_limit_key, observed_slot, window_minutes, unixepoch(COALESCE(
    canonical_resets_at_utc, resets_at_utc))) WHERE source='codex'``, and SQLite
    cannot skip an interior member: a pass constraining only the root falls back
    to ``idx_quota_window_source_root`` and reads the root's whole history per
    query. Measured read-only on the maintainer's 267,163-row store, 63
    assertions over 2 axis combinations resolving 57 groups:

    ==============================================  =========  =========
    pass                                            root-only   sharded
    ==============================================  =========  =========
    1 (one query, or one per axis combination)        63.9 ms   43.8 ms
    2 (one query per group)                         3609.7 ms  119.2 ms
    whole call, SQL and interpretation together     4443.5 ms  453.8 ms
    ==============================================  =========  =========

    Pass 2 is where the plan actually changes, and it is the dominant cost. It
    moves from ``SEARCH ... USING INDEX idx_quota_window_source_root
    (source_root_key=?)`` at 63.3 ms per group to ``SEARCH ... USING INDEX
    idx_qws_physical_group (source_root_key=? AND logical_limit_key=? AND
    observed_slot=? AND window_minutes=? AND <expr>=?)`` at 2.1 ms per group.
    Pass 1 cannot constrain the fifth member at all — it matches RAW resets
    while the index holds the coalesced anchor — so it seeks the four-member
    prefix and scans that range; its gain is modest and, on a store where every
    axis combination is asserted at once, it can cost slightly more than one
    root-wide query. That is accepted: pass 1 is under 2% of the call, and
    pairing the axes with each anchor is what lets pass 2 seek all five members.

    This runs three times per ``sync_codex_cache`` and once per dashboard tick
    and per ``codex quota``, inside both cache flocks.

    Interpretation matches ``load_codex_quota_observations`` member for member —
    the ``window_minutes`` snap, the limit-key snap, the model-pool rewrite, and
    the row-validity predicate — because the group key produced here has to equal
    ``_lib_quota._physical_window_key`` of the observations it will own, and the
    accounts read here have to be the accounts those observations carry.

    A row whose ``canonical_resets_at_utc`` is NULL is NOT lost: the anchor
    expression coalesces onto the raw reset, and ``QuotaObservation`` fills
    ``canonical_resets_at`` from ``resets_at`` under exactly the same rule
    (``bin/_lib_quota.py`` ``__post_init__``), so both sides key such a row on
    its own raw reset and they agree. Cache migration 032 backfills the column
    for ``source='codex'`` anyway.
    """
    shards = _codex_attribution_axis_shards(assertions)
    if not shards:
        return ()
    columns = {
        str(row[1]) for row in conn.execute(
            "PRAGMA table_info(quota_window_snapshots)")
    }
    if not {"source", "source_root_key", "resets_at_utc"} <= columns:
        return ()
    has_anchor = "canonical_resets_at_utc" in columns
    has_account = "account_key" in columns
    has_model = "observed_model" in columns
    anchor_expr = (
        "COALESCE(canonical_resets_at_utc, resets_at_utc)"
        if has_anchor else "resets_at_utc"
    )
    account_expr = "account_key" if has_account else "NULL"
    model_expr = "observed_model" if has_model else "NULL"
    # The seven required-text columns first, in `_CODEX_QUOTA_REQUIRED_TEXT`
    # order, so the shared predicate can be applied to `row[:7]` verbatim.
    pass_two_select = (
        "SELECT source, source_root_key, source_path, captured_at_utc,"
        "       observed_slot, logical_limit_key, resets_at_utc,"
        "       window_minutes, limit_id, limit_name, used_percent,"
        "       line_offset,"
        f"       {model_expr} AS observed_model,"
        f"       {account_expr} AS account_key,"
        f"       {anchor_expr} AS anchor"
        "  FROM quota_window_snapshots"
    )

    buckets: "dict[tuple, dict]" = {}
    # Per-CALL memos. Every one of these is a pure function of a value drawn
    # from a tiny domain — at most a handful of stored limit-key spellings, one
    # anchor spelling per group, a few distinct raw resets, one or two models —
    # evaluated once per ROW over a population that is ~1,700 rows per group on
    # the maintainer's store. Interpreting each row independently made the
    # Python half of this pass cost more than the (now indexed) SQL half.
    snapped_keys: "dict[str, str]" = {}
    pools: "dict[object, object]" = {}
    instants: "dict[str, object]" = {}
    valid_instants: "dict[str, bool]" = {}
    witness_texts: "dict[str, str]" = {}
    model_scoped: "dict[tuple, bool]" = {}
    for axes, shard_witnesses in sorted(shards.items()):
        axis_clause, axis_params = _codex_shard_axis_filter(axes)
        anchors = _codex_witness_anchors(
            conn, anchor_expr, axis_clause, axis_params,
            sorted(shard_witnesses))
        if not anchors:
            continue
        # ONE SHARD PER GROUP, for the reason the loader states at its own group
        # filter: an OR over the equality gives up and scans, while each group
        # as its own query seeks all five members of `idx_qws_physical_group`.
        pass_two = (
            pass_two_select + axis_clause
            + f"   AND unixepoch({anchor_expr}) = unixepoch(?)"
        )
        for anchor_text in sorted(anchors):
            for row in conn.execute(pass_two, (*axis_params, anchor_text)):
                if not _codex_quota_required_text_present(row[:7]):
                    continue
                if row[14] is None or not str(row[14]).strip():
                    continue
                stored_key = str(row[5])
                anchor_value = str(row[14])
                capture_text = str(row[3])
                reset_text = str(row[6])
                try:
                    snapped_minutes = snap_codex_window_minutes(int(row[7]))
                    logical_limit_key = snapped_keys.get(stored_key)
                    if logical_limit_key is None:
                        logical_limit_key = snapped_keys[stored_key] = (
                            snap_window_minutes(stored_key))
                    model = row[12]
                    if model not in pools:
                        pools[model] = codex_model_scoped_quota_pool(model)
                    if pools[model] is not None:
                        logical_limit_key = _codex_logical_limit_key(
                            str(row[1]), row[8], str(row[4]), snapped_minutes,
                            str(model),
                        )
                    anchor = instants.get(anchor_value)
                    if anchor is None:
                        anchor = instants[anchor_value] = _parse_utc(
                            anchor_value, "canonical_resets_at_utc")
                    used_percent = float(row[10])
                    line_offset = int(row[11])
                except (TypeError, ValueError, OverflowError):
                    # The loader skips a malformed physical row window-by-window
                    # rather than suppressing unrelated valid windows; so does
                    # this.
                    continue
                # The rest of the loader's per-row contract, which
                # `QuotaObservation.__post_init__` enforces there and which has
                # to be enforced here or the two populations disagree.
                for text in (capture_text, reset_text):
                    if text not in valid_instants:
                        valid_instants[text] = _codex_quota_instant_is_valid(
                            text)
                if (
                    not valid_instants[capture_text]
                    or not valid_instants[reset_text]
                    or not 0 <= used_percent <= 100
                    or line_offset < 0
                    or not str(row[2]).startswith("/")
                ):
                    continue
                key = ("codex", str(row[1]), logical_limit_key, str(row[4]),
                       snapped_minutes, anchor)
                bucket = buckets.get(key)
                if bucket is None:
                    bucket = buckets[key] = {
                        "root": str(row[1]),
                        "limit_key": logical_limit_key,
                        "slot": str(row[4]),
                        "minutes": snapped_minutes,
                        "witnesses": set(),
                        "accounts": set(),
                        "model_scoped": False,
                    }
                witness = witness_texts.get(reset_text)
                if witness is None:
                    witness = witness_texts[reset_text] = (
                        _codex_attribution_witness(reset_text))
                bucket["witnesses"].add(witness)
                account = row[13]
                if account not in (None, "", _lib_accounts.UNATTRIBUTED):
                    bucket["accounts"].add(str(account))
                # `limit_name` is compare=False on the identity, so the label can
                # differ across one group's observations; ANY Spark evidence
                # demotes the whole group out of account weekly quota (#373),
                # matching the spend fold. That direction only ever withholds an
                # attribution.
                scope_key = (logical_limit_key, row[9])
                if scope_key not in model_scoped:
                    model_scoped[scope_key] = is_model_scoped_codex_quota(
                        logical_limit_key, row[9])
                if model_scoped[scope_key]:
                    bucket["model_scoped"] = True
    return tuple(
        _wa.WindowGroup(
            group_key=key,
            source_root_key=bucket["root"],
            logical_limit_key=bucket["limit_key"],
            observed_slot=bucket["slot"],
            window_minutes=bucket["minutes"],
            raw_resets_at_utc=frozenset(bucket["witnesses"]),
            identified_accounts=frozenset(bucket["accounts"]),
            model_scoped=bucket["model_scoped"],
        )
        for key, bucket in sorted(buckets.items(), key=lambda item: str(item[0]))
    )


def resolve_codex_window_attributions(
    conn: sqlite3.Connection,
    *,
    source_root_keys: "Iterable[str] | None" = None,
    include_retracted: bool = False,
) -> "tuple[tuple[_wa.AssertionResolution, ...], Mapping[tuple, str]]":
    """``resolve_codex_window_attributions_with_evidence`` without the groups."""
    resolutions, ownership, _groups = (
        resolve_codex_window_attributions_with_evidence(
            conn, source_root_keys=source_root_keys,
            include_retracted=include_retracted))
    return resolutions, ownership


def resolve_codex_window_attributions_with_evidence(
    conn: sqlite3.Connection,
    *,
    source_root_keys: "Iterable[str] | None" = None,
    include_retracted: bool = False,
) -> "tuple[tuple[_wa.AssertionResolution, ...], Mapping[tuple, str], tuple[_wa.WindowGroup, ...]]":
    """Resolve every recorded assertion against COMPLETE current evidence.

    Returns the loaded GROUPS alongside the resolutions and the ownership map,
    because one caller needs a question the resolutions cannot answer: the §7.1
    spend reconciliation has to know every account the current world can
    JUSTIFY for a group, not only the account a resolved assertion supplies. A
    group's own ``identified_accounts`` is that justification — the fold adopts
    every unattributed member into it — and a resolution carries it only for
    ``SUPPRESSED_NATIVE``, so reading it from the groups covers the split and
    dormant shapes as well. The evidence is loaded once either way; this only
    stops it being discarded.

    The seam the overlay, ``account attribute``'s preview and the
    ``accounts.codex_window_attribution`` doctor leg all read: it returns a
    resolution for EVERY assertion, including the dormant, split and suppressed
    ones that apply nothing, plus the ownership map only the resolved ones
    populate.

    ``source_root_keys`` bounds which ASSERTIONS are loaded. It never bounds the
    evidence they are resolved against — that read derives its own roots from
    the assertions and is deliberately independent of any presentation bound.

    ``include_retracted`` additionally returns the tombstoned records so the
    §7.1 spend reconciliation can see the groups an assertion used to own. Their
    resolutions are reported, but they never enter the ownership map.
    """
    if not _codex_attribution_table_present(conn):
        return (), {}, ()
    assertions = _cache_module().load_active_window_attributions(
        conn, source_root_keys=source_root_keys)
    retracted: "tuple[Mapping[str, object], ...]" = ()
    if include_retracted:
        retracted = _cache_module().load_active_window_attributions(
            conn, source_root_keys=source_root_keys, retracted_only=True)
    if not assertions and not retracted:
        return (), {}, ()
    groups = _load_codex_window_group_evidence(conn, (*assertions, *retracted))
    resolutions, ownership = _wa.resolve_window_attributions(
        [
            _wa.WindowAssertion(
                op_id=str(record["op_id"]),
                account_key=str(record["account_key"]),
                source_root_key=str(record["source_root_key"]),
                logical_limit_key=str(record["logical_limit_key"]),
                observed_slot=str(record["observed_slot"]),
                window_minutes=int(record["window_minutes"]),
                raw_resets_at_utc=frozenset(
                    _codex_attribution_witness(value)
                    for value in record["raw_resets_at_utc"]
                ),
            )
            for record in assertions
        ],
        groups,
    )
    if not include_retracted:
        return resolutions, ownership, groups
    # Retracted records are resolved SEPARATELY, against the same evidence, so a
    # tombstone can never claim a group or join a conflict. Their only purpose
    # here is to name the range whose spend rows may still be stamped.
    tombstoned, _ignored = _wa.resolve_window_attributions(
        [
            _wa.WindowAssertion(
                op_id=str(record["op_id"]),
                account_key=str(record["account_key"]),
                source_root_key=str(record["source_root_key"]),
                logical_limit_key=str(record["logical_limit_key"]),
                observed_slot=str(record["observed_slot"]),
                window_minutes=int(record["window_minutes"]),
                raw_resets_at_utc=frozenset(
                    _codex_attribution_witness(value)
                    for value in record["raw_resets_at_utc"]
                ),
            )
            for record in retracted
        ],
        groups,
    )
    return (*resolutions, *tombstoned), ownership, groups


def codex_attribution_projection_scope(
    conn: sqlite3.Connection,
) -> "dict[str, frozenset] | None":
    """The dirty units an attribution change makes the projection owe.

    Returns the same ``{"raw_groups", "units"}`` shape ``_resolve_pass_scope``
    returns for a ledger-derived scope, so the two are unioned rather than
    special-cased, or ``None`` when this store has no attribution records at all.

    Three of §8.3's four points live here.

    **Snap expansion.** ``_apply_quota_projection_rows`` requires the loaded
    observations to carry the COMPLETE current membership of every dirty unit,
    and the loader's targeted filter matches RAW stored coordinates. One minute
    of weekly jitter lives in both the limit key and the column, so two raw
    groups can interpret into one window and asking only for the un-snapped
    spelling would hand the fold a partial population. This loads through the
    same ``codex_unit_loading_closure`` the scoped pass uses rather than
    re-deriving it, which also reaches a sibling pool's ``modelPool`` spelling
    of the unit (#1071).

    **Prior units, not only current ones.** A retraction, a newly dormant
    assertion and a component split all change which unit a group's rows belong
    to, so the sweep set carries every unit a record named AT ASSERTION TIME
    alongside every unit its current resolution names. Without the first half the
    obsolete blocks survive as exactly the duplicated window this work removes.

    **Suppressed and dormant records still count.** A record that resolves to
    nothing is precisely the one whose previously-materialized rows have to be
    swept, so the scope is built from every record — active, retracted,
    suppressed, split — and not from the ownership map.
    """
    if not _codex_attribution_table_present(conn):
        return None
    cache = _cache_module()
    records = (
        *cache.load_active_window_attributions(conn),
        *cache.load_active_window_attributions(conn, retracted_only=True),
    )
    if not records:
        return None
    unit_tuples: "set[tuple]" = set()
    for record in records:
        anchor = _ledger.normalize_reset(record["canonical_resets_at_utc"])
        unit_tuples.add(_ledger.loading_unit_from_raw((
            str(record["source_root_key"]),
            str(record["logical_limit_key"]),
            str(record["observed_slot"]),
            int(record["window_minutes"]),
            anchor,
        )))
    resolutions, _ownership = resolve_codex_window_attributions(
        conn, include_retracted=True)
    for resolution in resolutions:
        group_key = resolution.group_key
        if group_key is None:
            continue
        # A resolution's group key is the INTERPRETED one, so its unit is taken
        # through `loading_unit_from_identity`; both helpers strip a
        # `modelPool` member (#1071), so a SUPPRESSED_MODEL_SCOPED resolution
        # names the same unit its blocks stamp.
        unit_tuples.add(_ledger.loading_unit_from_identity(
            source_root_key=str(group_key[1]),
            logical_limit_key=str(group_key[2]),
            observed_slot=str(group_key[3]),
            window_minutes=int(group_key[4]),
            canonical_reset_iso=_utc_iso(group_key[5]),
        ))
    # The loader matches STORED columns, so each unit is loaded through the
    # shared closure: every stored key spelling of the unit — the `modelPool`
    # key a sibling pool sits under included — and every minute spelling. The
    # assertion matching above (`_codex_attribution_stored_limit_keys`) is a
    # separate question and is not broadened.
    return {
        "raw_groups": codex_unit_loading_closure(conn, unit_tuples),
        "units": frozenset(
            _ledger.physical_group_key_text(unit) for unit in unit_tuples),
    }


def _apply_codex_window_attribution_overlay(
    conn: sqlite3.Connection,
    observations: "list[QuotaObservation]",
    *,
    source_root_keys: "set[str] | None",
) -> "list[QuotaObservation]":
    """Stamp resolved ownership onto this load's currently-unattributed rows.

    The table-presence probe lives in the resolver, so this is one extra
    `sqlite_master` read and one indexed read of an empty table on a store that
    has never asserted anything, and nothing else.
    """
    if not observations:
        return observations
    _resolutions, ownership = resolve_codex_window_attributions(
        conn, source_root_keys=source_root_keys)
    if not ownership:
        return observations
    return list(_wa.apply_resolution(
        ownership,
        observations,
        _lib_quota._physical_window_key,
        lambda observation: observation.identity.account_key,
        lambda observation, account: replace(
            observation,
            identity=replace(observation.identity, account_key=account),
        ),
    ))


class _QuotaLoadValues:
    """The values one `load_codex_quota_observations` call shares (#997 D17).

    A full load materializes every retained observation (266 k on the
    maintainer's copy, about 2.3 KB each), and every row brought its own copy
    of each repeated text value, its own parsed reset instants and its own
    `QuotaWindowIdentity`. Within one call this keeps one object per distinct
    text value, one parsed instant per distinct stored reset text, and one
    identity per distinct tuple of ALL its fields. `limit_id` and
    `limit_name` are outside the identity's equality, so sharing by equality
    alone would hand one observation another's labels; the tuple includes them
    (with their types, so a hand-written ``1`` and ``1.0`` stay apart). Every
    shared value is immutable, so values, equality and order are unchanged.
    """

    __slots__ = ("_texts", "_resets", "_identities")

    def __init__(self) -> None:
        self._texts: dict = {}
        self._resets: dict = {}
        self._identities: dict = {}

    def text(self, value):
        """``value``'s shared object; a non-string is returned unchanged."""
        if type(value) is not str:
            return value
        return self._texts.setdefault(value, value)

    def reset(self, text: str, label: str) -> dt.datetime:
        """`_parse_utc(text, label)`, parsed once per distinct text."""
        parsed = self._resets.get(text)
        if parsed is None:
            parsed = self._resets[text] = _parse_utc(text, label)
        return parsed

    def identity(self, *, source, source_root_key, account_key,
                 logical_limit_key, observed_slot, window_minutes, limit_id,
                 limit_name) -> QuotaWindowIdentity:
        key = (source, source_root_key, account_key, logical_limit_key,
               observed_slot, type(window_minutes), window_minutes,
               type(limit_id), limit_id, type(limit_name), limit_name)
        identity = self._identities.get(key)
        if identity is None:
            text = self.text
            identity = self._identities[key] = QuotaWindowIdentity(
                source=text(source),
                source_root_key=text(source_root_key),
                account_key=text(account_key),
                logical_limit_key=text(logical_limit_key),
                observed_slot=text(observed_slot),
                window_minutes=window_minutes,
                limit_id=text(limit_id),
                limit_name=text(limit_name),
            )
        return identity


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
    latest_per_identity: bool = False,
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

    ``latest_per_identity`` (#566 §5.1 item 5) returns exactly the latest
    physical observation of each interpreted identity over the SAME all-history
    population, without materializing one Python object per retained row. It is
    a work bound, not a scope bound: no time range, no root filter and no row
    cap is introduced, so a caller that only needs each window's most recent
    capture — ``doctor`` — reads the same identities, the same latest captures
    and therefore the same verdict as the full load, on a store where the full
    load meant interpreting 266,337 rows to answer a question about 608 of them.

    #901 W1 (spec §5.1, T1): on a store carrying cache migration 047's objects
    the candidate rows come from ``codex_quota_partition_latest``, a
    trigger-maintained table holding every raw partition's maximum whole-second
    capture, joined back to every physical row tied at that second through
    ``idx_qws_partition_capture``. The window query described below stays as the
    fallback for a store without those objects (a read-only open before the
    migration ran, or a legacy shape); both select exactly the same rows, and
    tests/test_901_partition_latest_maintenance.py holds the summary to the
    frozen pre-#901 window query after every writer class.

    SQL narrows to the rows that can possibly win, and Python still decides. The
    partition is the RAW spelling of every column the interpretation reads
    (root, limit key, slot, window minutes, limit id and name, observed model,
    account key, and the coalesced reset anchor), which is strictly FINER than
    the interpreted identity — snapping and the model-pool rewrite only ever
    merge raw partitions, never split one — and the maximum over a set equals
    the maximum of its parts' maxima, so narrowing to per-partition winners
    cannot change the answer. Two consequences are load-bearing rather than
    incidental: the account key is a partition member, so every (physical
    window, identified account) pair still contributes a row and the
    window-account continuity fold below sees the same identified-account set it
    sees on the full population; and the cut keeps EVERY row tied at the
    partition's maximum whole-second capture rather than one row per partition,
    because SQL compares seconds while ``physical_order_key`` compares full
    datetimes and then breaks ties on the reset anchor and physical position.

    The residual differences are malformed or non-canonical instants. A row
    that survives SQLite's predicates but still fails Python parsing can hide
    an older valid capture of the same window. In the reverse direction,
    ``datetime.fromisoformat`` accepts basic-format ISO instants such as
    ``20260814T120000Z`` that SQLite's ``unixepoch`` rejects, so the SQL path
    drops that row and may let an older capture win. Neither shape is emitted
    by ``_utc_iso``; both are confined to a store carrying hand-written or
    corrupt quota rows.
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
    if latest_per_identity and (
        max_rows is not None
        or physical_signatures is not None
        or physical_groups is not None
        or captured_at_or_after is not None
        or active_at is not None
        or canonical_resets_between is not None
    ):
        # Every one of these either bounds the population or accumulates over
        # it, and the reduction below already discards the rows a bound would
        # act on. Refuse the combination rather than return a set whose meaning
        # depends on which narrowing ran first.
        raise ValueError(
            "latest_per_identity cannot be combined with a population bound")
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
        # #901: one probe for the summary and the two plan-pin indexes.
        read_objects = _codex_quota_read_objects(conn)
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
        select_list = """source, source_root_key, source_path, line_offset,
                   captured_at_utc, observed_slot, logical_limit_key, limit_id,
                   limit_name, window_minutes, used_percent, resets_at_utc,
                   plan_type, individual_limit_json, reached_type,
                   {model_expr}, {account_expr}, {anchor_expr}""".format(
            model_expr=model_expr, account_expr=account_expr,
            anchor_expr=anchor_expr,
        )
        # #901 W3 (spec §5.1, T1/T3): pin the plan. Unpinned, the planner sorts
        # an unbounded or multi-root load through a temp b-tree via the
        # `source` autoindex, and the load-order index hijacks the projector's
        # per-group shards into a whole-root walk (both measured). A bounded
        # `max_rows` read keeps its plan: its sorter holds at most `max_rows`
        # rows. Each pin is applied only when its index exists, so a store
        # without them keeps today's SQL.
        if group_filter is not None:
            table_hint = (
                " INDEXED BY idx_qws_physical_group"
                if "idx_qws_physical_group" in read_objects else "")
        elif physical_signatures is None and max_rows is not None:
            table_hint = ""
        else:
            table_hint = (
                " INDEXED BY idx_qws_codex_load_order"
                if "idx_qws_codex_load_order" in read_objects else "")
        sql = f"""
            SELECT {select_list}
              FROM quota_window_snapshots{table_hint}
             WHERE source='codex' AND source_root_key IS NOT NULL
        """
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
        # ``latest_per_identity`` replaces the base query wholesale below, so
        # assembling its ORDER BY here would be dead work.
        if not latest_per_identity:
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
                sql += (
                    " ORDER BY source_root_key, captured_at_utc, resets_at_utc, "
                    "source_path, line_offset"
                )
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
        if latest_per_identity:
            # Every column the per-row interpretation reads, in its RAW
            # spelling. Partitioning on the raw columns is strictly finer than
            # the interpreted identity, which is what makes the per-partition
            # maximum a safe candidate set (see the docstring).
            partition_by = ", ".join((
                "source_root_key",
                "logical_limit_key",
                "observed_slot",
                "window_minutes",
                "limit_id",
                "limit_name",
                "observed_model" if has_observed_model else "NULL",
                "account_key" if has_account else "NULL",
                reset_group_expr,
            ))
            # The same validity predicates the interpretation loop applies,
            # restricted to the ones SQL can state. A row SQLite cannot read as
            # a time can never win its partition, which matters because the
            # window maximum ignores NULLs.
            required_not_blank = " AND ".join(
                f"trim(coalesce({column}, '')) <> ''"
                for column in _CODEX_QUOTA_REQUIRED_TEXT
            )
            # The root filter is the only optional clause that can still be in
            # play here, because every other bound is refused above, so the
            # inner query is rebuilt rather than surgically edited.
            root_clause = (
                " AND source_root_key IN ("
                + ",".join("?" for _ in requested) + ")"
                if requested is not None else ""
            )
            sql = (
                f"SELECT * FROM (SELECT {select_list},"
                " MAX(unixepoch(captured_at_utc)) OVER"
                f" (PARTITION BY {partition_by}) AS _group_latest_capture"
                " FROM quota_window_snapshots WHERE"
                " source='codex' AND source_root_key IS NOT NULL"
                f" AND {required_not_blank}"
                " AND unixepoch(captured_at_utc) IS NOT NULL"
                " AND unixepoch(resets_at_utc) IS NOT NULL"
                f"{root_clause}"
                ") WHERE unixepoch(captured_at_utc) = _group_latest_capture"
                " ORDER BY source_root_key, captured_at_utc, resets_at_utc,"
                " source_path, line_offset"
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
        if latest_per_identity and _codex_quota_latest_summary_ready(
                read_objects):
            # #901 W1 (spec §5.1, T1): the maintained summary yields exactly
            # the rows the window query above selects, without sorting history.
            row_source = _codex_quota_latest_rows(conn, requested)
        else:
            row_source = _iter_shard_rows(conn, shards)
        result: list[QuotaObservation] = []
        signature_tuples: dict[str, dict[str, list[tuple[object, ...]]]] = {}
        # #997 D17: repeated values are shared within this call.
        values = _QuotaLoadValues()
        text = values.text
        for row in row_source:
            if not _codex_quota_required_text_present(
                row[name] for name in _CODEX_QUOTA_REQUIRED_TEXT
            ):
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
                identity = values.identity(
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
                    resets_at=values.reset(
                        str(row["resets_at_utc"]), "resets_at_utc"),
                    source_path=text(str(row["source_path"])),
                    line_offset=int(row["line_offset"]),
                    plan_type=text(row["plan_type"]),
                    individual_limit_json=text(row["individual_limit_json"]),
                    reached_type=text(row["reached_type"]),
                    canonical_resets_at=(
                        None if row["canonical_resets_at_utc"] in (None, "")
                        else values.reset(
                            str(row["canonical_resets_at_utc"]),
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
        # Operator attribution overlay (#500 spec §6.4), immediately before the
        # fold. Ordered here and nowhere else: the fold then finds nothing left
        # to adopt in an attributed group, `build_blocks` carries the account
        # into `quota_window_blocks`, and the spend-adoption pass sees a
        # claiming window that identifies exactly one account — all three axes
        # from ONE insertion point, with no new adoption logic anywhere.
        #
        # Ordered AFTER the physical signatures too, which are computed from the
        # physical tuple alone and are therefore account-independent, so the
        # certificate a bounded read stamps does not depend on whether an
        # assertion happened to apply.
        result = _apply_codex_window_attribution_overlay(
            conn, result, source_root_keys=requested)
        # Window-account continuity fold (#341 spec §2): adopt unidentified
        # observations into a same-physical-window identified account (exactly
        # one). Physical signatures above are account-independent (computed from
        # the physical tuple), so folding after them is order-invariant; the
        # recursion path below re-fetches + re-folds. Idempotent for an
        # already-identified set — a single-account cache is a no-op.
        result = list(adopt_unidentified_observations(result))
        if latest_per_identity:
            # Python decides the winner, using the same total order every other
            # caller uses. SQL only removed rows that could not have won.
            latest: dict[QuotaWindowIdentity, QuotaObservation] = {}
            for observation in result:
                held = latest.get(observation.identity)
                if held is None or physical_order_key(
                    observation,
                ) > physical_order_key(held):
                    latest[observation.identity] = observation
            result = sorted(latest.values(), key=lambda observation: (
                observation.identity.source_root_key,
                observation.captured_at,
                observation.resets_at,
                observation.source_path,
                observation.line_offset,
            ))
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


#: The resolved alert configuration ``_quota_alert_config`` returns: the global
#: gate, the quota gate, the per-identity rules and the JSON-shaped quota block.
#: A live reconcile reads it ONCE and hands this snapshot to both scope
#: selection and ``_evaluate_quota_alerts`` (#1070), so the stored gate, the
#: disarm decision and every claim come from one read.
QuotaAlertConfig = tuple[bool, bool, tuple[QuotaRule, ...], dict]


def _quota_alert_config() -> QuotaAlertConfig:
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


def _quota_projection_for_block_at(
    history: QuotaHistory, block: QuotaBlock, at: dt.datetime,
) -> float | None:
    """The projected percent the block had AS OF ``at``, from the evidence
    captured up to then — the deferred activation's historical suppression is
    computed as of its cutoff, never from the worker's later forecast (#1070,
    Q12)."""
    forecast = forecast_quota(
        [point for point in history.physical_observations
         if point.captured_at <= at], at)
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
    journal_terminal=None, alert_config: "QuotaAlertConfig | None" = None,
    report: "dict | None" = None,
    activation_cutoffs: "Mapping[str, dt.datetime] | None" = None,
) -> list[dict]:
    """Arm or claim quota alerts within the caller's stats transaction.

    A fresh fingerprint writes only terminal backfill suppressions. Later
    eligible observations can claim an alerted row. No non-terminal state is
    stored: the arming boundary plus unique terminal event key is sufficient.

    ``alert_config`` is the caller's one config snapshot (#1070); ``None``
    reads it here, which keeps the rebuild and rederive paths unchanged.
    ``report``, when supplied, gets ``report["disarmed"] = True`` when the
    disabled-delivery disarm executed, so the caller can store the gate that
    disarm implies.

    ``activation_cutoffs`` (root → cutoff, #1070 spec §5.6) is the deferred
    activation's: the ``_codex-quota-verify`` worker completing an obligation a
    hook recorded at its own ``now``. For an identity of such a root that is
    newly activated, the arming row — and its journal record — carries the
    CUTOFF as ``activated_at_utc``; thresholds satisfied by evidence captured
    at or before it (actual, or projected from the evidence up to it) are
    claimed ``suppressed_backfill``; evidence captured after it goes through
    the normal alert path, so a crossing between the hook and the worker still
    alerts. An identity whose matching arming row already carries the cutoff is
    the retry after a crash between the arming emission and the end of the
    activation (journal replay restored the row), and it completes the missing
    pre-cutoff suppressions the same way; any other existing boundary is kept
    as is (a scheduled evaluation never resets activation time). ``None``
    keeps the evaluation-time behaviour for rebuild, rederive and inline
    callers.
    """
    global_enabled, quota_enabled, rules, config = (
        _quota_alert_config() if alert_config is None else alert_config)
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
        if report is not None:
            report["disarmed"] = True
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
        cutoff = (
            None if activation_cutoffs is None
            else activation_cutoffs.get(identity.source_root_key))
        changed, activated_at = _activate_quota_rule(
            conn, identity, fingerprint,
            now_iso if cutoff is None else _utc_iso(cutoff),
            journal_emit=journal_emit)
        # The deferred activation's own boundary: newly armed at the cutoff
        # now, or by this activation's crashed attempt (replayed row).
        completing = cutoff is not None and (changed or activated_at == cutoff)

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
            if completing:
                before = _block_observations_at_or_before(block, cutoff)
                if before:
                    actual_percent = max(point.used_percent for point in before)
                    projected_percent = _quota_projection_for_block_at(
                        history, block, cutoff)
                    for decision in quota_threshold_decisions(
                        current_percent=actual_percent,
                        projected_percent=projected_percent,
                        actual_thresholds=resolved.actual_thresholds,
                        projected_thresholds=resolved.projected_thresholds,
                    ):
                        _insert_quota_terminal_event(
                            conn, identity=identity, resets_at=block.resets_at,
                            threshold=decision.threshold, kind=decision.kind,
                            qualifying_percent=(
                                actual_percent if decision.kind == "actual"
                                else None),
                            projected_percent=(
                                projected_percent
                                if decision.kind == "projected" else None),
                            disposition="suppressed_backfill", now_iso=now_iso,
                            journal_emit=journal_terminal,
                        )
                # Evidence after the cutoff falls through to the normal path
                # below (`captured_at > activated_at`, the cutoff).
            elif changed:
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
    alert_config: "QuotaAlertConfig | None" = None,
    activation_cutoffs=None,
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

    The stored delivery gate (#1070) is the observed ``alerts_enabled`` when
    ``consume_alert_axes``; ``False`` when ``_evaluate_quota_alerts`` executed
    the disabled-delivery disarm, because a pass that deleted every arming row
    must not leave a ``True`` gate behind (the re-enable would then never widen
    and the next real crossing would be recorded as a first-activation
    backfill); otherwise the stored value carried through. ``False`` is always
    the conservative direction: it can only cause a later widening.
    ``alert_config`` is the caller's one config snapshot, passed through to
    ``_evaluate_quota_alerts``, as is ``activation_cutoffs`` (#1070, the
    deferred activation's per-root cutoffs). ``holder["disarmed"]`` reports
    whether the disarm executed, which ends the delivery generation's pending
    alert obligations after the commit.

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
    alert_report: dict = {}
    queued = _evaluate_quota_alerts(
        conn, observations=observations,
        alert_eligible_roots=alert_eligible_roots & active_roots,
        now=now, now_iso=now_iso, journal_emit=journal_emit,
        journal_disarm=journal_disarm, journal_terminal=journal_terminal,
        alert_config=alert_config, report=alert_report,
        activation_cutoffs=activation_cutoffs,
    )
    if holder is not None:
        holder["disarmed"] = bool(alert_report.get("disarmed"))
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
    #
    # #1069: EVERY reconciled root is stamped, historic ones included, so each
    # publication restamps at least one row with its fresh generation and the
    # publication witness sees it. A historic root's signature is composed from
    # its live blocks (the empty composition once they are orphaned). Both
    # readers of this table iterate the ACTIVE roots only, so the extra rows
    # change no decision; the certificate below stays active-roots-only.
    roots_to_stamp = set(roots_to_reconcile)
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
            alerts_enabled if consume_alert_axes
            else False if alert_report.get("disarmed")
            else stored_alerts_enabled),
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
            cache = _cache_connection_owner()
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


def _codex_root_stored_limit_keys(
    cache_conn: sqlite3.Connection, root: str, *, indexed: bool,
) -> "list[str]":
    """Every distinct ``logical_limit_key`` stored for one Codex root.

    With ``idx_qws_physical_group`` the keys come from successive
    ``MIN(logical_limit_key) ... > ?`` seeks over the index's
    ``(source_root_key, logical_limit_key)`` prefix, never a walk of the root's
    rows (the real store holds 14 keys over 605,847 rows: 16 ms against 6 s for
    the ``DISTINCT`` read). The seeks run inside ONE recursive statement — a
    loose index scan — so a root with many keys costs one seek per key but
    still one statement, which keeps the bounded-read guards of the routes
    that call this (the dashboard block detail) independent of the key count.
    Without the index the ``DISTINCT`` read is slower but complete; neither
    path ever returns a partial list.
    """
    if not indexed:
        return [
            str(row[0]) for row in cache_conn.execute(
                "SELECT DISTINCT logical_limit_key FROM quota_window_snapshots"
                " WHERE source='codex' AND source_root_key=?"
                "   AND logical_limit_key IS NOT NULL", (root,))
        ]
    return [
        str(row[0]) for row in cache_conn.execute(
            "WITH RECURSIVE stored_keys(limit_key) AS ("
            " SELECT (SELECT MIN(logical_limit_key) FROM quota_window_snapshots"
            "         INDEXED BY idx_qws_physical_group"
            "         WHERE source='codex' AND source_root_key=?1"
            "           AND logical_limit_key > '')"
            " UNION ALL"
            " SELECT (SELECT MIN(logical_limit_key) FROM quota_window_snapshots"
            "         INDEXED BY idx_qws_physical_group"
            "         WHERE source='codex' AND source_root_key=?1"
            "           AND logical_limit_key > stored_keys.limit_key)"
            "   FROM stored_keys WHERE stored_keys.limit_key IS NOT NULL)"
            " SELECT limit_key FROM stored_keys WHERE limit_key IS NOT NULL",
            (root,))
    ]


def codex_unit_loading_closure(
    cache_conn: sqlite3.Connection, units: Iterable[tuple],
    *, stored_keys: "dict[str, list[str]] | None" = None,
) -> frozenset[tuple]:
    """The raw groups holding the COMPLETE population of each loading unit.

    #1071. A loading unit is pool-free (``loading_unit_from_raw`` strips
    ``modelPool``), but rows are stored under whichever key spelling ingest
    minted: a Spark row under the ``modelPool`` key, an ordinary row under the
    plain one, and either with the weekly minute jitter in the key. The loader
    matches STORED coordinates exactly, so asking for one spelling hands the
    projector part of a unit — and the sweep, which is keyed by the unit,
    orphans every block of it the partial load did not re-stamp.

    Per involved root the distinct stored keys are discovered once per call
    (``_codex_root_stored_limit_keys``), and every key whose snapped,
    pool-stripped value is the unit's key is kept alongside the unit's own key.
    Those are closed over the existing minute-column closure
    (``snap_equivalent_raw_groups``) with the unit's exact root, slot and reset,
    so a unit no stored key survives in still names itself. The caller supplies
    the connection, which is what lets discovery and loading share one snapshot.
    ``stored_keys`` is an optional per-snapshot cache of that discovery, keyed
    by root, which the interleave closure of the same preparation shares.
    """
    by_root: "dict[str, set[tuple]]" = {}
    for unit in units:
        root, limit_key, slot, minutes, reset = unit
        by_root.setdefault(str(root), set()).add(
            (str(root), str(limit_key), str(slot), int(minutes), str(reset)))
    if not by_root:
        return frozenset()
    indexed = cache_conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index'"
        "   AND name='idx_qws_physical_group'").fetchone() is not None
    if stored_keys is None:
        stored_keys = {}
    groups: "set[tuple]" = set()
    for root, root_units in by_root.items():
        if root not in stored_keys:
            stored_keys[root] = _codex_root_stored_limit_keys(
                cache_conn, root, indexed=indexed)
        spellings: "dict[str, set[str]]" = {}
        for key in stored_keys[root]:
            spellings.setdefault(
                _ledger.strip_model_pool(snap_window_minutes(key)),
                set()).add(key)
        for _root, unit_key, slot, minutes, reset in root_units:
            for key in spellings.get(unit_key, set()) | {unit_key}:
                groups.add((root, key, slot, minutes, reset))
    return _ledger.snap_equivalent_raw_groups(groups)


#: One stored prefix's canonical anchors and the textual capture extrema of
#: each, all by seeks on ``idx_qws_physical_group`` (#1071 Q20): a loose index
#: scan over the anchor member (spelled exactly as indexed — any other
#: spelling buys no seek), then a ``MIN`` and a ``MAX`` of ``captured_at_utc``
#: — the member after the anchor — per anchor, each one seek. Every statement
#: here is a literal, so the detector-access call graph reads it as the cache
#: read it is.
#:
#: The fourth column is the per-window spelling probe: whether ANY capture of
#: the window is outside the spelling ``_lib_quota_ledger._SUPPORTED_CAPTURE``
#: vouches for (the predicate is that pattern, ASCII digits only, so it can
#: only flag more). The extrema bound only rows spelled that way: an accepted
#: non-UTC offset strictly between two well-formed extrema (``…T00:05:00-02:00``
#: is 02:05 UTC) parses outside their span. Every column the probe reads is an
#: index member, so it walks index entries only and stops at the first such
#: row, but a window with none walks all of its entries once (measured about
#: 113 ms per 300,000-row window on the LAN runner; the extrema alone are
#: seeks).
_CODEX_PREFIX_ANCHOR_SPANS_SQL = (
    "WITH RECURSIVE anchors(epoch) AS ("
    " SELECT (SELECT MIN(unixepoch(COALESCE(canonical_resets_at_utc, resets_at_utc)))"
    "           FROM quota_window_snapshots INDEXED BY idx_qws_physical_group"
    "          WHERE source='codex' AND source_root_key=?1"
    "            AND logical_limit_key=?2 AND observed_slot=?3"
    "            AND window_minutes=?4"
    "            AND unixepoch(COALESCE(canonical_resets_at_utc, resets_at_utc)) IS NOT NULL)"
    " UNION ALL"
    " SELECT (SELECT MIN(unixepoch(COALESCE(canonical_resets_at_utc, resets_at_utc)))"
    "           FROM quota_window_snapshots INDEXED BY idx_qws_physical_group"
    "          WHERE source='codex' AND source_root_key=?1"
    "            AND logical_limit_key=?2 AND observed_slot=?3"
    "            AND window_minutes=?4"
    "            AND unixepoch(COALESCE(canonical_resets_at_utc, resets_at_utc)) > anchors.epoch)"
    "   FROM anchors WHERE anchors.epoch IS NOT NULL)"
    " SELECT epoch,"
    "  (SELECT MIN(captured_at_utc)"
    "     FROM quota_window_snapshots INDEXED BY idx_qws_physical_group"
    "    WHERE source='codex' AND source_root_key=?1"
    "      AND logical_limit_key=?2 AND observed_slot=?3"
    "      AND window_minutes=?4"
    "      AND unixepoch(COALESCE(canonical_resets_at_utc, resets_at_utc))=anchors.epoch),"
    "  (SELECT MAX(captured_at_utc)"
    "     FROM quota_window_snapshots INDEXED BY idx_qws_physical_group"
    "    WHERE source='codex' AND source_root_key=?1"
    "      AND logical_limit_key=?2 AND observed_slot=?3"
    "      AND window_minutes=?4"
    "      AND unixepoch(COALESCE(canonical_resets_at_utc, resets_at_utc))=anchors.epoch),"
    "  EXISTS (SELECT 1"
    "     FROM quota_window_snapshots INDEXED BY idx_qws_physical_group"
    "    WHERE source='codex' AND source_root_key=?1"
    "      AND logical_limit_key=?2 AND observed_slot=?3"
    "      AND window_minutes=?4"
    "      AND unixepoch(COALESCE(canonical_resets_at_utc, resets_at_utc))=anchors.epoch"
    "      AND NOT (substr(captured_at_utc, 1, 19) GLOB"
    "        '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]'"
    "        AND (substr(captured_at_utc, 20) IN ('Z', '+00:00')"
    "          OR (substr(captured_at_utc, 20, 1) = '.'"
    "            AND ((substr(captured_at_utc, -1) = 'Z'"
    "                  AND length(captured_at_utc) > 21"
    "                  AND rtrim(substr(captured_at_utc, 21,"
    "                                   length(captured_at_utc) - 21),"
    "                            '0123456789') = '')"
    "              OR (substr(captured_at_utc, -6) = '+00:00'"
    "                  AND length(captured_at_utc) > 26"
    "                  AND rtrim(substr(captured_at_utc, 21,"
    "                                   length(captured_at_utc) - 26),"
    "                            '0123456789') = '')))))"
    "    LIMIT 1)"
    "  FROM anchors WHERE epoch IS NOT NULL"
)

#: Without the index: the same answer for one stored prefix, by a read of its
#: rows (correct and slower; never a partial list).
_CODEX_PREFIX_ANCHOR_SPANS_FALLBACK_SQL = (
    "SELECT unixepoch(COALESCE(canonical_resets_at_utc, resets_at_utc)) AS epoch,"
    "       MIN(captured_at_utc), MAX(captured_at_utc),"
    "       MAX(NOT (substr(captured_at_utc, 1, 19) GLOB"
    "        '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]'"
    "        AND (substr(captured_at_utc, 20) IN ('Z', '+00:00')"
    "          OR (substr(captured_at_utc, 20, 1) = '.'"
    "            AND ((substr(captured_at_utc, -1) = 'Z'"
    "                  AND length(captured_at_utc) > 21"
    "                  AND rtrim(substr(captured_at_utc, 21,"
    "                                   length(captured_at_utc) - 21),"
    "                            '0123456789') = '')"
    "              OR (substr(captured_at_utc, -6) = '+00:00'"
    "                  AND length(captured_at_utc) > 26"
    "                  AND rtrim(substr(captured_at_utc, 21,"
    "                                   length(captured_at_utc) - 26),"
    "                            '0123456789') = ''))))))"
    "  FROM quota_window_snapshots"
    " WHERE source='codex' AND source_root_key=? AND logical_limit_key=?"
    "   AND observed_slot=? AND window_minutes=?"
    "   AND unixepoch(COALESCE(canonical_resets_at_utc, resets_at_utc)) IS NOT NULL"
    " GROUP BY epoch"
)

#: Every capture of one window, for a spelling the endpoint shortcut cannot
#: vouch for (indexed, and without the index).
_CODEX_ANCHOR_CAPTURES_SQL = (
    "SELECT captured_at_utc"
    "  FROM quota_window_snapshots INDEXED BY idx_qws_physical_group"
    " WHERE source='codex' AND source_root_key=? AND logical_limit_key=?"
    "   AND observed_slot=? AND window_minutes=?"
    "   AND unixepoch(COALESCE(canonical_resets_at_utc, resets_at_utc))=?"
)
_CODEX_ANCHOR_CAPTURES_FALLBACK_SQL = (
    "SELECT captured_at_utc FROM quota_window_snapshots"
    " WHERE source='codex' AND source_root_key=? AND logical_limit_key=?"
    "   AND observed_slot=? AND window_minutes=?"
    "   AND unixepoch(COALESCE(canonical_resets_at_utc, resets_at_utc))=?"
)


def _reset_anchor_node(reset: object) -> object:
    """The canonical anchor one unit's reset names, as ``unixepoch`` reads it.

    The loader's shards match ``unixepoch(<anchor>) = unixepoch(?)``, so a
    whole-second epoch is the coordinate two spellings of one window share. A
    value Python cannot read as an aware instant stays its own text node, so it
    still compares equal to itself.
    """
    try:
        parsed = dt.datetime.fromisoformat(str(reset).replace("Z", "+00:00"))
    except ValueError:
        return ("text", str(reset))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return ("text", str(reset))
    return int(math.floor(parsed.timestamp()))


def _unit_from_key_text(text: str) -> "tuple | None":
    """Parse a serialized loading unit (``physical_group_key_text``)."""
    parts = str(text).split("\x1f")
    if len(parts) != 5:
        return None
    try:
        minutes = int(parts[3])
    except ValueError:
        return None
    return (parts[0], parts[1], parts[2], minutes, parts[4])


@dataclass(frozen=True)
class _CodexHistoricalSpans:
    """The published interpreted span of every Codex block, by family (#1071).

    Read in the preparation's stats snapshot, beside the witness, so a
    publication after it is a witness mismatch and the in-lock re-preparation
    reads them again with the closure. ``by_family`` maps ``(root, pool-free
    key, slot, minutes)`` to ``{anchor node: spans | None}``; ``None`` marks a
    previously materialized window whose stored bounds cannot be trusted,
    which brings its whole family. ``unit_tuples`` names the stored unit of each
    ``(family, node)``, so a window known only from its blocks is swept under
    the key its blocks carry. An unreadable read (``readable=False``) knows of
    no window and brings every affected family whole.
    """

    readable: bool
    by_family: "Mapping[tuple, Mapping[object, tuple | None]]"
    unit_tuples: "Mapping[tuple, frozenset]"


def _codex_historical_block_spans(
    stats_conn: sqlite3.Connection,
) -> _CodexHistoricalSpans:
    """Every Codex block's published ``[first, last]`` observed span (#1071 Q19).

    Every account and pool block of a unit and orphans too: a deleted
    duplicate run keeps an interpreted representative inside its block's span,
    so any window whose dedup the deletion changes still intersects it. One
    read of ``quota_window_blocks`` (a row per window and account, not per
    observation).
    """
    try:
        rows = stats_conn.execute(
            "SELECT physical_group_key, first_observed_at_utc, "
            "       last_observed_at_utc FROM quota_window_blocks "
            " WHERE source='codex'").fetchall()
    except sqlite3.Error:
        return _CodexHistoricalSpans(readable=False, by_family={}, unit_tuples={})
    by_family: "dict[tuple, dict[object, list | None]]" = {}
    unit_tuples: "dict[tuple, set]" = {}
    for key_text, first, last in rows:
        if key_text is None:
            continue  # `stale_reverse_map` already routes the pass in full
        unit = _unit_from_key_text(key_text)
        if unit is None:
            # A block whose unit cannot be placed in a family could sit in
            # any of them, so no family's bounds can be trusted.
            return _CodexHistoricalSpans(
                readable=False, by_family={}, unit_tuples={})
        family, node = unit[:4], _reset_anchor_node(unit[4])
        unit_tuples.setdefault((family, node), set()).add(unit)
        nodes = by_family.setdefault(family, {})
        try:
            start = _parse_utc(str(first), "first_observed_at_utc").timestamp()
            end = _parse_utc(str(last), "last_observed_at_utc").timestamp()
        except (TypeError, ValueError):
            nodes[node] = None
            continue
        if start > end:
            nodes[node] = None
        elif nodes.get(node, ()) is not None:
            nodes.setdefault(node, []).append((start, end))
    return _CodexHistoricalSpans(
        readable=True,
        by_family={
            family: {
                node: (None if spans is None else tuple(spans))
                for node, spans in nodes.items()}
            for family, nodes in by_family.items()},
        unit_tuples={
            key: frozenset(units) for key, units in unit_tuples.items()},
    )


def _codex_anchor_complete_span(
    cache_conn: sqlite3.Connection, prefix: tuple, epoch: int, *, indexed: bool,
) -> "tuple[float, float] | None":
    """One window's capture span from every row, for a spelling the
    endpoint shortcut cannot vouch for (#1071 Q20). Rows Python cannot read
    are skipped, exactly as the loader skips them."""
    params = (*prefix, int(epoch))
    if indexed:
        rows = cache_conn.execute(_CODEX_ANCHOR_CAPTURES_SQL, params)
    else:
        rows = cache_conn.execute(_CODEX_ANCHOR_CAPTURES_FALLBACK_SQL, params)
    start = end = None
    for (captured,) in rows:
        try:
            instant = _parse_utc(str(captured), "captured_at_utc").timestamp()
        except (TypeError, ValueError):
            continue
        start = instant if start is None else min(start, instant)
        end = instant if end is None else max(end, instant)
    return None if start is None else (start, end)


def codex_interleave_closure(
    cache_conn: sqlite3.Connection, seeds: Iterable[tuple], *,
    historical: "_CodexHistoricalSpans | None",
    expandable_roots: "set[str] | frozenset[str] | None",
    limit: int = _MAX_INCREMENTAL_UNITS,
    stored_keys: "dict[str, list[str]] | None" = None,
) -> "tuple[frozenset[tuple], int] | None":
    """The interleave closure of a scoped pass's units (#1071, Q18–Q20).

    ``build_history`` collapses adjacent equal ``logical_value_tuple``s over an
    identity's WHOLE physical order, and the canonical reset is part of that
    value, so another window of the identity captured inside a unit's span
    breaks the unit's duplicate runs. ``force_full`` sees those separators; a
    scoped load of the dirty unit alone does not, and its block's interpreted
    endpoint differs. The fix is to load the separators, never to change the
    dedup.

    Within each seed's family ``(root, pool-free key, slot, minutes)`` — every
    account and pool, so a superset of the identities it holds — each window's
    interval is its current capture span (seeks, outward-rounded) united with
    the published span of each of its blocks; every window connected to a seed
    through intersecting closed intervals joins, transitively. A previously
    materialized window with untrustworthy bounds brings its whole family. Only
    families on ``expandable_roots`` (``None``: every root) are expanded.

    Returns ``(units, nodes)`` — the unit tuples to load and sweep and the
    number of distinct canonical windows they name — or ``None`` once more
    than ``limit`` windows are established, before anything is loaded.
    """
    seeds = set(seeds)
    families: "dict[tuple, set[tuple]]" = {}
    other_nodes: "set[object]" = set()
    for unit in seeds:
        if expandable_roots is None or unit[0] in expandable_roots:
            families.setdefault(tuple(unit[:4]), set()).add(unit)
        else:
            other_nodes.add((tuple(unit[:4]), _reset_anchor_node(unit[4])))
    result: "set[tuple]" = set(seeds)
    count = len(other_nodes)
    if count + sum(
            len({_reset_anchor_node(u[4]) for u in members})
            for members in families.values()) > limit:
        return None
    if historical is None:
        historical = _CodexHistoricalSpans(readable=True, by_family={},
                                           unit_tuples={})
    if stored_keys is None:
        stored_keys = {}
    indexed = cache_conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index'"
        "   AND name='idx_qws_physical_group'").fetchone() is not None
    for family in sorted(families):
        root, family_key, slot, minutes = family
        if root not in stored_keys:
            stored_keys[root] = _codex_root_stored_limit_keys(
                cache_conn, root, indexed=indexed)
        keys = {
            key for key in stored_keys[root]
            if _ledger.strip_model_pool(snap_window_minutes(key)) == family_key
        } | {family_key}
        intervals: "dict[object, list[tuple[float, float]]]" = {}
        for key in sorted(keys):
            for raw_minutes in codex_snap_equivalent_window_minutes(minutes):
                prefix = (root, key, slot, int(raw_minutes))
                if indexed:
                    rows = cache_conn.execute(
                        _CODEX_PREFIX_ANCHOR_SPANS_SQL, prefix).fetchall()
                else:
                    rows = cache_conn.execute(
                        _CODEX_PREFIX_ANCHOR_SPANS_FALLBACK_SQL,
                        prefix).fetchall()
                for epoch, low_text, high_text, unvouched in rows:
                    node = int(epoch)
                    span = (
                        None if unvouched
                        else _ledger.outward_capture_span(low_text, high_text))
                    if span is None:
                        span = _codex_anchor_complete_span(
                            cache_conn, prefix, node, indexed=indexed)
                    spans = intervals.setdefault(node, [])
                    if span is not None:
                        spans.append(span)
        whole_family = not historical.readable
        for node, spans in historical.by_family.get(family, {}).items():
            if spans is None:
                whole_family = True
                intervals.setdefault(node, [])
            else:
                intervals.setdefault(node, []).extend(spans)
        seed_nodes = {_reset_anchor_node(unit[4]) for unit in families[family]}
        selected = (
            set(intervals) | seed_nodes if whole_family
            else set(_ledger.interleave_closure(intervals, seed_nodes)))
        count += len(selected)
        if count > limit:
            return None
        for node in selected:
            if isinstance(node, int):
                result.add((root, family_key, slot, minutes,
                            dt.datetime.fromtimestamp(node, UTC).isoformat()))
            result.update(historical.unit_tuples.get((family, node), ()))
    return frozenset(result), count


def _resolve_pass_scope(
    cache_conn: sqlite3.Connection, *, force_full: bool,
    ledger_state: "dict | None", ledger_high: "int | None", watermark: int,
    active_roots: set[str], stale_reverse_map: bool,
    verification_due: bool = False,
    attribution_scope: "dict | None" = None,
    historical_spans: "_CodexHistoricalSpans | None" = None,
) -> "tuple[dict | None, int | None]":
    """Decide between a bounded pass and a whole-history one.

    Returns ``(scope, watermark_target)``. ``scope`` is ``None`` for a full pass
    or a dict carrying the exact raw groups to load and the loading units to
    sweep, plus ``seed_unit_count`` (the ledger and attribution units),
    ``closure_added_unit_count`` and ``final_unit_count`` (#1071: the
    interleave closure's windows, the number counted against the ceiling).
    ``watermark_target`` is the sequence the pass will stamp, or ``None`` when
    there is no ledger to stamp against.

    ``attribution_scope`` (``codex_attribution_projection_scope``) joins the
    ledger's units as seeds — a union, never a replacement — and
    ``historical_spans`` are the published block spans (``None``: no block is
    known), read by the preparation in its stats snapshot beside the witness
    (#1071 Q20): this routine runs inside the cache query session and reads
    no stats statement of its own (#787 / #861).

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
      a first ingest — where one unbounded scan beats N indexed seeks. Since
      #1071 the count is the interleave closure's (seeds, vanished seeds and
      added windows), established before anything is loaded.
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
    #
    # #1071: the units are CANONICAL — snapped and pool-stripped — so two
    # stored spellings of one window (its ordinary and its `modelPool` key)
    # are one unit, counted once against the ceiling before any spelling is
    # expanded for loading.
    unit_tuples = {_ledger.loading_unit_from_raw(group) for group in raw_groups}
    attribution_raw_groups: "frozenset[tuple]" = frozenset()
    if attribution_scope:
        # Union, never replace: the ledger's own dirty set is still owed.
        attribution_raw_groups = frozenset(attribution_scope["raw_groups"])
        for text in attribution_scope["units"]:
            unit = _unit_from_key_text(text)
            if unit is not None:
                unit_tuples.add(unit)
    seed_units = {_ledger.physical_group_key_text(unit) for unit in unit_tuples}
    if len(seed_units) > _MAX_INCREMENTAL_UNITS:
        return None, ledger_high
    # #1071 (Q18–Q20): the interleave closure. Another window of a unit's
    # identity captured inside the unit's span breaks its duplicate runs in
    # `build_history`, so the scope loads and sweeps every window connected to
    # a seed, counted against the ceiling before any row is loaded.
    stored_keys: "dict[str, list[str]]" = {}
    closure = codex_interleave_closure(
        cache_conn, unit_tuples, historical=historical_spans,
        expandable_roots=set(active_roots) if active_roots else None,
        stored_keys=stored_keys)
    if closure is None:
        return None, ledger_high
    closure_units, closure_count = closure
    units = {_ledger.physical_group_key_text(unit) for unit in closure_units}
    load_units = closure_units
    if active_roots:
        load_units = {unit for unit in closure_units if unit[0] in active_roots}
    return (
        {
            # The loader matches RAW stored coordinates, so the request has to
            # enumerate every spelling stored for a dirty unit: the minute
            # jitter that lives in BOTH the limit key and a column, and the
            # `modelPool` key a sibling pool's rows sit under (#1071). Loading
            # only the mutated spelling would hand the fold a PARTIAL
            # population and the unit-keyed sweep would orphan the rest.
            "raw_groups": codex_unit_loading_closure(
                cache_conn, load_units, stored_keys=stored_keys)
            | attribution_raw_groups,
            "units": units,
            "seed_unit_count": len(seed_units),
            "closure_added_unit_count": max(
                0, closure_count - len(seed_units)),
            "final_unit_count": closure_count,
        },
        ledger_high,
    )


class _UnreadableWitness:
    """A publication witness whose read failed (#1069).

    It never compares equal — not even to itself — so a leg holding one always
    re-prepares rather than trusting a preparation it cannot validate."""

    __slots__ = ()

    def __eq__(self, other: object) -> bool:
        return False

    def __ne__(self, other: object) -> bool:
        return True

    __hash__ = None  # type: ignore[assignment]

    def __repr__(self) -> str:
        return "_UnreadableWitness()"


def _codex_projection_witness(
    stats_conn: sqlite3.Connection,
) -> "tuple | _UnreadableWitness":
    """Everything a Codex quota publication changes in stats.db (#1069, Q2).

    A deterministic tuple of: the raw ``quota_projection_ledger_state`` row for
    ``source='codex'`` or an explicit absent marker; every
    ``quota_projection_state`` row ``(source_root_key, account_key, generation,
    physical_signature)``, ordered; the ``stats_publication_stamp`` rows with the
    stats ``user_version`` (a rebuild publishes a new stamp); and every Codex
    ``quota_alert_arming`` row the alert scope reads, ordered.

    Every publication restamps ``quota_projection_state`` with a fresh random
    generation for every reconciled root, so ANY publication between a
    preparation and its apply changes this value, including one that wrote
    semantically identical rows. Any read error yields an unreadable witness
    that never compares equal.
    """
    try:
        ledger = stats_conn.execute(
            "SELECT * FROM quota_projection_ledger_state WHERE source='codex'"
        ).fetchone()
        state = tuple(
            tuple(row) for row in stats_conn.execute(
                "SELECT source_root_key, account_key, generation, "
                "       physical_signature FROM quota_projection_state "
                " ORDER BY source_root_key, account_key"))
        stamps = tuple(
            tuple(row) for row in stats_conn.execute(
                "SELECT record_path, started_at_utc, stamped_at_utc "
                "  FROM stats_publication_stamp ORDER BY rowid"))
        user_version = int(
            stats_conn.execute("PRAGMA user_version").fetchone()[0])
        arming = tuple(
            tuple(row) for row in stats_conn.execute(
                "SELECT source_root_key, account_key, logical_limit_key, "
                "       observed_slot, window_minutes, rule_fingerprint, "
                "       activated_at_utc FROM quota_alert_arming "
                " WHERE source='codex' "
                " ORDER BY source_root_key, account_key, logical_limit_key, "
                "          observed_slot, window_minutes"))
    except (sqlite3.Error, TypeError, ValueError):
        return _UnreadableWitness()
    return (
        ("absent",) if ledger is None else ("present", tuple(ledger)),
        state,
        (user_version, stamps),
        arming,
    )


def _codex_witnesses_equal(prepared: object, live: object) -> bool:
    """The leg's one witness comparison. A module-level seam on purpose: the
    removed-validation mutation run replaces it to prove the races it guards
    are real."""
    return prepared == live


def _witness_projection_rows(witness: object):
    """The ``(source_root_key, physical_signature)`` rows a readable witness
    holds, or ``None`` for an unreadable one."""
    if not isinstance(witness, tuple):
        return None
    return [(row[0], row[3]) for row in witness[1]]


@dataclass(frozen=True)
class _PreparedPass:
    """One complete preparation of a Codex quota pass (#1069, spec §5.1).

    Everything the apply and the finalization consume, so a re-preparation
    replaces ALL of it: the config snapshot, the stats witness and the stats
    inputs, the cache inputs, the scope, the observations, the watermark, the
    alert-axis decisions and the requested worker hand-offs.

    ``disposition`` is one of ``"apply"``, ``"current"``, ``"deferred_verify"``,
    ``"deferred_model"`` and ``"unavailable"`` (the cache could not be
    opened). ``reconcile_roots`` is ``None`` for the genuine whole-history
    shape and ``dirty_units`` ``None`` for any complete (non-scoped) pass,
    exactly as ``_apply_quota_projection_rows`` reads them.

    ``alert_obligations`` (#1070, spec §5.6) is the complete-history alert
    work a ``"defer"`` pass hands to the worker instead of widening, as
    ``(reason, roots)`` pairs (empty roots: every applicable root); the leg
    records them in the sidecar before it applies anything.
    """

    disposition: str
    alert_config: "QuotaAlertConfig | None" = None
    witness: object = None
    ledger_state: "dict | None" = None
    has_arming: bool = False
    stale_reverse_map: bool = True
    alert_scope: object = None
    active_roots: "frozenset[str]" = frozenset()
    physical_seq: int = 0
    certificate: object = None
    ledger_high: "int | None" = None
    attribution_revision: int = 0
    model_debt: bool = False
    scope: "dict | None" = None
    reconcile_roots: "frozenset[str] | None" = None
    observations: "tuple[QuotaObservation, ...]" = ()
    watermark_target: "int | None" = None
    consume_alert_axes: bool = False
    complete_roots: "frozenset[str]" = frozenset()
    schedule_evaluated_roots: "frozenset[str]" = frozenset()
    worker_requests: "tuple[str, ...]" = ()
    dirty_units: "frozenset[str] | None" = None
    alert_eligible_roots: "frozenset[str]" = frozenset()
    prune_inactive_schedule: bool = False
    alert_obligations: "tuple[tuple[str, frozenset[str]], ...]" = ()


def _prepare_codex_quota_pass(
    *,
    stats_conn: sqlite3.Connection,
    source_root_keys,
    alert_eligible_root_keys: "frozenset[str]",
    now: dt.datetime,
    force_full: bool,
    full_pass: str,
    in_lock: bool,
    extra_active_roots: "frozenset[str]" = frozenset(),
) -> _PreparedPass:
    """Prepare one Codex quota pass; the optimistic path and the in-lock
    re-preparation are this one routine (#1069, Q1).

    Order (spec §5.1, Q2), which is what makes the witness sound:

    1. the alert configuration, read ONCE (#1070);
    2. ONE stats read snapshot: the witness, ``_ledger_state``, ``has_arming``,
       the reverse-map flag and the alert scope's inputs. Outside the lock it
       is a read transaction on ``stats_conn`` (the caller's fresh
       connection); under the lock it is ``ctx.conn`` inside the cycle's own
       transaction, which this routine never begins, commits or closes;
    3. ONE cache read snapshot opened after step 2 and held through the
       metadata, ``_ledger_rows_after``, the attribution scope, the interleave
       closure and the unit closure, and ``load_codex_quota_observations``,
       then closed.

    The published block spans the interleave closure reads (#1071 Q20) are
    part of step 2 too, read beside the witness for every pass that is not
    ``force_full`` (a complete pass has no closure) and handed into step 3
    as ``historical_spans``. Step 3 is one cache query session, under
    ``query`` authority for its whole duration, so no stats statement may run
    inside it (#787 / #861 R_COVERAGE_USE). The in-lock re-preparation
    repeats both reads in the cycle's transaction, beside the witness it is
    validated against.

    A publication landing after step 2 is visible to the leg's validation, and
    a cache input read in step 3 can only be newer than the stats state the
    witness describes — later incremental work, which the finalization fences
    keep owed.

    Every rule the reconcile applied before (the short-circuit, the interval
    hand-off, the catch-all hand-off, the attribution union, the axis
    consumption) is kept verbatim, and so is the alert widening under
    ``"inline"``. Under ``"defer"`` (#1070, spec §5.6, Q11) the alert axes never
    widen: complete-history alert work becomes ``alert_obligations`` on the
    bounded pass, so no hook preparation, optimistic or in the lock, loads a
    complete root history for an alert reason. Worker hand-offs are REQUESTED
    in ``worker_requests`` and run by the caller after ``run_stats_ingest``
    returns, never inside a transaction.

    ``extra_active_roots`` joins the cache's roots when ``source_root_keys`` is
    ``None``: the activation worker's configured roots, so a configured root
    with no retained observation is still a reconciled root whose obligation
    the pass can complete (and an empty store still stores the gate).
    """
    alert_eligible_roots = frozenset(str(key) for key in alert_eligible_root_keys)
    # ONE read (#1070): this snapshot decides the alert scope below AND the
    # apply's disarm, claims and stored gate, so a config change landing
    # mid-pass cannot split them. A re-preparation reads a new one.
    alert_config = _quota_alert_config()
    global_alerts_enabled, quota_alerts_enabled, _rules, _config = alert_config
    delivery_enabled = global_alerts_enabled and quota_alerts_enabled
    fields: dict = {
        "alert_config": alert_config,
        "alert_eligible_roots": alert_eligible_roots,
    }

    # ── step 2: the stats snapshot, BEFORE the cache one ────────────────────
    own_stats_txn = not in_lock and not stats_conn.in_transaction
    if own_stats_txn:
        stats_conn.execute("BEGIN")
    try:
        witness = _codex_projection_witness(stats_conn)
        ledger_state = _ledger_state(stats_conn)
        try:
            has_arming = bool(stats_conn.execute(
                "SELECT 1 FROM quota_alert_arming "
                "WHERE source='codex' LIMIT 1"
            ).fetchone())
        except sqlite3.Error:
            has_arming = True  # fail-safe: never skip a possible disarm
        stale_reverse_map = _blocks_missing_reverse_map(stats_conn)
        # #1069 Q4: the stats half of the empty-store test, read in this
        # snapshot beside the witness. Only a hook (`defer`) pass asks, and
        # only when the witness already holds no projection-state row, so an
        # established store never pays for the probes.
        stats_projection_empty = (
            full_pass == "defer" and not force_full
            and _witness_projection_rows(witness) == []
            and not _codex_projected_rows_present(stats_conn))
        alert_scope = None
        scope_detail: dict = {}
        if alert_eligible_roots:
            alert_scope = _resolve_alert_scope(
                stats_conn, ledger_scope=_axes.SCOPE_GROUPS, now=now,
                ledger_state=ledger_state,
                global_enabled=global_alerts_enabled,
                quota_enabled=quota_alerts_enabled,
                rules=_rules, config=_config,
                eligible_roots=set(alert_eligible_roots),
                defer_scheduled=(full_pass == "defer"),
                detail=scope_detail,
            )
        # #1071 (Q20): the closure's published spans, in this snapshot beside
        # the witness, never inside the cache session below (#787 / #861). A
        # complete pass has no closure; an unreadable read is
        # `readable=False`, which brings every affected family whole.
        historical_spans = (
            None if force_full else _codex_historical_block_spans(stats_conn))
    finally:
        if own_stats_txn:
            stats_conn.commit()
    fields.update(
        witness=witness, ledger_state=ledger_state, has_arming=has_arming,
        stale_reverse_map=stale_reverse_map, alert_scope=alert_scope)

    # ── step 3: one cache snapshot, held through the observation load ───────
    try:
        cache = _cache_connection()
    except (FileNotFoundError, sqlite3.Error):
        return _PreparedPass(disposition=PROJECTION_UNAVAILABLE, **fields)
    try:
        cache.execute("BEGIN")
        try:
            return _prepare_from_cache_snapshot(
                cache, fields,
                source_root_keys=source_root_keys,
                alert_eligible_roots=alert_eligible_roots,
                delivery_enabled=delivery_enabled,
                now=now, force_full=force_full, full_pass=full_pass,
                in_lock=in_lock, scope_detail=scope_detail,
                extra_active_roots=frozenset(extra_active_roots),
                stats_projection_empty=stats_projection_empty,
                historical_spans=historical_spans)
        finally:
            cache.commit()
    except sqlite3.Error:
        return _PreparedPass(disposition=PROJECTION_UNAVAILABLE, **fields)
    finally:
        cache.close()


def _prepare_from_cache_snapshot(
    cache: sqlite3.Connection, fields: dict, *, source_root_keys,
    alert_eligible_roots: "frozenset[str]", delivery_enabled: bool,
    now: dt.datetime, force_full: bool, full_pass: str, in_lock: bool,
    scope_detail: "dict | None" = None,
    extra_active_roots: "frozenset[str]" = frozenset(),
    historical_spans: "_CodexHistoricalSpans | None" = None,
    stats_projection_empty: bool = False,
) -> _PreparedPass:
    """Step 3 of ``_prepare_codex_quota_pass``, on its held cache snapshot."""
    ledger_state = fields["ledger_state"]
    stale_reverse_map = fields["stale_reverse_map"]
    alert_scope = fields["alert_scope"]
    has_arming = fields["has_arming"]
    worker_requests: "list[str]" = []

    def done(disposition: str, **extra) -> _PreparedPass:
        return _PreparedPass(
            disposition=disposition,
            worker_requests=tuple(worker_requests), **{**fields, **extra})

    active_roots = frozenset(
        set(_cache_root_keys(cache)) | set(extra_active_roots)
        if source_root_keys is None else {str(key) for key in source_root_keys}
    )
    physical_sequence = codex_physical_mutation_seq(cache)
    certificate = load_codex_quota_projection_certificate(cache)
    ledger_high = _ledger_max_seq(cache)
    # #500 §8.3. An attribution change fires no `quota_window_snapshots`
    # trigger and writes no ledger entry, so the change ledger cannot see it
    # and the certificate reader above has already declined a certificate
    # computed against a different revision. What is left is naming the groups
    # the pass now owes, which is only worth reading when the revision moved.
    attribution_revision = (
        _cache_module().codex_window_attribution_revision(cache))
    # #980 D6: quota projection requires established model resolution. While
    # the `model` flag, an unexpanded `full` or a malformed debt record
    # stands, no projection row and no certificate may be published —
    # interpreting a still-resolvable NULL `observed_model` would file a Spark
    # window as account weekly quota (#373).
    model_pending = _cache_module().codex_model_resolution_pending(cache)
    # #1070 (spec §5.6): the complete-history alert work this `"defer"` pass
    # owes, decided before any disposition so every outcome — a deferral or
    # a model fence included — reports it. `force_full` (no hook caller)
    # stays a complete inline pass.
    alert_obligations = (
        _deferred_alert_obligations(
            alert_scope, scope_detail or {},
            alert_eligible_roots & active_roots)
        if full_pass == "defer" and not force_full else ())
    fields.update(
        active_roots=active_roots, physical_seq=physical_sequence,
        certificate=certificate, ledger_high=ledger_high,
        attribution_revision=attribution_revision, model_debt=model_pending,
        alert_obligations=alert_obligations)
    if model_pending:
        worker_requests.append("model")
        return done(PROJECTION_DEFERRED_MODEL)
    # #1069 Q4: "`current` (certificate-backed nothing owed, or an empty
    # store)". With no Codex quota observation for any active root in this
    # cache snapshot and no projected row (projection state, block, milestone
    # or threshold event, any root) in the stats one, a complete pass would
    # materialize nothing and orphan nothing, so the hook pass is current.
    # Without this a fresh install has no projector state, the catch-all below
    # hands its whole-history pass to the worker as `deferred_verify`, and the
    # hook withholds lifecycle success on every turn until a worker runs.
    #
    # It stamps NOTHING — no watermark, no `last_full_pass_at`, no
    # interpretation version, no gate — because stamping is a stats write and
    # this pass applies none. So the store stays never-verified: once an
    # observation arrives the next pass is not empty and takes exactly the
    # route it took before this rule (under `defer`, the worker hand-off). A
    # store whose observations vanished while stats still holds a projected
    # row is not empty, because only a complete pass can orphan that row.
    # Alert obligations derived above still stand and still report pending.
    # `inline` and `force_full` never read as empty (step 2 gates the probe).
    if stats_projection_empty and not _codex_observations_present(
            cache, active_roots):
        return done(PROJECTION_CURRENT)
    attribution_stale =_certificate_attribution_revision(
        _codex_quota_projection_certificate_payload(cache)
    ) != attribution_revision
    attribution_scope = (
        codex_attribution_projection_scope(cache) if attribution_stale else None)
    # The certificate comparison reads the projection-state rows the witness
    # already holds: one stats read decides both (an unreadable witness is a
    # mismatch, the fail-safe direction).
    witness_rows = _witness_projection_rows(fields["witness"])
    signatures_match = (
        certificate is not None and witness_rows is not None
        and _projection_signatures_match_rows(
            witness_rows, active_roots, certificate[1])
    )

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
        # except this one. On a hook-only install the whole-history reconcile
        # would land on the blocking hook path once a day against Codex's
        # 30-second timeout, so it is handed to a detached worker — requested
        # here, spawned by the caller once the ingest has returned (#1069 Q4).
        #
        # Skipping it on a failed spawn is deliberate: the deadline is only
        # stamped by a pass that COMPLETES, so the next tick is still due
        # and retries (throttled). One missed daily verification is bounded
        # staleness; a 30-second hook stall is the reported defect.
        worker_requests.append("verify")
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
                return done(PROJECTION_CURRENT)

    # The attribution units are seeds of the same scope (a union, never a
    # replacement): a bounded pass that then exceeds the incremental ceiling
    # falls back to whole history, because N indexed seeks stop beating one
    # scan at the same point however the units were arrived at. Both seed
    # sets share one interleave closure (#1071).
    dirty_units, watermark_target = _resolve_pass_scope(
        cache,
        force_full=force_full,
        ledger_state=ledger_state,
        ledger_high=ledger_high,
        watermark=watermark,
        active_roots=set(active_roots),
        stale_reverse_map=stale_reverse_map,
        verification_due=verification_due,
        attribution_scope=attribution_scope,
        historical_spans=historical_spans,
    )
    fields.update(watermark_target=watermark_target)
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
        # The rebuilt-state route is the one that matters: every upgrading
        # install that rebuilds stats.db from the journal would otherwise run
        # the whole-history pass inline on top of the rebuild, on a path
        # Codex kills at 30 seconds, so `run_stats_ingest` commits nothing,
        # `last_full_pass_at` is never stamped, and the next tick repeats it.
        #
        # Unlike the interval gate above, this one performs NO projection
        # work at all: without a trustworthy watermark there is no bounded
        # pass to fall back to. That leaves the projection transiently
        # missing rather than merely stale, and it is accepted — the worker
        # converges it (throttled retry on failure), every non-hook caller
        # still runs inline, and a 30-second blocking tick is not an option.
        if "verify" not in worker_requests:
            worker_requests.append("verify")
        return done(PROJECTION_DEFERRED_VERIFY, scope=None)
    # Axis 2/3/4 (spec §3): alert state is not a function of window
    # dirtiness. A rule change and a delivery-gate ENABLE each require a
    # semantic pass over the affected identities with no observation having
    # moved — and activation must write `suppressed_backfill` terminal rows
    # rather than dispatch history, which it can only do if it actually sees
    # the blocks. Widening a bounded pass is always safe; missing an
    # identity is not.
    #
    # Axis 4 is root-scoped when epoch-1007 ownership is available. A
    # scalar-only legacy boundary records `REASON_SCHEDULED_DEFERRED` under
    # `full_pass="defer"`, because whole history remains its only honest
    # fallback.
    #
    # #1070 (spec §5.6, Q11, Q15): under `"defer"` none of them widens. The
    # widened pass took 32 s on a production-shaped store against Codex's
    # 30-second hook kill (§4.2 M1): the killed pass committed nothing, the
    # gate stayed un-stored and the next hook repeated it — a loop reached by
    # every first enable, rule change, rebuild NULL gate and re-enable. So the
    # hook keeps its BOUNDED physical scope, evaluates no threshold for the
    # deferred requirement, leaves the gate, the fingerprints and the due
    # boundary as stored, and hands the work to the `_codex-quota-verify`
    # worker as a durable obligation (recorded by the leg under the lock). An
    # `"inline"` caller still widens within its own pass.
    if (dirty_units is not None and alert_scope is not None
            and full_pass != "defer"):
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
    fields.update(
        scope=dirty_units,
        reconcile_roots=(
            None if reconcile_roots is None else frozenset(reconcile_roots)))
    if dirty_units is None:
        observations = load_codex_quota_observations(
            source_root_keys=(
                active_roots if reconcile_roots is None
                else active_roots & reconcile_roots
            ),
            cache_conn=cache,
        )
        final_units = None
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
        final_units = frozenset(
            dirty_units["units"]
            | {_observation_unit_text(o) for o in observations}
        )

    # A pass may only advance the two non-dirtiness alert axes it stores if it
    # actually did the work they exist to trigger.
    #
    # A REPORTING-ONLY pass (the `_codex-quota-verify` worker, the dashboard
    # tick, `codex quota`) never reaches a threshold decision —
    # `_evaluate_quota_alerts` returns at `if not alert_eligible_roots` — yet it
    # used to stamp both, so a delivery-gate ENABLE landing on the same tick as
    # a full pass was consumed with no arming row and no `suppressed_backfill`,
    # and could not re-fire because `gate_before` then read True. Carrying
    # eligible roots is what excludes it, and nothing else does.
    #
    # The second condition asks whether this pass LOOKED, not whether it
    # intended to. A bounded tick that deferred axis 4 declined to look, so it
    # must not retire the boundary. A tick that widened to whole-history for
    # some OTHER reason did look — at every observation of every active root,
    # and applied over all of them — so retiring it is honest. Gating on the
    # deferral alone instead was an absorbing state: `alert_dirty_scope` widens
    # whenever `gate_before is not True`, the hook is the only production
    # caller that carries eligible roots AND it always defers, so a due
    # boundary froze `alerts_enabled` at its stored value and every following
    # tick re-resolved the identical SCOPE_ALL inline on the blocking path.
    #
    # A pass that deferred complete-history alert work to the worker (#1070)
    # did not do it either, so it consumes nothing: the worker that completes
    # the obligation retires the axes it evaluated.
    consume_alert_axes = bool(alert_eligible_roots & active_roots) and (
        not alert_obligations
    ) and (
        final_units is None
        or alert_scope is None
        or _axes.REASON_SCHEDULED_DEFERRED not in alert_scope.reasons
    )
    complete_roots = frozenset(
        (active_roots if reconcile_roots is None
         else active_roots & reconcile_roots)
        if final_units is None else ()
    )
    return done(
        "apply",
        observations=observations,
        dirty_units=final_units,
        consume_alert_axes=consume_alert_axes,
        complete_roots=complete_roots,
        schedule_evaluated_roots=frozenset(
            complete_roots & alert_eligible_roots),
        prune_inactive_schedule=(
            source_root_keys is None
            and final_units is None
            and reconcile_roots is None),
    )


def _codex_prepared_pass_still_valid(
    stats_conn: sqlite3.Connection, prepared: _PreparedPass,
) -> bool:
    """The leg's validation (#1069, spec §5.1 step 1-2).

    Keeps a prepared pass only when the stats witness read through the cycle's
    own connection equals the prepared one, the attribution revision is
    unchanged, no model-resolution debt stands and the ledger was not reset
    (live high below the prepared high, the existing reset rule). Ordinary
    advances of the physical sequence, the ledger head or the root inventory do
    not invalidate it; they stay later incremental work, and the finalization
    fences keep them owed. Anything unreadable fails validation.
    """
    if not _codex_witnesses_equal(
            prepared.witness, _codex_projection_witness(stats_conn)):
        return False
    try:
        cache = _cache_connection()
    except (FileNotFoundError, sqlite3.Error):
        return False
    try:
        cache.execute("BEGIN")
        try:
            revision = _cache_module().codex_window_attribution_revision(cache)
            model_debt = _cache_module().codex_model_resolution_pending(cache)
            ledger_high = _ledger_max_seq(cache)
        finally:
            cache.commit()
    except sqlite3.Error:
        return False
    finally:
        cache.close()
    if revision != prepared.attribution_revision or model_debt:
        return False
    if prepared.ledger_high is not None and (
            ledger_high is None or ledger_high < prepared.ledger_high):
        return False
    return True


def _finalize_codex_quota_publication(
    prepared: _PreparedPass, *, signatures: Mapping[str, str],
) -> str:
    """Fenced, best-effort certificate finalization (#1069, spec §5.1, Q3).

    Runs in the ingest cycle's ``post_commit``, after the stats transaction has
    committed and after the ``_after_stats_commit`` seam, under the same ingest
    lock hold — so ingest → cache writer flocks → cache transaction, the order
    ``docs/journal-gotchas.md``'s lock law permits after the stats commit. The
    global then the Codex cache writer flock are taken non-blocking; when
    either is unavailable nothing is pruned, certified or deleted, which leaves
    recoverable work for a later finalization. In the one cache transaction the
    store helper checks the fences before pruning (physical sequence,
    attribution revision, ledger head, model debt): all equal prunes through
    ``watermark_target`` and stores the certificate; any mismatch prunes
    nothing and deletes the stored certificate.

    NEVER raises: the cycle's alert dispatch runs after this, and a raise from
    ``post_commit`` would drop a committed notification. Returns ``"stored"``,
    ``"fenced"`` or ``"skipped"``.
    """
    import _lib_cache_writer_lock

    held = None
    try:
        held = _lib_cache_writer_lock.acquire_cache_writer_flocks(
            _cctally_core.CACHE_LOCK_PATH, _cctally_core.CACHE_LOCK_CODEX_PATH,
            timeout=None)
        if held is None:
            return "skipped"
        outcome = _store_codex_quota_projection_certificate(
            sequence=prepared.physical_seq, signatures=signatures,
            prune_ledger_through=prepared.watermark_target,
            # The revision the pass computed against, not the post-ingest one:
            # the cycle's own step 3 can materialize an attribution after the
            # observations were loaded (#500 §8.3).
            attribution_revision=prepared.attribution_revision,
            expected_ledger_high=prepared.ledger_high,
            delete_on_mismatch=True,
        )
        return outcome if outcome in ("stored", "fenced") else "skipped"
    except Exception:  # noqa: BLE001 - best-effort by contract
        return "skipped"
    finally:
        if held:
            try:
                _lib_cache_writer_lock.release_cache_writer_flocks(held)
            except Exception:  # noqa: BLE001
                pass


@dataclass(frozen=True)
class _AlertLegPlan:
    """What the leg does about alerts once its pass is final (#1070, §5.6).

    Decided under the ingest lock, after the validation and any
    re-preparation, and BEFORE ``_apply_quota_projection_rows``: the
    sidecar's requests are recorded or merged first, so a request survives a
    rolled-back transaction harmlessly, while a pass whose obligation could not
    be durably recorded is withheld (it must not consume the dirtiness the
    request's cutoff describes).
    """

    evaluate_roots: "frozenset[str]"
    consume_alert_axes: bool
    schedule_evaluated_roots: "frozenset[str]"
    activation_cutoffs: "dict[str, dt.datetime] | None" = None
    completing_ids: "frozenset[str]" = frozenset()
    withhold: bool = False
    pending: bool = False
    issue: str = ""


def _plan_codex_alert_leg(
    final: _PreparedPass, *, activation_roots: "frozenset[str] | None",
    now_iso: str,
) -> _AlertLegPlan:
    """Record, merge and apply the pending alert obligations (#1070, §5.6).

    A reporting pass (no eligible roots, not the activation) and a pass with
    delivery off leave alerts exactly as before: the disarm runs inline and
    ends the generation after the commit. Otherwise the sidecar is read under
    the lock. A DAMAGED sidecar makes the pass reporting-only for alerts —
    nothing evaluated, no axis consumed, the physical scope still published —
    and pending, because stats cannot prove it held no obligation. A readable
    one is merged with this pass's derived obligations and durably rewritten
    when anything changed; a failed write withholds the pass. Then:

    * the hook (and any eligible pass that is not the activation) evaluates
      only roots with no pending obligation — an inline evaluation would
      activate at this tick's time without the cutoff, or dispatch before the
      activation — and consumes no axis while any request stands;
    * the activation (``activation_roots``: the worker's configured roots)
      evaluates its locked roots with each one's earliest cutoff, completes
      every request whose owed roots it holds, and consumes the axes only when
      it evaluated every applicable root and nothing remains.
    """
    unchanged = _AlertLegPlan(
        evaluate_roots=frozenset(final.alert_eligible_roots),
        consume_alert_axes=final.consume_alert_axes,
        schedule_evaluated_roots=frozenset(final.schedule_evaluated_roots))
    if activation_roots is None and not final.alert_eligible_roots:
        return unchanged
    config = final.alert_config
    if config is None or not (config[0] and config[1]):
        return unchanged
    eligible = frozenset(final.alert_eligible_roots) & frozenset(final.active_roots)
    state = _read_codex_alert_obligations()
    if state.damaged:
        return _AlertLegPlan(
            evaluate_roots=frozenset(), consume_alert_axes=False,
            schedule_evaluated_roots=frozenset(), pending=True,
            issue="alert_obligations_unreadable")
    fingerprint = _codex_alert_config_fingerprint(config)
    merged, changed = _merge_codex_alert_obligations(
        state.requests, derived=final.alert_obligations,
        fingerprint=fingerprint, observed_roots=eligible, now_iso=now_iso)
    if changed:
        try:
            _write_codex_alert_obligations(merged)
        except OSError:
            return _AlertLegPlan(
                evaluate_roots=frozenset(), consume_alert_axes=False,
                schedule_evaluated_roots=frozenset(), withhold=True,
                pending=True, issue="alert_obligations_unwritable")
    if not merged:
        return unchanged
    if activation_roots is not None:
        applicable = frozenset(activation_roots) & frozenset(final.active_roots)
        cutoffs: "dict[str, dt.datetime]" = {}
        completing: "set[str]" = set()
        # A request under another fingerprint survives the merge only when
        # this leg is stale (`_alert_obligations_newer_than`): it belongs to a
        # newer configuration, so this leg neither evaluates the roots it owes
        # under the older rules nor completes it.
        stale_owed: "set[str]" = set()
        for request in merged:
            owed = _alert_obligation_owed_roots(request, applicable)
            if request["config_fingerprint"] != fingerprint:
                stale_owed |= owed
                continue
            if owed <= eligible:
                completing.add(request["id"])
            for root in owed & eligible:
                cutoff = _parse_utc(
                    request["cutoffs"][root], "alert obligation cutoff")
                cutoffs[root] = min(cutoffs.get(root, cutoff), cutoff)
        pending = len(completing) < len(merged)
        return _AlertLegPlan(
            evaluate_roots=eligible - stale_owed,
            consume_alert_axes=(
                final.consume_alert_axes and not pending
                and applicable <= eligible),
            schedule_evaluated_roots=frozenset(final.schedule_evaluated_roots),
            activation_cutoffs=cutoffs, completing_ids=frozenset(completing),
            pending=pending)
    pending_roots: "set[str]" = set()
    for request in merged:
        pending_roots |= _alert_obligation_owed_roots(request, eligible)
    return _AlertLegPlan(
        evaluate_roots=frozenset(final.alert_eligible_roots) - pending_roots,
        consume_alert_axes=False,
        schedule_evaluated_roots=(
            frozenset(final.schedule_evaluated_roots) - pending_roots),
        pending=True)


def _alert_work_pending_without_leg(
    prepared: _PreparedPass, *, activation: bool,
) -> "tuple[bool, str]":
    """``alert_work_pending`` for a pass whose leg recorded nothing (a
    deferral, a model fence, contention): its own derived obligations, or the
    requests a lock-free read of the sidecar shows. Never raises."""
    if prepared.alert_obligations:
        return True, ""
    if not activation and not prepared.alert_eligible_roots:
        return False, ""
    config = prepared.alert_config
    if config is None or not (config[0] and config[1]):
        return False, ""
    state = _read_codex_alert_obligations()
    if state.damaged:
        return True, "alert_obligations_unreadable"
    return bool(state.requests), ""


def _settle_codex_alert_obligations(holder: dict) -> None:
    """After the commit, before the dispatch (#1070, spec §5.6): end the
    generation after a live disarm, or remove the requests the activation just
    completed. Best-effort under the finalization's contract: an ``OSError``
    is caught, never raises (the cycle's dispatch runs next, and a raise from
    ``post_commit`` would drop a committed notification), keeps the requests,
    keeps the committed status and counts, and leaves alert work pending; the
    retry is idempotent (its claims already exist), so nothing dispatches
    twice."""
    plan = holder.get("plan")
    try:
        if holder.get("disarmed"):
            _cancel_codex_alert_obligations()
            holder["alert_work_pending"] = False
            holder["alert_work_issue"] = ""
        elif plan is not None and plan.completing_ids and holder.get("applied"):
            state = _read_codex_alert_obligations()
            if state.state != "readable":
                return
            remaining = [
                request for request in state.requests
                if request["id"] not in plan.completing_ids]
            if len(remaining) != len(state.requests):
                _write_codex_alert_obligations(remaining)
            holder["alert_work_pending"] = bool(remaining)
    except OSError:
        holder["alert_work_pending"] = True
        holder["alert_work_issue"] = "alert_obligations_unwritable"
    except Exception:  # noqa: BLE001 - best-effort by contract
        holder["alert_work_pending"] = True


def _run_codex_worker_requests(
    requests: Iterable[str], *, hold_verify: bool = False,
) -> None:
    """Spawn the hand-offs a preparation requested, outside every transaction
    (#1069 Q4): after ``run_stats_ingest`` has returned, or at once when no
    ingest runs.

    ``hold_verify`` (#1070, Q13): a hook whose alert work is pending requests
    the verify worker itself once it has RELEASED its lifecycle locks, so the
    worker does not start against its parent's locks and lose its throttle
    window to an ``alert_lifecycle_contended`` run."""
    seen: set = set()
    for request in requests:
        if request in seen:
            continue
        seen.add(request)
        if request == "verify":
            if hold_verify:
                continue
            _defer_codex_quota_verification()
        elif request == "model":
            _request_codex_model_resolution()


def reconcile_codex_quota_projection(
    *,
    source_root_keys: Iterable[str] | None = None,
    alert_eligible_root_keys: Iterable[str] = (),
    now: dt.datetime | None = None,
    force_full: bool = False,
    full_pass: str = "inline",
    _before_stats_commit: Callable[[], None] | None = None,
    _after_stats_commit: Callable[[], None] | None = None,
    _alert_activation_roots: Iterable[str] | None = None,
    _read_deferrals: "list[str] | None" = None,
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

    The spec §3 alert axes (a delivery-gate ENABLE, a rule change, an owned or
    legacy scheduled instant) widen an ``"inline"`` pass, because activation
    must write ``suppressed_backfill`` terminal rows for already-satisfied
    blocks rather than dispatch history, and it can only do that by SEEING
    them. Under ``"defer"`` (#1070, spec §5.6) they never widen: that widened
    pass took 32 s on a production-shaped store against Codex's 30-second hook
    kill, so it never committed and every following hook repeated it. The hook
    keeps its bounded scope and records the complete-history work as a durable
    obligation (``codex-quota-alert-obligations.json``, cutoff = the hook's
    ``now``); the ``_codex-quota-verify`` worker completes it under the owed
    roots' real lifecycle locks (``_alert_activation_roots`` is its configured
    root set), and ``alert_work_pending`` reports work still owed. A crossing
    captured after the cutoff alerts when the worker completes; history stays
    ``suppressed_backfill``.

    A pass that did the complete-history alert work retires the axes it
    evaluated (the gate, the matured boundary); a pass that deferred it retires
    none, so the obligation keeps standing until the worker completes it.

    #1069: the pass is prepared OPTIMISTICALLY, outside the ingest lock
    (``_prepare_codex_quota_pass``), and the ``codex_apply`` leg validates that
    preparation against the cycle's own stats connection before applying it:
    the publication witness, the attribution revision, model-resolution debt
    and a ledger reset. A superseded preparation is replaced by ONE complete
    re-preparation inside the lock, so every schedule of concurrent reconcilers
    is equivalent to a serial one; the re-preparation routes alert work
    exactly as the optimistic one does, so it never loads a complete history
    on the hook either. The
    certificate and the ledger prune are finalized in ``post_commit``, behind
    fences, best-effort (``_finalize_codex_quota_publication``). The returned
    ``status`` is always explicit: ``published``, ``current``,
    ``deferred_verify``, ``deferred_model``, ``contended`` or ``unavailable``.

    #1076: off the hook (no Codex read policy active) the whole
    ``run_stats_ingest`` call runs under the defer-only
    ``off_hook_codex_read_policy()``, so a pending observation's pipeline
    never syncs Codex or reconciles again inside this ingest; its Codex budget
    readers defer with ``off_hook_unpinned``. ``_read_deferrals`` is an
    invocation-owned collector extended with those reasons, even when the
    reconcile raises. A call nested inside a held ingest returns
    ``contended`` before any work, or raises when it carries alert authority.
    """
    # #787 spec 5.1: owner entry, guarded in its own frame.
    _cctally_core.require_capability("WRITE", entry="reconcile_codex_quota_projection")
    if full_pass not in ("inline", "defer"):
        raise ValueError(
            "reconcile_codex_quota_projection: full_pass must be "
            "'inline' or 'defer'")
    if now is None:
        now = dt.datetime.now(UTC)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    now_iso = _utc_iso(now)
    alert_eligible_roots = frozenset(str(key) for key in alert_eligible_root_keys)
    prepare_kwargs = {
        "source_root_keys": (
            None if source_root_keys is None
            else frozenset(str(key) for key in source_root_keys)),
        "alert_eligible_root_keys": alert_eligible_roots,
        "now": now,
        "force_full": force_full,
        "full_pass": full_pass,
        "extra_active_roots": frozenset(
            str(key) for key in (_alert_activation_roots or ())),
    }
    activation_roots = (
        None if _alert_activation_roots is None
        else frozenset(str(key) for key in _alert_activation_roots))
    hold_verify_on_pending = full_pass == "defer"

    # #1076 D4: a reconcile nested inside a held ingest cycle could only wait
    # for the ingest lock its own context holds (10 s, then `contended`). It
    # returns `contended` at once: nothing recorded, no worker requested, no
    # certificate advanced; the stale certificate makes the next unnested
    # sync reconcile. Alert authority belongs to the top-level hook and
    # worker, so a nested call carrying it violates the lifecycle contract.
    import _cctally_store
    if _cctally_store.holds_ingest_lock():
        if alert_eligible_roots or activation_roots is not None:
            raise RuntimeError(
                "reconcile_codex_quota_projection: an alert-authorized "
                "reconcile must not run inside a held stats ingest")
        return QuotaProjectionResult(
            None, 0, 0, 0, 0, 0, 0, status=PROJECTION_CONTENDED)

    def _empty(
        status: str, pending: bool = False, issue: str = "",
    ) -> QuotaProjectionResult:
        return QuotaProjectionResult(
            None, 0, 0, 0, 0, 0, 0, status=status,
            alert_work_pending=pending, alert_work_issue=issue)

    # ── the optimistic preparation, outside the ingest lock ─────────────────
    # A fresh stats connection whose read snapshot holds only step 2; it is
    # closed before the ingest starts, as is the routine's own cache snapshot.
    stats_conn = _cctally_core.open_db()
    try:
        prepared = _prepare_codex_quota_pass(
            stats_conn=stats_conn, in_lock=False, **prepare_kwargs)
    finally:
        stats_conn.close()
    if prepared.disposition != "apply":
        # `current`, `deferred_verify`, `deferred_model`, `unavailable`: no
        # ingest; any hand-off runs now, outside every transaction. Alert work
        # this pass derived (or the sidecar already holds) stays pending: the
        # hook requests the worker after releasing its lifecycle locks.
        pending, issue = _alert_work_pending_without_leg(
            prepared, activation=activation_roots is not None)
        _run_codex_worker_requests(
            prepared.worker_requests,
            hold_verify=pending and hold_verify_on_pending)
        return _empty(prepared.disposition, pending, issue)

    # ── apply, under the ingest lock (Task 7 Item 3) ────────────────────────
    # The stats.db writes route through the single-flight ingest cycle. The
    # `codex_apply` leg validates the optimistic preparation against the
    # cycle's own connection and re-prepares ONCE when it was superseded
    # (#1069): no other quota publication can interleave inside the lock, so
    # the re-prepared pass cannot itself be superseded. The interpreted
    # `quota_*` tables are mutable projections re-materialized here (spec
    # §5.3), and each genuine arming activation journals a
    # `quota_alert_arming` evt so the forward-only boundary survives a
    # stats.db rebuild (Item 5).
    holder: dict = {}

    def _reset_holder() -> None:
        holder.clear()
        holder.update({
            "result": _empty(PROJECTION_CURRENT),
            "signatures": None,
            "pass": None,
            "applied": False,
            "finalization": None,
            "plan": None,
            "withheld": False,
            "disarmed": False,
            "alert_work_pending": False,
            "alert_work_issue": "",
        })

    _reset_holder()
    import _cctally_journal as _jr
    import _lib_journal as _jl

    def _codex_leg(ctx):
        # The ingest's correction recovery can invoke this twice in one call
        # (spec §4.1), so every invocation validates from a fresh holder.
        _reset_holder()

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

        final = prepared
        if not _codex_prepared_pass_still_valid(ctx.conn, final):
            # Once per invocation: the caller's arguments (eligible roots
            # included), stats through `ctx.conn`, a fresh cache snapshot and
            # a fresh config snapshot. Never a reconcile, never an ingest.
            final = _prepare_codex_quota_pass(
                stats_conn=ctx.conn, in_lock=True, **prepare_kwargs)
        holder["pass"] = final
        if final.disposition == "apply":
            # #1070 §5.6: record or merge the pending alert obligations BEFORE
            # any projection write; a pass whose obligation could not be
            # durably recorded is withheld whole.
            plan = _plan_codex_alert_leg(
                final, activation_roots=activation_roots, now_iso=now_iso)
            holder["plan"] = plan
            holder["alert_work_pending"] = plan.pending
            holder["alert_work_issue"] = plan.issue
            if plan.withhold:
                holder["withheld"] = True
        else:
            pending, issue = _alert_work_pending_without_leg(
                final, activation=activation_roots is not None)
            holder["alert_work_pending"] = pending
            holder["alert_work_issue"] = issue
        if final.disposition == "apply" and not holder["withheld"]:
            plan = holder["plan"]
            ledger_state = final.ledger_state
            # No configured roots and no existing interpreted history means
            # there is no stats work; the shared module-level apply returns
            # without a publication, which keeps the empty-Codex fast path.
            # Delegates to the shared apply so the live leg and the rebuild
            # re-materialization pass (Task 8) never drift.
            _apply_quota_projection_rows(
                ctx.conn, observations=final.observations,
                active_roots=set(final.active_roots),
                now=now, now_iso=now_iso, sink=ctx.pending_alerts,
                alert_eligible_roots=set(plan.evaluate_roots),
                journal_emit=_emit_arming, journal_disarm=_emit_disarm,
                journal_terminal=_emit_terminal_event,
                holder=holder, dirty_units=final.dirty_units,
                ledger_watermark=final.watermark_target,
                alerts_enabled=bool(
                    final.alert_config[0] and final.alert_config[1]),
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
                consume_alert_axes=plan.consume_alert_axes,
                reconcile_roots=(
                    None if final.reconcile_roots is None
                    else set(final.reconcile_roots)),
                schedule_evaluated_roots=plan.schedule_evaluated_roots,
                prune_inactive_schedule=final.prune_inactive_schedule,
                alert_config=final.alert_config,
                activation_cutoffs=plan.activation_cutoffs,
            )
            holder["applied"] = holder["signatures"] is not None
        # `_before_stats_commit` fires INSIDE the cycle txn, before COMMIT — a
        # raise rolls the whole cycle back, so the projection updates undo
        # together (the crash-consistency contract the callers test).
        if _before_stats_commit is not None:
            _before_stats_commit()

    def _post_commit():
        # After the commit, still under the ingest lock and BEFORE the cycle's
        # alert dispatch: the `_after_stats_commit` seam first (its raising
        # contract is preserved, and a raise skips finalization and dispatch
        # exactly as it skipped the certificate and dispatch before), then the
        # fenced finalization, which never raises.
        if _after_stats_commit is not None:
            _after_stats_commit()
        if holder["applied"]:
            holder["finalization"] = _finalize_codex_quota_publication(
                holder["pass"], signatures=holder["signatures"])
        # #1070: end a disarmed generation's obligations, or remove the ones
        # this activation completed. Never raises.
        _settle_codex_alert_obligations(holder)

    # AUTHORITATIVE: the reconcile must observe its own write + return the
    # materialized result synchronously; the cycle dispatches the queued alerts
    # post-commit via the ALERT_DISPATCHER.
    #
    # #1076 D2: off the hook, the whole call (correction recovery's retry,
    # every pending observation's pipeline, `codex_apply` with its in-lock
    # re-preparation, and `post_commit`) runs under the defer-only off-hook
    # Codex read policy. Under the hook's policy (the hook core and #1034's
    # settlement) nothing changes.
    import _cctally_cache
    off_hook = None
    try:
        if _cctally_cache._active_codex_read_policy() is None:
            with _cctally_cache.off_hook_codex_read_policy() as off_hook:
                ingest = _jr.run_stats_ingest(
                    mode="authoritative", codex_apply=_codex_leg,
                    post_commit=_post_commit,
                )
        else:
            ingest = _jr.run_stats_ingest(
                mode="authoritative", codex_apply=_codex_leg,
                post_commit=_post_commit,
            )
    finally:
        if off_hook is not None and _read_deferrals is not None:
            _read_deferrals.extend(off_hook.deferred_reasons)
    final = holder["pass"]
    if not getattr(ingest, "ran", False) or final is None:
        # The ingest never ran (the lock or the index refused it): nothing
        # was applied. The optimistic preparation's hand-offs still stand.
        pending, issue = _alert_work_pending_without_leg(
            prepared, activation=activation_roots is not None)
        _run_codex_worker_requests(
            prepared.worker_requests,
            hold_verify=pending and hold_verify_on_pending)
        return _empty(PROJECTION_CONTENDED, pending, issue)
    pending = bool(holder["alert_work_pending"])
    issue = str(holder["alert_work_issue"])
    if holder["withheld"]:
        _run_codex_worker_requests(
            final.worker_requests, hold_verify=hold_verify_on_pending)
        return _empty(PROJECTION_UNAVAILABLE, True, issue)
    _run_codex_worker_requests(
        final.worker_requests, hold_verify=pending and hold_verify_on_pending)
    if holder["applied"]:
        return replace(
            holder["result"], status=PROJECTION_PUBLISHED,
            alert_work_pending=pending, alert_work_issue=issue)
    if final.disposition == "apply":
        # The shared apply found no root to reconcile: an empty store's early
        # return, which is not a publication.
        return _empty(PROJECTION_CURRENT, pending, issue)
    return _empty(final.disposition, pending, issue)


def _load_active_milestones(
    identity: QuotaWindowIdentity, resets_at: dt.datetime,
    *, stats_conn: sqlite3.Connection | None = None,
) -> list[sqlite3.Row | tuple[object, ...]]:
    owns_conn = stats_conn is None
    # #787 / #861 AD21: an owned read is one query session.
    import _cctally_store_access as _store_access
    stats = (_store_access.statement_reader("stats") if stats_conn is None
             else stats_conn)
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

    FIVE stamping mechanisms exist and must never be conflated: the
    quota-observation fold (``adopt_unidentified_observations``, per physical-
    window group, landing post-fold in ``quota_window_blocks`` /
    ``quota_percent_milestones`` and NEVER written back to
    ``quota_window_snapshots``); per-file-range attribution
    (``codex_file_accounts`` -> ``codex_session_entries.account_key``,
    ``stably_absent`` -> NULL); WINDOW-SCOPED SPEND ADOPTION (the 2026-07-30
    spec — ``_lib_codex_account_adoption`` +
    ``_cctally_cache.apply_codex_window_spend_adoption``, which stamps that same
    ``codex_session_entries.account_key`` column at ingest from the window's
    single identified account); OPERATOR ATTRIBUTION (#500 —
    ``_lib_codex_window_attribution`` + ``_apply_codex_window_attribution_overlay``,
    which makes a group identified at FOLD time from a journal-recorded
    assertion and therefore reaches both the percentage and the spend axis
    through the two mechanisms above rather than beside them); and the stats
    ``accounts`` registry.

    The rule below is unchanged by that fifth entry, and deliberately so:
    operator attribution feeds the fold, so every read already scoped by the
    fold's answer stays scoped the same way and no predicate flavour moves.

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


def codex_entry_selection_bounds(
    start_captured_at: dt.datetime, last_captured_at_utc: object,
) -> tuple[str, str]:
    """The SQL text bounds of ``codex_quota_breakdown``'s accounting read.

    ``timestamp_utc`` is stored in canonical ``Z`` form, while retained quota
    observations may have arrived as ``+00:00``.  Keep this SQL bound
    deliberately one second wider and let the physical-tuple comparison in the
    breakdown enforce the exact inclusive endpoint.  This preserves same-second
    path/offset ordering without relying on mixed-spelling text equality.

    One definition (#1050 spec 4.7), because the Codex cycle-detail stamp's
    per-selection aggregate (``codex_entry_selection_aggregate``) must select
    exactly the rows the breakdown prices.
    """
    query_start = (
        start_captured_at - dt.timedelta(seconds=1)
    ).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    query_end = (
        _parse_utc(str(last_captured_at_utc), "captured_at_utc")
        + dt.timedelta(seconds=1)
    ).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return query_start, query_end


def codex_entry_models(cache_conn: sqlite3.Connection) -> tuple[str, ...]:
    """Every distinct ``codex_session_entries.model``, ascending (#1050).

    One ``model > ?`` seek per model through ``idx_codex_entries_model_time``
    rather than a ``SELECT DISTINCT`` over the table: the population is a few
    dozen models, the table is history-sized.
    """
    models: list[str] = []
    last = None
    while True:
        if last is None:
            row = cache_conn.execute(
                "SELECT MIN(model) FROM codex_session_entries").fetchone()
        else:
            row = cache_conn.execute(
                "SELECT MIN(model) FROM codex_session_entries WHERE model > ?",
                (last,)).fetchone()
        if row is None or row[0] is None:
            return tuple(models)
        last = str(row[0])
        models.append(last)


def codex_entry_selection_aggregate(
    cache_conn: sqlite3.Connection, *, source_root_key: str,
    query_start: str, query_end: str, account_key: str | None,
    models: tuple[str, ...],
) -> tuple:
    """The aggregate twin of ``codex_quota_breakdown``'s accounting read.

    #1050 spec 4.7: the same ``source_root_key``, the same text timestamp
    bounds (``codex_entry_selection_bounds``) and the same STRICT account
    predicate, returning ``(COUNT(*), MAX(rowid), per_model)`` where
    ``per_model`` is ``(model, count, TOTAL input, TOTAL cached input, TOTAL
    output, TOTAL reasoning output, TOTAL total)`` for every model with a row,
    ascending by model. A Codex cycle-detail stamp carries it, so a late entry
    inside a past milestone interval, a removed one or a re-tokenized one
    re-stamps the cycle whose dollars it moves.

    One statement per model, so each seeks ``idx_codex_entries_model_time``
    (``model``, ``timestamp_utc``) and SQLite builds no temp b-tree: a
    ``GROUP BY model`` over the timestamp range sorts every row it selects. The
    unary ``+`` keeps the planner off the root-only index, which on a one-root
    install is a scan of the whole table; it removes the column's affinity,
    which is equivalent here because the root key is TEXT bound as a string.
    """
    entry_predicate, entry_params = _codex_cache_account_predicate(account_key)
    total_count = 0
    max_rowid = None
    per_model: list[tuple] = []
    for model in models:
        row = cache_conn.execute(
            "SELECT COUNT(*), MAX(rowid), TOTAL(input_tokens), "
            "       TOTAL(cached_input_tokens), TOTAL(output_tokens), "
            "       TOTAL(reasoning_output_tokens), TOTAL(total_tokens) "
            "  FROM codex_session_entries "
            " WHERE model=? AND timestamp_utc>=? AND timestamp_utc<=? "
            "   AND +source_root_key=? " + entry_predicate,
            (model, query_start, query_end, source_root_key, *entry_params),
        ).fetchone()
        count = int(row[0] or 0)
        if not count:
            continue
        total_count += count
        max_rowid = row[1] if max_rowid is None else max(max_rowid, row[1])
        per_model.append((model, count, *(float(v) for v in row[2:])))
    return (total_count, max_rowid, tuple(per_model))


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
    query_start, query_end = codex_entry_selection_bounds(
        start[0], milestones[-1][1])
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


class _QuotaSelection(tuple):
    """Quota candidates resolved on the current-root population first (#1108).

    The tuple is what the command renders. ``retained`` is the same selection
    over complete retained history: a later selection step that finds no
    candidate in the tuple resolves on it instead, exactly as before the
    current-root scope existed (spec §3.6).
    """

    def __new__(cls, current, retained):
        self = super().__new__(cls, current)
        self.retained = tuple(retained)
        return self


def _narrow(selection, matching):
    """Apply one selection step current-root first (#1108 spec §3.6).

    Returns ``(population, found)``: ``found`` is ``matching`` over the
    current-root selection or, when that yields no candidate, over complete
    retained history; ``population`` is the set it resolved on, which an
    error lists as its candidates. A plain tuple (an explicit ``--root-key``)
    has no retained fallback.
    """
    retained = getattr(selection, "retained", None)
    found = tuple(matching(selection))
    if retained is None:
        return selection, found
    if found:
        return selection, _QuotaSelection(found, matching(retained))
    found = tuple(matching(retained))
    return retained, _QuotaSelection(found, found)


def _current_root_population(histories, *, sync_stats) -> _QuotaSelection:
    """Histories with withheld retired roots set aside as the fallback (#1108).

    The predicate tests the loaded population itself, never the registry, so
    a configured root stays visible whether or not it is registered.
    """
    cache_mod = _cctally()._load_sibling("_cctally_cache")
    scope = cache_mod.codex_cli_read_scope(
        lambda _keys: (history.identity.source_root_key for history in histories),
        sync_stats=sync_stats)
    return _QuotaSelection(
        (history for history in histories
         if not cache_mod.codex_root_withheld(scope, history.identity.source_root_key)),
        histories)


def _select_histories(
    histories: tuple[QuotaHistory, ...], *, root_key: str | None, limit_key: str | None,
) -> tuple[QuotaHistory, ...]:
    population, selected = _narrow(histories, lambda candidates: (
        history for history in candidates
        if (root_key is None or history.identity.source_root_key == root_key)
        and (limit_key is None or history.identity.logical_limit_key == limit_key)
    ))
    if (root_key is not None or limit_key is not None) and not selected:
        raise QuotaCLIError(
            "no quota identity matches the exact selectors; candidates:\n"
            + _candidate_text(population)
        )
    return selected


def _sync_and_load(
    args, as_of: dt.datetime, *, current_fresh_only: bool = False,
    reconcile_projection: bool = True,
):
    """Return ``(histories, sync_stats)``; ``sync_stats`` is ``None`` unsynced."""
    c = _cctally()
    sync_requested = getattr(args, "sync", None)
    should_sync = (
        bool(sync_requested)
        if sync_requested is not None
        else not getattr(args, "no_sync", False)
    )
    sync_stats = None
    if should_sync:
        cache = c.open_cache_db()
        try:
            cache_mod = c._load_sibling("_cctally_cache")
            sync_stats, cache = cache_mod._run_cache_operation_with_recovery(
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
    return build_history(observations), sync_stats


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
    histories, sync_stats = _sync_and_load(
        args, as_of, current_fresh_only=current_fresh_only,
        reconcile_projection=reconcile_projection,
    )
    root_key = getattr(args, "root_key", None)
    if root_key is None:
        # #1108 §3.6: every selection but an explicit --root-key resolves on
        # the current-root population first; naming a root is retained-history
        # navigation and sees the complete history, exactly as before.
        histories = _current_root_population(histories, sync_stats=sync_stats)
    selected = _select_histories(
        histories,
        root_key=root_key,
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
        _population, histories = _narrow(histories, lambda candidates: (
            h for h in candidates
            if getattr(h.identity, "account_key", _lib_accounts.UNATTRIBUTED)
            == account_key
        ))
    return account_key, None, histories


def _narrow_to_range(histories, in_range, *, since, until):
    """Make ``--since/--until`` eligibility a current-root-first step (#1108).

    Without a bound, or on an explicit ``--root-key`` selection (a plain
    tuple, complete history), the histories are returned unchanged. Otherwise
    the histories ``in_range`` accepts are taken from the current-root
    selection, or from complete retained history when none of the current
    ones falls in the range, so a historical range only a retired root covers
    renders its retained rows as before the scope existed.
    """
    if (since is None and until is None) or getattr(
            histories, "retained", None) is None:
        return histories
    _population, found = _narrow(histories, lambda candidates: (
        history for history in candidates if in_range(history)))
    return found


def _history_blocks_in_range(history, *, since, until):
    """The quota blocks of one history that ``blocks``' range shows."""
    return [
        block for block in build_blocks(history.physical_observations)
        if (since is None or block.last_observed_at >= since)
        and (until is None or block.first_observed_at < until)
    ]


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
    histories = _narrow_to_range(
        histories,
        lambda history: bool(_history_in_range(history, since=since, until=until)),
        since=since, until=until)
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
    histories = _narrow_to_range(
        histories,
        lambda history: bool(_history_blocks_in_range(
            history, since=since, until=until)),
        since=since, until=until)
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
        population, matching = _narrow(histories, lambda candidates: (
            block
            for block in (
                build_blocks(candidates[0].physical_observations)
                if len(candidates) == 1 else ()
            )
            if block.resets_at == reset_at
        ))
        if len(population) != 1:
            # #1108: the current root held no such block, and the complete
            # retained set is ambiguous, exactly as before the scope.
            raise QuotaCLIError(
                "breakdown requires selectors resolving to exactly one quota identity; candidates:\n"
                + _candidate_text(population)
            )
        identity = population[0].identity
        if len(matching) != 1:
            raise QuotaCLIError(
                "--reset-at matches no unique native quota block; candidates:\n"
                + _candidate_text(population)
            )
    except QuotaCLIError as exc:
        return _command_error(exc)
    c = _cctally()
    speed = c._resolve_codex_speed(args.speed)
    # #1108 (the twin of V2): an explicit `--account` scopes the accounting
    # read to that account, as it scopes the identity; without it the read
    # stays merged and byte-stable (R8).
    rows = codex_quota_breakdown(
        identity, reset_at, speed=speed,
        **({} if acct_key is None else {"account_key": acct_key}),
    )
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
        # #341 / #1108: `--account` scopes the candidates before the weekly,
        # `--reset-at` and default steps, through the same current-first
        # narrowing (a bad reference exits 2 with its diagnostic).
        acct_key, acct_exit, selected = _resolve_account_and_scope(
            args, selected)
        if acct_exit is not None:
            return acct_exit
        # #1108 §3.6: each step resolves on the current-root selection first
        # and on the complete retained set when that yields no candidate.
        _population, histories = _narrow(selected, lambda candidates: (
            history for history in candidates
            if history.identity.window_minutes == 10_080
        ))
        if reset_value:
            reset_at = _parse_reset_at(reset_value)
            population, matching = _narrow(histories, lambda candidates: (
                (history, block)
                for history in candidates
                for block in build_blocks(history.physical_observations)
                if block.resets_at == reset_at
            ))
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
            def current_cycles(population):
                candidates = tuple(
                    (history, select_baseline(history.observations, as_of))
                    for history in population
                )
                standard = tuple(
                    (history, baseline) for history, baseline in candidates
                    if not codex_history_is_model_scoped(history, baseline=baseline)
                )
                return (
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

            population, matching = _narrow(histories, current_cycles)
        if len(matching) != 1:
            raise QuotaCLIError(
                "percent-breakdown matches no unique native 7-day quota cycle; "
                "use exact selectors or --reset-at for retained history; candidates:\n"
                + _candidate_text(population)
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
        # #1108 V2: an explicit `--account` scopes the ladder's accounting
        # and its five-hour correlation to that account, as it scopes the
        # cycle; without it both reads stay merged and byte-stable (R8).
        account_kwargs = {} if acct_key is None else {"account_key": acct_key}
        rows = codex_quota_breakdown(
            history.identity, block.resets_at, speed=speed, cache_conn=cache,
            **account_kwargs,
        )
        five_hour_observations = load_codex_quota_observations(
            source_root_keys={history.identity.source_root_key},
            cache_conn=cache,
            captured_at_or_after=(
                rows[0].captured_at - dt.timedelta(hours=6) if rows else as_of
            ),
        )
        if acct_key is not None:
            five_hour_observations = tuple(
                observation for observation in five_hour_observations
                if getattr(observation.identity, "account_key",
                           _lib_accounts.UNATTRIBUTED) == acct_key
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
    _decorate_account(output, acct_key)  # #341 R8
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
