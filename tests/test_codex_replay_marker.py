"""The cache-side byte-zero replay marker (spec §4.3).

Migration 035 arms a Codex replay by writing ONE ``cache_meta`` marker; it
clears no table. ``sync_codex_cache`` consumes the marker and ORs it into its
own ``rebuild``, which is what makes the full-rebuild path capture
``rebuild_known_identities`` BEFORE the clear.

That indirection is the whole point. A migration that called
``_clear_codex_derived_rows`` directly would delete ``codex_session_files`` out
of band, leaving the next ordinary sync with an empty snapshot — every re-read
rollout would fall through to the live-``auth.json`` branch and historical Codex
spend would be re-attributed to whoever is authenticated now (spec §4.1).
"""
from __future__ import annotations

import base64
import datetime as dt
import fcntl
import json
import multiprocessing
import os
import pathlib
import shutil
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

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "codex-parity" / "v1" / "rollouts"


def _b64(obj) -> str:
    return base64.urlsafe_b64encode(
        json.dumps(obj).encode("utf-8")).decode("ascii").rstrip("=")


def _auth_json(account_id: str, email: str) -> str:
    payload = {
        "email": email,
        "https://api.openai.com/auth": {
            "chatgpt_account_id": account_id,
            "chatgpt_plan_type": "pro",
        },
    }
    id_token = (
        f"{_b64({'alg': 'RS256', 'typ': 'JWT'})}.{_b64(payload)}.sig")
    return json.dumps({
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": id_token, "access_token": "a", "refresh_token": "r",
        },
        "last_refresh": "2026-07-20T00:00:00Z",
    })


def _expected_key(account_id: str, email: str) -> str:
    return accts.account_key("codex", account_id + "\0" + email)


def _setup_root(tmp_path, rollout_name="modern-full.jsonl"):
    provider_root = tmp_path / "codex-provider"
    rollout = provider_root / "sessions" / "2026" / "07" / "20" / "rollout.jsonl"
    rollout.parent.mkdir(parents=True)
    shutil.copyfile(FIXTURES / rollout_name, rollout)
    return provider_root, rollout


