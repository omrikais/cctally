"""#887 — the provider-token matcher's contract (spec §5, test 1).

The S3 privacy tests search served text for short provider ids. This table
pins what the shared matcher strips (exactly the recognized per-run content),
what it still reports (a token anywhere else), and what the I4 tripwire
rejects (an unrecognized digest-shaped run), so a weakening or a widening of
any of them fails here first.
"""
from __future__ import annotations

import pytest

from tests._provider_token_text import (
    _G1_RE,
    _G2_RE,
    _G4_RE,
    _K_RE,
    assert_export_attribution,
    assert_token_carried,
    assert_tokens_absent,
    carries_token,
    normalize,
    per_run_literals,
    unrecognized_digest_runs,
)

TOKENS = ("70001", "70002", "70003", "70004")
KEY = "cbk1_" + "ab" * 20          # a recognized, token-free key


def key_with(token, family="cbk1"):
    """A recognized 40-hex key whose digest begins with `token`."""
    return f"{family}_" + token + "a" * (40 - len(token))


# ── carried / not carried ────────────────────────────────────────────────────

NOT_CARRIED = [
    ("token inside cbk1_ key", '{"k": "%s"}' % key_with("70001"), "70001", None),
    ("token inside civ1_ key", '"%s"' % key_with("70001", "civ1"), "70001", None),
    ("token inside cliv1_ key", '"%s"' % key_with("70001", "cliv1"), "70001", None),
    ("token inside per-run path", '"/tmp/pytest-70001/case0/provider"', "70001",
     "/tmp/pytest-70001/case0"),
    ("token inside encoded per-run path", '"-tmp-pytest-70001-case0-provider"', "70001",
     "/tmp/pytest-70001/case0"),
    ("dotted per-run path", '"/tmp/build.v1/pytest-70001/case0/provider"', "70001",
     "/tmp/build.v1/pytest-70001/case0"),
    ("dotted per-run path, product encoding", '"-tmp-build.v1-pytest-70001-case0-provider"',
     "70001", "/tmp/build.v1/pytest-70001/case0"),
    ("split fragments around a token-free key", "7000" + KEY + "1", "70001", None),
    ("separator: punctuation around a key", "ab." + KEY + ".cd", "..", None),
    ("separator: punctuation around a literal", "x/" + "/tmp/case0" + "/y", "//", "/tmp/case0"),
]

CARRIED = [
    ("JSON number", '{"session_id": 70001}', "70001", None),
    ("prose", "session 70001 resumed", "70001", None),
    ("after id", "id70001", "70001", None),
    ("after cafe", "cafe70001", "70001", None),
    ("after a key, punctuation", '"%s",70001' % KEY, "70001", None),
    ("before a key, punctuation", '70001,"%s"' % KEY, "70001", None),
    ("glued to a key's end", KEY + "70001", "70001", None),
    ("39-hex run after a prefix", "cbk1_70001" + "a" * 34, "70001", None),
    ("41-hex run after a prefix", "cbk1_70001" + "a" * 36, "70001", None),
    ("key glued after an identifier is not recognized", "id_" + key_with("70001"), "70001", None),
    ("key glued to a following identifier is not recognized", key_with("70001") + "g", "70001",
     None),
    ("inside an o1. value", "o1.70001" + "Q" * 38, "70001", None),
    ("inside a v1. value", "v1.70001" + "A" * 20, "70001", None),
    ("crossing a literal's end", "/tmp/case70001", "70001", "/tmp/case7000"),
    ("crossing an encoded literal's end", "-tmp-case70001", "70001", "/tmp/case7000"),
    ("adjacent to a literal", "/tmp/case7000 70001", "70001", "/tmp/case7000"),
    ("token-named descendant", "/tmp/case7000/70001", "70001", "/tmp/case7000"),
    ("glued to the username token", "test_s3_no_raw_session_id_reac070001", "70001",
     "/x/test_s3_no_raw_session_id_reac0"),
    ("adjacent to the username token", "test_s3_no_raw_session_id_reac0 70001", "70001",
     "/x/test_s3_no_raw_session_id_reac0"),
]


