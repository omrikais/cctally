"""The bounded recently-active restat (#769 S6, #716 Task A, #724).

An append to an already-tracked Codex rollout moves no directory mtime, no
schema version and no cursor, and when it lands mid-turn there is no hook
ticket either, so the certificate's 120-second expiry is the only evidence
that ever forces a re-walk. That is the measured 83-, 151- and 165-second lag
in #724.

Restatting every retained rollout would answer it and cost ~2,854 stats on the
production store, on every tick, forever. Restatting the paths that recent
activity NAMES answers it for the case that matters at a cost proportional to
the activity: the union of rollouts cctally itself ingested inside the recency
interval, the paths named by recent valid Codex tickets, and the paths an
interrupted or budget-stopped generation left pending.

Recency is measured by cctally's own observation and commit time. Never by the
rollout's event timestamps, never by the file's mtime, and never by the time
of a verification that found nothing changed — all three are writable by the
thing being observed, and the last one would keep a quiet file permanently
"recent" simply because it kept being looked at.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import sqlite3
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
BIN_DIR = REPO_ROOT / "bin"
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

import _lib_ingest_frontier as frontier  # noqa: E402


# ── the pure classifier ────────────────────────────────────────────────────

def _classify(**kwargs):
    base = dict(
        committed_size=100, committed_offset=100, committed_complete=True,
        committed_device=7, committed_inode=11,
        observed=frontier.ObservedFile(size=100, device=7, inode=11),
    )
    base.update(kwargs)
    return frontier.classify_recent_active_path(**base)


def test_an_unchanged_complete_file_is_unchanged():
    assert _classify() == "unchanged"


def test_a_grown_file_is_an_append():
    assert _classify(
        observed=frontier.ObservedFile(size=180, device=7, inode=11)) == "append"


def test_a_shrunk_file_is_a_cursor_gap():
    assert _classify(
        observed=frontier.ObservedFile(size=40, device=7, inode=11)
    ) == "cursor_gap"


def test_a_new_inode_is_a_replacement_even_when_it_grew():
    """Identity outranks size. A bigger file at a new inode is not an append."""
    assert _classify(
        observed=frontier.ObservedFile(size=180, device=7, inode=12)
    ) == "replaced"


def test_a_vanished_file_is_named_as_such():
    assert _classify(observed=None) == "vanished"


def test_an_incomplete_file_that_did_not_grow_still_resumes():
    """A budgeted stop leaves a suffix that no size comparison can see."""
    assert _classify(
        committed_complete=False, committed_offset=60) == "resume"


def test_an_unknown_identity_does_not_manufacture_a_replacement():
    """An upgraded store has NULL identity, which is no evidence either way."""
    assert _classify(
        committed_device=None, committed_inode=None,
        observed=frontier.ObservedFile(size=100, device=7, inode=99),
    ) == "unchanged"


def test_a_new_device_at_the_same_inode_is_not_a_replacement():
    """A remount reassigns `dev_t`; it does not replace any file.

    ``st_dev`` is a property of the mount, not of the file, so a
    ``$CODEX_HOME`` on an external or network volume gets a different device
    number every time the volume is mounted. Deciding replacement on the
    device would classify EVERY retained rollout as replaced at once, which
    resets every cursor to byte zero and re-attributes the whole estate from
    the currently-logged-in account.
    """
    assert _classify(
        observed=frontier.ObservedFile(size=100, device=9, inode=11),
    ) == "unchanged"


def test_a_new_device_at_the_same_inode_still_reports_an_append():
    """The remount must not mask an ordinary append either."""
    assert _classify(
        observed=frontier.ObservedFile(size=180, device=9, inode=11),
    ) == "append"


def test_a_new_inode_at_the_same_size_is_still_a_replacement():
    """The narrowing is to the device alone. The inode still decides."""
    assert _classify(
        observed=frontier.ObservedFile(size=100, device=7, inode=12),
    ) == "replaced"


def test_a_new_inode_on_a_new_device_is_still_a_replacement():
    """A remount that also renamed the file over is a replacement."""
    assert _classify(
        observed=frontier.ObservedFile(size=100, device=9, inode=12),
    ) == "replaced"


def test_an_unknown_inode_beside_a_known_device_is_no_evidence():
    """NULL on EITHER column stays no-evidence, as the pre-#769 tree behaved."""
    assert _classify(
        committed_inode=None,
        observed=frontier.ObservedFile(size=100, device=9, inode=11),
    ) == "unchanged"