@pytest.fixture
def env(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    provider_root, rollout = _setup_root(tmp_path)
    monkeypatch.setenv("CODEX_HOME", str(provider_root))
    return ns, provider_root, rollout


def _arm(conn):
    import _cctally_cache
    conn.execute(
        "INSERT OR REPLACE INTO cache_meta(key,value) VALUES(?,?)",
        (_cctally_cache.CODEX_REPLAY_FROM_ZERO_KEY, "1"),
    )
    conn.commit()


def _armed(conn) -> bool:
    import _cctally_cache
    return conn.execute(
        "SELECT 1 FROM cache_meta WHERE key=?",
        (_cctally_cache.CODEX_REPLAY_FROM_ZERO_KEY,),
    ).fetchone() is not None


def test_marker_promotes_an_ordinary_sync_to_rebuild(env):
    """A ``rebuild=False`` call must take the full-rebuild path while armed."""
    ns, _provider_root, _rollout = env
    conn = ns["open_cache_db"]()
    try:
        first = ns["sync_codex_cache"](conn)
        assert first.files_processed == 1
        # An unarmed second sync is the delta no-op: nothing re-read.
        unchanged = ns["sync_codex_cache"](conn)
        assert unchanged.files_skipped_unchanged == 1
        assert unchanged.files_processed == 0

        _arm(conn)
        replayed = ns["sync_codex_cache"](conn)
        # Byte-zero: the file is re-read despite an unchanged size.
        assert replayed.files_processed == 1
        assert replayed.files_skipped_unchanged == 0
    finally:
        conn.close()


def test_marker_is_cleared_only_after_a_clean_full_walk(env):
    """The marker is what makes the repair retry, so a contended or targeted
    call must leave it standing."""
    ns, _provider_root, rollout = env
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        _arm(conn)

        # Contended: another holder of the Codex writer flock.
        import _cctally_core
        _cctally_core.CACHE_LOCK_PATH.touch()
        holder = open(_cctally_core.CACHE_LOCK_PATH, "w")
        fcntl.flock(holder, fcntl.LOCK_EX)
        try:
            contended = ns["sync_codex_cache"](conn)
            assert contended.lock_contended is True
        finally:
            fcntl.flock(holder, fcntl.LOCK_UN)
            holder.close()
        assert _armed(conn), "a contended sync must not consume the marker"

        # Targeted: declines, and must not consume it either.
        targeted = ns["sync_codex_cache"](conn, only_paths={str(rollout)})
        assert targeted.deferred_reason is not None
        assert _armed(conn), "a targeted sync must not consume the marker"

        clean = ns["sync_codex_cache"](conn)
        assert clean.files_processed == 1
        assert not _armed(conn), "a clean full walk consumes the marker"
    finally:
        conn.close()


def test_marker_declines_a_targeted_sync_instead_of_raising(env):
    """A live-tail tick must DEFER, never crash.

    ``sync_codex_cache`` raises ``ValueError`` on ``targeted and rebuild``, so
    promoting the marker into ``rebuild`` before that guard would convert every
    live-tail tick into an exception for as long as the replay is pending.
    """
    ns, _provider_root, rollout = env
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        _arm(conn)
        stats = ns["sync_codex_cache"](conn, only_paths={str(rollout)})
        assert stats.deferred_reason == "replay_pending"
        assert stats.targeted_clean is False
    finally:
        conn.close()


def test_pre_mechanism_file_stays_unattributed_across_a_marker_replay(env):
    """The blocking regression (spec §4.1).

    A rollout ingested before durable attribution existed has a
    ``codex_session_files`` cursor and NO journaled decision. Its bytes must
    come back unattributed across the replay, even though a different account
    is authenticated now. This holds only because the marker is consumed inside
    ``sync_codex_cache``, so ``rebuild_known_identities`` is captured from that
    seeded row before the clear.
    """
    ns, provider_root, rollout = env
    live = _expected_key("acct-blue", "blue@x.com")
    (provider_root / "auth.json").write_text(
        _auth_json("acct-blue", "blue@x.com"))

    import _cctally_cache
    from _lib_source_identity import source_root_key

    conn = ns["open_cache_db"]()
    try:
        root_key = source_root_key(str(provider_root.resolve()))
        st = rollout.stat()
        # Seed the pre-#416 world: a cursor row and nothing else. No journal op,
        # no `codex_file_accounts` range — the bytes were never durably decided.
        conn.execute(
            "INSERT INTO codex_session_files"
            "(path,size_bytes,mtime_ns,last_byte_offset,last_ingested_at,"
            " source_root_key) VALUES (?,?,?,?,?,?)",
            (str(rollout.resolve()), st.st_size, st.st_mtime_ns, st.st_size,
             dt.datetime.now(dt.timezone.utc).isoformat(), root_key),
        )
        conn.commit()
        assert conn.execute(
            "SELECT COUNT(*) FROM codex_file_accounts").fetchone()[0] == 0

        _arm(conn)
        replayed = ns["sync_codex_cache"](conn)
        assert replayed.files_processed == 1

        rows = conn.execute(
            "SELECT account_key FROM codex_session_entries").fetchall()
        assert rows, "expected the replay to re-emit accounting rows"
        assert all(row[0] is None for row in rows), (
            "pre-mechanism Codex spend was re-attributed to the live account "
            f"{live!r}; the replay must leave it unattributed"
        )
    finally:
        conn.close()


# --- #1051: an interrupt after the rebuild's clear commits -------------------


def _shape_pre_mechanism_history(conn):
    """Issue #1051 step 2: the rollout is history ingested before #416.

    No journal, no durable decision, no incarnation or ledger evidence: the
    rollout's cursor row is the only witness that the bytes predate the
    mechanism, which is what the D1 snapshot reads before the clear."""
    import _cctally_core
    shutil.rmtree(_cctally_core.JOURNAL_DIR, ignore_errors=True)
    conn.execute("DELETE FROM codex_file_accounts")
    conn.execute("DELETE FROM codex_file_incarnations")
    conn.execute(
        "DELETE FROM codex_accounting_change_log WHERE change_kind='path'")
    conn.execute("UPDATE codex_session_entries SET account_key = NULL")
    conn.commit()


@pytest.mark.parametrize("retry", ["ordinary", "rebuild"])
def test_1051_probe_interrupt_after_clear_commit(env, monkeypatch, retry):
    """The issue's literal probe: a marker-armed replay interrupted after its
    clear's commit, then retried by an ordinary sync or an explicit rebuild.
    Detects: pre-mechanism Codex spend re-attributed to the live account
    after an interrupted replay (stored-data accuracy, #1051; fixed by #982
    for this trigger and kept as its regression)."""
    ns, provider_root, _rollout = env
    import _cctally_journal
    conn = ns["open_cache_db"]()
    try:
        first = ns["sync_codex_cache"](conn)
        assert first.files_processed == 1
        _shape_pre_mechanism_history(conn)
        (provider_root / "auth.json").write_text(
            _auth_json("acct-blue", "blue@x.com"))

        def interrupted(*_a, **_k):
            raise RuntimeError("injected: interrupt after the clear's commit")

        with monkeypatch.context() as patch:
            patch.setattr(_cctally_journal,
                          "rehydrate_codex_journal_families", interrupted)
            _arm(conn)
            cut = ns["sync_codex_cache"](conn)
        assert cut.deferred_reason == "attribution_rehydration"
        assert _armed(conn)

        if retry == "rebuild":
            ns["sync_codex_cache"](conn, rebuild=True)
        else:
            ns["sync_codex_cache"](conn)
        rows = conn.execute(
            "SELECT account_key FROM codex_session_entries").fetchall()
        assert rows
        assert all(r[0] is None for r in rows), (
            "pre-mechanism Codex spend was re-attributed to the live account "
            "after an interrupted replay")
    finally:
        conn.close()


# --- #1051: an explicit rebuild persists its D1 set with the clear ----------
#
# Fixture: three rollouts in discovery order. R was ingested while auth.json
# named red, so a journaled decision covers it. H is pre-mechanism history: a
# cursor row and no decision, so only the D1 snapshot keeps it unattributed.
# U is on disk before the rebuild and was never ingested: no D1 evidence, so
# it takes a live first-ingest decision (blue). No rollout carries
# rate_limits, so no quota window exists and window spend adoption cannot
# stamp H for the wrong reason.

_RED = ("acct-red", "red@x.com")
_BLUE = ("acct-blue", "blue@x.com")


class _Injected(RuntimeError):
    """An injected interrupt."""


def _cc():
    import _cctally_cache
    return _cctally_cache


def _d1_write(path: pathlib.Path, session_id: str) -> None:
    records = [
        {"timestamp": "2026-07-20T10:00:00.000Z", "type": "session_meta",
         "payload": {"id": session_id}},
        {"timestamp": "2026-07-20T10:00:01.000Z", "type": "turn_context",
         "payload": {"model": "gpt-5"}},
    ]
    for index, total in enumerate((100, 200)):
        records.append({
            "timestamp": f"2026-07-20T10:0{index + 2}:00.000Z",
            "type": "event_msg",
            "payload": {"type": "token_count", "info": {
                "last_token_usage": {
                    "input_tokens": 60, "cached_input_tokens": 0,
                    "output_tokens": 40, "reasoning_output_tokens": 0,
                    "total_tokens": 100},
                "total_token_usage": {"total_tokens": total}}},
        })
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n"
                            for r in records))


