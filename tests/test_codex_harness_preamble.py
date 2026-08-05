"""#463 S3 — the Codex tool-output harness preamble reader (spec §4.1, §4.2).

Pure kernel, no I/O. The defect this replaces is a regex written against an
assumed shape: `_HARNESS_STATUS_RE` targeted one of five grammars and matched
none of it — 39,942 production outputs carry the preamble it targets and 0
match. So the reader is a closed line vocabulary that degrades to `unknown` on
an unseen arrangement rather than silently matching nothing, and it is anchored
at position zero and terminated by an `Output:` line so a result that merely
begins with a lookalike line keeps its first line.
"""
from __future__ import annotations

import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BIN_DIR = REPO_ROOT / "bin"
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

from _lib_codex_harness_preamble import parse_harness_preamble  # noqa: E402


def test_js_sandbox_completed():
    fields, rest = parse_harness_preamble(
        "Script completed\nWall time 0.5 seconds\nOutput:\nreal output\n")
    assert fields["status"] == "completed"
    assert fields["wall_time_seconds"] == 0.5
    assert fields["exit_code"] is None
    assert rest == "real output\n"


def test_js_sandbox_failed():
    fields, _ = parse_harness_preamble(
        "Script failed\nWall time 2 seconds\nOutput:\n")
    assert fields["status"] == "failed"


def test_native_shell_exited_nonzero_is_failed():
    fields, rest = parse_harness_preamble(
        "Chunk ID: 8c93bf\nWall time: 1.0034 seconds\n"
        "Process exited with code 3\nOriginal token count: 12\nOutput:\nbody")
    assert fields["status"] == "failed"
    assert fields["exit_code"] == 3
    assert fields["wall_time_seconds"] == 1.0034
    assert rest == "body"


def test_native_shell_running_carries_the_announcement():
    fields, _ = parse_harness_preamble(
        "Chunk ID: 740265\nWall time: 1.0 seconds\n"
        "Process running with session ID 59671\nOriginal token count: 0\nOutput:\n")
    assert fields["status"] == "running"
    assert fields["session_announcement"] == "59671"
    assert fields["exit_code"] is None


def test_exit_code_grammar():
    fields, _ = parse_harness_preamble("Exit code: 0\nWall time: 0 seconds\nOutput:\n")
    assert fields["status"] == "completed"
    assert fields["exit_code"] == 0


def test_wall_time_only_is_unknown_but_recognised():
    fields, rest = parse_harness_preamble("Wall time: 0.73 seconds\nOutput:\n{}")
    assert fields["status"] == "unknown"
    assert fields["wall_time_seconds"] == 0.73
    assert rest == "{}"


def test_milliseconds_convert():
    fields, _ = parse_harness_preamble("Wall time: 250 ms\nOutput:\n")
    assert fields["wall_time_seconds"] == 0.25


def test_crlf_accepted():
    fields, rest = parse_harness_preamble(
        "Script completed\r\nWall time 0.5 seconds\r\nOutput:\r\nbody")
    assert fields["status"] == "completed"
    assert rest == "body"


def test_no_preamble_is_untouched():
    assert parse_harness_preamble("just some output\nwith lines\n") is None


def test_status_line_without_output_terminator_is_not_recognised():
    # A real result that merely BEGINS with a lookalike line keeps its first line.
    assert parse_harness_preamble("Exit code: 0 was returned by the child\n") is None
    assert parse_harness_preamble("Exit code: 0\nWall time: 1 seconds\nno terminator") is None


def test_malformed_field_makes_the_whole_preamble_unrecognised():
    assert parse_harness_preamble(
        "Exit code: notanumber\nWall time: 1 seconds\nOutput:\n") is None
    assert parse_harness_preamble(
        "Chunk ID: " + "x" * 65 + "\nWall time: 1 seconds\nOutput:\n") is None


def test_line_cap_before_output_is_unrecognised():
    body = "".join("Wall time: 1 seconds\n" for _ in range(9)) + "Output:\n"
    assert parse_harness_preamble(body) is None


