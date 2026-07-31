"""Pure kernel for recovering backgrounded-MCP results (no I/O).

Claude Code moves an MCP call that exceeds 120s to a background task. The
completion is persisted ONLY as an attachment/queued_command record with
commandMode=="task-notification" carrying <task-id> — never as a type:"user"
line and never with a <tool-use-id>. The bridge back to the originating call is
the placeholder tool_result, which names the task twice in two independently
worded places.

Two notification shapes exist and only one of them already works:

  A  subagent / Monitor   type:"user" line     joins on <tool-use-id>
  B  backgrounded MCP     attachment record    carries <task-id> ONLY

Shape A is classified META at ingest and joined in ``_assemble_session``'s
finalize stage today. Shape B never appears as a ``type:"user"`` line, so
nothing rescues it — this module is the parser + selection half of that rescue.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Gate: only a real harness placeholder may yield a task id. Ordinary prose that
# happens to contain "as task X" must never match.
_PLACEHOLDER_GATE = re.compile(r'\AMCP tool "[^"]+" is still running after ')
_AS_TASK = re.compile(r"moved to the background as task (\S+?)[\s.,;]")
_TASK_ID_QUOTED = re.compile(r'task_id "([^"]+)"')

_TASK_ID_TAG = re.compile(r"<task-id>([^<]*)</task-id>")
_STATUS_TAG = re.compile(r"<status>([^<]*)</status>")
_SUMMARY_TAG = re.compile(r"<summary>([^<]*)</summary>")

# Every field copied from the notification wrapper into the transcript store is
# bounded. Task ids are identities, so an oversized one is rejected rather than
# truncated; status/summary are presentation metadata and may be clipped.
MAX_TASK_ID_CHARS = 256
MAX_STATUS_CHARS = 64
MAX_SUMMARY_CHARS = 1024


@dataclass(frozen=True)
class BackgroundNotification:
    task_id: str
    status: str
    summary: str
    result_text: "str | None"


def parse_placeholder_task_id(text) -> "str | None":
    """Task id from a backgrounded-MCP placeholder tool_result, else None.

    BOTH spellings are read and must AGREE. Disagreement returns None rather
    than picking one — a wrong id would attach a response to the wrong call,
    which is worse than leaving the placeholder visible. Either spelling ALONE
    is sufficient (the harness wording is not contractual), but the leading
    ``MCP tool "…" is still running after`` gate must match, so ordinary prose
    quoting a task id can never be mistaken for a placeholder.
    """
    if not text or not _PLACEHOLDER_GATE.match(text):
        return None
    a = _AS_TASK.search(text)
    b = _TASK_ID_QUOTED.search(text)
    ids = {m.group(1) for m in (a, b) if m is not None}
    if len(ids) != 1:
        return None
    task_id = ids.pop()
    return task_id if len(task_id) <= MAX_TASK_ID_CHARS else None


def _extract_result(body: str) -> "str | None":
    """Span between the FIRST <result> and the wrapper's LAST </result>.

    Delimiter contract: the result carries arbitrary MCP text that may itself
    contain a literal ``</result>``, so a non-greedy match would truncate it.
    ``rfind`` deliberately takes the last close tag BEFORE the closing
    ``</task-notification>``. Content after that wrapper is foreign and cannot
    extend the result.
    """
    start = body.find("<result>")
    if start == -1:
        return None
    wrapper_end = body.find("</task-notification>", start + len("<result>"))
    if wrapper_end == -1:
        return None
    end = body.rfind("</result>", start + len("<result>"), wrapper_end)
    if end == -1 or end <= start:
        return None
    return body[start + len("<result>"):end].strip() or None


def parse_task_notification(body) -> "BackgroundNotification | None":
    """Parse a <task-notification> body. None when it carries no task id.

    <summary> deliberately carries a TRUNCATED task id (kravg1b9 for task
    kravg1b9s) and is never used as an identity.
    """
    if not body:
        return None
    m = _TASK_ID_TAG.search(body)
    if m is None or not m.group(1).strip():
        return None
    task_id = m.group(1).strip()
    if len(task_id) > MAX_TASK_ID_CHARS:
        return None
    status = _STATUS_TAG.search(body)
    summary = _SUMMARY_TAG.search(body)
    return BackgroundNotification(
        task_id=task_id,
        status=(status.group(1).strip()[:MAX_STATUS_CHARS] if status else ""),
        summary=(summary.group(1).strip()[:MAX_SUMMARY_CHARS] if summary else ""),
        result_text=_extract_result(body),
    )


def unambiguous_placeholder_tasks(placeholders):
    """Return one task per tool id only when every claim agrees.

    ``placeholders`` may be a mapping for legacy callers or an iterable of
    ``(tool_use_id, task_id)`` claims. The iterable form preserves conflicting
    claimants that a dictionary would silently collapse last-writer-wins.
    Repeated identical claims are harmless; distinct claims fail closed.
    """
    claims = placeholders.items() if hasattr(placeholders, "items") else placeholders
    by_tool = {}
    for tool_use_id, task_id in claims:
        by_tool.setdefault(tool_use_id, set()).add(task_id)
    return {
        tool_use_id: next(iter(task_ids))
        for tool_use_id, task_ids in by_tool.items()
        if len(task_ids) == 1
    }


def select_background_joins(placeholders, notifications):
    """``{tool_use_id: BackgroundNotification}`` for UNAMBIGUOUS matches only.

    ``placeholders`` is a mapping or an iterable of claimant pairs. Background
    task ids carry no session-uniqueness guarantee comparable to Anthropic tool
    ids, and a resumed session is assembled across every source file, so this
    FAILS CLOSED: one tool id claiming conflicting tasks, one task id claimed by
    more than one tool id, or more than one completed notification joins
    NOTHING. A visible placeholder is a recoverable disappointment; a response
    attached to the wrong call is a correctness failure.

    This is the ONE selection rule — the full-payload resolver calls it too, so
    a card can never display one capped response and load a different full one.
    """
    by_task: "dict[str, list]" = {}
    for tuid, task_id in unambiguous_placeholder_tasks(placeholders).items():
        by_task.setdefault(task_id, []).append(tuid)

    usable: "dict[str, list]" = {}
    for n in notifications:
        if n.status != "completed" or not n.result_text:
            continue
        usable.setdefault(n.task_id, []).append(n)

    out = {}
    for task_id, tuids in by_task.items():
        if len(tuids) != 1:
            continue                      # ambiguous placeholder side
        cands = usable.get(task_id) or []
        if len(cands) != 1:
            continue                      # absent or ambiguous notification side
        out[tuids[0]] = cands[0]
    return out