def _d1_estate(tmp_path, monkeypatch, *, accounts="split"):
    """``accounts``: ``split`` (red, then blue before the rebuild),
    ``single`` (red throughout) or ``none`` (no auth.json at any point)."""
    from _lib_source_identity import source_root_key
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    provider_root = tmp_path / "codex-provider"
    day = provider_root / "sessions" / "2026" / "07" / "20"
    paths = {key: day / f"rollout-{index}-{key.lower()}.jsonl"
             for index, key in enumerate("RHU", 1)}
    monkeypatch.setenv("CODEX_HOME", str(provider_root))
    _d1_write(paths["R"], "sess-r")
    if accounts != "none":
        (provider_root / "auth.json").write_text(_auth_json(*_RED))
    conn = ns["open_cache_db"]()
    try:
        assert ns["sync_codex_cache"](conn).files_processed == 1
        _d1_write(paths["H"], "sess-h")
        st = paths["H"].stat()
        conn.execute(
            "INSERT INTO codex_session_files"
            "(path,size_bytes,mtime_ns,last_byte_offset,last_ingested_at,"
            " source_root_key) VALUES (?,?,?,?,?,?)",
            (str(paths["H"].resolve()), st.st_size, st.st_mtime_ns,
             st.st_size, dt.datetime.now(dt.timezone.utc).isoformat(),
             source_root_key(str(provider_root.resolve()))))
        conn.commit()
        _d1_write(paths["U"], "sess-u")
        assert conn.execute(
            "SELECT COUNT(*) FROM quota_window_snapshots").fetchone()[0] == 0
    finally:
        conn.close()
    if accounts == "split":
        (provider_root / "auth.json").write_text(_auth_json(*_BLUE))
    return ns, provider_root, paths