def test_character_cap_before_output_is_unrecognised():
    assert parse_harness_preamble("Chunk ID: a\n" + " " * 600 + "\nOutput:\n") is None


# ── non-vacuity against the observed grammars (plan Task 1, Step 5) ───────────
#
# The exact byte sequences from spec Appendix A's grammar table, so a future edit
# that silently stops matching one of them is caught here rather than in a
# production read that quietly reverts to `unknown`. The census counts are
# recorded beside each row because they are what makes a silent regression
# expensive.
_OBSERVED_GRAMMARS = [
    # (label, exact bytes, status, exit_code, wall_time_seconds, announcement, remainder)
    ("Script completed (39,623)",
     "Script completed\nWall time 0.512 seconds\nOutput:\nalpha\n",
     "completed", None, 0.512, None, "alpha\n"),
    ("Script failed (319)",
     "Script failed\nWall time 0.2 seconds\nOutput:\nstderr line\n",
     "failed", None, 0.2, None, "stderr line\n"),
    ("Chunk ID + Process running with session ID (4,585)",
     "Chunk ID: 740265\nWall time: 1.0021 seconds\n"
     "Process running with session ID 59671\nOriginal token count: 0\n"
     "Output:\nwaiting\n",
     "running", None, 1.0021, "59671", "waiting\n"),
    ("Chunk ID + Process exited with code 0 (4,376)",
     "Chunk ID: 8c93bf\nWall time: 0.9007 seconds\n"
     "Process exited with code 0\nOriginal token count: 31\nOutput:\ndone\n",
     "completed", 0, 0.9007, None, "done\n"),
    ("Chunk ID + Process exited with a NON-ZERO code",
     "Chunk ID: 8c93bf\nWall time: 0.9007 seconds\n"
     "Process exited with code 127\nOriginal token count: 31\n"
     "Output:\nnot found\n",
     "failed", 127, 0.9007, None, "not found\n"),
    ("Exit code (1,249, apply_patch)",
     "Exit code: 0\nWall time: 0.0304 seconds\nOutput:\nSuccess\n",
     "completed", 0, 0.0304, None, "Success\n"),
    ("Wall time only (533, js and browser tools)",
     "Wall time: 0.7312 seconds\nOutput:\n{}\n",
     "unknown", None, 0.7312, None, "{}\n"),
]


def test_every_observed_grammar_still_resolves():
    for (label, raw, status, exit_code, wall, announcement,
         remainder) in _OBSERVED_GRAMMARS:
        parsed = parse_harness_preamble(raw)
        assert parsed is not None, label
        fields, rest = parsed
        assert fields["status"] == status, label
        assert fields["exit_code"] == exit_code, label
        assert fields["wall_time_seconds"] == wall, label
        assert fields["session_announcement"] == announcement, label
        assert rest == remainder, label


def test_the_ten_thousand_no_preamble_outputs_stay_untouched():
    """Real tool output that happens to open with prose is never consumed."""
    for raw in [
        "usage: git [--version] [--help]\n",
        "{\n  \"ok\": true\n}\n",
        "Wall time is not a preamble when nothing terminates it\n",
        "Output:\n",                       # a terminator with no preamble before it
        "",
    ]:
        assert parse_harness_preamble(raw) is None, raw


def test_a_blank_separator_line_is_tolerated():
    """The shape `_HARNESS_STATUS_RE` was written against still resolves.

    Zero production outputs carry the blank line before `Output:` (spec §1), but
    the shipped `session-b-card-wire` fixture does, and a reader that refused it
    would silently regress that fixture's resolved status to `unknown`. A blank
    line carries no field, so tolerating it cannot mis-parse one; the `Output:`
    terminator and the line/character caps still bound what is consumed.
    """
    fields, rest = parse_harness_preamble(
        "Script completed\nWall time 0.1 seconds\n\nOutput:")
    assert fields["status"] == "completed"
    assert fields["wall_time_seconds"] == 0.1
    assert rest == ""
