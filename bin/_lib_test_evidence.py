"""Evidence kernel for the authoritative test gate (#529 S2).

Pure functions only. No I/O beyond what a caller hands in, no environment
reads except the explicit `env` mappings passed as arguments, and no
knowledge of SSH, runner aliases, lock layout, the event ledger, or the
public-mirror grammar. Everything private is injected by the caller.

Published in the public tree by design, because the aggregator imports it.
That import is not written yet — this module lands ahead of the aggregator
work that consumes it — so treat the dependency as the reason for the
placement rather than as a description of the current tree.
"""
from __future__ import annotations

import re

RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SAFE_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def validate_run_id(value) -> bool:
    """True when `value` is a safe single path component."""
    if not isinstance(value, str) or value in (".", ".."):
        return False
    return bool(RUN_ID_RE.match(value))


def _sanitize_component(value: str) -> str:
    """Reduce arbitrary text to the run-id alphabet.

    Runs of rejected characters collapse to one dash, which keeps two
    distinct inputs distinct for every identity this module generates.
    """
    out = re.sub(r"[^A-Za-z0-9._-]+", "-", value or "").strip("-")
    return out[:128]


def generate_run_id(now_utc: str, pid: int, rand: int) -> str:
    """`<UTC stamp>-<pid>-<random>`.

    The caller supplies all three components, so this stays pure and a test
    can pin every part of the identity.
    """
    return f"{_sanitize_component(now_utc)}-{int(pid)}-{int(rand)}"


def resolve_run_id(env, now_utc: str, pid: int, rand: int) -> str:
    """Explicit value, else a GitHub Actions identity, else a generated one.

    Raises ValueError on an explicit value that is not a safe path
    component, because silently regenerating would put the evidence
    somewhere the caller is not looking.
    """
    explicit = (env.get("CCTALLY_TEST_RUN_ID") or "").strip()
    if explicit:
        if not validate_run_id(explicit):
            raise ValueError(
                f"CCTALLY_TEST_RUN_ID is not a safe path component: {explicit!r}"
            )
        return explicit
    if (env.get("GITHUB_ACTIONS") or "").lower() == "true":
        parts = [
            env.get("GITHUB_RUN_ID") or "0",
            env.get("GITHUB_RUN_ATTEMPT") or "0",
            env.get("GITHUB_JOB") or "job",
        ]
        # GITHUB_RUN_ID, the attempt and the job name are identical across a
        # matrix's legs, so without the matrix identity the three Linux
        # versions would share one evidence directory and one artifact name.
        matrix = env.get("CCTALLY_TEST_MATRIX_ID")
        if matrix:
            parts.append(matrix)
        candidate = _sanitize_component("-".join(parts))
        if validate_run_id(candidate):
            return candidate
    return generate_run_id(now_utc, pid, rand)


def resolve_evidence_layout(root, remote_dir: str, run_id: str):
    """Absolute paths under `root`, or None when no root was supplied.

    None is the ordinary local case: the aggregator then keeps its
    temporary log directory and deletes it on exit.
    """
    if not root:
        return None
    if not _SAFE_COMPONENT_RE.match(remote_dir or "") or remote_dir in (".", ".."):
        raise ValueError(f"unsafe remote dir component: {remote_dir!r}")
    if not validate_run_id(run_id):
        raise ValueError(f"unsafe run id: {run_id!r}")
    run_dir = f"{str(root).rstrip('/')}/{remote_dir}/{run_id}"
    export = f"{run_dir}/export"
    return {
        "root": str(root).rstrip("/"),
        "remote_dir": remote_dir,
        "run_id": run_id,
        "run_dir": run_dir,
        "logs": f"{run_dir}/logs",
        "timings": f"{run_dir}/timings",
        "export": export,
        "outcome": f"{export}/outcome.json",
        "failure_context": f"{export}/failure-context.txt",
        "manifest": f"{run_dir}/manifest.json",
    }


# ---------------------------------------------------------------- markers

# Anchored at line start after optional whitespace. `FAIL` needs a non-word
# boundary after it, or `FAILURE_THRESHOLD=3` and similar ordinary output
# would be read as a failure marker.
HARD_MARKERS = (
    r"FAIL(?![A-Za-z0-9_])",
    r"MISSING GOLDEN",
    r"RECONCILE FAIL",
    r"SELF-CHECK FAIL",
    r"AUDIT FAILURE",
    r"FIXTURE-CACHE POISONED",
    r"FAILED(?![A-Za-z0-9_])",
    r"ERROR(?![A-Za-z0-9_])",
    r"INTERNALERROR",
)
SUPPLEMENTAL_MARKERS = (r"WARN:",)

_HARD_RE = re.compile(r"^\s*(?:" + "|".join(HARD_MARKERS) + r")")
_SUPP_RE = re.compile(r"^\s*(?:" + "|".join(SUPPLEMENTAL_MARKERS) + r")")

WINDOW_BEFORE_LINES = 40
WINDOW_AFTER_LINES = 200
PER_SUBJECT_LINE_CAP = 600
GLOBAL_LINE_CAP = 2400
MAX_LINE_BYTES = 4096
MAX_TOTAL_BYTES = 524288


def classify_failure_marker(line):
    """`"hard"`, `"supplemental"`, or None."""
    if _HARD_RE.match(line or ""):
        return "hard"
    if _SUPP_RE.match(line or ""):
        return "supplemental"
    return None


def select_failure_windows(
    lines,
    before: int = WINDOW_BEFORE_LINES,
    after: int = WINDOW_AFTER_LINES,
    failing: bool = False,
):
    """Windows around every marker, merged where they overlap.

    The `before` span is the point of the rewrite. The previous forward-only
    awk rule discarded the explanation a harness prints ahead of its marker,
    and filled the window with the next case's output instead.

    `failing` supplies the marker-less fallback: a run the caller classified
    as failing whose log carries no recognised marker still retains its head
    and its tail, because an empty extract is the one case where the reader
    has nothing else to work from. The spans reuse `before` and `after`, so
    the caller never restates the two numbers.
    """
    raw = []
    for idx, line in enumerate(lines):
        kind = classify_failure_marker(line)
        if kind is None:
            continue
        raw.append(
            {
                "kind": kind,
                "marker_index": idx,
                "start": max(0, idx - before),
                "end": min(len(lines) - 1, idx + after),
            }
        )
    if not raw and failing and lines:
        last = len(lines) - 1
        raw = [
            {
                "kind": "hard",
                "marker_index": None,
                "start": 0,
                "end": min(last, before - 1),
            },
            {
                "kind": "hard",
                "marker_index": None,
                "start": max(0, len(lines) - after),
                "end": last,
            },
        ]
    merged = []
    for win in raw:
        if merged and win["start"] <= merged[-1]["end"]:
            prev = merged[-1]
            prev["end"] = max(prev["end"], win["end"])
            # A merged window is hard when any constituent marker is hard, so
            # a warning cluster absorbed into a failure cluster cannot demote
            # the failure to supplemental in the budget.
            if win["kind"] == "hard":
                prev["kind"] = "hard"
            continue
        merged.append(dict(win))
    return merged


