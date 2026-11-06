"""#1003 T1 — the incremental block-totals engine equals the full re-price.

Inside a scratch replay (`_scratch_read_adapters`) `_compute_block_totals`
is answered by `_cctally_rederive.BlockTotalsEngine`, which caches each load
range's entries, extends them by a delta fetch, and folds each owned entry
once (spec D1). A replay derives candidates, rows and journal bytes from
these totals, so any difference from the full re-price is a wrong five-hour
block cost, a wrong milestone or a wrong close correction. Every sequence
below runs twice over one seeded cache, once with the engine (shadow
verification on) and once with the slot forced empty, and the two must agree
on the returned dict (float bits and key order), the pricing evidence, the
owned entries by value and each call's stderr.
"""
from __future__ import annotations

import datetime as dt
import sqlite3

import pytest

from conftest import load_script, redirect_paths

UTC = dt.timezone.utc
S = dt.datetime(2026, 8, 1, tzinfo=UTC)
KNOWN = "claude-3-5-sonnet-20241022"
FAST = "claude-opus-4-6"
#: Models absent from the embedded pricing: one only ever unpriced, one also
#: seen with a raw `costUSD` (the evidence's `uncertainModels`).
UNKNOWN = "claude-zz-1003-unpriced"
AMBIGUOUS = "claude-zz-1003-ambiguous"


def _h(hours: float) -> dt.datetime:
    return S + dt.timedelta(hours=hours)


def _plus(moment: dt.datetime) -> str:
    """The text `_joined_entries` binds: `+00:00` offset."""
    return moment.isoformat()


def _zulu(moment: dt.datetime) -> str:
    """The same instant stored with a `Z` suffix, which sorts AFTER the
    `+00:00` spelling under SQLite's byte order."""
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


#: (text, model, input, output, cache_create, cache_1h, cache_read, raw cost,
#:  speed, source file). Rows are inserted in this order, so `id` follows it.
_ROWS = (
    (_plus(_h(0.25)), KNOWN, 100, 200, 300, 100, 400, None, None, "a"),
    (_zulu(_h(0.5)), UNKNOWN, 10, 20, 0, 0, 0, None, None, "b"),
    (_plus(_h(1)), KNOWN, 0, 0, 0, 0, 0, None, None, "a"),          # zero tokens
    (_plus(_h(1.5)), AMBIGUOUS, 5, 5, 0, 0, 0, 0.125, None, "a"),   # raw cost
    (_plus(_h(2)), FAST, 1000, 50, 0, None, 10, None, "fast", "b"),
    # Two equal texts at exactly the `through` of a call ending at 2h.
    (_plus(_h(2)), KNOWN, 7, 7, 0, 0, 0, None, None, "a"),
    # The same instant spelled `Z`: not loaded by an end of 2h `+00:00`.
    (_zulu(_h(2)), KNOWN, 9, 9, 0, 0, 0, None, None, "nometa"),
    (_plus(_h(2.5)), AMBIGUOUS, 3, 3, 0, 0, 0, None, None, "b"),
    (_zulu(_h(3)), KNOWN, 11, 13, 17, 5, 19, None, None, "a"),
    (_plus(_h(3.5)), UNKNOWN, 1, 1, 0, 0, 0, None, None, "a"),
    (_plus(_h(4)), KNOWN, 23, 29, 0, 0, 31, 0.5, None, "b"),
    (_plus(_h(4.75)), FAST, 37, 41, 43, 43, 47, None, "fast", "a"),
    (_plus(_h(5.5)), KNOWN, 53, 59, 0, 0, 0, None, None, "b"),
    (_plus(_h(6.5)), KNOWN, 61, 67, 0, 0, 0, None, None, "a"),
)
_FILES = {
    "a": ("/tmp/claude/projects/repo-a/s-a.jsonl", "sess-a", "/repo-a"),
    "b": ("/tmp/claude/projects/repo-b/s-b.jsonl", "sess-b", "/repo-b"),
    # No `session_files` row: the LEFT JOIN yields a NULL project.
    "nometa": ("/tmp/claude/projects/repo-c/s-c.jsonl", None, None),
}


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


