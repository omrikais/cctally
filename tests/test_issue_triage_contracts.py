from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".agent-workflows/skills/codex/cctally-issue-triage/scripts"
sys.path.insert(0, str(SCRIPTS))

from triage_contracts import (  # noqa: E402
    ContractError,
    canonical_checksum,
    source_fingerprint,
    validate_record,
    validate_record_set,
    validate_snapshot,
    validate_sol_decision,
)


def _issue(number: int, *, comments: list[dict] | None = None) -> dict:
    return {
        "number": number,
        "title": f"Issue {number}",
        "body": "body",
        "state": "OPEN",
        "labels": [{"name": "bug", "description": "Defect", "color": "d73a4a"}],
        "milestone": None,
        "assignees": [],
        "comments": comments or [],
        "relationships": [],
        "linkedReferences": [],
    }


def _user_comment() -> dict:
    return {"id": "U1", "body": "user evidence", "author": "alice", "createdAt": "2026-08-01T00:00:00Z"}


def _managed_comment(value: str) -> dict:
    return {"id": "M1", "body": f"<!-- cctally-issue-triage:record:v1 -->\n{value}", "author": "bot", "createdAt": "2026-08-01T00:00:00Z"}


def _snapshot(*numbers: int) -> dict:
    snapshot = {
        "schemaVersion": 1,
        "repository": {"nameWithOwner": "omrikais/cctally-dev", "id": "R1"},
        "issues": [_issue(number) for number in numbers],
        "labels": [{"id": "L1", "name": "bug", "description": "Defect", "color": "d73a4a"}],
        "milestones": [],
        "tracker": None,
        "project": None,
        "managedRecords": [],
        "reconcilerState": None,
    }
    snapshot["snapshotChecksum"] = canonical_checksum(snapshot)
    return snapshot


def _record(number: int, fingerprint: str | None = None) -> dict:
    return {
        "schemaVersion": 1,
        "issue": number,
        "sourceFingerprint": fingerprint or source_fingerprint(_issue(number)),
        "kind": "bug",
        "currentState": "open",
        "userImpact": "Impact",
        "implementationStatus": "reported",
        "evidence": [{"source": "issue", "locator": f"#{number}", "observation": "Observed", "supports": ["kind"]}],
        "explicitDependencies": [],
        "relationshipCandidates": [],
        "candidateBucket": "queued",
        "labelProposals": [],
        "closureCandidate": "none",
        "subsumptionCandidate": "none",
        "evidenceQuality": "strong",
        "uncertainties": [],
        "nextAction": "Investigate",
        "requiresCurrentMainValidation": False,
    }


def _decision(snapshot: dict, records: list[dict], *, depends: dict[int, list[int]] | None = None, parallel_groups: dict[int, str] | None = None) -> dict:
    depends = depends or {}
    parallel_groups = parallel_groups or {}
    entries = []
    for rank, issue in enumerate(snapshot["issues"], 1):
        number = issue["number"]
        entries.append({
            "issue": number,
            "bucket": "queued",
            "priority": "P2",
            "rank": rank,
            "wave": rank,
            "parallelGroup": parallel_groups.get(number, ""),
            "dependsOn": depends.get(number, []),
            "disposition": "keep-open",
            "labels": ["bug"],
            "annotation": "Evidence summary",
            "closureReason": None,
            "closureEvidence": [],
            "sourceFingerprint": records[rank - 1]["sourceFingerprint"],
        })
    return {
        "schemaVersion": 1,
        "repository": "omrikais/cctally-dev",
        "snapshotChecksum": snapshot["snapshotChecksum"],
        "recordSetChecksum": canonical_checksum(records),
        "issues": entries,
    }


def test_source_fingerprint_ignores_managed_triage_state() -> None:
    before = _issue(101, comments=[_user_comment(), _managed_comment("old")])
    after = _issue(101, comments=[_user_comment(), _managed_comment("new")])
    assert source_fingerprint(before) == source_fingerprint(after)


def test_source_fingerprint_ignores_project_membership_and_derived_updated_at() -> None:
    before = {
        **_issue(101),
        "updatedAt": "2026-08-01T00:00:00Z",
        "projectItems": [],
    }
    after = {
        **_issue(101),
        "updatedAt": "2026-08-01T01:00:00Z",
        "projectItems": [{"id": "PI1", "title": "cctally-dev Issue Triage"}],
    }
    assert source_fingerprint(before) == source_fingerprint(after)


