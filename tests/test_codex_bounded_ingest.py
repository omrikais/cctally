"""The Codex hook's resumable, budgeted ingest leg.

Public issue omrikais/cctally#5, Tasks 11-13. Spec:
``docs/superpowers/specs/2026-07-31-codex-hook-incremental-quota-reconcile-design.md``
§4.

Bounding the ingest requires fixing a latent hazard first.
``_write_codex_file_batch`` persists the file's full observed ``st_size``
alongside whatever ``final_offset`` ingestion actually reached, and delta
detection skips on ``size == prev_size`` without consulting the offset — so
committing a mid-file stop under that representation makes the unread suffix
PERMANENTLY invisible on any rollout that never grows again. Nothing raises;
the bytes simply never load.
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import pathlib
import sqlite3
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BIN_DIR = REPO_ROOT / "bin"
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))
if str(REPO_ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "tests"))

from conftest import load_script, redirect_paths  # noqa: E402
import _lib_accounts as accts  # noqa: E402


def _b64(obj) -> str:
    raw = json.dumps(obj).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _iso_ago(**delta) -> str:
    """A UTC stamp in the exact shape the cache_meta clocks are written in.

    Both `since` clocks are stamped at `timespec="seconds"`, so a test that
    compares two live ticks only discriminates when the pair happens to
    straddle a second boundary. Seeding an aged value instead makes the
    carry-forward assertion deterministic — and reproduces the state doctor's
    one-hour WARN actually depends on.
    """
    return (
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(**delta)
    ).isoformat(timespec="seconds").replace("+00:00", "Z")


def _auth_json(account_id: str, email: str) -> str:
    payload = {
        "email": email,
        "https://api.openai.com/auth": {
            "chatgpt_account_id": account_id,
            "chatgpt_plan_type": "pro",
        },
    }
    token = f"{_b64({'alg': 'RS256', 'typ': 'JWT'})}.{_b64(payload)}.sig"
    return json.dumps({
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": token, "access_token": "a", "refresh_token": "r"},
        "last_refresh": "2026-07-20T00:00:00Z",
    })


def _expected_key(account_id: str, email: str) -> str:
    return accts.account_key("codex", account_id + "\0" + email)


def _records(count: int, *, session_id: str = "sess-bounded"):
    """``session_meta`` + ``turn_context`` + ``count`` yielded token events."""
    out = [
        {"timestamp": "2026-07-20T10:00:00.000Z", "type": "session_meta",
         "payload": {"id": session_id}},
        {"timestamp": "2026-07-20T10:00:01.000Z", "type": "turn_context",
         "payload": {"model": "gpt-5"}},
    ]
    cumulative = 0
    for index in range(count):
        cumulative += 100
        out.append({
            "timestamp": f"2026-07-20T10:{index + 2:02d}:00.000Z",
            "type": "event_msg",
            "payload": {"type": "token_count", "info": {
                "last_token_usage": {
                    "input_tokens": 60, "cached_input_tokens": 0,
                    "output_tokens": 40, "reasoning_output_tokens": 0,
                    "total_tokens": 100},
                "total_token_usage": {"total_tokens": cumulative}}},
        })
    return out


def _write_rollout(path: pathlib.Path, records) -> list[int]:
    """Write ``records`` one per line; return the byte offset AFTER each line."""
    path.parent.mkdir(parents=True, exist_ok=True)
    offsets: list[int] = []
    with open(path, "w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, separators=(",", ":")) + "\n")
            fh.flush()
            offsets.append(fh.tell())
    return offsets


@pytest.fixture
def root(tmp_path, monkeypatch):
    """A single Codex provider root holding one rollout."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    provider_root = tmp_path / "codex-provider"
    provider_root.mkdir(parents=True, exist_ok=True)
    rollout = provider_root / "sessions" / "2026" / "07" / "20" / "rollout.jsonl"
    monkeypatch.setenv("CODEX_HOME", str(provider_root))
    return ns, provider_root, rollout


def _entry_offsets(ns) -> list[int]:
    conn = ns["open_cache_db"]()
    try:
        return [int(row[0]) for row in conn.execute(
            "SELECT line_offset FROM codex_session_entries ORDER BY line_offset")]
    finally:
        conn.close()


def _file_row(ns) -> dict:
    conn = ns["open_cache_db"]()
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM codex_session_files").fetchone()
        return {} if row is None else dict(row)
    finally:
        conn.close()


def _rewind_to_partial(ns, *, resume_offset: int, events_kept: int):
    """Make the stored cursor look like a budgeted mid-file stop.

    Full observed ``size_bytes``, a SHORT ``last_byte_offset``, and the explicit
    incomplete flag — which is precisely the representation the old skip-on-size
    detector could not tell apart from a fully-read file.

    ``last_total_tokens`` is rewound too. It is the iterator's dedup watermark,
    so leaving it at the whole file's cumulative would make every resumed event
    look already-seen and the resume would silently ingest nothing — a fixture
    artifact, not a production one: a real budgeted stop persists the watermark
    it actually reached.
    """
    conn = ns["open_cache_db"]()
    try:
        conn.execute(
            "DELETE FROM codex_session_entries WHERE line_offset >= ?",
            (resume_offset,))
        conn.execute(
            "UPDATE codex_session_files "
            "   SET last_byte_offset = ?, ingest_complete = 0, "
            "       last_total_tokens = ?",
            (resume_offset, events_kept * 100))
        conn.commit()
    finally:
        conn.close()


# ── Task 11: the silent skip ───────────────────────────────────────────────

def test_an_incomplete_file_resumes_instead_of_being_skipped(root):
    """The whole reason `ingest_complete` exists.

    The file has not grown, so the size comparison alone says "unchanged" and
    the unread suffix would never load again — not on the next tick, not ever,
    because a finished rollout's size is frozen.
    """
    ns, _provider_root, rollout = root
    offsets = _write_rollout(rollout, _records(4))
    cache = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](cache)
    finally:
        cache.close()
    assert len(_entry_offsets(ns)) == 4, "the fixture did not ingest cleanly"
    assert _file_row(ns)["ingest_complete"] == 1

    # Stop after the first token event; the file itself is untouched.
    _rewind_to_partial(ns, resume_offset=offsets[2], events_kept=1)
    assert len(_entry_offsets(ns)) == 1

    cache = ns["open_cache_db"]()
    try:
        stats = ns["sync_codex_cache"](cache)
    finally:
        cache.close()

    assert stats.files_skipped_unchanged == 0, (
        "an incomplete file was skipped as unchanged — its unread suffix is "
        "now permanently invisible")
    assert len(_entry_offsets(ns)) == 4
    assert _file_row(ns)["ingest_complete"] == 1