def _water_fill(alloc, targets, remaining: int) -> int:
    """Raise every subject toward its target in equal shares.

    Granting each subject's whole deficit in declared order would let the
    first subject consume the remainder, so the allocation would depend on
    the order the caller happened to enumerate subjects in. Ties below one
    line per subject are broken by name, which keeps the result independent
    of declaration order in every case.
    """
    pending = sorted(name for name in targets if alloc[name] < targets[name])
    while remaining > 0 and pending:
        share = remaining // len(pending)
        if share <= 0:
            for name in pending:
                if remaining <= 0:
                    break
                alloc[name] += 1
                remaining -= 1
            break
        progressed = False
        for name in list(pending):
            grant = min(share, targets[name] - alloc[name])
            if grant > 0:
                alloc[name] += grant
                remaining -= grant
                progressed = True
            if alloc[name] >= targets[name]:
                pending.remove(name)
        if not progressed:
            break
    return remaining


def allocate_budget(
    subjects,
    per_subject_cap: int = PER_SUBJECT_LINE_CAP,
    global_cap: int = GLOBAL_LINE_CAP,
):
    """Line budget per subject: hard clusters first, supplemental context last.

    A first-come budget lets one warning-heavy or noisy harness consume the
    global allowance before another subject's decisive failure is reached,
    which would omit the very thing the extract exists to show. Each phase
    draws only on hard content until no hard content is left unserved, so
    supplemental context can never displace a failure cluster.

    The spec's reserved minimum is delivered by `_water_fill` itself rather
    than by a phase of its own. Equal-share filling raises every subject
    together, so a subject with a positive target cannot be starved while
    another is served beyond its equal share, which is the whole content of
    the reservation. A separate reserve phase preceded this one; it was
    removed because it was unobservable — no input made its result differ
    from equal-share filling alone — except in the degenerate case where the
    budget is smaller than the number of subjects, where it made the result
    depend on the order the caller declared them in.
    """
    if not subjects:
        return {}
    names = [s["name"] for s in subjects]
    alloc = dict.fromkeys(names, 0)
    hard = {s["name"]: max(0, int(s.get("hard", 0))) for s in subjects}
    total = {
        s["name"]: max(0, int(s.get("hard", 0)) + int(s.get("supplemental", 0)))
        for s in subjects
    }
    remaining = int(global_cap)

    # 1. Hard clusters up to the per-subject cap.
    remaining = _water_fill(
        alloc, {n: min(hard[n], per_subject_cap) for n in names}, remaining
    )

    # 2. Supplemental context with whatever is left.
    remaining = _water_fill(
        alloc, {n: min(total[n], per_subject_cap) for n in names}, remaining
    )
    return alloc


OVERSIZED_PLACEHOLDER = "[REDACTED: oversized line]"


def bound_extract_lines(
    lines, max_line_bytes: int = MAX_LINE_BYTES, max_total_bytes: int = MAX_TOTAL_BYTES
):
    """Apply the per-line and total byte ceilings.

    An oversized line is replaced whole rather than truncated, because a
    prefix of an unclassified line is still unclassified content.
    """
    out = []
    stats = {"oversized_lines": 0, "omitted_lines": 0, "truncated": False}
    total = 0
    for i, line in enumerate(lines):
        text = line
        if len(text.encode("utf-8", "replace")) > max_line_bytes:
            text = OVERSIZED_PLACEHOLDER
            stats["oversized_lines"] += 1
        size = len(text.encode("utf-8", "replace")) + 1
        if total + size > max_total_bytes:
            stats["truncated"] = True
            # The notice is bytes on the same budget. Appending it uncounted
            # put the emitted output over the ceiling this function exists to
            # enforce, so retained lines are given back until it fits. Its own
            # length moves as the omitted count grows, hence the fixed point.
            omitted = len(lines) - i
            while True:
                notice = (
                    f"[TRUNCATED: {omitted} further lines omitted "
                    f"at the byte ceiling]"
                )
                notice_size = len(notice.encode("utf-8", "replace")) + 1
                if total + notice_size <= max_total_bytes or not out:
                    break
                total -= len(out.pop().encode("utf-8", "replace")) + 1
                omitted += 1
            stats["omitted_lines"] = omitted
            if total + notice_size <= max_total_bytes:
                out.append(notice)
                total += notice_size
            break
        out.append(text)
        total += size
    return out, stats


# ----------------------------------------------------------- the transformer

UNCLASSIFIED_PLACEHOLDER = "[REDACTED: unclassified line]"
UNCLASSIFIED_DETAIL = "[REDACTED: unclassified detail]"
JSON_PLACEHOLDER = "[REDACTED: json]"
EXCEPTION_MESSAGE_PLACEHOLDER = "[REDACTED: exception message]"

_TYPED_PATTERNS = (
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "<email>"),
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "<uuid>"),
    (re.compile(r"(?i)\b(?:bearer|token|api[_-]?key|authorization)\b\s*[:=]?\s*\S+"),
     "<credential>"),
    (re.compile(r"\b(?:sk|pk)-[A-Za-z0-9_-]{8,}"), "<credential>"),
    (re.compile(r"\b[a-z]+://[^\s/@]+:[^\s/@]+@\S+"), "<credential-url>"),
    (re.compile(r"\b[0-9a-fA-F]{32,}\b"), "<hex>"),
    (re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b"), "<b64>"),
)