def test_a_known_inode_beside_an_unknown_device_still_decides():
    """A stored inode is evidence even when the device column is NULL.

    The decision is the inode alone, so the no-evidence condition is the
    INODE alone. Requiring the device as well discards a real inode and
    misses a genuine replacement, which returns the caller to the size
    comparison and lets a same-size replacement pass as unchanged.
    """
    assert _classify(
        committed_device=None,
        observed=frontier.ObservedFile(size=100, device=9, inode=12),
    ) == "replaced"


def test_a_known_inode_beside_an_unknown_device_still_reports_unchanged():
    """The same narrowing must not manufacture a replacement either."""
    assert _classify(
        committed_device=None,
        observed=frontier.ObservedFile(size=100, device=9, inode=11),
    ) == "unchanged"


def test_the_replacement_verdict_ignores_the_observed_device_alone():
    """`source_identity_replaced` is the single point of truth; assert it directly."""
    assert frontier.source_identity_replaced(None, 11, 9, 12) is True
    assert frontier.source_identity_replaced(None, 11, 9, 11) is False
    assert frontier.source_identity_replaced(7, None, 9, 12) is False
    assert frontier.source_identity_replaced(7, 11, 9, None) is False



# ── the cursor table is derived, never spelled out at a call site ──────────

def test_the_ingest_cursor_table_is_named_once_per_provider():
    """One function answers "which ingest cursor table does this provider use?".

    The conversation planner already derives its table through
    `_conversation_source_table`; the ingest planner spelled
    `codex_session_files` out at its own call site. Two spellings of one
    answer is exactly the shape in which one of them stops matching.
    """
    assert frontier._ingest_source_table("codex") == "codex_session_files"
    assert frontier._ingest_source_table("claude") == "session_files"
    with pytest.raises(ValueError):
        frontier._ingest_source_table("gemini")


def test_the_ingest_planner_restats_the_table_its_provider_names(
    corpus, monkeypatch,
):
    """The planner's restat reads the provider's own ingest cursor table."""
    state, conn, _app_dir, roots, _recent, _old = corpus
    observed = []
    original = frontier.plan_recent_active_restat

    def record(conn_arg, *, table="codex_session_files", extra_paths=()):
        observed.append(table)
        return original(conn_arg, table=table, extra_paths=extra_paths)

    monkeypatch.setattr(frontier, "plan_recent_active_restat", record)
    state.plan_provider("codex", conn, roots=roots)
    assert observed == [frontier._ingest_source_table("codex")], observed


# ── the recency window is cctally's own clock ──────────────────────────────

def _iso(offset_seconds: float) -> str:
    return (
        dt.datetime.now(dt.timezone.utc)
        + dt.timedelta(seconds=offset_seconds)
    ).isoformat()


def _store(tmp_path, rows=()) -> pathlib.Path:
    path = tmp_path / "cache.db"
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE cache_meta(key TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            "CREATE TABLE codex_session_files("
            "path TEXT PRIMARY KEY, size_bytes INTEGER, mtime_ns INTEGER,"
            " last_byte_offset INTEGER, last_ingested_at TEXT,"
            " ingest_complete INTEGER NOT NULL DEFAULT 1,"
            " device_id INTEGER, inode INTEGER)")
        conn.execute(
            "INSERT INTO cache_meta VALUES(?, '1')",
            (frontier.CODEX_FULL_WALK_COMPLETE_KEY,))
        for row in rows:
            conn.execute(
                "INSERT INTO codex_session_files"
                "(path,size_bytes,mtime_ns,last_byte_offset,last_ingested_at,"
                " ingest_complete,device_id,inode) VALUES(?,?,?,?,?,?,?,?)",
                row)
        conn.commit()
    finally:
        conn.close()
    return path


def _rollout(root: pathlib.Path, name="rollout.jsonl", lines=3) -> pathlib.Path:
    path = root / name
    path.write_text("".join('{"a":1}\n' for _ in range(lines)),
                    encoding="utf-8")
    return path