def test_a_completed_file_is_still_skipped_when_unchanged(root):
    """The other half: `ingest_complete` must not turn every tick into a re-read.

    Requiring the flag in the skip predicate is only safe because the flag is 1
    for every pre-existing row and for every file read to its target.
    """
    ns, _provider_root, rollout = root
    _write_rollout(rollout, _records(3))
    cache = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](cache)
        stats = ns["sync_codex_cache"](cache)
    finally:
        cache.close()

    assert stats.files_skipped_unchanged == 1
    assert stats.files_processed == 0


def test_a_truncated_incomplete_file_resets_rather_than_resuming(root):
    """``size < last_byte_offset`` under an incomplete flag is a truncation.

    Resuming at a byte offset the file no longer has would seek past EOF and
    silently ingest nothing, leaving the cursor permanently ahead of the data.
    """
    ns, _provider_root, rollout = root
    offsets = _write_rollout(rollout, _records(4))
    cache = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](cache)
    finally:
        cache.close()
    _rewind_to_partial(ns, resume_offset=offsets[3], events_kept=2)

    # The rollout is rewritten shorter than the stored resume point.
    _write_rollout(rollout, _records(2, session_id="sess-reborn"))
    cache = ns["open_cache_db"]()
    try:
        stats = ns["sync_codex_cache"](cache)
    finally:
        cache.close()

    assert stats.files_reset_truncated == 1
    assert len(_entry_offsets(ns)) == 2
    assert _file_row(ns)["ingest_complete"] == 1


def test_a_grown_incomplete_file_finishes_its_stored_target_first(root):
    """A file that grew while its stored scan target was still incomplete.

    Two phases, and the boundary between them is what preserves #416's
    mid-file account switch. The DEFERRED bytes — the ones that already existed
    when the budget stopped — must keep the account decided for them, because
    where a hook happened to slice the work cannot be allowed to change
    attribution. The bytes appended AFTER the stop are genuinely new, so on the
    next tick they are an ordinary delta append and may legitimately acquire
    the account that is now logged in.

    Reading straight through to EOF would collapse both phases and silently
    file the new bytes under the old account forever: after that pass the row
    reads complete at the new size, so nothing would ever revisit them.
    """
    ns, provider_root, rollout = root
    (provider_root / "auth.json").write_text(_auth_json("acct-red", "red@x.com"))
    offsets = _write_rollout(rollout, _records(3))
    old_size = offsets[-1]
    cache = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](cache)
    finally:
        cache.close()
    _rewind_to_partial(ns, resume_offset=offsets[2], events_kept=1)

    # It grows, and the account changes underneath it.
    _write_rollout(rollout, _records(5))
    (provider_root / "auth.json").write_text(
        _auth_json("acct-blue", "blue@x.com"))

    cache = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](cache)
    finally:
        cache.close()

    red = _expected_key("acct-red", "red@x.com")
    blue = _expected_key("acct-blue", "blue@x.com")
    row = _file_row(ns)
    assert len(_entry_offsets(ns)) == 3, (
        "the resume ran past its stored scan target into the new suffix")
    assert row["ingest_complete"] == 1
    assert int(row["size_bytes"]) == old_size, (
        "the row committed to the new size, so the suffix can never be seen "
        "as an append")
    conn = ns["open_cache_db"]()
    try:
        assert {r[0] for r in conn.execute(
            "SELECT DISTINCT account_key FROM codex_session_entries")} == {red}
    finally:
        conn.close()

    cache = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](cache)
    finally:
        cache.close()

    conn = ns["open_cache_db"]()
    try:
        by_offset = dict(conn.execute(
            "SELECT line_offset, account_key FROM codex_session_entries"))
    finally:
        conn.close()
    assert len(by_offset) == 5
    assert {key for off, key in by_offset.items() if off < old_size} == {red}
    assert {key for off, key in by_offset.items() if off >= old_size} == {blue}


def test_a_resumed_partial_does_not_acquire_the_live_account(root):
    """Attribution must not depend on where a budget happened to slice.

    ``delta_append`` is what authorizes consulting the live ``auth.json`` and
    minting a NEW range at the resume offset. A partial continuation is not a
    delta append: the same bytes, ingested in one pass, would carry the account
    decided at offset 0, and slicing the work across two hooks must not change
    the answer.
    """
    ns, provider_root, rollout = root
    (provider_root / "auth.json").write_text(_auth_json("acct-red", "red@x.com"))
    offsets = _write_rollout(rollout, _records(4))
    cache = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](cache)
    finally:
        cache.close()
    _rewind_to_partial(ns, resume_offset=offsets[2], events_kept=1)

    (provider_root / "auth.json").write_text(
        _auth_json("acct-blue", "blue@x.com"))
    cache = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](cache)
    finally:
        cache.close()

    conn = ns["open_cache_db"]()
    try:
        keys = {row[0] for row in conn.execute(
            "SELECT DISTINCT account_key FROM codex_session_entries")}
    finally:
        conn.close()
    assert keys == {_expected_key("acct-red", "red@x.com")}, (
        "the resumed suffix acquired the account that happened to be logged "
        "in when the hook resumed")


def test_the_batch_writer_persists_the_incomplete_flag_it_is_given(root):
    """``INSERT OR REPLACE`` deletes and reinserts the row.

    A column omitted from its list therefore reverts to its schema DEFAULT — 1
    here — so a writer that stops mid-file and records that fact has it erased
    by its own commit, and the column stops meaning anything at all. Two
    successive incomplete writes is the shape that catches it: the second is
    where the resurrection would happen.
    """
    ns, _provider_root, rollout = root
    _write_rollout(rollout, _records(2))
    cache = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](cache)
    finally:
        cache.close()

    import _cctally_cache as cc
    row = _file_row(ns)
    discovered = cc.CodexDiscoveredFile(
        source_path=rollout, physical_path=rollout.resolve(),
        provider_root=rollout.parent.parent.parent.parent,
        walk_root=rollout.parent.parent.parent.parent / "sessions",
        source_root_key=str(row["source_root_key"]),
    )
    conn = ns["open_cache_db"]()
    try:
        for _ in range(2):
            cc._write_codex_file_batch(
                conn, discovered=discovered, path_str=str(rollout),
                size=int(row["size_bytes"]), mtime_ns=int(row["mtime_ns"]),
                final_offset=1, last_session_id=None, last_model=None,
                last_total_tokens=None, last_native_thread_id=None,
                last_root_thread_id=None, last_parent_thread_id=None,
                last_conversation_key=None, last_turn_id=None,
                reset_file=False, accounting_rows=[], quota_rows=[],
                thread_rows=[], active_root_keys={str(row["source_root_key"])},
                prune_roots=False, ingest_complete=False,
            )
            assert conn.execute(
                "SELECT ingest_complete FROM codex_session_files "
                " WHERE path = ?", (str(rollout),)).fetchone()[0] == 0
    finally:
        conn.close()


# ── Task 12: the bounded, resumable walk ───────────────────────────────────

