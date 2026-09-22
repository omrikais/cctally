"""#769 S9 / #815 — the Codex project and block detail routes read only their own rows.

``_codex_detail_inputs`` set ``range_start = now_utc - timedelta(days=365)`` and
loaded the full qualified Codex entry population for that year before either
builder selected a row. Measured on the production cache at S9 kickoff: 216,165
``codex_session_entries`` rows, every one of them inside the year predicate. The
year bounds nothing on a real store, so each detail click loaded the entire
Codex accounting corpus.

The block route never read what it paid for. ``_build_codex_block_detail``
receives no qualified entries, and ``entries = _codex_entries_from_qualified(
qualified)`` was consumed by neither builder.

The guard here is a data-access counter rather than a latency proxy, extending
the ``_CountingConnection`` technique in ``tests/test_codex_session_detail_rows.py``.
It classifies every yielded row by relation, so a route that returns the right
answer after visiting the whole corpus still fails.

THE COUNTER CANNOT PROVE AN INDEX IS USED. It observes rows AFTER SQLite has
applied its predicates, so a query that seeks two rows and one that scans the
year to find the same two are indistinguishable to it. That is why the scoped
accounting shards also carry ``EXPLAIN QUERY PLAN`` assertions below, following
``tests/test_codex_entries_root_path_index.py``, which exists precisely because
a query returned few rows while visiting the whole table.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import pathlib
import shutil
import sys

import pytest

from _lib_dashboard_sources import (
    SOURCE_SCHEMA_VERSION,
    CapabilityRecord,
    SourceDashboardBundle,
    SourceDashboardState,
    compose_all_state,
)
from conftest import load_script, redirect_paths


UTC = dt.timezone.utc
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
CORPUS = REPO_ROOT / "tests" / "fixtures" / "codex-parity" / "v1" / "rollouts"
NOW = dt.datetime(2026, 7, 16, 18, tzinfo=UTC)

#: Every relation a Codex detail read could materialise rows from. Accounting
#: and metadata are counted separately, because spec §4.6 accepts the metadata
#: read as a disclosed, bounded resolution cost while the accounting read is
#: what the acceptance criterion bounds.
_ACCOUNTING_RELATIONS = ("codex_session_entries",)
_METADATA_RELATIONS = ("codex_conversation_threads", "codex_session_files")
_QUOTA_RELATIONS = ("quota_window_snapshots", "quota_window_blocks")
_COUNTED_RELATIONS = _ACCOUNTING_RELATIONS + _METADATA_RELATIONS + _QUOTA_RELATIONS


class _CountingCursor:
    """A cursor that tallies every row a counted statement yields."""

    def __init__(self, inner, tally, rows, sql):
        self._inner = inner
        self._tally = tally
        self._rows = rows
        self._sql = sql

    def _relations(self):
        lowered = self._sql.lower()
        return [name for name in _COUNTED_RELATIONS if name in lowered]

    def _count(self, rows):
        for relation in self._relations():
            self._tally[relation] = self._tally.get(relation, 0) + len(rows)
            self._rows.setdefault(relation, []).extend(rows)
        return rows

    def fetchall(self):
        return self._count(list(self._inner.fetchall()))

    def fetchmany(self, *args):
        return self._count(list(self._inner.fetchmany(*args)))

    def fetchone(self):
        row = self._inner.fetchone()
        self._count([] if row is None else [row])
        return row

    def __iter__(self):
        for row in self._inner:
            self._count([row])
            yield row

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _CountingConnection:
    """A connection proxy that counts materialised rows per relation.

    ``__setattr__`` forwards, which is not cosmetic: the qualified reader sets
    ``conn.row_factory = sqlite3.Row`` and then indexes its rows BY NAME. A
    proxy that absorbed that assignment would leave the inner connection
    yielding plain tuples and the reader would fail on the first row.
    """

    _OWN = ("_inner", "_tally", "_rows", "_statements")

    def __init__(self, inner, tally, rows, statements):
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_tally", tally)
        object.__setattr__(self, "_rows", rows)
        object.__setattr__(self, "_statements", statements)

    def __setattr__(self, name, value):
        if name in self._OWN:                               # pragma: no cover
            object.__setattr__(self, name, value)
        else:
            setattr(self._inner, name, value)

    def execute(self, sql, *args, **kwargs):
        self._statements.append(" ".join(str(sql).split()))
        return _CountingCursor(
            self._inner.execute(sql, *args, **kwargs),
            self._tally, self._rows, str(sql),
        )

    def cursor(self, *args, **kwargs):                      # pragma: no cover
        raise AssertionError(
            "a detail route opened a raw cursor, which this counter cannot see"
        )

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _Counter:
    """One request's materialisation record."""

    def __init__(self):
        self.tally: dict[str, int] = {}
        self.rows: dict[str, list] = {}
        self.statements: list[str] = []

    @property
    def accounting(self) -> int:
        return sum(self.tally.get(name, 0) for name in _ACCOUNTING_RELATIONS)

    @property
    def metadata(self) -> int:
        return sum(self.tally.get(name, 0) for name in _METADATA_RELATIONS)

    def install(self, monkeypatch):
        cache_module = sys.modules["_cctally_cache"]
        real = cache_module.open_cache_db

        def _counting(*args, **kwargs):
            return _CountingConnection(
                real(*args, **kwargs), self.tally, self.rows, self.statements)

        monkeypatch.setattr(cache_module, "open_cache_db", _counting)
        return self


# ── the seeded store ───────────────────────────────────────────────────────

#: The requested 300-minute block already reset, and the decoy is still active.
#: The bounded read the route used to issue orders active windows FIRST, so a
#: decoy carrying more rows than the cap starves the requested group entirely.
_REQUESTED_RESET = NOW - dt.timedelta(hours=2)
_REQUESTED_START = _REQUESTED_RESET - dt.timedelta(minutes=300)
_DECOY_RESET = NOW + dt.timedelta(hours=3)


def _iso(value: dt.datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _limit_key(slot: str, minutes: int) -> str:
    return json.dumps(
        {"limitName": slot, "observedSlot": slot, "windowMinutes": minutes},
        sort_keys=True, separators=(",", ":"),
    )


def _seed_store(ns, tmp_path, monkeypatch, *, decoy_observations=1_200,
                requested_observations=40, distractor_entries=3_000,
                metadata_incomplete=False, malformed_rows=False):
    """A single-root Codex store carrying substantial IN-RANGE distractors.

    The existing route regression's hundred thousand distractor rows are two
    years old (`tests/test_dashboard_source_routes.py:187`) and therefore fall
    OUTSIDE the one-year predicate, which is exactly why it never reproduced
    #815. Every distractor here is inside the year.
    """
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    root = tmp_path / "provider"
    rollout = root / "sessions" / "2026" / "07" / "16" / "rollout.jsonl"
    rollout.parent.mkdir(parents=True)
    shutil.copyfile(CORPUS / "modern-full.jsonl", rollout)
    monkeypatch.setenv("CODEX_HOME", str(root))

    cache = ns["open_cache_db"]()
    stats = ns["open_db"]()
    try:
        ns["sync_codex_cache"](cache)
        root_key, root_path = cache.execute(
            "SELECT source_root_key, canonical_root_path FROM codex_source_roots"
        ).fetchone()
        target = cache.execute(
            "SELECT conversation_key, native_thread_id, root_thread_id, "
            "source_path, cwd FROM codex_conversation_threads LIMIT 1"
        ).fetchone()
        assert target is not None, "the corpus published no Codex thread"

        # Accounting inside the requested block's own span, so the published
        # block row has model breakdowns and is wired at all.
        cache.executemany(
            "INSERT INTO codex_session_entries "
            "(source_path, line_offset, timestamp_utc, session_id, model, "
            "input_tokens, cached_input_tokens, output_tokens, "
            "reasoning_output_tokens, total_tokens, source_root_key, "
            "conversation_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            tuple(
                (
                    str(target[3]), 90_000 + index,
                    _iso(_REQUESTED_START + dt.timedelta(minutes=10 * index)),
                    "target-session", "gpt-5", 100, 10, 20, 5, 130,
                    str(root_key), str(target[0]),
                )
                for index in range(6)
            ),
        )
        # And inside the decoy block's own span, so it is published too. The
        # decoy is the block whose group the old cap did NOT starve, which is
        # what lets the accounting-tally case fail on the tally rather than on
        # a 404 raised for a different reason.
        cache.executemany(
            "INSERT INTO codex_session_entries "
            "(source_path, line_offset, timestamp_utc, session_id, model, "
            "input_tokens, cached_input_tokens, output_tokens, "
            "reasoning_output_tokens, total_tokens, source_root_key, "
            "conversation_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            tuple(
                (
                    str(target[3]), 95_000 + index,
                    _iso(NOW - dt.timedelta(minutes=90) + dt.timedelta(minutes=10 * index)),
                    "decoy-session", "gpt-5", 50, 5, 10, 2, 67,
                    str(root_key), str(target[0]),
                )
                for index in range(4)
            ),
        )

        # IN-RANGE distractors: their own threads, their own projects, their
        # own paths, spread across the year the route used to load whole.
        for index in range(6):
            conversation = f"distractor-conversation-{index}"
            path = f"{root_path}/sessions/distractor-{index}.jsonl"
            cache.execute(
                "INSERT INTO codex_conversation_threads "
                "(conversation_key, source_root_key, native_thread_id, "
                "root_thread_id, source_path, cwd, git_json, first_seen_utc, "
                "last_seen_utc) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    conversation, str(root_key), f"distractor-thread-{index}",
                    f"distractor-thread-{index}", path,
                    f"/synthetic/root-a/distractor-{index}", None,
                    _iso(NOW - dt.timedelta(days=200)),
                    _iso(NOW - dt.timedelta(days=30)),
                ),
            )
            cache.executemany(
                "INSERT INTO codex_session_entries "
                "(source_path, line_offset, timestamp_utc, session_id, model, "
                "input_tokens, cached_input_tokens, output_tokens, "
                "reasoning_output_tokens, total_tokens, source_root_key, "
                "conversation_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                tuple(
                    (
                        path, offset,
                        _iso(NOW - dt.timedelta(days=300) + dt.timedelta(hours=offset)),
                        f"distractor-session-{index}", "gpt-5",
                        1, 0, 1, 0, 2, str(root_key), conversation,
                    )
                    for offset in range(distractor_entries // 6)
                ),
            )

        # The weekly cycle the hero and the block wire are scoped by.
        cache.execute(
            "INSERT INTO quota_window_snapshots "
            "(source, source_root_key, source_path, line_offset, "
            "captured_at_utc, observed_slot, logical_limit_key, limit_id, "
            "limit_name, window_minutes, used_percent, resets_at_utc) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "codex", str(root_key), f"{root_path}/fixture-weekly.jsonl",
                10_080, _iso(NOW - dt.timedelta(minutes=1)), "fixture-weekly",
                _limit_key("fixture-weekly", 10_080), "fixture-weekly",
                "Fixture weekly limit", 10_080, 25.0,
                _iso(NOW + dt.timedelta(days=1)),
            ),
        )

        # The REQUESTED group: a 300-minute window that has already reset.
        requested_rows = [
            (
                "codex", str(root_key), f"{root_path}/requested-5h.jsonl", index,
                _iso(_REQUESTED_START + dt.timedelta(minutes=7 * index)),
                "requested-5h", _limit_key("requested-5h", 300), "requested-5h",
                "Requested 5-hour limit", 300,
                round(1.0 + index * 1.5, 2), _iso(_REQUESTED_RESET),
            )
            for index in range(requested_observations)
        ]
        # One member stored under an EQUIVALENT window-minutes spelling. The
        # exact group filter has to ask for it by name, which is what
        # `snap_equivalent_raw_groups` exists to do.
        requested_rows.append((
            "codex", str(root_key), f"{root_path}/requested-5h-jitter.jsonl", 1,
            _iso(_REQUESTED_START + dt.timedelta(minutes=7 * requested_observations)),
            "requested-5h", _limit_key("requested-5h", 301), "requested-5h",
            "Requested 5-hour limit", 301,
            round(1.0 + requested_observations * 1.5, 2), _iso(_REQUESTED_RESET),
        ))
        cache.executemany(
            "INSERT INTO quota_window_snapshots "
            "(source, source_root_key, source_path, line_offset, "
            "captured_at_utc, observed_slot, logical_limit_key, limit_id, "
            "limit_name, window_minutes, used_percent, resets_at_utc) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            tuple(requested_rows),
        )

        # The DECOY group: still active, and larger than the thousand-row cap
        # the bounded read applied. Its rows sort ahead of every requested row,
        # so the requested group used to arrive empty.
        cache.executemany(
            "INSERT INTO quota_window_snapshots "
            "(source, source_root_key, source_path, line_offset, "
            "captured_at_utc, observed_slot, logical_limit_key, limit_id, "
            "limit_name, window_minutes, used_percent, resets_at_utc) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            tuple(
                (
                    "codex", str(root_key), f"{root_path}/decoy-5h.jsonl", index,
                    _iso(NOW - dt.timedelta(minutes=decoy_observations - index)),
                    "decoy-5h", _limit_key("decoy-5h", 300), "decoy-5h",
                    "Decoy 5-hour limit", 300,
                    round(index * 0.05, 2), _iso(_DECOY_RESET),
                )
                for index in range(decoy_observations)
            ),
        )

        if malformed_rows:
            # #845 7.1 / A17. One accounting row the ACCOUNTING-WINDOW health
            # read and the one-year probe both count, so the generation is
            # `malformed_row_partial` and its partial fold runs. An empty
            # conversation key is the one shape both aggregates already count
            # today, which is what lets this measurement predate the #846
            # change it exists to bound.
            cache.execute(
                "INSERT INTO codex_session_entries "
                "(source_path, line_offset, timestamp_utc, session_id, model, "
                "input_tokens, cached_input_tokens, output_tokens, "
                "reasoning_output_tokens, total_tokens, source_root_key, "
                "conversation_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    str(target[3]), 99_000,
                    _iso(NOW - dt.timedelta(days=2)),
                    "malformed-session", "gpt-5", 100, 10, 20, 5, 130,
                    str(root_key), "",
                ),
            )

        ns["_cctally_cache"]._bump_codex_physical_mutation_seq(cache)
        cache.commit()
        ns["reconcile_codex_quota_projection"](
            source_root_keys=(str(root_key),), now=NOW,
        )

        source_module = sys.modules["_cctally_dashboard_sources"]
        semantics = source_module.resolve_dashboard_source_semantics(
            {}, display_tz_name="UTC",
        )
        context = source_module.DashboardReadContext(
            cache_conn=cache,
            stats_conn=stats,
            range_start=NOW - dt.timedelta(days=30),
            now_utc=NOW,
            display_tz_name=semantics.display_tz_name,
            week_start_idx=semantics.week_start_idx,
            week_start_name=semantics.week_start_name,
            speed=semantics.speed,
            codex_budget=semantics.codex_budget,
        )
        if metadata_incomplete:
            # The same seam the source build itself uses to decide the
            # generation is metadata-incomplete.
            analytics = sys.modules["_cctally_source_analytics"]

            def _unavailable(*_args, **_kwargs):
                raise analytics.QualifiedMetadataUnavailable(
                    "Codex accounting metadata is unavailable")

            original = source_module.load_qualified_codex_entries
            source_module.load_qualified_codex_entries = _unavailable
            try:
                codex = source_module.build_codex_source_state(
                    context, data_version="codex-s9-v1")
            finally:
                source_module.load_qualified_codex_entries = original
        else:
            codex = source_module.build_codex_source_state(
                context, data_version="codex-s9-v1")
    finally:
        cache.close()
        stats.close()
    return str(root_key), codex


