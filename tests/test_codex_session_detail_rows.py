"""#769 S6 / #781 — Codex session detail is served from the published row.

The expensive path was never needed to find the row. ``source_detail_lookup``
already reads the frozen published bundle with no I/O, matches
``data["sessions"]["rows"]`` by key and enforces account ownership, and the
route holds that row before it calls the builder. The builder then reloaded a
full year of qualified Codex entries — the retired ``_codex_detail_inputs``
set ``range_start = now_utc - timedelta(days=365)`` — rebuilt every session row
and linearly rescanned them, recomputing ``dashboard_resource_key`` per row, to
find the same row again.

The decisive guard here is a data-access counter rather than a latency proxy: a
session-detail request must materialise ZERO accounting rows, and neither the
read-context opener ``_codex_detail_context`` (#769 S9 / #815 replaced
``_codex_detail_inputs`` with it) nor ``build_codex_session_view`` may be
called. A query-plan assertion would be implementation-sensitive and would not
prove runtime materialisation; a builder call count alone would not prove that
no unrelated rows were loaded first.
"""
from __future__ import annotations

import datetime as dt
import pathlib
import shutil
import sqlite3
import sys
from types import SimpleNamespace

import pytest

from _lib_dashboard_sources import (
    SOURCE_SCHEMA_VERSION,
    CapabilityRecord,
    SourceDashboardBundle,
    SourceDashboardState,
    compose_all_state,
)
from _lib_source_identity import identity_path, identity_path_alias
from conftest import load_script, redirect_paths


UTC = dt.timezone.utc
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
CORPUS = REPO_ROOT / "tests" / "fixtures" / "codex-parity" / "v1" / "rollouts"
NOW = dt.datetime(2026, 7, 16, 18, tzinfo=UTC)

#: Every accounting relation a Codex detail read could materialise rows from.
#: The counter below is scoped to these rather than to all SQL, because the
#: published bundle is already frozen in memory and the route legitimately
#: touches nothing else.
_ACCOUNTING_RELATIONS = (
    "codex_session_entries",
    "codex_conversation_threads",
    "quota_window_snapshots",
)


# ── the data-access counter ────────────────────────────────────────────────

class _CountingCursor:
    """A cursor that tallies every row an accounting statement yields."""

    def __init__(self, inner, tally, sql):
        self._inner = inner
        self._tally = tally
        self._sql = sql

    def _count(self, rows):
        lowered = self._sql.lower()
        for relation in _ACCOUNTING_RELATIONS:
            if relation in lowered:
                self._tally[relation] = self._tally.get(relation, 0) + len(rows)
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
    """A connection proxy that counts materialised accounting rows."""

    def __init__(self, inner, tally):
        self._inner = inner
        self._tally = tally

    def execute(self, sql, *args, **kwargs):
        return _CountingCursor(
            self._inner.execute(sql, *args, **kwargs), self._tally, str(sql),
        )

    def cursor(self, *args, **kwargs):                      # pragma: no cover
        raise AssertionError(
            "a detail route opened a raw cursor, which this counter cannot see"
        )

    def __getattr__(self, name):
        return getattr(self._inner, name)


# ── corpus and published state ─────────────────────────────────────────────

def _seed_corpus(ns, tmp_path, monkeypatch):
    root = tmp_path / "provider"
    rollout = root / "sessions" / "2026" / "07" / "16" / "rollout.jsonl"
    rollout.parent.mkdir(parents=True)
    shutil.copyfile(CORPUS / "modern-full.jsonl", rollout)
    monkeypatch.setenv("CODEX_HOME", str(root))
    cache = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](cache)
    finally:
        cache.close()
    return root


def _build_codex_state(ns, *, incomplete_metadata=False, strip_project=False):
    source_module = sys.modules["_cctally_dashboard_sources"]
    cache = ns["open_cache_db"]()
    stats = ns["open_db"]()
    try:
        if strip_project:
            # A rollout with no conversation identity at all resolves no
            # metadata, so its published row carries no project label. Both
            # sources have to go: the thread row and the file alias the
            # metadata reader falls back to.
            cache.execute("DELETE FROM codex_conversation_threads")
            cache.execute(
                "UPDATE codex_session_files SET last_native_thread_id=NULL")
            cache.commit()
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
        if incomplete_metadata:
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
                    context, data_version="codex-detail-v1")
            finally:
                source_module.load_qualified_codex_entries = original
        else:
            codex = source_module.build_codex_source_state(
                context, data_version="codex-detail-v1")
    finally:
        cache.close()
        stats.close()
    return codex


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
            "claude": claude,
            "codex": codex,
            "all": compose_all_state(claude, codex),
        },
    )
    return snap


def _published(ns, tmp_path, monkeypatch, **kwargs):
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    root = _seed_corpus(ns, tmp_path, monkeypatch)
    codex = _build_codex_state(ns, **kwargs)
    rows = codex.data["sessions"]["rows"]
    assert rows, "the corpus published no Codex session rows"
    return root, codex, _snapshot(ns, codex), rows


