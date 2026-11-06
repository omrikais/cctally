"""#620 S2 E2 — a pure fold is lifted out of `_compute_block_totals`.

`_compute_block_totals` is not currently a post-load fold: it loads entries
itself through `get_claude_session_entries`, prices each one, and accumulates
in the same loop. Its loader uses a CLOSED `>= start AND <= end` interval
that `tests/test_migration_009_boundary_inclusive.py` pins deliberately.

E2 lifts out only the arithmetic. The wrapper keeps its signature, its
loader, that closed interval, its ordering, its summation and every caller,
and the diagnosis separately loads its own half-open account-scoped window
and calls the same fold.
"""
from __future__ import annotations

import datetime as dt

import pytest

from conftest import load_script


UTC = dt.timezone.utc


def _record():
    return load_script()["_cctally_record"]


def _priced(rec, *, model="opus", project="/a", cost=1.0,
            input_tokens=0, output_tokens=0,
            cache_create_tokens=0, cache_read_tokens=0):
    return rec.PricedEntry(
        model=model,
        project_path=project,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_tokens=cache_create_tokens,
        cache_read_tokens=cache_read_tokens,
        cost_usd=cost,
    )


def test_fold_is_pure_and_buckets_by_model_and_project():
    rec = _record()
    entries = [
        _priced(rec, model="opus", project="/a", cost=1.0),
        _priced(rec, model="opus", project="/b", cost=2.0),
        _priced(rec, model="haiku", project="/a", cost=0.5),
    ]
    totals = rec.fold_block_totals(entries)
    assert totals.cost_usd == pytest.approx(3.5)
    assert totals.entry_count == 3
    assert totals.by_model["opus"].cost_usd == pytest.approx(3.0)
    assert totals.by_model["opus"].entry_count == 2
    assert totals.by_project["/a"].cost_usd == pytest.approx(1.5)


def test_unknown_project_falls_back_to_the_existing_sentinel():
    rec = _record()
    totals = rec.fold_block_totals(
        [_priced(rec, model="opus", project=None, cost=1.0)]
    )
    assert "(unknown)" in totals.by_project


def test_token_columns_are_summed_per_bucket_and_overall():
    rec = _record()
    totals = rec.fold_block_totals([
        _priced(rec, model="opus", project="/a", cost=0.0,
                input_tokens=1, output_tokens=2,
                cache_create_tokens=3, cache_read_tokens=4),
        _priced(rec, model="opus", project="/b", cost=0.0,
                input_tokens=10, output_tokens=20,
                cache_create_tokens=30, cache_read_tokens=40),
    ])
    assert (totals.input_tokens, totals.output_tokens,
            totals.cache_create_tokens, totals.cache_read_tokens) == (
        11, 22, 33, 44)
    assert totals.by_project["/b"].cache_read_tokens == 40


def test_bucket_insertion_order_follows_entry_order():
    """The wrapper's returned dict order is observable; the fold must not
    reorder it."""
    rec = _record()
    totals = rec.fold_block_totals([
        _priced(rec, model="zeta", project="/z"),
        _priced(rec, model="alpha", project="/a"),
    ])
    assert list(totals.by_model) == ["zeta", "alpha"]
    assert list(totals.by_project) == ["/z", "/a"]


def test_the_fold_opens_no_store():
    """A fold that still loads is not an extraction."""
    import inspect

    rec = _record()
    src = inspect.getsource(rec.fold_block_totals)
    for banned in ("get_claude_session_entries", "sqlite3", "_calculate_entry_cost"):
        assert banned not in src, f"the fold must not reference {banned}"


def test_empty_input_folds_to_a_zero_total():
    rec = _record()
    totals = rec.fold_block_totals([])
    assert totals.cost_usd == 0.0
    assert totals.entry_count == 0
    assert totals.by_model == {}
    assert totals.by_project == {}


# --- the wrapper's contract is unchanged --------------------------------


def _owned_window(ns, key, start, reset):
    """One competing-window entry in the #751a ownership context."""
    return ns["_lib_blocks"].OwnedWindow(key=key, start=start, reset=reset)