def _snapshot(ns, codex):
    claude = SourceDashboardState(
        source="claude",
        availability="ok",
        freshness="fresh",
        warnings=(),
        data_version="claude-v1",
        last_success_at=NOW,
        capabilities={"sessions": CapabilityRecord("supported")},
        data={
            "sessions": {"rows": ()}, "projects": {"rows": ()},
            "quota": {"blocks": ()},
        },
    )
    snap = ns["_empty_dashboard_snapshot"]()
    snap.generated_at = NOW
    snap.source_bundle = SourceDashboardBundle(
        source_schema_version=SOURCE_SCHEMA_VERSION,
        default_source="claude",
        source_order=("claude", "codex", "all"),
        sources={
            "claude": claude, "codex": codex,
            "all": compose_all_state(claude, codex),
        },
    )
    return snap


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    ns = load_script()
    root_key, codex = _seed_store(ns, tmp_path, monkeypatch)
    return ns, root_key, codex, _snapshot(ns, codex)


def _block_row(codex, reset: dt.datetime):
    blocks = codex.data["quota"]["blocks"]
    wanted = _iso(reset)
    for row in blocks:
        if str(row["resets_at"]).replace("Z", "+00:00") == wanted:
            return row
    raise AssertionError(
        f"no published block resets at {wanted}: "
        f"{[row['resets_at'] for row in blocks]}"
    )


def _requested_block_row(codex):
    return _block_row(codex, _REQUESTED_RESET)


def _decoy_block_row(codex):
    return _block_row(codex, _DECOY_RESET)


# ── the block route ────────────────────────────────────────────────────────

def test_a_codex_block_detail_materialises_no_accounting_rows(
    seeded, monkeypatch,
):
    """#815 / spec §1.2. The block builder receives no qualified entries and
    reads none, yet `_codex_detail_inputs` loaded a year of them before it ran.
    On the production store that is the entire Codex accounting corpus.
    """
    ns, _root_key, codex, snap = seeded
    dashboard = sys.modules["_cctally_dashboard"]
    # The DECOY block, deliberately: its group survives the old cap, so this
    # case fails on the accounting tally rather than on the separate
    # starvation 404 the next case is about.
    block = _decoy_block_row(codex)
    counter = _Counter().install(monkeypatch)

    detail = dashboard.build_source_detail(
        snapshot=snap, source="codex", resource="block",
        key=str(block["key"]),
    )

    assert detail["detail_kind"] == "codex_block"
    assert counter.accounting == 0, (
        "the block detail materialised accounting rows it never reads: "
        f"{counter.tally}"
    )


def test_a_codex_block_detail_reads_only_its_own_physical_group(
    seeded, monkeypatch,
):
    """#815 / spec §3.2. The old bound was on CARDINALITY, not on the answer.

    It ordered active windows first and capped at a thousand rows, so a decoy
    group larger than the cap starved the requested group completely — and
    `block.observations`, `percent_milestones`, `quota_freshness` and
    `forecast_quota` are then all recomputed from a set missing every member.
    """
    ns, _root_key, codex, snap = seeded
    dashboard = sys.modules["_cctally_dashboard"]
    block = _requested_block_row(codex)
    counter = _Counter().install(monkeypatch)

    detail = dashboard.build_source_detail(
        snapshot=snap, source="codex", resource="block",
        key=str(block["key"]),
    )

    observed = [item["captured_at"] for item in detail["observations"]]
    assert observed, "the requested window rendered no observation at all"

    # Every quota row that crossed the boundary belongs to the requested
    # group's snap-equivalent closure. No decoy row is loaded at all. Column
    # probes and ledger statements also name the relation, so the identity
    # assertions are scoped to rows that actually carry the identity columns.
    loaded = [
        row for row in counter.rows.get("quota_window_snapshots", [])
        if hasattr(row, "keys") and "observed_slot" in row.keys()
    ]
    assert loaded, "no observation row was materialised at all"
    slots = {str(row["observed_slot"]) for row in loaded}
    assert slots == {"requested-5h"}, (
        f"rows from unrelated physical groups crossed the boundary: {slots}"
    )

    # The closure is applied: the member stored under the equivalent
    # `window_minutes` spelling is found, not dropped.
    minutes = {int(row["window_minutes"]) for row in loaded}
    assert minutes == {300, 301}, (
        "the snap-equivalent closure was not applied to the group filter; "
        f"loaded window_minutes {minutes}"
    )

    # The full-group oracle, not whatever the cap returned.
    assert len(observed) == 41, (
        f"the requested group has 41 members; {len(observed)} rendered"
    )
    assert detail["forecast"]["status"]
    assert detail["milestones"]


def test_a_partial_generation_still_degrades_the_block_route(
    tmp_path, monkeypatch,
):
    """#815 / spec §3.1. The retired year-long qualified load was value-dead
    for the block route but NOT behaviour-dead.

    A `QualifiedMetadataUnavailable` raised while qualifying is caught around
    the whole builder and routes the request into the published-row partial
    fallback, so on a store with incomplete Codex metadata the block route
    degrades today. Deleting the load would have removed that silently, and the
    existing incomplete-metadata regression could not have caught it because it
    requests only sessions and projects. The frozen generation already decided
    the same question, so it is consulted instead of being provoked through a
    read the route does not use.

    As with the project sibling, this fixture forces the exception during the
    GENERATION build alone, so it pins the new rule rather than equivalence
    with the retired one. The two coincide in one direction only, for the
    reason that sibling's docstring sets out.
    """
    ns = load_script()
    dashboard = sys.modules["_cctally_dashboard"]
    _root_key, codex = _seed_store(
        ns, tmp_path, monkeypatch, metadata_incomplete=True)
    snap = _snapshot(ns, codex)
    assert codex.capabilities["projects"].semantics == (
        "conversation-metadata-partial"), "precondition: a partial generation"
    block = _decoy_block_row(codex)

    detail = dashboard.build_source_detail(
        snapshot=snap, source="codex", resource="block",
        key=str(block["key"]),
    )
    assert detail["metadata_availability"] == "partial"
    assert detail["key"] == block["key"]


def test_a_malformed_accounting_row_no_longer_breaks_the_block_route(
    seeded, monkeypatch,
):
    """The other half of the retired load's collateral behaviour, pinned.

    `_codex_entries_from_qualified` rejects an accounting row with no session
    identity by raising `SourceCapabilityUnavailable`, which the route maps to
    HTTP 400. Because the block route used to run that conversion over a year
    of rows it never read, ONE malformed row anywhere in the corpus answered
    400 for a quota window that was perfectly readable. That is collateral
    rather than a degradation worth preserving, so it goes: the block still
    renders, and the malformed row is simply never reached.
    """
    ns, _root_key, codex, snap = seeded
    dashboard = sys.modules["_cctally_dashboard"]
    block = _decoy_block_row(codex)

    cache = ns["open_cache_db"]()
    try:
        cache.execute(
            "UPDATE codex_session_entries SET session_id='' "
            "WHERE id=(SELECT id FROM codex_session_entries ORDER BY id LIMIT 1)"
        )
        cache.commit()
    finally:
        cache.close()

    detail = dashboard.build_source_detail(
        snapshot=snap, source="codex", resource="block",
        key=str(block["key"]),
    )
    assert detail["detail_kind"] == "codex_block"
    # #834 S2 (#829): present and null on a healthy generation, never absent.
    # A client cannot tell an omitted key from a key this build could not fill.
    assert detail["metadata_availability"] is None
    assert detail["metadata_reason"] is None


