"""Claude ingest attribution (#341 Task 3 slice C, spec §1/§2).

Observe-and-stamp for the Claude entry cache: ``sync_cache`` resolves the active
Claude identity ONCE per sync from ``~/.claude.json`` (stable-read, mtime-cached)
and stamps ``account_key`` onto newly-ingested ``session_entries`` rows (and the
``session_files`` last-observed diagnostic). A torn read (mid-rewrite) DEFERS the
whole Claude tail-ingest this cycle WITHOUT advancing any per-file cursor; a
missing/api-key ``~/.claude.json`` stamps NULL (``NULL ≡ unattributed`` on the
read path — byte-stable for the no-identity test corpus).

The Claude account_observe is journaled by ``record-usage`` (not ``sync_cache``),
so ``sync_cache`` owns the cache-row stamp only — no journal double-stamp.
"""
from __future__ import annotations

import json
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BIN_DIR = REPO_ROOT / "bin"
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))
if str(REPO_ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "tests"))

from conftest import load_script, redirect_paths  # noqa: E402
import _lib_accounts as accts  # noqa: E402

RED_UUID = "11111111-1111-4111-8111-111111111111"
BLUE_UUID = "22222222-2222-4222-8222-222222222222"


def _claude_json(uuid: str, email: str) -> str:
    return json.dumps({
        "oauthAccount": {
            "accountUuid": uuid,
            "emailAddress": email,
            "organizationUuid": "org-1",
            "plan": "max",
        },
    })


def _expected_key(uuid: str) -> str:
    return accts.account_key("claude", uuid)


