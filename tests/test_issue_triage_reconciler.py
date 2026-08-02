from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".agent-workflows/skills/codex/cctally-issue-triage/scripts"
sys.path.insert(0, str(SCRIPTS))

import reconcile_backlog as reconcile_module  # noqa: E402
from reconcile_backlog import (  # noqa: E402
    Mutation,
    ReconcileError,
    apply_mutations,
    build_mutations,
    execute_live,
    plan_checksum,
    semantic_label,
    verify_readback,
)
from github_gateway import GhGateway, GhResult  # noqa: E402
from triage_contracts import canonical_checksum, source_fingerprint  # noqa: E402


def _issue(number: int, *, state: str = "OPEN") -> dict:
    return {
        "number": number, "id": f"I{number}", "title": f"Issue {number}", "body": "body", "state": state,
        "labels": [{"id": "LUSER", "name": "customer-reported", "description": "User label", "color": "eeeeee"}],
        "milestone": None, "assignees": [], "comments": [], "relationships": [], "linkedReferences": [],
        "userProjectNote": "keep me",
    }


def _snapshot(*, closed: bool = False, partial: bool = False) -> dict:
    issues = [_issue(101, state="CLOSED" if closed else "OPEN"), _issue(102)]
    snapshot = {
        "schemaVersion": 1,
        "repository": {"id": "R1", "nameWithOwner": "omrikais/cctally-dev"},
        "issues": issues,
        "labels": [{"id": "LUSER", "name": "customer-reported", "description": "User label", "color": "eeeeee"}],
        "milestones": [],
        "tracker": {"id": "T1", "number": 900, "url": "https://github.test/issues/900"},
        "project": {"id": "P1", "number": 4, "url": "https://github.test/projects/4", "fields": []},
        "managedRecords": [],
        "reconcilerState": {
            "schemaVersion": 1, "status": "PARTIAL" if partial else "COMPLETE", "completedMutationKeys": ["label:101:correctness"] if partial else [],
            "remainingMutationKeys": [], "ownedIssueLabels": {}, "ownedDependencyEdges": [[101, 109]], "recordCommentIds": {},
        },
        "dependencies": {"userOwned": [[101, 109]], "triageOwned": []},
    }
    snapshot["snapshotChecksum"] = canonical_checksum({key: value for key, value in snapshot.items() if key != "snapshotChecksum"})
    return snapshot


def _unbootstrapped_snapshot() -> dict:
    snapshot = _snapshot()
    snapshot["tracker"] = None
    snapshot["project"] = None
    snapshot["reconcilerState"] = None
    snapshot["snapshotChecksum"] = canonical_checksum(
        {key: value for key, value in snapshot.items() if key != "snapshotChecksum"}
    )
    return snapshot


class StatefulLiveGateway:
    def __init__(self, snapshot: dict, *, fail_key: str | None = None) -> None:
        self.state = copy.deepcopy(snapshot)
        self.fail_key = fail_key
        self.persisted: list[dict] = []
        self.applied_keys: list[str] = []
        self.primed = False

    def prime(self, state: dict) -> None:
        self.primed = True

    def apply(self, mutation, context) -> None:
        if mutation.key == self.fail_key:
            raise RuntimeError(f"failed {mutation.key}")
        self.applied_keys.append(mutation.key)
        if mutation.kind == "create_project":
            self.state["project"] = {"id": "PNEW", "number": 7, "url": "https://github.test/projects/7", "items": {}}
        elif mutation.kind == "create_tracker":
            self.state["tracker"] = {"id": "TNEW", "number": 901, "url": "https://github.test/issues/901"}
        else:
            self.state = apply_mutations(
                self.state,
                context["decision"],
                [mutation],
                _skip_checksum=True,
                _skip_verify=True,
            )

    def persist(self, ledger: dict) -> None:
        self.state["reconcilerState"] = copy.deepcopy(ledger)
        self.persisted.append(copy.deepcopy(ledger))

    def collect_repository(self, repo: str) -> dict:
        return copy.deepcopy(self.state)


def _records(snapshot: dict) -> list[dict]:
    return [{"issue": issue["number"], "sourceFingerprint": source_fingerprint(issue)} for issue in snapshot["issues"]]


