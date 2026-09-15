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
