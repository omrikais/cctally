"""Evidence kernel for the authoritative test gate (#529 S2).

Pure functions only. No I/O beyond what a caller hands in, no environment
reads except the explicit `env` mappings passed as arguments, and no
knowledge of SSH, runner aliases, lock layout, the event ledger, or the
public-mirror grammar. Everything private is injected by the caller.

Published in the public tree by design, because the aggregator imports it.
That import is not written yet — this module lands ahead of the aggregator
work that consumes it — so treat the dependency as the reason for the
placement rather than as a description of the current tree.

ONE exception to "pure functions only", recorded rather than quietly taken:
`merge_duration_legs` (#630 S1, F3) reads the two per-leg duration files and
publishes the merged artifact. It lives here because it is evidence-artifact
handling, which this module already owns, and because the aggregator's own
embedded Python is not importable — logic placed there could only ever be
tested end to end. Its decision logic is factored into `_merge_duration_plan`,
which is pure and takes the decoded legs.
"""
from __future__ import annotations

import gzip
import json
import os
import re
import zlib

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
    # `token` is split out of the alternation below because it is ordinary
    # English in this codebase — a search needle, a lexer token, a token count
    # — while `bearer`, `api_key` and `authorization` are not. With the
    # separator optional, `token for` matched with no separator at all and the
    # whole line was replaced (#812). A literal `:` or `=` is now required.
    # Newly admitted, and accepted: every former match lacking a separator,
    # such as `token status` or a bare `token=`.
    #
    # THE ENTRY ENUMERATES FIVE SPELLINGS, and `_` being a word character is the
    # whole reason it has to (#820). `\btoken\b` never matches between a prefix
    # and `token`, and the alternation above fails at the boundary AFTER
    # `bearer` for the same reason, so `access_token`, `refresh_token`,
    # `id_token` and `bearer_token` matched NEITHER surface and published their
    # values in full. The four are enumerated here rather than moved into the
    # optional-separator alternation above, and rather than generalized to
    # `\w+_token`: either widening recreates the over-redaction #812 exists to
    # remove, and `api_token`, `csrf_token` and every other `\w+_token` form
    # stay out for that reason.
    #
    # `id_token` is in the set because it is a real credential in this
    # repository rather than incidental vocabulary: `bin/build-bench-fixtures.py`
    # writes the official Codex `auth.json` shape as `{"id_token": token,
    # "access_token": "a", "refresh_token": "r"}`, `bin/_lib_accounts.py`
    # decodes it to obtain the account email, and `bin/_lib_codex_hooks.py`
    # reads it from the live `auth.json`. Covering two names from one JSON
    # object and not the third would be an arbitrary boundary on a privacy
    # surface.
    (re.compile(r"(?i)\b(?:bearer|api[_-]?key|authorization)\b\s*[:=]?\s*\S+"),
     "<credential>"),
    (re.compile(r"(?i)\b(?:token|(?:access|refresh|id|bearer)_token)\b"
                r"\s*[:=]\s*\S+"), "<credential>"),
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
    re.compile(r"^[ \t]*$"),
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
# The tail is OPTIONAL and the rule is deliberately NOT end-anchored, so a
# summary line whose tail is prose the class vocabulary does not recognise
# still retains its node id and simply loses the tail. Anchoring the rule
# would drop the whole line to the marker path instead, which is the very
# outcome the indent tolerance exists to remove. Leading whitespace is
# tolerated because the aggregator prints the pytest summary block indented
# by two spaces, and `_MARKER_PREFIX_RE` would otherwise claim it.
# The node group closes its own parameter span rather than running to the
# first whitespace. `\S+` truncated a parameter id containing a space —
# `test_reason[elapsed_hours<24-less than 24 hours into the week]` became
# `test_reason[elapsed_hours<24-less` — and the normalizer below could not
# recognise the truncated form, so a fragment of an unnormalized test
# parameter was retained. 309 parametrize argument constants in `tests/` carry
# a space, so the shape is reachable with today's estate.
_PYTEST_NODE_RE = re.compile(
    r"^(?P<indent>\s*)(?P<head>(?:FAILED|ERROR)\s+)"
    r"(?P<node>[\w./-]+::[^\s\[]+(?:\[[^\]]*\])?)"
    r"(?:\s+-\s+(?P<cls>[A-Za-z_][\w.]*(?:Error|Exception|Warning))\s*:\s*(?P<msg>.*))?"
)
# The `E ` gutter carries no payload of its own: it is stripped and the body
# is re-dispatched through the same rule table ONCE, so the body must classify
# on its own terms. The `>` gutter is excluded because those lines are source.
_PYTEST_GUTTER_RE = re.compile(r"^(?P<indent>\s*)(?P<gutter>E\s+)(?P<body>\S.*)$")
# pytest's own counters line. Every token is generated: the integers are
# counts, the words come from a closed set pytest owns, and the duration is
# formatted by `format_session_duration`, which appends ` (H:MM:SS)` once a
# session runs a minute or longer — which every authoritative pytest leg does,
# so omitting that form would leave the rule dead on the only line it exists
# for. Nothing here interpolates repository content, so the rule is policy
# independent and needs neither a path predicate nor a vocabulary.
_PYTEST_COUNT_WORD = (
    r"passed|failed|error|errors|skipped|xfailed|xpassed"
    r"|deselected|warning|warnings|rerun|reruns"
)
_PYTEST_COUNTERS_RE = re.compile(
    r"^\s*=*\s*"
    r"\d+\s+(?:" + _PYTEST_COUNT_WORD + r")"
    r"(?:,\s*\d+\s+(?:" + _PYTEST_COUNT_WORD + r"))*"
    r"\s+in\s+(?:\d+m\s+)?[\d.]+s"
    r"(?:\s+\(\d+:\d{2}:\d{2}\))?"
    r"\s*=*\s*$"
)
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
# make. Every directory segment must either contain a word character or be `.`
# / `..`; otherwise the `-` before `/tmp` in `${TMPDIR:-/tmp}` wins the combined
# scan one character before the absolute-path alternative and misclassifies
# `-/tmp` as a public relative path. The final segment must carry a letter, or
# `38/56` in a progress line would read as a path and a counter would be
# redacted.
_REPO_REL_RE = re.compile(
    r"(?<![\w/.-])(?P<rel>"
    r"(?:(?:\.{1,2}|(?=[\w.-]*\w/)[\w.-]+)/)+"
    r"[\w.-]*[A-Za-z][\w.-]*)"
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
_SAFE_LINE_RE = re.compile("^[ \\t\\w.,:;=/()\\[\\]{}<>@%+*#!?'\"|~^&$–—-]*$")
_OPAQUE_RUN_RE = re.compile(r"[A-Za-z]{40,}")
_JSON_KEY_RE = re.compile(r'"\s*[A-Za-z_][\w.-]*"\s*:')
# Python repr uses single quotes for the common case. Boundaries on that arm
# distinguish a repr delimiter from the apostrophe inside `runner's`.
_QUOTED_RE = re.compile(
    r'''(?:"([^"\n]*)"|(?<![\w])'([^'\n]*)'(?![\w]))'''
)
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
        body = match.group(1) if match.group(1) is not None else match.group(2)
        if len(body) >= MIN_QUOTED_FREE_TEXT and re.search(r"\s", body):
            return True
    return False


# THE VALUE POSITION AFTER AN ENUMERATED TOKEN-CREDENTIAL PREFIX, and the
# placeholder shape, for the provenance check below (#821). The prefix is the
# transformer's own word set and separator, spelled here a third time rather
# than composed out of `_TYPED_PATTERNS`, because that entry consumes the whole
# value and so cannot report where the value begins.
# `test_both_surfaces_spell_the_same_token_word_set` compares all four live
# spellings and is what stops them drifting apart.
_TOKEN_VALUE_PREFIX_RE = re.compile(
    r"(?i)\b(?:token|(?:access|refresh|id|bearer)_token)\b\s*[:=]"
)
# EITHER CASE, deliberately. This kernel writes placeholder names in lowercase
# only, so an uppercase-named span can never be one this module wrote and can
# never be covered by a decided span — which means the rule below reads exactly
# as "a placeholder-shaped span this module did not write" rather than as a
# second rule about how a name is spelled. A lowercase-only class would be a
# shape rule of the kind this file records as a repeated defect class, and it
# would leave `token=<repo><A90210904812340981234>` publishing its twenty digits
# on the strength of the validator's backstop alone.
_PLACEHOLDER_SHAPE_RE = re.compile(r"<[A-Za-z][A-Za-z0-9-]*>")


def _undecided_placeholder_in_a_token_value(text: str, decided_spans=()) -> bool:
    """A placeholder-shaped span in a token value that this module did not write.

    PROVENANCE, not shape, and it is the same rule `unknown_vocabulary` already
    applies to words: the only text exempt is text this module itself wrote, at
    the position it wrote it. `decided_spans` are the positions `_reduce`
    MEASURED, never a shape re-derived from the output.

    WHAT IT CLOSES. A secret whose own bytes are placeholder-shaped published in
    full whenever a substitution consumed the value's head: the transformer
    emitted `token=/repo<secret-90210904812340981234>` as
    `token=<repo><secret-90210904812340981234>`, because the token entry then
    found no value after `token=` and the tail's only alphabetic word is one the
    repository vouches for. No shape rule can separate those bytes from a
    placeholder this kernel wrote, because only provenance distinguishes them.

    SCOPED TO THE VALUE POSITION, NEVER APPLIED TO THE WHOLE LINE, and the
    scoping is load-bearing rather than cautious. `bin/cctally-migrations-test`
    emits `echo "$db_label: <missing>"`, the golden at
    `tests/fixtures/migrations/01-fresh-install/expected.txt` retains
    `stats.db: <missing>`, and `_reduce` neither creates nor decides that span.
    A global rule would newly redact a safe public diagnostic that a committed
    golden pins.

    LINEAR IN THE SCAN, AND THE CONTAINMENT TEST IS NOT PART OF THAT BOUND.
    Each prefix's walk stops at the first byte that is neither whitespace nor a
    complete placeholder span, and a prefix can never sit inside a span —
    `<token>` offers no `[:=]` after the word — so the walks are disjoint and
    every byte is visited at most once. Each span found is then compared against
    every decided span, so the total is the line length plus the product of the
    two span counts. `MAX_LINE_BYTES` bounds both factors, which is why that
    product is affordable; it is not why it is absent.
    """
    spans = tuple((int(low), int(high)) for low, high in (decided_spans or ()))
    for prefix in _TOKEN_VALUE_PREFIX_RE.finditer(text):
        position = prefix.end()
        while position < len(text):
            if text[position].isspace():
                position += 1
                continue
            span = _PLACEHOLDER_SHAPE_RE.match(text, position)
            if span is None:
                # A raw byte ends the value's placeholder run. What follows is
                # the validator's `token-credential` leg's business, not this
                # check's.
                break
            if not any(
                low <= span.start() and span.end() <= high
                for low, high in spans
            ):
                return True
            position = span.end()
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
    # Provenance BEFORE ordinariness (#821): an undecided placeholder-shaped
    # span carries no alphabetic word the vocabulary check can refuse once its
    # own name is one the repository vouches for, so `_is_ordinary` publishes
    # it. This runs first because it is the stricter question.
    if _undecided_placeholder_in_a_token_value(reduced, spans):
        return UNCLASSIFIED_DETAIL, False
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


def scrub_line(line, ctx: "ScrubContext", *, _in_gutter: bool = False) -> str:
    """One line in, one safe line out. Fails closed.

    `_in_gutter` is the non-recursion guard for the `E ` gutter rule: a
    nested `E   E   x` must treat the inner gutter as ordinary content
    rather than peeling gutters until something classifies.
    """
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

    if _PYTEST_COUNTERS_RE.match(raw):
        # Kept OUT of `_STRUCTURED_VERBATIM` so that tuple stays the set of
        # shapes whose samples the anchoring and canary guards enumerate; this
        # rule carries its own guards.
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
        #
        # PROVENANCE IS NOT CONSULTED ON THIS PATH, deliberately.
        # `_undecided_placeholder_in_a_token_value` guards the two paths that
        # decide by ORDINARINESS — the tail fallback and `_scrub_free_field` —
        # and reaches each one before `_is_ordinary`, which is where a
        # placeholder-shaped secret would otherwise be published. This path
        # never asks that question: it vouches for the header's path word by
        # word and returns the placeholder otherwise, which is already the
        # stricter outcome.
        shown, spans = _reduce(header.group("path"), ctx)
        if unknown_vocabulary(shown, ctx, spans):
            return UNCLASSIFIED_PLACEHOLDER
        return f"{header.group('mark')} {shown}"

    node = _PYTEST_NODE_RE.match(raw)
    if node:
        path, _, name = node.group("node").partition("::")
        # ANY bracket in the name produces the placeholder, closed or not, and
        # EITHER bracket triggers it. The previous `\[.*\]$` required the span
        # to close at end of name, so an unterminated one — the shape a
        # truncated capture leaves — was left standing and its body was
        # published. Firing on `[` alone left the mirrored hole: the node
        # group's `[^\s\[]+` admits `]`, so `test_y]leak[` reached here with
        # its first bracket a CLOSING one and `]leak` was retained. pytest
        # cannot emit that name, but the rule this normalizer stands for is
        # that no fragment of a parameter id is ever retained, and a member of
        # that class the rule does not reach is a hole whatever produced it.
        name = re.sub(r"[\[\]].*$", "[<param>]", name)
        if not ctx.path_is_public(path):
            path = "<path>"
        out = f"{node.group('indent')}{node.group('head')}{path}::{name}"
        if node.group("cls"):
            # The class is retained; the message never is. `msg` is captured
            # only so the tail cannot ride out on the node rule's authority.
            out += f" - {node.group('cls')}: {EXCEPTION_MESSAGE_PLACEHOLDER}"
        return out

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

    gutter = _PYTEST_GUTTER_RE.match(raw)
    if gutter and not _in_gutter:
        inner = scrub_line(gutter.group("body"), ctx, _in_gutter=True)
        return f"{gutter.group('indent')}{gutter.group('gutter')}{inner}"

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
    if _undecided_placeholder_in_a_token_value(candidate, spans):
        return UNCLASSIFIED_PLACEHOLDER
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
#
# ONE SHARED GRAMMAR IS A STATED EXCEPTION TO "independently formulated", and
# it is recorded here rather than only beside itself, because a reader who takes
# the paragraph above as absolute will read the exception as a defect and "fix"
# it. IT NOW HAS FOUR LIVE SPELLINGS, two per surface: the transformer's
# `_TYPED_PATTERNS` entry and `_TOKEN_VALUE_PREFIX_RE`, and on this side the
# `token-credential` entry below and `_TOKEN_VALUE_NAME_PREFIX_RE`.
# `test_both_surfaces_spell_the_same_token_word_set` compares all four, so they
# cannot drift apart one at a time.
# `token-credential`'s prefix
# `(?i:\b(?:token|(?:access|refresh|id|bearer)_token)\b)\s*[:=]` is
# byte-identical to the transformer's
# `(?i)\b(?:token|(?:access|refresh|id|bearer)_token)\b\s*[:=]`, apart from the
# scope of the case-insensitivity flag, and the equality of the WORD SET and the
# SEPARATOR is deliberate: a validator whose separator scope is narrower than the
# transformer's misses regressions the transformer would publish, and one that
# is broader refuses exports the transformer legitimately published. Both
# failures were measured, and the comment beside the entry records them. What
# stays disjoint is the part that decides safety — the VALUE class — and the
# independence of the leg as a whole is carried by
# `test_the_validator_catches_a_transformer_regression_independently`, which
# deletes the transformer's token entry and requires this leg to refuse the
# credential anyway. No other entry in this tuple shares any text with a
# transformer classifier, and a new one should not.
#
# THE FOURTH SPELLING SITS BELOW THIS TUPLE. `_token_value_names_an_unknown_
# placeholder` reaches the same `token-credential` verdict from outside
# `_FORBIDDEN`, so a count taken from the tuple alone reads one short. It is
# named here so that this paragraph can be read on its own.

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
    # was disclosed in full. A leading slash preceded by a placeholder bracket,
    # a word character or a dot is not an absolute-path start. The dot exclusion
    # keeps `./public` and `../public` admissible alongside `<path>`,
    # `bin/cctally-test-all` and `38/56`.
    ("absolute-path", re.compile(r"(?<![<\w.])/[\w.-]")),
    ("json-payload",
     re.compile(r'"(?:content|text|prompt|message|cwd|project|account)"\s*:')),
    ("control-bytes", re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")),
    # THIS ENTRY IS LAST IN THE TUPLE, AND ITS POSITION IS PART OF THE RULE.
    # `validate_export` reports the FIRST leg that matches a line, and
    # `token-credential` is the only reason in this tuple that
    # `PER_LINE_VIOLATION_REASONS` contains; every other entry refuses the whole
    # export. Matched earlier, this leg would report its own reason for a line
    # that also violates a wholesale leg, `apply_validation_redactions` would
    # then replace that single line, and the export would publish — so the
    # wholesale leg's refusal would be downgraded to one replaced line without
    # anybody deciding to downgrade it. Measured with this entry at its former
    # position between `credential` and `credential-url`: a line reading
    # `token: value`, byte 0x01, then more text reported `token-credential`
    # rather than `control-bytes`, and `token=/etc/passwd` reported
    # `token-credential` rather than `absolute-path`. Two reviews classified
    # those two misreports as a diagnosis-quality defect with no effect on
    # safety, and that classification was correct only while this leg refused
    # wholesale, because both reasons withheld the export either way. A leg
    # added to this tuple later belongs ABOVE this entry unless its reason is
    # filed in `PER_LINE_VIOLATION_REASONS` as well. Pinned by
    # `test_a_wholesale_leg_is_not_shadowed_by_the_token_leg` and
    # `test_every_wholesale_denylist_leg_precedes_every_per_line_leg`.
    #
    # The WORD SET, the `[:=]` class and the `\s` whitespace class are the
    # transformer's own, and the five spellings are enumerated in the same order
    # here as there (#820): a prefixed spelling the transformer reduces and this
    # leg cannot see is a regression nothing catches, and one this leg sees and
    # the transformer does not reduce refuses the kernel's own output.
    # The equality is deliberate: a validator whose separator scope is
    # NARROWER than the transformer's misses regressions the transformer would
    # publish, and one that is BROADER refuses exports the transformer
    # legitimately published. An earlier form spelled the whitespace class
    # `[ \t]` and claimed independence from that wording while reusing `[:=]`
    # verbatim; the claim was wrong, and `token\xa0=\xa0SECRET` was matched by
    # the transformer and by no leg here. Independence is carried by
    # `test_the_validator_catches_a_transformer_regression_independently`, which
    # deletes the transformer's token entry, confirms the value actually leaks,
    # and requires this leg to refuse it anyway. The comment above `_FORBIDDEN`
    # records this equality as the tuple's one stated exception to
    # "independently formulated", so the two comments state the same fact.
    #
    # THE CASE-INSENSITIVITY FLAG IS SCOPED TO THE WORD SET,
    # `(?i:\b(?:token|(?:access|refresh|id|bearer)_token)\b)` rather
    # than a leading `(?i)`, and the scope is the only place this prefix diverges
    # from the transformer's. A leading `(?i)` applies to the whole pattern,
    # including the `<[a-z][a-z0-9-]*>` run and the lookahead, so the leg treated
    # `<A>`, `<SECRET>` and `<Email>` as complete placeholders although this
    # kernel writes placeholder names in lowercase only. That cost a disclosure
    # of the same class the lookahead closed: measured under the production
    # vocabulary, with nothing regressed in either surface, the transformer
    # published `token=/repo<SECRET-90210904812340981234>` as
    # `token=<repo><SECRET-90210904812340981234>` with the twenty digits intact
    # and the unscoped leg admitted it, as it did `<A...>`, `<Secret-...>` and
    # `<Email...>` tails. Scoping the flag makes the run and the lookahead
    # case-SENSITIVE, which refuses all four, while the word still matches
    # `TOKEN=` and `Token:`. Every placeholder this kernel writes is lowercase,
    # so no legitimate output moved: verified over the inventory derived in
    # `test_the_leg_admits_every_placeholder_name_the_kernel_can_write`.
    #
    # The separator scope survives the DELETION of the `\s*` that used to stand
    # between `[:=]` and the leading run, because the run's own `\s` branch
    # accepts exactly the same whitespace. Verified over every character Python's
    # `\s` matches, and over all 66,430 values the residual case enumerates: the
    # two spellings agree on every verdict, and `token\xa0=\xa0SECRET` is still
    # refused. Deleting it is what returns the leg's cost to linear, for the
    # reason `test_the_token_leg_quantifies_whitespace_after_the_separator_exactly_once`
    # records, and that case is the only thing that stops it coming back.
    #
    # The value may OPEN with a run of complete placeholders, because `_reduce`
    # substitutes roots and typed values before the token entry runs: an email,
    # a UUID or a root-prefixed path is consumed and marked decided, the
    # residual `token=` then matches no transformer pattern at all, and the
    # published line is the already-safe `token=<email>`. A `[^\s]` value class
    # refused that output, and because `token-credential` was a content leg the
    # refusal was wholesale, so one benign line destroyed the entire evidence
    # extract for a failing run (#812). The leg degrades per line now, so the
    # same false positive would cost one line rather than the file; the value
    # class is still the fix, because a line the validator refuses is a line the
    # operator does not get either way.
    #
    # What the run may not do is END in raw bytes. Requiring one byte after it is
    # what refuses `token=<repo>90210904812340981234`,
    # `token=<email>90210904812340981234`, `token=<uuid>-90210904812340981234`
    # and `token=<home>90210904812340981234`. Those four are what the
    # transformer publishes, with nothing regressed anywhere, when a raw value's
    # head is a root, an email or a UUID and its tail is a digit secret: the
    # head is consumed, the token entry finds no value after `token=`, and a
    # pure-digit tail carries no alphabetic word for the ordinary-word check to
    # refuse. A bare `[^\s<]` value class, with no leading run at all, admitted
    # all four.
    #
    # THE VALUE CLASS IS "A BYTE THAT IS NEITHER WHITESPACE NOR THE OPENING OF A
    # COMPLETE PLACEHOLDER", spelled as a negative lookahead in front of
    # `[^\s]`. The earlier spelling `[^\s<]` excluded every `<`, which cost a
    # disclosure: `<` is itself an attach byte the transformer publishes a secret
    # behind, and a `<` that opens no complete placeholder stopped the run
    # without being reachable by the value class either. Measured under the
    # production vocabulary, the transformer published
    # `token=/repo<90210904812340981234` as
    # `token=<repo><90210904812340981234` with the twenty digits intact and the
    # leg admitted it, through the `<email>`, `<uuid>` and `<home>` heads as
    # well. The lookahead refuses that `<` while still admitting a `<` that does
    # open a complete placeholder, so the kernel's own output is untouched. Four
    # rows of `test_a_secret_behind_a_placeholder_and_a_bracket_is_refused` pin
    # it at both seams.
    #
    # THE RUN ABSORBS WHITESPACE AS WELL AS COMPLETE PLACEHOLDERS, and the `|\s`
    # branch is what closes a disclosure the placeholder-only run admitted. A
    # raw value whose head a substitution consumes and whose tail is separated
    # from that head by a SPACE published its secret in full: measured under the
    # production vocabulary, the transformer published
    # `token=/repo 90210904812340981234` as
    # `token=<repo> 90210904812340981234`, and a run that could not step over
    # the space looked for its raw byte immediately after `<repo>`, found the
    # space, backtracked to a zero-length run, failed against `<`, and admitted
    # the line. The same shape leaked through `<email>`, `<uuid>` and `<home>`
    # heads, and through a tab as well as a space. With whitespace in the run
    # the value class is reached across it and all of them are refused.
    #
    # THE LEG'S COST IS LINEAR, and it was quadratic for one commit, so the
    # reason is recorded here rather than left to be rediscovered. Adding the
    # `|\s` branch left a `\s*` standing between `[:=]` and the run, and the two
    # quantifiers accepted the same whitespace: a line carrying only whitespace
    # after the separator gave the engine no terminator, so it retried every way
    # of splitting that whitespace between them. Measured on `token=` followed by
    # N spaces, the cost grew about fourfold per doubling of N, reaching 156,210
    # us for one line at `MAX_LINE_BYTES` — about 375 seconds for a
    # `GLOBAL_LINE_CAP`-sized extract, which would have denied the operator the
    # failure extract for six minutes. Deleting the redundant `\s*` returns it to
    # linear: 6.7 us at 200 spaces and 119 us at 4,090, about 0.29 seconds for
    # the whole extract, with no verdict changed anywhere. The lookahead inside
    # the quantified run introduces no new blowup of its own: neither of the two
    # shapes where one would appear grows faster than linearly. A long run of `<`
    # now matches at the first byte, so it is constant at 0.1 us where the earlier
    # form grew to 25 us at 4,090 bytes; a long `<a><a><a>...` run is linear and
    # costs about 1.3 times the earlier form at the same length, 69.9 us against
    # 54.2 us at 3,060 bytes, because each backtrack step now checks the
    # lookahead. NOTHING BUT THE PATTERN'S SHAPE PINS THIS. The quadratic form is
    # verdict-identical, so no admitted or refused line can tell the two apart,
    # and the only guard is
    # `test_the_token_leg_quantifies_whitespace_after_the_separator_exactly_once`.
    # That guard asserts the whole segment between `[:=]` and the trailing value
    # class, so a redundant `\s*` reintroduced on EITHER side of the run reds it.
    # An earlier form of the guard asserted only that `[:=]` is not followed by
    # `\s`, and the quadratic cost came back for a spelling it did not cover:
    # `(?:<[a-z][a-z0-9-]*>|\s)*\s*(?!<…>)` puts the redundant quantifier after
    # the run instead of before it, passes that guard, is verdict-identical over
    # all 66,430 values the residual case enumerates, and costs 93,329 us at
    # 4,090 spaces against 131 us for this form.
    #
    # RECORDED PLAINLY, as the residual THIS ENTRY admits: ONE class, and the
    # class is the whole bound of the entry. A value reaches no match here
    # exactly when it consists only of complete LOWERCASE-NAMED
    # placeholder-shaped spans and whitespace, the empty value included;
    # `token=`, `token: `, `token=<email>`, `token: <uuid>`,
    # `token=<repo><path>`, `token=<repo> <path>`, `token=<uuid> ` and
    # `token=<secret-payload>` are members, not the whole membership.
    #
    # THE ENTRY IS NO LONGER THE WHOLE LEG (#821). `validate_export` consults
    # `_token_value_names_an_unknown_placeholder` after every entry in this
    # tuple, and it reports the same `token-credential` reason for a value whose
    # spans are complete and lowercase-named but whose NAMES nobody vouches for.
    # So `token=<secret-payload>` is a violation now although this entry does not
    # match it, and `token=<repo><path>` is admitted only when the caller passes
    # the `roots` mapping that names `repo`. What follows describes this entry's
    # own bound, which is still exactly as stated; the enumeration below the
    # tuple describes the rest. The word
    # LOWERCASE carries weight: `token=<SECRET>` and `token=<Email>` are the same
    # shape with an uppercase letter in the name and they are refused, which is
    # what scoping the case flag to the word bought. Measured over 66,430 values
    # built from `<`, `>`, `a`, `1`, `-`, space, tab, `x` and `.` at every length
    # up to five, against a reachability model of the leading run written
    # independently of this regex: zero disagreements, 169 admitted, and zero
    # admitted values outside that class. Extending that alphabet with a single
    # uppercase `A` gives 111,111 values, of which 95 are decided differently by
    # the unscoped flag and this one, every one of them admitted there and refused
    # here, and none decided the permissive way round. The class is admitted on
    # purpose, because it is what the transformer legitimately writes when a
    # substitution consumed the whole value, and refusing it destroyed the
    # evidence extract for a failing run (#812).
    #
    # NO SHAPE RULE CAN CLOSE THE LOWERCASE SUBSET, and that subset alone was what
    # remained irreducible TO A SHAPE RULE rather than merely unclosed. An earlier
    # wording said "no shape rule can close it" of the whole class, and
    # measurement refuted it: scoping the case-insensitivity flag to the word is a
    # shape rule, and it closes every span whose name carries an uppercase letter
    # — `<A>`, `<SECRET>`, `<Email>` and `<Secret-payload>` are refused now and
    # were admitted before. What no shape rule can close is what remained: a span
    # whose name is spelled exactly the way this kernel spells one, lowercase
    # letters, digits and hyphens, because a leaker's bytes and the kernel's bytes
    # are then identical.
    #
    # #821 CLOSED IT, AND NOT WITH A SHAPE RULE. Separating those spans needs the
    # NAME, so the validator owns a frozen enumeration of the transformer's static
    # placeholder names below this tuple and takes the dynamic root names from the
    # `roots` mapping the caller already passes. That couples the two surfaces
    # this file otherwise keeps disjoint, which is why it carries its own drift
    # test: `tests/test_test_evidence_kernel.py` derives the transformer's
    # inventory by walking `_TYPED_PATTERNS` and the kernel's string constants
    # with `ast` and requires EXACT equality with the enumeration.
    #
    # THE CLASS REACHED THE PUBLISHED OUTPUT, which is why #821 was a disclosure
    # and not only a gap in this backstop, and why the TRANSFORMER changed as well
    # as this surface. Measured under the production vocabulary, with nothing
    # regressed in either surface, the transformer published
    # `token=/repo<a90210904812340981234>` as
    # `token=<repo><a90210904812340981234>` and
    # `token=/repo<secret-90210904812340981234>` as
    # `token=<repo><secret-90210904812340981234>`, twenty digits intact, and this
    # entry admitted both because each tail IS placeholder-shaped. The head is
    # consumed, the token entry finds no value after `token=`, and the tail's only
    # alphabetic word — `a`, `secret` — is one `build_known_tokens` vouches for,
    # so the ordinary-word check published the line.
    # `_undecided_placeholder_in_a_token_value` now fails those lines closed
    # before `_is_ordinary` runs, because the tail is a placeholder-shaped span
    # `_reduce` did not write. A pure-digit tail such as
    # `<90210904812340981234>` is not placeholder-shaped, so this entry refuses it
    # as it always did.
    # NOT every form of this entry has admitted the shape, and the earlier claim
    # that they all had was measured false. The original form, `b2385f688`'s
    # `(?i)\btoken\b[ \t]*[:=][ \t]*[^\s]`, refuses all three of those rows —
    # incidentally rather than by separating leak from legitimate output, because a
    # value class of `[^\s]` with no leading run refuses the kernel's own
    # `token=<email>` too, which is the #812 false positive. `f2a8e4309` widened
    # that class to `[^\s<]` and opened all three, and `135384cd3`, `b8402d5d7`
    # and `253190e98` admitted them until #821. So the three rows were the price of
    # closing #812, not a constant of the leg; what the `|\s` branch, the lookahead
    # and the scoped flag each did is recorded above, and none of the three
    # touched the lowercase subset. Pinned by
    # `PREVIOUSLY_DISCLOSED_PLACEHOLDER_SHAPED_SECRET` in the residual case, so it
    # cannot be lost the way the `<` attach case nearly was.
    #
    # A DESCRIPTION THAT WAS WRONG TWICE IS NOW ACCURATE, which is worth stating
    # because both corrections are recorded above this line. "A value made
    # ENTIRELY of placeholder-shaped spans" was measured false when the leg
    # admitted `token=<secretpayload`, `token=<SECRET`, `token=<Ab9!x`,
    # `token=<>`, `token=<1abc>` and `token=<a><b`. The value class was then
    # widened until the description became true: all six of those are refused
    # now, and the class is exactly lowercase-named placeholder-shaped spans and
    # whitespace. `token=<SECRET` in that list is the unterminated form; the
    # terminated `token=<SECRET>` was admitted until the case flag was scoped to
    # the word, and is refused now too. "Any value that begins with `<`" was wrong
    # in the other direction and still is, because
    # `token=<repo>90210904812340981234` begins with `<` and is refused.
    #
    # RECORDED PLAINLY AS WELL, because it is the price of admitting the residual
    # above: a placeholder value followed by ordinary content is refused. The
    # transformer publishes `token=<repo><path>:373: FAIL`, `token: <uuid>.`,
    # `token: <uuid> FAIL` and `token=<repo> diverged`, and this leg refuses each
    # of them — one replaced line each now that the reason degrades per line,
    # rather than the whole export. The lookahead widens that class by the shapes
    # whose value opens with a `<` that completes no placeholder, `token=<>` and
    # `token=<1abc>` among them. It is one class throughout, and none of its
    # members carries a secret.
    #
    # Shape cannot separate that class from the leak class, and the reason is NOT
    # that one of the four leaks above opens its tail with `-`. That wording
    # invited the reader to conclude that excluding `:` and `.` from the value
    # class would admit the benign lines safely, and the conclusion is false:
    # almost every candidate character is attackable the same way, so an
    # exclusion opens a leak for whatever it excludes. Measured over
    # `token=/repo` plus one candidate character plus `90210904812340981234`. The
    # candidate set is the 32 characters of Python's `string.punctuation` —
    # `!"#$%&'()*+,-./:;<=>?@[\]^_` plus a backtick, `{|}~` — plus a space and a
    # tab, so the denominator is 34. The transformer publishes the twenty-digit
    # secret for 31 of those 34, and only `/`, `\` and a backtick do not, because
    # those three reach the path and unknown-vocabulary rules instead. `:` and
    # `.` — the two characters an exclusion would have to cover to admit the
    # benign lines — are both in the attackable set, so excluding them would
    # admit `token=<repo>:90210904812340981234` and
    # `token=<repo>.90210904812340981234` with the secret in full. Of the 31 this
    # leg now admits NONE. Three of them were admitted at some point in this
    # tranche: the space and the tab until the `|\s` branch was added, and `<`
    # until the lookahead replaced `[^\s<]`.
    ("token-credential",
     re.compile(r"(?i:\b(?:token|(?:access|refresh|id|bearer)_token)\b)"
                r"\s*[:=](?:<[a-z][a-z0-9-]*>|\s)*"
                r"(?!<[a-z][a-z0-9-]*>)[^\s]")),
)

# ------------------------------------- the token value's placeholder NAMES
#
# #821. The `token-credential` leg above decides by SHAPE, and shape cannot
# separate a lowercase-named placeholder-shaped span a leaker wrote from one the
# transformer wrote: the bytes are identical. The leg therefore admitted
# `token=<repo><secret-90210904812340981234>` with its twenty digits intact, and
# the comment above it recorded the class as irreducible. It is not irreducible;
# it is unclosable BY SHAPE. Separating the two needs the NAME, and that is what
# this enumeration supplies.
#
# OWNED HERE, NOT IMPORTED FROM THE TRANSFORMER, and the duplication is the
# point: the paragraph above `_FORBIDDEN` keeps the two surfaces disjoint so a
# classifier that wrongly admits unsafe text cannot admit it in both places. The
# static names are written out; `tests/test_test_evidence_kernel.py` derives the
# transformer's own inventory by walking `_TYPED_PATTERNS` and the kernel's
# string constants with `ast` and requires EXACT equality with this set, so a
# transformer placeholder added later fails there and names both sides. That
# drift test is what makes an owned copy safe; without it the copy would be a
# transcription that cannot fail.
#
# `param` IS DELIBERATELY ABSENT. The node normalizer writes `[<param>]` inside
# a pytest node identifier, which is not a token value position, so admitting it
# here would widen the class for no reachable output.
#
# ROOT PLACEHOLDERS ARE NOT HARD-CODED. The caller supplies `roots`, the
# transformer renders each name as `<name>`, and this leg compares the exact
# rendered literal. The precedent for that coupling is already in this file: the
# `unnormalized-parameter` leg accepts the literal placeholder the transformer
# substitutes. A hard-coded root list would be a second transcription, and it
# would be wrong for any caller whose roots differ.
TOKEN_VALUE_PLACEHOLDER_NAMES = frozenset({
    "b64", "credential", "credential-url", "email", "hex", "path", "uuid",
})

# The leg's own prefix and span grammar, spelled independently of the
# transformer's for the reason the disjointness paragraph gives. The prefix is
# the one stated exception and is byte-identical to the leg's above.
_TOKEN_VALUE_NAME_PREFIX_RE = re.compile(
    r"(?i:\b(?:token|(?:access|refresh|id|bearer)_token)\b)\s*[:=]"
)
_TOKEN_VALUE_NAME_SPAN_RE = re.compile(r"<[a-z][a-z0-9-]*>")


def _token_value_names_an_unknown_placeholder(text: str, roots=None) -> bool:
    """A complete placeholder span in a token value whose NAME nobody vouches for.

    LAST-RUNNING AND LINEAR. `validate_export` consults this only after every
    entry in `_FORBIDDEN`, so no wholesale leg can be shadowed by it, and its
    reason is `token-credential`, which `PER_LINE_VIOLATION_REASONS` contains —
    a firing costs its own line. Each prefix's walk stops at the first byte that
    is neither whitespace nor a complete span, and a prefix cannot sit inside a
    span, so the walks are disjoint and every byte is visited at most once.

    A RAW BYTE IS NOT THIS PREDICATE'S BUSINESS. The `token-credential` entry in
    `_FORBIDDEN` already refuses a value that reaches one, and it has run by the
    time this is consulted. What is left for this to decide is exactly the class
    that entry admits: a value of whitespace and complete lowercase-named spans.
    """
    known = set(TOKEN_VALUE_PLACEHOLDER_NAMES)
    known.update(str(name) for name in (roots or {}))
    for prefix in _TOKEN_VALUE_NAME_PREFIX_RE.finditer(text):
        position = prefix.end()
        while position < len(text):
            if text[position].isspace():
                position += 1
                continue
            span = _TOKEN_VALUE_NAME_SPAN_RE.match(text, position)
            if span is None:
                break
            if span.group(0)[1:-1] not in known:
                return True
            position = span.end()
    return False


# The structural legs, stated as proportions rather than as token counts so
# that they do not restate the transformer's rule in different words. A span
# that is almost entirely letters and spaces, carries no placeholder and no
# number, and runs to several words is prose, and prose is production content
# this estate has no way to vouch for.
#
# The word floor is SIX. A three-space floor rejected `FAIL <name>: <label>
# diverged` — the line `bin/_lib-golden-diff.sh` emits from the chokepoint
# every fixture harness compares through — and rejected an indentation-only
# line as well. While this heuristic still refused wholesale, that floor
# deleted the deliverable on essentially every real failing run: it
# traded a disclosure hole for an availability hole, which is what turns a
# detector into an outage. The floor can sit here because the transformer no
# longer decides by how text looks: a suffix is now vouched for word by word
# against the repository's own vocabulary, so this leg catches what a
# transformer bug admits rather than carrying the whole burden alone.
_FREE_TEXT_MIN_WORDS = 6
_FREE_TEXT_MIN_RATIO = 0.95
# Independently formulated rather than shared with `_QUOTED_RE`; the same
# single-quote boundaries keep apostrophes from manufacturing a quoted span.
_QUOTED_SPAN_RE = re.compile(
    r'''(?:"([^"\n]{8,}?)"|(?<![\w])'([^'\n]{8,}?)'(?![\w]))'''
)

# The structural legs below cover the three shapes #630 S1 taught the
# transformer to retain. Each is formulated over the EMITTED line rather than
# over the transformer's classifier, and none of them imports, calls or
# textually reuses `_PYTEST_NODE_RE`, `_PYTEST_GUTTER_RE` or
# `_PYTEST_COUNTERS_RE`. They narrow what is structurally acceptable; they
# never short-circuit the root scan, the denylist or the free-text legs above.

# A pytest node identifier may carry a parameter id, and a parameter id is
# production content built from the test's own arguments. The only body this
# validator accepts is the literal placeholder the transformer substitutes.
#
# The token is identified by what PYTEST produces — a module side that is a
# Python source file, or the `<path>` placeholder this kernel substitutes when
# it may not disclose one — rather than by the transformer's `[\w./-]+::\S+`.
# That scoping is also the fix for a false positive: the leg used to judge
# every `::…[…]` span on every line, so `note: see foo::bar[baz]` was a
# violation, and at the time a violation refused the WHOLE export and wrote
# no file at all. `unnormalized-parameter` is a structural reason now, so the
# same false positive would cost one line rather than the file — but a leg
# that judges lines it was never meant to reach is still wrong, and the
# scoping is what keeps it off them.
_NODE_TOKEN_RE = re.compile(r"(?:(?<=\s)|\A)(?:\S+?\.py|<path>)::(?P<rest>\S*)")

# An exception message is never retained, so the placeholder that stands in
# for it is always the end of its line. Anything after it is text no rule
# vouched for. The placeholder constant is shared deliberately: it is the
# emitted token, not a classifier, so a mutated classifier cannot ride out on
# it.
_EXCEPTION_PLACEHOLDER_TEXT = EXCEPTION_MESSAGE_PLACEHOLDER

# THERE IS DELIBERATELY NO NESTED-GUTTER LEG. A `nested-gutter` leg shipped
# here and was withdrawn, because the premise it rested on — that no rule in
# this kernel can emit a line carrying two `E ` gutters — is false, and the
# leg therefore refused correct output. Measured against the shipped
# transformer, in both contexts:
#
#   fail-closed   `E   E   assert 1 == 2`                       verbatim
#   fail-closed   `  E   E   assert [REDACTED: unclassified …]` emitted
#   permissive    `  E   E   assert record[0] == 1`             verbatim
#   permissive    `E   E   raise [REDACTED: unclassified …]`    emitted
#
# Every one of them is an `assert`/`raise` body, and that is not an accident
# of the corpus: the transformer evaluates its assertion rule BEFORE its
# gutter rule so that `E       assert 1 == 2` keeps its bytes, and the
# assertion rule is the ONLY rule that retains a body starting with a second
# gutter. pytest reaches the shape with today's estate, because
# `FormattedExcinfo.get_source` prefixes every line of a multi-line assertion
# message with `E `, and this repository's own meta-tests embed a captured
# inner pytest run in an assertion message.
#
# A CORRECTION, because the withdrawal used to rest on a row that is not a
# measurement: `E   E   share diverged` does NOT survive the permissive
# context. `build_known_tokens` over this repository does not vouch for `e`, so
# the inner body `E   share diverged` carries an unvouched token, `_is_ordinary`
# fails, and the transformer emits `E   [REDACTED: unclassified line]`. Today's
# doubled-gutter emissions are therefore a purely STRUCTURAL class, and the
# claim that separating a safe doubled gutter from an unsafe one needs a
# word-by-word vocabulary is withdrawn with it: a leg reading "is the inner
# body an `assert`/`raise` form" would have been available.
#
# THE LEG STAYS WITHDRAWN ANYWAY, on the argument that does hold. This
# validator has no gutter awareness in any other leg, so `E   E   X` and
# `E   X` are judged identically by the root scan, the denylist and the
# free-text legs — and the single-gutter `E   SECRET PAYLOAD` is accepted
# here. Refusing the doubled form closes no disclosure class the single form
# leaves open, and the transformer emits neither: `E   E   SECRET PAYLOAD`
# comes out redacted in the fail-closed context, in the permissive one, and
# in a context whose vocabulary is extended with `e`. A leg that cannot reach
# a real disclosure and can refuse a real assertion is a false positive
# generator with no compensating cover, and per-line degradation lowers the
# price of that without making it worth paying. RECORDED PLAINLY:
# `E   E   SECRET PAYLOAD` is not refused by this validator, and no leg
# replaces the one withdrawn.

# The counters leg is a CLOSED TOKEN SET over a line that presents itself as
# pytest's summary — not the shape the transformer matches. It used to restate
# that shape's tail character for character, and because a restated shape is
# end-anchored, a transformer mutation in the SHAPE dimension produced a line
# this leg did not even attempt to judge: `1 failed, 100 passed in 45.67s --
# on host alpha` and `… (1:01:01) === SECRET` both passed it.
#
# The trigger is stated over the line's own TOKENS and reads no anchor the
# transformer supplies. It fires on either of two independent observations:
#
#   opening   the first token that is not a banner run of `=` is a bare
#             integer, and the token after it carries a letter;
#   summary   a counter pair (`<integer> <counter word>`) appears ANYWHERE,
#             and the line also carries pytest's `in <duration>` tail.
#
# The second observation is what removes the dependency the first one used to
# carry alone: a transformer mutation that lost its leading anchor emitted
# `acme-holdings-billing 1 failed, 100 passed in 45.67s`, which the opening
# observation cannot see and the summary observation refuses. The first is
# retained because it is the only one that reaches a single-counter line whose
# counter word is itself the foreign token (`1 sprocketed in 45.67s`).
#
# Neither observation is "the line contains a counter-shaped token anywhere".
# That wider trigger would judge the extract's own header (`… over 3
# subjects`), the retention notice (`EVIDENCE EVICTED: 5 runs, …`) and the
# per-harness progress line (`[ 12/56] FAIL share product 3 failed 112s`)
# against pytest's vocabulary and reject all three.
#
# RESIDUAL BLINDNESS, ACCEPTED: a mutation that loses the leading anchor AND
# emits no `in <duration>` tail is still unjudged. Closing that would need a
# trigger with no positional and no tail component at all, which is the wider
# trigger above, and the progress line proves it false-positives on output
# this estate emits on every run.
_COUNTERS_BANNER_CHAR = "="
# A duration is a generated token: pytest formats it and nothing interpolates
# repository content into it.
_COUNTERS_DURATION_TOKEN_RE = re.compile(
    r"\A(?:\d+m|\d+(?:\.\d+)?s|\(\d+:\d{2}:\d{2}\))\Z"
)
_COUNTERS_ALLOWED_WORDS = frozenset({
    "passed", "failed", "error", "errors", "skipped", "xfailed", "xpassed",
    "deselected", "warning", "warnings", "rerun", "reruns", "in",
})
# Banner dashes and the separator between counter pairs. `-` is deliberately
# absent: it is what makes a ` -- on host alpha` tail a foreign token.
_COUNTERS_PUNCTUATION = "=,"
# pytest's own failure gutter, which this leg must see past rather than judge.
# `FormattedExcinfo.get_source` prefixes every line of a multi-line assertion
# message with `E `, and this repository runs nested pytest sessions inside
# assertion messages (`tests/test_isolation_contract.py`), so the outer run's
# log really carries `E         1 failed in 0.42s`. The transformer strips the
# gutter, finds a counters line underneath and retains the whole line by
# design — and this leg then refused it, because `E` is not a counter word.
# That is the same fault that got the nested-gutter leg withdrawn: a validator
# leg refusing output the transformer legitimately produces, which costs the
# operator the one line a nested-run failure is explained by.
#
# Scoped to the LEADING position on purpose. An `E` anywhere else on a
# counters line is still a token nothing vouched for, so this tolerates the
# prefix pytest really emits without vouching for the letter generally.
_COUNTERS_GUTTER_TOKEN = "E"


def _strip_leading_gutter(tokens):
    """`tokens` with a leading run of bare `E` gutter tokens removed."""
    index = 0
    while index < len(tokens) and tokens[index] == _COUNTERS_GUTTER_TOKEN:
        index += 1
    return tokens[index:]


def _counters_opening(tokens) -> bool:
    """The line OPENS with a count, after an optional banner run of `=`."""
    count = None
    for token in tokens:
        stripped = token.strip(_COUNTERS_BANNER_CHAR)
        if not stripped:
            continue
        if count is None:
            if not stripped.isdigit():
                return False
            count = stripped
            continue
        # A letter STARTS the token after the count, so `3 + 4` is arithmetic
        # rather than a summary line whose every token this leg would then
        # judge. Written as "starts with" rather than "contains anywhere":
        # the regex this helper replaced required the leading character to be
        # a letter, and relaxing that to `any(ch.isalpha() …)` widened the
        # trigger silently — `5 -foo` fired where it had not before, which
        # turns a line no counters rule produced into a refused line.
        return stripped[:1].isalpha()
    return False


def _counters_summary(tokens) -> bool:
    """A counter pair appears anywhere AND the line carries a duration tail."""
    pair = False
    tail = False
    previous = None
    for token in tokens:
        word = token.strip(_COUNTERS_PUNCTUATION)
        # `in` is excluded from the pair vocabulary: it is the tail's own
        # word, and `3 in 4s` would otherwise read as a counter pair.
        if (
            previous is not None
            and previous.isdigit()
            and word.lower() != "in"
            and word.lower() in _COUNTERS_ALLOWED_WORDS
        ):
            pair = True
        if previous is not None and previous.lower() == "in" and (
            _COUNTERS_DURATION_TOKEN_RE.match(word)
        ):
            tail = True
        previous = word
    return pair and tail


def _structural_violation(text: str):
    """A reason string for a retained shape that carries content no rule
    vouched for, or None."""
    for token in _NODE_TOKEN_RE.finditer(text):
        rest = token.group("rest")
        # EITHER bracket, whichever comes first. `[^\s\[]+` in the emitted
        # node's own shape admits `]`, so a name may reach here with a
        # CLOSING bracket first and a `[`-only search reports no bracket at
        # all — which is how `test_y]leak` was neither normalized nor judged.
        cuts = [pos for pos in (rest.find("["), rest.find("]")) if pos != -1]
        cut = min(cuts) if cuts else -1
        # Compared against the END of the token, so an UNTERMINATED span is a
        # violation too. A parameter id may contain a space or a bracket, and
        # either one leaves a fragment that no normalization closed.
        if cut != -1 and rest[cut:] != "[<param>]":
            return "unnormalized-parameter"
    if _EXCEPTION_PLACEHOLDER_TEXT in text and not text.rstrip().endswith(
        _EXCEPTION_PLACEHOLDER_TEXT
    ):
        return "text-after-exception-placeholder"
    # The gutter is dropped for the refusal scan ONLY, never for the trigger.
    # Stripping it before the trigger let `_counters_opening` see a body it
    # could never reach before, so any emitted line whose body opened
    # `<integer> <word>` fired this leg — `E     2  usage error` from a help
    # table is the measured case. `_counters_summary` reaches every real
    # gutter-wrapped counters line on the unstripped tokens by itself,
    # because neither its pair predicate nor its duration-tail predicate
    # cares where on the line the pair sits.
    raw_tokens = text.split()
    if _counters_opening(raw_tokens) or _counters_summary(raw_tokens):
        tokens = _strip_leading_gutter(raw_tokens)
        # EVERY token, not only the ones a counter pattern happened to reach.
        # A token this set does not vouch for is content some other producer
        # put on a line that presents itself as pytest's own summary.
        for token in tokens:
            word = token.strip(_COUNTERS_PUNCTUATION)
            if not word or word.isdigit():
                continue
            if _COUNTERS_DURATION_TOKEN_RE.match(word):
                continue
            if word.lower() in _COUNTERS_ALLOWED_WORDS:
                continue
            return "unknown-counter-word"
    return None


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
    for match in _QUOTED_SPAN_RE.finditer(text):
        body = match.group(1) if match.group(1) is not None else match.group(2)
        if " " not in body:
            continue
        if _prose_ratio(body) >= _FREE_TEXT_MIN_RATIO:
            return True
    return False


def validate_export(lines, roots=None):
    """Violations requiring per-line redaction or publication refusal."""
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
        # AFTER every entry in `_FORBIDDEN`, for the positional reason the
        # comment above the `token-credential` entry states: a line that also
        # violates a wholesale leg must report that leg, or its refusal would be
        # downgraded to one replaced line without anybody deciding to downgrade
        # it (#821).
        if reason is None and _token_value_names_an_unknown_placeholder(
            text, roots
        ):
            reason = "token-credential"
        if reason is None and (
            _looks_like_free_text(text) or _looks_like_quoted_prose(text)
        ):
            reason = "free-form-text"
        if reason is None:
            reason = _structural_violation(text)
        if reason is not None:
            violations.append({"index": idx, "reason": reason, "excerpt": text[:120]})
    return violations


# The flagged line's own bytes are never written; the placeholder that stands
# in its place names the leg that refused it, so an operator reading the
# extract can tell a sanitizer fault from a missing failure.
VALIDATION_REDACTION_TEMPLATE = "[REDACTED: line refused by the validator: %s]"
VALIDATION_REDACTION_NOTICE = (
    "[REDACTED: %d of %d lines were refused by the validator and replaced; "
    "reasons: %s]"
)
# The three STRUCTURAL legs — see `_structural_violation` — and the heuristic
# free-form-text leg degrade per line. Each judges the SHAPE of sanitizer
# output rather than proving that a specific secret class survived. #630 S1
# established the structural class after validator false positives; the #637
# repository sweep added 320 retained free-form-text judgements. A check with
# that record must cost one line when it is wrong, not the whole export.
#
# Every OTHER reason `validate_export` can report is a CONTENT leg, with the one
# exception filed below: an unsubstituted root or one of the denylist patterns
# in `_FORBIDDEN`. A content violation means the transformer emitted a payload
# it was supposed to have removed, which is the systemic-breakage signal the
# previous whole-file refusal existed for, and it is not made safer by arriving
# alone.
#
# `token-credential` is a denylist leg and is filed here anyway. It is the
# exception, and it is decided on the same question of KIND as the rows above,
# not by volume. The leg's primary designed firing class is a value whose head
# a root or a typed substitution consumed, leaving a raw tail: the transformer
# publishes `token=<repo>90210904812340981234` with nothing regressed in either
# surface, and the four rows recorded above the leg itself are the measured
# shapes. Its second documented firing class, a placeholder value followed by
# ordinary punctuation, contains no secret by construction. Its THIRD, added by
# #821, is a value whose spans are complete and lowercase-named but whose names
# nobody vouches for — reported by
# `_token_value_names_an_unknown_placeholder` rather than by the tuple entry.
# That kind means either that the transformer regressed or that the caller did
# not pass the `roots` mapping naming its own placeholders, and the first of
# those is what the canary below separates out. So when this leg
# fires, nothing has regressed, and its firing is therefore not evidence that
# the UNFLAGGED lines cannot be trusted — which is the only protection a
# wholesale refusal adds, because the flagged line's own bytes never reach the
# file, the sidecar, the manifest or stderr either way.
#
# Volume could not have decided it, and the figure that supports that is
# measured on PUBLISHED OUTPUT, which the earlier wording did not say. The
# distinction is not cosmetic, because the two populations disagree. On RAW
# tracked text this leg fires on 533 lines of this repository at the commit that
# writes this sentence — `token: str` and `token = secrets.token_hex(24)` in
# committed Python, and several lines of this comment — since the word followed
# by a separator is ordinary Python and ordinary prose. Pushed through the
# transformer, whose own token entry replaces such a value with `<credential>`
# before this leg ever reads the line, it fires on ZERO of those same lines, all
# 2.07 million of them; the earlier sweep recorded zero over the 12,625 published
# lines of 118 retained failure extracts as well, and this sweep records zero
# under the unscoped case flag as well as the scoped one, so scoping it newly
# refused no published line anywhere. The figure over published output is the one
# the argument needs, because the validator reads nothing but published output,
# so the volume argument is silent in both directions and the kind argument
# decides alone. The raw-text figure would have argued the other way about a leg
# that never fires on anything this validator reads.
#
# Filing that reason here puts a POSITIONAL requirement on `_FORBIDDEN`,
# because `validate_export` reports the first leg that matches: the
# `token-credential` entry must be matched after every leg whose reason this
# set does not contain, or a line violating both would report
# `token-credential`, degrade to one replaced line, and publish — silently
# downgrading the other leg's refusal. It is the LAST entry in `_FORBIDDEN` for
# that reason, and the comment above the entry states the requirement where a
# reader adding a leg will meet it.
#
# The set is stated this way round on purpose. A leg added later is a content
# leg until somebody deliberately files it here, so an unfiled leg refuses
# wholesale rather than degrading per line, which is the fail-closed
# direction. Enumerating the content legs instead would make an unfiled leg
# degrade per line, which is the permissive one.
#
# MEMBERSHIP IN THIS SET IS A STATEMENT ABOUT A LEG, AND THAT IS ONLY VALID
# WHEN EVERY FIRING OF THE LEG MEANS THE SAME THING. The earlier formulation of
# the rule stopped at the reason: file the reason here and every occurrence of
# it degrades per line, leave it out and every occurrence refuses wholesale. A
# review measured that `token-credential` does not satisfy the precondition that
# formulation assumes. Its firings come in two kinds. One kind occurs with the
# transformer working exactly as shipped, because a substitution consumed the
# value's head and left a raw tail the ordinary-word check cannot refuse; that
# kind is a leg over-firing on safe output and must cost one line. The other
# kind occurs when the transformer has regressed, and that kind is evidence
# about every line in the export, not only the flagged one. A static set cannot
# tell them apart, because it is consulted per REASON and the two kinds share a
# reason.
#
# So the rule now has two levels, and the second one is what this leg needed. A
# reason filed here still says "one firing of this leg costs one line". What
# decides whether the export publishes at all is an OCCURRENCE-level escalation
# on evidence the legs do not carry: `_transformer_health_ok` below, consulted
# once per publication. A healthy transformer with an over-broad validator hit
# costs one line; a regressed transformer fails the canary and withholds every
# line, including the residuals no leg here can see.
#
# `token-credential` IS THE FIRST MIXED-KIND MEMBER OF THIS SET, and the next
# person filing a reason here has one question to ask because of it: does every
# firing of this leg mean the same thing? If it does, set membership alone is
# the whole rule and nothing else is needed. If it does not — if some firings
# are the transformer working and others are the transformer broken — then set
# membership is not sufficient by itself, and the filing owes an independent
# health check that separates the two, because the reason string cannot.
PER_LINE_VIOLATION_REASONS = frozenset({
    "free-form-text",
    "unnormalized-parameter",
    "text-after-exception-placeholder",
    "unknown-counter-word",
    # The one denylist reason in this set. See the paragraphs above, and the
    # positional requirement it puts on `_FORBIDDEN`.
    "token-credential",
})

# ---------------------------------------------- the transformer-health canary
#
# ONE fixed line, pushed through the REAL reduction once per publication. It is
# the occurrence-level evidence the rule above says a mixed-kind leg needs: the
# validator's legs judge the CONTENT that was published, and this judges whether
# the surface that produced it still works. Separating those two questions is
# the whole point, because the validator cannot answer the second one — the
# measured case is a transformer whose token entry is gone and a value whose
# tail is already placeholder-shaped, which produces no violation at all, so
# every leg here stays silent while the secret publishes.
#
# THIS IS THE ONE PLACE THE VALIDATOR SIDE CALLS A TRANSFORMER HELPER, and the
# paragraph above `_FORBIDDEN` forbidding exactly that still holds as written.
# What that paragraph protects is the independence of the legs that decide
# SAFETY: a leg built from a transformer classifier admits whatever the
# classifier admits, and the check then proves nothing. This function decides no
# line's safety and formulates no leg. It asks the transformer one question about
# itself, so consulting the transformer is not a shortcut around an independent
# judgement — it is the only way to obtain the answer. The legs stay
# independently formulated; only the escalation consults health.
#
# RUN UNCONDITIONALLY, not when `token-credential` fires. Keying it to that leg
# would leave the worst case uncovered for exactly the reason the leg needed an
# escalation in the first place: a regressed transformer together with a secret
# of the placeholder-shaped residual class produces no violation, so a
# violation-gated probe would never be consulted and that export would publish
# the secret. The cost is the same either way, because it is a fixed line.
#
# THE CANARY VALUE'S VERDICT DEPENDS ON THE TOKEN ENTRY AND ON NOTHING ELSE.
# `_reduce` substitutes roots, then the typed patterns in order, then paths, and
# a probe any of the others can claim would still pass with the token entry
# gone. This value reaches none of them: it carries no `@` for `<email>`, no
# hyphen-grouped hex for `<uuid>`, none of the `bearer`/`api_key`/
# `authorization` words, no `sk-`/`pk-` prefix, no `://`, no 32-character hex
# run, no 40-character unbroken alphanumeric run, and no `/` for either path
# rule. The context is EMPTY, so root substitution is the identity and no
# caller's root prefix can consume the value either. Exactly one entry in
# `_TYPED_PATTERNS` matches it, and deleting that entry leaves the line byte
# for byte with no decided span — which is the failure this check reports.
# Pinned by `test_the_canary_is_claimed_only_by_the_token_entry`.
#
# EMPTY CONTEXT ALSO MAKES THIS DATA-INDEPENDENT AND CONSTANT. It must not call
# `build_known_tokens`, shell out, or read the repository: `build_known_tokens`
# runs `git ls-files`, so borrowing the caller's real context would turn a
# per-publication probe into a subprocess per publication. `ScrubContext()` with
# no arguments compiles no root alternation and touches nothing outside memory.
# Measured at 4.6 us per call, context construction included.
TRANSFORMER_CANARY_LINE = "token=cctally-transformer-health-canary"
TRANSFORMER_CANARY_EXPECTED = "<credential>"
# Names WHAT HAPPENED rather than a leg, because no leg's verdict produced it
# and an operator reading `token-credential` here would look for a credential in
# the export instead of at the transformer. Deliberately NOT a member of
# `PER_LINE_VIOLATION_REASONS`: there is no line to replace, and the whole
# content of the verdict is that the unflagged lines cannot be trusted.
TRANSFORMER_HEALTH_REFUSAL = "transformer-health-check-failed"


def _transformer_health_ok() -> bool:
    """Does the real reduction still replace a separated token's value?

    FAIL CLOSED ON THE CHECK ITSELF. An unevaluable canary is an unhealthy
    transformer, not an inconclusive probe: a reduction that raises, returns the
    wrong shape, or has lost a name it needs cannot be trusted with the export
    either, and treating "I could not tell" as "healthy" would publish on
    exactly the faults this check exists to catch.

    `SystemExit` and `KeyboardInterrupt` are deliberately outside the clause.
    Neither is a failure mode of a pure in-memory reduction, and swallowing an
    operator's interrupt in order to record a canary verdict would be a worse
    outcome than refusing.
    """
    try:
        reduced, spans = _reduce(TRANSFORMER_CANARY_LINE, ScrubContext())
        return (
            reduced == TRANSFORMER_CANARY_EXPECTED
            and tuple(spans) == ((0, len(TRANSFORMER_CANARY_EXPECTED)),)
        )
    except Exception:                                   # noqa: BLE001
        return False


# NO RATE THRESHOLD. A proportional escape shipped here and is withdrawn,
# because it decided the question by VOLUME when the question is one of KIND.
#
# The issue is KIND, not VOLUME. A fault confined to the vocabulary stage is
# checked independently by the single/double-quoted-span rule and the free-text
# heuristic; every detected line is removed. An unsubstituted root, or a
# denylist hit whose reason `PER_LINE_VIOLATION_REASONS` does not contain, is
# stronger evidence that a concrete secret class survived, so one still refuses
# wholesale. A rate threshold adds no useful safety distinction to either group.
def apply_validation_redactions(lines, violations, roots=None):
    """Replace each flagged line with a placeholder, or refuse wholesale.

    Returns `(lines, record)`. `record` carries `redacted`, `total`,
    `reasons`, `notice` and `refused`; when `refused` is true the caller must
    publish nothing, because a denylist CONTENT violation means the transformer
    rather than one leg is broken and the UNFLAGGED lines cannot be trusted.

    EVERY REFUSAL PATH RETURNS AN EMPTY LIST, so "publish nothing" is a property
    of the value rather than only a rule the caller has to remember. `total`
    still states how many lines were withheld, and both callers already read
    `refused` before they read the lines, so the change is invisible to them.
    Returning the caller's own list on a refusal was safe only by convention,
    and a refusal is exactly the occasion on which a caller that skipped the
    convention would write out unsanitized text.

    Degrading per line rather than per file is what keeps a single false
    positive from costing the operator every byte of a failure extract — the
    same failure class, at the file level, that the sanitizer's own
    over-redaction was raised as. Fail-closed still holds for the offending
    line: its bytes never reach the caller's output. It applies to the reasons
    `PER_LINE_VIOLATION_REASONS` names — the structural legs, the heuristic
    free-text leg and `token-credential` — for the reasons recorded on that set.

    The placeholders and the notice are OUTPUT, so they are put back through
    the same validator. A reason string that did not clear it would publish
    through the very gate this function stands in for, so that case escalates
    to a wholesale refusal rather than being written.
    """
    lines = list(lines)
    total = len(lines)
    reasons = sorted({v.get("reason") for v in violations if v.get("reason")})
    record = {
        "redacted": len(violations),
        "total": total,
        "reasons": reasons,
        "notice": None,
        "refused": False,
        # WHY the export was refused, which is not always the first violation's
        # reason: a structural violation may be recorded ahead of the content
        # one that caused the refusal, and three of the four refusal paths below
        # are not a leg's verdict at all. A caller that reported
        # `violations[0]["reason"]` therefore named the wrong cause.
        "refusal": None,
    }
    # FIRST, AND WITHOUT REGARD TO WHAT THE VALIDATOR FOUND. A failed canary is
    # the strongest verdict this function can reach, so it is also the most
    # actionable cause to record: a content leg firing beside it is a symptom of
    # the same regression, and naming the leg would send the reader after one
    # line's payload instead of after the transformer. It sits ahead of the
    # no-violation return because a clean validator pass is exactly what a
    # regressed transformer produces on a secret of the residual class.
    if not _transformer_health_ok():
        record["refused"] = True
        record["refusal"] = TRANSFORMER_HEALTH_REFUSAL
        return [], record
    if not violations:
        return lines, record
    # A violation carrying no reason at all is not a recognized per-line leg:
    # it is a violation this function cannot classify, and an unclassifiable
    # violation takes the conservative outcome like any content leg.
    content = [
        v.get("reason") for v in violations
        if v.get("reason") not in PER_LINE_VIOLATION_REASONS
    ]
    if content:
        record["refused"] = True
        record["refusal"] = content[0] or "unclassified-violation"
        return [], record
    for violation in violations:
        index = violation.get("index")
        if not isinstance(index, int) or not 0 <= index < total:
            # A violation that does not address a line cannot be redacted, so
            # there is nothing safe to publish around it.
            record["refused"] = True
            record["refusal"] = "unlocatable-violation"
            return [], record
        lines[index] = VALIDATION_REDACTION_TEMPLATE % violation.get("reason")
    notice = VALIDATION_REDACTION_NOTICE % (
        len(violations), total, ", ".join(reasons) or "unknown",
    )
    if validate_export(lines + [notice], roots):
        record["refused"] = True
        record["refusal"] = "unvalidatable-replacement"
        return [], record
    record["notice"] = notice
    return lines, record


# ------------------------------------------------------------------ retention

RUN_STATES = ("active", "completed", "aborted", "abandoned")
# Both derived from measurement, not chosen (#630 S1, F5).
#
# The horizon is 12 rather than 11 because the cutoff below is whole days times
# 86,400 and the ledger's own window measured 11.06 days, so 11 does not cover
# it. At 7 days the store lost evidence the ledger still reported on, which is
# what every recorded coverage gap was: 51 of 447 measured retention passes
# evicted something and every one of them evicted for age. The byte cap has
# never bound.
DEFAULT_MAX_AGE_DAYS = 12
# 2 GiB. F5 alone would have needed no increase: today's footprint scaled by
# 12/7 projects about 111 MiB and 115 MiB, and adding one measured durations
# artifact per canonical run the horizon holds (531,404 compressed bytes across
# 152 and 116 runs) reaches only about 187 MiB. The cap rises for the case a
# compression ratio cannot be trusted to: the same records uncompressed are
# 7,902,147 bytes, which projects to about 1,256 MiB and therefore does not fit
# under the previous 1 GiB. Raising it to 2 GiB covers that worst case with
# headroom while staying inside twice it, because a cap that can never bind is
# not a cap.
DEFAULT_MAX_BYTES = 2147483648


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


# ------------------------------------------------------ per-test durations (#630 S1)

DURATIONS_ARTIFACT_NAME = "pytest-tests.jsonl.gz"
_DURATIONS_COMPRESS_LEVEL = 6


# A killed process leaves a gzip member with no end-of-stream marker, and the
# decompressor raises rather than ending the stream: `EOFError` for a member
# cut mid-stream and `zlib.error` for one whose bytes no longer decode.
# NEITHER is an `OSError`, so catching `OSError` alone let the one failure
# mode this completeness contract exists for escape the merge entirely — the
# bridge died with a traceback and published no artifact at all, rather than
# publishing one marked `complete: false`. `gzip.BadGzipFile` IS an `OSError`
# and is listed for the reader, not because it needs to be.
_TRUNCATED_LEG_ERRORS = (EOFError, zlib.error, gzip.BadGzipFile)


def _read_duration_leg(path):
    """`(records, footer, present)` for one leg's intermediate file.

    A file that does not exist, or that cannot be opened, yields no footer,
    and the merge reports that leg incomplete. The caller only hands in legs
    that RAN, so an unreadable file means the process died before it could
    finish one — never that the leg was skipped.

    A file that opens but ends mid-stream is PRESENT and incomplete: whatever
    decoded before the truncation is kept, because a partial record of a run
    that died is exactly what this session exists to preserve, and the absent
    footer is what marks it incomplete.
    """
    records, footer = [], None
    try:
        handle = gzip.open(path, "rt", encoding="utf-8")
    except OSError:
        return [], None, False
    try:
        with handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except ValueError:
                    # A truncated final line is exactly what a killed process
                    # leaves. It is dropped, and the absent footer is what
                    # makes the leg report incomplete.
                    continue
                if isinstance(item, dict) and item.get("footer"):
                    footer = item
                elif isinstance(item, dict):
                    records.append(item)
    except _TRUNCATED_LEG_ERRORS:
        return records, None, True
    except OSError:
        return records, footer, True
    return records, footer, True


# pytest's own exit statuses over a session whose recorded population is
# WHOLE: 0 (all passed), 1 (tests failed — an ordinary, complete run) and 5
# (nothing collected). Every other status — 2 interrupted, 3 internal error,
# 4 usage error — means the session stopped before it ran what it collected,
# so the leg's records cover fewer tests than the run intended.
#
# `pytest_sessionfinish` is reached on those paths too: `_pytest.main.
# wrap_session` calls it from its `finally` block whenever `initstate >= 2`,
# including after the `except BaseException` arm that sets INTERNAL_ERROR and
# after a KeyboardInterrupt. The footer alone therefore cannot say whether the
# population is whole, which is why the status is recorded beside it.
DURATIONS_COMPLETE_EXIT_STATUSES = frozenset({0, 1, 5})


def _leg_population_is_whole(footer) -> bool:
    """Whether one leg's footer describes a session that ran to the end.

    A footer with no `exitStatus` at all predates the field and is taken at
    its word, because before it existed the footer's presence WAS the signal
    and refusing those artifacts would rewrite history as incomplete.
    """
    if not footer or not footer.get("sessionFinished"):
        return False
    status = footer.get("exitStatus")
    if status is None:
        return True
    try:
        return int(status) in DURATIONS_COMPLETE_EXIT_STATUSES
    except (TypeError, ValueError):
        return False


def _merge_duration_plan(legs):
    """The pure half: ordering and completeness over already-decoded legs.

    `legs` is a list of `(name, records, footer, present)`. Records are ordered
    by leg, then node id, then phase, so the artifact is byte-stable for a
    given set of records.
    """
    ordered, summary, complete = [], [], True
    for name, records, footer, present in legs:
        # Two SEPARATE facts, reported separately because they fail
        # separately: the hook ran, and the session it closed had run what it
        # collected. An INTERNALERROR satisfies the first and not the second.
        finished = bool(footer and footer.get("sessionFinished"))
        whole = _leg_population_is_whole(footer)
        if not (present and whole):
            complete = False
        summary.append({
            "leg": (footer or {}).get("leg") or name,
            "present": bool(present),
            "sessionFinished": finished,
            "exitStatus": (footer or {}).get("exitStatus"),
            "populationWhole": whole,
            "records": len(records),
            "declaredRecords": (footer or {}).get("records"),
        })
        ordered.extend(records)
    ordered.sort(key=lambda r: (
        str(r.get("leg") or ""), str(r.get("nodeId") or ""), str(r.get("phase") or "")
    ))
    return ordered, summary, complete


def merge_duration_legs(leg_paths, out_path):
    """Publish `out_path` from every leg file in `leg_paths`.

    The two pytest invocations are separate processes, so they cannot share
    one output path — each writes its own file and this merges them. The
    artifact is published ATOMICALLY: written to a temporary sibling and then
    renamed, so no partially written file is ever visible at the final path
    for a concurrent reader or for a run that dies mid-merge.

    The merged artifact states its own completeness in a footer, because S6
    and S7 read the FILE. A leg whose footer is missing or whose session did
    not finish marks the artifact incomplete, and a consumer must refuse to
    draw estate-wide conclusions from it.
    """
    leg_paths = list(leg_paths or ())
    legs = []
    for path in leg_paths:
        records, footer, present = _read_duration_leg(path)
        legs.append((os.path.basename(path), records, footer, present))
    ordered, summary, complete = _merge_duration_plan(legs)

    tmp_path = out_path + ".tmp.%d" % os.getpid()
    try:
        with gzip.open(
            tmp_path, "wt", encoding="utf-8",
            compresslevel=_DURATIONS_COMPRESS_LEVEL,
        ) as handle:
            for record in ordered:
                handle.write(json.dumps(record, sort_keys=True) + "\n")
            handle.write(json.dumps({
                "footer": True,
                "complete": complete,
                "records": len(ordered),
                "legs": summary,
            }, sort_keys=True) + "\n")
        os.replace(tmp_path, out_path)
    except (OSError, zlib.error):
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return {"complete": complete, "records": len(ordered), "legs": summary}


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