# Two tiers, because `re.match` is prefix-only and a pattern without an end
# anchor returns whatever follows it untouched.
#
# Tier 1 matches a WHOLE line that carries no free field, so the line is
# retained verbatim. Every pattern here MUST be anchored at end of line; an
# unanchored one would disclose an arbitrary tail on the strength of a safe
# opening. `tests/test_test_evidence_kernel.py` asserts the anchoring
# structurally, so a new pattern cannot quietly reintroduce that hole.
#
# EVERY pattern here must also carry NO FREE FIELD ANYWHERE, not merely none
# at the end. A section rule was admitted here as `-{2,}[\w .-]{0,120}-{2,}$`:
# end-anchored, and still a 120-character free-text window returned byte for
# byte. An append-only guard could not see it, because appending breaks the
# required trailing dashes and the tainted line simply stops matching. The
# rule now lives in `_SECTION_RULE_RE` below, where its body is scrubbed.
_STRUCTURED_VERBATIM = (
    re.compile(r"^\s*$"),
    re.compile(r"^\s*-{3,}\s*$"),                               # a bare rule
    re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@\s*$"),     # diff hunk coords
    re.compile(r"^\s*Traceback \(most recent call last\):\s*$"),
)

# `---- <subject> FAIL details ----`. The dashes are generated and the body is
# not: `---- share FAIL details ----` and `---- the client hated it ----` are
# the same shape, so the body is a free field and is scrubbed like any other.
# Four dashes rather than two keeps this rule disjoint from a unified diff's
# `---` header and from a removed `-` payload line, which have their own rules.
_SECTION_RULE_RE = re.compile(
    r"^(?P<indent>\s*)(?P<open>-{4,})(?P<body>.*?)(?P<close>-{4,})(?P<trail>\s*)$"
)

# Tier 2 matches only a GENERATED PREFIX. What follows it is content the
# aggregator interpolated — a harness name, a verdict, counters, a heartbeat's
# subject list — and is scrubbed exactly like a marker's suffix. A progress
# line whose suffix classifies as ordinary comes back byte-identical; one that
# does not keeps its prefix and loses its detail. The cost is legibility on a
# heartbeat naming many harnesses; the alternative is the disclosure path this
# split exists to close.
_STRUCTURED_PREFIX = (
    re.compile(r"^\[[ 0-9]+/[0-9]+\](?=\s)"),                   # progress lines
    re.compile(r"^\[cctally-test-all\](?=\s)"),                 # progress banners
    re.compile(r"^(?:passed|failed):\s*\d+"),
    re.compile(r"^Total:(?=\s)"),
    re.compile(r"^Timing:(?=\s)"),
    re.compile(r"^Verdict:(?=\s)"),
)

_DIFF_HEADER_RE = re.compile(r"^(?P<mark>---|\+\+\+)\s+(?P<path>\S+)")
_PYTEST_NODE_RE = re.compile(r"^(?P<head>(?:FAILED|ERROR)\s+)(?P<node>[\w./-]+::\S+)")
_PYTEST_SHORT_FRAME_RE = re.compile(
    r"^(?P<path>[\w./-]+):(?P<line>\d+):\s*"
    r"(?P<cls>[A-Za-z_][\w.]*(?:Error|Exception|Warning))\s*$"
)
_PYTEST_ASSERTION_RE = re.compile(
    r"^(?P<head>\s*[>E]\s+(?:assert|raise)\b)(?P<rest>.*)$"
)
_TRACEBACK_RE = re.compile(
    r'^(?P<indent>\s*)File "(?P<path>[^"]+)", line (?P<line>\d+), in (?P<fn>\S+)\s*$'
)
# A traceback retains the exception class but never the message, which is
# free-form text an assertion may have built out of production values.
_EXCEPTION_RE = re.compile(
    r"^(?P<indent>\s*)(?P<cls>[A-Za-z_][\w.]*(?:Error|Exception|Warning))\s*:\s*"
    r"(?P<msg>\S.*)$"
)
# Built from the marker vocabularies rather than restated, so the retained
# prefix and `classify_failure_marker` can never disagree about what a marker
# is. They did: the vocabulary requires `WARN:` and a restated rule accepted a
# bare `WARN`, which handed free-form text a retained prefix it had not earned.
_MARKER_PREFIX_RE = re.compile(
    r"^(?P<indent>\s*)(?P<prefix>"
    + "|".join(HARD_MARKERS + SUPPLEMENTAL_MARKERS)
    + r")(?P<sep>:?)(?P<rest>.*)$"
)
# A deliberately tiny disclosure lane for the hermetic remote-wrapper harness.
# The case name is not free text: the private caller injects the exact set of
# `case_*` functions present in the tracked harness source.  The numeric source
# line carries no repository or host content and makes the fixed `top-level`
# fallback useful for older assertions that are not grouped in a function.
_TEST_REMOTE_CASE_RE = re.compile(
    r"^(?P<head>CASE:\s+test-remote/)(?P<case>case_[A-Za-z0-9_]+|top-level)"
    r"(?P<tail>\s+line\s+[1-9][0-9]*)$"
)
# ANY `<segment>/<segment>` token, routed through the predicate. This rule used
# to enumerate five known-public top-level directories, which left every other
# one — `.githooks/`, `.agentmem/`, `scripts/`, `homebrew/`, `telemetry/` —
# emitted verbatim. Enumerating what is safe cannot cover what nobody listed,
# which is the same inversion the validator's absolute-path leg already had to
# make. The final segment must carry a letter, or `38/56` in a progress line
# would read as a path and a counter would be redacted.
_REPO_REL_RE = re.compile(
    r"(?<![\w/.-])(?P<rel>(?:[\w.-]+/)+[\w.-]*[A-Za-z][\w.-]*)"
)
# Any absolute path that survived root substitution is unknown territory. One
# character after the slash is enough: a two-character floor left `<home>/a`
# intact, which the validator then reads as an unsubstituted absolute path and
# refuses the whole export over.
_ABS_PATH_RE = re.compile(r"(?<![<\w])(?P<abs>/[\w./-]+)")
# Both rules in ONE scan, absolute first. Two independent `re.sub` passes cannot
# report where they acted in the text they produced, and the caller needs
# exactly that: see `_substitute_paths`.
_PATH_SCAN_RE = re.compile(f"{_ABS_PATH_RE.pattern}|{_REPO_REL_RE.pattern}")