# ── the block route's account row-ownership ────────────────────────────────

_COLLIDING_RESET = NOW + dt.timedelta(hours=1)
_COLLIDING_START = _COLLIDING_RESET - dt.timedelta(minutes=300)
_COLLIDING_SLOT = "shared-5h"

#: Per-account evidence for the ONE physical 300-minute window both accounts
#: observe. The two series are disjoint in every derived field the wire
#: exposes, which is what lets the test discriminate WHICH account's block was
#: rendered rather than only which account's stats row supplied
#: `current_percent`:
#:
#: - the whole series decides `observations` and `milestones`;
#: - the last percent decides `current_percent` and the forecast;
#: - the latest capture decides `freshness`. `stale_after_seconds(300)` is 1800
#:   seconds, so acct-a's newest capture (three hours before `NOW`) is `stale`
#:   while acct-b's (ten minutes before `NOW`) is `fresh`. The window resets an
#:   hour after `NOW`, so a capture that recent is inside it.
_COLLIDING_EVIDENCE: dict[str, tuple[tuple[int, float], ...]] = {
    "acct-a": ((0, 2.0), (30, 12.0), (60, 22.0)),
    "acct-b": ((10, 3.0), (40, 33.0), (70, 63.0), (230, 70.0)),
}


def _seed_colliding_accounts(ns, tmp_path, monkeypatch):
    """Two REAL accounts whose 300-minute blocks agree on all FIVE key members.

    The resource key is built from root, logical limit key, observed slot,
    window minutes and reset alone, so both blocks produce ONE key and the
    candidate loop's `ORDER BY` decided which was returned.

    Two NON-DEFAULT accounts, deliberately. This repository records that
    account tests pass spuriously when both sides use the default account and
    require a real account key (`docs/codex-gotchas.md:70`), and the broader
    invariant treats a missing account predicate as its own defect class
    (`docs/accounts-gotchas.md:195`). A fixture pairing one real account with
    the `unattributed` bucket would prove nothing, and would not even turn
    decoration on.
    """
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    root = tmp_path / "provider"
    rollout = root / "sessions" / "2026" / "07" / "16" / "rollout.jsonl"
    rollout.parent.mkdir(parents=True)
    shutil.copyfile(CORPUS / "modern-full.jsonl", rollout)
    monkeypatch.setenv("CODEX_HOME", str(root))

    cache = ns["open_cache_db"]()
    stats = ns["open_db"]()
    try:
        ns["sync_codex_cache"](cache)
        root_key, root_path = cache.execute(
            "SELECT source_root_key, canonical_root_path FROM codex_source_roots"
        ).fetchone()
        target = cache.execute(
            "SELECT conversation_key, source_path FROM codex_conversation_threads LIMIT 1"
        ).fetchone()

        for account in ("acct-a", "acct-b"):
            stats.execute(
                "INSERT INTO accounts (account_key, provider, natural_id, email, "
                "label, plan_type, label_source, first_seen_utc, last_seen_utc) "
                "VALUES (?,'codex',?,?,?,NULL,'auto',?,?)",
                (account, account, f"{account}@example.test", account,
                 "2026-07-01T00:00:00Z", "2026-07-01T00:00:00Z"),
            )
        stats.commit()

        # Accounting inside the shared block's span, so both wire rows publish.
        cache.executemany(
            "INSERT INTO codex_session_entries "
            "(source_path, line_offset, timestamp_utc, session_id, model, "
            "input_tokens, cached_input_tokens, output_tokens, "
            "reasoning_output_tokens, total_tokens, source_root_key, "
            "conversation_key, account_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            tuple(
                (
                    str(target[1]), 70_000 + index,
                    _iso(_COLLIDING_START + dt.timedelta(minutes=20 * index)),
                    "collide-session", "gpt-5", 100, 10, 20, 5, 130,
                    str(root_key), str(target[0]), "acct-a" if index % 2 else "acct-b",
                )
                for index in range(6)
            ),
        )
        # The weekly cycle both blocks must overlap.
        cache.execute(
            "INSERT INTO quota_window_snapshots "
            "(source, source_root_key, source_path, line_offset, "
            "captured_at_utc, observed_slot, logical_limit_key, limit_id, "
            "limit_name, window_minutes, used_percent, resets_at_utc, account_key) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "codex", str(root_key), f"{root_path}/fixture-weekly.jsonl",
                10_080, _iso(NOW - dt.timedelta(minutes=1)), "fixture-weekly",
                _limit_key("fixture-weekly", 10_080), "fixture-weekly",
                "Fixture weekly limit", 10_080, 25.0,
                _iso(NOW + dt.timedelta(days=1)), "acct-a",
            ),
        )
        # One physical 300-minute window, observed under BOTH accounts.
        cache.executemany(
            "INSERT INTO quota_window_snapshots "
            "(source, source_root_key, source_path, line_offset, "
            "captured_at_utc, observed_slot, logical_limit_key, limit_id, "
            "limit_name, window_minutes, used_percent, resets_at_utc, account_key) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            tuple(
                (
                    "codex", str(root_key),
                    f"{root_path}/{account}-5h.jsonl", index,
                    _iso(_COLLIDING_START + dt.timedelta(minutes=offset_minutes)),
                    _COLLIDING_SLOT, _limit_key(_COLLIDING_SLOT, 300),
                    _COLLIDING_SLOT, "Shared 5-hour limit", 300,
                    percent, _iso(_COLLIDING_RESET), account,
                )
                for account, points in _COLLIDING_EVIDENCE.items()
                for index, (offset_minutes, percent) in enumerate(points)
            ),
        )
        ns["_cctally_cache"]._bump_codex_physical_mutation_seq(cache)
        cache.commit()
        ns["reconcile_codex_quota_projection"](
            source_root_keys=(str(root_key),), now=NOW,
        )

        source_module = sys.modules["_cctally_dashboard_sources"]
        semantics = source_module.resolve_dashboard_source_semantics(
            {}, display_tz_name="UTC",
        )
        codex = source_module.build_codex_source_state(
            source_module.DashboardReadContext(
                cache_conn=cache, stats_conn=stats,
                range_start=NOW - dt.timedelta(days=30), now_utc=NOW,
                display_tz_name=semantics.display_tz_name,
                week_start_idx=semantics.week_start_idx,
                week_start_name=semantics.week_start_name,
                speed=semantics.speed, codex_budget=semantics.codex_budget,
            ),
            data_version="codex-s9-collide-v1",
        )
    finally:
        cache.close()
        stats.close()
    return str(root_key), codex


def test_a_codex_block_detail_returns_the_requesting_accounts_block(
    tmp_path, monkeypatch,
):
    """#815 / spec §3.4. `quota_window_blocks` carries an `account_key`
    (`bin/_cctally_core.py:1579`) that this route never selected, and the
    resource key is built from root, limit key, slot, window and reset alone.
    Two accounts agreeing on those five produce one key, and the sort decided
    which was returned — so a request qualified to A could be answered with
    B's block.

    THE ACCOUNT MUST REACH THE EVIDENCE, NOT ONLY THE CANDIDATE ROW.
    Constraining the stats candidate is half the fix. `build_blocks` keys each
    block on the full `QuotaWindowIdentity`, which carries `account_key`
    (`bin/_lib_quota.py:375-380`), so one physical window observed by two
    accounts yields two `QuotaBlock`s at the same `resets_at`; selecting on
    `resets_at` alone returned whichever `identity_sort_key` ordered first,
    which is the account ascending (`:547-554`). The payload then carried one
    account's `current_percent` beside the other's `observations`,
    `milestones`, `forecast` and `freshness`, every one of which is derived
    from `block.observations`. Asserting `current_percent` alone cannot see
    that, so this case asserts each of the four derived fields as well.
    """
    ns = load_script()
    dashboard = sys.modules["_cctally_dashboard"]
    _root_key, codex = _seed_colliding_accounts(ns, tmp_path, monkeypatch)
    snap = _snapshot(ns, codex)

    colliding = [
        row for row in codex.data["quota"]["blocks"]
        if str(row["resets_at"]).replace("Z", "+00:00") == _iso(_COLLIDING_RESET)
    ]
    assert len(colliding) == 2, (
        "precondition: two decorated wire rows share one resource key; got "
        f"{colliding}"
    )
    assert len({row["key"] for row in colliding}) == 1, (
        "precondition: the collision is on the KEY, which excludes the account"
    )
    owners = {str(row["account_key"]) for row in colliding}
    assert owners == {"acct-a", "acct-b"}, (
        f"precondition: two REAL accounts, never the default bucket; got {owners}"
    )

    seen: dict[str, dict] = {}
    for row in colliding:
        account = str(row["account_key"])
        detail = dashboard.build_source_detail(
            snapshot=snap, source="codex", resource="block",
            key=str(row["key"]), account=account,
        )
        seen[account] = detail
        assert detail["current_percent"] == row["current_percent"], (
            f"the request qualified to {account} was answered with "
            f"another account's block: {detail['current_percent']} != "
            f"{row['current_percent']}"
        )
        # THE STATS ROW AND THE EVIDENCE MUST BE THE SAME ACCOUNT'S. The
        # candidate row was already constrained on the account, so
        # `current_percent` above came from the right block — but the
        # evidence did not. `build_blocks` keys each block on the full
        # `QuotaWindowIdentity`, which carries `account_key`, so one physical
        # window observed by two accounts yields TWO blocks at one
        # `resets_at`; selecting on `resets_at` alone took whichever
        # `identity_sort_key` put first, which is the account ASCENDING. The
        # four fields below are all derived from `block.observations`, so the
        # route rendered acct-b's percentage over acct-a's series.
        expected = _COLLIDING_EVIDENCE[account]
        assert [item["used_percent"] for item in detail["observations"]] == [
            percent for _offset, percent in expected
        ], (
            f"the request qualified to {account} rendered another account's "
            f"observation series: {detail['observations']}"
        )
        assert [
            item["captured_at"] for item in detail["observations"]
        ] == [
            _iso(_COLLIDING_START + dt.timedelta(minutes=offset))
            for offset, _percent in expected
        ], detail["observations"]
        assert detail["milestones"], "the fixture seeds a rising series"
        assert detail["milestones"][-1]["percent"] == int(expected[-1][1]), (
            f"the milestones for {account} end at another account's high "
            f"water: {detail['milestones'][-1]}"
        )
        assert detail["forecast"]["current_percent"] == expected[-1][1], (
            f"the forecast for {account} ran on another account's evidence: "
            f"{detail['forecast']}"
        )
        # `stale_after_seconds(300)` is 1800s. acct-a's newest capture is three
        # hours old and acct-b's is ten minutes old, so the freshness state
        # alone names which account's evidence was folded.
        assert detail["freshness"] == (
            "fresh" if account == "acct-b" else "stale"), (
            f"the freshness for {account} was computed over another account's "
            f"captures: {detail['freshness']}"
        )

    assert set(seen) == {"acct-a", "acct-b"}
    assert seen["acct-a"]["observations"] != seen["acct-b"]["observations"], (
        "non-vacuity: the two accounts must render different evidence, or "
        "none of the assertions above can discriminate"
    )

    # THE UNQUALIFIED PATH, where no published row supplies an owner. The
    # candidate loop then takes the first stored row for the key and the block
    # selector must still follow THAT row's own account (`matched[8]`), because
    # the published wire sorts its decorated rows account-DESCENDING while
    # `build_blocks` yields them ascending. The invariant that holds on every
    # path is that the rendered percentage and the rendered evidence come from
    # one account.
    for context in dashboard._codex_detail_context(snap):
        unqualified = dashboard._build_codex_block_detail(
            context, key=str(colliding[0]["key"]),
        )
    series = [item["used_percent"] for item in unqualified["observations"]]
    owning = [
        account for account, points in _COLLIDING_EVIDENCE.items()
        if series == [percent for _offset, percent in points]
    ]
    assert len(owning) == 1, (
        "the unqualified request rendered a series belonging to no single "
        f"account: {series}"
    )
    assert unqualified["current_percent"] == _COLLIDING_EVIDENCE[owning[0]][-1][1], (
        "the unqualified request rendered one account's percentage over the "
        f"other's evidence: {unqualified['current_percent']} against {series}"
    )
    assert unqualified["forecast"]["current_percent"] == series[-1]


