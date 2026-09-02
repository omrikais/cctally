"""One server-owned vocabulary for projection-basis presentation (#676).

The shared Python table publishes both registers. The status line consumes its
short register directly; the dashboard envelope carries both registers to the
React panel and modal. No TypeScript table is parsed here because the client
behavior is exercised by its own component tests.
"""
from __future__ import annotations

import pytest

from conftest import load_script

def _python_table():
    ns = load_script()
    copy = ns["_load_sibling"]("_lib_quota_copy")
    return {basis: dict(forms) for basis, forms in copy._BASIS_FORM.items()}


def test_the_two_tables_carry_the_same_words_for_the_same_bases(monkeypatch):
    """Historical node name retained: the real contract is now that the two
    Python producers observe the same shared table mutation."""
    ns = load_script()
    copy = ns["_load_sibling"]("_lib_quota_copy")
    monkeypatch.setitem(
        copy._BASIS_FORM["corrected-meter"], "short", "gauge")
    monkeypatch.setitem(
        copy._BASIS_FORM["corrected-meter"], "long", "corrected gauge")

    statusline = ns["_load_sibling"]("_lib_statusline")
    assert statusline._seven_day_projection(
        40.0, 96 * 3600, 0) == "→ 92% gauge"

    envelope = ns["_load_sibling"]("_cctally_dashboard_envelope")
    assert envelope._basis_presentation("corrected-meter") == {
        "code": "corrected-meter",
        "short": "gauge",
        "long": "corrected gauge",
    }


def test_the_words_are_the_ones_the_spec_and_the_docs_state():
    """Non-vacuity for the comparison above: two tables that drifted together
    would agree with each other and be wrong."""
    assert _python_table() == {
        "calibrated": {"short": "model", "long": "calibrated model"},
        "corrected-meter": {"short": "meter", "long": "corrected meter"},
        "withheld": {"short": "withheld", "long": "withheld"},
    }


def test_every_selectable_basis_has_a_short_form():
    """Every enum member has presentation even though a withheld projection
    renders its separate cause in the panel's value slot."""
    ns = load_script()
    fc = ns["_load_sibling"]("_lib_forecast")
    selectable = {b.value for b in fc.ProjectionBasis}
    assert set(_python_table()) == selectable


@pytest.mark.parametrize("word", ["model", "meter"])
def test_the_short_words_are_not_smuggled_into_the_cause_vocabulary(word):
    """The decision, pinned. A basis is not a withholding cause, so adding it
    to `EVIDENCE_CODES` or to the copy table would make one union answer two
    different questions."""
    ns = load_script()
    qm = ns["_load_sibling"]("_lib_quota_model")
    copy = ns["_load_sibling"]("_lib_quota_copy")
    assert word not in qm.EVIDENCE_CODES
    assert word not in copy._LONG_FORM


def test_the_modal_uses_the_long_register_rather_than_a_third_vocabulary():
    """The dashboard wire carries the long register that its modal consumes.
    React behavior is asserted in ForecastModal.quota.test.tsx."""
    ns = load_script()
    envelope = ns["_load_sibling"]("_cctally_dashboard_envelope")
    assert envelope._basis_presentation("calibrated")["long"] == (
        "calibrated model")
    assert envelope._basis_presentation("corrected-meter")["long"] == (
        "corrected meter")
