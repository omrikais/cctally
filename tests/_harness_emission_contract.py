"""Read a shell subject's public lines out of its own source (#783, #769 S4).

`tests/test_test_remote_observability.py` pinned the scaffolding diagnostics by
TRANSCRIBING thirteen of them into `SCAFFOLDING_LINES`. A transcription asserts
that the lines somebody wrote down survive the aggregator's scrub, and it can
never fail for a line nobody wrote down: three `bin/_lib-golden-diff.sh` lines
and eight `exited non-zero` emissions were redacted in every aggregated export
and no case could notice, because none of them was in the list.

`tests/test_source_aware_scrub_vocabulary.py` closed that class for ONE harness
by deriving the line set from the harness source. This module is that reader,
generalized so several subjects can share it, and it is deliberately parser-only:
it never loads a `bin/` module, never scrubs anything, and holds no declaration
of its own. The declarations and the scrub live in the test module, which is
mirror-private because it loads the private observability kernel.

The reader FAILS rather than skips. Every line it cannot read is returned as an
`Unreadable` for the caller to declare or reword, because a coverage case that
quietly covers a subset is worse than none: it licenses the belief that the
class is closed.

That promise was false until #769 S4 for one class, and it was the class most
likely to hide a public line: the reader flagged only `echo` and `printf`, so
an EMBEDDED INTERPRETER was invisible to it.
`bin/cctally-alerts-dispatch-test` runs a Python heredoc whose
`print(f"MARKER:…")` produces lines the harness later emits, and a
`python3 -c "…"` body beside it, and the reader neither read nor reported
either. It now flags a heredoc body's emitting lines and an interpreter's
emitting call wherever it appears, so the limit is declared rather than
silent. It still does not READ those lines: this module parses shell, and
saying so is the honest position.

POSITIONAL PARAMETERS, and why one sample per name could not reach them
(#834 S1 #808). `_PARAMETER` matched `${name}` and `$name` and nothing else,
so `$1` was invisible: `bin/cctally-rederive-test` emits `echo "FAIL $1"` from
inside a shell function, `parameters_of` returned no name for it, and the
contract therefore drove the scrub over the literal text `FAIL $1`, which no
harness ever prints. The line looked checked and was not. It now reads a
positional parameter as a parameter whose NAME is its digit, and
`read_call_sites` reads the invocations of the enclosing function so each
`$N` resolves to the literal argument that invocation passes. One emission
source with nine call sites is therefore checked nine times, against the nine
values the subject really prints, rather than once against one chosen sample.

`$?`, `$@`, `$*`, `$#` and `$$` are deliberately NOT read as parameters. Their
values are not stated at a call site, so reading them as parameters would
demand a sample for a value no site supplies, and twenty subject emissions
already carry `$?` verbatim as part of an exit-status diagnostic.
"""
from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class Emission:
    """One public line the subject can print, with its parameters unresolved.

    `function` names the shell function whose body the line sits in, or is
    `None` at file scope. It is what lets a positional parameter be resolved
    from the invocations of that function rather than from a chosen sample.
    """

    line_no: int
    body: str
    stream: str
    source: str
    function: "str | None" = None


@dataclass(frozen=True)
class Unreadable:
    """A line the reader cannot read as one fixed public line.

    Four causes, and `reason` says which: an `echo`/`printf` in a form the
    strict reader does not parse; an emitting line inside a heredoc body; an
    embedded interpreter's emitting call anywhere in the file; and an `echo`
    whose whole body is one variable, which has no fixed words to pin.
    """

    line_no: int
    source: str
    reason: str


@dataclass(frozen=True)
class CallSite:
    """One invocation of a shell function defined in the same subject.

    `arguments` holds the invocation's words after the function name, each
    already unquoted, with `None` in place of a word whose value this reader
    cannot state — one that expands or escapes something at run time. It is
    PER POSITION rather than per site on purpose: `assert_eq 1 exit-status-mismatch
    "0" "$s1_ec"` states its case identifier and its reason code and leaves the
    compared values to run time, so the two positions the verdict line prints
    resolve while the two it does not print stay unknown. Judging the site as a
    whole would have reported that line as unchecked.
    """

    function: str
    line_no: int
    arguments: "tuple[str | None, ...]"

    @property
    def key(self) -> str:
        """A stable identifier for this site: the function and its line."""
        return f"{self.function}@{self.line_no}"


