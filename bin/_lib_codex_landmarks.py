"""Read-time landmark derivation for the Codex conversation outline (#463 S4).

Pure kernel: no SQLite, no I/O, and nothing from the query layer. It consumes
the S3 card decoders in ``_lib_codex_conversation`` and the S2 heading
decomposition in ``_lib_codex_reasoning_headings``, and returns small derived
facts — a failure verdict per outcome-bearing row, the authored reasoning
headings of a reasoning row, and the per-file touches of a patch event.

**Everything here is read-time only.** Nothing it produces may reach
``codex_conversation_messages.detail_json``: those bytes feed ``_row_source_bytes``,
which drives ``PAGE_SOURCE_BYTE_BUDGET`` page boundaries, so persisting a derived
field would make two conversations with identical content paginate differently
according to which binary ingested them. The standing guard is
``tests/test_codex_conversation_normalization.py::test_s3_no_read_time_enrichment_is_ever_persisted``.
"""
from __future__ import annotations

import dataclasses
import re
from typing import Any, Iterable

from _lib_codex_reasoning_headings import decompose_reasoning_headings

# A physical row address — ``(source_path, line_offset)``, the key every
# conversation table is unique on and the one the payload pass joins by.
Position = tuple[str, int]

# The two status strings that mean the call failed. `decode_tool_output_card`
# already sets `is_error` from exactly this set, and `classify_tool_failure`
# below applies it to the patch and completion cards too so one definition
# covers every family.
#
# `"error"` is deliberately in this set. The CLIENT's `OUTCOME_STATUSES`
# excludes it, so a card carrying `status: "error"` on a call whose call-side
# card is not `terminal` collapses to `unknown` and is not flagged there. Spec
# §6.4 takes correctness over bug-compatibility: the server states one
# enumerated classification and Task 9 brings the client to it, because
# asserting "the server matches the client exactly" would have frozen a defect.
#
# `"running"` and `"unknown"` are NOT failures. `unknown` is a real state
# covering 17.6% of outputs — 4,585 of them are open sessions, measured — rather
# than an absence, and reporting it as a failure would invent errors that the
# reader could then not find.
_FAILED_STATUSES = frozenset({"failed", "error"})


def classify_tool_failure(cards: Any) -> bool:
    """Did this tool call fail? One enumerated definition, spec §6.4.

    ``cards`` maps a card family to the card the decoders produced for this
    call — ``terminal_output``, ``patch``, and ``web``/``mcp`` whose value holds
    the folded ``completion``. An absent family contributes nothing; an
    unrecognized one is ignored rather than guessed at.

    Each disjunct is named with the card it comes from:
    """
    if not isinstance(cards, dict):
        return False

    # 1. The result side of a terminal call — `decode_tool_output_card`, whose
    #    own `is_error` is the resolved five-grammar verdict.
    output = cards.get("terminal_output")
    if isinstance(output, dict):
        if output.get("is_error") is True:
            return True
        # 2. The same card's resolved status, which covers a card built by a
        #    caller that did not carry `is_error` forward.
        if output.get("status") in _FAILED_STATUSES:
            return True

    # 3. A patch, from either side — `decode_patch_event_card`'s `success`, and
    #    the standalone patch event's own `status`, which the client treats as
    #    an error independently of `success`.
    patch = cards.get("patch")
    if isinstance(patch, dict):
        if patch.get("success") is False:
            return True
        if patch.get("status") in _FAILED_STATUSES:
            return True

    # 4/5. The web-search and MCP completions — `decode_secondary_event_card`,
    #      reached through the call-side card's folded `completion`.
    for family in ("web", "mcp"):
        holder = cards.get(family)
        if not isinstance(holder, dict):
            continue
        completion = holder.get("completion")
        if isinstance(completion, dict) and completion.get("status") in _FAILED_STATUSES:
            return True

    return False


