from __future__ import annotations

import importlib.util
import json
import re
import sys
from copy import deepcopy
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BUILDER = REPO_ROOT / "bin" / "build-codex-parity-fixtures.py"
CORPUS = REPO_ROOT / "tests" / "fixtures" / "codex-parity" / "v1"
PUBLIC_DOC = REPO_ROOT / "docs" / "codex-parity.md"

REQUIRED_ACCEPTANCE_IDS = {
    "public-capability-matrix", "four-codex-reports-reconcile",
    "s1-physical-quota-retention", "s1-db-provider-root-thread-collision-safety",
    "s1-codex-file-lifecycle-atomicity",
    "s2-quota-interpretation-cli-kernel", "s4-dashboard-quota-reconciliation",
    "source-derived-project-attribution",
    "provider-qualified-collision-safety", "dashboard-source-switch-stale-safety",
    "provider-ingest-lifecycle", "autonomous-codex-alerts",
    "source-aware-cli-share-identity", "source-aware-dashboard-share-identity",
    "native-codex-conversation-stack", "codex-anonymization-privacy-gate",
    "synthetic-source-coverage", "existing-codex-compatibility", "s5-s8-ui-qa-gates",
    "root-docs-cover-both-sources", "production-scale-final-certification",
    "tui-freeze-explicit-disposition", "schema-drift-tolerance",
    "missing-rate-limits-degrades-quota-only", "report-per-source-never-blended",
    "percent-breakdown-per-source", "five-hour-breakdown-per-source",
    "codex-refresh-is-local-rollout-reread", "codex-local-rollout-quota-freshness",
    "codex-cache-hit-rate-not-applicable",
    "codex-title-first-prompt-fallback", "codex-threading-uses-thread-metadata",
    "codex-anon-plan-includes-roots", "debug-backend-source-counts",
    "s4-dashboard-share-backend-contract",
    "reading-position-qualified-key", "dashboard-s5-after-293-s4",
    "conversation-phase-independently-deferrable",
}


def _load_builder():
    spec = importlib.util.spec_from_file_location("_codex_parity_builder", BUILDER)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*")) if path.is_file()
    }


def _assert_required_scenarios(manifest, mod, root: Path) -> None:
    assert set(manifest["scenarios"]) == set(mod.REQUIRED_SCENARIOS)
    for rel in manifest["scenarios"].values():
        assert (root / rel).is_file(), rel


def _assert_arithmetic_policy(manifest, mod) -> None:
    policy = manifest["combinedArithmetic"]
    assert set(policy["additive"]) == set(mod.ADDITIVE_MEASURES)
    assert set(policy["nonAdditive"]) == set(mod.NON_ADDITIVE_MEASURES)
    assert {"quotaUsedPercent", "quotaReset", "quotaWindow", "dollarsPerPercent",
            "percentMilestones"} <= set(policy["nonAdditive"])
    assert not set(policy["additive"]) & set(policy["nonAdditive"])


def _records(root: Path, manifest, scenario: str) -> list[dict]:
    lines = (root / manifest["scenarios"][scenario]).read_text(encoding="utf-8").splitlines()
    records = []
    for line in lines:
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return records


def _get_path(value, dotted: str):
    current = value
    for segment in dotted.split("."):
        if not isinstance(current, dict) or segment not in current:
            return None, False
        current = current[segment]
    return current, True