@pytest.fixture
def d1(tmp_path, monkeypatch):
    return _d1_estate(tmp_path, monkeypatch)


def _identity(provider_root, path) -> str:
    from _lib_source_identity import source_root_key
    return _cc().codex_file_key_for_entry_path(
        source_root_key(str(provider_root.resolve())), str(path.resolve()))


def _spellings(path) -> set:
    return {str(path), str(path.resolve())}


def _cursor_count(conn, path) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM codex_session_files WHERE path IN (?, ?)",
        (str(path), str(path.resolve()))).fetchone()[0]


def _journal_decisions_naming(identity: str) -> int:
    import _cctally_core
    journal_dir = pathlib.Path(_cctally_core.JOURNAL_DIR)
    if not journal_dir.exists():
        return 0
    count = 0
    for segment in journal_dir.rglob("*"):
        if not segment.is_file():
            continue
        for line in segment.read_text(errors="replace").splitlines():
            if '"codex_file_account"' in line and identity in line:
                count += 1
    return count


def _durable_state(ns, provider_root, paths) -> dict:
    """The replay state as committed, read through a second connection."""
    cc = _cc()
    h_id = _identity(provider_root, paths["H"])
    conn = ns["open_cache_db"]()
    try:
        record = cc.load_codex_heal_record(conn)
        payload = (None if record is None
                   else cc.load_codex_heal_payload(conn, record))
        return {
            "marker": _armed(conn),
            "record": record,
            "payload": payload,
            "blocked": conn.execute(
                "SELECT 1 FROM cache_meta WHERE key=?",
                (cc.CODEX_REPLAY_BLOCKED_KEY,)).fetchone() is not None,
            "fence_active": cc._codex_heal_fence_active(conn),
            "h_id": h_id,
            "u_id": _identity(provider_root, paths["U"]),
            "h_cursor": _cursor_count(conn, paths["H"]),
            "h_decisions": conn.execute(
                "SELECT COUNT(*) FROM codex_file_accounts "
                "WHERE file_identity=?", (h_id,)).fetchone()[0]
            + _journal_decisions_naming(h_id),
            "entries": conn.execute(
                "SELECT COUNT(*) FROM codex_session_entries").fetchone()[0],
        }
    finally:
        conn.close()


def _assert_refill_owed(state, paths, *, rehydrated, phase="units"):
    """The state an interrupted replay leaves: marker and record durable,
    the D1 set holding H but not U, and no decision for H."""
    assert state["marker"], "the replay marker did not survive the interrupt"
    record = state["record"]
    assert record is not None and record.get("mode") == "replay", record
    assert not record.get("malformed"), record
    assert record.get("phase") == phase, record
    assert record.get("rehydrated") is rehydrated, record
    payload = state["payload"]
    assert payload is not None and payload.gen == record["gen"], (
        "the record's fence and payload must carry one generation")
    assert state["h_id"] in payload.known, (
        "the pre-mechanism rollout lost its D1 protection")
    assert state["u_id"] not in payload.known
    assert _spellings(paths["U"]) & set(payload.population)
    assert state["h_decisions"] == 0