def test_the_published_block_row_always_spells_its_reset_the_stored_way(
    seeded,
):
    """Spec §3.5's premise, proved rather than asserted.

    The candidate read is now predicated on the published row's `resets_at`
    and `window_minutes`. That is only safe if a match REQUIRES those to equal
    the stored values — otherwise the predicate would reject a block the
    unfiltered scan would have matched, and the route would 404 silently.

    `_codex_quota_block_rows` builds the published `resets_at` field and the
    resource key from the same `quota_window_blocks.resets_at_utc`, and its
    `window_minutes` from the same stored column, so the equality holds by
    construction. This pins it against a future divergence.
    """
    ns, _root_key, codex, _snap = seeded
    source_module = sys.modules["_cctally_dashboard_sources"]
    stats = ns["open_db"]()
    try:
        stored = {
            source_module.dashboard_resource_key(
                "block", "codex", row[0], row[1], row[2], row[3], row[5],
            ): (str(row[5]), int(row[3]))
            for row in stats.execute(
                "SELECT source_root_key, logical_limit_key, observed_slot, "
                "window_minutes, limit_name, resets_at_utc "
                "FROM quota_window_blocks WHERE source='codex'"
            )
        }
    finally:
        stats.close()

    published = codex.data["quota"]["blocks"]
    assert published, "the fixture published no Codex block"
    for row in published:
        key = str(row["key"])
        assert key in stored, (
            "a published key resolves to no stored block at all, so the "
            "route could never have matched it"
        )
        assert stored[key] == (str(row["resets_at"]), int(row["window_minutes"])), (
            "the published row spells its reset or window differently from "
            "the stored block, so predicating the candidate read on the "
            "published values would reject a block the scan would match"
        )


def test_a_codex_block_detail_resolves_beyond_the_two_hundred_and_fiftieth(
    seeded, monkeypatch,
):
    """What the predicate buys. Without it the candidate read takes the 250
    most recent blocks, so an older one the published bundle still carries is
    simply not found — and under decoration the account constraint would have
    shared that one cap across every account.
    """
    ns, root_key, codex, snap = seeded
    dashboard = sys.modules["_cctally_dashboard"]
    block = _requested_block_row(codex)

    stats = ns["open_db"]()
    try:
        # 300 unrelated blocks, every one of them resetting LATER than the
        # requested block, so an unpredicated `ORDER BY resets_at_utc DESC
        # … LIMIT 250` can no longer reach it.
        stats.executemany(
            "INSERT INTO quota_window_blocks (source, source_root_key, "
            "logical_limit_key, observed_slot, window_minutes, limit_id, "
            "limit_name, resets_at_utc, nominal_start_at_utc, "
            "first_observed_at_utc, last_observed_at_utc, first_percent, "
            "current_percent, last_source_path, last_line_offset, generation, "
            "account_key) VALUES ('codex',?,?,?,300,'codex','Crowd',?,?,?,?,"
            "1.0,9.0,'/crowd.jsonl',1,'g-crowd','unattributed')",
            tuple(
                (
                    root_key, _limit_key(f"crowd-{index}", 300),
                    f"crowd-{index}",
                    _iso(NOW + dt.timedelta(hours=6 + index)),
                    _iso(NOW + dt.timedelta(hours=1 + index)),
                    _iso(NOW + dt.timedelta(hours=1 + index)),
                    _iso(NOW + dt.timedelta(hours=2 + index)),
                )
                for index in range(300)
            ),
        )
        stats.commit()
    finally:
        stats.close()

    detail = dashboard.build_source_detail(
        snapshot=snap, source="codex", resource="block",
        key=str(block["key"]),
    )
    assert detail["detail_kind"] == "codex_block"
    assert detail["resets_at"].replace("Z", "+00:00") == _iso(_REQUESTED_RESET)


# ── the project route's key resolution ─────────────────────────────────────

_TARGET_CWD = "/synthetic/root-a/project-red"
_BLUE_CWD = "/synthetic/root-a/project-blue"
_GREEN_CWD = "/synthetic/root-a/project-green"


def _seed_precedence_shapes(ns, root_key, root_path, target_conversation):
    """The three shapes a naive project selector gets wrong (spec §4.1).

    1. A thread with NO `cwd` and a direct `git_json` resolving to project A,
       whose entries are read through a path whose INHERITED `cwd` resolves to
       project B. The entry belongs to B, because `cwd = row["cwd"] or
       inherited["cwd"]` is evaluated before either `git_json` is consulted.
       A selector that excludes it because its thread resolves to A loses it.
    2. One conversation appearing on TWO differently aliased paths. The schema
       permits it: accounting entries are unique on `(source_path,
       line_offset)` with no one-path-per-conversation constraint, while the
       thread table stores a single canonical path.
    3. A path carrying entries from SEVERAL conversations, only some of which
       belong to the target.
    """
    cache = ns["open_cache_db"]()
    try:
        def thread(conversation, native, path, cwd, git_json):
            cache.execute(
                "INSERT INTO codex_conversation_threads "
                "(conversation_key, source_root_key, native_thread_id, "
                "root_thread_id, source_path, cwd, git_json, first_seen_utc, "
                "last_seen_utc) VALUES (?,?,?,?,?,?,?,?,?)",
                (conversation, root_key, native, native, path, cwd, git_json,
                 _iso(NOW - dt.timedelta(days=10)),
                 _iso(NOW - dt.timedelta(days=1))),
            )

        def alias(path, native):
            # `source_root_key` is not optional: the inherited-metadata join
            # matches on `(files.source_root_key, files.last_native_thread_id)`,
            # so a NULL root makes the alias invisible and the case vacuous.
            cache.execute(
                "INSERT INTO codex_session_files "
                "(path, size_bytes, mtime_ns, last_byte_offset, "
                "last_ingested_at, last_session_id, last_model, "
                "source_root_key, last_native_thread_id) "
                "VALUES (?,1,1,1,?,NULL,NULL,?,?)",
                (path, _iso(NOW), root_key, native),
            )

        def entries(path, conversation, count, base_offset):
            cache.executemany(
                "INSERT INTO codex_session_entries "
                "(source_path, line_offset, timestamp_utc, session_id, model, "
                "input_tokens, cached_input_tokens, output_tokens, "
                "reasoning_output_tokens, total_tokens, source_root_key, "
                "conversation_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                tuple(
                    (
                        path, base_offset + index,
                        _iso(NOW - dt.timedelta(days=5, minutes=index)),
                        f"sess-{conversation}", "gpt-5",
                        10, 1, 2, 1, 13, root_key, conversation,
                    )
                    for index in range(count)
                ),
            )

        blue_path = f"{root_path}/sessions/blue-canonical.jsonl"
        green_path = f"{root_path}/sessions/green-canonical.jsonl"
        thread("c-blue", "thread-blue", blue_path, _BLUE_CWD, None)
        thread("c-green", "thread-green", green_path, _GREEN_CWD, None)
        entries(blue_path, "c-blue", 3, 1_000)
        entries(green_path, "c-green", 3, 2_000)

        # Shape 1. `c-git-a` has no cwd and a direct git_json; its entries sit
        # on a path whose inherited metadata is BLUE's, so they are blue.
        git_json = json.dumps(
            {"branch": "b", "repository": "repo-a"},
            sort_keys=True, separators=(",", ":"))
        shape1_path = f"{root_path}/sessions/shape1-inherits-blue.jsonl"
        thread("c-git-a", "thread-git-a",
               f"{root_path}/sessions/shape1-canonical.jsonl", None, git_json)
        alias(shape1_path, "thread-blue")
        entries(shape1_path, "c-git-a", 3, 10_000)

        # Shape 2. The TARGET conversation on two differently aliased paths:
        # one with no inherited metadata (stays target), one inheriting GREEN.
        plain_path = f"{root_path}/sessions/shape2-plain.jsonl"
        aliased_path = f"{root_path}/sessions/shape2-inherits-green.jsonl"
        alias(aliased_path, "thread-green")
        entries(plain_path, target_conversation, 4, 20_000)
        entries(aliased_path, target_conversation, 5, 30_000)

        # Shape 3. One un-aliased path carrying two conversations, only one of
        # which is the target.
        shared_path = f"{root_path}/sessions/shape3-shared.jsonl"
        entries(shared_path, target_conversation, 2, 40_000)
        entries(shared_path, "c-blue", 6, 41_000)

        cache.commit()
    finally:
        cache.close()


@pytest.fixture
def precedence(tmp_path, monkeypatch):
    ns = load_script()
    root_key, codex = _seed_store(ns, tmp_path, monkeypatch)
    cache = ns["open_cache_db"]()
    try:
        root_path = cache.execute(
            "SELECT canonical_root_path FROM codex_source_roots").fetchone()[0]
        target_conversation = cache.execute(
            "SELECT conversation_key FROM codex_conversation_threads "
            "WHERE cwd=?", (_TARGET_CWD,)).fetchone()[0]
    finally:
        cache.close()
    _seed_precedence_shapes(ns, root_key, str(root_path), str(target_conversation))
    return ns, root_key, str(root_path), str(target_conversation)


def _full_read(ns, *, speed="auto"):
    """The unscoped year-long qualified read: the oracle for every case here."""
    analytics = sys.modules["_cctally_source_analytics"]
    cache = ns["open_cache_db"]()
    try:
        return analytics.load_qualified_codex_entries(
            NOW - dt.timedelta(days=365),
            NOW + dt.timedelta(microseconds=1),
            speed=speed, sync=False, cache_conn=cache,
        )
    finally:
        cache.close()


def test_the_project_key_resolution_keeps_every_entry_the_qualifier_assigns(
    precedence,
):
    """Spec §4.1. Eligibility is decided over the full precedence, not over two
    independent sets, and the shard split is a query-execution concern.

    The oracle is the unscoped qualifier itself: for EVERY project key it
    emits, the scoped identity set must be able to reach every entry it
    assigned to that key. A set that reduces to "threads resolving here" drops
    shape 1; a set that reduces to "paths inheriting here" drops shapes 2 and 3.
    """
    ns, _root_key, _root_path, _target = precedence
    analytics = sys.modules["_cctally_source_analytics"]
    oracle = _full_read(ns)
    assert oracle, "the fixture qualified no entries at all"

    by_project: dict[str, list] = {}
    for entry in oracle:
        by_project.setdefault(entry.project_key, []).append(entry)
    assert len(by_project) >= 3, (
        f"the fixture must separate several projects; got {len(by_project)}")

    cache = ns["open_cache_db"]()
    try:
        for project_key, expected in by_project.items():
            conversations, paths, inherited = analytics.resolve_codex_project_scope(
                cache, lambda candidate, wanted=project_key: candidate == wanted)
            reachable = set(conversations)
            reachable_paths = set(paths)
            for entry in expected:
                identity = (entry.source_root_key, entry.conversation_key)
                path_identity = (entry.source_root_key, entry.source_path)
                assert (
                    identity in reachable or path_identity in reachable_paths
                ), (
                    f"entry {entry.cache_entry_id} belongs to {project_key} but "
                    f"neither its conversation {identity} nor its path "
                    f"{path_identity} is in the resolved scope"
                )
            assert isinstance(inherited, dict)
    finally:
        cache.close()