def _decision(snapshot: dict, *, close: bool = False, label: str = "correctness") -> dict:
    records = _records(snapshot)
    return {
        "schemaVersion": 1,
        "repository": "omrikais/cctally-dev",
        "snapshotChecksum": snapshot["snapshotChecksum"],
        "recordSetChecksum": canonical_checksum(records),
        "issues": [
            {
                "issue": 101, "bucket": "close-review" if close else "do-next", "priority": "P0", "rank": 1, "wave": 1,
                "parallelGroup": "", "dependsOn": [], "disposition": "close" if close else "keep-open",
                "labels": [label], "annotation": "first", "closureReason": "completed" if close else None,
                "closureEvidence": ["current-main:test-101"] if close else [], "sourceFingerprint": records[0]["sourceFingerprint"],
                "evidenceQuality": "strong",
            },
            {
                "issue": 102, "bucket": "queued", "priority": "P2", "rank": 2, "wave": 2,
                "parallelGroup": "", "dependsOn": [101], "disposition": "keep-open", "labels": [],
                "annotation": "second", "closureReason": None, "closureEvidence": [],
                "sourceFingerprint": records[1]["sourceFingerprint"], "evidenceQuality": "mixed",
            },
        ],
    }


def test_build_mutations_orders_closure_last() -> None:
    mutations = build_mutations(_snapshot(), _decision(_snapshot(), close=True))
    phases = [mutation.phase for mutation in mutations]
    assert phases == sorted(phases)
    close_index = next(index for index, mutation in enumerate(mutations) if mutation.kind == "close_issue")
    assert all(mutation.kind in {"render_tracker", "persist_complete", "verify"} for mutation in mutations[close_index + 1 :])
    assert [mutation.kind for mutation in mutations[-3:]] == [
        "render_tracker", "persist_complete", "verify",
    ]


def test_unbootstrapped_plan_creates_project_and_tracker_first() -> None:
    snapshot = _unbootstrapped_snapshot()
    mutations = build_mutations(snapshot, _decision(snapshot))
    assert [(mutation.phase, mutation.kind) for mutation in mutations[:2]] == [
        (1, "create_project"),
        (1, "create_tracker"),
    ]


def test_live_execution_bootstraps_and_recollects_complete_state() -> None:
    snapshot = _unbootstrapped_snapshot()
    decision = _decision(snapshot)
    mutations = build_mutations(snapshot, decision)
    checksum = plan_checksum(snapshot["snapshotChecksum"], canonical_checksum(decision), mutations)
    gateway = StatefulLiveGateway(snapshot)
    final_state = execute_live(gateway, snapshot, decision, mutations, checksum)
    assert final_state["project"]["id"] == "PNEW"
    assert final_state["tracker"]["number"] == 901
    assert final_state["reconcilerState"]["status"] == "COMPLETE"
    assert final_state["reconcilerState"]["remainingMutationKeys"] == []
    assert gateway.persisted[0]["status"] == "APPLYING"


def test_live_execution_persists_partial_state_before_raising() -> None:
    snapshot = _unbootstrapped_snapshot()
    decision = _decision(snapshot)
    mutations = build_mutations(snapshot, decision)
    checksum = plan_checksum(snapshot["snapshotChecksum"], canonical_checksum(decision), mutations)
    gateway = StatefulLiveGateway(snapshot, fail_key="project:101")
    with pytest.raises(ReconcileError, match="project:101"):
        execute_live(gateway, snapshot, decision, mutations, checksum)
    assert gateway.persisted[-1]["status"] == "PARTIAL"
    assert gateway.persisted[-1]["remainingMutationKeys"][0] == "project:101"


def test_live_execution_resumes_original_checksum_without_replaying_completed_keys() -> None:
    snapshot = _unbootstrapped_snapshot()
    decision = _decision(snapshot)
    mutations = build_mutations(snapshot, decision)
    checksum = plan_checksum(snapshot["snapshotChecksum"], canonical_checksum(decision), mutations)
    gateway = StatefulLiveGateway(snapshot, fail_key="project:101")
    with pytest.raises(ReconcileError):
        execute_live(gateway, snapshot, decision, mutations, checksum)
    completed_before_retry = list(gateway.state["reconcilerState"]["completedMutationKeys"])
    calls_before_retry = list(gateway.applied_keys)
    gateway.fail_key = None
    final_state = execute_live(
        gateway,
        snapshot,
        decision,
        mutations,
        checksum,
        current_state=gateway.state,
    )
    assert final_state["reconcilerState"]["status"] == "COMPLETE"
    assert gateway.primed is True
    assert gateway.applied_keys[:len(calls_before_retry)] == calls_before_retry
    assert all(
        gateway.applied_keys.count(key) == 1 for key in completed_before_retry
        if key not in {"state:applying"}
    )


