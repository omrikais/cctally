"""Contract tests for the #648 estate artifact helper."""
import importlib.util
import json
import pathlib
import subprocess
import sys
import threading
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
HELPER = ROOT / "bin" / "_lib_test_estate.py"


def _load():
    spec = importlib.util.spec_from_file_location("_lib_test_estate", HELPER)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_lib_test_estate"] = mod
    spec.loader.exec_module(mod)
    return mod


def _artifact(**over):
    doc = {
        "schemaVersion": 1,
        "generatedFrom": "0" * 40,
        "pytestNodes": ["tests/test_a.py::test_one"],
        "frontendTests": [{"runner": "vitest", "id": "a.test.ts > x",
                           "expectedStatus": None}],
        "suppressions": [{"key": "tests/test_a.py|<module>|skip_call|", "count": 1}],
        "pytestExecution": {
            "legs": [
                {"name": "benchmark", "selectors": ["tests/test_a.py::test_one"]},
                {"name": "pytest", "selectors": ["*complement*"]},
            ]
        },
    }
    doc.update(over)
    return doc


def test_a_missing_schema_version_is_refused(tmp_path):
    mod = _load()
    doc = _artifact()
    del doc["schemaVersion"]
    p = tmp_path / "a.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(mod.EstateError):
        mod.load_artifact(p)


def test_a_duplicate_pytest_node_is_refused(tmp_path):
    """The pytest axis is a SET; a repeated identifier is malformed."""
    mod = _load()
    doc = _artifact(pytestNodes=["tests/test_a.py::t", "tests/test_a.py::t"])
    p = tmp_path / "a.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(mod.EstateError):
        mod.load_artifact(p)


def test_the_suppression_axis_is_a_multiset_not_a_set(tmp_path):
    """Two identical suppressions in one scope are TWO suppressions.

    A `set` would collapse exactly the thing the key model exists to keep,
    so a count of 2 must survive a load/compare round trip.
    """
    mod = _load()
    doc = _artifact(suppressions=[{"key": "tests/test_a.py|<module>|skip_call|",
                                   "count": 2}])
    p = tmp_path / "a.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    loaded = mod.load_artifact(p)
    assert loaded["suppressions"][0]["count"] == 2


def test_axis_diff_reports_both_directions_separately():
    mod = _load()
    live_only, recorded_only = mod.axis_diff(["a", "b"], ["b", "c"], multiset=False)
    assert live_only == ["c"]
    assert recorded_only == ["a"]


def test_axis_diff_over_a_multiset_counts_occurrences():
    mod = _load()
    live_only, recorded_only = mod.axis_diff(["a", "a"], ["a"], multiset=True)
    assert live_only == []
    assert recorded_only == ["a"]


def test_compose_private_refuses_an_overlap():
    """A row cannot be both a public row and an overlay addition."""
    mod = _load()
    pub = _artifact()
    overlay = {
        "schemaVersion": 1,
        "publicDigest": mod.active_set_digest(pub),
        "allowlistDigest": "x" * 64,
        "pytestNodes": {"additions": ["tests/test_a.py::test_one"], "removals": []},
        "frontendTests": {"additions": [], "removals": []},
        "suppressions": {"additions": [], "removals": []},
    }
    with pytest.raises(mod.EstateError):
        mod.compose_private(pub, overlay)


def test_compose_private_refuses_a_stale_public_digest():
    """#648 D1 — a stale partition must not pass."""
    mod = _load()
    pub = _artifact()
    overlay = {
        "schemaVersion": 1,
        "publicDigest": "0" * 64,
        "allowlistDigest": "x" * 64,
        "pytestNodes": {"additions": [], "removals": []},
        "frontendTests": {"additions": [], "removals": []},
        "suppressions": {"additions": [], "removals": []},
    }
    with pytest.raises(mod.EstateError):
        mod.compose_private(pub, overlay)


def test_the_overlay_carries_removals_as_well_as_additions():
    """M5's subset relation is empirical, not an invariant.

    A public test that adds a parameter only when a private file is ABSENT
    produces a public-only row -- the exact inverse of `_share_docs()`. An
    additions-only overlay could not represent that, so the delta is signed.
    """
    mod = _load()
    pub = _artifact()
    overlay = {
        "schemaVersion": 1,
        "publicDigest": mod.active_set_digest(pub),
        "allowlistDigest": "x" * 64,
        "pytestNodes": {"additions": [], "removals": ["tests/test_a.py::test_one"]},
        "frontendTests": {"additions": [], "removals": []},
        "suppressions": {"additions": [], "removals": []},
    }
    combined = mod.compose_private(pub, overlay)
    assert combined["pytestNodes"] == []


def test_the_overlay_adds_a_private_only_serial_selector():
    """Private wall-clock nodes extend the plan without breaking public CI."""
    mod = _load()
    public_node = "tests/test_public.py::test_fast"
    bulk_node = "tests/test_public.py::test_bulk"
    private_node = "tests/test_private.py::test_wall_clock"
    pub = _artifact(
        pytestNodes=[public_node, bulk_node],
        pytestExecution={"legs": [
            {"name": "benchmark", "selectors": [public_node]},
            {"name": "pytest", "selectors": ["*complement*"]},
        ]},
    )
    overlay = {
        "schemaVersion": 1,
        "publicDigest": mod.active_set_digest(pub),
        "allowlistDigest": "x" * 64,
        "pytestNodes": {"additions": [private_node], "removals": []},
        "frontendTests": {"additions": [], "removals": []},
        "suppressions": {"additions": [], "removals": []},
        "pytestExecution": {"benchmarkAdditions": [private_node]},
    }

    combined = mod.compose_private(pub, overlay)
    plan = mod.plan_execution(combined)

    assert plan.selectors["benchmark"] == (
        ("node", public_node), ("node", private_node))
    assert set(plan.legs["benchmark"]) == {public_node, private_node}
    assert plan.legs["pytest"] == (bulk_node,)


def test_compose_private_refuses_a_removal_naming_an_absent_row():
    mod = _load()
    pub = _artifact()
    overlay = {
        "schemaVersion": 1,
        "publicDigest": mod.active_set_digest(pub),
        "allowlistDigest": "x" * 64,
        "pytestNodes": {"additions": [], "removals": ["tests/test_zzz.py::nope"]},
        "frontendTests": {"additions": [], "removals": []},
        "suppressions": {"additions": [], "removals": []},
    }
    with pytest.raises(mod.EstateError):
        mod.compose_private(pub, overlay)


def test_the_overlay_adds_a_suppression_occurrence_to_a_key_already_public():
    """The suppression axis composes by OCCURRENCE, not by row identity.

    On the two identity axes an addition naming a row the public artifact
    already carries is a malformed partition and is refused. On this axis the
    same shape is legitimate and means "the private profile has one more of
    these", so applying the identity rule here would report a correct partition
    as broken. The public helper is what a public clone runs, so this branch is
    exercised publicly rather than only inside the mirror-private generator's
    own module.
    """
    mod = _load()
    key = "tests/test_a.py|<module>|skip_call|"
    pub = _artifact(suppressions=[{"key": key, "count": 1}])
    overlay = {
        "schemaVersion": 1,
        "publicDigest": mod.active_set_digest(pub),
        "allowlistDigest": "x" * 64,
        "pytestNodes": {"additions": [], "removals": []},
        "frontendTests": {"additions": [], "removals": []},
        "suppressions": {"additions": [{"key": key, "count": 2}], "removals": []},
    }
    combined = mod.compose_private(pub, overlay)
    assert combined["suppressions"] == [{"key": key, "count": 3}]


def test_the_overlay_refuses_removing_a_suppression_occurrence_that_is_not_public():
    """The refusal branch of the same composition, which nothing else covered.

    A delta that removes more occurrences of a key than the public artifact
    records describes a boundary that does not exist. Composing it anyway would
    silently produce a private profile with a NEGATIVE count collapsed away,
    which is a partition that looks complete and is not.
    """
    mod = _load()
    key = "tests/test_a.py|<module>|skip_call|"
    pub = _artifact(suppressions=[{"key": key, "count": 1}])
    overlay = {
        "schemaVersion": 1,
        "publicDigest": mod.active_set_digest(pub),
        "allowlistDigest": "x" * 64,
        "pytestNodes": {"additions": [], "removals": []},
        "frontendTests": {"additions": [], "removals": []},
        "suppressions": {"additions": [], "removals": [{"key": key, "count": 2}]},
    }
    with pytest.raises(mod.EstateError) as excinfo:
        mod.compose_private(pub, overlay)
    assert "does not carry" in str(excinfo.value)


