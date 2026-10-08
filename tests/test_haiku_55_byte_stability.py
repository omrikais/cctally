"""#929 S1 A4: every existing Claude card prices float-identically.

Adding the Haiku 5.5 whole-request (100K prompt-length) card must not move a
single ULP for any card already in ``CLAUDE_MODEL_PRICING``. The literals below
were captured on the unmodified tree (``origin/main`` 66743ebc0) BEFORE any
caller was adapted to the new API, from the kernel (``_calculate_entry_cost``)
and the cache-report kernel (``_compute_entry_cache_dollars``), and are
compared by ``float.hex()`` — exact equality, never approx.

The cases cover the shapes the #929 change could plausibly perturb: a card
with ``*_above_200k_tokens`` marginal tiers (Sonnet 4.5 at 555,205 creation
tokens, with and without a 1h split), a card with NO tier on the ``h == 0``
early return and on the mixed-TTL proportional form (Fable 5), the fast
multiplier (Opus 5.5) and a just-under-100K legacy Haiku 4.5 request that a
mis-scoped whole-request selector would wrongly reprice.

``CARD_DIGESTS`` extends that to EVERY card that existed at the merge base
(all 37 keys of ``CLAUDE_MODEL_PRICING`` except ``claude-haiku-5-5``). For each
card the test walks ``_grid()`` (2,880 points: input x output x cache creation
x cache read x 1h split x speed, covering the 100,000/100,001 and
200,000/200,001 boundaries, ``h`` in {None, 0, 1, 123,456} and both speeds) and
hashes the canonical serialization ``_canonical_line``: one UTF-8 line per grid
point, in grid order,

    f"{i}|{o}|{cc}|{cr}|{h}|{speed}|{kernel}|{saved}|{wasted}|{net}\n"

where ``kernel`` is ``_calculate_entry_cost(model, usage, mode="calculate")``
and saved/wasted/net are ``_compute_entry_cache_dollars``, each as
``float.hex()``. One SHA-256 per card, so a failure names the card.

How the digests were generated: NOT from this tree. The two modules
(``bin/_lib_pricing.py`` and ``bin/_lib_cache_report.py``) were copied from the
merge base 66743ebc0, the same grid was evaluated against those copies (the
merge-base ``_compute_entry_cache_dollars`` has no ``input_tokens`` keyword; the
head call passes the request's ``input_tokens``), and the per-card digests were
computed twice — from that run's recorded per-case hex values and by
re-executing the merge-base modules — and agreed.

Regenerating one digest after an INTENTIONAL vendor-rate change to that card:
check out the commit that makes the change, confirm the change is the only
pricing difference, then evaluate ``_card_digest(model)`` from this module on
that tree and paste the new value for that one card with a comment naming
the change. Never regenerate every digest from the head code as a matter of
course: that would pin whatever the head computes, including a regression.
"""
from __future__ import annotations

import hashlib
import pathlib
import sys

_BIN = pathlib.Path(__file__).resolve().parent.parent / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

import _lib_pricing as pricing  # noqa: E402
import _lib_cache_report as cache_report  # noqa: E402
import _lib_cost_provenance as prov  # noqa: E402

HAIKU55 = "claude-haiku-5-5"

# (model, usage kwargs, kernel hex, cache-report saved hex, wasted hex, net hex)
EXPECTED = [
    ("claude-sonnet-4-5",
     {"input_tokens": 300000, "output_tokens": 250000,
      "cache_creation_tokens": 555205, "cache_read_tokens": 300000},
     "0x1.1b7d3c3611340p+3", "0x1.147ae147ae147p+0",
     "0x1.5d98f1d3ed529p-1", "0x1.96b9a176ddacap-2"),
    ("claude-sonnet-4-5",
     {"input_tokens": 1000, "output_tokens": 2000,
      "cache_creation_tokens": 555205, "cache_read_tokens": 10, "h": 123456},
     "0x1.f3861a6209c4fp+1", "0x1.c4fc1df3300dep-16",
     "0x1.23676ea68d674p+0", "-0x1.2365a9aa6f741p+0"),
    ("claude-fable-5",
     {"input_tokens": 7, "output_tokens": 11,
      "cache_creation_tokens": 555205, "cache_read_tokens": 13},
     "0x1.bc345ae5ffa3cp+2", "0x1.eabbcb1cc9646p-14",
     "0x1.6354c985f06f6p+0", "-0x1.634d1e96c3fc4p+0"),
    ("claude-fable-5",
     {"input_tokens": 7, "output_tokens": 11,
      "cache_creation_tokens": 555205, "cache_read_tokens": 13, "h": 123456},
     "0x1.f776a0dbad3a6p+2", "0x1.eabbcb1cc9646p-14",
     "0x1.282ef0ae53650p+1", "-0x1.282b1b36bd2b7p+1"),
    ("claude-opus-5-5",
     {"input_tokens": 123457, "output_tokens": 8765,
      "cache_creation_tokens": 45678, "cache_read_tokens": 234567,
      "h": 12345, "speed": "fast"},
     "0x1.f682c3943ee6cp+0", "0x1.c85fa15047403p+0",
     "0x1.52cadddf43c7fp-3", "0x1.9e0645945ec73p+0"),
    ("claude-haiku-4-5",
     {"input_tokens": 99999, "output_tokens": 1,
      "cache_creation_tokens": 2, "cache_read_tokens": 3},
     "0x1.99a0baf60ab3bp-4", "0x1.6a634b28f33e5p-19",
     "0x1.0c6f7a0b5ed90p-21", "0x1.27476ca61b881p-19"),
]