@pytest.fixture
def corpus(tmp_path):
    """One tracked rollout, ingested a moment ago, plus one ingested long ago."""
    sessions = tmp_path / "sessions"
    sessions.mkdir(parents=True)
    recent = _rollout(sessions, "recent.jsonl")
    old = _rollout(sessions, "old.jsonl", lines=5)
    recent_stat = recent.stat()
    old_stat = old.stat()
    store = _store(tmp_path, rows=(
        (str(recent), recent_stat.st_size, recent_stat.st_mtime_ns,
         recent_stat.st_size, _iso(-5), 1, recent_stat.st_dev,
         recent_stat.st_ino),
        (str(old), old_stat.st_size, old_stat.st_mtime_ns,
         old_stat.st_size, _iso(-86400), 1, old_stat.st_dev, old_stat.st_ino),
    ))
    app_dir = tmp_path / "data"
    app_dir.mkdir()
    conn = sqlite3.connect(store)
    state = frontier.DashboardIngestFrontier(app_dir)
    assert state.seed_provider("codex", conn, roots=(sessions,)), (
        state.last_seed_failure)
    yield state, conn, app_dir, (sessions,), recent, old
    conn.close()


def test_the_recent_set_names_only_what_cctally_ingested_recently(corpus):
    _state, conn, _app_dir, _roots, recent, old = corpus
    selected = frontier.select_recently_active_codex_paths(conn)
    assert str(recent) in selected
    assert str(old) not in selected


def test_an_idle_caught_up_tick_stats_no_tracked_rollout(corpus, monkeypatch):
    """Zero, not "few". A caught-up estate has nothing to look at."""
    state, conn, _app_dir, roots, recent, old = corpus
    # Age the recent row past the window without touching either file.
    conn.execute(
        "UPDATE codex_session_files SET last_ingested_at=?", (_iso(-86400),))
    conn.commit()
    statted = _count_stats(monkeypatch, {recent, old})
    plan = state.plan_provider("codex", conn, roots=roots)
    assert (plan.mode, plan.reason) == ("caught_up", "unchanged")
    assert statted == [], statted


def test_an_active_tick_stats_only_the_recent_path(corpus, monkeypatch):
    state, conn, _app_dir, roots, recent, old = corpus
    statted = _count_stats(monkeypatch, {recent, old})
    plan = state.plan_provider("codex", conn, roots=roots)
    assert (plan.mode, plan.reason) == ("caught_up", "unchanged")
    assert statted == [recent], statted


def test_a_ticketless_append_is_discovered_on_the_next_normal_tick(corpus):
    """#724. No ticket, no directory change, no expiry — and still found."""
    state, conn, _app_dir, roots, recent, _old = corpus
    with recent.open("a", encoding="utf-8") as fh:
        fh.write('{"appended":1}\n')
    plan = state.plan_provider("codex", conn, roots=roots)
    assert plan.mode == "targeted", plan.reason
    assert plan.reason == "recent_activity"
    assert plan.paths == frozenset({str(recent)})


def test_a_truncated_recent_path_escalates_to_a_full_walk(corpus):
    state, conn, _app_dir, roots, recent, _old = corpus
    with recent.open("r+b") as fh:
        fh.truncate(1)
    plan = state.plan_provider("codex", conn, roots=roots)
    assert (plan.mode, plan.reason) == ("full", "cursor_gap")


def test_a_replaced_recent_path_escalates_to_a_full_walk(corpus):
    """A same-size, same-mtime replacement never reaches a targeted plan.

    Renaming over an entry in a tracked directory moves that directory's
    mtime, so `filesystem_changed` answers first and the restat never sees the
    path. That ordering is correct and this test pins it; the identity
    comparison exists for the replacements the directory guard cannot see, and
    `test_a_new_inode_is_a_replacement_even_when_it_grew` pins that directly.
    """
    import os

    state, conn, _app_dir, roots, recent, _old = corpus
    before = recent.stat()
    replacement = recent.with_name(recent.name + ".swap")
    replacement.write_bytes(recent.read_bytes())
    os.replace(replacement, recent)
    os.utime(recent, ns=(before.st_atime_ns, before.st_mtime_ns))
    plan = state.plan_provider("codex", conn, roots=roots)
    assert plan.mode == "full", plan.reason
    assert plan.reason in {"filesystem_changed", "source_replaced"}, plan.reason