@pytest.fixture
def cache(ns):
    conn = ns["open_cache_db"]()
    for key, (path, session, project) in _FILES.items():
        if session is None:
            continue
        conn.execute(
            "INSERT INTO session_files (path, size_bytes, mtime_ns, "
            "last_byte_offset, last_ingested_at, session_id, project_path) "
            "VALUES (?,?,?,?,?,?,?)",
            (path, 100, 1, 100, "2026-08-01T00:00:00Z", session, project))
    for offset, (text, model, inp, out, cc, c1h, cr, raw, speed,
                 key) in enumerate(_ROWS):
        conn.execute(
            "INSERT INTO session_entries (source_path, line_offset, "
            "timestamp_utc, model, input_tokens, output_tokens, "
            "cache_create_tokens, cache_read_tokens, cache_create_1h_tokens, "
            "account_key, cost_usd_raw, speed) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (_FILES[key][0], offset * 10, text, model, inp, out, cc, cr, c1h,
             "acct-a", raw, speed))
    conn.commit()
    yield conn
    conn.close()


def _window(key, start, reset):
    import _lib_blocks
    return _lib_blocks.OwnedWindow(key=key, start=start, reset=reset)


W1 = (1, _h(0), _h(5))
#: A predecessor that resets earlier, so it owns [0h, 2h) of a run at 0h.
W0 = (0, _h(-3), _h(2))
#: W1 truncated by a credit at 2h, and the successor that takes over.
W1_CUT = (1, _h(0), _h(2))
W2 = (2, _h(2), _h(7))


def _ws(*specs):
    return [_window(*spec) for spec in specs]


def _canon(value):
    if isinstance(value, float):
        return ("float", value.hex())
    if isinstance(value, dict):
        return ("dict", tuple((k, _canon(v)) for k, v in value.items()))
    if isinstance(value, (list, tuple)):
        return ("seq", tuple(_canon(v) for v in value))
    if isinstance(value, dt.datetime):
        return ("dt", value.isoformat())
    return value


def _entry_value(entry):
    """An owned entry by value: every dataclass field plus the #869 source
    identity attributes `_joined_entries` sets after construction."""
    return _canon(dict(vars(entry)))


def _call(rec, spec):
    start, end, owner, windows, evidence, owned = spec
    pricing_evidence = {} if evidence else None
    owned_entries = [] if owned else None
    result = rec._compute_block_totals(
        start, end, owner_key=owner, windows=windows, skip_sync=True,
        pricing_evidence=pricing_evidence, owned_entries=owned_entries)
    return (
        _canon(result),
        pricing_evidence,
        None if owned_entries is None
        else tuple(_entry_value(e) for e in owned_entries),
    )


def _run(cache, calls, capsys, monkeypatch, *, engine):
    """Every call's answer and stderr, with the engine or with the slot
    forced empty. The one-shot unknown-model warning set starts empty, so
    both paths may warn and must warn on the same call."""
    import _cctally_record as rec
    import _cctally_rederive
    import _lib_pricing
    monkeypatch.setattr(_lib_pricing, "_unknown_model_warnings", set())
    capsys.readouterr()
    out = []
    with _cctally_rederive._scratch_read_adapters(cache):
        installed = rec._BLOCK_TOTALS_ENGINE
        assert isinstance(installed, _cctally_rederive.BlockTotalsEngine)
        if not engine:
            assert rec.swap_block_totals_engine(None) is installed
        try:
            for spec in calls:
                answer = _call(rec, spec)
                out.append((answer, capsys.readouterr().err))
        finally:
            if not engine:
                rec.swap_block_totals_engine(installed)
    return out, installed