# Per-card SHA-256 over the canonical grid serialization, computed from the
# merge-base (66743ebc0) modules; see the module docstring.
CARD_DIGESTS = {
    "claude-3-5-haiku-20241022":
        "81b8384d0528eed22e51b17cfa63e65ba4365cbefbc271a5cbc34754690b24ff",
    "claude-3-5-haiku-latest":
        "c1f8dd2378f516add17aadfc29f5918a0c4ced7a8d85da6d01ad095f03eaaa21",
    "claude-3-5-sonnet-20240620":
        "ef95798310e0b63b252a5600ad3528228b98f9e2bb4f75195424e4e557ec5ecd",
    "claude-3-5-sonnet-20241022":
        "ef95798310e0b63b252a5600ad3528228b98f9e2bb4f75195424e4e557ec5ecd",
    "claude-3-5-sonnet-latest":
        "ef95798310e0b63b252a5600ad3528228b98f9e2bb4f75195424e4e557ec5ecd",
    "claude-3-7-sonnet-20250219":
        "ef95798310e0b63b252a5600ad3528228b98f9e2bb4f75195424e4e557ec5ecd",
    "claude-3-7-sonnet-latest":
        "ef95798310e0b63b252a5600ad3528228b98f9e2bb4f75195424e4e557ec5ecd",
    "claude-3-haiku-20240307":
        "1c4df7b6eaeba6e89f11a5f67472c310d82c79c89437168843b54d1e30284942",
    "claude-3-opus-20240229":
        "4a0c54c339ebbcb97b69565d914e36f657f74290cfb75cec5ff261fedc97f0a3",
    "claude-3-opus-latest":
        "4a0c54c339ebbcb97b69565d914e36f657f74290cfb75cec5ff261fedc97f0a3",
    "claude-4-opus-20250514":
        "4a0c54c339ebbcb97b69565d914e36f657f74290cfb75cec5ff261fedc97f0a3",
    "claude-4-sonnet-20250514":
        "fc64ec6aabe50a7610f9d473629702a188fdf9617252318b5833af2b745fc749",
    "claude-fable-5":
        "c2f6aca72cb687577f209286ffb33fd2e0feca717f630c5227e9e77d7e8abc24",
    "claude-fable-5-1":
        "9f749bbf327d78f3d38bab3b53faa8076bf4a232fbaf17b2214c8c388866c44a",
    "claude-haiku-4-5":
        "c1f8dd2378f516add17aadfc29f5918a0c4ced7a8d85da6d01ad095f03eaaa21",
    "claude-haiku-4-5-20251001":
        "c1f8dd2378f516add17aadfc29f5918a0c4ced7a8d85da6d01ad095f03eaaa21",
    "claude-mythos-5":
        "c2f6aca72cb687577f209286ffb33fd2e0feca717f630c5227e9e77d7e8abc24",
    "claude-mythos-5-1":
        "9f749bbf327d78f3d38bab3b53faa8076bf4a232fbaf17b2214c8c388866c44a",
    "claude-mythos-preview":
        "dfdaaad023746db489f440fcc503b1aac60d61d7d8d1c03747ef230b3a7d0128",
    "claude-opus-4-1":
        "4a0c54c339ebbcb97b69565d914e36f657f74290cfb75cec5ff261fedc97f0a3",
    "claude-opus-4-1-20250805":
        "4a0c54c339ebbcb97b69565d914e36f657f74290cfb75cec5ff261fedc97f0a3",
    "claude-opus-4-20250514":
        "4a0c54c339ebbcb97b69565d914e36f657f74290cfb75cec5ff261fedc97f0a3",
    "claude-opus-4-5":
        "2e10ad64331385afc70ba0fade87b1aef540d444c67c75deb09f9c253057befd",
    "claude-opus-4-5-20251101":
        "2e10ad64331385afc70ba0fade87b1aef540d444c67c75deb09f9c253057befd",
    "claude-opus-4-6":
        "4c0ae588505712735322aa7e98dfa5d7b4a652ecacff6defaef1b925494e8f9a",
    "claude-opus-4-6-20260205":
        "4c0ae588505712735322aa7e98dfa5d7b4a652ecacff6defaef1b925494e8f9a",
    "claude-opus-4-7":
        "4c0ae588505712735322aa7e98dfa5d7b4a652ecacff6defaef1b925494e8f9a",
    "claude-opus-4-7-20260416":
        "4c0ae588505712735322aa7e98dfa5d7b4a652ecacff6defaef1b925494e8f9a",
    "claude-opus-4-8":
        "1494d7e92dad79e1b9e701884908319f60ee461be7cf691f55753e414f572930",
    "claude-opus-5":
        "1494d7e92dad79e1b9e701884908319f60ee461be7cf691f55753e414f572930",
    "claude-opus-5-5":
        "eace45a71fec4f6eabe18924f00990f784170c24ab29ffb5524d0ea577ed6176",
    "claude-sonnet-4-20250514":
        "fc64ec6aabe50a7610f9d473629702a188fdf9617252318b5833af2b745fc749",
    "claude-sonnet-4-5":
        "fc64ec6aabe50a7610f9d473629702a188fdf9617252318b5833af2b745fc749",
    "claude-sonnet-4-5-20250929":
        "fc64ec6aabe50a7610f9d473629702a188fdf9617252318b5833af2b745fc749",
    "claude-sonnet-4-6":
        "ef95798310e0b63b252a5600ad3528228b98f9e2bb4f75195424e4e557ec5ecd",
    "claude-sonnet-5":
        "184b8281376b30d1c612040274997cd45350d92cd80b4f0d42618d5c4c4b2c88",
    "claude-sonnet-5-5":
        "184b8281376b30d1c612040274997cd45350d92cd80b4f0d42618d5c4c4b2c88",
}

