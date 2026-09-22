"""#620 S2 — the diagnosis never runs inside the dashboard's pinned transaction.

`_tui_build_source_bundle` holds one `cache.db` read transaction across the
whole source build, measured at about 3.4 seconds, and everything folded inside
it pins every intervening WAL frame. The diagnosis is an on-demand read on a
request thread and must stay outside that pin.

A symbol grep plus one patched call cannot prove it: a grep cannot rule out an
alias or a dynamically loaded sibling, and one patched call proves only that
ONE call site behaved. So the boundary itself is instrumented. Two facts make
that instrumentation complete rather than a sample, and the last test in this
file pins both:

  * every diagnosis read goes through `_cctally_diagnosis_sources._execute`,
  * every diagnosis store open goes through `open_read_only`.

Whether a pin is HELD is read off the production cache connections themselves —
`sqlite3.Connection.in_transaction` on the objects `open_cache_db` handed out —
rather than inferred from where we are in the call stack. That is the same
observation `tests/test_snapshot_bounded_work.py` makes about the enumerated
folds, and it is why the non-vacuity guard below can assert that the pin really
was open while the build ran.
"""
from __future__ import annotations

import datetime as dt
import pathlib
import sqlite3
import sys
from dataclasses import dataclass

import pytest

from conftest import load_script, redirect_paths
from test_620_s2_diagnosis_route import _boot, _run_bounded_in_process
from test_620_s2_diagnosis_sources import (
    WINDOW_END, WINDOW_START, _seed_claude, _seed_claude_blocks,
)

from tests._support_http import stop


UTC = dt.timezone.utc
_REPO = pathlib.Path(__file__).resolve().parent.parent
_WINDOW = f"{WINDOW_START.date().isoformat()}..{WINDOW_END.date().isoformat()}"


@dataclass(frozen=True)
class _Observed:
    owner: str          # "cache" (a production cache connection) or "diagnosis"
    while_pinned: bool
    detail: str


def _in_transaction(conn) -> bool:
    try:
        return bool(conn.in_transaction)
    except sqlite3.ProgrammingError:
        # Closed after the build; a closed connection holds no pin.
        return False


