"""#944 U1: existing Claude cost stamps survive the upgrade (spec 5.6-5.7).

Migration 050 exports every unlinked accounting row's CURRENT stamp (NULL
included) as a ``preserved_history`` decision, in bounded chunks gated on
migration 029; migration 001 links every unlinked row before its wipe, any
population; migration 029 never overwrites a linked row. An "old writer" here
is a sync with the attribution schema reported absent: it stamps the ingest
identity and leaves the row unlinked, exactly as a pre-#944 release (or a
downgrade) does.
"""
from __future__ import annotations

import json
import pathlib
import sqlite3
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
for _p in (REPO_ROOT / "bin", REPO_ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from conftest import load_script, redirect_paths  # noqa: E402
import _lib_accounts as accts  # noqa: E402
from test_claude_account_attribution import (  # noqa: E402
    MODEL, _clock, _decision_ops, _line, _login, _obs, _reset_identity_cache,
    _sync,
)

M050 = "050_preserve_claude_cost_attribution"
M001 = "001_dedup_highest_wins"
M029 = "029_backfill_claude_account"
M030 = "030_session_entries_cache_creation_split"


@pytest.fixture
def pres(tmp_path, monkeypatch):
    """A current install whose accounts cutover recorded the real account Z,
    so a pending 029 would restamp every NULL row it reaches to Z."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    _reset_identity_cache()
    from types import SimpleNamespace
    env = SimpleNamespace(ns=ns, root=None, mp=monkeypatch,
                          order={"reverse": False}, extra=[])
    z = _login("Z")
    ns["open_db"]().close()
    import _cctally_cache as cc
    import _cctally_journal as jr
    jr.ensure_account_bound_cutover()
    jr.append_accounts_cutover_op(z)
    assert jr.find_accounts_cutover_op() == z
    root = tmp_path / "projects" / "-home-u-proj"
    root.mkdir(parents=True)
    env.root = root

    def discover():
        files = sorted(root.glob("*.jsonl"), key=str) + list(env.extra)
        return list(reversed(files)) if env.order["reverse"] else files
    monkeypatch.setattr(cc, "_iter_claude_jsonl_files", discover)
    ns["open_cache_db"]().close()
    return env


def _logout():
    import _cctally_core as core
    if core.CLAUDE_JSON_PATH.exists():
        core.CLAUDE_JSON_PATH.unlink()
    _reset_identity_cache()


def _old_writer_sync(env, **kw):
    """A sync by a writer that knows nothing of attribution decisions."""
    import _cctally_claude_attribution as G
    with pytest.MonkeyPatch.context() as m:
        m.setattr(G, "schema_present", lambda conn: False)
        return _sync(env, **kw)


def _cache():
    import _cctally_core as core
    return sqlite3.connect(core.CACHE_DB_PATH)


def _set_state(name, state):
    """Put cache migration ``name`` in ``state`` the way the dispatcher and
    `db skip` leave it, and invalidate the fast path as `db unskip` does."""
    import _cctally_core as core
    conn = _cache()
    try:
        conn.execute("DELETE FROM schema_migrations WHERE name=?", (name,))
        conn.execute("DELETE FROM schema_migrations_skipped WHERE name=?",
                     (name,))
        if state == "skipped":
            conn.execute(
                "INSERT INTO schema_migrations_skipped (name, skipped_at_utc,"
                " reason) VALUES (?, ?, 'test')",
                (name, core.now_utc_iso()))
        elif state == "applied":
            conn.execute(
                "INSERT INTO schema_migrations (name, applied_at_utc) "
                "VALUES (?, ?)", (name, core.now_utc_iso()))
        conn.execute("PRAGMA user_version = 0")
        conn.commit()
    finally:
        conn.close()


def _state(name):
    conn = _cache()
    try:
        if conn.execute("SELECT 1 FROM schema_migrations WHERE name=?",
                        (name,)).fetchone():
            return "applied"
        if conn.execute("SELECT 1 FROM schema_migrations_skipped WHERE name=?",
                        (name,)).fetchone():
            return "skipped"
        return "pending"
    finally:
        conn.close()


def _open(env):
    env.ns["open_cache_db"]().close()


def _cli(env, *argv):
    args = env.ns["build_parser"]().parse_args(list(argv))
    return args.func(args)


def _rows():
    """``{(path name, line_offset): (msg_id, account, basis, form)}``."""
    conn = _cache()
    try:
        return {(pathlib.Path(r[0]).name, r[1]): tuple(r[2:]) for r in
                conn.execute(
                    "SELECT e.source_path, e.line_offset, e.msg_id,"
                    " e.account_key, d.basis, d.form FROM session_entries e"
                    " LEFT JOIN claude_cost_decisions d"
                    " ON d.decision_key = e.attribution_decision_key")}
    finally:
        conn.close()


def _unlinked():
    conn = _cache()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM session_entries WHERE "
            "attribution_decision_key IS NULL").fetchone()[0]
    finally:
        conn.close()


def _progress():
    import _cctally_claude_attribution as G
    conn = _cache()
    try:
        return G._meta_json(conn, G.PRESERVATION_KEY)
    finally:
        conn.close()


def _accounts():
    return {k: v[1] for k, v in _rows().items()}


def _key(name):
    return accts.account_key("claude", name)


def _raw_line(env, obj, *, file="s1"):
    with (env.root / f"{file}.jsonl").open("a") as fh:
        fh.write(json.dumps(obj) + "\n")


def _unkeyed(at, *, out=500, cache_read=0, split=False):
    usage = {"input_tokens": 1000, "output_tokens": out,
             "cache_creation_input_tokens": 100 if split else 0,
             "cache_read_input_tokens": cache_read}
    if split:
        usage["cache_creation"] = {"ephemeral_1h_input_tokens": 40,
                                   "ephemeral_5m_input_tokens": 60}
    return {"type": "assistant", "timestamp": at, "sessionId": "s1",
            "cwd": "/home/u/proj", "message": {"model": MODEL,
                                               "usage": usage}}


# --------------------------------------------------------------------------
# migration 050
# --------------------------------------------------------------------------

def _mixed_history(env):
    """Pre-upgrade history: keyed rows stamped A and B, a stably-absent NULL
    row, an orphan-source row stamped A and a NULL-id row stamped A."""
    _login("A")
    _clock(env, "2026-10-09T10:05:00Z")
    _line(env, "2026-10-09T10:01:00Z", "a1")
    _raw_line(env, _unkeyed("2026-10-09T10:01:30Z"))
    _line(env, "2026-10-09T10:01:40Z", "gone", file="orphan")
    _old_writer_sync(env)
    (env.root / "orphan.jsonl").unlink()
    _login("B")
    _line(env, "2026-10-09T10:02:00Z", "b1", file="s2")
    _old_writer_sync(env)
    _logout()
    _line(env, "2026-10-09T10:03:00Z", "n1", file="s3")
    _old_writer_sync(env)
    return {("s1.jsonl", 0): _key("A"), ("orphan.jsonl", 0): _key("A"),
            ("s2.jsonl", 0): _key("B"), ("s3.jsonl", 0): None}


def test_050_exports_every_existing_stamp_unchanged(pres):
    """U1: every pre-upgrade row (mixed real stamps, NULL, an orphan whose
    transcript is gone, a row lacking an id) keeps its account and becomes
    durable as preserved history. Detects: the upgrade re-attributing
    existing history, or a later rebuild losing it."""
    expected = _mixed_history(pres)
    before = _accounts()
    assert _unlinked() == len(before) == 5
    _set_state(M050, "pending")
    _open(pres)
    assert _state(M050) == "applied" and _unlinked() == 0
    rows = _rows()
    assert _accounts() == before
    for key, account in expected.items():
        assert rows[key][1] == account and rows[key][2] == "preserved_history"
    forms = {r[3] for r in rows.values() if r[0] is None}
    assert forms == {"l1"}
    progress = _progress()
    assert (progress["saved"], progress["total"]) == (5, 5)
    assert progress["lastError"] is None


@pytest.mark.parametrize("stamp", ["unattributed", ""],
                         ids=["sentinel", "empty"])
@pytest.mark.parametrize("path", ["050", "rebuild"])
def test_a_literal_unattributed_stamp_is_preserved_as_null(pres, path, stamp):
    """A pre-upgrade row may carry the literal ``unattributed`` sentinel
    (read paths treat it as NULL), or an empty stamp (#944 F1); the
    journal's two-shaped stamp rule forbids both in a decision, which
    carries NULL. Detects: 050 failing on every open, or a rebuild crashing,
    over such a row instead of preserving it as unattributed; and 050's link
    rewriting the row's bytes, which drifts a reviewed rederive's cache
    fingerprint for no change in any account. A rebuild re-ingests every row
    from its decision, so there the row carries the decision's NULL."""
    _login("A")
    _clock(pres, "2026-10-09T10:05:00Z")
    _line(pres, "2026-10-09T10:01:00Z", "a1")
    _raw_line(pres, _unkeyed("2026-10-09T10:01:30Z"))
    _old_writer_sync(pres)
    conn = _cache()
    try:
        conn.execute("UPDATE session_entries SET account_key=?", (stamp,))
        conn.commit()
    finally:
        conn.close()
    if path == "050":
        _set_state(M050, "pending")
        _open(pres)
        assert _state(M050) == "applied"
    else:
        assert _cli(pres, "cache-sync", "--source", "claude",
                    "--rebuild") == 0
    rows = _rows()
    assert len(rows) == 2
    assert {(r[1], r[2]) for r in rows.values()} == {
        (stamp if path == "050" else None, "preserved_history")}
    conn = _cache()
    try:
        assert conn.execute(
            "SELECT DISTINCT account_key FROM claude_cost_decisions"
        ).fetchall() == [(None,)]
    finally:
        conn.close()
    assert _unlinked() == 0


@pytest.mark.parametrize("path", ["050", "rebuild"])
def test_a_stored_empty_id_row_is_exported_by_its_projection(pres, path):
    """#944 F1: a transcript line with an empty requestId, stored before the
    parser normalized empty ids, carries an empty-string req_id. Detects:
    its export building a keyed item the journal rejects -- a ValueError
    that failed migration 050 on every open and aborted every rebuild --
    instead of exporting the row's projection (legacy form) with its stamp
    unchanged."""
    _login("A")
    _clock(pres, "2026-10-09T10:05:00Z")
    obj = _unkeyed("2026-10-09T10:01:00Z")
    obj["requestId"] = ""
    obj["message"]["id"] = "m-a1"
    _raw_line(pres, obj)
    _old_writer_sync(pres)
    conn = _cache()
    try:
        # The pre-fix parser stored the empty id as it was.
        conn.execute("UPDATE session_entries SET req_id=''")
        conn.commit()
    finally:
        conn.close()
    if path == "050":
        _set_state(M050, "pending")
        _open(pres)
        assert _state(M050) == "applied"
        ((msg_id, account, basis, form),) = _rows().values()
        assert (msg_id, account, basis, form) == (
            "m-a1", _key("A"), "preserved_history", "l1")
    else:
        _login("B")
        assert _cli(pres, "cache-sync", "--source", "claude",
                    "--rebuild") == 0
        # The re-read record (now id-less) falls back to the exported
        # legacy decision at its location: the account is kept.
        (row,) = _rows().values()
        assert row[1:3] == (_key("A"), "preserved_history")
    assert _unlinked() == 0


def test_050_records_the_high_water_its_replay_consumed(pres):
    """#944 F8 (spec 5.4): every exported decision records the safe
    high-water the export's replay consumed. Detects: an export sampling the
    high-water at its first append, past a record another writer appended
    after the replay."""
    import _cctally_claude_attribution as G
    import _cctally_journal as jr
    _mixed_history(pres)
    _set_state(M050, "pending")
    real = G.rehydrate_claude_attribution
    consumed = []

    def replay_then_foreign_append(conn, **kw):
        out = real(conn, **kw)
        consumed.append(list(jr.journal_safe_high_water()))
        _obs("B", "2026-10-09T09:00:00Z")
        return out
    pres.mp.setattr(G, "rehydrate_claude_attribution",
                    replay_then_foreign_append)
    _open(pres)
    assert _state(M050) == "applied"
    (hw,) = consumed
    ops = _decision_ops()
    assert ops and {tuple(op["journal_hw"]) for op in ops} == {tuple(hw)}


@pytest.mark.parametrize("change,expected_basis", [
    (0, "preserved_history"), (7, "fallback")])
def test_a_rebuild_keeps_a_legacy_decision_only_for_identical_bytes(
        pres, change, expected_basis):
    """A row lacking an id is exported by its projection; a replacement at
    the same offset that differs only in cache-read tokens is a different
    record and gets its own decision (residual R8 covers identical bytes).
    Detects: an exported stamp inherited by a different record. (The
    rebuild's own preserve-before-wipe exports the same decision, so this
    also guards that path; it is not specific to 050.)"""
    _login("A")
    _clock(pres, "2026-10-09T10:05:00Z")
    _raw_line(pres, _unkeyed("2026-10-09T10:01:30Z"))
    _old_writer_sync(pres)
    _set_state(M050, "pending")
    _open(pres)
    (pres.root / "s1.jsonl").write_text(json.dumps(_unkeyed(
        "2026-10-09T10:01:30Z", cache_read=change)) + "\n")
    _login("B")
    _clock(pres, "2026-10-09T10:06:00Z")
    _sync(pres, rebuild=True)
    (row,) = _rows().values()
    assert row[2] == expected_basis
    assert row[1] == _key("A" if change == 0 else "B")


def test_050_resumes_in_committed_chunks(pres, monkeypatch):
    """Detects: an export that cannot bound one open's work, loses committed
    chunks when it defers, or stamps itself applied with rows unlinked."""
    import _cctally_db
    _mixed_history(pres)
    monkeypatch.setattr(_cctally_db, "_CLAUDE_PRESERVATION_CHUNK_ROWS", 2)
    _set_state(M050, "pending")
    seen = []
    for _ in range(3):
        _open(pres)
        seen.append((_state(M050), _unlinked(), _progress()["saved"]))
    assert seen == [("pending", 3, 2), ("pending", 1, 4), ("applied", 0, 5)]
    assert _progress()["total"] == 5


def test_050_waits_for_029(pres, monkeypatch):
    """While 029 is pending (here: its journal lookup fails) the export must
    not record NULL stamps 029 is about to backfill; a skipped 029 releases
    it. Detects: U1 exporting the pre-029 state, or 050 blocked forever by a
    skipped 029."""
    import _cctally_journal as jr
    _logout()
    _clock(pres, "2026-10-09T10:05:00Z")
    _line(pres, "2026-10-09T10:01:00Z", "n1")
    _old_writer_sync(pres)
    _set_state(M029, "pending")
    _set_state(M050, "pending")

    def unreadable():
        raise OSError(5, "Input/output error")
    with pytest.MonkeyPatch.context() as m:
        m.setattr(jr, "find_accounts_cutover_op", unreadable)
        _open(pres)
    assert (_state(M029), _state(M050), _unlinked()) == (
        "pending", "pending", 1)
    _set_state(M029, "skipped")
    _set_state(M050, "pending")
    _open(pres)
    assert _state(M050) == "applied"
    assert _rows()[("s1.jsonl", 0)][1:3] == (None, "preserved_history")


def test_050_records_a_stall_and_resumes(pres, monkeypatch):
    """U4': an unwritable journal stalls the export (recorded for doctor),
    links nothing and stamps nothing; the next open resumes. Detects: a
    silent stall, or links written for decisions the journal never got."""
    import _cctally_journal as jr
    _mixed_history(pres)
    _set_state(M050, "pending")
    real = jr.append_records

    def boom(*_a, **_k):
        raise OSError(30, "Read-only file system")
    monkeypatch.setattr(jr, "append_records", boom)
    _open(pres)
    progress = _progress()
    assert _state(M050) == "pending" and _unlinked() == 5
    assert progress["lastError"] and progress["lastErrorAt"]
    assert progress["saved"] == 0
    monkeypatch.setattr(jr, "append_records", real)
    _open(pres)
    progress = _progress()
    assert _state(M050) == "applied" and _unlinked() == 0
    assert progress["lastError"] is None and progress["saved"] == 5


def test_050_aligns_a_downgrade_restamp_and_a_rebuild_keeps_it(pres):
    """A decided row an older writer restamped (and unlinked) is aligned to
    its decision by 050, and reads the decided account before and after a
    rebuild. Detects: the downgrade's ingest-time restamp surviving the
    upgrade, or flipping at the next rebuild."""
    import _cctally_claude_attribution as G
    _obs("A", "2026-10-09T10:00:00Z")
    _line(pres, "2026-10-09T10:01:00Z", "x")
    _obs("A", "2026-10-09T10:02:00Z")
    _login("B")
    _clock(pres, "2026-10-09T10:05:00Z")
    _sync(pres)
    assert _accounts()[("s1.jsonl", 0)] == _key("A")
    conn = _cache()
    try:
        conn.execute("UPDATE session_entries SET account_key=?,"
                     " attribution_decision_key=NULL", (_key("B"),))
        conn.commit()
    finally:
        conn.close()
    _set_state(M050, "pending")
    _open(pres)
    assert _state(M050) == "applied"
    assert _rows()[("s1.jsonl", 0)][1:3] == (_key("A"), "timeline_agreement")
    _login("C")
    _sync(pres, rebuild=True)
    assert _rows()[("s1.jsonl", 0)][1:3] == (_key("A"), "timeline_agreement")
    conn = _cache()
    try:
        assert G._meta_json(conn, G.ALIGNED_KEY)["count"] == 1
    finally:
        conn.close()
    assert len(_decision_ops("m-x")) == 1


def test_two_spellings_of_one_root_keep_their_own_stamps(pres):
    """A record lacking an id that an old writer ingested through two
    spellings of one root is two rows (A and B); each keeps its own stamp
    through 050 and a rebuild. Detects: one decision shared by two
    pre-upgrade rows (alignment would overwrite U1-protected history).
    Compatibility for the rebuild path, which exports the same way."""
    alias = pres.root.parent / "alias"
    alias.symlink_to(pres.root, target_is_directory=True)
    _raw_line(pres, _unkeyed("2026-10-09T10:01:30Z"))
    _clock(pres, "2026-10-09T10:05:00Z")
    _login("A")
    _old_writer_sync(pres)
    _login("B")
    pres.extra.append(alias / "s1.jsonl")
    _old_writer_sync(pres)
    by_path = {}
    conn = _cache()
    try:
        for path, account in conn.execute(
                "SELECT source_path, account_key FROM session_entries"):
            by_path[path] = account
    finally:
        conn.close()
    assert sorted(by_path.values()) == sorted([_key("A"), _key("B")])
    _set_state(M050, "pending")
    _open(pres)
    _login("C")
    _sync(pres, rebuild=True)
    conn = _cache()
    try:
        after = dict(conn.execute(
            "SELECT source_path, account_key FROM session_entries"))
    finally:
        conn.close()
    assert after == by_path


def test_the_195_rewalk_keeps_a_legacy_decision(pres):
    """030 armed, 050 exports a NULL-id row with an unknown TTL split, the
    re-walk fills the split, then a rebuild under another login: the row
    stays on its exported account and no new decision is journaled.
    Detects: the #195 enrichment changing a legacy identity (the rebuild
    would re-decide it to the current login)."""
    _login("A")
    _clock(pres, "2026-10-09T10:05:00Z")
    _raw_line(pres, _unkeyed("2026-10-09T10:01:30Z", split=True))
    _old_writer_sync(pres)
    conn = _cache()
    try:
        conn.execute("UPDATE session_entries SET cache_create_1h_tokens=NULL,"
                     " cache_create_5m_tokens=NULL")
        conn.commit()
    finally:
        conn.close()
    _set_state(M030, "pending")
    _set_state(M050, "pending")
    _login("B")
    _open(pres)
    assert _state(M050) == "applied"
    ops_after_export = len(_decision_ops())
    _sync(pres)
    conn = _cache()
    try:
        split = conn.execute("SELECT cache_create_1h_tokens,"
                             " cache_create_5m_tokens FROM session_entries"
                             ).fetchone()
    finally:
        conn.close()
    assert tuple(split) == (40, 60)
    _sync(pres, rebuild=True)
    (row,) = _rows().values()
    assert row[1:3] == (_key("A"), "preserved_history")
    assert len(_decision_ops()) == ops_after_export


def test_ordinary_ingest_during_the_export_preserves_an_unlinked_row(
        pres, monkeypatch):
    """While 050 is still pending, an ingest reaching an existing unlinked
    row (a resumed copy of its record) records the row's current stamp, not
    a new kernel decision. Detects: pre-upgrade history re-attributed by
    write-time evidence before the export reached it."""
    import _cctally_db
    monkeypatch.setattr(_cctally_db, "_CLAUDE_PRESERVATION_CHUNK_ROWS", 1)
    _login("A")
    _clock(pres, "2026-10-09T10:05:00Z")
    _line(pres, "2026-10-09T10:01:00Z", "first")
    _line(pres, "2026-10-09T10:01:30Z", "second")
    _line(pres, "2026-10-09T10:02:00Z", "x")
    _old_writer_sync(pres)
    _set_state(M050, "pending")
    _open(pres)
    assert _state(M050) == "pending" and _unlinked() == 2
    _obs("B", "2026-10-09T10:01:50Z")
    _obs("B", "2026-10-09T10:02:10Z")
    _line(pres, "2026-10-09T10:02:00Z", "x", file="s2")
    _login("B")
    _sync(pres)  # its open links "second"; the ingest itself reaches "x"
    (op,) = _decision_ops("m-x")
    assert (op["basis"], op.get("account_key")) == (
        "preserved_history", _key("A"))


# --------------------------------------------------------------------------
# migrations 001 and 029
# --------------------------------------------------------------------------

@pytest.mark.parametrize("population", ["real", "null", "null-029-skipped"])
def test_an_unskipped_001_preserves_every_population(pres, population):
    """001 links every unlinked row before its wipe, whatever the stamps;
    with 029 also skipped and unskipped (`db unskip` of both) the NULL rows
    stay NULL. Detects: 001's re-ingest restamping history to the current
    login, or 029 then restamping deliberate NULLs to the cutover account."""
    for name in (M001, *(("null-029-skipped" == population) * (M029,))):
        _set_state(name, "skipped")
    _clock(pres, "2026-10-09T10:05:00Z")
    if population == "real":
        _login("A")
    else:
        _logout()
    _line(pres, "2026-10-09T10:01:00Z", "x")
    _line(pres, "2026-10-09T10:02:00Z", "y", file="s2")
    _old_writer_sync(pres)
    expected = _accounts()
    assert set(expected.values()) == ({_key("A")} if population == "real"
                                      else {None})
    for name in (M001, *(("null-029-skipped" == population) * (M029,))):
        assert _cli(pres, "db", "unskip", f"cache.db:{name}") == 0
    _login("B")
    _open(pres)
    assert _state(M001) == "applied"
    _sync(pres)
    assert _accounts() == expected
    assert {r[2] for r in _rows().values()} == {"preserved_history"}


def test_decided_nulls_survive_a_deferred_029(pres):
    """Stably-absent records ingested while 029 is deferred are decided
    NULL; when 029 finally runs it leaves them NULL. Detects: 029's cutover
    backfill overwriting a decided NULL."""
    import _cctally_journal as jr
    _set_state(M029, "pending")

    def unreadable():
        raise OSError(5, "Input/output error")
    _logout()
    _clock(pres, "2026-10-09T10:05:00Z")
    _line(pres, "2026-10-09T10:01:00Z", "x")
    with pytest.MonkeyPatch.context() as m:
        m.setattr(jr, "find_accounts_cutover_op", unreadable)
        _sync(pres)
        assert _state(M029) == "pending"
    assert _rows()[("s1.jsonl", 0)][1:3] == (None, "fallback")
    _open(pres)
    assert _state(M029) == "applied"
    assert _rows()[("s1.jsonl", 0)][1:3] == (None, "fallback")


def test_skip_029_export_then_unskip_029_keeps_the_nulls(pres):
    """Detects: unskipping 029 after the export overwriting preserved NULLs
    with the cutover account."""
    _logout()
    _clock(pres, "2026-10-09T10:05:00Z")
    _line(pres, "2026-10-09T10:01:00Z", "x")
    _old_writer_sync(pres)
    _set_state(M029, "skipped")
    _set_state(M050, "pending")
    _open(pres)
    assert _state(M050) == "applied"
    assert _cli(pres, "db", "unskip", f"cache.db:{M029}") == 0
    _open(pres)
    assert _state(M029) == "applied"
    assert _rows()[("s1.jsonl", 0)][1:3] == (None, "preserved_history")


def test_001_without_a_writable_journal_defers_without_wiping(
        pres, monkeypatch):
    """Detects: 001 wiping rows it could not make durable."""
    import _cctally_journal as jr
    _set_state(M001, "skipped")
    _login("A")
    _clock(pres, "2026-10-09T10:05:00Z")
    _line(pres, "2026-10-09T10:01:00Z", "x")
    _old_writer_sync(pres)
    assert _cli(pres, "db", "unskip", f"cache.db:{M001}") == 0

    def boom(*_a, **_k):
        raise OSError(30, "Read-only file system")
    monkeypatch.setattr(jr, "append_records", boom)
    _open(pres)
    assert _state(M001) == "pending"
    assert _accounts() == {("s1.jsonl", 0): _key("A")}


@pytest.mark.parametrize("migration", [M001, M029])
def test_a_direct_call_on_a_historical_store_behaves_as_before(
        pres, tmp_path, migration):
    """Compatibility: on a store without the attribution schema (a direct
    handler call on a historical ``pre.sqlite``) 001 wipes and 029 backfills
    exactly as before, and nothing is journaled."""
    import _cctally_db as db
    path = tmp_path / "historical.sqlite"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            "CREATE TABLE schema_migrations (name TEXT PRIMARY KEY,"
            " applied_at_utc TEXT NOT NULL);"
            "CREATE TABLE cache_meta (key TEXT PRIMARY KEY, value TEXT);"
            "CREATE TABLE session_entries (id INTEGER PRIMARY KEY,"
            " source_path TEXT, line_offset INTEGER, account_key TEXT);"
            "CREATE TABLE session_files (path TEXT PRIMARY KEY,"
            " account_key TEXT);"
            "INSERT INTO session_entries (source_path, line_offset)"
            " VALUES ('/p/a.jsonl', 0);"
            "INSERT INTO session_files (path) VALUES ('/p/a.jsonl');")
        ops = len(_decision_ops())
        next(m.handler for m in db._CACHE_MIGRATIONS
             if m.name == migration)(conn)
        rows = conn.execute(
            "SELECT account_key FROM session_entries").fetchall()
        assert rows == ([] if migration == M001 else [(_key("Z"),)])
        assert len(_decision_ops()) == ops
    finally:
        conn.close()
