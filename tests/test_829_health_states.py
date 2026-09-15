"""#834 S2 (#828, #829) — the typed Codex metadata-health carrier.

Plan: ``docs/superpowers/plans/2026-09-13-834-s2-source-recovery-read-model.md``.

The carrier replaces one frozen boolean. A boolean cannot tell a row that is
deterministically unqualifiable from a read that failed and will succeed on the
next refresh, and both used to publish the same partial explanation, so a
reader was told to rebuild the cache over a transient SQLite error.
"""
from __future__ import annotations

import ast
import datetime as dt
import pathlib

import dataclasses

import pytest

import _lib_dashboard_sources as lds


UTC = dt.timezone.utc
NOW = dt.datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)
BIN = pathlib.Path(__file__).resolve().parent.parent / "bin"

#: Every explicit `SourceDashboardState(...)` construction in `bin/` must
#: either pass `metadata_health` or appear here with the reason it does not.
#: A field several constructors build independently is a field one of them
#: will drop, and this gate is what makes the omission fail a test instead of
#: silently publishing a healthy-looking generation.
_CARRIER_EXEMPT_CONSTRUCTIONS = {
    # Carries no data and an empty version by construction, so there is no
    # generation whose metadata health could be described.
    ("_lib_dashboard_sources.py", "unavailable_source_state"),
    # The All source is a COMPOSITION of two providers. Metadata health is a
    # Codex provider fact, and publishing one on the composed state would
    # invite a consumer to read provider health off the wrong object.
    ("_lib_dashboard_sources.py", "compose_all_state"),
    # Claude has no Codex conversation metadata, so the field is inapplicable
    # rather than unknown.
    ("_cctally_tui.py", "_tui_build_source_bundle"),
    # The hydrating seed has coordinated no provider ingest at all; it
    # publishes `partial`/`stale` with no data and describes nothing.
    ("_cctally_tui.py", "_tui_hydrating_source_bundle"),
}


def _health(state, rows=None):
    return lds.build_metadata_health(state, incomplete_rows=rows)


def _codex_state(health, *, availability="ok", freshness="fresh"):
    return lds.SourceDashboardState(
        source="codex",
        availability=availability,
        freshness=freshness,
        warnings=(),
        data_version="codex-version",
        last_success_at=NOW,
        capabilities={},
        data={"hero": {"cost_usd": 1.0}},
        account_scope={"real_account_count": 1},
        metadata_health=health,
    )


_EVERY_HEALTH = (
    ("healthy", None),
    ("malformed_row_partial", 3),
    ("transient_read_failure", None),
)


@pytest.mark.parametrize("state_name,rows", _EVERY_HEALTH)
def test_829_metadata_health_survives_every_copy(state_name, rows):
    """Each copy either preserves the carrier or reinitializes it on purpose."""
    import _cctally_dashboard_envelope as envelope
    import _cctally_dashboard_sources as sources
    import _cctally_tui as tui

    health = _health(state_name, rows)
    original = _codex_state(health)
    assert original.metadata_health == health

    # 1. The degrade path republishes the retained generation, so it must
    #    retain what that generation knew about its own metadata.
    warning = lds.SourceDashboardWarning(
        "source_ingest_failed", "Source ingest failed.", "ingest",
    )
    degraded = lds.degrade_source_state(original, warning)
    assert degraded.availability == "partial"
    assert degraded.metadata_health == health

    # 2. The idle clock refreshes presentation axes over the SAME rows.
    clocked = sources.refresh_codex_source_clock(original, now_utc=NOW)
    assert clocked.metadata_health == health

    # 3. The TUI's account-scope reattachment is a `dataclasses.replace`, so
    #    it preserves the field structurally — asserted rather than assumed,
    #    because a later rewrite into an explicit constructor would drop it.
    #    The scope passed here MUST differ from the original's, because
    #    `_tui_with_account_scope` returns the prior object untouched when it
    #    matches, and an identity return asserts nothing about the copy. An
    #    earlier form of this leg called `_tui_attach_account_scope`, which
    #    does not exist in `bin/`, so its `hasattr` guard was always false and
    #    this assertion never executed at all.
    assert original.account_scope != {"real_account_count": 2}
    rescoped = tui._tui_with_account_scope(original, {"real_account_count": 2})
    assert rescoped is not original
    assert rescoped.metadata_health == health

    # 3b. The fresh-build path attaches `aggregate_scope` through the same
    #     `dataclasses.replace` mechanism, so it is covered on the same axis
    #     rather than left to the constructor gate, which sees explicit
    #     constructions only.
    rescoped_range = dataclasses.replace(
        original, aggregate_scope={"start_at": "2026-09-01T00:00:00Z",
                                  "end_at": "2026-09-14T00:00:00Z"},
    )
    assert rescoped_range is not original
    assert rescoped_range.metadata_health == health

    # 4. Envelope serialization publishes it on the wire, from the carrier
    #    rather than from a second construction.
    wire = envelope._source_state_to_wire(original)
    assert wire["metadata_health"] == dict(health)

    # 5. The All composition deliberately carries none: metadata health is a
    #    provider fact and the composed state is not a provider.
    claude = lds.SourceDashboardState(
        source="claude",
        availability="ok",
        freshness="fresh",
        warnings=(),
        data_version="claude-version",
        last_success_at=NOW,
        capabilities={},
        data={"hero": {"cost_usd": 1.0}},
        account_scope={"real_account_count": 1},
    )
    composed = lds.compose_all_state(claude, original)
    assert composed.metadata_health is None


