"""One vocabulary for the projection basis, across Python and TypeScript.

#661 S2 remediation, finding E7. Spec §9 says the surface states which
measurement produced the projection "in the short register", and the status
line prints `model` / `meter` while `ForecastPanel`'s `BASIS_LABEL` maps the
same two wire values to the same two words. Neither is a member of
`EVIDENCE_CODES` and neither has a `_lib_quota_copy` entry, which the review
read as a possible fourth vocabulary.

The reading taken, and recorded beside the table itself, is that §8's "short
register" names a REGISTER rather than a closed set of cause codes: a basis is
not a withholding cause, so it cannot be a member of that union. What the
concern gets right is that the two words are spelled twice, once per language,
with nothing holding them together. This module is that hold.

It compares the two tables directly rather than asserting each against a
literal, because two literals agreeing with a third say nothing about whether
they agree with each other — but it also pins the words, because two tables
that had drifted TOGETHER would satisfy a comparison alone.
"""
from __future__ import annotations

import pathlib
import re

import pytest

from conftest import load_script

ROOT = pathlib.Path(__file__).resolve().parents[1]
PANEL_TS = ROOT / "dashboard" / "web" / "src" / "panels" / "ForecastPanel.tsx"
MODAL_TS = ROOT / "dashboard" / "web" / "src" / "modals" / "ForecastModal.tsx"


def _python_table():
    ns = load_script()
    return dict(ns["_load_sibling"]("_lib_statusline").BASIS_SHORT_FORM)


def _typescript_table() -> dict:
    """Parse `BASIS_LABEL`'s object literal out of the panel source."""
    source = PANEL_TS.read_text(encoding="utf-8")
    match = re.search(
        r"const BASIS_LABEL:\s*Record<string,\s*string>\s*=\s*\{(.*?)\}",
        source, re.S)
    assert match, "BASIS_LABEL was renamed or reshaped in ForecastPanel.tsx"
    body = match.group(1)
    pairs = re.findall(r"'?([A-Za-z-]+)'?\s*:\s*'([^']+)'", body)
    assert pairs, f"no entries parsed from BASIS_LABEL: {body!r}"
    return dict(pairs)


def test_the_two_tables_carry_the_same_words_for_the_same_bases():
    assert _python_table() == _typescript_table()


def test_the_words_are_the_ones_the_spec_and_the_docs_state():
    """Non-vacuity for the comparison above: two tables that drifted together
    would agree with each other and be wrong."""
    assert _python_table() == {
        "calibrated": "model",
        "corrected-meter": "meter",
    }


def test_every_selectable_basis_has_a_short_form():
    """`withheld` is deliberately absent: that state renders the withholding
    CAUSE through the copy table, not a basis word, on both surfaces."""
    ns = load_script()
    fc = ns["_load_sibling"]("_lib_forecast")
    selectable = {b.value for b in fc.ProjectionBasis
                  if b is not fc.ProjectionBasis.WITHHELD}
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
    """§8's other register, on the surface that has room for it. This is a
    text tripwire, so it states what it can: the modal spells the long forms
    and does not import the panel's short table."""
    source = MODAL_TS.read_text(encoding="utf-8")
    assert "'calibrated model'" in source
    assert "'corrected meter'" in source
    assert "BASIS_LABEL" not in source