def _cache_meta(ns, key):
    conn = ns["open_cache_db"]()
    try:
        row = conn.execute(
            "SELECT value FROM cache_meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else json.loads(str(row[0]))
    finally:
        conn.close()


def _rollouts(provider_root, count, *, events=3):
    """``count`` distinct rollouts, named so the discovered order is stable."""
    paths = []
    for index in range(count):
        path = (provider_root / "sessions" / "2026" / "07" / "20"
                / f"rollout-{index:03d}.jsonl")
        _write_rollout(path, _records(events, session_id=f"sess-{index:03d}"))
        paths.append(path)
    return paths


def _files_ingested(ns) -> set[str]:
    conn = ns["open_cache_db"]()
    try:
        return {str(row[0]) for row in conn.execute(
            "SELECT DISTINCT source_path FROM codex_session_entries")}
    finally:
        conn.close()


def test_a_zero_budget_stops_before_touching_anything(root):
    """The budget is checked BEFORE a file is opened.

    A tick either commits a file whole (or to a recorded partial offset) or
    does not touch it at all — there is no state where a half-parsed file has
    advanced a cursor.
    """
    ns, provider_root, _rollout = root
    _rollouts(provider_root, 4)
    cache = ns["open_cache_db"]()
    try:
        stats = ns["sync_codex_cache"](cache, budget_seconds=-1.0)
    finally:
        cache.close()

    assert stats.files_processed == 0
    assert stats.budget_exhausted is True
    assert stats.backlog_files == 4
    assert stats.backlog_bytes > 0
    assert _files_ingested(ns) == set()
    assert _cache_meta(ns, "codex_ingest_backlog")["files"] == 4


def test_the_walk_drains_its_tail_across_budgeted_ticks(root):
    """The convergence property the resume cursor exists for.

    The front files are re-touched every tick (they are what a sorted walk
    reaches first), so without a persisted cursor the budget would be spent on
    them forever and the tail would never be read at all.
    """
    ns, provider_root, _rollout = root
    paths = _rollouts(provider_root, 6)
    everything = {str(p) for p in paths}

    import _cctally_cache as cc
    real_batch = cc._write_codex_file_batch
    real_clock = cc._walk_clock
    state = {"committed": 0}

    def _counting_batch(*args, **kwargs):
        result = real_batch(*args, **kwargs)
        state["committed"] += 1
        return result

    def _clock(_state=state):
        # Time stands still until one file has committed, then jumps past any
        # budget. Deterministic "exactly one file per tick" — a real clock
        # cannot express that under variable load, and a convergence test that
        # cannot pin where the budget stops is not testing convergence.
        return 0.0 if _state["committed"] < 1 else 10_000.0

    ticks = 0
    progress = []
    cc._write_codex_file_batch = _counting_batch
    cc._walk_clock = _clock
    try:
        while _files_ingested(ns) != everything and ticks < 20:
            ticks += 1
            state["committed"] = 0
            cache = ns["open_cache_db"]()
            try:
                ns["sync_codex_cache"](cache, budget_seconds=1.0)
            finally:
                cache.close()
            progress.append(len(_files_ingested(ns)))
    finally:
        cc._write_codex_file_batch = real_batch
        cc._walk_clock = real_clock

    assert _files_ingested(ns) == everything, (
        f"the tail never drained: {ticks} ticks, progress {progress}")
    assert ticks == len(paths), (
        f"{ticks} ticks for {len(paths)} files (progress {progress}) — the "
        f"cursor is not advancing, so each tick re-walks the same prefix")

    # One more unbounded tick completes the cycle and clears both records.
    cache = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](cache)
    finally:
        cache.close()
    assert _cache_meta(ns, "codex_ingest_backlog") is None
    assert _cache_meta(ns, "codex_ingest_resume_cursor") is None


def test_the_cursor_survives_its_target_disappearing(root):
    """File-set churn: the cursor's own file is deleted between ticks.

    Falling back to the top of the walk would let a store that loses its cursor
    file each tick re-read the same prefix forever, so the stored ORDINAL is
    what keeps forward progress.
    """
    ns, provider_root, _rollout = root
    paths = _rollouts(provider_root, 5)
    cache = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](cache, budget_seconds=-1.0)
    finally:
        cache.close()
    cursor = _cache_meta(ns, "codex_ingest_resume_cursor")
    assert cursor["ordinal"] == 0

    # Point it at the middle file, then delete that file.
    conn = ns["open_cache_db"]()
    try:
        conn.execute(
            "UPDATE cache_meta SET value = ? WHERE key = ?",
            (json.dumps({"root_key": cursor["root_key"],
                         "path": str(paths[2].resolve()), "ordinal": 2},
                        sort_keys=True),
             "codex_ingest_resume_cursor"))
        conn.commit()
    finally:
        conn.close()
    paths[2].unlink()

    import _cctally_cache as cc
    ordered = cc._order_codex_walk(
        cc._discover_codex_files_with_roots(),
        cursor=(cursor["root_key"], str(paths[2].resolve()), 2),
        active_path=None,
    )
    # Four files remain; ordinal 2 is now the FOURTH original file, and the
    # rotation still visits every one of them.
    assert [p.source_path for p in ordered][0] == paths[3]
    assert {p.source_path for p in ordered} == {
        p for p in paths if p != paths[2]}


def test_a_file_inserted_before_the_cursor_waits_for_the_wrap(root):
    """Rotation, not truncation.

    A newly written rollout that sorts before the cursor is visited at the END
    of the current cycle rather than jumping the queue — which is what keeps
    "every file within one cycle" true regardless of where the budget stopped.
    """
    ns, provider_root, _rollout = root
    paths = _rollouts(provider_root, 3)
    inserted = (provider_root / "sessions" / "2026" / "07" / "20"
                / "rollout-000a.jsonl")
    _write_rollout(inserted, _records(2, session_id="sess-inserted"))

    import _cctally_cache as cc
    discovered = cc._discover_codex_files_with_roots()
    ordered = cc._order_codex_walk(
        discovered,
        cursor=(discovered[0].source_root_key, str(paths[1].resolve()), 2),
        active_path=None,
    )
    names = [p.source_path.name for p in ordered]
    assert names[0] == paths[1].name
    assert names[-1] == inserted.name or inserted.name in names
    assert set(names) == {p.name for p in [*paths, inserted]}


def test_the_active_rollout_is_walked_first(root):
    """Priority: the hook's stdin payload names the live session."""
    ns, provider_root, _rollout = root
    paths = _rollouts(provider_root, 4)

    import _cctally_cache as cc
    ordered = cc._order_codex_walk(
        cc._discover_codex_files_with_roots(),
        cursor=None, active_path=paths[3].resolve(),
    )
    assert ordered[0].source_path == paths[3]
    assert len(ordered) == 4