def test_active_set_digest_is_stable_under_input_order():
    """The digest is over CONTENT, so a reordered input must not move it."""
    mod = _load()
    a = _artifact(pytestNodes=["tests/a.py::x", "tests/a.py::y"])
    b = _artifact(pytestNodes=["tests/a.py::y", "tests/a.py::x"])
    assert mod.active_set_digest(a) == mod.active_set_digest(b)


def test_active_set_digest_moves_when_a_row_is_removed():
    mod = _load()
    a = _artifact(pytestNodes=["tests/a.py::x", "tests/a.py::y"])
    b = _artifact(pytestNodes=["tests/a.py::x"])
    assert mod.active_set_digest(a) != mod.active_set_digest(b)


def test_active_set_digest_excludes_provenance_and_the_execution_split():
    """#648 D3a binds a declaration to the ACTIVE SET, not to the document.

    A regenerated `generatedFrom`, or a leg selector rewritten in a later
    session, would otherwise invalidate every outstanding declaration without
    a single estate row having changed.
    """
    mod = _load()
    a = _artifact()
    b = _artifact(
        generatedFrom="f" * 40,
        pytestExecution={"legs": [
            {"name": "benchmark", "selectors": ["tests/test_other.py"]},
            {"name": "pytest", "selectors": ["*complement*"]},
        ]},
    )
    assert mod.active_set_digest(a) == mod.active_set_digest(b)


def test_active_set_digest_moves_when_a_playwright_status_flips():
    """The status is part of the recorded row, so it is part of the digest."""
    mod = _load()
    a = _artifact(frontendTests=[{"runner": "playwright", "id": "x",
                                  "expectedStatus": "passed"}])
    b = _artifact(frontendTests=[{"runner": "playwright", "id": "x",
                                  "expectedStatus": "skipped"}])
    assert mod.active_set_digest(a) != mod.active_set_digest(b)


