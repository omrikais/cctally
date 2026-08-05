"""#463 S3 — the closed line reader for Codex tool-output harness preambles.

Pure kernel: no I/O, no DB, no config, and nothing imported beyond ``re``.

Replaces ``_HARNESS_STATUS_RE``, which was one regex written against one assumed
shape. Executed against every retained tool output on a read-only copy of the
production store it examined 60,862 outputs, of which 39,942 carry the exact
``Script completed`` / ``Script failed`` preamble it targets and **0 match**: it
required a blank line before ``Output:`` and end-of-string after it, while the
harness writes a single newline and a trailing newline. Five distinct preamble
grammars exist; that regex targeted one of them.

The replacement is a line reader over a CLOSED vocabulary rather than one regex
per grammar, deliberately (spec section 4.1). The defect being fixed is a
pattern written against an assumed shape, and the grammar set was mis-described
twice while the design was written. A closed vocabulary degrades to ``unknown``
on an arrangement it has not seen, instead of silently matching nothing.

Two rules bound what may be consumed:

  * **Anchored, never searched.** Matching starts at position zero, because the
    preamble is positionally guaranteed and a search would match a user's own
    output somewhere in the middle of a real result.
  * **Terminated by an ``Output:`` line.** All five observed grammars end that
    way. Without the terminator a result that legitimately begins with
    ``Exit code: 0`` would lose its first line; with it, the 10,177 outputs that
    carry no preamble are untouched by construction.

Any malformed or over-limit field makes the WHOLE preamble unrecognized rather
than partially parsed, so a near-match degrades to today's behaviour instead of
stripping a line it did not understand.
"""
from __future__ import annotations

import re

# At most this many lines and this many characters are examined before an
# `Output:` line. Reaching either limit first leaves the text untouched.
_MAX_PREAMBLE_LINES = 8
_MAX_PREAMBLE_CHARS = 512

_TERMINATOR = "Output:"

# Value syntaxes (spec section 4.1). A token is 1-64 characters from
# `[A-Za-z0-9_-]`; an integer is 1-10 digits with an optional leading `-`; a wall
# time is a decimal number with a unit from a closed set.
_TOKEN = r"[A-Za-z0-9_-]{1,64}"
_INT = r"-?\d{1,10}"
_UNSIGNED_INT = r"\d{1,10}"
_NUMBER = r"\d+(?:\.\d+)?"
_UNITS = r"seconds|second|ms"

_CHUNK_ID_RE = re.compile(rf"Chunk ID: ({_TOKEN})")
_EXIT_CODE_RE = re.compile(rf"Exit code: ({_INT})")
_WALL_TIME_RE = re.compile(rf"Wall time:? ({_NUMBER}) ({_UNITS})")
_PROCESS_EXITED_RE = re.compile(rf"Process exited with code ({_INT})")
_PROCESS_RUNNING_RE = re.compile(rf"Process running with session ID ({_TOKEN})")
_TOKEN_COUNT_RE = re.compile(rf"Original token count: ({_UNSIGNED_INT})")

_MILLISECOND_UNITS = frozenset({"ms"})


class _Reject(Exception):
    """The line vocabulary refused a line; the whole preamble is unrecognized."""


def _wall_time_seconds(number: str, unit: str) -> float:
    value = float(number)
    return value / 1000.0 if unit in _MILLISECOND_UNITS else value


def _read_line(line: str, observed: dict) -> bool:
    """Record one recognized preamble line, or raise ``_Reject``.

    Returns True when the line carried a FIELD (so a bare terminator with nothing
    before it can be refused) and False for a blank separator.
    """
    if line == "":
        # A blank separator carries no field, so tolerating it cannot mis-parse
        # one, and the terminator plus the two caps still bound what is consumed.
        # Zero production outputs carry it, but the shipped `session-b-card-wire`
        # fixture does, and a reader that refused it would silently regress that
        # fixture's resolved status to `unknown`.
        return False
    if line in ("Script completed", "Script failed"):
        observed["script"] = "completed" if line.endswith("completed") else "failed"
        return True
    match = _CHUNK_ID_RE.fullmatch(line)
    if match is not None:
        return True                      # deliberately not published (section 4.3)
    match = _EXIT_CODE_RE.fullmatch(line)
    if match is not None:
        observed["exit_code"] = int(match.group(1))
        return True
    match = _WALL_TIME_RE.fullmatch(line)
    if match is not None:
        observed["wall_time_seconds"] = _wall_time_seconds(
            match.group(1), match.group(2))
        return True
    match = _PROCESS_EXITED_RE.fullmatch(line)
    if match is not None:
        observed["exit_code"] = int(match.group(1))
        return True
    match = _PROCESS_RUNNING_RE.fullmatch(line)
    if match is not None:
        observed["session_announcement"] = match.group(1)
        observed["running"] = True
        return True
    match = _TOKEN_COUNT_RE.fullmatch(line)
    if match is not None:
        return True
    raise _Reject(line)


def _resolve_status(observed: dict) -> str:
    """Spec section 4.2, in priority order.

    ``running`` is explicitly NOT an error: 4,585 outputs are open sessions, and
    treating them as failures would be worse than the present silence.
    """
    if observed.get("script") == "failed":
        return "failed"
    if "exit_code" in observed:
        return "completed" if observed["exit_code"] == 0 else "failed"
    if observed.get("running"):
        return "running"
    if observed.get("script") == "completed":
        return "completed"
    return "unknown"


def parse_harness_preamble(text: str) -> tuple[dict, str] | None:
    """``(fields, remainder)`` for a recognized preamble, else ``None``.

    ``fields`` carries ``status`` (one of ``completed``, ``failed``, ``running``,
    ``unknown``), ``exit_code``, ``wall_time_seconds`` and
    ``session_announcement``. ``remainder`` is ``text`` with the consumed run,
    terminator included, removed.

    ``None`` means the caller leaves the text exactly as it found it.

    ``session_announcement`` carries the provider's raw session id for the
    conversation-level session index ONLY. It is never published on a card:
    the reader sees a conversation-local ordinal instead (spec section 4.3).
    """
    if not isinstance(text, str) or not text:
        return None
    observed: dict = {}
    consumed = 0
    field_lines = 0
    for index, raw in enumerate(text.split("\n")):
        line = raw[:-1] if raw.endswith("\r") else raw
        consumed += len(raw) + 1
        if index >= _MAX_PREAMBLE_LINES or consumed > _MAX_PREAMBLE_CHARS:
            return None
        if line == _TERMINATOR:
            # A bare terminator with no field before it is not a preamble — it is
            # a tool's own first line, and consuming it would delete real output.
            if field_lines == 0:
                return None
            fields = {
                "status": _resolve_status(observed),
                "exit_code": observed.get("exit_code"),
                "wall_time_seconds": observed.get("wall_time_seconds"),
                "session_announcement": observed.get("session_announcement"),
            }
            return fields, text[consumed:]
        try:
            if _read_line(line, observed):
                field_lines += 1
        except _Reject:
            return None
    return None                          # ran out of text before a terminator


__all__ = ["parse_harness_preamble"]