def test_an_inherited_cwd_outranks_a_direct_git_json(precedence):
    """Shape 1 stated on its own, so a regression names the rule it broke."""
    ns, _root_key, root_path, _target = precedence
    oracle = _full_read(ns)
    shape1 = [
        entry for entry in oracle
        if entry.source_path.endswith("shape1-inherits-blue.jsonl")
    ]
    assert len(shape1) == 3, "precondition: shape 1 qualified three entries"
    blue = [entry for entry in oracle if entry.source_path.endswith("blue-canonical.jsonl")]
    assert blue, "precondition: the blue project has entries of its own"
    assert {entry.project_key for entry in shape1} == {blue[0].project_key}, (
        "an inherited cwd must outrank the thread's own git_json, because "
        "`cwd = row['cwd'] or inherited['cwd']` is evaluated first"
    )


def test_both_project_stages_read_one_snapshot(precedence, monkeypatch):
    """Spec §4.4.1. Python's `sqlite3` driver does not begin a transaction for
    a `SELECT` — this repository records that at `bin/_cctally_cache.py:7607` —
    and the detail route's context opener merely opens a connection.

    A cache writer can therefore commit between stage one and stage two. If the
    two stages read different snapshots the answer is TORN: it carries stage
    one's identity set and stage two's rows, which matches neither the store as
    it was nor the store as it became. Under one read transaction the answer is
    the store as it was, which is what a single joined statement would have
    returned.

    The concurrent write below is deliberately two-sided. It appends rows to a
    conversation the identity set ALREADY holds, which only a shared snapshot
    suppresses, and it adds a whole new conversation of the same project, which
    no staged selector can see. A torn read takes the first and misses the
    second.
    """
    ns, root_key, root_path, target = precedence
    analytics = sys.modules["_cctally_source_analytics"]
    before = _full_read(ns)
    project_key = next(
        entry.project_key for entry in before
        if entry.conversation_key == target
        and entry.source_path.endswith("shape2-plain.jsonl")
    )
    expected = {
        entry.cache_entry_id for entry in before
        if entry.project_key == project_key
    }
    assert expected, "precondition: the target project has entries"

    original = analytics.resolve_codex_project_scope
    fired: list[str] = []

    def _commit_between(conn, matcher, **kwargs):
        result = original(conn, matcher, **kwargs)
        if fired:
            return result
        fired.append(matcher)
        writer = ns["open_cache_db"]()
        try:
            writer.executemany(
                "INSERT INTO codex_session_entries "
                "(source_path, line_offset, timestamp_utc, session_id, model, "
                "input_tokens, cached_input_tokens, output_tokens, "
                "reasoning_output_tokens, total_tokens, source_root_key, "
                "conversation_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                tuple(
                    (
                        f"{root_path}/sessions/shape2-plain.jsonl", 60_000 + index,
                        _iso(NOW - dt.timedelta(days=4, minutes=index)),
                        "sess-late", "gpt-5", 10, 1, 2, 1, 13, root_key, target,
                    )
                    for index in range(5)
                ),
            )
            writer.execute(
                "INSERT INTO codex_conversation_threads "
                "(conversation_key, source_root_key, native_thread_id, "
                "root_thread_id, source_path, cwd, git_json, first_seen_utc, "
                "last_seen_utc) VALUES (?,?,?,?,?,?,NULL,?,?)",
                ("c-late", root_key, "thread-late", "thread-late",
                 f"{root_path}/sessions/late.jsonl", _TARGET_CWD,
                 _iso(NOW), _iso(NOW)),
            )
            writer.execute(
                "INSERT INTO codex_session_entries "
                "(source_path, line_offset, timestamp_utc, session_id, model, "
                "input_tokens, cached_input_tokens, output_tokens, "
                "reasoning_output_tokens, total_tokens, source_root_key, "
                "conversation_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"{root_path}/sessions/late.jsonl", 1,
                 _iso(NOW - dt.timedelta(days=3)), "sess-late-2", "gpt-5",
                 10, 1, 2, 1, 13, root_key, "c-late"),
            )
            writer.commit()
        finally:
            writer.close()
        return result

    monkeypatch.setattr(
        analytics, "resolve_codex_project_scope", _commit_between)

    cache = ns["open_cache_db"]()
    try:
        scoped = analytics.load_codex_project_scoped_entries(
            NOW - dt.timedelta(days=365),
            NOW + dt.timedelta(microseconds=1),
            speed="auto", cache_conn=cache,
            matches_project_key=lambda candidate: candidate == project_key,
        )
    finally:
        cache.close()
    assert fired, "the barrier never ran, so this case proves nothing"

    observed = {entry.cache_entry_id for entry in scoped}
    assert observed == expected, (
        "both stages must read one snapshot; a torn read takes the rows "
        "committed after stage one while still missing the conversation "
        f"committed with them. extra={sorted(observed - expected)} "
        f"missing={sorted(expected - observed)}"
    )


def _target_project_row(codex):
    rows = codex.data["projects"]["rows"]
    assert rows, "the fixture published no Codex project"
    return rows[0]


def _unscoped_project_detail(ns, snap, key):
    """The pre-#815 answer: the project detail over the WHOLE year's rows.

    Produced by making the scoped reader return the unscoped population, so
    both arms go through the identical builder and the identical wire. An
    oracle assembled from hand-written expected values would only pin what the
    author already believed.
    """
    analytics = sys.modules["_cctally_source_analytics"]
    dashboard = sys.modules["_cctally_dashboard"]

    def _whole_year(start, end, *, speed, matches_project_key, cache_conn,
                    group="git-root"):
        return analytics.load_qualified_codex_entries(
            start, end, speed=speed, sync=False, group=group,
            cache_conn=cache_conn,
        )

    # NOT `monkeypatch.undo()`: that would also undo `redirect_paths`, and
    # the isolation guard then catches the test writing to the maintainer's
    # real production data directory. Restore exactly the one attribute.
    original = analytics.load_codex_project_scoped_entries
    analytics.load_codex_project_scoped_entries = _whole_year
    try:
        return dashboard.build_source_detail(
            snapshot=snap, source="codex", resource="project", key=key,
        )
    finally:
        analytics.load_codex_project_scoped_entries = original


def test_a_codex_project_detail_reads_only_its_own_projects_rows(
    precedence, tmp_path, monkeypatch,
):
    """#815 / spec §4.2, §4.4, §5.1.

    Three assertions at once, against a store carrying substantial IN-RANGE
    distractors: every materialised accounting row belongs to the resolved
    target scope; the count equals the target project's own rows in the
    window; and the rendered payload equals a FULL-READ oracle field for
    field. Metadata-resolution rows are counted and recorded SEPARATELY, per
    the maintainer decision in spec §4.6 — they are disclosed evidence, not a
    failure.
    """
    ns, _root_key, _root_path, _target = precedence
    dashboard = sys.modules["_cctally_dashboard"]
    analytics = sys.modules["_cctally_source_analytics"]
    codex = _rebuild_state(ns)
    snap = _snapshot(ns, codex)
    row = _target_project_row(codex)
    key = str(row["key"])

    oracle = _unscoped_project_detail(ns, snap, key)

    counter = _Counter().install(monkeypatch)
    detail = dashboard.build_source_detail(
        snapshot=snap, source="codex", resource="project", key=key,
    )

    for field in (
        "detail_kind", "key", "range_start", "range_end", "first_seen",
        "last_seen", "session_count", "cost_usd", "input_tokens",
        "cached_input_tokens", "output_tokens", "reasoning_output_tokens",
        "total_tokens", "models", "sessions",
    ):
        assert detail[field] == oracle[field], (
            f"the scoped read changed `{field}`: {detail[field]!r} != "
            f"{oracle[field]!r}"
        )

    # Every materialised accounting row belongs to the resolved scope.
    cache = ns["open_cache_db"]()
    try:
        conversations, paths, _inherited = analytics.resolve_codex_project_scope(
            cache, lambda candidate: _project_resource_key(candidate) == key)
        expected_ids = {
            int(entry_id) for entry_id in _scoped_entry_ids(
                cache, conversations, paths)
        }
    finally:
        cache.close()

    materialised = [
        item for item in counter.rows.get("codex_session_entries", [])
        if hasattr(item, "keys") and "cache_entry_id" in item.keys()
    ]
    assert materialised, "the project detail materialised no accounting row"
    observed_ids = {int(item["cache_entry_id"]) for item in materialised}
    assert observed_ids <= expected_ids, (
        "the scoped read materialised rows outside the resolved identity "
        f"scope: {sorted(observed_ids - expected_ids)[:10]}"
    )

    # And the count is the project's own rows, not the year's population.
    unscoped_total = len(_full_read(ns))
    assert counter.accounting < unscoped_total / 4, (
        f"the detail still materialises the year's population: "
        f"{counter.accounting} of {unscoped_total}"
    )
    # Spec §4.6: metadata resolution is a disclosed, bounded cost. Recorded
    # rather than bounded to zero.
    assert counter.metadata > 0, (
        "the key must be re-derived over metadata, so this can never be zero"
    )
    print(
        f"#815 project detail materialisation: accounting={counter.accounting} "
        f"of {unscoped_total}; metadata={counter.metadata}"
    )


def _project_resource_key(opaque: str) -> str:
    return sys.modules["_cctally_dashboard_sources"].dashboard_resource_key(
        "project", "codex", opaque)


def _scoped_entry_ids(cache, conversations, paths):
    seen = set()
    for root_key, conversation_key in conversations:
        for (entry_id,) in cache.execute(
            "SELECT id FROM codex_session_entries "
            "WHERE conversation_key=? AND source_root_key=?",
            (conversation_key, root_key),
        ):
            seen.add(entry_id)
    for root_key, path in paths:
        for (entry_id,) in cache.execute(
            "SELECT id FROM codex_session_entries "
            "WHERE source_root_key=? AND source_path=?",
            (root_key, path),
        ):
            seen.add(entry_id)
    return seen


def _rebuild_state(ns):
    """Republish the Codex state over the CURRENT store.

    The precedence fixture adds its shapes after `_seed_store` published, so
    the published project rows have to be rebuilt before a detail request can
    name them.
    """
    source_module = sys.modules["_cctally_dashboard_sources"]
    cache = ns["open_cache_db"]()
    stats = ns["open_db"]()
    try:
        semantics = source_module.resolve_dashboard_source_semantics(
            {}, display_tz_name="UTC")
        return source_module.build_codex_source_state(
            source_module.DashboardReadContext(
                cache_conn=cache, stats_conn=stats,
                range_start=NOW - dt.timedelta(days=365), now_utc=NOW,
                display_tz_name=semantics.display_tz_name,
                week_start_idx=semantics.week_start_idx,
                week_start_name=semantics.week_start_name,
                speed=semantics.speed, codex_budget=semantics.codex_budget,
            ),
            data_version="codex-s9-project-v1",
        )
    finally:
        cache.close()
        stats.close()