@dataclass(frozen=True)
class UnresolvedCall:
    """An invocation whose arguments this reader cannot read as literals."""

    function: str
    line_no: int
    source: str
    reason: str


# The call-site key an emission carries when it needs no call-site value at
# all, so a file-scope line and a function-scope line that interpolates only
# named parameters keep one key rather than one per invocation.
FILE_SCOPE_CALL_SITE = "-"

_COMMENT = re.compile(r"^\s*#")

# One line-initial `echo` of one double-quoted body, optionally to stderr.
# `>&2` counts: the runner log carries both streams and the aggregator scrubs
# what it finds there, so a stderr diagnostic is redacted exactly like a stdout
# one.
_STRICT_ECHO = re.compile(r'^\s*echo "((?:[^"\\])*)"(\s*>&2)?\s*$')

# `echo` or `printf` used as a COMMAND word rather than as part of a name. A
# line carrying one that the strict form above did not read is returned as
# `Unreadable`; the caller declares it as a non-emission or rewords it.
_EMITTER_WORD = re.compile(r"(?<![\w./-])(echo|printf)(?![\w.-])")

# `${name}`, `${name[*]}`, `${name:-default}` and bare `$name`. The precedent's
# regex read `${name:-?}` as `name` and left `:-?}` in the sample, which then
# reached the scrub as text nobody wrote.
#
# Since #834 S1 #808 a POSITIONAL parameter is read too, under the digits as
# its name: `$1`, `${1}` and `${1:-}`. The named alternatives are kept first so
# a name is never split on a leading digit it cannot have.
_PARAMETER = re.compile(
    r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?:\[[^\]]*\])?(?::[-=?+][^}]*)?\}"
    r"|\$([A-Za-z_][A-Za-z0-9_]*)"
    r"|\$\{([0-9]+)(?::[-=?+][^}]*)?\}"
    r"|\$([0-9])"
)


def _parameter_name(match) -> str:
    """The one name a `_PARAMETER` match carries, whichever branch matched."""
    return (
        match.group(1) or match.group(2) or match.group(3) or match.group(4)
    )


def is_positional(name: str) -> bool:
    """Whether a parameter name is a positional one, so a call site sets it."""
    return name.isdigit()


# `name () {` — the one function-definition form this estate writes. The
# closing brace is matched at the SAME indentation, so a `}` that closes an
# inner block cannot end the function early.
_FUNCTION_OPEN = re.compile(
    r"^(?P<indent>[ \t]*)(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\(\)\s*\{\s*$"
)

# One invocation of a known function as a whole simple command: the name, then
# its words, and nothing else. Anything richer — a pipeline, a conditional, a
# command substitution, a trailing `|| true` — does not match and is reported
# as an `UnresolvedCall` rather than skipped.
_SIMPLE_CALL = re.compile(r"^[ \t]*(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?P<rest>.*)$")

# One shell word: a double-quoted run, a single-quoted run, or a bare run.
_WORD = re.compile(r'"([^"]*)"|\'([^\']*)\'|(\S+)')

# A word whose value this reader cannot state: it expands something, or escapes
# something, at run time.
_NOT_A_LITERAL = re.compile(r"[$`\\]")

# `$(( … ))`, which carries bare names no parameter expansion would match. It is
# substituted first and as a whole, under this reserved name.
_ARITHMETIC = re.compile(r"\$\(\([^()]*\)\)")
ARITHMETIC_PARAMETER = "arithmetic"

# An echo body that is nothing but one parameter expansion: `$out`, `${out}`,
# `${out:-}`. Such a line has no fixed words, so there is nothing for this
# contract to pin and checking it would only record that a chosen SAMPLE
# survived the scrub.
_WHOLE_LINE_DUMP = re.compile(
    r"^\s*(?:\$\{[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]*\])?(?::[-=?+][^}]*)?\}"
    r"|\$[A-Za-z_][A-Za-z0-9_]*)\s*$"
)

