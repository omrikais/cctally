"""Boundary self-tests for `tests/_owner_expectations.py`.

The helper narrows an exact comparison per entry by DECLARED visibility, and it
checks every private declaration against the tree's own mirror boundary. These
cases pin both halves: the filtering (unannotated is public, an unknown profile
and a blank owner refuse) and the validation (drift refuses, a private tree
without its classifier refuses, the tree's own allowlist is what decides).

They were carried by `tests/test_estate_manifest.py` until the test regime
reset deleted that module with the estate helper they then lived in.
"""
import os
import sys

import pytest

import _owner_expectations as mod


#: A stand-in for `.githooks/_match.py` written into a synthetic tree.
#:
#: It classifies a path as public exactly when the path appears as a
#: whitespace-separated token in the allowlist TEXT it was handed. That is not
#: the real matching grammar and is not meant to be: the point is that the
#: helper has to read the tree's own `.mirror-allowlist` and hand it to the
#: tree's own classifier, so a helper that reimplemented matching, or that read
#: neither file, produces a different answer and fails these cases.
#:
#: The real `.githooks/_match.py` is mirror-private, so a public test module
#: cannot copy it: on the public projection there is nothing to copy. A stub
#: keeps every case below runnable in both profiles with no skip.
_MATCH_STUB = '''\
"""Synthetic allowlist classifier (test double)."""


def classify(paths, allowlist_path=".mirror-allowlist", *, allowlist_text=None):
    public = set((allowlist_text or "").split())
    return {
        "public": [p for p in paths if p in public],
        "private": [p for p in paths if p not in public],
        "unmatched": [],
    }
'''


def _visibility_tree(tmp_path, *, allowlist=None, matcher=True):
    """A synthetic repository root for the owner-path validation cases.

    ``allowlist`` is the text of `.mirror-allowlist`, or ``None`` for a tree
    that carries none — which is what a public projection looks like.
    """
    root = tmp_path / "repo"
    (root / ".githooks").mkdir(parents=True)
    if allowlist is not None:
        (root / mod.ALLOWLIST).write_text(allowlist, encoding="utf-8")
    if matcher:
        (root / ".githooks" / "_match.py").write_text(_MATCH_STUB, encoding="utf-8")
    return root


def test_an_unannotated_entry_is_expected_in_both_profiles():
    """Every entry is public unless it says otherwise.

    The default has to be the safe direction: a writer added later and left
    unannotated must still be expected on the public profile, so the public
    comparison fails closed on it.
    """
    entries = {"a": 1, "b": 2}
    for profile in mod.PROFILES:
        assert mod.applicable_expectations(entries, profile=profile) == {"a": 1, "b": 2}


def test_a_private_entry_is_expected_only_on_the_private_profile():
    entries = {
        "a": 1,
        "b": mod.private_expectation(2, "bin/cctally-private-thing"),
    }
    assert mod.applicable_expectations(entries, profile=mod.PRIVATE) == {"a": 1, "b": 2}
    assert mod.applicable_expectations(entries, profile=mod.PUBLIC) == {"a": 1}


def test_filtering_refuses_a_profile_it_does_not_know():
    with pytest.raises(mod.OwnerExpectationError):
        mod.applicable_expectations({"a": 1}, profile="maintainer")


@pytest.mark.parametrize("owner", ["", "   ", None, 3])
def test_a_declared_owner_path_must_be_a_non_empty_string(owner):
    with pytest.raises(mod.OwnerExpectationError):
        mod.private_expectation(1, owner)


def test_the_allowlist_presence_selects_the_active_profile(tmp_path):
    """`.mirror-allowlist` is the tree's own profile marker."""
    public_tree = _visibility_tree(tmp_path / "a", allowlist=None)
    assert mod.active_profile(public_tree) == mod.PUBLIC
    private_tree = _visibility_tree(tmp_path / "b", allowlist="tests/**\n")
    assert mod.active_profile(private_tree) == mod.PRIVATE