# ── the query plans the row counter cannot see ─────────────────────────────

def _entries_plan_node(conn, sql, params):
    """The `EXPLAIN QUERY PLAN` node for the `codex_session_entries` leg.

    SQLite names the node by the statement's ALIAS (`entries`), not by the
    table, so matching on the table name finds nothing at all.
    """
    nodes = [
        str(row[3]) for row in conn.execute(
            "EXPLAIN QUERY PLAN " + sql, params)
    ]
    entries = [
        node for node in nodes
        if node.startswith(("SEARCH entries ", "SCAN entries"))
    ]
    assert entries, f"no `entries` node in the plan: {nodes}"
    return entries[0], nodes


def test_both_scoped_accounting_shards_seek_rather_than_scan(precedence):
    """Spec §5.1.1. The row counter observes rows AFTER SQLite applies its
    predicates, so a query that seeks two rows and one that scans the year to
    find the same two are indistinguishable to it. The materialisation
    assertions above therefore cannot establish that the shipped indexes
    suffice, and this repository has been here before:
    `tests/test_codex_entries_root_path_index.py` exists precisely because a
    query returned few rows while visiting the whole table.

    Asserting merely "not a SCAN" would be too weak — a root-only index still
    registers as a SEARCH while visiting every entry row. Each shard is
    therefore checked for the identity column it must be constrained on.

    ONE QUERY PER IDENTITY is what makes this hold. A multi-identity `OR`
    predicate can make SQLite abandon the index and scan, which is why
    production issues one shard per physical group.
    """
    ns, root_key, root_path, target = precedence
    analytics = sys.modules["_cctally_source_analytics"]
    start = _iso(NOW - dt.timedelta(days=365))
    end = _iso(NOW + dt.timedelta(microseconds=1))

    cache = ns["open_cache_db"]()
    try:
        conversation_node, conversation_plan = _entries_plan_node(
            cache, analytics._CONVERSATION_SCOPED_CODEX_ENTRIES_SQL,
            (target, root_key, start, end),
        )
        path_node, path_plan = _entries_plan_node(
            cache, analytics._ROOT_PATH_SCOPED_CODEX_ENTRIES_SQL,
            (root_key, f"{root_path}/sessions/shape2-plain.jsonl", start, end),
        )
    finally:
        cache.close()

    assert "SCAN" not in conversation_node, (
        f"the conversation shard scans the table: {conversation_node} "
        f"(plan {conversation_plan})"
    )
    assert "idx_codex_entries_conversation" in conversation_node, (
        f"expected the conversation index in the plan, got: {conversation_node}"
    )
    assert "conversation_key=?" in conversation_node, (
        "the conversation shard must be constrained on conversation_key, not "
        f"merely registered as a SEARCH: {conversation_node}"
    )

    assert "SCAN" not in path_node, (
        f"the path shard scans the table: {path_node} (plan {path_plan})"
    )
    assert "idx_codex_entries_root_path" in path_node, (
        f"expected the composite root/path index in the plan, got: {path_node}"
    )
    assert "source_path=?" in path_node, (
        "the path shard must be constrained on source_path — a root-only "
        "index still registers as a SEARCH while visiting every entry row: "
        f"{path_node}"
    )

    # NO TEMPORARY SORT. Each shard is one of many whose union
    # `load_codex_project_scoped_entries` re-sorts in Python, so a SQL
    # `ORDER BY` here is superseded the moment it returns — and it is not free,
    # because the forced `INDEXED BY` cannot supply that order, so SQLite builds
    # a temp B-tree per shard to produce an ordering nothing reads.
    for label, plan in (("conversation", conversation_plan), ("path", path_plan)):
        assert not any("TEMP B-TREE" in node.upper() for node in plan), (
            f"the {label} shard sorts into a temporary B-tree for an ordering "
            f"the Python re-sort immediately supersedes: {plan}"
        )


def test_the_pinned_plans_run_over_a_non_empty_result(precedence):
    """Non-vacuity guard, following `tests/test_codex_entries_root_path_index.py`:
    the statements the plan test pins must actually select rows, so a future
    edit that empties the result cannot leave the plan assertions passing over
    nothing."""
    ns, root_key, root_path, target = precedence
    analytics = sys.modules["_cctally_source_analytics"]
    start = _iso(NOW - dt.timedelta(days=365))
    end = _iso(NOW + dt.timedelta(microseconds=1))
    cache = ns["open_cache_db"]()
    try:
        by_conversation = cache.execute(
            analytics._CONVERSATION_SCOPED_CODEX_ENTRIES_SQL,
            (target, root_key, start, end),
        ).fetchall()
        by_path = cache.execute(
            analytics._ROOT_PATH_SCOPED_CODEX_ENTRIES_SQL,
            (root_key, f"{root_path}/sessions/shape2-plain.jsonl", start, end),
        ).fetchall()
    finally:
        cache.close()
    assert by_conversation, "the conversation shard selected nothing"
    assert by_path, "the path shard selected nothing"


def test_a_project_key_resolving_to_nothing_is_a_not_found(
    precedence, monkeypatch,
):
    """Spec §4.5. `source_detail_lookup` stays the first gate and keeps
    enforcing frozen-bundle membership, so a key it admits but that resolves to
    no conversation and no entry in the window must still raise
    `SourceResourceNotFound` rather than render an empty project.
    """
    ns, _root_key, _root_path, _target = precedence
    dashboard = sys.modules["_cctally_dashboard"]
    sources = sys.modules["_cctally_dashboard_sources"]
    codex = _rebuild_state(ns)
    row = _target_project_row(codex)

    # Admitted by the bundle gate, resolvable by nothing in the store. The
    # published state is frozen, so the extra row goes into a rebuilt copy
    # rather than being written into its mapping.
    orphan = sources.dashboard_resource_key(
        "project", "codex", "project:000000000000000000000000")
    published = dict(row)
    published["key"] = orphan
    data = dict(codex.data)
    data["projects"] = {
        **dict(codex.data["projects"]),
        "rows": (*codex.data["projects"]["rows"], published),
    }
    codex = dataclasses.replace(codex, data=data)
    snap = _snapshot(ns, codex)

    with pytest.raises(sources.SourceResourceNotFound):
        dashboard.build_source_detail(
            snapshot=snap, source="codex", resource="project", key=orphan,
        )


def test_a_transient_read_failure_degrades_the_project_route_and_says_so(
    tmp_path, monkeypatch,
):
    """Spec §4.5's other half, retargeted by #834 S2 (#829) rather than kept.

    This case forces `QualifiedMetadataUnavailable` during the GENERATION
    build over an otherwise healthy store, which is precisely the TRANSIENT
    case: the store underneath is fine, and the retired detail's own year-long
    read would have succeeded here and rendered a full page. Keeping its
    malformed-partial expectation would contradict D5, because the reader
    would be told to rebuild a Codex cache that is not the problem.

    What it pins now is the transient state end to end — the typed carrier,
    the null row count, the EMPTY project rows and the refusal to reuse the
    degraded generation. Clearing at the next healthy publish is pinned in
    `tests/test_828_metadata_probe.py`, which owns its store's lifetime and can
    rebuild over it.

    RETARGETED AGAIN by #846 §4.6 rule 1, which states the change to this
    S2-pinned behavior plainly. This test used to assert that the transient
    generation still published rows through the partial fold, which it could do
    only because its fixture monkeypatches `load_qualified_codex_entries` alone
    and the real conversation-metadata read supplied the identities. A
    generation whose metadata legs failed has established nothing about the
    rows, so it now publishes none of them and the project route answers
    `source_resource_not_found`. The detail-disclosure assertions moved to the
    malformed generation, which is A3.
    """
    import _lib_dashboard_sources as lds

    ns = load_script()
    dashboard = sys.modules["_cctally_dashboard"]
    sources = sys.modules["_cctally_dashboard_sources"]
    _root_key, codex = _seed_store(
        ns, tmp_path, monkeypatch, metadata_incomplete=True)
    snap = _snapshot(ns, codex)
    assert codex.data["projects"]["rows"] == (), (
        "a transient generation must publish no Codex project row")

    assert codex.metadata_health == {
        "state": "transient_read_failure",
        "incomplete_rows": None,
        "retryable": True,
    }
    assert dashboard._codex_generation_metadata_state(snap) == (
        "transient_read_failure")

    # A key that the healthy generation DOES publish is not reachable through
    # this one, because the frozen bundle carries no row to admit.
    _root_key, healthy = _seed_store(ns, tmp_path / "healthy", monkeypatch)
    healthy_rows = healthy.data["projects"]["rows"]
    assert healthy_rows, "the healthy control published no project row"
    with pytest.raises(sources.SourceResourceNotFound):
        dashboard.build_source_detail(
            snapshot=snap, source="codex", resource="project",
            key=str(healthy_rows[0]["key"]),
        )

    # The SOURCE WARNING has to draw the same distinction the route note
    # already draws. Its message was derived from `health.incomplete_rows`
    # before `metadata_incomplete` was reassigned from the accounting capture,
    # so a transient failure — which leaves that count at zero — selected the
    # rebuild sentence: the chip told the reader to rebuild a Codex cache that
    # is not the problem while the note beside it said the build would retry.
    # No test pinned the message text, which is how the two halves of D5 came
    # apart, so the text is asserted here rather than only the code.
    metadata_warnings = [
        warning for warning in codex.warnings
        if warning.code == "codex_metadata_incomplete"
    ]
    assert len(metadata_warnings) == 1
    assert metadata_warnings[0].message == (
        "Codex project metadata could not be read for this build; it will "
        "retry on the next refresh."
    )
    assert "cache-sync" not in metadata_warnings[0].message
    assert "--rebuild" not in metadata_warnings[0].message

    # A degraded generation is never handed back unexamined (#830).
    assert codex.availability == "partial"
    assert lds.reuse_coherent_source_state(
        codex, data_version=codex.data_version) is None


@pytest.mark.parametrize("transient,expected_reason", [
    (True, "This build could not check project metadata health; it will retry "
           "on the next refresh."),
    (False, "Project metadata is unavailable for this item."),
])
def test_a_healthy_generation_still_discloses_a_failed_live_project_read(
    tmp_path, monkeypatch, transient, expected_reason,
):
    """#834 S2. A degraded serve must never report complete attribution.

    The project route is not served from the frozen bundle. `source_detail_lookup`
    gates membership only, and `_build_codex_project_detail` then opens its own
    `cache.db` connection and reads a YEAR of live rows, because the published
    row covers roughly thirty days and carries neither `models` nor a
    per-session breakdown. The generation carrier and this request's read are
    therefore two different measurements over two different windows, and a
    healthy carrier cannot answer for a live read that raised.

    Consulting it anyway published `metadata_availability: null` over the
    truncated cache-only fallback payload, so the reader was told attribution
    was complete on a page the route had just failed to build. `origin/main`
    published `partial` here unconditionally, which makes the carrier lookup a
    regression rather than a gap. Every other partial assertion in this estate
    forces its failure during GENERATION construction, which leaves the
    carrier non-healthy and so cannot reach this combination at all.

    Both directions are pinned, because the cause decides the remedy: a
    transient read says it will retry, and a deterministically unqualifiable
    row is the only state that may name the cache rebuild.
    """
    ns = load_script()
    dashboard = sys.modules["_cctally_dashboard"]
    analytics = sys.modules["_cctally_source_analytics"]
    _root_key, codex = _seed_store(ns, tmp_path, monkeypatch)
    snap = _snapshot(ns, codex)
    assert codex.metadata_health["state"] == "healthy", (
        "precondition: the generation carrier must read healthy, or this test "
        "reduces to the already-covered non-healthy case")
    row = _target_project_row(codex)
    key = str(row["key"])

    def _raise(*_args, **_kwargs):
        raise analytics.QualifiedMetadataUnavailable(
            "live project read failed", transient=transient,
        )

    # NOT `monkeypatch.undo()`: that would also undo `redirect_paths`, and the
    # isolation guard then catches the test writing to the maintainer's real
    # production data directory. Restore exactly the one attribute.
    original = analytics.load_codex_project_scoped_entries
    analytics.load_codex_project_scoped_entries = _raise
    try:
        detail = dashboard.build_source_detail(
            snapshot=snap, source="codex", resource="project", key=key,
        )
    finally:
        analytics.load_codex_project_scoped_entries = original

    assert detail["metadata_availability"] == "partial"
    assert detail["metadata_reason"] == expected_reason
    assert detail["key"] == row["key"]


