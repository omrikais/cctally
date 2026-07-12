"""Pure, I/O-free anonymization kernel for conversation sharing (#281 S4).

The single scrub chokepoint for the "share this session" flow. Mirrors the
``_lib_share.py`` / ``_lib_conversation_export.py`` purity contract: no DB, no
filesystem, no env reads, no clock — every input is injected. The whole export
(and every per-card copy) passes through :func:`scrub_text`, so no field can
bypass the redaction, and one mechanism serves both the server exports and the
client-side copies (the TS applier is a dumb executor of :func:`plan_to_wire`).

Two passes, in order:

1. **Identity** — a SINGLE pass over the original text. All identity tokens
   (observed project roots → ``project-N``, their dash-encoded dirname variants
   → ``-project-N``, home dirs → ``~``, usernames → ``user``, and project labels
   → their root's ``project-N``) compile into ONE alternation regex; each match
   is resolved through a lookup map. Because a replacement's output is never
   re-scanned, generated tokens cannot be re-matched — this is what makes
   ``scrub_text(scrub_text(x)) == scrub_text(x)`` structural, not aspirational.
   Bounded tokens (labels, usernames) carry an explicit shared ASCII boundary
   (``A-Za-z0-9_.-`` single-char lookarounds, deliberately NOT ``\\b``); the
   trailing ``-`` in that class is what protects a generated ``project-N`` from
   re-matching the bare ``project`` label. A lookup miss (impossible by
   construction) yields the fixed sentinel ``ANON_SENTINEL`` — never passthrough.

   **Trie compilation (perf, byte-identical).** The tokens are globally ordered
   longest-text-first, so a flat ``t1|t2|…`` alternation is *longest-match-wins*
   (Python ``re`` takes the first listed alternative that matches, and the first
   that can match at any position is the longest, since two equal-length literals
   cannot both match one position). A flat alternation over ~1300 tokens is
   O(text × alternatives), though — every alternative is retried at every
   position. So :func:`_build_identity_pattern` collapses each MAXIMAL
   CONSECUTIVE RUN of same-boundedness tokens into a trie-structured subgroup
   (shared prefixes fold into nested ``(?:…)`` groups; a node that is both
   end-of-word and has children makes the children group optional-greedy so the
   longest branch wins). An unbounded run is a bare trie; a bounded run — whose
   members share the *same* lookarounds — is one trie wrapped in a single
   ``(?<![B])(?:…)(?![B])`` guard (the trailing lookahead backtracks through the
   trie's greedy optionals to the longest boundary-clearing member, exactly as
   the per-singleton flat form would). Bounded runs merge too because real DB
   plans are heavily bounded (~26% project-label tokens), so leaving them
   singleton would keep the identity pass O(text × labels). This is
   byte-identical to the flat alternation *by
   construction*: within a chunk the trie yields the longest matching member;
   across chunks/singletons the first alternative with any match wins, and
   because every token in an earlier chunk is ≥ every later alternative in
   length, "first chunk that matches" is exactly "longest flat alternative that
   matches" (proof + equivalence property test in ``test_conversation_anon``).
   ``plan_to_wire`` and the TS applier are UNTOUCHED (per-card texts are small).

2. **Secrets** — a small documented, high-precision pattern set
   (:data:`SECRET_PATTERNS`), applied AFTER identity. Each pattern is a
   cross-language descriptor (valid in BOTH Python ``re`` and JS ``RegExp``): no
   inline flags, no named groups, no lookbehind, no ``$n`` templates. The
   effective replacement is ``(group 1 if keep_group_1 else "") + [REDACTED:<name>]``.

**Honest guarantee (no over-claiming, #280 privacy):** the tested claim is
"zero *known* identity tokens survive" — observed project roots/labels, home
dirs, usernames, plus the documented secret patterns. NOT comprehensively
detected or guaranteed removed in v1: emails, IPs, hostnames, remote URLs,
session IDs, and any project identity absent from cctally's DB. Best-effort over
known tokens; review before sharing. See ``docs/commands/transcript.md``.
"""
from __future__ import annotations

import dataclasses
import re

ANON_SENTINEL = "(unknown)"
# Shared Python/JS boundary class for bounded tokens (deliberately NOT `\b`,
# whose Unicode behavior differs across runtimes). Single-char lookarounds are
# fixed-width — legal in Python `re` and identical in JS.
_BOUNDARY = "A-Za-z0-9_.-"


