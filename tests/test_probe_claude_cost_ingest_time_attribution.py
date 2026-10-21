"""#825/#832 Task 4.3 — the residual-5 reproduction probe (spec section 13,
residual 5; operator Q7; review finding R1-002).

EXPECTATION TEST OF TODAY'S BEHAVIOR, not a statement of the desired one.
Claude cost rows in `cache.db` are stamped with the identity active when
cache ingest RUNS, not the identity active when Claude Code WROTE the JSONL
line, and the first stamp is kept. No old process is involved: the account
switches between a JSONL append and the next cache ingest, and
`session_entries.account_key` of the appended line carries the account that
was active at ingest time.

If this test starts failing, the attribution changed — revisit residual 5 and
the M20 attribution-timing trace rather than updating the expectation.
"""
import json

import pytest

import _outcome_quarantine_trace as T


@pytest.fixture
def app(monkeypatch, tmp_path):
    app = T.make_app(monkeypatch, tmp_path)
    app.tmp_home = tmp_path
    return app


def _append(app, at, tag):
    path = app.tmp_home / ".claude" / "projects" / "-tmp-proj" / "s1.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps({
            "type": "assistant", "timestamp": at, "sessionId": "s1",
            "cwd": "/tmp/proj", "requestId": f"r-{tag}",
            "message": {"id": f"m-{tag}", "model": "claude-sonnet-4-5-20250929",
                        "usage": {"input_tokens": 1000, "output_tokens": 500,
                                  "cache_creation_input_tokens": 0,
                                  "cache_read_input_tokens": 0}},
        }) + "\n")


def _cache_sync(app):
    args = app.build_parser().parse_args(["cache-sync", "--source", "claude"])
    assert args.func(args) == 0


def _stamps(app):
    cache = app.open_cache_db()
    try:
        return [tuple(row) for row in cache.execute(
            "SELECT timestamp_utc, account_key FROM session_entries "
            "WHERE source_path LIKE '%-tmp-proj%' ORDER BY timestamp_utc")]
    finally:
        cache.close()


def test_a_jsonl_tail_is_stamped_with_the_account_active_at_ingest(
        app, monkeypatch):
    a = T.identity("A")
    T.set_clock(monkeypatch, "2026-10-09T10:00:00Z")
    _append(app, "2026-10-09T09:59:00Z", "a1")
    _cache_sync(app)
    # Written while A is active ...
    _append(app, "2026-10-09T10:01:00Z", "a2")
    # ... ingested after the switch to B.
    b = T.identity("B")
    T.set_clock(monkeypatch, "2026-10-09T10:02:00Z")
    _cache_sync(app)
    stamps = _stamps(app)
    assert stamps[0][1] == a, stamps
    # Reproduced: the A-written line carries B, the identity at ingest time.
    assert stamps[1][1] == b, stamps
    # And the first stamp is kept: switching back and re-ingesting does not
    # re-attribute it.
    T.identity("A")
    _cache_sync(app)
    assert _stamps(app)[1][1] == b
