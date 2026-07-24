"""Regression: the dashboard Sessions panel keeps its transcript-derived titles.

#320 (commit cdd5450a) moved the transcript tables out of ``cache.db`` and, with
them, dropped the title attach from ``_tui_build_sessions``. Every
``TuiSessionRow.title`` became ``None``; ``snapshot_to_envelope`` emits the
``title`` key only for a non-None title, so the Session column of the Recent
Sessions card rendered its em-dash fallback for every row.

The restore preserves what #320 actually protected — core accounting must never
BLOCK on the transcript store — rather than the feature it dropped:

* ``read_session_titles_bounded`` uses a RAW read-only connection with a 50ms
  busy timeout, never ``open_conversations_db`` (which applies schema +
  migrations and would wait out the 15s store-policy timeout), and returns ``{}``
  on any failure — missing store, locked store, absent tables.
* It reads only INDEXED columns: the materialized ``conversation_sessions.title``
  rollup plus the ``conversation_ai_titles`` overlay — never the expensive
  ``conversation_messages`` first-prompt scan (#302).
* It runs on the DASHBOARD build only. The terminal TUI has no title consumer,
  so its rows stay title-free and the #320 invariant
  (``test_core_sessions_panel_never_opens_conversation_store``) holds verbatim.
"""

from __future__ import annotations

import datetime as dt
import pathlib
import sqlite3
import sys
import time

import pytest

from conftest import load_script, redirect_paths

_BIN = pathlib.Path(__file__).resolve().parent.parent / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

import _cctally_db as db                      # noqa: E402
import _lib_conversation_query as cq          # noqa: E402

NOW = dt.datetime(2026, 7, 21, tzinfo=dt.timezone.utc)


@pytest.fixture(autouse=True)
def _pin_tz_etc_utc(monkeypatch):
    monkeypatch.setenv("TZ", "Etc/UTC")
    import time as _time
    _time.tzset()


def _conversations_conn():
    """An in-memory DB carrying the conversations schema."""
    conn = sqlite3.connect(":memory:")
    db._apply_conversations_schema(conn)
    return conn


def _seed_titles(conn, *, rollup=(), ai=()):
    for sid, title in rollup:
        conn.execute(
            "INSERT INTO conversation_sessions (session_id, title) VALUES (?,?)",
            (sid, title),
        )
    for sid, title in ai:
        conn.execute(
            "INSERT INTO conversation_ai_titles "
            "(session_id, ai_title, byte_offset) VALUES (?,?,0)",
            (sid, title),
        )
    conn.commit()


def _seed_titles_on_disk(ns, *, rollup=(), ai=()):
    conn = ns["open_conversations_db"]()
    try:
        _seed_titles(conn, rollup=rollup, ai=ai)
    finally:
        conn.close()


def _seed_core_session(ns, session_id):
    """One accounting session in cache.db — the Sessions panel's own store."""
    cache = ns["open_cache_db"]()
    try:
        cache.execute(
            "INSERT INTO session_files "
            "(path,size_bytes,mtime_ns,last_byte_offset,last_ingested_at,"
            "session_id,project_path) VALUES (?,100,1,100,"
            "'2026-07-20T00:00:00+00:00',?,'/project')",
            (f"{session_id}.jsonl", session_id),
        )
        cache.execute(
            "INSERT INTO session_entries "
            "(source_path,line_offset,timestamp_utc,model,input_tokens,"
            "output_tokens) VALUES (?,0,'2026-07-20T00:00:00+00:00',"
            "'claude-opus-4-7',10,5)",
            (f"{session_id}.jsonl",),
        )
        cache.commit()
    finally:
        cache.close()


# ---------------------------------------------------------------- kernel ----

def test_indexed_titles_prefer_the_ai_title_over_the_stored_rollup():
    """AI title (truthy) wins; the stored rollup title fills the rest."""
    conn = _conversations_conn()
    _seed_titles(
        conn,
        rollup=[("s-ai", "first prompt text"), ("s-rollup", "rollup only"),
                ("s-blank", "")],
        ai=[("s-ai", "AI Title"), ("s-empty-ai", "")],
    )

    got = cq.session_titles_indexed_map(
        conn, ["s-ai", "s-rollup", "s-blank", "s-empty-ai", "s-missing"])

    assert got == {"s-ai": "AI Title", "s-rollup": "rollup only"}


