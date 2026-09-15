"""#761 residual 1 — the manual credit's suppression lists must be canonical.

`_apply_credit`'s ingest path captures the doomed stale-replica snapshots, and
under `--force` also the week's old command-owned synthetics and its old credit
floors, with three `SELECT`s that carry no `ORDER BY`. Nothing sorted the
results afterwards, so the `wce:<op id>` payload depended on SQLite's row order.

That matters because the `wce` id names the operation and carries no payload
digest of its own. Two runs of one operation that produced differently ordered
lists would emit two different payloads under one id, and
`_classify_live_effective_event` withholds the second as a conflict — leaving
its DELETE standing as an inline-only effect a rebuild undoes.

Both paths canonicalize inside `_doomed_snapshot_rows`, whose suppression
projection is `sorted(set(...))` (#834 S1, #835; before that the automatic path
canonicalized through its own capture's `ORDER BY journal_id`). This module
asserts the manual path's widened list agrees, over rows whose insertion order
differs from their identifier order in the FIRST position, so an unordered
capture cannot accidentally pass: the doomed snapshots are seeded `c, a, b` and
the old floors in full reverse.
"""
from __future__ import annotations

import json
import types

import pytest

from conftest import load_script, redirect_paths


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


_AS_OF = "2026-01-04T09:00:05Z"
#: Deliberately not in identifier order: inserted as written, `sa:seed-c` gets
#: the lowest rowid and would lead an unordered capture.
_DOOMED = ("sa:seed-c", "sa:seed-a", "sa:seed-b")
_OLD_FLOORS = ("wcf:seed-z", "wcf:seed-m", "wcf:seed-b")


def _plan():
    return types.SimpleNamespace(
        week_start_date="2026-01-01",
        week_start_at="2026-01-01T00:00:00+00:00",
        week_end_at="2026-01-07T23:59:59+00:00",
        from_pct=60.0,
        from_source="hwm",
        to_pct=40.0,
        effective_iso="2026-01-04T09:00:00+00:00",
        captured_iso="2026-01-04T09:00:05Z",
    )


def _seed_snapshot(conn, *, journal_id, percent, source="test", captured=None):
    conn.execute(
        "INSERT INTO weekly_usage_snapshots "
        "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
        " week_end_at, weekly_percent, source, payload_json, journal_id) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (captured or "2026-01-04T09:00:03Z", "2026-01-01", "2026-01-07",
         "2026-01-01T00:00:00+00:00", "2026-01-07T23:59:59+00:00", percent,
         source, "{}", journal_id),
    )


def _seed_floor(conn, *, journal_id, effective_at):
    conn.execute(
        "INSERT INTO weekly_credit_floors "
        "(week_start_date, effective_at_utc, observed_pre_credit_pct, "
        " applied_at_utc, journal_id) VALUES (?,?,?,?,?)",
        ("2026-01-01", effective_at, 60.0, "2026-01-03T00:00:00Z", journal_id),
    )


def _wce_payload(ns, *, order, forced):
    """Run one manual credit over `order` and return its `wce` payload."""
    import _cctally_journal as jr
    J = __import__("_lib_journal")

    conn = ns["open_db"]()
    try:
        for journal_id in order:
            _seed_snapshot(conn, journal_id=journal_id, percent=60.0)
        if forced:
            for index, journal_id in enumerate(_OLD_FLOORS):
                _seed_floor(
                    conn, journal_id=journal_id,
                    effective_at=f"2026-01-0{index + 1}T00:00:00+00:00",
                )
            for journal_id in ("sa:old-syn-c", "sa:old-syn-a"):
                _seed_snapshot(
                    conn, journal_id=journal_id, percent=12.0,
                    source="record-credit", captured="2026-01-02T00:00:00Z",
                )
        conn.commit()
        ctx = jr.IngestContext(conn=conn, batch=[])
        ns["_apply_credit"](
            conn, _plan(), ctx=ctx, id_base="o:opid", as_of=_AS_OF,
            commit=False, forced=forced,
        )
    finally:
        conn.close()

    import _cctally_core
    payloads = []
    for segment in jr.list_segments():
        for raw in (_cctally_core.JOURNAL_DIR / segment).read_bytes().splitlines():
            if not raw.strip():
                continue
            record = J.decode_line(raw)
            if record is not None and record.get("t") == "evt" and record["id"] == "wce:o:opid":
                payloads.append(record["payload"])
    assert len(payloads) == 1, payloads
    return payloads[0]


def test_the_doomed_capture_is_sorted_and_unique(ns):
    payload = _wce_payload(ns, order=_DOOMED, forced=False)
    assert payload["suppression"] == sorted(_DOOMED)


def test_two_runs_over_differently_ordered_rows_emit_identical_payloads(
    ns, monkeypatch, tmp_path
):
    """The payload must be a pure function of the operation, not of row order."""
    forward = _wce_payload(ns, order=_DOOMED, forced=False)

    other = load_script()
    redirect_paths(other, monkeypatch, tmp_path / "second")
    reversed_run = _wce_payload(other, order=tuple(reversed(_DOOMED)), forced=False)

    assert json.dumps(forward, sort_keys=True) == json.dumps(
        reversed_run, sort_keys=True
    ), "the wce payload still depends on SQLite row order"


def test_the_forced_captures_are_canonical_too(ns):
    """`--force` widens both lists, and `floor_suppression` is one of them."""
    payload = _wce_payload(ns, order=_DOOMED, forced=True)
    assert payload["floor_suppression"] == sorted(_OLD_FLOORS)
    assert payload["suppression"] == sorted(
        set(_DOOMED) | {"sa:old-syn-c", "sa:old-syn-a"}
    )
