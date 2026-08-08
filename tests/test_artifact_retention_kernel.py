"""#496 S6 — the pure retained-artifact kernel (`bin/_lib_artifact_retention.py`).

Spec sections §3.1 through §3.6, §4.3 and §6.5. Every test in this file runs
against the kernel alone: no filesystem, no locks, no clock, no config.
"""
from __future__ import annotations

import argparse
import dataclasses
import random
import sys
import threading
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parent.parent / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

from _lib_artifact_retention import _Selection  # noqa: E402
from _lib_artifact_retention import (  # noqa: E402
    DEFAULT_POLICY,
    RetentionGraph,
    RetentionMember,
    RetentionPolicy,
    RetentionRoot,
    RetentionState,
    backup_origin,
    backup_sidecar_applies,
    build_graph,
    classification_applies,
    deletion_closure,
    incident_classification,
    incident_is_finalized,
    plan_artifact_retention,
    resolve_retention_policy,
    validate_bundle,
    validate_incident,
    validate_rebuild_record,
)

from conftest import load_script, redirect_paths  # noqa: E402

CONFIG_KEY = "storage.artifact_retention"


# --------------------------------------------------------------------------
# Member constructors for the graph tests
# --------------------------------------------------------------------------
#
# Every axis the protection gate reads defaults to the SAFE-TO-DELETE value, so
# each test states exactly the one condition it is about. `classification`
# defaults to "exact" for the same reason: an unclassified root is protected
# (§3.2), so leaving it None would make every graph test pass for the wrong
# reason.


def _member(member_id, kind, **kw):
    fields = {
        "id": member_id,
        "kind": kind,
        "family": "stats.db",
        "created_at_epoch": 0.0,
        "disk_bytes": 0,
        "logical_bytes": 0,
        "references": (),
        "is_symlink": False,
        "in_root": True,
        "exists": True,
        "valid": True,
        "classification": "exact",
        "shape_token": None,
        "finalized": True,
        "active": False,
    }
    if "refs" in kw:
        kw["references"] = tuple(kw.pop("refs"))
    if "t" in kw:
        kw["created_at_epoch"] = float(kw.pop("t"))
    if "disk" in kw:
        kw["disk_bytes"] = kw["logical_bytes"] = int(kw.pop("disk"))
    if "shape" in kw:
        kw["shape_token"] = kw.pop("shape")
    unknown = set(kw) - set(fields)
    assert not unknown, f"unknown member field(s): {sorted(unknown)}"
    fields.update(kw)
    return RetentionMember(**fields)


def incident_m(member_id, **kw):
    return _member(member_id, "incident", **kw)


def bundle_m(member_id, **kw):
    return _member(member_id, "bundle", **kw)


def record_m(member_id, **kw):
    return _member(member_id, "rebuild_record", **kw)


def wal_m(member_id, **kw):
    return _member(member_id, "wal_evidence", **kw)


def backup_m(member_id, **kw):
    return _member(member_id, "backup", **kw)