# The ordinariness tests. A residue is admitted only when nothing in it looks
# like an opaque blob or a structured payload, nothing in it is embedded free
# text, and every word in it is one this repository can vouch for.
# The em and en dashes are in the class because this estate writes them in its
# OWN generated diagnostics — `bin/_lib-golden-diff.sh` separates the IO-error
# clause with one, and so do the aggregator's banner and heartbeat lines. Their
# absence redacted those lines for the punctuation alone, independently of
# whether every word in them was vouched for, so registering the words would
# not have made the line legible. This class is a coarse "is this structured
# output" test; the word-by-word provenance check below is what decides.
_SAFE_LINE_RE = re.compile("^[\\s\\w.,:;=/()\\[\\]{}<>@%+*#!?'\"|~^&$–—-]*$")
_OPAQUE_RUN_RE = re.compile(r"[A-Za-z]{40,}")
_JSON_KEY_RE = re.compile(r'"\s*[A-Za-z_][\w.-]*"\s*:')
_QUOTED_RE = re.compile(r'"([^"]*)"')
# UNICODE letters, not Latin ones. `_SAFE_LINE_RE` admits `\w`, which is
# Unicode-aware, so a Latin-only word rule inverted default-deny for every
# non-Latin script: `FAIL diff: клиент diverged` matched no word run at all,
# `unknown_vocabulary` therefore found nothing unknown, and the line came back
# byte for byte. The class excludes digits and underscore so that a counter is
# still not a word.
_WORD_RUN_RE = re.compile(r"[^\W\d_]+")
MIN_QUOTED_FREE_TEXT = 12


class ScrubContext:
    """Everything the transformer needs, injected rather than discovered.

    `roots` maps a token name to an absolute prefix.

    `is_public_path` is a callable taking a repository-relative path and
    returning True when the path may be disclosed; None means no path may
    be, which is the fail-closed default the published tree runs under.

    `known_tokens` is the same shape applied to VOCABULARY: the set of words
    this repository can vouch for, supplied by the caller from closed,
    repo-committed sources. None means no word is vouched for, so every
    alphabetic token is unknown and the text carrying it is redacted. That
    is the same fail-closed default, for the same reason.
    """

    def __init__(
        self,
        roots=None,
        is_public_path=None,
        known_tokens=None,
        known_case_ids=None,
    ):
        self.roots = dict(roots or {})
        self.is_public_path = is_public_path
        self.known_tokens = (
            None if known_tokens is None
            else frozenset(str(token).lower() for token in known_tokens)
        )
        self.known_case_ids = (
            None if known_case_ids is None
            else frozenset(str(case_id) for case_id in known_case_ids)
        )
        # One alternation, longest prefix first, so /Users/x/.claude does not
        # lose to /Users/x. Compiled once here rather than rebuilt per line.
        items = sorted(
            ((name, prefix) for name, prefix in self.roots.items() if prefix),
            key=lambda kv: len(kv[1]),
            reverse=True,
        )
        self._root_names = {"g%d" % i: name for i, (name, _) in enumerate(items)}
        self._root_pattern = (
            re.compile(
                "|".join(
                    "(?P<g%d>%s)" % (i, re.escape(prefix))
                    for i, (_, prefix) in enumerate(items)
                )
            )
            if items
            else None
        )

    def path_is_public(self, rel: str) -> bool:
        return self.is_public_path is not None and bool(self.is_public_path(rel))

    def token_is_known(self, word: str) -> bool:
        return self.known_tokens is not None and word.lower() in self.known_tokens

    def case_id_is_known(self, case_id: str) -> bool:
        return self.known_case_ids is not None and case_id in self.known_case_ids


# The reduction is a SEGMENT rewrite, not three successive text rewrites.
# Every pass acts only on the segments no earlier pass decided about, and every
# segment it writes is marked decided. `_reduce` then reports the exact spans
# this module produced, MEASURED rather than re-derived from how the output
# looks.
#
# What that replaces: `unknown_vocabulary` used to re-discover placeholders in
# the finished text with a `<[a-z][a-z0-9-]*>` scan, so a production token that
# arrived already shaped like `<acme-holdings>` inherited the exemption and its
# words were never judged at all. Deciding by shape rather than by provenance
# is the same defect class this kernel has now carried three times — the
# section rule's free-text window, the digit-touching letter run, and this —
# so it is fixed as the class: the only text exempt from the vocabulary check
# is text this module itself wrote, at the position it wrote it.


def _rewrite_segments(segments, rewrite):
    out = []
    for decided, text in segments:
        if decided:
            out.append((decided, text))
        else:
            out.extend(rewrite(text))
    return out


def _scan_segments(text: str, pattern, render):
    """Split `text` on `pattern`; `render(match)` supplies the decided text."""
    out = []
    pos = 0
    for match in pattern.finditer(text):
        if match.start() > pos:
            out.append((False, text[pos:match.start()]))
        out.append((True, render(match)))
        pos = match.end()
    if pos < len(text) or not out:
        out.append((False, text[pos:]))
    return out


def _substitute_roots(segments, ctx: "ScrubContext"):
    if ctx._root_pattern is None:
        return segments
    return _rewrite_segments(
        segments,
        lambda text: _scan_segments(
            text,
            ctx._root_pattern,
            lambda match: "<%s>" % ctx._root_names[match.lastgroup],
        ),
    )


def _substitute_typed(segments):
    for pattern, token in _TYPED_PATTERNS:
        segments = _rewrite_segments(
            segments,
            lambda text, p=pattern, t=token: _scan_segments(
                text, p, lambda match, t=t: t
            ),
        )
    return segments


def _substitute_paths(segments, ctx: "ScrubContext"):
    """Replace or admit every path-shaped token.

    A repository-relative path the predicate admitted and the `<path>` written
    everywhere else are BOTH decided, so the caller skips exactly those regions
    when it judges words instead of inferring "this was already handled" from
    the presence of a slash. That inference was wrong in both directions of the
    rule: `_REPO_REL_RE` requires a final segment carrying a letter, so a
    single segment with a trailing slash matched nothing, and the absolute-path
    lookbehind fails when the slash follows a word character — so
    `acme-holdings/` had been decided about by neither rule and was skipped by
    the vocabulary check anyway. `ls -d`, `find -type d`, `rsync` and `cwd`
    reporting all emit that shape.
    """
    def render(match):
        rel = match.group("rel")
        return rel if (rel is not None and ctx.path_is_public(rel)) else "<path>"

    return _rewrite_segments(
        segments, lambda text: _scan_segments(text, _PATH_SCAN_RE, render)
    )