def test_gh_gateway_bootstraps_project_tracker_and_persists_state() -> None:
    remote = {"trackerBody": ""}

    def runner(argv: list[str]) -> GhResult:
        joined = " ".join(argv)
        if "project create" in joined:
            payload = {"id": "PNEW", "number": 7, "url": "https://github.test/projects/7"}
            return GhResult(tuple(argv), 0, json.dumps(payload), "")
        if "issue create" in joined:
            return GhResult(tuple(argv), 0, "https://github.test/omrikais/cctally-dev/issues/901\n", "")
        if "issue view 901" in joined:
            payload = {"id": "TNEW", "number": 901, "url": "https://github.test/issues/901", "body": remote["trackerBody"]}
            return GhResult(tuple(argv), 0, json.dumps(payload), "")
        if "issue edit 901" in joined:
            remote["trackerBody"] = argv[argv.index("--body") + 1]
            return GhResult(tuple(argv), 0, "", "")
        raise AssertionError(argv)

    gateway = GhGateway(runner=runner)
    context = {"decision": {"repository": "omrikais/cctally-dev"}, "ledger": {}}
    gateway.apply(Mutation("create_project", "bootstrap:project", None, {"owner": "omrikais", "title": "cctally-dev Issue Triage"}, False, 1), context)
    gateway.apply(Mutation("create_tracker", "bootstrap:tracker", None, {"repo": "omrikais/cctally-dev", "title": "[Tracking] Open issue triage and execution order", "body": "<!-- cctally-issue-triage:tracker:v1 -->"}, False, 1), context)
    gateway.persist({"schemaVersion": 1, "status": "APPLYING", "remainingMutationKeys": ["project:101"]})
    assert gateway.runtime["project"]["id"] == "PNEW"
    assert gateway.runtime["tracker"]["number"] == 901
    assert "cctally-issue-triage:state:v1" in remote["trackerBody"]
    assert '"status":"APPLYING"' in remote["trackerBody"]


def test_tracker_render_survives_later_ledger_persistence() -> None:
    remote = {"body": ""}

    def runner(argv: list[str]) -> GhResult:
        joined = " ".join(argv)
        if "issue edit 901" in joined:
            remote["body"] = argv[argv.index("--body") + 1]
            return GhResult(tuple(argv), 0, "", "")
        raise AssertionError(argv)

    gateway = GhGateway(runner=runner)
    gateway.runtime.update({
        "repo": "omrikais/cctally-dev",
        "tracker": {"number": 901},
        "project": {"id": "P1", "number": 7, "url": "https://github.test/projects/7"},
    })
    decision = {
        "snapshotChecksum": "a" * 64,
        "issues": [{"issue": 101, "rank": 1, "priority": "P0", "bucket": "do-next", "wave": 1, "parallelGroup": "lane-a", "dependsOn": [], "disposition": "keep-open"}],
    }
    gateway.apply(Mutation("render_tracker", "tracker:render", None, {}, False, 8), {"decision": decision})
    gateway.persist({"schemaVersion": 1, "status": "COMPLETE", "planChecksum": "b" * 64, "remainingMutationKeys": []})
    assert "https://github.test/projects/7" in remote["body"]
    assert "| 1 | #101 | P0 | do-next | 1 | lane-a |" in remote["body"]
    assert "b" * 64 in remote["body"]


def test_persisted_ledger_contains_complete_owned_state_identity() -> None:
    remote = {"body": ""}

    def runner(argv: list[str]) -> GhResult:
        remote["body"] = argv[argv.index("--body") + 1]
        return GhResult(tuple(argv), 0, "", "")

    gateway = GhGateway(runner=runner)
    gateway.runtime.update({
        "repo": "omrikais/cctally-dev",
        "tracker": {"id": "T1", "number": 901},
        "project": {"id": "P1", "number": 7, "url": "https://github.test/projects/7"},
    })
    ledger = {"schemaVersion": 1, "status": "APPLYING", "remainingMutationKeys": []}
    gateway.persist(ledger)
    assert ledger["projectOwner"] == "omrikais"
    assert ledger["projectNumber"] == 7
    assert ledger["projectId"] == "P1"
    assert ledger["trackerIssue"] == 901
    assert ledger["managedLabelIds"] == {}
    assert ledger["ownedIssueLabels"] == {}
    assert ledger["ownedDependencyEdges"] == []
    assert ledger["recordCommentIds"] == {}
    assert ledger["updatedAt"].endswith("Z")


