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
          severity="alarm", withholding_status=None,
          detector_input_causes=None, composition_provenance=None,
          baseline_withheld_days=None):
    """One stored row. The four #690 evidence columns default to NULL, which
    is the legacy state a v1 record materializes."""
    conn.execute(
        "INSERT INTO meter_rate_change_events "
        "(provider, account_key, effective_from, previous_units_per_point,"
        " new_units_per_point, severity, detected_at_utc, created_at_utc,"
        " withholding_status, detector_input_causes, composition_provenance,"
        " baseline_withheld_days) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (provider, account_key, effective_from, previous, new, severity,
         "2026-08-29T00:00:00+00:00", "2026-08-29T00:00:00+00:00",
         withholding_status, detector_input_causes, composition_provenance,
         baseline_withheld_days))
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
# #747 — the severity vocabulary is a VALUE, and both languages declare it
# --------------------------------------------------------------------------
def _ts_severity_tuple() -> tuple:
    """The ordered members of `RATE_CHANGE_SEVERITIES` in envelope.ts.

    Parsed rather than duplicated, for the same reason `_ts_interface_fields`
    is: a copy here would agree with itself forever and stop reporting drift,
    which is the whole defect #747 describes.
    """
    source = ENVELOPE_TS.read_text(encoding="utf-8")
    match = re.search(
        r"export const RATE_CHANGE_SEVERITIES\s*=\s*\[(.*?)\]\s*as const",
        source, re.S)
    assert match is not None, (
        f"RATE_CHANGE_SEVERITIES is not declared in {ENVELOPE_TS}")
    return tuple(re.findall(r"'([^']+)'", match.group(1)))


def test_747_every_transition_severity_is_a_declared_member(ns):
    """The kernel's codomain, not a sample of it.

    Walks the ratio space either side of every boundary `transition_severity`
    branches on — the non-positive guard, equality, the notable-drop fraction
    and the alarm-drop fraction — so a new branch returning a fourth token
    fails here rather than reaching a toast with no border rule.
    """
    mrc = _mrc(ns)
    seen = set()
    for previous, new in [
            (1.0, 1.0), (1.0, 1.01), (1.0, 1.2), (1.0, 1.5), (1.0, 2.0),
            (1.0, 5.0), (1.0, 0.99), (1.0, 0.5), (1.0, 0.1),
            (2.0, 1.0), (0.5, 1.0), (100.0, 1.0), (1.0, 100.0),
            (0.0, 1.0), (1.0, 0.0), (-1.0, 1.0), (1.0, -1.0)]:
        seen.add(mrc.transition_severity(previous, new))
    assert seen <= set(mrc.RATE_CHANGE_SEVERITIES), (
        f"transition_severity returned "
        f"{sorted(seen - set(mrc.RATE_CHANGE_SEVERITIES))}, which no "
        "TypeScript border rule can match")
    assert seen, "the probe produced no severities at all"


def test_747_python_and_typescript_declare_the_same_ordered_vocabulary(ns):
    """Ordered equality, because the TypeScript type derives from the tuple.

    `RateChangeSeverity` is `(typeof RATE_CHANGE_SEVERITIES)[number]`, and
    `toastSeverityCoverage.test.ts` walks that same tuple to prove every
    member has a reachable border rule. Nothing connected the two halves
    before this, so a fourth token added on either side was invisible to the
    other.
    """
    mrc = _mrc(ns)
    assert tuple(mrc.RATE_CHANGE_SEVERITIES) == _ts_severity_tuple()


def test_747_the_envelope_clamps_an_out_of_vocabulary_stored_severity(ns):
    """A corrupt row renders at `info`; it does not break the envelope.

    The stats column is unconstrained, so this is reachable without a code
    defect — an older writer, a hand-repaired row, or a future kernel. The
    notifier path already applies this rule; clamping here applies the same
    one at the second site rather than introducing a third.
    """
    conn = ns["open_db"]()
    try:
        _seed(conn, severity="chartreuse")
        rows = ns["_build_meter_rate_change_array"](conn)
    finally:
        conn.close()
    assert rows, "the fixture produced no rows, so the clamp was never reached"
    assert rows[0]["severity"] == "info"


def test_747_the_envelope_leaves_a_declared_severity_verbatim(ns):
    """The clamp must not flatten the vocabulary it is guarding.

    A clamp written against an empty or unresolved tuple would send every row
    to `info` and still pass the test above, so the in-vocabulary direction is
    pinned beside it.
    """
    conn = ns["open_db"]()
    try:
        _seed(conn, severity="warn")
        rows = ns["_build_meter_rate_change_array"](conn)
    finally:
        conn.close()
    assert rows and rows[0]["severity"] == "warn"