@pytest.mark.parametrize("label,text,token,home", NOT_CARRIED, ids=[c[0] for c in NOT_CARRIED])
def test_recognized_content_does_not_carry_the_token(label, text, token, home):
    per_run = per_run_literals(home) if home else ()
    assert not carries_token(text, token, per_run), normalize(text, per_run)


@pytest.mark.parametrize("label,text,token,home", CARRIED, ids=[c[0] for c in CARRIED])
def test_a_token_outside_recognized_content_is_carried(label, text, token, home):
    per_run = per_run_literals(home) if home else ()
    assert carries_token(text, token, per_run), normalize(text, per_run)


def test_the_separators_are_what_keep_fragments_apart():
    """Deleting a span without a separator would join its neighbours."""
    assert ".." in ("ab." + KEY + ".cd").replace(KEY, "")
    assert not carries_token("ab." + KEY + ".cd", "..")


def test_per_run_literals_use_the_product_encoding_and_the_username_token():
    lits = per_run_literals("/tmp/build.v1/pytest-70001/case0")
    assert "/tmp/build.v1/pytest-70001/case0" in lits
    assert "-tmp-build.v1-pytest-70001-case0" in lits      # only "/" is encoded
    assert "case0" in lits                                   # the HOME basename
    assert list(lits) == sorted(lits, key=lambda s: (-len(s), s))


def test_the_plan_username_token_is_clean_and_carries_nothing():
    per_run = per_run_literals("/x/test_s3_no_raw_session_id_reac0")
    fragment = ('{"text": "test_s3_no_raw_session_id_reac0", '
                '"replacement": "user", "bounded": true}')
    assert unrecognized_digest_runs(fragment, per_run) == []
    assert not any(carries_token(fragment, t, per_run) for t in TOKENS)


# ── the I4 tripwire ──────────────────────────────────────────────────────────

FLAGGED = [
    ("K: future version", "cbk2_" + "ab" * 20),
    ("K: declined key followed by g", KEY + "g"),
    ("K: declined key after an identifier", "id_" + KEY),
    ("K: bare o1.", '"o1.' + "A" * 43 + '"'),
    ("K: bare v1.", '"v1.' + "A" * 24 + '"'),
    ("K: upper-case prefix", "CBK1_" + "ab" * 20),
    ("K: glued o1., no token", "id_o1.80001" + "Q" * 38),
    ("K: glued o1., token", "id_o1.70001" + "Q" * 38),
    ("K: glued v1.", "x_v1." + "A" * 24),
    ("K: glued cbk2_", "id_cbk2_" + "A" * 30),
    ("K: ofc1.", "ofc1." + "A" * 43),
    ("K: glued ofc1., no token", "id_ofc1.80001"),
    ("K: glued ofc1., token", "id_ofc1.70001"),
    ("K: one-character body", "cbk1_a"),
    ("K: documented false-positive shape", "foo1.bar"),
    ("G2: 16 hex", "f" * 16),
    ("G2: glued hex key", "id_cbk2_" + "ab" * 24),
    ("G4: 10 digits", "1234567890"),
    ("G1: unknown family", '"zz9.abcdefgh"'),
    ("G3: mixed-case base64url", "aB3" + "x" * 13),
]

SHORT_BODIES = [
    "cbk1_a80001b", "cbk1_a70001b", "cbk1_80001", "cbk1_70001",
    "id_cbk1_a80001", "id_cbk1_a70001", "cbk2_a80001b", "cbk2_a70001b",
    "id_o1.80001", "id_o1.70001", "v9.a80001", "v9.a70001",
    "CBK1_a70001b", "ID_O1.70001",
]

NOT_FLAGGED = [
    ("15 hex", "f" * 15),
    ("9 digits", "123456789"),
    ("lowercase dashed path", "-synthetic-root-a-project-red"),
    ("dotted path", "/tmp/build.v1/pytest-70001/case0"),
    ("S3 test name", "test_s3_no_raw_session_id_reac0"),
    ("S3 test name inside a non-literal path", "/var/test_s3_no_raw_session_id_reac0/x"),
    ("bare prefix without a body", "cbk1_ x"),
    ("recognized key", KEY),
]