def test_gh_gateway_primes_runtime_from_recollected_state() -> None:
    gateway = GhGateway(runner=lambda argv: GhResult(tuple(argv), 0, "", ""))
    gateway.prime({
        "repository": {"nameWithOwner": "omrikais/cctally-dev"},
        "tracker": {"id": "T1", "number": 475, "url": "https://github.test/issues/475"},
        "project": {
            "id": "P1", "number": 4, "url": "https://github.test/projects/4",
            "fields": [{"name": "Rank"}],
        },
    })
    assert gateway.runtime["repo"] == "omrikais/cctally-dev"
    assert gateway.runtime["tracker"]["number"] == 475
    assert gateway.runtime["project"]["number"] == 4
    assert gateway.runtime["field_names"] == {"Rank"}


def test_gh_gateway_creates_managed_fields_and_sets_project_item_values() -> None:
    remote = {"fields": {"Title"}, "fieldDefs": {}, "values": {}}

    def runner(argv: list[str]) -> GhResult:
        joined = " ".join(argv)
        if "project field-list" in joined:
            return GhResult(tuple(argv), 0, json.dumps({"fields": [{"id": "FTITLE", "name": name} for name in sorted(remote["fields"])]}), "")
        if "project field-create" in joined:
            name = argv[argv.index("--name") + 1]
            field_id = f"F:{name}"
            field = {"id": field_id, "name": name, "type": "ProjectV2Field"}
            if "--single-select-options" in argv:
                field["type"] = "ProjectV2SingleSelectField"
                field["options"] = [
                    {"id": f"O:{name}:{value}", "name": value}
                    for value in argv[argv.index("--single-select-options") + 1].split(",")
                ]
            remote["fields"].add(name)
            remote["fieldDefs"][field_id] = field
            return GhResult(tuple(argv), 0, json.dumps(field), "")
        if "addProjectV2ItemById" in joined:
            payload = {"data": {"addProjectV2ItemById": {"item": {"id": "ITEM101"}}}}
            return GhResult(tuple(argv), 0, json.dumps(payload), "")
        if "project item-edit" in joined:
            field_id = argv[argv.index("--field-id") + 1]
            field = remote["fieldDefs"][field_id]
            for flag in ("--single-select-option-id", "--number", "--text", "--date"):
                if flag in argv:
                    value = argv[argv.index(flag) + 1]
                    if flag == "--single-select-option-id":
                        value = next(item["name"] for item in field["options"] if item["id"] == value)
                    remote["values"][field["name"]] = value
                    break
            return GhResult(tuple(argv), 0, json.dumps({"id": "ITEM101"}), "")
        raise AssertionError(argv)

    gateway = GhGateway(runner=runner)
    gateway.runtime.update({"project": {"id": "P1", "number": 7}, "repo": "omrikais/cctally-dev"})
    context = {"decision": {"repository": "omrikais/cctally-dev"}, "snapshot": {"issues": [_issue(101) ]}}
    mutation = Mutation("set_project_fields", "project:101", 101, {
        "Bucket": "do-next", "Priority": "P0", "Rank": 1, "Wave": 1,
        "Parallel group": "lane-a", "Evidence": "Strong",
        "Source fingerprint": "a" * 64, "Last triaged": "2026-08-01",
    }, False, 4)
    gateway.apply(mutation, context)
    assert remote["fields"] >= set(mutation.payload)
    assert remote["values"] == {
        "Bucket": "do-next", "Priority": "P0", "Rank": "1", "Wave": "1",
        "Parallel group": "lane-a", "Evidence": "Strong",
        "Source fingerprint": "a" * 64, "Last triaged": "2026-08-01",
    }


