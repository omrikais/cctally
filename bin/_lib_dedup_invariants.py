"""The one implementation of R-DEDUP1 through R-DEDUP5 (#529 S4 F12).

Ported verbatim from the embedded heredoc that used to live at
``bin/cctally-reconcile-test:2441-3097``. Each invariant keeps the query and the
comparison it had there: **R-DEDUP1 compares exact integer token maxima; only
R-DEDUP2 through R-DEDUP5 use the 1e-9 USD tolerance.** Confirmed against the
block before porting, and stated here because the two are easy to conflate.

Two modes
=========

**Strict** takes a fixture root and a pinned ``as_of``, and is what
``bin/cctally-reconcile-test`` calls. It preserves the S4 corpus contract:
R-DEDUP2 through R-DEDUP5 compare migration output with a recomputation over the
same generated cache. Every database must open, every invariant must inspect at
least one eligible row, and a missing database, absent row, cache drift or
unapplied migration is a failure. This keeps the 76-case reconcile gate and its
five-by-five mutation diagonal unchanged.

**Live** is the real-store diagnostic contract. R-DEDUP1 still compares raw
emissions with the deduped cache. R-DEDUP2, R-DEDUP4 and R-DEDUP5 compare the
disposable stats.db row with the selected retained journal event that materialized
it. R-DEDUP5 additionally checks the physical reset row against the event's
logical ``reset_event_ref``, so post-credit cumulative costs remain segment
facts. R-DEDUP3 is the one mutable projection: it recomputes the open block only
through that row's own ``last_observed_at_utc``. Missing/unopenable stores and
absent eligible rows still degrade to incomplete evidence; journal or projection
disagreement is a failure.

Why the checked count is not enough
===================================

A checked count above zero is satisfiable over a strict-mode window containing
no ``session_entries``, because a stored zero equals a recomputed zero. Strict
mode therefore also records the number of source rows consumed and the magnitude
produced, and requires both to be positive. In live mode a retained event counts
as one source row; the open projection reports its recomputed cache-row count.

What the five invariants actually observe
=========================================

Over the generated corpus in strict mode, R-DEDUP2 through R-DEDUP5 call
``_calculate_entry_cost`` on one side of the comparison and read a value
migrations 008/009/010 produced by calling the same function on the other. They
prove that the migration ran and that both sides agree on the window bounds;
they do not price anything independently. **R-DEDUP1 is the only invariant that
observes deduplication itself**. Live mode deliberately reclassifies the other
checks as replay-fidelity/projection checks rather than pretending they are
independent dedup proofs.

The recomputation prices exactly as the migrations did
======================================================

R-DEDUP2 through R-DEDUP5 recompute through ``claude_usage_dict`` over the same
projection migrations 008/009/010 read (``bin/_cctally_db.py:7808-7830``,
``8104-8141``, ``8388-8414``), including the ``usage_extra_json`` ``speed``
fallback those migrations apply when the materialized column is NULL.

The port originally recomputed from four flat token columns and therefore
dropped two the migrations read:

* ``session_entries.cache_create_1h_tokens`` (#195). The migration splits cache
  creation by TTL and prices the 1-hour portion at twice the input rate; the
  four-column form priced the whole quantity at the 5-minute rate.
* ``session_entries.speed``. ``_calculate_entry_cost`` multiplies the total when
  it reads ``"fast"`` (``bin/_lib_pricing.py:953-956``).

That was not only a violation of the CLAUDE.md rule that every cost-feeding
usage dict go through ``claude_usage_dict`` and every cost SELECT over
``session_entries`` fetch ``cache_create_1h_tokens``. It was measurably wrong.
On the corpus's own ``msg_a1`` row the four-column form returns 0.110435, the
1-hour split returns 0.116060, and ``speed='fast'`` returns 0.662610, because
``_claude_fast_multiplier('claude-opus-4-7')`` is 6.0. On the maintainer's store
155,703 of 409,222 entries carry a non-zero split, so the two forms differ over
most windows there.

**Why live mode does not reuse that comparison.** Measured read-only
on 2026-08-11: of 30 sampled ``weekly_cost_snapshots`` windows, 28 match the
four-column price to within 1e-9 and only 1 matches the nine-column price;
closed blocks give 23 against 6, milestones 18 against 2. The stored values are
journaled facts replayed into stats.db, priced as the live writer priced them
when it wrote them, so the four-column form was agreeing by coincidence with a
history of pre-#195 prices rather than by being right. The reason to close the
divergence is that CLAUDE.md's #195 rule is not optional and that the
nine-column form is the correct price of the cache as it stands — not that it
reconciles the two sides. No drift predicate can. Issue #543 therefore keeps the
recomputation in strict fixture mode and makes live R-DEDUP2/4/5 compare the
materialized row with its selected retained event instead.

The ``usage_extra_json`` fallback is dead on any current store:
``bin/_cctally_cache.py:4124`` has written that column as NULL since #181. It is
kept because the migrations keep it, so the two sides agree on a store old
enough to still hold the blob. Measured read-only against the maintainer's store
on 2026-08-11: 409,069 ``session_entries`` rows, 0 carrying a non-NULL
``usage_extra_json``, 155,688 (38%) carrying a non-zero
``cache_create_1h_tokens``, and 1 carrying ``speed = 'fast'``.

The generated corpus stays free of both shapes — ``bin/build-dedup-fixtures.py``
calls ``emit_streaming_pair`` without ``cache_1h_tokens``, and that helper writes
``speed="standard"``, whose multiplier is 1.0 — so closing the divergence moves
no fixture value. That is a fact about the corpus, not a licence for the
comparison to diverge: the suite cannot observe either axis, which is why the
divergence had to be measured against a real store instead.

One projection, two readers
===========================

``_ENTRY_PROJECTION`` is the only place this module names ``session_entries``
columns. R-DEDUP1 reads its per-key row through the same projection and sums the
four token columns in Python, so a column the cost path later needs cannot be
added to one column list and missed in the other.

Public in the mirror, because ``bin/cctally-reconcile-test`` imports it and a
public checkout must be able to run its own suite. Absent from ``package.json``
``files[]`` for the same reason ``bin/_lib_test_evidence.py`` is: an npm user
installs the CLI, not the repository's harnesses.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import pathlib
import sqlite3
import sys
from collections import defaultdict

INVARIANT_NAMES = (
    "R-DEDUP1", "R-DEDUP2", "R-DEDUP3", "R-DEDUP4", "R-DEDUP5",
)

# The migration whose output each invariant reconciles. R-DEDUP1 reconciles the
# cache-side dedup, so its dependency is cache.db's 001; R-DEDUP3 and R-DEDUP4
# share 009, which walks every block, active and closed.
_REQUIRED_MIGRATION = {
    "R-DEDUP1": ("cache", "001_dedup_highest_wins"),
    "R-DEDUP2": ("stats", "008_recompute_weekly_cost_snapshots_dedup_fix"),
    "R-DEDUP3": ("stats", "009_recompute_five_hour_blocks_dedup_fix"),
    "R-DEDUP4": ("stats", "009_recompute_five_hour_blocks_dedup_fix"),
    "R-DEDUP5": ("stats", "010_recompute_percent_milestones_dedup_fix"),
}

# The sample bound the heredoc applied, kept as-is: enough to surface a
# `_should_replace` regression, because any single multi-emission key with a
# below-max cached row fails.
_R1_SAMPLE = 100

# The nine columns migrations 008/009/010 read, in their order. The two the
# original port dropped — `cache_create_1h_tokens` and `speed` — are what make
# this side of the comparison price the way the side that wrote the stored value
# priced. Both readers below build their statement from this one string.
_ENTRY_PROJECTION = (
    "SELECT model, input_tokens, output_tokens, "
    "       cache_create_tokens, cache_read_tokens, "
    "       cache_create_1h_tokens, speed, "
    "       usage_extra_json, cost_usd_raw "
    "  FROM session_entries "
)

_ENTRY_BY_WINDOW = _ENTRY_PROJECTION + (
    " WHERE timestamp_utc >= ? "
    "   AND timestamp_utc <= ?"
)

_ENTRY_BY_KEY = _ENTRY_PROJECTION + " WHERE msg_id = ? AND req_id = ?"

_pricing_module = None


def _pricing():
    """Bind ``_lib_pricing`` on first use.

    Loaded by path rather than by name so this module works from a harness that
    has not put ``bin/`` on ``sys.path``, and deferred so importing the module
    costs nothing.
    """
    global _pricing_module
    if _pricing_module is not None:
        return _pricing_module
    module = sys.modules.get("_lib_pricing")
    if module is None:
        import importlib.machinery
        import importlib.util

        path = pathlib.Path(__file__).resolve().parent / "_lib_pricing.py"
        loader = importlib.machinery.SourceFileLoader("_lib_pricing", str(path))
        spec = importlib.util.spec_from_loader("_lib_pricing", loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules["_lib_pricing"] = module
        loader.exec_module(module)
    _pricing_module = module
    return module


@dataclasses.dataclass
class Result:
    """What ran, over how much, and what it found."""

    checked: dict = dataclasses.field(default_factory=dict)
    source_counts: dict = dataclasses.field(default_factory=dict)
    recomputed_magnitude: dict = dataclasses.field(default_factory=dict)
    failures: list = dataclasses.field(default_factory=list)
    skips: list = dataclasses.field(default_factory=list)

    def __post_init__(self):
        for name in INVARIANT_NAMES:
            self.checked.setdefault(name, 0)
            self.source_counts.setdefault(name, 0)
            self.recomputed_magnitude.setdefault(name, 0.0)

    def executed(self) -> set:
        """The invariant names that inspected at least one eligible row."""
        return {n for n, count in self.checked.items() if count > 0}

    @property
    def ok(self) -> bool:
        return not self.failures

    def render(self) -> list:
        """One human-readable line per invariant, in name order."""
        lines = []
        for name in INVARIANT_NAMES:
            failed = [f for f in self.failures if f.startswith(name)]
            skipped = [s for s in self.skips if s.startswith(name)]
            if failed:
                lines.extend(f"  {text}" for text in failed)
            elif skipped:
                lines.extend(f"  {text}" for text in skipped)
            else:
                lines.append(
                    f"  {name}: ok ({self.checked[name]} checked over "
                    f"{self.source_counts[name]} source rows)")
        return lines


class _Context:
    """One check run: the roots, the mode, and the accumulating Result."""

    def __init__(self, root, as_of, strict):
        self.root = pathlib.Path(root)
        self.strict = strict
        self.as_of = as_of
        share = self.root / ".local" / "share" / "cctally"
        self.stats_path = share / "stats.db"
        self.cache_path = share / "cache.db"
        self.claude_projects = self.root / ".claude" / "projects"
        self.result = Result()
        # `applied_at_utc` of the migration the invariant last asked about; the
        # cache-drift baseline reads it. `migration_applied` clears it on entry,
        # so a failed lookup cannot leave the previous invariant's value behind
        # for the next one to read as its own baseline.
        self._applied_at = None

    # -- reporting ---------------------------------------------------------
    def degrade(self, name: str, message: str) -> None:
        """Record a condition live mode tolerates and strict mode refuses."""
        text = f"{name}: {message}"
        if self.strict:
            self.result.failures.append(text)
        else:
            self.result.skips.append(f"{text} (skipped)")

    def fail(self, name: str, message: str) -> None:
        self.result.failures.append(f"{name}: {message}")

    def note(self, name: str, checked: int, sources: int, magnitude: float) -> None:
        self.result.checked[name] += checked
        self.result.source_counts[name] += sources
        self.result.recomputed_magnitude[name] = max(
            self.result.recomputed_magnitude[name], abs(magnitude))

    # -- stores ------------------------------------------------------------
    def open_ro(self, name: str, *paths):
        """Open each path read-only, or degrade and return None.

        A concurrent cctally writer can leave a database momentarily
        un-openable even when the path exists, so a `mode=ro` connect raises
        `sqlite3.OperationalError: unable to open database file`. Live mode
        treats that as a skip so the diagnostic stays usable during active use;
        strict mode treats it as a failure, because nothing writes to a
        generated corpus.
        """
        conns = []
        for path in paths:
            if not path.exists():
                for conn in conns:
                    conn.close()
                self.degrade(name, f"{path.name} is not present at {path}")
                return None
            try:
                conns.append(sqlite3.connect(f"file:{path}?mode=ro", uri=True))
            except sqlite3.OperationalError as exc:
                for conn in conns:
                    conn.close()
                self.degrade(
                    name,
                    f"{path.name} not openable — concurrent writer? {exc}")
                return None
        return conns[0] if len(conns) == 1 else tuple(conns)

    def migration_applied(self, name: str, stats, cache) -> bool:
        """True when the migration this invariant reconciles has applied.

        Strict mode records a failure when it has not, because the stored values
        would then still be the pre-dedup ones and the invariant would be
        reconciling a shape nobody has corrected.
        """
        self._applied_at = None
        label, migration = _REQUIRED_MIGRATION[name]
        conn = stats if label == "stats" else cache
        try:
            row = conn.execute(
                "SELECT applied_at_utc FROM schema_migrations WHERE name = ?",
                (migration,)).fetchone()
        except sqlite3.OperationalError as exc:
            self.degrade(name, f"{label}.db has no schema_migrations ({exc})")
            return False
        if row is None:
            self.degrade(
                name, f"migration {migration} has not applied to {label}.db")
            return False
        self._applied_at = row[0]
        return True

    def drifted(self, name: str, cache, baseline: str) -> bool:
        """Strict-corpus guard for ingest after a migration recomputation.

        Nothing writes to the generated corpus between build and validation,
        so a later ``session_files.last_ingested_at`` means the fixture's stored
        migration output is no longer comparable with its cache. Live replay
        checks do not call this method: durable facts compare with their journal
        events, while R-DEDUP3 compares the current open projection directly.
        """
        stale = cache.execute(
            "SELECT 1 FROM session_files WHERE last_ingested_at > ? LIMIT 1",
            (baseline,)).fetchone()
        if stale is None:
            return False
        if self.strict:
            self.fail(
                name,
                f"cache drift — a session file was ingested after the "
                f"recompute baseline {baseline}")
        return True

    # -- recomputation -----------------------------------------------------
    def recompute(self, cache, start_utc: str, end_utc: str):
        """Sum the entry cost over a closed UTC interval; return (cost, rows).

        Matches the production reader's predicate: `iter_entries` converts both
        bounds to UTC ISO and runs the closed-interval lexical compare against
        `session_entries.timestamp_utc`, which is stored in UTC.

        The per-row body is migrations 008/009/010's body verbatim, down to the
        `usage_extra_json` speed fallback, so a divergence reported here is a
        divergence in the stored value rather than in how the two sides price.
        """
        pricing = _pricing()
        rows = cache.execute(_ENTRY_BY_WINDOW, (start_utc, end_utc)).fetchall()
        total = 0.0
        for model, i, o, cc, cr, cc1h, speed, extras_json, raw in rows:
            if speed is None and extras_json:
                speed = json.loads(extras_json).get("speed")
            usage = pricing.claude_usage_dict(   # #195 chokepoint
                input_tokens=i, output_tokens=o,
                cache_creation_tokens=cc, cache_read_tokens=cr,
                cache_1h_tokens=cc1h, speed=speed)
            total += pricing._calculate_entry_cost(
                model, usage, mode="auto", cost_usd=raw)
        return total, len(rows)

    # -- retained journal facts -------------------------------------------
    def selected_payload(
        self, name: str, stats, journal_id: str, expected_kind: str,
    ) -> "dict | None":
        """Resolve one materialized row's selected retained event payload."""
        if not journal_id:
            self.fail(name, "materialized durable row has no journal_id")
            return None
        try:
            row = stats.execute(
                "SELECT status, event_json FROM journal_effective_events "
                "WHERE event_id = ?", (journal_id,),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            self.fail(name, f"effective-event selector unavailable: {exc}")
            return None
        if row is None:
            self.fail(
                name,
                f"journal_id={journal_id} has no selected retained event")
            return None
        status, raw = row
        if status != "active":
            self.fail(
                name,
                f"journal_id={journal_id} selects {status}, not active")
            return None
        try:
            record = json.loads(raw)
        except (TypeError, ValueError):
            self.fail(name, f"journal_id={journal_id} has invalid event_json")
            return None
        if record.get("t") != "evt" or record.get("id") != journal_id:
            self.fail(name, f"journal_id={journal_id} resolved to the wrong event")
            return None
        payload = record.get("payload")
        if not isinstance(payload, dict) or payload.get("kind") != expected_kind:
            actual = payload.get("kind") if isinstance(payload, dict) else None
            self.fail(
                name,
                f"journal_id={journal_id} expected kind={expected_kind}, "
                f"found {actual!r}")
            return None
        return payload

    def replay_cost(
        self, name: str, stats, journal_id: str, expected_kind: str,
        field: str, stored: float, detail: str,
    ) -> "dict | None":
        """Compare a materialized durable cost with its selected event."""
        payload = self.selected_payload(name, stats, journal_id, expected_kind)
        if payload is None:
            return None
        retained = payload.get(field)
        if isinstance(retained, bool) or not isinstance(retained, (int, float)):
            self.fail(
                name,
                f"journal_id={journal_id} has non-numeric {field}={retained!r}")
            return None
        retained = float(retained)
        if abs(float(stored) - retained) >= 1e-9:
            self.fail(
                name,
                f"{detail} stored=${float(stored):.9f} "
                f"journal=${retained:.9f}")
            return None
        self.note(name, 1, 1, retained)
        return payload


def _utc(value: str) -> str:
    """Normalize an ISO timestamp to UTC — but read a NAIVE one as host-local.

    `astimezone` on a naive datetime attaches the host's zone before converting,
    so a naive input is not read as UTC. Every value read out of the two
    databases carries an offset, and R-DEDUP5's date-only fallback appends
    `T00:00:00+00:00`, so the stored side is unaffected. `--as-of` is the one
    input that can arrive naive: `bin/cctally-dedup-audit` validates it with
    `fromisoformat`, which accepts `2026-04-27T12:00:00`, and that script pins no
    `TZ`, so such a value is taken in the operator's zone. Strict mode is not
    exposed — `bin/cctally-reconcile-test` passes the builder's `AS_OF`, which
    ends in `Z`, and runs under `TZ=Etc/UTC`.
    """
    return dt.datetime.fromisoformat(
        value.replace("Z", "+00:00")
    ).astimezone(dt.timezone.utc).isoformat()


# ── R-DEDUP1 ───────────────────────────────────────────────────────────────
def _r_dedup1(ctx: _Context) -> None:
    """`session_entries` must hold the MAX token total per (msg_id, req_id).

    Exact integer comparison, not the USD tolerance: this reconciles the dedup
    decision itself against the raw emissions, and the two sides are counts.
    """
    name = "R-DEDUP1"
    if not ctx.claude_projects.exists():
        ctx.degrade(name, f"no projects directory at {ctx.claude_projects}")
        return
    if not ctx.cache_path.exists():
        ctx.degrade(name, f"cache.db is not present at {ctx.cache_path}")
        return

    max_totals = defaultdict(int)
    emission_counts = defaultdict(int)
    for path in sorted(ctx.claude_projects.glob("**/*.jsonl")):
        try:
            handle = path.open()
        except OSError:
            continue
        with handle:
            for line in handle:
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if obj.get("type") != "assistant":
                    continue
                message = obj.get("message") or {}
                usage = message.get("usage") or {}
                # Mirror the ingest path's `<synthetic>` drop
                # (`_parse_usage_entries` / `_iter_jsonl_entries_with_offsets`,
                # matching ccusage claude_loader.rs:454). `session_entries`
                # never holds these rows, so counting them here would report a
                # false dedup regression whenever a synthetic emission shares a
                # key with — or out-totals — a real row.
                model = message.get("model") or obj.get("model")
                if isinstance(model, str) and model.strip() == "<synthetic>":
                    continue
                msg_id = message.get("id")
                req_id = obj.get("requestId")
                if not msg_id or not req_id:
                    continue
                total = (
                    usage.get("input_tokens", 0)
                    + usage.get("output_tokens", 0)
                    + usage.get("cache_creation_input_tokens", 0)
                    + usage.get("cache_read_input_tokens", 0)
                )
                key = (msg_id, req_id)
                emission_counts[key] += 1
                if total > max_totals[key]:
                    max_totals[key] = total

    multi = sorted(k for k, count in emission_counts.items() if count >= 2)
    if not multi:
        ctx.degrade(name, "no multi-emission (msg_id, req_id) pairs")
        return

    conn = ctx.open_ro(name, ctx.cache_path)
    if conn is None:
        return
    try:
        if not ctx.migration_applied(name, None, conn):
            return
        sampled = multi[:_R1_SAMPLE]
        checked = 0
        magnitude = 0
        for msg_id, req_id in sampled:
            row = conn.execute(_ENTRY_BY_KEY, (msg_id, req_id)).fetchone()
            if row is None:
                # The key may belong to a JSONL the cache has not ingested yet
                # (mid-write), which live mode tolerates. A generated corpus is
                # walked to completion, so strict mode does not.
                ctx.degrade(
                    name,
                    f"({msg_id}, {req_id}) is in the JSONL but not in "
                    f"session_entries")
                if ctx.strict:
                    return
                continue
            # The token columns are positions 1-4 of `_ENTRY_PROJECTION`; the
            # cost columns after them belong to the recomputation, not here.
            _model, in_t, out_t, cc_t, cr_t = row[:5]
            cached_total = (
                int(in_t or 0) + int(out_t or 0)
                + int(cc_t or 0) + int(cr_t or 0))
            expected = max_totals[(msg_id, req_id)]
            if cached_total != expected:
                ctx.fail(
                    name,
                    f"({msg_id}, {req_id}) cache holds {cached_total} tokens, "
                    f"max emission was {expected}")
                return
            checked += 1
            magnitude = max(magnitude, expected)
        ctx.note(name, checked, checked, float(magnitude))
        if checked == 0:
            ctx.degrade(name, "no multi-emission key resolved to a cached row")
    finally:
        conn.close()


# ── R-DEDUP2 ───────────────────────────────────────────────────────────────
def _r_dedup2(ctx: _Context) -> None:
    """Validate in-scope weekly costs under the mode-appropriate authority.

    Strict mode compares migration 008 output with the generated cache over the
    stored range. Live mode compares the materialized cost with the selected
    ``weekly_cost_snapshot`` event; repricing today's cache cannot rewrite that
    durable fact. Scope stays migration 008's ``mode='auto'``, null-project,
    bounded-range selection in both modes.
    """
    name = "R-DEDUP2"
    if not ctx.strict:
        stats = ctx.open_ro(name, ctx.stats_path)
        if stats is None:
            return
        try:
            if not ctx.migration_applied(name, stats, None):
                return
            rows = stats.execute(
                "SELECT id, cost_usd, journal_id "
                "FROM weekly_cost_snapshots "
                "WHERE mode='auto' AND project IS NULL "
                "  AND range_start_iso IS NOT NULL "
                "  AND range_end_iso IS NOT NULL"
            ).fetchall()
            if not rows:
                ctx.degrade(name, "no eligible weekly_cost_snapshots rows")
                return
            for snap_id, stored, journal_id in rows:
                if ctx.replay_cost(
                    name, stats, journal_id, "weekly_cost_snapshot",
                    "cost_usd", stored, f"snapshot id={snap_id}",
                ) is None:
                    return
        finally:
            stats.close()
        return

    conns = ctx.open_ro(name, ctx.stats_path, ctx.cache_path)
    if conns is None:
        return
    stats, cache = conns
    try:
        if not ctx.migration_applied(name, stats, cache):
            return
        baseline_migration = ctx._applied_at

        query = (
            "SELECT id, range_start_iso, range_end_iso, cost_usd, "
            "       captured_at_utc "
            "  FROM weekly_cost_snapshots "
            " WHERE mode = 'auto' AND project IS NULL "
            "   AND range_start_iso IS NOT NULL "
            "   AND range_end_iso IS NOT NULL"
        )
        rows = stats.execute(query).fetchall()
        if not rows:
            ctx.degrade(name, "no eligible weekly_cost_snapshots rows")
            return

        checked = 0
        for snap_id, start_iso, end_iso, stored, captured_at_utc in rows:
            baseline = baseline_migration or captured_at_utc
            if ctx.drifted(name, cache, baseline):
                if ctx.strict:
                    return
                continue
            computed, sources = ctx.recompute(
                cache, _utc(start_iso), _utc(end_iso))
            if abs(stored - computed) >= 1e-9:
                ctx.fail(
                    name,
                    f"snapshot id={snap_id} stored=${stored:.9f} "
                    f"computed=${computed:.9f} "
                    f"range=[{start_iso}, {end_iso}]")
                return
            checked += 1
            ctx.note(name, 1, sources, computed)
        if checked == 0:
            ctx.degrade(
                name, "every eligible snapshot had a post-capture cache ingest")
    finally:
        stats.close()
        cache.close()


# ── R-DEDUP3 ───────────────────────────────────────────────────────────────
def _r_dedup3(ctx: _Context) -> None:
    """The ACTIVE 5h block's `total_cost_usd` must equal the recompute over
    `[block_start_at, last_observed_at_utc]`.

    "Active" is `block_start_at + 5h` still ahead of the reference instant —
    the same predicate the dashboard and TUI use for the ACTIVE row. This is the
    one mutable, unjournaled projection, so both modes recompute its retained
    range. Strict additionally proves the corpus did not ingest after migration
    009; live compares the projection directly.
    """
    name = "R-DEDUP3"
    conns = ctx.open_ro(name, ctx.stats_path, ctx.cache_path)
    if conns is None:
        return
    stats, cache = conns
    try:
        if not ctx.migration_applied(name, stats, cache):
            return
        baseline_migration = ctx._applied_at

        reference = _utc(
            ctx.as_of or dt.datetime.now(dt.timezone.utc).isoformat())
        active = stats.execute(
            "SELECT block_start_at, last_observed_at_utc, total_cost_usd, "
            "       last_updated_at_utc "
            "  FROM five_hour_blocks "
            " WHERE is_closed=0 AND journal_id IS NULL "
            "   AND unixepoch(block_start_at) + 5 * 3600 > unixepoch(?) "
            " ORDER BY block_start_at DESC LIMIT 1",
            (reference,)).fetchone()
        if active is None:
            ctx.degrade(name, f"no open 5h block is active at {reference}")
            return

        (
            block_start_at, last_observed_at_utc, stored_cost,
            last_updated_at_utc,
        ) = active
        block_start_utc = _utc(block_start_at)
        last_obs_utc = _utc(last_observed_at_utc)
        # Strict mode proves the migration-built corpus did not ingest again
        # after migration 009. Live mode compares the mutable projection with
        # the current cache directly and has no migration baseline.
        if ctx.strict and ctx.drifted(
            name, cache, baseline_migration or last_updated_at_utc
        ):
            return
        computed_cost, sources = ctx.recompute(
            cache, block_start_utc, last_obs_utc)
        if abs(stored_cost - computed_cost) >= 1e-9:
            ctx.fail(
                name,
                f"active block block_start_at={block_start_at} "
                f"stored=${stored_cost:.9f} computed=${computed_cost:.9f}")
            return
        ctx.note(name, 1, sources, computed_cost)
    finally:
        stats.close()
        cache.close()


# ── R-DEDUP4 ───────────────────────────────────────────────────────────────
def _r_dedup4(ctx: _Context) -> None:
    """Validate sampled closed blocks under the mode-appropriate authority.

    Strict compares migration 009 output with the generated cache. Live compares
    the frozen parent cost with its selected ``five_hour_block_close`` event.
    Closed blocks only; the unjournaled open projection belongs to R-DEDUP3.
    """
    name = "R-DEDUP4"
    if not ctx.strict:
        stats = ctx.open_ro(name, ctx.stats_path)
        if stats is None:
            return
        try:
            if not ctx.migration_applied(name, stats, None):
                return
            rows = stats.execute(
                "SELECT id, block_start_at, total_cost_usd, journal_id "
                "FROM five_hour_blocks WHERE is_closed=1 "
                "ORDER BY block_start_at DESC LIMIT 50"
            ).fetchall()
            if not rows:
                ctx.degrade(name, "no eligible closed 5h blocks")
                return
            for block_id, block_start_at, stored, journal_id in rows:
                if ctx.replay_cost(
                    name, stats, journal_id, "five_hour_block_close",
                    "total_cost_usd", stored,
                    f"block id={block_id} block_start_at={block_start_at}",
                ) is None:
                    return
        finally:
            stats.close()
        return

    conns = ctx.open_ro(name, ctx.stats_path, ctx.cache_path)
    if conns is None:
        return
    stats, cache = conns
    try:
        if not ctx.migration_applied(name, stats, cache):
            return
        baseline_migration = ctx._applied_at

        query = (
            "SELECT id, block_start_at, last_observed_at_utc, total_cost_usd "
            "  FROM five_hour_blocks "
            " WHERE is_closed = 1"
        )
        query += " ORDER BY block_start_at DESC LIMIT 50"
        rows = stats.execute(query).fetchall()
        if not rows:
            ctx.degrade(name, "no eligible closed 5h blocks")
            return

        checked = 0
        for block_id, block_start_at, last_observed_at_utc, stored_cost in rows:
            block_start_utc = _utc(block_start_at)
            last_obs_utc = _utc(last_observed_at_utc)
            baseline = baseline_migration or last_obs_utc
            if ctx.drifted(name, cache, baseline):
                if ctx.strict:
                    return
                continue
            computed_cost, sources = ctx.recompute(
                cache, block_start_utc, last_obs_utc)
            if abs(stored_cost - computed_cost) >= 1e-9:
                ctx.fail(
                    name,
                    f"block id={block_id} block_start_at={block_start_at} "
                    f"stored=${stored_cost:.9f} "
                    f"computed=${computed_cost:.9f}")
                return
            checked += 1
            ctx.note(name, 1, sources, computed_cost)
        if checked == 0:
            ctx.degrade(
                name, "every eligible block had a post-close cache ingest")
    finally:
        stats.close()
        cache.close()


# ── R-DEDUP5 ───────────────────────────────────────────────────────────────
def _r_dedup5(ctx: _Context) -> None:
    """Validate milestone cumulative costs and reset-segment identity.

    Strict compares migration 010 output with the generated cache from the
    migration's week-start fallback through capture. Live compares the durable
    cost with its selected ``percent_milestone`` event and resolves the physical
    ``reset_event_id`` back to that event's logical ``reset_event_ref``. It never
    recomputes a post-credit segment from the original week start.
    """
    name = "R-DEDUP5"
    if not ctx.strict:
        stats = ctx.open_ro(name, ctx.stats_path)
        if stats is None:
            return
        try:
            if not ctx.migration_applied(name, stats, None):
                return
            rows = stats.execute(
                "SELECT id, week_start_date, captured_at_utc, "
                "       cumulative_cost_usd, reset_event_id, journal_id "
                "FROM percent_milestones "
                "ORDER BY captured_at_utc DESC LIMIT 100"
            ).fetchall()
            if not rows:
                ctx.degrade(name, "no eligible percent_milestones")
                return
            for (
                mid, week_start_date, captured_at_utc, stored,
                reset_event_id, journal_id,
            ) in rows:
                payload = ctx.replay_cost(
                    name, stats, journal_id, "percent_milestone",
                    "cumulative_cost_usd", stored,
                    f"milestone id={mid} week={week_start_date} "
                    f"captured={captured_at_utc}",
                )
                if payload is None:
                    return
                if int(reset_event_id or 0) == 0:
                    materialized_ref = "0"
                else:
                    reset = stats.execute(
                        "SELECT journal_id FROM week_reset_events WHERE id=?",
                        (int(reset_event_id),),
                    ).fetchone()
                    materialized_ref = reset[0] if reset is not None else None
                retained_ref = payload.get("reset_event_ref", "0")
                if str(retained_ref) != str(materialized_ref):
                    ctx.fail(
                        name,
                        f"milestone id={mid} reset segment materialized="
                        f"{materialized_ref!r} journal={retained_ref!r}")
                    return
        finally:
            stats.close()
        return

    conns = ctx.open_ro(name, ctx.stats_path, ctx.cache_path)
    if conns is None:
        return
    stats, cache = conns
    try:
        if not ctx.migration_applied(name, stats, cache):
            return
        baseline_migration = ctx._applied_at

        query = (
            "SELECT id, week_start_date, week_start_at, captured_at_utc, "
            "       cumulative_cost_usd "
            "  FROM percent_milestones"
        )
        query += " ORDER BY captured_at_utc DESC LIMIT 100"
        rows = stats.execute(query).fetchall()
        if not rows:
            ctx.degrade(name, "no eligible percent_milestones")
            return

        checked = 0
        for (
            mid, week_start_date, week_start_at, captured_at_utc, stored_cum,
        ) in rows:
            if week_start_at:
                range_start_iso = week_start_at
            elif week_start_date:
                range_start_iso = f"{week_start_date}T00:00:00+00:00"
            else:
                continue
            baseline = baseline_migration or captured_at_utc
            if ctx.drifted(name, cache, baseline):
                if ctx.strict:
                    return
                continue
            computed, sources = ctx.recompute(
                cache, _utc(range_start_iso), _utc(captured_at_utc))
            if abs(stored_cum - computed) >= 1e-9:
                ctx.fail(
                    name,
                    f"milestone id={mid} week={week_start_date} "
                    f"captured={captured_at_utc} stored=${stored_cum:.9f} "
                    f"computed=${computed:.9f}")
                return
            checked += 1
            ctx.note(name, 1, sources, computed)
        if checked == 0:
            ctx.degrade(
                name, "every eligible milestone had a post-capture cache ingest")
    finally:
        stats.close()
        cache.close()


_INVARIANTS = (_r_dedup1, _r_dedup2, _r_dedup3, _r_dedup4, _r_dedup5)


def check(root, *, as_of=None, strict: bool) -> Result:
    """Run all five invariants under `root` and return what they found.

    `root` is a HOME-shaped directory: `.claude/projects` plus
    `.local/share/cctally/{cache,stats}.db`. Nothing here reads `HOME`, the
    password database or `CCTALLY_DATA_DIR` — the caller decides which store is
    under test, which is what keeps the suite off the maintainer's real data.
    """
    if strict and not as_of:
        raise ValueError("strict mode requires a pinned as_of")
    ctx = _Context(root, as_of, strict)
    for invariant in _INVARIANTS:
        invariant(ctx)
    if strict:
        _enforce_non_vacuity(ctx)
    return ctx.result


def _enforce_non_vacuity(ctx: _Context) -> None:
    """Strict mode's second half: a check that inspected nothing is a failure.

    Without this, R-DEDUP2 through R-DEDUP5 report five healthy checks over a
    corpus whose windows contain no `session_entries` at all, because a stored
    zero equals a recomputed zero.
    """
    for name in INVARIANT_NAMES:
        if any(f.startswith(name) for f in ctx.result.failures):
            continue
        if ctx.result.checked[name] <= 0:
            ctx.fail(name, "inspected no eligible row")
            continue
        if ctx.result.source_counts[name] <= 0:
            ctx.fail(name, "recomputed over zero source rows")
            continue
        if ctx.result.recomputed_magnitude[name] <= 0.0:
            ctx.fail(
                name,
                "recomputed to zero, which a stored zero satisfies vacuously")