def _rows_by_file(conn) -> dict:
    rows: dict = {}
    for source_path, account_key in conn.execute(
            "SELECT source_path, account_key FROM codex_session_entries"):
        rows.setdefault(pathlib.Path(source_path).name, []).append(
            account_key)
    return rows


def _assert_completed(ns, provider_root, paths, *, r="red", u="blue"):
    """A completed walk: H unattributed, R and U on their own accounts, and
    nothing of the replay left behind."""
    keys = {"red": _expected_key(*_RED), "blue": _expected_key(*_BLUE),
            None: None}
    state = _durable_state(ns, provider_root, paths)
    conn = ns["open_cache_db"]()
    try:
        rows = _rows_by_file(conn)
    finally:
        conn.close()
    assert rows.get(paths["H"].name), "H must be re-ingested"
    assert all(key is None for key in rows[paths["H"].name]), (
        "pre-mechanism Codex spend was re-attributed to the live account "
        "after an interrupted explicit rebuild")
    assert rows.get(paths["R"].name)
    assert all(key == keys[r] for key in rows[paths["R"].name]), (
        "an attributed rollout moved off its decided account")
    assert rows.get(paths["U"].name)
    assert all(key == keys[u] for key in rows[paths["U"].name]), (
        "a rollout with no D1 evidence must take a live decision")
    assert not state["marker"]
    assert state["record"] is None and state["payload"] is None
    assert not state["blocked"]
    assert state["h_decisions"] == 0


def _sync(ns, **kwargs):
    conn = ns["open_cache_db"]()
    try:
        return ns["sync_codex_cache"](conn, **kwargs)
    finally:
        conn.close()


def _run_child(body) -> None:
    """Run ``body`` in a forked child that must die in its seam (exit 9):
    a hard crash skips every ``finally`` that commits completed steps."""
    def child():
        body()
        os._exit(0)
    proc = multiprocessing.get_context("fork").Process(target=child)
    proc.start()
    proc.join(120)
    if proc.is_alive():
        # A hung child (a fork deadlock) must not outlive the test and block
        # the worker's exit.
        proc.kill()
        proc.join()
    assert proc.exitcode == 9, proc.exitcode


def _cut_p1_caught(ns, monkeypatch, paths, *, rebuild=True):
    """After the clear's commit, before the rehydration's: it raises."""
    import _cctally_journal

    def interrupted(*_a, **_k):
        raise _Injected("P1: interrupt after the clear's commit")

    with monkeypatch.context() as patch:
        patch.setattr(_cctally_journal,
                      "rehydrate_codex_journal_families", interrupted)
        stats = _sync(ns, rebuild=rebuild)
    assert stats.deferred_reason == "attribution_rehydration", stats


def _cut_p1_crash(ns, monkeypatch, paths):
    """The same point as a hard crash in a forked child."""
    import _cctally_journal

    def body():
        _cctally_journal.rehydrate_codex_journal_families = (
            lambda *_a, **_k: os._exit(9))
        _sync(ns, rebuild=True)
    _run_child(body)


def _cut_p2(ns, monkeypatch, paths):
    """After the rehydration's commit, before the first file: the walk's
    discovery (not the preflight's) raises."""
    cc = _cc()
    real = cc._discover_codex_files_with_roots

    def discover(*, skipped=None):
        if skipped is not None:
            raise _Injected("P2: interrupt before the first file")
        return real()

    with monkeypatch.context() as patch:
        patch.setattr(cc, "_discover_codex_files_with_roots", discover)
        with pytest.raises(_Injected):
            _sync(ns, rebuild=True)


def _cut_p3(ns, monkeypatch, paths, *, rebuild=True):
    """After a non-final batch commit: one file per batch, interrupted once
    R's batch has committed, with H not yet refilled."""
    cc = _cc()

    def committed(path_str):
        if pathlib.Path(path_str).name == paths["R"].name:
            raise _Injected("P3: interrupt after R's batch commit")

    with monkeypatch.context() as patch:
        patch.setattr(cc, "CODEX_ACCOUNTING_BATCH_FILES", 1)
        with pytest.raises(_Injected):
            _sync(ns, rebuild=rebuild, _on_file_committed=committed)