def test_source_fingerprint_changes_for_user_evidence() -> None:
    before = _issue(101, comments=[_user_comment()])
    after = _issue(101, comments=[{**_user_comment(), "body": "changed evidence"}])
    assert source_fingerprint(before) != source_fingerprint(after)


def test_record_set_requires_exactly_one_current_record_per_issue() -> None:
    snapshot = _snapshot(101, 102)
    with pytest.raises(ContractError, match="exactly one record"):
        validate_record_set(snapshot, [_record(101), _record(101)])


def test_sol_decision_rejects_dependency_cycles() -> None:
    snapshot = _snapshot(101, 102)
    records = [_record(101), _record(102)]
    with pytest.raises(ContractError, match="dependency cycle"):
        validate_sol_decision(_decision(snapshot, records, depends={101: [102], 102: [101]}), snapshot, records)


def test_sol_decision_rejects_parallel_dependency_conflicts() -> None:
    snapshot = _snapshot(101, 102)
    records = [_record(101), _record(102)]
    with pytest.raises(ContractError, match="parallel group"):
        validate_sol_decision(_decision(snapshot, records, depends={102: [101]}, parallel_groups={101: "p1", 102: "p1"}), snapshot, records)


@pytest.mark.parametrize("validator,payload", [
    (validate_snapshot, {"schemaVersion": 2}),
    (validate_record, {"schemaVersion": 2}),
])
def test_contracts_reject_unknown_schema_versions(validator, payload) -> None:
    with pytest.raises(ContractError, match="schemaVersion"):
        validator(payload)


def test_sol_decision_rejects_checksum_and_rank_errors() -> None:
    snapshot = _snapshot(101, 102)
    records = [_record(101), _record(102)]
    decision = _decision(snapshot, records)
    decision["snapshotChecksum"] = "0" * 64
    with pytest.raises(ContractError, match="snapshotChecksum"):
        validate_sol_decision(decision, snapshot, records)
    decision = _decision(snapshot, records)
    decision["issues"][1]["rank"] = 1
    with pytest.raises(ContractError, match="rank"):
        validate_sol_decision(decision, snapshot, records)


def test_sol_decision_rejects_unknown_issue_and_unproved_closure() -> None:
    snapshot = _snapshot(101)
    records = [_record(101)]
    decision = _decision(snapshot, records)
    decision["issues"][0]["issue"] = 999
    with pytest.raises(ContractError, match="unknown issue"):
        validate_sol_decision(decision, snapshot, records)
    decision = _decision(snapshot, records)
    decision["issues"][0].update(disposition="close", closureReason="fixed")
    with pytest.raises(ContractError, match="closureEvidence"):
        validate_sol_decision(decision, snapshot, records)


def test_record_rejects_malformed_label_proposal() -> None:
    record = _record(101)
    record["labelProposals"] = [{"name": "area:new", "description": "", "color": "not-a-color"}]
    with pytest.raises(ContractError, match="labelProposals"):
        validate_record(record)


def test_record_requires_array_supports_and_complete_relationship_candidates() -> None:
    record = _record(101)
    record["evidence"][0]["supports"] = "kind"
    with pytest.raises(ContractError, match="supports"):
        validate_record(record)
    record = _record(101)
    record["relationshipCandidates"] = [{"issue": 102, "kind": "related"}]
    with pytest.raises(ContractError, match="basis"):
        validate_record(record)


def test_luna_green_behavior_evidence_is_schema_valid_and_complete() -> None:
    artifact = json.loads((ROOT / "tests/fixtures/issue-triage/luna-green-result.json").read_text())
    records = [validate_record(record) for record in artifact["records"]]
    assert [record["issue"] for record in records] == list(range(101, 108))
    assert len({record["sourceFingerprint"] for record in records}) == 7
    assert artifact["assertions"] == {key: True for key in artifact["assertions"]}
    assert records[3]["currentState"] == "partially-complete"
    assert records[4]["subsumptionCandidate"] == "candidate"
    assert records[5]["requiresCurrentMainValidation"] is True
    assert "semantic" in " ".join(records[6]["uncertainties"]).lower()