def reasoning_heading_texts(payload: Any) -> list[str] | None:
    """The authored headings of one reasoning payload's ``summary``, or ``None``.

    Lifted verbatim out of ``_lib_codex_conversation_query._reasoning_headings``
    so the outline and the reader decompose by ONE rule. S2's §4.6 precedent is
    that the wire publishes every heading and the render layer dedupes; a second
    copy of this parse is exactly how the two would drift.

    Headings come from ``payload["summary"]`` ONLY. ``payload["content"]`` is the
    body, which stays disclosure content and is never decomposed.

    All-or-nothing. When the payload is absent, unreadable or malformed this
    returns ``None`` and the caller omits the field entirely, so the client falls
    back to the stored ``title``/``summary`` rendering. Decomposition never fails
    the request and never partially populates.
    """
    if not isinstance(payload, dict):
        return None
    summary = payload.get("summary")
    if not isinstance(summary, list) or not summary:
        return None
    entries: list[str] = []
    for entry in summary:
        if not isinstance(entry, dict):
            return None
        text = entry.get("text")
        # Mirror `_join_content_texts`, which is what produced the stored
        # summary: it keeps non-empty string `text` leaves and ignores the rest.
        if text is None:
            continue
        if not isinstance(text, str):
            return None
        if text:
            entries.append(text)
    headings = decompose_reasoning_headings(entries)
    return headings or None


# `@@ -<old start>[,<old count>] +<new start>[,<new count>] @@`. An omitted count
# means 1, which is what a single-line hunk emits.
_HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")


def _diff_counts(diff: str) -> tuple[int, int] | None:
    """Added and removed line counts of one unified diff, or ``None``.

    Counted INSIDE the ``@@`` hunks, never by prefix over the whole text. A
    prefix filter cannot separate a file header from content that looks like
    one: removing the SQL comment ``-- legacy`` renders ``--- legacy`` and
    adding ``++i;`` renders ``+++i;``, so excluding every line that starts with
    ``---`` or ``+++`` drops real changed lines and silently understates the
    diff — the same undercount §4.5 exists to prevent, reached by a different
    route. A header can only appear before a hunk opens, and the hunk header
    states how many old-side and new-side lines follow, so inside a hunk nothing
    has to be recognised by its prefix alone.

    ``None`` when the text carries no hunk at all. Such a string is not a
    unified diff, and counting its ``+``/``-`` prefixed lines would be the same
    guess; the caller reports an undetermined count rather than a wrong one.
    """
    added = removed = 0
    old_left = new_left = 0
    in_hunk = saw_hunk = False
    for line in diff.split("\n"):
        if not in_hunk:
            header = _HUNK_HEADER_RE.match(line)
            if header is None:
                continue
            saw_hunk = True
            old_left = int(header.group(1)) if header.group(1) is not None else 1
            new_left = int(header.group(2)) if header.group(2) is not None else 1
            in_hunk = old_left > 0 or new_left > 0
            continue
        if line.startswith("\\"):
            # "\ No newline at end of file" belongs to neither side's line count.
            continue
        if line.startswith("+"):
            added += 1
            new_left -= 1
        elif line.startswith("-"):
            removed += 1
            old_left -= 1
        else:
            old_left -= 1
            new_left -= 1
        if old_left <= 0 and new_left <= 0:
            in_hunk = False
    return (added, removed) if saw_hunk else None


