"""#661 S2 Task D1 — the ONE cause-code copy table (spec section 8).

S1 publishes fifteen withheld-figure codes in `EVIDENCE_CODES`. Section 8
renders them through one table in two registers, and this module is that
table.

**Short register.** A fixed-width slot — the status line, a panel chip, a
table cell — where a sentence does not fit. It is DERIVED from the code by
`derive_short_from_code`, a pure function of the string, and `short_form` is
that derivation. Deriving rather than curating is the point: a client that
receives no presentation fields must still render something, and a
derivation it can reimplement in four lines cannot fall out of step with a
table it never sees.

**Long register.** A full sentence, for a modal, a footer or a `--help`-style
report. This is what a client cannot derive and what the wire's presentation
fields exist to carry.

**The wire keeps the machine code.** `code` is unchanged and stays the stable
machine-readable value; the presentation fields sit ALONGSIDE it. `--json`
keeps emitting the code, because full sentences do not replace machine codes
there.

The degraded path is therefore: no presentation fields on the wire, so the
client shows the derived short token and no sentence. Never blank, which
matters because a dashboard tab can outlive a server restart through
`execvp` and will meet an older server's envelope.

Pure: no I/O, no clock, no store. The vocabulary it covers is the closed
15-member `EVIDENCE_CODES` union, and `tests/test_quota_copy_table.py`
asserts both registers exist for every member — a code added to the kernel
without copy fails there rather than rendering as a bare token in a modal.
"""
from __future__ import annotations

#: Full sentences, keyed by the machine code. Curated, because a sentence is
#: exactly what a client cannot derive. A code missing from this table falls
#: back to its short form, so the long register degrades to the short one
#: rather than to nothing — but the test above means that never ships.
_LONG_FORM: dict = {
    # --- WithholdingCause: why one daily observation carries no value ---
    "no-local-history":
        "no local entries cover this day, so there is nothing to model from",
    "sparse-local-history":
        "this day holds some local usage, but below the fence the fit needs",
    "transition-day":
        "this day sits on a metering-rate boundary, so its usage was priced "
        "under two different rates",
    "right-censored":
        "the meter reached its ceiling, so the real consumption is only "
        "known to be at least the reading",
    "token-split-unknown":
        "the token split for part of this window is unknown, so the "
        "weighted units cannot be computed",
    "unsupported-composition":
        "this window's model and token mix sits outside the calibration's "
        "support",
    # --- CalibrationStatus: the state of one account's calibration ---
    "insufficient-history":
        "not enough local history has accumulated to fit a rate yet",
    "fragmented-history":
        "the local history has too many gaps to fit a rate across",
    "unstable-fit":
        "the fit did not settle on one rate, so no single value describes "
        "this account",
    "local-history-incomplete":
        "the local entry history does not cover the whole window being "
        "modelled",
    "unsupported-model-mix":
        "the models in use sit outside the mix the calibration was fitted "
        "over",
    "unvalidated-coefficient-era":
        "this window predates the token weights the model is validated for",
    "stale":
        "the stored calibration was fitted under different constants, so it "
        "no longer describes this build",
    "future":
        "the stored calibration came from a newer cctally than this one",
    "unavailable":
        "no usable calibration could be read for this account",
}


def derive_short_from_code(code) -> str:
    """The short token, derived from the code alone.

    The whole derivation: the hyphens become spaces. It is written out as a
    named function rather than inlined because a client that receives no
    presentation fields reimplements exactly this, and a rule stated in one
    place is a rule two implementations can be checked against.

    Never returns an empty string for a non-empty code, which is the property
    the degraded path depends on.
    """
    text = str(code or "").strip()
    if not text:
        return ""
    return text.replace("-", " ")


def short_form(code) -> str:
    """The fixed-width register. Identical to `derive_short_from_code`.

    Two names for one function, deliberately: `short_form` is what a renderer
    asks for, and `derive_short_from_code` is what a client without the wire
    fields reimplements. Keeping them the same function is what makes the
    degraded path a degradation in the SENTENCE and not in the token.
    """
    return derive_short_from_code(code)


def long_form(code) -> str:
    """The sentence register, or the short token when no sentence exists."""
    text = str(code or "").strip()
    if not text:
        return ""
    return _LONG_FORM.get(text) or derive_short_from_code(text)


def presentation(code) -> dict:
    """The three fields a wire payload carries for one code.

    `code` is the stable machine value and is repeated here so a consumer
    holding only this dict still has it. The two rendering fields are
    additive: an older server sends neither and the client derives the short
    one.
    """
    text = str(code or "").strip()
    if not text:
        return {}
    return {"code": text, "short": short_form(text), "long": long_form(text)}