_DETECTORS = {"K": _K_RE, "G1": _G1_RE, "G2": _G2_RE, "G4": _G4_RE}


@pytest.mark.parametrize("label,text", FLAGGED, ids=[c[0] for c in FLAGGED])
def test_the_tripwire_flags_every_digest_shape(label, text):
    runs = unrecognized_digest_runs(text)
    assert runs, label
    # The row's NAMED detector must fire on its own, so a weakened detector
    # cannot hide behind another one (a hex-bodied K row would otherwise stay
    # green on G2 alone after K lost its case-insensitivity).
    detector = label.split(":")[0]
    if detector == "G3":
        assert text in runs, (label, runs)
    else:
        assert _DETECTORS[detector].search(normalize(text)), (label, runs)


@pytest.mark.parametrize("text", SHORT_BODIES)
def test_short_known_family_bodies_fail_with_the_guard_diagnostic(text):
    with pytest.raises(AssertionError, match=r"^unrecognized digest-shaped run at card: "):
        assert_tokens_absent(text, TOKENS, "card")


@pytest.mark.parametrize("label,text", NOT_FLAGGED, ids=[c[0] for c in NOT_FLAGGED])
def test_the_tripwire_ignores_legitimate_shapes(label, text):
    assert unrecognized_digest_runs(text) == [], label


def test_a_stripped_per_run_literal_is_not_flagged():
    per_run = per_run_literals("/tmp/case0")
    assert unrecognized_digest_runs("/tmp/case0/provider", per_run) == []


def test_the_approved_residual_is_outside_the_guarantee():
    """Spec §3: an unknown base64url family glued after an identifier
    character and lacking a letter case evades I4 (operator-approved).

    This pins the documented gap, not a desired behavior: if a tightened G1/G3
    makes it fail, the residual has CLOSED — update this test and the spec's
    residual statement together rather than restoring the gap."""
    assert unrecognized_digest_runs("id_zz9.80001" + "Q" * 38) == []


# ── exact messages ───────────────────────────────────────────────────────────

def test_assert_tokens_absent_messages():
    with pytest.raises(AssertionError, match=r"^provider token '70001' leaked at card$"):
        assert_tokens_absent(["clean", "x 70001"], TOKENS, "card")
    with pytest.raises(AssertionError, match=r"^unrecognized digest-shaped run at card: "):
        assert_tokens_absent("cbk2_" + "ab" * 20, TOKENS, "card")
    assert_tokens_absent(['"%s"' % key_with("70001")], TOKENS, "card")


def test_assert_token_carried_messages():
    with pytest.raises(AssertionError, match=r"^no provider token outside opaque keys at args$"):
        assert_token_carried(["nothing here", '"%s"' % key_with("70001")], TOKENS, "args")
    assert_token_carried(["nothing here", "id 70001"], TOKENS, "args")
    with pytest.raises(AssertionError, match=r"^no provider token outside opaque keys at payload_content$"):
        assert_token_carried(["70001", "nothing"], TOKENS, "payload_content", each=True)
    with pytest.raises(AssertionError, match=r"^no provider token outside opaque keys at args$"):
        assert_token_carried([], TOKENS, "args")


def test_assert_export_attribution_messages():
    raw = {"exec 70001"}
    assert_export_attribution("# Title\nexec 70001\n", raw, TOKENS)
    with pytest.raises(AssertionError, match=r"^no provider token outside opaque keys at export$"):
        assert_export_attribution("# Title\n", raw, TOKENS)
    with pytest.raises(AssertionError, match=r"^export line carrying a provider token is not provider bytes: "):
        assert_export_attribution("exec 70001\nderived 70002\n", raw, TOKENS)
    with pytest.raises(AssertionError, match=r"^unrecognized digest-shaped run at export: "):
        assert_export_attribution("exec 70001\nderived cbk2_" + "b" * 48 + "\n", raw, TOKENS)
    assert_export_attribution("exec 70001\nderived %s\n" % key_with("70003"), raw, TOKENS)
