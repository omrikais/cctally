from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/issue-triage"
SCRIPTS = ROOT / ".agent-workflows/skills/codex/cctally-issue-triage/scripts"
sys.path.insert(0, str(SCRIPTS))

from collect_backlog import analysis_queue, build_snapshot  # noqa: E402
from github_gateway import GhGateway, GhResult, GitHubGatewayError  # noqa: E402
from triage_contracts import canonical_checksum, source_fingerprint  # noqa: E402


REPO = "omrikais/cctally-dev"


class FixtureGateway:
    def __init__(self, fixture: str = "github-state.json") -> None:
        self.state = json.loads((FIXTURES / fixture).read_text())
        self.calls: list[tuple[str, dict]] = []

    def collect_repository(self, repo: str) -> dict:
        self.calls.append((repo, {}))
        return copy.deepcopy(self.state)


def test_build_snapshot_excludes_only_the_marked_triage_tracker() -> None:
    snapshot = build_snapshot(FixtureGateway(), REPO)
    assert [item["number"] for item in snapshot["issues"]] == [101, 102, 103]
    assert snapshot["repository"]["nameWithOwner"] == REPO
    assert snapshot["tracker"]["number"] == 900


def test_build_snapshot_contains_only_current_open_issues() -> None:
    gateway = FixtureGateway()
    gateway.state["issues"].append({
        **copy.deepcopy(gateway.state["issues"][0]),
        "number": 999,
        "state": "CLOSED",
        "title": "Already closed",
    })
    assert 999 not in [item["number"] for item in build_snapshot(gateway, REPO)["issues"]]


def test_build_snapshot_is_stable_and_preserves_complete_evidence() -> None:
    first = build_snapshot(FixtureGateway(), REPO)
    second = build_snapshot(FixtureGateway(), REPO)
    assert first == second
    assert first["snapshotChecksum"] == canonical_checksum({key: value for key, value in first.items() if key != "snapshotChecksum"})
    issue = first["issues"][0]
    assert issue["body"] and issue["comments"] and issue["labels"]
    assert issue["relationships"] and issue["linkedReferences"]


def test_analysis_queue_selects_only_records_that_need_analysis() -> None:
    snapshot = build_snapshot(FixtureGateway(), REPO)
    snapshot["issues"].extend([
        {**copy.deepcopy(snapshot["issues"][0]), "number": 104, "title": "missing"},
        {**copy.deepcopy(snapshot["issues"][0]), "number": 105, "title": "unsupported"},
    ])
    snapshot["issues"] = sorted(snapshot["issues"], key=lambda item: item["number"])
    snapshot["managedRecords"] = [
        {"issue": 101, "schemaVersion": 1, "sourceFingerprint": source_fingerprint(snapshot["issues"][0])},
        {"issue": 102, "schemaVersion": 1, "sourceFingerprint": "0" * 64},
        {"issue": 103, "schemaVersion": 1, "sourceFingerprint": source_fingerprint(snapshot["issues"][2])},
        {"issue": 105, "schemaVersion": 99, "sourceFingerprint": source_fingerprint(snapshot["issues"][4])},
    ]
    assert [(item["number"], item["reason"]) for item in analysis_queue(snapshot)] == [
        (102, "source-fingerprint-changed"),
        (104, "record-missing"),
        (105, "record-schema-unsupported"),
    ]


def test_collector_rejects_wrong_repository_identity() -> None:
    gateway = FixtureGateway()
    gateway.state["repository"]["nameWithOwner"] = "other/repo"
    with pytest.raises(GitHubGatewayError, match="repository identity"):
        build_snapshot(gateway, REPO)


def test_gateway_follows_pagination_and_uses_argv_lists() -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> GhResult:
        calls.append(argv)
        page = 2 if "cursor=CURSOR1" in argv else 1
        payload = {"nodes": [{"number": page}], "pageInfo": {"hasNextPage": page == 1, "endCursor": "CURSOR1" if page == 1 else None}}
        return GhResult(tuple(argv), 0, json.dumps(payload), "")

    gateway = GhGateway(runner=runner)
    nodes = gateway.paginate_graphql(["api", "graphql", "-f", "query=q"], purpose="issues")
    assert [node["number"] for node in nodes] == [1, 2]
    assert all(isinstance(call, list) and call[0] == "gh" for call in calls)


def test_gateway_reports_scope_and_json_failures_without_secrets() -> None:
    def denied(argv: list[str]) -> GhResult:
        return GhResult(tuple(argv), 1, "", "error: missing required scope project; token ghp_secret")

    with pytest.raises(GitHubGatewayError, match="missing required scope project") as exc:
        GhGateway(runner=denied).json(["api", "graphql"], purpose="project discovery")
    assert "ghp_secret" not in str(exc.value)

    def malformed(argv: list[str]) -> GhResult:
        return GhResult(tuple(argv), 0, "{bad", "")

    with pytest.raises(GitHubGatewayError, match="malformed JSON"):
        GhGateway(runner=malformed).json(["api", "repos/x/y"], purpose="repository")


