"""The access-contract registry of the strict detector path (#825/#832,
spec 5.6 item 6).

Every transitive stats.db access reachable from the Claude usage pipeline's
roots has exactly one contract here: its function, its SQL fingerprint
(whitespace-collapsed literal SQL, or `dynamic:<function>#<n>` for SQL built at
run time), the table, key columns, predicate, ordering and selected values, its
write effect, and its class — period-keyed, range-keyed, cross-period or
singleton. A cross-period read must carry an `inert_rule`: the reason a
differing historical result cannot change a certified decision. While every
cross-period read has one, a deletion outcome leaves a recovery certificate in
place; an entry without one makes `non_inert_cross_period()` non-empty, and
then every deletion outcome revokes its account (`_cctally_quarantine`).

`tests/test_detector_access_registry.py` regenerates the reachable set from
the actual call graph (`tests/_detector_access_callgraph.py`) and fails on a
new, changed, removed or unclassified access. The entries below were generated
by that collector and classified by rule; the inert rules are the sentences a
reviewer checks against spec 5.6 items 4 and 5.

Cost-only families use the operator's Q7 residual; a field that feeds a strict
decision may not.

Reviewed re-pins (#825/#832 fix round R8, Codex fix-round-4 R8 option A). A
dynamic site's `sql_shape` covers its complete transitive closure, and that
closure deliberately includes the pricing tables: `_rebuild_stats_index_locked#1`
reaches `_lib_pricing.CLAUDE_MODEL_PRICING`, `CODEX_MODEL_PRICING`,
`PRICING_SNAPSHOT_DATE`, `_LIVE_PRICING_SNAPSHOT`, the pricing functions and
`_cctally_cache.CONVERSATION_ROLLUP_PRICING_FP_KEY` through `FOLD_APPLIERS`.
None of them is inert (the fingerprint key is a SQL lookup key, and the
snapshot date takes part in write authorization), so every pricing sync fails
the registry test until the affected pins are re-pinned. A re-pin is accepted
only through this review:

1. Print the before/after shape-line delta of every affected site with
   `bin/cctally-test-remote python3 tests/_detector_access_callgraph.py
   --base <rev>`, where `<rev>` is the full SHA of the commit the current
   pins were reviewed on (the merge base for a committed branch, the
   checkout's commit for an uncommitted change). Never pass `HEAD`: the runner
   snapshots the working tree into its own commit, so its `HEAD` already
   contains the change and the delta comes out empty.
   Read it beside the source diff (`git diff <rev> -- bin/`).
2. Explain every added (`+`), removed (`-`) and changed (`~`) dependency.
   "Only additions" is not an explanation.
3. Confirm that the site's table selection, keys, predicates, ordering,
   selected columns, parameter construction, execution conditions and write
   effects did not change, and say why each changed dependency leaves its
   contract and inert rule valid. A changed access needs a contract review
   (its `table`/`keys`/`predicate`/`ordering`/`selected`/`effects`/`classes`/
   `inert_rule`), not a new hash.
4. Update `sql_shape` only on the explained sites, to the `now` digest the
   command prints. The command must then end `guard: clean`: no missing, stale
   or unresolved access, and no pin mismatch.

Regenerating every pin in bulk is not acceptance: it would also accept an
access change hidden in the same closure, which the R8 tests in
`tests/test_detector_access_registry.py` prove the guard otherwise rejects.
"""
from __future__ import annotations

import dataclasses


@dataclasses.dataclass(frozen=True)
class AccessContract:
    function: str          # "module:qualified_name"
    sql_fingerprint: str   # normalized literal SQL, or "dynamic:<function>#<n>"
    table: str
    keys: "tuple[str, ...]"
    predicate: str
    ordering: str
    selected: "tuple[str, ...]"
    effects: str           # "read" | "write" | "delete" | "upsert"
    classes: "tuple[str, ...]"  # subset of CLASSES
    inert_rule: "str | None"
    # #825/#832 fix round C (CD2-003): a dynamic site's fingerprint names
    # only its function and ordinal, so its contract also carries the digest
    # of what it executes (`_detector_access_callgraph.CallGraph.
    # dynamic_shape`; since fix round E its producer's complete body, the
    # helpers its SQL is built from and their constants; since revision 2
    # the canonical AST of every binding they read, of any value type,
    # followed through imports, aliases and transitive dependencies; since
    # fix round R the definition of every function or class producing such
    # a value, transitively, however it is imported). A changed query under
    # an unchanged ordinal fails the guard, and a site depending on a name
    # nothing resolves fails it closed.
    sql_shape: "str | None" = None


CLASSES = frozenset({"period-keyed", "range-keyed", "cross-period", "singleton"})
EFFECTS = frozenset({"read", "write", "delete", "upsert"})

#: Milestone writers are reached through `_pipeline_claude_usage`.
ROOTS = (
    "_cctally_record:_pipeline_claude_usage",
    "_cctally_record:detect_reset_and_credit",
    "_cctally_record:_pipeline_record_credit",
    "_cctally_journal:_harvest",
    "_cctally_journal:emit_model_a",
)

# -- inert rules --------------------------------------------------------------

ACCOUNTS = (
    'Account registry metadata (labels, emails, last-seen): display and '
    'attribution of names, never a detector input.'
)

ANCHOR = (
    'Five-hour anchor selection: every prefix raw reset lies beyond both '
    "tolerances of the witness's (clause 2) and continuing validity keeps "
    'later raw resets at or after it, so no prefix anchor or block is ever '
    'selected.'
)

BASIS = (
    'Accepted weekly basis: with a date key it reads only the current, '
    'post-witness week (clause 1); without one it is reached only from a '
    'scratch reviewed hold, which is never a witness and whose reviewed op '
    'revokes every certificate.'
)

BLOCKS = (
    'Five-hour block rollup, ownership and cross-week flag: a Q7 '
    'cost/display family; the block existence that gates the rollover heal '
    'is read by exact key (period-keyed).'
)

CODEX = (
    'Codex quota family: provider-codex rows only; nothing it reads feeds a '
    'Claude automatic credit, moved-end transition or drop-consensus '
    'decision.'
)

DEBOUNCE = (
    'Debounce singleton: one row per account whose stored date key and end '
    'are compared in Python with the incoming reading; a prefix state '
    "carries a date key below the witness's (clauses 1 and 6), so it never "
    'matches a forward reading.'
)

JOURNAL = (
    'Journal identity machinery: rows are matched by exact logical or '
    'physical identity (event id, journal id, cursor, selector generation); '
    'a fresh witness and its forward events share no identity with the '
    'prefix (clause 7), so a prefix row is never returned for a forward '
    'decision.'
)

MAINT = (
    'Maintenance only: runs at open, rebuild, publication or validation, '
    'never inside a live automatic decision.'
)

MANUAL = (
    "Manual record-credit path: the quarantine fold revokes the account's "
    'certificates before these effects run (spec 5.6 item 8), so no '
    'certified decision depends on what it reads.'
)

PRED_FIVE = (
    'Five-hour predecessor (latest five-hour row, any window): a prefix '
    "row's window key is below the witness's (clause 1) and continuing "
    'validity keeps keys forward, so same_window is false in every '
    'reference and the identical cancel/baseline branch is taken.'
)

PRED_WEEKLY = (
    'Weekly predecessor (latest non-held row of the account, any week): a '
    "prefix row's weekly end precedes the witness's detection clock (clause "
    '5) and continuing validity keeps the clock forward, so a prefix row '
    'takes neither the moved-end nor the in-place branch (both require '
    'prior_end > now) in any reference.'
)

Q7 = (
    'Q7 cost/alert family: its result sets milestone costs, budget or '
    'projected alerts and block rollups, never an automatic credit, '
    'moved-end transition or drop-consensus decision (operator Q7).'
)

QUARANTINE = (
    'Outcome-quarantine fold state (triggers, envelope, certificates): read '
    "whole to answer an account's quarantine status, a control input that "
    'gates or reports a decision; the rows carry no observation, window or '
    'week, so no prefix row is ever returned as a detector input.'
)

READ_IDX = (
    'Index-wide sample for a projection or diagnostic value, not a detector '
    'decision.'
)

WAIT = (
    'Statusline waiting-head dependency tokens (fix round K13): counted, '
    'hashed and compared only for equality with a wait descriptor to decide '
    'whether an activating event dispatches the reducer, which re-reads and '
    'reclassifies everything it publishes; never a detector input.'
)

LINEAGE = (
    'Correction-to-fit lineage projection (#1015): written for the '
    'calibration lineage readers (forecast, project, doctor, status line); '
    'no automatic credit, moved-end or drop-consensus decision reads it.'
)