def test_indexed_titles_never_scan_conversation_messages():
    """The panel read is indexed-only: a session with prompt text but no
    materialized rollup row resolves to NO title (the em-dash fallback), rather
    than paying the windowed ``conversation_messages`` scan #302 removed from
    the hot read path."""
    conn = _conversations_conn()
    conn.execute(
        "INSERT INTO conversation_messages "
        "(session_id, uuid, source_path, byte_offset, timestamp_utc, "
        "entry_type, text, is_sidechain) VALUES ('s-live','u1',"
        "'/p/s-live.jsonl',0,'2026-07-20T00:00:00Z','human',"
        "'Rebuild the Vite bundle',0)"
    )
    conn.commit()
    # Non-vacuity: the expensive scan CAN derive a title for this session.
    assert cq._session_first_prompt_titles_map(conn, ["s-live"]) != {}

    assert cq.session_titles_indexed_map(conn, ["s-live"]) == {}


def test_indexed_titles_tolerate_a_bare_database():
    """Pre-migration / rebuilding store: absent tables degrade to {}, never
    raise."""
    bare = sqlite3.connect(":memory:")

    assert cq.session_titles_indexed_map(bare, ["s-1"]) == {}
    assert cq.session_titles_indexed_map(bare, []) == {}


# ---------------------------------------------------------- bounded read ----

def test_bounded_reader_never_uses_the_heavyweight_opener(tmp_path, monkeypatch):
    """It reads the store, but NOT through ``open_conversations_db`` (schema
    apply + migration dispatcher + 15s policy busy_timeout)."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _seed_titles_on_disk(ns, rollup=[("s-1", "Fix the flaky test")])
    cache_mod = ns["_load_sibling"]("_cctally_cache")

    def forbidden(*_a, **_kw):
        raise AssertionError("bounded title read used open_conversations_db")

    monkeypatch.setattr(cache_mod, "open_conversations_db", forbidden)

    assert cache_mod.read_session_titles_bounded(["s-1"]) == {
        "s-1": "Fix the flaky test"}


def test_bounded_reader_returns_empty_when_the_store_is_missing(
    tmp_path, monkeypatch,
):
    """A transcript store that was never built (or was deleted for a rebuild)
    degrades to no titles — it must not create the file either."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    cache_mod = ns["_load_sibling"]("_cctally_cache")
    path = ns["_cctally_core"].CONVERSATIONS_DB_PATH
    assert not path.exists()

    assert cache_mod.read_session_titles_bounded(["s-1"]) == {}
    assert not path.exists()