def test_an_active_path_outside_the_configured_roots_is_ignored(root):
    """An arbitrary path from a hook payload must never be trusted.

    It is matched against the DISCOVERED set; anything else leaves the walk
    order exactly as the cursor rotation produced it.
    """
    ns, provider_root, _rollout = root
    paths = _rollouts(provider_root, 3)
    cache = ns["open_cache_db"]()
    try:
        stats = ns["sync_codex_cache"](
            cache, budget_seconds=30.0,
            active_transcript_path="/etc/passwd")
    finally:
        cache.close()

    assert stats.files_processed == 3
    assert _files_ingested(ns) == {str(p) for p in paths}


class _TickingClock:
    """A monotonic stand-in that advances a fixed step on every read.

    `_walk_clock` is read once at entry, once per file before it is opened, and
    once per record inside a file, so a constant step makes "which file the
    budget stops inside, and after how many records" a pure function of the step
    — deterministic in a way a real clock under CI load is not.
    """

    def __init__(self, step: float) -> None:
        self.step = step
        self.now = -step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def test_the_backlog_excludes_bytes_the_partial_file_just_committed(root):
    """The backlog is measured against what the walk COMMITTED, not the
    pre-walk cursor snapshot.

    `existing` is read before the walk, so a file the budget stopped inside is
    otherwise measured from the offset it had when the tick started — reporting
    every byte this tick just ingested as still owed, and a brand-new file's
    whole length.
    """
    ns, provider_root, _rollout = root
    rollout = (provider_root / "sessions" / "2026" / "07" / "20"
               / "rollout-000.jsonl")
    offsets = _write_rollout(rollout, _records(12))
    size = offsets[-1]

    import _cctally_cache as cc
    real_clock = cc._walk_clock
    cc._walk_clock = _TickingClock(0.2)
    try:
        cache = ns["open_cache_db"]()
        try:
            stats = ns["sync_codex_cache"](cache, budget_seconds=1.0)
        finally:
            cache.close()
    finally:
        cc._walk_clock = real_clock

    row = _file_row(ns)
    assert row["ingest_complete"] == 0, "the fixture did not stop mid-file"
    committed = int(row["last_byte_offset"])
    assert 0 < committed < size, committed
    assert stats.backlog_bytes == size - committed, (
        "the backlog counted bytes this tick had already committed")


def test_the_active_rollouts_own_partial_stays_in_the_backlog(root):
    """The active rollout has its OWN cap, so there can be two partial files.

    It can stop short while the overall deadline still has room, and a later
    file then stops short too. Tracking only the last one loses the active
    rollout's remainder from the count — convergence is unaffected, because
    active-first revisits it, but the reported figure is wrong.
    """
    ns, provider_root, _rollout = root
    paths = _rollouts(provider_root, 2, events=12)

    import _cctally_cache as cc
    real_clock = cc._walk_clock
    # 0.4 per read against a 2.0s budget: the active file's cap is 1.0, so it
    # reads one record and stops; the second file is then opened at 1.6 and
    # stops on its first record at 2.0.
    cc._walk_clock = _TickingClock(0.4)
    try:
        cache = ns["open_cache_db"]()
        try:
            stats = ns["sync_codex_cache"](
                cache, budget_seconds=2.0,
                active_transcript_path=str(paths[1]))
        finally:
            cache.close()
    finally:
        cc._walk_clock = real_clock

    conn = ns["open_cache_db"]()
    try:
        incomplete = {
            str(row[0]) for row in conn.execute(
                "SELECT path FROM codex_session_files WHERE ingest_complete = 0")
        }
    finally:
        conn.close()
    assert incomplete == {str(p) for p in paths}, (
        f"the fixture did not stop inside both files: {incomplete}")
    assert stats.backlog_files == 2, (
        "the active rollout's own partial was dropped from the backlog")


def test_an_unbudgeted_sync_records_no_backlog_and_clears_one(root):
    """`cache-sync` is the drain command, so it must complete AND clear."""
    ns, provider_root, _rollout = root
    paths = _rollouts(provider_root, 3)
    cache = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](cache, budget_seconds=-1.0)
    finally:
        cache.close()
    assert _cache_meta(ns, "codex_ingest_backlog")["files"] == 3

    cache = ns["open_cache_db"]()
    try:
        stats = ns["sync_codex_cache"](cache)
    finally:
        cache.close()

    assert stats.budget_exhausted is False
    assert stats.backlog_files == 0
    assert _files_ingested(ns) == {str(p) for p in paths}
    assert _cache_meta(ns, "codex_ingest_backlog") is None
    assert _cache_meta(ns, "codex_ingest_resume_cursor") is None


def _arm(ns, key, payload="{}"):
    conn = ns["open_cache_db"]()
    try:
        conn.execute(
            "INSERT INTO cache_meta(key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, payload))
        conn.commit()
    finally:
        conn.close()


def _raw_meta(ns, key):
    conn = ns["open_cache_db"]()
    try:
        row = conn.execute(
            "SELECT value FROM cache_meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row[0])
    finally:
        conn.close()


def test_a_budgeted_tick_never_runs_a_byte_zero_replay(root):
    """A byte-zero replay cannot be sliced across budgeted ticks.

    The replay is "clear everything, then re-read everything", and the clear
    happens ONCE at the top of the walk. A budget stop increments neither
    `files_failed` nor `files_deferred_torn`, so the end-of-walk consume
    condition is satisfied TRIVIALLY by a walk that looked at almost nothing —
    and the marker is deleted. Every un-walked rollout that predates #416 then
    has no durable decision AND no `rebuild_known_identities` entry, so the next
    tick sends it to the live `auth.json` branch and re-attributes historical
    spend to whoever is logged in now (#416 spec D1).

    Keeping the marker instead is no better on its own: the next tick re-enters
    the rebuild, re-wipes the store, and captures a snapshot holding only the
    PREVIOUS tick's files — the same D1 violation, plus a walk that never
    converges. So the hook does not replay at all.
    """
    ns, provider_root, _rollout = root
    paths = _rollouts(provider_root, 4)
    cache = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](cache)
    finally:
        cache.close()
    everything = {str(p) for p in paths}
    assert _files_ingested(ns) == everything

    import _cctally_cache as cc
    _arm(ns, cc.CODEX_REPLAY_FROM_ZERO_KEY, "1")

    cache = ns["open_cache_db"]()
    try:
        stats = ns["sync_codex_cache"](cache, budget_seconds=-1.0)
    finally:
        cache.close()

    assert stats.deferred_reason == "replay_pending"
    assert _raw_meta(ns, cc.CODEX_REPLAY_FROM_ZERO_KEY) is not None, (
        "a budgeted tick consumed the byte-zero replay marker without "
        "replaying — the un-walked history is now re-attributable")
    assert _files_ingested(ns) == everything, (
        "the budgeted tick wiped the Codex cache it could not rebuild")


def test_a_budgeted_decline_is_recorded_so_the_stall_is_visible(root):
    """Declining silently is the other half of the freeze.

    The decline returns BEFORE the pre-walk and end-of-walk backlog writes, so
    `stats.backlog_files` stays 0, the lifecycle line logs `backlog=0`,
    `doctor codex.ingest_backlog` reports a drained store and the dashboard
    omits the field. `codex_replay_from_zero_blocked` cannot cover it either —
    only a walk that actually RAN writes that. A hook-only install with every
    Codex ingest frozen looked healthy on every surface this feature added.
    """
    ns, provider_root, _rollout = root
    _rollouts(provider_root, 3)
    import _cctally_cache as cc
    _arm(ns, cc.CODEX_REPLAY_FROM_ZERO_KEY, "1")

    cache = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](cache, budget_seconds=-1.0)
    finally:
        cache.close()

    record = _cache_meta(ns, cc.CODEX_REPLAY_DEFERRED_KEY)
    assert record is not None, (
        "a budgeted tick declined the replay and left no trace of it — the "
        "install is frozen and every surface reports fine")
    assert record["since"].endswith("Z")
    assert record["at"].endswith("Z")


