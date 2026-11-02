"""#944 (inverting the #825/#832 residual-5 probe; spec 8 test 2, AC1).

Claude cost rows used to be stamped with the identity active when cache ingest
RAN. #944 attributes each line to the account active when Claude Code WROTE
it, from the admitted bound `record-usage` observations that bracket the
line. This probe is the distinguishing reproduction: on the pre-#944 code the
A-written line ingested after the switch to B carries B.

The bracketing observations are appended directly through the journal with
the exact bound `record-usage` payload shape, so no milestone crossing can run
`sync_cache` before the post-switch ingest (spec finding 9); the test asserts
the target row is absent right before that ingest.

Detects: ingest-time attribution of a Claude cost row written under another
account (wrong per-account cost after an account switch).
"""
import json

import pytest

import _outcome_quarantine_trace as T


@pytest.fixture
def app(monkeypatch, tmp_path):
    app = T.make_app(monkeypatch, tmp_path)
    app.tmp_home = tmp_path
    return app


def _path(app, name):
    path = app.tmp_home / ".claude" / "projects" / "-tmp-proj" / f"{name}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _append(app, at, tag, *, file="s1"):
    with _path(app, file).open("a") as fh:
        fh.write(json.dumps({
            "type": "assistant", "timestamp": at, "sessionId": file,
            "cwd": "/tmp/proj", "requestId": f"r-{tag}",
            "message": {"id": f"m-{tag}", "model": "claude-sonnet-4-5-20250929",
                        "usage": {"input_tokens": 1000, "output_tokens": 500,
                                  "cache_creation_input_tokens": 0,
                                  "cache_read_input_tokens": 0}},
        }) + "\n")


def _observe(name, at):
    """One bound Claude `record-usage` observation captured at ``at`` under
    account ``name``, in the shape `record_usage_bound` journals, appended
    without running any ingest. Returns its journal coordinate."""
    import _cctally_journal as jr
    import _lib_account_binding as ab
    import _lib_accounts
    import _lib_journal as lj
    key = _lib_accounts.account_key("claude", name)
    binding = ab.capture({
        "status": ab.STATUS_IDENTIFIED, "account_key": key,
        "natural_id": name, "email": f"{name}@example.test",
        "plan_type": None})
    payload = {
        "weekly_percent": 10.0, "resets_at": T.epoch("2026-10-12T00:00:00Z"),
        "source": "api", "captured_at": at,
        "account_binding": binding.document(),
    }
    record = lj.make_obs(at, "record-usage", "claude", payload, account=key)
    # A real cutover, so admission is decided by the boundary, not by its
    # absence.
    jr.ensure_account_bound_cutover()
    seg, end = jr.append_record(record)
    return record, seg, end - len(lj.encode_line(record))


def _admitted(record, seg, off):
    import _cctally_journal as jr
    import _lib_account_protocol as ap
    boundary = jr.find_account_bound_cutover()
    assert boundary is not None
    return ap.classify_admission(
        record, ap.Coordinate(seg, off), boundary).admitted


def _cache_sync(app):
    args = app.build_parser().parse_args(["cache-sync", "--source", "claude"])
    assert args.func(args) == 0


def _account(app, msg_id):
    """The row's account, read from columns the pre-#944 schema already has,
    so the probe fails on pre-#944 code on the account value itself."""
    cache = app.open_cache_db()
    try:
        row = cache.execute(
            "SELECT account_key FROM session_entries WHERE msg_id=?",
            (msg_id,)).fetchone()
        return None if row is None else row[0]
    finally:
        cache.close()


def _basis(app, msg_id):
    cache = app.open_cache_db()
    try:
        row = cache.execute(
            "SELECT d.basis FROM session_entries e JOIN claude_cost_decisions"
            " d ON d.decision_key = e.attribution_decision_key"
            " WHERE e.msg_id=?", (msg_id,)).fetchone()
        return None if row is None else row[0]
    finally:
        cache.close()


def _present(app, msg_id):
    cache = app.open_cache_db()
    try:
        return cache.execute(
            "SELECT 1 FROM session_entries WHERE msg_id=?",
            (msg_id,)).fetchone() is not None
    finally:
        cache.close()


@pytest.mark.parametrize("first,second", [("A", "B"), ("B", "A")])
@pytest.mark.parametrize("reverse_discovery", [False, True])
def test_a_line_written_before_a_switch_keeps_its_write_time_account(
        app, monkeypatch, first, second, reverse_discovery):
    import _cctally_cache as cc
    original = cc._iter_claude_jsonl_files
    if reverse_discovery:
        monkeypatch.setattr(cc, "_iter_claude_jsonl_files",
                            lambda: list(reversed(list(original()))))
    first_key = T.identity(first)
    T.set_clock(monkeypatch, "2026-10-09T10:00:30Z")
    before = _observe(first, "2026-10-09T10:00:30Z")
    # Written while the first account is active, in two files so both
    # discovery orders are exercised.
    _append(app, "2026-10-09T10:01:00Z", "target", file="s1")
    _append(app, "2026-10-09T10:01:05Z", "other", file="s2")
    T.set_clock(monkeypatch, "2026-10-09T10:01:30Z")
    after = _observe(first, "2026-10-09T10:01:30Z")
    assert _admitted(*before) and _admitted(*after)
    assert (before[0]["payload"]["captured_at"] < "2026-10-09T10:01:00Z"
            < after[0]["payload"]["captured_at"])
    # No ingest ran yet: the target row is absent before the post-switch one.
    assert not _present(app, "m-target")
    # The switch, then the ingest under the second account.
    T.identity(second)
    T.set_clock(monkeypatch, "2026-10-09T10:02:00Z")
    _observe(second, "2026-10-09T10:02:00Z")
    T.set_clock(monkeypatch, "2026-10-09T10:03:00Z")
    _cache_sync(app)
    assert _account(app, "m-target") == first_key
    assert _account(app, "m-other") == first_key
    assert _basis(app, "m-target") == "timeline_agreement"
    assert _basis(app, "m-other") == "timeline_agreement"


def test_without_timeline_evidence_the_line_takes_the_ingest_account(
        app, monkeypatch):
    """The original evidence-free sequence: no bound observation precedes the
    line, so it falls back to the ingest identity (reason `pre_timeline`) and
    the first decision is kept."""
    a = T.identity("A")
    T.set_clock(monkeypatch, "2026-10-09T10:00:00Z")
    _append(app, "2026-10-09T09:59:00Z", "a1")
    _cache_sync(app)
    _append(app, "2026-10-09T10:01:00Z", "a2")
    b = T.identity("B")
    T.set_clock(monkeypatch, "2026-10-09T10:02:00Z")
    _cache_sync(app)
    assert (_account(app, "m-a1"), _account(app, "m-a2")) == (a, b)
    assert (_basis(app, "m-a1"), _basis(app, "m-a2")) == (
        "fallback", "fallback")
    cache = app.open_cache_db()
    try:
        reasons = {r[0] for r in cache.execute(
            "SELECT d.reason FROM session_entries e JOIN claude_cost_decisions"
            " d ON d.decision_key = e.attribution_decision_key")}
    finally:
        cache.close()
    assert reasons == {"pre_timeline"}
    # Switching back and re-ingesting does not re-attribute it.
    T.identity("A")
    _cache_sync(app)
    assert (_account(app, "m-a2"), _basis(app, "m-a2")) == (b, "fallback")
