"""Codex quota-pool classification (#373).

A pure leaf module: stdlib only, no cctally imports. It lives apart from
``_lib_quota.py`` so ``_lib_jsonl`` need not import the quota kernel (and
``_lib_accounts`` through it) merely to classify a pool label, and so module
load order in ``bin/cctally`` is unaffected.

Spec: docs/superpowers/specs/2026-07-25-373-codex-foreign-pool-phantom-week.md
"""
from __future__ import annotations

import json


def codex_model_scoped_quota_pool(model: object) -> str | None:
    """Return the native model pool when Codex documents it as separate.

    GPT Codex Spark runs against its own allowance and does not consume the
    standard Codex quota.  Accepts either the sticky rollout model or the
    native ``limit_name``; both spell the pool the same way once normalized.
    """
    if not isinstance(model, str):
        return None
    normalized = model.strip().lower()
    return normalized if "-codex-spark" in normalized else None


def is_model_scoped_codex_quota(logical_limit_key: object, limit_name: object) -> bool:
    """Whether a window sits outside account-level standard quota.

    Two INDEPENDENT axes -- an unparseable key leaves the first unknown but
    must still let the second fire.  ``limit_id`` is deliberately not an axis:
    treating an unknown id as non-standard would demote the real account quota
    the moment the provider renamed it (#373 spec §6 Q1).
    """
    if _key_has_model_pool(logical_limit_key):
        return True
    return codex_model_scoped_quota_pool(limit_name) is not None


def _key_has_model_pool(logical_limit_key: object) -> bool:
    if not isinstance(logical_limit_key, str):
        return False
    try:
        payload = json.loads(logical_limit_key)
    except (json.JSONDecodeError, TypeError, ValueError):
        return False
    return (
        isinstance(payload, dict)
        and isinstance(payload.get("modelPool"), str)
        and bool(payload["modelPool"].strip())
    )


def codex_history_is_model_scoped(history, *, baseline=None) -> bool:
    """Classify one QuotaHistory, reading the label from an AUTHORITATIVE
    observation rather than from ``history.identity``.

    ``build_history`` groups by identity equality and ``limit_id``/``limit_name``
    are ``compare=False``, so ``history.identity`` retains whichever label was
    seen FIRST.  The authority is the baseline observation when the caller has
    one, else the latest physical observation.

    ``history.identity`` is NEVER consulted for the label, not even as a
    last-resort widening when the authority is unlabelled (#373 spec §7.1).
    The retained identity is an artefact of iteration order, so reading it
    would make the same two observations classify differently depending on the
    order they arrived in — nondeterminism, not a safety net.  Unlabelled
    evidence therefore fails closed to ``False`` (standard quota), a direction
    that can only keep a window in the account cycle and never demote a real
    one out of it.
    """
    identity = getattr(history, "identity", None)
    if _key_has_model_pool(getattr(identity, "logical_limit_key", None)):
        return True
    authority = baseline
    if authority is None:
        physical = getattr(history, "physical_observations", ()) or ()
        if physical:
            authority = max(physical, key=lambda o: o.captured_at)
    label = getattr(getattr(authority, "identity", None), "limit_name", None)
    return codex_model_scoped_quota_pool(label) is not None