def test_the_helper_imports_nothing_outside_the_standard_library():
    """`bin/` is stdlib-only; this helper ships to the public mirror."""
    import ast
    tree = ast.parse(HELPER.read_text(encoding="utf-8"))
    third_party = {"pytest", "yaml", "rich", "xdist"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in third_party, alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            assert node.module.split(".")[0] not in third_party, node.module


def test_the_helper_is_public_in_the_mirror_allowlist():
    """#648 D5 / issue finding 11.

    `.mirror-allowlist` covers `bin/_lib-*` with a HYPHEN and enumerates
    underscore modules individually, so an unlisted underscore module
    classifies as private -- and the public clone is required to RUN this
    checker, so an unlisted helper breaks the public suite outright.
    """
    allowlist = ROOT / ".mirror-allowlist"
    if not allowlist.exists():  # mirror-private-ok
        pytest.skip("the mirror allowlist is maintainer-local")
    text = allowlist.read_text(encoding="utf-8")
    assert "bin/_lib_test_estate.py" in text.splitlines()


# ---------------------------------------------------------------------------
# #648 Task 3 — history-bound transition validation and the declaration ledger
# ---------------------------------------------------------------------------


def _ledger_entry(**over):
    e = {
        "id": "R-0001",
        "axis": "pytestNodes",
        "profile": "public",
        "cause": "retired",
        "reason": "the covered branch was deleted in #648",
        "rows": ["tests/test_a.py::test_one"],
        "predecessorDigest": None,
    }
    e.update(over)
    return e


def _rename_entry(**over):
    e = {
        "id": "R-0002",
        "axis": "pytestNodes",
        "profile": "public",
        "cause": "renamed",
        "reason": "the successor names the same live coverage truthfully",
        "renames": [{
            "from": "tests/test_a.py::test_old",
            "to": "tests/test_a.py::test_new",
        }],
        "predecessorDigest": None,
    }
    e.update(over)
    return e


def test_removing_a_pytest_node_without_a_declaration_is_unauthorized():
    """#648 D3 -- the harmful direction on an identity axis is REMOVAL."""
    mod = _load()
    prev = _artifact(pytestNodes=["tests/test_a.py::test_one"])
    cur = _artifact(pytestNodes=[])
    problems = mod.validate_transition(prev, cur, [], "public")
    assert problems, "a bare removal must not be authorized"


def test_removing_a_pytest_node_with_a_covering_declaration_is_authorized():
    mod = _load()
    prev = _artifact(pytestNodes=["tests/test_a.py::test_one"])
    cur = _artifact(pytestNodes=[])
    entry = _ledger_entry(predecessorDigest=mod.active_set_digest(prev))
    assert mod.validate_transition(prev, cur, [entry], "public") == []


def test_adding_a_pytest_node_is_free():
    mod = _load()
    prev = _artifact(pytestNodes=[])
    cur = _artifact(pytestNodes=["tests/test_a.py::test_one"])
    assert mod.validate_transition(prev, cur, [], "public") == []


def test_a_verified_rename_authorizes_the_old_identity_removal():
    """#674: a rename is recorded as a migration, not false retirement.

    The successor must be a newly-added identity in the same transition.  This
    makes the mapping evidence rather than prose attached to an unconditional
    removal waiver.
    """
    mod = _load()
    prev = _artifact(pytestNodes=["tests/test_a.py::test_old"])
    cur = _artifact(pytestNodes=["tests/test_a.py::test_new"])
    entry = _rename_entry(predecessorDigest=mod.active_set_digest(prev))
    assert mod.validate_transition(prev, cur, [entry], "public") == []


@pytest.mark.parametrize("current", [
    [],
    ["tests/test_a.py::test_preexisting"],
])
def test_a_rename_without_its_declared_new_successor_is_unauthorized(current):
    mod = _load()
    prev = _artifact(pytestNodes=["tests/test_a.py::test_old"])
    cur = _artifact(pytestNodes=current)
    entry = _rename_entry(predecessorDigest=mod.active_set_digest(prev))
    assert mod.validate_transition(prev, cur, [entry], "public")


def test_a_rename_cannot_point_at_an_identity_that_already_existed():
    """An unrelated surviving test is not evidence that coverage was renamed."""
    mod = _load()
    prev = _artifact(pytestNodes=["tests/test_a.py::test_old",
                                  "tests/test_a.py::test_new"])
    cur = _artifact(pytestNodes=["tests/test_a.py::test_new"])
    entry = _rename_entry(predecessorDigest=mod.active_set_digest(prev))
    assert mod.validate_transition(prev, cur, [entry], "public")


def test_two_removed_identities_cannot_claim_one_successor():
    """A many-to-one declaration would conceal a real loss of coverage."""
    mod = _load()
    prev = _artifact(pytestNodes=["tests/test_a.py::test_old",
                                  "tests/test_a.py::test_other_old"])
    cur = _artifact(pytestNodes=["tests/test_a.py::test_new"])
    entry = _rename_entry(
        renames=[
            {"from": "tests/test_a.py::test_old",
             "to": "tests/test_a.py::test_new"},
            {"from": "tests/test_a.py::test_other_old",
             "to": "tests/test_a.py::test_new"},
        ],
        predecessorDigest=mod.active_set_digest(prev),
    )
    with pytest.raises(mod.EstateError):
        mod.load_ledger_document({"schemaVersion": 1,
                                  "declarations": [entry]})


def test_ADDING_a_suppression_without_a_declaration_is_unauthorized():
    """#648 D3 -- the suppression axis's harmful direction is INVERTED.

    This is the defect the pre-plan review gate caught in the first draft. A
    new suppression is `live - old`, so an additions-are-free rule would
    accept it and write it into the artifact, leaving the gate structurally
    unable to detect the very thing #529 criterion 1 names.
    """
    mod = _load()
    prev = _artifact(suppressions=[])
    cur = _artifact(suppressions=[{"key": "tests/test_a.py|<module>|skip_call|",
                                  "count": 1}])
    problems = mod.validate_transition(prev, cur, [], "public")
    assert problems, "a new suppression must not be a free addition"


def test_REMOVING_a_suppression_is_free():
    mod = _load()
    prev = _artifact(suppressions=[{"key": "k", "count": 1}])
    cur = _artifact(suppressions=[])
    assert mod.validate_transition(prev, cur, [], "public") == []


def test_a_playwright_row_going_active_to_skipped_is_harmful():
    mod = _load()
    prev = _artifact(frontendTests=[{"runner": "playwright", "id": "x",
                                     "expectedStatus": "passed"}])
    cur = _artifact(frontendTests=[{"runner": "playwright", "id": "x",
                                    "expectedStatus": "skipped"}])
    problems = mod.validate_transition(prev, cur, [], "public")
    assert problems


def test_a_replayed_declaration_fails_because_its_predecessor_digest_moved():
    """#648 D3a -- binding to the predecessor digest is what stops replay."""
    mod = _load()
    prev = _artifact(pytestNodes=["tests/test_a.py::test_one"])
    cur = _artifact(pytestNodes=[])
    stale = _ledger_entry(predecessorDigest="0" * 64)
    problems = mod.validate_transition(prev, cur, [stale], "public")
    assert problems, "a declaration bound to another predecessor must not apply"


def test_a_duplicate_declaration_id_is_refused(tmp_path):
    mod = _load()
    p = tmp_path / "ledger.json"
    p.write_text(json.dumps({"schemaVersion": 1,
                             "declarations": [_ledger_entry(), _ledger_entry()]}),
                 encoding="utf-8")
    with pytest.raises(mod.EstateError):
        mod.load_ledger(p)


def test_a_reclassified_cause_requires_the_row_in_the_other_profile():
    """#648 D3a -- a `reclassified` claim is verified, never trusted.

    Promoting a file in `.mirror-allowlist` moves rows between profiles. That
    is not a retirement, and demanding one would fire on every promotion; but
    an unverified `reclassified` label would let a real deletion wear it.
    """
    mod = _load()
    prev = _artifact(pytestNodes=["tests/test_a.py::test_one"])
    cur = _artifact(pytestNodes=[])
    entry = _ledger_entry(cause="reclassified",
                          predecessorDigest=mod.active_set_digest(prev))
    # The row is absent from the other profile, so the claim is false.
    problems = mod.validate_transition(prev, cur, [entry], "public",
                                       other_profile_rows={"pytestNodes": []})
    assert problems
    # Present in the other profile, so the claim is true.
    assert mod.validate_transition(
        prev, cur, [entry], "public",
        other_profile_rows={"pytestNodes": ["tests/test_a.py::test_one"]}) == []


def test_a_declaration_for_the_other_profile_does_not_apply():
    mod = _load()
    prev = _artifact(pytestNodes=["tests/test_a.py::test_one"])
    cur = _artifact(pytestNodes=[])
    entry = _ledger_entry(profile="private",
                          predecessorDigest=mod.active_set_digest(prev))
    assert mod.validate_transition(prev, cur, [entry], "public")


def test_a_declaration_must_carry_a_non_empty_reason(tmp_path):
    mod = _load()
    p = tmp_path / "ledger.json"
    p.write_text(json.dumps({"schemaVersion": 1,
                             "declarations": [_ledger_entry(reason="")]}),
                 encoding="utf-8")
    with pytest.raises(mod.EstateError):
        mod.load_ledger(p)


def test_one_declaration_may_cover_a_batch_of_rows():
    """A parametrization rewrite costs ONE entry, not one per row."""
    mod = _load()
    rows = [f"tests/test_a.py::t[{i}]" for i in range(50)]
    prev = _artifact(pytestNodes=rows)
    cur = _artifact(pytestNodes=[])
    entry = _ledger_entry(rows=rows, predecessorDigest=mod.active_set_digest(prev))
    assert mod.validate_transition(prev, cur, [entry], "public") == []


def test_a_suppression_count_INCREASE_is_harmful_and_a_DECREASE_is_free():
    """The axis is a multiset of KEYS, so an occurrence is the unit.

    Neither of the two directional cases above distinguishes this, because both
    move a key between absent and present. Comparing the recorded `{key, count}`
    OBJECTS instead of the occurrences they denote inverts the rule for exactly
    this case: 2 -> 1 reads as the addition of a `{key, count: 1}` object and
    would be reported as a new suppression, while 1 -> 2 reads as the addition
    of a `{key, count: 2}` object and would be reported identically. The rule
    has to see one occurrence gained and one occurrence lost.
    """
    mod = _load()
    one = [{"key": "k", "count": 1}]
    two = [{"key": "k", "count": 2}]
    assert mod.validate_transition(
        _artifact(suppressions=one), _artifact(suppressions=two), [], "public")
    assert mod.validate_transition(
        _artifact(suppressions=two), _artifact(suppressions=one), [], "public") == []


def test_a_playwright_row_going_skipped_to_active_is_free():
    """Un-skipping a test is the beneficial direction and costs nothing.

    Matching a frontend removal on the FULL row would report this as the
    removal of the skipped row, so re-enabling a test would demand a retirement
    declaration. The removal rule matches on (runner, id); only the status
    transition rule reads the status.
    """
    mod = _load()
    prev = _artifact(frontendTests=[{"runner": "playwright", "id": "x",
                                     "expectedStatus": "skipped"}])
    cur = _artifact(frontendTests=[{"runner": "playwright", "id": "x",
                                    "expectedStatus": "passed"}])
    assert mod.validate_transition(prev, cur, [], "public") == []


def test_a_frontend_test_that_disappears_entirely_is_harmful():
    mod = _load()
    prev = _artifact(frontendTests=[{"runner": "vitest", "id": "a.test.ts > x",
                                     "expectedStatus": None}])
    cur = _artifact(frontendTests=[])
    assert mod.validate_transition(prev, cur, [], "public")


def test_a_declaration_covers_only_the_rows_it_names():
    """A partial declaration authorizes its own rows and nothing else."""
    mod = _load()
    prev = _artifact(pytestNodes=["tests/test_a.py::one", "tests/test_a.py::two"])
    cur = _artifact(pytestNodes=[])
    entry = _ledger_entry(rows=["tests/test_a.py::one"],
                          predecessorDigest=mod.active_set_digest(prev))
    problems = mod.validate_transition(prev, cur, [entry], "public")
    assert len(problems) == 1
    assert "tests/test_a.py::two" in problems[0]


def test_a_reclassified_claim_with_no_other_profile_supplied_is_reported():
    """`other_profile_rows=None` makes the claim unverifiable, so it fails.

    An unverifiable claim must not be treated as a verified one; that is the
    whole difference between checking a `reclassified` cause and trusting it.
    """
    mod = _load()
    prev = _artifact(pytestNodes=["tests/test_a.py::test_one"])
    cur = _artifact(pytestNodes=[])
    entry = _ledger_entry(cause="reclassified",
                          predecessorDigest=mod.active_set_digest(prev))
    assert mod.validate_transition(prev, cur, [entry], "public")


def test_a_declaration_naming_another_axis_does_not_apply():
    mod = _load()
    prev = _artifact(pytestNodes=["tests/test_a.py::test_one"])
    cur = _artifact(pytestNodes=[])
    entry = _ledger_entry(axis="frontendTests",
                          predecessorDigest=mod.active_set_digest(prev))
    assert mod.validate_transition(prev, cur, [entry], "public")


def test_an_unknown_cause_is_refused(tmp_path):
    mod = _load()
    p = tmp_path / "ledger.json"
    p.write_text(json.dumps({"schemaVersion": 1,
                             "declarations": [_ledger_entry(cause="obsolete")]}),
                 encoding="utf-8")
    with pytest.raises(mod.EstateError):
        mod.load_ledger(p)


def test_a_duplicate_declaration_id_ACROSS_the_two_ledgers_is_refused(tmp_path):
    """#648 acceptance criterion 11 -- the id is unique across BOTH files.

    `load_ledger` can only see one document, so within-document uniqueness is
    not the whole rule: the public and mirror-private ledgers are separate
    files, and a replay pasted from one into the other is exactly the case a
    per-file check cannot see.
    """
    mod = _load()
    public = tmp_path / "public.json"
    private = tmp_path / "private.json"
    public.write_text(json.dumps({"schemaVersion": 1,
                                  "declarations": [_ledger_entry()]}),
                      encoding="utf-8")
    private.write_text(json.dumps({"schemaVersion": 1,
                                   "declarations": [_ledger_entry(profile="private")]}),
                       encoding="utf-8")
    with pytest.raises(mod.EstateError):
        mod.merge_ledgers(mod.load_ledger(public), mod.load_ledger(private))


def test_an_empty_ledger_loads_as_no_declarations(tmp_path):
    """A ledger with no declarations is well formed, not malformed.

    Both files were seeded empty, and a public clone that has never retired
    anything keeps its ledger in exactly that state.
    """
    mod = _load()
    p = tmp_path / "ledger.json"
    p.write_text(json.dumps({"schemaVersion": 1, "declarations": []}),
                 encoding="utf-8")
    assert mod.load_ledger(p) == []


def test_the_committed_ledgers_load_and_carry_validated_declarations():
    """Both ledgers are committed, append-only, and parse under the checker.

    The assertion was `== []` while both files were newly seeded. They are no
    longer empty -- the tranche A review split seven generator tests out of this
    module and into a mirror-private one, which retired seven public node
    identifiers -- so what is pinned is that every committed declaration passes
    `load_ledger`, and that no identifier is reused across the two files.
    """
    mod = _load()
    # The waiver has to sit inside a SIMPLE statement, and a `for` header is a
    # compound one, so the names are bound first. The private half is absent on
    # a public clone and the loop skips it.
    names = ("tests/authoritative-estate-retirements.json",
             "tests/authoritative-estate-retirements.private.json")  # mirror-private-ok
    loaded = []
    for name in names:
        path = ROOT / name
        if not path.exists():  # mirror-private-ok
            pytest.skip(f"{name} is maintainer-local")
        entries = mod.load_ledger(path)
        for entry in entries:
            assert entry["reason"].strip(), (name, entry["id"])
            payload = (entry["renames"] if entry["cause"] == "renamed"
                       else entry["rows"])
            assert payload, (name, entry["id"])
        loaded.append(entries)
    mod.merge_ledgers(*loaded)


def test_the_private_ledger_and_overlay_are_negated_in_the_allowlist():
    """#648 D3a -- a declaration NAMES the identifiers it retires.

    A declaration covering a mirror-private test would publish that test's name,
    so the private ledger stays behind the boundary for the same reason the
    ownership overlay does, and so does the private overlay itself.
    """
    allowlist = ROOT / ".mirror-allowlist"
    if not allowlist.exists():  # mirror-private-ok
        pytest.skip("the mirror allowlist is maintainer-local")
    lines = allowlist.read_text(encoding="utf-8").splitlines()
    assert "!tests/authoritative-estate.private.json" in lines
    assert "!tests/authoritative-estate-retirements.private.json" in lines


# ---------------------------------------------------------------------------
# #648 Task 4 — the generator's mirror boundary
#
# Every other generator test lives in `tests/test_estate_manifest_generator.py`,
# which is mirror-private: those tests LOAD `bin/cctally-estate-manifest`, and a
# public clone does not carry it. This one stays here because it reads only the
# allowlist and skips when that is absent.
# ---------------------------------------------------------------------------


def test_the_generator_is_private_in_the_mirror_allowlist():
    """It imports bin/_cctally_public_projection.py, which is maintainer-only.

    The `bin/cctally-*` glob matches it, so omitting the negation would PUBLISH
    it rather than hide it -- and the published copy would import a module the
    mirror does not carry.
    """
    allowlist = ROOT / ".mirror-allowlist"
    if not allowlist.exists():  # mirror-private-ok
        pytest.skip("the mirror allowlist is maintainer-local")
    assert "!bin/cctally-estate-manifest" in allowlist.read_text(
        encoding="utf-8").splitlines()


# ---------------------------------------------------------------------------
# #648 review remediation — the comparison, the seams and the vacuity holes
# ---------------------------------------------------------------------------


class _Row:
    """A duck-typed discovery-kernel row; the helper never imports the kernel."""

    def __init__(self, **fields):
        self.__dict__.update(fields)


class _Sets:
    """A duck-typed `EstateSets`, in the kernel's own attribute spelling."""

    def __init__(self, pytest_nodes=(), frontend_tests=(), suppressions=()):
        self.pytest_nodes = tuple(pytest_nodes)
        self.frontend_tests = tuple(frontend_tests)
        self.suppressions = tuple(suppressions)


def test_live_axes_renders_a_kernel_snapshot_in_the_artifacts_row_shapes():
    """#648 review P2-3 -- the function the gate feeds `compare` with.

    It is the one place the kernel's attribute spelling becomes the artifact's
    key spelling, and a mistranslation there compares a live tree against the
    record on the wrong field.
    """
    mod = _load()
    axes = mod.live_axes(_Sets(
        pytest_nodes=("tests/test_a.py::one",),
        frontend_tests=(_Row(runner="playwright", id="e2e/a.spec.ts::A > b",
                             expected_status="expected"),),
        suppressions=(_Row(key="k"), _Row(key="k"), _Row(key="j")),
    ))
    assert axes["pytestNodes"] == ["tests/test_a.py::one"]
    assert axes["frontendTests"] == [
        {"runner": "playwright", "id": "e2e/a.spec.ts::A > b",
         "expectedStatus": "expected"},
    ]
    # The kernel emits one row per occurrence; the artifact records counts.
    assert axes["suppressions"] == [{"key": "j", "count": 1},
                                    {"key": "k", "count": 2}]


def test_live_axes_passes_a_mapping_through_unchanged():
    """The gate may already hold artifact-shaped axes; that must not re-derive."""
    mod = _load()
    given = {"pytestNodes": ["a"], "frontendTests": [], "suppressions": []}
    assert mod.live_axes(given) == given


def test_live_discovery_overlaps_the_two_subprocess_collectors(
        tmp_path, monkeypatch):
    """Pytest and frontend collection are independent, expensive subprocesses.

    Running them serially pushed the real projected-public-tree acceptance
    test past the authoritative suite's 120-second per-node timeout. Each fake
    collector therefore requires the other to have started before it returns;
    a serial implementation fails instead of merely making this test slow.
    """
    mod = _load()
    repo = tmp_path / "repo"
    builder = repo / "bin" / "build-e2e-fixtures.py"
    builder.parent.mkdir(parents=True)
    builder.write_text("# fixture builder seam\n", encoding="utf-8")
    pytest_started = threading.Event()
    frontend_started = threading.Event()

    class _Discovery:
        @staticmethod
        def collect_pytest_nodes(_repo):
            pytest_started.set()
            # timing-budget: the short wait is the concurrency assertion
            assert frontend_started.wait(1.0), "frontend collection stayed serial"
            return ["tests/test_a.py::test_one"]

        @staticmethod
        def collect_frontend_tests(_repo, runtime_dir):
            assert runtime_dir.is_dir()
            frontend_started.set()
            # timing-budget: the short wait is the concurrency assertion
            assert pytest_started.wait(1.0), "pytest collection stayed serial"
            return [_Row(runner="vitest", id="a.test.ts > x",
                         expected_status=None)]

        @staticmethod
        def scan_suppressions(_tests, base):
            assert base == repo
            return [_Row(key="tests/test_a.py::<module>::skip_call::x")]

    monkeypatch.setattr(mod, "_load_discovery_kernel",
                        lambda _repo: _Discovery)
    monkeypatch.setattr(mod.subprocess, "run", lambda *args, **kwargs:
                        types.SimpleNamespace(returncode=0, stdout="", stderr=""))

    assert mod.discover_live(repo) == {
        "pytestNodes": ["tests/test_a.py::test_one"],
        "frontendTests": [{"runner": "vitest", "id": "a.test.ts > x",
                           "expectedStatus": None}],
        "suppressions": [{"key": "tests/test_a.py::<module>::skip_call::x",
                          "count": 1}],
    }


def test_compare_is_empty_when_the_record_and_the_tree_agree():
    mod = _load()
    art = _artifact()
    assert mod.compare(art, {axis: list(art[axis]) for axis in mod.AXES}) == []


def test_compare_reports_both_directions_on_all_three_axes():
    """#648 review P2-3 -- every `manifest-*` reason code comes from here.

    Six codes, one pair per axis, and the pairing is what makes a lost test and
    an unrecorded new one different findings.
    """
    mod = _load()
    art = _artifact()
    live = {
        "pytestNodes": ["tests/test_b.py::test_new"],
        "frontendTests": [{"runner": "vitest", "id": "b.test.ts > y",
                           "expectedStatus": None}],
        "suppressions": [{"key": "tests/test_b.py|<module>|skip_call|",
                          "count": 1}],
    }
    findings = mod.compare(art, live)
    seen = {(f.code_tag, f.axis) for f in findings}
    for axis in mod.AXES:
        assert ("unexpected", axis) in seen, (axis, seen)
        assert ("missing", axis) in seen, (axis, seen)
    unexpected = [f for f in findings
                  if f.code_tag == "unexpected" and f.axis == "pytestNodes"]
    assert unexpected[0].rows == ["tests/test_b.py::test_new"]


def test_compare_counts_a_suppression_OCCURRENCE_rather_than_a_key():
    """A count of two against a count of one is one unexpected occurrence.

    A set would collapse it to nothing, which is exactly the loss the key model
    exists to prevent.
    """
    mod = _load()
    art = _artifact(suppressions=[{"key": "k", "count": 1}])
    findings = mod.compare(art, {"pytestNodes": art["pytestNodes"],
                                 "frontendTests": art["frontendTests"],
                                 "suppressions": [{"key": "k", "count": 2}]})
    assert [(f.code_tag, f.axis, f.rows) for f in findings] == [
        ("unexpected", "suppressions", ["k"]),
    ]


def test_compare_sees_a_playwright_status_flip_as_two_findings():
    """The status is part of the recorded row, so a flip is a row that moved."""
    mod = _load()
    art = _artifact(frontendTests=[{"runner": "playwright", "id": "x",
                                    "expectedStatus": "expected"}])
    live = {"pytestNodes": art["pytestNodes"], "suppressions": art["suppressions"],
            "frontendTests": [{"runner": "playwright", "id": "x",
                               "expectedStatus": "skipped"}]}
    tags = {(f.code_tag, f.axis) for f in mod.compare(art, live)}
    assert tags == {("unexpected", "frontendTests"), ("missing", "frontendTests")}


def test_compose_private_verifies_the_recorded_allowlist_digest():
    """#648 D1 -- the checker refuses when EITHER digest has moved.

    The overlay records the digest of the `.mirror-allowlist` it was cut
    against, and that recording was validated only for being a non-empty string,
    so an allowlist edit that moved rows between profiles composed silently.
    """
    mod = _load()
    pub = _artifact()
    overlay = {
        "schemaVersion": 1,
        "publicDigest": mod.active_set_digest(pub),
        "allowlistDigest": "a" * 64,
        "pytestNodes": {"additions": [], "removals": []},
        "frontendTests": {"additions": [], "removals": []},
        "suppressions": {"additions": [], "removals": []},
    }
    # The seam is optional, because a public clone carries no allowlist to
    # digest and must still be able to compose nothing at all.
    mod.compose_private(pub, overlay)
    mod.compose_private(pub, overlay, expect_allowlist_digest="a" * 64)
    with pytest.raises(mod.EstateError):
        mod.compose_private(pub, overlay, expect_allowlist_digest="b" * 64)


def test_allowlist_digest_reads_the_file_and_refuses_an_absent_one(tmp_path):
    """One definition of the digest, so the generator and the gate agree.

    Digested from a fixture rather than from the real `.mirror-allowlist`,
    which a public clone does not carry: a skip here would ADD a suppression to
    both profiles, and the mechanism this session builds treats that as the
    harmful direction.
    """
    import hashlib
    mod = _load()
    path = tmp_path / "allowlist"
    path.write_bytes(b"bin/cctally\n!bin/cctally-estate-manifest\n")
    assert mod.allowlist_digest(path) == hashlib.sha256(
        path.read_bytes()).hexdigest()
    with pytest.raises(mod.EstateError):
        mod.allowlist_digest(tmp_path / "absent")


def test_a_transition_with_NO_predecessor_is_authorized():
    """#648 review P3-2 -- the gate meets a root commit and a seeding run.

    Nothing can have been lost relative to a predecessor that does not exist, so
    the answer is an empty problem list rather than an `AttributeError`.
    """
    mod = _load()
    cur = _artifact(suppressions=[{"key": "k", "count": 2}])
    assert mod.validate_transition(None, cur, [], "public") == []
    assert mod.uncovered_transitions(None, cur, [], "public") == []


def test_adding_a_frontend_test_is_free():
    """#648 review P3-7 -- the free direction on the frontend axis.

    Every existing frontend case moves a row out or flips a status, so a rule
    that made `frontendTests` harmful in BOTH directions passed the suite.
    """
    mod = _load()
    prev = _artifact(frontendTests=[])
    cur = _artifact(frontendTests=[{"runner": "vitest", "id": "a.test.ts > x",
                                    "expectedStatus": None}])
    assert mod.validate_transition(prev, cur, [], "public") == []


def test_adding_a_pytest_node_stays_free_when_a_suppression_is_added_too():
    """The per-axis rule has to hold with both axes moving at once."""
    mod = _load()
    prev = _artifact(pytestNodes=[], suppressions=[])
    cur = _artifact(pytestNodes=["tests/test_a.py::t"],
                    suppressions=[{"key": "k", "count": 1}])
    problems = mod.validate_transition(prev, cur, [], "public")
    assert len(problems) == 1
    assert problems[0].startswith("suppressions:"), problems


def test_a_reclassified_cause_cannot_authorize_an_ADDED_suppression():
    """#648 review P2-6 -- the verification is vacuous in that direction.

    `reclassified` is verified by finding the row in the OTHER profile. On the
    public profile the other profile is private, and private is a superset of
    public, so any newly added public suppression satisfies the check by
    construction. The claim is also meaningless: a suppression that appears in
    both profiles was not moved between them.
    """
    mod = _load()
    prev = _artifact(suppressions=[])
    cur = _artifact(suppressions=[{"key": "k", "count": 1}])
    entry = _ledger_entry(axis="suppressions", cause="reclassified", rows=["k"],
                          predecessorDigest=mod.active_set_digest(prev))
    problems = mod.validate_transition(
        prev, cur, [entry], "public",
        other_profile_rows={"suppressions": [{"key": "k", "count": 1}]})
    assert problems, "a superset lookup cannot authorize a new suppression"
    # A `retired` cause is refused for a different reason -- it is simply not
    # what the ledger says -- so the same declaration under `retired` DOES
    # authorize it, which is what makes the clause above load-bearing.
    retired = dict(entry, cause="retired")
    assert mod.validate_transition(prev, cur, [retired], "public") == []


def test_a_reclassified_cause_cannot_authorize_a_playwright_SKIP():
    """The same vacuity, on the status-transition rule.

    The row keeps its identity when its status flips, so it is present in both
    profiles by construction and the other-profile lookup always succeeds.
    Skipping a test is not moving it across the mirror boundary.
    """
    mod = _load()
    prev = _artifact(frontendTests=[{"runner": "playwright", "id": "x",
                                     "expectedStatus": "expected"}])
    cur = _artifact(frontendTests=[{"runner": "playwright", "id": "x",
                                    "expectedStatus": "skipped"}])
    entry = _ledger_entry(axis="frontendTests", cause="reclassified",
                          rows=["playwright::x"],
                          predecessorDigest=mod.active_set_digest(prev))
    assert mod.validate_transition(
        prev, cur, [entry], "public",
        other_profile_rows={"frontendTests": cur["frontendTests"]})


def test_a_reclassified_cause_still_authorizes_a_real_REMOVAL():
    """The clause narrows the cause; it must not retire it.

    Promoting or demoting a file in `.mirror-allowlist` moves identity rows
    between the profiles, and that is exactly what `reclassified` is for.
    """
    mod = _load()
    prev = _artifact(pytestNodes=["tests/test_a.py::test_one"])
    cur = _artifact(pytestNodes=[])
    entry = _ledger_entry(cause="reclassified",
                          predecessorDigest=mod.active_set_digest(prev))
    assert mod.validate_transition(
        prev, cur, [entry], "public",
        other_profile_rows={"pytestNodes": ["tests/test_a.py::test_one"]}) == []


def test_uncovered_transitions_carry_the_row_token_STRUCTURALLY():
    """#648 review P2-1 -- the caller must not have to re-parse the message.

    The generator recovered the row by splitting its own message on `" "`, which
    truncated every node identifier and every Playwright row containing a space.
    The structured record carries the token itself.
    """
    mod = _load()
    row = "tests/test_setup.py::test_recognizer[/usr/bin/cctally statusline]"
    prev = _artifact(pytestNodes=[row])
    cur = _artifact(pytestNodes=[])
    found = mod.uncovered_transitions(prev, cur, [], "public")
    assert [(u.axis, u.token, u.kind) for u in found] == [
        ("pytestNodes", row, "removed"),
    ]
    assert row in found[0].message
    # `validate_transition` stays the message-rendering face of the same walk.
    assert mod.validate_transition(prev, cur, [], "public") == [found[0].message]


def test_uncovered_transitions_names_the_kind_of_each_harmful_change():
    mod = _load()
    prev = _artifact(suppressions=[],
                     frontendTests=[{"runner": "playwright", "id": "x",
                                     "expectedStatus": "expected"}])
    cur = _artifact(suppressions=[{"key": "k", "count": 1}],
                    frontendTests=[{"runner": "playwright", "id": "x",
                                    "expectedStatus": "skipped"}])
    kinds = {(u.axis, u.kind) for u in mod.uncovered_transitions(
        prev, cur, [], "public")}
    assert kinds == {("suppressions", "added"), ("frontendTests", "skipped")}


def test_the_e2e_runtime_seam_falls_back_on_an_EMPTY_variable():
    """#648 review P3-6 -- `??` only falls back on null and undefined.

    `CCTALLY_E2E_RUNTIME_DIR=""` is neither, so the nullish coalescing operator
    resolved the empty string against the process working directory and the nine
    spec files that read the manifest at module load looked for it there. A
    Playwright-side module cannot be exercised from pytest, so what is pinned
    here is the operator itself.
    """
    utils = ROOT / "dashboard" / "web" / "e2e" / "utils.ts"
    lines = [line for line in utils.read_text(encoding="utf-8").splitlines()
             if "CCTALLY_E2E_RUNTIME_DIR" in line and "resolve(" in line]
    assert len(lines) == 1, lines
    assert "??" not in lines[0], lines[0]
    assert "||" in lines[0], lines[0]


# ---------------------------------------------------------------------------
# #648 tranche B — the checker the gate runs
#
# `bin/_lib-test-contract.sh` owns the verdict and every reason-code literal;
# these tests cover the machinery it calls. The helper is public because a
# public clone runs the same checker against its own live collection, so every
# unit below is exercised from the public module.
# ---------------------------------------------------------------------------


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True)