def test_bounded_reader_does_not_block_on_an_exclusively_locked_store(
    tmp_path, monkeypatch,
):
    """A store held under ``locking_mode=EXCLUSIVE`` (rebuild / vacuum) must
    fail fast, not stall the sync tick behind the busy timeout."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _seed_titles_on_disk(ns, rollup=[("s-1", "Fix the flaky test")])
    cache_mod = ns["_load_sibling"]("_cctally_cache")
    path = ns["_cctally_core"].CONVERSATIONS_DB_PATH

    holder = sqlite3.connect(str(path))
    try:
        holder.execute("PRAGMA locking_mode=EXCLUSIVE")
        holder.execute("BEGIN IMMEDIATE")
        holder.execute(
            "INSERT INTO conversation_sessions (session_id, title) "
            "VALUES ('s-2','held')"
        )
        started = time.monotonic()
        locked_out = cache_mod.read_session_titles_bounded(["s-1"])
        elapsed = time.monotonic() - started
    finally:
        holder.rollback()
        holder.execute("PRAGMA locking_mode=NORMAL")
        holder.execute("SELECT 1").fetchone()
        holder.close()

    assert locked_out == {}
    assert elapsed < 2.0, f"blocked {elapsed:.2f}s on a locked transcript store"
    # Non-vacuous: the very same call succeeds once the lock is released.
    assert cache_mod.read_session_titles_bounded(["s-1"]) == {
        "s-1": "Fix the flaky test"}


# ------------------------------------------------------------ regression ----

def test_dashboard_sessions_build_attaches_titles(tmp_path, monkeypatch):
    """THE regression: the dashboard's Sessions rows carry their titles again,
    while the core/TUI build stays title-free (#320 invariant)."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _seed_core_session(ns, "session-core")
    _seed_titles_on_disk(ns, rollup=[("session-core", "Fix the flaky test")])

    dashboard_rows = ns["_tui_build_sessions"](
        NOW, skip_sync=True, use_session_cache=False, with_titles=True,
    )
    core_rows = ns["_tui_build_sessions"](
        NOW, skip_sync=True, use_session_cache=False,
    )

    assert [r.session_id for r in dashboard_rows] == ["session-core"]
    assert dashboard_rows[0].title == "Fix the flaky test"
    assert core_rows[0].title is None


def test_dashboard_sessions_build_degrades_when_the_store_is_unavailable(
    tmp_path, monkeypatch,
):
    """No transcript store: the panel still renders its rows, untitled."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _seed_core_session(ns, "session-core")

    rows = ns["_tui_build_sessions"](
        NOW, skip_sync=True, use_session_cache=False, with_titles=True,
    )

    assert [r.session_id for r in rows] == ["session-core"]
    assert rows[0].title is None


def _built_snapshot(ns):
    """One real dashboard build over the seeded tmp env."""
    ns["_load_sibling"]("_lib_snapshot_cache").reset_session_cache_state()
    ns["_load_update_state"] = lambda: None
    ns["_load_update_suppress"] = lambda: {
        "skipped_versions": [], "remind_after": None}
    return ns["_tui_build_snapshot"](
        now_utc=NOW, skip_sync=True, precompute_envelope=True,
        runtime_bind="127.0.0.1",
    )


def _claude_source_row_lists(env):
    """The two SOURCE-scoped Claude session row lists the All tab reads."""
    sources = env.get("sources") or {}
    claude = ((sources.get("claude") or {}).get("data") or {})
    providers = (((sources.get("all") or {}).get("data") or {})
                 .get("providers") or {})
    return [
        ((claude.get("sessions") or {}).get("rows") or []),
        (((providers.get("claude") or {}).get("sessions") or {})
         .get("rows") or []),
    ]


def test_envelope_carries_the_session_title_end_to_end(tmp_path, monkeypatch):
    """The whole chain the regression broke: cache.db + conversations.db ->
    ``_tui_build_snapshot`` (dashboard build) -> envelope ``sessions.rows``."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _seed_core_session(ns, "session-core")
    _seed_titles_on_disk(ns, rollup=[("session-core", "Fix the flaky test")])

    snap = _built_snapshot(ns)
    env = ns["snapshot_to_envelope"](
        snap, now_utc=NOW, monotonic_now=None, transcripts_visible=True,
    )

    rows = env["sessions"]["rows"]
    assert [r["session_id"] for r in rows] == ["session-core"]
    assert rows[0]["title"] == "Fix the flaky test"


def test_source_scoped_claude_rows_carry_the_title_behind_the_same_gate(
    tmp_path, monkeypatch,
):
    """The All tab reads the SOURCE-scoped Claude rows, not the legacy block
    (``dashboard/web/src/lib/sourceRows.ts``), so they need the title too — and
    strictly behind the same per-request gate."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _seed_core_session(ns, "session-core")
    _seed_titles_on_disk(ns, rollup=[("session-core", "Fix the flaky test")])

    snap = _built_snapshot(ns)
    open_env = ns["snapshot_to_envelope"](
        snap, now_utc=NOW, monotonic_now=None, transcripts_visible=True,
    )
    closed_env = ns["snapshot_to_envelope"](
        snap, now_utc=NOW, monotonic_now=None, transcripts_visible=False,
    )

    open_lists = _claude_source_row_lists(open_env)
    assert all(rows for rows in open_lists), "expected both source row lists"
    for rows in open_lists:
        assert [r.get("title") for r in rows] == ["Fix the flaky test"]
    # Same snapshot, gate closed -> the key is absent everywhere, exactly as the
    # legacy block behaves.
    for rows in _claude_source_row_lists(closed_env):
        assert all("title" not in r for r in rows)
    assert all("title" not in r for r in closed_env["sessions"]["rows"])


def test_published_source_bundle_never_stores_transcript_titles(
    tmp_path, monkeypatch,
):
    """The bundle is built ONCE on the sync thread and shared by every SSE
    client, so it must not carry gate-dependent content: the title is injected
    per request, never published. This is what makes the overlay fail closed."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _seed_core_session(ns, "session-core")
    _seed_titles_on_disk(ns, rollup=[("session-core", "Fix the flaky test")])

    snap = _built_snapshot(ns)
    # Non-vacuous: the snapshot DID resolve a title for this session.
    assert [s.title for s in snap.sessions] == ["Fix the flaky test"]

    bundle = snap.source_bundle
    published = bundle.sources["claude"].data["sessions"]["rows"]
    assert published and all("title" not in row for row in published)