def test_gh_gateway_uses_direct_project_node_ids_after_recollection() -> None:
    calls: list[list[str]] = []
    payload = {
        "Bucket": "do-next", "Priority": "P0", "Rank": 1, "Wave": 1,
        "Parallel group": "", "Evidence": "Strong",
        "Source fingerprint": "a" * 64, "Last triaged": "2026-08-01",
    }
    fields = []
    for index, (name, value) in enumerate(payload.items()):
        field = {"id": f"F{index}", "name": name, "type": "ProjectV2Field"}
        if name in {"Bucket", "Priority", "Evidence"}:
            field["type"] = "ProjectV2SingleSelectField"
            field["options"] = [{"id": f"O{index}", "name": value}]
        fields.append(field)

    def runner(argv: list[str]) -> GhResult:
        calls.append(argv)
        return GhResult(tuple(argv), 0, json.dumps({"id": "ITEM101"}), "")

    gateway = GhGateway(runner=runner)
    gateway.prime({
        "repository": {"nameWithOwner": "omrikais/cctally-dev"},
        "tracker": {"id": "T1", "number": 475},
        "project": {
            "id": "P1", "number": 4, "fields": fields,
            "items": {"101": payload}, "itemIds": {"101": "ITEM101"},
        },
    })
    gateway.apply(
        Mutation("set_project_fields", "project:101", 101, payload, False, 4),
        {"decision": {"repository": "omrikais/cctally-dev"}, "snapshot": {"issues": [_issue(101)]}},
    )
    assert len(calls) == len(payload)
    assert all("--id" in call and "ITEM101" in call for call in calls)
    assert all("--project-id" in call and "P1" in call for call in calls)
    assert all("--field-id" in call and "--field" not in call and "--url" not in call for call in calls)


def test_gh_gateway_updates_only_managed_issue_state_and_closes_evidence_first() -> None:
    remote = {"comment": "old", "labels": [], "events": []}

    def runner(argv: list[str]) -> GhResult:
        joined = " ".join(argv)
        if "api graphql" in joined:
            remote["comment"] = argv[argv.index("body=") + 1] if "body=" in argv else next(value.removeprefix("body=") for value in argv if value.startswith("body="))
            remote["events"].append("annotation")
            return GhResult(tuple(argv), 0, json.dumps({"data": {"updateIssueComment": {"issueComment": {"id": "C1"}}}}), "")
        if "issue edit 101" in joined and "--add-label" in argv:
            remote["labels"].append(argv[argv.index("--add-label") + 1])
            remote["events"].append("label")
            return GhResult(tuple(argv), 0, "", "")
        if "issue comment 101" in joined:
            remote["events"].append("closure-evidence")
            return GhResult(tuple(argv), 0, "https://github.test/comment/1\n", "")
        if "issue close 101" in joined:
            remote["events"].append("close")
            return GhResult(tuple(argv), 0, "", "")
        raise AssertionError(argv)

    gateway = GhGateway(runner=runner)
    gateway.runtime["repo"] = "omrikais/cctally-dev"
    snapshot = {"issues": [{**_issue(101), "comments": [{"id": "C1", "body": "<!-- cctally-issue-triage:record:v1 -->\nold"}]}]}
    context = {"decision": {"repository": "omrikais/cctally-dev"}, "snapshot": snapshot, "ledger": {}}
    gateway.apply(Mutation("upsert_annotation", "annotation:101", 101, {"body": "<!-- cctally-issue-triage:record:v1 -->\n{}"}, False, 5), context)
    gateway.apply(Mutation("add_label", "label:101:bug", 101, {"name": "bug"}, False, 3), context)
    gateway.apply(Mutation("close_issue", "close:101", 101, {"reason": "completed", "evidence": ["current-main:test"]}, True, 7), context)
    assert remote["comment"].endswith("{}")
    assert remote["labels"] == ["bug"]
    assert remote["events"][-2:] == ["closure-evidence", "close"]


def test_gh_gateway_creates_annotation_by_node_id_and_ledgers_comment_id() -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> GhResult:
        calls.append(argv)
        payload = {"data": {"addComment": {"commentEdge": {"node": {"id": "CNEW"}}}}}
        return GhResult(tuple(argv), 0, json.dumps(payload), "")

    gateway = GhGateway(runner=runner)
    gateway.runtime["repo"] = "omrikais/cctally-dev"
    ledger: dict = {}
    snapshot = {"issues": [_issue(101)]}
    gateway.apply(
        Mutation("upsert_annotation", "annotation:101", 101, {"body": "<!-- cctally-issue-triage:record:v1 -->\n{}"}, False, 5),
        {"snapshot": snapshot, "decision": {"repository": "omrikais/cctally-dev"}, "ledger": ledger},
    )
    assert ledger["recordCommentIds"] == {"101": "CNEW"}
    assert any("addComment" in value for value in calls[0])
    assert "subjectId=I101" in calls[0]