_CUTS = {"P1-caught": (_cut_p1_caught, False),
         "P1-crash": (_cut_p1_crash, False),
         "P2": (_cut_p2, True),
         "P3": (_cut_p3, True)}


def _complete(ns, completion):
    if completion == "executor":
        result = _cc().run_codex_admitted_heal(wait_for_requests=False)
        assert result.outcome == "done", result
        return
    stats = _sync(ns, rebuild=completion == "rebuild")
    assert stats.deferred_reason is None, stats
    assert stats.files_failed == 0, stats


@pytest.mark.parametrize("cut,completion", [
    pytest.param("P1-caught", "ordinary", id="P1-caught-ordinary"),
    pytest.param("P1-caught", "rebuild", id="P1-caught-rebuild"),
    pytest.param("P1-caught", "executor", id="P1-caught-executor"),
    pytest.param("P1-crash", "ordinary", id="P1-crash-ordinary"),
    pytest.param("P2", "ordinary", id="P2-ordinary"),
    pytest.param("P3", "ordinary", id="P3-ordinary"),
    pytest.param("P3", "rebuild", id="P3-rebuild"),
])
def test_explicit_rebuild_interrupt_keeps_d1(d1, monkeypatch, cut,
                                             completion):
    """An explicit rebuild interrupted between its clear's commit and its
    walk's completion, then completed by an ordinary sync, another explicit
    rebuild or the admitted heal executor. Detects: pre-mechanism Codex spend
    re-attributed to the live account, or attributed spend moved off its
    account, after an interrupted ``cache-sync --rebuild`` (stored-data
    accuracy, #1051 A2/A3/A4/A6)."""
    ns, provider_root, paths = d1
    run_cut, rehydrated = _CUTS[cut]
    run_cut(ns, monkeypatch, paths)
    state = _durable_state(ns, provider_root, paths)
    _assert_refill_owed(state, paths, rehydrated=rehydrated)
    if cut == "P3":
        assert state["h_cursor"] == 0, "the P3 cut must leave H unrefilled"
    _complete(ns, completion)
    _assert_completed(ns, provider_root, paths)


def test_explicit_rebuild_clear_transaction_is_atomic(d1, monkeypatch):
    """P0: a failure inside the clear's transaction, at the new record
    write. Detects: a clear committed without its marker and record (a
    store cleared with no persisted D1 set), or a marker or record left
    over a store that was not cleared (stored-data accuracy, #1051 A4)."""
    ns, provider_root, paths = d1
    cc = _cc()

    def failing(*_a, **_k):
        raise _Injected("P0: inside the clear's transaction")

    def rows():
        conn = ns["open_cache_db"]()
        try:
            return _rows_by_file(conn)
        finally:
            conn.close()

    before = _durable_state(ns, provider_root, paths), rows()
    with monkeypatch.context() as patch:
        patch.setattr(cc, "_store_codex_heal_record", failing)
        with pytest.raises(_Injected):
            _sync(ns, rebuild=True)
    state = _durable_state(ns, provider_root, paths)
    assert (state, rows()) == before, "the clear must roll back whole"
    assert not state["marker"]
    assert state["record"] is None and state["payload"] is None


