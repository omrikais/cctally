"""#620 S3 — the two-stage execution plan, cell for cell.

Spec §3 is the single authority on the route behaviour, the withheld causes,
the stores opened, the cache projection and the generation hash, and this
file is that section's matrix transcribed. Stage 1 is decided from
authorization and provider capability alone; stage 2 folds in what opening
the permitted stores revealed, and stage 2's plan is the one that is hashed.
"""
from __future__ import annotations

import pytest

# conftest puts bin/ on sys.path.
import _lib_diagnosis as k

CLAUDE_ACCOUNTING = ["model_mix", "project_concentration",
                     "session_concentration", "five_hour_bursts"]


def _by_kind(plan):
    return {c.kind: (c.mode, c.cause) for c in plan.classes}


@pytest.mark.parametrize("visible,available,expected", [
    # (transcripts_visible, conversations_available, {kind: (mode, cause)})
    (True,  True,  {"cache_churn": ("measure", None),
                    "short_high_context": ("measure", None),
                    "subagent_fanout": ("measure", None)}),
    (False, True,  {"cache_churn": ("withhold", "transcripts_not_visible"),
                    "short_high_context": ("withhold", "transcripts_not_visible"),
                    "subagent_fanout": ("withhold", "transcripts_not_visible")}),
    (True,  False, {"cache_churn": ("withhold", "signal_unavailable"),
                    "short_high_context": ("withhold", "signal_unavailable"),
                    "subagent_fanout": ("withhold", "signal_unavailable")}),
    # Denial outranks absence: the forbidden store is never probed, so
    # signal_unavailable cannot arise for a denied class.
    (False, False, {"cache_churn": ("withhold", "transcripts_not_visible"),
                    "short_high_context": ("withhold", "transcripts_not_visible"),
                    "subagent_fanout": ("withhold", "transcripts_not_visible")}),
])
def test_claude_plan_matrix(visible, available, expected):
    policy = k.resolve_policy_plan("claude", transcripts_visible=visible)
    plan = k.establish_plan(policy, conversations_available=available,
                            provider_cause=None)
    by_kind = _by_kind(plan)
    for kind in CLAUDE_ACCOUNTING:
        assert by_kind[kind] == ("measure", None)
    for kind, want in expected.items():
        assert by_kind[kind] == want


@pytest.mark.parametrize("visible,available,short_high_context", [
    (True,  True,  ("measure", None)),
    (False, True,  ("withhold", "transcripts_not_visible")),
    (True,  False, ("withhold", "signal_unavailable")),
    (False, False, ("withhold", "transcripts_not_visible")),
])
def test_codex_short_high_context_matches_the_codex_table(
        visible, available, short_high_context):
    policy = k.resolve_policy_plan("codex", transcripts_visible=visible)
    plan = k.establish_plan(policy, conversations_available=available,
                            provider_cause=None)
    assert _by_kind(plan)["short_high_context"] == short_high_context


@pytest.mark.parametrize("visible,available,fanout", [
    (True,  True,  ("measure", None)),
    (False, True,  ("measure", None)),   # D-D: cache.db only, no gate applies
    (True,  False, ("measure", None)),
    (False, False, ("measure", None)),
])
def test_codex_fanout_never_needs_transcripts(visible, available, fanout):
    policy = k.resolve_policy_plan("codex", transcripts_visible=visible)
    plan = k.establish_plan(policy, conversations_available=available,
                            provider_cause=None)
    by_kind = _by_kind(plan)
    assert by_kind["subagent_fanout"] == fanout
    assert by_kind["cache_churn"] == ("not_applicable", None)


def test_accounting_failure_overlay_preempts_every_cell():
    """A provider whose accounting store cannot be read has no denominator, so
    provider_unavailable overrides the matrix INCLUDING the cells that
    measure."""
    policy = k.resolve_policy_plan("codex", transcripts_visible=True)
    plan = k.establish_plan(policy, conversations_available=True,
                            provider_cause="provider_unavailable")
    for c in plan.classes:
        if c.kind == "cache_churn":
            # Capability outranks availability.
            assert c.mode == "not_applicable"
        else:
            assert (c.mode, c.cause) == ("withhold", "provider_unavailable")


def test_the_overlay_leaves_no_class_requiring_a_store():
    policy = k.resolve_policy_plan("claude", transcripts_visible=True)
    plan = k.establish_plan(policy, conversations_available=True,
                            provider_cause="provider_unavailable")
    assert plan.requires_conversations() is False
    assert plan.requires_s3_projection() is False


def test_a_denied_policy_plan_requires_no_conversations_connection():
    policy = k.resolve_policy_plan("claude", transcripts_visible=False)
    assert policy.requires_conversations() is False
    assert policy.requires_s3_projection() is False


def test_a_denied_codex_policy_plan_still_needs_the_s3_projection():
    """Codex fan-out reads late-added join keys out of cache.db, so a denied
    route still runs the expanded projection while opening no conversations
    connection at all."""
    policy = k.resolve_policy_plan("codex", transcripts_visible=False)
    assert policy.requires_conversations() is False
    assert policy.requires_s3_projection() is True


def test_every_plan_names_all_seven_classes_in_registry_order():
    for source in ("claude", "codex"):
        for visible in (True, False):
            policy = k.resolve_policy_plan(source, transcripts_visible=visible)
            assert [c.kind for c in policy.classes] == \
                [s.kind for s in k.CONTRIBUTOR_REGISTRY]


def test_two_different_plans_never_share_a_token():
    a = k.resolve_policy_plan("claude", transcripts_visible=True)
    b = k.resolve_policy_plan("claude", transcripts_visible=False)
    assert a.plan_token() != b.plan_token()


def test_the_same_plan_tokenizes_identically():
    a = k.resolve_policy_plan("codex", transcripts_visible=True)
    b = k.resolve_policy_plan("codex", transcripts_visible=True)
    assert a.plan_token() == b.plan_token()


def test_the_provider_is_part_of_the_token():
    a = k.resolve_policy_plan("claude", transcripts_visible=True)
    b = k.resolve_policy_plan("codex", transcripts_visible=True)
    assert a.plan_token() != b.plan_token()


def test_an_established_plan_differs_from_the_policy_plan_it_came_from():
    """The hashed plan is the ESTABLISHED one, carrying availability outcomes
    rather than intentions."""
    policy = k.resolve_policy_plan("claude", transcripts_visible=True)
    established = k.establish_plan(policy, conversations_available=False,
                                   provider_cause=None)
    assert policy.plan_token() != established.plan_token()


def test_the_four_accounting_classes_never_require_a_conversation_store():
    for source in ("claude", "codex"):
        policy = k.resolve_policy_plan(source, transcripts_visible=True)
        for c in policy.classes:
            if c.kind in CLAUDE_ACCOUNTING:
                assert c.requires_conversations is False
                assert c.requires_s3_projection is False
                assert (c.mode, c.cause) == ("measure", None)