def _write_session_jsonl(path: pathlib.Path, *, session_id: str, msg_id: str,
                         out_tokens: int = 100) -> None:
    """One assistant message that yields exactly one session_entries row."""
    path.parent.mkdir(parents=True, exist_ok=True)
    obj = {
        "type": "assistant",
        "sessionId": session_id,
        "cwd": "/home/u/proj",
        "timestamp": "2026-05-22T17:04:00.000Z",
        "requestId": "req-" + msg_id,
        "message": {
            "id": msg_id,
            "model": "claude-opus-4-7",
            "usage": {
                "input_tokens": 10,
                "output_tokens": out_tokens,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        },
    }
    path.write_text(json.dumps(obj) + "\n", encoding="utf-8")


def _reset_identity_cache():
    import _cctally_core as core
    core._ACTIVE_CLAUDE_ACCOUNT_CACHE.clear()
    core._ACTIVE_CLAUDE_ACCOUNT_CACHE.update({"sig": None, "identity": None})


def _stub_discovery(monkeypatch, files):
    import _cctally_cache as cc
    monkeypatch.setattr(cc, "_iter_claude_jsonl_files", lambda: list(files))


def test_identified_stamps_real_account_key(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    _reset_identity_cache()
    import _cctally_core as core
    core.CLAUDE_JSON_PATH.write_text(_claude_json(RED_UUID, "red@x.com"))
    jsonl = tmp_path / "projects" / "proj" / "s1.jsonl"
    _write_session_jsonl(jsonl, session_id="sess-red", msg_id="m-red")
    _stub_discovery(monkeypatch, [jsonl])
    expected = _expected_key(RED_UUID)

    cache = ns["open_cache_db"]()
    try:
        stats = ns["sync_cache"](cache)
        assert stats.files_processed == 1
        rows = cache.execute(
            "SELECT account_key FROM session_entries").fetchall()
        assert rows, "expected at least one accounting row"
        assert all(r[0] == expected for r in rows), (
            f"session_entries should stamp {expected!r}, got {rows!r}")
        frow = cache.execute(
            "SELECT account_key FROM session_files WHERE path=?",
            (str(jsonl),)).fetchone()
        assert frow[0] == expected, (
            f"session_files last-observed stamp {expected!r}, got {frow!r}")
    finally:
        cache.close()


def test_missing_claude_json_stamps_null(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    _reset_identity_cache()
    # No ~/.claude.json -> stably-absent -> NULL (NULL ≡ unattributed). Byte-
    # stable for the no-identity corpus.
    jsonl = tmp_path / "projects" / "proj" / "s1.jsonl"
    _write_session_jsonl(jsonl, session_id="sess-x", msg_id="m-x")
    _stub_discovery(monkeypatch, [jsonl])

    cache = ns["open_cache_db"]()
    try:
        stats = ns["sync_cache"](cache)
        assert stats.files_processed == 1
        rows = cache.execute(
            "SELECT account_key FROM session_entries").fetchall()
        assert rows
        assert all(r[0] is None for r in rows), (
            f"stably-absent identity must stamp NULL, got {rows!r}")
    finally:
        cache.close()


def test_torn_claude_json_defers_without_advancing_cursor(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    _reset_identity_cache()
    import _cctally_core as core
    core.CLAUDE_JSON_PATH.write_text(_claude_json(RED_UUID, "red@x.com"))
    jsonl = tmp_path / "projects" / "proj" / "s1.jsonl"
    _write_session_jsonl(jsonl, session_id="sess-red", msg_id="m-red")
    _stub_discovery(monkeypatch, [jsonl])

    real = core._resolve_active_claude_identity
    calls = {"n": 0}

    def flip():
        calls["n"] += 1
        if calls["n"] == 1:
            return {"account_key": accts.UNATTRIBUTED, "natural_id": None,
                    "email": None, "plan_type": None, "status": "torn"}
        return real()

    monkeypatch.setattr(core, "_resolve_active_claude_identity", flip)

    cache = ns["open_cache_db"]()
    try:
        stats = ns["sync_cache"](cache)
        # Torn defers the whole Claude tail-ingest: nothing ingested.
        assert stats.files_deferred_torn == 1
        assert stats.files_processed == 0
        assert cache.execute(
            "SELECT COUNT(*) FROM session_entries").fetchone() == (0,)
        # Cursor NOT advanced: no session_files row for this file.
        assert cache.execute(
            "SELECT COUNT(*) FROM session_files WHERE path=?",
            (str(jsonl),)).fetchone() == (0,)

        # Next sync (identity now stably readable) ingests the deferred file
        # and stamps the real account.
        _reset_identity_cache()
        stats2 = ns["sync_cache"](cache)
        assert stats2.files_processed == 1
        stamped = cache.execute(
            "SELECT DISTINCT account_key FROM session_entries").fetchall()
        assert stamped == [(_expected_key(RED_UUID),)], (
            f"deferred file must ingest+stamp on the next sync, got {stamped!r}")
    finally:
        cache.close()


# --------------------------------------------------------------------------
# #944 T3: journal-backed attribution glue (_cctally_claude_attribution)
# --------------------------------------------------------------------------

import pytest  # noqa: E402

LOC = "0123456789abcdef0123456789abcdef"


@pytest.fixture
def glue(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    _reset_identity_cache()
    import _cctally_claude_attribution as G
    conn = ns["open_cache_db"]()
    yield ns, G, conn
    conn.close()


def _bound_obs(name, at, *, account=True):
    import _lib_account_binding as ab
    import _lib_journal as lj
    key = accts.account_key("claude", name)
    binding = ab.capture({"status": ab.STATUS_IDENTIFIED, "account_key": key,
                          "natural_id": name, "email": None,
                          "plan_type": None})
    payload = {"weekly_percent": 1.0, "resets_at": 1, "source": "api",
               "captured_at": at, "account_binding": binding.document()}
    return lj.make_obs(at, "record-usage", "claude", payload,
                       account=key if account else None)


def _decision_op(items, *, account, basis="fallback", reason="pre_timeline"):
    import _lib_journal as lj
    return lj.make_claude_cost_account(
        basis=basis, reason=reason if basis == "fallback" else None,
        account_key=account, decided_at="2026-10-09T10:00:00.000000Z",
        journal_hw=["observations-2026-10.jsonl", 0], closing_capture=None,
        evidence=[], items=items)


def _append(*records):
    import _cctally_journal as jr
    return jr.append_records(list(records))


def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_replay_is_idempotent_and_authoritative_replay_recounts(glue):
    """An incremental replay applies each journal line once; an authoritative
    one clears and rebuilds both tables from the journal, and a conflicting
    later decision is counted once either way. Detects: duplicated timeline
    evidence, or a conflict count that grows on every rebuild."""
    _ns, G, conn = glue
    a, b = accts.account_key("claude", "A"), accts.account_key("claude", "B")
    _append(_bound_obs("A", "2026-10-09T10:00:00Z"),
            _decision_op([["k1", "m1", "r1", "2026-10-09T09:00:00Z"]],
                         account=a),
            _decision_op([["k1", "m1", "r1", "2026-10-09T09:00:00Z"]],
                         account=b))
    assert G.rehydrate_claude_attribution(conn, authoritative=False)
    conn.commit()
    assert _count(conn, "claude_account_timeline") == 1
    assert G.lookup_decision(conn, G.keyed_identity("m1", "r1")).account_key == a
    assert G.decision_conflicts(conn) == 1
    assert G.rehydrate_claude_attribution(conn, authoritative=False)
    assert _count(conn, "claude_account_timeline") == 1
    assert G.decision_conflicts(conn) == 1
    assert G.rehydrate_claude_attribution(conn, authoritative=True)
    conn.commit()
    assert _count(conn, "claude_account_timeline") == 1
    assert _count(conn, "claude_cost_decisions") == 1
    assert G.decision_conflicts(conn) == 1


def test_replay_admits_only_bound_claude_usage_observations(glue):
    """An observation whose top-level stamp contradicts its binding, or one
    without a parseable capture time, is never timeline evidence. Detects:
    an unbound reading deciding an account."""
    _ns, G, conn = glue
    bad_stamp = _bound_obs("A", "2026-10-09T10:00:00Z", account=False)
    no_time = _bound_obs("A", "2026-10-09T10:00:01Z")
    no_time["payload"]["captured_at"] = "soon"
    import _lib_journal as lj
    no_time = lj.make_obs(no_time["at"], "record-usage", "claude",
                          no_time["payload"], account=no_time["account"])
    _append(bad_stamp, no_time, _bound_obs("B", "2026-10-09T10:00:02Z"))
    assert G.rehydrate_claude_attribution(conn, authoritative=True)
    rows = conn.execute(
        "SELECT captured_at_utc, account_key FROM claude_account_timeline"
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        ("2026-10-09T10:00:02.000000Z", accts.account_key("claude", "B"))]


def _timeline(conn, *rows):
    for i, (at, precision, account) in enumerate(rows):
        conn.execute(
            "INSERT INTO claude_account_timeline (captured_at_utc,"
            " precision_us, account_key, journal_segment, journal_offset)"
            " VALUES (?,?,?,?,?)", (at, precision, account, "seg", i))


def test_neighbours_group_ambiguous_and_overlapping_instants(glue):
    """Two accounts at one instant make that instant ambiguous; a
    second-precision capture covering the record overlaps it and never
    stands in for Pb or Sa. Detects: a contradictory instant read as
    agreement."""
    _ns, G, conn = glue
    import _lib_claude_attribution as K
    _timeline(conn,
              ("2026-10-09T10:00:00.000000Z", 1, "a"),
              ("2026-10-09T10:00:00.000000Z", 1, "b"),
              ("2026-10-09T10:00:05.000000Z", 1_000_000, "a"),
              ("2026-10-09T10:00:09.000000Z", 1_000_000, "a"))
    rec = K.record_interval("2026-10-09T10:00:05.250Z")
    found = G.timeline_neighbours(conn, rec)
    assert found.before.ambiguous and found.before.accounts == {"a", "b"}
    assert [o.accounts for o in found.overlapping] == [{"a"}]
    assert found.after.accounts == {"a"}
    assert K.us_from_iso("2026-10-09T10:00:09Z") == found.after.lo_us
    evidence = G.neighbour_evidence(conn, rec, found)
    assert {e[2] for e in evidence} == {0, 1, 2, 3}


def test_lookup_prefers_keyed_then_physical_then_legacy(glue):
    """Detects: a record inheriting a decision of the wrong identity form, or
    an unknown-split legacy decision winning over an exact one."""
    _ns, G, conn = glue
    import _lib_claude_attribution as K
    projection = ["2026-10-09T10:00:00+00:00", "m", 1, 2, 3, 4, 1, 2, None,
                  0.5]
    unknown = projection[:6] + [None, None] + projection[8:]
    raw = b'{"x":1}'
    _append(
        _decision_op([["l1", LOC, 7, K.projection_sha256(unknown), unknown]],
                     account="u" * 32, basis="preserved_history"),
        _decision_op([["l1", LOC, 7, K.projection_sha256(projection),
                       projection]],
                     account="e" * 32, basis="preserved_history"))
    assert G.rehydrate_claude_attribution(conn, authoritative=True)
    path = "/p/s.jsonl"
    monkey_loc = K.loc_for(path)
    ident = G.record_identity(msg_id=None, req_id=None, source_path=path,
                              byte_offset=7, record_bytes=raw,
                              projection=projection)
    assert ident.form == "p1"
    # Different location: nothing.
    assert G.lookup_decision(conn, ident) is None
    ident = G.RecordIdentity(form="p1", key=K.physical_key(LOC, 7, raw),
                             loc=LOC, offset=7, digest=K.record_sha256(raw),
                             projection=projection)
    assert G.lookup_decision(conn, ident).account_key == "e" * 32
    _append(_decision_op([["p1", LOC, 7, K.record_sha256(raw), None]],
                         account="p" * 32))
    assert G.rehydrate_claude_attribution(conn, authoritative=False)
    assert G.lookup_decision(conn, ident).account_key == "p" * 32
    assert G.lookup_decision(conn, G.keyed_identity("m", "r")) is None
    assert monkey_loc != LOC


def _insert_row(conn, *, path, offset, msg_id, req_id, account, ts=None,
                out=20):
    import _cctally_cache as cc
    ts = ts or "2026-10-09T10:00:00+00:00"
    conn.execute(cc.SESSION_ENTRY_UPSERT_SQL, (
        path, offset, ts, "claude-sonnet-4-5", msg_id, req_id, 10, out, 0, 0,
        None, None, None, None, None, 1, ts, account))
    return conn.execute(
        "SELECT id FROM session_entries WHERE source_path=? AND line_offset=?",
        (path, offset)).fetchone()[0]


def _seq(conn):
    row = conn.execute("SELECT value FROM cache_meta WHERE key="
                       "'session_entries_mutation_seq'").fetchone()
    return None if row is None else int(row[0])


def test_preserve_aligns_decided_rows_and_exports_the_rest(glue):
    """A row whose identity already holds a decision takes that account (the
    mutation counter advances); every other row's current stamp is exported
    as preserved history and linked without moving the counter. Detects: a
    wipe losing a row's account, or a cache reader missing a changed row."""
    _ns, G, conn = glue
    a, b = accts.account_key("claude", "A"), accts.account_key("claude", "B")
    _append(_decision_op([["k1", "m1", "r1", None]], account=b))
    hw = G.rehydrate_claude_attribution(conn, authoritative=True)
    assert hw
    decided = _insert_row(conn, path="/p/a.jsonl", offset=0, msg_id="m1",
                          req_id="r1", account=a)
    keyed = _insert_row(conn, path="/p/a.jsonl", offset=100, msg_id="m2",
                        req_id="r2", account=a)
    unkeyed = _insert_row(conn, path="/p/a.jsonl", offset=200, msg_id=None,
                          req_id=None, account=None)
    conn.commit()
    seq0 = _seq(conn)
    result = G.preserve_unlinked_rows(conn, journal_hw=hw)
    conn.commit()
    assert (result.saved, result.aligned, result.exported,
            result.remaining) == (3, 1, 2, False)
    rows = {r[0]: tuple(r[1:]) for r in conn.execute(
        "SELECT e.id, e.account_key, d.basis, d.form FROM session_entries e"
        " JOIN claude_cost_decisions d"
        " ON d.decision_key = e.attribution_decision_key")}
    assert rows[decided] == (b, "fallback", "k1")
    assert rows[keyed] == (a, "preserved_history", "k1")
    assert rows[unkeyed] == (None, "preserved_history", "l1")
    assert _seq(conn) == (seq0 or 0) + 1
    # A second pass finds nothing unlinked and appends nothing.
    again = G.preserve_unlinked_rows(conn, journal_hw=hw)
    assert (again.saved, again.exported) == (0, 0)
    # The exported decisions survive an authoritative replay.
    assert G.rehydrate_claude_attribution(conn, authoritative=True)
    assert _count(conn, "claude_cost_decisions") == 3


def test_preserve_is_chunked_and_resumable(glue, monkeypatch):
    """Detects: a preserve pass that cannot bound its work per commit."""
    _ns, G, conn = glue
    monkeypatch.setattr(G, "_PRESERVE_CHUNK", 2)
    ids = [_insert_row(conn, path="/p/c.jsonl", offset=i * 10,
                       msg_id=f"m{i}", req_id=f"r{i}", account=None)
           for i in range(5)]
    hw = G.rehydrate_claude_attribution(conn, authoritative=True)
    first = G.preserve_unlinked_rows(conn, journal_hw=hw, limit=3)
    assert (first.saved, first.remaining, first.last_id) == (3, True, ids[2])
    rest = G.preserve_unlinked_rows(conn, journal_hw=hw,
                                    after_id=first.last_id)
    assert (rest.saved, rest.remaining) == (2, False)
    assert conn.execute("SELECT COUNT(*) FROM session_entries WHERE "
                        "attribution_decision_key IS NULL").fetchone()[0] == 0


def test_append_splits_batches_under_the_line_budget(glue, monkeypatch):
    """Every decision lands on a journal line that names it, each line under
    the budget. Detects: a batch over the journal line limit, or a decision
    row pointing at the wrong journal line."""
    _ns, G, conn = glue
    import _cctally_journal as jr
    import _lib_claude_attribution as K
    import _lib_journal as lj
    monkeypatch.setattr(G, "_BATCH_LINE_BUDGET", 900)
    decisions = [(G.keyed_identity(f"msg-{i:03d}", f"req-{i:03d}"),
                  K.Decision("a" * 32, K.BASIS_FALLBACK, "pre_timeline",
                             False), "2026-10-09T10:00:00Z")
                 for i in range(20)]
    stored = G.append_decisions(conn, decisions, journal_hw=G.safe_high_water(),
                                closing_capture=None, evidence=[])
    assert [s.key for s in stored] == [d[0].key for d in decisions]
    segments = {s.journal_pos for s in stored}
    assert len(segments) > 1
    for s in stored:
        seg, off = s.journal_pos
        with open(jr._cctally_core.JOURNAL_DIR / seg, "rb") as fh:
            fh.seek(off)
            line = fh.readline()
        assert len(line) <= 900
        rec = lj.decode_line(line)
        keys = {K.keyed_key(i[1], i[2]) for i in rec["payload"]["items"]}
        assert s.key in keys


def _greedy_batches_oracle(G, groups, *, decided_at, journal_hw,
                           closing_capture):
    """The reference: the original greedy batch builder, which rebuilt and
    re-encoded the whole growing batch for every member it tried."""
    import _lib_claude_attribution as K
    import _lib_journal as lj
    out = []
    for (basis, reason, account, uses_capture), members in groups.items():
        capture = G._capture_doc(closing_capture) if uses_capture else None

        def build(chunk):
            chunk_evidence = []
            seen = set()
            for _identity, _item, item_evidence in chunk:
                for entry in item_evidence:
                    marker = tuple(entry)
                    if marker not in seen:
                        seen.add(marker)
                        chunk_evidence.append(list(entry))
            return lj.make_claude_cost_account(
                basis=basis, reason=reason, account_key=account,
                decided_at=decided_at, journal_hw=journal_hw,
                closing_capture=capture,
                evidence=(chunk_evidence
                          if basis == K.BASIS_AGREEMENT else []),
                items=[item for _identity, item, _e in chunk])

        chunk = []
        for member in members:
            chunk.append(member)
            if len(chunk) > 1 and G._encoded_len(build(chunk)) > (
                    G._BATCH_LINE_BUDGET):
                chunk.pop()
                out.append((build(chunk), chunk))
                chunk = [member]
        if chunk:
            out.append((build(chunk), chunk))
    return out


def _oracle_groups():
    """Every item form and basis the append path batches: keyed, physical and
    legacy items; fallback reasons; agreements whose members' evidence
    overlaps (and one carrying none); non-ASCII strings; and one legacy member
    larger than a 900-byte budget on its own."""
    import _lib_claude_attribution as K
    digest = "f" * 64

    def legacy(i, model):
        projection = [f"2026-10-09T10:{i % 60:02d}:00Z", model, i, 5, 0, 0,
                      0, 0, "standard", 0.25 * i]
        return ["l1", LOC, i * 7, K.projection_sha256(projection),
                projection]

    def ev(n, account="acct-ä"):
        return (f"2026-10-09T09:{n % 60:02d}:00.000000Z", account, n)

    groups = {}
    groups[(K.BASIS_FALLBACK, "pre_timeline", "a" * 32, False)] = [
        (f"k{i}", ["k1", f"msg-é-{i}", f"req-{i}",
                   f"2026-10-09T10:{i % 60:02d}:00Z"], ())
        for i in range(60)]
    groups[(K.BASIS_FALLBACK, "missing_timestamp", None, False)] = [
        (f"p{i}", ["p1", LOC, i * 100, digest, None], ())
        for i in range(25)]
    groups[(K.BASIS_PRESERVED, None, "b" * 32, False)] = (
        [(f"l{i}", legacy(i, "claude-ü-model"), ()) for i in range(30)]
        + [("big", legacy(99, "m" * 1200), ())]
        + [(f"pk{i}", ["k1", f"m-{i}", f"r-{i}"], ()) for i in range(10)])
    groups[(K.BASIS_AGREEMENT, None, "c" * 32, True)] = [
        (f"a{i}", ["k1", f"am-{i}", f"ar-{i}", "2026-10-09T10:00:00Z"],
         (ev(i), ev(i + 1), ev(i)) if i % 4 else ())
        for i in range(40)]
    groups[(K.BASIS_AGREEMENT, None, None, False)] = [
        (f"q{i}", ["p1", LOC, i, digest, "2026-10-09T10:00:00Z"],
         (ev(i % 3, "日本"),))
        for i in range(20)]
    return groups


@pytest.mark.parametrize("budget", [900, 2_500, None])
def test_batches_are_identical_to_the_greedy_reference(glue, monkeypatch,
                                                       budget):
    """The linear batch builder emits byte-identical lines in the same
    partition as the original greedy builder, including when its own size
    arithmetic is wrong (the backstop re-splits with the exact rule).
    Detects: a decision journaled on a different line, or a line whose bytes
    (items, evidence order, content id) differ from what the rule
    specifies."""
    _ns, G, _conn = glue
    import _lib_claude_attribution as K
    import _lib_journal as lj
    if budget is not None:
        monkeypatch.setattr(G, "_BATCH_LINE_BUDGET", budget)
    kwargs = dict(decided_at="2026-10-09T10:00:00.000000Z",
                  journal_hw=("observations-2026-10.jsonl", 123),
                  closing_capture=K.ClosingCapture("c" * 32,
                                                   1_760_000_000_000_000))

    def lines(batches):
        return [(lj.encode_line(lj.prepare_record(record)),
                 [member[0] for member in members])
                for record, members in batches]

    expected = lines(_greedy_batches_oracle(G, _oracle_groups(), **kwargs))
    fallbacks = []
    real_greedy = G._greedy_batches

    def greedy(members, build):
        fallbacks.append(len(members))
        return real_greedy(members, build)
    monkeypatch.setattr(G, "_greedy_batches", greedy)
    assert lines(G._build_batches(_oracle_groups(), evidence=[],
                                  **kwargs)) == expected
    assert fallbacks == []  # the linear path itself was exact
    if budget == 900:
        assert any(len(line) > 900 for line, _m in expected)  # the lone big
        assert len(expected) > len(_oracle_groups())
    # A wrong size prediction must still produce the exact greedy output.
    monkeypatch.setattr(G, "_json_len", lambda _value: 0)
    assert lines(G._build_batches(_oracle_groups(), evidence=[],
                                  **kwargs)) == expected
    assert fallbacks


def test_append_validates_each_batch_once(glue, monkeypatch):
    """Batch building is linear: each emitted line is built and validated
    once, each item once. Detects: the quadratic rebuild that re-validated
    the growing batch per member -- a 50,000-row migration-050 export chunk
    spent ~23 s there (17.7 M item validations), so every open while 050 was
    pending blocked the statusline for 12-16 s."""
    _ns, G, conn = glue
    import _cctally_journal as jr
    import _lib_claude_attribution as K
    import _lib_journal as lj
    calls = {"batch": 0, "item": 0}
    real_batch = lj.validate_claude_cost_account
    real_item = lj._validate_claude_cost_item

    def batch(payload):
        calls["batch"] += 1
        return real_batch(payload)

    def item(value, basis):
        calls["item"] += 1
        return real_item(value, basis)
    monkeypatch.setattr(lj, "validate_claude_cost_account", batch)
    monkeypatch.setattr(lj, "_validate_claude_cost_item", item)
    appended = []
    real_append = jr.append_records

    def append(records, **kw):
        appended.extend(records)
        return real_append(records, **kw)
    monkeypatch.setattr(jr, "append_records", append)
    decisions = [(G.keyed_identity(f"msg-{i:05d}", f"req-{i:05d}"),
                  K.Decision("a" * 32, K.BASIS_FALLBACK, "pre_timeline",
                             False), "2026-10-09T10:00:00Z")
                 for i in range(3_000)]
    G.append_decisions(conn, decisions, journal_hw=G.safe_high_water(),
                       closing_capture=None, evidence=[])
    assert len(appended) > 1
    assert calls["batch"] == len(appended)
    assert calls["item"] == len(decisions)


def test_append_failure_raises_and_inserts_nothing(glue, monkeypatch):
    """Detects: a decision used by the cache although it never reached the
    journal (it would be lost on the next rebuild)."""
    _ns, G, conn = glue
    import _cctally_journal as jr
    import _lib_claude_attribution as K

    def boom(records, **_kw):
        raise OSError("read-only file system")
    monkeypatch.setattr(jr, "append_records", boom)
    with pytest.raises(G.AttributionJournalUnavailable):
        G.append_decisions(
            conn, [(G.keyed_identity("m", "r"),
                    K.Decision(None, K.BASIS_FALLBACK, "pre_timeline", False),
                    None)],
            journal_hw=("observations-2026-10.jsonl", 0),
            closing_capture=None, evidence=[])
    assert _count(conn, "claude_cost_decisions") == 0


# --------------------------------------------------------------------------
# #944 T4: sync_cache integration (spec 5.5-5.6; spec tests 3, 4; AC2, AC3)
# --------------------------------------------------------------------------

import datetime as _dt  # noqa: E402
import os  # noqa: E402
import time as _time  # noqa: E402
from types import SimpleNamespace  # noqa: E402

MODEL = "claude-sonnet-4-5-20250929"


@pytest.fixture
def ing(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    _reset_identity_cache()
    ns["open_db"]().close()
    import _cctally_cache as cc
    import _cctally_journal as jr
    jr.ensure_account_bound_cutover()
    root = tmp_path / "projects" / "-home-u-proj"
    root.mkdir(parents=True)
    order = {"reverse": False}

    def discover():
        files = sorted(root.glob("*.jsonl"), key=str)
        return list(reversed(files)) if order["reverse"] else files
    monkeypatch.setattr(cc, "_iter_claude_jsonl_files", discover)
    return SimpleNamespace(ns=ns, root=root, mp=monkeypatch, order=order)


def _epoch(text):
    return _dt.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def _clock(env, at):
    moment = _epoch(at)
    env.mp.setattr(_time, "time", lambda: float(moment))


def _login(name):
    import _cctally_core as core
    core.CLAUDE_JSON_PATH.write_text(json.dumps({"oauthAccount": {
        "accountUuid": name, "emailAddress": f"{name}@example.test"}}))
    _reset_identity_cache()
    return accts.account_key("claude", name)


def _line(env, at, tag, *, file="s1", out=500, newline=True, ids=True):
    obj = {"type": "assistant", "timestamp": at, "sessionId": file,
           "cwd": "/home/u/proj",
           "message": {"model": MODEL, "usage": {
               "input_tokens": 1000, "output_tokens": out,
               "cache_creation_input_tokens": 0,
               "cache_read_input_tokens": 0}}}
    if ids:
        obj["requestId"] = f"r-{tag}"
        obj["message"]["id"] = f"m-{tag}"
    with (env.root / f"{file}.jsonl").open("a") as fh:
        fh.write(json.dumps(obj) + ("\n" if newline else ""))


def _obs(name, at, **kw):
    _append(_bound_obs(name, at, **kw))


def _sync(env, **kw):
    conn = env.ns["open_cache_db"]()
    try:
        return env.ns["sync_cache"](conn, **kw)
    finally:
        conn.close()


def _rows(env):
    conn = env.ns["open_cache_db"]()
    try:
        return {r[0]: tuple(r[1:]) for r in conn.execute(
            "SELECT e.msg_id, e.account_key, d.basis, d.reason,"
            " e.output_tokens FROM session_entries e LEFT JOIN"
            " claude_cost_decisions d ON d.decision_key ="
            " e.attribution_decision_key")}
    finally:
        conn.close()


def _decision_ops(msg_id=None):
    """Every journaled claude_cost_account op (optionally naming msg_id)."""
    import _cctally_core as core
    import _lib_journal as lj
    out = []
    for seg in sorted(core.JOURNAL_DIR.glob("*.jsonl")):
        for raw in seg.read_bytes().splitlines(keepends=True):
            rec = lj.decode_line(raw)
            if not rec or rec.get("t") != "op":
                continue
            payload = rec.get("payload") or {}
            if payload.get("kind") != "claude_cost_account":
                continue
            if msg_id is None or any(
                    i[0] == "k1" and i[1] == msg_id for i in payload["items"]):
                out.append(payload)
    return out


def _journal_size():
    import _cctally_core as core
    return sum(p.stat().st_size for p in core.JOURNAL_DIR.glob("*.jsonl"))


@pytest.mark.parametrize("ingest_as,expected", [
    ("A", ("A", "timeline_agreement", None)),
    ("B", ("B", "fallback", "switch_window")),
])
def test_prompt_ingest_closes_with_the_capture(ing, ingest_as, expected):
    """With no observation after the record, the sync's own closing capture
    serves as Sa: agreeing with Pb it decides that account (and is journaled
    with the decision); disagreeing it falls back. Detects: a prompt ingest
    left undecided or decided without its closing evidence."""
    _obs("A", "2026-10-09T10:00:00Z")
    _line(ing, "2026-10-09T10:01:00Z", "x")
    key = _login(ingest_as)
    _clock(ing, "2026-10-09T10:02:00Z")
    _sync(ing)
    account, basis, reason = expected
    assert _rows(ing)["m-x"][:3] == (accts.account_key("claude", account),
                                     basis, reason)
    (op,) = _decision_ops("m-x")
    if basis == "timeline_agreement":
        assert op["closing_capture"]["account_key"] == key
        assert op["evidence"]
    else:
        assert "closing_capture" not in op


def _after_freeze(ing, action):
    """Run ``action`` once, right after the sync froze its sources (inside
    the replay that precedes the walk)."""
    import _cctally_claude_attribution as G
    real = G.rehydrate_claude_attribution
    fired = []

    def wrapper(conn, **kw):
        if not fired:
            fired.append(True)
            action()
        return real(conn, **kw)
    ing.mp.setattr(G, "rehydrate_claude_attribution", wrapper)


def test_bytes_written_after_the_stat_wait_for_the_next_sync(ing):
    """Detects: a record ingested past the frozen prefix, i.e. decided with a
    closing capture taken before it was written."""
    _login("A")
    _clock(ing, "2026-10-09T10:05:00Z")
    _line(ing, "2026-10-09T10:01:00Z", "first")
    size = (ing.root / "s1.jsonl").stat().st_size
    _after_freeze(ing, lambda: _line(ing, "2026-10-09T10:02:00Z", "late"))
    _sync(ing)
    assert set(_rows(ing)) == {"m-first"}
    conn = ing.ns["open_cache_db"]()
    try:
        assert tuple(conn.execute(
            "SELECT size_bytes, last_byte_offset FROM session_files"
        ).fetchone()) == (size, size)
    finally:
        conn.close()
    _sync(ing)
    assert set(_rows(ing)) == {"m-first", "m-late"}


def test_a_partial_final_record_waits(ing):
    """Detects: a half-written record decided (and its cursor advanced)."""
    _login("A")
    _clock(ing, "2026-10-09T10:05:00Z")
    _line(ing, "2026-10-09T10:01:00Z", "whole")
    _line(ing, "2026-10-09T10:02:00Z", "half", newline=False)
    _sync(ing)
    assert set(_rows(ing)) == {"m-whole"}
    with (ing.root / "s1.jsonl").open("a") as fh:
        fh.write("\n")
    _sync(ing)
    assert set(_rows(ing)) == {"m-whole", "m-half"}


def test_a_descriptor_replaced_between_stat_and_open_is_skipped(ing):
    """Detects: bytes of a different file than the one frozen being walked
    under the frozen size and cursor."""
    _login("A")
    _clock(ing, "2026-10-09T10:05:00Z")
    _line(ing, "2026-10-09T10:01:00Z", "old")
    path = ing.root / "s1.jsonl"

    def replace():
        tmp = ing.root / "s1.tmp"
        tmp.write_text(path.read_text().replace("m-old", "m-new"))
        os.replace(tmp, path)
    _after_freeze(ing, replace)
    stats = _sync(ing)
    assert stats.files_skipped_replaced == 1 and stats.files_failed == 0
    assert _rows(ing) == {}
    _sync(ing)
    assert set(_rows(ing)) == {"m-new"}


def test_late_backdated_evidence_changes_no_decision(ing):
    """Detects: a durable decision re-made from evidence that arrived after
    it (every rebuild would then disagree with the first ingest)."""
    a = _login("A")
    _clock(ing, "2026-10-09T10:05:00Z")
    _line(ing, "2026-10-09T10:01:00Z", "x")
    _sync(ing)
    assert _rows(ing)["m-x"][:3] == (a, "fallback", "pre_timeline")
    _obs("B", "2026-10-09T10:00:00Z")
    _obs("B", "2026-10-09T10:02:00Z")
    _line(ing, "2026-10-09T10:04:00Z", "y")
    _sync(ing)
    _sync(ing, rebuild=True)
    assert _rows(ing)["m-x"][:3] == (a, "fallback", "pre_timeline")
    assert len(_decision_ops("m-x")) == 1


@pytest.mark.parametrize("kind", ["legacy-writer", "unbound"])
def test_excluded_and_unbound_observations_are_not_evidence(ing, kind):
    """Detects: a previous-release (excluded) or unbound reading deciding a
    record's account."""
    import _lib_journal as lj

    def bad(at):
        rec = _bound_obs("B", at)
        if kind == "legacy-writer":
            # The previous release's exact bytes: unstamped, appended past
            # the cutover chokepoint (which would otherwise stamp it).
            import _outcome_quarantine_trace as trace
            trace.raw_append([lj.unstamped_record(rec)])
            return
        payload = dict(rec["payload"])
        payload.pop("account_binding")
        _append(lj.make_obs(rec["at"], rec["src"], rec["provider"],
                            payload, account=rec["account"]))
    bad("2026-10-09T10:00:00Z")
    _line(ing, "2026-10-09T10:01:00Z", "x")
    bad("2026-10-09T10:02:00Z")
    a = _login("A")
    _clock(ing, "2026-10-09T10:05:00Z")
    _sync(ing)
    assert _rows(ing)["m-x"][:3] == (a, "fallback", "pre_timeline")


@pytest.mark.parametrize("reverse", [False, True])
def test_a_resumed_copy_keeps_the_logical_decision(ing, reverse):
    """The same keyed record in two files (a resumed session) is one logical
    record with one decision, whatever the discovery order, and a later
    copy ingested under another account keeps it. Detects: a resumed copy
    re-deciding (and flipping) the account of an existing message."""
    ing.order["reverse"] = reverse
    _obs("A", "2026-10-09T10:00:00Z")
    _line(ing, "2026-10-09T10:01:00Z", "x", file="s1")
    _line(ing, "2026-10-09T10:01:00Z", "x", file="s2")
    _obs("A", "2026-10-09T10:02:00Z")
    _login("B")
    a = accts.account_key("claude", "A")
    _clock(ing, "2026-10-09T10:05:00Z")
    _sync(ing)
    assert _rows(ing)["m-x"][:2] == (a, "timeline_agreement")
    assert len(_decision_ops("m-x")) == 1
    _line(ing, "2026-10-09T10:01:00Z", "x", file="s3")
    _sync(ing)
    assert _rows(ing)["m-x"][:2] == (a, "timeline_agreement")
    assert len(_decision_ops("m-x")) == 1


def test_a_streaming_upgrade_keeps_the_account_and_link(ing):
    """Compatibility: the dedup winner update raises the tokens in place and
    keeps the decided account and link (and advances the mutation counter,
    as every in-place upsert does)."""
    _obs("A", "2026-10-09T10:00:00Z")
    _line(ing, "2026-10-09T10:01:00Z", "x", out=100)
    _obs("A", "2026-10-09T10:02:00Z")
    a = accts.account_key("claude", "A")
    _login("B")
    _clock(ing, "2026-10-09T10:05:00Z")
    _sync(ing)
    _line(ing, "2026-10-09T10:01:00Z", "x", out=900)
    _sync(ing)
    assert _rows(ing)["m-x"] == (a, "timeline_agreement", None, 900)
    assert len(_decision_ops("m-x")) == 1


def test_an_unchanged_tick_does_no_attribution_work(ing):
    """Detects: a hook tick that replays, appends or writes attribution rows
    (and so moves the dashboard's mutation signal) with nothing to ingest."""
    import _cctally_claude_attribution as G
    _login("A")
    _clock(ing, "2026-10-09T10:05:00Z")
    _line(ing, "2026-10-09T10:01:00Z", "x")
    _sync(ing)
    size = _journal_size()
    conn = ing.ns["open_cache_db"]()
    try:
        seq = conn.execute("SELECT value FROM cache_meta WHERE key="
                           "'session_entries_mutation_seq'").fetchone()
    finally:
        conn.close()
    calls = []
    ing.mp.setattr(G, "rehydrate_claude_attribution",
                   lambda *a, **k: calls.append(1) or True)
    ing.mp.setattr(G, "append_decisions",
                   lambda *a, **k: calls.append(2) or [])
    stats = _sync(ing)
    assert stats.files_skipped_unchanged == 1 and calls == []
    assert _journal_size() == size
    conn = ing.ns["open_cache_db"]()
    try:
        assert conn.execute("SELECT value FROM cache_meta WHERE key="
                            "'session_entries_mutation_seq'").fetchone() == seq
    finally:
        conn.close()


def _unwritable_journal(ing):
    import _cctally_journal as jr

    def boom(*_a, **_k):
        raise OSError(30, "Read-only file system")
    ing.mp.setattr(jr, "append_records", boom)


def _cli(ing, *argv):
    args = ing.ns["build_parser"]().parse_args(["cache-sync", *argv])
    return args.func(args)


def test_an_unwritable_journal_defers_a_plain_sync(ing, capsys):
    """Detects: cost rows committed with decisions that never reached the
    journal (lost on the next rebuild), or a silent CLI deferral."""
    import _cctally_claude_attribution as G
    import _cctally_journal as jr
    real = jr.append_records
    _login("A")
    _clock(ing, "2026-10-09T10:05:00Z")
    _line(ing, "2026-10-09T10:01:00Z", "x")
    _unwritable_journal(ing)
    capsys.readouterr()
    assert _cli(ing, "--source", "claude") == 1
    err = capsys.readouterr().err
    assert ("[cache-sync] claude sync deferred: the cctally journal could not"
            " be written. Check that the cctally journal directory is"
            " writable.") in err
    assert _rows(ing) == {}
    # The hook's path: sync_cache itself prints nothing about it and records
    # the deferral.
    stats = _sync(ing)
    assert stats.deferred_reason == "attribution_rehydration"
    assert "journal" not in capsys.readouterr().err
    conn = ing.ns["open_cache_db"]()
    try:
        assert G.deferral_recorded(conn)
    finally:
        conn.close()
    # The next sync that appends clears it.
    ing.mp.setattr(jr, "append_records", real)
    assert _sync(ing).deferred_reason is None
    conn = ing.ns["open_cache_db"]()
    try:
        assert not G.deferral_recorded(conn)
    finally:
        conn.close()
    assert set(_rows(ing)) == {"m-x"}


def test_an_unwritable_journal_defers_a_rebuild_without_wiping(ing, capsys):
    """Detects: a rebuild wiping unlinked history it could not preserve."""
    _clock(ing, "2026-10-09T10:05:00Z")
    _line(ing, "2026-10-09T10:01:00Z", "x")
    conn = ing.ns["open_cache_db"]()
    try:
        _insert_row(conn, path=str(ing.root / "s1.jsonl"), offset=0,
                    msg_id="m-x", req_id="r-x", account="z" * 32)
        conn.execute(
            "INSERT INTO session_files (path, size_bytes, mtime_ns,"
            " last_byte_offset, last_ingested_at) VALUES (?,?,0,?,'x')",
            (str(ing.root / "s1.jsonl"),
             (ing.root / "s1.jsonl").stat().st_size,
             (ing.root / "s1.jsonl").stat().st_size))
        conn.commit()
    finally:
        conn.close()
    _login("A")
    _unwritable_journal(ing)
    capsys.readouterr()
    assert _cli(ing, "--source", "claude", "--rebuild") == 1
    err = capsys.readouterr().err
    assert ("[cache-sync] rebuild skipped (claude): the cctally journal could"
            " not be written. Check that the cctally journal directory is"
            " writable.") in err
    assert _rows(ing) == {"m-x": ("z" * 32, None, None, 20)}


def test_an_append_failure_after_the_rebuild_wipe_reports_incomplete(
        ing, capsys):
    """Detects: a rebuild that has already wiped telling the operator it was
    skipped (U5) when it in fact rebuilt every file but the deferred ones."""
    _login("A")
    _clock(ing, "2026-10-09T10:05:00Z")
    _line(ing, "2026-10-09T10:01:00Z", "x")
    _sync(ing)
    _line(ing, "2026-10-09T10:03:00Z", "y", file="s2")
    _clock(ing, "2026-10-09T10:06:00Z")
    _unwritable_journal(ing)
    capsys.readouterr()
    assert _cli(ing, "--source", "claude", "--rebuild") == 1
    err = capsys.readouterr().err
    assert ("[cache-sync] rebuild incomplete (claude): 1 file(s) failed"
            in err)
    assert "rebuild skipped (claude)" not in err
    assert set(_rows(ing)) == {"m-x"}


@pytest.mark.parametrize("rebuild", [False, True])
def test_a_torn_closing_capture_defers_like_the_first_read(ing, rebuild):
    """The closing capture is a second identity read; torn there, the whole
    sync defers before any accounting mutation, exactly as a torn first read
    does. Detects: a sync (or a rebuild's wipe) proceeding on a guessed
    closing account."""
    import _cctally_core as core
    _login("A")
    _clock(ing, "2026-10-09T10:05:00Z")
    _line(ing, "2026-10-09T10:01:00Z", "x")
    _sync(ing)
    _line(ing, "2026-10-09T10:03:00Z", "y")
    _clock(ing, "2026-10-09T10:06:00Z")
    conn = ing.ns["open_cache_db"]()
    try:
        cursor = conn.execute(
            "SELECT last_byte_offset FROM session_files").fetchall()
    finally:
        conn.close()
    before = _rows(ing)
    real = core._resolve_active_claude_identity
    calls = {"n": 0}

    def second_read_torn():
        calls["n"] += 1
        if calls["n"] == 2:
            return {"account_key": accts.UNATTRIBUTED, "natural_id": None,
                    "email": None, "plan_type": None, "status": "torn"}
        return real()
    ing.mp.setattr(core, "_resolve_active_claude_identity", second_read_torn)
    stats = _sync(ing, rebuild=rebuild)
    assert calls["n"] == 2
    assert stats.deferred_reason == "identity_torn"
    assert stats.files_deferred_torn == 1
    assert _rows(ing) == before
    conn = ing.ns["open_cache_db"]()
    try:
        assert conn.execute(
            "SELECT last_byte_offset FROM session_files").fetchall() == cursor
    finally:
        conn.close()


def _populate_two_accounts(ing):
    """A delayed-ingest fixture with independently expected accounts: two A
    records bracketed by A readings, one B record bracketed by B readings,
    ingested later under C; plus one pre-upgrade row stamped X whose record
    the walk reaches (exported as preserved history, never re-decided)."""
    _obs("A", "2026-10-09T10:00:00Z")
    _line(ing, "2026-10-09T10:01:00Z", "a1", out=500)
    _line(ing, "2026-10-09T10:01:30Z", "x3", out=50)
    conn = ing.ns["open_cache_db"]()
    try:
        _insert_row(conn, path=str(ing.root / "s1.jsonl"), offset=999_999,
                    msg_id="m-x3", req_id="r-x3", account="9" * 32)
        conn.commit()
    finally:
        conn.close()
    _line(ing, "2026-10-09T10:02:00Z", "a2", out=700, file="s2")
    _obs("A", "2026-10-09T10:03:00Z")
    _obs("B", "2026-10-09T11:00:00Z")
    _line(ing, "2026-10-09T11:01:00Z", "b1", out=300)
    _obs("B", "2026-10-09T11:02:00Z")
    _login("C")
    _clock(ing, "2026-10-09T12:00:00Z")
    _sync(ing)
    return {"m-a1": accts.account_key("claude", "A"),
            "m-a2": accts.account_key("claude", "A"),
            "m-b1": accts.account_key("claude", "B"),
            "m-x3": "9" * 32}


def _accounts(ing):
    return {k: v[0] for k, v in _rows(ing).items()}


@pytest.mark.parametrize("wipe", ["rebuild", "truncation", "recreated"])
def test_every_wipe_reproduces_the_decided_accounts(ing, wipe):
    """Detects: a wipe path restamping rows to the current login (the
    pre-#944 rebuild behaviour) or losing a decision."""
    expected = _populate_two_accounts(ing)
    assert _accounts(ing) == expected
    decisions = len(_decision_ops())
    _login("D")
    _clock(ing, "2026-10-09T13:00:00Z")
    if wipe == "rebuild":
        _sync(ing, rebuild=True)
    elif wipe == "truncation":
        # Shrinking s2 escalates to a full re-ingest; s2's record is gone.
        (ing.root / "s2.jsonl").write_text("")
        expected.pop("m-a2")
        _sync(ing)
    else:
        import _cctally_core as core
        for suffix in ("", "-wal", "-shm"):
            p = pathlib.Path(str(core.CACHE_DB_PATH) + suffix)
            if p.exists():
                p.unlink()
        _sync(ing)
    assert _accounts(ing) == expected
    assert len(_decision_ops()) == decisions


def test_an_orphans_decision_survives_until_its_transcript_returns(ing):
    """Detects: a missing transcript invalidating its decision (residual R7:
    the row returns with its decided account, not the current login)."""
    expected = _populate_two_accounts(ing)
    saved = (ing.root / "s2.jsonl").read_text()
    (ing.root / "s2.jsonl").unlink()
    _login("D")
    _sync(ing, rebuild=True)
    assert "m-a2" not in _rows(ing)
    (ing.root / "s2.jsonl").write_text(saved)
    _sync(ing)
    assert _accounts(ing) == expected


def test_a_downgrade_restamp_is_aligned_before_the_wipe(ing):
    """An older writer restamps a decided row and drops its link; the rebuild
    first aligns it back to its decision, then wipes. Detects: a
    downgrade's ingest-time restamp surviving (or blocking) a rebuild."""
    import _cctally_claude_attribution as G
    expected = _populate_two_accounts(ing)
    conn = ing.ns["open_cache_db"]()
    try:
        conn.execute("UPDATE session_entries SET account_key=?,"
                     " attribution_decision_key=NULL WHERE msg_id='m-a1'",
                     (accts.account_key("claude", "B"),))
        conn.commit()
    finally:
        conn.close()
    _sync(ing, rebuild=True)
    assert _accounts(ing) == expected
    conn = ing.ns["open_cache_db"]()
    try:
        assert G._meta_json(conn, G.ALIGNED_KEY)["count"] == 1
    finally:
        conn.close()


def test_account_scoped_reads_after_a_delayed_ingest(ing):
    """AC2: unchanged account-scoped readers return the write-time accounts'
    costs, independently expected from the transcript usage. Detects:
    per-account spend attributed to the account active at ingest."""
    _populate_two_accounts(ing)
    price = ing.ns["_calculate_entry_cost"]

    def cost(out):
        return price(MODEL, {"input_tokens": 1000, "output_tokens": out,
                             "cache_creation_input_tokens": 0,
                             "cache_read_input_tokens": 0})
    start = _dt.datetime(2026, 10, 9, tzinfo=_dt.timezone.utc)
    end = start + _dt.timedelta(days=1)
    got = {}
    for name in ("A", "B", "C"):
        entries = ing.ns["get_entries"](
            start, end, skip_sync=True,
            account_key=accts.account_key("claude", name))
        got[name] = sum(price(e.model, e.usage) for e in entries)
    assert got["A"] == pytest.approx(cost(500) + cost(700), abs=1e-9)
    assert got["C"] == 0 and cost(50) > 0
    assert got["B"] == pytest.approx(cost(300), abs=1e-9)
    assert got["C"] == 0
    assert got["A"] > got["B"] > 0


def test_rederive_reads_the_write_time_accounts_costs(ing):
    """L1: `db rederive` re-derives per-account Claude history through its
    scratch read adapters; after a delayed ingest across an A->B switch they
    return A's and B's independently expected costs. Detects: a rederive
    re-filing A's and B's spend to the account active at ingest (C)."""
    import _cctally_rederive as R
    cctally = sys.modules["cctally"]
    _populate_two_accounts(ing)
    price = ing.ns["_calculate_entry_cost"]

    def cost(out):
        return price(MODEL, {"input_tokens": 1000, "output_tokens": out,
                             "cache_creation_input_tokens": 0,
                             "cache_read_input_tokens": 0})

    def joined_cost(e):
        return price(e.model, {
            "input_tokens": e.input_tokens, "output_tokens": e.output_tokens,
            "cache_creation_input_tokens": e.cache_creation_tokens,
            "cache_read_input_tokens": e.cache_read_tokens})
    start = _dt.datetime(2026, 10, 9, tzinfo=_dt.timezone.utc)
    end = start + _dt.timedelta(days=1)
    got = {}
    conn = ing.ns["open_cache_db"]()
    try:
        with R._scratch_read_adapters(conn):
            for name in ("A", "B", "C"):
                key = accts.account_key("claude", name)
                joined = cctally.get_claude_session_entries(
                    start, end, skip_sync=True, account_key=key)
                plain = cctally.get_entries(
                    start, end, skip_sync=True, account_key=key)
                got[name] = (sum(joined_cost(e) for e in joined),
                             sum(price(e.model, e.usage) for e in plain))
    finally:
        conn.close()
    a, b = cost(500) + cost(700), cost(300)
    assert got["A"] == (pytest.approx(a, abs=1e-9), pytest.approx(a, abs=1e-9))
    assert got["B"] == (pytest.approx(b, abs=1e-9), pytest.approx(b, abs=1e-9))
    assert got["C"] == (0, 0)


def test_a_crash_after_the_append_recovers_without_a_second_decision(ing):
    """A process killed after its decisions reached the journal and before
    the cache commit: the next sync recovers those decisions by replay.
    Detects: a crash window producing two decisions for one record."""
    import multiprocessing
    import _cctally_cache as cc
    _obs("A", "2026-10-09T10:00:00Z")
    _line(ing, "2026-10-09T10:01:00Z", "x")
    _obs("A", "2026-10-09T10:02:00Z")
    _login("B")
    _clock(ing, "2026-10-09T10:05:00Z")

    def child():
        def die(point):
            if point == "claude_precommit":
                os._exit(9)
        cc._cache_storm_test_pause = die
        _sync(ing)
        os._exit(0)
    proc = multiprocessing.get_context("fork").Process(target=child)
    proc.start()
    proc.join(60)
    assert proc.exitcode == 9
    assert len(_decision_ops("m-x")) == 1
    assert _rows(ing) == {}
    _login("C")
    _sync(ing)
    assert _rows(ing)["m-x"][:2] == (accts.account_key("claude", "A"),
                                     "timeline_agreement")
    assert len(_decision_ops("m-x")) == 1


def test_an_append_failure_recovers_on_the_next_sync(ing):
    """Detects: a failed append leaving a cursor advanced past undecided
    records (they would never be ingested)."""
    a = _login("A")
    _clock(ing, "2026-10-09T10:05:00Z")
    _line(ing, "2026-10-09T10:01:00Z", "x")
    import _cctally_journal as jr
    real = jr.append_records
    _unwritable_journal(ing)
    _sync(ing)
    assert _rows(ing) == {}
    ing.mp.setattr(jr, "append_records", real)
    stats = _sync(ing)
    assert stats.deferred_reason is None
    assert _rows(ing)["m-x"][:3] == (a, "fallback", "pre_timeline")
    assert len(_decision_ops("m-x")) == 1


def test_the_neighbour_memo_holds_only_the_current_file(ing):
    """The sync's timeline-neighbour memo is scoped to the file being
    attributed. Detects: a memo that kept every interval of the whole run --
    a cold first sync of a 697,733-row store peaked at 1,188 MB RSS against
    630 MB before write-time attribution."""
    import _cctally_cache as cc
    _login("A")
    _clock(ing, "2026-10-09T12:00:00Z")
    _line(ing, "2026-10-09T10:01:00Z", "a", file="s1")
    _line(ing, "2026-10-09T10:02:00Z", "b", file="s2")
    real = cc._attribute_claude_rows
    seen = []

    def spy(conn, run, path_str, rows, metas):
        out = real(conn, run, path_str, rows, metas)
        seen.append((pathlib.Path(path_str).name, set(run.memo)))
        return out
    ing.mp.setattr(cc, "_attribute_claude_rows", spy)
    _sync(ing)
    assert [name for name, _keys in seen] == ["s1.jsonl", "s2.jsonl"]
    first, second = seen[0][1], seen[1][1]
    assert first and second
    assert not first & second


@pytest.mark.parametrize("bad,stored_msg_id", [
    ({"requestId": ""}, "m-bad"),
    ({"message": {"id": 12345}}, None),
], ids=["empty-requestId", "numeric-message-id"])
def test_an_invalid_record_id_never_wedges_ingest(ing, bad, stored_msg_id):
    """#944 F1: one transcript line with an empty requestId (or a numeric
    message.id) aborted every later sync: its ids reached the journal as a
    keyed item the validator rejects, a ValueError no sync path catches.
    Such a record is identified by its bytes (physical form), the walk goes
    on to the next files, and later syncs keep ingesting. Also pins #944 F7:
    the walk keeps raw line bytes only for records it identifies by bytes."""
    import _cctally_cache as cc
    _obs("A", "2026-10-09T10:00:00Z")
    obj = {"type": "assistant", "timestamp": "2026-10-09T10:01:00Z",
           "sessionId": "s1", "cwd": "/home/u/proj", "requestId": "r-bad",
           "message": {"id": "m-bad", "model": MODEL, "usage": {
               "input_tokens": 1000, "output_tokens": 500,
               "cache_creation_input_tokens": 0,
               "cache_read_input_tokens": 0}}}
    for key, value in bad.items():
        if isinstance(value, dict):
            obj[key].update(value)
        else:
            obj[key] = value
    with (ing.root / "s1.jsonl").open("a") as fh:
        fh.write(json.dumps(obj) + "\n")
    _line(ing, "2026-10-09T10:01:30Z", "ok", file="s1")
    _line(ing, "2026-10-09T10:02:00Z", "next", file="s2")
    _obs("A", "2026-10-09T10:03:00Z")
    a = accts.account_key("claude", "A")
    _login("B")
    _clock(ing, "2026-10-09T10:05:00Z")
    real = cc._attribute_claude_rows
    kept = []

    def spy(conn, run, path_str, rows, metas):
        kept.extend((r[4] is not None and r[5] is not None, raw is not None)
                    for r, (raw, _ts) in zip(rows, metas))
        return real(conn, run, path_str, rows, metas)
    ing.mp.setattr(cc, "_attribute_claude_rows", spy)
    _sync(ing)
    rows = _rows(ing)
    assert {k: v[:2] for k, v in rows.items()} == {
        "m-ok": (a, "timeline_agreement"),
        "m-next": (a, "timeline_agreement"),
        stored_msg_id: (a, "timeline_agreement")}
    assert sorted(kept) == [(False, True), (True, False), (True, False)]
    forms = [i[0] for op in _decision_ops() for i in op["items"]]
    assert sorted(forms) == ["k1", "k1", "p1"]
    # Every later sync still ingests.
    _line(ing, "2026-10-09T10:02:30Z", "later", file="s2")
    _sync(ing)
    assert _rows(ing)["m-later"][:2] == (a, "timeline_agreement")


def test_every_decision_records_the_high_water_its_replay_consumed(ing):
    """#944 F8 (spec 5.4): ``journal_hw`` is the safe high-water the rule ran
    against. Detects: a sync sampling the high-water at its first append,
    past a record another writer appended after the replay, so the decision
    claims evidence the rule never read."""
    import _cctally_claude_attribution as G
    import _cctally_journal as jr
    _obs("A", "2026-10-09T10:00:00Z")
    _line(ing, "2026-10-09T10:01:00Z", "x", file="s1")
    _line(ing, "2026-10-09T10:01:30Z", "y", file="s2")
    _login("A")
    _clock(ing, "2026-10-09T10:05:00Z")
    real = G.rehydrate_claude_attribution
    consumed = []

    def replay_then_foreign_append(conn, **kw):
        out = real(conn, **kw)
        consumed.append(list(jr.journal_safe_high_water()))
        _obs("B", "2026-10-09T09:00:00Z")
        return out
    ing.mp.setattr(G, "rehydrate_claude_attribution",
                   replay_then_foreign_append)
    _sync(ing)
    (hw,) = consumed
    ops = _decision_ops()
    assert len(ops) == 2
    assert [op["journal_hw"] for op in ops] == [hw, hw]