def _reduce(text: str, ctx: "ScrubContext"):
    """`(reduced_text, decided_spans)` — roots, then typed values, then paths."""
    segments = _substitute_paths(
        _substitute_typed(_substitute_roots([(False, text)], ctx)), ctx
    )
    pieces = []
    spans = []
    length = 0
    for decided, piece in segments:
        pieces.append(piece)
        if decided:
            spans.append((length, length + len(piece)))
        length += len(piece)
    return "".join(pieces), tuple(spans)


def _reduce_roots(text: str, ctx: "ScrubContext") -> str:
    """Root substitution alone, for a caller that then applies its own rule."""
    return "".join(
        piece for _, piece in _substitute_roots([(False, text)], ctx)
    )


def unknown_vocabulary(text: str, ctx: "ScrubContext", decided_spans=()):
    """Alphabetic words in `text` that the caller's vocabulary cannot vouch for.

    PROVENANCE, not linguistics. `acme-holdings-billing` and
    `case-floor-unmet` are lexically identical, so no rule about how a token
    LOOKS can separate them, and tuning such a rule moves the boundary
    without closing the class. The durable difference is that one of them
    comes from this repository and the other does not, which is a fact the
    caller holds and this module takes as an injection — the same shape as
    `is_public_path`, applied to words instead of paths.

    Default-deny, so the rule never has to enumerate what is dangerous.
    Numbers, punctuation and this module's own placeholders are admissible
    without an entry; a UNIT is not, and is registered in the caller's
    vocabulary like any other word. A rule exempting every letter run that
    touched a digit lived here, and it was a rule about how a token looks:
    `q3` and `acme2026` are indistinguishable to it, project directories and
    branch names carry digits routinely, and `acme2026merger`, `2acme` and
    `acme3` all came back byte-identical because of it.

    `decided_spans` are the regions `_reduce` produced — an admitted path, a
    `<path>`, a `<home>`, a `<credential>` — skipped because re-judging text
    this module itself wrote would redact exactly what the predicate cleared.
    They are POSITIONS the reduction measured, never a shape this function
    re-derives from the output.
    """
    skip = [(int(low), int(high)) for low, high in (decided_spans or ())]
    unknown = []
    for match in _WORD_RUN_RE.finditer(text):
        start, end = match.span()
        if any(low <= start and end <= high for low, high in skip):
            continue
        if not ctx.token_is_known(match.group(0)):
            unknown.append(match.group(0))
    return unknown


def _has_quoted_free_text(text: str) -> bool:
    for match in _QUOTED_RE.finditer(text):
        body = match.group(1)
        if len(body) >= MIN_QUOTED_FREE_TEXT and re.search(r"\s", body):
            return True
    return False


def _is_ordinary(text: str, ctx: "ScrubContext", decided_spans=()) -> bool:
    if not _SAFE_LINE_RE.match(text):
        return False
    if _OPAQUE_RUN_RE.search(text):
        return False
    if _JSON_KEY_RE.search(text):
        return False
    if _has_quoted_free_text(text):
        return False
    return not unknown_vocabulary(text, ctx, decided_spans)


def _looks_like_json(text: str) -> bool:
    stripped = text.strip()
    return stripped.startswith("{") and stripped.endswith("}")


def _scrub_free_field(text: str, ctx: "ScrubContext"):
    """`(safe_text, ok)` for any field the aggregator interpolated into a
    generated shape — a marker's suffix, a progress line's tail, a section
    rule's body. The shape vouches for itself and for nothing inside it."""
    reduced, spans = _reduce(text, ctx)
    if _is_ordinary(reduced, ctx, spans):
        return reduced, True
    return UNCLASSIFIED_DETAIL, False


def _scrub_generated_suffix(rest: str, ctx: "ScrubContext") -> str:
    """Everything after a generated prefix, reduced and then classified.

    A generated prefix vouches for itself and for nothing that follows it.
    Both the marker family and the progress family interpolate values the
    aggregator was handed, so both route their suffix through here.
    """
    text, ok = _scrub_free_field(rest, ctx)
    return text if ok else f" {UNCLASSIFIED_DETAIL}"