def _seed_repo(tmp_path, artifact=None, overlay=None, ledger=None):
    """A tiny git repository carrying committed estate documents."""
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    if artifact is not None:
        (repo / "tests" / "authoritative-estate.json").write_text(
            json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if overlay is not None:
        (repo / "tests" / "authoritative-estate.private.json").write_text(
            json.dumps(overlay, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if ledger is not None:
        (repo / "tests" / "authoritative-estate-retirements.json").write_text(
            json.dumps(ledger, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")
    return repo


def test_predecessor_revisions_names_head_on_an_ordinary_commit(tmp_path):
    mod = _load()
    repo = _seed_repo(tmp_path, artifact=_artifact())
    status, revs = mod.predecessor_revisions(repo)
    assert status == "ok"
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert revs == [head]


def test_predecessor_revisions_names_every_parent_of_a_merge(tmp_path):
    """#648 D3 -- a merge is precisely where a row vanishes from both sides.

    A merge resolution can drop a test and its artifact row together, and the
    merge commit itself then agrees with neither parent's artifact about what
    was lost. Comparing only against the merge commit would compare the tree
    with itself.
    """
    mod = _load()
    repo = _seed_repo(tmp_path, artifact=_artifact())
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "checkout", "-q", "-b", "side")
    (repo / "side.txt").write_text("s\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "side")
    side = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "checkout", "-q", "-")
    (repo / "main.txt").write_text("m\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "main")
    mainline = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge", side)
    merge = _git(repo, "rev-parse", "HEAD").stdout.strip()
    status, revs = mod.predecessor_revisions(repo)
    assert status == "ok"
    assert revs[0] == merge
    assert set(revs[1:]) == {mainline, side}
    assert base not in revs


def test_predecessor_revisions_reports_an_unreadable_repository(tmp_path):
    """#648 D9 -- "I could not read HEAD" is not "HEAD records nothing".

    Treating a git failure as an absent predecessor would silently disable the
    whole transition check on an unusable repository, which is the same class
    of inability D9 forbids substituting an empty derivation for.
    """
    mod = _load()
    plain = tmp_path / "plain"
    plain.mkdir()
    status, message = mod.predecessor_revisions(plain)
    assert status == "unreadable"
    assert "git" in message.lower()


def test_predecessor_revisions_reports_no_predecessor_on_an_unborn_branch(tmp_path):
    """A repository with no commits records no artifact; that is not a failure."""
    mod = _load()
    repo = tmp_path / "empty"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    status, revs = mod.predecessor_revisions(repo)
    assert status == "ok"
    assert revs == []


def test_committed_document_separates_absent_from_unreadable(tmp_path):
    mod = _load()
    repo = _seed_repo(tmp_path, artifact=_artifact())
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    recorded = mod.committed_document(repo, head, "tests/authoritative-estate.json")
    assert recorded.status == "recorded"
    assert recorded.document["schemaVersion"] == 1
    # Through the module constant rather than as a literal. The public
    # dependency-closure guard flags a public test file that names a
    # mirror-excluded path in one string, and it is right to: the path the
    # constant holds is exactly such a path.
    absent = mod.committed_document(repo, head, mod.PRIVATE_OVERLAY)
    assert absent.status == "absent"
    bad = mod.committed_document(repo, "cafe" * 10, "tests/authoritative-estate.json")
    assert bad.status == "unreadable"


def test_committed_document_reports_malformed_json_as_unreadable(tmp_path):
    mod = _load()
    repo = _seed_repo(tmp_path, artifact=_artifact())
    (repo / "tests" / "authoritative-estate.json").write_text("{", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "break")
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    result = mod.committed_document(repo, head, "tests/authoritative-estate.json")
    assert result.status == "unreadable"


def test_plan_execution_partitions_the_recorded_estate():
    """#648 D7 -- both legs are built from ONE declaration, and it is proven."""
    mod = _load()
    doc = _artifact(
        pytestNodes=["tests/test_a.py::test_one", "tests/test_b.py::test_two",
                     "tests/test_b.py::test_three"],
        pytestExecution={"legs": [
            {"name": "benchmark", "selectors": ["tests/test_b.py"]},
            {"name": "pytest", "selectors": ["*complement*"]},
        ]},
    )
    plan = mod.plan_execution(doc)
    assert plan.legs["benchmark"] == ("tests/test_b.py::test_three",
                                      "tests/test_b.py::test_two")
    assert plan.legs["pytest"] == ("tests/test_a.py::test_one",)
    assert plan.selectors["benchmark"] == (("file", "tests/test_b.py"),)


def test_plan_execution_refuses_a_selector_that_matches_nothing():
    """A missing target becomes an admission failure, never a skipped leg."""
    mod = _load()
    doc = _artifact(
        pytestNodes=["tests/test_a.py::test_one"],
        pytestExecution={"legs": [
            {"name": "benchmark", "selectors": ["tests/test_gone.py"]},
            {"name": "pytest", "selectors": ["*complement*"]},
        ]},
    )
    with pytest.raises(mod.EstateError) as excinfo:
        mod.plan_execution(doc)
    assert "tests/test_gone.py" in str(excinfo.value)


def test_plan_execution_refuses_a_leg_whose_share_is_empty():
    """Every REQUIRED leg must match at least one node, not just its selectors.

    A complement leg carries no selector to check, so the selector rule alone
    cannot see it emptying out -- and an empty leg is a leg that silently runs
    nothing.
    """
    mod = _load()
    doc = _artifact(
        pytestNodes=["tests/test_b.py::test_two"],
        pytestExecution={"legs": [
            {"name": "benchmark", "selectors": ["tests/test_b.py"]},
            {"name": "pytest", "selectors": ["*complement*"]},
        ]},
    )
    with pytest.raises(mod.EstateError) as excinfo:
        mod.plan_execution(doc)
    assert "pytest" in str(excinfo.value)


def test_plan_execution_collapses_two_selectors_that_name_the_same_node():
    """Overlap WITHIN one leg is accepted, because a share is a set.

    A file selector and an explicit node identifier under it name the same
    node, and one leg claiming it twice still runs it once. The name of this
    test used to promise a refusal that its body never asserted; the refusal
    that does exist is between legs, and is pinned below.
    """
    mod = _load()
    doc = _artifact(
        pytestNodes=["tests/test_a.py::test_one", "tests/test_b.py::test_two"],
        pytestExecution={"legs": [
            {"name": "benchmark",
             "selectors": ["tests/test_b.py", "tests/test_b.py::test_two"]},
            {"name": "pytest", "selectors": ["*complement*"]},
        ]},
    )
    plan = mod.plan_execution(doc)
    assert plan.legs["benchmark"] == ("tests/test_b.py::test_two",)


def test_plan_execution_refuses_a_node_two_legs_both_claim():
    """#648 D7 -- the shares must be pairwise DISJOINT, so no node runs twice.

    The complement leg cannot produce this, because it is defined as what no
    other leg claimed. Two selector-carrying legs can, and this is the branch
    that refuses them.
    """
    mod = _load()
    doc = _artifact(
        pytestNodes=["tests/test_a.py::test_one", "tests/test_b.py::test_two"],
        pytestExecution={"legs": [
            {"name": "benchmark", "selectors": ["tests/test_b.py"]},
            {"name": "contention", "selectors": ["tests/test_b.py::test_two"]},
            {"name": "pytest", "selectors": ["*complement*"]},
        ]},
    )
    with pytest.raises(mod.EstateError) as excinfo:
        mod.plan_execution(doc)
    assert "tests/test_b.py::test_two" in str(excinfo.value)
    assert "contention" in str(excinfo.value)


def test_run_check_reports_both_directions_on_every_axis(tmp_path):
    """#648 D5 -- six directional tags, one pair per axis."""
    mod = _load()
    doc = _artifact()
    repo = _seed_repo(tmp_path, artifact=doc)
    live = {
        "pytestNodes": ["tests/test_a.py::test_other"],
        "frontendTests": [{"runner": "vitest", "id": "b.test.ts > y",
                           "expectedStatus": None}],
        "suppressions": [{"key": "tests/test_b.py|<module>|skip_call|", "count": 1}],
    }
    report = mod.run_check(repo, "public", discover=lambda _root: live)
    tags = sorted({(f.code_tag, f.axis) for f in report.findings})
    assert tags == [
        ("missing", "frontendTests"), ("missing", "pytestNodes"),
        ("missing", "suppressions"),
        ("unexpected", "frontendTests"), ("unexpected", "pytestNodes"),
        ("unexpected", "suppressions"),
    ]
    assert not report.inabilities


def test_run_check_records_a_failed_derivation_as_an_inability(tmp_path):
    """#648 D9 -- a derivation that cannot be completed is never an empty one."""
    mod = _load()
    repo = _seed_repo(tmp_path, artifact=_artifact())

    def _boom(_root):
        raise RuntimeError("vitest is not installed")

    report = mod.run_check(repo, "public", discover=_boom)
    assert [tag for tag, _ in report.inabilities] == ["discovery-failed"]
    assert not report.findings


def test_run_check_refuses_a_private_profile_with_no_overlay(tmp_path):
    """#648 acceptance criterion 16, second clause."""
    mod = _load()
    repo = _seed_repo(tmp_path, artifact=_artifact())
    report = mod.run_check(repo, "private", discover=lambda _root: mod.live_axes(_artifact()))
    assert [tag for tag, _ in report.inabilities] == ["partition-invalid"]


def test_run_check_refuses_an_overlay_on_a_public_profile_tree(tmp_path):
    """#648 acceptance criterion 16, third clause."""
    mod = _load()
    doc = _artifact()
    overlay = {
        "schemaVersion": 1,
        "publicDigest": mod.active_set_digest(doc),
        "allowlistDigest": "x" * 64,
        "pytestNodes": {"additions": [], "removals": []},
        "frontendTests": {"additions": [], "removals": []},
        "suppressions": {"additions": [], "removals": []},
    }
    repo = _seed_repo(tmp_path, artifact=doc, overlay=overlay)
    report = mod.run_check(repo, "public", discover=lambda _root: mod.live_axes(doc))
    assert [tag for tag, _ in report.inabilities] == ["partition-invalid"]


def test_run_check_reports_an_unreadable_predecessor_rather_than_no_predecessor(tmp_path):
    """#648 D9 -- an unusable repository must not silently disable the check."""
    mod = _load()
    doc = _artifact()
    root = tmp_path / "loose"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "authoritative-estate.json").write_text(
        json.dumps(doc), encoding="utf-8")
    report = mod.run_check(root, "public", discover=lambda _r: mod.live_axes(doc))
    assert "artifact-unreadable" in [tag for tag, _ in report.inabilities]


def test_run_check_reports_an_uncovered_shrink_against_the_committed_parent(tmp_path):
    """#648 D3 -- the gate enforces authorization against committed history."""
    mod = _load()
    doc = _artifact(pytestNodes=["tests/test_a.py::test_one",
                                 "tests/test_a.py::test_two"])
    repo = _seed_repo(tmp_path, artifact=doc)
    shrunk = _artifact(pytestNodes=["tests/test_a.py::test_one"])
    (repo / "tests" / "authoritative-estate.json").write_text(
        json.dumps(shrunk), encoding="utf-8")
    report = mod.run_check(repo, "public",
                           discover=lambda _r: mod.live_axes(shrunk))
    assert [(u.axis, u.token) for u in report.uncovered] == [
        ("pytestNodes", "tests/test_a.py::test_two")]
    assert report.uncovered[0].profile == "public"


def test_run_check_accepts_a_shrink_the_trees_ledger_authorizes(tmp_path):
    """The declaration lands in the SAME tree as the shrink it authorizes.

    Reading the predecessor's ledger instead would make a working-tree shrink
    unauthorizable: the entry that covers it cannot be in a commit that predates
    it.
    """
    mod = _load()
    doc = _artifact(pytestNodes=["tests/test_a.py::test_one",
                                 "tests/test_a.py::test_two"])
    repo = _seed_repo(tmp_path, artifact=doc)
    shrunk = _artifact(pytestNodes=["tests/test_a.py::test_one"])
    (repo / "tests" / "authoritative-estate.json").write_text(
        json.dumps(shrunk), encoding="utf-8")
    (repo / "tests" / "authoritative-estate-retirements.json").write_text(
        json.dumps({"schemaVersion": 1, "declarations": [{
            "id": "R-9001", "axis": "pytestNodes", "profile": "public",
            "cause": "retired", "reason": "deleted with its subject",
            "rows": ["tests/test_a.py::test_two"],
            "predecessorDigest": mod.active_set_digest(doc),
        }]}), encoding="utf-8")
    report = mod.run_check(repo, "public",
                           discover=lambda _r: mod.live_axes(shrunk))
    assert not report.uncovered
    assert not report.inabilities


def test_run_check_validates_the_public_profile_on_a_private_tree(tmp_path):
    """#648 D3 -- BOTH profiles, not only the active one.

    A row dropped from the PUBLIC artifact and added back by the overlay leaves
    the private profile identical, so a private tree that validated only its own
    profile would pass and the loss would surface later in the mirror's own CI.
    """
    mod = _load()
    public_before = _artifact(pytestNodes=["tests/test_a.py::test_one",
                                           "tests/test_a.py::test_two"])
    overlay_before = {
        "schemaVersion": 1,
        "publicDigest": mod.active_set_digest(public_before),
        "allowlistDigest": "x" * 64,
        "pytestNodes": {"additions": [], "removals": []},
        "frontendTests": {"additions": [], "removals": []},
        "suppressions": {"additions": [], "removals": []},
    }
    repo = _seed_repo(tmp_path, artifact=public_before, overlay=overlay_before)
    public_after = _artifact(pytestNodes=["tests/test_a.py::test_one"])
    overlay_after = dict(overlay_before)
    overlay_after["publicDigest"] = mod.active_set_digest(public_after)
    overlay_after["pytestNodes"] = {"additions": ["tests/test_a.py::test_two"],
                                    "removals": []}
    (repo / "tests" / "authoritative-estate.json").write_text(
        json.dumps(public_after), encoding="utf-8")
    (repo / "tests" / "authoritative-estate.private.json").write_text(
        json.dumps(overlay_after), encoding="utf-8")
    private_after = mod.compose_private(public_after, overlay_after)
    report = mod.run_check(repo, "private",
                           discover=lambda _r: mod.live_axes(private_after))
    assert [(u.profile, u.axis, u.token) for u in report.uncovered] == [
        ("public", "pytestNodes", "tests/test_a.py::test_two")]


def test_the_report_renders_one_machine_readable_line_per_record(tmp_path):
    """The shell owns the verdict, so the helper's output must be parseable.

    Bash 3.2 reads it with `IFS=$'\\t' read`, so every field is tab-separated
    and no field may carry a tab or a newline of its own.
    """
    mod = _load()
    repo = _seed_repo(tmp_path, artifact=_artifact())
    live = {"pytestNodes": ["tests/test_a.py::test_other"],
            "frontendTests": [], "suppressions": []}
    report = mod.run_check(repo, "public", discover=lambda _r: live)
    lines = mod.render_report(report).splitlines()
    assert lines[0] == "version\t1"
    assert "profile\tpublic" in lines
    assert lines[-1] == "end\tproblems"
    findings = [ln for ln in lines if ln.startswith("finding\t")]
    assert any(ln.startswith("finding\tunexpected\tpytestNodes\t") for ln in findings)
    for line in lines:
        assert "\n" not in line
        assert len(line.split("\t")) >= 2


def test_the_report_ends_ok_when_the_record_and_the_tree_agree(tmp_path):
    mod = _load()
    doc = _artifact()
    repo = _seed_repo(tmp_path, artifact=doc)
    report = mod.run_check(repo, "public", discover=lambda _r: mod.live_axes(doc))
    assert mod.render_report(report).splitlines()[-1] == "end\tok"


# ---------------------------------------------------------------------------
# The command line
#
# `bin/_lib-test-contract.sh` invokes this module as a SUBPROCESS in all three
# modes, so `main` and `build_parser` are production surface rather than a
# convenience wrapper. Nothing called either until these cases, which left the
# usage branch and the OSError arm reached by no test at all.
# ---------------------------------------------------------------------------


def _two_leg_artifact():
    """An artifact whose complement leg owns a node, so a plan can be built.

    The module default records ONE node and gives it to the benchmark leg, so
    `plan_execution` correctly refuses it: every declared leg is required and
    the complement would run nothing.
    """
    return _artifact(pytestNodes=["tests/test_a.py::test_one",
                                  "tests/test_b.py::test_two"])


def test_the_parser_requires_a_profile_and_offers_exactly_three_modes():
    """The profile is the CALLER's resolution and is never inferred here."""
    mod = _load()
    parser = mod.build_parser()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["--check"])
    assert excinfo.value.code == 2
    args = parser.parse_args(["--profile", "public", "--check"])
    assert args.check and args.plan_legs is None and not args.validate_legs
    assert parser.parse_args(["--profile", "public", "--plan-legs", "d"]).plan_legs == "d"
    assert parser.parse_args(["--profile", "public", "--validate-legs"]).validate_legs
    with pytest.raises(SystemExit):
        parser.parse_args(["--profile", "neither-one"])


def test_an_invocation_that_names_no_mode_exits_two_and_reads_nothing(tmp_path,
                                                                     capsys):
    """Exit 2 is USAGE, and it must not depend on the tree being usable.

    The repository here carries no artifact at all, so a mode check that ran
    after the artifact was loaded would report 3 and mislabel a caller's
    mistake as an inability of the tree.
    """
    mod = _load()
    empty = tmp_path / "empty"
    empty.mkdir()
    assert mod.main(["--repo", str(empty), "--profile", "private"]) == 2
    captured = capsys.readouterr()
    assert "--check" in captured.err
    assert captured.out == ""


def test_an_unusable_artifact_exits_three_rather_than_raising(tmp_path, capsys):
    """`--plan-legs` reads the record before it writes anything.

    `--check` reports an unreadable artifact INSIDE its report, because the
    contract shell maps that to `estate-artifact-unreadable`. The plan modes
    have no report to put it in, so they exit 3 and the shell fails closed on
    the exit status instead.
    """
    mod = _load()
    empty = tmp_path / "empty"
    empty.mkdir()
    out = tmp_path / "legs"
    assert mod.main(["--repo", str(empty), "--profile", "public",
                     "--plan-legs", str(out)]) == 3
    assert "_lib_test_estate.py:" in capsys.readouterr().err
    assert not out.exists()


def test_an_os_error_exits_three_rather_than_escaping_as_a_traceback(tmp_path,
                                                                    capsys):
    """The OSError arm, reached by an output directory that is a FILE.

    A traceback here would reach the contract shell as a non-zero exit with no
    parseable report, which it already fails closed on -- but the diagnostic
    the operator reads would be a stack trace rather than the one line naming
    the path.
    """
    mod = _load()
    repo = _seed_repo(tmp_path, artifact=_two_leg_artifact())
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("", encoding="utf-8")
    assert mod.main(["--repo", str(repo), "--profile", "public",
                     "--plan-legs", str(blocked)]) == 3
    assert "_lib_test_estate.py:" in capsys.readouterr().err


def test_validate_legs_exits_zero_prints_the_plan_and_writes_nothing(tmp_path,
                                                                    capsys):
    """The admission-time form. It has nowhere to write and needs nowhere."""
    mod = _load()
    repo = _seed_repo(tmp_path, artifact=_two_leg_artifact())
    before = sorted(path.name for path in repo.iterdir())
    assert mod.main(["--repo", str(repo), "--profile", "public",
                     "--validate-legs"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[-1] == "end\tok"
    assert [line for line in lines if line.startswith("leg\t")]
    for line in lines:
        if line.startswith("leg\t"):
            assert line.split("\t")[3] == "", line
    assert sorted(path.name for path in repo.iterdir()) == before


# ---------------------------------------------------------------------------
# Loading the discovery kernel
# ---------------------------------------------------------------------------


def test_the_kernel_loader_registers_the_module_it_executes():
    """#648 review P3-7 -- `setdefault` registered one object and ran another.

    `sys.modules.setdefault` returns any PRE-EXISTING entry, which the loader
    discarded, and then executed into the new object regardless, so the name
    stayed bound to a module no caller ever received. The kernel combines
    `from __future__ import annotations` with `@dataclass`, and `dataclasses`
    resolves each field's annotation through `sys.modules[cls.__module__]`
    while the class body executes, so the binding is a live input to the load
    rather than a tidiness point.
    """
    mod = _load()
    previous = sys.modules.get("_lib_estate_discovery")
    sentinel = types.ModuleType("_lib_estate_discovery")
    sys.modules["_lib_estate_discovery"] = sentinel
    try:
        loaded = mod._load_discovery_kernel(ROOT)
        assert loaded is not sentinel
        assert sys.modules["_lib_estate_discovery"] is loaded
        assert hasattr(loaded, "discover")
    finally:
        if previous is None:
            sys.modules.pop("_lib_estate_discovery", None)
        else:
            sys.modules["_lib_estate_discovery"] = previous


def test_a_kernel_that_fails_to_execute_restores_the_previous_binding(tmp_path):
    """The loader OVERWRITES the name, so it must put back what it displaced.

    Overwriting is deliberate: the kernel this loader must return is the one
    belonging to the repository under test, and returning a cached module would
    hand a caller checking one tree the kernel of another. The cost of that
    choice is this obligation, and a failed load must leave the interpreter as
    it found it.
    """
    mod = _load()
    broken = tmp_path / "repo" / "bin"
    broken.mkdir(parents=True)
    (broken / "_lib_estate_discovery.py").write_text(
        "raise RuntimeError('this kernel does not import')\n", encoding="utf-8")
    previous = sys.modules.get("_lib_estate_discovery")
    sentinel = types.ModuleType("_lib_estate_discovery")
    sys.modules["_lib_estate_discovery"] = sentinel
    try:
        with pytest.raises(RuntimeError):
            mod._load_discovery_kernel(tmp_path / "repo")
        assert sys.modules["_lib_estate_discovery"] is sentinel
    finally:
        if previous is None:
            sys.modules.pop("_lib_estate_discovery", None)
        else:
            sys.modules["_lib_estate_discovery"] = previous