# ── #845/#846 §7.1 — the block measurement that precedes the #846 change ────

#: The measured count of `quota.blocks` a healthy generation publishes over the
#: §7.1 store. Taken from the executed measurement of 2026-09-16 on this tree,
#: recorded in the A0 commit body; it is an oracle, never recomputed here.
_SEVEN_ONE_BLOCK_COUNT = 2


def test_845_a_degraded_generation_publishes_the_same_quota_blocks(
    tmp_path, monkeypatch,
):
    """Spec §7.1 / A17. #846 claims a degraded build empties `quota.blocks`.

    `_quota_wire` reads `quota_window_blocks` and the supplied accounting
    entries only; it never consults project metadata. This measures that on
    THIS tree rather than assuming it, over a store seeded with a 300-minute
    quota block, and pins the measured count as a hard-coded oracle.

    Expected to pass before and after the #846 change: the malformed arm is
    reached through an entry with an empty conversation key, a shape the
    accounting-window health read and the one-year probe already counted before
    this session, and the transient arm is reached through the real capture's
    `QualifiedMetadataUnavailable` handler.
    """
    ns = load_script()
    _root_key, healthy = _seed_store(ns, tmp_path, monkeypatch)
    assert healthy.metadata_health["state"] == "healthy"

    _root_key, malformed = _seed_store(
        ns, tmp_path / "malformed", monkeypatch, malformed_rows=True)
    assert malformed.metadata_health["state"] == "malformed_row_partial"

    _root_key, transient = _seed_store(
        ns, tmp_path / "transient", monkeypatch, metadata_incomplete=True)
    assert transient.metadata_health["state"] == "transient_read_failure"

    measured = (
        len(healthy.data["quota"]["blocks"]),
        len(malformed.data["quota"]["blocks"]),
        len(transient.data["quota"]["blocks"]),
    )
    assert measured == (
        _SEVEN_ONE_BLOCK_COUNT, _SEVEN_ONE_BLOCK_COUNT, _SEVEN_ONE_BLOCK_COUNT
    ), (
        "a malformed and a transient generation must both publish the healthy "
        "generation's blocks"
    )


# ── #845 A7 — the inventory replaces the quadratic join ────────────────────


def _inventory_store():
    """A minimal store carrying only the two tables the inventory reads."""
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE codex_conversation_threads ("
        " conversation_key TEXT PRIMARY KEY, source_root_key TEXT,"
        " native_thread_id TEXT, last_seen_utc TEXT, cwd TEXT, git_json TEXT)"
    )
    conn.execute(
        "CREATE TABLE codex_session_files ("
        " path TEXT PRIMARY KEY, source_root_key TEXT,"
        " last_native_thread_id TEXT)"
    )
    return conn


def test_845_the_inventory_reads_each_table_once_and_issues_no_per_file_query():
    """A7. The retired statement joined `codex_session_files` to
    `codex_conversation_threads` for every file row, so on a one-root machine
    every file rescanned all 2,646 threads. A row count cannot see that, so
    this counts the STATEMENTS that reach each table."""
    analytics = sys.modules["_cctally_source_analytics"]
    conn = _inventory_store()
    try:
        for index in range(20):
            conn.execute(
                "INSERT INTO codex_conversation_threads VALUES (?,?,?,?,?,?)",
                (f"conv-{index}", "root", f"native-{index}",
                 f"2026-07-0{index % 9 + 1}T00:00:00+00:00",
                 f"/repo/{index}", None),
            )
        for index in range(30):
            conn.execute(
                "INSERT INTO codex_session_files VALUES (?,?,?)",
                (f"/rollouts/{index}.jsonl", "root", f"native-{index % 20}"),
            )
        conn.commit()

        statements: list[str] = []
        conn.set_trace_callback(statements.append)
        inventory = analytics.load_codex_thread_metadata_inventory(conn)
        conn.set_trace_callback(None)

        threads = [s for s in statements if "codex_conversation_threads" in s
                   and "sqlite_master" not in s]
        files = [s for s in statements if "codex_session_files" in s
                 and "sqlite_master" not in s and not s.startswith("PRAGMA")]
        assert len(threads) == 1, threads
        assert len(files) == 1, files
        assert len(inventory.inherited_by_path) == 30
        assert inventory.undecodable_direct == frozenset()
        assert inventory.undecodable_inherited == frozenset()
    finally:
        conn.close()


def test_845_the_linear_alias_map_preserves_the_sql_winner():
    """A5. Two candidate inherited rows of different `last_seen_utc`, and one
    NULL beside one non-NULL. SQLite sorts NULL FIRST ascending, so a NULL
    `last_seen_utc` sorts LAST under the retired statement's
    `ORDER BY last_seen_utc DESC, conversation_key DESC`, and the qualifier
    kept the FIRST row. Regression row for #845, not a RED proof."""
    analytics = sys.modules["_cctally_source_analytics"]
    conn = _inventory_store()
    try:
        conn.executemany(
            "INSERT INTO codex_conversation_threads VALUES (?,?,?,?,?,?)",
            [
                ("older", "root", "shared", "2026-07-01T00:00:00+00:00",
                 "/repo/older", None),
                ("newer", "root", "shared", "2026-07-09T00:00:00+00:00",
                 "/repo/newer", None),
                ("null-a", "root", "nulls", None, "/repo/null-a", None),
                ("null-b", "root", "nulls", None, "/repo/null-b", None),
                ("stored", "root", "nulls", "2026-01-01T00:00:00+00:00",
                 "/repo/stored", None),
            ],
        )
        conn.executemany(
            "INSERT INTO codex_session_files VALUES (?,?,?)",
            [("/rollouts/shared.jsonl", "root", "shared"),
             ("/rollouts/nulls.jsonl", "root", "nulls")],
        )
        conn.commit()
        inventory = analytics.load_codex_thread_metadata_inventory(conn)
    finally:
        conn.close()

    assert inventory.inherited_winner_by_path[("root", "/rollouts/shared.jsonl")] == (
        "root", "newer")
    assert inventory.inherited_by_path[("root", "/rollouts/shared.jsonl")].cwd == (
        "/repo/newer")
    # A stored timestamp beats both NULLs, whatever their conversation keys.
    assert inventory.inherited_winner_by_path[("root", "/rollouts/nulls.jsonl")] == (
        "root", "stored")
    assert inventory.inherited_by_path[("root", "/rollouts/nulls.jsonl")].cwd == (
        "/repo/stored")


# ── #845 A3 — the partial generation over six shapes ───────────────────────

from test_dashboard_source_read_model import (  # noqa: E402
    _cache_root_key,
    _install_active_native_cycle,
    _seeded_context,
)
from test_dashboard_accounts_wire import _seed_codex_accounts  # noqa: E402

A3_BAD = b"/synthetic/\xffundecodable"
A3_ACCOUNT_X = "x" * 32
A3_ACCOUNT_Y = "y" * 32


def _a3_thread(cache, *, key, root, native, path, cwd=None, git_json=None):
    cache.execute(
        "INSERT INTO codex_conversation_threads "
        "(conversation_key, source_root_key, native_thread_id, root_thread_id,"
        " source_path, cwd, git_json, first_seen_utc, last_seen_utc) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (key, root, native, native, path, cwd, git_json,
         _iso(NOW - dt.timedelta(days=10)), _iso(NOW)),
    )


def _a3_alias(cache, *, root, path, native):
    cache.execute(
        "INSERT INTO codex_session_files "
        "(path, size_bytes, mtime_ns, last_byte_offset, last_ingested_at,"
        " source_root_key, last_native_thread_id) VALUES (?,0,0,0,?,?,?)",
        (path, _iso(NOW), root, native),
    )


def _a3_entry(cache, *, root, path, key, offset, account_key=None):
    cache.execute(
        "INSERT INTO codex_session_entries "
        "(source_path, line_offset, timestamp_utc, session_id, model, "
        "input_tokens, cached_input_tokens, output_tokens, "
        "reasoning_output_tokens, total_tokens, source_root_key, "
        "conversation_key, account_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (path, offset, _iso(NOW - dt.timedelta(hours=3)), f"session-{offset}",
         "gpt-5", 100, 10, 20, 5, 130, root, key, account_key),
    )


