"""#279 S3 F2 — the cache-ingest path and the direct-JSONL fallback path must
produce the SAME set of parsed entries over the SAME window.

The fallback ``_parse_usage_entries`` (used when cache.db can't be opened) was
a second, hand-copied implementation of the per-line gating that
``parse_cost_entry`` owns for the cache path — two copies that could silently
drift. S3 F2 rewrites the fallback to delegate to ``parse_cost_entry`` so there
is ONE gating implementation. This test is the enforcement: a rich fixture that
exercises every gating branch is parsed both ways and the projected multisets
must be identical.

The projection deliberately excludes ``source_path``: the cross-file dedup
duplicate's sticky-source_path semantics are pinned separately by
``tests/test_cache_dedup_fallback_source_path_sticky.py``. It compares the
fields BOTH paths guarantee: (timestamp, model, 4 token counts, speed,
cost_usd).
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "tests"))

from conftest import load_script, redirect_paths  # noqa: E402


# Query window used by both paths. The out-of-range fixture row sits before it.
RANGE_START = dt.datetime(2026, 7, 1, tzinfo=dt.timezone.utc)
RANGE_END = dt.datetime(2026, 7, 8, tzinfo=dt.timezone.utc)


def _assistant_line(*, msg_id, req_id, model="claude-opus-4-8", ts,
                    inp=0, out=0, cc=0, cr=0, speed=None, cost_usd="__omit__"):
    usage = {
        "input_tokens": inp, "output_tokens": out,
        "cache_creation_input_tokens": cc, "cache_read_input_tokens": cr,
    }
    if speed is not None:
        usage["speed"] = speed
    obj = {
        "type": "assistant",
        "timestamp": ts,
        "message": {"id": msg_id, "model": model, "usage": usage},
    }
    if req_id is not None:
        obj["requestId"] = req_id
    if cost_usd != "__omit__":
        obj["costUSD"] = cost_usd
    return json.dumps(obj)


def _write_rich_fixture(tmp_path: pathlib.Path) -> None:
    """Write two JSONL files exercising every gating branch of both parsers."""
    projects = tmp_path / ".claude" / "projects"
    proj_a = projects / "-Users-u-proj-A"
    proj_b = projects / "-Users-u-proj-B"
    proj_a.mkdir(parents=True, exist_ok=True)
    proj_b.mkdir(parents=True, exist_ok=True)

    # File A ────────────────────────────────────────────────────────────────
    a_lines = [
        # valid keyed entry
        _assistant_line(msg_id="mA1", req_id="rA1", ts="2026-07-02T10:00:00Z",
                        inp=10, out=5),
        # null-requestId entry -> no-key list
        _assistant_line(msg_id="mA2", req_id=None, ts="2026-07-02T10:01:00Z",
                        inp=3, out=2),
        # <synthetic> row -> dropped
        _assistant_line(msg_id="mA3", req_id="rA3", model="<synthetic>",
                        ts="2026-07-02T10:02:00Z", inp=1, out=1),
        # drift: bad/missing timestamp (empty string) -> skipped
        _assistant_line(msg_id="mA4", req_id="rA4", ts="", inp=1, out=1),
        # malformed JSON line -> skipped
        "{ this is not valid json",
        # blank line -> skipped
        "",
        # cross-file dup (lower total: 200) -> B wins the dedup contest
        _assistant_line(msg_id="mDUP", req_id="rDUP", ts="2026-07-03T09:00:00Z",
                        inp=100, out=100),
        # numeric costUSD row
        _assistant_line(msg_id="mA5", req_id="rA5", ts="2026-07-03T11:00:00Z",
                        inp=7, out=8, cost_usd=0.25),
        # IN-RANGE non-numeric costUSD -> must degrade to cost_usd=None, not raise
        _assistant_line(msg_id="mA6", req_id="rA6", ts="2026-07-03T12:00:00Z",
                        inp=4, out=4, cost_usd="oops"),
        # OUT-OF-RANGE non-numeric costUSD -> excluded by both range filters
        _assistant_line(msg_id="mA7", req_id="rA7", ts="2026-06-01T00:00:00Z",
                        inp=9, out=9, cost_usd="oops2"),
        # Range-boundary rows (review P3-1): both paths treat the window as
        # INCLUSIVE on both ends — rows exactly AT the bounds must appear in
        # both, and a row one second past the end in neither. An
        # inclusive-vs-exclusive drift in either filter diverges the multisets.
        _assistant_line(msg_id="mB0", req_id="rB0", ts="2026-07-01T00:00:00Z",
                        inp=11, out=11),   # == RANGE_START
        _assistant_line(msg_id="mB8", req_id="rB8", ts="2026-07-08T00:00:00Z",
                        inp=12, out=12),   # == RANGE_END
        _assistant_line(msg_id="mB9", req_id="rB9", ts="2026-07-08T00:00:01Z",
                        inp=13, out=13),   # just past RANGE_END -> excluded
    ]
    (proj_a / "sess-a.jsonl").write_text("\n".join(a_lines) + "\n")

    # File B ────────────────────────────────────────────────────────────────
    b_lines = [
        # null-message.id entry -> no-key list
        _assistant_line(msg_id=None, req_id="rB1", ts="2026-07-02T13:00:00Z",
                        inp=6, out=6),
        # drift: usage non-dict -> skipped
        json.dumps({"type": "assistant", "timestamp": "2026-07-02T13:01:00Z",
                    "requestId": "rB2",
                    "message": {"id": "mB2", "model": "claude-opus-4-8",
                                "usage": "not-a-dict"}}),
        # drift: missing model -> skipped
        json.dumps({"type": "assistant", "timestamp": "2026-07-02T13:02:00Z",
                    "requestId": "rB3",
                    "message": {"id": "mB3",
                                "usage": {"input_tokens": 1, "output_tokens": 1}}}),
        # cross-file dup (higher total: 400) -> wins
        _assistant_line(msg_id="mDUP", req_id="rDUP", ts="2026-07-03T09:00:00Z",
                        inp=200, out=200),
        # speed-tiebreak pair (equal totals: 50); the speed-set row wins
        _assistant_line(msg_id="mSPD", req_id="rSPD", ts="2026-07-04T08:00:00Z",
                        inp=25, out=25),
        _assistant_line(msg_id="mSPD", req_id="rSPD", ts="2026-07-04T08:00:00Z",
                        inp=25, out=25, speed="standard"),
    ]
    (proj_b / "sess-b.jsonl").write_text("\n".join(b_lines) + "\n")


def _project(entries):
    """The fields BOTH paths guarantee. source_path is deliberately excluded
    (its cross-file-dup sticky semantics are pinned elsewhere)."""
    return sorted(
        (
            e.timestamp.isoformat(),
            e.model,
            int(e.usage.get("input_tokens", 0) or 0),
            int(e.usage.get("output_tokens", 0) or 0),
            int(e.usage.get("cache_creation_input_tokens", 0) or 0),
            int(e.usage.get("cache_read_input_tokens", 0) or 0),
            e.usage.get("speed"),
            e.cost_usd,
        )
        for e in entries
    )


def test_cache_and_fallback_paths_are_equivalent(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _write_rich_fixture(tmp_path)

    sync_cache = ns["sync_cache"]
    open_cache_db = ns["open_cache_db"]
    iter_entries = ns["iter_entries"]
    collect_direct = ns["_collect_entries_direct"]

    conn = open_cache_db()
    try:
        sync_cache(conn)
        cache_entries = iter_entries(conn, RANGE_START, RANGE_END)
    finally:
        conn.close()

    fallback_entries = collect_direct(RANGE_START, RANGE_END)

    assert len(cache_entries) > 0, "fixture must actually parse into rows"
    assert _project(cache_entries) == _project(fallback_entries)
    # Boundary pins (review P3-1): the two at-bound rows are IN, the
    # past-the-end row is OUT — in BOTH paths (the multiset equality above
    # proves the paths agree; these prove the agreed behavior is inclusive).
    projected = _project(cache_entries)
    in_tokens = {p[2] for p in projected}
    assert 11 in in_tokens and 12 in in_tokens, "at-bound rows must be included"
    assert 13 not in in_tokens, "past-the-end row must be excluded"