@dataclasses.dataclass(frozen=True)
class SecretPattern:
    """A cross-language secret descriptor. ``source`` must compile in BOTH
    Python ``re`` and JS ``RegExp`` (no inline flags / named groups / lookbehind).
    ``ignore_case`` is a language-neutral boolean (``re.IGNORECASE`` / JS ``i``);
    ``keep_group_1`` preserves capture group 1 (the key + separator) before the
    ``[REDACTED:<name>]`` marker."""
    name: str
    source: str
    ignore_case: bool
    keep_group_1: bool


@dataclasses.dataclass(frozen=True)
class AnonPlan:
    """An ordered identity token list (``(text, replacement, bounded)``,
    longest-text-first, tie lexical) plus the secret-pattern tuple."""
    tokens: tuple
    patterns: tuple


# Ordered: authorization-header BEFORE bearer-token (an ``Authorization: Bearer
# …`` line must redact the whole value, not just the token after ``Bearer``);
# anthropic-key BEFORE generic-sk-key (the specific ``sk-ant-…`` name wins).
SECRET_PATTERNS = (
    SecretPattern("authorization-header", r"(\bAuthorization:[ \t]*)\S[^\r\n]*", True, True),
    SecretPattern("bearer-token", r"(\bBearer[ \t]+)[A-Za-z0-9._~+/=-]{16,}", True, True),
    SecretPattern("anthropic-key", r"sk-ant-[A-Za-z0-9_-]{8,}", False, False),
    SecretPattern("generic-sk-key", r"sk-[A-Za-z0-9_-]{20,}", False, False),
    SecretPattern("github-token", r"(?:gh[pousr]|github_pat)_[A-Za-z0-9_]{16,}", False, False),
    SecretPattern("aws-access-key", r"AKIA[0-9A-Z]{16}", False, False),
    SecretPattern("slack-token", r"xox[baprs]-[A-Za-z0-9-]{10,}", False, False),
    SecretPattern(
        "secret-assignment",
        r"(\b(?:api[_-]?key|secret|token|passwd|password)\b[ \t]*[=:][ \t]*)"
        r"(?:\"[^\"\r\n]{6,}\"|'[^'\r\n]{6,}'|[^\s\"']{6,})", True, True),
)


def _dash_encode(path: str) -> str:
    """``/a/b`` -> ``-a-b`` — the Claude project-dir encoding (as it appears in
    ``~/.claude/projects/<encoded>`` and scratchpad paths)."""
    return path.replace("/", "-")


def build_anon_plan(*, project_roots, home_dirs, usernames) -> AnonPlan:
    """Build an :class:`AnonPlan` from injected identity sources.

    ``project_roots`` maps root path -> label. Numbering is deterministic,
    lexical by root path (``project-1..N``), so CLI and HTTP builds from the same
    DB produce identical plans (byte-parity depends on this). Duplicate basenames
    map the bare label to the LOWEST-numbered of their ``project-N`` targets
    (``setdefault`` over the sorted roots).
    """
    tokens: dict = {}          # (text, bounded) -> replacement
    label_target: dict = {}    # label -> project-N (lowest-N wins)
    for n, root in enumerate(sorted(project_roots), 1):
        if not root:
            continue
        rep = f"project-{n}"
        tokens[(root, False)] = rep
        tokens[(_dash_encode(root), False)] = f"-{rep}"
        label = project_roots[root]
        if label:
            label_target.setdefault(label, rep)   # lowest-N claims the label
    for label, rep in label_target.items():
        tokens[(label, True)] = rep
    for h in home_dirs:
        if h:
            tokens.setdefault((h, False), "~")     # a root already present wins
    for u in usernames:
        if u:
            tokens.setdefault((u, True), "user")
    ordered = tuple(sorted(
        ((t, rep, b) for (t, b), rep in tokens.items()),
        key=lambda x: (-len(x[0]), x[0])))
    return AnonPlan(tokens=ordered, patterns=SECRET_PATTERNS)


# End-of-word sentinel for the trie (no token is the empty string, so "" cannot
# collide with a single-character child key).
_WORD_END = ""


