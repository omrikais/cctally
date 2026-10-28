"""#997 T-C4 — the quota projection load shares repeated values (D17, T17).

The final rebuild's quota projection loads every retained Codex quota
observation: 266 k `QuotaObservation` objects at about 2.3 KB each on the
maintainer's copy, each with its own copy of the root, limit and account keys,
slot, labels, `source_path`, plan type and reset instants, and its own
`QuotaWindowIdentity`. One call to `load_codex_quota_observations` now shares
each repeated text value, each reset instant per distinct stored text, and one
identity per distinct tuple of ALL its fields. Labels (`limit_id`,
`limit_name`) are outside the identity's equality, so sharing by equality
alone would hand one window's label to another; the tuple includes them.
"""
from __future__ import annotations

import datetime as dt
import importlib

import pytest

from conftest import load_script, redirect_paths

UTC = dt.timezone.utc
RESET = "2026-07-15T15:00:00+00:00"


def _iso(hour: int, minute: int = 0) -> str:
    return dt.datetime(2026, 7, 15, hour, minute, tzinfo=UTC).isoformat()


@pytest.fixture
def env(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns, importlib.import_module("_cctally_quota")


def _seed(ns, rows):
    """``rows``: dicts overriding the defaults of one quota snapshot row."""
    conn = ns["open_cache_db"]()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO codex_source_roots "
            "(source_root_key, canonical_root_path, first_seen_utc, "
            " last_seen_utc) VALUES ('root-a', '/codex/root-a', ?, ?)",
            (_iso(10), _iso(10)))
        for index, override in enumerate(rows):
            row = {
                "source_root_key": "root-a",
                "source_path": "/codex/root-a/rollout.jsonl",
                "line_offset": index,
                "captured_at_utc": _iso(10, index),
                "observed_slot": "primary",
                "logical_limit_key": "limit-primary",
                "limit_id": "native-primary",
                "limit_name": "Primary",
                "window_minutes": 300,
                "used_percent": 10.0 + index,
                "resets_at_utc": RESET,
                "plan_type": "pro",
            }
            row.update(override)
            conn.execute(
                "INSERT INTO quota_window_snapshots "
                "(source, source_root_key, source_path, line_offset, "
                " captured_at_utc, observed_slot, logical_limit_key, limit_id, "
                " limit_name, window_minutes, used_percent, resets_at_utc, "
                " plan_type, individual_limit_json, reached_type) "
                "VALUES ('codex', :source_root_key, :source_path, :line_offset,"
                " :captured_at_utc, :observed_slot, :logical_limit_key,"
                " :limit_id, :limit_name, :window_minutes, :used_percent,"
                " :resets_at_utc, :plan_type, NULL, NULL)", row)
        conn.commit()
    finally:
        conn.close()


def _load(ns, quota):
    conn = ns["open_cache_db"]()
    try:
        return quota.load_codex_quota_observations(cache_conn=conn)
    finally:
        conn.close()


def test_equal_identities_share_one_object(env):
    ns, quota = env
    _seed(ns, [{}, {}, {}])
    observations = _load(ns, quota)
    assert len(observations) == 3
    first = observations[0]
    for other in observations[1:]:
        assert other.identity is first.identity
        assert other.source_path is first.source_path
        assert other.resets_at is first.resets_at
        assert other.canonical_resets_at == first.canonical_resets_at


def test_label_difference_keeps_distinct_identities(env):
    ns, quota = env
    _seed(ns, [{}, {"limit_name": "Primary (renamed)"},
               {"limit_id": "native-primary-2"}, {}])
    observations = _load(ns, quota)
    assert len(observations) == 4
    a, renamed, relabelled, a_again = observations
    assert a.identity == renamed.identity == relabelled.identity
    assert renamed.identity is not a.identity
    assert relabelled.identity is not a.identity
    assert relabelled.identity is not renamed.identity
    assert a_again.identity is a.identity
    assert (a.identity.limit_id, a.identity.limit_name) == (
        "native-primary", "Primary")
    assert (renamed.identity.limit_id, renamed.identity.limit_name) == (
        "native-primary", "Primary (renamed)")
    assert (relabelled.identity.limit_id, relabelled.identity.limit_name) == (
        "native-primary-2", "Primary")


def test_repeated_text_values_shared(env):
    ns, quota = env
    _seed(ns, [
        {},
        {"observed_slot": "secondary", "logical_limit_key": "limit-secondary",
         "window_minutes": 10080, "limit_id": "native-secondary",
         "limit_name": "Weekly", "resets_at_utc": "2026-07-20T00:00:00+00:00"},
    ])
    primary, secondary = sorted(
        _load(ns, quota), key=lambda o: o.identity.observed_slot)
    assert primary.identity is not secondary.identity
    assert primary.source_path is secondary.source_path
    assert primary.identity.source_root_key is secondary.identity.source_root_key
    assert primary.identity.source is secondary.identity.source
    assert primary.identity.account_key == secondary.identity.account_key
    assert primary.plan_type is secondary.plan_type


class _Unshared:
    """The loader's per-call value table, with nothing shared."""

    def __init__(self):
        self._quota = importlib.import_module("_cctally_quota")

    def text(self, value):
        return value

    def reset(self, text, label):
        return self._quota._parse_utc(text, label)

    def identity(self, **fields):
        return self._quota.QuotaWindowIdentity(**fields)


def test_shared_load_equals_unshared(env, monkeypatch):
    ns, quota = env
    _seed(ns, [
        {}, {"limit_name": "Primary (renamed)"}, {},
        {"observed_slot": "secondary", "logical_limit_key": "limit-secondary",
         "window_minutes": 10080, "limit_id": None, "limit_name": None},
        {"source_path": "/codex/root-a/other.jsonl", "plan_type": None},
        {"resets_at_utc": "2026-07-15T15:00:07+00:00"},
    ])
    shared = _load(ns, quota)
    monkeypatch.setattr(quota, "_QuotaLoadValues", _Unshared)
    unshared = _load(ns, quota)
    assert len(shared) == len(unshared) == 6
    assert shared == unshared
    for left, right in zip(shared, unshared):
        assert left == right
        assert (left.identity.limit_id, left.identity.limit_name) == (
            right.identity.limit_id, right.identity.limit_name)
        assert left.identity.account_key == right.identity.account_key
