"""One instance of every suppression form the scanner must see. Fixture only.

This file is DATA, not a test module. It lives under ``tests/fixtures/``, which
pytest does not collect, and its name does not match ``test_*.py`` either, so
nothing here ever runs. ``tests/test_estate_discovery.py`` points the scanner at
this directory and asserts the scanner reports every form below.

Keep exactly one instance of each form. A second instance would still be
counted correctly, but the corpus reads as an enumeration of the class and a
duplicate makes it read as a sample of the tree instead. Two forms below are
deliberate pairs rather than single instances, because the property under test
is a DIFFERENCE: a helper application is written both as a call and as a ``with``
statement, and a parameter-gated helper has one call site that can reach its
skip and one that cannot.
"""
import contextlib

import pytest
from pytest import skip as aliased_skip

pytestmark = pytest.mark.skipif(False, reason="module scope")

_shared_mark = pytest.mark.skipif(True, reason="shared mark object")

pytest.importorskip("json")

# The assigned form of the same call. ``mod = pytest.importorskip("x")`` is the
# common idiom for that API, and both derivations that read it — the suppression
# scan and `declared_collection_dependencies` — must agree that it is one.
_json_module = pytest.importorskip("json")


def _helper_guard():
    pytest.skip("helper implementation — must NOT be counted here")


@contextlib.contextmanager
def _helper_context():
    """A helper applied as a context manager rather than as a plain call.

    Recognizing the helper while missing this application form is strictly worse
    than not recognizing it at all: excluding the helper's body then DELETES the
    suppression from the axis instead of relocating it to the call site.
    """
    if False:
        pytest.skip("context-manager helper implementation — not counted here")
    yield


def _helper_parameter_gated(strict=False):
    """A helper whose only skip sits under one of its OWN parameters.

    Every path to its skip passes a parameter guard, so it is parameter-gated
    and a call site counts only when the argument it supplies can reach the
    skip. ``_estate`` in ``tests/test_authoritative_test_contract.py`` is the
    live instance: 87 call sites, of which exactly two pass ``private=True``.
    """
    if strict:
        pytest.skip("only a caller that asks for strict can be suppressed")


def test_direct_call():
    if True:
        pytest.skip("direct call")


def test_aliased_call():
    aliased_skip("aliased module import")


@pytest.mark.skip(reason="decorator skip")
def test_mark_skip():
    pass


@pytest.mark.skipif(True, reason="decorator skipif")
def test_mark_skipif():
    pass


@_shared_mark
def test_shared_mark():
    pass


@pytest.mark.parametrize("v", [pytest.param(1, marks=_shared_mark), 2])
def test_param_mark(v):
    pass


def test_helper_application():
    _helper_guard()


def test_helper_context_application():
    with _helper_context():
        pass


def test_parameter_gate_satisfied():
    _helper_parameter_gated(strict=True)


def test_parameter_gate_not_satisfied():
    _helper_parameter_gated()


class TestClassScopedSuppression:
    """``pytestmark`` in a class body suppresses every test in the class.

    It is supported pytest with zero instances in this tree today, which is
    exactly the argument the spec makes for covering the module-level form: a
    scan built from present examples omits it, and one such assignment could
    switch off a whole class invisibly.
    """

    pytestmark = pytest.mark.skipif(True, reason="class scope")

    def test_in_the_class(self):
        pass