def _ext(owner=1, windows=(W1,), *, evidence=True, owned=True):
    return lambda end: (S, end, owner, _ws(*windows), evidence, owned)


_SEQUENCES = {
    "extending": [
        _ext()(_h(h)) for h in (0.5, 1, 2, 3, 4, 4.99)],
    "repeated_end": [_ext()(_h(2.5))] * 3,
    "backward_then_forward": [
        _ext()(_h(4)), _ext()(_h(1)), _ext()(_h(4.5)), _ext()(_h(4.5))],
    "end_before_start": [
        _ext()(_h(-1)), _ext()(_h(2)), _ext()(_h(-0.5)), _ext()(_h(3))],
    # 2h `+00:00` is a cut between equal texts and the `Z` spelling of the
    # same instant: the delta after it must neither repeat the two equal
    # texts nor skip the `Z` one.
    "ties_across_through": [
        _ext()(_h(2)), _ext()(_h(2)), _ext()(_h(2.25)), _ext()(_h(2))],
    # Text order, not instant order: after loading through 4h, an end of 3h
    # `+00:00` excludes the entry stored as 3h `Z`.
    "mixed_offsets_backward": [
        _ext()(_h(4)), _ext()(_h(3)), _ext()(_h(2)), _ext()(_h(4))],
    "window_set_changes": [
        _ext()(_h(4)),
        _ext(1, (W1_CUT, W2))(_h(4)),
        _ext(2, (W1_CUT, W2))(_h(4)),
        _ext(2, (W1_CUT, W2))(_h(6.75)),
        _ext()(_h(4.5)),
    ],
    "two_owners_one_run": [
        _ext(1, (W1, W0))(_h(1)), _ext(0, (W1, W0))(_h(1)),
        _ext(1, (W1, W0))(_h(3)), _ext(0, (W1, W0))(_h(3)),
        _ext(1, (W1, W0))(_h(4.9)), _ext(0, (W1, W0))(_h(1.9)),
    ],
    "evidence_and_owned_toggle": [
        _ext(evidence=False, owned=False)(_h(1)),
        _ext(evidence=True, owned=False)(_h(2.5)),
        _ext(evidence=False, owned=True)(_h(3.75)),
        _ext(evidence=True, owned=True)(_h(4.9)),
        _ext(evidence=True, owned=True)(_h(4.9)),
    ],
}


@pytest.mark.parametrize("case", sorted(_SEQUENCES))
def test_engine_equals_full_reprice(case, cache, capsys, monkeypatch):
    """Each named sequence: identical dicts, evidence, owned entries and
    per-call stderr with and without the engine (shadow verification on).
    Detects a cached run, prefix or fold state answering a call differently
    from the full re-price it replaces."""
    import _cctally_rederive
    monkeypatch.setattr(_cctally_rederive.BlockTotalsEngine, "verify", True)
    calls = _SEQUENCES[case]
    expected, _ = _run(cache, calls, capsys, monkeypatch, engine=False)
    got, engine = _run(cache, calls, capsys, monkeypatch, engine=True)
    for index, (want, have) in enumerate(zip(expected, got, strict=True)):
        assert have == want, f"{case}: call {index} differs"
    assert sum(engine.counts[k] for k in ("extended", "cached")) == len(calls)
    print(f"engine counts {case}: {dict(engine.counts)}")


def test_evidence_and_warnings_are_exercised(cache, capsys, monkeypatch):
    """Non-vacuity of the evidence case: the fixture yields an unpriced
    model, an uncertain model and a one-shot unknown-model warning on the
    first call that prices it, on both paths."""
    calls = _SEQUENCES["evidence_and_owned_toggle"]
    got, _ = _run(cache, calls, capsys, monkeypatch, engine=True)
    evidence = got[-1][0][1]
    assert evidence["unpricedModels"] == [UNKNOWN]
    assert evidence["uncertainModels"] == [AMBIGUOUS]
    warnings = [err.count("[cost] unknown model") for (_, err) in got]
    assert warnings[0] == 1 and sum(warnings) == 2, warnings
    owned = got[-1][0][2]
    assert any(dict(e[1]).get("source_account_key") == "acct-a"
               for e in owned)