def test_recency_ignores_the_rollouts_own_event_time(corpus, monkeypatch):
    """The observed thing does not get to say when it was observed.

    Rewriting the rollout's event timestamps far into the future changes what
    the file claims about itself and changes nothing about when cctally read
    it, so the recency set must not move.
    """
    state, conn, _app_dir, roots, recent, old = corpus
    conn.execute(
        "UPDATE codex_session_files SET last_ingested_at=? WHERE path=?",
        (_iso(-86400), str(old)))
    conn.commit()
    old.write_text(
        json.dumps({"timestamp": "2099-01-01T00:00:00.000Z", "type": "x"})
        + "\n", encoding="utf-8")
    selected = frontier.select_recently_active_codex_paths(conn)
    assert str(old) not in selected


def test_recency_ignores_the_files_mtime(corpus):
    import os

    _state, conn, _app_dir, _roots, _recent, old = corpus
    conn.execute(
        "UPDATE codex_session_files SET last_ingested_at=? WHERE path=?",
        (_iso(-86400), str(old)))
    conn.commit()
    os.utime(old, None)
    assert str(old) not in frontier.select_recently_active_codex_paths(conn)


def test_an_unchanged_verification_does_not_refresh_recency(corpus, monkeypatch):
    """Looking at a file is not activity in it.

    If a restat that found nothing renewed the path's recency, a rollout that
    went quiet would be statted on every tick for the rest of the process.
    """
    state, conn, _app_dir, roots, recent, old = corpus
    first = _count_stats(monkeypatch, {recent, old})
    state.plan_provider("codex", conn, roots=roots)
    assert first == [recent]
    row_before = conn.execute(
        "SELECT last_ingested_at FROM codex_session_files WHERE path=?",
        (str(recent),)).fetchone()[0]
    state.plan_provider("codex", conn, roots=roots)
    row_after = conn.execute(
        "SELECT last_ingested_at FROM codex_session_files WHERE path=?",
        (str(recent),)).fetchone()[0]
    assert row_after == row_before


def test_a_pending_path_stays_selected_however_old_it_is(corpus):
    """A budget-stopped generation left work nothing else will ever name."""
    _state, conn, _app_dir, _roots, _recent, old = corpus
    conn.execute(
        "UPDATE codex_session_files SET last_ingested_at=?, ingest_complete=0 "
        "WHERE path=?", (_iso(-86400), str(old)))
    conn.commit()
    assert str(old) in frontier.select_recently_active_codex_paths(conn)


def test_the_restat_never_reads_an_accounting_row(corpus, monkeypatch):
    """The whole point is a bounded cost proportional to recent activity."""
    state, conn, _app_dir, roots, _recent, _old = corpus
    seen = []
    conn.set_trace_callback(lambda sql: seen.append(" ".join(str(sql).split())))
    try:
        state.plan_provider("codex", conn, roots=roots)
    finally:
        conn.set_trace_callback(None)
    assert seen, "the trace callback recorded nothing, so it proves nothing"
    offenders = [item for item in seen if "codex_session_entries" in item]
    assert offenders == [], offenders


def test_a_store_without_the_cursor_columns_escalates_rather_than_raising(
    tmp_path,
):
    """A missing column is store state, and the planner must answer it.

    `plan_provider`'s own `except` clause covers `OSError` and `ValueError`
    only, so an `sqlite3.DatabaseError` escaping the restat would end the tick
    rather than degrade it.
    """
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE codex_session_files("
            "path TEXT PRIMARY KEY, size_bytes INTEGER, mtime_ns INTEGER,"
            " last_byte_offset INTEGER, last_ingested_at TEXT)")
        conn.commit()
        targets, escalation = frontier.plan_recent_active_restat(conn)
        assert targets == frozenset()
        assert escalation == "codex_cursor_unavailable"
    finally:
        conn.close()


def _count_stats(monkeypatch, watched):
    """Record every `stat` taken on one of `watched`, in call order."""
    watched = {pathlib.Path(item).resolve() for item in watched}
    calls: list[pathlib.Path] = []
    real_stat = pathlib.Path.stat

    def counting(self, *args, **kwargs):
        try:
            resolved = self.resolve()
        except OSError:
            resolved = self
        if resolved in watched:
            calls.append(resolved)
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "stat", counting)
    return calls


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