@pytest.mark.parametrize("cause", ["failed", "torn"])
def test_explicit_rebuild_blocked_keeps_d1(d1, monkeypatch, cause):
    """P4: the walk completes blocked, H's read failing or U meeting a torn
    auth.json. Detects: a blocked explicit rebuild leaving nothing armed, so
    the failed rollout's next read takes the live account, or a conversations
    pass publishing ahead of it (stored-data accuracy, #1051 A2/A7)."""
    ns, provider_root, paths = d1
    cc = _cc()
    with monkeypatch.context() as patch:
        if cause == "failed":
            real = cc._write_codex_file_batch

            def write(conn, **kwargs):
                if pathlib.Path(str(kwargs.get("path_str"))).name == \
                        paths["H"].name:
                    raise sqlite3.OperationalError("injected: H's read fails")
                return real(conn, **kwargs)

            patch.setattr(cc, "_write_codex_file_batch", write)
        else:
            patch.setattr(cc, "_resolve_codex_account_for_root",
                          lambda _root: cc._CodexRootAccount("torn", None))
        stats = _sync(ns, rebuild=True)
    if cause == "failed":
        assert stats.files_failed == 1, stats
    else:
        assert stats.files_deferred_torn == 1, stats
    state = _durable_state(ns, provider_root, paths)
    _assert_refill_owed(state, paths, rehydrated=True, phase="certify")
    assert state["blocked"]
    assert not state["fence_active"]
    if cause == "torn":
        conn = ns["open_cache_db"]()
        try:
            assert _cursor_count(conn, paths["U"]) == 0
            assert conn.execute(
                "SELECT COUNT(*) FROM codex_file_accounts "
                "WHERE file_identity=?", (state["u_id"],)).fetchone()[0] == 0
        finally:
            conn.close()
    conv = ns["open_conversations_db"]()
    try:
        assert cc.sync_codex_conversations(conv).deferred_reason in (
            "cache_replay_pending", "cache_heal_pending")
    finally:
        conv.close()
    _complete(ns, "ordinary")
    _assert_completed(ns, provider_root, paths)


@pytest.mark.parametrize("outcome", ["unwind", "crash"])
def test_explicit_rebuild_consumption_boundary(d1, monkeypatch, outcome):
    """P5: after the consumption's savepoint release, before the final W11
    commit. An exception unwinds through the finalization's commit-so-far,
    which commits the walk and the consumption together; a hard crash rolls
    both back. Detects: the protected set dropped while the walk that
    justified dropping it is lost (stored-data accuracy, #1051 A4)."""
    ns, provider_root, paths = d1
    cc = _cc()
    # The seam fires before EVERY W11 commit that carries applied files; it
    # is the walk's final commit only because the whole estate fits in one
    # batch.
    assert len(paths) < cc.CODEX_ACCOUNTING_BATCH_FILES
    if outcome == "unwind":
        def hook():
            raise _Injected("P5: before the final commit")

        with monkeypatch.context() as patch:
            patch.setattr(cc, "_CODEX_BATCH_PRECOMMIT_HOOK", hook)
            with pytest.raises(_Injected):
                _sync(ns, rebuild=True)
        _assert_completed(ns, provider_root, paths)
        return

    def body():
        cc._CODEX_BATCH_PRECOMMIT_HOOK = lambda: os._exit(9)
        _sync(ns, rebuild=True)
    _run_child(body)
    state = _durable_state(ns, provider_root, paths)
    _assert_refill_owed(state, paths, rehydrated=True)
    assert state["entries"] == 0
    _complete(ns, "ordinary")
    _assert_completed(ns, provider_root, paths)


@pytest.mark.parametrize("sequence",
                         ["ordinary-then-ordinary", "rebuild-then-rebuild"])
def test_explicit_rebuild_repeated_interrupts(d1, monkeypatch, sequence):
    """Two interrupts in a row: a refill reads the persisted D1 set, and a
    second clear unites it with a fresh capture under a new generation.
    Detects: protection lost on the second interrupt (stored-data accuracy,
    #1051 A2)."""
    ns, provider_root, paths = d1
    _cut_p1_caught(ns, monkeypatch, paths)
    first = _durable_state(ns, provider_root, paths)
    _assert_refill_owed(first, paths, rehydrated=False)
    if sequence == "ordinary-then-ordinary":
        _cut_p3(ns, monkeypatch, paths, rebuild=False)
        second = _durable_state(ns, provider_root, paths)
        _assert_refill_owed(second, paths, rehydrated=True)
    else:
        _cut_p1_caught(ns, monkeypatch, paths)
        second = _durable_state(ns, provider_root, paths)
        _assert_refill_owed(second, paths, rehydrated=False)
        assert second["record"]["gen"] != first["record"]["gen"]
    assert second["h_cursor"] == 0
    _complete(ns, "ordinary")
    _assert_completed(ns, provider_root, paths)


