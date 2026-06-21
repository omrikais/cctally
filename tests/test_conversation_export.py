"""Golden-string tests for the pure Markdown serializer ``export_session_markdown``
(issue #217 S5, F1/F5 — `bin/_lib_conversation_export.py`).

The serializer is fed the SAME assembled `items` (+ `subagent_meta`) that
`_assemble_session` returns, so these fixtures mirror that shape: each item is a
dict with `kind` / `anchor` / `ts` / `text` / `blocks` / `subagent_key` /
`is_sidechain` (+ `meta_kind` on meta items, `model` on assistant turns); each
block is a dict with `kind` (`text`/`thinking`/`tool_call`/`tool_result`) and the
tool-call fields (`name`/`input`/`input_truncated`/`result`/`edit_stat`/`stderr`).
Pure function — no DB / filesystem / locks.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
from _lib_conversation_export import export_session_markdown  # noqa: E402


def _anchor(u):
    return {"session_id": "s1", "uuid": u, "id": 1}


def sample_items():
    """A small assembled session: human turn, assistant turn (prose + thinking +
    Edit + Bash), a meta `context` turn, and one subagent turn (subagent_key=k1).
    """
    return [
        {"kind": "human", "anchor": _anchor("h1"), "ts": "2026-06-01T00:00:00Z",
         "text": "please fix the bug", "blocks": [],
         "subagent_key": None, "is_sidechain": False},
        {"kind": "assistant", "anchor": _anchor("a1"), "ts": "2026-06-01T00:00:05Z",
         "text": "Sure, here is the fix.", "model": "claude-opus-4-8",
         "subagent_key": None, "is_sidechain": False,
         "blocks": [
             {"kind": "text", "text": "Sure, here is the fix."},
             {"kind": "thinking", "text": "I should edit cost.ts then run echo."},
             {"kind": "tool_call", "name": "Edit", "tool_use_id": "t-edit",
              "input": {"file_path": "a/cost.ts",
                        "old_string": "return x", "new_string": "return x + 1"},
              "input_truncated": False, "result": {"text": "updated",
                                                   "truncated": False,
                                                   "is_error": False}},
             {"kind": "tool_call", "name": "Bash", "tool_use_id": "t-bash",
              "input": {"command": "echo hi"}, "input_truncated": False,
              "result": {"text": "hi\n", "truncated": False, "is_error": False}},
         ]},
        {"kind": "meta", "anchor": _anchor("m1"), "ts": "2026-06-01T00:00:06Z",
         "meta_kind": "context", "text": "Current git status: clean",
         "subagent_key": None, "is_sidechain": False, "blocks": []},
        {"kind": "assistant", "anchor": _anchor("sa1"), "ts": "2026-06-01T00:00:07Z",
         "text": "subagent prose", "model": "claude-opus-4-8",
         "subagent_key": "k1", "is_sidechain": True,
         "blocks": [{"kind": "text", "text": "subagent prose"}]},
    ]


def sample_subagent_meta():
    return {"k1": {"kind": "Explore"}}


def sample_items_with_truncated_tool():
    return [
        {"kind": "assistant", "anchor": _anchor("a1"), "ts": "2026-06-01T00:00:05Z",
         "text": "writing a big file", "model": "claude-opus-4-8",
         "subagent_key": None, "is_sidechain": False,
         "blocks": [
             {"kind": "text", "text": "writing a big file"},
             {"kind": "tool_call", "name": "Write", "tool_use_id": "t-write",
              "input": {"file_path": "big.txt", "content": "line\n"},
              "input_truncated": True,
              "result": {"text": "wrote it (clipped)", "truncated": True,
                         "is_error": False}},
         ]},
    ]


def test_export_all_includes_prose_thinking_tools_meta_subagent():
    md = export_session_markdown(
        sample_items(), "all",
        subagent_meta=sample_subagent_meta(), title="My Session")
    assert "## 👤 Human" in md and "## 🤖 Assistant" in md
    assert "> 💭 Thinking" in md
    assert "```diff" in md                      # Edit rendered as a fenced diff
    assert "$ echo hi" in md                    # Bash command
    assert "### ⎇ Subagent: Explore" in md      # subagent grouped + labelled
    assert "subagent prose" in md
    assert "context" in md.lower()              # the meta context turn surfaced


def test_export_chat_is_prose_only_main_session():
    md = export_session_markdown(
        sample_items(), "chat", subagent_meta=sample_subagent_meta())
    assert "```diff" not in md and "> 💭 Thinking" not in md  # no tools/thinking
    assert "### ⎇ Subagent" not in md                        # main-session only
    assert "subagent prose" not in md
    assert "please fix the bug" in md and "Sure, here is the fix." in md


def test_export_prompts_lists_human_turns():
    md = export_session_markdown(sample_items(), "prompts")
    assert md.count("## Prompt ") >= 1
    assert "please fix the bug" in md


def test_export_recipe_is_numbered_list():
    md = export_session_markdown(sample_items(), "recipe")
    assert "# Replay recipe" in md and "\n1. " in md
    assert "please fix the bug" in md


def test_export_marks_truncation():
    md = export_session_markdown(sample_items_with_truncated_tool(), "all")
    assert "[truncated]" in md


def test_export_title_renders_as_h1():
    md = export_session_markdown(sample_items(), "all", title="My Session")
    assert "# My Session" in md