def test_the_deferral_clock_is_not_restarted_by_a_later_declining_tick(root):
    """`since` is how long the freeze has stood, so it carries forward.

    Re-minting it every tick would hold the age below the WARN threshold
    forever, which is exactly the bug the record exists to expose.

    The clock is seeded an HOUR back rather than compared across successive
    live ticks. `since` is stamped at `timespec="seconds"` and three declines
    run well inside one second, so a same-second comparison passes an
    implementation that re-mints on every tick unless the loop happens to
    straddle a second boundary — discriminating power by wall-clock luck. An
    aged seed is deterministic, and it is exactly the state doctor's one-hour
    WARN depends on surviving.
    """
    ns, provider_root, _rollout = root
    _rollouts(provider_root, 2)
    import _cctally_cache as cc
    _arm(ns, cc.CODEX_REPLAY_FROM_ZERO_KEY, "1")
    aged = _iso_ago(hours=1)
    _arm(ns, cc.CODEX_REPLAY_DEFERRED_KEY,
         json.dumps({"since": aged, "at": aged}, sort_keys=True))

    for _ in range(3):
        cache = ns["open_cache_db"]()
        try:
            ns["sync_codex_cache"](cache, budget_seconds=-1.0)
        finally:
            cache.close()
        record = _cache_meta(ns, cc.CODEX_REPLAY_DEFERRED_KEY)
        assert record["since"] == aged, (
            "the declining tick restarted the freeze clock, so doctor's "
            "one-hour WARN can never fire however long the install stays stuck")

    # `at` is the opposite: it is this tick's stamp and must move, or the record
    # cannot distinguish a live freeze from an abandoned one.
    assert record["at"] != aged

    # And the drain, once it lands, clears it along with the marker.
    cache = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](cache)
    finally:
        cache.close()
    assert _raw_meta(ns, cc.CODEX_REPLAY_FROM_ZERO_KEY) is None
    assert _cache_meta(ns, cc.CODEX_REPLAY_DEFERRED_KEY) is None


def test_a_declining_tick_hands_the_drain_to_a_detached_worker(root, monkeypatch):
    """The blocker: nothing guaranteed an UNBUDGETED caller would ever run.

    A user on <= v1.87 with Codex CLI hooks, no dashboard and no `codex quota`
    habit upgrades; migration 035 arms the marker; from that tick on EVERY hook
    returns before walking a byte. Codex spend, quota observations, milestones
    and alerts freeze permanently — strictly worse than the four-second stall
    this feature set out to remove, and it is the reporter's install shape.
    """
    ns, provider_root, _rollout = root
    _rollouts(provider_root, 2)
    import _cctally_cache as cc
    import _cctally_update
    _arm(ns, cc.CODEX_REPLAY_FROM_ZERO_KEY, "1")

    spawned: list[str] = []
    monkeypatch.setattr(
        _cctally_update, "_spawn_detached",
        lambda command: spawned.append(command) or True)

    cache = ns["open_cache_db"]()
    try:
        stats = ns["sync_codex_cache"](cache, budget_seconds=-1.0)
    finally:
        cache.close()
    assert stats.deferred_reason == "replay_pending"

    assert cc._defer_codex_replay_drain() == "spawned"
    assert spawned == [cc.CODEX_REPLAY_DRAIN_COMMAND]

    # Attempt-throttled, exactly like the quota-verification worker: the marker
    # only disappears when a drain SUCCEEDS, so every tick in between still
    # reads as needing one and a success-stamped throttle would put one worker
    # on the box per hook tick.
    assert cc._defer_codex_replay_drain() == "throttled"
    assert spawned == [cc.CODEX_REPLAY_DRAIN_COMMAND]


def test_the_drain_worker_performs_the_replay_the_hook_declined(root):
    """What the hook hands off has to actually happen."""
    ns, provider_root, _rollout = root
    paths = _rollouts(provider_root, 3)
    import _cctally_cache as cc
    _arm(ns, cc.CODEX_REPLAY_FROM_ZERO_KEY, "1")

    assert cc.cmd_codex_replay_drain_internal(None) == 0

    assert _raw_meta(ns, cc.CODEX_REPLAY_FROM_ZERO_KEY) is None, (
        "the detached drain did not consume the marker, so the install is "
        "still frozen")
    assert _files_ingested(ns) == {str(p) for p in paths}


def test_an_unbudgeted_sync_still_consumes_the_replay_marker(root):
    """The other half: `cache-sync` and the dashboard still perform the replay.

    Deferring it on the hook path is only acceptable because every unbudgeted
    caller — `cache-sync --source codex`, the dashboard sync, every Codex read
    command — runs it to completion.
    """
    ns, provider_root, _rollout = root
    _rollouts(provider_root, 3)
    import _cctally_cache as cc
    _arm(ns, cc.CODEX_REPLAY_FROM_ZERO_KEY, "1")

    cache = ns["open_cache_db"]()
    try:
        stats = ns["sync_codex_cache"](cache)
    finally:
        cache.close()

    assert stats.deferred_reason is None
    assert _raw_meta(ns, cc.CODEX_REPLAY_FROM_ZERO_KEY) is None


def test_an_explicit_rebuild_refuses_a_budget(root):
    """`rebuild` and `budget_seconds` are mutually exclusive, like `only_paths`.

    A rebuild wipes first and re-reads after; bounding that walk commits the
    wipe and only part of the re-read.
    """
    ns, provider_root, _rollout = root
    _rollouts(provider_root, 2)
    cache = ns["open_cache_db"]()
    try:
        with pytest.raises(ValueError):
            ns["sync_codex_cache"](cache, rebuild=True, budget_seconds=5.0)
    finally:
        cache.close()


