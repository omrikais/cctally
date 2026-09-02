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

import dataclasses
import datetime as dt
import pathlib
import re
import sqlite3
from zoneinfo import ZoneInfo

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


# --------------------------------------------------------------------------
# #688 — the withholding disclosure rides on the ALERT payload only
# --------------------------------------------------------------------------
CREATED_AT = "2026-08-29T00:00:00+00:00"
WITHHELD_STATUS = "unsupported-model-mix"


def _mrc(ns):
    return ns["_load_sibling"]("_lib_meter_rate_change")


def _ordinary(ns):
    return _mrc(ns).RateChangeTransition(
        provider="claude", account_key="unattributed",
        effective_from=BOUNDARY,
        previous_units_per_point=2_442_620.0,
        new_units_per_point=1_665_096.0,
        severity="alarm", detected_at="2026-08-29T00:00:00+00:00")


def _withheld(ns):
    return dataclasses.replace(_ordinary(ns),
                               withholding_status=WITHHELD_STATUS)


def test_688_alert_payload_always_carries_the_key(ns):
    mrc = _mrc(ns)
    ordinary = mrc.alert_payload(_ordinary(ns))
    assert "withholding_status" in ordinary
    assert ordinary["withholding_status"] is None
    assert mrc.alert_payload(_withheld(ns))["withholding_status"] == \
        WITHHELD_STATUS


def test_688_the_journal_payload_is_unchanged(ns):
    """The guard against reintroducing the divergent-hash hazard.

    Event selection hashes the whole record and quarantines two revision-0
    records that share an id and differ in hash, so a key added here would
    quarantine any re-emission of an already-recorded identity — after a
    calibration reset, a quarantine, or a file deletion followed by
    re-detection.
    """
    mrc = _mrc(ns)
    assert mrc.JOURNAL_IDENTITY_VERSION == 1
    assert set(mrc.event_payload(_withheld(ns), created_at=CREATED_AT)) == {
        "provider", "account_key", "effective_from",
        "previous_units_per_point", "new_units_per_point", "severity",
        "detected_at_utc", "created_at_utc", "journal_identity_version"}
    # Stronger than a key-set check: the two payloads must be EQUAL, so a
    # withheld transition and an ordinary one at the same key hash alike.
    assert mrc.event_payload(_withheld(ns), created_at=CREATED_AT) == \
        mrc.event_payload(_ordinary(ns), created_at=CREATED_AT)


#: The canonical encoding of `event_payload` for `_ordinary` at `CREATED_AT`,
#: and its content hash under `_lib_journal._sha256_canonical`. That is the
#: same hash FUNCTION journal event selection uses, over a different ARGUMENT:
#: selection hashes the whole record, and this hashes the payload alone.
#: Computed once from the shipped implementation and pasted in. Do NOT
#: recompute these from the code under test: a value read back from the
#: implementation pins nothing.
FROZEN_EVENT_JSON = (
    '{"account_key":"unattributed",'
    '"created_at_utc":"2026-08-29T00:00:00+00:00",'
    '"detected_at_utc":"2026-08-29T00:00:00+00:00",'
    '"effective_from":"2026-08-25T00:00:00+00:00",'
    '"journal_identity_version":1,'
    '"new_units_per_point":1665096.0,'
    '"previous_units_per_point":2442620.0,'
    '"provider":"claude",'
    '"severity":"alarm"}'
)
FROZEN_EVENT_HASH = (
    "sha256:317d4fbb5c5e6f79f12cf613067f7d67388c90546a21a7c2711de26854d80120")


def test_689_the_event_bytes_are_frozen_exactly(ns):
    """Acceptance 5 as an EXACT freeze, not a key check.

    `test_688_the_journal_payload_is_unchanged` compares key sets and the
    withheld-versus-ordinary equality, so it would not notice a changed VALUE
    — a renamed severity vocabulary, a reformatted instant, a bumped identity
    version. The family shipped in v1.104.0, so retained v1 events exist in
    the wild: two revision-0 records sharing an id and differing in hash
    quarantine each other, and #689 re-offers already-persisted identities by
    design, so any change to these bytes would quarantine history rather than
    merely alter a future line.
    """
    import json
    import _lib_journal
    mrc = _mrc(ns)
    payload = mrc.event_payload(_ordinary(ns), created_at=CREATED_AT)
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True,
                         ensure_ascii=False)
    assert encoded == FROZEN_EVENT_JSON
    assert _lib_journal._sha256_canonical(payload) == FROZEN_EVENT_HASH


def test_688_the_identity_is_unchanged_by_the_withholding(ns):
    assert _withheld(ns).identity() == _ordinary(ns).identity()


def test_688_severity_is_unchanged_by_the_withholding(ns):
    # Severity is a function of the observed size of the rate move, not of
    # the calibration's confidence.
    mrc = _mrc(ns)
    assert mrc.alert_payload(_withheld(ns))["severity"] == \
        mrc.alert_payload(_ordinary(ns))["severity"] == "alarm"


def test_688_the_alert_text_names_the_withholding_and_promises_no_budget(ns):
    alerts = ns["_load_sibling"]("_cctally_alerts")
    mrc = _mrc(ns)
    _title, _subtitle, body = alerts._alert_text_meter_rate_change(
        mrc.alert_payload(_withheld(ns)), ZoneInfo("UTC"))
    # The INSTRUCTION is what has to go, not the words. Spec section 8 says
    # the body stops telling the reader to run `cctally quota` for a fitted
    # budget when no fitted budget exists; stating that none is available is
    # the correction, so the phrase itself must still be readable.
    #
    # Pinned EXACTLY, like the `_ordinary` sibling below it. Substring
    # assertions were written here first and are too weak to hold that rule:
    # a rewrite to "Run `cctally quota` to obtain your fitted budget"
    # contains none of the phrases they forbade, so it passed while
    # reintroducing the very instruction section 8 bans.
    assert body == (
        "Effective 2026-08-25. The calibration was withheld "
        f"({WITHHELD_STATUS}), so no fitted budget is available. "
        "Run `cctally quota` for the evidence."), body


def test_688_the_ordinary_alert_text_is_unchanged(ns):
    alerts = ns["_load_sibling"]("_cctally_alerts")
    mrc = _mrc(ns)
    _t, _s, body = alerts._alert_text_meter_rate_change(
        mrc.alert_payload(_ordinary(ns)), ZoneInfo("UTC"))
    assert body == ("Effective 2026-08-25. Run `cctally quota` for the "
                    "fitted budget and its evidence.")


def test_688_the_title_and_subtitle_are_unchanged_by_the_withholding(ns):
    alerts = ns["_load_sibling"]("_cctally_alerts")
    mrc = _mrc(ns)
    withheld = alerts._alert_text_meter_rate_change(
        mrc.alert_payload(_withheld(ns)), ZoneInfo("UTC"))
    ordinary = alerts._alert_text_meter_rate_change(
        mrc.alert_payload(_ordinary(ns)), ZoneInfo("UTC"))
    assert withheld[:2] == ordinary[:2]