def test_gh_gateway_applies_native_dependency_and_updates_owned_ledger() -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> GhResult:
        calls.append(argv)
        return GhResult(tuple(argv), 0, json.dumps({"data": {"addBlockedBy": {"issue": {"id": "I102"}}}}), "")

    gateway = GhGateway(runner=runner)
    gateway.runtime["repo"] = "omrikais/cctally-dev"
    ledger = {"ownedDependencyEdges": []}
    snapshot = {"issues": [_issue(101), _issue(102)]}
    gateway.apply(
        Mutation("add_dependency", "dependency:102:101", 102, {"dependsOn": 101}, False, 6),
        {"snapshot": snapshot, "decision": {"repository": "omrikais/cctally-dev"}, "ledger": ledger},
    )
    assert ledger["ownedDependencyEdges"] == [[102, 101]]
    assert any("addBlockedBy" in value for value in calls[0])
    assert "issueId=I102" in calls[0]
    assert "blockingIssueId=I101" in calls[0]


def test_apply_cli_uses_live_gateway_not_in_memory_simulation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = _unbootstrapped_snapshot()
    decision = _decision(snapshot)
    records = [
        {
            "schemaVersion": 1, "issue": issue["number"], "sourceFingerprint": source_fingerprint(issue),
            "kind": "bug", "currentState": "open", "userImpact": "impact",
            "implementationStatus": "reported", "evidence": [{"source": "issue", "locator": "body", "observation": "observed", "supports": ["kind"]}],
            "explicitDependencies": [], "relationshipCandidates": [], "candidateBucket": "queued",
            "labelProposals": [], "closureCandidate": "none", "subsumptionCandidate": "none",
            "evidenceQuality": "mixed", "uncertainties": [], "nextAction": "act",
            "requiresCurrentMainValidation": False,
        }
        for issue in snapshot["issues"]
    ]
    mutations = build_mutations(snapshot, decision, records=records)
    checksum = plan_checksum(snapshot["snapshotChecksum"], canonical_checksum(decision), mutations)
    snapshot_path, decision_path, records_path, output_path = (
        tmp_path / "snapshot.json", tmp_path / "decision.json", tmp_path / "records.json", tmp_path / "result.json"
    )
    snapshot_path.write_text(json.dumps(snapshot))
    decision_path.write_text(json.dumps(decision))
    records_path.write_text(json.dumps(records))
    gateway = StatefulLiveGateway(snapshot)
    monkeypatch.setattr(reconcile_module, "GhGateway", lambda: gateway, raising=False)
    monkeypatch.setattr(sys, "argv", [
        "reconcile_backlog.py", "--apply", "--snapshot", str(snapshot_path),
        "--decision", str(decision_path), "--records", str(records_path),
        "--plan-checksum", checksum, "--output", str(output_path),
    ])
    assert reconcile_module.main() == 0
    assert gateway.state["project"]["id"] == "PNEW"
    assert json.loads(output_path.read_text())["reconcilerState"]["status"] == "COMPLETE"


def test_apply_cli_resumes_exact_frozen_preview_from_partial_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = _unbootstrapped_snapshot()
    decision = _decision(snapshot)
    mutations = build_mutations(snapshot, decision)
    checksum = plan_checksum(snapshot["snapshotChecksum"], canonical_checksum(decision), mutations)
    gateway = StatefulLiveGateway(snapshot, fail_key="project:101")
    with pytest.raises(ReconcileError):
        execute_live(gateway, snapshot, decision, mutations, checksum)
    gateway.fail_key = None
    snapshot_path = tmp_path / "snapshot.json"
    decision_path = tmp_path / "decision.json"
    current_path = tmp_path / "current.json"
    preview_path = tmp_path / "preview.json"
    output_path = tmp_path / "result.json"
    snapshot_path.write_text(json.dumps(snapshot))
    decision_path.write_text(json.dumps(decision))
    current_path.write_text(json.dumps(gateway.state))
    preview_path.write_text(json.dumps({
        "schemaVersion": 1,
        "planChecksum": checksum,
        "mutations": [mutation.__dict__ for mutation in mutations],
    }))
    monkeypatch.setattr(reconcile_module, "GhGateway", lambda: gateway, raising=False)
    monkeypatch.setattr(sys, "argv", [
        "reconcile_backlog.py", "--apply", "--snapshot", str(snapshot_path),
        "--decision", str(decision_path), "--current-state", str(current_path),
        "--resume-preview", str(preview_path), "--plan-checksum", checksum,
        "--output", str(output_path),
    ])
    assert reconcile_module.main() == 0
    assert json.loads(output_path.read_text())["reconcilerState"]["status"] == "COMPLETE"