class _FakeJoined:
    def __init__(self, model, project_path, cost_usd,
                 timestamp=dt.datetime(2026, 8, 1, 1, tzinfo=UTC)):
        self.model = model
        self.project_path = project_path
        # #751a: ownership reads the timestamp off every loaded entry.
        self.timestamp = timestamp
        self.input_tokens = 1
        self.output_tokens = 2
        self.cache_creation_tokens = 3
        self.cache_read_tokens = 4
        self.cost_usd = cost_usd
        self.cache_1h_tokens = None
        self.speed = None


def test_compute_block_totals_still_returns_the_legacy_dict_shape(monkeypatch):
    """Every caller indexes this dict; the key set and its order stay put."""
    ns = load_script()
    rec = ns["_cctally_record"]
    rows = [
        _FakeJoined("claude-opus-4-20250514", "/a", 1.0),
        _FakeJoined("claude-opus-4-20250514", "/b", 2.0),
    ]
    monkeypatch.setitem(ns, "get_claude_session_entries",
                        lambda *a, **k: list(rows))
    out = rec._compute_block_totals(
        dt.datetime(2026, 8, 1, tzinfo=UTC),
        dt.datetime(2026, 8, 1, 5, tzinfo=UTC),
        owner_key=1,
        windows=[_owned_window(
            ns, 1,
            dt.datetime(2026, 8, 1, tzinfo=UTC),
            dt.datetime(2026, 8, 1, 5, tzinfo=UTC),
        )],
    )
    assert isinstance(out, dict)
    assert list(out) == [
        "input_tokens", "output_tokens", "cache_create_tokens",
        "cache_read_tokens", "cost_usd", "by_model", "by_project",
    ]
    assert list(out["by_model"]["claude-opus-4-20250514"]) == [
        "input_tokens", "output_tokens", "cache_create_tokens",
        "cache_read_tokens", "cost_usd", "entry_count",
    ]
    assert out["cost_usd"] == pytest.approx(3.0)
    assert out["by_project"]["/a"]["entry_count"] == 1
    assert out["input_tokens"] == 2


def test_compute_block_totals_keeps_its_closed_interval_loader(monkeypatch):
    """`tests/test_migration_009_boundary_inclusive.py` pins the closed
    `>= start AND <= end` interval; the extraction must not touch it."""
    ns = load_script()
    rec = ns["_cctally_record"]
    seen = {}

    def _spy(range_start, range_end, **kwargs):
        seen["args"] = (range_start, range_end, kwargs)
        return []

    monkeypatch.setitem(ns, "get_claude_session_entries", _spy)
    start = dt.datetime(2026, 8, 1, tzinfo=UTC)
    end = dt.datetime(2026, 8, 1, 5, tzinfo=UTC)
    rec._compute_block_totals(
        start, end, skip_sync=True,
        owner_key=1, windows=[_owned_window(ns, 1, start, end)],
    )
    assert seen["args"][0] == start
    assert seen["args"][1] == end
    assert seen["args"][2] == {"skip_sync": True}


def test_the_fold_signature_has_resolvable_annotations():
    """`typing.get_type_hints` must not raise on the lifted fold.

    The annotation is a string, so an unimported name inside it is invisible
    until something resolves the hints — a dataclass, a serializer, a doc
    tool, or `get_type_hints` itself, which is exactly where it raised
    `NameError: name 'Iterable' is not defined`.
    """
    import typing

    rec = _record()
    hints = typing.get_type_hints(rec.fold_block_totals)
    assert "entries" in hints


# --- #1003 D1: the shared fold step and the engine seam -----------------


def _float_bits(value):
    """A float by its exact bits, so a reordered addition cannot pass."""
    return value.hex() if isinstance(value, float) else value


def _totals_bits(totals) -> tuple:
    """Every top-level and bucket value by exact float bits, buckets in
    insertion order."""
    def bucket(b):
        return tuple(_float_bits(getattr(b, name)) for name in (
            "input_tokens", "output_tokens", "cache_create_tokens",
            "cache_read_tokens", "cost_usd", "entry_count"))
    return (
        bucket(totals),
        tuple((key, bucket(b)) for key, b in totals.by_model.items()),
        tuple((key, bucket(b)) for key, b in totals.by_project.items()),
    )


