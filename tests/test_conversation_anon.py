"""Kernel tests for the pure anonymization scrubber (#281 S4 — safe sharing).

Pure pytest, no DB: exercises ``bin/_lib_conversation_anon.py`` — the single
scrub chokepoint (identity single-pass alternation, then language-neutral secret
patterns) plus the ``AnonPlan`` / ``plan_to_wire`` surface and the
zero-original-tokens guarantee over the real ``export_session_markdown`` render.

The guarantee test carries an explicit PRESENCE leg per scope×field (spec §8.1):
each canary is first asserted present in the RAW render of every scope where its
field renders, so the subsequent "zero canaries survive the anonymized render"
assertion can never pass vacuously. Six non-vacuity RED confirmations (spec §8.9)
are recorded in the plan execution notes / commit body, not automated here.
"""
import importlib.util
import json
import random
import re
import sys
import time
from pathlib import Path

BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))
import _lib_conversation_anon as anon  # noqa: E402
from _lib_conversation_export import export_session_markdown  # noqa: E402

_PARITY_FIXTURE = BIN.parent / "tests" / "fixtures" / "anon" / "parity.json"


def _load_parity_generator():
    """Load bin/build-anon-parity-fixture.py (hyphenated → not import-able by
    name) via importlib, so the golden guard shares the generator's exact
    build_fixture()/dumps_fixture()."""
    p = BIN / "build-anon-parity-fixture.py"
    spec = importlib.util.spec_from_file_location("_anon_parity_gen", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_plan(**over):
    kw = dict(
        project_roots={"/Users/alice/repos/cctally-dev": "cctally-dev",
                       "/Volumes/EXT/repos/project": "project"},
        home_dirs=["/Users/alice"],
        usernames=["alice"],
    )
    kw.update(over)
    return anon.build_anon_plan(**kw)


# ---- identity substitution -------------------------------------------------

def test_path_literal_and_label():
    p = make_plan()
    out = anon.scrub_text("edited /Users/alice/repos/cctally-dev/bin/x in cctally-dev", p)
    assert "/Users/alice" not in out and "cctally-dev" not in out
    assert "project-1/bin/x" in out  # lexical numbering: /Users/... < /Volumes/...


def test_dash_encoded_dirname_variant():
    p = make_plan()
    out = anon.scrub_text("cache at ~/.claude/projects/-Users-alice-repos-cctally-dev/x.jsonl", p)
    assert "-Users-alice-repos-cctally-dev" not in out


def test_idempotent_including_common_word_label():
    p = make_plan()
    text = "the project root /Volumes/EXT/repos/project and word project again"
    once = anon.scrub_text(text, p)
    assert anon.scrub_text(once, p) == once            # no project-2-2 cascade
    assert "project-2-" not in once.replace("project-2/", "")


def test_boundary_rule_not_wordb():
    p = make_plan()
    # label inside a hyphenated generated-style token must NOT rematch
    assert anon.scrub_text("project-9", p) == "project-9"
    # but standalone label does
    assert "project-2" in anon.scrub_text("see project here", p)


def test_home_and_username():
    p = make_plan()
    out = anon.scrub_text("whoami -> alice, home /Users/alice, mail alice@example.com", p)
    assert "alice" not in out.replace("alice@", "") or "alice" not in out
    assert "~" in out and "user" in out


def test_duplicate_basenames_lowest_n():
    p = make_plan(project_roots={"/a/repos/foo": "foo", "/b/repos/foo": "foo"})
    out = anon.scrub_text("foo", p)
    assert out == "project-1"


# ---- secret patterns -------------------------------------------------------

def test_secret_patterns_each():
    p = make_plan()
    cases = {
        "sk-ant-api03-abcdefgh1234": "anthropic-key",
        "sk-" + "a" * 24: "generic-sk-key",
        "ghp_" + "A" * 20: "github-token",
        "github_pat_" + "A" * 20: "github-token",
        "AKIA" + "A" * 16: "aws-access-key",
        "xoxb-1234567890-abc": "slack-token",
    }
    for tok, name in cases.items():
        out = anon.scrub_text(f"key={tok}", p)
        assert tok not in out and f"[REDACTED:{name}]" in out, (tok, out)
    out = anon.scrub_text("Authorization: Bearer abc123def456ghi789", p)
    assert out == "Authorization: [REDACTED:authorization-header]"
    out = anon.scrub_text("Bearer abcdefghijklmnop1234", p)
    assert out == "Bearer [REDACTED:bearer-token]"
    out = anon.scrub_text('password = "hunter2secret"', p)
    assert out == 'password = [REDACTED:secret-assignment]'


def test_secret_sources_cross_language_safe():
    for sp in anon.SECRET_PATTERNS:
        assert "(?i" not in sp.source and "(?P" not in sp.source and "(?<" not in sp.source
        re.compile(sp.source)  # compiles in Python
        if sp.keep_group_1:
            assert re.compile(sp.source).groups >= 1


def test_generated_tokens_never_match_secrets():
    p = make_plan()
    for gen in ("project-1", "~", "user", "(unknown)", "[REDACTED:aws-access-key]"):
        assert anon.scrub_text(gen, p) == gen


def test_secret_runs_after_identity():
    # A secret pass must run after the identity pass, and generated identity
    # tokens must not be swallowed by any secret pattern (spec §3.2).
    p = make_plan()
    out = anon.scrub_text("in /Users/alice/repos/cctally-dev the key is sk-ant-abcdefgh1234", p)
    assert "project-1" in out and "[REDACTED:anthropic-key]" in out


# ---- plan surface ----------------------------------------------------------

def test_plan_to_wire_shape():
    w = anon.plan_to_wire(make_plan())
    assert set(w) == {"tokens", "patterns"}
    assert all(set(t) == {"text", "replacement", "bounded"} for t in w["tokens"])
    assert all(set(pt) == {"name", "source", "ignoreCase", "keepGroup1"} for pt in w["patterns"])
    lens = [len(t["text"]) for t in w["tokens"]]
    assert lens == sorted(lens, reverse=True)


def test_sentinel_constant():
    assert anon.ANON_SENTINEL == "(unknown)"


# ---- zero-original-tokens guarantee, non-vacuous per scope×field -----------

# Identity + secret canaries seeded into distinct fields so the guarantee test
# proves the scrub reaches every render surface — not just prose.
ROOT = "/Users/alice/repos/cctally-dev"          # project root (→ project-1)
LABEL = "cctally-dev"                            # basename of ROOT
HOME = "/Users/alice"                            # home dir (→ ~)
USER = "alice"                                   # username (→ user)
ENCODED = "-Users-alice-repos-cctally-dev"       # Claude project-dir encoding (→ -project-1)
SK = "sk-ant-api03-" + "Z" * 24                  # anthropic secret
GH = "ghp_" + "Q" * 24                           # github secret
AKIA = "AKIA" + "M" * 16                          # aws secret
# The title carries the label as an interior, space-delimited word. NOTE: the
# `recipe` scope wraps the title in markdown emphasis (`_title_`), and `_` is in
# the bounded-token boundary class (deliberately, to avoid mangling snake_case
# identifiers) — so a bounded label/username that is the LEADING or TRAILING word
# of a recipe title is `_`-adjacent and survives. That is an accepted property of
# the text-level scrub (spec §3.1 boundary rule + §11 label trade-off; the
# identity-critical path/root/home/encoded/secret tokens always scrub). Placing
# the label interior exercises the common case where it does scrub.
TITLE = f"debug {LABEL} run"


def _anchor(u):
    return {"session_id": "s1", "uuid": u, "id": 1}


def _guarantee_items():
    return [
        # Human prompt prose: identity tokens render in ALL four scopes.
        {"kind": "human", "anchor": _anchor("h1"), "ts": "2026-06-01T00:00:00Z",
         "text": f"working in {ROOT}, label {LABEL}, home {HOME}, me {USER}",
         "blocks": [], "subagent_key": None, "is_sidechain": False},
        # Assistant turn: prose (ENCODED + SK render in all/chat), Edit (file_path
        # ROOT + GH in the diff → all only), Bash (ROOT + AKIA → all only).
        {"kind": "assistant", "anchor": _anchor("a1"), "ts": "2026-06-01T00:00:05Z",
         "text": f"cache dir {ENCODED} and my key {SK}", "model": "claude-opus-4-8",
         "subagent_key": None, "is_sidechain": False,
         "blocks": [
             {"kind": "text", "text": f"cache dir {ENCODED} and my key {SK}"},
             {"kind": "tool_call", "name": "Edit", "tool_use_id": "t-edit",
              "input": {"file_path": f"{ROOT}/cost.ts",
                        # GH is a BARE token (no `token=`/`secret=` prefix), so
                        # ONLY the github-token pattern covers it — that keeps the
                        # github-token secret pattern load-bearing for §8.9 (a
                        # `token=<GH>` form would be double-covered by
                        # secret-assignment and mask the drop).
                        "old_string": f"revoke {GH} now", "new_string": "rotated"},
              "input_truncated": False,
              "result": {"text": "updated", "truncated": False, "is_error": False}},
             {"kind": "tool_call", "name": "Bash", "tool_use_id": "t-bash",
              "input": {"command": f"cd {ROOT} && printenv"},
              "input_truncated": False,
              "result": {"text": f"AWS {AKIA}\n", "truncated": False, "is_error": False}},
         ]},
        # Meta turn: HOME renders in `all` only.
        {"kind": "meta", "anchor": _anchor("m1"), "ts": "2026-06-01T00:00:06Z",
         "meta_kind": "context", "text": f"host home {HOME} for {USER}",
         "subagent_key": None, "is_sidechain": False, "blocks": []},
    ]


# Per-scope PRESENCE matrix (spec §8.1): which canaries the RAW render of each
# scope MUST contain. chat/prompts/recipe intentionally omit tool payloads, so a
# tool-only canary is vacuously absent there without this presence leg.
_SCOPE_PRESENT = {
    "all":     [ROOT, LABEL, HOME, USER, ENCODED, SK, GH, AKIA],
    "chat":    [ROOT, LABEL, HOME, USER, ENCODED, SK],
    "prompts": [ROOT, LABEL, HOME, USER],
    "recipe":  [ROOT, LABEL, HOME, USER],
}
_ALL_CANARIES = [ROOT, LABEL, HOME, USER, ENCODED, SK, GH, AKIA]


def test_zero_original_tokens_guarantee_per_scope():
    plan = make_plan()
    for scope, present in _SCOPE_PRESENT.items():
        raw = export_session_markdown(_guarantee_items(), scope, title=TITLE)
        # Presence leg: prove each expected canary genuinely renders in raw.
        for canary in present:
            assert canary in raw, (scope, "presence leg failed", canary)
        # Guarantee: zero canaries survive the anonymized render.
        scrubbed = anon.scrub_text(raw, plan)
        for canary in _ALL_CANARIES:
            assert canary not in scrubbed, (scope, "canary survived", canary)
        # Home-dir leg is load-bearing: the standalone HOME (`/Users/alice`,
        # which renders in every scope via the human prompt) collapses to `~`
        # ONLY through the home-dir token — the username token alone would leave
        # a `/Users/user` residue. `~` never appears in the raw render, so its
        # presence proves the home-dir token fired (drops → RED, non-vacuity §8.9).
        assert "~" in scrubbed, (scope, "home-dir token did not fire")


def test_scrub_idempotent_on_full_render():
    plan = make_plan()
    raw = export_session_markdown(_guarantee_items(), "all", title=TITLE)
    once = anon.scrub_text(raw, plan)
    assert anon.scrub_text(once, plan) == once


# ---- cross-language parity fixture (spec §8.4) -----------------------------

def test_parity_fixture_is_regenerated():
    """Golden guard: the committed fixture must byte-match a fresh regeneration
    from the LIVE SECRET_PATTERNS + fixed plan — so drift between the production
    kernel and the fixture (which vitest also executes) fails CI."""
    gen = _load_parity_generator()
    assert _PARITY_FIXTURE.read_text(encoding="utf-8") == gen.dumps_fixture(
        gen.build_fixture()), "regenerate: bin/build-anon-parity-fixture.py"


def test_parity_fixture_cases_match_python_applier():
    """Python applier over every parity case (the vitest suite runs the TS
    applier over the same inputs/expected — every production pattern in both)."""
    fixture = json.loads(_PARITY_FIXTURE.read_text(encoding="utf-8"))
    plan = make_plan()   # same fixed inputs the generator used
    for case in fixture["cases"]:
        assert anon.scrub_text(case["input"], plan) == case["expected"], case["input"]


# ---- trie ≡ flat alternation equivalence (perf change is byte-identical) ----

def _flat_reference_scrub(text, plan):
    """A NAIVE flat longest-first alternation reference — the pre-trie identity
    semantics, kept independent of the production kernel so the equivalence test
    is meaningful. Secrets pass mirrors the kernel (unchanged by the perf work)."""
    if plan.tokens:
        lookup = {t: rep for t, rep, _ in plan.tokens}
        alt = "|".join(
            (f"(?<![{anon._BOUNDARY}])" + re.escape(t) + f"(?![{anon._BOUNDARY}])") if b
            else re.escape(t)
            for t, _, b in plan.tokens)
        text = re.compile(alt).sub(
            lambda m: lookup.get(m.group(0), anon.ANON_SENTINEL), text)
    for sp in plan.patterns:
        rx = re.compile(sp.source, re.IGNORECASE if sp.ignore_case else 0)

        def _repl(m, _sp=sp):
            return (m.group(1) if _sp.keep_group_1 else "") + f"[REDACTED:{_sp.name}]"

        text = rx.sub(_repl, text)
    return text


def _shared_prefix_plan():
    """≥50 roots incl. shared-prefix roots (/a/x, /a/xy, /a/x/y) and a common-word
    label. The shared-prefix roots and the filler roots are label-LESS so they
    stay unbounded and land in the SAME trie chunk — that co-chunking is what
    exercises the trie's prefix folding (/a/x carries an optional 'y' branch for
    /a/xy in one chunk), so this corpus is genuinely non-vacuous against a
    trie-optionality bug (verified: a non-optional-group break yields >0 corpus
    mismatches). The common-word 'common' is the one bounded label + a username."""
    roots = {"/a/x": "", "/a/xy": "", "/a/x/y": "", "/proj/common": "common"}
    for i in range(60):
        roots[f"/srv/app{i:02d}/root"] = ""
    return anon.build_anon_plan(
        project_roots=roots, home_dirs=["/home/dev"], usernames=["dev", "common"])


def _equivalence_corpus(plan, n=200, seed=20260711):
    """A deterministic corpus mixing token fragments, whole tokens,
    boundary-adjacent placements, and overlapping prefixes."""
    rng = random.Random(seed)
    toks = [t for t, _, _ in plan.tokens]
    frags = (toks
             + [t[: len(t) // 2] for t in toks if len(t) > 2]        # prefixes
             + [t + "x" for t in toks[:20]]                          # boundary-adjacent tails
             + ["hello", "/a/", "/a/xz", "/a/x/y", "common", "commonly", "-a-x", "xy"])
    seps = [" ", "", "\n", "/", "-", "x", "y", ".", "_"]
    out = []
    for _ in range(n):
        pieces = []
        for _ in range(rng.randint(1, 8)):
            pieces.append(rng.choice(frags))
            pieces.append(rng.choice(seps))
        out.append("".join(pieces))
    return out


def test_trie_equivalence_over_fixture_inputs():
    """Production scrub_text (chunked-trie) == the naive flat reference over every
    committed parity-fixture input — the perf change cannot alter one byte."""
    fixture = json.loads(_PARITY_FIXTURE.read_text(encoding="utf-8"))
    plan = make_plan()
    for case in fixture["cases"]:
        s = case["input"]
        assert anon.scrub_text(s, plan) == _flat_reference_scrub(s, plan), s


def test_trie_equivalence_property_generated_corpus():
    """Production scrub_text == the naive flat reference over a deterministic
    200-text corpus (fragments / whole tokens / boundary-adjacent / overlapping
    prefixes) against a ≥50-root plan with shared-prefix roots + a common-word
    label. This is the load-bearing proof that the trie preserves semantics."""
    plan = _shared_prefix_plan()
    corpus = _equivalence_corpus(plan, n=200)
    assert len(corpus) == 200
    for s in corpus:
        assert anon.scrub_text(s, plan) == _flat_reference_scrub(s, plan), repr(s)


# ---- trie-specific edge cases --------------------------------------------

def test_trie_shared_prefix_longest_wins():
    # /a/x (project-1) is a strict prefix of /a/xy (project-2); at a shared start
    # the LONGER token must win, not project-1 + a stray "y".
    p = anon.build_anon_plan(
        project_roots={"/a/x": "", "/a/xy": ""}, home_dirs=[], usernames=[])
    assert anon.scrub_text("/a/xy", p) == "project-2"           # NOT "project-1y"
    assert anon.scrub_text("/a/x", p) == "project-1"
    assert anon.scrub_text("/a/xy/z", p) == "project-2/z"
    assert anon.scrub_text("/a/xz", p) == "project-1z"          # diverges → shorter word
    assert anon.scrub_text("/a/xy", p) == _flat_reference_scrub("/a/xy", p)


def test_trie_strict_prefix_inside_one_chunk():
    # Two unbounded tokens in ONE chunk where one is a strict prefix of the other.
    p = anon.build_anon_plan(
        project_roots={"/foo": "", "/foobar": ""}, home_dirs=[], usernames=[])
    assert anon.scrub_text("/foobar", p) == "project-2"         # /foo<​/foobar
    assert anon.scrub_text("/foo!", p) == "project-1!"          # boundary char → /foo
    assert anon.scrub_text("/foobarbaz", p) == "project-2baz"
    for s in ("/foo", "/foobar", "/foo/foobar", "/fooba"):
        assert anon.scrub_text(s, p) == _flat_reference_scrub(s, p), s


def test_trie_chunk_boundary_bounded_interleaved_by_length():
    # A bounded token whose LENGTH falls between two unbounded lengths splits the
    # otherwise-contiguous unbounded run into two chunks, with a bounded chunk
    # between them. Lengths: /longpath (9, unbounded) > boundtok (8, bounded
    # username) > /mid (4, unbounded).
    p = anon.build_anon_plan(
        project_roots={"/longpath": "", "/mid": ""},
        home_dirs=[], usernames=["boundtok"])
    pat = anon._build_identity_pattern(p.tokens)
    # The bounded username sits in its OWN boundary-guarded chunk between the two
    # unbounded tries — the shared `(?<![B])(?:…)(?![B])` guard wraps the (here
    # single-member) bounded trie, never bleaking its lookarounds onto the
    # neighbouring unbounded runs.
    assert f"(?<![{anon._BOUNDARY}])(?:boundtok)(?![{anon._BOUNDARY}])" in pat
    # Three maximal runs present (unbounded / bounded / unbounded).
    assert pat.count("(?:") >= 3
    for s in ("/longpath boundtok /mid",
              "x/longpathboundtok",           # boundtok adjacency: username must NOT fire
              "call boundtok now",            # standalone username → user
              "/mid/longpath boundtok"):
        assert anon.scrub_text(s, p) == _flat_reference_scrub(s, p), s
    assert anon.scrub_text("call boundtok now", p) == "call user now"


def test_trie_bounded_run_merges_into_one_guard():
    # A maximal run of CONSECUTIVE bounded tokens (all sharing the same
    # lookarounds) folds into ONE `(?<![B])(?:trie)(?![B])` guard — the
    # optimization that actually removes the O(text × labels) blow-up on
    # label-heavy real plans. Byte-identical to per-singleton flat alternation.
    p = anon.build_anon_plan(
        project_roots={}, home_dirs=[], usernames=["alpha", "alphabet", "be", "beta"])
    pat = anon._build_identity_pattern(p.tokens)
    guard = f"(?<![{anon._BOUNDARY}])(?:"
    assert pat.count(guard) == 1                          # exactly ONE bounded run
    assert pat.count(f"(?![{anon._BOUNDARY}])") == 1      # one trailing boundary
    # longest-wins INSIDE the merged bounded trie (alpha ⊂ alphabet, be ⊂ beta):
    assert anon.scrub_text("alphabet", p) == "user"       # NOT "userbet"
    assert anon.scrub_text("beta", p) == "user"           # NOT "userta"
    # the shared boundary guard is honored through the merge:
    assert anon.scrub_text("alphax", p) == "alphax"       # adjacency → no match
    assert anon.scrub_text("see alpha here", p) == "see user here"
    for s in ("alpha be beta alphabet", "xalphabet", "alpha-be", "be.alpha",
              "alphabetalpha", "alpha alphabet"):
        assert anon.scrub_text(s, p) == _flat_reference_scrub(s, p), s


# ---- coarse perf smoke: trie collapses the O(text × alternatives) blow-up ----

def test_trie_perf_smoke_large_plan_under_5s():
    # ~1200 unbounded root tokens (label-less → no bounded singletons), so the
    # identity pass is ONE big trie — the shape the real 2.5MB export hit. The old
    # flat alternation over ~2400 alternatives takes >>10s on a ~1MB text; the trie
    # is well under a second, so a 5s wall-clock ceiling is a 10×+ flake margin.
    roots = {f"/srv/service{i:04d}/checkout": "" for i in range(1200)}
    plan = anon.build_anon_plan(
        project_roots=roots, home_dirs=["/home/ci"], usernames=["ci"])
    root_list = list(roots)
    rng = random.Random(99)
    chunks, size = [], 0
    while size < 1_000_000:
        piece = (rng.choice(root_list) if rng.random() < 0.05
                 else "lorem ipsum dolor sit amet consectetur")
        chunks.append(piece)
        size += len(piece) + 1
    text = "\n".join(chunks)
    t0 = time.monotonic()
    out = anon.scrub_text(text, plan)
    dt = time.monotonic() - t0
    assert dt < 5.0, f"scrub_text took {dt:.2f}s — trie regression?"
    assert "project-" in out                                    # matches actually fired