# A heredoc opening, quoted or not, with or without the `<<-` tab-stripping
# form. Everything up to the closing delimiter is a body this reader does not
# parse, and it is very often ANOTHER LANGUAGE.
_HEREDOC_OPEN = re.compile(r"<<-?\s*[\"']?([A-Za-z_][A-Za-z0-9_]*)[\"']?")

# Emitting forms of the interpreters this repository embeds in heredocs. This
# reader parses shell, so it can neither read these lines nor prove they are
# not public: `bin/cctally-alerts-dispatch-test` embeds a Python heredoc whose
# `print(f"MARKER:…")` produces lines the harness later emits with `echo`. The
# reader reported neither, so its own promise — "every line it cannot read is
# returned as an Unreadable" — was false for exactly the class most likely to
# hide one. Flagging them makes the limit loud instead of silent.
_INTERPRETER_EMITTER = re.compile(
    r"(?<![\w.])(?:print|sys\.stdout\.write|sys\.stderr\.write|console\.log)\s*\("
)


def read_emissions(text: str) -> tuple[list[Emission], list[Unreadable]]:
    """`(emissions, unreadable lines)` for one shell subject's source."""
    emissions: list[Emission] = []
    unreadable: list[Unreadable] = []
    delimiter: "str | None" = None
    function: "str | None" = None
    function_indent = ""
    for number, line in enumerate(text.splitlines(), start=1):
        if delimiter is None:
            if function is None:
                opened_function = _FUNCTION_OPEN.match(line)
                if opened_function:
                    function = opened_function.group("name")
                    function_indent = opened_function.group("indent")
                    continue
            elif line.rstrip() == function_indent + "}":
                function = None
                continue
        if delimiter is not None:
            if line.strip() == delimiter:
                delimiter = None
                continue
            if _INTERPRETER_EMITTER.search(line) or _EMITTER_WORD.search(line):
                unreadable.append(
                    Unreadable(
                        line_no=number,
                        source=line.strip(),
                        reason="an emitting line inside a heredoc body, which "
                               "this shell reader does not parse",
                    )
                )
            continue
        if _COMMENT.match(line):
            continue
        if _INTERPRETER_EMITTER.search(line):
            # Not every embedded interpreter arrives in a heredoc: a
            # `python3 -c "…"` body is an ordinary double-quoted shell string
            # spanning many lines, and `bin/cctally-alerts-dispatch-test`
            # carries one whose `print(line[len(prefix):])` the heredoc tracker
            # above cannot see. An interpreter's emitting call inside a shell
            # subject is flagged wherever it appears, because this reader
            # cannot read it in ANY of those forms and must say so.
            unreadable.append(
                Unreadable(
                    line_no=number,
                    source=line.strip(),
                    reason="an embedded interpreter's emitting call, which "
                           "this shell reader does not parse",
                )
            )
            continue
        opened = _HEREDOC_OPEN.search(line)
        if opened:
            delimiter = opened.group(1)
        match = _STRICT_ECHO.match(line)
        if match and _WHOLE_LINE_DUMP.match(match.group(1)):
            # `echo "$out"` carries no fixed words at all — it prints whatever
            # a variable holds. Reading it as an emission means checking a
            # SAMPLE through the scrub and recording that the sample survived,
            # which asserts nothing about the harness. The shared scaffolding
            # already declares the equivalent lines as non-emissions for this
            # reason; this makes the reader treat them alike (#769 S4).
            unreadable.append(
                Unreadable(
                    line_no=number,
                    source=line.strip(),
                    reason="a whole-line dump of one variable, which carries "
                           "no fixed words to pin",
                )
            )
            continue
        if match:
            emissions.append(
                Emission(
                    line_no=number,
                    body=match.group(1),
                    stream="stderr" if match.group(2) else "stdout",
                    source=line.strip(),
                    function=function,
                )
            )
            continue
        found = _EMITTER_WORD.search(line)
        if found:
            unreadable.append(
                Unreadable(
                    line_no=number,
                    source=line.strip(),
                    reason=f"`{found.group(1)}` in a form the reader cannot read",
                )
            )
    return emissions, unreadable