def test_the_rebuild_budget_contract_holds_with_a_replay_armed(root):
    """A contract that holds only sometimes is not one.

    The check sat AFTER the replay probe, so an armed byte-zero marker made the
    decline return first and the same call quietly deferred instead of raising
    — the answer depending on a `cache_meta` row the caller cannot see. The
    argument-level contracts are now checked on the caller's own arguments,
    ahead of the probe.
    """
    ns, provider_root, _rollout = root
    _rollouts(provider_root, 2)
    import _cctally_cache as cc
    _arm(ns, cc.CODEX_REPLAY_FROM_ZERO_KEY, "1")

    cache = ns["open_cache_db"]()
    try:
        with pytest.raises(ValueError):
            ns["sync_codex_cache"](cache, rebuild=True, budget_seconds=5.0)
        with pytest.raises(ValueError):
            ns["sync_codex_cache"](cache, rebuild=True, only_paths={"/x"})
    finally:
        cache.close()
    assert _raw_meta(ns, cc.CODEX_REPLAY_FROM_ZERO_KEY) is not None, (
        "the refused call still consumed the replay marker")


def test_a_contended_drain_does_not_report_success(root, monkeypatch):
    """A contended sync returns with every counter at zero.

    Reporting that as `success files=0` claimed a drain that never walked a
    byte, indistinguishable in the log from one that found nothing to do — and
    the log is the only window onto a worker whose streams are all /dev/null.
    """
    ns, provider_root, _rollout = root
    import _cctally_cache as cc
    import _cctally_core

    monkeypatch.setattr(
        cc, "sync_codex_cache",
        lambda conn, **kwargs: cc.CodexIngestStats(lock_contended=True))

    assert cc.cmd_codex_replay_drain_internal(None) == 0

    log = _cctally_core.HOOK_TICK_LOG_PATH.read_text()
    assert "op=replay-drain result=contended" in log
    assert "result=success" not in log


def test_the_deferral_record_re_raises_classified_corruption(root, monkeypatch):
    """Classified family corruption belongs to the shared recovery boundary.

    Its two siblings at the end of this same walk already re-raise it; this one
    swallowed it, so a corrupt cache presented as an ordinary decline — on the
    one path a hook-only install takes every single tick.
    """
    ns, _provider_root, _rollout = root
    import _cctally_cache as cc

    def _malformed(conn, key, value):
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(cc, "_set_cache_meta", _malformed)
    conn = ns["open_cache_db"]()
    try:
        with pytest.raises(sqlite3.DatabaseError):
            cc._record_codex_replay_deferral(conn)
    finally:
        conn.close()

    # An ordinary DatabaseError stays best-effort: a bookkeeping write must not
    # turn a deferral into a raised hook tick.
    def _ordinary(conn, key, value):
        raise sqlite3.DatabaseError("no such table: cache_meta")

    monkeypatch.setattr(cc, "_set_cache_meta", _ordinary)
    conn = ns["open_cache_db"]()
    try:
        cc._record_codex_replay_deferral(conn)
    finally:
        conn.close()


def test_a_budget_truncated_walk_leaves_the_torn_auth_marker_standing(root):
    """The same class as the replay marker, one table over.

    `codex_torn_auth_deferred` describes a tree-wide condition — a frozen Codex
    login — that `doctor` reports. A budgeted walk that never REACHES the torn
    file defers nothing, so the zero-count branch would clear a marker
    describing a real, ongoing stall.
    """
    ns, provider_root, _rollout = root
    _rollouts(provider_root, 3)
    _arm(ns, "codex_torn_auth_deferred",
         json.dumps({"files": 2, "at": "2026-07-30T00:00:00Z"}))

    cache = ns["open_cache_db"]()
    try:
        stats = ns["sync_codex_cache"](cache, budget_seconds=-1.0)
    finally:
        cache.close()

    assert stats.budget_exhausted is True
    assert stats.files_deferred_torn == 0
    assert _cache_meta(ns, "codex_torn_auth_deferred") is not None, (
        "a walk that reached no file cleared the frozen-login signal")


def test_a_complete_walk_still_clears_the_torn_auth_marker(root):
    """And the marker must not become un-clearable.

    A whole-tree walk that reaches every file and defers none is exactly the
    evidence that the torn `auth.json` recovered.
    """
    ns, provider_root, _rollout = root
    _rollouts(provider_root, 3)
    _arm(ns, "codex_torn_auth_deferred",
         json.dumps({"files": 2, "at": "2026-07-30T00:00:00Z"}))

    cache = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](cache)
    finally:
        cache.close()

    assert _cache_meta(ns, "codex_torn_auth_deferred") is None


def test_the_first_budgeted_tick_records_its_backlog_before_walking(root):
    """The end-of-walk record cannot describe a walk that never reaches its end.

    Every LATER tick is safe: `since` carries forward and a crashed tick leaves
    the previous record standing — stale but conservative. The FIRST budgeted
    tick over a fresh backlog has no previous record, so a crash mid-walk leaves
    the backlog reading zero and `doctor` reports OK while a real backlog grows.
    """
    ns, provider_root, _rollout = root
    _rollouts(provider_root, 5)

    import _cctally_cache as cc
    real_batch = cc._write_codex_file_batch
    state = {"n": 0}

    def _crash(*args, **kwargs):
        state["n"] += 1
        if state["n"] > 1:
            # Not a DatabaseError: the per-file retry must not swallow it.
            raise KeyboardInterrupt("the turn was killed mid-walk")
        return real_batch(*args, **kwargs)

    cc._write_codex_file_batch = _crash
    try:
        cache = ns["open_cache_db"]()
        try:
            with pytest.raises(KeyboardInterrupt):
                ns["sync_codex_cache"](cache, budget_seconds=30.0)
        finally:
            cache.close()
    finally:
        cc._write_codex_file_batch = real_batch

    record = _cache_meta(ns, "codex_ingest_backlog")
    assert record is not None, (
        "a crash on the first budgeted tick left no backlog record at all, so "
        "doctor and the dashboard report a drained store")
    assert record["files"] == 5
    assert record["bytes"] > 0


