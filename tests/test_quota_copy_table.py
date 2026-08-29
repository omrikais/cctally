"""#661 S2 Task D1 — the single cause-code copy table (spec section 8).

Fifteen codes, two registers, one table. What these tests own is that the
table covers the whole closed union, that the short register is derivable
from the code alone so a client with no presentation fields renders a token
rather than a blank, and that `--json` keeps emitting the machine code
alongside the new rendering fields rather than in place of it.
"""
from __future__ import annotations

import pytest

from conftest import load_script


def _copy(ns):
    return ns["_load_sibling"]("_lib_quota_copy")


def _qm(ns):
    return ns["_load_sibling"]("_lib_quota_model")


def test_d1_every_evidence_code_has_both_registers():
    ns = load_script()
    copy = _copy(ns)
    qm = _qm(ns)
    assert len(qm.EVIDENCE_CODES) == 15, sorted(qm.EVIDENCE_CODES)
    for code in qm.EVIDENCE_CODES:
        assert copy.short_form(code), code
        assert copy.long_form(code), code


def test_d1_the_long_register_is_a_sentence_and_not_the_token_again():
    """The non-vacuity twin of the test above. `long_form` falls back to the
    short token when a code has no sentence, so a table that covered nothing
    would still pass a bare truthiness check."""
    ns = load_script()
    copy = _copy(ns)
    qm = _qm(ns)
    for code in qm.EVIDENCE_CODES:
        assert copy.long_form(code) != copy.short_form(code), (
            f"{code} has no curated sentence, so its long register is the "
            "short token repeated")
        assert " " in copy.long_form(code), code


def test_d1_short_form_is_derivable_without_the_table():
    """A new client meeting an OLD server gets no presentation fields and
    must still render something. The fallback is one named function, and it
    is the same function the server uses, so the token cannot disagree —
    only the sentence is lost."""
    ns = load_script()
    copy = _copy(ns)
    qm = _qm(ns)
    for code in qm.EVIDENCE_CODES:
        assert copy.derive_short_from_code(code) == copy.short_form(code)


def test_d1_the_derivation_never_returns_a_blank_for_a_real_code():
    """The property the degraded path depends on, asserted over the union
    and over a code this build has never heard of."""
    ns = load_script()
    copy = _copy(ns)
    qm = _qm(ns)
    for code in qm.EVIDENCE_CODES:
        assert copy.derive_short_from_code(code).strip()
    assert copy.derive_short_from_code("a-code-from-the-future") == (
        "a code from the future")


def test_d1_an_empty_or_absent_code_renders_nothing_rather_than_a_dash():
    """`None` is not a cause, so it must not become a token that looks like
    one. The renderer decides what to show for an absent cause; this table
    does not invent one."""
    ns = load_script()
    copy = _copy(ns)
    assert copy.short_form(None) == ""
    assert copy.long_form("") == ""
    assert copy.presentation(None) == {}


def test_d1_presentation_carries_the_code_beside_the_renderings():
    ns = load_script()
    copy = _copy(ns)
    out = copy.presentation("insufficient-history")
    assert out["code"] == "insufficient-history"
    assert out["short"] == "insufficient history"
    assert out["long"] != out["short"]
    assert set(out) == {"code", "short", "long"}


def test_d1_json_keeps_the_machine_code():
    """Section 8: the wire keeps the machine code and ADDS presentation
    fields. Full sentences do not replace machine codes in `--json`."""
    ns = load_script()
    glue = ns["_load_sibling"]("_cctally_quota_model")
    qm = _qm(ns)
    withheld = qm.evidence_withheld("insufficient-history", {"days": 0})
    payload = glue._evidence_json(withheld)
    assert payload["code"] == "insufficient-history"
    assert payload["state"] == "withheld"
    assert payload["value"] is None


@pytest.mark.parametrize("code", ["right-censored", "unavailable",
                                  "unsupported-model-mix"])
def test_d1_the_sentence_never_states_a_number_or_a_command(code):
    """The registers render a CAUSE. A remediation or a figure belongs to
    the surface that has the context to state it, and putting either here
    would make one table answer for every caller's layout."""
    ns = load_script()
    text = _copy(ns).long_form(code)
    assert "cctally " not in text, text
    assert "%" not in text, text
