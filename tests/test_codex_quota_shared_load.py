"""One bounded Codex quota load per source build (#566 §5.1 item 3).

The spec expected four per-account loads here and attributed 6.99s to them.
Reading the profile's callers showed otherwise: the dashboard's bounded
population is already loaded ONCE, in `build_codex_source_state`, and
`_codex_account_scopes_wire` partitions that tuple in memory by
`observation.identity.account_key`. The 6.99s belonged to `doctor`'s separate
unbounded load, which `latest_per_identity` removes.

What remains here is therefore a regression guard rather than a change: the
count must not grow with the account count, the children must keep reading the
shared tuple rather than re-querying, and the partition must stay strictly by
account so one account can never observe another's windows. A model-pool
window must also stay out of account weekly quota, which is
`_lib_codex_pools`' call and not this partition's.

The remaining per-account loads are the per-BLOCK five-hour correlation loads
in `_quota_read_model`. They are a different population — no row cap, no
`active_at`, and a range anchored on the block's nominal start rather than 35
days — so they are deliberately not folded into the bounded load. Measured
cost on the maintainer's store: about 0.2s profiled across the whole build.
"""
from __future__ import annotations

import sys

from test_codex_account_read_model import (  # noqa: E402
    _ACCT_A,
    _ACCT_B,
    _build,
    _decorate,
    codex_env,  # noqa: F401  (pytest fixture, re-exported by import)
)

def _counted_loader(monkeypatch, source_module):
    """Wrap the module's loader, recording each call's bounds."""
    real = source_module.load_codex_quota_observations
    calls: list[dict] = []

    def counting(**kwargs):
        calls.append(dict(kwargs))
        return real(**kwargs)

    monkeypatch.setattr(
        source_module, "load_codex_quota_observations", counting)
    return calls


def _bounded(calls):
    limit = sys.modules[
        "_cctally_dashboard_sources"].DASHBOARD_QUOTA_OBSERVATION_LIMIT
    return [call for call in calls if call.get("max_rows") == limit]


def test_one_bounded_load_per_build_whatever_the_account_count(
    codex_env, monkeypatch,  # noqa: F811
):
    _ns, cache, stats, source_module, _root = codex_env

    calls = _counted_loader(monkeypatch, source_module)
    _build(source_module, cache, stats, version="undecorated")
    assert len(_bounded(calls)) == 1

    _decorate(stats)
    calls.clear()
    _build(source_module, cache, stats, version="decorated")
    # Two real accounts, so the children exist. The bounded load must still
    # happen once: a per-account bounded load would make this 3.
    assert len(_bounded(calls)) == 1


def test_the_bounded_load_keeps_the_dashboard_bounds(
    codex_env, monkeypatch,  # noqa: F811
):
    _ns, cache, stats, source_module, _root = codex_env
    module = sys.modules["_cctally_dashboard_sources"]
    calls = _counted_loader(monkeypatch, source_module)
    _decorate(stats)
    _build(source_module, cache, stats, version="bounds")
    bounded = _bounded(calls)[0]
    assert bounded["max_rows"] == module.DASHBOARD_QUOTA_OBSERVATION_LIMIT
    assert bounded["active_at"] is not None
    assert bounded["captured_at_or_after"] is not None
    assert bounded["source_root_keys"] is not None


def test_children_read_the_shared_tuple_rather_than_reloading(
    codex_env, monkeypatch,  # noqa: F811
):
    """The partition is in memory, so every child's observations are the very
    objects the one bounded load produced."""
    _ns, cache, stats, source_module, _root = codex_env
    _decorate(stats)
    seen: list[tuple] = []
    real_read_model = source_module._quota_read_model

    def recording(context, observations, **kwargs):
        seen.append(tuple(observations))
        return real_read_model(context, observations, **kwargs)

    monkeypatch.setattr(source_module, "_quota_read_model", recording)
    calls = _counted_loader(monkeypatch, source_module)
    _build(source_module, cache, stats, version="shared")

    assert len(_bounded(calls)) == 1
    # The parent's `_quota_read_model` runs first and receives the whole
    # bounded population; every child receives a subset of those objects.
    shared = {id(observation) for observation in seen[0]}
    for child in seen[1:]:
        assert {id(observation) for observation in child} <= shared


def test_a_child_never_sees_another_accounts_observations(
    codex_env, monkeypatch,  # noqa: F811
):
    """The partition is strictly by `identity.account_key`.

    Asserted on the observations each scope's `_quota_read_model` actually
    receives, rather than on a rendered subtree, because the rendered subtrees
    a child emits depend on which windows resolved to blocks — a shape that can
    be empty and would make the claim vacuous.
    """
    _ns, cache, stats, source_module, _root = codex_env
    _decorate(stats)
    seen: list[tuple] = []
    real_read_model = source_module._quota_read_model

    def recording(context, observations, **kwargs):
        seen.append((kwargs.get("account_key"), tuple(observations)))
        return real_read_model(context, observations, **kwargs)

    monkeypatch.setattr(source_module, "_quota_read_model", recording)
    _build(source_module, cache, stats, version="isolation")

    scoped = [(key, rows) for key, rows in seen if key is not None]
    assert {key for key, _ in scoped} >= {_ACCT_A, _ACCT_B}
    for key, rows in scoped:
        assert {row.identity.account_key for row in rows} <= {key}
    # Non-vacuity: the two REAL accounts must each have received their own
    # windows. A residual scope such as `unattributed` legitimately owns
    # none, so the emptiness claim is made only where evidence exists.
    for key in (_ACCT_A, _ACCT_B):
        rows = [row for scope, batch in scoped if scope == key
                for row in batch]
        assert rows, f"scope {key} received no observations"
        assert {row.identity.account_key for row in rows} == {key}


def test_a_model_pool_window_is_not_filed_as_account_weekly_quota():
    """The classifier stays the single home for the pool decision.

    `_codex_account_scopes_wire` partitions by account and never inspects the
    limit, so this asserts the axis the partition must not be allowed to blur:
    a Spark window is model-scoped whatever account stamped it.
    """
    from _lib_codex_pools import (
        codex_model_scoped_quota_pool,
        is_model_scoped_codex_quota,
    )

    assert codex_model_scoped_quota_pool("gpt-5.3-codex-spark") is not None
    assert codex_model_scoped_quota_pool("gpt-5") is None
    assert is_model_scoped_codex_quota("limit", "GPT-5.3-Codex-Spark")
    assert not is_model_scoped_codex_quota("limit", "weekly")