def test_a_drained_store_runs_no_pre_walk_stat_sweep(root):
    """Acceptance criterion 1 measures exactly this store shape.

    `_codex_backlog_after` stats every discovered rollout, and the pre-walk
    record is by definition absent on a drained store — so the unconditional
    form ran that sweep on EVERY steady-state tick and wrote nothing. On the
    real 1,859-rollout store that is a second full stat pass per tick on top of
    the walk's own per-file stats.
    """
    ns, provider_root, _rollout = root
    paths = _rollouts(provider_root, 5)
    cache = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](cache)
    finally:
        cache.close()

    import _cctally_cache as cc
    real_after = cc._codex_backlog_after
    calls = {"n": 0}

    def _counting(*args, **kwargs):
        calls["n"] += 1
        return real_after(*args, **kwargs)

    # An append to the active rollout: the tick genuinely owes bytes, and still
    # must not pay a full-tree sweep to say so.
    with open(paths[0], "a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "timestamp": "2026-07-20T11:00:00.000Z", "type": "event_msg",
            "payload": {"type": "token_count", "info": {
                "last_token_usage": {
                    "input_tokens": 1, "cached_input_tokens": 0,
                    "output_tokens": 1, "reasoning_output_tokens": 0,
                    "total_tokens": 2},
                "total_token_usage": {"total_tokens": 999999}}}}) + "\n")

    cc._codex_backlog_after = _counting
    try:
        cache = ns["open_cache_db"]()
        try:
            ns["sync_codex_cache"](cache, budget_seconds=30.0)
        finally:
            cache.close()
    finally:
        cc._codex_backlog_after = real_after

    assert calls["n"] == 1, (
        f"a drained store's budgeted tick ran {calls['n']} full-tree stat "
        f"sweeps; only the end-of-walk one is owed")


def test_an_incomplete_file_still_gets_its_pre_walk_record(root):
    """The gate must not swallow the case the pre-walk record exists for.

    A rollout the previous tick stopped INSIDE is known to the cursor snapshot,
    so "unknown file" alone would miss it — the flag is the other half.
    """
    ns, provider_root, rollout = root
    offsets = _write_rollout(rollout, _records(4))
    cache = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](cache)
    finally:
        cache.close()
    _rewind_to_partial(ns, resume_offset=offsets[2], events_kept=1)

    import _cctally_cache as cc
    real_batch = cc._write_codex_file_batch

    def _crash(*args, **kwargs):
        raise KeyboardInterrupt("the turn was killed mid-walk")

    cc._write_codex_file_batch = _crash
    try:
        cache = ns["open_cache_db"]()
        try:
            with pytest.raises(KeyboardInterrupt):
                ns["sync_codex_cache"](cache, budget_seconds=30.0)
        finally:
            cache.close()
    finally:
        cc._write_codex_file_batch = real_batch

    record = _cache_meta(ns, "codex_ingest_backlog")
    assert record is not None and record["files"] == 1


def test_the_backlog_since_stamp_is_not_restarted_by_a_later_tick(root):
    """The one-hour staleness clock has to survive repeated budgeted ticks.

    Re-minting `since` on every tick would keep the age below an hour forever
    and doctor's WARN would never fire, which is the whole point of the leg.

    Seeded an hour back rather than compared across two same-second ticks, for
    the reason spelled out on the deferral clock's sibling test: a stamp at
    `timespec="seconds"` makes a back-to-back comparison pass a re-minting
    implementation unless the two ticks straddle a second boundary.
    """
    ns, provider_root, _rollout = root
    _rollouts(provider_root, 3)
    cache = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](cache, budget_seconds=-1.0)
    finally:
        cache.close()
    record = _cache_meta(ns, "codex_ingest_backlog")
    assert record["files"] > 0, "the fixture left no backlog to age"
    aged = _iso_ago(hours=1)
    # `bytes` is `since`'s opposite, and it is what keeps this test honest: the
    # writer recomputes it from the walk every time and emits `int(owed_bytes)`,
    # so a negative seed is a value it can never produce. Surviving it means the
    # later tick left the ARMED record standing — under which a preserved
    # `since` proves nothing, because a mutation that stopped writing the record
    # at all would satisfy the assertion below just as well.
    _arm(ns, "codex_ingest_backlog",
         json.dumps({**record, "since": aged, "bytes": -1}, sort_keys=True))

    cache = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](cache, budget_seconds=-1.0)
    finally:
        cache.close()

    later = _cache_meta(ns, "codex_ingest_backlog")
    assert later["bytes"] >= 0, (
        "the later budgeted tick never rewrote the backlog record, so the "
        "preserved clock below is the seed reading itself back")
    assert later["since"] == aged, (
        "the later budgeted tick restarted the backlog clock, so doctor's "
        "one-hour WARN can never fire however far behind the store falls")


# ── the hook wiring ────────────────────────────────────────────────────────

def test_the_hook_passes_the_budget_and_the_active_transcript(root, monkeypatch):
    """Only the hook budgets its ingest, and only it names the live rollout.

    Every other caller — `cache-sync`, the dashboard, the targeted live-tail —
    must keep running to completion, so the keywords have to arrive from this
    one call site rather than from a default.
    """
    from types import SimpleNamespace

    ns, provider_root, _rollout = root
    seen = {}

    class _Cache:
        def close(self):
            pass

    monkeypatch.setitem(ns, "open_cache_db", lambda: _Cache())
    monkeypatch.setitem(
        ns, "sync_codex_cache",
        lambda conn, **kwargs: seen.update(kwargs) or SimpleNamespace(
            lock_contended=False, backlog_files=7),
    )
    monkeypatch.setitem(
        ns, "reconcile_codex_quota_projection",
        lambda **kwargs: SimpleNamespace(
            blocks_upserted=0, milestones_upserted=0, blocks_orphaned=0,
            milestones_orphaned=0, alerts_dispatched=0),
    )
    monkeypatch.setitem(
        ns, "maybe_record_codex_budget_milestone", lambda saved, **kwargs: 0)
    monkeypatch.setattr(
        "sys.stdin", _StdinPayload(json.dumps({
            "hook_event_name": "Stop", "session_id": "s",
            "transcript_path": "/codex/live/rollout.jsonl", "cwd": "/x"})))

    import argparse

    import _cctally_config as cfg
    assert ns["cmd_hook_tick"](argparse.Namespace(
        source="codex", foreground=True, explain=False, no_oauth=True,
        throttle_seconds=0, event=None)) == 0

    assert seen["budget_seconds"] == (
        cfg.CODEX_HOOK_INGEST_BUDGET_DEFAULT_SECONDS)
    assert seen["active_transcript_path"] == "/codex/live/rollout.jsonl"
    assert seen["lock_timeout"] == 0

    log = (ns["APP_DIR"] / "logs" / "hook-tick.log").read_text()
    assert "backlog=7" in log, (
        "the Codex lifecycle line must surface the remaining backlog")


class _StdinPayload:
    """Minimal stdin stand-in: the hook reads `sys.stdin.buffer.read(n)`."""

    def __init__(self, text: str) -> None:
        self.buffer = _StdinBuffer(text.encode("utf-8"))


class _StdinBuffer:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self, size: int = -1) -> bytes:
        data, self._data = self._data, b""
        return data


# ── Task 13: one reconcile per tick ────────────────────────────────────────