# ── the cases ──────────────────────────────────────────────────────────────

def test_a_session_detail_materialises_no_accounting_rows(tmp_path, monkeypatch):
    """The counter is the guard, not the latency and not the query plan."""
    ns = load_script()
    dashboard = sys.modules["_cctally_dashboard"]
    cache_module = sys.modules["_cctally_cache"]
    _root, _codex, snap, rows = _published(ns, tmp_path, monkeypatch)

    tally: dict[str, int] = {}
    opened: list[str] = []
    real_open_cache_db = cache_module.open_cache_db

    def _counting_open_cache_db(*args, **kwargs):
        opened.append("cache")
        return _CountingConnection(
            real_open_cache_db(*args, **kwargs), tally)

    monkeypatch.setattr(cache_module, "open_cache_db", _counting_open_cache_db)

    def _refuse_detail_context(*_args, **_kwargs):
        raise AssertionError(
            "the session detail opened a relational read context at all")

    monkeypatch.setattr(
        dashboard, "_codex_detail_context", _refuse_detail_context)

    def _refuse_session_view(*_args, **_kwargs):
        raise AssertionError(
            "the session detail rebuilt every Codex session row")

    monkeypatch.setattr(
        sys.modules["cctally"], "build_codex_session_view", _refuse_session_view)

    detail = dashboard.build_source_detail(
        snapshot=snap, source="codex", resource="session",
        key=str(rows[0]["key"]),
    )

    assert detail["detail_kind"] == "codex_session"
    assert tally == {}, f"the detail materialised accounting rows: {tally}"
    assert opened == [], "the detail opened a cache connection it does not need"


def test_the_session_detail_carries_every_published_accounting_field(
    tmp_path, monkeypatch,
):
    """Parity by enumeration against the row the route already holds."""
    ns = load_script()
    dashboard = sys.modules["_cctally_dashboard"]
    _root, _codex, snap, rows = _published(ns, tmp_path, monkeypatch)

    for row in rows:
        detail = dashboard.build_source_detail(
            snapshot=snap, source="codex", resource="session",
            key=str(row["key"]),
        )
        assert detail["detail_kind"] == "codex_session"
        assert detail["key"] == row["key"]
        for field in (
            "last_activity", "cost_usd", "input_tokens", "cached_input_tokens",
            "output_tokens", "reasoning_output_tokens", "total_tokens",
        ):
            assert detail[field] == row[field], field
        assert detail["models"] == list(row["models"])
        assert detail["model_breakdowns"] == [
            {
                name: item[name]
                for name in (
                    "modelName", "inputTokens", "cachedInputTokens",
                    "outputTokens", "reasoningOutputTokens", "totalTokens",
                    "cost", "isFallback",
                )
                if name in item
            }
            for item in row["model_breakdowns"]
        ]
        # The four fields the route merges from the row at the call site.
        for field in ("label", "project", "started_at", "duration_min"):
            assert detail[field] == row.get(field), field
        # No raw identity travels with the detail.
        assert "codex_root" not in detail
        assert "session_id_path" not in detail


def test_a_complete_generation_publishes_a_null_metadata_qualifier(
    tmp_path, monkeypatch,
):
    """Case one of three: the status comes from the frozen generation.

    #834 S2 (#829) made both keys explicitly null here rather than omitted.
    That is an intentional wire change, carried by the schema bump to 12: a
    client cannot tell an omitted key from a key this build could not fill,
    and that ambiguity is what the typed health result exists to end.
    """
    ns = load_script()
    dashboard = sys.modules["_cctally_dashboard"]
    _root, codex, snap, rows = _published(ns, tmp_path, monkeypatch)
    assert codex.capabilities["projects"].semantics == "qualified-attribution"
    assert codex.metadata_health["state"] == "healthy"

    detail = dashboard.build_source_detail(
        snapshot=snap, source="codex", resource="session",
        key=str(rows[0]["key"]),
    )
    assert detail["metadata_availability"] is None
    assert detail["metadata_reason"] is None


def test_a_partial_generation_with_a_project_publishes_no_qualifier(
    tmp_path, monkeypatch,
):
    """Case two: partial attribution that still resolved THIS row's project.

    Today's partial branch marks a session partial only when its own row
    carries no project, and that rule is preserved when the status moves to the
    frozen generation.
    """
    ns = load_script()
    dashboard = sys.modules["_cctally_dashboard"]
    _root, codex, snap, rows = _published(
        ns, tmp_path, monkeypatch, incomplete_metadata=True)
    assert codex.capabilities["projects"].semantics == (
        "conversation-metadata-partial")
    with_project = [row for row in rows if row.get("project")]
    assert with_project, "this case needs a row that still resolved a project"

    detail = dashboard.build_source_detail(
        snapshot=snap, source="codex", resource="session",
        key=str(with_project[0]["key"]),
    )
    assert detail["metadata_availability"] is None
    assert detail["metadata_reason"] is None