def _json_type(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    raise AssertionError(type(value))


def _matches_selector(record: dict, selector: dict) -> bool:
    return all(found and actual == expected
               for path, expected in selector.items()
               for actual, found in [_get_path(record, path)])


def _assert_field_inventory(manifest, root: Path) -> None:
    inventory = manifest["fieldInventory"]
    assert isinstance(inventory, list)
    required_keys = {"variant", "scenario", "recordSelector", "path", "required", "types"}
    observed_quota_paths = set()
    for entry in inventory:
        assert set(entry) == required_keys
        assert entry["types"]
        records = _records(root, manifest, entry["scenario"])
        selected = [record for record in records if _matches_selector(record, entry["recordSelector"])]
        assert selected, entry
        observed = [value for record in selected for value, found in [_get_path(record, entry["path"])] if found]
        if entry["required"]:
            assert len(observed) == len(selected), entry
            assert all(_json_type(value) in set(entry["types"]) for value in observed), entry
        else:
            assert observed, entry
            assert all(_json_type(value) in set(entry["types"]) for value in observed), entry
        if entry["path"].endswith("rate_limits"):
            observed_quota_paths.add(entry["path"])
    assert observed_quota_paths == {"payload.info.rate_limits", "payload.rate_limits"}


def test_builder_contract_and_determinism(tmp_path):
    mod = _load_builder()
    assert mod.SCHEMA_VERSION == 1
    assert mod.SOURCES == ("claude", "codex")
    assert set(mod.CAPABILITY_STATES) == {
        "supported", "derived", "unavailable", "deferred", "not_applicable",
    }
    out_a = tmp_path / "a" / "v1"
    out_b = tmp_path / "b" / "v1"
    mod.build(out_a)
    mod.build(out_b)
    assert _tree_bytes(out_a) == _tree_bytes(out_b)
    assert _tree_bytes(out_a) == _tree_bytes(CORPUS)


def test_required_scenarios_are_committed():
    mod = _load_builder()
    manifest = _json(CORPUS / "manifest.json")
    _assert_required_scenarios(manifest, mod, CORPUS)


def test_arithmetic_policy_is_truthful():
    mod = _load_builder()
    manifest = _json(CORPUS / "manifest.json")
    _assert_arithmetic_policy(manifest, mod)


def test_cross_source_and_multiroot_collisions_are_real():
    mod = _load_builder()
    shared = "11111111-1111-4111-8111-111111111111"
    claude = mod.canonical_identity("claude", "conversation", None, shared, None)
    root_a = mod.canonical_identity("codex", "conversation", "root-a", shared, shared)
    root_b = mod.canonical_identity("codex", "conversation", "root-b", shared, shared)
    assert len({claude, root_a, root_b}) == 3
    assert all(key.startswith("v1.") for key in (claude, root_a, root_b))
    assert "/synthetic/" not in root_a + root_b


def test_acceptance_matrix_is_complete_and_executable_inventory():
    mod = _load_builder()
    manifest = _json(CORPUS / "manifest.json")
    matrix = _json(CORPUS / "acceptance-matrix.json")
    required_keys = {"id", "ownerSession", "sources", "capability", "contractState",
                     "fixtureScenarios", "futureTestTargets", "requirement"}
    rows = matrix["requirements"]
    assert len({row["id"] for row in rows}) == len(rows)
    assert set(matrix["requiredCapabilityFamilies"]) == set(manifest["requiredCapabilityFamilies"])
    for row in rows:
        assert set(row) == required_keys
        assert re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", row["id"])
        assert row["ownerSession"] in {f"S{i}" for i in range(10)}
        assert set(row["sources"]) <= set(mod.SOURCES) | {"all"}
        assert row["contractState"] in mod.CAPABILITY_STATES
        assert row["requirement"].strip()
        assert row["futureTestTargets"] and all("TBD" not in path for path in row["futureTestTargets"])
        assert set(row["fixtureScenarios"]) <= set(mod.REQUIRED_SCENARIOS)


def test_audit_specific_acceptance_rows_are_explicit():
    ids = {row["id"] for row in _json(CORPUS / "acceptance-matrix.json")["requirements"]}
    assert {
        "schema-drift-tolerance", "missing-rate-limits-degrades-quota-only",
        "report-per-source-never-blended", "percent-breakdown-per-source",
        "five-hour-breakdown-per-source", "codex-refresh-is-local-rollout-reread",
        "codex-cache-hit-rate-not-applicable", "codex-title-first-prompt-fallback",
        "codex-threading-uses-thread-metadata", "codex-anon-plan-includes-roots",
        "debug-backend-source-counts", "reading-position-qualified-key",
        "dashboard-s5-after-293-s4", "conversation-phase-independently-deferrable",
    } <= ids


def test_s1_physical_ingest_acceptance_rows_are_supported_and_narrow():
    """S1 certifies retained physical facts, collision-safe DB identities, and
    atomic file lifecycle only; later interpretation and UI routes stay owned
    by their deferred sessions."""
    rows = {
        row["id"]: row
        for row in _json(CORPUS / "acceptance-matrix.json")["requirements"]
    }
    expected = {
        "s1-physical-quota-retention": {
            "scenarios": {"modern-full", "modern-quota-payload", "modern-dual-location-conflict"},
        },
        "s1-db-provider-root-thread-collision-safety": {
            "scenarios": {"root-a-collision", "root-b-collision", "claude-collision"},
        },
        "s1-codex-file-lifecycle-atomicity": {
            "scenarios": {"malformed-tail", "metadata-only-tail", "duplicate-token-count"},
        },
    }
    for requirement_id, contract in expected.items():
        row = rows[requirement_id]
        assert row["ownerSession"] == "S1"
        assert row["contractState"] == "supported"
        assert set(contract["scenarios"]) <= set(row["fixtureScenarios"])
        assert row["futureTestTargets"] == ["tests/test_codex_fused_ingest.py"]

    assert "generic-quota-retention" not in rows
    assert rows["s2-quota-interpretation-cli-kernel"]["contractState"] == "supported"
    assert rows["s4-dashboard-quota-reconciliation"]["contractState"] == "supported"
    assert rows["dashboard-source-switch-stale-safety"]["ownerSession"] == "S4"
    assert rows["native-codex-conversation-stack"]["ownerSession"] == "S8"


def test_jsonl_parseability_and_named_malformed_tail():
    manifest = _json(CORPUS / "manifest.json")
    for name, rel in manifest["scenarios"].items():
        lines = (CORPUS / rel).read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            if name == "malformed-tail" and index == len(lines) - 1:
                with pytest.raises(json.JSONDecodeError):
                    json.loads(line)
            else:
                assert isinstance(json.loads(line), dict), (name, index)


def test_schema_snapshot_is_tolerant_and_quota_is_generic():
    manifest = _json(CORPUS / "manifest.json")
    assert manifest["schemaPosture"]["snapshotIsClosedSchema"] is False
    assert manifest["schemaPosture"]["missingRateLimits"] == "degrade-quota-only"
    full = [json.loads(line) for line in
            (CORPUS / manifest["scenarios"]["modern-full"]).read_text().splitlines()]
    token = next(record for record in full if record.get("payload", {}).get("type") == "token_count")
    windows = token["payload"]["info"]["rate_limits"]
    assert windows["primary"]["window_minutes"] not in {300, 10080}
    assert windows["secondary"]["window_minutes"] not in {300, 10080}
    assert "future_info" in token["payload"]["info"]


def test_nested_codex_fixture_uses_metadata_not_agent_filename():
    manifest = _json(CORPUS / "manifest.json")
    child_rel = manifest["scenarios"]["nested-child"]
    assert "agent-" not in Path(child_rel).name
    child = [json.loads(line) for line in (CORPUS / child_rel).read_text().splitlines()]
    meta = next(record for record in child if record.get("type") == "session_meta")
    assert meta["payload"]["thread_source"]


def test_fixture_corpus_contains_no_maintainer_data():
    forbidden = ("omrikaisari", "/Users/", "/Volumes/TRANSCEND", "cctally-dev",
                 "sk-proj-", "sk-ant-", "ghp_", "github_pat_")
    for rel, data in _tree_bytes(CORPUS).items():
        text = data.decode("utf-8")
        assert not any(token in text for token in forbidden), rel


def test_public_doc_names_contract_and_explicit_exceptions():
    text = PUBLIC_DOC.read_text(encoding="utf-8")
    for token in (
        "supported", "derived", "unavailable", "deferred", "not applicable",
        "report", "percent-breakdown", "five-hour-breakdown", "refresh-usage",
        "cache-report", "first meaningful user prompt", "readingPosition.ts",
        "build_anon_plan_for_db", "/api/debug/backend", "#293 S4", "TUI",
    ):
        assert token in text
    assert "never sum or average" in text


def test_required_scenario_guard_is_non_vacuous(tmp_path):
    mod = _load_builder()
    out = tmp_path / "v1"
    mod.build(out)
    manifest = _json(out / "manifest.json")
    manifest["scenarios"].pop("modern-no-quota")
    with pytest.raises(AssertionError):
        _assert_required_scenarios(manifest, mod, out)


def test_arithmetic_guard_is_non_vacuous(tmp_path):
    mod = _load_builder()
    out = tmp_path / "v1"
    mod.build(out)
    manifest = _json(out / "manifest.json")
    manifest["combinedArithmetic"]["nonAdditive"].remove("quotaUsedPercent")
    manifest["combinedArithmetic"]["additive"].append("quotaUsedPercent")
    with pytest.raises(AssertionError):
        _assert_arithmetic_policy(manifest, mod)


def test_capability_truth_and_owner_decisions_are_independent_of_builder_constants():
    rows = {row["id"]: row for row in _json(CORPUS / "acceptance-matrix.json")["requirements"]}
    expected = {
        "codex-pricing-coverage-supported": ("S1", "supported"),
        "codex-budget-existing-semantics": ("S1", "supported"),
        "s1-physical-quota-retention": ("S1", "supported"),
        "s2-quota-interpretation-cli-kernel": ("S2", "supported"),
        "s4-dashboard-quota-reconciliation": ("S4", "supported"),
        "source-derived-project-attribution": ("S3", "supported"),
        "source-aware-cli-share-identity": ("S3", "supported"),
        "source-aware-dashboard-share-identity": ("S5", "deferred"),
        "report-per-source-never-blended": ("S3", "supported"),
        "codex-cache-hit-rate-not-applicable": ("S3", "not_applicable"),
        "codex-token-reuse-forensics": ("S3", "supported"),
        "provider-ingest-lifecycle": ("S2", "supported"),
        "autonomous-codex-alerts": ("S2", "supported"),
        "percent-breakdown-per-source": ("S2", "supported"),
        "five-hour-breakdown-per-source": ("S2", "deferred"),
        "codex-refresh-is-local-rollout-reread": ("S2", "unavailable"),
        "codex-local-rollout-quota-freshness": ("S2", "supported"),
    }
    assert {key: (rows[key]["ownerSession"], rows[key]["contractState"]) for key in expected} == expected


def test_s2_completed_matrix_rows_are_generated_with_truthful_ownership_and_evidence():
    """The committed generated matrix, not only the Markdown table, records S2."""
    rows = {
        row["id"]: row
        for row in _json(CORPUS / "acceptance-matrix.json")["requirements"]
    }
    assert "generic-quota-retention" not in rows
    expected = {
        "s1-physical-quota-retention": (
            "S1", "supported", ["tests/test_codex_fused_ingest.py"],
        ),
        "s2-quota-interpretation-cli-kernel": (
            "S2", "supported", [
                "tests/test_lib_quota.py",
                "tests/test_codex_quota_projection.py",
                "tests/test_codex_quota_cli.py",
                "bin/cctally-codex-quota-test",
            ],
        ),
        "s4-dashboard-quota-reconciliation": (
            "S4", "supported", [
                "tests/test_dashboard_source_read_model.py",
                "tests/test_dashboard_source_invalidation.py",
            ],
        ),
        "provider-ingest-lifecycle": (
            "S2", "supported", [
                "tests/test_codex_fused_ingest.py",
                "tests/test_codex_hook_lifecycle.py",
                "tests/test_codex_quota_cli.py",
            ],
        ),
        "autonomous-codex-alerts": (
            "S2", "supported", [
                "tests/test_quota_alerts.py",
                "tests/test_codex_hook_lifecycle.py",
                "tests/test_codex_hooks_setup.py",
            ],
        ),
        "percent-breakdown-per-source": (
            "S2", "supported", [
                "tests/test_codex_quota_projection.py",
                "tests/test_codex_quota_cli.py",
                "bin/cctally-codex-quota-test",
            ],
        ),
        "codex-local-rollout-quota-freshness": (
            "S2", "supported", [
                "tests/test_lib_quota.py",
                "tests/test_codex_quota_cli.py",
                "tests/test_codex_quota_doctor.py",
            ],
        ),
    }
    actual = {
        requirement_id: (
            rows[requirement_id]["ownerSession"],
            rows[requirement_id]["contractState"],
            rows[requirement_id]["futureTestTargets"],
        )
        for requirement_id in expected
    }
    assert actual == expected


def test_s4_dashboard_backend_rows_are_supported_with_executable_source_contracts():
    """S4 certifies backend state; S5 still owns visible source controls."""
    rows = {
        row["id"]: row
        for row in _json(CORPUS / "acceptance-matrix.json")["requirements"]
    }
    expected_targets = {
        "s4-dashboard-quota-reconciliation": [
            "tests/test_dashboard_source_read_model.py",
            "tests/test_dashboard_source_invalidation.py",
        ],
        "dashboard-source-switch-stale-safety": [
            "tests/test_dashboard_source_read_model.py",
            "tests/test_dashboard_source_invalidation.py",
        ],
        "debug-backend-source-counts": [
            "tests/test_dashboard_debug_backend.py",
            "tests/test_dashboard_source_invalidation.py",
        ],
        "s4-dashboard-share-backend-contract": [
            "tests/test_dashboard_source_share.py",
            "tests/test_dashboard_source_routes.py",
        ],
    }
    for requirement_id, targets in expected_targets.items():
        row = rows[requirement_id]
        assert row["ownerSession"] == "S4"
        assert row["contractState"] == "supported"
        assert row["futureTestTargets"] == targets
    assert rows["source-aware-dashboard-share-identity"] == {
        **rows["source-aware-dashboard-share-identity"],
        "ownerSession": "S5",
        "contractState": "deferred",
    }
    coverage = set(rows["s4-dashboard-share-backend-contract"]["fixtureScenarios"])
    assert {
        "claude-only", "codex-only", "mixed-source", "empty-source",
        "stale-cache", "root-a-collision", "root-b-collision", "malformed-tail",
    } <= coverage


def test_field_inventory_is_observed_structured_snapshot():
    manifest = _json(CORPUS / "manifest.json")
    _assert_field_inventory(manifest, CORPUS)


def test_synthetic_taxonomy_and_identity_evidence_are_real():
    manifest = _json(CORPUS / "manifest.json")
    assert {"empty-source", "stale-cache", "claude-only", "codex-only", "mixed-source"} <= set(manifest["scenarios"])
    full = _records(CORPUS, manifest, "modern-full")
    response = [record["payload"] for record in full if record.get("type") == "response_item"]
    response_types = {item["type"] for item in response}
    assert {"message", "reasoning", "function_call", "function_call_output", "custom_tool_call", "custom_tool_call_output", "tool_search_call", "tool_search_output", "web_search_call"} <= response_types
    assert any(item["type"] == "message" and item.get("role") == "user" and item.get("content") for item in response)
    calls = {item["call_id"] for item in response if item["type"] in {"function_call", "custom_tool_call"}}
    outputs = {item["call_id"] for item in response if item["type"] in {"function_call_output", "custom_tool_call_output"}}
    assert calls <= outputs
    events = {record["payload"]["type"] for record in full if record.get("type") == "event_msg" and record.get("payload", {}).get("type") != "token_count"}
    assert {"agent_message", "agent_reasoning", "task_started", "task_complete", "context_compacted", "patch_apply_end", "mcp_tool_call_end", "web_search_end", "user_message"} <= events
    metas = [_records(CORPUS, manifest, name)[0]["payload"] for name in ("claude-collision", "root-a-collision", "root-b-collision")]
    assert {meta["session_id"] for meta in metas} == {"11111111-1111-4111-8111-111111111111"}
    assert metas[1]["cwd"] != metas[2]["cwd"]
    parent = _records(CORPUS, manifest, "nested-parent")[0]["payload"]
    child = _records(CORPUS, manifest, "nested-child")[0]["payload"]
    assert child["thread_source"] == parent["id"] == child["forked_from_id"]
    mod = _load_builder()
    keys = [mod.canonical_identity(meta["source"], "conversation", meta["cwd"], meta["session_id"], meta["thread_source"])
            for meta in metas]
    assert len(set(keys)) == 3


def test_complete_acceptance_inventory_is_hard_coded():
    ids = {row["id"] for row in _json(CORPUS / "acceptance-matrix.json")["requirements"]}
    assert REQUIRED_ACCEPTANCE_IDS <= ids


def test_builder_rejects_unowned_existing_output(tmp_path):
    mod = _load_builder()
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    sentinel = unrelated / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")
    with pytest.raises(ValueError):
        mod.build(unrelated)
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_public_matrix_current_state_and_owner_decisions_are_explicit():
    text = PUBLIC_DOC.read_text(encoding="utf-8")
    for row in (
        "| pricing coverage and drift | supported | supported | S1 |",
        "| budget calculation and actual/projected alerts | supported | supported | S1 |",
        "| quota history | supported | supported | S2 |",
        "| `report` and `$ / 1%` | supported | deferred | S3 |",
        "| `cache-report` cache hit rate | supported | not applicable | S3 |",
        "| setup-managed autonomous sync and alerts | supported | supported | S2 |",
        "| `refresh-usage` / provider-live OAuth refresh | supported | unavailable | S2 |",
        "| local rollout quota freshness/reread | supported | supported | S2 |",
    ):
        assert row in text


def test_field_inventory_requiredness_guard_is_non_vacuous(tmp_path):
    mod = _load_builder()
    out = tmp_path / "v1"
    mod.build(out)
    manifest = _json(out / "manifest.json")
    broadened = deepcopy(manifest)
    token_entry = next(entry for entry in broadened["fieldInventory"]
                       if entry["path"] == "payload.info.last_token_usage" and entry["required"])
    assert "payload.type" in token_entry["recordSelector"]
    token_entry["recordSelector"].pop("payload.type")
    with pytest.raises(AssertionError):
        _assert_field_inventory(broadened, out)