def test_829_an_older_state_without_the_carrier_serializes_to_null():
    """Never to healthy. Absence is no evidence, not a clean bill of health."""
    import _cctally_dashboard_envelope as envelope

    legacy = lds.SourceDashboardState(
        source="codex",
        availability="ok",
        freshness="fresh",
        warnings=(),
        data_version="codex-version",
        last_success_at=NOW,
        capabilities={},
        data={"hero": {}},
    )
    assert legacy.metadata_health is None
    assert envelope._source_state_to_wire(legacy)["metadata_health"] is None


def test_829_the_three_states_are_exactly_the_taxonomy():
    assert lds.METADATA_HEALTH_STATES == (
        "healthy", "malformed_row_partial", "transient_read_failure",
    )


def test_829_a_transient_failure_counts_no_rows_and_is_retryable():
    """A read that failed learned nothing, so it must not report a count.

    Reporting zero would be indistinguishable from a healthy probe, which is
    the exact confusion the typed result exists to end.
    """
    health = _health("transient_read_failure")
    assert health == {
        "state": "transient_read_failure",
        "incomplete_rows": None,
        "retryable": True,
    }


def test_829_a_malformed_row_partial_counts_its_rows_and_is_not_retryable():
    """Rebuilding the cache clears it; refreshing the dashboard does not."""
    health = _health("malformed_row_partial", 7)
    assert health == {
        "state": "malformed_row_partial",
        "incomplete_rows": 7,
        "retryable": False,
    }


def test_829_a_healthy_probe_counts_zero_rows():
    assert _health("healthy") == {
        "state": "healthy", "incomplete_rows": 0, "retryable": False,
    }


@pytest.mark.parametrize(
    "state_name,rows",
    (
        ("healthy", 1),                    # healthy cannot carry incomplete rows
        ("malformed_row_partial", 0),      # a partial with no row is not one
        ("malformed_row_partial", None),   # nor one that counted nothing
        ("transient_read_failure", 0),     # a failed read counts nothing at all
        ("nonsense", None),                # not a member of the taxonomy
    ),
)
def test_829_the_factory_refuses_a_self_contradictory_result(state_name, rows):
    with pytest.raises(ValueError):
        lds.build_metadata_health(state_name, incomplete_rows=rows)


def test_829_a_malformed_carrier_is_refused_at_construction():
    """The state validates the carrier rather than trusting its builder."""
    for bad in (
        {"state": "healthy"},
        {"state": "healthy", "incomplete_rows": 0},
        {"state": "unknown", "incomplete_rows": 0, "retryable": False},
        {"state": "healthy", "incomplete_rows": 0, "retryable": False, "x": 1},
        "healthy",
    ):
        with pytest.raises(ValueError):
            _codex_state(bad)


def test_829_the_carrier_is_frozen_on_the_state():
    health = dict(_health("malformed_row_partial", 2))
    state = _codex_state(health)
    health["incomplete_rows"] = 99
    assert state.metadata_health["incomplete_rows"] == 2
    with pytest.raises(TypeError):
        state.metadata_health["incomplete_rows"] = 99


def test_829_the_source_schema_version_moved_for_the_meaning_change():
    """Metadata availability now MEANS something different across an execvp.

    The dashboard `execvp`s itself on an in-place update while an already
    loaded client reconnects over its existing EventSource, so an old client
    does meet a new server. `docs/cli-contract.md` calls a changed meaning
    breaking, and a partial that used to mean one thing now distinguishes two.
    """
    assert lds.SOURCE_SCHEMA_VERSION == 12


def _state_constructions():
    """Every explicit `SourceDashboardState(...)` call site under `bin/`."""
    found = []
    for path in sorted(BIN.glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        parents = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name != "SourceDashboardState":
                continue
            owner = node
            while owner in parents:
                owner = parents[owner]
                if isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    break
            found.append((
                path.name,
                getattr(owner, "name", "<module>"),
                node.lineno,
                {kw.arg for kw in node.keywords},
            ))
    return found


def test_829_every_state_constructor_decides_the_carrier():
    """An explicit constructor lists every field, so an omission drops one.

    This is the same class of defect `account_scope`, `aggregate_scope` and
    `hero_cohort` each shipped a comment about at their own constructors. A
    comment is a request; this is a gate. A new construction fails here until
    it either passes `metadata_health` or is declared exempt with a reason.
    """
    constructions = _state_constructions()
    assert constructions, "the AST walk found no constructions at all"
    undecided = [
        (module, owner, lineno)
        for module, owner, lineno, kwargs in constructions
        if "metadata_health" not in kwargs
        and (module, owner) not in _CARRIER_EXEMPT_CONSTRUCTIONS
    ]
    assert not undecided, (
        "these SourceDashboardState constructions neither pass "
        f"metadata_health nor declare an exemption: {undecided}"
    )