def test_real_gateway_collects_through_supported_gh_surfaces() -> None:
    calls: list[tuple[str, ...]] = []

    def runner(argv: list[str]) -> GhResult:
        calls.append(tuple(argv))
        joined = " ".join(argv)
        if "repo view" in joined:
            payload = {"id": "R1", "nameWithOwner": REPO, "url": "https://github.test/repo"}
        elif "issue list" in joined:
            payload = [
                {"number": 101, "id": "I101", "title": "Issue", "body": "body", "state": "OPEN", "labels": [{"id": "L1", "name": "bug"}], "milestone": None, "assignees": [], "comments": [{"id": "C1", "body": "Human annotation\n\n<details>\n<summary>Machine-readable triage record</summary>\n\n<!-- cctally-issue-triage:record:v1 -->\n```json\n{\"schemaVersion\":1,\"issue\":101,\"sourceFingerprint\":\"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\"}\n```\n</details>"}], "blockedBy": {"nodes": [{"number": 99}], "totalCount": 1}, "blocking": {"nodes": [], "totalCount": 0}, "subIssues": {"nodes": [], "totalCount": 0}, "parent": None, "closedByPullRequestsReferences": [], "projectItems": []},
                {"number": 102, "id": "I102", "title": "Legacy issue", "body": "legacy body", "state": "OPEN", "labels": [], "milestone": None, "assignees": [], "comments": [{"id": "C2", "body": "<!-- cctally-issue-triage:record:v1 -->\n{\"schemaVersion\":1,\"issue\":102,\"sourceFingerprint\":\"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\"}\n\nLegacy annotation"}], "blockedBy": {"nodes": [], "totalCount": 0}, "blocking": {"nodes": [], "totalCount": 0}, "subIssues": {"nodes": [], "totalCount": 0}, "parent": None, "closedByPullRequestsReferences": [], "projectItems": []},
                {"number": 900, "id": "T1", "title": "[Tracking] Open issue triage and execution order", "body": "<!-- cctally-issue-triage:tracker:v1 -->\n<!-- cctally-issue-triage:state:v1 -->\n{\"schemaVersion\":1,\"status\":\"COMPLETE\",\"remainingMutationKeys\":[],\"ownedIssueLabels\":{\"101\":[\"bug\"]},\"ownedDependencyEdges\":[[101,99]]}\n<!-- cctally-issue-triage:state:end -->", "state": "OPEN", "labels": [], "milestone": None, "assignees": [], "comments": [], "blockedBy": {"nodes": [], "totalCount": 0}, "blocking": {"nodes": [], "totalCount": 0}, "subIssues": {"nodes": [], "totalCount": 0}, "parent": None, "closedByPullRequestsReferences": [], "projectItems": []},
            ]
        elif "label list" in joined:
            payload = [{"id": "L1", "name": "bug", "description": "Defect", "color": "d73a4a"}]
        elif "project list" in joined:
            payload = {"projects": [{"id": "P1", "number": 7, "title": "cctally-dev Issue Triage", "url": "https://github.test/projects/7"}], "totalCount": 1}
        elif "project field-list" in joined:
            payload = {"fields": [
                {"name": "Rank", "id": "F1", "type": "ProjectV2Field"},
                {"name": "Parallel group", "id": "F2", "type": "ProjectV2Field"},
            ]}
        elif "project item-list" in joined:
            payload = {"items": [{
                "id": "PI1", "content": {"number": 101, "url": "https://github.test/issues/101"},
                "rank": 1,
            }]}
        else:
            raise AssertionError(argv)
        return GhResult(tuple(argv), 0, json.dumps(payload), "")

    state = GhGateway(runner=runner).collect_repository(REPO)
    assert state["repository"]["nameWithOwner"] == REPO
    assert state["issues"][0]["relationships"] == [{"kind": "blocked-by", "issue": 99, "owned": True}]
    assert state["issues"][0]["linkedReferences"] == []
    assert state["project"]["items"]["101"] == {"Rank": 1, "Parallel group": ""}
    assert state["project"]["itemIds"] == {"101": "PI1"}
    assert state["reconcilerState"]["status"] == "COMPLETE"
    assert [(record["issue"], record["commentId"]) for record in state["managedRecords"]] == [
        (101, "C1"),
        (102, "C2"),
    ]
    assert state["issues"][0]["labels"][0]["managedByTriage"] is True
    assert state["issues"][0]["relationships"][0]["owned"] is True
    assert state["dependencies"] == {"userOwned": [], "triageOwned": [[101, 99]]}
    assert any(call[1:3] == ("issue", "list") for call in calls)
    assert next(call for call in calls if call[1:3] == ("issue", "list"))[next(call for call in calls if call[1:3] == ("issue", "list")).index("--state") + 1] == "all"
    assert all("issue-triage-state" not in " ".join(call) for call in calls)