@pytest.fixture
def a3_store(tmp_path, monkeypatch):
    """One healthy project plus the six shapes A3 names, all in window."""
    ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    source_module = sys.modules["_cctally_dashboard_sources"]
    root = _cache_root_key(cache)
    paths = {
        name: f"/synthetic/a3/{name}.jsonl"
        for name in ("healthy", "e1", "e2", "inherited", "emptykey",
                     "nojoin", "shared")
    }
    _a3_thread(cache, key="a3-healthy", root=root, native="n-healthy",
               path=paths["healthy"], cwd="/synthetic/project-healthy")
    _a3_entry(cache, root=root, path=paths["healthy"], key="a3-healthy",
              offset=20_001)

    # 1. E1 — undecodable direct `cwd` beside a VALID `git_json`, so revision
    #    4's map would have published it as a Git project.
    _a3_thread(cache, key="a3-e1", root=root, native="n-e1", path=paths["e1"],
               cwd=None, git_json='{"repository":"fixture"}')
    cache.execute(
        "UPDATE codex_conversation_threads SET cwd = CAST(? AS TEXT) "
        "WHERE conversation_key = 'a3-e1'", (A3_BAD,))
    _a3_entry(cache, root=root, path=paths["e1"], key="a3-e1", offset=20_002)

    # 2. E2 — empty direct metadata whose path's alias winner is undecodable.
    _a3_thread(cache, key="a3-bad-alias", root=root, native="n-bad-alias",
               path="/synthetic/a3/bad-alias.jsonl", cwd=None)
    cache.execute(
        "UPDATE codex_conversation_threads SET cwd = CAST(? AS TEXT) "
        "WHERE conversation_key = 'a3-bad-alias'", (A3_BAD,))
    _a3_thread(cache, key="a3-e2", root=root, native="n-e2", path=paths["e2"],
               cwd="", git_json="")
    _a3_alias(cache, root=root, path=paths["e2"], native="n-bad-alias")
    _a3_entry(cache, root=root, path=paths["e2"], key="a3-e2", offset=20_003)

    # 3. Healthy through inheritance — empty direct metadata, valid winner.
    _a3_thread(cache, key="a3-good-alias", root=root, native="n-good-alias",
               path="/synthetic/a3/good-alias.jsonl",
               cwd="/synthetic/project-inherited")
    _a3_thread(cache, key="a3-inherited", root=root, native="n-inherited",
               path=paths["inherited"], cwd="", git_json="")
    _a3_alias(cache, root=root, path=paths["inherited"], native="n-good-alias")
    _a3_entry(cache, root=root, path=paths["inherited"], key="a3-inherited",
              offset=20_004)

    # 4. An EMPTY conversation key on a path whose alias winner is valid. The
    #    reader refuses it and the aggregate counts it; the map must not
    #    attribute it through that winner.
    _a3_alias(cache, root=root, path=paths["emptykey"], native="n-good-alias")
    _a3_entry(cache, root=root, path=paths["emptykey"], key="", offset=20_005)

    # 5. A nonempty key with neither a thread row nor an alias winner.
    _a3_entry(cache, root=root, path=paths["nojoin"], key="a3-missing",
              offset=20_006)

    # 6. One rollout path hosting a healthy conversation owned by account X
    #    and an E1 conversation owned by account Y.
    _a3_thread(cache, key="a3-shared-healthy", root=root,
               native="n-shared-healthy", path=paths["shared"],
               cwd="/synthetic/project-shared")
    _a3_thread(cache, key="a3-shared-bad", root=root, native="n-shared-bad",
               path=paths["shared"], cwd=None)
    cache.execute(
        "UPDATE codex_conversation_threads SET cwd = CAST(? AS TEXT) "
        "WHERE conversation_key = 'a3-shared-bad'", (A3_BAD,))
    _a3_entry(cache, root=root, path=paths["shared"], key="a3-shared-healthy",
              offset=20_007, account_key=A3_ACCOUNT_X)
    _a3_entry(cache, root=root, path=paths["shared"], key="a3-shared-bad",
              offset=20_008, account_key=A3_ACCOUNT_Y)
    cache.commit()

    _seed_codex_accounts(stats, [
        {"account_key": A3_ACCOUNT_X, "natural_id": "x", "email": "x@example",
         "label": "X", "plan_type": "pro"},
        {"account_key": A3_ACCOUNT_Y, "natural_id": "y", "email": "y@example",
         "label": "Y", "plan_type": "pro"},
    ])
    _install_active_native_cycle(
        monkeypatch, source_module, reset=NOW + dt.timedelta(days=2), root=root)
    try:
        yield ns, cache, stats, root, paths
    finally:
        cache.close()
        stats.close()


def _a3_state(cache, stats):
    source_module = sys.modules["_cctally_dashboard_sources"]
    from test_dashboard_source_read_model import START as READ_MODEL_START

    return source_module.build_codex_source_state(
        source_module.DashboardReadContext(
            cache_conn=cache, stats_conn=stats, range_start=READ_MODEL_START,
            now_utc=NOW, display_tz_name="UTC",
        ),
        data_version="a3-v1",
    )


def test_845_a3_the_partial_generation_publishes_every_surviving_project(
    a3_store,
):
    """A3. Every other real project row is published under its healthy key,
    the affected and refused entries are excluded on every consumer of the
    map, and the project route works.

    It fails under revision 4's map, which attributes the E1 entry as a Git
    project; under any path-keyed map, which cannot decide the shared path;
    under direct-only attribution, which drops the inherited project; and under
    revision 8's map, which attributes the empty-key entry through its alias
    winner and files the missing-join entry as `(unassigned)`.
    """
    _ns, cache, stats, _root, paths = a3_store
    state = _a3_state(cache, stats)

    assert state.metadata_health["state"] == "malformed_row_partial"
    labels = sorted(row["label"] for row in state.data["projects"]["rows"])
    # `project-red` is the base corpus `_seeded_context` ingests.
    assert labels == [
        "project-healthy", "project-inherited", "project-red",
        "project-shared",
    ], labels
    published = repr(state.data["projects"]["rows"])
    assert "Git project" not in published
    assert "(unassigned)" not in published

    # The session rows of every path that carries an unattributed identity.
    by_path = {
        row["key"]: row for row in state.data["sessions"]["rows"]
    }
    assert by_path, "the partial generation published no session rows"
    null_project_rows = [
        row for row in state.data["sessions"]["rows"]
        if row["project"] is None
    ]
    # Exactly the five affected paths: the E1 conversation's, the E2 one's,
    # the empty-key entry's, the missing-join entry's, and the shared path,
    # whose single project field cannot represent one healthy and one
    # malformed conversation at once. Every other path keeps the path map's
    # answer, which is why a healthy path's session row is unchanged.
    assert len(null_project_rows) == 5, [
        row["key"] for row in null_project_rows]
    assert all(row["project_key"] is None for row in null_project_rows)
    # A session row reading `(unassigned)` is a real answer for a thread with
    # no metadata, not a decode failure, so its presence is not a violation;
    # what matters is that no AFFECTED path carries a project at all.

    # The account children. The shared rollout path hosts a healthy
    # conversation owned by X and an E1 conversation owned by Y, so a
    # path-keyed decision could not tell them apart; each child computes the
    # unattributed set over its OWN partition.
    scopes = state.data["account_scopes"]
    assert set(scopes) == {A3_ACCOUNT_X, A3_ACCOUNT_Y, "unattributed"}, sorted(
        scopes)
    child_labels = {
        key: sorted(row["label"] for row in child["projects"]["rows"])
        for key, child in scopes.items()
    }
    assert child_labels[A3_ACCOUNT_X] == ["project-shared"]
    assert child_labels[A3_ACCOUNT_Y] == []
    assert child_labels["unattributed"] == [
        "project-healthy", "project-inherited", "project-red"]
    for key, child in scopes.items():
        published_child = repr(child["projects"]["rows"])
        assert "Git project" not in published_child, key
        assert "(unassigned)" not in published_child, key

    # X and Y own one entry each, both on the shared rollout path, so each
    # child publishes exactly one session row and the two rows describe the
    # SAME path under different owners. A session row's single project field
    # cannot represent a path that hosts one healthy and one malformed
    # conversation, so the decision has to be per child.
    x_rows = scopes[A3_ACCOUNT_X]["sessions"]["rows"]
    y_rows = scopes[A3_ACCOUNT_Y]["sessions"]["rows"]
    assert len(x_rows) == 1 and len(y_rows) == 1, (x_rows, y_rows)
    assert x_rows[0]["key"] == y_rows[0]["key"], "both must be the shared path"
    assert x_rows[0]["project"] == "project-shared"
    assert x_rows[0]["project_key"] is not None
    assert y_rows[0]["project"] is None
    assert y_rows[0]["project_key"] is None

    # Cache-report's project fold. Every affected and refused entry lands in
    # the fold's own `(unknown)` bucket; no Git, inherited, path-derived or
    # `(unassigned)` bucket is minted for one.
    by_project = sorted(
        row["key"] for row in state.data["cache_report"]["by_project"])
    assert by_project == [
        "(unknown)", "project-healthy", "project-inherited", "project-red",
        "project-shared",
    ], by_project


def test_845_a3_the_two_refused_entries_are_counted_by_their_own_reasons(
    a3_store,
):
    """The map, the counter and the reader agree: the same two refusals, in
    the same order, before the predicate."""
    _ns, cache, _stats, _root, _paths = a3_store
    analytics = sys.modules["_cctally_source_analytics"]
    health = analytics.load_codex_project_metadata_health(cache_conn=cache)
    # The EXACT seeded counts, not a floor. `>= 1` passes for a build that
    # files every one of the eight seeded entries under a single reason, which
    # is the disagreement between the map, the counter and the reader this row
    # exists to refuse.
    assert health.total_rows == 9
    assert health.missing_conversation_key_rows == 1, "the empty-key entry"
    assert health.missing_thread_join_rows == 1, "the no-thread, no-alias entry"
    # E1, E2 and the shared path's E1 conversation.
    assert health.undecodable_metadata_rows == 3
    assert health.qualified_rows == 4


def test_845_a3_the_project_route_reaches_every_surviving_key(a3_store):
    """`source_detail_lookup` resolves every published key, and the detail
    discloses the MALFORMED reason rather than the transient retry sentence."""
    ns, cache, stats, _root, _paths = a3_store
    dashboard = sys.modules["_cctally_dashboard"]
    state = _a3_state(cache, stats)
    snap = _snapshot(ns, state)
    rows = state.data["projects"]["rows"]
    assert rows
    for row in rows:
        detail = dashboard.build_source_detail(
            snapshot=snap, source="codex", resource="project",
            key=str(row["key"]),
        )
        assert detail["metadata_availability"] == "partial"
        assert detail["metadata_reason"] == (
            "Project metadata is unavailable for this item.")


# ── #845 §4.3 — ONE inventory read per build ───────────────────────────────
#
# Section 4.3 states one inventory read per build. The health counter, the
# probe counter, the qualified reader and — in a partial generation — the
# identity map each loaded the whole thread-and-alias inventory for
# themselves, so a build paid the 2,646-row thread pass three or four times
# over. A row count cannot see that, so this counts the STATEMENTS that carry
# the inventory's own text.

_INVENTORY_THREAD_MARKER = (
    "SELECT conversation_key, source_root_key, native_thread_id, last_seen_utc")
_INVENTORY_ALIAS_MARKER = (
    "SELECT files.source_root_key, files.path, files.last_native_thread_id")


def _inventory_statement_counts(statements):
    threads = [s for s in statements if _INVENTORY_THREAD_MARKER in s]
    aliases = [s for s in statements if _INVENTORY_ALIAS_MARKER in s]
    return threads, aliases


def test_845_the_partial_build_reads_the_inventory_exactly_once(a3_store):
    """A partial generation runs every consumer: both counters, the qualified
    reader (whose deterministic refusal is what makes the generation partial)
    and the identity map. One inventory read serves all four."""
    _ns, cache, stats, _root, _paths = a3_store
    statements: list[str] = []
    cache.set_trace_callback(statements.append)
    try:
        state = _a3_state(cache, stats)
    finally:
        cache.set_trace_callback(None)

    assert state.metadata_health["state"] == "malformed_row_partial"
    threads, aliases = _inventory_statement_counts(statements)
    assert len(threads) == 1, threads
    assert len(aliases) == 1, aliases


def test_845_the_healthy_build_reads_the_inventory_exactly_once(
    tmp_path, monkeypatch,
):
    """A healthy generation runs both counters and the qualified reader, which
    loaded the inventory three times between them. It builds no identity map,
    so one read is still the whole cost."""
    _ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    source_module = sys.modules["_cctally_dashboard_sources"]
    from test_dashboard_source_read_model import START as READ_MODEL_START

    root = _cache_root_key(cache)
    _install_active_native_cycle(
        monkeypatch, source_module, reset=NOW + dt.timedelta(days=2), root=root)
    try:
        statements: list[str] = []
        cache.set_trace_callback(statements.append)
        try:
            state = source_module.build_codex_source_state(
                source_module.DashboardReadContext(
                    cache_conn=cache, stats_conn=stats,
                    range_start=READ_MODEL_START, now_utc=NOW,
                    display_tz_name="UTC",
                ),
                data_version="f8-healthy",
            )
        finally:
            cache.set_trace_callback(None)
        assert state.metadata_health["state"] == "healthy"
        threads, aliases = _inventory_statement_counts(statements)
        assert len(threads) == 1, threads
        assert len(aliases) == 1, aliases
    finally:
        cache.close()
        stats.close()