def test_a_declaration_the_classifier_calls_public_raises(tmp_path):
    """A row that claims to be private while the real boundary publishes it.

    That is a declaration drifting away from `.mirror-allowlist`, and it would
    silently narrow the public comparison to less than the public tree holds.
    """
    root = _visibility_tree(tmp_path, allowlist="bin/public-thing\n")
    entries = {"x": mod.private_expectation(1, "bin/public-thing")}
    with pytest.raises(mod.OwnerExpectationError) as caught:
        mod.validate_owner_paths(entries, repo=root)
    assert "bin/public-thing" in str(caught.value)


def test_a_declaration_the_classifier_calls_private_is_accepted(tmp_path):
    root = _visibility_tree(tmp_path, allowlist="bin/something-else\n")
    entries = {
        "x": mod.private_expectation(1, "bin/private-thing"),
        "y": 2,
    }
    assert mod.validate_owner_paths(entries, repo=root) is None


def test_owner_validation_is_skipped_on_a_public_projection(tmp_path):
    """The public clone carries neither the allowlist nor the classifier.

    Validation is what is skipped there, never the comparison itself: the
    declared visibility is consumed directly and the equality assertion still
    runs.
    """
    root = _visibility_tree(tmp_path, allowlist=None, matcher=False)
    entries = {"x": mod.private_expectation(1, "bin/anything-at-all")}
    assert mod.validate_owner_paths(entries, repo=root) is None


def test_a_private_tree_without_the_classifier_refuses(tmp_path):
    """A tree that claims to be private but cannot answer the question.

    It refuses rather than reporting a success it did not establish.
    """
    root = _visibility_tree(tmp_path, allowlist="bin/x\n", matcher=False)
    entries = {"x": mod.private_expectation(1, "bin/x")}
    with pytest.raises(mod.OwnerExpectationError) as caught:
        mod.validate_owner_paths(entries, repo=root)
    assert "_match.py" in str(caught.value)


def test_a_classifier_without_classify_refuses(tmp_path):
    root = _visibility_tree(tmp_path, allowlist="bin/x\n", matcher=False)
    (root / ".githooks" / "_match.py").write_text("VALUE = 1\n", encoding="utf-8")
    entries = {"x": mod.private_expectation(1, "bin/x")}
    with pytest.raises(mod.OwnerExpectationError) as caught:
        mod.validate_owner_paths(entries, repo=root)
    assert "classify()" in str(caught.value)


def test_validation_reads_the_trees_own_allowlist(tmp_path):
    """The helper never reimplements allowlist matching.

    The stub classifier answers only from the text it is handed, so a helper
    that classified on its own, or that never read `.mirror-allowlist`, cannot
    make this pair of trees disagree.
    """
    entries = {"x": mod.private_expectation(1, "bin/thing")}
    publishing = _visibility_tree(tmp_path / "a", allowlist="bin/thing\n")
    withholding = _visibility_tree(tmp_path / "b", allowlist="bin/other\n")
    with pytest.raises(mod.OwnerExpectationError):
        mod.validate_owner_paths(entries, repo=publishing)
    assert mod.validate_owner_paths(entries, repo=withholding) is None


def test_loading_the_classifier_leaves_sys_modules_alone(tmp_path):
    """Two trees in one interpreter must not share a cached `_match`.

    A registered module name would make the second tree's validation answer
    with the first tree's classifier.
    """
    before = sys.modules.get("_match")
    loader_name = "_owner_expectations_allowlist_matcher"
    before_loader = sys.modules.get(loader_name)
    cwd = os.getcwd()
    root = _visibility_tree(tmp_path, allowlist="bin/other\n")
    mod.validate_owner_paths({"x": mod.private_expectation(1, "bin/thing")}, repo=root)
    assert sys.modules.get("_match") is before
    # The helper's own loader name stays unregistered too, and validating
    # another tree never moves the working directory.
    assert sys.modules.get(loader_name) is before_loader
    assert os.getcwd() == cwd