def scrub_line(line, ctx: "ScrubContext") -> str:
    """One line in, one safe line out. Fails closed."""
    if line is None:
        return UNCLASSIFIED_PLACEHOLDER
    raw = line.rstrip("\n")

    case_ref = _TEST_REMOTE_CASE_RE.match(raw)
    if case_ref:
        if ctx.case_id_is_known(case_ref.group("case")):
            return raw
        return case_ref.group("head") + UNCLASSIFIED_DETAIL

    for pattern in _STRUCTURED_VERBATIM:
        if pattern.match(raw):
            return raw

    rule = _SECTION_RULE_RE.match(raw)
    if rule:
        body, ok = _scrub_free_field(rule.group("body"), ctx)
        if not ok:
            body = f" {UNCLASSIFIED_DETAIL} "
        return (
            f"{rule.group('indent')}{rule.group('open')}{body}"
            f"{rule.group('close')}{rule.group('trail')}"
        )

    for pattern in _STRUCTURED_PREFIX:
        head = pattern.match(raw)
        if head:
            return raw[: head.end()] + _scrub_generated_suffix(
                raw[head.end():], ctx
            )

    if _looks_like_json(raw):
        # DELIBERATE DEVIATION from the spec, not an oversight. The spec
        # reduces JSON carrying transcript-shaped fields to a diagnostic-key
        # allowlist and redacts an unrecognised schema whole. Nothing in this
        # estate emits a diagnostic schema, so there is no allowlist to reduce
        # against and guessing one would be the permissive half of the rule
        # without the evidence to support it. Redacting every JSON line whole
        # is strictly more conservative than the spec requires. Revisit when a
        # producer of a documented diagnostic schema actually exists.
        return JSON_PLACEHOLDER

    header = _DIFF_HEADER_RE.match(raw)
    if header:
        # Unified-diff file headers survive path normalization, never verbatim.
        # The first token was returned whole, so `--- acme-holdings-billing`
        # rode out on the header marker's authority as an unrecognised single
        # token. A token this kernel cannot vouch for is not a path it can
        # disclose, and a `---` line carrying one is a removed payload line
        # rather than a header, so it takes the payload rule's outcome.
        shown, spans = _reduce(header.group("path"), ctx)
        if unknown_vocabulary(shown, ctx, spans):
            return UNCLASSIFIED_PLACEHOLDER
        return f"{header.group('mark')} {shown}"

    node = _PYTEST_NODE_RE.match(raw)
    if node:
        path, _, name = node.group("node").partition("::")
        name = re.sub(r"\[.*\]$", "[<param>]", name)
        if not ctx.path_is_public(path):
            path = "<path>"
        return f"{node.group('head')}{path}::{name}"

    short_frame = _PYTEST_SHORT_FRAME_RE.match(raw)
    if short_frame:
        path = short_frame.group("path")
        if not ctx.path_is_public(path):
            path = "<path>"
        return (
            f"{path}:{short_frame.group('line')}: "
            f"{short_frame.group('cls')}"
        )

    assertion = _PYTEST_ASSERTION_RE.match(raw)
    if assertion:
        return assertion.group("head") + _scrub_generated_suffix(
            assertion.group("rest"), ctx
        )

    tb = _TRACEBACK_RE.match(raw)
    if tb:
        shown = _reduce_roots(tb.group("path"), ctx)
        rel = shown.split(">/", 1)[-1] if shown.startswith("<") else shown
        shown = rel if ctx.path_is_public(rel) else "<path>"
        return (
            f'{tb.group("indent")}File "{shown}", line {tb.group("line")}, '
            f'in {tb.group("fn")}'
        )

    exc = _EXCEPTION_RE.match(raw)
    if exc:
        return f"{exc.group('indent')}{exc.group('cls')}: {EXCEPTION_MESSAGE_PLACEHOLDER}"

    marker = _MARKER_PREFIX_RE.match(raw)
    if marker:
        # The prefix is generated text and safe; the suffix interpolates
        # arbitrary values and is not, so it is scrubbed like any other
        # content and redacted whole when it cannot be classified.
        return (
            f"{marker.group('indent')}{marker.group('prefix')}"
            f"{marker.group('sep')}"
            f"{_scrub_generated_suffix(marker.group('rest'), ctx)}"
        )

    if raw[:1] in ("+", "-", " "):
        # Diff payload without provenance: an added, a removed OR a context
        # line. The spec admits one only where provenance establishes both a
        # public committed expected file and a fixture-derived actual file.
        # DEVIATION, recorded deliberately: `scrub_line` classifies one line
        # at a time and cannot see the diff's file headers, so it has no
        # provenance to establish and fails closed for all three. The cost is
        # that an indented ordinary line is indistinguishable from a context
        # line here and is redacted with it. Coordinates were admitted above.
        return UNCLASSIFIED_PLACEHOLDER

    candidate, spans = _reduce(raw, ctx)
    if _is_ordinary(candidate, ctx, spans):
        return candidate
    return UNCLASSIFIED_PLACEHOLDER


def normalize_clusters(clusters, total: int):
    """`(start, end, marker_index)` triples, sorted, validated against `total`.

    `select_failure_windows` yields dicts and a caller may equally hand in
    plain `(start, end)` pairs; both are accepted, which removes the one place
    the two shapes could be wired up wrong without failing loudly.

    Raises ValueError when a boundary falls outside `[0, total - 1]`. The
    indices address the ORIGINAL log — see `build_scrubbed_extract` — and the
    competing reading, indices into the concatenated windows, produces a
    plausible extract built from the wrong lines rather than an error. Task 6
    is the first caller, so the mis-wiring has to be loud.
    """
    bounds = []
    for cluster in clusters or ():
        if isinstance(cluster, dict):
            start = cluster.get("start")
            end = cluster.get("end")
            marker = cluster.get("marker_index")
        else:
            start, end = cluster
            marker = None
        if start is None or end is None:
            continue
        start, end = int(start), int(end)
        if end < start:
            continue
        if start < 0 or end >= total:
            raise ValueError(
                f"cluster ({start}, {end}) is outside a {total}-line subject; "
                f"cluster indices address the subject's full line list"
            )
        bounds.append((start, end, None if marker is None else int(marker)))
    return sorted(bounds)


def _cluster_slice(cluster, budget: int):
    """`(start, stop)` for at most `budget` lines of one cluster.

    A cluster that fits comes back whole. One that does not is truncated
    AROUND ITS MARKER, keeping as much of the preceding context as fits:
    the marker is the line the cluster exists for, and the forty lines before
    it are the explanation a harness prints ahead of its marker, which is the
    case the window rewrite exists for. With no marker recorded the opening of
    the cluster is kept, for the same reason.
    """
    start, end, marker = cluster
    length = end - start + 1
    if length <= budget:
        return start, end + 1
    if marker is None or not start <= marker <= end:
        return start, start + budget
    context = min(marker - start, budget - 1)
    low = marker - context
    return low, low + budget