def test_a_partial_generation_without_a_project_publishes_the_qualifier(
    tmp_path, monkeypatch,
):
    """Case three: partial attribution and no project on this row."""
    ns = load_script()
    dashboard = sys.modules["_cctally_dashboard"]
    _root, codex, snap, rows = _published(
        ns, tmp_path, monkeypatch,
        incomplete_metadata=True, strip_project=True,
    )
    assert codex.capabilities["projects"].semantics == (
        "conversation-metadata-partial")
    without_project = [row for row in rows if not row.get("project")]
    assert without_project, "this case needs a row with no project label"

    detail = dashboard.build_source_detail(
        snapshot=snap, source="codex", resource="session",
        key=str(without_project[0]["key"]),
    )
    assert detail["metadata_availability"] == "partial"
    assert detail["metadata_reason"] == (
        "Project metadata is unavailable for this item.")


def test_the_key_constructions_no_longer_diverge_under_an_active_alias(
    tmp_path, monkeypatch,
):
    """The hazard the published-row path removes, pinned rather than assumed.

    ``_session_wire`` builds the key from ``identity_path(row.codex_root)``
    while the old builder used ``row.codex_root`` raw. ``identity_path`` is a
    no-op OUTSIDE ``identity_path_alias``, so the two constructions can only be
    observed to diverge with an alias set — a test without one proves nothing.
    Under an active alias the old builder scanned for a key the wire never
    published and raised ``SourceResourceNotFound``; serving from the published
    row leaves exactly one construction, so the request resolves.
    """
    ns = load_script()
    dashboard = sys.modules["_cctally_dashboard"]
    source_module = sys.modules["_cctally_dashboard_sources"]
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    root = _seed_corpus(ns, tmp_path, monkeypatch)
    logical = pathlib.Path("/logical/codex-root")

    with identity_path_alias(root, logical):
        codex = _build_codex_state(ns)
        snap = _snapshot(ns, codex)
        rows = codex.data["sessions"]["rows"]
        assert rows

        cache = ns["open_cache_db"]()
        try:
            physical_roots = [
                str(value) for (value,) in cache.execute(
                    "SELECT canonical_root_path FROM codex_source_roots")
            ]
        finally:
            cache.close()
        assert physical_roots
        # The alias is genuinely active and genuinely rewrites this root, so
        # the divergence below is real rather than vacuous.
        assert identity_path(physical_roots[0]) != physical_roots[0]

        # Reconstruct the RETIRED construction over the same view rows the old
        # builder walked: the raw `codex_root`, not `identity_path(...)`.
        # #769 S9 / #815 retired `_codex_detail_inputs`: the year-long
        # qualified load it performed for every resource now belongs to the
        # project path alone, so the retired construction is assembled here
        # from the same two calls that function used to make.
        retired_keys: set[str] = set()
        for context in dashboard._codex_detail_context(snap):
            entries = source_module._codex_entries_from_qualified(
                sys.modules["_cctally_source_analytics"].load_qualified_codex_entries(
                    context.range_start,
                    context.now_utc + dt.timedelta(microseconds=1),
                    speed=context.speed, sync=False,
                    cache_conn=context.cache_conn,
                )
            )
            view = sys.modules["cctally"].build_codex_session_view(
                entries,
                now_utc=context.now_utc,
                tz_name=context.display_tz_name,
                speed=context.speed,
            )
            retired_keys = {
                source_module.dashboard_resource_key(
                    "session", "codex",
                    view_row.codex_root or "single-root",
                    view_row.session_id_path,
                )
                for view_row in view.rows
            }
            break
        published_keys = {str(row["key"]) for row in rows}
        assert retired_keys, "the retired construction produced no keys at all"
        assert retired_keys.isdisjoint(published_keys), (
            "the alias did not separate the two constructions, so this case "
            "cannot observe the divergence it exists for"
        )

        for row in rows:
            published = str(row["key"])
            detail = dashboard.build_source_detail(
                snapshot=snap, source="codex", resource="session",
                key=published,
            )
            assert detail["key"] == published
            assert detail["cost_usd"] == row["cost_usd"]
            assert detail["total_tokens"] == row["total_tokens"]


def test_an_absent_key_is_still_refused_before_anything_is_built(
    tmp_path, monkeypatch,
):
    """``source_detail_lookup`` stays the first operation, so 404 grace and
    account row-ownership are unchanged by construction."""
    ns = load_script()
    dashboard = sys.modules["_cctally_dashboard"]
    _root, _codex, snap, rows = _published(ns, tmp_path, monkeypatch)

    with pytest.raises(dashboard.SourceResourceNotFound):
        dashboard.build_source_detail(
            snapshot=snap, source="codex", resource="session",
            key="session:codex:absent",
        )
    # A key that resolves only to another account is refused rather than
    # served: the qualifier reaches `source_detail_lookup` unchanged.
    with pytest.raises(dashboard.SourceResourceNotFound):
        dashboard.build_source_detail(
            snapshot=snap, source="codex", resource="session",
            key="session:codex:absent", account="someone-else",
        )
