"""#661 S2 Task C3 — the non-threshold wire variant and its PY/TS parity (§6.1).

Every current alert axis is a numeric threshold axis. The dashboard's
`AlertAxis` is a closed six-member union, `AlertEntry` requires a numeric
`threshold`, its `id` is threshold-shaped, and `alert_row_owner` deliberately
RAISES on a seventh axis so that adding one without deciding its ownership
fails a test rather than shipping an invisible row.

A rate transition has no percentage threshold and no threshold-derived
severity. It is therefore a separate event family with its own discriminated
wire variant, carried in its own array, and this module pins that: the family
is not in the registry, the seventh-axis guard still raises, and the Python
builder's keys and the TypeScript interface's fields agree exactly.
"""
from __future__ import annotations

import datetime as dt
import pathlib
import re
import sqlite3

import pytest

from conftest import load_script, redirect_paths

ROOT = pathlib.Path(__file__).resolve().parents[1]
ENVELOPE_TS = ROOT / "dashboard" / "web" / "src" / "types" / "envelope.ts"

UTC = dt.timezone.utc
BOUNDARY = "2026-08-25T00:00:00+00:00"


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.setenv("CCTALLY_DISABLE_UPDATE_CHECK", "1")
    monkeypatch.setenv("CCTALLY_DISABLE_TELEMETRY", "1")
    return ns


def _seed(conn, *, provider="claude", account_key="unattributed",
          effective_from=BOUNDARY, previous=2_442_620.0, new=1_685_000.0,
          severity="alarm"):
    conn.execute(
        "INSERT INTO meter_rate_change_events "
        "(provider, account_key, effective_from, previous_units_per_point,"
        " new_units_per_point, severity, detected_at_utc, created_at_utc) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (provider, account_key, effective_from, previous, new, severity,
         "2026-08-29T00:00:00+00:00", "2026-08-29T00:00:00+00:00"))
    conn.commit()


def _rows(ns):
    conn = ns["open_db"]()
    try:
        _seed(conn)
        return ns["_build_meter_rate_change_array"](conn)
    finally:
        conn.close()


# --------------------------------------------------------------------------
# It is not an axis (§6.1)
# --------------------------------------------------------------------------
def test_c3_is_not_an_axis_registry_member(ns):
    assert "meter_rate_change" not in {a.id for a in ns["AXIS_REGISTRY"]}


def test_c3_alert_row_owner_still_raises_on_an_unknown_axis(ns):
    """The guard that makes a seventh axis fail a test rather than ship an
    invisible row. It must still raise, and it must raise for THIS family
    too — which is the proof that the family really did stay outside."""
    tui = ns["_load_sibling"]("_cctally_tui")
    with pytest.raises(Exception):
        tui.alert_row_owner("some_new_axis", None, None)
    with pytest.raises(Exception):
        tui.alert_row_owner("meter_rate_change", None, None)


def test_c3_the_row_carries_no_threshold(ns):
    """`AlertEntry` requires a numeric threshold. A row carrying one would
    be indistinguishable from an axis member to every consumer that reads
    the field, and the notifier would re-derive severity from it."""
    row = _rows(ns)[0]
    assert "threshold" not in row


# --------------------------------------------------------------------------
# The row's own content
# --------------------------------------------------------------------------
def test_c3_the_row_carries_severity_both_rates_and_the_instant(ns):
    row = _rows(ns)[0]
    assert row["family"] == "meter_rate_change"
    assert row["severity"] == "alarm"
    assert row["previous_units_per_point"] == pytest.approx(2_442_620.0)
    assert row["new_units_per_point"] == pytest.approx(1_685_000.0)
    assert row["effective_from"] == BOUNDARY
    assert row["owner"] == "claude"


def test_c3_a_single_account_store_carries_no_decoration(ns):
    """R8: nothing decorates at one real account, exactly as the alert rows
    behave."""
    row = _rows(ns)[0]
    assert "accountKey" not in row
    assert "accountLabel" not in row


def test_c3_rows_are_newest_first_by_parsed_instant(ns):
    conn = ns["open_db"]()
    try:
        _seed(conn, effective_from="2026-07-01T00:00:00+00:00")
        _seed(conn, effective_from="2026-08-25T02:00:00+02:00")
        rows = ns["_build_meter_rate_change_array"](conn)
    finally:
        conn.close()
    assert [r["effective_from"] for r in rows] == [
        "2026-08-25T02:00:00+02:00", "2026-07-01T00:00:00+00:00"], (
        "the order compares SPELLINGS rather than instants")


def test_c3_a_store_without_the_table_publishes_an_empty_array(ns):
    """An install whose epoch-1011 rebuild has not run yet — the rebuild is
    deferred to a background worker — must render "no change recorded"
    rather than emptying the whole envelope."""
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE accounts (account_key TEXT,"
                     " provider TEXT, email TEXT, label TEXT)")
        assert ns["_build_meter_rate_change_array"](conn) == []
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Python-to-TypeScript parity
# --------------------------------------------------------------------------
def _ts_interface_fields(name: str) -> set:
    """Field names declared on one exported TypeScript interface.

    Parsed rather than transcribed: a hand-copied list is a third copy that
    nothing checks, which is the failure this test exists to prevent.
    """
    source = ENVELOPE_TS.read_text(encoding="utf-8")
    match = re.search(
        r"export interface " + re.escape(name) + r"\s*\{(.*?)\n\}",
        source, re.S)
    assert match is not None, f"{name} is not declared in {ENVELOPE_TS}"
    body = match.group(1)
    # Strip line comments so a commented-out field name cannot count.
    body = re.sub(r"//[^\n]*", "", body)
    return set(re.findall(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\??\s*:", body,
                          re.M))


def test_c3_python_and_typescript_agree_on_the_variant_fields(ns):
    """The whole point of a discriminated variant is that both ends agree.

    The optional `accountKey`/`accountLabel` pair is present in TypeScript
    and absent from an undecorated Python row by design (R8), so the
    comparison is over a DECORATED row, where every field is emitted.
    """
    conn = ns["open_db"]()
    try:
        conn.execute(
            "INSERT INTO accounts (account_key, provider, email, label)"
            " VALUES ('a1','claude','a@x','A'), ('a2','claude','b@x','B')")
        _seed(conn, account_key="a1")
        rows = ns["_build_meter_rate_change_array"](conn)
    finally:
        conn.close()
    assert rows and "accountKey" in rows[0], (
        "the decorated fixture did not decorate, so the comparison below "
        "would be over a partial row")
    assert set(rows[0]) == _ts_interface_fields("MeterRateChangeEntry")


def test_c3_the_settings_mirror_agrees_too(ns):
    """§6.2's toggle is mirrored to Settings, and the mirror must be a key
    the client's own type declares."""
    assert "rate_change_enabled" in _ts_interface_fields(
        "AlertsSettingsEnvelope")
    block = ns["_get_alerts_config"]({})
    assert block["rate_change_enabled"] is False


def test_c3_the_envelope_declares_the_array_as_optional(ns):
    """A dashboard tab can outlive a server restart through `execvp`, so a
    new client meeting an older server must tolerate the array's absence."""
    source = ENVELOPE_TS.read_text(encoding="utf-8")
    assert "meter_rate_changes?: MeterRateChangeEntry[];" in source
