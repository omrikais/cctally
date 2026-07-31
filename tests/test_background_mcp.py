"""Unit tests for the pure background-MCP kernel (bin/_lib_background_mcp.py).

Spec: docs/superpowers/specs/2026-07-31-background-mcp-result-recovery-design.md
"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bin"))

import _lib_background_mcp as bg   # noqa: E402

PLACEHOLDER = (
    'MCP tool "codex/codex" is still running after 120s. It was moved to the '
    'background as task kravg1b9s and keeps running; you\'ll receive a '
    'notification with the result when it completes. You can keep working in '
    'the meantime. To stop it, use TaskStop with task_id "kravg1b9s". Note: it '
    'does not survive exiting this session.'
)


def test_placeholder_task_id_parses():
    assert bg.parse_placeholder_task_id(PLACEHOLDER) == "kravg1b9s"


def test_placeholder_rejects_when_two_spellings_disagree():
    bad = PLACEHOLDER.replace('task_id "kravg1b9s"', 'task_id "different1"')
    assert bg.parse_placeholder_task_id(bad) is None


def test_non_placeholder_prose_is_rejected():
    assert bg.parse_placeholder_task_id(
        "We moved as task kravg1b9s to the background yesterday.") is None


def test_placeholder_with_only_one_spelling_still_parses():
    """Either spelling ALONE is enough — only a DISAGREEMENT rejects."""
    only_as_task = (
        'MCP tool "codex/codex" is still running after 120s. It was moved to '
        'the background as task kravg1b9s and keeps running.'
    )
    assert bg.parse_placeholder_task_id(only_as_task) == "kravg1b9s"


def test_notification_parses_task_status_and_result():
    body = (
        "<task-notification>\n<task-id>kravg1b9s</task-id>\n"
        "<status>completed</status>\n"
        "<summary>MCP task kravg1b9 (codex/codex) completed.</summary>\n"
        '<result>\n{"threadId":"t1","content":"hello"}\n</result>\n'
        "</task-notification>"
    )
    n = bg.parse_task_notification(body)
    assert n.task_id == "kravg1b9s"
    assert n.status == "completed"
    assert '"content":"hello"' in n.result_text


def test_result_containing_literal_close_tag_uses_last_delimiter():
    body = (
        "<task-notification>\n<task-id>t9</task-id>\n<status>completed</status>\n"
        '<result>\n{"content":"see </result> inside"}\n</result>\n'
        "</task-notification>"
    )
    n = bg.parse_task_notification(body)
    assert n.result_text.strip() == '{"content":"see </result> inside"}'


def test_result_delimiter_after_notification_wrapper_is_ignored():
    body = (
        "<task-notification>\n<task-id>t1</task-id>\n"
        "<status>completed</status>\n"
        "<result>inside </result> payload</result>\n"
        "</task-notification>\nforeign trailer </result>"
    )
    n = bg.parse_task_notification(body)
    assert n.result_text == "inside </result> payload"


def test_oversized_task_id_is_rejected_fail_closed():
    body = (
        "<task-notification><task-id>" + ("t" * 300) + "</task-id>"
        "<status>completed</status><result>payload</result>"
        "</task-notification>"
    )
    assert bg.parse_task_notification(body) is None


def test_summary_truncated_id_is_never_the_task_id():
    body = (
        "<task-notification>\n<task-id>kravg1b9s</task-id>\n"
        "<status>completed</status>\n"
        "<summary>MCP task kravg1b9 (codex/codex) completed.</summary>\n"
        "<result>{}</result>\n</task-notification>"
    )
    n = bg.parse_task_notification(body)
    assert n.task_id == "kravg1b9s"
    assert n.summary == "MCP task kravg1b9 (codex/codex) completed."


def test_notification_without_task_id_is_none():
    assert bg.parse_task_notification(
        "<task-notification>\n<status>completed</status>\n"
        "</task-notification>") is None


def test_join_matches_one_to_one():
    n = bg.BackgroundNotification("t1", "completed", "s", '{"content":"x"}')
    got = bg.select_background_joins({"toolu_A": "t1"}, [n])
    assert got == {"toolu_A": n}


def test_join_fails_closed_on_repeated_task_id():
    n = bg.BackgroundNotification("t1", "completed", "s", '{"content":"x"}')
    got = bg.select_background_joins({"toolu_A": "t1", "toolu_B": "t1"}, [n])
    assert got == {}


def test_join_fails_closed_on_conflicting_tasks_for_one_tool_use_id():
    """Claim collection must not collapse one tool id to a last writer."""
    a = bg.BackgroundNotification("t1", "completed", "", "result one")
    b = bg.BackgroundNotification("t2", "completed", "", "result two")
    got = bg.select_background_joins(
        [("toolu_A", "t1"), ("toolu_A", "t2")], [a, b])
    assert got == {}


def test_join_fails_closed_on_ambiguous_notifications():
    a = bg.BackgroundNotification("t1", "completed", "s", '{"content":"a"}')
    b = bg.BackgroundNotification("t1", "completed", "s", '{"content":"b"}')
    got = bg.select_background_joins({"toolu_A": "t1"}, [a, b])
    assert got == {}


def test_join_skips_non_completed_and_resultless():
    running = bg.BackgroundNotification("t1", "running", "s", None)
    empty = bg.BackgroundNotification("t2", "completed", "s", None)
    got = bg.select_background_joins({"toolu_A": "t1", "toolu_B": "t2"},
                                     [running, empty])
    assert got == {}


def test_join_of_concurrent_distinct_tasks_matches_each():
    a = bg.BackgroundNotification("t1", "completed", "s", '{"content":"A"}')
    b = bg.BackgroundNotification("t2", "completed", "s", '{"content":"B"}')
    # Notifications arrive OUT OF ORDER relative to the calls.
    got = bg.select_background_joins({"toolu_A": "t1", "toolu_B": "t2"}, [b, a])
    assert got == {"toolu_A": a, "toolu_B": b}