def _dispatch_rate_change(ns, severity):
    """Dispatch one rate-change payload and return its `alerts.log` fields.

    The log's seventh column is the severity the notifier settled on, and the
    same value picks the notify-send urgency token, so it is the observable
    for what the dispatch path did with the vocabulary.
    """
    import _cctally_core
    alerts = ns["_load_sibling"]("_cctally_alerts")
    alerts._dispatch_alert_notification(
        {"axis": "meter_rate_change", "severity": severity,
         "provider": "claude", "account_key": "*",
         "effective_from": BOUNDARY,
         "previous_units_per_point": 2_442_620.0,
         "new_units_per_point": 1_685_000.0},
        popen_factory=lambda *a, **k: None, mode="test")
    lines = [line for line in
             (_cctally_core.LOG_DIR / "alerts.log").read_text().splitlines()
             if line.strip()]
    assert len(lines) == 1, lines
    return lines[0].split("\t")


def test_747_the_notifier_resolves_the_vocabulary_rather_than_restating_it(
        ns, monkeypatch):
    """A coordinated fourth member must survive the notifier with its token.

    The envelope resolves this vocabulary from `RATE_CHANGE_SEVERITIES`. The
    notifier restated it as a literal tuple, so a fourth member added to the
    kernel, to the TypeScript tuple and to the CSS passed every parity test
    and was still clamped to `info` here — putting the wrong urgency on the OS
    notification and the wrong token in the trailing `alerts.log` column while
    the suite stayed green. That is #747's defect at a second site, so both
    sites must read the one constant.
    """
    alerts = ns["_load_sibling"]("_cctally_alerts")
    mrc = alerts._load_lib("_lib_meter_rate_change")
    monkeypatch.setattr(
        mrc, "RATE_CHANGE_SEVERITIES",
        tuple(mrc.RATE_CHANGE_SEVERITIES) + ("emergency",))
    assert _dispatch_rate_change(ns, "emergency")[6] == "emergency"


def test_747_the_notifier_still_clamps_a_token_outside_the_vocabulary(ns):
    """The other direction, so the resolution is not simply no clamp at all.

    A stored or hand-repaired row can carry any string, and `severity_to_
    urgency` answers `normal` for an unknown token rather than raising, so
    nothing downstream would report it.
    """
    assert _dispatch_rate_change(ns, "chartreuse")[6] == "info"


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


FROZEN_EVENT_JSON_V2_WITHHELD = (
    '{"account_key":"unattributed",'
    '"baseline_withheld_days":3,'
    '"composition_provenance":"[\\"forecast-aggregate\\"]",'
    '"created_at_utc":"2026-08-29T00:00:00+00:00",'
    '"detected_at_utc":"2026-08-29T00:00:00+00:00",'
    '"detector_input_causes":"[]",'
    '"effective_from":"2026-08-25T00:00:00+00:00",'
    '"journal_identity_version":1,'
    '"new_units_per_point":1665096.0,'
    '"payload_version":2,'
    '"previous_units_per_point":2442620.0,'
    '"provider":"claude",'
    '"severity":"alarm",'
    '"withholding_status":"unsupported-model-mix"}'
)
FROZEN_EVENT_HASH_V2_WITHHELD = (
    "sha256:04140c98b9088cf0f3473904ea3bf3c467bf8d1c8ab6bbf99c7e5b8c3e4ff35c")

FROZEN_EVENT_JSON_V2_ORDINARY = (
    '{"account_key":"unattributed",'
    '"baseline_withheld_days":null,'
    '"composition_provenance":null,'
    '"created_at_utc":"2026-08-29T00:00:00+00:00",'
    '"detected_at_utc":"2026-08-29T00:00:00+00:00",'
    '"detector_input_causes":null,'
    '"effective_from":"2026-08-25T00:00:00+00:00",'
    '"journal_identity_version":1,'
    '"new_units_per_point":1665096.0,'
    '"payload_version":2,'
    '"previous_units_per_point":2442620.0,'
    '"provider":"claude",'
    '"severity":"alarm",'
    '"withholding_status":null}'
)
FROZEN_EVENT_HASH_V2_ORDINARY = (
    "sha256:dc19998172f419c031e1390d73a764d070f2e4aba84660b36d2bf96098e586e3")


def _v2_withheld(ns):
    """A transition carrying all four evidence values, so the freeze below
    covers every key rather than only the four nulls."""
    return dataclasses.replace(
        _ordinary(ns), withholding_status=WITHHELD_STATUS,
        detector_input_causes="[]",
        composition_provenance='["forecast-aggregate"]',
        baseline_withheld_days=3)