_INPUTS = (0, 7, 300_000)
_OUTPUTS = (0, 11, 250_000)
_CREATES = (0, 1, 7, 99_999, 100_000, 100_001, 199_999, 200_000, 200_001,
            555_205)
_READS = (0, 13, 100_001, 300_000)
_ONE_HOURS = (None, 0, 1, 123_456)
_SPEEDS = (None, "fast")


def _grid():
    for i in _INPUTS:
        for o in _OUTPUTS:
            for cc in _CREATES:
                for cr in _READS:
                    for h in _ONE_HOURS:
                        for speed in _SPEEDS:
                            yield i, o, cc, cr, h, speed


def _canonical_line(model, i, o, cc, cr, h, speed):
    usage = pricing.claude_usage_dict(
        cache_1h_tokens=h, speed=speed, input_tokens=i, output_tokens=o,
        cache_creation_tokens=cc, cache_read_tokens=cr)
    kernel = pricing._calculate_entry_cost(model, usage, mode="calculate")
    saved, wasted, net = cache_report._compute_entry_cache_dollars(
        model, cc, cr, input_tokens=i, pricing=pricing.CLAUDE_MODEL_PRICING,
        cache_1h_tokens=usage.get("cache_creation_1h_input_tokens"),
        speed=speed)
    return (f"{i}|{o}|{cc}|{cr}|{h}|{speed}|{kernel.hex()}|{saved.hex()}|"
            f"{wasted.hex()}|{net.hex()}\n")


def _card_digest(model):
    digest = hashlib.sha256()
    for point in _grid():
        digest.update(_canonical_line(model, *point).encode("utf-8"))
    return digest.hexdigest()

def _usage(kw):
    kw = dict(kw)
    return pricing.claude_usage_dict(
        cache_1h_tokens=kw.pop("h", None), speed=kw.pop("speed", None), **kw)


def test_kernel_cost_is_byte_stable():
    for model, kw, kernel_hex, _s, _w, _n in EXPECTED:
        got = pricing._calculate_entry_cost(model, _usage(kw), mode="calculate")
        assert got.hex() == kernel_hex, (model, kw, got.hex(), kernel_hex)


def test_cache_report_is_byte_stable():
    for model, kw, _k, saved_hex, wasted_hex, net_hex in EXPECTED:
        usage = _usage(kw)
        got = cache_report._compute_entry_cache_dollars(
            model, usage["cache_creation_input_tokens"],
            usage["cache_read_input_tokens"],
            input_tokens=kw["input_tokens"],
            pricing=pricing.CLAUDE_MODEL_PRICING,
            cache_1h_tokens=usage.get("cache_creation_1h_input_tokens"),
            speed=usage.get("speed"))
        assert tuple(v.hex() for v in got) == (saved_hex, wasted_hex, net_hex), (
            model, kw)


def test_legacy_cards_have_no_provenance_violations():
    violations = prov.claude_card_rate_violations(
        pricing.current_pricing_snapshot())
    legacy = set(pricing.CLAUDE_MODEL_PRICING) - {HAIKU55}
    assert legacy, "precondition: the live table has legacy cards"
    offending = [v for v in violations if v[0] in legacy]
    assert offending == []


def test_every_merge_base_card_is_still_priced():
    assert len(CARD_DIGESTS) == 37
    assert HAIKU55 not in CARD_DIGESTS
    missing = sorted(set(CARD_DIGESTS) - set(pricing.CLAUDE_MODEL_PRICING))
    assert missing == []
    # Cards added after the merge base (Haiku 5.5 and any later model) are
    # outside this pin: a future pricing sync must not have to edit it.
    assert HAIKU55 in pricing.CLAUDE_MODEL_PRICING


def test_every_merge_base_card_digest_is_byte_stable():
    moved = {model: _card_digest(model) for model in CARD_DIGESTS}
    moved = {model: got for model, got in moved.items()
             if got != CARD_DIGESTS[model]}
    assert moved == {}, sorted(moved)