def _trie_to_pattern(node: dict) -> str:
    """Render one trie node to a regex fragment that matches the LONGEST word
    branch reachable from it. Children are keyed by distinct next-characters, so
    the branches of a ``(?:…|…)`` group are mutually exclusive and their order is
    irrelevant. When the node is also end-of-word AND has children, the whole
    descent is wrapped optional-greedy (``(?:…)?``) so the longest branch is tried
    first and a diverging tail backtracks to the shorter complete word. The regex
    therefore only ever matches a COMPLETE inserted word (every accept state is an
    end-of-word node), so ``m.group(0)`` is always a live lookup key."""
    end = _WORD_END in node
    alts = [re.escape(ch) + _trie_to_pattern(node[ch])
            for ch in node if ch != _WORD_END]
    if not alts:
        return ""                       # pure leaf: the word ends here
    if end:
        return "(?:" + "|".join(alts) + ")?"
    if len(alts) == 1:
        return alts[0]
    return "(?:" + "|".join(alts) + ")"


def _build_identity_pattern(tokens: tuple) -> str:
    """Compile the ordered identity tokens to ONE alternation string. Each MAXIMAL
    CONSECUTIVE RUN of same-boundedness tokens collapses into a trie subgroup
    (:func:`_trie_to_pattern`); an unbounded run stays a bare trie, a bounded run
    is wrapped once in the shared ``(?<![B])(?:…)(?![B])`` boundary guard. Matches
    are byte-identical to a flat longest-first ``t1|t2|…`` alternation of the same
    tokens (unbounded literal, bounded boundary-guarded singleton) — the
    equivalence property test proves it against exactly that flat reference.

    **Bounded runs merge too (byte-identical).** Every bounded token carries the
    *same* ``_BOUNDARY`` lookarounds, so a run's members all start at the match
    position and share one leading ``(?<![B])``; the trailing ``(?![B])`` sits
    after the trie, and Python backtracks through the trie's greedy optionals to
    the longest member whose trailing char clears the boundary — exactly the
    member a flat ``…|(?<![B])bi(?![B])|…`` alternation (longest-first) would pick.
    This matters on real data: an observed DB plan is ~26% bounded project-label
    tokens (336 of 1286), and leaving those as singletons keeps the identity pass
    O(text × labels) — the very blow-up the trie exists to remove."""
    parts: list = []
    run: list = []                      # texts of the current same-boundedness run
    run_bounded = False                 # boundedness of the current run

    def _flush():
        if not run:
            return
        root: dict = {}
        for word in run:
            node = root
            for ch in word:
                node = node.setdefault(ch, {})
            node[_WORD_END] = True
        frag = _trie_to_pattern(root)
        if run_bounded:
            frag = f"(?<![{_BOUNDARY}])(?:{frag})(?![{_BOUNDARY}])"
        parts.append(frag)
        run.clear()

    for t, _rep, bounded in tokens:
        if run and bounded != run_bounded:
            _flush()                    # boundary between an unbounded/bounded run
        run_bounded = bounded
        run.append(t)
    _flush()
    return "|".join(parts)


def scrub_text(text: str, plan: AnonPlan) -> str:
    """Identity single-pass, then secrets. Idempotent (tested)."""
    if plan.tokens:
        lookup = {t: rep for t, rep, _ in plan.tokens}
        text = re.compile(_build_identity_pattern(plan.tokens)).sub(
            lambda m: lookup.get(m.group(0), ANON_SENTINEL), text)
    for sp in plan.patterns:
        flags = re.IGNORECASE if sp.ignore_case else 0
        rx = re.compile(sp.source, flags)

        def _repl(m, _sp=sp):
            prefix = m.group(1) if _sp.keep_group_1 else ""
            return f"{prefix}[REDACTED:{_sp.name}]"

        text = rx.sub(_repl, text)
    return text


def plan_to_wire(plan: AnonPlan) -> dict:
    """Project an :class:`AnonPlan` to the JSON wire contract the TS applier
    executes: ``{"tokens": [{"text","replacement","bounded"}...],
    "patterns": [{"name","source","ignoreCase","keepGroup1"}...]}``."""
    return {
        "tokens": [{"text": t, "replacement": rep, "bounded": b}
                   for t, rep, b in plan.tokens],
        "patterns": [{"name": sp.name, "source": sp.source,
                      "ignoreCase": sp.ignore_case, "keepGroup1": sp.keep_group_1}
                     for sp in plan.patterns],
    }