def test_add_priced_is_the_folds_exact_per_entry_step():
    """#1003 D1: the incremental engine folds one entry at a time through
    `BlockTotals.add_priced`, and its equality proof needs that step to be
    the fold's own. Detects an engine total that differs from the live
    total by float summation order or bucket order: a manual `add_priced`
    loop must reproduce `fold_block_totals` bit for bit."""
    rec = _record()
    entries = [
        _priced(rec, model="zeta", project=None, cost=0.1,
                input_tokens=3, output_tokens=5,
                cache_create_tokens=7, cache_read_tokens=11),
        _priced(rec, model="alpha", project="/b", cost=0.2),
        _priced(rec, model="zeta", project="/a", cost=1e-17,
                input_tokens=1),
        _priced(rec, model="alpha", project="/b", cost=0.3),
    ]
    folded = rec.fold_block_totals(entries)
    manual = rec.BlockTotals()
    for e in entries:
        manual.add_priced(
            e.model, e.project_path, e.input_tokens, e.output_tokens,
            e.cache_creation_tokens, e.cache_read_tokens, e.cost_usd)
    assert _totals_bits(manual) == _totals_bits(folded)
    assert manual.as_legacy_dict() == folded.as_legacy_dict()
    assert list(manual.by_project) == ["(unknown)", "/b", "/a"]


class _StubEngine:
    def __init__(self):
        self.calls = []

    def totals(self, block_start_at, range_end, **kwargs):
        self.calls.append((block_start_at, range_end, kwargs))
        return {"stub": True}


def test_installed_engine_answers_compute_block_totals(monkeypatch):
    """#1003 D1 seam: with an engine in the slot `_compute_block_totals`
    returns the engine's answer and never loads entries itself; the
    windows reach the engine materialized as a list. Detects a seam that
    still runs the full re-price (the replay cost #1003 removes) or that
    drops an argument the engine needs."""
    ns = load_script()
    rec = ns["_cctally_record"]

    def _no_load(*a, **k):
        raise AssertionError("the engine path must not load entries")

    monkeypatch.setitem(ns, "get_claude_session_entries", _no_load)
    stub = _StubEngine()
    start = dt.datetime(2026, 8, 1, tzinfo=UTC)
    end = dt.datetime(2026, 8, 1, 5, tzinfo=UTC)
    window = _owned_window(ns, 1, start, end)
    evidence: dict = {}
    owned: list = []
    previous = rec.swap_block_totals_engine(stub)
    try:
        out = rec._compute_block_totals(
            start, end, owner_key=1, windows=iter([window]),
            skip_sync=True, pricing_evidence=evidence, owned_entries=owned)
    finally:
        assert rec.swap_block_totals_engine(previous) is stub
    assert previous is None
    assert out == {"stub": True}
    [(got_start, got_end, kwargs)] = stub.calls
    assert (got_start, got_end) == (start, end)
    assert kwargs["owner_key"] == 1
    assert kwargs["windows"] == [window]
    assert kwargs["pricing_evidence"] is evidence
    assert kwargs["owned_entries"] is owned


def test_owner_check_runs_before_the_engine():
    """#1003 D1: the owner-key check stays ahead of the seam, so a caller
    passing a context without its own window is refused on both paths
    rather than priced by the engine."""
    ns = load_script()
    rec = ns["_cctally_record"]
    stub = _StubEngine()
    start = dt.datetime(2026, 8, 1, tzinfo=UTC)
    end = dt.datetime(2026, 8, 1, 5, tzinfo=UTC)
    previous = rec.swap_block_totals_engine(stub)
    try:
        with pytest.raises(ValueError, match="absent from"):
            rec._compute_block_totals(
                start, end, owner_key=2,
                windows=[_owned_window(ns, 1, start, end)])
    finally:
        rec.swap_block_totals_engine(previous)
    assert stub.calls == []
