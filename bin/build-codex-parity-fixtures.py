#!/usr/bin/env python3
"""Generate the synthetic #294 S0 Codex-parity contract corpus."""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from _lib_source_identity import (
    canonical_identity,
    canonical_identity_from_root_key,
    source_root_key,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO_ROOT / "tests" / "fixtures" / "codex-parity" / "v1"
SCHEMA_VERSION = 1
CORPUS_ID = "cctally-codex-parity"
SOURCES = ("claude", "codex")
CAPABILITY_STATES = ("supported", "derived", "unavailable", "deferred", "not_applicable")
ADDITIVE_MEASURES = ("usdCost", "compatibleTokenTotals", "explicitCombinedUsdBudget")
NON_ADDITIVE_MEASURES = ("quotaUsedPercent", "quotaReset", "quotaWindow", "dollarsPerPercent", "percentMilestones")
REQUIRED_SCENARIOS = (
    "modern-full", "modern-quota-payload", "modern-no-quota", "modern-partial-quota",
    "modern-dual-location-conflict", "quota-kernel-history",
    "legacy-envelope", "unknown-records", "malformed-tail", "duplicate-token-count",
    "metadata-only-tail", "root-a-collision", "root-b-collision", "nested-parent",
    "nested-child", "claude-collision", "secret-canary", "empty-source", "stale-cache",
    "claude-only", "codex-only", "mixed-source",
)

SHARED_ID = "11111111-1111-4111-8111-111111111111"
ROOT_A = "/synthetic/root-a/project-red"
ROOT_B = "/synthetic/root-b/project-blue"
MODEL = "gpt-synthetic-codex"


def _canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, records: list[dict], malformed_tail: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(_canonical_json(record) + "\n" for record in records)
    path.write_text(text + (malformed_tail or ""), encoding="utf-8")


def _session_meta(*, root: str = ROOT_A, source: object = "codex", session_id: str = SHARED_ID,
                  record_id: str = "root-thread-a", thread_source: str = "root-thread-a",
                  forked_from_id: str = "root-thread-a") -> dict:
    return {"timestamp": "2026-07-14T12:00:00Z", "type": "session_meta", "payload": {
        "id": record_id, "session_id": session_id, "cwd": root,
        "git": {"branch": "fixture-branch", "repository": "fixture-repository"},
        "source": source, "thread_source": thread_source, "forked_from_id": forked_from_id,
        "model_provider": "fixture-provider", "model": MODEL, "context_window": 272000,
        "model_context_window": 272000, "user": "fixture-user", "instructions": "synthetic instructions",
        "tools": [{"name": "fixture-tool"}],
    }}


def _rate_limits(*, malformed: bool = False) -> dict:
    secondary: object = {"used_percent": 42.0, "window_minutes": 10020, "resets_at": 1784635200}
    if malformed:
        secondary = {"used_percent": "unknown", "window_minutes": 10020}
    return {"primary": {"used_percent": 12.5, "window_minutes": 330, "resets_at": 1784048400},
            "secondary": secondary, "credits": None, "plan_type": "synthetic-plan",
            "limit_id": "synthetic-limit", "limit_name": "Synthetic limit", "individual_limit": None,
            "rate_limit_reached_type": None}


def _token_event(*, timestamp: str = "2026-07-14T12:02:00Z", total: int = 1600,
                 include_quota: bool = True, quota_at_payload: bool = False, malformed: bool = False) -> dict:
    info = {"last_token_usage": {"input_tokens": 1200, "cached_input_tokens": 300,
                                  "output_tokens": 400, "reasoning_output_tokens": 100, "total_tokens": total},
            "total_token_usage": {"total_tokens": total}, "model_context_window": 272000,
            "future_info": {"preserve_or_ignore": True}}
    payload = {"type": "token_count", "info": info}
    if include_quota:
        if quota_at_payload:
            payload["rate_limits"] = _rate_limits(malformed=malformed)
        else:
            info["rate_limits"] = _rate_limits(malformed=malformed)
    return {"timestamp": timestamp, "type": "event_msg", "payload": payload}


def _dual_location_quota_event() -> dict:
    """Direct quota fields win independently over valid ``info`` fallbacks."""
    info_limits = _rate_limits()
    info_limits["limit_id"] = "conflict-info-limit"
    info_limits["limit_name"] = "Conflict info limit"
    info_limits["plan_type"] = "conflict-info-plan"
    direct_limits = {
        "primary": {"used_percent": 77.0, "window_minutes": 440, "resets_at": 1784048400},
        "secondary": {"used_percent": "invalid", "window_minutes": "invalid", "resets_at": None},
        "plan_type": None,
        "limit_id": 123,
        "limit_name": "",
        "individual_limit": None,
        "rate_limit_reached_type": None,
    }
    event = _token_event(include_quota=False)
    event["payload"]["info"]["rate_limits"] = info_limits
    event["payload"]["rate_limits"] = direct_limits
    return event


def _response_items() -> list[dict]:
    def item(timestamp: str, payload: dict) -> dict:
        return {"timestamp": timestamp, "type": "response_item", "payload": payload}
    return [
        item("2026-07-14T12:03:00Z", {"type": "message", "role": "user", "phase": "input", "content": [{"type": "input_text", "text": "Synthetic first meaningful user prompt"}]}),
        item("2026-07-14T12:03:10Z", {"type": "message", "role": "assistant", "phase": "output", "content": [{"type": "output_text", "text": "Synthetic assistant response"}]}),
        item("2026-07-14T12:03:20Z", {"type": "reasoning", "content": [{"type": "reasoning_text", "text": "Synthetic reasoning"}], "encrypted_content": "fixture-encrypted", "summary": [{"type": "summary_text", "text": "Synthetic summary"}]}),
        item("2026-07-14T12:03:30Z", {"type": "function_call", "call_id": "fn-1", "name": "fixture_function", "arguments": "{}", "namespace": "fixture"}),
        item("2026-07-14T12:03:40Z", {"type": "function_call_output", "call_id": "fn-1", "output": {"ok": True}}),
        item("2026-07-14T12:03:50Z", {"type": "custom_tool_call", "call_id": "custom-1", "name": "fixture_custom", "input": {"q": "synthetic"}, "status": "completed"}),
        item("2026-07-14T12:04:00Z", {"type": "custom_tool_call_output", "call_id": "custom-1", "output": {"answer": "synthetic"}}),
        item("2026-07-14T12:04:10Z", {"type": "tool_search_call", "call_id": "search-1", "arguments": {"query": "synthetic"}, "execution": {"id": "exec-1"}, "status": "completed"}),
        item("2026-07-14T12:04:20Z", {"type": "tool_search_output", "call_id": "search-1", "execution": {"id": "exec-1"}, "status": "completed", "tools": [{"name": "fixture-search"}]}),
        item("2026-07-14T12:04:30Z", {"type": "web_search_call", "action": "search", "status": "completed"}),
    ]


def _lifecycle_events() -> list[dict]:
    payloads = [
        {"type": "agent_message", "message": "Synthetic agent message", "phase": "final", "memory_citation": None},
        {"type": "agent_reasoning", "text": "Synthetic agent reasoning"},
        {"type": "task_started", "collaboration_mode_kind": "parallel", "model_context_window": 272000, "started_at": "2026-07-14T12:05:00Z", "turn_id": "turn-a"},
        {"type": "task_complete", "completed_at": "2026-07-14T12:05:10Z", "duration_ms": 10, "last_agent_message": "done", "turn_id": "turn-a"},
        {"type": "context_compacted"},
        {"type": "patch_apply_end", "call_id": "patch-1", "changes": [{"path": "synthetic.txt"}], "status": "completed", "stderr": "", "stdout": "ok", "success": True, "turn_id": "turn-a"},
        {"type": "mcp_tool_call_end", "call_id": "mcp-1", "duration": 1, "invocation": {"name": "fixture"}, "result": {"ok": True}},
        {"type": "web_search_end", "action": "search", "call_id": "search-1", "query": "synthetic"},
        {"type": "user_message", "images": [], "local_images": [], "message": "Synthetic user event", "text_elements": [{"text": "Synthetic user event"}]},
    ]
    return [{"timestamp": f"2026-07-14T12:{5 + index:02d}:00Z", "type": "event_msg", "payload": payload}
            for index, payload in enumerate(payloads)]


def _scenarios() -> dict[str, tuple[list[dict], str | None]]:
    full = [_session_meta(), {"timestamp": "2026-07-14T12:01:00Z", "type": "turn_context", "payload": {"turn_id": "turn-a", "model": MODEL, "model_context_window": 272000}}, _token_event(), *_response_items(), *_lifecycle_events()]
    duplicate = [_session_meta()]
    for index, total in enumerate((1000, 1000, 3500, 3500, 7000)):
        duplicate.append(_token_event(timestamp=f"2026-07-14T12:{30 + index:02d}:00Z", total=total))
    parent = _session_meta(record_id="parent-thread-fixture", session_id="parent-thread-fixture", thread_source="parent-thread-fixture", forked_from_id="parent-thread-fixture")
    child = _session_meta(record_id="child-thread-fixture", thread_source="parent-thread-fixture", forked_from_id="parent-thread-fixture")
    quota_kernel_history = [
        {
            "timestamp": "2026-07-15T08:00:00Z",
            "type": "event_msg",
            "payload": {
                "rate_limits": {
                    "primary": {"used_percent": 10.0, "window_minutes": 120, "resets_at": 1784106000},
                    "secondary": {"used_percent": 30.0, "window_minutes": 60, "resets_at": 1784102400},
                },
                "limit_id": "quota-kernel-fixture",
                "limit_name": "Sanitized quota kernel fixture",
            },
        },
        {
            "timestamp": "2026-07-15T08:15:00Z",
            "type": "event_msg",
            "payload": {
                "rate_limits": {
                    "primary": {"used_percent": 30.0, "window_minutes": 120, "resets_at": 1784106000},
                },
                "limit_id": "quota-kernel-fixture",
                "limit_name": "Sanitized quota kernel fixture",
            },
        },
    ]
    return {
        "modern-full": (full, None),
        "modern-quota-payload": ([_session_meta(), _token_event(quota_at_payload=True)], None),
        "modern-no-quota": ([_session_meta(), _token_event(include_quota=False)], None),
        "modern-partial-quota": ([_session_meta(), _token_event(malformed=True)], None),
        "modern-dual-location-conflict": ([_session_meta(), _dual_location_quota_event()], None),
        "quota-kernel-history": (quota_kernel_history, None),
        "legacy-envelope": ([{"timestamp": "2026-07-14T12:03:00Z", "record_type": "token_count", "input_tokens": 700, "output_tokens": 200, "total_tokens": 900}], None),
        "unknown-records": ([{"timestamp": "2026-07-14T12:04:00Z", "type": "world_state", "payload": {"unknown": True}}, {"timestamp": "2026-07-14T12:05:00Z", "type": "future_record_v99", "payload": {"nested": {"future": 99}}}], None),
        "malformed-tail": ([_session_meta(), _token_event()], '{"timestamp":"2026-07-14T12:09:00Z"'),
        "duplicate-token-count": (duplicate, None),
        "metadata-only-tail": ([_token_event(), _session_meta()], None),
        "root-a-collision": ([_session_meta(root=ROOT_A, record_id="codex-root-a"), _token_event()], None),
        "root-b-collision": ([_session_meta(root=ROOT_B, record_id="codex-root-b"), _token_event()], None),
        "nested-parent": ([parent, _token_event()], None), "nested-child": ([child, _token_event()], None),
        "claude-collision": ([_session_meta(source="claude", record_id="claude-root", thread_source="claude-root", forked_from_id="claude-root"), {"timestamp": "2026-07-14T12:02:00Z", "type": "assistant", "payload": {"session_id": SHARED_ID}}], None),
        "secret-canary": ([{"timestamp": "2026-07-14T12:11:00Z", "type": "message", "payload": {"api_key_shape": "sk-fixture-not-a-secret", "authorization_shape": "Authorization: Bearer fixture-token", "home_shape": "/home/fixture-user/project-green", "root_shape": ROOT_A}}], None),
        "empty-source": ([], None),
        "stale-cache": ([_session_meta(source={"kind": "codex"}), {"timestamp": "2025-01-01T00:00:00Z", "type": "event_msg", "payload": {"type": "stale_cache_marker", "age_seconds": 999999}}], None),
        "claude-only": ([_session_meta(source="claude", record_id="claude-only", thread_source="claude-only", forked_from_id="claude-only")], None),
        "codex-only": ([_session_meta(record_id="codex-only", thread_source="codex-only", forked_from_id="codex-only")], None),
        "mixed-source": ([_session_meta(source="claude", record_id="mixed-claude", thread_source="mixed-claude", forked_from_id="mixed-claude"), _session_meta(source="codex", record_id="mixed-codex", thread_source="mixed-codex", forked_from_id="mixed-codex")], None),
    }


def _row(requirement_id, owner, sources, capability, state, scenarios, target, requirement):
    targets = [target] if isinstance(target, str) else target
    return {"id": requirement_id, "ownerSession": owner, "sources": sources, "capability": capability,
            "contractState": state, "fixtureScenarios": scenarios, "futureTestTargets": targets, "requirement": requirement}


def _acceptance_matrix() -> dict:
    p = "tests/test_codex_parity_contract.py"
    rows = [
        _row("public-capability-matrix", "S0", ["all"], "public-contract", "supported", ["modern-full"], p, "Public matrix names every outcome, owner, state, reason, and evidence."),
        _row("s1-physical-quota-retention", "S1", ["codex"], "physical-quota-retention", "supported", ["modern-full", "modern-quota-payload", "modern-dual-location-conflict"], "tests/test_codex_fused_ingest.py", "Physically retain root-qualified native quota observations and complete rollout records; S2 owns history selection and user-facing interpretation."),
        _row("s1-db-provider-root-thread-collision-safety", "S1", ["all"], "physical-qualified-identity", "supported", ["root-a-collision", "root-b-collision", "claude-collision"], "tests/test_codex_fused_ingest.py", "Database source-root and thread identities keep colliding provider values distinct; routes, FTS, export, share, and browser storage remain deferred."),
        _row("s1-codex-file-lifecycle-atomicity", "S1", ["codex"], "physical-file-lifecycle", "supported", ["malformed-tail", "metadata-only-tail", "duplicate-token-count"], "tests/test_codex_fused_ingest.py", "Per-file fused ingest atomically retains accounting, physical events, quota facts, thread facts, and its resume watermark across append, requalification, prune, rebuild, and retry."),
        _row("four-codex-reports-reconcile", "S1", ["codex"], "accounting-ingest", "supported", ["duplicate-token-count", "legacy-envelope"], "tests/test_codex_fused_ingest.py", "Existing Codex reports reconcile and preserve their deliberate dedup divergence."),
        _row("s2-quota-interpretation-cli-kernel", "S2", ["codex"], "quota-interpretation", "supported", ["modern-full", "modern-quota-payload", "stale-cache"], ["tests/test_lib_quota.py", "tests/test_codex_quota_projection.py", "tests/test_codex_quota_cli.py", "bin/cctally-codex-quota-test"], "The pure quota kernel and native history, statusline, forecast, blocks, and breakdown CLI interpret root-qualified native windows without slot assumptions."),
        _row("s4-dashboard-quota-reconciliation", "S4", ["all"], "dashboard-quota-reconciliation", "supported", ["modern-full", "modern-quota-payload", "mixed-source", "stale-cache", "root-a-collision", "root-b-collision"], ["tests/test_dashboard_source_read_model.py", "tests/test_dashboard_source_invalidation.py"], "Dashboard quota blocks and alerts reconcile to the S2 native CLI kernels without blending independent windows."),
        _row("source-derived-project-attribution", "S3", ["all"], "project-attribution", "supported", ["root-a-collision", "root-b-collision"], ["tests/test_source_aware_analytics.py", "tests/test_source_aware_cli.py", "bin/cctally-source-aware-test"], "Cwd, git, projects, diff, and range cost remain provider-qualified."),
        _row("provider-qualified-collision-safety", "S1", ["all"], "qualified-identity", "deferred", ["root-a-collision", "root-b-collision", "claude-collision"], "tests/test_codex_fused_ingest.py", "Qualified identities protect DB, routes, FTS, modals, exports, share, and browser storage."),
        _row("dashboard-source-switch-stale-safety", "S4", ["all"], "dashboard-source-state", "supported", ["claude-only", "codex-only", "mixed-source", "empty-source", "stale-cache", "malformed-tail"], ["tests/test_dashboard_source_read_model.py", "tests/test_dashboard_source_invalidation.py"], "Published source state fails closed or retains the complete prior provider generation; it never cross-falls back."),
        _row("provider-ingest-lifecycle", "S2", ["all"], "provider-ingest", "supported", ["malformed-tail", "metadata-only-tail", "stale-cache", "empty-source"], ["tests/test_codex_fused_ingest.py", "tests/test_codex_hook_lifecycle.py", "tests/test_codex_quota_cli.py"], "One S1 all-root sync per Codex lifecycle tick preserves no-sync, throttle, lock, truncation, rebuild, prune, and metadata-tail semantics."),
        _row("autonomous-codex-alerts", "S2", ["codex"], "lifecycle-alerts", "supported", ["modern-full"], ["tests/test_quota_alerts.py", "tests/test_codex_hook_lifecycle.py", "tests/test_codex_hooks_setup.py"], "Opt-in pure-Codex quota and budget alerts integrate additive setup-managed hooks, due-root evaluation, and owned-only uninstall."),
        _row("source-aware-cli-share-identity", "S3", ["all"], "share-identity", "supported", ["mixed-source", "root-a-collision"], ["tests/test_source_aware_share.py", "bin/cctally-source-aware-test"], "CLI share artifacts and the source-bearing share kernel preserve opaque source-qualified identity."),
        _row("source-aware-dashboard-share-identity", "S5", ["all"], "share-identity", "deferred", ["mixed-source", "root-a-collision"], "tests/test_source_aware_dashboard_share.py", "Dashboard share, composer, and history preserve source identity."),
        _row("s4-dashboard-share-backend-contract", "S4", ["all"], "dashboard-share-backend", "supported", ["claude-only", "codex-only", "mixed-source", "empty-source", "stale-cache", "root-a-collision", "root-b-collision", "malformed-tail"], ["tests/test_dashboard_source_share.py", "tests/test_dashboard_source_routes.py"], "Provider-qualified dashboard detail routes resolve bounded relational native data with collision safety, while canonical source-bearing share snapshots drive render, digest identity, composition, presets, and history before S5 adds source controls."),
        _row("native-codex-conversation-stack", "S8", ["codex"], "conversation-reader", "deferred", ["modern-full", "nested-child"], "dashboard/web/src/conversations/CodexConversationReader.test.tsx", "Native browse, search, reader, find, export, and live-tail consume normalized qualified identities."),
        _row("codex-anonymization-privacy-gate", "S7", ["codex"], "conversation-privacy", "deferred", ["secret-canary"], "tests/test_codex_conversation_api.py", "Roots, usernames, encoded paths, and secret patterns obey the same privacy gate."),
        _row("synthetic-source-coverage", "S0", ["all"], "synthetic-corpus", "supported", ["claude-only", "codex-only", "mixed-source", "empty-source", "stale-cache", "malformed-tail", "duplicate-token-count", "root-a-collision", "nested-child"], p, "Corpus covers source-only, mixed, empty, stale, malformed, collision, and nested cases."),
        _row("existing-codex-compatibility", "S1", ["codex"], "compatibility", "supported", ["legacy-envelope", "duplicate-token-count"], "tests/test_codex_fused_ingest.py", "Existing accounting, budget, pricing, aliases, and deliberate divergences remain compatible."),
        _row("s5-s8-ui-qa-gates", "S9", ["all"], "ui-certification", "deferred", [], "dashboard/web/src/conversations/CodexConversationReader.test.tsx", "React/CSS tests, typecheck/build, dashboard goldens, and browser QA gate S5 and S8."),
        _row("root-docs-cover-both-sources", "S9", ["all"], "documentation", "deferred", [], "tests/test_codex_parity_certification.py", "Root help, commands, dashboard, transcript/share/privacy, config, setup, and doctor docs cover both sources."),
        _row("production-scale-final-certification", "S9", ["all"], "production-scale-certification", "deferred", [], "tests/test_codex_parity_certification.py", "S9 certifies production-scale source-qualified behavior."),
        _row("tui-freeze-explicit-disposition", "S9", ["all"], "tui-governance", "not_applicable", [], "tests/test_codex_parity_certification.py", "TUI presentation stays under the approved bugfix-only freeze."),
        _row("schema-drift-tolerance", "S0", ["codex"], "schema-tolerance", "supported", ["modern-full", "modern-quota-payload", "unknown-records", "legacy-envelope"], p, "Observed schema is tolerant and captures both quota locations."),
        _row("missing-rate-limits-degrades-quota-only", "S0", ["codex"], "quota-degradation", "supported", ["modern-no-quota", "modern-partial-quota", "malformed-tail"], p, "Missing or malformed quota preserves recoverable accounting."),
        _row("report-per-source-never-blended", "S3", ["all"], "report", "supported", ["modern-full"], ["tests/test_source_aware_analytics.py", "tests/test_source_aware_cli.py", "bin/cctally-source-aware-test"], "Report and dollars per percent use S2 quota kernels but remain per source."),
        _row("percent-breakdown-per-source", "S2", ["all"], "percent-breakdown", "supported", ["modern-full"], ["tests/test_codex_quota_projection.py", "tests/test_codex_quota_cli.py", "bin/cctally-codex-quota-test"], "Native Codex quota breakdown keeps percent milestones and query-time cost correlation per source root and logical limit."),
        _row("five-hour-breakdown-per-source", "S2", ["all"], "five-hour-breakdown", "deferred", ["modern-full"], "tests/test_codex_quota.py", "Breakdowns preserve native quota identity rather than slot aliases."),
        _row("codex-refresh-is-local-rollout-reread", "S2", ["codex"], "provider-live-refresh", "unavailable", ["modern-full"], "tests/test_codex_quota.py", "Codex has no provider-live or OAuth refresh analogue."),
        _row("codex-local-rollout-quota-freshness", "S2", ["codex"], "local-rollout-quota-freshness", "supported", ["modern-full", "stale-cache"], ["tests/test_lib_quota.py", "tests/test_codex_quota_cli.py", "tests/test_codex_quota_doctor.py"], "Local rollout captures surface native-window freshness only; this is not a provider-live refresh."),
        _row("codex-cache-hit-rate-not-applicable", "S3", ["codex"], "cache-hit-rate", "not_applicable", ["modern-full"], ["tests/test_source_aware_analytics.py", "tests/test_source_aware_cli.py"], "Codex cached input is not relabeled as Claude cache hit rate."),
        _row("codex-token-reuse-forensics", "S3", ["codex"], "token-reuse", "supported", ["modern-full"], ["tests/test_source_aware_analytics.py", "tests/test_source_aware_cli.py", "bin/cctally-source-aware-test"], "Cached input is a truthful Codex token-reuse outcome."),
        _row("codex-pricing-coverage-supported", "S1", ["codex"], "pricing-coverage", "supported", ["modern-full"], "tests/test_codex_fused_ingest.py", "Existing Codex pricing coverage and drift semantics remain supported."),
        _row("codex-budget-existing-semantics", "S1", ["codex"], "budget", "supported", ["modern-full"], "tests/test_codex_fused_ingest.py", "Existing Codex budget calculation and actual/projected semantics remain supported."),
        _row("codex-title-first-prompt-fallback", "S6", ["codex"], "conversation-title", "deferred", ["modern-full"], "tests/test_codex_conversation_normalization.py", "Initial title uses the first meaningful user prompt."),
        _row("codex-threading-uses-thread-metadata", "S6", ["codex"], "conversation-threading", "deferred", ["nested-parent", "nested-child"], "tests/test_codex_conversation_normalization.py", "Nesting uses thread metadata, not filenames."),
        _row("codex-anon-plan-includes-roots", "S7", ["codex"], "anon-plan", "deferred", ["secret-canary"], "tests/test_codex_conversation_api.py", "The anonymization plan includes provider roots and labels."),
        _row("debug-backend-source-counts", "S4", ["all"], "debug-diagnostics", "supported", ["mixed-source", "stale-cache", "root-a-collision"], ["tests/test_dashboard_debug_backend.py", "tests/test_dashboard_source_invalidation.py"], "Loopback-only backend diagnostics expose source-aware aggregate counts and opaque versions without private identities."),
        _row("reading-position-qualified-key", "S8", ["all"], "reading-position", "deferred", ["root-a-collision", "root-b-collision"], "dashboard/web/src/store/readingPosition.test.ts", "Reading positions use opaque qualified conversation keys."),
        _row("dashboard-s5-after-293-s4", "S5", ["all"], "dashboard-sequencing", "deferred", [], "tests/test_source_aware_dashboard.py", "S5 waits for issue 293 S4 or an approved ownership split."),
        _row("conversation-phase-independently-deferrable", "S6", ["all"], "conversation-phase", "deferred", [], "tests/test_codex_conversation_normalization.py", "S6 through S8 may defer without falsely certifying the whole epic."),
    ]
    return {"schemaVersion": SCHEMA_VERSION, "requiredCapabilityFamilies": ["accounting", "quota", "analytics-share", "dashboard", "conversations", "lifecycle-governance"], "requirements": rows}


def _inventory_entry(variant, scenario, selector, path, required, *types):
    return {"variant": variant, "scenario": scenario, "recordSelector": selector, "path": path,
            "required": required, "types": list(types)}


def _field_inventory() -> list[dict]:
    e = _inventory_entry
    token = {"type": "event_msg", "payload.type": "token_count"}
    inventory = [
        e("modern-session-meta", "modern-full", {"type": "session_meta"}, "timestamp", True, "string"), e("modern-session-meta", "modern-full", {"type": "session_meta"}, "type", True, "string"), e("modern-session-meta", "modern-full", {"type": "session_meta"}, "payload", True, "object"),
        e("modern-session-meta", "modern-full", {"type": "session_meta"}, "payload.id", True, "string"), e("modern-session-meta", "modern-full", {"type": "session_meta"}, "payload.session_id", False, "string"), e("modern-session-meta", "modern-full", {"type": "session_meta"}, "payload.cwd", True, "string"), e("modern-session-meta", "modern-full", {"type": "session_meta"}, "payload.git", True, "object"), e("modern-session-meta", "modern-full", {"type": "session_meta"}, "payload.source", True, "string"), e("modern-session-meta", "stale-cache", {"type": "session_meta"}, "payload.source", False, "object"), e("modern-session-meta", "modern-full", {"type": "session_meta"}, "payload.thread_source", False, "string"), e("modern-session-meta", "modern-full", {"type": "session_meta"}, "payload.forked_from_id", False, "string"), e("modern-session-meta", "modern-full", {"type": "session_meta"}, "payload.model_provider", True, "string"), e("modern-session-meta", "modern-full", {"type": "session_meta"}, "payload.context_window", False, "number"), e("modern-session-meta", "modern-full", {"type": "session_meta"}, "payload.model_context_window", False, "number"),
        e("turn-context", "modern-full", {"type": "turn_context"}, "payload.turn_id", True, "string"), e("turn-context", "modern-full", {"type": "turn_context"}, "payload.model", False, "string"), e("turn-context", "modern-full", {"type": "turn_context"}, "payload.model_context_window", False, "number"),
        e("token-count-info-quota", "modern-full", token, "payload.type", True, "string"), e("token-count-info-quota", "modern-full", token, "payload.info.last_token_usage", True, "object"), e("token-count-info-quota", "modern-full", token, "payload.info.last_token_usage.input_tokens", True, "number"), e("token-count-info-quota", "modern-full", token, "payload.info.last_token_usage.cached_input_tokens", True, "number"), e("token-count-info-quota", "modern-full", token, "payload.info.last_token_usage.output_tokens", True, "number"), e("token-count-info-quota", "modern-full", token, "payload.info.last_token_usage.reasoning_output_tokens", True, "number"), e("token-count-info-quota", "modern-full", token, "payload.info.total_token_usage.total_tokens", True, "number"), e("token-count-info-quota", "modern-full", token, "payload.info.rate_limits", True, "object"), e("token-count-payload-quota", "modern-quota-payload", token, "payload.rate_limits", True, "object"),
    ]
    for scenario, path in (("modern-full", "payload.info.rate_limits"), ("modern-quota-payload", "payload.rate_limits")):
        for key, types in (("primary", ("object",)), ("secondary", ("object", "null")), ("credits", ("object", "null")), ("plan_type", ("string", "null")), ("limit_id", ("string",)), ("limit_name", ("string", "null")), ("individual_limit", ("number", "object", "null")), ("rate_limit_reached_type", ("string", "null"))):
            inventory.append(e("quota-window", scenario, token, f"{path}.{key}", key == "primary", *types))
        for window in ("primary", "secondary"):
            for key in ("used_percent", "window_minutes", "resets_at"):
                inventory.append(e("quota-window", scenario, token, f"{path}.{window}.{key}", True, "number"))
    response_fields = {
        "message": (("role", "string"), ("content", "array")),
        "reasoning": (("content", "array"), ("encrypted_content", "string"), ("summary", "array")),
        "function_call": (("arguments", "string"), ("call_id", "string"), ("name", "string"), ("namespace", "string")),
        "function_call_output": (("call_id", "string"), ("output", "object")),
        "custom_tool_call": (("call_id", "string"), ("input", "object"), ("name", "string"), ("status", "string")),
        "custom_tool_call_output": (("call_id", "string"), ("output", "object")),
        "tool_search_call": (("arguments", "object"), ("call_id", "string"), ("execution", "object"), ("status", "string")),
        "tool_search_output": (("call_id", "string"), ("execution", "object"), ("status", "string"), ("tools", "array")),
        "web_search_call": (("action", "string"), ("status", "string")),
    }
    for record_type, fields in response_fields.items():
        for field, json_type in fields:
            inventory.append(e("response-item", "modern-full", {"type": "response_item", "payload.type": record_type}, f"payload.{field}", True, json_type))
    event_fields = {
        "agent_message": (("message", "string"), ("phase", "string")), "agent_reasoning": (("text", "string"),),
        "task_started": (("collaboration_mode_kind", "string"), ("model_context_window", "number"), ("turn_id", "string")),
        "task_complete": (("duration_ms", "number"), ("last_agent_message", "string"), ("turn_id", "string")),
        "context_compacted": (), "patch_apply_end": (("call_id", "string"), ("changes", "array"), ("success", "boolean"), ("turn_id", "string")),
        "mcp_tool_call_end": (("call_id", "string"), ("duration", "number"), ("invocation", "object"), ("result", "object")),
        "web_search_end": (("action", "string"), ("call_id", "string"), ("query", "string")),
        "user_message": (("images", "array"), ("local_images", "array"), ("message", "string"), ("text_elements", "array")),
    }
    for record_type, fields in event_fields.items():
        inventory.append(e("event-taxonomy", "modern-full", {"type": "event_msg", "payload.type": record_type}, "payload.type", True, "string"))
        for field, json_type in fields:
            inventory.append(e("event-taxonomy", "modern-full", {"type": "event_msg", "payload.type": record_type}, f"payload.{field}", True, json_type))
    inventory += [e("world-state", "unknown-records", {"type": "world_state"}, "payload", True, "object"), e("legacy-envelope", "legacy-envelope", {"record_type": "token_count"}, "record_type", True, "string")]
    return inventory


def _manifest() -> dict:
    return {"corpusId": CORPUS_ID, "schemaVersion": SCHEMA_VERSION, "observedAsOf": "2026-07-14",
            "sources": list(SOURCES), "capabilityStates": list(CAPABILITY_STATES),
            "schemaPosture": {"mode": "version-tolerant-feature-detection", "unknownRecords": "ignore-or-preserve-without-aborting-accounting", "missingRateLimits": "degrade-quota-only", "partialFinalLine": "retain-valid-prefix", "snapshotIsClosedSchema": False},
            "observedRecordTypes": ["session_meta", "turn_context", "event_msg", "response_item", "world_state", "record_type"],
            "quotaWindowFields": ["observedSlot", "limitId", "limitName", "windowMinutes", "usedPercent", "resetsAtUtc", "capturedAtUtc", "planType", "individualLimit", "reachedType"],
            "identityFields": ["version", "source", "resourceKind", "sourceRootKey", "nativeKey", "parentKey"],
            "combinedArithmetic": {"additive": list(ADDITIVE_MEASURES), "nonAdditive": list(NON_ADDITIVE_MEASURES)},
            "requiredCapabilityFamilies": ["accounting", "quota", "analytics-share", "dashboard", "conversations", "lifecycle-governance"],
            "scenarios": {name: f"rollouts/{name}.jsonl" for name in REQUIRED_SCENARIOS}, "fieldInventory": _field_inventory(),
            "history": [{"version": 1, "date": "2026-07-14", "change": "Initial synthetic snapshot from issue 294 audit"}]}


def _safe_output(out_dir: Path) -> Path:
    resolved = out_dir.resolve()
    if resolved in {REPO_ROOT.resolve(), (REPO_ROOT / "tests" / "fixtures").resolve()}:
        raise ValueError("refusing to replace repository root or fixture parent")
    if resolved.exists() and resolved != DEFAULT_OUT.resolve():
        manifest_path = resolved / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            raise ValueError("refusing to replace unowned output directory") from None
        if manifest.get("corpusId") != CORPUS_ID or manifest.get("schemaVersion") != SCHEMA_VERSION:
            raise ValueError("refusing to replace unowned output directory")
    return resolved


def build(out_dir: Path = DEFAULT_OUT) -> None:
    out_dir = _safe_output(Path(out_dir))
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    for name, (records, malformed) in _scenarios().items():
        _write_jsonl(out_dir / "rollouts" / f"{name}.jsonl", records, malformed)
    _write_json(out_dir / "manifest.json", _manifest())
    _write_json(out_dir / "acceptance-matrix.json", _acceptance_matrix())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    build(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