REGISTRY: "tuple[AccessContract, ...]" = (
    AccessContract(
        function='_cctally_account:account_label',
        sql_fingerprint='SELECT label, email FROM accounts WHERE account_key = ?',
        table='accounts',
        keys=('account_key',),
        predicate='account_key = ?',
        ordering='',
        selected=('label', 'email'),
        effects='read',
        classes=('cross-period',),
        inert_rule=ACCOUNTS,
    ),
    AccessContract(
        function='_cctally_account:display_account_label',
        sql_fingerprint='SELECT provider FROM accounts WHERE account_key = ?',
        table='accounts',
        keys=('account_key',),
        predicate='account_key = ?',
        ordering='',
        selected=('provider',),
        effects='read',
        classes=('cross-period',),
        inert_rule=ACCOUNTS,
    ),
    AccessContract(
        function='_cctally_account:load_accounts',
        sql_fingerprint='dynamic:_cctally_account:load_accounts#1',
        sql_shape='sha256:fb36e398466d6f61',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=ACCOUNTS,
    ),
    AccessContract(
        function='_cctally_account:real_account_count',
        sql_fingerprint='SELECT COUNT(*) FROM accounts WHERE provider = ? AND account_key != ?',
        table='accounts',
        keys=('provider',),
        predicate='provider = ? AND account_key != ?',
        ordering='',
        selected=('COUNT(*)',),
        effects='read',
        classes=('cross-period',),
        inert_rule=ACCOUNTS,
    ),
    AccessContract(
        function='_cctally_cache:sync_cache',
        sql_fingerprint="SELECT 1 FROM schema_migrations WHERE name='001_dedup_highest_wins'",
        table='schema_migrations',
        keys=(),
        predicate="name='001_dedup_highest_wins'",
        ordering='',
        selected=('1',),
        effects='read',
        classes=('cross-period',),
        inert_rule=READ_IDX,
    ),
    # Reviewed re-fingerprint (#1015 T3, epoch 1022, spec 5.7). This one
    # executescript creates every quota projection table, and its literal
    # text changed in exactly two places: `meter_rate_change_events` gains the
    # NULLABLE `fit_lineage` column (the canonical JSON lineage of the fits a
    # transition compared, NULL on a row detected before #1015), and the
    # script now also creates `meter_rate_change_supersessions` (the terminal
    # supersession projection) with its explanatory comments outside the
    # statement. The statement stays a schema-only MAINT write run at open;
    # no detector decision reads either addition, so the contract stands.
    AccessContract(
        function='_cctally_core:_apply_quota_projection_schema',
        sql_fingerprint='CREATE TABLE IF NOT EXISTS quota_window_blocks ( id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, source_root_key TEXT NOT NULL, logical_limit_key TEXT NOT NULL, observed_slot TEXT NOT NULL, window_minutes INTEGER NOT NULL CHECK(window_minutes > 0), limit_id TEXT, limit_name TEXT, resets_at_utc TEXT NOT NULL, nominal_start_at_utc TEXT NOT NULL, first_observed_at_utc TEXT NOT NULL, last_observed_at_utc TEXT NOT NULL, first_percent REAL NOT NULL, current_percent REAL NOT NULL, last_source_path TEXT NOT NULL, last_line_offset INTEGER NOT NULL, generation TEXT NOT NULL, orphaned_at TEXT, account_key TEXT NOT NULL DEFAULT \'unattributed\', -- public #5: the REVERSE MAP. `physical_group_key` records the -- physical window this block was materialized from, so the -- generation sweep can be scoped to the groups a bounded pass -- actually re-materialized instead of to whole roots. -- `physical_group_digest` is that group\'s contribution to its -- root\'s physical signature: the root value is a digest over the -- root\'s sorted (group key, group digest) pairs, which makes it -- ASSOCIATIVE. A bounded pass recomputes only the dirty groups\' -- digests and re-derives the root value from the stored set — -- O(groups), 608 on the real store, against O(observations) at -- 211K. Hanging it off the blocks is what makes it self-maintaining: -- a group swept to nothing loses its blocks and drops out of the -- composition with them. physical_group_key TEXT, physical_group_digest TEXT, UNIQUE(source, source_root_key, account_key, logical_limit_key, observed_slot, window_minutes, resets_at_utc) ); CREATE INDEX IF NOT EXISTS idx_quota_blocks_active ON quota_window_blocks(source, source_root_key, account_key, orphaned_at, logical_limit_key, observed_slot, window_minutes, resets_at_utc); CREATE TABLE IF NOT EXISTS quota_percent_milestones ( id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, source_root_key TEXT NOT NULL, logical_limit_key TEXT NOT NULL, observed_slot TEXT NOT NULL, window_minutes INTEGER NOT NULL CHECK(window_minutes > 0), resets_at_utc TEXT NOT NULL, percent_threshold INTEGER NOT NULL CHECK(percent_threshold BETWEEN 1 AND 100), captured_at_utc TEXT NOT NULL, source_path TEXT NOT NULL, line_offset INTEGER NOT NULL, high_water_percent INTEGER NOT NULL CHECK(high_water_percent BETWEEN 1 AND 100), generation TEXT NOT NULL, orphaned_at TEXT, account_key TEXT NOT NULL DEFAULT \'unattributed\', UNIQUE(source, source_root_key, account_key, logical_limit_key, observed_slot, window_minutes, resets_at_utc, percent_threshold) ); CREATE INDEX IF NOT EXISTS idx_quota_milestones_active ON quota_percent_milestones(source, source_root_key, account_key, orphaned_at, logical_limit_key, observed_slot, window_minutes, resets_at_utc, percent_threshold); CREATE TABLE IF NOT EXISTS quota_threshold_events ( id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, source_root_key TEXT NOT NULL, logical_limit_key TEXT NOT NULL, observed_slot TEXT NOT NULL, window_minutes INTEGER NOT NULL CHECK(window_minutes > 0), resets_at_utc TEXT NOT NULL, threshold INTEGER NOT NULL CHECK(threshold BETWEEN 1 AND 100), qualifying_kind TEXT NOT NULL CHECK(qualifying_kind IN (\'actual\',\'projected\')), qualifying_percent REAL, projected_percent REAL, severity TEXT NOT NULL, created_at_utc TEXT NOT NULL, disposition TEXT NOT NULL CHECK(disposition IN (\'alerted\',\'suppressed_backfill\')), alerted_at TEXT, suppressed_at TEXT, orphaned_at TEXT, account_key TEXT NOT NULL DEFAULT \'unattributed\', CHECK((disposition = \'alerted\' AND alerted_at IS NOT NULL AND suppressed_at IS NULL) OR (disposition = \'suppressed_backfill\' AND suppressed_at IS NOT NULL AND alerted_at IS NULL)), UNIQUE(source, source_root_key, account_key, logical_limit_key, observed_slot, window_minutes, resets_at_utc, threshold) ); CREATE INDEX IF NOT EXISTS idx_quota_threshold_events_active ON quota_threshold_events(source, source_root_key, account_key, orphaned_at, logical_limit_key, observed_slot, window_minutes, resets_at_utc, threshold); CREATE TABLE IF NOT EXISTS quota_projection_state ( source_root_key TEXT NOT NULL, account_key TEXT NOT NULL DEFAULT \'unattributed\', generation TEXT NOT NULL, physical_signature TEXT NOT NULL, completed_at_utc TEXT NOT NULL, PRIMARY KEY(source_root_key, account_key) ); -- public #5: everything the incremental projector needs to know about -- its own last pass, keyed by provider source. One row, read once per -- reconcile. -- -- `watermark_seq` is the highest `quota_window_change_log.seq` this -- index has consumed. It is written INSIDE the same stats transaction -- as the projection it describes, so the two commit or roll back -- together; a crash therefore replays a ledger range rather than -- skipping one, which is safe because re-materializing a group is -- idempotent. (Writing it after the commit would need a second stats -- transaction, and `run_stats_ingest` is the sole stats writer.) -- -- `interpretation_version` invalidates the mechanism itself: a -- classification change alters interpreted keys with no row mutation -- for the ledger to observe, so a bump queues one complete pass. -- -- `alerts_enabled` / `next_evaluation_at_utc` are the two alert axes -- that are not functions of window dirtiness — a delivery-gate -- transition, and a future-clocked observation that becomes eligible -- when wall time passes it with no row mutation at all. -- `next_evaluation_by_root_json` owns that scalar minimum: one earliest -- future capture per root, so a due hook tick can reconcile only the -- roots whose instants matured. -- -- `last_full_pass_at` is the periodic verification\'s deadline. Two -- cases a scoped sweep structurally cannot see — a block whose physical -- group is absent from the cache entirely, and a milestone on a historic -- root no longer active — are otherwise repairable only by an -- interpretation bump, a rebuild or a burst overflow, none of which -- happen on a normal install. Every full pass stamps it, whatever -- triggered it, so the deadline is satisfied by whichever caller reaches -- it first and the bound on staleness is one interval rather than -- forever. NULL means "never verified", which reads as due. CREATE TABLE IF NOT EXISTS quota_projection_ledger_state ( source TEXT NOT NULL, watermark_seq INTEGER NOT NULL DEFAULT 0, interpretation_version INTEGER NOT NULL DEFAULT 0, alerts_enabled INTEGER, next_evaluation_at_utc TEXT, last_full_pass_at TEXT, next_evaluation_by_root_json TEXT NOT NULL DEFAULT \'{}\', PRIMARY KEY(source) ); -- #661 S2 §6: a provider metering-rate transition, recorded from the -- first upgrade whether or not notification is enabled (§6.2). The -- row is the durable forward-only latch: its presence is what stops -- the same transition alerting twice, and it survives a stats.db -- rebuild because the journal event that created it carries a -- self-sufficient payload (§6.5). -- -- The identity is `(provider, account_key, effective_from)` and the -- promise is stated at exactly that precision — once per exact key, -- not "once per provider regime". `reduce_state` closes and -- re-creates regimes on a fingerprint change and a revised detector -- can select a different `effective_from` for the same underlying -- transition; separately, R8 can turn a formerly merged identity into -- a real account key. Both produce a new key for a transition a human -- would call the same one. The FINGERPRINT stays out of the key -- deliberately: including it would re-alert on our own algorithm -- revisions. -- #690 / #692 (epoch 1012) adds the last four columns: the disclosure -- evidence captured at DETECTION time, stored as four independent -- facts rather than one merged status taxonomy. Folding the baseline -- count into `withholding_status` would make an otherwise healthy -- successor read as currently withheld. -- -- NULL, an empty array and 0 are THREE distinct states and every -- consumer must preserve the distinction: NULL means legacy or -- unrecoverable evidence, an empty typed set means assessed with no -- such origin, 0 means a clean baseline, and a positive count means -- thin baseline evidence. The #689 regime-recovery route sees stored -- calibration regimes and no `QuotaAnalysis` at all, and all four -- values are analysis-derived, so on that route all four are NULL by -- construction — which is why every one of them is nullable. -- -- `detector_input_causes` and `composition_provenance` hold canonical -- JSON arrays of enum VALUES, never their `repr`, so a sixth -- `CompositionProvenance` origin added later cannot silently alias -- onto an existing one. `baseline_withheld_days` carries -- `baseline_fit.population["withheld"]` — the count of fenced -- observations carrying a cause that fall strictly BEFORE -- `window_start` — as a count rather than a boolean, so zero, one and -- many stay distinguishable and honest copy stays possible. -- -- These comments sit OUTSIDE the statement deliberately: SQLite -- stores the CREATE TABLE text verbatim, so a comment written inside -- it lands in `sqlite_schema.sql`, in every `iterdump()` golden and -- in `_REBUILD_SCHEMA_FINGERPRINT`. CREATE TABLE IF NOT EXISTS meter_rate_change_events ( id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT NOT NULL, account_key TEXT NOT NULL DEFAULT \'unattributed\', effective_from TEXT NOT NULL, previous_units_per_point REAL NOT NULL, new_units_per_point REAL NOT NULL, severity TEXT NOT NULL, detected_at_utc TEXT NOT NULL, created_at_utc TEXT NOT NULL, notified_at TEXT, withholding_status TEXT, detector_input_causes TEXT, composition_provenance TEXT, baseline_withheld_days INTEGER, fit_lineage TEXT, UNIQUE(provider, account_key, effective_from) ); CREATE INDEX IF NOT EXISTS idx_meter_rate_change_events_key ON meter_rate_change_events(provider, account_key, effective_from); -- #1015 epoch 1022 (spec 5.7). `fit_lineage` above is the canonical -- JSON lineage of the fits a transition compared, NULL on a row -- detected before #1015. `meter_rate_change_supersessions` is the -- terminal, idempotent projection of the journalled -- `meter_rate_change_supersession` event: one row per superseded -- transition identity, `corrections` the canonical JSON op ids. CREATE TABLE IF NOT EXISTS meter_rate_change_supersessions ( provider TEXT NOT NULL, account_key TEXT NOT NULL, effective_from TEXT NOT NULL, reason TEXT NOT NULL, corrections TEXT NOT NULL, superseded_at_utc TEXT NOT NULL, created_at_utc TEXT NOT NULL, PRIMARY KEY (provider, account_key, effective_from) ); CREATE TABLE IF NOT EXISTS quota_alert_arming ( id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, source_root_key TEXT NOT NULL, logical_limit_key TEXT NOT NULL, observed_slot TEXT NOT NULL, window_minutes INTEGER NOT NULL CHECK(window_minutes > 0), rule_fingerprint TEXT NOT NULL, activated_at_utc TEXT NOT NULL, account_key TEXT NOT NULL DEFAULT \'unattributed\', UNIQUE(source, source_root_key, account_key, logical_limit_key, observed_slot, window_minutes) );',
        table='quota_window_blocks',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:_apply_quota_projection_schema',
        sql_fingerprint="CREATE TABLE quota_projection_state ( source_root_key TEXT NOT NULL, account_key TEXT NOT NULL DEFAULT 'unattributed', generation TEXT NOT NULL, physical_signature TEXT NOT NULL, completed_at_utc TEXT NOT NULL, PRIMARY KEY(source_root_key, account_key) )",
        table='quota_projection_state',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:_apply_quota_projection_schema',
        sql_fingerprint='DROP TABLE quota_projection_state',
        table='quota_projection_state',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:_apply_quota_projection_schema',
        sql_fingerprint='PRAGMA table_info(quota_projection_state)',
        table='quota_projection_state',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:_apply_stats_read_indexes',
        sql_fingerprint='dynamic:_cctally_core:_apply_stats_read_indexes#1',
        sql_shape='sha256:b2b8c8f0aa057c00',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:_get_latest_row_for_week',
        sql_fingerprint='dynamic:_cctally_core:_get_latest_row_for_week#1',
        sql_shape='sha256:8ab553bf203922bf',
        table='dynamic',
        keys=(),
        predicate='latest snapshot of ONE week date key (and account)',
        ordering='',
        selected=(),
        effects='read',
        classes=('period-keyed',),
        inert_rule=None,
    ),
    AccessContract(
        function='_cctally_core:_latest_reset_event_for_end',
        sql_fingerprint='dynamic:_cctally_core:_latest_reset_event_for_end#1',
        sql_shape='sha256:7e0f76eaaca68934',
        table='dynamic',
        keys=(),
        predicate='week_reset_events row for one new_week_end_at',
        ordering='',
        selected=(),
        effects='read',
        classes=('period-keyed',),
        inert_rule=None,
    ),
    AccessContract(
        function='_cctally_core:_rebuild_retired_week_reset_uniqueness',
        sql_fingerprint='ALTER TABLE week_reset_events__rebuild_1013 RENAME TO week_reset_events',
        table='week_reset_events',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:_rebuild_retired_week_reset_uniqueness',
        sql_fingerprint='DROP TABLE week_reset_events',
        table='week_reset_events',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:_rebuild_retired_week_reset_uniqueness',
        sql_fingerprint='PRAGMA index_list(week_reset_events)',
        table='week_reset_events',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:_rebuild_retired_week_reset_uniqueness',
        sql_fingerprint='PRAGMA table_info(week_reset_events)',
        table='week_reset_events',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:_rebuild_retired_week_reset_uniqueness',
        sql_fingerprint='dynamic:_cctally_core:_rebuild_retired_week_reset_uniqueness#1',
        sql_shape='sha256:a7e7c1e51534c5ea',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:_reset_aware_floor',
        sql_fingerprint='dynamic:_cctally_core:_reset_aware_floor#1',
        sql_shape='sha256:87c79958f961edbc',
        table='dynamic',
        keys=(),
        predicate='reset effective instant in [week_start_at, week_end_at); manual floor by account and date key',
        ordering='',
        selected=(),
        effects='read',
        classes=('range-keyed', 'period-keyed'),
        inert_rule=None,
    ),
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint='CREATE INDEX IF NOT EXISTS idx_claude_oauth_samples_five_hour ON claude_oauth_samples (account_key, five_hour_window_key, requested_at_utc)',
        table='claude_oauth_samples',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint='CREATE INDEX IF NOT EXISTS idx_claude_oauth_samples_weekly ON claude_oauth_samples (account_key, weekly_resets_at_utc, requested_at_utc)',
        table='claude_oauth_samples',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint='CREATE INDEX IF NOT EXISTS idx_cost_week_start_at_time ON weekly_cost_snapshots(week_start_at, captured_at_utc DESC, id DESC)',
        table='weekly_cost_snapshots',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint='CREATE INDEX IF NOT EXISTS idx_cost_week_time ON weekly_cost_snapshots(week_start_date, captured_at_utc DESC, id DESC)',
        table='weekly_cost_snapshots',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint='CREATE INDEX IF NOT EXISTS idx_five_hour_block_models_block ON five_hour_block_models(block_id)',
        table='five_hour_block_models',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint='CREATE INDEX IF NOT EXISTS idx_five_hour_block_models_window ON five_hour_block_models(five_hour_window_key)',
        table='five_hour_block_models',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint='CREATE INDEX IF NOT EXISTS idx_five_hour_block_projects_block ON five_hour_block_projects(block_id)',
        table='five_hour_block_projects',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint='CREATE INDEX IF NOT EXISTS idx_five_hour_block_projects_window ON five_hour_block_projects(five_hour_window_key)',
        table='five_hour_block_projects',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint='CREATE INDEX IF NOT EXISTS idx_five_hour_blocks_block_start ON five_hour_blocks(block_start_at DESC)',
        table='five_hour_blocks',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint='CREATE INDEX IF NOT EXISTS idx_five_hour_milestones_block ON five_hour_milestones(block_id)',
        table='five_hour_milestones',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint='CREATE INDEX IF NOT EXISTS idx_journal_protocol_violations_batch ON journal_protocol_violations(batch_id)',
        table='journal_protocol_violations',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint='CREATE INDEX IF NOT EXISTS idx_usage_week_start_at_time ON weekly_usage_snapshots(week_start_at, captured_at_utc DESC, id DESC)',
        table='weekly_usage_snapshots',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint='CREATE INDEX IF NOT EXISTS idx_usage_week_time ON weekly_usage_snapshots(week_start_date, captured_at_utc DESC, id DESC)',
        table='weekly_usage_snapshots',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint='CREATE INDEX IF NOT EXISTS idx_weekly_usage_snapshots_5h_window_key ON weekly_usage_snapshots(five_hour_window_key)',
        table='weekly_usage_snapshots',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint="CREATE TABLE IF NOT EXISTS accounts ( account_key TEXT PRIMARY KEY, provider TEXT NOT NULL, natural_id TEXT, email TEXT, label TEXT, plan_type TEXT, label_source TEXT NOT NULL DEFAULT 'auto', first_seen_utc TEXT, last_seen_utc TEXT )",
        table='accounts',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint="CREATE TABLE IF NOT EXISTS budget_milestones ( id INTEGER PRIMARY KEY AUTOINCREMENT, vendor TEXT NOT NULL, period_start_at TEXT NOT NULL, period TEXT, threshold INTEGER NOT NULL, budget_usd REAL NOT NULL, spent_usd REAL NOT NULL, consumption_pct REAL NOT NULL, crossed_at_utc TEXT NOT NULL, alerted_at TEXT, account_key TEXT NOT NULL DEFAULT '*', UNIQUE(vendor, account_key, period_start_at, period, threshold) )",
        table='budget_milestones',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint='CREATE TABLE IF NOT EXISTS claude_oauth_samples ( account_key TEXT NOT NULL, observation_id TEXT NOT NULL, requested_at_utc TEXT NOT NULL, captured_at_utc TEXT NOT NULL, weekly_percent REAL, weekly_resets_at_utc TEXT, five_hour_percent REAL, five_hour_resets_at_utc TEXT, five_hour_window_key INTEGER, PRIMARY KEY (account_key, observation_id) )',
        table='claude_oauth_samples',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint='CREATE TABLE IF NOT EXISTS claude_weekly_observation_decisions ( observation_id TEXT PRIMARY KEY, disposition TEXT NOT NULL, decision_op_id TEXT NOT NULL )',
        table='claude_weekly_observation_decisions',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    # #1015 epoch 1022 (spec 5.1): the correction-to-fit lineage
    # projections, created by open_db's schema block beside
    # `claude_weekly_observation_decisions`: the observation index and its
    # (account_key, captured_at_utc) index, the reviewed decision history,
    # the decision op coordinates, and the completed batch lineage with its
    # footprint. Schema-only statements run at open, so MAINT.
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint='CREATE INDEX IF NOT EXISTS idx_claude_weekly_observation_index_window ON claude_weekly_observation_index (account_key, captured_at_utc)',
        table='claude_weekly_observation_index',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint="CREATE TABLE IF NOT EXISTS claude_weekly_correction_history ( decision_op_id TEXT NOT NULL, observation_id TEXT NOT NULL, disposition TEXT NOT NULL CHECK(disposition IN ('hold','retract','accept')), PRIMARY KEY (decision_op_id, observation_id) )",
        table='claude_weekly_correction_history',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint='CREATE TABLE IF NOT EXISTS claude_weekly_correction_ops ( decision_op_id TEXT PRIMARY KEY, op_segment TEXT NOT NULL, op_offset INTEGER NOT NULL )',
        table='claude_weekly_correction_ops',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    # #1018 twin epoch 1024 (spec 5.1): the observation index gains its four
    # enriched columns and the partial freshness index. Schema-only, MAINT.
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint='CREATE INDEX IF NOT EXISTS idx_claude_weekly_observation_index_freshness ON claude_weekly_observation_index (account_key, weekly_resets_at_utc, weekly_percent, captured_at_utc DESC) WHERE weekly_freshness_eligible = 1',
        table='claude_weekly_observation_index',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint='CREATE TABLE IF NOT EXISTS claude_weekly_observation_index ( observation_id TEXT PRIMARY KEY, account_key TEXT NOT NULL, captured_at_utc TEXT NOT NULL, weekly_percent REAL, weekly_resets_at_utc TEXT, weekly_reading_genuine INTEGER NOT NULL DEFAULT 0, weekly_freshness_eligible INTEGER NOT NULL DEFAULT 0 )',
        table='claude_weekly_observation_index',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint='CREATE TABLE IF NOT EXISTS correction_batch_footprint ( batch_id TEXT NOT NULL, provider TEXT NOT NULL, account_key TEXT NOT NULL, PRIMARY KEY (batch_id, provider, account_key) )',
        table='correction_batch_footprint',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint='CREATE TABLE IF NOT EXISTS correction_batch_lineage ( batch_id TEXT PRIMARY KEY, commit_segment TEXT NOT NULL, commit_offset INTEGER NOT NULL )',
        table='correction_batch_lineage',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint="CREATE TABLE IF NOT EXISTS five_hour_block_models ( id INTEGER PRIMARY KEY AUTOINCREMENT, block_id INTEGER NOT NULL, five_hour_window_key INTEGER NOT NULL, model TEXT NOT NULL, input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0, cache_create_tokens INTEGER NOT NULL DEFAULT 0, cache_read_tokens INTEGER NOT NULL DEFAULT 0, cost_usd REAL NOT NULL DEFAULT 0, entry_count INTEGER NOT NULL DEFAULT 0, account_key TEXT NOT NULL DEFAULT 'unattributed', UNIQUE(account_key, five_hour_window_key, model), FOREIGN KEY (block_id) REFERENCES five_hour_blocks(id) )",
        table='five_hour_block_models',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint="CREATE TABLE IF NOT EXISTS five_hour_block_projects ( id INTEGER PRIMARY KEY AUTOINCREMENT, block_id INTEGER NOT NULL, five_hour_window_key INTEGER NOT NULL, project_path TEXT NOT NULL, input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0, cache_create_tokens INTEGER NOT NULL DEFAULT 0, cache_read_tokens INTEGER NOT NULL DEFAULT 0, cost_usd REAL NOT NULL DEFAULT 0, entry_count INTEGER NOT NULL DEFAULT 0, account_key TEXT NOT NULL DEFAULT 'unattributed', UNIQUE(account_key, five_hour_window_key, project_path), FOREIGN KEY (block_id) REFERENCES five_hour_blocks(id) )",
        table='five_hour_block_projects',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint="CREATE TABLE IF NOT EXISTS five_hour_blocks ( id INTEGER PRIMARY KEY AUTOINCREMENT, five_hour_window_key INTEGER NOT NULL, five_hour_resets_at TEXT NOT NULL, block_start_at TEXT NOT NULL, first_observed_at_utc TEXT NOT NULL, last_observed_at_utc TEXT NOT NULL, final_five_hour_percent REAL NOT NULL, seven_day_pct_at_block_start REAL, seven_day_pct_at_block_end REAL, crossed_seven_day_reset INTEGER NOT NULL DEFAULT 0, total_input_tokens INTEGER NOT NULL DEFAULT 0, total_output_tokens INTEGER NOT NULL DEFAULT 0, total_cache_create_tokens INTEGER NOT NULL DEFAULT 0, total_cache_read_tokens INTEGER NOT NULL DEFAULT 0, total_cost_usd REAL NOT NULL DEFAULT 0, pricing_provenance_json TEXT, is_closed INTEGER NOT NULL DEFAULT 0, created_at_utc TEXT NOT NULL, last_updated_at_utc TEXT NOT NULL, account_key TEXT NOT NULL DEFAULT 'unattributed', UNIQUE(account_key, five_hour_window_key) )",
        table='five_hour_blocks',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint='CREATE TABLE IF NOT EXISTS five_hour_credit_confirmation_state ( account_key TEXT NOT NULL, five_hour_window_key INTEGER NOT NULL, source TEXT NOT NULL, baseline_pct REAL NOT NULL, pending_low_pct REAL, pending_at_utc TEXT, pending_observation_id TEXT, baseline_observation_id TEXT, baseline_generation TEXT, pending_generation TEXT, pending_position TEXT, pending_captured_at_utc TEXT, PRIMARY KEY (account_key, five_hour_window_key, source) )',
        table='five_hour_credit_confirmation_state',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    # #943 T3 (spec 4.7): the exact-boundary companion of a five-hour credit,
    # a harvest family (prefix `fhcb`, order 29) keyed by the credit's
    # natural key. Schema creation only.
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint='CREATE TABLE IF NOT EXISTS five_hour_credit_boundaries ( id INTEGER PRIMARY KEY, account_key TEXT NOT NULL, five_hour_window_key INTEGER NOT NULL, effective_reset_at_utc TEXT NOT NULL, boundary_at_utc TEXT NOT NULL, arming_observation_id TEXT NOT NULL, credit_revision TEXT NOT NULL, detected_at_utc TEXT NOT NULL, journal_id TEXT, UNIQUE(account_key, five_hour_window_key, effective_reset_at_utc) )',
        table='five_hour_credit_boundaries',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    # #956 T5 (spec 4.6.3): the opening fact of a five-hour block, a harvest
    # family (prefix `fhbo`, order 35) keyed by the block's natural key.
    # Schema creation only.
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint='CREATE TABLE IF NOT EXISTS five_hour_block_openings ( id INTEGER PRIMARY KEY, account_key TEXT NOT NULL, five_hour_window_key INTEGER NOT NULL, five_hour_resets_at TEXT NOT NULL, block_start_at TEXT NOT NULL, first_observed_at_utc TEXT NOT NULL, created_at_utc TEXT NOT NULL, opening_record_id TEXT NOT NULL, creation_kind TEXT NOT NULL, five_hour_percent REAL NOT NULL, seven_day_pct_at_block_start REAL, weekly_observation_held INTEGER NOT NULL DEFAULT 0, weekly_basis_captured_at_utc TEXT, journal_id TEXT, UNIQUE(account_key, five_hour_window_key) )',
        table='five_hour_block_openings',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint="CREATE TABLE IF NOT EXISTS five_hour_milestones ( id INTEGER PRIMARY KEY AUTOINCREMENT, block_id INTEGER NOT NULL, five_hour_window_key INTEGER NOT NULL, percent_threshold INTEGER NOT NULL, captured_at_utc TEXT NOT NULL, usage_snapshot_id INTEGER NOT NULL, block_input_tokens INTEGER NOT NULL DEFAULT 0, block_output_tokens INTEGER NOT NULL DEFAULT 0, block_cache_create_tokens INTEGER NOT NULL DEFAULT 0, block_cache_read_tokens INTEGER NOT NULL DEFAULT 0, block_cost_usd REAL NOT NULL DEFAULT 0, marginal_cost_usd REAL, seven_day_pct_at_crossing REAL, reset_event_id INTEGER NOT NULL DEFAULT 0, account_key TEXT NOT NULL DEFAULT 'unattributed', UNIQUE(account_key, five_hour_window_key, percent_threshold, reset_event_id), FOREIGN KEY (block_id) REFERENCES five_hour_blocks(id) )",
        table='five_hour_milestones',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint="CREATE TABLE IF NOT EXISTS five_hour_reset_events ( id INTEGER PRIMARY KEY AUTOINCREMENT, detected_at_utc TEXT NOT NULL, five_hour_window_key INTEGER NOT NULL, prior_percent REAL NOT NULL, post_percent REAL NOT NULL, effective_reset_at_utc TEXT NOT NULL, account_key TEXT NOT NULL DEFAULT 'unattributed', UNIQUE(account_key, five_hour_window_key, effective_reset_at_utc) )",
        table='five_hour_reset_events',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint="CREATE TABLE IF NOT EXISTS journal_admission_summary (id INTEGER PRIMARY KEY CHECK (id = 1), cutover_id TEXT, scanned_high_water TEXT, excluded_total INTEGER NOT NULL DEFAULT 0, family_counts_json TEXT NOT NULL DEFAULT '{}', first_excluded_coordinate TEXT, latest_excluded_coordinate TEXT, coordinate_digest TEXT, complete_through TEXT, boundary_json TEXT)",
        table='journal_admission_summary',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint='CREATE TABLE IF NOT EXISTS journal_cursor (id INTEGER PRIMARY KEY CHECK (id = 1), segment TEXT NOT NULL, offset INTEGER NOT NULL, applied_segment TEXT, applied_offset INTEGER)',
        table='journal_cursor',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint="CREATE TABLE IF NOT EXISTS journal_effective_events (event_id TEXT PRIMARY KEY, rev INTEGER NOT NULL CHECK (rev >= 0), status TEXT NOT NULL CHECK (status IN ('active','tombstone')), content_hash TEXT NOT NULL, batch_id TEXT, event_json TEXT, winning_sequence INTEGER, conflict_hashes_json TEXT)",
        table='journal_effective_events',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint='CREATE TABLE IF NOT EXISTS journal_protocol_violations (fingerprint TEXT PRIMARY KEY, batch_id TEXT NOT NULL, kind TEXT NOT NULL, violation_json TEXT NOT NULL, available_after INTEGER)',
        table='journal_protocol_violations',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint="CREATE TABLE IF NOT EXISTS journal_selector_batch_records (batch_id TEXT NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('marker','action')), key TEXT NOT NULL, record_digest TEXT NOT NULL, identity_digest TEXT, sequence INTEGER NOT NULL, action_core_json TEXT, PRIMARY KEY (batch_id, kind, key))",
        table='journal_selector_batch_records',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint="CREATE TABLE IF NOT EXISTS journal_selector_batches (batch_id TEXT PRIMARY KEY, status TEXT NOT NULL CHECK (status IN ('begin_only','completed','tainted')), action_count INTEGER, action_set_hash TEXT, begin_segment TEXT, begin_offset INTEGER, earliest_commit_segment TEXT, earliest_commit_offset INTEGER)",
        table='journal_selector_batches',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint='CREATE TABLE IF NOT EXISTS journal_selector_state (id INTEGER PRIMARY KEY CHECK (id = 1), generation_record_path TEXT, generation_stamped_at_utc TEXT, covered_segment TEXT, covered_offset INTEGER, next_sequence INTEGER NOT NULL DEFAULT 0, selector_version INTEGER NOT NULL, cutover_seen INTEGER NOT NULL DEFAULT 0, cutover_account_key TEXT)',
        table='journal_selector_state',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint='CREATE TABLE IF NOT EXISTS outcome_quarantine_axis_state (generation INTEGER NOT NULL, account_key TEXT NOT NULL, axis TEXT NOT NULL, state TEXT NOT NULL, waiting_for TEXT, coverage_id TEXT, witness_id TEXT, release_id TEXT, revocation TEXT, since TEXT, recovered_at TEXT, not_before INTEGER, witness_key TEXT, PRIMARY KEY (generation, account_key, axis))',
        table='outcome_quarantine_axis_state',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint='CREATE TABLE IF NOT EXISTS outcome_quarantine_bounds (scope TEXT NOT NULL, axis TEXT NOT NULL, bounds_json TEXT NOT NULL, PRIMARY KEY (scope, axis))',
        table='outcome_quarantine_bounds',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint='CREATE TABLE IF NOT EXISTS outcome_quarantine_episodes (opener_segment TEXT, opener_offset INTEGER, opened_at TEXT, closer_segment TEXT, closer_offset INTEGER, closed_at TEXT, PRIMARY KEY (opener_segment, opener_offset))',
        table='outcome_quarantine_episodes',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint='CREATE TABLE IF NOT EXISTS outcome_quarantine_triggers (seq INTEGER PRIMARY KEY, segment TEXT, byte_offset INTEGER, kind TEXT NOT NULL, reason TEXT NOT NULL, at TEXT)',
        table='outcome_quarantine_triggers',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint="CREATE TABLE IF NOT EXISTS percent_milestones ( id INTEGER PRIMARY KEY AUTOINCREMENT, captured_at_utc TEXT NOT NULL, week_start_date TEXT NOT NULL, week_end_date TEXT NOT NULL, week_start_at TEXT, week_end_at TEXT, percent_threshold INTEGER NOT NULL, cumulative_cost_usd REAL NOT NULL, marginal_cost_usd REAL, usage_snapshot_id INTEGER NOT NULL, cost_snapshot_id INTEGER NOT NULL, reset_event_id INTEGER NOT NULL DEFAULT 0, account_key TEXT NOT NULL DEFAULT 'unattributed', UNIQUE(account_key, week_start_date, percent_threshold, reset_event_id) )",
        table='percent_milestones',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint="CREATE TABLE IF NOT EXISTS project_budget_milestones ( id INTEGER PRIMARY KEY AUTOINCREMENT, week_start_at TEXT NOT NULL, project_key TEXT NOT NULL, -- canonical git-root (bucket_path) threshold INTEGER NOT NULL, budget_usd REAL NOT NULL, -- project's target snapshotted AT crossing spent_usd REAL NOT NULL, consumption_pct REAL NOT NULL, crossed_at_utc TEXT NOT NULL, alerted_at TEXT, account_key TEXT NOT NULL DEFAULT '*', -- account-blind this epic (spec §6): always '*' UNIQUE(account_key, week_start_at, project_key, threshold) )",
        table='project_budget_milestones',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint="CREATE TABLE IF NOT EXISTS projected_milestones ( id INTEGER PRIMARY KEY AUTOINCREMENT, week_start_at TEXT NOT NULL, -- period-start instant (subscription-week OR calendar period-start; back-compat name) period TEXT, -- configured period at crossing; NULL = pre-011 unknown (migration 011) metric TEXT NOT NULL, -- 'weekly_pct' | 'budget_usd' | 'codex_budget_usd' threshold INTEGER NOT NULL, -- 90 | 100 projected_value REAL NOT NULL, denominator REAL NOT NULL, -- target_usd (budget / codex_budget) | 100.0 (weekly) crossed_at_utc TEXT NOT NULL, alerted_at TEXT, account_key TEXT NOT NULL DEFAULT '*', -- '*' for vendor-budget metrics; real account for weekly_pct (Task 3) UNIQUE(account_key, week_start_at, period, metric, threshold) )",
        table='projected_milestones',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint='CREATE TABLE IF NOT EXISTS stats_publication_stamp (record_path TEXT NOT NULL, started_at_utc TEXT NOT NULL, stamped_at_utc TEXT NOT NULL)',
        table='stats_publication_stamp',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint='CREATE TABLE IF NOT EXISTS stats_quota_projection_state (id INTEGER PRIMARY KEY CHECK (id = 1), incomplete INTEGER NOT NULL DEFAULT 0, target_version INTEGER NOT NULL DEFAULT 0, recovery_target_json TEXT)',
        table='stats_quota_projection_state',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint='CREATE TABLE IF NOT EXISTS usage_snapshot_suppressions (journal_id TEXT PRIMARY KEY)',
        table='usage_snapshot_suppressions',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint="CREATE TABLE IF NOT EXISTS week_reset_events ( id INTEGER PRIMARY KEY AUTOINCREMENT, detected_at_utc TEXT NOT NULL, old_week_end_at TEXT NOT NULL, new_week_end_at TEXT NOT NULL, effective_reset_at_utc TEXT NOT NULL, observed_pre_credit_pct REAL, account_key TEXT NOT NULL DEFAULT 'unattributed', origin_observation_id TEXT )",
        table='week_reset_events',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint="CREATE TABLE IF NOT EXISTS weekly_cost_snapshots ( id INTEGER PRIMARY KEY AUTOINCREMENT, captured_at_utc TEXT NOT NULL, week_start_date TEXT NOT NULL, week_end_date TEXT NOT NULL, week_start_at TEXT, week_end_at TEXT, range_start_iso TEXT, range_end_iso TEXT, cost_usd REAL NOT NULL, source TEXT NOT NULL DEFAULT 'cctally-range-cost', mode TEXT NOT NULL DEFAULT 'auto', project TEXT, account_key TEXT NOT NULL DEFAULT 'unattributed' )",
        table='weekly_cost_snapshots',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint="CREATE TABLE IF NOT EXISTS weekly_credit_floors ( id INTEGER PRIMARY KEY AUTOINCREMENT, week_start_date TEXT NOT NULL, effective_at_utc TEXT NOT NULL, observed_pre_credit_pct REAL NOT NULL, applied_at_utc TEXT NOT NULL, account_key TEXT NOT NULL DEFAULT 'unattributed', UNIQUE(account_key, week_start_date, effective_at_utc) )",
        table='weekly_credit_floors',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint='CREATE TABLE IF NOT EXISTS weekly_reset_debounce_state ( account_key TEXT PRIMARY KEY, week_start_date TEXT NOT NULL, week_end_at TEXT NOT NULL, baseline_pct REAL NOT NULL, first_zero_at_utc TEXT NOT NULL, first_zero_observation_id TEXT )',
        table='weekly_reset_debounce_state',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint="CREATE TABLE IF NOT EXISTS weekly_usage_snapshots ( id INTEGER PRIMARY KEY AUTOINCREMENT, captured_at_utc TEXT NOT NULL, week_start_date TEXT NOT NULL, week_end_date TEXT NOT NULL, week_start_at TEXT, week_end_at TEXT, weekly_percent REAL NOT NULL, page_url TEXT, source TEXT NOT NULL DEFAULT 'userscript', payload_json TEXT NOT NULL, account_key TEXT NOT NULL DEFAULT 'unattributed', weekly_observation_held INTEGER NOT NULL DEFAULT 0 CHECK (weekly_observation_held IN (0, 1)) )",
        table='weekly_usage_snapshots',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint='CREATE TRIGGER IF NOT EXISTS trg_usage_snapshot_suppression AFTER DELETE ON weekly_usage_snapshots BEGIN INSERT OR IGNORE INTO usage_snapshot_suppressions (journal_id) SELECT OLD.journal_id WHERE OLD.journal_id IS NOT NULL; UPDATE percent_milestones SET usage_snapshot_id = 0 WHERE usage_snapshot_id = OLD.id; UPDATE five_hour_milestones SET usage_snapshot_id = 0 WHERE usage_snapshot_id = OLD.id; END',
        table='usage_snapshot_suppressions',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint='CREATE UNIQUE INDEX IF NOT EXISTS idx_week_reset_events_legacy_tuple ON week_reset_events(account_key, old_week_end_at, new_week_end_at) WHERE origin_observation_id IS NULL',
        table='week_reset_events',
        keys=(),
        predicate='origin_observation_id IS NULL',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint='CREATE UNIQUE INDEX IF NOT EXISTS idx_week_reset_events_origin ON week_reset_events(account_key, origin_observation_id) WHERE origin_observation_id IS NOT NULL',
        table='week_reset_events',
        keys=(),
        predicate='origin_observation_id IS NOT NULL',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint='SELECT 1 FROM five_hour_blocks LIMIT 1',
        table='five_hour_blocks',
        keys=(),
        predicate='',
        ordering='',
        selected=('1',),
        effects='read',
        classes=('cross-period',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint='SELECT 1 FROM weekly_usage_snapshots WHERE five_hour_resets_at IS NOT NULL AND five_hour_window_key IS NULL LIMIT 1',
        table='weekly_usage_snapshots',
        keys=(),
        predicate='five_hour_resets_at IS NOT NULL AND five_hour_window_key IS NULL',
        ordering='',
        selected=('1',),
        effects='read',
        classes=('cross-period',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint='SELECT 1 FROM weekly_usage_snapshots WHERE five_hour_window_key IS NOT NULL AND five_hour_percent IS NOT NULL LIMIT 1',
        table='weekly_usage_snapshots',
        keys=(),
        predicate='five_hour_window_key IS NOT NULL AND five_hour_percent IS NOT NULL',
        ordering='',
        selected=('1',),
        effects='read',
        classes=('cross-period',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint='SELECT id, five_hour_resets_at FROM weekly_usage_snapshots WHERE five_hour_resets_at IS NOT NULL AND five_hour_window_key IS NULL',
        table='weekly_usage_snapshots',
        keys=(),
        predicate='five_hour_resets_at IS NOT NULL AND five_hour_window_key IS NULL',
        ordering='',
        selected=('id', 'five_hour_resets_at'),
        effects='read',
        classes=('cross-period',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint='SELECT name FROM schema_migrations_skipped',
        table='schema_migrations_skipped',
        keys=(),
        predicate='',
        ordering='',
        selected=('name',),
        effects='read',
        classes=('cross-period',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint='UPDATE journal_cursor SET applied_segment = segment, applied_offset = offset WHERE applied_segment IS NULL AND applied_offset IS NULL',
        table='journal_cursor',
        keys=(),
        predicate='applied_segment IS NULL AND applied_offset IS NULL',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint='UPDATE weekly_usage_snapshots SET five_hour_window_key = ? WHERE id = ?',
        table='weekly_usage_snapshots',
        keys=('id',),
        predicate='id = ?',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    # Reviewed re-pin (#797/#823/#840 Task 2, base 5f3db4ddd), for open_db
    # #1, #2 and #3. Changed: only the producer `open_db` itself, whose
    # epoch-1019 schema block now also creates `claude_oauth_samples`, its
    # two indexes and `claude_weekly_observation_decisions`, and creates
    # `five_hour_credit_confirmation_state` with four more NULLABLE
    # columns. Those are literal statements, registered as their own MAINT
    # contracts above. No line of these three dynamic sites, their helpers
    # or the bindings they read changed, so each site's table selection,
    # predicates, ordering, selected columns, execution condition and
    # read-only effect are unchanged and the contract and its MAINT inert
    # rule stand.
    # Reviewed re-pin (#1019/#1014 Task A, base 5b15db929), for open_db #1,
    # #2 and #3. Changed: only bindings these sites read through `open_db`'s
    # closure — `STATS_INDEX_EPOCH` 1019 -> 1020 (spec 4.7 C6), and the
    # `_init_paths_from_env` assignments of APP_DIR, DB_PATH, LOG_DIR,
    # HOOK_TICK_LOG_DIR and STATUSLINE_ACCOUNT_BOUND_DIR, whose statements
    # changed because the same function now also assigns the retired v2 root
    # and the revision-3 cache path and names the v3 root. DB_PATH's value is
    # unchanged. No SQL, table selection, predicate, ordering, selected column,
    # execution condition or effect changed, so each contract and its MAINT
    # inert rule stand.
    # Reviewed re-pin (#943 T3, base c5419d209), for open_db #1. Changed: only
    # bindings this site reads — `STATS_INDEX_EPOCH` 1020 -> 1021 (spec 4.7)
    # and the producer `open_db` itself, whose epoch-1021 schema block now
    # also creates `five_hour_credit_boundaries` with its two journal-id
    # indexes and creates `five_hour_credit_confirmation_state` with the
    # NULLABLE `pending_captured_at_utc` column; those are literal statements,
    # registered as their own MAINT contracts. No SQL, table selection,
    # predicate, ordering, selected column, execution condition or effect of
    # this site changed, so the contract and its inert rule stand.
    # Reviewed re-pin (#956 T5, base af10118c7), for open_db #1. Changed: only
    # the producer `open_db` itself, whose epoch-1021 schema block now also
    # creates `five_hour_block_openings` (the #956 class-2 opening fact, spec
    # 4.6.3) and lists it in the two journal-id index loops; the CREATE TABLE is
    # a literal statement, registered as its own MAINT contract. No SQL, table
    # selection, predicate, ordering, selected column, execution condition or
    # effect of this site changed, so the contract and its inert rule stand.
    # Reviewed re-pin (#1015 T3, base 16b055cae), for open_db #1. Changed: the
    # binding `STATS_INDEX_EPOCH` 1021 -> 1022 (spec 5.1) and the producer
    # `open_db` itself, whose epoch-1022 schema block now also creates
    # `claude_weekly_observation_index` with its (account_key,
    # captured_at_utc) index, `claude_weekly_correction_history`,
    # `claude_weekly_correction_ops`, `correction_batch_lineage` and
    # `correction_batch_footprint`; those are literal statements, registered
    # as their own MAINT contracts. No SQL, table selection, predicate,
    # ordering, selected column, execution condition or effect of this site
    # changed, so the contract and its MAINT inert rule stand.
    # Reviewed re-pin (#948 T-A, base 7e2ea2411), for open_db #1, #2 and #3.
    # Changed: the binding `STATS_INDEX_EPOCH` 1022 -> 1023 and the producer
    # `open_db` itself, whose epoch-1023 schema block no longer creates
    # `outcome_quarantine_envelope` / `outcome_quarantine_certificates` and
    # now creates `outcome_quarantine_episodes`, `outcome_quarantine_bounds`
    # and `outcome_quarantine_axis_state`; those are literal statements,
    # registered as their own MAINT contracts. No SQL, table selection,
    # predicate, ordering, selected column, execution condition or effect of
    # these sites changed, so each contract and its MAINT inert rule stand.
    # Reviewed re-pin (#1018 twin T1, base d94a83792), for open_db #1.
    # Changed: the binding `STATS_INDEX_EPOCH` 1023 -> 1024 (spec 5.1) and the
    # producer `open_db` itself, whose epoch-1024 schema block creates
    # `claude_weekly_observation_index` with the four enriched columns
    # (`weekly_percent`, `weekly_resets_at_utc`, `weekly_reading_genuine`,
    # `weekly_freshness_eligible`) and the partial index
    # `idx_claude_weekly_observation_index_freshness`; those are literal
    # statements, registered as their own MAINT contracts. No SQL, table
    # selection, predicate, ordering, selected column, execution condition or
    # effect of this site changed, so the contract and its MAINT inert rule
    # stand.
    # Reviewed re-pin (#1050 T2, base a858a87ab), for open_db #1. Changed:
    # only the producer `open_db` itself, which now calls the new helper
    # `_ensure_stats_content_revision` before both current-epoch `return
    # conn` paths and after the epoch stamp of the fresh, scratch and cutover
    # schema apply (spec 4.3). That helper installs the content-revision
    # kernel's tables, triggers and index; its statements are registered as
    # their own MAINT contracts under `_lib_stats_content_revision`. No SQL,
    # table selection, predicate, ordering, selected column, execution
    # condition or effect of this site changed, so the contract and its MAINT
    # inert rule stand.
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint='dynamic:_cctally_core:open_db#1',
        sql_shape='sha256:89c6c21da1b99cc6',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=MAINT,
    ),
    # Reviewed re-pin (#943 T3, base c5419d209), for open_db #2. Changed: only
    # bindings this site reads — `STATS_INDEX_EPOCH` 1020 -> 1021 (spec 4.7)
    # and the producer `open_db` itself, whose epoch-1021 schema block now
    # also creates `five_hour_credit_boundaries` with its two journal-id
    # indexes and creates `five_hour_credit_confirmation_state` with the
    # NULLABLE `pending_captured_at_utc` column; those are literal statements,
    # registered as their own MAINT contracts. No SQL, table selection,
    # predicate, ordering, selected column, execution condition or effect of
    # this site changed, so the contract and its inert rule stand.
    # Reviewed re-pin (#956 T5, base af10118c7), for open_db #2. Changed: only
    # the producer `open_db` itself, whose epoch-1021 schema block now also
    # creates `five_hour_block_openings` (the #956 class-2 opening fact, spec
    # 4.6.3) and lists it in the two journal-id index loops; the CREATE TABLE is
    # a literal statement, registered as its own MAINT contract. No SQL, table
    # selection, predicate, ordering, selected column, execution condition or
    # effect of this site changed, so the contract and its inert rule stand.
    # Reviewed re-pin (#1015 T3, base 16b055cae), for open_db #2. Changed: the
    # binding `STATS_INDEX_EPOCH` 1021 -> 1022 (spec 5.1) and the producer
    # `open_db` itself, whose epoch-1022 schema block now also creates
    # `claude_weekly_observation_index` with its (account_key,
    # captured_at_utc) index, `claude_weekly_correction_history`,
    # `claude_weekly_correction_ops`, `correction_batch_lineage` and
    # `correction_batch_footprint`; those are literal statements, registered
    # as their own MAINT contracts. No SQL, table selection, predicate,
    # ordering, selected column, execution condition or effect of this site
    # changed, so the contract and its MAINT inert rule stand.
    # Reviewed re-pin (#1018 twin T1, base d94a83792), for open_db #2.
    # Changed: the binding `STATS_INDEX_EPOCH` 1023 -> 1024 (spec 5.1) and the
    # producer `open_db` itself, whose epoch-1024 schema block creates
    # `claude_weekly_observation_index` with the four enriched columns
    # (`weekly_percent`, `weekly_resets_at_utc`, `weekly_reading_genuine`,
    # `weekly_freshness_eligible`) and the partial index
    # `idx_claude_weekly_observation_index_freshness`; those are literal
    # statements, registered as their own MAINT contracts. No SQL, table
    # selection, predicate, ordering, selected column, execution condition or
    # effect of this site changed, so the contract and its MAINT inert rule
    # stand.
    # Reviewed re-pin (#1050 T2, base a858a87ab), for open_db #2. Changed:
    # only the producer `open_db` itself, which now calls the new helper
    # `_ensure_stats_content_revision` before both current-epoch `return
    # conn` paths and after the epoch stamp of the fresh, scratch and cutover
    # schema apply (spec 4.3). That helper installs the content-revision
    # kernel's tables, triggers and index; its statements are registered as
    # their own MAINT contracts under `_lib_stats_content_revision`. No SQL,
    # table selection, predicate, ordering, selected column, execution
    # condition or effect of this site changed, so the contract and its MAINT
    # inert rule stand.
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint='dynamic:_cctally_core:open_db#2',
        sql_shape='sha256:da7e324fa4e6c968',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=MAINT,
    ),
    # Reviewed re-pin (#943 T3, base c5419d209), for open_db #3. Changed: only
    # bindings this site reads — `STATS_INDEX_EPOCH` 1020 -> 1021 (spec 4.7)
    # and the producer `open_db` itself, whose epoch-1021 schema block now
    # also creates `five_hour_credit_boundaries` with its two journal-id
    # indexes and creates `five_hour_credit_confirmation_state` with the
    # NULLABLE `pending_captured_at_utc` column; those are literal statements,
    # registered as their own MAINT contracts. No SQL, table selection,
    # predicate, ordering, selected column, execution condition or effect of
    # this site changed, so the contract and its inert rule stand.
    # Reviewed re-pin (#956 T5, base af10118c7), for open_db #3. Changed: only
    # the producer `open_db` itself, whose epoch-1021 schema block now also
    # creates `five_hour_block_openings` (the #956 class-2 opening fact, spec
    # 4.6.3) and lists it in the two journal-id index loops; the CREATE TABLE is
    # a literal statement, registered as its own MAINT contract. No SQL, table
    # selection, predicate, ordering, selected column, execution condition or
    # effect of this site changed, so the contract and its inert rule stand.
    # Reviewed re-pin (#1015 T3, base 16b055cae), for open_db #3. Changed: the
    # binding `STATS_INDEX_EPOCH` 1021 -> 1022 (spec 5.1) and the producer
    # `open_db` itself, whose epoch-1022 schema block now also creates
    # `claude_weekly_observation_index` with its (account_key,
    # captured_at_utc) index, `claude_weekly_correction_history`,
    # `claude_weekly_correction_ops`, `correction_batch_lineage` and
    # `correction_batch_footprint`; those are literal statements, registered
    # as their own MAINT contracts. No SQL, table selection, predicate,
    # ordering, selected column, execution condition or effect of this site
    # changed, so the contract and its MAINT inert rule stand.
    # Reviewed re-pin (#1018 twin T1, base d94a83792), for open_db #3.
    # Changed: the binding `STATS_INDEX_EPOCH` 1023 -> 1024 (spec 5.1) and the
    # producer `open_db` itself, whose epoch-1024 schema block creates
    # `claude_weekly_observation_index` with the four enriched columns
    # (`weekly_percent`, `weekly_resets_at_utc`, `weekly_reading_genuine`,
    # `weekly_freshness_eligible`) and the partial index
    # `idx_claude_weekly_observation_index_freshness`; those are literal
    # statements, registered as their own MAINT contracts. No SQL, table
    # selection, predicate, ordering, selected column, execution condition or
    # effect of this site changed, so the contract and its MAINT inert rule
    # stand.
    # Reviewed re-pin (#1050 T2, base a858a87ab), for open_db #3. Changed:
    # only the producer `open_db` itself, which now calls the new helper
    # `_ensure_stats_content_revision` before both current-epoch `return
    # conn` paths and after the epoch stamp of the fresh, scratch and cutover
    # schema apply (spec 4.3). That helper installs the content-revision
    # kernel's tables, triggers and index; its statements are registered as
    # their own MAINT contracts under `_lib_stats_content_revision`. No SQL,
    # table selection, predicate, ordering, selected column, execution
    # condition or effect of this site changed, so the contract and its MAINT
    # inert rule stand.
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint='dynamic:_cctally_core:open_db#3',
        sql_shape='sha256:c3f6f15f3c9914f7',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:weekly_held_exclusion',
        sql_fingerprint='PRAGMA table_info(weekly_usage_snapshots)',
        table='weekly_usage_snapshots',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_forecast:_apply_midweek_reset_override',
        sql_fingerprint='SELECT effective_reset_at_utc FROM week_reset_events WHERE new_week_end_at = ? AND datetime(detected_at_utc) <= datetime(?) ORDER BY datetime(detected_at_utc) DESC LIMIT 1',
        table='week_reset_events',
        keys=('new_week_end_at',),
        predicate='new_week_end_at = ? AND datetime(detected_at_utc) <= datetime(?)',
        ordering='datetime(detected_at_utc) DESC',
        selected=('effective_reset_at_utc',),
        effects='read',
        classes=('period-keyed', 'range-keyed'),
        inert_rule=Q7,
    ),
    AccessContract(
        function='_cctally_forecast:_fetch_current_week_snapshots',
        sql_fingerprint='dynamic:_cctally_forecast:_fetch_current_week_snapshots#1',
        # Reviewed re-pin (#1018 twin T3, base 90dc7562c): the opt-in
        # `include_journal_id` binding appends `, journal_id` to the selected
        # columns (after the optional account, before the trailing row id).
        # Read-only; no predicate, ordering or account scope changed, and
        # every existing caller's selected columns are unchanged.
        sql_shape='sha256:9e126e751be4da6b',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=Q7,
    ),
    AccessContract(
        function='_cctally_forecast:_fetch_current_week_snapshots',
        sql_fingerprint='dynamic:_cctally_forecast:_fetch_current_week_snapshots#2',
        # Reviewed re-pin (#1018 twin T3, base 90dc7562c): the opt-in
        # `include_journal_id` binding appends `, journal_id` to the selected
        # columns (after the optional account, before the trailing row id).
        # Read-only; no predicate, ordering or account scope changed, and
        # every existing caller's selected columns are unchanged.
        sql_shape='sha256:144e218d4af90f27',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=Q7,
    ),
    AccessContract(
        function='_cctally_forecast:_fetch_current_week_snapshots',
        sql_fingerprint='dynamic:_cctally_forecast:_fetch_current_week_snapshots#3',
        # Reviewed re-pin (#1018 twin T3, base 90dc7562c): the opt-in
        # `include_journal_id` binding appends `, journal_id` to the selected
        # columns (after the optional account, before the trailing row id).
        # Read-only; no predicate, ordering or account scope changed, and
        # every existing caller's selected columns are unchanged.
        sql_shape='sha256:210baccc777fcd1e',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=Q7,
    ),
    AccessContract(
        function='_cctally_forecast:_fetch_current_week_snapshots',
        sql_fingerprint='dynamic:_cctally_forecast:_fetch_current_week_snapshots#4',
        # Reviewed re-pin (#1018 twin T3, base 90dc7562c): the opt-in
        # `include_journal_id` binding appends `, journal_id` to the selected
        # columns (after the optional account, before the trailing row id).
        # Read-only; no predicate, ordering or account scope changed, and
        # every existing caller's selected columns are unchanged.
        sql_shape='sha256:fe11d3e5ad616f9f',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=Q7,
    ),
    AccessContract(
        function='_cctally_journal:_apply_account_last_seen',
        sql_fingerprint='UPDATE accounts SET last_seen_utc = ? WHERE account_key = ? AND (last_seen_utc IS NULL OR ? > last_seen_utc)',
        table='accounts',
        keys=('account_key',),
        predicate='account_key = ? AND (last_seen_utc IS NULL OR ? > last_seen_utc)',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton', 'range-keyed'),
        inert_rule=ACCOUNTS,
    ),
    # Reviewed re-pin (#980 D3, base 5b021b820), for _apply_quota_records #1
    # and #2. Changed (`~ producer`): only the producer's bookkeeping around
    # its two dynamic statements. It now compares `cache.total_changes`
    # before and after the batch, collects each executed record's root,
    # raw reset and canonical anchor into in-memory spans, and, when the
    # batch changed rows, calls `_cctally_cache.add_codex_finalization_debt`
    # (a `cache_meta` read and write) with `model` from
    # `_codex_unresolved_quota_model_exists` (one indexed probe of
    # `quota_window_snapshots`), all on the same cache.db connection and
    # transaction. The natural-key read and the INSERT/UPSERT this pin
    # covers keep their table selection, predicate, ordering, selected
    # columns, parameter construction, execution conditions and effects, so
    # the contract and its inert rule stand.
    #
    # Reviewed re-pin (#982 #975 T1, base 3d3e759da), for #1 and #2.
    # Changed (`~ producer`): only the producer's choice of reset-anchor
    # resolver. It gains an optional `anchors=` argument; when given, it is
    # used in place of the per-call `_codex_anchor_resolver(cache)` (the
    # history provenance drain passes a resolver seeded from copies of its
    # committed-evidence base), and it is ignored when the cache lacks the
    # anchor column, exactly as the per-call resolver was. Every other caller
    # omits it and is unchanged. The resolver only computes the
    # `canonical_resets_at_utc` value the INSERT/UPSERT binds, as before; the
    # natural-key read and the INSERT/UPSERT keep their table selection,
    # predicate, ordering, selected columns, parameter construction,
    # execution conditions and effects, so the contract and its inert rule
    # stand.
    AccessContract(
        function='_cctally_journal:_apply_quota_records',
        sql_fingerprint='dynamic:_cctally_journal:_apply_quota_records#1',
        sql_shape='sha256:6163599100a812de',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=CODEX,
    ),
    AccessContract(
        function='_cctally_journal:_apply_quota_records',
        sql_fingerprint='dynamic:_cctally_journal:_apply_quota_records#2',
        sql_shape='sha256:c04f34a3783ade13',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=CODEX,
    ),
    # #1015 T7 contract review: the evidence backfill also fills the
    # nullable `fit_lineage` column (spec 5.6) with COALESCE, so a lineage
    # already folded is never erased. Same table, natural-key predicate and
    # write effect; the column is read only by the calibration lineage
    # readers, never by a detector decision.
    AccessContract(
        function='_cctally_journal:_backfill_meter_rate_change_evidence',
        sql_fingerprint='UPDATE meter_rate_change_events SET withholding_status = COALESCE(withholding_status, ?), detector_input_causes = COALESCE(detector_input_causes, ?), composition_provenance = COALESCE(composition_provenance, ?), baseline_withheld_days = COALESCE(baseline_withheld_days, ?), fit_lineage = COALESCE(fit_lineage, ?) WHERE provider = ? AND account_key = ? AND effective_from = ?',
        table='meter_rate_change_events',
        keys=('provider', 'account_key', 'effective_from'),
        predicate='provider = ? AND account_key = ? AND effective_from = ?',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=JOURNAL,
    ),
    AccessContract(
        function='_cctally_journal:_build_harvest_evt',
        sql_fingerprint='dynamic:_cctally_journal:_build_harvest_evt#1',
        sql_shape='sha256:be6d8235005bc42f',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=JOURNAL,
    ),
    AccessContract(
        function='_cctally_journal:_keep_five_hour_retirement',
        sql_fingerprint='SELECT five_hour_percent, five_hour_resets_at, five_hour_window_key, weekly_observation_held FROM weekly_usage_snapshots WHERE id = ?',
        table='weekly_usage_snapshots',
        keys=('id',),
        predicate='id = ?',
        ordering='',
        selected=('five_hour_percent', 'five_hour_resets_at', 'five_hour_window_key', 'weekly_observation_held'),
        effects='read',
        classes=('period-keyed',),
        inert_rule=JOURNAL,
    ),
    AccessContract(
        function='_cctally_journal:_keep_weekly_retirement',
        sql_fingerprint='SELECT weekly_observation_held, five_hour_percent, five_hour_resets_at FROM weekly_usage_snapshots WHERE id = ?',
        table='weekly_usage_snapshots',
        keys=('id',),
        predicate='id = ?',
        ordering='',
        selected=('weekly_observation_held', 'five_hour_percent', 'five_hour_resets_at'),
        effects='read',
        classes=('period-keyed',),
        inert_rule=JOURNAL,
    ),
    # #840 (spec section 4.8): the convergence keepers keep a retirement only
    # through exact effective suppression membership. The candidates are the
    # active effective decisions whose retained record names the snapshot id;
    # membership is decided on the decoded payload's exact `suppression` list,
    # so a row is matched by exact logical identity, never by a percentage band.
    AccessContract(
        function='_cctally_journal:_suppression_retires',
        sql_fingerprint="SELECT event_id, event_json FROM journal_effective_events WHERE status = 'active' AND event_json LIKE ?",
        table='journal_effective_events',
        keys=(),
        predicate="status = 'active' AND event_json LIKE ?",
        ordering='',
        selected=('event_id', 'event_json'),
        effects='read',
        classes=('cross-period',),
        inert_rule=JOURNAL,
    ),
    AccessContract(
        function='_cctally_journal:_converge_row_from_effective',
        sql_fingerprint='SELECT account_key, five_hour_window_key FROM five_hour_blocks WHERE id = ?',
        table='five_hour_blocks',
        keys=('id',),
        predicate='id = ?',
        ordering='',
        selected=('account_key', 'five_hour_window_key'),
        effects='read',
        classes=('period-keyed',),
        inert_rule=JOURNAL,
    ),
    # Reviewed re-pin (#797/#823/#840 Task 6 Part B, base 79847ac5a). Changed:
    # the producer `_converge_row_from_effective` takes a `suppressors`
    # keyword and passes it, with the converged event id, to
    # `_keep_five_hour_retirement` and `_keep_weekly_retirement`, which now
    # keep a retirement only through exact effective suppression membership
    # (`_suppression_retires`, its own contract above) instead of the removed
    # credit-band and five-hour-credit reads (their three contracts are
    # removed). Both sites' target-table `SELECT id ... WHERE journal_id = ?`
    # and `UPDATE ... WHERE id = ?`, their keys, predicates, execution
    # conditions and effects are unchanged, so the contracts and their inert
    # rules stand.
    # Reviewed re-pin (#1019/#1014 Task A, base 5b15db929), for both sites.
    # Changed: `_lib_account_protocol.strip_stamp` now also strips `wpx`
    # (new bindings SUCCESSOR_REVISION_KEY and _STAMP_KEYS), the semantic
    # comparison of a converged event, and the `_init_paths_from_env` path
    # assignments (see open_db above). The target-table SELECT/UPDATE, keys,
    # predicates, execution conditions and effects are unchanged, so the
    # contracts and inert rules stand.
    # Reviewed re-pin (#956 T5, base af10118c7), for
    # _converge_row_from_effective #1. Changed: the producer, which now ends
    # with the opening fact's coupled convergence (`_converge_opened_block`, its
    # statements registered as their own contracts) when the target is
    # `five_hour_block_openings`; and the `_EVT_SPECS` binding, whose
    # `five_hour_block_open` spec carries the applier
    # `_apply_five_hour_block_open`, so the closure now follows it into
    # `_apply_generic_evt` (and from there `_insert_or_ignore`,
    # `_resolve_fold_ref`, `_resolve_ref`, `_derived_fk_value`,
    # `_report_unresolved_fold_reference`, `_SUPPRESSIBLE_FOLD_REFS`,
    # `JournalError`, `UnresolvedFoldReference`), `_block_opening_row` and
    # `_materialize_opened_block`. This site still reads `spec.table` from the
    # spec, never its applier, and the dynamic statement it executes is
    # unchanged. No SQL, table selection, predicate, ordering, selected column,
    # execution condition or effect of this site changed, so the contract and
    # its inert rule stand.
    # Reviewed re-pin (#1015 T7, base 7455ca434), for _converge_row_from_effective #1. Changed: only the
    # fold machinery this site reaches through bindings: `_EVT_SPECS` gained
    # the table-less `meter_rate_change_supersession` spec (order 46), whose
    # applier `_apply_meter_rate_change_supersession` calls
    # `_insert_meter_rate_change_supersession` (a literal INSERT OR IGNORE,
    # registered as its own contract) and reads `MRC_SUPERSESSION_REASON`
    # and `_canonical_json_or_none`; `_MeterRateChangeRow` gained the
    # nullable `fit_lineage` field, which `_meter_rate_change_row` fills
    # through `_canonical_json_or_none` and
    # `_backfill_meter_rate_change_evidence` COALESCEs (its literal UPDATE is
    # reviewed beside its own contract). A table-less spec returns
    # `CONVERGE_DROPPED` before this site's statement, which still names the
    # spec's table. No SQL, table selection, predicate, ordering, selected
    # column, execution condition or effect of this site changed, so the
    # contract and its inert rule stand.
    AccessContract(
        function='_cctally_journal:_converge_row_from_effective',
        sql_fingerprint='dynamic:_cctally_journal:_converge_row_from_effective#1',
        sql_shape='sha256:b92199c9a76d4a20',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=JOURNAL,
    ),
    # Reviewed re-pin (#956 T5, base af10118c7), for
    # _converge_row_from_effective #2. Changed: the producer, which now ends
    # with the opening fact's coupled convergence (`_converge_opened_block`, its
    # statements registered as their own contracts) when the target is
    # `five_hour_block_openings`; and the `_EVT_SPECS` binding, whose
    # `five_hour_block_open` spec carries the applier
    # `_apply_five_hour_block_open`, so the closure now follows it into
    # `_apply_generic_evt` (and from there `_insert_or_ignore`,
    # `_resolve_fold_ref`, `_resolve_ref`, `_derived_fk_value`,
    # `_report_unresolved_fold_reference`, `_SUPPRESSIBLE_FOLD_REFS`,
    # `JournalError`, `UnresolvedFoldReference`), `_block_opening_row` and
    # `_materialize_opened_block`. This site still reads `spec.table` from the
    # spec, never its applier, and the dynamic statement it executes is
    # unchanged. No SQL, table selection, predicate, ordering, selected column,
    # execution condition or effect of this site changed, so the contract and
    # its inert rule stand.
    # Reviewed re-pin (#1015 T7, base 7455ca434), for _converge_row_from_effective #2. Changed: only the
    # fold machinery this site reaches through bindings: `_EVT_SPECS` gained
    # the table-less `meter_rate_change_supersession` spec (order 46), whose
    # applier `_apply_meter_rate_change_supersession` calls
    # `_insert_meter_rate_change_supersession` (a literal INSERT OR IGNORE,
    # registered as its own contract) and reads `MRC_SUPERSESSION_REASON`
    # and `_canonical_json_or_none`; `_MeterRateChangeRow` gained the
    # nullable `fit_lineage` field, which `_meter_rate_change_row` fills
    # through `_canonical_json_or_none` and
    # `_backfill_meter_rate_change_evidence` COALESCEs (its literal UPDATE is
    # reviewed beside its own contract). A table-less spec returns
    # `CONVERGE_DROPPED` before this site's statement, which still names the
    # spec's table. No SQL, table selection, predicate, ordering, selected
    # column, execution condition or effect of this site changed, so the
    # contract and its inert rule stand.
    AccessContract(
        function='_cctally_journal:_converge_row_from_effective',
        sql_fingerprint='dynamic:_cctally_journal:_converge_row_from_effective#2',
        sql_shape='sha256:a43354e85f72febd',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=JOURNAL,
    ),
    AccessContract(
        function='_cctally_journal:_correction_index_converged',
        sql_fingerprint='SELECT rev, status, content_hash, batch_id FROM journal_effective_events WHERE event_id = ?',
        table='journal_effective_events',
        keys=('event_id',),
        predicate='event_id = ?',
        ordering='',
        selected=('rev', 'status', 'content_hash', 'batch_id'),
        effects='read',
        classes=('period-keyed',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_journal:_derived_fk_value',
        sql_fingerprint='dynamic:_cctally_journal:_derived_fk_value#1',
        sql_shape='sha256:5ddb375652a6e6e8',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=JOURNAL,
    ),
    AccessContract(
        function='_cctally_journal:_derived_fk_value',
        sql_fingerprint='dynamic:_cctally_journal:_derived_fk_value#2',
        sql_shape='sha256:876b10f37733a663',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=JOURNAL,
    ),
    AccessContract(
        function='_cctally_journal:_effective_event_for_convergence',
        sql_fingerprint='SELECT rev, status, content_hash, event_json FROM journal_effective_events WHERE event_id = ?',
        table='journal_effective_events',
        keys=('event_id',),
        predicate='event_id = ?',
        ordering='',
        selected=('rev', 'status', 'content_hash', 'event_json'),
        effects='read',
        classes=('period-keyed',),
        inert_rule=JOURNAL,
    ),
    # Reviewed re-pin (#797/#823/#840 Task 6 Part B, base 79847ac5a). Changed:
    # the producer `_emit_harvest_row`'s derivation-completion branch passes
    # its retention's applied decisions (`applied_records`) to
    # `_converge_row_from_effective` as `suppressors` (#840 exact suppression
    # membership). The harvested table, keys, predicates, ordering, execution
    # conditions and effects are unchanged, so the contracts and their inert
    # rules stand.
    AccessContract(
        function='_cctally_journal:_emit_harvest_row',
        sql_fingerprint='dynamic:_cctally_journal:_emit_harvest_row#1',
        sql_shape='sha256:aca33abe5dcc2e43',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=JOURNAL,
    ),
    AccessContract(
        function='_cctally_journal:_emit_harvest_row',
        sql_fingerprint='dynamic:_cctally_journal:_emit_harvest_row#2',
        sql_shape='sha256:aca33abe5dcc2e43',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=JOURNAL,
    ),
    # Reviewed re-pin (#956 T5, base af10118c7), for _delete_scratch_row #1.
    # Changed: only the producer, which now first calls `_remove_opened_block`
    # (its statements registered as their own contracts) when the table is
    # `five_hour_block_openings`, so a tombstoned opening takes the open block
    # it materialized with it (spec 4.6.3). The `DELETE FROM {table} WHERE id =
    # ?` this pin covers is unchanged. No SQL, table selection, predicate,
    # ordering, selected column, execution condition or effect of this site
    # changed, so the contract and its inert rule stand.
    AccessContract(
        # #825/#832 fix round E: a derivation-completion scratch replay
        # removes the one row a retained tombstone says does not exist, on
        # the replay's PRIVATE projection, by its physical id; the table is
        # a journal family's own (`_HARVEST_SPECS`/`_EVT_SPECS`).
        function='_cctally_journal:_delete_scratch_row',
        sql_fingerprint='dynamic:_cctally_journal:_delete_scratch_row#1',
        sql_shape='sha256:24df60c360ccd098',
        table='dynamic',
        keys=('id',),
        predicate='id = ? on a scratch projection (a journal family table)',
        ordering='',
        selected=(),
        effects='delete',
        classes=('singleton',),
        inert_rule=JOURNAL,
    ),
    AccessContract(
        function='_cctally_journal:_full_taint_transition',
        sql_fingerprint='SELECT DISTINCT batch_id FROM journal_effective_events WHERE batch_id IS NOT NULL',
        table='journal_effective_events',
        keys=(),
        predicate='batch_id IS NOT NULL',
        ordering='',
        selected=('DISTINCT batch_id',),
        effects='read',
        classes=('cross-period',),
        inert_rule=MAINT,
    ),
    # Reviewed re-pin (#943 T3, base c5419d209), for _harvest #1. Changed:
    # only bindings this site reads — `_HARVEST_SPECS`, which gains the
    # `five_hour_credit_boundary` spec (table `five_hour_credit_boundaries`,
    # prefix `fhcb`, order 29). The site iterates the specs generically, so
    # the new family is harvested by the same statement shapes as every other
    # harvest family. No SQL, table selection, predicate, ordering, selected
    # column, execution condition or effect of this site changed, so the
    # contract and its inert rule stand.
    # Reviewed re-pin (#956 T5, base af10118c7), for _harvest #1. Changed: only
    # the `_HARVEST_SPECS` binding, which gains the `five_hour_block_open` spec
    # (table `five_hour_block_openings`, prefix `fhbo`, order 35). The site
    # iterates the specs generically, so the new family is harvested by the same
    # statement shapes as every other harvest family. No SQL, table selection,
    # predicate, ordering, selected column, execution condition or effect of
    # this site changed, so the contract and its inert rule stand.
    AccessContract(
        function='_cctally_journal:_harvest',
        sql_fingerprint='dynamic:_cctally_journal:_harvest#1',
        sql_shape='sha256:eeb08bab185c5989',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=JOURNAL,
    ),
    AccessContract(
        function='_cctally_journal:_insert_effective_metadata',
        sql_fingerprint='INSERT INTO journal_effective_events (event_id, rev, status, content_hash, batch_id, event_json) VALUES (?, ?, ?, ?, ?, ?)',
        table='journal_effective_events',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=JOURNAL,
    ),
    # Reviewed re-pin (#1015 T7, base 7455ca434), for _insert_meter_rate_change #1. Changed: only the
    # binding `_MeterRateChangeRow`, which gained the nullable `fit_lineage`
    # field (spec 5.6), so the row-driven INSERT OR IGNORE column list also
    # writes `fit_lineage`. Same table, natural-key conflict target, parameter
    # construction and execution condition; the added column is read only
    # by the calibration lineage readers, never by a detector decision, so
    # the contract and its inert rule stand.
    AccessContract(
        function='_cctally_journal:_insert_meter_rate_change',
        sql_fingerprint='dynamic:_cctally_journal:_insert_meter_rate_change#1',
        sql_shape='sha256:2c2d27ca4dddd598',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=JOURNAL,
    ),
    # #1015 T7: the `meter_rate_change_supersession` evt applier folds one
    # explicit supersession by its natural key with INSERT OR IGNORE, so a
    # replay or a duplicate repeats to the same row.
    AccessContract(
        function='_cctally_journal:_insert_meter_rate_change_supersession',
        sql_fingerprint='INSERT OR IGNORE INTO meter_rate_change_supersessions (provider, account_key, effective_from, reason, corrections, superseded_at_utc, created_at_utc) VALUES (?,?,?,?,?,?,?)',
        table='meter_rate_change_supersessions',
        keys=('provider', 'account_key', 'effective_from'),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=LINEAGE,
    ),
    AccessContract(
        function='_cctally_journal:_insert_or_ignore',
        sql_fingerprint='dynamic:_cctally_journal:_insert_or_ignore#1',
        sql_shape='sha256:ddf39d7c6c26c825',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=JOURNAL,
    ),
    AccessContract(
        function='_cctally_journal:_latest_weekly_basis',
        sql_fingerprint='dynamic:_cctally_journal:_latest_weekly_basis#1',
        sql_shape='sha256:de026ae7c7c1f17e',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=BASIS,
    ),
    AccessContract(
        function='_cctally_journal:_metadata_event_record',
        sql_fingerprint='SELECT event_json FROM journal_effective_events WHERE event_id = ?',
        table='journal_effective_events',
        keys=('event_id',),
        predicate='event_id = ?',
        ordering='',
        selected=('event_json',),
        effects='read',
        classes=('period-keyed',),
        inert_rule=JOURNAL,
    ),
    AccessContract(
        function='_cctally_journal:_metadata_row',
        sql_fingerprint='SELECT rev, status, content_hash, batch_id FROM journal_effective_events WHERE event_id = ?',
        table='journal_effective_events',
        keys=('event_id',),
        predicate='event_id = ?',
        ordering='',
        selected=('rev', 'status', 'content_hash', 'batch_id'),
        effects='read',
        classes=('period-keyed',),
        inert_rule=JOURNAL,
    ),
    AccessContract(
        function='_cctally_journal:_prior_meter_rate_change_event',
        sql_fingerprint='SELECT status FROM journal_effective_events WHERE event_id = ?',
        table='journal_effective_events',
        keys=('event_id',),
        predicate='event_id = ?',
        ordering='',
        selected=('status',),
        effects='read',
        classes=('period-keyed',),
        inert_rule=JOURNAL,
    ),
    AccessContract(
        function='_cctally_journal:_publish_generation_in_place',
        sql_fingerprint='DELETE FROM main.stats_publication_stamp',
        table='stats_publication_stamp',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='delete',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_journal:_publish_generation_in_place',
        sql_fingerprint='INSERT INTO main.stats_publication_stamp (record_path, started_at_utc, stamped_at_utc) VALUES (?, ?, ?)',
        table='stats_publication_stamp',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_journal:_publish_generation_in_place',
        sql_fingerprint="SELECT 1 FROM main.sqlite_schema WHERE type='table' AND name='journal_selector_state'",
        table='journal_selector_state',
        keys=(),
        predicate="type='table' AND name='journal_selector_state'",
        ordering='',
        selected=('1',),
        effects='read',
        classes=('cross-period',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_journal:_publish_generation_in_place',
        sql_fingerprint='UPDATE main.journal_selector_state SET generation_record_path = ?, generation_stamped_at_utc = ?',
        table='journal_selector_state',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    # #949: the replace mechanism's twin of the four sites above. It stamps
    # the closed scratch on the scratch's own connection before `os.replace`,
    # under the regime of the publishing rebuild.
    AccessContract(
        function='_cctally_journal:_stamp_replace_scratch',
        sql_fingerprint='DELETE FROM stats_publication_stamp',
        table='stats_publication_stamp',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='delete',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_journal:_stamp_replace_scratch',
        sql_fingerprint='INSERT INTO stats_publication_stamp (record_path, started_at_utc, stamped_at_utc) VALUES (?, ?, ?)',
        table='stats_publication_stamp',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_journal:_stamp_replace_scratch',
        sql_fingerprint="SELECT 1 FROM sqlite_schema WHERE type='table' AND name='journal_selector_state'",
        table='journal_selector_state',
        keys=(),
        predicate="type='table' AND name='journal_selector_state'",
        ordering='',
        selected=('1',),
        effects='read',
        classes=('cross-period',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_journal:_stamp_replace_scratch',
        sql_fingerprint='UPDATE journal_selector_state SET generation_record_path = ?, generation_stamped_at_utc = ?',
        table='journal_selector_state',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    # Reviewed re-pin (#1019/#1014 Task A, base 5b15db929), for all five
    # `_publish_generation_in_place` sites. Changed: only the
    # `STATS_INDEX_EPOCH` binding (1019 -> 1020) the publication stamps; the
    # statements, tables, predicates and effects are unchanged.
    # Reviewed re-pin (#943 T3, base c5419d209), for
    # _publish_generation_in_place #1. Changed: only bindings this site reads
    # — `STATS_INDEX_EPOCH` 1020 -> 1021 (spec 4.7). No SQL, table selection,
    # predicate, ordering, selected column, execution condition or effect of
    # this site changed, so the contract and its inert rule stand.
    # Reviewed re-pin (#1015 T3, base 16b055cae), for _publish_generation_in_place #1. Changed: only
    # the binding `STATS_INDEX_EPOCH` 1021 -> 1022 (spec 5.1). No SQL, table
    # selection, predicate, ordering, selected column, execution condition or
    # effect of this site changed, so the contract and its inert rule stand.
    # Reviewed re-pin (#948 T-A, base 7e2ea2411), for
    # _publish_generation_in_place #1-#5, _rebuild_stats_index_locked #1 and
    # stats_index_matches_journal_prefix #1. Changed: only the binding
    # `STATS_INDEX_EPOCH` 1022 -> 1023 (and, for the rebuild site, its helper
    # `open_db`, whose schema block changed as described at open_db #1). No
    # SQL, table selection, predicate, ordering, selected column, execution
    # condition or effect of these sites changed, so each contract and its
    # inert rule stand.
    # Reviewed re-pin (#1018 twin T1, base d94a83792), for _publish_generation_in_place #1. Changed:
    # only the binding `STATS_INDEX_EPOCH` 1023 -> 1024 (spec 5.1). No SQL,
    # table selection, predicate, ordering, selected column, execution
    # condition or effect of this site changed, so the contract and its
    # inert rule stand.
    AccessContract(
        function='_cctally_journal:_publish_generation_in_place',
        sql_fingerprint='dynamic:_cctally_journal:_publish_generation_in_place#1',
        sql_shape='sha256:dc539be3e0b79518',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=MAINT,
    ),
    # Reviewed re-pin (#943 T3, base c5419d209), for
    # _publish_generation_in_place #2/#3/#5. Changed: only bindings this site
    # reads — `STATS_INDEX_EPOCH` 1020 -> 1021 (spec 4.7). No SQL, table
    # selection, predicate, ordering, selected column, execution condition or
    # effect of this site changed, so the contract and its inert rule stand.
    # Reviewed re-pin (#1015 T3, base 16b055cae), for _publish_generation_in_place #2. Changed: only
    # the binding `STATS_INDEX_EPOCH` 1021 -> 1022 (spec 5.1). No SQL, table
    # selection, predicate, ordering, selected column, execution condition or
    # effect of this site changed, so the contract and its inert rule stand.
    # Reviewed re-pin (#1018 twin T1, base d94a83792), for _publish_generation_in_place #2. Changed:
    # only the binding `STATS_INDEX_EPOCH` 1023 -> 1024 (spec 5.1). No SQL,
    # table selection, predicate, ordering, selected column, execution
    # condition or effect of this site changed, so the contract and its
    # inert rule stand.
    AccessContract(
        function='_cctally_journal:_publish_generation_in_place',
        sql_fingerprint='dynamic:_cctally_journal:_publish_generation_in_place#2',
        sql_shape='sha256:a997bd74615a0821',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=MAINT,
    ),
    # Reviewed re-pin (#943 T3, base c5419d209), for
    # _publish_generation_in_place #2/#3/#5. Changed: only bindings this site
    # reads — `STATS_INDEX_EPOCH` 1020 -> 1021 (spec 4.7). No SQL, table
    # selection, predicate, ordering, selected column, execution condition or
    # effect of this site changed, so the contract and its inert rule stand.
    # Reviewed re-pin (#1015 T3, base 16b055cae), for _publish_generation_in_place #3. Changed: only
    # the binding `STATS_INDEX_EPOCH` 1021 -> 1022 (spec 5.1). No SQL, table
    # selection, predicate, ordering, selected column, execution condition or
    # effect of this site changed, so the contract and its inert rule stand.
    # Reviewed re-pin (#1018 twin T1, base d94a83792), for _publish_generation_in_place #3. Changed:
    # only the binding `STATS_INDEX_EPOCH` 1023 -> 1024 (spec 5.1). No SQL,
    # table selection, predicate, ordering, selected column, execution
    # condition or effect of this site changed, so the contract and its
    # inert rule stand.
    AccessContract(
        function='_cctally_journal:_publish_generation_in_place',
        sql_fingerprint='dynamic:_cctally_journal:_publish_generation_in_place#3',
        sql_shape='sha256:a997bd74615a0821',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=MAINT,
    ),
    # Reviewed re-pin (#943 T3, base c5419d209), for
    # _publish_generation_in_place #4. Changed: only bindings this site reads
    # — `STATS_INDEX_EPOCH` 1020 -> 1021 (spec 4.7). No SQL, table selection,
    # predicate, ordering, selected column, execution condition or effect of
    # this site changed, so the contract and its inert rule stand.
    # Reviewed re-pin (#1015 T3, base 16b055cae), for _publish_generation_in_place #4. Changed: only
    # the binding `STATS_INDEX_EPOCH` 1021 -> 1022 (spec 5.1). No SQL, table
    # selection, predicate, ordering, selected column, execution condition or
    # effect of this site changed, so the contract and its inert rule stand.
    # Reviewed re-pin (#1018 twin T1, base d94a83792), for _publish_generation_in_place #4. Changed:
    # only the binding `STATS_INDEX_EPOCH` 1023 -> 1024 (spec 5.1). No SQL,
    # table selection, predicate, ordering, selected column, execution
    # condition or effect of this site changed, so the contract and its
    # inert rule stand.
    AccessContract(
        function='_cctally_journal:_publish_generation_in_place',
        sql_fingerprint='dynamic:_cctally_journal:_publish_generation_in_place#4',
        sql_shape='sha256:e2a4af53267f96f7',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=MAINT,
    ),
    # Reviewed re-pin (#943 T3, base c5419d209), for
    # _publish_generation_in_place #2/#3/#5. Changed: only bindings this site
    # reads — `STATS_INDEX_EPOCH` 1020 -> 1021 (spec 4.7). No SQL, table
    # selection, predicate, ordering, selected column, execution condition or
    # effect of this site changed, so the contract and its inert rule stand.
    # Reviewed re-pin (#1015 T3, base 16b055cae), for _publish_generation_in_place #5. Changed: only
    # the binding `STATS_INDEX_EPOCH` 1021 -> 1022 (spec 5.1). No SQL, table
    # selection, predicate, ordering, selected column, execution condition or
    # effect of this site changed, so the contract and its inert rule stand.
    # Reviewed re-pin (#1018 twin T1, base d94a83792), for _publish_generation_in_place #5. Changed:
    # only the binding `STATS_INDEX_EPOCH` 1023 -> 1024 (spec 5.1). No SQL,
    # table selection, predicate, ordering, selected column, execution
    # condition or effect of this site changed, so the contract and its
    # inert rule stand.
    AccessContract(
        function='_cctally_journal:_publish_generation_in_place',
        sql_fingerprint='dynamic:_cctally_journal:_publish_generation_in_place#5',
        sql_shape='sha256:a997bd74615a0821',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_journal:_read_admission_summary',
        sql_fingerprint='SELECT cutover_id, scanned_high_water, excluded_total, family_counts_json, first_excluded_coordinate, latest_excluded_coordinate, coordinate_digest, complete_through, boundary_json FROM journal_admission_summary WHERE id = 1',
        table='journal_admission_summary',
        keys=(),
        predicate='id = 1',
        ordering='',
        selected=('cutover_id', 'scanned_high_water', 'excluded_total', 'family_counts_json', 'first_excluded_coordinate', 'latest_excluded_coordinate', 'coordinate_digest', 'complete_through', 'boundary_json'),
        effects='read',
        classes=('cross-period',),
        inert_rule=JOURNAL,
    ),
    AccessContract(
        function='_cctally_journal:_read_cursor',
        sql_fingerprint='SELECT segment, offset, applied_segment, applied_offset FROM journal_cursor WHERE id = 1',
        table='journal_cursor',
        keys=(),
        predicate='id = 1',
        ordering='',
        selected=('segment', 'offset', 'applied_segment', 'applied_offset'),
        effects='read',
        classes=('cross-period',),
        inert_rule=JOURNAL,
    ),
    AccessContract(
        function='_cctally_journal:_read_selector_state',
        sql_fingerprint='SELECT generation_record_path, generation_stamped_at_utc, covered_segment, covered_offset, next_sequence, selector_version, cutover_seen, cutover_account_key FROM journal_selector_state',
        table='journal_selector_state',
        keys=(),
        predicate='',
        ordering='',
        selected=('generation_record_path', 'generation_stamped_at_utc', 'covered_segment', 'covered_offset', 'next_sequence', 'selector_version', 'cutover_seen', 'cutover_account_key'),
        effects='read',
        classes=('cross-period',),
        inert_rule=JOURNAL,
    ),
    # Reviewed re-pin (#825/#832 fix round J4, #954): the closure's imported
    # producer `_backfill_five_hour_blocks` now takes the open block's boundary
    # row from a shared helper (the same MIN-captured SELECT, now also used by
    # the ownership pre-pass, which selects two more columns of the same row)
    # overridden by the producer's journaled first observation (pure Python,
    # `journaled_block_first_observations`), and its INSERT also writes
    # `pricing_provenance_json` through the new pure dependency
    # `_block_pricing_provenance_json` (the pricing snapshot date the closure
    # already reaches). The producer only passes that mapping. Table, keys,
    # predicates, ordering and effects of this maintenance rebuild site are
    # unchanged, so the contract and its MAINT inert rule stand.
    # Reviewed re-pin (#825/#832 fix round J5, 825-832-CD4-V01): the boundary
    # row helper now returns the producer's first-writer mapping whenever it
    # holds the window, and runs the same MIN-captured SELECT (text, table,
    # predicate and ordering unchanged) only as the fallback; the producer now
    # passes the decoded prefix and its effective `by_id` to the pure helper,
    # which orders decisions by first appearance. No SQL was added or changed,
    # so the contract and its MAINT inert rule stand.
    # Reviewed re-pin (#997 T-A5/T-A6, base b193e984e). Added: the
    # `_lib_rebuild_progress` bindings (`_sink`, `_phase`, `_counters`,
    # `_last_write`, `THROTTLE_SECONDS`) and its `phase`, `tick` and `_emit`,
    # which the producer now calls to report progress to a detached worker's
    # state file; they hold process-local progress state, execute no SQL and
    # are no-ops unless a worker installed a sink. Changed: the producer (the
    # phase and tick calls, the Codex quota observations retained as
    # `QuotaLineSource` coordinates for the cache leg, and the record's
    # `peakResidentBytes` and `quotaLockMaxHoldSeconds`) and
    # `_backfill_five_hour_blocks` (a throttled tick every 1,024 blocks in its
    # insert loop). This site's `SELECT COUNT(*) FROM {tbl}` over
    # `_REBUILD_COUNT_TABLES`, its execution condition and its read-only
    # effect are unchanged, so the contract and its MAINT inert rule stand.
    # Reviewed re-pin (#997 T-C1/T-C3 and the revision-10 fix round, base
    # 9282d8b73). Changed: the producer builds its prefix hasher through
    # `_new_prefix_hasher` (the streamed accumulator with its segment opener),
    # compacts retained records with `_lib_journal.compact_retained` right
    # after decode, computes the open-block projection's first observations
    # and the late `effective` fields before releasing the read-pass state,
    # and reads the quota bundle after that release. None of these executes
    # SQL; this site's `SELECT COUNT(*) FROM {tbl}` over
    # `_REBUILD_COUNT_TABLES`, its execution condition and its read-only
    # effect are unchanged, so the contract and its MAINT inert rule stand.
    # Reviewed re-pin (#997 D19, base 0bfc75475). Changed: the closure's
    # imported producer `_backfill_five_hour_blocks`, whose `only_missing`
    # key query now takes each account's latest qualifying snapshot from one
    # `ROW_NUMBER()` ranking pass instead of a correlated subquery per row.
    # The selected keys are identical (T19 holds the old query as its oracle);
    # the producer's own execute sites are not registered accesses, and this
    # site's `SELECT COUNT(*) FROM {tbl}` over `_REBUILD_COUNT_TABLES`, its
    # execution condition and its read-only effect are unchanged, so the
    # contract and its MAINT inert rule stand.
    # Reviewed re-pin (#797/#823/#840 Task 2, base 5f3db4ddd). Added: the
    # fold applier `_cctally_journal._apply_op_weekly_observation_decision`
    # and the function it calls,
    # `_cctally_oauth_samples.apply_weekly_observation_decision_op`, with
    # its bindings `_DECISION_LISTS`, `_DISPOSITIONS` and `_UPSERT_DECISION`
    # (the reviewed weekly decision projection, upserting
    # `claude_weekly_observation_decisions` by observation id). Changed:
    # `FOLD_APPLIERS` (the two reviewed decision kinds now map to that
    # applier), the helper `open_db` (the epoch-1019 schema statements, see
    # the open_db re-pin) and the producer (its read pass offers each
    # admitted record to `_cctally_oauth_samples.RebuildProjector`, which
    # projects provider samples in bounded batches). This site's
    # `SELECT COUNT(*) FROM {tbl}` over `_REBUILD_COUNT_TABLES` (left
    # unchanged, so `db rebuild --json` keeps its counts), its execution
    # condition and its read-only effect are unchanged, so the contract and
    # its MAINT inert rule stand.
    # Reviewed re-pin (#797/#823/#840 Task 6, base 31bb25621). Changed: the
    # binding `_cctally_oauth_samples._DECISION_LISTS`, which now maps the
    # reviewed decision payload schema 3 (the `retract` disposition) to its
    # `weekly_axis_decisions` list, the hold projection schema 2 already
    # used, so a retraction folds into `claude_weekly_observation_decisions`
    # as the `hold` it projects. The applier's upsert, its table and its key
    # are unchanged; this site's `SELECT COUNT(*) FROM {tbl}` over
    # `_REBUILD_COUNT_TABLES`, its execution condition and its read-only
    # effect are unchanged, so the contract and its MAINT inert rule stand.
    # Reviewed re-pin (#1019/#1014 Task A, base 5b15db929). Changed:
    # `STATS_INDEX_EPOCH` 1019 -> 1020, `_lib_journal.PROTOCOL_RESOLUTION_KINDS`
    # gaining `journal_protocol_resolution_v4` (new PROTOCOL_RESOLUTION_V4_KIND
    # bindings in both modules: the v4 repair acknowledgement, spec 4.7 C5)
    # and the `_init_paths_from_env` path assignments (see open_db). The
    # fold's SQL, tables, predicates and effects are unchanged, so the
    # contract and its inert rule stand.
    # Reviewed re-pin (#943 T3, base c5419d209), for
    # _rebuild_stats_index_locked #1. Changed: only bindings this site reads —
    # `STATS_INDEX_EPOCH` 1020 -> 1021 (spec 4.7) and the producer `open_db`
    # itself, whose epoch-1021 schema block now also creates
    # `five_hour_credit_boundaries` with its two journal-id indexes and
    # creates `five_hour_credit_confirmation_state` with the NULLABLE
    # `pending_captured_at_utc` column; those are literal statements,
    # registered as their own MAINT contracts. No SQL, table selection,
    # predicate, ordering, selected column, execution condition or effect of
    # this site changed, so the contract and its inert rule stand.
    # Reviewed re-pin (#956 T4, base 6528383a7), for
    # _rebuild_stats_index_locked #1. Changed: only functions in the closure
    # it reaches through Phase 2a's `_backfill_five_hour_blocks` —
    # `_backfill_five_hour_blocks` (its open-candidate path now materializes
    # start columns and refreshes every open block, its candidate ranking
    # breaks ties by `journal_id`) and the new `OpenBlockEnd`,
    # `select_open_block_end`, `project_open_block_end`,
    # `_materialize_open_candidate` and `_refresh_every_open_block`, whose
    # literal statements write the rebuild's private scratch index. None of
    # them is the dynamic statement this pin covers; its table selection,
    # predicate, ordering, selected columns, execution condition and effect
    # are unchanged, so the contract and its inert rule stand.
    # Reviewed re-pin (#956 T5, base af10118c7), for _rebuild_stats_index_locked
    # #1. Changed: the producer, whose Phase 2a now computes
    # `retired_block_openings` beside `journaled_block_first_observations` and
    # passes it to the backfill; `_backfill_five_hour_blocks`, which drops a
    # tombstoned opening's window from the legacy open candidates;
    # `select_open_block_end` and the `OpenBlockEnd` docstring, which project an
    # opening's reading when the window has no readings (origin `opening`; the
    # new seed read is registered as its own BLOCKS contract); and the helper
    # `open_db` (see open_db above). None of them is the dynamic statement this
    # pin covers. No SQL, table selection, predicate, ordering, selected column,
    # execution condition or effect of this site changed, so the contract and
    # its inert rule stand.
    # Reviewed re-pin (#943 F-I1-1, base 63d4363dc), for
    # _rebuild_stats_index_locked #1. Changed (`~ computed-by`): only
    # `_apply_op_weekly_credit_floor`, reached through `FOLD_APPLIERS`, which
    # for a `forced` op now also UPDATEs the `weekly_credit_floors` row at the
    # same `(account_key, week_start_date, effective_at_utc)` slot to the
    # forced op's journal id, observed level and applied time (a same-second
    # `record-credit --force` takes the slot over instead of being IGNOREd).
    # The applier already wrote that table; the new statement is a literal
    # write the writer-surface freeze counts, not the dynamic statement this
    # pin covers. No SQL, table selection, predicate, ordering, selected
    # column, execution condition or effect of this site changed, so the
    # contract and its inert rule stand.
    # Reviewed re-pin (#980 D5, base 5b021b820), for
    # _rebuild_stats_index_locked #1. Changed (`~ computed-by`
    # `_maybe_truncate_wal`, `+ computed-by` `_cache_wal_truncate_due` and
    # `_cache_wal_trigger_bytes`): the cache.db WAL size gate moved out of
    # `_maybe_truncate_wal` into those two helpers so the budgeted Codex
    # sync can record the TRUNCATE as owed maintenance. The gate is the same
    # strict `-wal` size > `CACHE_WAL_CHECKPOINT_TRIGGER_BYTES` comparison
    # with the same test override, and the checkpoint is a literal cache.db
    # statement, not the dynamic statement this pin covers. No SQL, table
    # selection, predicate, ordering, selected column, execution condition
    # or effect of this site changed, so the contract and its inert rule
    # stand.
    # Reviewed re-pin (#1015 T3, base 16b055cae), for
    # _rebuild_stats_index_locked #1. Added: the bindings
    # `_cctally_oauth_samples.DECISION_OP_KINDS` (the two reviewed weekly
    # decision op kinds whose journal coordinates the read pass now records)
    # and `_cctally_oauth_samples._INSERT_HISTORY` (the literal history
    # insert). Changed: `STATS_INDEX_EPOCH` 1021 -> 1022; the fold applier
    # `apply_weekly_observation_decision_op` (`~ computed-by`), which beside
    # its unchanged decision upsert now writes one
    # `claude_weekly_correction_history` row per decided observation with its
    # decoded disposition (`retract` for a schema-3 retraction); the helpers
    # `open_db` (see the open_db re-pin) and `_apply_quota_projection_schema`
    # (the `fit_lineage` column and `meter_rate_change_supersessions`, see its
    # re-fingerprint); and the producer, whose read pass records decision op
    # coordinates, resolves staged cutover-attributed observations through
    # `attribute_staged_observations` after the cutover is known, writes the
    # completed batches' lineage in transaction A and records each decision
    # op's coordinate after the op folds, and whose inventories list the new
    # tables. Every new statement is a literal write registered as its own
    # contract. This site's `SELECT COUNT(*) FROM {tbl}` over the unchanged
    # `_REBUILD_COUNT_TABLES`, its execution condition and its read-only
    # effect are unchanged, so the contract and its MAINT inert rule stand.
    # Reviewed re-pin (#939 D5, base 81bbcde43), for
    # _rebuild_stats_index_locked #1. Changed (`~ computed-by`
    # `_cctally_cache.IngestStats`): the dataclass gains one field,
    # `failed_paths: set` (default empty), the paths behind `files_failed`
    # that a conversation sync records through `_record_failed_files` and
    # only the dashboard's writeIo admission hold compares between passes;
    # the transcript rebuild worker does not serialize it
    # (`_CONVERSATION_RESIDUAL_FIELDS`). It is Python state, never SQL: this
    # site's `SELECT COUNT(*) FROM {tbl}` over the unchanged
    # `_REBUILD_COUNT_TABLES`, its table selection, predicate, ordering,
    # selected column, parameters, execution condition and read-only effect
    # are unchanged, so the contract and its MAINT inert rule stand.
    # Reviewed re-pin (#948 merge of #939 part A, base f840dd69d), for
    # _rebuild_stats_index_locked #1: the shape combines the #939 change
    # above (`IngestStats.failed_paths`) with the #948 T-A change recorded at
    # _publish_generation_in_place #1 (`STATS_INDEX_EPOCH` 1022 -> 1023 and
    # the `open_db` schema block). Neither touches this site's SQL, table
    # selection, predicate, ordering, selected column, execution condition or
    # effect, so the contract and its MAINT inert rule stand.
    # Reviewed re-pin (#1020, base a781dec99), for
    # _rebuild_stats_index_locked #1. The closure reaches the owner-record
    # liveness helper `_cctally_db._repair_marker_is_live` through the W9
    # keeper fence (`_lib_wal_checkpoint._fence_at`). Changed
    # (`~ computed-by` `_repair_marker_is_live`, `- computed-by`
    # `_read_repair_owner`, `+ computed-by` `_read_repair_owner_record`,
    # `_read_owner_record_text`, `_owner_record_file`,
    # `_decode_repair_owner`, `+ binding` `_OWNER_RECORD_FILE_NAME`,
    # `_OWNER_RECORD_READ_ATTEMPTS`): the record reader now resolves
    # `<marker>/owner` for a directory-form record (a volume without hard
    # links) with a bounded re-read, the old decoding moved unchanged into
    # `_decode_repair_owner`, and the identity-unavailable lease is taken
    # from the record file actually read. A file-form record reads and
    # judges exactly as before. It is file-system Python state, never SQL:
    # this site's `SELECT COUNT(*) FROM {tbl}` over the unchanged
    # `_REBUILD_COUNT_TABLES`, its table selection, predicate, ordering,
    # selected column, parameters, execution condition and read-only effect
    # are unchanged, so the contract and its MAINT inert rule stand.
    # Reviewed re-pin (#1003/#1004, base bfc595fe9), for
    # _rebuild_stats_index_locked #1. Added: `binding` and `assigned`
    # `_cctally_record._BLOCK_TOTALS_ENGINE` (#1003 D1's engine slot, which
    # `_compute_block_totals` consults; only `_scratch_read_adapters` sets it,
    # never this rebuild), `computed-by _cctally_record._block_totals_reprice`
    # (today's full re-price body, moved unchanged out of
    # `_compute_block_totals`) and `binding
    # _lib_retained_records.MATERIALIZE_FOR_TESTS` (#1004 D4's test switch).
    # Changed: `BlockTotals` (gains `add_priced`, the one per-entry fold step
    # `fold_block_totals` and the engine share), `_compute_block_totals` and
    # `fold_block_totals` (the seam and the shared step, same additions in the
    # same order), and the producer, whose read pass keeps its retained
    # records as a coordinate-backed `RetainedRecords` and installs the legacy
    # account normalization as a decode-time hook instead of an in-place loop.
    # All of it is Python state, never SQL: this site's `SELECT COUNT(*) FROM
    # {tbl}` over the unchanged `_REBUILD_COUNT_TABLES`, its table selection,
    # predicate, ordering, selected column, parameters, execution condition
    # and read-only effect are unchanged, so the contract and its MAINT inert
    # rule stand.
    # Reviewed re-pin (#916, base 6807e4052), for
    # _rebuild_stats_index_locked #1. The closure reaches
    # `_cctally_cache.sync_cache`. Added: `binding _cctally_cache._ingest_frontier`
    # (the module's existing frontier handle, which sync_cache now reads for
    # the `claude_ingest_walk_orphan_residual` key) and `computed-by
    # _cctally_cache._clear_claude_walk_verdicts` (the rebuild and truncation
    # wipes' single DELETE of the walk-complete marker and that token).
    # Changed: `sync_cache` (an orphan-only walk now ends residual-only and
    # writes the token instead of the marker, the re-walk UPSERT is chosen per
    # file from its own -1 cursor, and `finally` withdraws both verdicts),
    # `IngestStats` (gains `full_walk_residual_only` and
    # `orphan_residual_count`), and `assigned _LAST_WARNED_ORPHAN_SET`, whose
    # assigning body is sync_cache; the warning throttle itself is unchanged.
    # All of it is cache.db Python state, never SQL: this site's
    # `SELECT COUNT(*) FROM {tbl}` over the unchanged `_REBUILD_COUNT_TABLES`,
    # its table selection, predicate, ordering, selected column, parameters,
    # execution condition and read-only effect are unchanged, so the contract
    # and its MAINT inert rule stand.
    # Reviewed re-pin (#1018 twin T1, base d94a83792), for
    # _rebuild_stats_index_locked #1. Added: the bindings
    # `_cctally_oauth_samples._HOLD_ELIGIBILITY`, `_READ_ELIGIBILITY_FACTS`
    # and `_SET_ELIGIBILITY` and `_lib_oauth_samples._CANONICAL_FORMAT`, and
    # `computed-by` `_cctally_oauth_samples._maintain_freshness_eligibility`,
    # `_before_reset`, `_lib_oauth_samples.canonical_instant`, `_parse_aware`
    # and `format_request_started`. Changed: `STATS_INDEX_EPOCH` 1023 -> 1024,
    # the helper `open_db` (see the open_db re-pin) and the fold applier
    # `apply_weekly_observation_decision_op` (`~ computed-by`), which beside
    # its unchanged decision upsert and history insert now keeps the decided
    # observation's `claude_weekly_observation_index.weekly_freshness_eligible`
    # current by primary key: a `hold` clears it, an `accept` reads that row's
    # stored genuine flag, weekly value, reset and capture and recomputes it
    # with the parsed pre-reset test (spec 5.1). Those are keyed writes to a
    # projection no detector decision reads. This site's `SELECT COUNT(*)
    # FROM {tbl}` over the unchanged `_REBUILD_COUNT_TABLES`, its execution
    # condition and its read-only effect are unchanged, so the contract and
    # its MAINT inert rule stand.
    # Reviewed re-pin (#1050 T2, base a858a87ab), for
    # _rebuild_stats_index_locked #1. Added: `helper
    # _cctally_core:_ensure_stats_content_revision` and the
    # `_lib_stats_content_revision` installer it calls (`helper`
    # `ensure_stats_content_revision`, `classify`, `_inspect`, `_install`,
    # `_default_guard`, `_contention_errors`, `_is_busy`), with every binding
    # and `computed-by` they read: the kernel's table/index/trigger SQL
    # (`REVISION_TABLE_SQL`, `SCOPE_TABLE_SQL`, `USAGE_SNAPSHOT_INDEX_SQL`,
    # `TRIGGER_SQL` built by `_trigger_sql`/`_body`/`_upsert`/`_owner`/
    # `_render`/`_scope_selects` over `TRACKED_TABLES`), its names, states
    # and `_SCHEMA_READ`. Changed: the helper `open_db` (see the open_db
    # re-pin). The rebuild reaches them only through the scratch's
    # `open_db(_target_path=...)` schema apply, which now installs the
    # kernel objects into the scratch (spec 4.3); each installer statement
    # is registered as its own MAINT contract. This site's `SELECT COUNT(*)
    # FROM {tbl}` over the unchanged `_REBUILD_COUNT_TABLES`, its execution
    # condition and its read-only effect are unchanged, so the contract and
    # its MAINT inert rule stand.
    # Reviewed re-pin (#1050 T3, base 737379aab), for
    # _rebuild_stats_index_locked #1. Added: `computed-by
    # _lib_stats_content_revision.child_set_unchanged` with its helper
    # `_default_value` and bindings `CHILD_TABLES`, `_CHILD_SELECTS`,
    # `_CHILD_COLUMNS` and `_UNDECIDABLE`, and `computed-by
    # _cctally_five_hour._open_block_child_rows`. Changed: `computed-by
    # _cctally_five_hour.project_open_block_end`, reached through
    # `_refresh_every_open_block` in the scratch rebuild: it now builds its
    # model and project rows with `_open_block_child_rows` and skips its
    # unchanged DELETE and INSERT statements when
    # `child_set_unchanged` finds the stored set equal to them (an
    # exact-key read of the same window and account the DELETE names). The
    # rows the projection leaves are identical either way. This site's
    # `SELECT COUNT(*) FROM {tbl}` over the unchanged `_REBUILD_COUNT_TABLES`,
    # its execution condition and its read-only effect are unchanged, so the
    # contract and its MAINT inert rule stand.
    # Reviewed re-pin (#1050 T3 fix, base 7602973ef), for
    # _rebuild_stats_index_locked #1. Changed: only the binding
    # `_lib_stats_content_revision._CHILD_COLUMNS`, which drops its
    # `ORDER BY cid` (the column order only has to agree between the two
    # tuple sets built from that one read, and the sort cost a temp B-tree on
    # the live record path). The column set it returns is unchanged, so the
    # contract and its MAINT inert rule stand.
    # Reviewed re-pin (#1055, base a2b64a5fb; the same delta on a merge
    # candidate over d2da6cd94), for _rebuild_stats_index_locked #1, reached
    # through `sync_cache`. Added: `computed-by
    # _cctally_cache._wait_behind_drain_unit` and the
    # `_lib_codex_drain_admission` helpers it calls (`ClaudeWaiter`,
    # `_waiter_path`, `lock_busy`), their bindings
    # (`CLAUDE_HOOK_DRAIN_POLL_SECONDS`, `CLAUDE_WAITER_FILE_NAME`,
    # `CLAUDE_WAITER_HOLD_ALLOWANCE_SECONDS`, `GATE_FILE_NAME`,
    # `_WAITER_MAGIC`, `_WAITER_VERSION`, `_monotonic_ns`) and `assigned
    # _cctally_core.CACHE_LOCK_CODEX_PATH`, the lock the wait probes.
    # Changed: `sync_cache` (its `drain_wait_seconds` keyword, the bounded
    # wait after a refused first attempt, and the nested cleanup that
    # unlocks before it releases the waiter), `_acquire_cache_flock` (its
    # `poll` and absolute `deadline` keywords; a `timeout`-only call keeps
    # the old loop) and `assigned _LAST_WARNED_ORPHAN_SET`, whose assigning
    # body is sync_cache. All of it is lock-file and waiter-record Python
    # state, never SQL, and only the hook-tick Claude leg passes
    # `drain_wait_seconds`, so the wait never runs on the rebuild path. This
    # site's `SELECT COUNT(*) FROM {tbl}` over the unchanged
    # `_REBUILD_COUNT_TABLES`, its execution condition and its read-only
    # effect are unchanged, so the contract and its MAINT inert rule stand.
    AccessContract(
        function='_cctally_journal:_rebuild_stats_index_locked',
        sql_fingerprint='dynamic:_cctally_journal:_rebuild_stats_index_locked#1',
        sql_shape='sha256:2c06127369fd4dad',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint='CREATE TABLE IF NOT EXISTS journal_derivation_frontier (id INTEGER PRIMARY KEY CHECK (id = 1), segment TEXT NOT NULL, offset INTEGER NOT NULL, output_segment TEXT, output_offset INTEGER)',
        table='journal_derivation_frontier',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_core:open_db',
        sql_fingerprint='CREATE TABLE IF NOT EXISTS journal_excluded_event_revisions (event_id TEXT PRIMARY KEY, max_rev INTEGER NOT NULL)',
        table='journal_excluded_event_revisions',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_journal:_read_derivation_state',
        sql_fingerprint='SELECT segment, offset, output_segment, output_offset, proof_generation FROM journal_derivation_frontier WHERE id = 1',
        table='journal_derivation_frontier',
        keys=('id',),
        predicate='id = 1',
        ordering='',
        selected=('segment', 'offset', 'output_segment', 'output_offset', 'proof_generation'),
        effects='read',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_journal:_record_excluded_revisions',
        sql_fingerprint='INSERT INTO journal_excluded_event_revisions (event_id, max_rev) VALUES (?, ?) ON CONFLICT(event_id) DO UPDATE SET max_rev = MAX(max_rev, excluded.max_rev)',
        table='journal_excluded_event_revisions',
        keys=('event_id',),
        predicate='event_id = ?',
        ordering='',
        selected=(),
        effects='upsert',
        classes=('period-keyed',),
        inert_rule=JOURNAL,
    ),
    AccessContract(
        function='_cctally_journal:_write_derivation_frontier',
        sql_fingerprint='DELETE FROM journal_derivation_frontier',
        table='journal_derivation_frontier',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='delete',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_journal:_write_derivation_frontier',
        sql_fingerprint='INSERT INTO journal_derivation_frontier (id, segment, offset, output_segment, output_offset, proof_generation) VALUES (1, ?, ?, ?, ?, ?)',
        table='journal_derivation_frontier',
        keys=('id',),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_journal:physical_excluded_revision',
        sql_fingerprint='SELECT max_rev FROM journal_excluded_event_revisions WHERE event_id = ?',
        table='journal_excluded_event_revisions',
        keys=('event_id',),
        predicate='event_id = ?',
        ordering='',
        selected=('max_rev',),
        effects='read',
        classes=('period-keyed',),
        inert_rule=JOURNAL,
    ),
    # Reviewed re-pin (#1003 D1, base bfc595fe9), for _joined_entries #1.
    # Changed: only the producer. The row-to-entry construction moved
    # unchanged into `_joined_entry_from_row`, which `BlockTotalsEngine`
    # shares so an engine entry equals a fresh query's field for field. The
    # SQL text, its cache.db `session_entries`/`session_files` tables,
    # predicate, ordering, selected columns, parameters and read-only effect
    # are unchanged, and it never reads stats.db, so the contract and its
    # MAINT inert rule stand.
    AccessContract(
        function='_cctally_rederive:_joined_entries',
        sql_fingerprint='dynamic:_cctally_rederive:_joined_entries#1',
        sql_shape='sha256:019898f4526da0c8',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL over cache.db session entries only (the scratch derivation cost read); never stats.db',
        ordering='',
        selected=(),
        effects='read',
        classes=('range-keyed',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_journal:_record_new_effective_event',
        sql_fingerprint='DELETE FROM journal_effective_events WHERE event_id = ?',
        table='journal_effective_events',
        keys=('event_id',),
        predicate='event_id = ?',
        ordering='',
        selected=(),
        effects='delete',
        classes=('period-keyed',),
        inert_rule=JOURNAL,
    ),
    # #943 T3 (spec 4.7): the post-credit floor is now bound from
    # `resolve_five_hour_axis_floor` (the exact companion boundary, else the
    # floored slot) instead of an inline COALESCE over the latest
    # `five_hour_reset_events` row, so the subquery and its ordering left
    # the statement. Same table, keys, `>=` band direction and read effect;
    # the older-policy fold branch now issues this identical statement and
    # is covered by the `_usage_snapshot_fold_decision` contract of the
    # same text.
    AccessContract(
        function='_cctally_journal:_rematerialize_five_hour_high_water',
        sql_fingerprint='SELECT MAX(five_hour_percent) FROM weekly_usage_snapshots WHERE five_hour_window_key = ? AND account_key = ? AND unixepoch(captured_at_utc) >= unixepoch(?)',
        table='weekly_usage_snapshots',
        keys=('five_hour_window_key', 'account_key'),
        predicate='five_hour_window_key = ? AND account_key = ? AND unixepoch(captured_at_utc) >= unixepoch(?)',
        ordering='',
        selected=('MAX(five_hour_percent)',),
        effects='read',
        classes=('period-keyed', 'range-keyed'),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_journal:_rematerialize_five_hour_high_water',
        sql_fingerprint='SELECT five_hour_window_key, account_key FROM weekly_usage_snapshots WHERE five_hour_window_key IS NOT NULL ORDER BY captured_at_utc DESC, id DESC LIMIT 1',
        table='weekly_usage_snapshots',
        keys=(),
        predicate='five_hour_window_key IS NOT NULL',
        ordering='captured_at_utc DESC, id DESC',
        selected=('five_hour_window_key', 'account_key'),
        effects='read',
        classes=('cross-period',),
        inert_rule=MAINT,
    ),
    # Reviewed re-pin (#1050 T3, base 737379aab), for
    # _replace_block_children #1. Changed: only the producer itself. It
    # builds each child set's rows first and calls
    # `_lib_stats_content_revision.child_set_unchanged` (its own contract
    # below) with the same window and account as this DELETE's predicate,
    # skipping the DELETE and the strict INSERTs when the stored set equals
    # them. The `DELETE FROM {child_table} WHERE {predicate}` over the
    # unchanged `_BLOCK_CHILDREN`, its predicate and parameter construction
    # and its effect are unchanged; it now runs only when the set differs,
    # and the rows left behind are identical either way, so the contract and
    # its JOURNAL inert rule stand.
    AccessContract(
        function='_cctally_journal:_replace_block_children',
        sql_fingerprint='dynamic:_cctally_journal:_replace_block_children#1',
        sql_shape='sha256:d8dee0c16168bf40',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=JOURNAL,
    ),
    AccessContract(
        function='_cctally_journal:_replace_protocol_violations',
        sql_fingerprint='DELETE FROM journal_protocol_violations',
        table='journal_protocol_violations',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='delete',
        classes=('singleton',),
        inert_rule=JOURNAL,
    ),
    AccessContract(
        function='_cctally_journal:_replace_protocol_violations',
        sql_fingerprint='INSERT INTO journal_protocol_violations (fingerprint, batch_id, kind, violation_json, available_after) VALUES (?, ?, ?, ?, ?)',
        table='journal_protocol_violations',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=JOURNAL,
    ),
    AccessContract(
        function='_cctally_journal:_resolve_fold_ref',
        sql_fingerprint='SELECT 1 FROM usage_snapshot_suppressions WHERE journal_id = ?',
        table='usage_snapshot_suppressions',
        keys=('journal_id',),
        predicate='journal_id = ?',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=JOURNAL,
    ),
    AccessContract(
        function='_cctally_journal:_resolve_ref',
        sql_fingerprint='dynamic:_cctally_journal:_resolve_ref#1',
        sql_shape='sha256:985085c93cb87555',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=JOURNAL,
    ),
    AccessContract(
        function='_cctally_journal:_reverse_ref',
        sql_fingerprint='dynamic:_cctally_journal:_reverse_ref#1',
        sql_shape='sha256:4f71c4c1dd51392d',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=JOURNAL,
    ),
    AccessContract(
        function='_cctally_journal:_scoped_query',
        sql_fingerprint='dynamic:_cctally_journal:_scoped_query#1',
        sql_shape='sha256:65fb6ece865bf93b',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=JOURNAL,
    ),
    AccessContract(
        function='_cctally_journal:_scoped_query',
        sql_fingerprint='dynamic:_cctally_journal:_scoped_query#2',
        sql_shape='sha256:fea23067d238cbe7',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=JOURNAL,
    ),
    AccessContract(
        function='_cctally_journal:_selector_generation_matches',
        sql_fingerprint='SELECT record_path, stamped_at_utc FROM stats_publication_stamp',
        table='stats_publication_stamp',
        keys=(),
        predicate='',
        ordering='',
        selected=('record_path', 'stamped_at_utc'),
        effects='read',
        classes=('cross-period',),
        inert_rule=JOURNAL,
    ),
    AccessContract(
        function='_cctally_journal:_first_post_floor_unchanged',
        sql_fingerprint='SELECT five_hour_percent, five_hour_window_key, five_hour_resets_at, captured_at_utc FROM weekly_usage_snapshots WHERE week_start_date = ? AND account_key = ? ORDER BY captured_at_utc DESC, id DESC LIMIT 1',
        table='weekly_usage_snapshots',
        keys=('week_start_date', 'account_key'),
        predicate='week_start_date = ? AND account_key = ?',
        ordering='captured_at_utc DESC, id DESC',
        selected=('five_hour_percent', 'five_hour_window_key', 'five_hour_resets_at', 'captured_at_utc'),
        effects='read',
        classes=('period-keyed',),
        inert_rule=JOURNAL,
    ),
    AccessContract(
        function='_cctally_journal:latest_post_floor_weekly_row',
        sql_fingerprint='SELECT weekly_percent, captured_at_utc FROM weekly_usage_snapshots WHERE week_start_date = ? AND account_key = ? AND weekly_observation_held = 0 AND weekly_percent IS NOT NULL AND unixepoch(week_end_at) = unixepoch(?) AND unixepoch(captured_at_utc) >= unixepoch(?) ORDER BY captured_at_utc DESC, id DESC LIMIT 1',
        table='weekly_usage_snapshots',
        keys=('week_start_date', 'account_key'),
        predicate='week_start_date = ? AND account_key = ? AND weekly_observation_held = 0 AND weekly_percent IS NOT NULL AND unixepoch(week_end_at) = unixepoch(?) AND unixepoch(captured_at_utc) >= unixepoch(?)',
        ordering='captured_at_utc DESC, id DESC',
        selected=('weekly_percent', 'captured_at_utc'),
        effects='read',
        classes=('period-keyed', 'range-keyed'),
        inert_rule=JOURNAL,
    ),
    AccessContract(
        function='_cctally_journal:_usage_snapshot_fold_decision',
        sql_fingerprint='SELECT MAX(five_hour_percent) FROM weekly_usage_snapshots WHERE five_hour_window_key = ? AND account_key = ? AND unixepoch(captured_at_utc) >= unixepoch(?)',
        table='weekly_usage_snapshots',
        keys=('five_hour_window_key', 'account_key'),
        predicate='five_hour_window_key = ? AND account_key = ? AND unixepoch(captured_at_utc) >= unixepoch(?)',
        ordering='',
        selected=('MAX(five_hour_percent)',),
        effects='read',
        classes=('period-keyed', 'range-keyed'),
        inert_rule=JOURNAL,
    ),
    AccessContract(
        function='_cctally_journal:_usage_snapshot_fold_decision',
        sql_fingerprint='SELECT 1 FROM five_hour_blocks WHERE five_hour_window_key = ? AND account_key = ? LIMIT 1',
        table='five_hour_blocks',
        keys=('five_hour_window_key', 'account_key'),
        predicate='five_hour_window_key = ? AND account_key = ?',
        ordering='',
        selected=('1',),
        effects='read',
        classes=('period-keyed',),
        inert_rule=JOURNAL,
    ),
    AccessContract(
        function='_cctally_journal:_usage_snapshot_fold_decision',
        sql_fingerprint='SELECT MAX(weekly_percent) FROM weekly_usage_snapshots WHERE week_start_date = ? AND account_key = ? AND weekly_observation_held = 0 AND unixepoch(captured_at_utc) >= unixepoch(?)',
        table='weekly_usage_snapshots',
        keys=('week_start_date', 'account_key'),
        predicate='week_start_date = ? AND account_key = ? AND weekly_observation_held = 0 AND unixepoch(captured_at_utc) >= unixepoch(?)',
        ordering='',
        selected=('MAX(weekly_percent)',),
        effects='read',
        classes=('period-keyed', 'range-keyed'),
        inert_rule=JOURNAL,
    ),
    AccessContract(
        function='_cctally_journal:_usage_snapshot_fold_decision',
        sql_fingerprint='SELECT weekly_percent, five_hour_percent, five_hour_window_key FROM weekly_usage_snapshots WHERE week_start_date = ? AND account_key = ? ORDER BY captured_at_utc DESC, id DESC LIMIT 1',
        table='weekly_usage_snapshots',
        keys=('week_start_date', 'account_key'),
        predicate='week_start_date = ? AND account_key = ?',
        ordering='captured_at_utc DESC, id DESC',
        selected=('weekly_percent', 'five_hour_percent', 'five_hour_window_key'),
        effects='read',
        classes=('period-keyed',),
        inert_rule=JOURNAL,
    ),
    AccessContract(
        function='_cctally_journal:_v1_rate_change_identity_exists',
        sql_fingerprint='SELECT 1 FROM journal_effective_events WHERE event_id = ?',
        table='journal_effective_events',
        keys=('event_id',),
        predicate='event_id = ?',
        ordering='',
        selected=('1',),
        effects='read',
        classes=('period-keyed',),
        inert_rule=JOURNAL,
    ),
    AccessContract(
        function='_cctally_journal:_validate_rebuilt_stats_index',
        sql_fingerprint='SELECT id, journal_id FROM weekly_usage_snapshots ORDER BY id DESC LIMIT 1',
        table='weekly_usage_snapshots',
        keys=(),
        predicate='',
        ordering='id DESC',
        selected=('id', 'journal_id'),
        effects='read',
        classes=('cross-period',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_journal:_validate_rebuilt_stats_index',
        sql_fingerprint='SELECT segment, offset, applied_segment, applied_offset FROM journal_cursor WHERE id = 1',
        table='journal_cursor',
        keys=(),
        predicate='id = 1',
        ordering='',
        selected=('segment', 'offset', 'applied_segment', 'applied_offset'),
        effects='read',
        classes=('cross-period',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_journal:_write_admission_summary',
        sql_fingerprint='INSERT OR REPLACE INTO journal_admission_summary (id, cutover_id, scanned_high_water, excluded_total, family_counts_json, first_excluded_coordinate, latest_excluded_coordinate, coordinate_digest, complete_through, boundary_json) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        table='journal_admission_summary',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='upsert',
        classes=('singleton',),
        inert_rule=JOURNAL,
    ),
    AccessContract(
        function='_cctally_journal:_write_cursor',
        sql_fingerprint='INSERT INTO journal_cursor (id, segment, offset, applied_segment, applied_offset) VALUES (1, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET segment = excluded.segment, offset = excluded.offset, applied_segment = excluded.applied_segment, applied_offset = excluded.applied_offset',
        table='journal_cursor',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='upsert',
        classes=('singleton',),
        inert_rule=JOURNAL,
    ),
    AccessContract(
        function='_cctally_journal:_write_quota_projection_state',
        sql_fingerprint='INSERT INTO stats_quota_projection_state(id, incomplete, target_version, recovery_target_json) VALUES (1, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET incomplete = excluded.incomplete, target_version = excluded.target_version, recovery_target_json = excluded.recovery_target_json',
        table='stats_quota_projection_state',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='upsert',
        classes=('singleton',),
        inert_rule=CODEX,
    ),
    AccessContract(
        function='_cctally_journal:_write_selector_delta',
        sql_fingerprint='DELETE FROM journal_protocol_violations WHERE fingerprint = ?',
        table='journal_protocol_violations',
        keys=('fingerprint',),
        predicate='fingerprint = ?',
        ordering='',
        selected=(),
        effects='delete',
        classes=('singleton',),
        inert_rule=JOURNAL,
    ),
    AccessContract(
        function='_cctally_journal:_write_selector_delta',
        sql_fingerprint='INSERT INTO journal_selector_state (id, generation_record_path, generation_stamped_at_utc, covered_segment, covered_offset, next_sequence, selector_version, cutover_seen, cutover_account_key) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET generation_record_path = excluded.generation_record_path, generation_stamped_at_utc = excluded.generation_stamped_at_utc, covered_segment = excluded.covered_segment, covered_offset = excluded.covered_offset, next_sequence = excluded.next_sequence, selector_version = excluded.selector_version, cutover_seen = excluded.cutover_seen, cutover_account_key = excluded.cutover_account_key',
        table='journal_selector_state',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='upsert',
        classes=('singleton',),
        inert_rule=JOURNAL,
    ),
    AccessContract(
        function='_cctally_journal:_write_selector_delta',
        sql_fingerprint='INSERT OR REPLACE INTO journal_effective_events (event_id, rev, status, content_hash, batch_id, event_json, winning_sequence, conflict_hashes_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        table='journal_effective_events',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='upsert',
        classes=('singleton',),
        inert_rule=JOURNAL,
    ),
    AccessContract(
        function='_cctally_journal:_write_selector_delta',
        sql_fingerprint='INSERT OR REPLACE INTO journal_protocol_violations (fingerprint, batch_id, kind, violation_json, available_after) VALUES (?, ?, ?, ?, ?)',
        table='journal_protocol_violations',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='upsert',
        classes=('singleton',),
        inert_rule=JOURNAL,
    ),
    AccessContract(
        function='_cctally_journal:_write_selector_delta',
        sql_fingerprint='INSERT OR REPLACE INTO journal_selector_batch_records (batch_id, kind, key, record_digest, identity_digest, sequence, action_core_json) VALUES (?, ?, ?, ?, ?, ?, ?)',
        table='journal_selector_batch_records',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='upsert',
        classes=('singleton',),
        inert_rule=JOURNAL,
    ),
    AccessContract(
        function='_cctally_journal:_write_selector_delta',
        sql_fingerprint='INSERT OR REPLACE INTO journal_selector_batches (batch_id, status, action_count, action_set_hash, begin_segment, begin_offset, earliest_commit_segment, earliest_commit_offset) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        table='journal_selector_batches',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='upsert',
        classes=('singleton',),
        inert_rule=JOURNAL,
    ),
    AccessContract(
        function='_cctally_journal:_write_selector_state',
        sql_fingerprint='DELETE FROM journal_effective_events',
        table='journal_effective_events',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='delete',
        classes=('singleton',),
        inert_rule=JOURNAL,
    ),
    AccessContract(
        function='_cctally_journal:_write_selector_state',
        sql_fingerprint='DELETE FROM journal_selector_batch_records',
        table='journal_selector_batch_records',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='delete',
        classes=('singleton',),
        inert_rule=JOURNAL,
    ),
    AccessContract(
        function='_cctally_journal:_write_selector_state',
        sql_fingerprint='DELETE FROM journal_selector_batches',
        table='journal_selector_batches',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='delete',
        classes=('singleton',),
        inert_rule=JOURNAL,
    ),
    AccessContract(
        function='_cctally_journal:_write_selector_state',
        sql_fingerprint='DELETE FROM journal_selector_state',
        table='journal_selector_state',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='delete',
        classes=('singleton',),
        inert_rule=JOURNAL,
    ),
    AccessContract(
        function='_cctally_journal:_write_selector_state',
        sql_fingerprint='INSERT INTO journal_effective_events (event_id, rev, status, content_hash, batch_id, event_json, winning_sequence, conflict_hashes_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        table='journal_effective_events',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=JOURNAL,
    ),
    AccessContract(
        function='_cctally_journal:_write_selector_state',
        sql_fingerprint='INSERT INTO journal_selector_batch_records (batch_id, kind, key, record_digest, identity_digest, sequence, action_core_json) VALUES (?, ?, ?, ?, ?, ?, ?)',
        table='journal_selector_batch_records',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=JOURNAL,
    ),
    AccessContract(
        function='_cctally_journal:_write_selector_state',
        sql_fingerprint='INSERT INTO journal_selector_batches (batch_id, status, action_count, action_set_hash, begin_segment, begin_offset, earliest_commit_segment, earliest_commit_offset) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        table='journal_selector_batches',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=JOURNAL,
    ),
    AccessContract(
        function='_cctally_journal:_write_selector_state',
        sql_fingerprint='INSERT INTO journal_selector_state (id, generation_record_path, generation_stamped_at_utc, covered_segment, covered_offset, next_sequence, selector_version, cutover_seen, cutover_account_key) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)',
        table='journal_selector_state',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=JOURNAL,
    ),
    # Reviewed re-pin (#956 T4, base 6528383a7), for emit_model_a #1. Changed:
    # the producer `emit_model_a` itself and one binding it now reads,
    # `_FIVE_HOUR_DIRTYING_KINDS`. The function gained two calls to
    # `five_hour_windows_touched_by_evt` (an exact `journal_id` read,
    # registered as its own JOURNAL contract) and one in-memory update of
    # `ctx.five_hour_dirty_windows`; the dynamic `SELECT id FROM {table} WHERE
    # journal_id = ?` this pin covers is unchanged: same table selection,
    # key, predicate, selected column, execution condition and effect, so
    # the contract and its inert rule stand.
    # Reviewed re-pin (#956 T5, base af10118c7), for emit_model_a #1. Changed:
    # only the binding `_FIVE_HOUR_DIRTYING_KINDS`, which gains
    # `five_hour_block_open`, so an applied opening marks its own window and its
    # account's open blocks dirty (the in-memory dirty set;
    # `five_hour_windows_touched_by_evt`'s new account-scoped read is registered
    # as its own BLOCKS contract). The dynamic `SELECT id FROM {table} WHERE
    # journal_id = ?` this pin covers is unchanged. No SQL, table selection,
    # predicate, ordering, selected column, execution condition or effect of
    # this site changed, so the contract and its inert rule stand.
    AccessContract(
        function='_cctally_journal:emit_model_a',
        sql_fingerprint='dynamic:_cctally_journal:emit_model_a#1',
        sql_shape='sha256:5babe769cb993e34',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=JOURNAL,
    ),
    AccessContract(
        function='_cctally_journal:freeze_five_hour_block_close',
        sql_fingerprint='SELECT * FROM five_hour_blocks WHERE id = ?',
        table='five_hour_blocks',
        keys=('id',),
        predicate='id = ?',
        ordering='',
        selected=('*',),
        effects='read',
        classes=('period-keyed',),
        inert_rule=JOURNAL,
    ),
    # Reviewed re-pin (#943 T3, base c5419d209), for
    # latch_crossing_was_retired #1. Changed: only bindings this site reads —
    # `_HARVEST_SPECS`, which gains the `five_hour_credit_boundary` spec
    # (table `five_hour_credit_boundaries`, prefix `fhcb`, order 29). No SQL,
    # table selection, predicate, ordering, selected column, execution
    # condition or effect of this site changed, so the contract and its inert
    # rule stand.
    # Reviewed re-pin (#956 T5, base af10118c7), for latch_crossing_was_retired
    # #1. Changed: only the `_HARVEST_SPECS` binding, which gains the
    # `five_hour_block_open` spec (prefix `fhbo`); the site looks up the latch's
    # own family spec, which is unchanged. No SQL, table selection, predicate,
    # ordering, selected column, execution condition or effect of this site
    # changed, so the contract and its inert rule stand.
    AccessContract(
        function='_cctally_journal:latch_crossing_was_retired',
        sql_fingerprint='dynamic:_cctally_journal:latch_crossing_was_retired#1',
        sql_shape='sha256:36fef39af242870b',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=JOURNAL,
    ),
    AccessContract(
        function='_cctally_journal:read_publication_stamp',
        sql_fingerprint='SELECT record_path FROM stats_publication_stamp',
        table='stats_publication_stamp',
        keys=(),
        predicate='',
        ordering='',
        selected=('record_path',),
        effects='read',
        classes=('cross-period',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_journal:record_meter_rate_change',
        sql_fingerprint='SELECT provider, account_key, effective_from, previous_units_per_point, new_units_per_point, severity, withholding_status, detector_input_causes, composition_provenance, baseline_withheld_days FROM meter_rate_change_events WHERE provider = ? AND account_key = ? AND effective_from = ? LIMIT 1',
        table='meter_rate_change_events',
        keys=('provider', 'account_key', 'effective_from'),
        predicate='provider = ? AND account_key = ? AND effective_from = ?',
        ordering='',
        selected=('provider', 'account_key', 'effective_from', 'previous_units_per_point', 'new_units_per_point', 'severity', 'withholding_status', 'detector_input_causes', 'composition_provenance', 'baseline_withheld_days'),
        effects='read',
        classes=('cross-period',),
        inert_rule=JOURNAL,
    ),
    # #1015 T7: the supersession emitter's presence check returns early
    # when the explicit row for this natural key already exists.
    AccessContract(
        function='_cctally_journal:record_meter_rate_change_supersession',
        sql_fingerprint='SELECT 1 FROM meter_rate_change_supersessions WHERE provider = ? AND account_key = ? AND effective_from = ?',
        table='meter_rate_change_supersessions',
        keys=('provider', 'account_key', 'effective_from'),
        predicate='provider = ? AND account_key = ? AND effective_from = ?',
        ordering='',
        selected=('1',),
        effects='read',
        classes=('singleton',),
        inert_rule=LINEAGE,
    ),
    AccessContract(
        function='_cctally_journal:stats_index_matches_journal_prefix',
        sql_fingerprint='SELECT event_id, rev, status, content_hash, batch_id, event_json FROM journal_effective_events ORDER BY event_id',
        table='journal_effective_events',
        keys=(),
        predicate='',
        ordering='event_id',
        selected=('event_id', 'rev', 'status', 'content_hash', 'batch_id', 'event_json'),
        effects='read',
        classes=('cross-period',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_journal:stats_index_matches_journal_prefix',
        sql_fingerprint='SELECT violation_json FROM journal_protocol_violations ORDER BY batch_id, kind, fingerprint',
        table='journal_protocol_violations',
        keys=(),
        predicate='',
        ordering='batch_id, kind, fingerprint',
        selected=('violation_json',),
        effects='read',
        classes=('cross-period',),
        inert_rule=MAINT,
    ),
    # Reviewed re-pin (#997 T-C1 and the revision-10 fix round, base
    # 9282d8b73): the producer builds its prefix hasher through
    # `_new_prefix_hasher` and passes each segment's size to `begin_segment`
    # (the streamed accumulator, D12). The hasher executes no SQL; the SQL
    # text, its execution condition and its read-only effect are unchanged,
    # so the contract and its MAINT inert rule stand.
    # Reviewed re-pin (#1019/#1014 Task A, base 5b15db929), for #1 and #2.
    # Changed: `STATS_INDEX_EPOCH`, `PROTOCOL_RESOLUTION_KINDS` (+v4),
    # `_validate_protocol_resolution` accepting `wpx` in its key set and
    # identity core, `strip_stamp` stripping `wpx` (new `_WPX`,
    # SUCCESSOR_REVISION_KEY and _STAMP_KEYS bindings) and the path
    # assignments. The prefix-match reads, their tables, predicates and
    # read-only effect are unchanged, so the contracts stand.
    # Reviewed re-pin (#943 T3, base c5419d209), for
    # stats_index_matches_journal_prefix #1. Changed: only bindings this site
    # reads — `STATS_INDEX_EPOCH` 1020 -> 1021 (spec 4.7). No SQL, table
    # selection, predicate, ordering, selected column, execution condition or
    # effect of this site changed, so the contract and its inert rule stand.
    # Reviewed re-pin (#956 T5, base af10118c7), for
    # stats_index_matches_journal_prefix #1. Changed: only the `_EVT_SPECS`
    # binding, whose new `five_hour_block_open` spec carries the applier
    # `_apply_five_hour_block_open`; the closure follows it into
    # `_apply_generic_evt` and its helpers (`_insert_or_ignore`,
    # `_resolve_fold_ref`, `_resolve_ref`, `_derived_fk_value`,
    # `_report_unresolved_fold_reference`, `_SUPPRESSIBLE_FOLD_REFS`,
    # `JournalError`, `UnresolvedFoldReference`), `_block_opening_row` and
    # `_materialize_opened_block`. The site reads the family tables the specs
    # name, and the new family's table is read by the same statement shape as
    # every other. No SQL, table selection, predicate, ordering, selected
    # column, execution condition or effect of this site changed, so the
    # contract and its inert rule stand.
    # Reviewed re-pin (#1015 T3, base 16b055cae), for stats_index_matches_journal_prefix #1. Changed: only
    # the binding `STATS_INDEX_EPOCH` 1021 -> 1022 (spec 5.1). No SQL, table
    # selection, predicate, ordering, selected column, execution condition or
    # effect of this site changed, so the contract and its inert rule stand.
    # Reviewed re-pin (#1015 T7, base 7455ca434), for stats_index_matches_journal_prefix #1. Changed: only the
    # fold machinery this site reaches through bindings: `_EVT_SPECS` gained
    # the table-less `meter_rate_change_supersession` spec (order 46), whose
    # applier `_apply_meter_rate_change_supersession` calls
    # `_insert_meter_rate_change_supersession` (a literal INSERT OR IGNORE,
    # registered as its own contract) and reads `MRC_SUPERSESSION_REASON`
    # and `_canonical_json_or_none`; `_MeterRateChangeRow` gained the
    # nullable `fit_lineage` field, which `_meter_rate_change_row` fills
    # through `_canonical_json_or_none` and
    # `_backfill_meter_rate_change_evidence` COALESCEs (its literal UPDATE is
    # reviewed beside its own contract). The site skips a table-less spec
    # (`spec.table is None`) and reads the family tables the other specs
    # name with the same statement shape. No SQL, table selection,
    # predicate, ordering, selected column, execution condition or effect of
    # this site changed, so the contract and its inert rule stand.
    # Reviewed re-pin (#1018 twin T1, base d94a83792), for stats_index_matches_journal_prefix #1. Changed:
    # only the binding `STATS_INDEX_EPOCH` 1023 -> 1024 (spec 5.1). No SQL,
    # table selection, predicate, ordering, selected column, execution
    # condition or effect of this site changed, so the contract and its
    # inert rule stand.
    AccessContract(
        function='_cctally_journal:stats_index_matches_journal_prefix',
        sql_fingerprint='dynamic:_cctally_journal:stats_index_matches_journal_prefix#1',
        sql_shape='sha256:ee1cf10e54836826',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=MAINT,
    ),
    # Same reviewed re-pin as #1 (#997 D12).
    # Reviewed re-pin (#956 T5, base af10118c7), for
    # stats_index_matches_journal_prefix #2. Changed: only the `_EVT_SPECS`
    # binding, whose new `five_hour_block_open` spec carries the applier
    # `_apply_five_hour_block_open`; the closure follows it into
    # `_apply_generic_evt` and its helpers (`_insert_or_ignore`,
    # `_resolve_fold_ref`, `_resolve_ref`, `_derived_fk_value`,
    # `_report_unresolved_fold_reference`, `_SUPPRESSIBLE_FOLD_REFS`,
    # `JournalError`, `UnresolvedFoldReference`), `_block_opening_row` and
    # `_materialize_opened_block`. The site reads the family tables the specs
    # name, and the new family's table is read by the same statement shape as
    # every other. No SQL, table selection, predicate, ordering, selected
    # column, execution condition or effect of this site changed, so the
    # contract and its inert rule stand.
    # Reviewed re-pin (#1015 T7, base 7455ca434), for stats_index_matches_journal_prefix #2. Changed: only the
    # fold machinery this site reaches through bindings: `_EVT_SPECS` gained
    # the table-less `meter_rate_change_supersession` spec (order 46), whose
    # applier `_apply_meter_rate_change_supersession` calls
    # `_insert_meter_rate_change_supersession` (a literal INSERT OR IGNORE,
    # registered as its own contract) and reads `MRC_SUPERSESSION_REASON`
    # and `_canonical_json_or_none`; `_MeterRateChangeRow` gained the
    # nullable `fit_lineage` field, which `_meter_rate_change_row` fills
    # through `_canonical_json_or_none` and
    # `_backfill_meter_rate_change_evidence` COALESCEs (its literal UPDATE is
    # reviewed beside its own contract). The site skips a table-less spec
    # (`spec.table is None`) and reads the family tables the other specs
    # name with the same statement shape. No SQL, table selection,
    # predicate, ordering, selected column, execution condition or effect of
    # this site changed, so the contract and its inert rule stand.
    AccessContract(
        function='_cctally_journal:stats_index_matches_journal_prefix',
        sql_fingerprint='dynamic:_cctally_journal:stats_index_matches_journal_prefix#2',
        sql_shape='sha256:0cb254b5c3b1d634',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_milestones:_budget_crossings',
        sql_fingerprint='UPDATE budget_milestones SET alerted_at = ? WHERE vendor = ? AND account_key = ? AND period_start_at = ? AND period = ? AND threshold = ? AND alerted_at IS NULL',
        table='budget_milestones',
        keys=('vendor', 'account_key', 'period_start_at', 'period', 'threshold'),
        predicate='vendor = ? AND account_key = ? AND period_start_at = ? AND period = ? AND threshold = ? AND alerted_at IS NULL',
        ordering='',
        selected=(),
        effects='write',
        classes=('period-keyed',),
        inert_rule=Q7,
    ),
    AccessContract(
        function='_cctally_milestones:_projected_levels_already_latched',
        sql_fingerprint='SELECT threshold FROM projected_milestones WHERE week_start_at = ? AND (period = ? OR period IS NULL) AND metric = ?',
        table='projected_milestones',
        keys=('week_start_at', 'period', 'metric'),
        predicate='week_start_at = ? AND (period = ? OR period IS NULL) AND metric = ?',
        ordering='',
        selected=('threshold',),
        effects='read',
        classes=('period-keyed',),
        inert_rule=Q7,
    ),
    AccessContract(
        function='_cctally_milestones:get_max_milestone_for_week',
        sql_fingerprint='SELECT MAX(percent_threshold) AS max_pct FROM percent_milestones WHERE week_start_date = ? AND reset_event_id = ? AND account_key = ?',
        table='percent_milestones',
        keys=('week_start_date', 'reset_event_id', 'account_key'),
        predicate='week_start_date = ? AND reset_event_id = ? AND account_key = ?',
        ordering='',
        selected=('MAX(percent_threshold) AS max_pct',),
        effects='read',
        classes=('period-keyed',),
        inert_rule=Q7,
    ),
    AccessContract(
        function='_cctally_milestones:get_milestone_cost_for_week',
        sql_fingerprint='SELECT cumulative_cost_usd FROM percent_milestones WHERE week_start_date = ? AND percent_threshold = ? AND reset_event_id = ? AND account_key = ?',
        table='percent_milestones',
        keys=('week_start_date', 'percent_threshold', 'reset_event_id', 'account_key'),
        predicate='week_start_date = ? AND percent_threshold = ? AND reset_event_id = ? AND account_key = ?',
        ordering='',
        selected=('cumulative_cost_usd',),
        effects='read',
        classes=('period-keyed',),
        inert_rule=Q7,
    ),
    AccessContract(
        function='_cctally_milestones:insert_budget_milestone',
        sql_fingerprint='INSERT OR IGNORE INTO budget_milestones (vendor, account_key, period_start_at, period, threshold, budget_usd, spent_usd, consumption_pct, crossed_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
        table='budget_milestones',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=Q7,
    ),
    AccessContract(
        function='_cctally_milestones:insert_cost_snapshot',
        sql_fingerprint='INSERT INTO weekly_cost_snapshots ( captured_at_utc, week_start_date, week_end_date, week_start_at, week_end_at, range_start_iso, range_end_iso, cost_usd, mode, project, account_key ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        table='weekly_cost_snapshots',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=Q7,
    ),
    AccessContract(
        function='_cctally_milestones:insert_percent_milestone',
        sql_fingerprint='INSERT OR IGNORE INTO percent_milestones ( captured_at_utc, week_start_date, week_end_date, week_start_at, week_end_at, percent_threshold, cumulative_cost_usd, marginal_cost_usd, usage_snapshot_id, cost_snapshot_id, five_hour_percent_at_crossing, reset_event_id, account_key ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        table='percent_milestones',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=Q7,
    ),
    AccessContract(
        function='_cctally_milestones:insert_project_budget_milestone',
        sql_fingerprint='INSERT OR IGNORE INTO project_budget_milestones (week_start_at, project_key, threshold, budget_usd, spent_usd, consumption_pct, crossed_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?)',
        table='project_budget_milestones',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=Q7,
    ),
    AccessContract(
        function='_cctally_milestones:insert_projected_milestone',
        sql_fingerprint='INSERT OR IGNORE INTO projected_milestones (week_start_at, period, metric, threshold, projected_value, denominator, crossed_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?)',
        table='projected_milestones',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=Q7,
    ),
    # #1015 epoch 1022 (spec 5.1): the correction-to-fit lineage writers.
    # `project_observation` indexes each admitted Claude weekly observation
    # by its attributed account on live ingest (reached from
    # `_pipeline_claude_usage` through `project_record`); the apply-only
    # rebuild stages a cutover-attributed observation and
    # `attribute_staged_observations` resolves it once the cutover is known.
    # `record_correction_op` records a reviewed decision op's journal
    # coordinate. `write_batch_lineage` records each completed correction
    # batch's commit coordinate (an authoritative upsert from the selector's
    # completion step, else INSERT OR IGNORE) and its footprint. All are
    # keyed writes that a replay repeats to the same rows, and no detector
    # decision reads them.
    # #1018 twin (spec 5.1): the two index writes also carry the enriched
    # columns, and their freshness eligibility consults the effective
    # reviewed decision of the same observation id (a decision folded before
    # its observation still applies). The read is keyed and feeds only the
    # written eligibility, which the current-week freshness reader alone
    # consumes; no detector decision reads it.
    AccessContract(
        function='_cctally_journal:write_batch_lineage',
        sql_fingerprint='INSERT INTO correction_batch_lineage (batch_id, commit_segment, commit_offset) VALUES (?, ?, ?) ON CONFLICT(batch_id) DO UPDATE SET commit_segment = excluded.commit_segment, commit_offset = excluded.commit_offset',
        table='correction_batch_lineage',
        keys=('batch_id',),
        predicate='',
        ordering='',
        selected=(),
        effects='upsert',
        classes=('singleton',),
        inert_rule=LINEAGE,
    ),
    AccessContract(
        function='_cctally_journal:write_batch_lineage',
        sql_fingerprint='INSERT OR IGNORE INTO correction_batch_footprint (batch_id, provider, account_key) VALUES (?, ?, ?)',
        table='correction_batch_footprint',
        keys=('batch_id', 'provider', 'account_key'),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=LINEAGE,
    ),
    AccessContract(
        function='_cctally_journal:write_batch_lineage',
        sql_fingerprint='INSERT OR IGNORE INTO correction_batch_lineage (batch_id, commit_segment, commit_offset) VALUES (?, ?, ?)',
        table='correction_batch_lineage',
        keys=('batch_id',),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=LINEAGE,
    ),
    AccessContract(
        function='_cctally_oauth_samples:attribute_staged_observations',
        sql_fingerprint="INSERT OR IGNORE INTO claude_weekly_observation_index (observation_id, account_key, captured_at_utc, weekly_percent, weekly_resets_at_utc, weekly_reading_genuine, weekly_freshness_eligible) SELECT s.observation_id, ?, s.captured_at_utc, s.weekly_percent, s.weekly_resets_at_utc, s.base_genuine AND (s.required_account IS NULL OR s.required_account = ?), s.base_genuine AND (s.required_account IS NULL OR s.required_account = ?) AND s.pre_reset AND NOT EXISTS (SELECT 1 FROM claude_weekly_observation_decisions AS d WHERE d.observation_id = s.observation_id AND d.disposition = 'hold') FROM rebuild_weekly_observation_unstamped AS s",
        table='claude_weekly_observation_index',
        keys=('observation_id',),
        predicate="eligibility reads claude_weekly_observation_decisions by observation_id (disposition = 'hold')",
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=LINEAGE,
    ),
    AccessContract(
        function='_cctally_oauth_samples:project_observation',
        sql_fingerprint="INSERT OR IGNORE INTO claude_weekly_observation_index (observation_id, account_key, captured_at_utc, weekly_percent, weekly_resets_at_utc, weekly_reading_genuine, weekly_freshness_eligible) VALUES (?, ?, ?, ?, ?, ?, ? AND NOT EXISTS (SELECT 1 FROM claude_weekly_observation_decisions AS d WHERE d.observation_id = ? AND d.disposition = 'hold'))",
        table='claude_weekly_observation_index',
        keys=('observation_id',),
        predicate="eligibility reads claude_weekly_observation_decisions by observation_id (disposition = 'hold')",
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=LINEAGE,
    ),
    AccessContract(
        function='_cctally_oauth_samples:record_correction_op',
        sql_fingerprint='INSERT OR IGNORE INTO claude_weekly_correction_ops (decision_op_id, op_segment, op_offset) VALUES (?, ?, ?)',
        table='claude_weekly_correction_ops',
        keys=('decision_op_id',),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=LINEAGE,
    ),
    # #797/#823/#840 epoch 1019 (spec 4.3 D8): the provider-sample
    # projection, reached from `_pipeline_claude_usage` through
    # `project_record`. One row per admitted keyed api observation, keyed by
    # account and observation id; INSERT OR IGNORE, so a replay of the same
    # record writes nothing. Not inert: the stale-contributor reducer reads
    # these rows as provider evidence.
    AccessContract(
        function='_cctally_oauth_samples:project_sample',
        sql_fingerprint='INSERT OR IGNORE INTO claude_oauth_samples (account_key, observation_id, requested_at_utc, captured_at_utc, weekly_percent, weekly_resets_at_utc, five_hour_percent, five_hour_resets_at_utc, five_hour_window_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
        table='claude_oauth_samples',
        keys=('account_key', 'observation_id'),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=None,
    ),
    AccessContract(
        function='_cctally_quarantine:load_state',
        sql_fingerprint='SELECT account_key, axis, state, waiting_for, coverage_id, witness_id, release_id, revocation, since, recovered_at, not_before, witness_key FROM outcome_quarantine_axis_state WHERE generation = ?',
        table='outcome_quarantine_axis_state',
        keys=('generation',),
        predicate='generation = ?',
        ordering='',
        selected=('account_key', 'axis', 'state', 'waiting_for', 'coverage_id', 'witness_id', 'release_id', 'revocation', 'since', 'recovered_at', 'not_before', 'witness_key'),
        effects='read',
        classes=('cross-period',),
        inert_rule=QUARANTINE,
    ),
    AccessContract(
        function='_cctally_quarantine:load_state',
        sql_fingerprint='SELECT opener_segment, opener_offset, opened_at, closer_segment, closer_offset, closed_at FROM outcome_quarantine_episodes ORDER BY rowid',
        table='outcome_quarantine_episodes',
        keys=(),
        predicate='',
        ordering='rowid',
        selected=('opener_segment', 'opener_offset', 'opened_at', 'closer_segment', 'closer_offset', 'closed_at'),
        effects='read',
        classes=('cross-period',),
        inert_rule=QUARANTINE,
    ),
    AccessContract(
        function='_cctally_quarantine:load_state',
        sql_fingerprint='SELECT scope, axis, bounds_json FROM outcome_quarantine_bounds',
        table='outcome_quarantine_bounds',
        keys=(),
        predicate='',
        ordering='',
        selected=('scope', 'axis', 'bounds_json'),
        effects='read',
        classes=('cross-period',),
        inert_rule=QUARANTINE,
    ),
    AccessContract(
        function='_cctally_quarantine:load_state',
        sql_fingerprint='SELECT seq, segment, byte_offset, kind, reason, at FROM outcome_quarantine_triggers ORDER BY seq DESC LIMIT 1',
        table='outcome_quarantine_triggers',
        keys=(),
        predicate='',
        ordering='seq DESC',
        selected=('seq', 'segment', 'byte_offset', 'kind', 'reason', 'at'),
        effects='read',
        classes=('cross-period',),
        inert_rule=QUARANTINE,
    ),
    AccessContract(
        function='_cctally_quota:_activate_quota_rule',
        sql_fingerprint='INSERT INTO quota_alert_arming (source, source_root_key, logical_limit_key, observed_slot, window_minutes, rule_fingerprint, activated_at_utc, account_key) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(source, source_root_key, account_key, logical_limit_key, observed_slot, window_minutes) DO UPDATE SET rule_fingerprint=excluded.rule_fingerprint, activated_at_utc=excluded.activated_at_utc',
        table='quota_alert_arming',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='upsert',
        classes=('singleton',),
        inert_rule=CODEX,
    ),
    AccessContract(
        function='_cctally_quota:_apply_quota_projection_rows',
        sql_fingerprint='INSERT INTO quota_percent_milestones (source, source_root_key, logical_limit_key, observed_slot, window_minutes, resets_at_utc, percent_threshold, captured_at_utc, source_path, line_offset, high_water_percent, generation, orphaned_at, account_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,NULL,?) ON CONFLICT(source, source_root_key, account_key, logical_limit_key, observed_slot, window_minutes, resets_at_utc, percent_threshold) DO UPDATE SET captured_at_utc=excluded.captured_at_utc, source_path=excluded.source_path, line_offset=excluded.line_offset, high_water_percent=excluded.high_water_percent, generation=excluded.generation, orphaned_at=NULL',
        table='quota_percent_milestones',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='upsert',
        classes=('singleton',),
        inert_rule=CODEX,
    ),
    AccessContract(
        function='_cctally_quota:_apply_quota_projection_rows',
        sql_fingerprint='INSERT INTO quota_projection_state (source_root_key, account_key, generation, physical_signature, completed_at_utc) VALUES (?,?,?,?,?) ON CONFLICT(source_root_key, account_key) DO UPDATE SET generation=excluded.generation, physical_signature=excluded.physical_signature, completed_at_utc=excluded.completed_at_utc',
        table='quota_projection_state',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='upsert',
        classes=('singleton',),
        inert_rule=CODEX,
    ),
    AccessContract(
        function='_cctally_quota:_apply_quota_projection_rows',
        sql_fingerprint='INSERT INTO quota_window_blocks (source, source_root_key, logical_limit_key, observed_slot, window_minutes, limit_id, limit_name, resets_at_utc, nominal_start_at_utc, first_observed_at_utc, last_observed_at_utc, first_percent, current_percent, last_source_path, last_line_offset, generation, orphaned_at, account_key, physical_group_key, physical_group_digest) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,?,?) ON CONFLICT(source, source_root_key, account_key, logical_limit_key, observed_slot, window_minutes, resets_at_utc) DO UPDATE SET limit_id=excluded.limit_id, limit_name=excluded.limit_name, nominal_start_at_utc=excluded.nominal_start_at_utc, first_observed_at_utc=excluded.first_observed_at_utc, last_observed_at_utc=excluded.last_observed_at_utc, first_percent=excluded.first_percent, current_percent=excluded.current_percent, last_source_path=excluded.last_source_path, last_line_offset=excluded.last_line_offset, generation=excluded.generation, orphaned_at=NULL, physical_group_key=excluded.physical_group_key, physical_group_digest=excluded.physical_group_digest',
        table='quota_window_blocks',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='upsert',
        classes=('singleton',),
        inert_rule=CODEX,
    ),
    AccessContract(
        function='_cctally_quota:_apply_quota_projection_rows',
        sql_fingerprint='dynamic:_cctally_quota:_apply_quota_projection_rows#1',
        sql_shape='sha256:0c02fccf21f059a5',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=CODEX,
    ),
    AccessContract(
        function='_cctally_quota:_armed_identities',
        sql_fingerprint="SELECT source, source_root_key, account_key, logical_limit_key, observed_slot, window_minutes, rule_fingerprint FROM quota_alert_arming WHERE source='codex'",
        table='quota_alert_arming',
        keys=(),
        predicate="source='codex'",
        ordering='',
        selected=('source', 'source_root_key', 'account_key', 'logical_limit_key', 'observed_slot', 'window_minutes', 'rule_fingerprint'),
        effects='read',
        classes=('cross-period',),
        inert_rule=CODEX,
    ),
    AccessContract(
        function='_cctally_quota:_arming_row',
        sql_fingerprint='SELECT rule_fingerprint, activated_at_utc FROM quota_alert_arming WHERE source=? AND source_root_key=? AND account_key=? AND logical_limit_key=? AND observed_slot=? AND window_minutes=?',
        table='quota_alert_arming',
        keys=('source', 'source_root_key', 'account_key', 'logical_limit_key', 'observed_slot', 'window_minutes'),
        predicate='source=? AND source_root_key=? AND account_key=? AND logical_limit_key=? AND observed_slot=? AND window_minutes=?',
        ordering='',
        selected=('rule_fingerprint', 'activated_at_utc'),
        effects='read',
        classes=('cross-period',),
        inert_rule=CODEX,
    ),
    AccessContract(
        function='_cctally_quota:_blocks_missing_reverse_map',
        sql_fingerprint="SELECT 1 FROM quota_window_blocks WHERE source='codex' AND physical_group_key IS NULL LIMIT 1",
        table='quota_window_blocks',
        keys=(),
        predicate="source='codex' AND physical_group_key IS NULL",
        ordering='',
        selected=('1',),
        effects='read',
        classes=('cross-period',),
        inert_rule=CODEX,
    ),
    AccessContract(
        function='_cctally_quota:_codex_quota_latest_rows',
        sql_fingerprint='dynamic:_cctally_quota:_codex_quota_latest_rows#1',
        sql_shape='sha256:303314c1c826d8b3',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=CODEX,
    ),
    AccessContract(
        function='_cctally_quota:_codex_quota_read_objects',
        sql_fingerprint='dynamic:_cctally_quota:_codex_quota_read_objects#1',
        sql_shape='sha256:6124cce1a9689748',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=CODEX,
    ),
    AccessContract(
        function='_cctally_quota:_codex_witness_anchors',
        sql_fingerprint='dynamic:_cctally_quota:_codex_witness_anchors#1',
        sql_shape='sha256:174522237238d2e7',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=CODEX,
    ),
    AccessContract(
        function='_cctally_quota:_evaluate_quota_alerts',
        sql_fingerprint="DELETE FROM quota_alert_arming WHERE source='codex'",
        table='quota_alert_arming',
        keys=(),
        predicate="source='codex'",
        ordering='',
        selected=(),
        effects='delete',
        classes=('singleton',),
        inert_rule=CODEX,
    ),
    AccessContract(
        function='_cctally_quota:_evaluate_quota_alerts',
        sql_fingerprint="SELECT source, source_root_key, account_key, logical_limit_key, observed_slot, window_minutes FROM quota_alert_arming WHERE source='codex'",
        table='quota_alert_arming',
        keys=(),
        predicate="source='codex'",
        ordering='',
        selected=('source', 'source_root_key', 'account_key', 'logical_limit_key', 'observed_slot', 'window_minutes'),
        effects='read',
        classes=('cross-period',),
        inert_rule=CODEX,
    ),
    AccessContract(
        function='_cctally_quota:_historic_root_keys',
        sql_fingerprint='dynamic:_cctally_quota:_historic_root_keys#1',
        sql_shape='sha256:7034d91472e41a27',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=CODEX,
    ),
    AccessContract(
        function='_cctally_quota:_insert_quota_terminal_event',
        sql_fingerprint='INSERT OR IGNORE INTO quota_threshold_events (source, source_root_key, logical_limit_key, observed_slot, window_minutes, resets_at_utc, threshold, qualifying_kind, qualifying_percent, projected_percent, severity, created_at_utc, disposition, alerted_at, suppressed_at, account_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        table='quota_threshold_events',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=CODEX,
    ),
    AccessContract(
        function='_cctally_quota:_iter_shard_rows',
        sql_fingerprint='dynamic:_cctally_quota:_iter_shard_rows#1',
        sql_shape='sha256:165fc1ccc9cadbab',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=CODEX,
    ),
    AccessContract(
        function='_cctally_quota:_ledger_rows_after',
        sql_fingerprint='dynamic:_cctally_quota:_ledger_rows_after#1',
        sql_shape='sha256:0bc7183f2e3057ba',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=CODEX,
    ),
    AccessContract(
        function='_cctally_quota:_ledger_state',
        sql_fingerprint="SELECT watermark_seq, interpretation_version, alerts_enabled, next_evaluation_at_utc, last_full_pass_at, next_evaluation_by_root_json FROM quota_projection_ledger_state WHERE source='codex'",
        table='quota_projection_ledger_state',
        keys=(),
        predicate="source='codex'",
        ordering='',
        selected=('watermark_seq', 'interpretation_version', 'alerts_enabled', 'next_evaluation_at_utc', 'last_full_pass_at', 'next_evaluation_by_root_json'),
        effects='read',
        classes=('cross-period',),
        inert_rule=CODEX,
    ),
    AccessContract(
        function='_cctally_quota:_load_codex_window_group_evidence',
        sql_fingerprint='dynamic:_cctally_quota:_load_codex_window_group_evidence#1',
        sql_shape='sha256:db969904c3774419',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=CODEX,
    ),
    AccessContract(
        function='_cctally_quota:_orphan_unseen',
        sql_fingerprint='dynamic:_cctally_quota:_orphan_unseen#1',
        sql_shape='sha256:c7af562f46f3fbaa',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=CODEX,
    ),
    AccessContract(
        function='_cctally_quota:_orphan_unseen',
        sql_fingerprint='dynamic:_cctally_quota:_orphan_unseen#2',
        sql_shape='sha256:9603ce362ea0ea86',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=CODEX,
    ),
    AccessContract(
        function='_cctally_quota:_orphan_unseen',
        sql_fingerprint='dynamic:_cctally_quota:_orphan_unseen#3',
        sql_shape='sha256:43538e803b819a39',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=CODEX,
    ),
    AccessContract(
        function='_cctally_quota:_orphan_unseen_scoped',
        sql_fingerprint='dynamic:_cctally_quota:_orphan_unseen_scoped#1',
        sql_shape='sha256:ccbdb93e0a0638bd',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=CODEX,
    ),
    AccessContract(
        function='_cctally_quota:_orphan_unseen_scoped',
        sql_fingerprint='dynamic:_cctally_quota:_orphan_unseen_scoped#2',
        sql_shape='sha256:b6c101d59a0df239',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=CODEX,
    ),
    AccessContract(
        function='_cctally_quota:_orphan_unseen_scoped',
        sql_fingerprint='dynamic:_cctally_quota:_orphan_unseen_scoped#3',
        sql_shape='sha256:7fdd1662b4fc9bab',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=CODEX,
    ),
    AccessContract(
        function='_cctally_quota:_reanchor_terminal_events',
        sql_fingerprint='dynamic:_cctally_quota:_reanchor_terminal_events#1',
        sql_shape='sha256:c25bb9f7f3b70ee0',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=CODEX,
    ),
    AccessContract(
        function='_cctally_quota:_root_accounts',
        sql_fingerprint="SELECT DISTINCT account_key FROM quota_window_blocks WHERE source='codex' AND source_root_key=? AND orphaned_at IS NULL",
        table='quota_window_blocks',
        keys=('source_root_key',),
        predicate="source='codex' AND source_root_key=? AND orphaned_at IS NULL",
        ordering='',
        selected=('DISTINCT account_key',),
        effects='read',
        classes=('cross-period',),
        inert_rule=CODEX,
    ),
    AccessContract(
        function='_cctally_quota:_root_group_pairs',
        sql_fingerprint="SELECT DISTINCT physical_group_key, physical_group_digest FROM quota_window_blocks WHERE source='codex' AND source_root_key=? AND orphaned_at IS NULL AND physical_group_key IS NOT NULL AND physical_group_digest IS NOT NULL",
        table='quota_window_blocks',
        keys=('source_root_key',),
        predicate="source='codex' AND source_root_key=? AND orphaned_at IS NULL AND physical_group_key IS NOT NULL AND physical_group_digest IS NOT NULL",
        ordering='',
        selected=('DISTINCT physical_group_key', 'physical_group_digest'),
        effects='read',
        classes=('cross-period',),
        inert_rule=CODEX,
    ),
    AccessContract(
        function='_cctally_quota:_stats_projection_signatures_match',
        sql_fingerprint='SELECT source_root_key, account_key, physical_signature FROM quota_projection_state',
        table='quota_projection_state',
        keys=(),
        predicate='',
        ordering='',
        selected=('source_root_key', 'account_key', 'physical_signature'),
        effects='read',
        classes=('cross-period',),
        inert_rule=CODEX,
    ),
    AccessContract(
        function='_cctally_quota:_store_ledger_state',
        sql_fingerprint="INSERT INTO quota_projection_ledger_state (source, watermark_seq, interpretation_version, alerts_enabled, next_evaluation_at_utc, last_full_pass_at, next_evaluation_by_root_json) VALUES ('codex',?,?,?,?,?,?) ON CONFLICT(source) DO UPDATE SET watermark_seq=excluded.watermark_seq, interpretation_version=excluded.interpretation_version, alerts_enabled=excluded.alerts_enabled, next_evaluation_at_utc=excluded.next_evaluation_at_utc, last_full_pass_at=excluded.last_full_pass_at, next_evaluation_by_root_json=excluded.next_evaluation_by_root_json",
        table='quota_projection_ledger_state',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='upsert',
        classes=('singleton',),
        inert_rule=CODEX,
    ),
    # Reviewed re-pin (#997 T-C4, base 9282d8b73): the producer shares
    # repeated text values, reset instants and `QuotaWindowIdentity` objects
    # within one call through `_QuotaLoadValues` (D17). That helper executes
    # no SQL and returns equal values in the same order; the SQL text, its
    # execution condition and its read-only effect are unchanged, so the
    # contract and its inert rule stand.
    AccessContract(
        function='_cctally_quota:load_codex_quota_observations',
        sql_fingerprint='dynamic:_cctally_quota:load_codex_quota_observations#1',
        sql_shape='sha256:1f20459441a53e78',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=CODEX,
    ),
    AccessContract(
        function='_cctally_quota:reconcile_codex_quota_projection',
        sql_fingerprint="SELECT 1 FROM quota_alert_arming WHERE source='codex' LIMIT 1",
        table='quota_alert_arming',
        keys=(),
        predicate="source='codex'",
        ordering='',
        selected=('1',),
        effects='read',
        classes=('cross-period',),
        inert_rule=CODEX,
    ),
    # #1015 T5/T7 and R6: the calibration lineage readers. `_read_base`
    # reads the always-needed half of the correction facts (completed
    # batches, their footprint, each reviewed op's coordinate, the distinct
    # ops the history names by a loose primary-key scan, and a probe that the
    # observation index exists); `_read_rows` reads one op's history rows
    # joined to their observation, narrowed to what the assessed lineages
    # can be affected by; `rate_change_supersession` reads one transition's
    # explicit supersession and its stored lineage by natural key. They feed
    # the forecast, project, doctor, status-line and quota refusal and the
    # rate-change marker/notification suppression; no automatic credit,
    # moved-end or drop-consensus decision reads them.
    AccessContract(
        function='_cctally_quota_calibration:_read_base',
        sql_fingerprint='SELECT batch_id, commit_segment, commit_offset FROM correction_batch_lineage',
        table='correction_batch_lineage',
        keys=(),
        predicate='',
        ordering='',
        selected=('batch_id', 'commit_segment', 'commit_offset'),
        effects='read',
        classes=('cross-period',),
        inert_rule=LINEAGE,
    ),
    AccessContract(
        function='_cctally_quota_calibration:_read_base',
        sql_fingerprint='SELECT batch_id, provider, account_key FROM correction_batch_footprint',
        table='correction_batch_footprint',
        keys=(),
        predicate='',
        ordering='',
        selected=('batch_id', 'provider', 'account_key'),
        effects='read',
        classes=('cross-period',),
        inert_rule=LINEAGE,
    ),
    AccessContract(
        function='_cctally_quota_calibration:_read_base',
        sql_fingerprint='SELECT decision_op_id, op_segment, op_offset FROM claude_weekly_correction_ops',
        table='claude_weekly_correction_ops',
        keys=(),
        predicate='',
        ordering='',
        selected=('decision_op_id', 'op_segment', 'op_offset'),
        effects='read',
        classes=('cross-period',),
        inert_rule=LINEAGE,
    ),
    AccessContract(
        function='_cctally_quota_calibration:_read_base',
        sql_fingerprint='SELECT observation_id, account_key, captured_at_utc FROM claude_weekly_observation_index LIMIT 0',
        table='claude_weekly_observation_index',
        keys=(),
        predicate='LIMIT 0',
        ordering='',
        selected=('observation_id', 'account_key', 'captured_at_utc'),
        effects='read',
        classes=('cross-period',),
        inert_rule=LINEAGE,
    ),
    AccessContract(
        function='_cctally_quota_calibration:_read_base',
        sql_fingerprint='WITH RECURSIVE d(op) AS (SELECT MIN(decision_op_id) FROM claude_weekly_correction_history UNION ALL SELECT (SELECT MIN(decision_op_id) FROM claude_weekly_correction_history WHERE decision_op_id > d.op) FROM d WHERE d.op IS NOT NULL) SELECT op FROM d WHERE op IS NOT NULL',
        table='claude_weekly_correction_history',
        keys=('decision_op_id',),
        predicate='decision_op_id > d.op',
        ordering='',
        selected=('decision_op_id',),
        effects='read',
        classes=('cross-period',),
        inert_rule=LINEAGE,
    ),
    AccessContract(
        function='_cctally_quota_calibration:_read_rows',
        sql_fingerprint="SELECT h.observation_id, h.disposition, i.account_key, i.captured_at_utc FROM claude_weekly_correction_history h LEFT JOIN claude_weekly_observation_index i ON i.observation_id = h.observation_id WHERE h.decision_op_id = :op AND (i.observation_id IS NULL OR ((:scope = '*' OR i.account_key = :scope) AND (:account IS NULL OR i.account_key = :account) AND (:lo IS NULL OR i.captured_at_utc >= :lo) AND (:hi IS NULL OR i.captured_at_utc < :hi)) OR ((:scope = '*' OR i.account_key = :scope) AND NOT (substr(i.captured_at_utc, 1, 19) GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]' AND (substr(i.captured_at_utc, 20) IN ('Z', '+00:00') OR substr(i.captured_at_utc, 20) GLOB '.[0-9][0-9][0-9][0-9][0-9][0-9]Z' OR substr(i.captured_at_utc, 20) GLOB '.[0-9][0-9][0-9][0-9][0-9][0-9]+00:00' OR substr(i.captured_at_utc, 20) GLOB '.[0-9][0-9][0-9]Z' OR substr(i.captured_at_utc, 20) GLOB '.[0-9][0-9][0-9]+00:00') AND substr(i.captured_at_utc, 1, 4) <> '0000' AND datetime(substr(i.captured_at_utc, 1, 19)) = replace(substr(i.captured_at_utc, 1, 19), 'T', ' '))))",
        table='claude_weekly_correction_history',
        keys=('decision_op_id',),
        predicate='h.decision_op_id = :op AND (unresolved, or not a canonical UTC capture, or inside the account scope and the capture box)',
        ordering='',
        selected=('observation_id', 'disposition', 'account_key', 'captured_at_utc'),
        effects='read',
        classes=('cross-period',),
        inert_rule=LINEAGE,
    ),
    AccessContract(
        function='_cctally_quota_calibration:rate_change_supersession',
        sql_fingerprint='SELECT detected_at_utc, fit_lineage FROM meter_rate_change_events WHERE provider = ? AND account_key = ? AND effective_from = ?',
        table='meter_rate_change_events',
        keys=('provider', 'account_key', 'effective_from'),
        predicate='provider = ? AND account_key = ? AND effective_from = ?',
        ordering='',
        selected=('detected_at_utc', 'fit_lineage'),
        effects='read',
        classes=('singleton',),
        inert_rule=LINEAGE,
    ),
    AccessContract(
        function='_cctally_quota_calibration:rate_change_supersession',
        sql_fingerprint='SELECT reason, corrections, superseded_at_utc FROM meter_rate_change_supersessions WHERE provider = ? AND account_key = ? AND effective_from = ?',
        table='meter_rate_change_supersessions',
        keys=('provider', 'account_key', 'effective_from'),
        predicate='provider = ? AND account_key = ? AND effective_from = ?',
        ordering='',
        selected=('reason', 'corrections', 'superseded_at_utc'),
        effects='read',
        classes=('singleton',),
        inert_rule=LINEAGE,
    ),
    AccessContract(
        function='_cctally_record:_apply_credit',
        sql_fingerprint='INSERT OR IGNORE INTO weekly_credit_floors (week_start_date, effective_at_utc, observed_pre_credit_pct, applied_at_utc, account_key) VALUES (?, ?, ?, ?, ?)',
        table='weekly_credit_floors',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MANUAL,
    ),
    AccessContract(
        function='_cctally_record:_apply_credit',
        sql_fingerprint='SELECT journal_id FROM weekly_credit_floors WHERE week_start_date = ? AND account_key = ? AND journal_id IS NOT NULL AND journal_id != ?',
        table='weekly_credit_floors',
        keys=('week_start_date', 'account_key'),
        predicate='week_start_date = ? AND account_key = ? AND journal_id IS NOT NULL AND journal_id != ?',
        ordering='',
        selected=('journal_id',),
        effects='read',
        classes=('period-keyed',),
        inert_rule=MANUAL,
    ),
    AccessContract(
        function='_cctally_record:_apply_credit',
        sql_fingerprint="SELECT journal_id FROM weekly_usage_snapshots WHERE week_start_date = ? AND account_key = ? AND source = 'record-credit' AND journal_id IS NOT NULL AND journal_id NOT LIKE 'sa:' || ? || ':syn:%'",
        table='weekly_usage_snapshots',
        keys=('week_start_date', 'account_key'),
        predicate="week_start_date = ? AND account_key = ? AND source = 'record-credit' AND journal_id IS NOT NULL AND journal_id NOT LIKE 'sa:' || ? || ':syn:%'",
        ordering='',
        selected=('journal_id',),
        effects='read',
        classes=('period-keyed',),
        inert_rule=MANUAL,
    ),
    AccessContract(
        function='_cctally_record:_arm_reset_debounce_state',
        sql_fingerprint='INSERT INTO weekly_reset_debounce_state (account_key, week_start_date, week_end_at, baseline_pct, first_zero_at_utc, first_zero_observation_id) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(account_key) DO UPDATE SET week_start_date = excluded.week_start_date, week_end_at = excluded.week_end_at, baseline_pct = excluded.baseline_pct, first_zero_at_utc = excluded.first_zero_at_utc, first_zero_observation_id = excluded.first_zero_observation_id',
        table='weekly_reset_debounce_state',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='upsert',
        classes=('singleton',),
        inert_rule=None,
    ),
    AccessContract(
        function='_cctally_record:_clear_reset_debounce_state',
        sql_fingerprint='DELETE FROM weekly_reset_debounce_state WHERE account_key = ?',
        table='weekly_reset_debounce_state',
        keys=('account_key',),
        predicate='account_key = ?',
        ordering='',
        selected=(),
        effects='delete',
        classes=('singleton',),
        inert_rule=None,
    ),
    AccessContract(
        function='_cctally_record:_delete_doomed_snapshot_rows',
        sql_fingerprint='dynamic:_cctally_record:_delete_doomed_snapshot_rows#1',
        sql_shape='sha256:029afc7add377117',
        table='dynamic',
        keys=(),
        predicate='stale-replica band: account, date key, capture >= effective, weekly band, carrying its own five-hour reading (spec 4.7 revision 7: kept weekly-held)',
        ordering='',
        selected=(),
        effects='write',
        classes=('period-keyed', 'range-keyed'),
        inert_rule=None,
    ),
    AccessContract(
        function='_cctally_record:_delete_doomed_snapshot_rows',
        sql_fingerprint='dynamic:_cctally_record:_delete_doomed_snapshot_rows#2',
        sql_shape='sha256:332a2909ac5f8963',
        table='dynamic',
        keys=(),
        predicate='stale-replica band: account, date key, capture >= effective, weekly band',
        ordering='',
        selected=(),
        effects='delete',
        classes=('period-keyed', 'range-keyed'),
        inert_rule=None,
    ),
    AccessContract(
        function='_cctally_record:_derive_5h_window_key',
        sql_fingerprint='SELECT five_hour_resets_at, five_hour_window_key FROM weekly_usage_snapshots WHERE five_hour_resets_at IS NOT NULL AND five_hour_window_key IS NOT NULL AND account_key = ? ORDER BY captured_at_utc DESC, id DESC LIMIT 1',
        table='weekly_usage_snapshots',
        keys=('account_key',),
        predicate='five_hour_resets_at IS NOT NULL AND five_hour_window_key IS NOT NULL AND account_key = ?',
        ordering='captured_at_utc DESC, id DESC',
        selected=('five_hour_resets_at', 'five_hour_window_key'),
        effects='read',
        classes=('cross-period',),
        inert_rule=ANCHOR,
    ),
    AccessContract(
        function='_cctally_record:_derive_5h_window_key',
        sql_fingerprint="SELECT five_hour_window_key, five_hour_resets_at FROM five_hour_blocks WHERE account_key = ? AND abs(? - CAST(strftime('%s', five_hour_resets_at) AS INTEGER)) <= ? ORDER BY abs(? - CAST(strftime('%s', five_hour_resets_at) AS INTEGER)) ASC LIMIT 1",
        table='five_hour_blocks',
        keys=('account_key',),
        predicate="account_key = ? AND abs(? - CAST(strftime('%s', five_hour_resets_at) AS INTEGER)) <= ?",
        ordering="abs(? - CAST(strftime('%s', five_hour_resets_at) AS INTEGER)) ASC",
        selected=('five_hour_window_key', 'five_hour_resets_at'),
        effects='read',
        classes=('range-keyed', 'cross-period'),
        inert_rule=ANCHOR,
    ),
    AccessContract(
        function='_cctally_record:_doomed_snapshot_rows',
        sql_fingerprint='dynamic:_cctally_record:_doomed_snapshot_rows#1',
        sql_shape='sha256:b5941392ff09fd6b',
        table='dynamic',
        keys=(),
        predicate='stale-replica band: account, date key, capture >= effective, weekly band',
        ordering='',
        selected=(),
        effects='read',
        classes=('period-keyed', 'range-keyed'),
        inert_rule=None,
    ),
    AccessContract(
        function='_cctally_record:_fire_in_place_credit',
        sql_fingerprint='INSERT OR IGNORE INTO week_reset_events (detected_at_utc, old_week_end_at, new_week_end_at, effective_reset_at_utc, observed_pre_credit_pct, account_key, origin_observation_id) VALUES (?, ?, ?, ?, ?, ?, ?)',
        table='week_reset_events',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=None,
    ),
    AccessContract(
        function='_cctally_record:_five_hour_ownership_windows',
        sql_fingerprint='SELECT five_hour_window_key, block_start_at, five_hour_resets_at FROM five_hour_blocks WHERE account_key = ? AND five_hour_window_key != ? AND unixepoch(five_hour_resets_at) > unixepoch(?) AND unixepoch(block_start_at) <= unixepoch(?)',
        table='five_hour_blocks',
        keys=('account_key',),
        predicate='account_key = ? AND five_hour_window_key != ? AND unixepoch(five_hour_resets_at) > unixepoch(?) AND unixepoch(block_start_at) <= unixepoch(?)',
        ordering='',
        selected=('five_hour_window_key', 'block_start_at', 'five_hour_resets_at'),
        effects='read',
        classes=('range-keyed', 'cross-period'),
        inert_rule=BLOCKS,
    ),
    AccessContract(
        function='_cctally_record:_insert_credit_snapshot',
        sql_fingerprint='dynamic:_cctally_record:_insert_credit_snapshot#1',
        sql_shape='sha256:93b68f833e86ea12',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=MANUAL,
    ),
    AccessContract(
        function='_cctally_record:_pipeline_claude_usage',
        sql_fingerprint='SELECT * FROM weekly_usage_snapshots WHERE week_start_date = ? AND account_key = ? ORDER BY captured_at_utc DESC, id DESC LIMIT 1',
        table='weekly_usage_snapshots',
        keys=('week_start_date', 'account_key'),
        predicate='week_start_date = ? AND account_key = ?',
        ordering='captured_at_utc DESC, id DESC',
        selected=('*',),
        effects='read',
        classes=('period-keyed',),
        inert_rule=None,
    ),
    AccessContract(
        function='_cctally_record:_pipeline_claude_usage',
        sql_fingerprint='SELECT * FROM weekly_usage_snapshots WHERE id = ?',
        table='weekly_usage_snapshots',
        keys=('id',),
        predicate='id = ?',
        ordering='',
        selected=('*',),
        effects='read',
        classes=('period-keyed',),
        inert_rule=None,
    ),
    AccessContract(
        function='_cctally_record:_read_five_hour_source_state',
        sql_fingerprint='SELECT baseline_pct, pending_low_pct, pending_at_utc, pending_observation_id, baseline_observation_id, baseline_generation, pending_generation, pending_position, pending_captured_at_utc FROM five_hour_credit_confirmation_state WHERE account_key = ? AND five_hour_window_key = ? AND source = ?',
        table='five_hour_credit_confirmation_state',
        keys=('account_key', 'five_hour_window_key', 'source'),
        predicate='account_key = ? AND five_hour_window_key = ? AND source = ?',
        ordering='',
        selected=('baseline_pct', 'pending_low_pct', 'pending_at_utc', 'pending_observation_id', 'baseline_observation_id', 'baseline_generation', 'pending_generation', 'pending_position', 'pending_captured_at_utc'),
        effects='read',
        classes=('period-keyed',),
        inert_rule=None,
    ),
    # #797/#823/#840 spec 4.9 (D3, D11): the v2 detector's eager rebase of
    # the window's other contributors at a credit, the ordinal of a credit's
    # retained causal facts, and the selected credit's content for its
    # generation (the row `resolve_five_hour_reset_event` chose by logical
    # order; the id only fetches it). All three read one account and window.
    AccessContract(
        function='_cctally_record:_rebase_five_hour_sources',
        sql_fingerprint='SELECT source, baseline_pct, pending_low_pct, pending_at_utc, pending_observation_id, baseline_observation_id, baseline_generation, pending_generation, pending_position, pending_captured_at_utc FROM five_hour_credit_confirmation_state WHERE account_key = ? AND five_hour_window_key = ?',
        table='five_hour_credit_confirmation_state',
        keys=('account_key', 'five_hour_window_key'),
        predicate='account_key = ? AND five_hour_window_key = ?',
        ordering='',
        selected=('source', 'baseline_pct', 'pending_low_pct', 'pending_at_utc', 'pending_observation_id', 'baseline_observation_id', 'baseline_generation', 'pending_generation', 'pending_position', 'pending_captured_at_utc'),
        effects='read',
        classes=('period-keyed',),
        inert_rule=None,
    ),
    AccessContract(
        function='_cctally_record:_record_five_hour_credit_facts',
        sql_fingerprint="SELECT COUNT(*) FROM five_hour_credit_confirmation_state WHERE account_key = ? AND five_hour_window_key = ? AND source LIKE '#credit|%'",
        table='five_hour_credit_confirmation_state',
        keys=('account_key', 'five_hour_window_key'),
        predicate="account_key = ? AND five_hour_window_key = ? AND source LIKE '#credit|%'",
        ordering='',
        selected=('COUNT(*)',),
        effects='read',
        classes=('period-keyed',),
        inert_rule=None,
    ),
    AccessContract(
        function='_cctally_record:five_hour_credit_generation',
        sql_fingerprint='SELECT detected_at_utc, prior_percent, post_percent, effective_reset_at_utc FROM five_hour_reset_events WHERE id = ?',
        table='five_hour_reset_events',
        keys=('id',),
        predicate='id = ?',
        ordering='',
        selected=('detected_at_utc', 'prior_percent', 'post_percent', 'effective_reset_at_utc'),
        effects='read',
        classes=('period-keyed',),
        inert_rule=None,
    ),
    AccessContract(
        function='_cctally_record:_read_reset_debounce_state',
        sql_fingerprint='SELECT week_start_date, week_end_at, baseline_pct, first_zero_at_utc, first_zero_observation_id FROM weekly_reset_debounce_state WHERE account_key = ?',
        table='weekly_reset_debounce_state',
        keys=('account_key',),
        predicate='account_key = ?',
        ordering='',
        selected=('week_start_date', 'week_end_at', 'baseline_pct', 'first_zero_at_utc', 'first_zero_observation_id'),
        effects='read',
        classes=('cross-period',),
        inert_rule=DEBOUNCE,
    ),
    AccessContract(
        function='_cctally_record:_record_budget_milestone_for_vendor',
        sql_fingerprint='SELECT id FROM budget_milestones WHERE vendor=? AND account_key=? AND period_start_at=? AND period=? AND threshold=?',
        table='budget_milestones',
        keys=('vendor', 'account_key', 'period_start_at', 'period', 'threshold'),
        predicate='vendor=? AND account_key=? AND period_start_at=? AND period=? AND threshold=?',
        ordering='',
        selected=('id',),
        effects='read',
        classes=('period-keyed',),
        inert_rule=Q7,
    ),
    AccessContract(
        function='_cctally_record:_record_budget_milestone_for_vendor',
        sql_fingerprint='SELECT threshold FROM budget_milestones WHERE vendor = ? AND account_key = ? AND period_start_at = ? AND (period = ? OR period IS NULL)',
        table='budget_milestones',
        keys=('vendor', 'account_key', 'period_start_at', 'period'),
        predicate='vendor = ? AND account_key = ? AND period_start_at = ? AND (period = ? OR period IS NULL)',
        ordering='',
        selected=('threshold',),
        effects='read',
        classes=('period-keyed',),
        inert_rule=Q7,
    ),
    AccessContract(
        function='_cctally_record:_write_five_hour_source_state',
        sql_fingerprint='DELETE FROM five_hour_credit_confirmation_state WHERE account_key = ? AND source = ? AND five_hour_window_key < ?',
        table='five_hour_credit_confirmation_state',
        keys=('account_key', 'source'),
        predicate='account_key = ? AND source = ? AND five_hour_window_key < ?',
        ordering='',
        selected=(),
        effects='delete',
        classes=('singleton', 'range-keyed'),
        inert_rule=None,
    ),
    AccessContract(
        function='_cctally_record:_write_five_hour_source_state',
        sql_fingerprint='INSERT INTO five_hour_credit_confirmation_state (account_key, five_hour_window_key, source, baseline_pct, pending_low_pct, pending_at_utc, pending_observation_id, baseline_observation_id, baseline_generation, pending_generation, pending_position, pending_captured_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(account_key, five_hour_window_key, source) DO UPDATE SET baseline_pct = excluded.baseline_pct, pending_low_pct = excluded.pending_low_pct, pending_at_utc = excluded.pending_at_utc, pending_observation_id = excluded.pending_observation_id, baseline_observation_id = excluded.baseline_observation_id, baseline_generation = excluded.baseline_generation, pending_generation = excluded.pending_generation, pending_position = excluded.pending_position, pending_captured_at_utc = excluded.pending_captured_at_utc',
        table='five_hour_credit_confirmation_state',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='upsert',
        classes=('singleton',),
        inert_rule=None,
    ),
    AccessContract(
        function='_cctally_record:detect_reset_and_credit',
        sql_fingerprint='DELETE FROM weekly_usage_snapshots WHERE five_hour_window_key = ? AND account_key = ? AND unixepoch(captured_at_utc) >= unixepoch(?) AND ABS(five_hour_percent - ?) < 1.0',
        table='weekly_usage_snapshots',
        keys=('five_hour_window_key', 'account_key'),
        predicate='five_hour_window_key = ? AND account_key = ? AND unixepoch(captured_at_utc) >= unixepoch(?) AND ABS(five_hour_percent - ?) < 1.0',
        ordering='',
        selected=(),
        effects='delete',
        classes=('period-keyed', 'range-keyed'),
        inert_rule=None,
    ),
    AccessContract(
        function='_cctally_record:detect_reset_and_credit',
        sql_fingerprint='INSERT OR IGNORE INTO five_hour_reset_events (detected_at_utc, five_hour_window_key, prior_percent, post_percent, effective_reset_at_utc, account_key) VALUES (?, ?, ?, ?, ?, ?)',
        table='five_hour_reset_events',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=None,
    ),
    # #943 T3 (spec 4.7): the credit's exact-boundary companion, written
    # only when the credit INSERT above inserted a row (rowcount 1), so a
    # replayed or colliding confirm never mints a second boundary.
    AccessContract(
        function='_cctally_record:detect_reset_and_credit',
        sql_fingerprint='INSERT OR IGNORE INTO five_hour_credit_boundaries (account_key, five_hour_window_key, effective_reset_at_utc, boundary_at_utc, arming_observation_id, credit_revision, detected_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?)',
        table='five_hour_credit_boundaries',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=None,
    ),
    AccessContract(
        function='_cctally_record:detect_reset_and_credit',
        sql_fingerprint='INSERT OR IGNORE INTO week_reset_events (detected_at_utc, old_week_end_at, new_week_end_at, effective_reset_at_utc, account_key, origin_observation_id) VALUES (?, ?, ?, ?, ?, ?)',
        table='week_reset_events',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=None,
    ),
    # #943 T3 (spec 4.2.3 crash analysis): the detector's previous accepted
    # row now excludes the observation's own `sa:<observation id>` snapshot,
    # which step 4a can fold from a crashed cycle before the detector
    # re-runs. Same table, keys, ordering, selected columns, read effect and
    # inert rule; live the excluded row does not exist yet.
    AccessContract(
        function='_cctally_record:detect_reset_and_credit',
        sql_fingerprint='SELECT five_hour_window_key, five_hour_percent, five_hour_resets_at FROM weekly_usage_snapshots WHERE five_hour_window_key IS NOT NULL AND five_hour_percent IS NOT NULL AND account_key = ? AND (? IS NULL OR journal_id IS NOT ?) ORDER BY captured_at_utc DESC, id DESC LIMIT 1',
        table='weekly_usage_snapshots',
        keys=('account_key',),
        predicate='five_hour_window_key IS NOT NULL AND five_hour_percent IS NOT NULL AND account_key = ? AND (? IS NULL OR journal_id IS NOT ?)',
        ordering='captured_at_utc DESC, id DESC',
        selected=('five_hour_window_key', 'five_hour_percent', 'five_hour_resets_at'),
        effects='read',
        classes=('cross-period',),
        inert_rule=PRED_FIVE,
    ),
    AccessContract(
        function='_cctally_record:detect_reset_and_credit',
        sql_fingerprint='UPDATE weekly_usage_snapshots SET five_hour_percent = NULL, five_hour_resets_at = NULL, five_hour_window_key = NULL WHERE five_hour_window_key = ? AND account_key = ? AND unixepoch(captured_at_utc) >= unixepoch(?) AND ABS(five_hour_percent - ?) < 1.0',
        table='weekly_usage_snapshots',
        keys=('five_hour_window_key', 'account_key'),
        predicate='five_hour_window_key = ? AND account_key = ? AND unixepoch(captured_at_utc) >= unixepoch(?) AND ABS(five_hour_percent - ?) < 1.0',
        ordering='',
        selected=(),
        effects='write',
        classes=('period-keyed', 'range-keyed'),
        inert_rule=None,
    ),
    AccessContract(
        function='_cctally_record:detect_reset_and_credit',
        sql_fingerprint='UPDATE weekly_usage_snapshots SET five_hour_percent = NULL, five_hour_resets_at = NULL, five_hour_window_key = NULL WHERE five_hour_window_key = ? AND account_key = ? AND unixepoch(captured_at_utc) >= unixepoch(?) AND ABS(five_hour_percent - ?) < 1.0 AND weekly_observation_held = 0',
        table='weekly_usage_snapshots',
        keys=('five_hour_window_key', 'account_key'),
        predicate='five_hour_window_key = ? AND account_key = ? AND unixepoch(captured_at_utc) >= unixepoch(?) AND ABS(five_hour_percent - ?) < 1.0 AND weekly_observation_held = 0',
        ordering='',
        selected=(),
        effects='write',
        classes=('period-keyed', 'range-keyed'),
        inert_rule=None,
    ),
    AccessContract(
        function='_cctally_record:detect_reset_and_credit',
        sql_fingerprint='SELECT journal_id FROM weekly_usage_snapshots WHERE five_hour_window_key = ? AND account_key = ? AND unixepoch(captured_at_utc) >= unixepoch(?) AND ABS(five_hour_percent - ?) < 1.0',
        table='weekly_usage_snapshots',
        keys=('five_hour_window_key', 'account_key'),
        predicate='five_hour_window_key = ? AND account_key = ? AND unixepoch(captured_at_utc) >= unixepoch(?) AND ABS(five_hour_percent - ?) < 1.0',
        ordering='',
        selected=('journal_id',),
        effects='read',
        classes=('period-keyed', 'range-keyed'),
        inert_rule=None,
    ),
    AccessContract(
        function='_cctally_record:detect_reset_and_credit',
        sql_fingerprint='SELECT prior_percent, post_percent FROM five_hour_reset_events WHERE five_hour_window_key = ? AND account_key = ? ORDER BY id DESC LIMIT 1',
        table='five_hour_reset_events',
        keys=('five_hour_window_key', 'account_key'),
        predicate='five_hour_window_key = ? AND account_key = ?',
        ordering='id DESC',
        selected=('prior_percent', 'post_percent'),
        effects='read',
        classes=('period-keyed',),
        inert_rule=None,
    ),
    # #943 (the weekly twin of T3's spec 4.2.3 crash analysis): the weekly
    # previous accepted row also excludes the observation's own
    # `sa:<observation id>` snapshot, which step 4a can fold from a cycle
    # that crashed between the `snapshot_accept` and `wr` appends. Same
    # table, keys, ordering, selected columns, read effect and inert rule;
    # live the excluded row does not exist yet.
    AccessContract(
        function='_cctally_record:detect_reset_and_credit',
        sql_fingerprint='SELECT week_end_at, weekly_percent FROM weekly_usage_snapshots WHERE week_end_at IS NOT NULL AND account_key = ? AND weekly_observation_held = 0 AND (? IS NULL OR journal_id IS NOT ?) ORDER BY captured_at_utc DESC, id DESC LIMIT 1',
        table='weekly_usage_snapshots',
        keys=('account_key',),
        predicate='week_end_at IS NOT NULL AND account_key = ? AND weekly_observation_held = 0 AND (? IS NULL OR journal_id IS NOT ?)',
        ordering='captured_at_utc DESC, id DESC',
        selected=('week_end_at', 'weekly_percent'),
        effects='read',
        classes=('cross-period',),
        inert_rule=PRED_WEEKLY,
    ),
    AccessContract(
        function='_cctally_record:maybe_record_milestone',
        sql_fingerprint='SELECT MIN(weekly_percent) FROM weekly_usage_snapshots WHERE week_start_date = ? AND account_key = ? AND weekly_observation_held = 0 AND unixepoch(captured_at_utc) >= unixepoch(?) AND unixepoch(captured_at_utc) <= unixepoch(?)',
        table='weekly_usage_snapshots',
        keys=('week_start_date', 'account_key'),
        predicate='week_start_date = ? AND account_key = ? AND weekly_observation_held = 0 AND unixepoch(captured_at_utc) >= unixepoch(?) AND unixepoch(captured_at_utc) <= unixepoch(?)',
        ordering='',
        selected=('MIN(weekly_percent)',),
        effects='read',
        classes=('period-keyed', 'range-keyed'),
        inert_rule=Q7,
    ),
    AccessContract(
        function='_cctally_record:maybe_record_milestone',
        sql_fingerprint='SELECT cumulative_cost_usd, week_start_at FROM percent_milestones WHERE week_start_date = ? AND percent_threshold = ? AND reset_event_id = ? AND account_key = ?',
        table='percent_milestones',
        keys=('week_start_date', 'percent_threshold', 'reset_event_id', 'account_key'),
        predicate='week_start_date = ? AND percent_threshold = ? AND reset_event_id = ? AND account_key = ?',
        ordering='',
        selected=('cumulative_cost_usd', 'week_start_at'),
        effects='read',
        classes=('period-keyed',),
        inert_rule=Q7,
    ),
    AccessContract(
        function='_cctally_record:maybe_record_milestone',
        sql_fingerprint='UPDATE percent_milestones SET alerted_at = ? WHERE week_start_date = ? AND percent_threshold = ? AND reset_event_id = ? AND account_key = ? AND alerted_at IS NULL',
        table='percent_milestones',
        keys=('week_start_date', 'percent_threshold', 'reset_event_id', 'account_key'),
        predicate='week_start_date = ? AND percent_threshold = ? AND reset_event_id = ? AND account_key = ? AND alerted_at IS NULL',
        ordering='',
        selected=(),
        effects='write',
        classes=('period-keyed',),
        inert_rule=Q7,
    ),
    AccessContract(
        function='_cctally_record:maybe_record_project_budget_milestone',
        sql_fingerprint='SELECT project_key, threshold FROM project_budget_milestones WHERE week_start_at = ?',
        table='project_budget_milestones',
        keys=('week_start_at',),
        predicate='week_start_at = ?',
        ordering='',
        selected=('project_key', 'threshold'),
        effects='read',
        classes=('period-keyed',),
        inert_rule=Q7,
    ),
    AccessContract(
        function='_cctally_record:maybe_record_project_budget_milestone',
        sql_fingerprint='UPDATE project_budget_milestones SET alerted_at = ? WHERE week_start_at = ? AND project_key = ? AND threshold = ? AND alerted_at IS NULL',
        table='project_budget_milestones',
        keys=('week_start_at', 'project_key', 'threshold'),
        predicate='week_start_at = ? AND project_key = ? AND threshold = ? AND alerted_at IS NULL',
        ordering='',
        selected=(),
        effects='write',
        classes=('period-keyed',),
        inert_rule=Q7,
    ),
    AccessContract(
        function='_cctally_record:maybe_record_projected_alert',
        sql_fingerprint='UPDATE projected_milestones SET alerted_at = ? WHERE week_start_at = ? AND period = ? AND metric = ? AND threshold = ? AND alerted_at IS NULL',
        table='projected_milestones',
        keys=('week_start_at', 'period', 'metric', 'threshold'),
        predicate='week_start_at = ? AND period = ? AND metric = ? AND threshold = ? AND alerted_at IS NULL',
        ordering='',
        selected=(),
        effects='write',
        classes=('period-keyed',),
        inert_rule=Q7,
    ),
    AccessContract(
        function='_cctally_record:maybe_update_five_hour_block',
        sql_fingerprint='INSERT OR IGNORE INTO five_hour_milestones ( block_id, five_hour_window_key, percent_threshold, captured_at_utc, usage_snapshot_id, block_input_tokens, block_output_tokens, block_cache_create_tokens, block_cache_read_tokens, block_cost_usd, marginal_cost_usd, seven_day_pct_at_crossing, reset_event_id, account_key ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        table='five_hour_milestones',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=BLOCKS,
    ),
    AccessContract(
        function='_cctally_record:maybe_update_five_hour_block',
        sql_fingerprint='SELECT COUNT(*) FROM five_hour_blocks WHERE account_key = ?',
        table='five_hour_blocks',
        keys=('account_key',),
        predicate='account_key = ?',
        ordering='',
        selected=('COUNT(*)',),
        effects='read',
        classes=('cross-period',),
        inert_rule=BLOCKS,
    ),
    AccessContract(
        function='_cctally_record:maybe_update_five_hour_block',
        sql_fingerprint='SELECT MAX(percent_threshold) AS m FROM five_hour_milestones WHERE five_hour_window_key = ? AND reset_event_id = ? AND account_key = ?',
        table='five_hour_milestones',
        keys=('five_hour_window_key', 'reset_event_id', 'account_key'),
        predicate='five_hour_window_key = ? AND reset_event_id = ? AND account_key = ?',
        ordering='',
        selected=('MAX(percent_threshold) AS m',),
        effects='read',
        classes=('period-keyed',),
        inert_rule=BLOCKS,
    ),
    AccessContract(
        function='_cctally_record:maybe_update_five_hour_block',
        sql_fingerprint='SELECT block_cost_usd FROM five_hour_milestones WHERE five_hour_window_key = ? AND percent_threshold = ? AND reset_event_id = ? AND account_key = ?',
        table='five_hour_milestones',
        keys=('five_hour_window_key', 'percent_threshold', 'reset_event_id', 'account_key'),
        predicate='five_hour_window_key = ? AND percent_threshold = ? AND reset_event_id = ? AND account_key = ?',
        ordering='',
        selected=('block_cost_usd',),
        effects='read',
        classes=('period-keyed',),
        inert_rule=BLOCKS,
    ),
    AccessContract(
        function='_cctally_record:maybe_update_five_hour_block',
        sql_fingerprint='SELECT block_start_at FROM five_hour_blocks WHERE five_hour_window_key = ? AND account_key = ?',
        table='five_hour_blocks',
        keys=('five_hour_window_key', 'account_key'),
        predicate='five_hour_window_key = ? AND account_key = ?',
        ordering='',
        selected=('block_start_at',),
        effects='read',
        classes=('period-keyed',),
        inert_rule=BLOCKS,
    ),
    AccessContract(
        function='_cctally_record:maybe_update_five_hour_block',
        sql_fingerprint='SELECT id AS prior_block_id, block_start_at AS block_start_at, five_hour_resets_at AS prior_resets_at, is_closed AS is_closed, journal_id AS journal_id, last_updated_at_utc AS last_updated_at_utc FROM five_hour_blocks WHERE five_hour_window_key = ? AND account_key = ?',
        table='five_hour_blocks',
        keys=('five_hour_window_key', 'account_key'),
        predicate='five_hour_window_key = ? AND account_key = ?',
        ordering='',
        selected=('id AS prior_block_id', 'block_start_at AS block_start_at', 'five_hour_resets_at AS prior_resets_at', 'is_closed AS is_closed', 'journal_id AS journal_id', 'last_updated_at_utc AS last_updated_at_utc'),
        effects='read',
        classes=('period-keyed',),
        inert_rule=BLOCKS,
    ),
    AccessContract(
        function='_cctally_record:maybe_update_five_hour_block',
        sql_fingerprint='SELECT id FROM five_hour_blocks WHERE five_hour_window_key = ? AND account_key = ?',
        table='five_hour_blocks',
        keys=('five_hour_window_key', 'account_key'),
        predicate='five_hour_window_key = ? AND account_key = ?',
        ordering='',
        selected=('id',),
        effects='read',
        classes=('period-keyed',),
        inert_rule=BLOCKS,
    ),
    AccessContract(
        function='_cctally_record:maybe_update_five_hour_block',
        sql_fingerprint='SELECT id FROM five_hour_blocks WHERE id = ? AND is_closed = 1 AND journal_id IS NULL',
        table='five_hour_blocks',
        keys=('id',),
        predicate='id = ? AND is_closed = 1 AND journal_id IS NULL',
        ordering='',
        selected=('id',),
        effects='read',
        classes=('period-keyed',),
        inert_rule=BLOCKS,
    ),
    AccessContract(
        function='_cctally_record:maybe_update_five_hour_block',
        sql_fingerprint='SELECT id FROM five_hour_blocks WHERE is_closed = 1 AND journal_id IS NOT NULL AND account_key = ? AND last_updated_at_utc = ? AND (five_hour_window_key < ? OR unixepoch(five_hour_resets_at) < unixepoch(?))',
        table='five_hour_blocks',
        keys=('account_key', 'last_updated_at_utc'),
        predicate='is_closed = 1 AND journal_id IS NOT NULL AND account_key = ? AND last_updated_at_utc = ? AND (five_hour_window_key < ? OR unixepoch(five_hour_resets_at) < unixepoch(?))',
        ordering='',
        selected=('id',),
        effects='read',
        classes=('range-keyed', 'cross-period'),
        inert_rule=BLOCKS,
    ),
    # #956 class 2 (spec 4.6.3): every block creation retains an opening fact
    # whose fold materializes the open block. The opening's writer and the
    # milestone re-link run inside the creating cycle by exact key; the seed
    # read projects a reading-less window; the coupled convergence and removal
    # run only on derivation-completion and correction convergence paths, by
    # exact physical id or natural key. All are the five-hour block rollup
    # (BLOCKS), a Q7 cost/display family.
    AccessContract(
        function='_cctally_five_hour:select_open_block_end',
        sql_fingerprint='SELECT first_observed_at_utc, five_hour_percent, seven_day_pct_at_block_start FROM five_hour_block_openings WHERE account_key = ? AND five_hour_window_key = ?',
        table='five_hour_block_openings',
        keys=('account_key', 'five_hour_window_key'),
        predicate='account_key = ? AND five_hour_window_key = ?',
        ordering='',
        selected=('first_observed_at_utc', 'five_hour_percent', 'seven_day_pct_at_block_start'),
        effects='read',
        classes=('period-keyed',),
        inert_rule=BLOCKS,
    ),
    AccessContract(
        function='_cctally_journal:_converge_opened_block',
        sql_fingerprint='SELECT * FROM five_hour_block_openings WHERE id = ?',
        table='five_hour_block_openings',
        keys=('id',),
        predicate='id = ?',
        ordering='',
        selected=('*',),
        effects='read',
        classes=('period-keyed',),
        inert_rule=BLOCKS,
    ),
    AccessContract(
        function='_cctally_journal:_converge_opened_block',
        sql_fingerprint='dynamic:_cctally_journal:_converge_opened_block#1',
        sql_shape='sha256:e37cbdd6ab3cc61d',
        table='five_hour_blocks',
        keys=('account_key', 'five_hour_window_key'),
        predicate='account_key = ? AND five_hour_window_key = ? AND is_closed = 0 AND journal_id IS NULL (SET the opening start columns)',
        ordering='',
        selected=(),
        effects='write',
        classes=('period-keyed',),
        inert_rule=BLOCKS,
    ),
    AccessContract(
        function='_cctally_journal:_remove_opened_block',
        sql_fingerprint='DELETE FROM five_hour_blocks WHERE id = ?',
        table='five_hour_blocks',
        keys=('id',),
        predicate='id = ?',
        ordering='',
        selected=(),
        effects='delete',
        classes=('period-keyed',),
        inert_rule=BLOCKS,
    ),
    AccessContract(
        function='_cctally_journal:_remove_opened_block',
        sql_fingerprint='SELECT account_key, five_hour_window_key FROM five_hour_block_openings WHERE id = ?',
        table='five_hour_block_openings',
        keys=('id',),
        predicate='id = ?',
        ordering='',
        selected=('account_key', 'five_hour_window_key'),
        effects='read',
        classes=('period-keyed',),
        inert_rule=BLOCKS,
    ),
    AccessContract(
        function='_cctally_journal:_remove_opened_block',
        sql_fingerprint='SELECT id FROM five_hour_blocks WHERE account_key = ? AND five_hour_window_key = ? AND is_closed = 0 AND journal_id IS NULL',
        table='five_hour_blocks',
        keys=('account_key', 'five_hour_window_key'),
        predicate='account_key = ? AND five_hour_window_key = ? AND is_closed = 0 AND journal_id IS NULL',
        ordering='',
        selected=('id',),
        effects='read',
        classes=('period-keyed',),
        inert_rule=BLOCKS,
    ),
    AccessContract(
        function='_cctally_journal:_remove_opened_block',
        sql_fingerprint='UPDATE five_hour_milestones SET block_id = 0 WHERE account_key = ? AND five_hour_window_key = ?',
        table='five_hour_milestones',
        keys=('account_key', 'five_hour_window_key'),
        predicate='account_key = ? AND five_hour_window_key = ?',
        ordering='',
        selected=(),
        effects='write',
        classes=('period-keyed',),
        inert_rule=BLOCKS,
    ),
    AccessContract(
        function='_cctally_journal:_remove_opened_block',
        sql_fingerprint='dynamic:_cctally_journal:_remove_opened_block#1',
        sql_shape='sha256:4412618cbf5c0995',
        table='dynamic',
        keys=('account_key', 'five_hour_window_key'),
        predicate='account_key = ? AND five_hour_window_key = ? on each `_BLOCK_CHILDREN` table',
        ordering='',
        selected=(),
        effects='delete',
        classes=('period-keyed',),
        inert_rule=BLOCKS,
    ),
    AccessContract(
        function='_cctally_journal:five_hour_windows_touched_by_evt',
        sql_fingerprint='SELECT five_hour_window_key FROM five_hour_blocks WHERE account_key = ? AND is_closed = 0',
        table='five_hour_blocks',
        keys=('account_key',),
        predicate='account_key = ? AND is_closed = 0',
        ordering='',
        selected=('five_hour_window_key',),
        effects='read',
        classes=('cross-period',),
        inert_rule=BLOCKS,
    ),
    AccessContract(
        function='_cctally_record:_insert_five_hour_block_opening',
        sql_fingerprint='INSERT OR IGNORE INTO five_hour_block_openings ( account_key, five_hour_window_key, five_hour_resets_at, block_start_at, first_observed_at_utc, created_at_utc, opening_record_id, creation_kind, five_hour_percent, seven_day_pct_at_block_start, weekly_observation_held, weekly_basis_captured_at_utc ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        table='five_hour_block_openings',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=BLOCKS,
    ),
    AccessContract(
        function='_cctally_record:maybe_update_five_hour_block',
        sql_fingerprint='UPDATE five_hour_milestones SET block_id = ? WHERE account_key = ? AND five_hour_window_key = ? AND block_id = 0',
        table='five_hour_milestones',
        keys=('account_key', 'five_hour_window_key'),
        predicate='account_key = ? AND five_hour_window_key = ? AND block_id = 0',
        ordering='',
        selected=(),
        effects='write',
        classes=('period-keyed',),
        inert_rule=BLOCKS,
    ),
    # #956 class 1 (spec 4.5): the capture-order open-block end projection
    # and its dirty-window marking and flush. Every site is an exact-key or
    # account-scoped read or write of the five-hour block rollup (BLOCKS), or
    # an exact journal-id lookup (JOURNAL).
    AccessContract(
        function='_cctally_five_hour:project_open_block_end',
        sql_fingerprint='DELETE FROM five_hour_block_models WHERE five_hour_window_key = ? AND account_key = ?',
        table='five_hour_block_models',
        keys=('five_hour_window_key', 'account_key'),
        predicate='five_hour_window_key = ? AND account_key = ?',
        ordering='',
        selected=(),
        effects='delete',
        classes=('period-keyed',),
        inert_rule=BLOCKS,
    ),
    AccessContract(
        function='_cctally_five_hour:project_open_block_end',
        sql_fingerprint='DELETE FROM five_hour_block_projects WHERE five_hour_window_key = ? AND account_key = ?',
        table='five_hour_block_projects',
        keys=('five_hour_window_key', 'account_key'),
        predicate='five_hour_window_key = ? AND account_key = ?',
        ordering='',
        selected=(),
        effects='delete',
        classes=('period-keyed',),
        inert_rule=BLOCKS,
    ),
    AccessContract(
        function='_cctally_five_hour:project_open_block_end',
        sql_fingerprint='INSERT INTO five_hour_block_models ( block_id, five_hour_window_key, model, input_tokens, output_tokens, cache_create_tokens, cache_read_tokens, cost_usd, entry_count, account_key ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        table='five_hour_block_models',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=BLOCKS,
    ),
    AccessContract(
        function='_cctally_five_hour:project_open_block_end',
        sql_fingerprint='INSERT INTO five_hour_block_projects ( block_id, five_hour_window_key, project_path, input_tokens, output_tokens, cache_create_tokens, cache_read_tokens, cost_usd, entry_count, account_key ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        table='five_hour_block_projects',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=BLOCKS,
    ),
    AccessContract(
        function='_cctally_five_hour:project_open_block_end',
        sql_fingerprint='SELECT * FROM five_hour_blocks WHERE id = ?',
        table='five_hour_blocks',
        keys=('id',),
        predicate='id = ?',
        ordering='',
        selected=('*',),
        effects='read',
        classes=('period-keyed',),
        inert_rule=BLOCKS,
    ),
    AccessContract(
        function='_cctally_five_hour:project_open_block_end',
        sql_fingerprint='SELECT EXISTS ( SELECT 1 FROM week_reset_events WHERE account_key = ? AND unixepoch(effective_reset_at_utc) BETWEEN unixepoch(?) AND unixepoch(?) ) OR EXISTS ( SELECT 1 FROM weekly_usage_snapshots WHERE week_start_at IS NOT NULL AND account_key = ? AND unixepoch(week_start_at) > unixepoch(?) AND unixepoch(week_start_at) <= unixepoch(?) )',
        table='week_reset_events',
        keys=('account_key',),
        predicate='account_key = ? AND unixepoch(effective_reset_at_utc) BETWEEN unixepoch(?) AND unixepoch(?) ) OR EXISTS ( SELECT 1 FROM weekly_usage_snapshots WHERE week_start_at IS NOT NULL AND account_key = ? AND unixepoch(week_start_at) > unixepoch(?) AND unixepoch(week_start_at) <= unixepoch(?) )',
        ordering='',
        selected=('EXISTS ( SELECT 1',),
        effects='read',
        classes=('range-keyed', 'cross-period'),
        inert_rule=BLOCKS,
    ),
    AccessContract(
        function='_cctally_five_hour:project_open_block_end',
        sql_fingerprint='UPDATE five_hour_blocks SET last_observed_at_utc = ?, final_five_hour_percent = ?, seven_day_pct_at_block_end = ?, total_input_tokens = ?, total_output_tokens = ?, total_cache_create_tokens = ?, total_cache_read_tokens = ?, total_cost_usd = ?, pricing_provenance_json = ?, crossed_seven_day_reset = ?, last_updated_at_utc = ? WHERE id = ? AND is_closed = 0',
        table='five_hour_blocks',
        keys=('id',),
        predicate='id = ? AND is_closed = 0',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=BLOCKS,
    ),
    AccessContract(
        function='_cctally_five_hour:select_open_block_end',
        sql_fingerprint='SELECT unixepoch(captured_at_utc) AS captured_epoch, journal_id, captured_at_utc, five_hour_percent, weekly_percent FROM weekly_usage_snapshots WHERE account_key = ? AND five_hour_window_key = ? AND five_hour_percent IS NOT NULL',
        table='weekly_usage_snapshots',
        keys=('account_key', 'five_hour_window_key'),
        predicate='account_key = ? AND five_hour_window_key = ? AND five_hour_percent IS NOT NULL',
        ordering='',
        selected=('unixepoch(captured_at_utc) AS captured_epoch', 'journal_id', 'captured_at_utc', 'five_hour_percent', 'weekly_percent'),
        effects='read',
        classes=('period-keyed',),
        inert_rule=BLOCKS,
    ),
    AccessContract(
        function='_cctally_journal:five_hour_windows_touched_by_evt',
        sql_fingerprint='SELECT account_key, five_hour_window_key FROM weekly_usage_snapshots WHERE journal_id = ?',
        table='weekly_usage_snapshots',
        keys=('journal_id',),
        predicate='journal_id = ?',
        ordering='',
        selected=('account_key', 'five_hour_window_key'),
        effects='read',
        classes=('period-keyed',),
        inert_rule=JOURNAL,
    ),
    AccessContract(
        function='_cctally_record:_refresh_open_five_hour_block',
        sql_fingerprint='SELECT id, block_start_at, five_hour_resets_at, last_observed_at_utc FROM five_hour_blocks WHERE account_key = ? AND five_hour_window_key = ? AND is_closed = 0',
        table='five_hour_blocks',
        keys=('account_key', 'five_hour_window_key'),
        predicate='account_key = ? AND five_hour_window_key = ? AND is_closed = 0',
        ordering='',
        selected=('id', 'block_start_at', 'five_hour_resets_at', 'last_observed_at_utc'),
        effects='read',
        classes=('period-keyed',),
        inert_rule=BLOCKS,
    ),
    AccessContract(
        function='_cctally_record:mark_account_open_blocks_dirty',
        sql_fingerprint='SELECT five_hour_window_key FROM five_hour_blocks WHERE account_key = ? AND is_closed = 0',
        table='five_hour_blocks',
        keys=('account_key',),
        predicate='account_key = ? AND is_closed = 0',
        ordering='',
        selected=('five_hour_window_key',),
        effects='read',
        classes=('cross-period',),
        inert_rule=BLOCKS,
    ),
    AccessContract(
        function='_cctally_record:maybe_update_five_hour_block',
        sql_fingerprint='INSERT INTO five_hour_blocks ( five_hour_window_key, five_hour_resets_at, block_start_at, first_observed_at_utc, last_observed_at_utc, final_five_hour_percent, seven_day_pct_at_block_start, seven_day_pct_at_block_end, crossed_seven_day_reset, total_input_tokens, total_output_tokens, total_cache_create_tokens, total_cache_read_tokens, total_cost_usd, pricing_provenance_json, is_closed, created_at_utc, last_updated_at_utc, account_key ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?) ON CONFLICT(account_key, five_hour_window_key) DO NOTHING',
        table='five_hour_blocks',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='upsert',
        classes=('singleton',),
        inert_rule=BLOCKS,
    ),
    AccessContract(
        function='_cctally_record:maybe_update_five_hour_block',
        sql_fingerprint='SELECT id, five_hour_window_key FROM five_hour_blocks WHERE is_closed = 0 AND account_key = ? AND (five_hour_window_key < ? OR unixepoch(five_hour_resets_at) < unixepoch(?))',
        table='five_hour_blocks',
        keys=('account_key',),
        predicate='is_closed = 0 AND account_key = ? AND (five_hour_window_key < ? OR unixepoch(five_hour_resets_at) < unixepoch(?))',
        ordering='',
        selected=('id', 'five_hour_window_key'),
        effects='read',
        classes=('range-keyed', 'cross-period'),
        inert_rule=BLOCKS,
    ),
    AccessContract(
        function='_cctally_record:_flag_blocks_spanning_week_resets',
        sql_fingerprint='UPDATE five_hour_blocks SET crossed_seven_day_reset = 1 WHERE crossed_seven_day_reset = 0 AND account_key = ? AND (is_closed = 0 OR journal_id IS NULL) AND ( EXISTS ( SELECT 1 FROM week_reset_events e WHERE e.account_key = five_hour_blocks.account_key AND unixepoch(e.effective_reset_at_utc) BETWEEN unixepoch(five_hour_blocks.block_start_at) AND unixepoch(five_hour_blocks.last_observed_at_utc) ) OR EXISTS ( SELECT 1 FROM weekly_usage_snapshots ws WHERE ws.week_start_at IS NOT NULL AND ws.account_key = five_hour_blocks.account_key AND unixepoch(ws.week_start_at) > unixepoch(five_hour_blocks.block_start_at) AND unixepoch(ws.week_start_at) <= unixepoch(five_hour_blocks.last_observed_at_utc) ) )',
        table='five_hour_blocks',
        keys=('account_key',),
        predicate='crossed_seven_day_reset = 0 AND account_key = ? AND (is_closed = 0 OR journal_id IS NULL) AND ( EXISTS ( SELECT 1 FROM week_reset_events e WHERE e.account_key = five_hour_blocks.account_key AND unixepoch(e.effective_reset_at_utc) BETWEEN unixepoch(five_hour_blocks.block_start_at) AND unixepoch(five_hour_blocks.last_observed_at_utc) ) OR EXISTS ( SELECT 1 FROM weekly_usage_snapshots ws WHERE ws.week_start_at IS NOT NULL AND ws.account_key = five_hour_blocks.account_key AND unixepoch(ws.week_start_at) > unixepoch(five_hour_blocks.block_start_at) AND unixepoch(ws.week_start_at) <= unixepoch(five_hour_blocks.last_observed_at_utc) ) )',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton', 'range-keyed'),
        inert_rule=BLOCKS,
    ),
    AccessContract(
        function='_cctally_record:maybe_update_five_hour_block',
        sql_fingerprint='UPDATE five_hour_blocks SET is_closed = 1, last_updated_at_utc = ? WHERE five_hour_window_key = ? AND account_key = ? AND is_closed = 0 AND unixepoch(five_hour_resets_at) < unixepoch(?)',
        table='five_hour_blocks',
        keys=('five_hour_window_key', 'account_key'),
        predicate='five_hour_window_key = ? AND account_key = ? AND is_closed = 0 AND unixepoch(five_hour_resets_at) < unixepoch(?)',
        ordering='',
        selected=(),
        effects='write',
        classes=('period-keyed', 'range-keyed'),
        inert_rule=BLOCKS,
    ),
    AccessContract(
        function='_cctally_record:maybe_update_five_hour_block',
        sql_fingerprint='UPDATE five_hour_blocks SET is_closed = 1, last_updated_at_utc = ? WHERE is_closed = 0 AND account_key = ? AND five_hour_window_key < ?',
        table='five_hour_blocks',
        keys=('account_key',),
        predicate='is_closed = 0 AND account_key = ? AND five_hour_window_key < ?',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton', 'range-keyed'),
        inert_rule=BLOCKS,
    ),
    AccessContract(
        function='_cctally_record:maybe_update_five_hour_block',
        sql_fingerprint='UPDATE five_hour_blocks SET is_closed = 1, last_updated_at_utc = ? WHERE is_closed = 0 AND account_key = ? AND unixepoch(five_hour_resets_at) < unixepoch(?)',
        table='five_hour_blocks',
        keys=('account_key',),
        predicate='is_closed = 0 AND account_key = ? AND unixepoch(five_hour_resets_at) < unixepoch(?)',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton', 'range-keyed'),
        inert_rule=BLOCKS,
    ),
    AccessContract(
        function='_cctally_record:maybe_update_five_hour_block',
        sql_fingerprint='UPDATE five_hour_milestones SET alerted_at = ? WHERE five_hour_window_key = ? AND percent_threshold = ? AND reset_event_id = ? AND account_key = ? AND alerted_at IS NULL',
        table='five_hour_milestones',
        keys=('five_hour_window_key', 'percent_threshold', 'reset_event_id', 'account_key'),
        predicate='five_hour_window_key = ? AND percent_threshold = ? AND reset_event_id = ? AND account_key = ? AND alerted_at IS NULL',
        ordering='',
        selected=(),
        effects='write',
        classes=('period-keyed',),
        inert_rule=BLOCKS,
    ),
    AccessContract(
        function='_cctally_record:resolve_five_hour_reset_event',
        sql_fingerprint='SELECT detected_at_utc, effective_reset_at_utc, id, prior_percent, post_percent FROM five_hour_reset_events WHERE five_hour_window_key = ? AND account_key = ?',
        table='five_hour_reset_events',
        keys=('five_hour_window_key', 'account_key'),
        predicate='five_hour_window_key = ? AND account_key = ?',
        ordering='',
        selected=('detected_at_utc', 'effective_reset_at_utc', 'id', 'prior_percent', 'post_percent'),
        effects='read',
        classes=('period-keyed',),
        inert_rule=None,
    ),
    # #943 T3 (spec 4.7): the boundary resolver's companion lookup, by the
    # selected credit's natural key; the caller accepts the boundary only
    # when its `credit_revision` equals the credit's revision.
    AccessContract(
        function='_cctally_record:_five_hour_credit_companion',
        sql_fingerprint='SELECT boundary_at_utc, credit_revision FROM five_hour_credit_boundaries WHERE account_key = ? AND five_hour_window_key = ? AND effective_reset_at_utc = ?',
        table='five_hour_credit_boundaries',
        keys=('account_key', 'five_hour_window_key', 'effective_reset_at_utc'),
        predicate='account_key = ? AND five_hour_window_key = ? AND effective_reset_at_utc = ?',
        ordering='',
        selected=('boundary_at_utc', 'credit_revision'),
        effects='read',
        classes=('period-keyed',),
        inert_rule=None,
    ),
    AccessContract(
        function='_cctally_statusline:_tokens_from',
        sql_fingerprint='SELECT record_path, started_at_utc, stamped_at_utc FROM stats_publication_stamp ORDER BY rowid',
        table='stats_publication_stamp',
        keys=(),
        predicate='',
        ordering='rowid',
        selected=('record_path', 'started_at_utc', 'stamped_at_utc'),
        effects='read',
        classes=('cross-period',),
        inert_rule=WAIT,
    ),
    AccessContract(
        function='_cctally_statusline:_tokens_from',
        sql_fingerprint='SELECT segment, offset, applied_segment, applied_offset FROM journal_cursor WHERE id = 1',
        table='journal_cursor',
        keys=(),
        predicate='id = 1',
        ordering='',
        selected=('segment', 'offset', 'applied_segment', 'applied_offset'),
        effects='read',
        classes=('cross-period',),
        inert_rule=WAIT,
    ),
    AccessContract(
        function='_cctally_statusline:_tokens_from',
        sql_fingerprint='dynamic:_cctally_statusline:_tokens_from#1',
        sql_shape='sha256:3792420114949f41',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=WAIT,
    ),
    AccessContract(
        function='_cctally_store:apply_connection_policy',
        sql_fingerprint='dynamic:_cctally_store:apply_connection_policy#1',
        sql_shape='sha256:1c25e1344c04db44',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_store:apply_connection_policy',
        sql_fingerprint='dynamic:_cctally_store:apply_connection_policy#2',
        sql_shape='sha256:32c028acaaf363ab',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_store:apply_connection_policy',
        sql_fingerprint='dynamic:_cctally_store:apply_connection_policy#3',
        sql_shape='sha256:2f23eec2c9c63bb2',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_store:apply_policy',
        sql_fingerprint='dynamic:_cctally_store:apply_policy#1',
        sql_shape='sha256:f14edcbecbaa2e1d',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_store:apply_policy',
        sql_fingerprint='dynamic:_cctally_store:apply_policy#2',
        sql_shape='sha256:492dde2a4628ee0f',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_store:apply_writer_temp_store',
        sql_fingerprint='dynamic:_cctally_store:apply_writer_temp_store#1',
        sql_shape='sha256:37cd251c8e8e9040',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=MAINT,
    ),
    # #997 T-A1 (spec D1, D6): the disposable scratch policy applied to the
    # completion replay copies, the owed-outputs predecessor and the rederive
    # scratch, never to a published index. It probes for the usage table and,
    # when present, creates the three partial `replay_usage_*` indexes on that
    # scratch (`_REPLAY_SCRATCH_INDEXES`, the dynamic site). Both run on a
    # private scratch before its replay, never inside a live automatic
    # decision; an index changes no result set, and the order-insensitivity of
    # every statement it can serve is audited in the #997 plan's appendix and
    # pinned by `tests/test_997_replay_indexes.py`.
    AccessContract(
        function='_cctally_store:apply_disposable_scratch_policy',
        sql_fingerprint="SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'weekly_usage_snapshots'",
        table='weekly_usage_snapshots',
        keys=(),
        predicate="type = 'table' AND name = 'weekly_usage_snapshots'",
        ordering='',
        selected=('1',),
        effects='read',
        classes=('cross-period',),
        inert_rule=MAINT,
    ),
    # #1003 D2 (base bfc595fe9): the policy's own page-cache pragma,
    # `PRAGMA cache_size=-{DISPOSABLE_SCRATCH_CACHE_SIZE_KIB}` (64 MiB), is
    # now the first dynamic site, so the replay-index loop moved to #2 with
    # its contract and shape unchanged apart from the producer (and the
    # constant the producer now names). The pragma sets the private scratch
    # connection's cache, before its replay, beside the static
    # `journal_mode=MEMORY` and `synchronous=OFF` pragmas of the same policy;
    # it reads and writes no table and changes no result set, and it never
    # runs on a published index or inside a live automatic decision.
    AccessContract(
        function='_cctally_store:apply_disposable_scratch_policy',
        sql_fingerprint='dynamic:_cctally_store:apply_disposable_scratch_policy#1',
        sql_shape='sha256:2260b6be0f086524',
        table='dynamic',
        keys=(),
        predicate='dynamic PRAGMA cache_size on the disposable scratch connection; no table',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_store:apply_disposable_scratch_policy',
        sql_fingerprint='dynamic:_cctally_store:apply_disposable_scratch_policy#2',
        sql_shape='sha256:94c0a37bd46dce8c',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_cctally_sync_week:_cmd_sync_week_via_ingest',
        sql_fingerprint='SELECT id, week_start_date, week_end_date, week_start_at, week_end_at, range_start_iso, range_end_iso, cost_usd, mode FROM weekly_cost_snapshots WHERE journal_id LIKE ? ORDER BY id DESC LIMIT 1',
        table='weekly_cost_snapshots',
        keys=(),
        predicate='journal_id LIKE ?',
        ordering='id DESC',
        selected=('id', 'week_start_date', 'week_end_date', 'week_start_at', 'week_end_at', 'range_start_iso', 'range_end_iso', 'cost_usd', 'mode'),
        effects='read',
        classes=('cross-period',),
        inert_rule=Q7,
    ),
    AccessContract(
        function='_cctally_weekrefs:_apply_reset_events_to_weekrefs',
        sql_fingerprint='dynamic:_cctally_weekrefs:_apply_reset_events_to_weekrefs#1',
        sql_shape='sha256:ef62f4486cc766b0',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=Q7,
    ),
    AccessContract(
        # #825/#832 fix round C (CD2-003): the canonical weekly boundary is
        # NOT a cost-only read: `_usage_snapshot_columns` stamps the returned
        # `week_start_at`/`week_end_at` into the accepted usage snapshot a
        # strict decision reads. The account predicate is spliced in (an
        # `account_key` is always supplied on this path), so the site is
        # dynamic; its contract is the real one, held by `sql_shape`.
        function='_cctally_weekrefs:_get_canonical_boundary_for_date',
        sql_fingerprint='dynamic:_cctally_weekrefs:_get_canonical_boundary_for_date#1',
        sql_shape='sha256:8e47b9024179d8d0',
        table='weekly_usage_snapshots',
        keys=('week_start_date', 'account_key'),
        predicate="week_start_date = ? AND week_start_at IS NOT NULL AND week_start_at != '' AND week_end_at IS NOT NULL AND week_end_at != '' AND account_key = ?",
        ordering='captured_at_utc ASC, id ASC',
        selected=('week_start_at', 'week_end_at'),
        effects='read',
        classes=('period-keyed',),
        inert_rule=None,
    ),
    AccessContract(
        function='_cctally_weekrefs:_week_ref_has_reset_event',
        sql_fingerprint='dynamic:_cctally_weekrefs:_week_ref_has_reset_event#1',
        sql_shape='sha256:5087166fa6bd0e2e',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=Q7,
    ),
    AccessContract(
        function='_lib_five_hour:latest_five_hour_reading',
        sql_fingerprint='dynamic:_lib_five_hour:latest_five_hour_reading#1',
        sql_shape='sha256:c61db33f7457ea09',
        table='weekly_usage_snapshots',
        keys=('week_start_date', 'account_key'),
        predicate=('latest five-hour reading of ONE week date key and account '
                   '(the no-change ingest derivation, #825/#832 spec 4.7 '
                   'revision 7)'),
        ordering='captured_at_utc DESC, id DESC',
        selected=('id', 'account_key', 'captured_at_utc', 'five_hour_percent',
                  'five_hour_resets_at', 'five_hour_window_key',
                  'weekly_observation_held'),
        effects='read',
        classes=('period-keyed',),
        inert_rule=None,
    ),
    # #1050 T2: the content-revision kernel's installer (spec 4.3), reached
    # from every `open_db` path. Each statement creates, seeds or repairs the
    # kernel's own objects under the open-time maintenance guard; none is read
    # by a detector decision.
    AccessContract(
        function='_lib_stats_content_revision:_inspect',
        sql_fingerprint='SELECT singleton, format_version, domain_generation, seq FROM stats_content_revision LIMIT 2',
        table='stats_content_revision',
        keys=('singleton',),
        predicate='',
        ordering='',
        selected=('singleton', 'format_version', 'domain_generation', 'seq'),
        effects='read',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_lib_stats_content_revision:_install',
        sql_fingerprint='CREATE INDEX idx_five_hour_milestones_usage_snapshot_id ON five_hour_milestones(usage_snapshot_id)',
        table='five_hour_milestones',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_lib_stats_content_revision:_install',
        sql_fingerprint='CREATE TABLE stats_content_revision (singleton INTEGER PRIMARY KEY CHECK (singleton = 1), format_version INTEGER NOT NULL CHECK (format_version >= 1), domain_generation INTEGER NOT NULL DEFAULT 0 CHECK (domain_generation >= 0), seq INTEGER NOT NULL DEFAULT 0 CHECK (seq >= 0))',
        table='stats_content_revision',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_lib_stats_content_revision:_install',
        sql_fingerprint="CREATE TABLE stats_content_revision_scopes (kind TEXT NOT NULL CHECK (kind IN ('cohort', 'bucket', 'block', 'reset_point')), account_key TEXT NOT NULL, scope_key TEXT NOT NULL, start_epoch INTEGER, end_epoch INTEGER, revision INTEGER NOT NULL, PRIMARY KEY (kind, account_key, scope_key))",
        table='stats_content_revision_scopes',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_lib_stats_content_revision:_install',
        sql_fingerprint='INSERT INTO stats_content_revision (singleton, format_version, domain_generation, seq) VALUES (1, ?, 0, 0)',
        table='stats_content_revision',
        keys=(),
        predicate='',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_lib_stats_content_revision:_install',
        sql_fingerprint='UPDATE stats_content_revision SET format_version = ?, domain_generation = ?, seq = ? WHERE singleton = 1',
        table='stats_content_revision',
        keys=('singleton',),
        predicate='singleton = 1',
        ordering='',
        selected=(),
        effects='write',
        classes=('singleton',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_lib_stats_content_revision:_install',
        sql_fingerprint='dynamic:_lib_stats_content_revision:_install#1',
        sql_shape='sha256:15b28df089bc0407',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_lib_stats_content_revision:_install',
        sql_fingerprint='dynamic:_lib_stats_content_revision:_install#2',
        sql_shape='sha256:96968b8250f530b2',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_lib_stats_content_revision:_install',
        sql_fingerprint='dynamic:_lib_stats_content_revision:_install#3',
        sql_shape='sha256:2e1fd5db25812db9',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_lib_stats_content_revision:ensure_stats_content_revision',
        sql_fingerprint='dynamic:_lib_stats_content_revision:ensure_stats_content_revision#1',
        sql_shape='sha256:7451a814fc141b51',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=MAINT,
    ),
    AccessContract(
        function='_lib_stats_content_revision:ensure_stats_content_revision',
        sql_fingerprint='dynamic:_lib_stats_content_revision:ensure_stats_content_revision#2',
        sql_shape='sha256:b312dbf76e573326',
        table='dynamic',
        keys=(),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('cross-period',),
        inert_rule=MAINT,
    ),
    # #1050 T3: the identical child-set check of the open-block projection
    # and the block-close fold/convergence. `SELECT * FROM {table}` over
    # `CHILD_TABLES` (the two five-hour block child tables) by the exact
    # window, and account when the caller's DELETE names one: the same
    # period-keyed rows the caller would replace, read only to skip a
    # rewrite that would store identical rows (BLOCKS).
    # Reviewed re-pin (#1050 T3 fix, base 7602973ef): only the binding
    # `_CHILD_COLUMNS` changed, dropping its `ORDER BY cid` as above; this
    # site's table set, keys, predicate and read-only effect are unchanged.
    AccessContract(
        function='_lib_stats_content_revision:child_set_unchanged',
        sql_fingerprint='dynamic:_lib_stats_content_revision:child_set_unchanged#1',
        sql_shape='sha256:2ff8bd5b011e00b5',
        table='dynamic',
        keys=('five_hour_window_key', 'account_key'),
        predicate='dynamic SQL',
        ordering='',
        selected=(),
        effects='read',
        classes=('period-keyed',),
        inert_rule=BLOCKS,
    ),
)


def non_inert_cross_period(registry=None) -> "tuple[AccessContract, ...]":
    """Every registered cross-period READ without an inert rule."""
    entries = REGISTRY if registry is None else registry
    return tuple(c for c in entries
                 if "cross-period" in c.classes and c.effects == "read"
                 and c.inert_rule is None)