def _instrument_store_reads(ns, monkeypatch, record: list):
    """Record every production cache statement and every diagnosis read.

    The cache leg exists for the non-vacuity guard: without it, a build that
    never opened its pin would satisfy "no diagnosis query ran while pinned"
    while asserting nothing at all.
    """
    cache_connections: list = []
    real_open = ns["open_cache_db"]

    def _open(*args, **kwargs):
        conn = real_open(*args, **kwargs)
        cache_connections.append(conn)
        conn.set_trace_callback(
            lambda sql, _c=conn: record.append(
                _Observed("cache", _in_transaction(_c), sql[:60]))
        )
        return conn

    monkeypatch.setitem(ns, "open_cache_db", _open)

    def _pin_held() -> bool:
        return any(_in_transaction(c) for c in cache_connections)

    sources = ns["_load_sibling"]("_cctally_diagnosis_sources")
    real_execute = sources._execute
    real_open_ro = sources.open_read_only

    def _execute(conn, sql, params=()):
        record.append(_Observed("diagnosis", _pin_held(), str(sql)[:60]))
        return real_execute(conn, sql, params)

    def _open_read_only(kind):
        record.append(_Observed("diagnosis", _pin_held(), f"open:{kind}"))
        return real_open_ro(kind)

    monkeypatch.setattr(sources, "_execute", _execute)
    monkeypatch.setattr(sources, "open_read_only", _open_read_only)


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    """A store with enough in it that the pinned build does real work."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.setenv("CCTALLY_AS_OF",
                       WINDOW_END.isoformat().replace("+00:00", "Z"))
    _seed_claude(ns, models=(("claude-opus-4-20250514", 50),
                             ("claude-haiku-4-20250514", 10)))
    _seed_claude_blocks(ns)
    return ns


def _build_authoritative_snapshot(ns):
    """The whole tick, including the `precompute_envelope` tail that is the
    only path reaching `_tui_build_source_bundle` and its pin."""
    return ns["_tui_build_snapshot"](
        now_utc=WINDOW_END, skip_sync=True,
        precompute_envelope=True, runtime_bind="127.0.0.1",
    )


def test_no_diagnosis_query_runs_while_the_cache_pin_is_held(seeded, monkeypatch):
    seen: list[_Observed] = []
    _instrument_store_reads(seeded, monkeypatch, seen)

    _build_authoritative_snapshot(seeded)

    assert any(q.owner == "cache" and q.while_pinned for q in seen), (
        "non-vacuity: the build never held its cache pin, so every "
        "`while_pinned` below is False for a reason that has nothing to do "
        "with the diagnosis")
    offenders = [q for q in seen if q.owner == "diagnosis" and q.while_pinned]
    assert offenders == [], (
        f"the diagnosis read a store inside the pinned transaction: "
        f"{[q.detail for q in offenders]}")


def test_the_snapshot_build_does_not_reach_the_diagnosis_at_all(seeded,
                                                                monkeypatch):
    """Stronger than the pin question, and the reason the pin question stays
    answerable: a tick that never calls the diagnosis cannot pay for it either,
    which is what keeps this an on-demand surface rather than a per-tick cost.
    """
    seen: list[_Observed] = []
    _instrument_store_reads(seeded, monkeypatch, seen)
    _build_authoritative_snapshot(seeded)
    assert [q for q in seen if q.owner == "diagnosis"] == []


def test_the_enumerated_pinned_folds_are_unchanged():
    """No diagnosis fold appears in the snapshot builder's pinned-work bound.

    This is the static half of the guard; the two tests above are the dynamic
    proof, because they observe the reads a request actually makes. This one
    says that nobody has added a diagnosis fold to the enumeration the
    snapshot builder maintains, which would mean the diagnosis had become part
    of per-tick work rather than staying on demand.

    It reads `IN_TRANSACTION_FORBIDDEN` because #617 narrowed the coherent
    cache read pin and, in doing so, removed the `FOLDS_MEASURED_INSIDE_THE_PIN`
    subset this test previously imported. That subset named the folds the
    driver observed running inside the pin; after the narrowing it no longer
    exists, and the surviving enumeration is the bound itself.
    """
    from test_snapshot_bounded_work import IN_TRANSACTION_FORBIDDEN

    enumerated = " ".join(
        f"{module}.{name}" for module, name in IN_TRANSACTION_FORBIDDEN
    )
    assert enumerated, "non-vacuity: the enumeration is empty"
    assert "diagnosis" not in enumerated


def test_the_route_reads_after_the_snapshot_transaction_has_ended(seeded,
                                                                   monkeypatch):
    """The route's own reads, observed rather than assumed.

    The build runs first so the pin has genuinely opened and closed in this
    process; the request then follows, and every diagnosis read it makes must
    fall outside any held pin.
    """
    seen: list[_Observed] = []
    _instrument_store_reads(seeded, monkeypatch, seen)
    _run_bounded_in_process(
        monkeypatch,
        seeded["_load_sibling"]("_cctally_diagnosis_sources"),
    )
    _build_authoritative_snapshot(seeded)
    assert any(q.owner == "cache" and q.while_pinned for q in seen), (
        "non-vacuity: the build never held its cache pin")

    server, thread, client = _boot(seeded)
    try:
        response = client.get(f"/api/diagnosis?window={_WINDOW}")
    finally:
        stop(server, thread)

    assert response.status == 200, response.body
    diagnosis_reads = [q for q in seen if q.owner == "diagnosis"]
    assert diagnosis_reads, (
        "non-vacuity: the route made no instrumented store read, so the "
        "assertion below holds over an empty set")
    assert not any(q.while_pinned for q in diagnosis_reads)


# --- supplementary static checks ---------------------------------------
#
# These are evidence, not proof: they say the shape the instrumentation above
# relies on is still the shape on disk.

def test_the_diagnosis_read_boundary_is_exactly_two_functions():
    """Patching `_execute` and `open_read_only` instruments EVERY read.

    If a second `.execute(` or a second `sqlite3.connect(` appears in the
    adapter, the instrumentation above silently becomes a sample of the reads
    rather than all of them, and the pin question stops being answered.
    """
    source = (_REPO / "bin" / "_cctally_diagnosis_sources.py").read_text()
    assert source.count("sqlite3.connect(") == 1
    # Two: the `PRAGMA busy_timeout` inside `open_read_only`, and the one
    # inside `_execute` itself.
    assert source.count(".execute(") == 2
    assert ".executemany(" not in source
    for other in ("_lib_diagnosis.py", "_cctally_diagnosis.py"):
        text = (_REPO / "bin" / other).read_text()
        assert ".execute(" not in text, f"{other} opened a second read path"
        assert ".executemany(" not in text, f"{other} opened a second read path"
        assert ".executescript(" not in text, f"{other} opened a second read path"
        # The ban is on OPENING a store, not on the name `sqlite3`. A blanket
        # substring ban also refused `except sqlite3.Error`, which is how the
        # CLI narrows its catch around the week-anchor read — and refusing that
        # pushes the code back to `except Exception`, which reports a
        # programming error as an infrastructure failure. Naming an exception
        # type opens nothing; `connect(` is what opens a store.
        assert "sqlite3.connect(" not in text, (
            f"{other} is not supposed to open a store")


def test_static_check_finds_no_diagnosis_symbol_in_the_bundle_module():
    source = (_REPO / "bin" / "_cctally_tui.py").read_text()
    for banned in ("_cctally_diagnosis_sources", "build_diagnosis"):
        assert banned not in source, (
            f"{banned} is reachable from the module that owns the pinned "
            "source-bundle build")


def test_the_dashboard_reaches_the_diagnosis_only_from_the_request_handler():
    """`_cctally_dashboard.py` builds both the snapshot and the routes, so the
    symbol check there is scoped: the diagnosis may be named inside the route
    handler and nowhere else."""
    source = (_REPO / "bin" / "_cctally_dashboard.py").read_text()
    reaching = [line.strip() for line in source.splitlines()
                if "_cctally_diagnosis_sources" in line
                or "build_diagnosis(" in line]
    assert reaching, "non-vacuity: the route must reach the adapter somewhere"
    handler_start = source.index("def _handle_get_diagnosis")
    selector_start = source.index("def _diagnosis_selectors")
    account_start = source.index("def _resolve_diagnosis_account")
    region_start = min(handler_start, selector_start, account_start)
    for line in reaching:
        offset = source.index(line)
        assert offset >= region_start, (
            f"the diagnosis is reached outside the request handler: {line}")
