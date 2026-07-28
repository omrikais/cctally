"""Codex quota-pool classification (#373 spec §7.1)."""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bin"))

import pytest

from _lib_codex_pools import (
    codex_model_scoped_quota_pool,
    is_model_scoped_codex_quota,
)

STD = ('{"limitId":"codex","observedSlot":"primary","source":"codex",'
       '"sourceRootKey":"r","windowMinutes":10080}')
POOLED = ('{"limitId":"codex","modelPool":"gpt-5.3-codex-spark","observedSlot":"primary",'
          '"source":"codex","sourceRootKey":"r","windowMinutes":10080}')
BENGALFOX = ('{"limitId":"codex_bengalfox","observedSlot":"primary","source":"codex",'
             '"sourceRootKey":"r","windowMinutes":10080}')


def test_model_pool_axis():
    assert is_model_scoped_codex_quota(POOLED, None) is True


def test_native_limit_name_axis():
    # The exact phantom shape: no modelPool, pool named only by limit_name.
    assert is_model_scoped_codex_quota(BENGALFOX, "GPT-5.3-Codex-Spark") is True


def test_standard_quota_is_not_model_scoped():
    assert is_model_scoped_codex_quota(STD, None) is False


def test_limit_id_is_not_an_axis():
    # Spec §6 Q1: an unknown limit_id must NOT demote a window on its own.
    assert is_model_scoped_codex_quota(BENGALFOX, "7-day limit") is False


def test_axes_are_independent_not_sequential():
    # An unparseable key leaves axis 1 unknown but must still let axis 2 fire.
    assert is_model_scoped_codex_quota("not-json{", "GPT-5.3-Codex-Spark") is True


def test_unparseable_key_without_label_fails_closed_to_standard():
    assert is_model_scoped_codex_quota("not-json{", None) is False


@pytest.mark.parametrize("value,expected", [
    ("gpt-5.3-codex-spark", "gpt-5.3-codex-spark"),
    ("GPT-5.3-Codex-Spark", "gpt-5.3-codex-spark"),
    ("gpt-5.6-sol", None),
    (None, None),
    (123, None),
])
def test_pool_name_rule_unchanged(value, expected):
    assert codex_model_scoped_quota_pool(value) == expected


import datetime as dt

from _lib_quota import QuotaObservation, QuotaWindowIdentity, build_history

from _lib_codex_pools import codex_history_is_model_scoped


def _ident(limit_name):
    return QuotaWindowIdentity(
        source="codex", source_root_key="r", logical_limit_key=BENGALFOX,
        observed_slot="primary", window_minutes=10080,
        limit_id="codex_bengalfox", limit_name=limit_name,
    )


def _obs(limit_name, minute, pct):
    return QuotaObservation(
        identity=_ident(limit_name),
        captured_at=dt.datetime(2026, 7, 25, 8, minute, tzinfo=dt.timezone.utc),
        used_percent=pct,
        resets_at=dt.datetime(2026, 8, 1, 8, 58, tzinfo=dt.timezone.utc),
        source_path="/tmp/rollout.jsonl", line_offset=minute,
    )


def test_label_drift_first_observation_unlabelled():
    """The label arrives only on the SECOND observation.

    build_history keys the group by the FIRST identity, so history.identity
    carries limit_name=None even though the pool is Spark. Classifying off
    history.identity would silently return False here.

    The two orderings must AGREE for this fixture to pin what it claims:
    ``by_identity.setdefault`` retains the first-ITERATED identity, while the
    authority (spec §7.1) is the latest-CAPTURED physical observation.  So the
    unlabelled row is both first-iterated and earlier in time, and the labelled
    row is both second-iterated and later in time.
    """
    histories = build_history((_obs(None, 58, 0.0), _obs("GPT-5.3-Codex-Spark", 59, 1.0)))
    assert len(histories) == 1
    history = histories[0]
    assert history.identity.limit_name is None          # the trap, pinned
    # The authority genuinely disagrees with history.identity.
    assert max(
        history.physical_observations, key=lambda o: o.captured_at,
    ).identity.limit_name == "GPT-5.3-Codex-Spark"
    assert codex_history_is_model_scoped(history) is True


def test_label_drift_authority_is_the_latest_not_merely_any_observation():
    """The mirror image: the label is present only on an OLDER observation.

    Spec §7.1 names the latest physical observation as the authority, and
    unlabelled input fails closed to standard quota — a direction that can only
    keep a window in the account cycle, never demote a real one out of it.
    """
    histories = build_history((_obs(None, 59, 0.0), _obs("GPT-5.3-Codex-Spark", 58, 1.0)))
    assert len(histories) == 1
    assert codex_history_is_model_scoped(histories[0]) is False


def test_classification_does_not_depend_on_observation_iteration_order():
    """The verdict is a function of the EVIDENCE, not of insertion order.

    `build_history` retains whichever equal identity was inserted FIRST, and
    `limit_name` is `compare=False`, so the retained `history.identity` label is
    an artefact of iteration order. Consulting it — even only as a last-resort
    widening when the authoritative observation is unlabelled — makes the same
    two observations classify differently depending on the order they arrive
    in, which is why spec §7.1 declares `history.identity` non-authoritative.

    `test_label_drift_authority_is_the_latest_not_merely_any_observation`
    already pins the intended answer for this evidence: `False`.
    """
    labelled = _obs("GPT-5.3-Codex-Spark", 58, 1.0)
    unlabelled = _obs(None, 59, 0.0)
    forward = build_history((unlabelled, labelled))[0]
    reversed_ = build_history((labelled, unlabelled))[0]
    # Positive precondition: the retained identity really does differ, so the
    # two orderings genuinely exercise the divergence.
    assert forward.identity.limit_name is None
    assert reversed_.identity.limit_name == "GPT-5.3-Codex-Spark"
    # ... while the authority — the latest physical capture — is identical.
    for history in (forward, reversed_):
        assert max(
            history.physical_observations, key=lambda o: o.captured_at,
        ).identity.limit_name is None

    assert codex_history_is_model_scoped(forward) is False
    assert codex_history_is_model_scoped(reversed_) is False


def test_explicit_baseline_outranks_the_latest_physical_observation():
    """A caller holding a baseline hands it in, and it wins (spec §7.1)."""
    labelled = _obs("GPT-5.3-Codex-Spark", 58, 1.0)
    history = build_history((_obs(None, 59, 0.0), labelled))[0]
    assert codex_history_is_model_scoped(history) is False
    assert codex_history_is_model_scoped(history, baseline=labelled) is True


def test_history_standard_pool_is_not_model_scoped():
    def std(minute):
        ident = QuotaWindowIdentity(
            source="codex", source_root_key="r", logical_limit_key=STD,
            observed_slot="primary", window_minutes=10080,
            limit_id="codex", limit_name=None,
        )
        return QuotaObservation(
            identity=ident,
            captured_at=dt.datetime(2026, 7, 25, 8, minute, tzinfo=dt.timezone.utc),
            used_percent=28.0,
            resets_at=dt.datetime(2026, 7, 28, 17, 2, tzinfo=dt.timezone.utc),
            source_path="/tmp/rollout.jsonl", line_offset=minute,
        )
    assert codex_history_is_model_scoped(build_history((std(10), std(11)))[0]) is False
