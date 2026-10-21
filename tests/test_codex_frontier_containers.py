"""#968 spec 5.9 / T7 frontier convergence: both planners and the kernel."""
from __future__ import annotations

import os
import pathlib
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
for extra in (REPO_ROOT / "bin", REPO_ROOT / "tests"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from conftest import (  # noqa: E402
    load_script, redirect_paths)
from _codex_zstd_fixtures import (  # noqa: E402
    compress_in_place, conversation_records, write_rollout, zstd_bytes)

import _lib_codex_conversation_watch as watch  # noqa: E402
import _lib_ingest_frontier as frontier  # noqa: E402


def _sid(i):
    return f"0199aaaa-bbbb-7ccc-8ddd-eeeeffff04{i:02d}"


def _name(i):
    return f"rollout-2026-07-20T10-{i:02d}-00-{_sid(i)}.jsonl"


# ── the pure pieces ─────────────────────────────────────────────────────────

def test_a_pending_container_is_its_own_verdict_before_identity():
    observed = frontier.ObservedFile(0, 9, 99, container_pending=True)
    assert frontier.classify_recent_active_path(
        committed_size=500, committed_offset=500, committed_complete=True,
        committed_device=1, committed_inode=2, observed=observed,
    ) == "container_pending"


def test_the_plain_observation_is_unchanged():
    observed = frontier.ObservedFile(600, 1, 2)
    assert frontier.classify_recent_active_path(
        committed_size=500, committed_offset=500, committed_complete=True,
        committed_device=1, committed_inode=2, observed=observed) == "append"


@pytest.mark.parametrize("observed,committed,expected", [
    (("plain", 10), ("plain", 9), True),
    (("plain", 9), ("plain", 9), False),
    (("plain", 8), ("plain", 9), False),
    (("zstd", 5, 7, 1, 3), ("zstd", 5, 7, 1, 3), False),
    (("zstd", 6, 8, 1, 3), ("zstd", 5, 7, 1, 3), True),
    (("zstd", 5, 7, 2, 3), ("zstd", 5, 7, 1, 3), True),   # a remount (device)
    (("zstd", 5, 7, 1, 3), ("plain", 900), True),
    (("plain", 10), 9, True),          # legacy int committed size
    (10, 9, True),
    (None, ("plain", 9), False),
    (("plain", 1), None, True),
])
def test_codex_sig_advanced(observed, committed, expected):
    assert watch.codex_sig_advanced(observed, committed) is expected


def test_the_kernel_registers_each_logical_rollout_once():
    tree = {"/root": [
        ("a.jsonl.zst", "/root/a.jsonl.zst", False, False),
        ("b.jsonl", "/root/b.jsonl", False, False),
        ("b.jsonl.zst", "/root/b.jsonl.zst", False, False),
        ("c.txt", "/root/c.txt", False, False),
    ]}
    kernel = watch.CodexChildFrontier(
        "/root", scandir_fn=lambda d: tree.get(d, []),
        dir_mtime_fn=lambda d: 1, file_size_fn=lambda p: ("plain", 1))
    surfaced = kernel.cycle(known_paths=set(), committed_sizes={})
    assert surfaced == {"/root/a.jsonl", "/root/b.jsonl"}
    assert kernel.pending_candidates() == {"/root/a.jsonl", "/root/b.jsonl"}


def test_a_compressed_only_pending_candidate_is_observed_not_dropped():
    sigs = {"/root/a.jsonl": ("zstd", 5, 7, 1, 3)}
    tree = {"/root": [("a.jsonl.zst", "/root/a.jsonl.zst", False, False)]}
    kernel = watch.CodexChildFrontier(
        "/root", scandir_fn=lambda d: tree.get(d, []),
        dir_mtime_fn=lambda d: 1, file_size_fn=lambda p: sigs.get(p))
    kernel.cycle(known_paths=set(), committed_sizes={})
    surfaced = kernel.cycle(known_paths=set(),
                            committed_sizes={"/root/a.jsonl": ("zstd", 5, 7, 1, 3)})
    assert "/root/a.jsonl" in kernel.pending_candidates()
    assert surfaced == set()          # unchanged container: not rescheduled
    sigs["/root/a.jsonl"] = ("zstd", 6, 8, 1, 3)
    surfaced = kernel.cycle(known_paths=set(),
                            committed_sizes={"/root/a.jsonl": ("zstd", 5, 7, 1, 3)})
    assert surfaced == {"/root/a.jsonl"}


def test_the_default_file_sig_reads_the_selected_container(tmp_path):
    plain = tmp_path / "x.jsonl"
    plain.write_bytes(b'{"a":1}\n')
    assert watch.default_file_sig(str(plain)) == ("plain", 8)
    twin = tmp_path / "x.jsonl.zst"
    twin.write_bytes(zstd_bytes(b'{"a":1}\n'))
    plain.unlink()
    sig = watch.default_file_sig(str(plain))
    st = twin.stat()
    assert sig == ("zstd", st.st_size, st.st_mtime_ns, st.st_dev, st.st_ino)


def test_the_provider_certificate_reads_the_declared_reset_deferral_field():
    """Review S-P2a. The predicate reads the field through `getattr` because
    it is shared with the Claude `IngestStats`, which has no Codex fields
    (`bin/_cctally_tui.py:9068-9071`); this pins that the Codex field is a
    DECLARED dataclass field the walk sets, so the getattr default can never
    silently answer for it."""
    load_script()
    cache_mod = sys.modules["_cctally_cache"]
    deferred = cache_mod.CodexIngestStats(full_walk_complete=True,
                                          files_deferred_reset=1)
    assert not frontier.provider_sync_certifiable("full", deferred)
    clean = cache_mod.CodexIngestStats(full_walk_complete=True)
    assert frontier.provider_sync_certifiable("full", clean)
    # the shared predicate still accepts the Claude stats type
    assert frontier.provider_sync_certifiable("targeted", cache_mod.IngestStats())


# ── the real stores ─────────────────────────────────────────────────────────

@pytest.fixture
def estate(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(
        ns, monkeypatch, tmp_path / "data")
    provider = tmp_path / "codex"
    day = provider / "sessions" / "2026" / "07" / "20"
    day.mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(provider))
    monkeypatch.setattr(sys.modules["_cctally_update"], "_spawn_detached",
                        lambda command: True)
    a, b = day / _name(1), day / _name(2)
    write_rollout(a, conversation_records(3, session_id=_sid(1), label="a"))
    write_rollout(b, conversation_records(2, session_id=_sid(2), label="b"))
    cache_mod = sys.modules["_cctally_cache"]
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
    finally:
        conn.close()
    conv = ns["open_conversations_db"]()
    try:
        ns["sync_codex_conversations"](conv)
    finally:
        conv.close()
    roots = tuple(r.walk_root for r in cache_mod._codex_provider_roots())
    return ns, cache_mod, a, b, roots, tmp_path


def _tick(ns, state, roots):
    conn = ns["open_cache_db"]()
    try:
        plan = state.plan_provider("codex", conn, roots=roots)
        stats = None
        if plan.mode == "full":
            stats = ns["sync_codex_cache"](conn)
        elif plan.mode == "targeted":
            stats = ns["sync_codex_cache"](conn, only_paths=set(plan.paths))
        if frontier.provider_sync_certifiable(plan.mode, stats):
            if plan.mode == "full":
                state.seed_provider("codex", conn, roots=roots)
            else:
                state.commit_provider(plan, conn, roots=roots)
        return plan
    finally:
        conn.close()


def test_the_accounting_planner_converges_across_a_compression(estate):
    ns, cache_mod, a, _b, roots, tmp_path = estate
    state = frontier.DashboardIngestFrontier(tmp_path / "frontier")
    conn = ns["open_cache_db"]()
    try:
        assert state.seed_provider("codex", conn, roots=roots), state.last_seed_failure
    finally:
        conn.close()
    assert _tick(ns, state, roots).mode == "caught_up"
    compress_in_place(a)
    modes = [_tick(ns, state, roots).mode for _ in range(5)]
    # one walk detects the transition (not certifiable), one certifies the held
    # state, then the estate is caught up: no perpetual full walk
    assert modes.count("full") <= 2 and modes[-1] == "caught_up", modes
    assert cache_mod.run_codex_container_verification_drain(
        wait_for_requests=False).adopted >= 1
    assert [_tick(ns, state, roots).mode for _ in range(2)] == ["caught_up"] * 2
    # no missed transition: a changed compressed container, same pathname
    twin = a.with_name(a.name + ".zst")
    longer = b"".join(
        __import__("json").dumps(r, separators=(",", ":")).encode() + b"\n"
        for r in conversation_records(5, session_id=_sid(1), label="a"))
    twin.write_bytes(zstd_bytes(longer))
    plan = _tick(ns, state, roots)
    assert plan.mode in ("targeted", "full") and plan.reason != "cursor_gap"
    conn = ns["open_cache_db"]()
    try:
        state_row = conn.execute(
            "SELECT container_state FROM codex_session_files WHERE path=?",
            (str(a.resolve()),)).fetchone()[0]
    finally:
        conn.close()
    assert state_row == "pending"


def test_both_planners_treat_a_compressed_target_as_container_work(estate):
    ns, cache_mod, a, _b, roots, _tmp = estate
    key = str(a.resolve())
    compress_in_place(a)
    conn = ns["open_cache_db"]()
    try:
        # pending (not yet verified): never a cursor gap
        assert frontier._target_has_cursor_gap(conn, "codex", key) is False
    finally:
        conn.close()
    conv = ns["open_conversations_db"]()
    try:
        assert frontier._conversation_target_risk(conv, "codex", key) is None
    finally:
        conv.close()
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
    finally:
        conn.close()
    conv = ns["open_conversations_db"]()
    try:
        ns["sync_codex_conversations"](conv)
    finally:
        conv.close()
    cache_mod.run_codex_container_verification_drain(wait_for_requests=False)
    conn = ns["open_cache_db"]()
    try:   # accepted: the decoded cursor equals the decoded observation
        assert frontier._target_has_cursor_gap(conn, "codex", key) is False
    finally:
        conn.close()
    conv = ns["open_conversations_db"]()
    try:
        assert frontier._conversation_target_risk(conv, "codex", key) is None
    finally:
        conv.close()
    a.with_name(a.name + ".zst").unlink()
    conn = ns["open_cache_db"]()
    try:   # truly gone: still a gap
        assert frontier._target_has_cursor_gap(conn, "codex", key) is True
    finally:
        conn.close()


def test_ticket_targets_accept_either_spelling_and_refuse_an_escape(estate):
    _ns, _cache_mod, a, b, roots, tmp_path = estate
    compress_in_place(a)
    twin = str(a) + ".zst"
    paths, reason = frontier._ticket_target_paths(
        "codex", [("codex", twin, None, None, None)], roots)
    assert reason is None and paths == frozenset({str(a)})
    outside = tmp_path / "outside"
    outside.mkdir()
    escaped = outside / "escaped.jsonl.zst"
    escaped.write_bytes(zstd_bytes(b'{"a":1}\n'))
    link = b.parent / ("rollout-2026-07-20T10-09-00-" + _sid(9) + ".jsonl.zst")
    os.symlink(escaped, link)
    _paths, reason = frontier._ticket_target_paths(
        "codex", [("codex", str(link), None, None, None)], roots)
    assert reason == "target_outside_scope"
    # Claude tickets keep the plain-only rule
    assert frontier._target_in_roots(twin, roots) is False


def _conversation_tick(ns, state, roots):
    conn = ns["open_conversations_db"]()
    try:
        plan = state.plan_provider("codex", conn, roots=roots)
        stats = None
        if plan.mode == "full":
            stats = ns["sync_codex_conversations"](conn)
        elif plan.mode == "targeted":
            stats = ns["sync_codex_conversations"](conn, only_paths=set(plan.paths))
        if frontier.conversation_sync_certifiable(
                plan.mode, stats, expected_paths=len(plan.paths)):
            state.commit_provider(plan, conn, roots=roots)
        return plan
    finally:
        conn.close()


def test_the_conversation_planner_converges_across_a_compression(estate):
    ns, cache_mod, a, _b, roots, tmp_path = estate
    state = frontier.ConversationSyncFrontier(tmp_path / "conversation-frontier")
    conn = ns["open_conversations_db"]()
    try:
        assert state.seed_provider("codex", conn, roots=roots), state.last_seed_failure
    finally:
        conn.close()
    assert _conversation_tick(ns, state, roots).mode == "caught_up"
    compress_in_place(a)
    modes = [_conversation_tick(ns, state, roots).mode for _ in range(5)]
    assert modes.count("full") <= 2 and modes[-1] == "caught_up", modes
    assert cache_mod.run_codex_container_verification_drain(
        wait_for_requests=False).adopted >= 1
    assert [_conversation_tick(ns, state, roots).mode for _ in range(2)] == ["caught_up"] * 2


def test_observation_never_resolves_a_decoder_or_opens_a_stream(tmp_path, monkeypatch):
    import _lib_codex_rollout_container as rc

    def forbidden(*args, **kwargs):
        raise AssertionError("observation opened a stream or resolved a decoder")

    plain = tmp_path / "x.jsonl"
    twin = plain.with_name(plain.name + ".zst")
    twin.write_bytes(b"container-only-stat-evidence")
    st = twin.stat()
    fp = rc.ContainerFingerprint(rc.ZSTD, str(twin), st.st_size,
                                  st.st_mtime_ns, st.st_dev, st.st_ino)
    monkeypatch.setattr(rc, "resolve_zstd_backend", forbidden)
    monkeypatch.setattr(rc, "open_decoded", forbidden)
    accepted = frontier.ContainerEvidence(
        "zstd", st.st_size, 900, st.st_mtime_ns, st.st_dev, st.st_ino,
        "accepted", None)
    assert frontier._observe_logical(str(plain), accepted).size == 900
    pending = accepted._replace(state="pending", pending_fingerprint=fp.to_json())
    observed = frontier._observe_logical(str(plain), pending)
    assert observed.container_pending and observed.container_known
    unknown = pending._replace(pending_fingerprint=fp._replace(device=st.st_dev+1).to_json())
    assert not frontier._observe_logical(str(plain), unknown).container_known
    remounted = accepted._replace(device=st.st_dev+1)
    assert frontier._observe_logical(str(plain), remounted).container_pending
    assert watch.default_file_sig(str(plain)) == fp.watch_sig()
    assert watch.committed_watch_sig("zstd", 900, st.st_size,
                                      st.st_mtime_ns, st.st_dev, st.st_ino) == fp.watch_sig()


def test_container_evidence_uses_only_indexed_nonempty_candidates():
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE codex_session_files(path TEXT PRIMARY KEY, "
                 "container_kind TEXT, container_size_bytes INTEGER, "
                 "decoded_size_bytes INTEGER, mtime_ns INTEGER, device_id INTEGER, "
                 "inode INTEGER, container_state TEXT, pending_container_fingerprint TEXT)")
    statements = []
    conn.set_trace_callback(statements.append)
    assert frontier._container_evidence_for(conn, "codex_session_files", []) == {}
    assert statements == []
    assert frontier._container_evidence_for(conn, "codex_session_files", ["/x.jsonl"]) == {}
    assert len(statements) == 1
    plan = conn.execute("EXPLAIN QUERY PLAN " + statements[0]).fetchall()
    assert any("SEARCH" in row[3] and "INDEX" in row[3] for row in plan)
    assert not any(any(term in row[3] for term in (
        "USE TEMP B-TREE", "AUTOMATIC", "MATERIALIZE")) for row in plan)
    conn.close()


@pytest.mark.parametrize("held_state", ["pending", "unverified"])
def test_known_held_container_certifies_only_the_observed_census(estate, held_state):
    """A known hold completes discovery, without claiming verified content."""
    import json

    from _cctally_quota import codex_physical_mutation_seq

    ns, cache_mod, a, _b, _roots, _tmp_path = estate
    compress_in_place(a)
    conn = ns["open_cache_db"]()
    try:
        first = ns["sync_codex_cache"](conn)
        assert first.container_pending == 1
        assert not first.full_walk_complete
        assert not frontier.provider_sync_certifiable("full", first)
        if held_state == "unverified":
            conn.execute("UPDATE codex_session_files SET container_state=? WHERE path=?",
                         (held_state, str(a)))
            conn.commit()
        cursor_sql = (
            "SELECT size_bytes,last_byte_offset,last_ingested_at,container_state,"
            "pending_container_fingerprint FROM codex_session_files WHERE path=?")
        cursor = tuple(conn.execute(cursor_sql, (str(a),)).fetchone())
        accounting = conn.execute("SELECT * FROM codex_session_entries ORDER BY rowid").fetchall()
        sequence = codex_physical_mutation_seq(conn)
        held = ns["sync_codex_cache"](conn)
        reason = "verification-" + held_state
        assert held.container_pending == 0
        assert held.container_skipped == {reason: 1}
        assert held.rows_changed == held.files_processed == 0
        assert tuple(conn.execute(cursor_sql, (str(a),)).fetchone()) == cursor
        assert conn.execute("SELECT * FROM codex_session_entries ORDER BY rowid").fetchall() == accounting
        assert codex_physical_mutation_seq(conn) == sequence
        status = json.loads(conn.execute(
            "SELECT value FROM cache_meta WHERE key=?",
            (cache_mod.CODEX_CONTAINER_STATUS_KEY,)).fetchone()[0])
        assert status["skipped"][reason] == 1
        assert held.full_walk_complete
        assert frontier.provider_sync_certifiable("full", held)
        # The certificate proves an observed known hold, never debt settlement.
        conn.execute(
            "INSERT INTO codex_prune_debt(source_path,source_root_key,recorded_at_utc) "
            "SELECT path,source_root_key,'2026-07-20T10:00:00Z' "
            "FROM codex_session_files WHERE path=?", (str(a),))
        conn.commit()
        debt = conn.execute("SELECT * FROM codex_prune_debt").fetchall()
        indebted = ns["sync_codex_cache"](conn)
        assert indebted.prune_debt_pending == 1
        assert not indebted.full_walk_complete
        assert not frontier.provider_sync_certifiable("full", indebted)
        assert conn.execute("SELECT * FROM codex_prune_debt").fetchall() == debt
        assert tuple(conn.execute(cursor_sql, (str(a),)).fetchone()) == cursor
        assert codex_physical_mutation_seq(conn) == sequence
    finally:
        conn.close()


_R01_CACHE_FAMILIES = (
    "codex_session_entries", "quota_window_snapshots", "codex_file_accounts",
    "codex_file_incarnations", "codex_conversation_threads", "codex_session_files",
)
_R01_CONVERSATION_FAMILIES = (
    "codex_conversation_events", "codex_conversation_messages",
    "codex_conversation_rollups", "codex_conversation_source_files",
)


def _conversation_certificate_families(ns):
    from _codex_zstd_fixtures import snapshot_tables
    from _cctally_quota import codex_physical_mutation_seq

    out = {}
    for label, opener, tables in (
            ("cache", "open_cache_db", _R01_CACHE_FAMILIES),
            ("conversation", "open_conversations_db", _R01_CONVERSATION_FAMILIES)):
        conn = ns[opener]()
        try:
            out[label] = snapshot_tables(conn, tables)
            if label == "cache":
                out["physical_mutation_seq"] = codex_physical_mutation_seq(conn)
        finally:
            conn.close()
    return out


@pytest.fixture
def conversation_certificate_estate(estate):
    """A recognized plain keeper and nonempty retained accounting/transcripts."""
    import json

    ns, _cache_mod, a, _b, _roots, _tmp_path = estate
    quota = {
        "timestamp": "2026-07-20T10:50:00.000Z", "type": "event_msg",
        "payload": {"type": "token_count", "info": {"rate_limits": {
            "limit_id": "codex", "limit_name": "codex",
            "primary": {"used_percent": 42, "window_minutes": 10080,
                        "resets_at": 1785150000}}}}}
    with a.open("ab") as fh:
        fh.write(json.dumps(quota, separators=(",", ":")).encode() + b"\n")
    for opener, sync in (("open_cache_db", "sync_codex_cache"),
                         ("open_conversations_db", "sync_codex_conversations")):
        conn = ns[opener]()
        try:
            ns[sync](conn)
        finally:
            conn.close()
    before = _conversation_certificate_families(ns)
    for label in ("cache", "conversation"):
        assert all(before[label].values()), (label, before[label])
    assert before["physical_mutation_seq"] > 0
    print("nonempty certificate oracles:", {
        label: {table: len(rows) for table, rows in before[label].items()}
        for label in ("cache", "conversation")})
    return estate


@pytest.mark.parametrize("mode", ["full", "targeted"])
@pytest.mark.parametrize("failure", ["no-decoder", "corrupt-stream", "corrupt-after-validation"])
def test_new_unread_conversation_container_never_certifies(
        conversation_certificate_estate, monkeypatch, mode, failure):
    import _lib_codex_rollout_container as rc

    ns, _cache_mod, a, _b, _roots, _tmp_path = conversation_certificate_estate
    fresh = a.parent / _name(3)
    write_rollout(fresh, conversation_records(2, session_id=_sid(3), label="fresh"))
    twin = compress_in_place(fresh)
    blob = twin.read_bytes()
    corruptions = []
    if failure == "no-decoder":
        monkeypatch.setattr(rc, "resolve_zstd_backend", lambda: rc.UNAVAILABLE)
    elif failure == "corrupt-stream":
        twin.write_bytes(blob[:len(blob) // 2])
    else:
        # #968 Task11: the actual decoded-read failure belongs to phase 2.
        # Account first, capture the nonempty oracle below, then arm the
        # one-shot mutation only after publication planning finishes.
        cc = sys.modules["_cctally_cache"]
        cache = ns["open_cache_db"]()
        try:
            ns["sync_codex_cache"](cache)
        finally:
            cache.close()
        validate = rc.validate_full_stream
        plan_units = cc._plan_codex_publication_units
        phase_two = {"armed": False}

        def planned(conn, work):
            units = plan_units(conn, work)
            assert any(str(fresh) in unit.caps for unit in units)
            phase_two["armed"] = True
            return units

        def corrupt_after_validation(container, **kwargs):
            result = validate(container, **kwargs)
            if (phase_two["armed"] and container.path == twin
                    and not corruptions):
                assert result.ok
                twin.write_bytes(blob[:len(blob) // 2])
                corruptions.append(str(container.path))
            return result

        monkeypatch.setattr(cc, "_plan_codex_publication_units", planned)
        monkeypatch.setattr(rc, "validate_full_stream", corrupt_after_validation)
    before = _conversation_certificate_families(ns)
    conv = ns["open_conversations_db"]()
    try:
        kwargs = {} if mode == "full" else {"only_paths": {str(fresh)}}
        stats = ns["sync_codex_conversations"](conv, **kwargs)
        assert not stats.prune_refused and stats.deferred_reason is None
        assert stats.files_total == (3 if mode == "full" else 1)
        assert stats.files_failed == stats.container_pending == 0
        reason = "decoder-unavailable" if failure == "no-decoder" else "decode-error"
        assert stats.container_skipped == {reason: 1}
        assert conv.execute(
            "SELECT 1 FROM codex_conversation_source_files WHERE path=?",
            (str(fresh),)).fetchone() is None
        assert conv.execute(
            "SELECT 1 FROM codex_conversation_events WHERE source_path=?",
            (str(fresh),)).fetchone() is None
        assert conv.execute(
            "SELECT 1 FROM codex_conversation_messages WHERE source_path=?",
            (str(fresh),)).fetchone() is None
        assert _conversation_certificate_families(ns) == before
        if failure == "corrupt-after-validation":
            assert corruptions == [str(twin)]
        assert not frontier.conversation_sync_certifiable(
            mode, stats, expected_paths=stats.files_total)
        assert not frontier.provider_sync_certifiable(mode, stats)
        assert stats.container_reads_held == 1
    finally:
        conv.close()


@pytest.mark.parametrize("held_state", ["pending", "unverified"])
def test_known_held_conversation_container_certifies_its_preserved_census(
        conversation_certificate_estate, held_state):
    import json

    ns, cache_mod, a, _b, _roots, _tmp_path = conversation_certificate_estate
    compress_in_place(a)
    conv = ns["open_conversations_db"]()
    try:
        first = ns["sync_codex_conversations"](conv)
        assert first.container_pending == 1
        assert not frontier.conversation_sync_certifiable("full", first)
        assert not frontier.conversation_sync_certifiable(
            "targeted", first, expected_paths=first.files_total)
        if held_state == "unverified":
            conv.execute(
                "UPDATE codex_conversation_source_files SET container_state=? WHERE path=?",
                (held_state, str(a)))
            conv.commit()
        before = _conversation_certificate_families(ns)
        reason = "verification-" + held_state
        for mode in ("full", "targeted"):
            kwargs = {} if mode == "full" else {"only_paths": {str(a)}}
            held = ns["sync_codex_conversations"](conv, **kwargs)
            assert held.container_pending == held.container_reads_held == 0
            assert held.container_skipped == {reason: 1}
            assert held.rows_changed == held.files_processed == 0
            assert frontier.conversation_sync_certifiable(
                mode, held, expected_paths=held.files_total)
            assert _conversation_certificate_families(ns) == before
            status = json.loads(conv.execute(
                "SELECT value FROM cache_meta WHERE key=?",
                (cache_mod.CODEX_CONTAINER_STATUS_KEY,)).fetchone()[0])
            assert status["skipped"][reason] == 1
        conv.execute(
            "INSERT INTO codex_conversation_prune_debt"
            "(source_path,source_root_key,recorded_at_utc) "
            "SELECT path,source_root_key,'2026-07-20T10:00:00Z' "
            "FROM codex_conversation_source_files WHERE path=?", (str(a),))
        conv.commit()
        debt = conv.execute("SELECT * FROM codex_conversation_prune_debt").fetchall()
        assert len(debt) == 1
        indebted = ns["sync_codex_conversations"](conv)
        assert indebted.prune_debt_pending == 1
        assert not frontier.conversation_sync_certifiable("full", indebted)
        assert conv.execute("SELECT * FROM codex_conversation_prune_debt").fetchall() == debt
        assert _conversation_certificate_families(ns) == before
    finally:
        conv.close()



def _assert_no_new_conversation_source(conn, path):
    for table, column in (("codex_conversation_source_files", "path"),
                          ("codex_conversation_events", "source_path"),
                          ("codex_conversation_messages", "source_path")):
        assert conn.execute(f"SELECT 1 FROM {table} WHERE {column}=?",
                            (str(path),)).fetchone() is None


def _certification_decisions(mode, stats):
    return (frontier.conversation_sync_certifiable(
                mode, stats, expected_paths=stats.files_total),
            frontier.provider_sync_certifiable(mode, stats),
            stats.targeted_clean)


def _account_then_publish(ns, fresh, mode):
    import _lib_codex_conversation_query as query
    cache = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](cache)
        key = cache.execute("SELECT last_conversation_key FROM codex_session_files "
                            "WHERE path=?", (str(fresh),)).fetchone()[0]
    finally:
        cache.close()
    conv = ns["open_conversations_db"]()
    try:
        kwargs = {} if mode == "full" else {"only_paths": {str(fresh)}}
        stats = ns["sync_codex_conversations"](conv, **kwargs)
        assert not stats.barrier_retry
        publication = conv.execute(
            "SELECT published_revision,legacy_uncertified "
            "FROM codex_conversation_publications WHERE conversation_key=?",
            (key,)).fetchone()
        assert publication is not None and publication[0] > 0 and publication[1] == 0
        row = next(r for r in query.list_codex_conversations(
            conv, effective_speed="standard", limit=50)["rows"]
            if r["conversation_key"] == key)
        assert row["count"] == 4 and row["title"] == "Prompt fresh0"
        assert query.get_codex_conversation(
            conv, key, effective_speed="standard", limit=0)["status"] == "ok"
        assert frontier._codex_publication_backlog(conv) == (False, frozenset())
        assert frontier.conversation_sync_certifiable(
            mode, stats, expected_paths=stats.files_total)
    finally:
        conv.close()


@pytest.mark.parametrize("mode", ["full", "targeted"])
def test_before_accounting_publication_deferral_never_certifies(
        conversation_certificate_estate, mode):
    """968-T11-STOP-01: no membership/repair unit covers this readable source."""
    ns, _cc, a, _b, _roots, _tmp = conversation_certificate_estate
    fresh = a.parent / _name(3)
    write_rollout(fresh, conversation_records(2, session_id=_sid(3), label="fresh"))
    compress_in_place(fresh)
    before = _conversation_certificate_families(ns)
    conv = ns["open_conversations_db"]()
    try:
        kwargs = {} if mode == "full" else {"only_paths": {str(fresh)}}
        stats = ns["sync_codex_conversations"](conv, **kwargs)
        assert stats.barrier_files_deferred == 1
        assert stats.files_failed == stats.container_pending == stats.container_reads_held == 0
        assert stats.container_skipped == {}
        _assert_no_new_conversation_source(conv, fresh)
        assert _conversation_certificate_families(ns) == before
        decisions = _certification_decisions(mode, stats)
        print("PRE_ACCOUNTING_DEFERRAL", mode, decisions, vars(stats))
        assert decisions == (False, False, False)
        assert stats.barrier_retry
    finally:
        conv.close()
    _account_then_publish(ns, fresh, mode)


@pytest.mark.parametrize("mode", ["full", "targeted"])
def test_between_phase_unreadability_keeps_seal_debt_and_retries(
        conversation_certificate_estate, monkeypatch, mode):
    """968-T11-STOP-02: a real phase-2 decoder refusal retains the backlog."""
    import _lib_codex_publication_barrier as barrier
    import _lib_codex_rollout_container as rc
    ns, cc, a, _b, _roots, _tmp = conversation_certificate_estate
    fresh = a.parent / _name(3)
    write_rollout(fresh, conversation_records(2, session_id=_sid(3), label="fresh"))
    compress_in_place(fresh)
    cache = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](cache)
        seal = cache.execute("SELECT value FROM cache_meta WHERE key=?",
                             (barrier.SEAL_SEQ_KEY,)).fetchone()[0]
    finally:
        cache.close()
    before = _conversation_certificate_families(ns)
    real_plan, real_resolve = cc._plan_codex_publication_units, rc.resolve_zstd_backend
    planned_units = []

    def plan_then_lose_decoder(conn, work):
        units = real_plan(conn, work)
        assert any(str(fresh) in unit.caps for unit in units)
        planned_units.extend(units)
        monkeypatch.setattr(rc, "resolve_zstd_backend", lambda: rc.UNAVAILABLE)
        return units

    monkeypatch.setattr(cc, "_plan_codex_publication_units", plan_then_lose_decoder)
    conv = ns["open_conversations_db"]()
    try:
        kwargs = {} if mode == "full" else {"only_paths": {str(fresh)}}
        stats = ns["sync_codex_conversations"](conv, **kwargs)
        assert planned_units
        _assert_no_new_conversation_source(conv, fresh)
        assert _conversation_certificate_families(ns) == before
        ack = conv.execute("SELECT value FROM cache_meta WHERE key=?",
                           (barrier.PUBLISHED_SEQ_KEY,)).fetchone()
        decisions = _certification_decisions(mode, stats)
        print("BETWEEN_PHASE_UNREAD", mode, decisions, vars(stats), "seal", seal, "ack", ack)
        assert stats.container_skipped == {"decoder-unavailable": 1}
        assert stats.container_reads_held == 1
        assert stats.barrier_retry and stats.barrier_keys_published == 0
        assert decisions == (False, False, False)
        assert ack is None or ack[0] != seal
        assert frontier._codex_publication_backlog(conv) == (True, frozenset({str(fresh)}))
    finally:
        conv.close()
    monkeypatch.setattr(cc, "_plan_codex_publication_units", real_plan)
    monkeypatch.setattr(rc, "resolve_zstd_backend", real_resolve)
    _account_then_publish(ns, fresh, mode)


def test_barrier_retry_alone_refuses_every_completion_predicate(estate):
    ns, cc, _a, _b, _roots, _tmp = estate
    stats = cc.CodexIngestStats(files_total=1, barrier_files_deferred=1,
                                barrier_retry=True, full_walk_complete=True)
    assert not stats.targeted_clean
    assert not frontier.provider_sync_certifiable("full", stats)
    assert not frontier.provider_sync_certifiable("targeted", stats)
    assert not frontier.conversation_sync_certifiable("full", stats)
    assert not frontier.conversation_sync_certifiable("targeted", stats, expected_paths=1)