def test_explicit_rebuild_restores_a_missing_marker(d1, monkeypatch):
    """A valid replay record whose marker is gone (a damaged cache_meta),
    then an explicit rebuild interrupted after its clear. Detects: arming on
    ``not replay_active`` instead of marker absence, which leaves a record
    without the marker its lifecycle needs (stored-data accuracy, #1051,
    Appendix A Q3)."""
    ns, provider_root, paths = d1
    cc = _cc()
    h_id = _identity(provider_root, paths["H"])
    conn = ns["open_cache_db"]()
    try:
        seeded = cc._store_codex_heal_record(
            conn, {"mode": "replay", "phase": "units", "rehydrated": True},
            population=[str(path) for path in paths.values()], known=[h_id])
        conn.execute(
            "DELETE FROM codex_session_files WHERE path IN (?, ?)",
            (str(paths["H"]), str(paths["H"].resolve())))
        conn.commit()
        assert not _armed(conn)
    finally:
        conn.close()
    _cut_p1_caught(ns, monkeypatch, paths)
    state = _durable_state(ns, provider_root, paths)
    _assert_refill_owed(state, paths, rehydrated=False)
    assert state["record"]["gen"] != seeded["gen"]
    _complete(ns, "ordinary")
    _assert_completed(ns, provider_root, paths)


def test_refill_declines_while_due(d1, monkeypatch):
    """After an interrupted explicit rebuild, a budgeted hook tick and a
    live-tail targeted sync decline instead of ingesting. Detects: either
    caller reading the cleared store with a live decision (stored-data
    accuracy, #1051 A2)."""
    ns, provider_root, paths = d1
    _cut_p1_caught(ns, monkeypatch, paths)
    conn = ns["open_cache_db"]()
    try:
        before = conn.execute(
            "SELECT COUNT(*) FROM codex_session_entries").fetchone()[0]
        budgeted = ns["sync_codex_cache"](conn, budget_seconds=5)
        assert budgeted.deferred_reason == "replay_pending", budgeted
        assert conn.execute(
            "SELECT COUNT(*) FROM codex_session_entries").fetchone()[0] \
            == before
        targeted = ns["sync_codex_cache"](
            conn, only_paths={str(paths["U"])})
        assert targeted.deferred_reason == "replay_pending", targeted
    finally:
        conn.close()
    _complete(ns, "ordinary")
    _assert_completed(ns, provider_root, paths)


def test_conversations_defer_behind_an_interrupted_rebuild(d1, monkeypatch):
    """Detects: a conversations pass publishing from the cleared thread rows
    of an interrupted explicit rebuild, which stamps a permanent
    "(unassigned)" project (stored-data accuracy, #1051 A7)."""
    ns, _provider_root, paths = d1
    cc = _cc()
    _cut_p1_caught(ns, monkeypatch, paths)
    conv = ns["open_conversations_db"]()
    try:
        assert cc._cache_side_replay_pending(conv)
        assert cc.sync_codex_conversations(conv).deferred_reason in (
            "cache_replay_pending", "cache_heal_pending")
    finally:
        conv.close()


def test_uninterrupted_explicit_rebuild_leaves_nothing_armed(d1):
    """Detects: a protected set left behind after a completed explicit
    rebuild, which would hold every later sync in replay mode and defer
    Codex transcript ingest (#1051 A4)."""
    ns, provider_root, paths = d1
    stats = _sync(ns, rebuild=True)
    assert not stats.files_failed and not stats.files_deferred_torn, stats
    _assert_completed(ns, provider_root, paths)


@pytest.mark.parametrize("accounts", ["no-auth", "single-account"])
def test_explicit_rebuild_controls(tmp_path, monkeypatch, accounts):
    """Installs with no auth.json, or with one account throughout. Detects:
    the persisted D1 set over-protecting a live first-ingest decision or
    changing what an unaffected install stores (#1051 A5)."""
    ns, provider_root, paths = _d1_estate(
        tmp_path, monkeypatch,
        accounts="none" if accounts == "no-auth" else "single")
    _cut_p1_caught(ns, monkeypatch, paths)
    _complete(ns, "ordinary")
    if accounts == "no-auth":
        _assert_completed(ns, provider_root, paths, r=None, u=None)
    else:
        _assert_completed(ns, provider_root, paths, r="red", u="red")