def _cluster_selection(total: int, ordered, budget: int):
    """`(head, tail)` index ranges to retain, or None for an absent block.

    With no clusters this is the even split from the ends of the line list,
    the tail taking the odd line. With clusters it selects from the SPANS:
    the latest cluster first, because it is usually the one that ended the
    run, then the earliest if it fits whole in what is left. The earliest is
    whole-or-nothing rather than truncated, because one or two lines of a
    cluster's opening is noise where the whole of the latest cluster is the
    failure.
    """
    if budget <= 0:
        return None, None
    if not ordered:
        head = min(budget // 2, total)
        tail = max(0, min(budget - head, total - head))
        return (0, head), (total - tail, total)
    earliest, latest = ordered[0], ordered[-1]
    tail = _cluster_slice(latest, budget)
    remaining = budget - (tail[1] - tail[0])
    if earliest is latest or earliest[0] >= tail[0]:
        return None, tail
    if earliest[1] - earliest[0] + 1 <= remaining:
        return (earliest[0], earliest[1] + 1), tail
    return None, tail


def build_scrubbed_extract(subject_lines, ctx, alloc, log_names=None, clusters=None):
    """Assemble the extract: one labelled block per subject, every line
    scrubbed, each block reduced to its allocated line count.

    The block header and the `[OMITTED: …]` notice are charged to the budget,
    because they are lines in the emitted file. Emitting them outside it made
    the run-wide cap advisory rather than a cap: fifty-six subjects allocated
    four lines each emitted six each.

    Over budget, the block retains the EARLIEST and the LATEST clusters and
    states how many lines it dropped, naming the log that still holds all of
    them. `clusters` carries the boundaries `select_failure_windows` computed;
    without them this function sees a flat line list and keeps its two ends,
    which retains no part of the failure whenever the clusters sit inside the
    log. `log_names` lets the caller supply the retained log's name per
    subject; the default names it relative to the evidence root, because an
    absolute path in the extract is exactly what the validator refuses.

    INDEX CONTRACT, enforced rather than assumed: `clusters[name]` indexes into
    `subject_lines[name]`, which must therefore be the subject's FULL line list
    — the same list `select_failure_windows` was given, since it returns
    indices into that list. Passing the concatenated windows as
    `subject_lines` while passing whole-log indices as `clusters` is the
    competing reading; it silently builds an extract out of the wrong lines, so
    `normalize_clusters` raises on any boundary outside the list instead.
    """
    log_names = dict(log_names or {})
    clusters = dict(clusters or {})
    out = []
    for name in sorted(subject_lines):
        budget = int(alloc.get(name, 0))
        lines = list(subject_lines[name])
        # Validated before the budget is consulted, so a mis-wired caller fails
        # on every run rather than only on the runs that overflow.
        ordered = normalize_clusters(clusters.get(name), len(lines))
        # One line buys a header with nothing underneath it, which reports
        # nothing the run manifest does not already carry.
        if budget <= 1:
            continue
        out.append(f"---- {name} ----")
        if len(lines) <= budget - 1:
            out.extend(scrub_line(line, ctx) for line in lines)
            continue
        head, tail = _cluster_selection(len(lines), ordered, budget - 2)
        kept = 0
        for block in (head, tail):
            if block is not None:
                kept += block[1] - block[0]
        omitted = len(lines) - kept
        retained = log_names.get(name) or f"logs/{name}.log"
        if head is not None:
            out.extend(scrub_line(line, ctx) for line in lines[head[0]:head[1]])
        out.append(
            f"[OMITTED: {omitted} lines outside the retained blocks; "
            f"full log retained at {retained}]"
        )
        if tail is not None:
            out.extend(scrub_line(line, ctx) for line in lines[tail[0]:tail[1]])
    return out


# ------------------------------------------------------ the independent validator
#
# Deliberately NOT built from the transformer's classifiers. A shared grammar
# means a classifier that wrongly admits unsafe text admits it in both places
# and the check proves nothing. This is a denylist over raw shapes plus an
# independently formulated structural test, and it never calls scrub_line or
# any helper the transformer uses. Do not refactor the two into one helper:
# their disjointness is the property the mutation tests exist to prove.

_FORBIDDEN = (
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("uuid", re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")),
    ("long-hex", re.compile(r"[0-9a-fA-F]{32,}")),
    ("long-b64", re.compile(r"[A-Za-z0-9+/]{40,}={0,2}")),
    ("credential", re.compile(r"(?i)bearer\s+\S|sk-[A-Za-z0-9_-]{8,}|api[_-]?key")),
    ("credential-url", re.compile(r"[a-z]+://[^\s/@]+:[^\s/@]+@")),
    # Stated as "any absolute path", not as a list of known roots. Enumerating
    # roots left most of the filesystem admissible — /root, /etc, /Library,
    # /mnt and /Volumes among them — so a checkout outside the enumerated set
    # was disclosed in full. A leading slash preceded by a placeholder bracket
    # or by a word character is not a path start, which is what keeps
    # `<path>`, `bin/cctally-test-all` and `38/56` admissible.
    ("absolute-path", re.compile(r"(?<![<\w])/[\w.-]")),
    ("json-payload",
     re.compile(r'"(?:content|text|prompt|message|cwd|project|account)"\s*:')),
    ("control-bytes", re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")),
)

# The structural legs, stated as proportions rather than as token counts so
# that they do not restate the transformer's rule in different words. A span
# that is almost entirely letters and spaces, carries no placeholder and no
# number, and runs to several words is prose, and prose is production content
# this estate has no way to vouch for.
#
# The word floor is SIX. A three-space floor rejected `FAIL <name>: <label>
# diverged` — the line `bin/_lib-golden-diff.sh` emits from the chokepoint
# every fixture harness compares through — and rejected an indentation-only
# line as well. Since a failed validation leaves no export file at all, that
# floor deleted the deliverable on essentially every real failing run: it
# traded a disclosure hole for an availability hole, which is what turns a
# detector into an outage. The floor can sit here because the transformer no
# longer decides by how text looks: a suffix is now vouched for word by word
# against the repository's own vocabulary, so this leg catches what a
# transformer bug admits rather than carrying the whole burden alone.
_FREE_TEXT_MIN_WORDS = 6
_FREE_TEXT_MIN_RATIO = 0.95
_QUOTED_SPAN_RE = re.compile(r'"([^"\n]{8,}?)"')


def _prose_ratio(text: str) -> float:
    return sum(1 for ch in text if ch.isalpha() or ch == " ") / len(text)


def _looks_like_free_text(text: str) -> bool:
    if not text:
        return False
    if "<" in text and ">" in text:
        return False
    if any(ch.isdigit() for ch in text):
        return False
    # Counted as words rather than as spaces, so a run of indentation is not
    # read as a five-word sentence made entirely of nothing.
    if len(text.split()) < _FREE_TEXT_MIN_WORDS:
        return False
    return _prose_ratio(text) >= _FREE_TEXT_MIN_RATIO


def _looks_like_quoted_prose(text: str) -> bool:
    """The same proportion test applied to a quoted span rather than a line.

    A quoted span survives a line whose own proportions look structural — a
    path in quotes drags the ratio down and a few quoted words hide inside a
    longer diagnostic — so the span is measured on its own terms. A quoted
    path has no internal space and is therefore not reached.
    """
    for body in _QUOTED_SPAN_RE.findall(text):
        if " " not in body:
            continue
        if _prose_ratio(body) >= _FREE_TEXT_MIN_RATIO:
            return True
    return False


def validate_export(lines, roots=None):
    """Violations that must block publication. Empty list means publishable."""
    violations = []
    for idx, line in enumerate(lines):
        text = line if isinstance(line, str) else ""
        reason = None
        # Roots first: an unsubstituted root is the most specific and most
        # actionable diagnosis, and every root is also an absolute path, so
        # checking the general rule first would report the vaguer reason.
        for prefix in (roots or {}).values():
            if prefix and prefix in text:
                reason = "unsubstituted-root"
                break
        if reason is None:
            for name, pattern in _FORBIDDEN:
                if pattern.search(text):
                    reason = name
                    break
        if reason is None and (
            _looks_like_free_text(text) or _looks_like_quoted_prose(text)
        ):
            reason = "free-form-text"
        if reason is not None:
            violations.append({"index": idx, "reason": reason, "excerpt": text[:120]})
    return violations


# ------------------------------------------------------------------ retention

RUN_STATES = ("active", "completed", "aborted", "abandoned")
DEFAULT_MAX_AGE_DAYS = 7
DEFAULT_MAX_BYTES = 1073741824


def reconcile_run_states(runs, live_pids):
    """An `active` run whose owning process is gone becomes `abandoned`.

    Without this an interrupted run stays protected from eviction forever
    and the store's cap becomes unenforceable. The process-start identity is
    compared as well as the pid, because a recycled pid is a different
    process, and a run that recorded no start identity is treated as gone
    rather than as live: with `pid_start` unset both sides of the equality
    are None, so an equality test alone read exactly the runs that recorded
    the least about themselves as the ones worth protecting.
    """
    out = []
    for run in runs:
        item = dict(run)
        state = item.get("state")
        if state not in RUN_STATES:
            raise ValueError(f"unknown run state: {state!r}")
        if state == "active":
            pid = item.get("pid")
            start = item.get("pid_start")
            if pid is None or start is None or live_pids.get(pid) != start:
                item["state"] = "abandoned"
        out.append(item)
    return out


def _coverage_gaps(runs, evicted_ids):
    """Intervals in which no evidence remains.

    A maximal contiguous span of evicted runs is one hole, reported from the
    first record lost to the next record retained. Reporting only the last
    lost record would understate the hole, and a rate computed against that
    boundary would overstate coverage.
    """
    gaps = []
    pending = None
    for run in sorted(runs, key=lambda r: r.get("started_epoch") or 0):
        if run["run_id"] in evicted_ids:
            if pending is None:
                pending = run.get("started_epoch")
        elif pending is not None:
            gaps.append({"from_epoch": pending, "to_epoch": run.get("started_epoch")})
            pending = None
    if pending is not None:
        gaps.append({"from_epoch": pending, "to_epoch": None})
    return gaps


def plan_evidence_evictions(
    runs,
    now_epoch,
    *,
    live_pids,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    max_bytes: int = DEFAULT_MAX_BYTES,
    protect_ids=(),
):
    """Age, then reconcile `active` to `abandoned`, then cap. Passing runs go
    before failing ones.

    `live_pids` is required rather than optional, and reconciliation runs
    here rather than being left to the caller, because the spec fixes this
    order and a caller that reconciled afterwards left a dead-but-`active`
    run protected from eviction for good — which makes the byte cap
    unenforceable, the exact failure the reconciliation exists to prevent.
    Requiring the argument is what makes the ordering unfalsifiable at the
    call site.

    A live `active` run and the just-completed run are never evicted. When
    what remains still exceeds the cap, the store is reported over cap
    rather than the current run being truncated: truncating the evidence a
    caller is about to read is worse than briefly exceeding a disk budget,
    and the next run's eviction pass reclaims it.

    `gaps` and `coverage` describe THIS pass only. Coverage is cumulative
    across the store's whole life, so the caller must persist the intervals
    and report their union; a surface that rendered one pass's intervals as
    the store's coverage would report a store full of holes as complete
    after any pass that evicted nothing.
    """
    runs = reconcile_run_states(runs, live_pids)
    protect = set(protect_ids)
    horizon = now_epoch - (max_age_days * 86400)
    evict, keep = [], []
    for run in runs:
        if run["run_id"] in protect or run.get("state") == "active":
            keep.append(run)
        elif (run.get("finished_epoch") or run.get("started_epoch") or 0) < horizon:
            item = dict(run)
            item["reason"] = "age"
            evict.append(item)
        else:
            keep.append(run)

    def _cap_order(run):
        # 0 for a completed pass, 1 for anything else, then oldest first.
        rank = 0 if run.get("outcome") == "pass" else 1
        return (rank, run.get("started_epoch") or 0)

    total = sum(r.get("bytes", 0) for r in keep)
    for run in sorted(keep, key=_cap_order):
        if total <= max_bytes:
            break
        if run["run_id"] in protect or run.get("state") == "active":
            continue
        keep = [r for r in keep if r["run_id"] != run["run_id"]]
        item = dict(run)
        item["reason"] = "cap"
        evict.append(item)
        total -= run.get("bytes", 0)

    evicted_ids = {r["run_id"] for r in evict}
    gaps = _coverage_gaps(runs, evicted_ids)
    return {
        "evict": evict,
        "keep": sorted(keep, key=lambda r: r.get("started_epoch") or 0),
        "over_cap": total > max_bytes,
        "gaps": gaps,
        "coverage": "degraded" if gaps else "complete",
        "bytes_after": total,
    }


def render_retention_notice(plan) -> str:
    """One operator-facing line. Eviction is never silent, because a store
    that quietly drops evidence while a surface still reports coverage is
    the exact failure class this session exists to remove."""
    evict = plan.get("evict") or []
    if not evict:
        return ""
    passes = sum(1 for r in evict if r.get("outcome") == "pass")
    fails = len(evict) - passes
    total = sum(r.get("bytes", 0) for r in evict)
    reasons = sorted({r.get("reason") for r in evict if r.get("reason")})
    reason_clause = f"; reasons: {', '.join(reasons)}" if reasons else ""
    tail = " (store still over cap)" if plan.get("over_cap") else ""
    return (
        f"EVIDENCE EVICTED: {len(evict)} runs, {total} bytes "
        f"({passes} pass, {fails} fail){reason_clause}; "
        f"{len(plan.get('gaps') or [])} coverage gaps{tail}"
    )