def _build_bounded(members, seconds=20.0, builder=None):
    """`build_graph`, failing rather than hanging if it does not terminate.

    A reachability walk without a visited set does not terminate on the real
    production cycle below. Running it on a daemon thread turns that into a
    failed assertion instead of a suite that never finishes.
    """
    build = builder or build_graph
    box = {}

    def run():
        try:
            box["graph"] = build(members)
        except BaseException as exc:  # noqa: BLE001 - reported below
            box["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(seconds)
    assert not worker.is_alive(), (
        f"build_graph did not terminate within {seconds:g}s — the reachability "
        "walk is missing its visited set (spec §3.1)"
    )
    if "error" in box:
        raise box["error"]
    return box["graph"]


# --------------------------------------------------------------------------
# §6.5 — the strict policy resolver
# --------------------------------------------------------------------------


def test_missing_block_resolves_to_defaults():
    r = resolve_retention_policy({})
    assert r.status == "missing"
    assert r.policy == DEFAULT_POLICY


def test_partial_block_inherits_defaults_per_field():
    r = resolve_retention_policy({"max_age_days": 7})
    assert r.status == "valid"
    assert r.policy.max_age_seconds == 7 * 86400
    assert r.policy.max_count_per_family == DEFAULT_POLICY.max_count_per_family


def test_explicit_null_disables_that_rule():
    r = resolve_retention_policy({"max_age_days": None})
    assert r.status == "valid"
    assert r.policy.max_age_seconds is None


def test_shape_examples_is_not_nullable():
    r = resolve_retention_policy({"max_shape_examples": None})
    assert r.status == "malformed"
    assert "max_shape_examples" in r.reason


def test_disabling_every_size_rule_is_malformed():
    r = resolve_retention_policy(
        {"max_age_days": None, "max_count_per_family": None, "max_total_mib": None}
    )
    assert r.status == "malformed"


@pytest.mark.parametrize("block", [
    {"max_age_days": True},            # bool is not an int here
    {"max_age_days": 0},               # below floor
    {"max_count_per_family": 0},
    {"max_total_mib": 0},
    {"min_free_mib": -1},
    {"max_shape_examples": 0},
    {"unknown_field": 1},
    {"max_age_days": "30"},
    [],                                # not an object
])
def test_invalid_blocks_are_malformed_with_a_reason(block):
    r = resolve_retention_policy(block)
    assert r.status == "malformed"
    assert r.reason


def test_a_boolean_never_satisfies_an_integer_field():
    # isinstance(True, int) is True in Python; every integer field must
    # reject a bool explicitly or `max_count_per_family: true` becomes 1.
    for field in (
        "max_age_days", "max_count_per_family", "max_total_mib",
        "min_free_mib", "max_shape_examples",
    ):
        r = resolve_retention_policy({field: True})
        assert r.status == "malformed", field
        assert field in r.reason, field


def test_min_free_mib_may_be_zero_but_the_others_may_not():
    assert resolve_retention_policy({"min_free_mib": 0}).status == "valid"
    assert resolve_retention_policy({"max_age_days": 1}).status == "valid"
    assert resolve_retention_policy({"max_total_mib": 1}).status == "valid"


def test_min_free_mib_alone_does_not_satisfy_the_one_enabled_rule_requirement():
    # min_free_mib is a floor, not a size bound: disabling age, count and
    # total leaves nothing that bounds growth, so it stays malformed.
    r = resolve_retention_policy({
        "max_age_days": None,
        "max_count_per_family": None,
        "max_total_mib": None,
        "min_free_mib": 1024,
    })
    assert r.status == "malformed"


def test_units_convert_days_to_seconds_and_mib_to_bytes():
    r = resolve_retention_policy(
        {"max_age_days": 3, "max_total_mib": 2, "min_free_mib": 5}
    )
    assert r.policy.max_age_seconds == 3 * 86400
    assert r.policy.max_total_bytes == 2 * 1024 * 1024
    assert r.policy.min_free_bytes == 5 * 1024 * 1024


def test_the_default_policy_matches_the_spec_decision_q8():
    assert DEFAULT_POLICY.max_age_seconds == 30 * 86400
    assert DEFAULT_POLICY.max_count_per_family == 20
    assert DEFAULT_POLICY.max_total_bytes == 4096 * 1024 * 1024
    assert DEFAULT_POLICY.min_free_bytes == 10240 * 1024 * 1024
    assert DEFAULT_POLICY.max_shape_examples == 8


def test_none_resolves_to_missing():
    # An absent `storage` block reads back as None, not {}.
    r = resolve_retention_policy(None)
    assert r.status == "missing"
    assert r.policy == DEFAULT_POLICY


#: Everything the kernel is allowed to import. None of these can reach the
#: filesystem, a lock, a clock or the config.
PURE_IMPORTS = frozenset({
    "__future__", "dataclasses", "typing", "enum", "math", "re", "collections",
    "collections.abc", "numbers", "itertools", "functools", "operator",
})


#: Builtins that reach the filesystem, the clock, the environment or the
#: interpreter. None of them needs an import, so the import scan below is blind
#: to every one of them: `open("/etc/passwd")` passes it unchanged.
IMPURE_BUILTINS = frozenset({
    "open", "input", "exec", "eval", "compile", "__import__", "breakpoint",
    "print", "globals", "vars", "memoryview",
})


def _kernel_ast():
    import ast

    return ast.parse(
        (_BIN / "_lib_artifact_retention.py").read_text(encoding="utf-8")
    )


def test_the_kernel_imports_nothing_impure():
    """The module docstring's claim, checked rather than repeated.

    `import os`, `import pathlib`, `import time` or `import json` added here
    would fail this, which is the whole content of "pure kernel".
    """
    import ast

    tree = _kernel_ast()
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert imported, "no imports found — the scan did not read the module"
    assert imported <= PURE_IMPORTS, sorted(imported - PURE_IMPORTS)


def test_the_kernel_calls_no_impure_builtin():
    """The import scan cannot see a builtin, because a builtin needs no import.

    `open` is the one that matters: a kernel that read `config.json` for itself
    would arm deletion with a policy the caller never resolved, and the import
    assertion above would stay green throughout.
    """
    import ast

    called: set[str] = set()
    for node in ast.walk(_kernel_ast()):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            called.add(node.func.id)
    assert called, "no calls found — the scan did not read the module"
    assert not (called & IMPURE_BUILTINS), sorted(called & IMPURE_BUILTINS)


def test_the_builtin_scan_is_not_vacuous(tmp_path):
    """The scan above passes on an empty file too. This is what proves it can
    fail: the same walk over a module that calls `open` reports it."""
    import ast

    tree = ast.parse("def read():\n    return open('/etc/passwd').read()\n")
    called = {
        node.func.id for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert called & IMPURE_BUILTINS == {"open"}


def test_resolver_is_a_pure_function_of_its_argument():
    """The same input resolves identically, and the input is never mutated.

    Calling it and asserting nothing proved nothing: a resolver that read
    `config.json` itself, or that consumed its argument, would pass that.
    """
    block = {"max_age_days": 1}
    first = resolve_retention_policy(block)
    second = resolve_retention_policy(block)
    assert first.status == "valid"
    assert first.policy == second.policy
    assert first.policy.max_age_seconds == 86400
    assert block == {"max_age_days": 1}


def test_the_policy_is_frozen():
    with pytest.raises(Exception):
        DEFAULT_POLICY.max_age_seconds = 1  # type: ignore[misc]


# --------------------------------------------------------------------------
# §6.5 — the config surface
# --------------------------------------------------------------------------


def _load(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


def _set(ns, value):
    return ns["_cmd_config_set"](
        argparse.Namespace(key=CONFIG_KEY, value=value, emit_json=False)
    )


def _unset(ns):
    return ns["_cmd_config_unset"](argparse.Namespace(key=CONFIG_KEY))


def _raw_block(ns):
    config = ns["load_config"]()
    storage = config.get("storage") if isinstance(config, dict) else None
    if not isinstance(storage, dict):
        return None
    return storage.get("artifact_retention")


def test_the_config_key_is_registered(tmp_path, monkeypatch):
    ns = _load(tmp_path, monkeypatch)
    assert CONFIG_KEY in ns["ALLOWED_CONFIG_KEYS"]


def test_config_set_rejects_a_malformed_block(tmp_path, monkeypatch):
    ns = _load(tmp_path, monkeypatch)
    assert _set(ns, '{"max_shape_examples": null}') == 2
    assert _raw_block(ns) is None


def test_config_set_rejects_non_json(tmp_path, monkeypatch):
    ns = _load(tmp_path, monkeypatch)
    assert _set(ns, "not json") == 2


def test_config_set_persists_a_valid_block(tmp_path, monkeypatch):
    ns = _load(tmp_path, monkeypatch)
    assert _set(ns, '{"max_age_days": 1}') == 0
    resolution = resolve_retention_policy(_raw_block(ns))
    assert resolution.status == "valid"
    assert resolution.policy.max_age_seconds == 86400


def test_config_unset_restores_the_whole_default_policy(tmp_path, monkeypatch):
    ns = _load(tmp_path, monkeypatch)
    assert _set(ns, '{"max_age_days": 1}') == 0
    assert _unset(ns) == 0
    assert resolve_retention_policy(_raw_block(ns)).policy == DEFAULT_POLICY


def test_config_get_reports_the_resolved_block(tmp_path, monkeypatch):
    ns = _load(tmp_path, monkeypatch)
    assert _set(ns, '{"max_age_days": 1}') == 0
    value = ns["_config_known_value"](ns["load_config"](), CONFIG_KEY)
    assert isinstance(value, dict)
    assert value["max_age_days"] == 1
    assert value["max_shape_examples"] == 8


def test_config_get_surfaces_defaults_when_unset(tmp_path, monkeypatch):
    ns = _load(tmp_path, monkeypatch)
    value = ns["_config_known_value"](ns["load_config"](), CONFIG_KEY)
    assert value == {
        "max_age_days": 30,
        "max_count_per_family": 20,
        "max_total_mib": 4096,
        "min_free_mib": 10240,
        "max_shape_examples": 8,
    }


# --------------------------------------------------------------------------
# §3.1 — the reference graph
# --------------------------------------------------------------------------


def test_a_bundle_referenced_by_a_rebuild_record_is_not_a_root():
    g = build_graph([record_m("r1", refs=("b1",)), bundle_m("b1")])
    assert {r.id for r in g.roots} == {"r1"}


def test_an_unreferenced_bundle_is_a_root():
    g = build_graph([bundle_m("b1")])
    assert {r.id for r in g.roots} == {"b1"}


def test_an_incident_stays_a_root_even_when_something_references_it():
    # The kind rule, not the "referenced by nobody" rule, is what makes an
    # incident a root. The cycle below is exactly why: a rebuild record names
    # the incident back, so a pure in-degree test would leave BOTH nodes
    # unrooted and the whole cycle permanently invisible to the planner.
    g = build_graph([incident_m("A", refs=("r1",)), record_m("r1", refs=("A",))])
    assert {r.id for r in g.roots} == {"A"}


def test_a_backup_stem_is_always_a_root():
    g = build_graph([backup_m("stats.db.bak-corrupt-malformed-x")])
    assert {r.id for r in g.roots} == {"stats.db.bak-corrupt-malformed-x"}


def test_inbound_index_records_every_root_that_reaches_a_member():
    g = build_graph([
        incident_m("A", refs=("T",)), incident_m("B", refs=("T",)), bundle_m("T"),
    ])
    assert g.inbound_roots["T"] == frozenset({"A", "B"})


def test_a_root_reaches_itself():
    # The closure formula deletes the root itself, so the root must appear in
    # its own reachable set and in its own inbound index.
    g = build_graph([incident_m("A")])
    assert g.roots_by_id["A"].reachable_ids == frozenset({"A"})
    assert g.inbound_roots["A"] == frozenset({"A"})


def test_a_reference_cycle_terminates_and_is_traversed_once():
    # Real production shape: an incident names its rebuild record and the
    # record names the incident back (_cctally_journal.py:7122 and :7770).
    g = _build_bounded([incident_m("A", refs=("r1",)), record_m("r1", refs=("A",))])
    assert "r1" in g.roots_by_id["A"].reachable_ids
    assert g.roots_by_id["A"].reachable_ids == frozenset({"A", "r1"})


def test_a_long_reference_ring_terminates():
    # One two-node cycle can be survived by an accidental depth cap; a ring of
    # 200 cannot. Without a visited set this walk never returns.
    members = [incident_m("A", refs=("n0",))]
    members += [record_m(f"n{i}", refs=(f"n{(i + 1) % 200}",)) for i in range(200)]
    members[-1] = record_m("n199", refs=("A",))
    g = _build_bounded(members)
    assert len(g.roots_by_id["A"].reachable_ids) == 201


def test_a_reference_to_an_unknown_member_marks_the_root_invalid():
    g = build_graph([incident_m("A", refs=("missing",))])
    assert "dangling-reference" in g.roots_by_id["A"].protected_reasons


def test_a_roots_metadata_comes_from_its_own_member_never_a_neighbours():
    # §3.1: "a root's age is its own timestamp — never a neighbour's". A
    # component-oriented model took the newest member's time and pinned old
    # evidence forever.
    g = build_graph([
        incident_m("A", t=100, family="cache.db", shape="abc", refs=("T",)),
        bundle_m("T", t=9_000, family="stats.db", shape="zzz"),
    ])
    root = g.roots_by_id["A"]
    assert root.created_at_epoch == 100
    assert root.family == "cache.db"
    assert root.kind == "incident"
    assert root.shape_token == "abc"


def test_own_member_ids_lists_the_non_root_members_the_root_reaches():
    g = build_graph([
        incident_m("A", refs=("T",)), bundle_m("T", refs=("w",)), wal_m("w"),
    ])
    assert g.roots_by_id["A"].own_member_ids == frozenset({"T", "w"})


def test_roots_are_ordered_oldest_first():
    g = build_graph([incident_m("B", t=2), incident_m("A", t=1), incident_m("C", t=3)])
    assert [r.id for r in g.roots] == ["A", "B", "C"]


def test_every_member_is_indexed_even_when_no_root_reaches_it():
    g = build_graph([incident_m("A"), bundle_m("T")])
    assert set(g.members) == {"A", "T"}


def test_build_graph_rejects_a_duplicate_member_id():
    with pytest.raises(ValueError):
        build_graph([incident_m("A"), bundle_m("A")])


def test_the_termination_guard_itself_fails_on_a_walk_that_never_returns():
    """Non-vacuity for the two cycle tests above.

    Both of them would pass against a `build_graph` that hung, if the guard
    they run under could not observe the hang. It can.
    """
    stop = threading.Event()
    try:
        with pytest.raises(AssertionError, match="did not terminate"):
            _build_bounded(
                [], seconds=0.3, builder=lambda members: stop.wait(30),
            )
    finally:
        stop.set()


# --------------------------------------------------------------------------
# §3.2 — the protection gate is absolute
# --------------------------------------------------------------------------


@pytest.mark.parametrize("reason,member", [
    ("invalid",               incident_m("A", valid=False)),
    ("unfinished",            incident_m("A", finalized=False)),
    ("active",                incident_m("A", active=True)),
    ("symlink",               incident_m("A", is_symlink=True)),
    ("outside-root",          incident_m("A", in_root=False)),
    ("missing",               incident_m("A", exists=False)),
    ("unclassified",          incident_m("A", classification="unknown")),
    ("unclassified",          incident_m("A", classification=None)),
    ("dangling-reference",    incident_m("A", refs=("nowhere",))),
    ("unrecognized-kind",     _member("A", "something-new")),
    ("unreferenced-evidence", wal_m("A")),
])
def test_each_condition_protects_the_root(reason, member):
    g = build_graph([member])
    assert reason in g.roots_by_id["A"].protected_reasons


def test_a_fully_healthy_incident_is_not_protected():
    # Non-vacuity for the table above: the defaults it varies one field from
    # must themselves produce an UNprotected root, or every row passes for
    # free.
    g = build_graph([incident_m("A")])
    assert g.roots_by_id["A"].protected_reasons == ()


def test_protection_propagates_backward_from_a_shared_member():
    # The exact case a static exclusive closure loses: an invalid SHARED
    # member belongs to no root's exclusive closure.
    g = build_graph([
        incident_m("A", refs=("T",), classification="exact"),
        incident_m("B", refs=("T",), classification="exact"),
        bundle_m("T", valid=False),
    ])
    assert "invalid" in g.roots_by_id["A"].protected_reasons
    assert "invalid" in g.roots_by_id["B"].protected_reasons


def test_protection_propagates_through_a_chain_not_just_one_hop():
    g = build_graph([
        incident_m("A", refs=("T",)), bundle_m("T", refs=("w",)),
        wal_m("w", active=True),
    ])
    assert "active" in g.roots_by_id["A"].protected_reasons


def test_a_standalone_bundle_is_valid_without_an_incident_manifest():
    g = build_graph([bundle_m("b1", valid=True)])
    assert g.roots_by_id["b1"].protected_reasons == ()


def test_a_wal_evidence_directory_referenced_by_a_bundle_is_not_protected():
    # It is unprotected for the reason §3.3 gives: a valid bundle references
    # it. The unreferenced case above is the one that cannot be validated.
    g = build_graph([bundle_m("b1", refs=("w",)), wal_m("w")])
    assert g.roots_by_id["b1"].protected_reasons == ()
    assert "w" not in g.roots_by_id


def test_an_unclassified_member_does_not_protect_the_root_that_owns_it():
    # Classification is a ROOT-level condition (§3.3: a referenced bundle
    # "inherits" from its referring incident). Gating it per member would
    # protect every incident whose bundle carries no verdict of its own,
    # which is every incident.
    g = build_graph([incident_m("A", refs=("T",)), bundle_m("T", classification=None)])
    assert g.roots_by_id["A"].protected_reasons == ()


# --------------------------------------------------------------------------
# §3.3 — validation by member kind
# --------------------------------------------------------------------------


@pytest.mark.parametrize("status,valid", [
    ("ok", True), ("failed", True),
    ("pending", False), (None, False), ("weird", False), ("OK", False),
])
def test_only_enumerated_terminal_rebuild_statuses_are_valid(status, valid):
    assert validate_rebuild_record({"status": status}) is valid


def test_a_rebuild_record_that_is_not_an_object_is_invalid():
    assert validate_rebuild_record(None) is False
    assert validate_rebuild_record([]) is False


def test_moved_files_comparison_ignores_control_metadata():
    assert validate_incident(
        manifest={"movedFiles": ["cache.db"]},
        observed=["cache.db", "manifest.json", "classification.json"],
    ) is True


@pytest.mark.parametrize("manifest,observed", [
    ({"movedFiles": ["cache.db"]}, ["cache.db", "cache.db-wal"]),
    ({"movedFiles": ["cache.db", "cache.db-wal"]}, ["cache.db"]),
    ({}, ["cache.db"]),
    (None, ["cache.db"]),
    ({"movedFiles": "cache.db"}, ["cache.db"]),
])
def test_an_incident_whose_members_disagree_with_its_manifest_is_invalid(
    manifest, observed,
):
    assert validate_incident(manifest=manifest, observed=observed) is False


def test_an_empty_incident_with_an_empty_manifest_is_valid():
    # `quarantine_db_family` returns an empty incident when nothing was present.
    assert validate_incident(manifest={"movedFiles": []}, observed=[]) is True


@pytest.mark.parametrize("payload,valid", [
    ({"schemaVersion": 1}, True),
    ({"schemaVersion": 2, "trigger": {"origin": "db-rebuild"}}, True),
    ({}, False),
    ({"schemaVersion": None}, False),
    (None, False),
])
def test_a_bundle_is_valid_when_it_parses_and_carries_a_schema_version(
    payload, valid,
):
    assert validate_bundle(payload) is valid


# --------------------------------------------------------------------------
# §3.3 — classification, and the exact predicate S1 settled
# --------------------------------------------------------------------------


def test_a_classification_naming_another_incident_does_not_classify_this_one():
    assert classification_applies(
        verdict={"incident": "other", "confidence": "exact"}, incident_name="A",
    ) is False


@pytest.mark.parametrize("verdict,applies", [
    ({"incident": "A", "confidence": "exact"}, True),
    ({"incident": "A", "confidence": "candidate"}, True),
    ({"incident": "A", "confidence": "unknown"}, False),
    ({"incident": "A"}, False),
    ({"confidence": "exact"}, False),
    (None, False),
])
def test_a_classification_applies_only_when_it_names_this_incident_and_decided(
    verdict, applies,
):
    assert classification_applies(verdict=verdict, incident_name="A") is applies


def test_the_verdict_field_read_is_confidence_and_never_verdict():
    # A `verdict` key was read once during design, which reports every
    # incident as unclassified. The field is `confidence`.
    assert classification_applies(
        verdict={"incident": "A", "verdict": "exact"}, incident_name="A",
    ) is False


def test_a_v2_manifest_with_a_truthy_trigger_classifies_exactly():
    assert incident_classification(
        manifest={"schemaVersion": 2, "trigger": "cache.open"},
        verdict=None,
        incident_name="A",
    ) == "exact"


@pytest.mark.parametrize("manifest", [
    {"schemaVersion": 2},
    {"schemaVersion": 2, "trigger": ""},
    {"schemaVersion": 2, "trigger": None},
    {"schemaVersion": 1, "trigger": "cache.open"},
])
def test_a_manifest_that_does_not_meet_both_halves_does_not_classify(manifest):
    assert incident_classification(
        manifest=manifest, verdict=None, incident_name="A",
    ) is None


def test_a_sidecar_verdict_classifies_when_the_manifest_does_not():
    assert incident_classification(
        manifest={"schemaVersion": 1},
        verdict={"incident": "A", "confidence": "candidate"},
        incident_name="A",
    ) == "candidate"


def test_the_manifest_wins_over_a_weaker_sidecar():
    assert incident_classification(
        manifest={"schemaVersion": 2, "trigger": "cache.open"},
        verdict={"incident": "A", "confidence": "candidate"},
        incident_name="A",
    ) == "exact"


# --------------------------------------------------------------------------
# §3.3 / §3.7 — backup families
# --------------------------------------------------------------------------


def _bmember(name, inode, *, device=1, size=10):
    return {"name": name, "device": device, "inode": inode, "size": size}


def test_a_backup_sidecar_whose_identities_moved_does_not_authorize_deletion():
    assert backup_sidecar_applies(
        sidecar={"confidence": "exact", "members": [_bmember("stats.db.bak-x", 1)]},
        observed=[_bmember("stats.db.bak-x", 2)],
    ) is False


def test_a_backup_sidecar_matching_the_family_on_disk_authorizes_deletion():
    members = [_bmember("stats.db.bak-x", 1), _bmember("stats.db.bak-x-wal", 2)]
    assert backup_sidecar_applies(
        sidecar={"confidence": "exact", "members": members},
        observed=list(reversed(members)),
    ) is True


@pytest.mark.parametrize("observed", [
    [_bmember("stats.db.bak-x", 1, device=9)],          # different device
    [_bmember("stats.db.bak-x", 1, size=11)],           # same inode, resized
    [],                                                  # family gone
    [_bmember("stats.db.bak-x", 1), _bmember("stats.db.bak-x-wal", 2)],  # extra
])
def test_a_backup_family_that_no_longer_matches_is_not_authorized(observed):
    assert backup_sidecar_applies(
        sidecar={"confidence": "exact", "members": [_bmember("stats.db.bak-x", 1)]},
        observed=observed,
    ) is False


def test_a_backup_sidecar_with_an_undecided_confidence_authorizes_nothing():
    assert backup_sidecar_applies(
        sidecar={"confidence": "unknown", "members": [_bmember("s.bak-x", 1)]},
        observed=[_bmember("s.bak-x", 1)],
    ) is False


@pytest.mark.parametrize("name,origin", [
    ("stats.db.bak-corrupt-malformed-20260807T101112Z", "machine"),
    ("cache.db.bak-corrupt-malformed-20260807T101112Z", "machine"),
    ("stats.db.bak-20260807T101112Z", "user"),
    ("stats.db.bak-pre-011-reversal", "unknown"),
    ("stats.db.bak-feb27-stale-event", "unknown"),
    ("stats.db.bak-temp-dashboard", "unknown"),
    ("stats.db", "unknown"),
])
def test_the_three_backup_naming_shapes_are_told_apart(name, origin):
    assert backup_origin(name) == origin


# --------------------------------------------------------------------------
# §3.2 — "unfinished", and the manifests that predate the `complete` key
# --------------------------------------------------------------------------


def test_a_live_pending_marker_means_unfinished():
    assert incident_is_finalized(
        manifest={"movedFiles": [], "complete": True}, pending_marker_present=True,
    ) is False


def test_an_explicit_incomplete_manifest_means_unfinished():
    assert incident_is_finalized(
        manifest={"complete": False}, pending_marker_present=False,
    ) is False


def test_a_manifest_predating_the_complete_key_is_finished():
    # 22 of the 142 incidents on the maintainer's store carry no `complete`
    # key, holding 8.83 GiB — including every cache incident §4.3's
    # correlation was measured to unlock. The manifest is written only after
    # every member has been moved, so its existence already proves the move
    # finished; treating an absent key as unfinished would protect that 8.83
    # GiB forever and make acceptance criterion 2 unreachable.
    assert incident_is_finalized(
        manifest={"schemaVersion": 1, "movedFiles": ["cache.db"]},
        pending_marker_present=False,
    ) is True


# --------------------------------------------------------------------------
# §3.1 / §3.4 / §3.5 / §3.6 — the planner
# --------------------------------------------------------------------------
#
# Every test below injects its own thresholds. A fixture compared against
# DEFAULT_POLICY cannot fail: the production budget is 4096 MiB and no fixture
# here reaches a kilobyte.


def policy(**kw):
    fields = {
        "max_age_seconds": None,
        "max_count_per_family": None,
        "max_total_bytes": None,
        "min_free_bytes": None,
        "max_shape_examples": 8,
    }
    unknown = set(kw) - set(fields)
    assert not unknown, f"unknown policy field(s): {sorted(unknown)}"
    fields.update(kw)
    return RetentionPolicy(**fields)


def state_of(*members, now=1000.0, free=None):
    return RetentionState(
        graph=build_graph(members), now_epoch=now, free_disk_bytes=free,
    )


def test_the_older_of_two_classified_roots_is_selected_under_a_tiny_budget():
    plan = plan_artifact_retention(
        state_of(incident_m("A", t=1, disk=60), incident_m("B", t=2, disk=60)),
        policy(max_total_bytes=100),
    )
    assert plan.delete_ids == ("A",)
    assert plan.before_bytes == 120
    assert plan.reclaimable_bytes == 60
    assert plan.projected_bytes == 60
    assert plan.unsatisfied_rules == ()


def _shared_target_state():
    return state_of(
        incident_m("A", t=1, disk=1, refs=("T",)),
        incident_m("B", t=2, disk=1, refs=("T",)),
        bundle_m("T", disk=100),
    )


def test_a_shared_target_is_excluded_from_the_singleton_closure_of_a():
    assert deletion_closure(_shared_target_state().graph, {"A"}) == frozenset({"A"})


def test_a_shared_target_is_excluded_from_the_singleton_closure_of_b():
    assert deletion_closure(_shared_target_state().graph, {"B"}) == frozenset({"B"})


def test_the_pair_closure_contains_both_roots_and_their_shared_target():
    closure = deletion_closure(_shared_target_state().graph, {"A", "B"})
    assert closure == frozenset({"A", "B", "T"})


def test_a_third_inbound_root_keeps_the_target_out_of_the_pair_closure():
    """Two of three is not enough — the LAST inbound root is what admits it.

    The three assertions this replaced restated their neighbours: each said
    `"T" not in X` where the previous test had already asserted `X == {"A"}`.
    A shared target under a THREE-way selection is what discriminates an
    implementation that admits a member once any inbound root is selected.
    """
    st = state_of(
        incident_m("A", t=1, refs=("T",)), incident_m("B", t=2, refs=("T",)),
        incident_m("C", t=3, refs=("T",)), bundle_m("T", disk=100),
    )
    assert "T" not in deletion_closure(st.graph, {"A", "B"})
    assert "T" in deletion_closure(st.graph, {"A", "B", "C"})


def test_an_exclusive_intermediate_enters_the_closure_although_its_leaf_does_not():
    """Exclusivity is per MEMBER, not per path.

    `A` reaches the shared leaf `T` only through `M`, which nothing else
    reaches. An implementation that excluded a whole branch once any member of
    it was shared would drop `M` too.
    """
    st = state_of(
        incident_m("A", t=1, refs=("M",)), bundle_m("M", disk=7, refs=("T",)),
        incident_m("B", t=2, refs=("T",)), bundle_m("T", disk=100),
    )
    assert deletion_closure(st.graph, {"A"}) == frozenset({"A", "M"})


def test_the_closure_does_not_depend_on_the_order_of_the_selected_set():
    """A selection is a SET. An implementation that folded roots in sequence,
    assigning each shared member to whichever root reached it first, would
    disagree with itself here."""
    graph = _shared_target_state().graph
    assert deletion_closure(graph, ["A", "B"]) == deletion_closure(graph, ["B", "A"])


def test_selecting_the_first_root_reclaims_none_of_the_shared_bytes():
    # Only A is older than the age bound, so B is retained and pins T.
    st = state_of(
        incident_m("A", t=1, disk=1, refs=("T",)),
        incident_m("B", t=900, disk=1, refs=("T",)),
        bundle_m("T", disk=100),
        now=1000,
    )
    plan = plan_artifact_retention(st, policy(max_age_seconds=500))
    assert plan.delete_ids == ("A",)
    assert plan.reclaimable_bytes == 1
    assert plan.reference_pinned_bytes == 100


def test_selecting_the_second_root_reclaims_all_of_the_shared_bytes():
    st = _shared_target_state()
    plan = plan_artifact_retention(st, policy(max_total_bytes=1))
    assert set(plan.delete_ids) == {"A", "B", "T"}
    assert plan.reclaimable_bytes == 102
    assert plan.reference_pinned_bytes == 0


def test_the_shared_target_is_deleted_exactly_once():
    plan = plan_artifact_retention(_shared_target_state(), policy(max_total_bytes=1))
    assert len(plan.delete_ids) == len(set(plan.delete_ids))


def test_the_same_pair_selected_in_different_phases_gives_the_same_result():
    st = _shared_target_state()
    by_age = plan_artifact_retention(st, policy(max_age_seconds=0))
    by_bytes = plan_artifact_retention(st, policy(max_total_bytes=0))
    assert set(by_age.delete_ids) == set(by_bytes.delete_ids)
    assert by_age.reclaimable_bytes == by_bytes.reclaimable_bytes


def test_one_root_by_age_and_the_other_by_bytes_still_reclaims_the_target():
    # A is over the age bound and B is not, so the two roots enter the
    # selection in different phases. The target's bytes belong to whichever
    # phase selected the SECOND root, which is why the marginal is recomputed
    # after every root rather than once per phase.
    st = state_of(
        incident_m("A", t=1, disk=1, refs=("T",)),
        incident_m("B", t=900, disk=1, refs=("T",)),
        bundle_m("T", disk=100),
        now=1000,
    )
    plan = plan_artifact_retention(
        st, policy(max_age_seconds=500, max_total_bytes=50),
    )
    assert set(plan.delete_ids) == {"A", "B", "T"}
    assert plan.projected_bytes == 0


def test_a_shared_target_holding_most_of_the_bytes_is_not_claimed_early():
    st = state_of(
        incident_m("A", t=1, disk=1, refs=("T",)),
        incident_m("B", t=2, disk=1, refs=("T",)),
        bundle_m("T", disk=1000),
    )
    plan = plan_artifact_retention(st, policy(max_total_bytes=500))
    # The budget is met only after the LAST inbound root is selected.
    assert set(plan.delete_ids) == {"A", "B", "T"}
    assert plan.projected_bytes == 0


def test_a_protected_inbound_root_pins_the_target_and_marks_the_bound_unsatisfied():
    st = state_of(
        incident_m("A", t=1, refs=("T",), classification="unknown"),
        incident_m("B", t=2, refs=("T",), classification="exact"),
        bundle_m("T", disk=10_000),
    )
    plan = plan_artifact_retention(st, policy(max_total_bytes=1))
    assert "T" not in plan.delete_ids
    assert plan.reference_pinned_bytes == 10_000
    assert "max_total_bytes" in plan.unsatisfied_rules
    assert "A" in plan.protected_ids


def test_a_zero_marginal_root_still_counts_toward_the_count_bound():
    st = state_of(
        incident_m("A", t=1, disk=0, refs=("T",)),
        incident_m("B", t=2, disk=0, refs=("T",)),
        bundle_m("T", disk=100),
    )
    plan = plan_artifact_retention(st, policy(max_count_per_family=1))
    assert plan.delete_ids == ("A",)
    assert plan.reclaimable_bytes == 0


def test_the_byte_phase_ends_when_every_eligible_root_has_been_selected():
    # A root whose marginal contribution is zero must not stall the loop.
    st = state_of(
        incident_m("A", t=1, disk=0, refs=("T",)),
        incident_m("B", t=2, disk=0, refs=("T",)),
        bundle_m("T", disk=100),
        incident_m("P", t=3, disk=0, refs=("T",), classification="unknown"),
    )
    plan = plan_artifact_retention(st, policy(max_total_bytes=1))
    assert set(plan.delete_ids) == {"A", "B"}
    assert "max_total_bytes" in plan.unsatisfied_rules


def test_the_shape_floor_keeps_the_last_example_of_a_shape():
    st = state_of(
        incident_m("old", t=1, shape="rare"),
        incident_m("new", t=2, shape="common"),
        incident_m("newer", t=3, shape="common"),
    )
    plan = plan_artifact_retention(st, policy(max_count_per_family=1))
    assert "old" not in plan.delete_ids
    assert "new" in plan.delete_ids


def test_the_literal_none_shape_earns_no_floor():
    st = state_of(
        incident_m("old", t=1, shape="none"), incident_m("new", t=2, shape="none"),
    )
    plan = plan_artifact_retention(st, policy(max_count_per_family=1))
    assert "old" in plan.delete_ids


def test_a_root_with_no_shape_token_earns_no_floor():
    st = state_of(incident_m("old", t=1), incident_m("new", t=2))
    plan = plan_artifact_retention(st, policy(max_count_per_family=1))
    assert "old" in plan.delete_ids


def test_the_shape_floor_is_capped_at_max_shape_examples():
    st = state_of(*[
        incident_m(f"s{i}", t=i, shape=f"shape{i}") for i in range(1, 5)
    ])
    plan = plan_artifact_retention(
        st, policy(max_count_per_family=1, max_shape_examples=2),
    )
    # Two shapes hold a floor; the other two roots are ordinary candidates.
    assert len(plan.delete_ids) == 2


def test_planning_terminates_when_the_budget_cannot_be_met():
    st = state_of(
        incident_m("A", t=1, disk=500, classification="unknown"),
        incident_m("B", t=2, disk=500, classification=None),
    )
    plan = plan_artifact_retention(st, policy(max_total_bytes=1))
    assert plan.delete_ids == ()
    assert "max_total_bytes" in plan.unsatisfied_rules
    assert set(plan.protected_ids) == {"A", "B"}


def test_a_protected_root_is_never_selected_by_the_age_bound():
    st = state_of(incident_m("A", t=1, classification=None), now=1_000_000)
    plan = plan_artifact_retention(st, policy(max_age_seconds=1))
    assert plan.delete_ids == ()
    assert "max_age_seconds" in plan.unsatisfied_rules


def test_the_age_bound_selects_only_roots_older_than_it():
    st = state_of(
        incident_m("A", t=1), incident_m("B", t=600), incident_m("C", t=999),
        now=1000,
    )
    plan = plan_artifact_retention(st, policy(max_age_seconds=500))
    assert plan.delete_ids == ("A",)


def test_the_count_bound_counts_roots_per_family():
    st = state_of(
        incident_m("s1", t=1, family="stats.db"),
        incident_m("s2", t=2, family="stats.db"),
        incident_m("c1", t=3, family="cache.db"),
    )
    plan = plan_artifact_retention(st, policy(max_count_per_family=1))
    assert plan.delete_ids == ("s1",)


def test_the_count_bound_counts_protected_roots_too():
    # §3.6: protection alone exceeding a bound deletes everything eligible and
    # reports the bound unsatisfied. Two protected roots against a bound of one
    # is the case that discriminates: counting only ELIGIBLE roots would report
    # the bound met while the family still holds two.
    st = state_of(
        incident_m("p1", t=1, classification=None),
        incident_m("p2", t=2, classification=None),
        incident_m("a", t=3), incident_m("b", t=4),
    )
    plan = plan_artifact_retention(st, policy(max_count_per_family=1))
    assert set(plan.delete_ids) == {"a", "b"}
    assert "max_count_per_family" in plan.unsatisfied_rules


def test_the_count_bound_is_satisfied_once_the_survivors_fit():
    # Non-vacuity for the row above: the same shape with ONE protected root
    # meets the bound and reports nothing unsatisfied.
    st = state_of(
        incident_m("p", t=1, classification=None),
        incident_m("a", t=2), incident_m("b", t=3),
    )
    plan = plan_artifact_retention(st, policy(max_count_per_family=1))
    assert set(plan.delete_ids) == {"a", "b"}
    assert plan.unsatisfied_rules == ()


def test_the_free_disk_floor_selects_until_the_floor_is_reached():
    st = state_of(
        incident_m("A", t=1, disk=40), incident_m("B", t=2, disk=40),
        incident_m("C", t=3, disk=40), free=100,
    )
    plan = plan_artifact_retention(st, policy(min_free_bytes=150))
    assert plan.delete_ids == ("A", "B")


def test_the_free_disk_floor_is_skipped_when_free_space_is_unknown():
    st = state_of(incident_m("A", t=1, disk=40), free=None)
    plan = plan_artifact_retention(st, policy(min_free_bytes=10_000))
    assert plan.delete_ids == ()
    assert plan.unsatisfied_rules == ()


def test_before_bytes_counts_a_shared_member_once():
    st = state_of(
        incident_m("A", t=1, disk=1, refs=("T",)),
        incident_m("B", t=2, disk=1, refs=("T",)),
        bundle_m("T", disk=100),
    )
    plan = plan_artifact_retention(st, policy(max_total_bytes=1_000_000))
    assert plan.before_bytes == 102


def test_delete_ids_puts_a_referrer_before_its_referent():
    # §5.4 renames reference-bearing roots first, so a crash cannot leave a
    # surviving manifest pointing at a tombstone. The order comes from here.
    st = state_of(incident_m("A", t=1, disk=1, refs=("T",)), bundle_m("T", disk=1))
    plan = plan_artifact_retention(st, policy(max_total_bytes=0))
    assert plan.delete_ids.index("A") < plan.delete_ids.index("T")


def test_delete_ids_orders_roots_oldest_first():
    st = state_of(
        incident_m("A", t=3, disk=1), incident_m("B", t=1, disk=1),
        incident_m("C", t=2, disk=1),
    )
    plan = plan_artifact_retention(st, policy(max_total_bytes=0))
    assert plan.delete_ids == ("B", "C", "A")


def test_every_id_carries_a_reason():
    st = state_of(
        incident_m("A", t=1, disk=1, refs=("T",)), bundle_m("T", disk=1),
        incident_m("P", t=2, disk=1, classification=None),
        incident_m("K", t=900, disk=1),
        now=1000,
    )
    plan = plan_artifact_retention(st, policy(max_age_seconds=500))
    assert plan.reasons["A"] == "max_age_seconds"
    assert plan.reasons["T"] == "closure"
    assert plan.reasons["K"] == "retained"
    assert "unclassified" in plan.reasons["P"]
    assert set(plan.keep_ids) == {"K"}
    assert set(plan.protected_ids) == {"P"}


def test_a_deletion_closure_over_an_empty_selection_is_empty():
    assert deletion_closure(_shared_target_state().graph, set()) == frozenset()


def test_planning_an_empty_corpus_is_a_no_op():
    plan = plan_artifact_retention(state_of(), policy(max_total_bytes=0))
    assert plan.delete_ids == ()
    assert plan.before_bytes == 0
    assert plan.unsatisfied_rules == ()


# --------------------------------------------------------------------------
# §3.5 — the bounds interact, and each one measures the state the last one left
# --------------------------------------------------------------------------
#
# "Projected state is recomputed after every root added, not once per phase."
# Every test below runs two bounds TOGETHER and asserts the second one measured
# what the first one left. Running a bound on its own cannot fail this way, so
# each pair also has a single-bound partner that pins the expected figure.


def _four_old_six_recent():
    """Four roots over the age bound, six under it, all in one family."""
    return state_of(
        *[incident_m(f"old{i}", t=float(i), disk=1) for i in range(4)],
        *[incident_m(f"new{i}", t=900.0 + i, disk=1) for i in range(6)],
        now=1000,
    )


def _survivors(plan):
    return set(plan.keep_ids) | set(plan.protected_ids)


def test_the_count_bound_alone_leaves_exactly_the_bound():
    plan = plan_artifact_retention(
        _four_old_six_recent(), policy(max_count_per_family=5),
    )
    assert len(_survivors(plan)) == 5


def test_the_count_bound_measures_the_population_the_age_phase_left():
    # The RED for the count phase's stale baseline. The age bound removes four
    # roots from the candidate list; a count phase that still measured all ten
    # keeps taking five more and leaves ONE survivor against a bound of five.
    plan = plan_artifact_retention(
        _four_old_six_recent(),
        policy(max_age_seconds=500, max_count_per_family=5),
    )
    assert len(_survivors(plan)) == 5
    assert plan.unsatisfied_rules == ()


def test_the_age_bound_alone_leaves_the_six_recent_roots():
    plan = plan_artifact_retention(
        _four_old_six_recent(), policy(max_age_seconds=500),
    )
    assert len(_survivors(plan)) == 6


def test_the_count_bound_keeps_families_independent_after_a_firing_age_bound():
    # The age bound fires on one family only. The other family's tally must be
    # unaffected by it.
    st = state_of(
        *[incident_m(f"s_old{i}", t=float(i), family="stats.db") for i in range(4)],
        *[incident_m(f"s_new{i}", t=900.0 + i, family="stats.db") for i in range(4)],
        *[incident_m(f"c{i}", t=900.0 + i, family="cache.db") for i in range(4)],
        now=1000,
    )
    plan = plan_artifact_retention(
        st, policy(max_age_seconds=500, max_count_per_family=3),
    )
    survivors = _survivors(plan)
    assert len([s for s in survivors if s.startswith("s_")]) == 3
    assert len([s for s in survivors if s.startswith("c")]) == 3


def test_the_byte_bound_credits_what_the_age_phase_already_reclaimed():
    # The age bound reclaims 60 of 100 bytes, which already meets a 50-byte
    # budget. A byte phase measuring the original total would take more.
    st = state_of(
        incident_m("old1", t=1, disk=30), incident_m("old2", t=2, disk=30),
        incident_m("new1", t=900, disk=20), incident_m("new2", t=901, disk=20),
        now=1000,
    )
    plan = plan_artifact_retention(
        st, policy(max_age_seconds=500, max_total_bytes=50),
    )
    assert set(plan.delete_ids) == {"old1", "old2"}
    assert plan.projected_bytes == 40


def test_the_byte_bound_credits_what_the_count_phase_already_reclaimed():
    st = state_of(
        incident_m("a", t=1, disk=30), incident_m("b", t=2, disk=30),
        incident_m("c", t=3, disk=20), incident_m("d", t=4, disk=20),
        now=1000,
    )
    plan = plan_artifact_retention(
        st, policy(max_count_per_family=2, max_total_bytes=50),
    )
    assert set(plan.delete_ids) == {"a", "b"}
    assert plan.projected_bytes == 40


def test_the_free_disk_floor_credits_what_the_age_phase_already_reclaimed():
    st = state_of(
        incident_m("old", t=1, disk=60),
        incident_m("new1", t=900, disk=40), incident_m("new2", t=901, disk=40),
        now=1000, free=100,
    )
    plan = plan_artifact_retention(
        st, policy(max_age_seconds=500, min_free_bytes=150),
    )
    assert set(plan.delete_ids) == {"old"}
    assert plan.unsatisfied_rules == ()


def test_the_free_disk_floor_credits_what_the_byte_phase_already_reclaimed():
    st = state_of(
        incident_m("a", t=1, disk=60), incident_m("b", t=2, disk=40),
        incident_m("c", t=3, disk=40), free=100,
    )
    plan = plan_artifact_retention(
        st, policy(max_total_bytes=80, min_free_bytes=150),
    )
    assert set(plan.delete_ids) == {"a"}
    assert plan.unsatisfied_rules == ()


def test_a_shape_floored_root_survives_a_firing_age_bound():
    # The floor is applied before every phase, so a floored root is not a
    # candidate for the age bound either. Spec revision 5 (§3.6) then EXCUSES
    # that survival from `unsatisfied_rules` — the operator asked for it
    # through `max_shape_examples` and has nothing to act on — and reports it
    # as a floor-retained root instead.
    st = state_of(
        incident_m("keeper", t=1, shape="rare", disk=11),
        incident_m("other", t=2, shape="common"),
        incident_m("newer", t=900, shape="common"),
        now=1000,
    )
    plan = plan_artifact_retention(st, policy(max_age_seconds=500))
    assert "keeper" not in plan.delete_ids
    assert plan.reasons["keeper"] == "shape-floor"
    assert plan.unsatisfied_rules == ()
    assert plan.floor_retained_ids == ("keeper",)
    assert plan.floor_retained_bytes == 11


def test_the_count_bound_counts_a_selected_root_the_closure_cannot_delete():
    # A hand-built graph in which the "pinned" root is reachable from a root
    # that is never selected. It must keep counting as a survivor, or the count
    # phase credits a deletion that will not happen.
    pinned = _pinned_root_graph()
    plan = plan_artifact_retention(
        RetentionState(graph=pinned, now_epoch=1000.0, free_disk_bytes=None),
        policy(max_count_per_family=1),
    )
    assert "A" not in plan.delete_ids
    assert "max_count_per_family" in plan.unsatisfied_rules


# --------------------------------------------------------------------------
# §3.1 — the two graph invariants the planner and the doctor leg rest on
# --------------------------------------------------------------------------
#
# 1. No root is reachable from another root, so a selected root is always inside
#    its own deletion closure.
# 2. Every member is reachable from some root, so nothing is invisible.
#
# Without (1) a root can be selected, credited against a bound, and then appear
# in NONE of `delete_ids`, `keep_ids` and `protected_ids` — the bound reported
# satisfied over a corpus still over budget.


def _root(member_id, *, reachable, protected=(), t=1.0, family="stats.db"):
    return RetentionRoot(
        id=member_id, kind="incident", family=family, created_at_epoch=t,
        reachable_ids=frozenset(reachable), own_member_ids=frozenset(),
        requires_classification=True,
        classification=None if protected else "exact",
        shape_token=None, protected_reasons=tuple(protected),
    )


def _pinned_root_graph():
    """A graph in which root `A` is reachable from the permanently protected `P`.

    `build_graph` cannot produce this — invariant (1) forbids it — so the graph
    is assembled by hand. The planner must still report `A` exactly once, which
    is what makes the guard there defense in depth rather than dead code.
    """
    members = {
        "A": incident_m("A", t=1, disk=10),
        "P": incident_m("P", t=2, disk=10, refs=("A",), classification=None),
    }
    roots = (
        _root("A", reachable={"A"}, t=1.0),
        _root("P", reachable={"P", "A"}, protected=("unclassified",), t=2.0),
    )
    return RetentionGraph(
        roots=roots,
        members=members,
        inbound_roots={"A": frozenset({"A", "P"}), "P": frozenset({"P"})},
        roots_by_id={root.id: root for root in roots},
    )


_GRAPH_SHAPES = {
    # The one-way edge the reviewer named: a rebuild record naming an incident
    # that does not name it back.
    "record-to-incident": [record_m("r1", refs=("A",)), incident_m("A")],
    "incident-to-record-cycle": [
        incident_m("A", refs=("r1",)), record_m("r1", refs=("A",)),
    ],
    "shared-bundle": [
        incident_m("A", refs=("T",)), incident_m("B", refs=("T",)), bundle_m("T"),
    ],
    # The production diamond: an incident naming both a bundle and a record,
    # and that record naming the same bundle. 28 of the maintainer's 142
    # incidents have this shape.
    "diamond": [
        incident_m("I", refs=("Bu", "R")),
        record_m("R", refs=("Bu", "I")),
        bundle_m("Bu", refs=("W",)),
        wal_m("W"),
    ],
    "mutual-bundles": [bundle_m("b1", refs=("b2",)), bundle_m("b2", refs=("b1",))],
    "unreferenced-bundle-chain": [bundle_m("b", refs=("w",)), wal_m("w")],
    "backup-stem": [backup_m("stats.db.bak-corrupt-malformed-x")],
    "one-way-into-a-backup": [
        record_m("r1", refs=("stats.db.bak-corrupt-malformed-x",)),
        backup_m("stats.db.bak-corrupt-malformed-x"),
    ],
}


@pytest.mark.parametrize("shape", sorted(_GRAPH_SHAPES))
def test_no_root_is_reachable_from_another_root(shape):
    g = _build_bounded(_GRAPH_SHAPES[shape])
    for root in g.roots:
        assert g.inbound_roots[root.id] == frozenset({root.id}), (
            f"{root.id} is reachable from another root, so a selection "
            "containing it excludes it from its own deletion closure"
        )


@pytest.mark.parametrize("shape", sorted(_GRAPH_SHAPES))
def test_every_member_is_reachable_from_some_root(shape):
    g = _build_bounded(_GRAPH_SHAPES[shape])
    for member_id in g.members:
        assert g.inbound_roots[member_id], (
            f"{member_id} is reachable from no root, so no selection can ever "
            "delete it and nothing reports it"
        )


@pytest.mark.parametrize("shape", sorted(_GRAPH_SHAPES))
def test_every_root_lands_in_exactly_one_output_list(shape):
    st = RetentionState(
        graph=_build_bounded(_GRAPH_SHAPES[shape]),
        now_epoch=1000.0, free_disk_bytes=None,
    )
    plan = plan_artifact_retention(st, policy(max_total_bytes=0))
    for root in st.graph.roots:
        landed = [
            name for name, ids in (
                ("delete", plan.delete_ids),
                ("keep", plan.keep_ids),
                ("protected", plan.protected_ids),
            )
            if root.id in ids
        ]
        assert landed and len(landed) == 1, f"{root.id} landed in {landed}"
        assert root.id in plan.reasons


def test_a_root_pinned_in_a_hand_built_graph_is_reported_as_kept():
    # Non-vacuity for the invariant test above: the same assertion applied to a
    # graph that VIOLATES invariant (1) is what the planner's own guard has to
    # answer. Without it "A" appears in no list at all.
    plan = plan_artifact_retention(
        RetentionState(graph=_pinned_root_graph(), now_epoch=1000.0,
                       free_disk_bytes=None),
        policy(max_total_bytes=0),
    )
    assert "A" not in plan.delete_ids
    assert "A" in plan.keep_ids
    assert plan.reasons["A"] == "reference-pinned"


def test_a_record_referencing_an_incident_does_not_pin_it():
    # The concrete defect. `r1` is unreferenced, so it is a root; under a walk
    # that traverses into another root it also reaches `A`, and `A` is then
    # excluded from its own singleton closure.
    g = build_graph([record_m("r1", refs=("A",)), incident_m("A", disk=10)])
    assert g.inbound_roots["A"] == frozenset({"A"})
    assert deletion_closure(g, {"A"}) == frozenset({"A"})


def test_two_mutually_referencing_bundles_stay_reclaimable():
    # Neither is "referenced by nobody", so the base rule roots neither and no
    # root reaches either. The completion loop promotes one, which then reaches
    # the other, so the pair is deleted together rather than being invisible.
    st = state_of(
        bundle_m("b1", t=1, disk=5, refs=("b2",)),
        bundle_m("b2", t=2, disk=5, refs=("b1",)),
    )
    plan = plan_artifact_retention(st, policy(max_total_bytes=0))
    assert set(plan.delete_ids) == {"b1", "b2"}
    assert plan.projected_bytes == 0


def test_a_diamond_emits_every_referrer_before_the_shared_referent():
    # §5.4 renames a referrer before its referent so a crash cannot leave a
    # surviving manifest naming a tombstone. A depth-first pre-order alone does
    # not give that: `Bu` is reached from `I` in one hop and from `R` in two, so
    # under this reference ordering it would be emitted before `R`.
    st = state_of(
        incident_m("I", t=1, disk=1, refs=("Bu", "R")),
        record_m("R", disk=1, refs=("Bu",)),
        bundle_m("Bu", disk=1),
    )
    plan = plan_artifact_retention(st, policy(max_total_bytes=0))
    assert set(plan.delete_ids) == {"I", "R", "Bu"}
    assert plan.delete_ids.index("R") < plan.delete_ids.index("Bu")
    assert plan.delete_ids.index("I") < plan.delete_ids.index("R")


def test_the_production_cycle_still_puts_the_incident_first():
    # Non-vacuity for the ordering pass: on the real incident-to-rebuild-record
    # cycle nothing is ever "ready", so the tie-break has to fire and the entry
    # root has to win it.
    st = state_of(
        incident_m("A", t=1, disk=1, refs=("r1",)),
        record_m("r1", disk=1, refs=("A",)),
    )
    plan = plan_artifact_retention(st, policy(max_total_bytes=0))
    assert plan.delete_ids == ("A", "r1")


def test_delete_groups_lead_with_their_root_and_carry_its_closure():
    """The marking engine decides per root, so the grouping is part of the plan.

    Re-deriving it from `reasons` would couple the engine to the planner's
    private labelling; assembling it in the caller is what produced a flat plan
    whose per-member decisions deleted a referent behind a skipped referrer.
    """
    st = state_of(
        incident_m("A", t=1, disk=1, refs=("T",)), bundle_m("T", disk=1),
        incident_m("B", t=2, disk=1),
    )
    plan = plan_artifact_retention(st, policy(max_total_bytes=0))
    assert plan.delete_groups == (("A", ("A", "T")), ("B", ("B",)))


def test_delete_groups_flatten_back_to_delete_ids():
    st = state_of(
        incident_m("I", t=1, disk=1, refs=("Bu", "R")),
        record_m("R", disk=1, refs=("Bu",)), bundle_m("Bu", disk=1),
        incident_m("B", t=2, disk=1),
    )
    plan = plan_artifact_retention(st, policy(max_total_bytes=0))
    flattened = [m for _root, members in plan.delete_groups for m in members]
    assert tuple(flattened) == plan.delete_ids


def test_a_shared_member_joins_the_group_of_the_root_that_completed_it():
    """A member enters the closure only when its LAST inbound root is selected,
    so it belongs to that root's group and to no earlier one."""
    plan = plan_artifact_retention(_shared_target_state(), policy(max_total_bytes=1))
    assert plan.delete_groups == (("A", ("A",)), ("B", ("B", "T")))


def test_the_free_disk_floor_credits_what_the_count_phase_already_reclaimed():
    # The fourth phase pair. The count bound reclaims 60 of the 140 retained
    # bytes, which already lifts free space to the floor.
    st = state_of(
        incident_m("a", t=1, disk=30), incident_m("b", t=2, disk=30),
        incident_m("c", t=3, disk=40), incident_m("d", t=4, disk=40),
        free=100,
    )
    plan = plan_artifact_retention(
        st, policy(max_count_per_family=2, min_free_bytes=150),
    )
    assert set(plan.delete_ids) == {"a", "b"}
    assert plan.unsatisfied_rules == ()


# --------------------------------------------------------------------------
# §3.6 — the shape floor is excused from `unsatisfied_rules`; protection is not
# --------------------------------------------------------------------------
#
# The two look alike: both leave a root on disk that a bound would otherwise
# have taken. They differ in whether the operator can act. A protected root is
# blocked by something an operator can resolve — classify it, inspect it, wait
# out an active heal. A shape-floored root is retained ON PURPOSE, by the
# policy the operator set through `max_shape_examples`.
#
# The concrete failure is certain rather than hypothetical: two of the four
# damage shapes on the maintainer's corpus appear exactly once, so once those
# examples age past `max_age_seconds` the floor pins them forever and
# `db.retained_artifacts` FAILs permanently on a healthy install.


def test_a_root_the_shape_floor_kept_does_not_make_the_age_bound_unsatisfied():
    st = state_of(incident_m("only", t=1, shape="rare", disk=10), now=1_000_000)
    plan = plan_artifact_retention(
        st, policy(max_age_seconds=1, max_shape_examples=8),
    )
    assert plan.delete_ids == ()
    assert plan.unsatisfied_rules == ()


def test_a_root_the_shape_floor_kept_does_not_make_the_byte_bound_unsatisfied():
    st = state_of(incident_m("only", t=1, shape="rare", disk=500))
    plan = plan_artifact_retention(st, policy(max_total_bytes=1))
    assert plan.delete_ids == ()
    assert plan.unsatisfied_rules == ()


def test_a_root_the_shape_floor_kept_does_not_make_the_count_bound_unsatisfied():
    st = state_of(
        incident_m("a", t=1, shape="one"), incident_m("b", t=2, shape="two"),
    )
    plan = plan_artifact_retention(st, policy(max_count_per_family=1))
    assert plan.delete_ids == ()
    assert plan.unsatisfied_rules == ()


def test_protected_evidence_still_makes_a_bound_unsatisfied_beside_a_floor():
    """The discriminating case: the floor is excused, protection is not.

    A test asserting only the excusal would pass against an implementation that
    dropped `unsatisfied_rules` altogether, so this pins the other direction on
    the same corpus — one floored root and one protected root, both over the
    same bound.
    """
    st = state_of(
        incident_m("floored", t=1, shape="rare", disk=500),
        incident_m("blocked", t=2, disk=500, classification="unknown"),
    )
    plan = plan_artifact_retention(st, policy(max_total_bytes=1))
    assert plan.delete_ids == ()
    assert plan.unsatisfied_rules == ("max_total_bytes",)


def test_the_floor_reports_the_roots_and_bytes_it_retained():
    """§3.6: reported separately, as a floor-retained count and byte total."""
    st = state_of(
        incident_m("floored", t=1, shape="rare", disk=400, refs=("sat",)),
        bundle_m("sat", t=1, disk=100),
        incident_m("ordinary", t=2, disk=7),
    )
    plan = plan_artifact_retention(st, policy(max_total_bytes=1))
    assert plan.floor_retained_ids == ("floored",)
    # The root plus the member exclusive to it: what deleting it would free.
    assert plan.floor_retained_bytes == 500
    assert "ordinary" in plan.delete_ids


def test_a_floored_root_no_bound_would_have_taken_is_not_reported_as_retained():
    """§6.4 says "that age and count would otherwise have removed"."""
    st = state_of(incident_m("young", t=999, shape="rare", disk=10), now=1000.0)
    plan = plan_artifact_retention(st, policy(max_age_seconds=100_000))
    assert plan.floor_retained_ids == ()
    assert plan.floor_retained_bytes == 0


def test_a_floored_root_still_occupies_its_family_count_budget():
    """§3.6: still counted as a survivor for the count bound.

    `a` holds the only floor. With three roots and a budget of one, the count
    phase must still take `b` and `c` — it may not treat the floored root as
    absent from the family.
    """
    st = state_of(
        incident_m("a", t=1, shape="rare"),
        incident_m("b", t=2),
        incident_m("c", t=3),
    )
    plan = plan_artifact_retention(st, policy(max_count_per_family=1))
    assert set(plan.delete_ids) == {"b", "c"}


# --------------------------------------------------------------------------
# §3.1 — `_Selection` against `deletion_closure`, differentially
# --------------------------------------------------------------------------
#
# `_Selection` maintains the deletion closure incrementally so that
# `plan_artifact_retention` stays linear in the root count. `deletion_closure`
# remains the authoritative statement of §3.1, and THIS SECTION is where the
# two are proven to agree — over randomly generated corpora, after every
# addition, on every prefix of a randomly ordered selection.
#
# The equality is deliberately NOT asserted at runtime inside `add`. Re-deriving
# the whole closure once per candidate is exactly the quadratic cost the
# incremental form was written to remove (measured 8.0 ms over 142 roots and
# 535.7 ms over 1136), and this leg runs on the periodic doctor gather.

#: Corpora generated per differential test. The previous review used 400 for a
#: comparable check and that is the model followed here.
_DIFFERENTIAL_GRAPH_COUNT = 400

_CORPUS_KIND_CONSTRUCTORS = {
    "incident": incident_m,
    "rebuild_record": record_m,
    "bundle": bundle_m,
    "wal_evidence": wal_m,
    "backup": backup_m,
    "backup_member": lambda member_id, **kw: _member(
        member_id, "backup_member", **kw
    ),
}

_CORPUS_ID_PREFIXES = {
    "incident": "I",
    "rebuild_record": "R",
    "bundle": "B",
    "wal_evidence": "W",
    "backup": "K",
    "backup_member": "S",
}


def _random_corpus(rng):
    """A corpus spanning every shape the incremental countdown has to get right.

    Shared targets, reference chains, the production incident-to-record cycle,
    multi-root diamonds, satellites nothing references and orphan pairs the
    completion loop has to promote all occur with non-trivial probability.
    `test_the_generated_corpora_cover_every_shape` is what proves they do,
    because a generator that silently stopped producing shared targets would
    leave the differential test passing over corpora that cannot discriminate.
    """
    counts = {
        "incident": rng.randint(1, 5),
        "rebuild_record": rng.randint(0, 4),
        "bundle": rng.randint(0, 4),
        "wal_evidence": rng.randint(0, 3),
        "backup": rng.randint(0, 2),
        "backup_member": rng.randint(0, 2),
    }
    ids = {
        kind: [f"{_CORPUS_ID_PREFIXES[kind]}{index}" for index in range(total)]
        for kind, total in counts.items()
    }

    def pick(pool, most):
        if not pool:
            return []
        return rng.sample(pool, rng.randint(0, min(most, len(pool))))

    references = {}
    for member_id in ids["wal_evidence"] + ids["backup_member"]:
        references[member_id] = ()
    for member_id in ids["bundle"]:
        references[member_id] = tuple(pick(ids["wal_evidence"], 2))
    for member_id in ids["rebuild_record"]:
        refs = pick(ids["bundle"], 2)
        if ids["incident"] and rng.random() < 0.4:
            # The real production shape: the record names the incident back.
            refs.append(rng.choice(ids["incident"]))
        references[member_id] = tuple(refs)
    for member_id in ids["incident"]:
        references[member_id] = tuple(
            pick(ids["rebuild_record"] + ids["bundle"], 3)
        )
    for member_id in ids["backup"]:
        references[member_id] = tuple(pick(ids["backup_member"], 2))

    members = [
        _CORPUS_KIND_CONSTRUCTORS[kind](
            member_id,
            refs=references[member_id],
            t=float(rng.randint(0, 50)),
            disk=rng.randint(0, 1000),
            family=rng.choice(("stats.db", "cache.db", "conversations.db")),
        )
        for kind, member_ids in ids.items()
        for member_id in member_ids
    ]
    rng.shuffle(members)
    return members


def _expected_surviving_counts(graph, closure):
    counts = {}
    for root in graph.roots:
        if root.id not in closure:
            counts[root.family] = counts.get(root.family, 0) + 1
    return counts


def _assert_selection_matches_closure(graph, order, factory=_Selection, note=""):
    """Every prefix of `order` agrees with the authoritative closure.

    Returns a census of what the run exercised so the caller can prove the
    corpora were discriminating rather than merely numerous.
    """
    selection = factory(graph)
    selected = set()
    withheld_events = 0
    multi_member_additions = 0

    assert selection.closure == deletion_closure(graph, selected), (
        f"{note}: an empty selection must have an empty deletion closure"
    )

    for root_id in order:
        before = selection.closure
        selection.add(root_id, "differential")
        selected.add(root_id)
        expected = deletion_closure(graph, selected)
        assert selection.closure == expected, (
            f"{note}: after adding {root_id} to {sorted(selected)} the "
            f"incremental deletion closure was {sorted(selection.closure)} "
            f"but the authoritative one is {sorted(expected)}"
        )
        assert selection.bytes_reclaimed == sum(
            graph.members[member_id].disk_bytes for member_id in expected
        ), f"{note}: bytes_reclaimed disagrees with the closure it claims"
        assert selection.surviving_root_counts() == _expected_surviving_counts(
            graph, expected
        ), f"{note}: surviving_root_counts disagrees with the closure"

        reached = graph.roots_by_id[root_id].reachable_ids
        if reached - expected:
            withheld_events += 1
        if len(expected - before) > 1:
            multi_member_additions += 1

    return withheld_events, multi_member_additions


class _StaticClosureSelection:
    """The defect §3.1 names: a closure assigned statically, per root.

    Adding a root closes everything that root reaches, whether or not another
    unselected root also reaches it. This is the implementation the differential
    harness exists to reject, and `test_the_differential_harness_rejects_a_
    statically_assigned_closure` runs the harness against it to prove the
    harness can fail.
    """

    def __init__(self, graph):
        self._graph = graph
        self._closed = set()

    @property
    def closure(self):
        return frozenset(self._closed)

    @property
    def bytes_reclaimed(self):
        return sum(
            self._graph.members[member_id].disk_bytes
            for member_id in self._closed
        )

    def surviving_root_counts(self):
        return _expected_surviving_counts(self._graph, self._closed)

    def add(self, root_id, reason):
        self._closed.update(self._graph.roots_by_id[root_id].reachable_ids)


def _differential_graphs(seed):
    rng = random.Random(seed)
    for index in range(_DIFFERENTIAL_GRAPH_COUNT):
        graph = _build_bounded(_random_corpus(rng))
        order = [root.id for root in graph.roots]
        rng.shuffle(order)
        yield index, graph, order


def test_the_incremental_selection_equals_the_authoritative_closure():
    """§3.1, after EVERY addition, over 400 generated corpora.

    The class docstring used to claim this equality was asserted after every
    addition. It was not: no assertion existed in `add` and no test compared
    the two. This is where the claim is made true.
    """
    withheld = 0
    multi = 0
    for index, graph, order in _differential_graphs(4966):
        events, additions = _assert_selection_matches_closure(
            graph, order, note=f"corpus {index}"
        )
        withheld += events
        multi += additions

    # Non-vacuity. `withheld` counts additions where the added root reached a
    # member the closure correctly refused, which is the shared-target case a
    # static implementation gets wrong; `multi` counts additions that released
    # more than the root itself, which is the case a "root only" implementation
    # gets wrong. A generator producing neither would make the loop above pass
    # against both defects.
    #
    # Both floors are about half the measured values (644 and 813 at seed
    # 4966), for the reason given at `_SHAPE_CENSUS_FLOORS`: a flat 50 sat 13x
    # below what the generator actually produces and could not detect a
    # generator that had mostly stopped producing either shape.
    assert withheld >= 322, f"only {withheld} additions withheld a shared member"
    assert multi >= 406, f"only {multi} additions released more than their root"


def test_the_differential_harness_rejects_a_statically_assigned_closure():
    graph = _shared_target_state().graph
    with pytest.raises(AssertionError, match="deletion closure"):
        _assert_selection_matches_closure(
            graph, ["A"], factory=_StaticClosureSelection, note="static"
        )


def test_the_differential_harness_rejects_a_root_only_closure():
    """The other direction: an implementation that never releases a member."""

    class _RootOnly(_StaticClosureSelection):
        def add(self, root_id, reason):
            self._closed.add(root_id)

    graph = state_of(
        incident_m("A", t=1, disk=1, refs=("T",)), bundle_m("T", disk=100),
    ).graph
    with pytest.raises(AssertionError, match="deletion closure"):
        _assert_selection_matches_closure(
            graph, ["A"], factory=_RootOnly, note="root-only"
        )


def _corpus_census(graph):
    """Which of the shapes §3.1 cares about this one corpus contains."""
    shapes = set()
    if any(len(inbound) >= 2 for inbound in graph.inbound_roots.values()):
        shapes.add("shared-target")
    if any(len(root.reachable_ids) >= 3 for root in graph.roots):
        shapes.add("chain")
    if any(not root.own_member_ids for root in graph.roots):
        shapes.add("bare-root")
    for member_id, member in graph.members.items():
        for ref in member.references:
            target = graph.members.get(ref)
            if target is not None and member_id in target.references:
                shapes.add("cycle")
    for root in graph.roots:
        referrers = {}
        for member_id in root.reachable_ids:
            for ref in graph.members[member_id].references:
                if ref in root.reachable_ids:
                    referrers[ref] = referrers.get(ref, 0) + 1
        if any(total >= 2 for total in referrers.values()):
            shapes.add("diamond")
    return shapes


#: Per-shape floors, each about HALF the count measured from this generator at
#: seed 4966 over 400 corpora: shared-target 308, chain 329, cycle 96,
#: diamond 149, bare-root 383.
#:
#: A single flat floor of 20 was 4.8x below the tightest of those, so a
#: generator that had degraded by three quarters would still have passed and
#: reported the differential test as discriminating. The seed is fixed, so the
#: census is deterministic and a floor at half the measured value carries no
#: flake risk — it only leaves room for the incidental drift a kernel change
#: to the root rule would cause.
_SHAPE_CENSUS_FLOORS = {
    "shared-target": 154,
    "chain": 164,
    "cycle": 48,
    "diamond": 74,
    "bare-root": 191,
}


def test_the_generated_corpora_cover_every_shape():
    """A count measured from the generator, not a property assumed of it."""
    census = {}
    for _, graph, _ in _differential_graphs(4966):
        for shape in _corpus_census(graph):
            census[shape] = census.get(shape, 0) + 1
    for shape, floor in sorted(_SHAPE_CENSUS_FLOORS.items()):
        assert census.get(shape, 0) >= floor, (
            f"the generator produced {shape} in only {census.get(shape, 0)} of "
            f"{_DIFFERENTIAL_GRAPH_COUNT} corpora, below the floor of {floor} "
            "— the differential test is not exercising it as measured"
        )


def test_a_member_no_root_reaches_is_excluded_by_both_implementations():
    """`build_graph` cannot produce one, so the graph is made by hand.

    §3.1's completion loop guarantees every member is reachable from some root,
    so this shape never occurs downstream of `build_graph`. It is still the one
    case where the countdown's arithmetic and the closure's subset test could
    disagree — an empty inbound set is trivially a subset of every selection,
    and a countdown starting at zero is trivially satisfied — so both sides
    exclude it explicitly and this test pins that they do so together.
    """
    built = _shared_target_state().graph
    stranded = bundle_m("ORPHAN", disk=777)
    members = dict(built.members)
    members[stranded.id] = stranded
    inbound = dict(built.inbound_roots)
    inbound[stranded.id] = frozenset()
    graph = dataclasses.replace(built, members=members, inbound_roots=inbound)

    assert "ORPHAN" not in deletion_closure(graph, {"A", "B"})
    _assert_selection_matches_closure(graph, ["A", "B"], note="stranded")
    selection = _Selection(graph)
    selection.add("A", "differential")
    selection.add("B", "differential")
    assert "ORPHAN" not in selection.closure
    assert selection.bytes_reclaimed == 102