def test_reconcile_preserves_unowned_state() -> None:
    snapshot = _snapshot()
    final_state = apply_mutations(snapshot, _decision(snapshot), build_mutations(snapshot, _decision(snapshot)))
    issue = next(item for item in final_state["issues"] if item["number"] == 101)
    assert "customer-reported" in [label["name"] for label in issue["labels"]]
    assert issue["userProjectNote"] == "keep me"
    assert [101, 109] in final_state["dependencies"]["userOwned"]


def test_existing_user_label_is_reused_without_claiming_ownership() -> None:
    snapshot = _snapshot()
    snapshot["issues"][0]["labels"].append({
        "id": "LBUG", "name": "bug", "description": "Defect", "color": "d73a4a"
    })
    snapshot["labels"].append({
        "id": "LBUG", "name": "bug", "description": "Defect", "color": "d73a4a"
    })
    snapshot["snapshotChecksum"] = canonical_checksum(
        {key: value for key, value in snapshot.items() if key != "snapshotChecksum"}
    )
    decision = _decision(snapshot, label="bug")
    assert all(
        mutation.key != "label:101:bug"
        for mutation in build_mutations(snapshot, decision)
    )


def test_second_apply_is_a_noop() -> None:
    snapshot = _snapshot()
    decision = _decision(snapshot)
    first = apply_mutations(snapshot, decision, build_mutations(snapshot, decision))
    frozen_decision = copy.deepcopy(decision)
    frozen_decision["snapshotChecksum"] = first["snapshotChecksum"]
    for entry in frozen_decision["issues"]:
        issue = next(item for item in first["issues"] if item["number"] == entry["issue"])
        entry["sourceFingerprint"] = source_fingerprint(issue)
    assert build_mutations(first, frozen_decision) == []


def test_frozen_evidence_plus_fresh_managed_state_previews_zero_mutations() -> None:
    frozen_snapshot = _snapshot()
    decision = _decision(frozen_snapshot)
    first = apply_mutations(
        frozen_snapshot,
        decision,
        build_mutations(frozen_snapshot, decision),
    )
    assert build_mutations(
        frozen_snapshot,
        decision,
        current_state=first,
    ) == []


def test_frozen_fingerprint_contract_compares_normalized_source_on_readback() -> None:
    frozen_snapshot = _snapshot()
    decision = _decision(frozen_snapshot)
    for entry in decision["issues"]:
        entry["sourceFingerprint"] = f"legacy-{entry['issue']}"
    current = copy.deepcopy(frozen_snapshot)
    for issue in current["issues"]:
        issue["updatedAt"] = "2026-08-01T01:00:00Z"
        issue["projectItems"] = [{"title": "cctally-dev Issue Triage"}]
    assert build_mutations(
        frozen_snapshot,
        decision,
        current_state=current,
    )


def test_partial_apply_resumes_only_remaining_keys() -> None:
    snapshot = _snapshot(partial=True)
    decision = _decision(snapshot)
    mutations = build_mutations(snapshot, decision)
    assert "label:101:correctness" not in [mutation.key for mutation in mutations]
    assert any(mutation.key == "annotation:101" for mutation in mutations)