def test_the_hook_tick_reconciles_exactly_once(root, monkeypatch):
    """The headline defect: two full reconciles per growing turn.

    `sync_codex_cache` fires a reconcile after releasing its writer flocks
    whenever the Codex physical mutation sequence advanced — which ingesting
    new bytes always causes — and the hook then calls one explicitly. The
    second call could never take the certificate short-circuit, because that
    short-circuit is guarded by `not alert_eligible_roots` and the hook always
    passes a non-empty set. Both calls therefore cost full price.
    """
    ns, provider_root, _rollout = root
    _rollouts(provider_root, 2)
    (provider_root / "auth.json").write_text(_auth_json("acct-red", "red@x.com"))

    import _cctally_quota as quota
    import _cctally_update
    calls = []
    real = quota.reconcile_codex_quota_projection

    # This store has no projector state yet, so the hook's `defer` hands the
    # (whole-history) first pass to a detached worker. Stub the spawn: a real
    # one would outlive the test as an orphan process holding the tmp APP_DIR.
    monkeypatch.setattr(_cctally_update, "_spawn_detached", lambda command: True)

    def _counting(**kwargs):
        calls.append(tuple(sorted(kwargs)))
        return real(**kwargs)

    monkeypatch.setattr(
        quota, "reconcile_codex_quota_projection", _counting)
    monkeypatch.setitem(ns, "reconcile_codex_quota_projection", _counting)
    monkeypatch.setattr(
        "sys.stdin", _StdinPayload(json.dumps({
            "hook_event_name": "Stop", "session_id": "s",
            "transcript_path": "", "cwd": "/x"})))

    import argparse
    assert ns["cmd_hook_tick"](argparse.Namespace(
        source="codex", foreground=True, explain=False, no_oauth=True,
        throttle_seconds=0, event=None)) == 0

    assert len(calls) == 1, (
        f"the hook tick ran {len(calls)} reconciles: {calls}")
    assert "alert_eligible_root_keys" in calls[0], (
        "the surviving reconcile is not the alert-eligible one")


def test_the_hook_defers_the_periodic_verification(root, monkeypatch):
    """The one unbounded operation left must not ride the blocking hook path.

    The daily whole-history pass is ~14-30s against Codex's 30-second timeout,
    and a hook-only install has no dashboard tick or `codex quota` invocation to
    reach the deadline first. Only this call site defers it.
    """
    from types import SimpleNamespace

    ns, provider_root, _rollout = root
    _rollouts(provider_root, 2)
    seen = {}

    monkeypatch.setitem(
        ns, "reconcile_codex_quota_projection",
        lambda **kwargs: seen.update(kwargs) or SimpleNamespace(
            blocks_upserted=0, milestones_upserted=0, blocks_orphaned=0,
            milestones_orphaned=0, alerts_dispatched=0),
    )
    monkeypatch.setattr(
        "sys.stdin", _StdinPayload(json.dumps({
            "hook_event_name": "Stop", "session_id": "s",
            "transcript_path": "", "cwd": "/x"})))

    import argparse
    assert ns["cmd_hook_tick"](argparse.Namespace(
        source="codex", foreground=True, explain=False, no_oauth=True,
        throttle_seconds=0, event=None)) == 0

    assert seen["full_pass"] == "defer"


def test_every_other_caller_still_reconciles_inside_the_sync(root, monkeypatch):
    """`defer` is the hook's alone.

    The sync-internal reconcile is what keeps the projection current for the
    dashboard and `cache-sync`; making it conditional for everyone would trade
    one defect for a staler one.
    """
    ns, provider_root, _rollout = root
    _rollouts(provider_root, 2)

    import _cctally_quota as quota
    calls = []
    real = quota.reconcile_codex_quota_projection
    monkeypatch.setattr(
        quota, "reconcile_codex_quota_projection",
        lambda **kwargs: calls.append(kwargs) or real(**kwargs))

    cache = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](cache)
    finally:
        cache.close()

    assert len(calls) == 1


def test_quota_reconcile_rejects_an_unknown_mode(root):
    ns, _provider_root, _rollout = root
    cache = ns["open_cache_db"]()
    try:
        with pytest.raises(ValueError):
            ns["sync_codex_cache"](cache, quota_reconcile="sometimes")
    finally:
        cache.close()


# ── config: the budget knob ────────────────────────────────────────────────

def test_the_budget_is_capped_below_the_codex_hook_timeout(root):
    """Positive is not enough.

    An operator who typed 60 asked for exactly the timeout this budget exists
    to prevent, so it is rejected rather than silently clamped.
    """
    import _cctally_config as cfg

    assert cfg._validate_codex_hook_ingest_budget_value("5") == 5.0
    assert cfg._validate_codex_hook_ingest_budget_value(2) == 2.0
    for bad in (0, -1, "abc", True, cfg.CODEX_HOOK_INGEST_BUDGET_MAX_SECONDS,
                60, "60"):
        with pytest.raises(ValueError):
            cfg._validate_codex_hook_ingest_budget_value(bad)
    assert (cfg.CODEX_HOOK_INGEST_BUDGET_MAX_SECONDS
            < cfg.CODEX_HOOK_TIMEOUT_SECONDS)


def test_a_corrupt_budget_resolves_to_the_default(root):
    """A hand-edited config must never be able to make the hook itself fail."""
    import _cctally_config as cfg

    default = cfg.CODEX_HOOK_INGEST_BUDGET_DEFAULT_SECONDS
    assert cfg.resolve_codex_hook_ingest_budget({}) == default
    assert cfg.resolve_codex_hook_ingest_budget(
        {"codex": {"hook": {"ingest_budget_seconds": "nonsense"}}}) == default
    assert cfg.resolve_codex_hook_ingest_budget(
        {"codex": {"hook": {"ingest_budget_seconds": 999}}}) == default
    assert cfg.resolve_codex_hook_ingest_budget(
        {"codex": {"hook": {"ingest_budget_seconds": 2.5}}}) == 2.5


def test_the_budget_key_is_settable_and_unsettable(root):
    """Full `config get|set|unset` round trip on the new key."""
    import argparse

    import _cctally_config as cfg

    assert "codex.hook.ingest_budget_seconds" in cfg.ALLOWED_CONFIG_KEYS
    key = "codex.hook.ingest_budget_seconds"
    assert cfg._cmd_config_set(argparse.Namespace(
        key=key, value="3", emit_json=False)) == 0
    assert cfg._config_known_value(cfg._load_config_unlocked(), key) == 3.0
    assert cfg._cmd_config_set(argparse.Namespace(
        key=key, value="99", emit_json=False)) == 2
    assert cfg._config_known_value(cfg._load_config_unlocked(), key) == 3.0, (
        "a rejected value must not have been written")
    assert cfg._cmd_config_unset(argparse.Namespace(key=key)) == 0
    assert cfg._config_known_value(cfg._load_config_unlocked(), key) == (
        cfg.CODEX_HOOK_INGEST_BUDGET_DEFAULT_SECONDS)


if __name__ == "__main__":  # pragma: no cover - convenience
    raise SystemExit(pytest.main([__file__, "-v"]))