def patch_file_touches(payload: Any) -> list[dict]:
    """Per-file touches of one ``patch_apply_end`` payload, in provider order.

    Each entry is ``{"path", "op", "added", "removed"}``; ``added``/``removed``
    are ``None`` when the count cannot be determined, never 0, because 0 is a
    claim and an undetermined count is not.

    **Counted from the UNBOUNDED raw ``changes`` entry, before card allocation**
    (spec §4.5). ``decode_patch_event_card`` shares one 16,000-character budget
    across stdout, stderr and every file and caps the file list at 128, and S3
    measured 85 real change entries over that budget — so counting `+`/`-` lines
    out of a served ``unified_diff`` silently undercounts, and a wholly clipped
    diff would read as no change at all despite complete evidence sitting in the
    payload. Presentation cards and whole-session statistics have different
    bounds and must not share one.

    Only the DICT-shaped ``changes`` is a real production source. S3 measured
    all 4,829 patch events arriving as a dict keyed by file path; #489 taught
    the stored file-search projection to consume that shape too. The legacy
    list shape stays supported here and at ingest so a provider change cannot
    silently produce an empty file list.
    """
    if not isinstance(payload, dict) or payload.get("type") != "patch_apply_end":
        return []
    changes = payload.get("changes")
    touches: list[dict] = []
    if isinstance(changes, dict):
        items: Iterable[tuple[Any, Any]] = changes.items()
    elif isinstance(changes, list):
        # The path lives on the entry in this shape, and the kind is `status`
        # rather than `type` — the two shapes disagree on both.
        items = (
            (change.get("path"), change) for change in changes
            if isinstance(change, dict)
        )
    else:
        return []
    for path, change in items:
        if not isinstance(path, str) or not isinstance(change, dict):
            continue
        kind = change.get("type")
        if not isinstance(kind, str):
            kind = change.get("status")
        added = removed = None
        diff = change.get("unified_diff")
        if isinstance(diff, str):
            counted = _diff_counts(diff)
            if counted is not None:
                added, removed = counted
        elif kind in {"add", "delete"} and isinstance(change.get("content"), str):
            lines = change["content"].split("\n")
            if lines and lines[-1] == "":
                lines.pop()
            added, removed = ((len(lines), 0) if kind == "add"
                              else (0, len(lines)))
        touches.append({
            "path": path,
            "op": kind if isinstance(kind, str) else None,
            "added": added,
            "removed": removed,
        })
    return touches


def fold_owner_by_position(
    groups: Iterable[Iterable[Position]],
) -> dict[Position, Position]:
    """Map every row position to the position of its fold group's first row.

    That first row is the ``tool_call`` a failing ``tool_output`` or completion
    event belongs to. ``_fold_groups_for_item`` computes this membership
    payload-free and ``_build_segment_index`` discarded it, so the outline had no
    way to say WHICH call a failure belongs to — only that the segment contained
    one.

    Grouping is a conservative SUPERSET of what the block builder actually folds,
    which is the safe direction here: attributing a failure to the call it was
    grouped with can at worst name a call that did not fold, and never splits a
    call from an outcome that did.
    """
    owners: dict[Position, Position] = {}
    for group in groups:
        head: Position | None = None
        for position in group:
            if head is None:
                head = position
            owners[position] = head
    return owners


@dataclasses.dataclass
class EventDerivation:
    """Everything the outline derives from one scoped pass over the raw payloads.

    Keyed by physical position throughout, because that is the only identity the
    message table and the event table share, and because the item and segment
    keys a landmark ultimately carries are minted later from the rows these
    positions name.

    **Mutable, and filled in by the pass as it streams.** The maps are complete
    only once ``_derive_outline_events`` has returned; a partly-filled instance
    is never handed to a consumer. The dataclass is deliberately not frozen:
    freezing rebinding of the three attributes while the dicts they name are
    mutated in place would advertise an immutability this object does not have.

    ``errors_by_position`` answers "did this call fail" for the outcome-bearing
    rows the pass classified — a ``tool_output``, or a ``patch_apply_end`` /
    ``web_search_end`` / ``mcp_tool_call_end`` event. **Absence is a third state,
    not ``False``.** A position is absent when the scope did not select it, and
    equally when it was selected and could not be classified — its event row is
    gone, its payload does not parse, or the decoder declined the shape. The
    caller cannot tell those apart from this map and must not read absence as a
    claim that the call succeeded: a count derived from it is a count of
    failures FOUND, and a route that needs to distinguish "looked and found
    none" from "could not look" compares these keys against the in-scope set it
    supplied.
    """

    errors_by_position: dict[Position, bool] = dataclasses.field(default_factory=dict)
    patch_files_by_position: dict[Position, list[dict]] = dataclasses.field(
        default_factory=dict)
    headings_by_position: dict[Position, list[str]] = dataclasses.field(
        default_factory=dict)

    def failing_positions(self) -> set[Position]:
        return {pos for pos, failed in self.errors_by_position.items() if failed}