def test_690_the_v2_event_bytes_are_frozen_exactly(ns):
    """The v2 payload acquires the v1 payload's hazard the moment it ships.

    `test_689_the_event_bytes_are_frozen_exactly` pins v1 because two
    revision-0 records sharing an id and differing in hash quarantine each
    other, and #689 re-offers already-persisted identities by design — so a
    key added later would quarantine retained history rather than merely
    alter a future line. v2 is now the emitted payload for every new
    transition, so retained v2 events exist from this release forward and the
    same reasoning applies to it unchanged.

    BOTH shapes are pinned, because the nulled-not-dropped rule is only
    visible on the ordinary one: a change that dropped the four keys when
    they carry no value would leave the withheld bytes untouched.
    """
    import json
    import _lib_journal
    mrc = _mrc(ns)
    for transition, expected_json, expected_hash in (
            (_v2_withheld(ns), FROZEN_EVENT_JSON_V2_WITHHELD,
             FROZEN_EVENT_HASH_V2_WITHHELD),
            (_ordinary(ns), FROZEN_EVENT_JSON_V2_ORDINARY,
             FROZEN_EVENT_HASH_V2_ORDINARY)):
        payload = mrc.event_payload_v2(transition, created_at=CREATED_AT)
        encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True,
                             ensure_ascii=False)
        assert encoded == expected_json
        assert _lib_journal._sha256_canonical(payload) == expected_hash


def test_690_the_v2_payload_key_set_is_the_v1_set_plus_five(ns):
    """Named separately from the byte freeze so a key addition reports as one.

    A new key moves the bytes AND the hash of both shapes above, which is
    three failures describing one change; this one says what changed.
    """
    mrc = _mrc(ns)
    v1 = set(mrc.event_payload(_ordinary(ns), created_at=CREATED_AT))
    v2 = set(mrc.event_payload_v2(_v2_withheld(ns), created_at=CREATED_AT))
    assert v2 - v1 == {
        "payload_version", "withholding_status", "detector_input_causes",
        "composition_provenance", "baseline_withheld_days"}
    assert v1 - v2 == set(), "the v2 payload dropped a v1 key"


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


# --------------------------------------------------------------------------
# #690 — null, empty and zero survive to the wire as three distinct states
# --------------------------------------------------------------------------
def test_690_null_and_empty_and_zero_survive_to_the_wire(ns):
    """Three distinct states, not two.

    NULL means legacy or unrecoverable, `[]` means assessed with no such
    origin, 0 means a clean baseline. A serializer that folds any pair
    together destroys the reader's ability to tell them apart, and the
    client's copy branches on exactly that distinction.
    """
    conn = ns["open_db"]()
    try:
        _seed(conn, effective_from="2026-09-01T00:00:00+00:00")
        _seed(conn, effective_from="2026-09-02T00:00:00+00:00",
              withholding_status="unsupported-model-mix",
              detector_input_causes="[]", composition_provenance="[]",
              baseline_withheld_days=0)
        _seed(conn, effective_from="2026-09-03T00:00:00+00:00",
              withholding_status="unsupported-model-mix",
              detector_input_causes='["sparse-day"]',
              composition_provenance='["forecast-aggregate"]',
              baseline_withheld_days=3)
        rows = {r["effective_from"]: r
                for r in ns["_build_meter_rate_change_array"](conn)}
    finally:
        conn.close()
    legacy = rows["2026-09-01T00:00:00+00:00"]
    assessed = rows["2026-09-02T00:00:00+00:00"]
    populated = rows["2026-09-03T00:00:00+00:00"]

    assert legacy["withholding_status"] is None
    assert legacy["detector_input_causes"] is None
    assert legacy["composition_provenance"] is None
    assert legacy["baseline_withheld_days"] is None

    assert assessed["detector_input_causes"] == []
    assert assessed["composition_provenance"] == []
    assert assessed["baseline_withheld_days"] == 0

    assert populated["detector_input_causes"] == ["sparse-day"]
    assert populated["composition_provenance"] == ["forecast-aggregate"]
    assert populated["baseline_withheld_days"] == 3


def test_690_the_evidence_arrays_reach_the_client_typed(ns):
    """Decoded from their canonical JSON, not forwarded as strings.

    A string `"[]"` is neither null nor an empty array, so forwarding the
    stored text would collapse the very distinction above at the one place
    the client reads it.
    """
    conn = ns["open_db"]()
    try:
        _seed(conn, detector_input_causes='["sparse-day","stale-day"]')
        row = ns["_build_meter_rate_change_array"](conn)[0]
    finally:
        conn.close()
    assert row["detector_input_causes"] == ["sparse-day", "stale-day"]


def test_690_an_undecodable_evidence_column_degrades_to_null(ns):
    """Null, never `[]`.

    `[]` is a MEASUREMENT — assessed, no such origin. Publishing it for a
    column nobody could read would assert something nobody measured, which is
    the whole failure this disclosure exists to stop.
    """
    conn = ns["open_db"]()
    try:
        _seed(conn, detector_input_causes="{not json",
              composition_provenance='"a string, not an array"')
        row = ns["_build_meter_rate_change_array"](conn)[0]
    finally:
        conn.close()
    assert row["detector_input_causes"] is None
    assert row["composition_provenance"] is None