def read_call_sites(
    text: str,
) -> "tuple[dict[str, tuple[CallSite, ...]], tuple[UnresolvedCall, ...]]":
    """`({function: sites}, unresolved sites)` for one shell subject's source.

    Only functions this subject DEFINES are looked for, because a positional
    parameter inside a function body is set by that function's own invocations
    and by nothing else. A name used as a command word that the simple-command
    form cannot read is returned as an `UnresolvedCall`, never dropped: a site
    this reader silently skipped would be a value the contract never checked,
    which is the silent-subset failure the module exists to refuse.
    """
    lines = text.splitlines()
    defined: dict[str, str] = {}
    for line in lines:
        opened = _FUNCTION_OPEN.match(line)
        if opened:
            defined[opened.group("name")] = opened.group("indent")

    sites: dict[str, list[CallSite]] = {name: [] for name in defined}
    unresolved: list[UnresolvedCall] = []
    delimiter: "str | None" = None
    for number, line in enumerate(lines, start=1):
        if delimiter is not None:
            if line.strip() == delimiter:
                delimiter = None
            continue
        opened_heredoc = _HEREDOC_OPEN.search(line)
        if _COMMENT.match(line):
            continue
        if _FUNCTION_OPEN.match(line):
            continue
        for name in defined:
            if not re.search(r"(?<![\w./-])" + re.escape(name) + r"(?![\w.-])", line):
                continue
            call = _SIMPLE_CALL.match(line)
            if not call or call.group("name") != name:
                unresolved.append(
                    UnresolvedCall(
                        function=name,
                        line_no=number,
                        source=line.strip(),
                        reason=f"`{name}` appears here in a form this reader "
                               "cannot read as one simple command",
                    )
                )
                continue
            sites[name].append(
                CallSite(
                    function=name,
                    line_no=number,
                    arguments=_split_words(call.group("rest")),
                )
            )
        if opened_heredoc:
            delimiter = opened_heredoc.group(1)
    return (
        {name: tuple(found) for name, found in sites.items()},
        tuple(unresolved),
    )


def _split_words(rest: str) -> "tuple[str | None, ...]":
    """One simple command's arguments, `None` where the value is not a literal."""
    words: "list[str | None]" = []
    for double, single, bare in _WORD.findall(rest):
        if single:
            words.append(single)
            continue
        text = double if double else bare
        words.append(None if _NOT_A_LITERAL.search(text) else text)
    return tuple(words)


def call_site_bindings(site: "CallSite") -> "dict[str, tuple[str, str]]":
    """The positional parameters one invocation states, as sample pairs.

    Both sides are the literal argument. A call-site value is DERIVED from the
    subject rather than chosen for it, so there is no second form to declare:
    where the aggregator reduces such a value, the contract must see that and
    fail, because the argument is the thing the author can change.

    A position whose value the reader cannot state contributes NO binding, so a
    line interpolating it is reported as unchecked rather than filled with a
    guess.
    """
    return {
        str(index): (value, value)
        for index, value in enumerate(site.arguments, start=1)
        if value is not None
    }


def parameters_of(body: str) -> set[str]:
    """Every declared-sample key one emission body needs."""
    names: set[str] = set()
    if _ARITHMETIC.search(body):
        names.add(ARITHMETIC_PARAMETER)
    for match in _PARAMETER.finditer(_ARITHMETIC.sub("", body)):
        names.add(_parameter_name(match))
    return names


def substitute(body: str, samples: dict[str, tuple[str, str]], index: int) -> str:
    """`body` with every parameter replaced by one side of its declared sample.

    `index` selects the side: 0 is the value fed to the scrub, 1 is the form the
    scrub is required to leave. The two differ only where a value is a PATH,
    which the aggregator reduces by design; the fixed words around it are what
    the contract pins.
    """
    filled = _ARITHMETIC.sub(
        lambda _m: samples[ARITHMETIC_PARAMETER][index], body
    )
    return _PARAMETER.sub(
        lambda m: samples[_parameter_name(m)][index], filled
    )
