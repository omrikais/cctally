"""#496 S6 Task 3 — family-parameterized incident classification (spec §4.3).

The shipped `bin/cctally-classify-incidents` correlator is stats-only: it
hardcodes `^stats\\.db-`, reads `incident / "stats.db"`, and fixes the candidate
set to `("corruption-heal", "db-rebuild")`. The correlation ALGORITHM transfers
to cache and conversations unchanged — bundles are named from `db_path.name`
and incidents share the same `_db_backup_timestamp()` stem — but the verdict
semantics do not: cache and conversations producers already pass a trigger into
`write_corruption_forensics`, so a correlated bundle that names its own
`trigger.origin` identifies the trigger EXACTLY rather than narrowing it to a
candidate set.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import stat
import sys
from datetime import timedelta


_BIN = pathlib.Path(__file__).resolve().parent.parent / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

from _lib_artifact_retention import (  # noqa: E402
    FAMILY_CANDIDATE_TRIGGERS,
    classify_incident,
)

T0 = dt.datetime(2026, 1, 1, 0, 0, 0, tzinfo=dt.timezone.utc)


def _bundle(offset_seconds, payload=None, path="/logs/b.json"):
    return (T0 + timedelta(seconds=offset_seconds), path, payload or {})


# --------------------------------------------------------------------------
# The pure verdict
# --------------------------------------------------------------------------


def test_a_v2_manifest_with_a_trigger_classifies_exactly():
    v = classify_incident(
        family="cache.db", incident_name="cache.db-20260101T000000Z",
        manifest={"schemaVersion": 2, "trigger": "cache.open"},
        bundles=[], incident_time=T0,
    )
    assert (v.confidence, v.method) == ("exact", "manifest-v2")
    assert v.trigger == "cache.open"


def test_a_v2_manifest_without_a_trigger_is_not_classified():
    v = classify_incident(
        family="cache.db", incident_name="i",
        manifest={"schemaVersion": 2}, bundles=[], incident_time=T0,
    )
    assert v.confidence == "unknown"


def test_a_v2_manifest_with_an_empty_trigger_is_not_classified():
    v = classify_incident(
        family="cache.db", incident_name="i",
        manifest={"schemaVersion": 2, "trigger": ""},
        bundles=[], incident_time=T0,
    )
    assert v.confidence == "unknown"


def test_a_correlated_bundle_carrying_trigger_origin_yields_exact():
    b = _bundle(-2, {"trigger": {"origin": "cache.open"}})
    v = classify_incident(
        family="cache.db", incident_name="i",
        manifest={"schemaVersion": 1}, bundles=[b], incident_time=T0,
    )
    assert (v.confidence, v.method, v.trigger) == (
        "exact", "forensics-trigger", "cache.open",
    )
    assert v.forensics_path == "/logs/b.json"


def test_a_correlated_bundle_without_trigger_yields_a_family_candidate_set():
    b = _bundle(-2, {})
    v = classify_incident(
        family="cache.db", incident_name="i",
        manifest={"schemaVersion": 1}, bundles=[b], incident_time=T0,
    )
    assert (v.confidence, v.method) == ("candidate", "forensics-correlation")
    assert v.candidates == FAMILY_CANDIDATE_TRIGGERS["cache.db"]


def test_the_candidate_set_is_family_specific():
    b = _bundle(-2, {})
    for family in ("stats.db", "cache.db", "conversations.db"):
        v = classify_incident(
            family=family, incident_name="i",
            manifest={"schemaVersion": 1}, bundles=[b], incident_time=T0,
        )
        assert v.candidates == FAMILY_CANDIDATE_TRIGGERS[family], family
    # The three sets are genuinely distinct, so a mixed-up family is visible.
    assert len({FAMILY_CANDIDATE_TRIGGERS[f] for f in FAMILY_CANDIDATE_TRIGGERS}) == 3


def test_a_bundle_outside_the_window_does_not_correlate():
    b = _bundle(-601, {"trigger": {"origin": "cache.open"}})
    v = classify_incident(
        family="cache.db", incident_name="i",
        manifest={"schemaVersion": 1}, bundles=[b], incident_time=T0,
    )
    assert (v.confidence, v.method) == ("unknown", "header-only")


def test_a_bundle_exactly_on_the_window_edge_still_correlates():
    b = _bundle(-600, {"trigger": {"origin": "cache.open"}})
    v = classify_incident(
        family="cache.db", incident_name="i",
        manifest={"schemaVersion": 1}, bundles=[b], incident_time=T0,
    )
    assert v.confidence == "exact"


def test_a_following_bundle_does_not_correlate():
    b = _bundle(1, {"trigger": {"origin": "cache.open"}})
    v = classify_incident(
        family="cache.db", incident_name="i",
        manifest={"schemaVersion": 1}, bundles=[b], incident_time=T0,
    )
    assert v.confidence == "unknown"


def test_the_nearest_preceding_bundle_wins():
    far = _bundle(-300, {"trigger": {"origin": "cache.sync"}}, "/logs/far.json")
    near = _bundle(-2, {"trigger": {"origin": "cache.open"}}, "/logs/near.json")
    v = classify_incident(
        family="cache.db", incident_name="i",
        manifest={"schemaVersion": 1}, bundles=[far, near], incident_time=T0,
    )
    assert v.trigger == "cache.open"
    assert v.forensics_path == "/logs/near.json"


def test_the_verdict_names_the_incident_it_describes():
    v = classify_incident(
        family="cache.db", incident_name="cache.db-20260101T000000Z",
        manifest={"schemaVersion": 2, "trigger": "cache.open"},
        bundles=[], incident_time=T0,
    )
    assert v.incident == "cache.db-20260101T000000Z"


def test_an_unparseable_incident_time_never_correlates():
    b = _bundle(-2, {"trigger": {"origin": "cache.open"}})
    v = classify_incident(
        family="cache.db", incident_name="cache.db-nonsense",
        manifest={}, bundles=[b], incident_time=None,
    )
    assert v.confidence == "unknown"


# --------------------------------------------------------------------------
# The glue: family-parameterized discovery
# --------------------------------------------------------------------------


def _write(path: pathlib.Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


def test_bundle_discovery_is_filtered_by_family(tmp_path):
    """Both exclusions, each exercised by the entry it actually covers.

    The WAL-evidence directory beside a bundle shares its stem but not its
    `.json` suffix, so the NAME regex rejects it. A directory or a symlink
    whose name does match a bundle is what `is_file(follow_symlinks=False)`
    covers, and only those entries reach that guard.
    """
    import _cctally_retention as ret

    logs = tmp_path / "logs"
    _write(logs / "cache.db-corruption-forensics-20260101T000000Z.json",
           {"schemaVersion": 1, "trigger": {"origin": "cache.open"}})
    _write(logs / "stats.db-corruption-forensics-20260101T000001Z.json",
           {"schemaVersion": 1})
    # Rejected on the name: the real WAL-evidence directory shape.
    (logs / "cache.db-corruption-forensics-20260101T000000Z").mkdir()
    # Rejected by the is_file guard: a directory named exactly like a bundle.
    (logs / "cache.db-corruption-forensics-20260102T000000Z.json").mkdir()
    # Rejected by the is_file guard too: symlinks are refused, not followed.
    (logs / "cache.db-corruption-forensics-20260103T000000Z.json").symlink_to(
        logs / "cache.db-corruption-forensics-20260101T000000Z.json"
    )

    found = ret.list_family_bundles(logs, "cache.db")
    assert [entry[1] for entry in found] == [
        str(logs / "cache.db-corruption-forensics-20260101T000000Z.json")
    ]
    assert found[0][0] == dt.datetime(
        2026, 1, 1, 0, 0, 0, tzinfo=dt.timezone.utc
    )
    assert found[0][2]["trigger"]["origin"] == "cache.open"


def test_incident_time_recognizes_both_stamp_shapes(tmp_path):
    import _cctally_retention as ret

    assert ret.incident_time("cache.db", "cache.db-20260101T000000Z") == dt.datetime(
        2026, 1, 1, tzinfo=dt.timezone.utc
    )
    assert ret.incident_time(
        "stats.db", "stats.db-20260101T000000_123456"
    ) == dt.datetime(2026, 1, 1, 0, 0, 0, 123456, tzinfo=dt.timezone.utc)
    assert ret.incident_time("cache.db", "stats.db-20260101T000000Z") is None
    assert ret.incident_time("cache.db", "cache.db-not-a-stamp") is None


def test_classify_incident_dir_reads_a_v2_manifest_from_disk(tmp_path):
    import _cctally_retention as ret

    incident = tmp_path / "quarantine" / "cache.db-20260101T000000Z"
    _write(incident / "manifest.json",
           {"schemaVersion": 2, "trigger": "cache.open"})
    v = ret.classify_incident_dir(incident, family="cache.db", bundles=[])
    assert (v.confidence, v.trigger) == ("exact", "cache.open")


def test_the_backfill_writes_a_private_classification_and_is_idempotent(tmp_path):
    import _cctally_retention as ret

    logs = tmp_path / "logs"
    _write(logs / "cache.db-corruption-forensics-20260101T000000Z.json",
           {"schemaVersion": 1, "trigger": {"origin": "cache.open"}})
    incident = tmp_path / "quarantine" / "cache.db-20260101T000001Z"
    _write(incident / "manifest.json", {"schemaVersion": 1})

    written = ret.backfill_classification(
        incident, family="cache.db",
        bundles=ret.list_family_bundles(logs, "cache.db"),
    )
    assert written is True
    record = json.loads((incident / "classification.json").read_text())
    assert record["confidence"] == "exact"
    assert record["incident"] == incident.name
    assert stat.S_IMODE(
        (incident / "classification.json").stat().st_mode
    ) == 0o600

    # A second run over an unchanged corpus rewrites nothing.
    again = ret.backfill_classification(
        incident, family="cache.db",
        bundles=ret.list_family_bundles(logs, "cache.db"),
    )
    assert again is False


def test_the_backfill_never_overwrites_a_considered_verdict(tmp_path):
    """§4.4 — the correlator's existing `unknown` verdicts are not overridden."""
    import _cctally_retention as ret

    incident = tmp_path / "quarantine" / "stats.db-20260101T000001Z"
    _write(incident / "manifest.json", {"schemaVersion": 1})
    _write(incident / "classification.json", {
        "schemaVersion": 1,
        "incident": incident.name,
        "method": "header-only",
        "confidence": "unknown",
    })
    before = (incident / "classification.json").read_text()

    written = ret.backfill_classification(
        incident, family="stats.db", bundles=[],
    )
    assert written is False
    assert (incident / "classification.json").read_text() == before


def test_family_of_incident_reads_the_directory_name():
    import _cctally_retention as ret

    assert ret.family_of_incident("cache.db-20260101T000000Z") == "cache.db"
    assert ret.family_of_incident(
        "conversations.db-20260101T000000Z"
    ) == "conversations.db"
    assert ret.family_of_incident("stats.db-20260101T000000_123456") == "stats.db"
    assert ret.family_of_incident("something-else") is None