def test_managed_annotation_persists_reusable_record_and_ignores_rank_only_changes() -> None:
    snapshot = _snapshot()
    decision = _decision(snapshot)
    records = [
        {
            "schemaVersion": 1, "issue": issue["number"],
            "sourceFingerprint": source_fingerprint(issue), "kind": "bug",
            "currentState": "open", "userImpact": "impact", "implementationStatus": "reported",
            "evidence": [{"source": "issue", "locator": "body", "observation": "observed", "supports": ["kind"]}],
            "explicitDependencies": [], "relationshipCandidates": [], "candidateBucket": "queued",
            "labelProposals": [], "closureCandidate": "none", "subsumptionCandidate": "none",
            "evidenceQuality": "mixed", "uncertainties": [], "nextAction": "act",
            "requiresCurrentMainValidation": False,
        }
        for issue in snapshot["issues"]
    ]
    mutations = build_mutations(snapshot, decision, records=records)
    annotation = next(mutation for mutation in mutations if mutation.key == "annotation:101")
    body = annotation.payload["body"]
    assert body.startswith("first\n\n<details>\n<summary>Machine-readable triage record</summary>")
    assert body.index("first") < body.index("<!-- cctally-issue-triage:record:v1 -->")
    assert json.loads(body.split("```json\n", 1)[1].split("\n```", 1)[0]) == records[0]
    assert body.endswith("</details>")
    applied = apply_mutations(snapshot, decision, mutations)
    decision["issues"][0]["rank"], decision["issues"][1]["rank"] = 2, 1
    decision["issues"][0]["wave"], decision["issues"][1]["wave"] = 2, 1
    decision["snapshotChecksum"] = applied["snapshotChecksum"]
    assert all(
        mutation.key != "annotation:101"
        for mutation in build_mutations(applied, decision, records=records)
    )


def test_mixed_legacy_and_collapsed_annotations_rewrite_only_the_legacy_comment() -> None:
    snapshot = _snapshot()
    decision = _decision(snapshot)
    records = [
        {
            "schemaVersion": 1, "issue": issue["number"],
            "sourceFingerprint": source_fingerprint(issue), "kind": "bug",
            "currentState": "open", "userImpact": "impact", "implementationStatus": "reported",
            "evidence": [{"source": "issue", "locator": "body", "observation": "observed", "supports": ["kind"]}],
            "explicitDependencies": [], "relationshipCandidates": [], "candidateBucket": "queued",
            "labelProposals": [], "closureCandidate": "none", "subsumptionCandidate": "none",
            "evidenceQuality": "mixed", "uncertainties": [], "nextAction": "act",
            "requiresCurrentMainValidation": False,
        }
        for issue in snapshot["issues"]
    ]
    marker = "<!-- cctally-issue-triage:record:v1 -->"
    snapshot["issues"][0]["comments"] = [{
        "id": "LEGACY",
        "body": f"{marker}\n{json.dumps(records[0], sort_keys=True, separators=(',', ':'))}\n\nfirst",
    }]
    snapshot["issues"][1]["comments"] = [{
        "id": "COLLAPSED",
        "body": (
            "second\n\n<details>\n<summary>Machine-readable triage record</summary>\n\n"
            f"{marker}\n```json\n{json.dumps(records[1], sort_keys=True, separators=(',', ':'))}\n```\n</details>"
        ),
    }]

    mutation_keys = [
        mutation.key
        for mutation in build_mutations(snapshot, decision, records=records)
        if mutation.kind == "upsert_annotation"
    ]

    assert mutation_keys == ["annotation:101"]


def test_plan_checksum_rejects_snapshot_or_decision_drift() -> None:
    snapshot = _snapshot()
    decision = _decision(snapshot)
    mutations = build_mutations(snapshot, decision)
    checksum = plan_checksum(snapshot["snapshotChecksum"], canonical_checksum(decision), mutations)
    with pytest.raises(ReconcileError, match="checksum"):
        apply_mutations(snapshot, decision, mutations, expected_plan_checksum="0" * 64)
    assert len(checksum) == 64


def test_semantic_label_reuses_normalized_equivalent() -> None:
    labels = [{"id": "L1", "name": "type: Correctness", "description": "Correctness defects", "color": "ffffff"}]
    assert semantic_label(labels, {"name": "correctness", "description": "correctness defect", "color": "d73a4a"})["id"] == "L1"


def test_closed_issue_is_never_reopened() -> None:
    snapshot = _snapshot(closed=True)
    decision = _decision(snapshot)
    assert all(mutation.kind != "reopen_issue" for mutation in build_mutations(snapshot, decision))


def test_readback_mismatch_is_structural() -> None:
    snapshot = _snapshot()
    decision = _decision(snapshot)
    final_state = apply_mutations(snapshot, decision, build_mutations(snapshot, decision))
    final_state["project"]["items"]["101"]["Rank"] = 99
    with pytest.raises(ReconcileError, match="read-back mismatch.*Rank"):
        verify_readback(final_state, decision)