def test_bounds_evict_and_fall_back_without_changing_answers(
        cache, capsys, monkeypatch):
    """Spec D1 *Bounds*, with the budgets patched small: runs are evicted
    least recently used (dropping their fold states), states are capped, and
    a run that alone exceeds the entry budget is answered by today's body
    uncached. Every answer still equals the full re-price. Detects an
    eviction that leaves a stale state answering, or a bound that never
    applies."""
    import _cctally_rederive
    engine_cls = _cctally_rederive.BlockTotalsEngine
    monkeypatch.setattr(engine_cls, "verify", True)
    monkeypatch.setattr(engine_cls, "ENTRY_BUDGET", 6)
    monkeypatch.setattr(engine_cls, "MAX_RUNS", 3)
    monkeypatch.setattr(engine_cls, "MAX_STATES", 2)
    calls = []
    for round_ in range(3):
        for offset in (0, 2, 3.5, 4.5):
            start = _h(offset)
            window = (1, start, start + dt.timedelta(hours=5))
            for span in (0.5, 1.25, 2):
                calls.append((start, start + dt.timedelta(hours=span), 1,
                              _ws(window), True, round_ == 1))
    # Three window sets over one run: more fold states than MAX_STATES.
    for round_ in range(2):
        for reset in (5, 4, 3):
            calls.append((S, _h(1.75 + round_), 1, _ws((1, S, _h(reset))),
                          True, False))
    # Oversized: the run at 0h through 6.75h holds 14 entries > 6.
    calls.append((S, _h(6.75), 1, _ws((1, _h(0), _h(7))), True, True))
    calls.append((S, _h(6.75), 1, _ws((1, _h(0), _h(7))), True, True))
    expected, _ = _run(cache, calls, capsys, monkeypatch, engine=False)
    got, engine = _run(cache, calls, capsys, monkeypatch, engine=True)
    assert got == expected
    counts = engine.counts
    assert counts["uncached"] >= 2, dict(counts)
    assert counts["runs_evicted"] > 0, dict(counts)
    assert counts["states_evicted"] > 0, dict(counts)
    assert len(engine._runs) <= 3
    assert len(engine._states) <= 2
    assert engine._cached_entries <= 6
    print(f"engine counts bounds: {dict(counts)}")


def test_many_empty_ranges_keep_at_most_64_runs(cache, capsys, monkeypatch):
    """Spec D1 *Bounds*: an empty range costs no entries but still holds a
    key, so 10,000 distinct empty ranges must leave at most 64 runs (and 64
    fold states) retained, each answer the empty legacy dict. Detects an
    engine whose key count grows with the replay's length."""
    import _cctally_record as rec
    import _cctally_rederive
    far = dt.datetime(2027, 1, 1, tzinfo=UTC)
    with _cctally_rederive._scratch_read_adapters(cache):
        engine = rec._BLOCK_TOTALS_ENGINE
        prev = rec.swap_block_totals_engine(None)
        empty = rec._compute_block_totals(
            far, far + dt.timedelta(minutes=1), owner_key=1,
            windows=_ws((1, far, far + dt.timedelta(hours=5))))
        rec.swap_block_totals_engine(prev)
        for index in range(10_000):
            start = far + dt.timedelta(minutes=index)
            got = rec._compute_block_totals(
                start, start + dt.timedelta(minutes=1), owner_key=1,
                windows=_ws((1, start, start + dt.timedelta(hours=5))))
            assert got == empty
        assert len(engine._runs) <= 64
        assert len(engine._states) <= 64
    assert empty["cost_usd"] == 0.0 and empty["by_model"] == {}
