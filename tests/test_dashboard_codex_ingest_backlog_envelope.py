"""The `ingest_backlog` Codex source field (public #5 spec §5).

The dashboard golden harness publishes an EMPTY Codex source, so it is
structurally blind to anything this field does — a green harness run proves
nothing here. The committed oracle under ``tests/golden/`` is what makes the
claim instead: a snapshot of the Codex source subtree for a NON-EMPTY store,
captured before the field existed. Two tests diff against it.

It lives beside the other envelope goldens rather than under the dashboard
harness's own fixture root, whose scenario dirs that harness rebuilds in place
— a reader there would have to be allowlisted against the #296 overlap race for
no benefit.

Additive means two separate things, and both are asserted:

* With no backlog the field is OMITTED ENTIRELY, so the normal payload is
  byte-identical to the pre-change one. Emitting a zero would change every
  existing install's envelope for a condition that is not happening.
* With a backlog the ONLY difference from the oracle is the added subtree —
  `availability` and `freshness` in particular are untouched. Those are read by
  a long and explicitly non-exhaustive list of gates, and degrading a shared
  signal for a domain-local condition is a failure this project has already
  hit.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys
from collections.abc import Mapping

import pytest

from _cctally_dashboard_sources import DashboardReadContext

from test_dashboard_source_read_model import (  # noqa: E402
    NOW,
    START,
    _seeded_context,
)

ORACLE = (pathlib.Path(__file__).resolve().parent / "golden"
          / "codex_source_envelope_pre_ingest_backlog.json")


def _context(cache, stats):
    return DashboardReadContext(
        cache_conn=cache, stats_conn=stats, range_start=START,
        now_utc=NOW, display_tz_name="UTC",
    )


#: The wire's opaque privacy keys are salted per install, so they differ on
#: every run of the fixture. Masking them is the ONLY normalization the oracle
#: applies beyond dropping per-build provenance — the salt is not part of what
#: this test is asserting, and leaving it in would make the snapshot compare
#: nothing but noise.
_SALTED_PREFIXES = ("quota:", "project:", "session:", "account:", "conversation:")


def _plain(value):
    """Deep-convert the frozen wire into plain JSON containers.

    `Mapping`/`Sequence`, not `dict`/`list`: the wire is frozen into
    `MappingProxyType` and tuples, and `MappingProxyType` is NOT a `dict`
    subclass — a `json.dumps(default=str)` would stringify the whole subtree
    into one opaque leaf and the oracle would compare nothing.
    """
    if isinstance(value, Mapping):
        return {str(key): _plain(value[key]) for key in value}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, str):
        for prefix in _SALTED_PREFIXES:
            if value.startswith(prefix):
                return prefix + "<salted>"
        return value
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)


def _normalized(state) -> dict:
    """The source state as plain JSON, with per-run provenance dropped.

    ``data_version`` and ``last_success_at`` are per-build stamps; keeping them
    would make the oracle unfalsifiable in the wrong direction (always
    different) instead of catching a real envelope change.
    """
    return {
        "source": state.source,
        "availability": state.availability,
        "freshness": state.freshness,
        "warnings": [
            [warning.code, warning.message, warning.domain]
            for warning in state.warnings],
        "capabilities": {
            key: [record.status, record.semantics]
            for key, record in sorted(state.capabilities.items())
        },
        "domain_freshness": dict(sorted(state.domain_freshness.items())),
        "data": _plain(state.data),
    }


@pytest.fixture
def codex_state(tmp_path, monkeypatch):
    ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    source_module = sys.modules["_cctally_dashboard_sources"]

    def build():
        return _normalized(source_module.build_codex_source_state(
            _context(cache, stats), data_version="v"))

    try:
        yield ns, cache, build
    finally:
        cache.close()
        stats.close()


def _set_backlog(cache, record):
    cache.execute(
        "INSERT OR REPLACE INTO cache_meta(key, value) VALUES (?, ?)",
        ("codex_ingest_backlog", json.dumps(record, sort_keys=True)))
    cache.commit()


def _without_cache_report(state: dict) -> dict:
    """Drop `data.cache_report` from both sides of an oracle comparison.

    #443 S2 deliberately changed what the Codex cache report publishes, so
    this oracle's copy of that subtree is known-stale BY DESIGN: it captures
    the exact bug S2 fixed. Its `days` list holds one row dated 2026-07-14
    while `today.date` is 2026-07-20 — precisely the state in which both
    dashboard charts label a real older row "Today" while the spotlight
    publishes a fabricated 0%. S2 inserts a synthetic unobserved today row
    there and adds the Codex vocabulary fields (`cached_input_percent`,
    `not_applicable`, `anomaly_predicates`, `anomaly_unevaluated`,
    `observed`) beside it.

    Regenerating the oracle is NOT the fix. It was captured with `bin/`
    reverted to before the ingest-backlog change, and that provenance is the
    only reason it can falsify anything; recapturing it now would make it
    assert that the code equals itself.

    The cache-report subtree is covered instead by the two dedicated
    dashboard goldens (`codex-cache-active` / `codex-cache-idle`) and by
    tests/test_dashboard_source_read_model.py. Everything else in the Codex
    source envelope — including the `availability` and `freshness` signals
    these two tests exist to protect — is still pinned here.
    """
    return {**state,
            "data": {key: value for key, value in state["data"].items()
                     if key != "cache_report"}}


# #556 S1 §4.1 repointed `domain_freshness.hero` from percent-observation age
# to current-cycle ACCOUNTING resolvability. This fixture's Codex hero cannot
# resolve a cycle, so the axis reads `stale` where the pre-change oracle
# recorded `fresh`.
#
# Regenerating the oracle is NOT the fix, for exactly the reason stated in
# `_without_cache_report`: it was captured with `bin/` reverted to before the
# ingest-backlog change, and that provenance is the only reason it can falsify
# anything. This carve-out is one named leaf with an asserted expected value,
# so it cannot mask a second axis moving.
_REPOINTED_HERO_AXIS = "stale"


def _with_repointed_hero_axis(state: dict) -> dict:
    return {
        **state,
        "domain_freshness": {
            **state["domain_freshness"], "hero": _REPOINTED_HERO_AXIS,
        },
    }


def test_a_store_with_no_backlog_matches_the_pre_change_oracle(codex_state):
    """The byte-identity claim, against a snapshot taken before the field."""
    _ns, _cache, build = codex_state
    oracle = _with_repointed_hero_axis(json.loads(ORACLE.read_text()))
    built = build()
    # The repointing carve-out is CHECKED, not assumed: the built state must
    # actually carry the new value, so a third axis moving still reddens.
    assert built["domain_freshness"]["hero"] == _REPOINTED_HERO_AXIS
    assert _without_cache_report(built) == _without_cache_report(oracle)
    # The carve-out is scoped to one subtree, not to the field's own claim.
    assert "ingest_backlog" not in built["data"]


def test_a_backlog_adds_exactly_one_subtree_and_nothing_else(codex_state):
    """`availability` and `freshness` are NOT touched.

    They are read by a long, explicitly non-exhaustive list of gates, and this
    is a domain-local condition. The whole point of a separate additive field
    is that no shared signal has to degrade for it.
    """
    _ns, cache, build = codex_state
    oracle = _with_repointed_hero_axis(json.loads(ORACLE.read_text()))
    _set_backlog(cache, {
        "files": 4, "bytes": 8192, "since": "2026-07-16T09:00:00Z"})
    after = build()

    assert after["availability"] == oracle["availability"]
    assert after["freshness"] == oracle["freshness"]
    assert after["domain_freshness"] == oracle["domain_freshness"]
    assert after["warnings"] == oracle["warnings"]

    added = after["data"].pop("ingest_backlog")
    assert added == {
        "files": 4, "bytes": 8192, "since": "2026-07-16T09:00:00Z"}
    assert _without_cache_report(after) == _without_cache_report(oracle), (
        "the backlog field changed something other than its own subtree")


def test_a_zero_backlog_record_is_still_omitted(codex_state):
    """A record can legitimately read zero mid-drain; the field must not appear.

    Omission is what keeps the payload byte-identical for the overwhelming
    majority of installs, and a zero-valued field would defeat it.
    """
    _ns, cache, build = codex_state
    _set_backlog(cache, {"files": 0, "bytes": 0, "since": None})
    assert "ingest_backlog" not in build()["data"]


def test_a_malformed_backlog_record_is_ignored(codex_state):
    """The envelope must never fail on a hand-edited health record."""
    _ns, cache, build = codex_state
    cache.execute(
        "INSERT OR REPLACE INTO cache_meta(key, value) VALUES (?, ?)",
        ("codex_ingest_backlog", "{not json"))
    cache.commit()
    assert "ingest_backlog" not in build()["data"]


def test_the_source_schema_version_moved(codex_state):
    """An already-loaded tab DOES meet a new server.

    `cctally update` `execvp`s the server while the loaded client reconnects
    over its existing EventSource without reloading its JS, so an additive
    field still needs the version bump as a signal.
    """
    import _lib_dashboard_sources as sources

    assert sources.SOURCE_SCHEMA_VERSION >= 3


if __name__ == "__main__":  # pragma: no cover - convenience
    raise SystemExit(pytest.main([__file__, "-v"]))
