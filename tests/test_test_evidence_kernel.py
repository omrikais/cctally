"""Tests for bin/_lib_test_evidence.py (#529 S2)."""
from __future__ import annotations

import ast
import gzip
import importlib.util
import json
import pathlib
import re
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]


def _load_kernel():
    path = REPO / "bin" / "_lib_test_evidence.py"
    spec = importlib.util.spec_from_file_location("_lib_test_evidence", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_lib_test_evidence"] = mod
    spec.loader.exec_module(mod)
    return mod


K = _load_kernel()


def test_generate_run_id_has_the_documented_grammar():
    rid = K.generate_run_id("20260809T142211Z", 4821, 7)
    assert rid == "20260809T142211Z-4821-7"
    assert K.validate_run_id(rid)


def test_validate_run_id_rejects_path_traversal_and_separators():
    for bad in ["", ".", "..", "a/b", "a\\b", "-leading", "a:b", "x" * 129, "a b"]:
        assert not K.validate_run_id(bad), bad


def test_resolve_run_id_prefers_an_explicit_valid_value():
    env = {"CCTALLY_TEST_RUN_ID": "my-run-1"}
    assert K.resolve_run_id(env, "20260809T142211Z", 1, 2) == "my-run-1"


def test_resolve_run_id_refuses_an_explicit_invalid_value():
    env = {"CCTALLY_TEST_RUN_ID": "../escape"}
    try:
        K.resolve_run_id(env, "20260809T142211Z", 1, 2)
    except ValueError as exc:
        assert "CCTALLY_TEST_RUN_ID" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_resolve_run_id_builds_a_distinct_identity_per_matrix_version():
    base = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_RUN_ID": "31197585616",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_JOB": "test-linux",
    }
    a = K.resolve_run_id({**base, "CCTALLY_TEST_MATRIX_ID": "3.11"}, "t", 1, 2)
    b = K.resolve_run_id({**base, "CCTALLY_TEST_MATRIX_ID": "3.13"}, "t", 1, 2)
    assert a != b
    assert K.validate_run_id(a) and K.validate_run_id(b)


def test_resolve_evidence_layout_returns_none_without_a_root():
    assert K.resolve_evidence_layout(None, "cctally-dev", "r1") is None
    assert K.resolve_evidence_layout("", "cctally-dev", "r1") is None


def test_resolve_evidence_layout_keys_by_remote_dir_then_run_id():
    layout = K.resolve_evidence_layout("/ev", "cctally-dev", "r1")
    assert layout["run_dir"] == "/ev/cctally-dev/r1"
    assert layout["logs"] == "/ev/cctally-dev/r1/logs"
    assert layout["timings"] == "/ev/cctally-dev/r1/timings"
    assert layout["export"] == "/ev/cctally-dev/r1/export"
    assert layout["outcome"] == "/ev/cctally-dev/r1/export/outcome.json"
    assert layout["failure_context"] == "/ev/cctally-dev/r1/export/failure-context.txt"
    assert layout["manifest"] == "/ev/cctally-dev/r1/manifest.json"


def test_resolve_evidence_layout_refuses_an_unsafe_remote_dir():
    for bad in ["..", "a/b", "", "-x"]:
        try:
            K.resolve_evidence_layout("/ev", bad, "r1")
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected ValueError for {bad!r}")


def test_classify_failure_marker_covers_every_documented_form():
    hard = [
        "FAIL: something",
        "    FAIL: indented",
        "MISSING GOLDEN: x",
        "RECONCILE FAIL: y",
        "SELF-CHECK FAIL: z",
        "AUDIT FAILURE",
        "FIXTURE-CACHE POISONED",
        "FAILED tests/test_x.py::test_y",
        "ERROR tests/test_x.py",
        "INTERNALERROR> boom",
    ]
    for line in hard:
        assert K.classify_failure_marker(line) == "hard", line
    assert K.classify_failure_marker("WARN: careful") == "supplemental"
    assert K.classify_failure_marker("    WARN: indented") == "supplemental"


def test_classify_failure_marker_does_not_match_a_word_containing_fail():
    for line in ["FAILURE_THRESHOLD=3", "passed: 3 failed: 0", "unFAILing"]:
        assert K.classify_failure_marker(line) is None, line


def test_select_failure_windows_includes_lines_before_the_marker():
    lines = [f"line{i}" for i in range(100)]
    lines[60] = "FAIL: boom"
    windows = K.select_failure_windows(lines, before=5, after=5)
    assert len(windows) == 1
    assert windows[0]["start"] == 55
    assert windows[0]["end"] == 65
    assert windows[0]["kind"] == "hard"


def test_select_failure_windows_merges_overlapping_windows():
    lines = [f"line{i}" for i in range(100)]
    lines[40] = "FAIL: a"
    lines[43] = "FAIL: b"
    windows = K.select_failure_windows(lines, before=5, after=5)
    assert len(windows) == 1
    assert windows[0]["start"] == 35
    assert windows[0]["end"] == 48


def test_select_failure_windows_clamps_at_the_file_edges():
    lines = ["FAIL: first"] + [f"line{i}" for i in range(3)]
    windows = K.select_failure_windows(lines, before=40, after=200)
    assert windows[0]["start"] == 0
    assert windows[0]["end"] == 3


def test_a_merged_window_is_hard_when_any_constituent_marker_is_hard():
    lines = [f"line{i}" for i in range(60)]
    lines[20] = "WARN: careful"
    lines[22] = "FAIL: boom"
    windows = K.select_failure_windows(lines, before=5, after=5)
    assert len(windows) == 1
    assert windows[0]["kind"] == "hard"


def test_a_failing_run_with_no_marker_retains_the_head_and_the_tail():
    # The spec's fallback. Without it a failing run whose log carries no
    # recognised marker produces an empty extract, which is the one case where
    # the reader has nothing else to go on.
    lines = [f"line{i}" for i in range(1000)]
    # Non-vacuity: nothing in this log is a marker, so the fallback is the
    # only thing that can select anything.
    assert all(K.classify_failure_marker(line) is None for line in lines)
    assert K.select_failure_windows(lines) == []
    windows = K.select_failure_windows(lines, failing=True)
    assert [(w["start"], w["end"]) for w in windows] == [
        (0, K.WINDOW_BEFORE_LINES - 1),
        (len(lines) - K.WINDOW_AFTER_LINES, len(lines) - 1),
    ]
    assert {w["kind"] for w in windows} == {"hard"}


def test_the_marker_less_fallback_merges_on_a_short_log():
    lines = [f"line{i}" for i in range(50)]
    windows = K.select_failure_windows(lines, failing=True)
    assert [(w["start"], w["end"]) for w in windows] == [(0, 49)]


def test_the_marker_less_fallback_yields_to_a_real_marker():
    lines = [f"line{i}" for i in range(1000)]
    lines[500] = "FAIL: boom"
    windows = K.select_failure_windows(lines, failing=True)
    assert len(windows) == 1
    assert windows[0]["marker_index"] == 500


def test_allocate_budget_reserves_a_minimum_for_every_failed_subject():
    # The discriminating fixture is a global budget BELOW the per-subject cap.
    # With the cap doing the limiting instead, a plain first-come allocator —
    # the rule the spec forbids — returns the same {noisy: 600, quiet: 20} the
    # allocated rule does, and the test proves nothing about the reservation.
    # Here the noisy subject alone could take the whole budget, so the two
    # rules separate: first-come yields {noisy: 100, quiet: 0}.
    subjects = [
        {"name": "noisy", "hard": 5000, "supplemental": 5000},
        {"name": "quiet", "hard": 20, "supplemental": 0},
    ]
    alloc = K.allocate_budget(subjects, per_subject_cap=5000, global_cap=100)
    assert alloc == {"noisy": 80, "quiet": 20}, alloc
    assert sum(alloc.values()) == 100


def test_allocate_budget_serves_hard_clusters_before_supplemental_ones():
    # The discriminating case: a warning-heavy subject and a subject whose
    # hard cluster needs almost the whole global budget. A reservation that
    # can be spent on warnings would leave the decisive failure truncated.
    subjects = [
        {"name": "warnings", "hard": 10, "supplemental": 5000},
        {"name": "failure", "hard": 400, "supplemental": 0},
    ]
    alloc = K.allocate_budget(subjects, per_subject_cap=600, global_cap=410)
    assert alloc["failure"] == 400, alloc
    assert alloc["warnings"] == 10, alloc


def test_allocate_budget_does_not_depend_on_declaration_order():
    # An odd global budget is the discriminating case. An even one divides
    # exactly, so the sub-unit remainder branch — the only place declaration
    # order could leak in — never runs and the test proves nothing.
    subjects = [
        {"name": "x", "hard": 5000, "supplemental": 0},
        {"name": "y", "hard": 5000, "supplemental": 0},
    ]
    forward = K.allocate_budget(subjects, per_subject_cap=600, global_cap=801)
    backward = K.allocate_budget(
        list(reversed(subjects)), per_subject_cap=600, global_cap=801
    )
    assert forward == backward
    assert forward == {"x": 401, "y": 400}


def test_allocate_budget_never_starves_a_subject_another_could_crowd_out():
    # The property the spec calls a reserved minimum. Equal-share filling is
    # what delivers it; a rule that granted each subject its whole deficit in
    # turn would hand the first subject everything.
    subjects = [
        {"name": name, "hard": 5000, "supplemental": 0} for name in ("a", "b", "c")
    ]
    alloc = K.allocate_budget(subjects, per_subject_cap=600, global_cap=90)
    assert alloc == {"a": 30, "b": 30, "c": 30}


def test_allocate_budget_never_exceeds_either_cap():
    subjects = [
        {"name": f"h{i}", "hard": 10000, "supplemental": 10000} for i in range(10)
    ]
    alloc = K.allocate_budget(subjects, per_subject_cap=600, global_cap=2400)
    assert sum(alloc.values()) <= 2400
    assert max(alloc.values()) <= 600
    assert min(alloc.values()) > 0


def test_bound_extract_lines_truncates_an_oversized_line_wholesale():
    out, stats = K.bound_extract_lines(["ok", "x" * 9000], max_line_bytes=100)
    assert out[0] == "ok"
    assert out[1] == "[REDACTED: oversized line]"
    assert stats["oversized_lines"] == 1


def test_bound_extract_lines_stops_at_the_byte_ceiling_and_says_so():
    out, stats = K.bound_extract_lines(["y" * 50] * 100, max_total_bytes=500)
    assert stats["truncated"] is True
    assert stats["omitted_lines"] > 0
    assert any("omitted" in line for line in out[-1:]), out[-1:]


def test_bound_extract_lines_charges_its_own_truncation_notice():
    # The notice is bytes on the same budget. Appending it uncounted put the
    # measured output over a ceiling the function exists to enforce.
    out, stats = K.bound_extract_lines(["y" * 50] * 100, max_total_bytes=500)
    emitted = sum(len(line.encode("utf-8")) + 1 for line in out)
    assert emitted <= 500, (emitted, out)
    assert stats["truncated"] is True
    assert out[-1].startswith("[TRUNCATED:")
    assert stats["omitted_lines"] == 100 - (len(out) - 1)


# --------------------------------------------------------------- the transformer

# At least two canaries per class, plus a production-looking sentence that
# matches no secret pattern at all (the second `prose` entry), so a class
# cannot be declared covered on the strength of one lucky literal.
CANARY_CLASSES = {
    "path": [
        "/Users/testuser/.local/share/cctally/stats.db",
        "/Users/testuser/.claude/projects/foo/abc.jsonl",
    ],
    "email": [
        "maintainer@example.invalid",
        "billing.ops@acme-holdings.example.com",
    ],
    "uuid": [
        "908bcb51-4adb-41e2-ae92-727bac1acc6b",
        "3f2b1a0c-9d8e-4f7a-b6c5-d4e3f2a1b0c9",
    ],
    "credential": [
        "Authorization: Bearer sk-ant-oat01-abcdefghijklmnop",
        "api_key=sk-proj-QWERTYUIOPASDFGHJKL",
    ],
    "credential_url": [
        "https://user:hunter2@example.com/x",
        "postgres://svc:s3cr3tpw@db.internal.example/cctally",
    ],
    "opaque_token": [
        "a3f29c81b4d75e6f0a1c2b3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f6071",
        "ZXlKaGJHY2lPaUpJVXpJMU5pSXNJblI1Y0NJNklrcFhWQ0o5YWJjZGVmZ2g",
    ],
    "json_payload": [
        '{"role":"user","content":"my private prompt text"}',
        '{"cwd":"/Users/testuser/work/acme","project":"acme-billing"}',
    ],
    "prose": [
        "The reconciler decided the third quarter numbers were unusual.",
        "Our client asked whether the migration would delay their launch.",
    ],
}

CANARIES = {
    f"{cls}_{i}": raw
    for cls, members in CANARY_CLASSES.items()
    for i, raw in enumerate(members, start=1)
}


def test_every_canary_class_carries_at_least_two_members():
    thin = sorted(cls for cls, members in CANARY_CLASSES.items() if len(members) < 2)
    assert not thin, thin
    assert len(set(CANARIES.values())) == len(CANARIES)


# A stand-in for what the private caller builds from closed, repo-committed
# sources: harness names from the estate manifest, scenario and fixture names
# under `tests/fixtures/`, the reason-code registry in
# `bin/_lib-test-contract.sh`, and the words the shared harness scaffolding
# emits. Deliberately DEFAULT-DENY: it never enumerates what is dangerous, so
# a production identifier nobody registered is redacted for want of a member,
# not for looking suspicious.
KNOWN_TOKENS = frozenset({
    # harness names and the components they decompose into
    "cctally", "test", "all", "diff", "share", "forecast", "project",
    "reconcile", "statusline", "blocks", "migrations", "dashboard", "session",
    "daily", "monthly", "weekly", "report", "setup", "hook", "tick",
    # reason codes and their components
    "case", "floor", "unmet", "harness", "failed", "killed", "pytest",
    "summary", "unreadable", "outcome", "record", "missing", "exit",
    # the estate's own generated diagnostic vocabulary
    "fail", "pass", "warn", "passed", "details", "output", "log", "logs",
    "stdout", "stderr", "json", "golden", "diverged", "mismatch", "boom",
    "product", "infrastructure", "incomplete", "none", "verdict", "timing",
    "total", "shell", "pool", "complete", "in", "cases", "lines", "line",
    "dedup", "benchmark", "phase", "elapsed", "running", "queued", "done",
    "bin", "tests", "docs", "fixtures", "main", "syntax", "error",
    # Units. A unit is a WORD like any other and is registered, not exempted by
    # shape: the digit-adjacency rule that used to exempt `48s` exempted
    # `acme2026` with it.
    "s", "ms", "h", "d", "pp", "kib", "mib", "gib", "q",
    # The kernel's own PLACEHOLDER NAMESPACE, registered by name because
    # provenance rather than shape is what admits a token. A raw line arriving
    # with a literal `<home>` in it is judged word by word like any other, so
    # `<home>` survives on the strength of `home` being vouched for while
    # `<acme-holdings>` is redacted for want of an entry. The previous rule
    # exempted anything SHAPED like a placeholder, which admitted both.
    "home", "repo", "path", "tmp", "tmpdir", "temp", "runner", "private",
    "param", "email", "uuid", "hex", "credential", "url", "redacted",
    "unclassified", "detail", "oversized", "exception", "message",
    "truncated", "omitted",
})


def _ctx(**kw):
    kw.setdefault("known_tokens", KNOWN_TOKENS)
    kw.setdefault(
        "known_case_ids",
        {"top-level", "case_a_failed_canonical_run_keeps_a_mac_side_extract"},
    )
    return K.ScrubContext(
        roots={"home": "/Users/testuser", "repo": "/repo", "tmp": "/tmp"}, **kw
    )


def _closed_ctx():
    """Neither predicate supplied — the fail-closed default the public tree
    runs under, where no path is disclosable and no word is vouched for."""
    return K.ScrubContext(roots={"home": "/Users/testuser", "repo": "/repo",
                                 "tmp": "/tmp"})


def _open_ctx():
    """Both predicates supplied — what the private kernel injects."""
    return _ctx(is_public_path=lambda p: True)


@pytest.mark.parametrize("name,raw", sorted(CANARIES.items()))
def test_every_canary_is_absent_from_the_scrubbed_line(name, raw):
    # A harness marker interpolates arbitrary values, which is exactly how a
    # production-derived value reaches the console.
    carrier = f"FAIL: canary case {raw}"
    # Non-vacuity: the canary must really be in the input. Without this the
    # test would also pass against a transformer that never ran.
    assert raw in carrier
    out = K.scrub_line(carrier, _ctx())
    assert raw not in out, f"{name} survived: {out!r}"


@pytest.mark.parametrize("name,raw", sorted(CANARIES.items()))
def test_every_canary_is_absent_when_it_is_the_whole_line(name, raw):
    # Non-vacuity, stated as a falsifiable precondition rather than as
    # `raw in raw`: the independently written validator confirms the raw line
    # really carries the class this canary stands for. A canary that matched
    # no forbidden shape would fail here instead of passing on an empty
    # premise.
    assert K.validate_export([raw], ROOTS), f"{name} is not a secret shape"
    out = K.scrub_line(raw, _ctx())
    assert raw not in out, f"{name} survived: {out!r}"


def test_a_safe_control_line_survives_byte_for_byte():
    # Without this the suite would pass against a scrubber that redacts
    # everything, which is safe and useless. The line takes the ORDINARY path
    # — no structured prefix, no marker — so what it proves is that the
    # general classifier admits genuinely safe content.
    safe = "shell pool complete in 544s"
    assert K.scrub_line(safe, _ctx()) == safe


def test_a_generated_progress_line_survives_byte_for_byte():
    # The progress family reaches the export through the prefix path, whose
    # suffix is scrubbed; a real completion line must still come back whole.
    safe = "[ 12/56] FAIL  share             product      3 failed   112s"
    assert K.scrub_line(safe, _ctx()) == safe


# A sentence that no typed pattern matches, so the only thing that can stop
# it is the structural classification of the line that carries it.
TAIL_CANARY = "the client hated the quarterly numbers"

# One sample per verbatim pattern, index-aligned. Adding a pattern without
# adding its sample fails the length assertion below, which is the point: a
# seventh unanchored pattern cannot be slipped in unexamined.
VERBATIM_SAMPLES = (
    "",
    "-----",
    "@@ -1,4 +1,4 @@",
    "Traceback (most recent call last):",
)

PREFIX_SAMPLES = (
    "[ 12/56]",
    "[cctally-test-all]",
    "passed: 340",
    "Total:",
    "Timing:",
    "Verdict:",
)


def test_every_verbatim_pattern_is_anchored_at_end_of_line():
    # `re.match` is prefix-only, so an unanchored pattern in the verbatim tier
    # returns an arbitrary tail untouched. This is the static half of the
    # guard; the behavioural half is the next test.
    for pattern in K._STRUCTURED_VERBATIM:
        assert pattern.pattern.endswith(("$", r"\Z")), pattern.pattern


def test_no_verbatim_pattern_admits_an_arbitrary_tail():
    assert len(VERBATIM_SAMPLES) == len(K._STRUCTURED_VERBATIM)
    for idx, pattern in enumerate(K._STRUCTURED_VERBATIM):
        sample = VERBATIM_SAMPLES[idx]
        # Non-vacuity: the sample really is a member of this pattern's class,
        # and really does survive whole.
        assert pattern.match(sample), (idx, sample)
        assert K.scrub_line(sample, _ctx()) == sample, (idx, sample)
        tainted = f"{sample} {TAIL_CANARY}"
        assert TAIL_CANARY in tainted
        assert TAIL_CANARY not in K.scrub_line(tainted, _ctx()), (idx, tainted)


def test_no_verbatim_pattern_admits_a_canary_anywhere_inside_it():
    # An APPEND-ONLY guard cannot observe a free field in the MIDDLE of a
    # pattern: appending to `---- share FAIL details ----` breaks the required
    # trailing dashes, so the tainted line stops matching and the guard passes
    # while the hole stays open. Inserting at every position is what makes a
    # mid-pattern free field visible.
    assert len(VERBATIM_SAMPLES) == len(K._STRUCTURED_VERBATIM)
    for idx, sample in enumerate(VERBATIM_SAMPLES):
        for cut in range(len(sample) + 1):
            tainted = sample[:cut] + TAIL_CANARY + sample[cut:]
            # Non-vaciuty, per position: the canary really is in the carrier
            # at the position under test.
            assert tainted[cut:cut + len(TAIL_CANARY)] == TAIL_CANARY
            out = K.scrub_line(tainted, _ctx())
            assert TAIL_CANARY not in out, (idx, cut, tainted, out)


def test_no_prefix_pattern_admits_a_canary_anywhere_inside_it():
    for head in PREFIX_SAMPLES:
        for cut in range(len(head) + 1):
            tainted = head[:cut] + TAIL_CANARY + head[cut:] + " tail"
            assert TAIL_CANARY in tainted
            out = K.scrub_line(tainted, _ctx())
            assert TAIL_CANARY not in out, (head, cut, out)


def test_no_prefix_pattern_passes_its_tail_through():
    assert len(PREFIX_SAMPLES) == len(K._STRUCTURED_PREFIX)
    for pattern, head in zip(K._STRUCTURED_PREFIX, PREFIX_SAMPLES):
        line = f"{head} {TAIL_CANARY}"
        match = pattern.match(line)
        # Non-vacuity: the pattern matches, and matches only the generated
        # head, so everything after it is content the transformer must scrub.
        assert match and match.end() <= len(head), (pattern.pattern, line)
        out = K.scrub_line(line, _ctx())
        assert out.startswith(head), out
        assert TAIL_CANARY not in out, out


@pytest.mark.parametrize("line,secret", [
    ("Timing: /root/secrets/prod.db", "/root/secrets/prod.db"),
    ("[ 3/56] the merger with Acme closes on Friday",
     "the merger with Acme closes on Friday"),
    ("[cctally-test-all] client Acme wants the numbers today",
     "client Acme wants the numbers today"),
    ("passed: 3 and the client hated it", "and the client hated it"),
    ("Total: /Volumes/EXTERNAL/repos/cctally-dev/tests/fixtures/x",
     "/Volumes/EXTERNAL/repos/cctally-dev/tests/fixtures/x"),
])
def test_a_structured_prefix_never_discloses_what_follows_it(line, secret):
    # Non-vacuity: the secret is genuinely present in the carrier line, at the
    # exact position the prefix rule used to wave through.
    assert secret in line
    out = K.scrub_line(line, _ctx())
    assert secret not in out, out
    assert K.validate_export([out], ROOTS) == [], out


def test_registered_reason_codes_and_counters_survive():
    for safe in [
        "FAIL",
        "passed: 340   failed: 0",
        "case-floor-unmet",
        "Timing: total=1054s  shell-pool=548s  pytest=506s",
        "[cctally-test-all] shell pool complete",
        "Verdict: product",
    ]:
        assert K.scrub_line(safe, _ctx()) == safe, safe


def test_a_marker_keeps_its_prefix_and_scrubs_its_dynamic_suffix():
    line = "FAIL: dedup /Users/testuser/.claude/projects/x/a.jsonl"
    out = K.scrub_line(line, _ctx())
    assert out.startswith("FAIL:")
    assert "/Users/testuser" not in out
    assert "<home>" in out


def test_a_source_classified_test_remote_case_identifier_survives_verbatim():
    line = (
        "CASE: test-remote/"
        "case_a_failed_canonical_run_keeps_a_mac_side_extract line 5624"
    )
    assert K.scrub_line(line, _ctx()) == line
    assert K.validate_export([line], {"repo": "/repo"}) == []


def test_an_unknown_test_remote_case_identifier_fails_closed():
    line = "CASE: test-remote/case_acme_holdings_invoice line 5624"
    out = K.scrub_line(line, _ctx())
    assert "acme" not in out
    assert "invoice" not in out
    assert "REDACTED" in out


def test_a_marker_suffix_carrying_a_json_payload_is_redacted():
    # A marker prefix is generated text; its suffix is not, and a structured
    # payload interpolated after the prefix must not ride out on the marker's
    # authority.
    line = 'FAIL: replay {"role":"user","content":"my private prompt text"}'
    out = K.scrub_line(line, _ctx())
    assert out.startswith("FAIL:")
    assert "private prompt text" not in out


def test_unclassifiable_prose_is_redacted_not_passed_through():
    out = K.scrub_line(CANARIES["prose_1"], _ctx())
    assert out == "[REDACTED: unclassified line]"


def test_prose_carrying_a_known_root_is_still_redacted():
    # Substituting a known root does not make the rest of the sentence safe.
    line = (
        "The reconciler read /Users/testuser/x and decided the client "
        "numbers were unusual."
    )
    out = K.scrub_line(line, _ctx())
    assert "reconciler" not in out
    assert "/Users/testuser" not in out


def test_a_repo_relative_path_is_redacted_without_a_public_predicate():
    out = K.scrub_line("bin/cctally-test-remote:1955: boom", _ctx())
    assert "cctally-test-remote" not in out


def test_a_repo_relative_path_survives_when_the_predicate_says_public():
    ctx = _ctx(is_public_path=lambda p: p == "bin/cctally-test-all")
    out = K.scrub_line("bin/cctally-test-all:373: boom", ctx)
    assert "bin/cctally-test-all" in out
    out2 = K.scrub_line("bin/cctally-test-remote:1: boom", ctx)
    assert "cctally-test-remote" not in out2


def test_diff_coordinates_survive_but_payload_without_provenance_does_not():
    ctx = _ctx()
    assert K.scrub_line("@@ -1,4 +1,4 @@", ctx) == "@@ -1,4 +1,4 @@"
    out = K.scrub_line("+  some actual golden payload", ctx)
    assert out == "[REDACTED: unclassified line]"


def test_a_diff_context_line_is_redacted_without_provenance():
    # A unified diff's context lines carry the same payload its `+` and `-`
    # lines do; the only difference is the leading character.
    # The payload is deliberately one the ordinary classifier would admit, so
    # the test discriminates: it fails against a transformer that has no
    # context-line branch at all.
    payload = "share diff diverged 340 cases"
    line = f" {payload}"
    assert payload in line
    assert K._is_ordinary(payload, _ctx())
    out = K.scrub_line(line, _ctx())
    assert payload not in out, out
    assert out == K.UNCLASSIFIED_PLACEHOLDER, out


def test_a_pytest_node_identifier_is_gated_on_the_public_path_predicate():
    ctx = _ctx(is_public_path=lambda p: p == "tests/test_share.py")
    assert K.scrub_line("FAILED tests/test_share.py::test_x[case-1]", ctx) == (
        "FAILED tests/test_share.py::test_x[<param>]"
    )
    # A path the predicate answers "not public" for. It is deliberately a
    # name no file in this tree carries: a public test that spells a real
    # mirror-private path breaks the published suite, which
    # `tests/test_public_test_dep_closure.py` exists to catch.
    private = "FAILED tests/test_a_private_harness.py::test_ledger"
    assert "tests/test_a_private_harness.py" in private
    assert not ctx.path_is_public("tests/test_a_private_harness.py")
    out = K.scrub_line(private, ctx)
    assert out == "FAILED <path>::test_ledger", out


def test_a_pytest_short_frame_keeps_only_public_path_line_and_exception_class():
    ctx = _ctx(is_public_path=lambda p: p == "tests/test_share.py")
    assert K.scrub_line("tests/test_share.py:42: AssertionError", ctx) == (
        "tests/test_share.py:42: AssertionError"
    )
    assert K.scrub_line(
        "tests/test_a_private_harness.py:9: AssertionError", ctx
    ) == "<path>:9: AssertionError"


def test_a_pytest_assertion_keeps_safe_structure_and_scrubs_dynamic_values():
    safe = ">       assert 2 + 2 == 5"
    assert K.scrub_line(safe, _ctx()) == safe

    secret = "/Users/testuser/.claude/projects/x/secret.jsonl"
    scrubbed = K.scrub_line(f">       assert False, {secret!r}", _ctx())
    assert scrubbed.startswith(">       assert")
    assert secret not in scrubbed
    assert "/Users/testuser" not in scrubbed


def test_summary_line_retains_node_and_exception_class():
    ctx = _closed_ctx()          # neither predicate supplied
    out = K.scrub_line(
        "FAILED tests/x.py::test_y - TimeoutError: timed out", ctx)
    assert out == "FAILED <path>::test_y - TimeoutError: [REDACTED: exception message]"


def test_indented_summary_line_takes_the_node_rule_not_the_marker_rule():
    ctx = _closed_ctx()
    out = K.scrub_line(
        "  FAILED tests/x.py::test_y - TimeoutError: timed out", ctx)
    assert out == "  FAILED <path>::test_y - TimeoutError: [REDACTED: exception message]"


def test_parameter_values_are_still_normalized_in_the_tail_form():
    ctx = _open_ctx()            # both predicates supplied
    out = K.scrub_line(
        "FAILED tests/x.py::test_y[acme_private_token] - ValueError: bad", ctx)
    assert "acme_private_token" not in out
    assert out == "FAILED tests/x.py::test_y[<param>] - ValueError: [REDACTED: exception message]"


# A parameter id is production content, and pytest writes it into the node id
# verbatim — spaces, brackets and all. `tests/` holds 309 parametrize argument
# constants containing a space, so every form below is reachable today.
UNSAFE_PARAMETER_IDS = (
    "elapsed_hours<24-less than 24 hours into the week",
    "the client hated the quarterly numbers",
    "nested[inner]value",
    "trailing]bracket",
    "acme_private_token",
)


@pytest.mark.parametrize("param", UNSAFE_PARAMETER_IDS)
@pytest.mark.parametrize("ctx_name", ("closed", "open"))
def test_a_parameter_id_is_normalized_whole_in_both_contexts(ctx_name, param):
    # The node group used to stop at the first whitespace, so a parameter id
    # carrying a space was truncated mid-value and the truncated span — which
    # has no closing bracket — was never normalized. The fragment was then
    # published: the transformer emitted it and the validator could not see
    # it either, because its leg required the span to close.
    ctx = _closed_ctx() if ctx_name == "closed" else _open_ctx()
    raw = f"FAILED tests/x.py::test_y[{param}] - ValueError: boom"
    out = K.scrub_line(raw, ctx)
    for word in param.split():
        assert word not in out, (raw, out)
    assert "[<param>]" in out, out
    assert K.validate_export([out], ROOTS) == [], out


def test_a_class_nested_node_id_keeps_its_class_and_normalizes_its_parameter():
    # The widened node group must still admit the `::Class::test` form, whose
    # second separator is neither whitespace nor a bracket.
    out = K.scrub_line(
        "FAILED tests/x.py::TestThing::test_y[secret value] - ValueError: b",
        _open_ctx(),
    )
    assert out == (
        "FAILED tests/x.py::TestThing::test_y[<param>] - "
        "ValueError: [REDACTED: exception message]"
    ), out


@pytest.mark.parametrize("param", UNSAFE_PARAMETER_IDS)
@pytest.mark.parametrize("ctx_name", ("closed", "open"))
def test_no_retained_bracket_span_is_anything_but_the_placeholder(ctx_name, param):
    # The invariant behind the normalizer, asserted over the EMITTED bytes:
    # every `[` a node line retains opens either the parameter placeholder or
    # one of the kernel's own redaction placeholders, and nothing else.
    ctx = _closed_ctx() if ctx_name == "closed" else _open_ctx()
    allowed = {
        "[<param>]",
        K.EXCEPTION_MESSAGE_PLACEHOLDER,
        K.UNCLASSIFIED_PLACEHOLDER,
        K.UNCLASSIFIED_DETAIL,
        K.JSON_PLACEHOLDER,
    }
    for raw in (
        f"FAILED tests/x.py::test_y[{param}]",
        f"FAILED tests/x.py::test_y[{param}] - ValueError: boom",
        f"  ERROR tests/x.py::test_y[{param}] - ValueError: boom",
        f"FAILED tests/x.py::test_y[{param}",
    ):
        out = K.scrub_line(raw, ctx)
        spans = re.findall(r"\[[^\[\]]*\]", out)
        assert set(spans) <= allowed, (raw, out, spans)
        assert out.count("[") == len(spans), (raw, out)


def test_unrecognized_tail_class_drops_the_tail_and_keeps_the_node():
    ctx = _open_ctx()
    out = K.scrub_line("FAILED tests/x.py::test_y - some prose here", ctx)
    assert out == "FAILED tests/x.py::test_y"


def test_e_gutter_retains_the_exception_class():
    ctx = _closed_ctx()
    out = K.scrub_line("E   TimeoutError: timed out", ctx)
    assert out == "E   TimeoutError: [REDACTED: exception message]"


def test_e_gutter_assertion_form_is_unchanged():
    ctx = _closed_ctx()
    line = "E       assert 1 == 2"
    assert K.scrub_line(line, ctx) == line


def test_nested_e_gutter_does_not_recurse():
    ctx = _closed_ctx()
    out = K.scrub_line("E   E   TimeoutError: timed out", ctx)
    assert out == "E   " + K.UNCLASSIFIED_PLACEHOLDER


def test_counters_line_is_retained_without_a_vocabulary():
    ctx = _closed_ctx()
    line = "1 failed, 100 passed, 3 skipped in 45.67s"
    assert K.scrub_line(line, ctx) == line


def test_counters_banner_form_is_retained_without_a_vocabulary():
    ctx = _closed_ctx()
    line = "=========== 1 failed, 100 passed in 12.34s ==========="
    assert K.scrub_line(line, ctx) == line


def test_counters_grammar_rejects_an_unknown_word():
    ctx = _closed_ctx()
    out = K.scrub_line("1 failed, 100 sprocketed in 45.67s", ctx)
    assert out == K.UNCLASSIFIED_PLACEHOLDER


def test_counters_grammar_rejects_trailing_prose():
    ctx = _closed_ctx()
    out = K.scrub_line("1 failed, 100 passed in 45.67s -- on host alpha", ctx)
    assert out == K.UNCLASSIFIED_PLACEHOLDER


def test_counters_grammar_admits_the_parenthetical_duration_pytest_really_emits():
    # `format_session_duration` appends ` (H:MM:SS)` once a session runs a
    # minute or longer, and every authoritative pytest leg does. Without this
    # form the rule would be dead on the only line it exists for.
    ctx = _closed_ctx()
    line = "==== 1 failed, 340 passed, 3 skipped in 506.20s (0:08:26) ===="
    assert K.scrub_line(line, ctx) == line


def test_the_counters_rule_is_anchored_at_both_ends():
    assert K._PYTEST_COUNTERS_RE.pattern.startswith("^")
    assert K._PYTEST_COUNTERS_RE.pattern.endswith("$")


def test_the_counters_rule_admits_no_canary_at_any_position():
    # The append-only form of this guard cannot see a free field in the
    # middle of an anchored pattern, so the canary is inserted at every
    # position instead.
    sample = "1 failed, 100 passed, 3 skipped in 45.67s"
    assert K.scrub_line(sample, _closed_ctx()) == sample
    for cut in range(len(sample) + 1):
        tainted = sample[:cut] + TAIL_CANARY + sample[cut:]
        assert tainted[cut:cut + len(TAIL_CANARY)] == TAIL_CANARY
        out = K.scrub_line(tainted, _closed_ctx())
        assert TAIL_CANARY not in out, (cut, tainted, out)


def test_the_node_tail_never_retains_an_exception_message():
    # The message is captured only so it cannot ride out on the node rule's
    # authority; it must never appear in the output.
    ctx = _open_ctx()
    secret = "the client hated the quarterly numbers"
    out = K.scrub_line(f"FAILED tests/x.py::test_y - ValueError: {secret}", ctx)
    assert secret not in out, out
    assert out.endswith(K.EXCEPTION_MESSAGE_PLACEHOLDER), out


def test_the_gutter_authorizes_no_payload_of_its_own():
    ctx = _closed_ctx()
    secret = "the client hated the quarterly numbers"
    out = K.scrub_line(f"E   {secret}", ctx)
    assert secret not in out, out
    assert out == "E   " + K.UNCLASSIFIED_PLACEHOLDER, out


def test_the_marker_vocabulary_and_the_marker_prefix_rule_agree():
    # `SUPPLEMENTAL_MARKERS` requires the colon; the prefix rule must require
    # it too, or `WARN` and `WARN:` are two vocabularies pretending to be one.
    for marker in K.HARD_MARKERS + K.SUPPLEMENTAL_MARKERS:
        assert marker in K._MARKER_PREFIX_RE.pattern, marker
    assert K.classify_failure_marker("WARN careful") is None
    # A bare `WARN` is not a marker, so it must not buy its line a retained
    # prefix. Free-form text after it is redacted whole, not after a `WARN`.
    assert K.scrub_line("WARN the client hated the numbers", _ctx()) == (
        K.UNCLASSIFIED_PLACEHOLDER
    )
    assert K.scrub_line("WARN: the client hated the numbers", _ctx()) == (
        f"WARN: {K.UNCLASSIFIED_DETAIL}"
    )


def test_the_vocabulary_boundary_is_pinned_from_both_sides():
    # Without both sides the mechanism floats: a rule that vouched for
    # everything, or for nothing, would leave one of these two assertions
    # green on its own.
    vouched = "shell pool complete in 544s"
    assert K.unknown_vocabulary(vouched, _ctx()) == []
    assert K.scrub_line(vouched, _ctx()) == vouched
    # One word changed, and only that word is outside the vocabulary.
    unvouched = "shell pool complete quickly in 544s"
    assert K.unknown_vocabulary(unvouched, _ctx()) == ["quickly"]
    assert K.scrub_line(unvouched, _ctx()) == K.UNCLASSIFIED_PLACEHOLDER


@pytest.mark.parametrize("fragment", [
    "The client hates this",
    "client asked about layoffs",
    "FAIL: golden mismatch for example-billing",
])
def test_a_short_production_fragment_is_redacted_by_provenance(fragment):
    # These are four-word fragments, which is exactly the length a linguistic
    # rule reads as a label rather than as a sentence. Tuning that rule moved
    # the boundary without closing the class; the vocabulary rule closes it,
    # because none of these words is one this repository registered.
    assert K.unknown_vocabulary(fragment, _ctx())
    scrubbed = K.scrub_line(fragment, _ctx())
    assert fragment not in scrubbed, scrubbed
    for word in ("client", "layoffs", "example", "billing"):
        assert word not in scrubbed, (word, scrubbed)


def test_a_diff_file_header_has_its_path_normalized():
    ctx = _ctx()
    out = K.scrub_line("--- /Users/testuser/.claude/projects/x/a.jsonl", ctx)
    assert out.startswith("---")
    assert "/Users/testuser" not in out


def test_a_section_rule_carrying_a_path_is_not_waved_through():
    out = K.scrub_line("---- /Users/testuser/secret ----", _ctx())
    assert "/Users/testuser" not in out


def test_json_of_an_unknown_schema_is_redacted_whole():
    out = K.scrub_line('{"unknown_key": "value", "other": 1}', _ctx())
    assert "value" not in out


def test_an_exception_line_keeps_its_class_and_drops_its_message():
    out = K.scrub_line("ValueError: user maintainer@example.invalid is unknown", _ctx())
    assert out.startswith("ValueError:")
    assert "maintainer@example.invalid" not in out
    assert "unknown" not in out


def test_a_traceback_frame_keeps_a_public_file_and_line_only():
    ctx = _ctx(is_public_path=lambda p: p == "bin/cctally-test-all")
    out = K.scrub_line('  File "/repo/bin/cctally-test-all", line 42, in main', ctx)
    assert out == '  File "bin/cctally-test-all", line 42, in main'
    private = K.scrub_line('  File "/repo/bin/cctally-test-remote", line 9, in x', ctx)
    assert "cctally-test-remote" not in private
    assert "line 9" in private


def test_build_scrubbed_extract_labels_each_subject_and_honours_allocation():
    subject_lines = {
        "share": ["FAIL: a", "detail one", "detail two"],
        "diff": ["FAIL: b"],
    }
    alloc = {"share": 4, "diff": 2}
    out = K.build_scrubbed_extract(subject_lines, _ctx(), alloc)
    text = "\n".join(out)
    assert "share" in text and "diff" in text
    assert sum(1 for line in out if line.startswith("FAIL:")) == 2


def test_build_scrubbed_extract_retains_the_earliest_and_latest_clusters():
    # The spec's rule when markers exceed the budget: keep both ends, state
    # how many lines went, and name the log that still holds all of them.
    # Taking the first `budget` lines drops the latest cluster entirely, which
    # is usually the one that ended the run.
    lines = [f"FAIL: case {i}" for i in range(10)]
    out = K.build_scrubbed_extract({"share": lines}, _ctx(), {"share": 4})
    body = [line for line in out if not line.startswith("---- ")]
    assert body[0] == "FAIL: case 0"
    assert body[-1] == "FAIL: case 9"
    notices = [line for line in body if line.startswith("[OMITTED:")]
    assert len(notices) == 1, body
    assert "8" in notices[0], notices[0]
    assert "logs/share.log" in notices[0], notices[0]
    assert K.validate_export(out, ROOTS) == []


def test_build_scrubbed_extract_names_a_caller_supplied_log():
    out = K.build_scrubbed_extract(
        {"share": [f"FAIL: case {i}" for i in range(6)]},
        _ctx(),
        {"share": 3},
        log_names={"share": "logs/share-rerun.log"},
    )
    assert any("logs/share-rerun.log" in line for line in out), out


def test_build_scrubbed_extract_does_not_split_a_subject_within_budget():
    lines = [f"FAIL: case {i}" for i in range(3)]
    out = K.build_scrubbed_extract({"share": lines}, _ctx(), {"share": 4})
    assert out == ["---- share ----", *lines]


def test_build_scrubbed_extract_skips_a_subject_with_no_budget():
    assert K.build_scrubbed_extract({"s": ["FAIL: diff"]}, _ctx(), {"s": 0}) == []
    # One line buys a header with nothing under it, which reports nothing
    # the run manifest does not already carry.
    assert K.build_scrubbed_extract({"s": ["FAIL: diff"]}, _ctx(), {"s": 1}) == []
    assert K.build_scrubbed_extract({"s": ["FAIL: diff"]}, _ctx(), {"s": 2}) == [
        "---- s ----", "FAIL: diff",
    ]


def test_build_scrubbed_extract_charges_its_header_and_its_notice():
    # The header and the `[OMITTED: …]` notice are lines in the emitted file,
    # so a budget that does not charge them is not a budget. Emitting six
    # lines against an allocation of four made the run-wide cap advisory:
    # 56 subjects at 4 allocated 224 lines and emitted 336.
    lines = [f"FAIL: case {i}" for i in range(20)]
    out = K.build_scrubbed_extract({"share": lines}, _ctx(), {"share": 4})
    assert len(out) == 4, out
    assert out[0] == "---- share ----"
    assert sum(1 for line in out if line.startswith("[OMITTED:")) == 1


def test_the_whole_extract_stays_inside_the_run_wide_allocation():
    subjects = {f"h{i:02d}": [f"FAIL: case {n}" for n in range(30)] for i in range(56)}
    alloc = {name: 4 for name in subjects}
    out = K.build_scrubbed_extract(subjects, _ctx(), alloc)
    # Non-vacuity: every subject really did overflow its allocation, so the
    # accounting is exercised rather than trivially satisfied.
    assert all(len(v) > alloc[k] for k, v in subjects.items())
    assert len(out) <= sum(alloc.values()), (len(out), sum(alloc.values()))


def test_build_scrubbed_extract_does_not_split_a_cluster():
    # The spec retains the earliest and latest CLUSTERS. A flat line list has
    # no cluster boundaries in it, so a line-level head and tail cuts through
    # the middle of one — and at a tight budget drops the latest cluster whole,
    # which is usually the cluster that ended the run.
    lines = [f"FAIL: case {i}" for i in range(6)]
    clusters = {"share": [(0, 2), (3, 5)]}
    out = K.build_scrubbed_extract(
        {"share": lines}, _ctx(), {"share": 6}, clusters=clusters
    )
    body = [line for line in out if not line.startswith(("---- ", "[OMITTED:"))]
    assert body == ["FAIL: case 3", "FAIL: case 4", "FAIL: case 5"], body
    # The discriminating half: a line-level split at the same budget would
    # have produced the first two and the last two lines, cutting both
    # clusters in half.
    flat = K.build_scrubbed_extract({"share": lines}, _ctx(), {"share": 6})
    flat_body = [line for line in flat
                 if not line.startswith(("---- ", "[OMITTED:"))]
    assert flat_body == ["FAIL: case 0", "FAIL: case 1",
                         "FAIL: case 4", "FAIL: case 5"], flat_body


def test_cluster_boundaries_may_arrive_in_the_shape_select_failure_windows_emits():
    # The windows the kernel already computes are dicts, so accepting them
    # directly removes the one place the two shapes could be wired up wrong
    # without failing loudly.
    lines = [f"FAIL: case {i}" for i in range(6)]
    windows = [{"start": 0, "end": 2, "kind": "hard"},
               {"start": 3, "end": 5, "kind": "hard"}]
    as_dicts = K.build_scrubbed_extract(
        {"share": lines}, _ctx(), {"share": 6}, clusters={"share": windows}
    )
    as_pairs = K.build_scrubbed_extract(
        {"share": lines}, _ctx(), {"share": 6}, clusters={"share": [(0, 2), (3, 5)]}
    )
    assert as_dicts == as_pairs


def test_build_scrubbed_extract_retains_both_ends_when_both_clusters_fit():
    lines = [f"FAIL: case {i}" for i in range(6)]
    out = K.build_scrubbed_extract(
        {"share": lines}, _ctx(), {"share": 6}, clusters={"share": [(0, 1), (4, 5)]}
    )
    body = [line for line in out if not line.startswith(("---- ", "[OMITTED:"))]
    assert body == ["FAIL: case 0", "FAIL: case 1",
                    "FAIL: case 4", "FAIL: case 5"], body


def _numbered_log(total=300):
    # Vouched filler, so a retained line is legible and the test can say
    # exactly which indices came back rather than counting placeholders.
    return [f"line {i}" for i in range(total)]


def test_build_scrubbed_extract_selects_from_the_clusters_not_the_ends():
    # The spec retains the earliest and latest CLUSTERS. A head and tail
    # measured from the ends of the LINE LIST retains neither when the clusters
    # sit inside the log, which is where a failure normally sits — so the
    # extract contained none of the failure and was identical to the one
    # produced by passing no clusters at all. Every cluster fixture in this
    # file put its clusters at the ends of a six-line list, where "earliest and
    # latest clusters" and "earliest and latest lines" are the same answer.
    lines = _numbered_log()
    lines[122] = "FAIL share: stdout diverged"
    lines[202] = "FAIL diff: json diverged"
    clusters = {"share": [(120, 124), (200, 204)]}
    out = K.build_scrubbed_extract(
        {"share": lines}, _ctx(), {"share": 12}, clusters=clusters
    )
    assert len(out) <= 12, out
    body = [line for line in out if not line.startswith(("---- ", "[OMITTED:"))]
    assert body == lines[120:125] + lines[200:205], body
    # The discriminating half: the same call with no cluster boundaries keeps
    # the ends of the log and neither marker, which is what the cluster
    # argument was silently producing.
    flat = K.build_scrubbed_extract({"share": lines}, _ctx(), {"share": 12})
    flat_body = [
        line for line in flat if not line.startswith(("---- ", "[OMITTED:"))
    ]
    assert flat_body[0] == "line 0", flat_body
    assert not any("FAIL" in line for line in flat_body), flat_body
    assert out != flat


def test_a_cluster_larger_than_the_budget_is_truncated_around_its_marker():
    # The realistic shape: one marker two thirds of the way into a 300-line
    # log, so `select_failure_windows` returns a single window far larger than
    # the budget. Retaining the window's arbitrary ends drops the marker; the
    # marker is the point, and the forty preceding lines are the context this
    # session's window rewrite exists to keep.
    lines = _numbered_log()
    lines[150] = "FAIL share: stdout diverged"
    windows = K.select_failure_windows(lines)
    assert windows == [
        {"kind": "hard", "marker_index": 150, "start": 110, "end": 299}
    ], windows
    out = K.build_scrubbed_extract(
        {"share": lines}, _ctx(), {"share": 42}, clusters={"share": windows}
    )
    body = [line for line in out if not line.startswith(("---- ", "[OMITTED:"))]
    assert len(out) <= 42
    assert lines[150] in body, body[:3]
    assert body[-1] == lines[150], body[-3:]
    assert body[0] == "line 111", body[:3]
    # And the head of the LOG, which is what the previous rule returned, is
    # nowhere in it.
    assert "line 0" not in body


def test_cluster_indices_must_address_the_lines_they_are_given_with():
    # `select_failure_windows` indexes the ORIGINAL log, so `subject_lines`
    # must be that same list. The alternative reading — indices into the
    # concatenated windows — is silently incompatible, and Task 6 is the first
    # caller, so the mis-wiring has to fail loudly rather than produce a
    # plausible extract from the wrong lines.
    lines = [f"line {i}" for i in range(10)]
    with pytest.raises(ValueError):
        K.build_scrubbed_extract(
            {"share": lines}, _ctx(), {"share": 6}, clusters={"share": [(4, 40)]}
        )
    with pytest.raises(ValueError):
        K.build_scrubbed_extract(
            {"share": lines}, _ctx(), {"share": 6}, clusters={"share": [(-2, 3)]}
        )


def test_the_fixed_traceback_banner_survives():
    # A literal with no free field, so retaining it costs nothing and makes
    # the frames that follow readable.
    banner = "Traceback (most recent call last):"
    assert K.scrub_line(banner, _ctx()) == banner


# ------------------------------------------------ provenance, not linguistics

# Production-shaped text that no secret pattern matches and that is lexically
# indistinguishable from this repository's own identifiers. `case-floor-unmet`
# and `acme-holdings-billing` have the same shape, so no rule about how text
# LOOKS can separate them; only a rule about where it CAME FROM can.
PRODUCTION_SHAPED = (
    "FAIL project: -Users-testuser-work-acme-holdings-billing",
    "RECONCILE FAIL account_scope acme-holdings-billing: mismatch",
    "FAIL share: branch feat-acme-merger-q3-pricing diverged",
    "cwd -Users-testuser-clients-acme-2026-merger",
    "row 3 acme holdings billing 4211",
    "----------- Q3 revenue missed plan -----------",
    "--- acme-holdings-billing",
)


@pytest.mark.parametrize("line", PRODUCTION_SHAPED)
def test_text_outside_the_repositorys_vocabulary_does_not_survive(line):
    # Non-vacuity, asserted against the carrier: the words that must not
    # survive really are in the input, and really are not vouched for.
    unvouched = [
        word for word in re.findall(r"[A-Za-z]+", line)
        if word.lower() not in KNOWN_TOKENS
    ]
    assert unvouched, line
    out = K.scrub_line(line, _ctx())
    for word in unvouched:
        assert word not in out, (word, out)


# Two shape rules used to exempt a token from the vocabulary check entirely, so
# each of these came back byte-identical with the validator reporting nothing.
# A rule about how a token LOOKS cannot separate `q3` from `acme2026`, or
# `acme-holdings/` from a path, which is the whole content of the module's own
# "PROVENANCE, not linguistics" docstring.
SHAPE_EXEMPTION_BYPASS = (
    # Digit adjacency: any maximal letter run touching a digit was exempt.
    "FAIL project: acme2026",
    "FAIL project: acme2026merger",
    "FAIL share: 2acme",
    "RECONCILE FAIL acme3: mismatch",
    "line 3 acme2 holdings3 billing4 4211",
    # A chunk carrying a slash was skipped on the assumption that
    # `_substitute_paths` had already decided about it. A single segment with a
    # trailing slash matches neither path rule, so nothing had decided about it
    # at all — and `ls -d`, `find -type d`, `rsync` and `cwd` all emit that shape.
    "FAIL project: acme-holdings/",
    "logs acme-holdings-billing/",
)


@pytest.mark.parametrize("line", SHAPE_EXEMPTION_BYPASS)
def test_no_shape_rule_exempts_a_token_from_the_vocabulary(line):
    # Non-vacuity, asserted against the carrier: the words that must not
    # survive really are in the input, and really are not vouched for.
    unvouched = [
        word for word in re.findall(r"[A-Za-z]+", line)
        if word.lower() not in KNOWN_TOKENS
    ]
    assert unvouched, line
    out = K.scrub_line(line, _ctx())
    for word in unvouched:
        assert word not in out, (word, out)


def test_unknown_vocabulary_reports_a_digit_adjacent_word():
    # The kernel-level statement of the same defect: the transformer's decision
    # rests on this list, and the list was empty for every one of these.
    assert K.unknown_vocabulary("acme2026merger", _ctx()) == ["acme", "merger"]
    assert K.unknown_vocabulary("2acme", _ctx()) == ["acme"]
    assert K.unknown_vocabulary("acme-holdings/", _ctx()) == ["acme", "holdings"]


def test_a_dash_encoded_project_directory_does_not_survive():
    # The decisive case. `~/.claude/projects/` names are dash-encoded absolute
    # paths, so root substitution finds no prefix to replace, the absolute-path
    # rule finds no slash, and a linguistic rule finds no isolated words.
    # `bin/cctally-reconcile-test` reads that estate, which is why a marker's
    # suffix is untrusted in the first place.
    encoded = "-Users-testuser-work-acme-holdings-billing"
    line = f"FAIL project: {encoded}"
    assert encoded in line
    out = K.scrub_line(line, _ctx())
    assert out.startswith("FAIL")
    assert "acme" not in out, out
    assert "holdings" not in out, out


def test_numbers_units_and_placeholders_need_no_vocabulary_entry():
    # Numbers and this kernel's own placeholders carry no words to vouch for. A
    # unit does, and is registered in the vocabulary rather than exempted by
    # its shape — see `test_no_shape_rule_exempts_a_token_from_the_vocabulary`
    # for what the shape rule admitted along with `48s`.
    for safe in [
        "[ 38/56] PASS  diff  340 cases  48s",
        "Timing: total=1054s  shell-pool=548s  pytest=506s",
        "FAIL: dedup <home><path>",
    ]:
        assert K.scrub_line(safe, _ctx()) == safe, safe


def test_an_em_dash_alone_does_not_redact_a_vouched_line():
    # The estate writes em dashes in its own generated diagnostics, so a
    # character class that excluded them redacted those lines for the
    # punctuation alone — before the vocabulary check had any say, which means
    # registering every word in the line would not have made it legible.
    line = "FAIL diff: stdout diverged — 3 cases"
    assert "—" in line
    assert K.unknown_vocabulary(line, _ctx()) == []
    assert K.scrub_line(line, _ctx()) == line
    # And the punctuation buys nothing on its own: an unvouched word beside it
    # is still redacted, so this is not a hole the dash opened.
    assert K.scrub_line("FAIL diff: acme — 3 cases", _ctx()) == (
        f"FAIL {K.UNCLASSIFIED_DETAIL}"
    )


def test_the_vocabulary_check_fails_closed_with_no_known_tokens():
    # Same contract as `is_public_path`: with nothing injected, nothing is
    # vouched for, so the published tree redacts rather than discloses.
    bare = K.ScrubContext(roots={"home": "/Users/testuser"})
    assert bare.token_is_known("diff") is False
    line = "FAIL diff: stdout diverged"
    assert K.scrub_line(line, _ctx()) == line, "the premise: vouched text survives"
    assert K.scrub_line(line, bare) != line


def test_a_repo_relative_path_is_not_re_judged_as_vocabulary():
    # A path already has its own provenance rule. Re-judging its segments as
    # words would redact exactly the public paths the predicate admitted.
    ctx = _ctx(is_public_path=lambda p: p == "bin/cctally-review-public-mirror-diff")
    out = K.scrub_line("bin/cctally-review-public-mirror-diff:12: FAIL", ctx)
    assert out == "bin/cctally-review-public-mirror-diff:12: FAIL", out


# ------------------------------------------------------------ the section rule


@pytest.mark.parametrize("line,secret", [
    ("---- the client hated the numbers ----", "the client hated the numbers"),
    ("----------- Q3 revenue missed plan -----------", "revenue missed plan"),
    ("---- acme holdings billing 4211 ----", "acme holdings billing"),
])
def test_a_section_rules_body_is_a_free_field_and_is_scrubbed(line, secret):
    # The body was a 120-character free-text window returned byte for byte on
    # the strength of the dashes around it.
    assert secret in line
    out = K.scrub_line(line, _ctx())
    assert secret not in out, out
    assert K.validate_export([out], ROOTS) == [], out


def test_a_section_rule_the_aggregator_emits_still_reads_as_one():
    # The rebuild must not cost the estate its own section headers.
    for safe in ["---- share FAIL details ----",
                 "---- cctally-diff-test FAIL details ----"]:
        assert K.scrub_line(safe, _ctx()) == safe, safe


def test_two_leading_dashes_do_not_buy_a_diff_payload_an_exemption():
    # Same content, opposite outcomes, decided by two leading dashes: the
    # verbatim tier was consulted before the diff-payload rule, so a removed
    # line whose content began with dashes skipped the provenance rule.
    bare = "-acme merger closes q3"
    dashed = "--- acme merger closes q3 --"
    assert "acme" in bare and "acme" in dashed
    assert "acme" not in K.scrub_line(bare, _ctx())
    assert "acme" not in K.scrub_line(dashed, _ctx())


def test_a_placeholder_shaped_production_token_is_not_exempt():
    # `unknown_vocabulary` used to re-discover placeholders in the FINISHED
    # text by shape, so a token that arrived already shaped like one inherited
    # the exemption and its words were never judged at all. Only text this
    # module itself wrote, at the position it wrote it, is exempt now.
    line = "FAIL diff: <acme-holdings> diverged"
    assert "<acme-holdings>" in line
    out = K.scrub_line(line, _ctx())
    assert "acme" not in out, out
    assert "holdings" not in out, out


def test_a_placeholder_the_vocabulary_vouches_for_still_survives():
    # The control for the case above. The rule is provenance, not a blanket
    # ban on angle brackets: `<path>` survives because `path` is a registered
    # word, and a suite that redacted both would be safe and useless.
    line = "FAIL diff: <path> diverged"
    assert K.scrub_line(line, _ctx()) == line


def test_a_substituted_root_is_exempt_at_the_position_it_was_written():
    # The exemption comes from the reduction MEASURING where it wrote, so a
    # home root really is substituted and its placeholder really is skipped
    # by the word check rather than re-derived from the output.
    raw = "FAIL: dedup /Users/testuser/.claude/projects/x/a.jsonl"
    assert "/Users/testuser" in raw
    out = K.scrub_line(raw, _ctx())
    assert "/Users/testuser" not in out
    assert "<home>" in out, out


@pytest.mark.parametrize(
    "word", ["\u043a\u043b\u0438\u0435\u043d\u0442", "\u9867\u5ba2",
             "\u03c0\u03b5\u03bb\u03ac\u03c4\u03b7\u03c2"]
)
def test_a_non_latin_word_is_judged_like_any_other(word):
    # `_WORD_RUN_RE` matched Latin letters only while `_SAFE_LINE_RE` admits
    # Unicode `\w`, so default-deny was INVERTED for every non-Latin script:
    # the line matched no word run, nothing was found unknown, and it came
    # back byte for byte.
    line = f"FAIL diff: {word} diverged"
    assert word in line
    out = K.scrub_line(line, _ctx())
    assert word not in out, out


def test_a_non_latin_word_is_reached_only_because_the_line_looks_ordinary():
    # Non-vacuity: the redaction above is the vocabulary check, not the
    # character-class check rejecting the line before it is ever reached.
    line = "FAIL diff: \u043a\u043b\u0438\u0435\u043d\u0442 diverged"
    assert K._SAFE_LINE_RE.match("\u043a\u043b\u0438\u0435\u043d\u0442")
    # Exactly one word is unknown, and it is the non-Latin one: the Latin
    # words around it are registered, so the line reaches the vocabulary check
    # and is redacted for the word the old rule could not even see.
    assert K.unknown_vocabulary(line, _ctx()) == [
        "\u043a\u043b\u0438\u0435\u043d\u0442"
    ]


def test_a_diff_header_naming_something_that_is_not_a_path_is_redacted():
    # `_DIFF_HEADER_RE` returned its first token verbatim, so an unrecognised
    # single token rode out on the header marker's authority.
    line = "--- acme-holdings-billing"
    assert "acme-holdings-billing" in line
    out = K.scrub_line(line, _ctx())
    assert "acme" not in out, out
    # The header form itself still works for something that IS a path.
    ctx = _ctx(is_public_path=lambda p: p == "tests/fixtures/diff/expected.txt")
    assert K.scrub_line("--- tests/fixtures/diff/expected.txt", ctx) == (
        "--- tests/fixtures/diff/expected.txt"
    )
    assert K.scrub_line("--- <path>", _ctx()) == "--- <path>"


# --------------------------------------------- repository-relative paths (P1-4)


@pytest.mark.parametrize("path", [
    ".hooks-example/matcher.py",
    ".state-example/notes.md",
    "scripts-example/deploy.sh",
    "packaging-example/Formula/example.rb",
    "service-example/worker/index.js",
])
def test_every_repo_relative_path_routes_through_the_predicate(path):
    # The rule was an enumeration of five known-public top-level directories —
    # bin, tests, docs, dashboard and .github — so everything outside it was
    # emitted verbatim. Inverted: any `<segment>/<segment>` token is a path,
    # and a path is disclosed only when the predicate says so.
    #
    # The samples are deliberately SYNTHETIC. This file is published, and a
    # public test that names a real mirror-private path breaks the published
    # suite, which `tests/test_public_test_dep_closure.py` exists to catch. The
    # rule under test is structural, so a synthetic segment exercises it
    # exactly as a real one would.
    line = f"FAIL: {path} missing"
    assert path in line
    out = K.scrub_line(line, _ctx())
    assert path not in out, out
    assert "<path>" in out, out
    allowed = _ctx(is_public_path=lambda candidate: candidate == path)
    assert path in K.scrub_line(line, allowed)


def test_the_inverted_path_rule_does_not_read_a_ratio_as_a_path():
    # `38/56` is two numbers, not a path. Requiring a letter in the final
    # segment is what keeps a progress counter out of the path rule.
    assert K.scrub_line("[ 38/56] PASS  diff  340 cases  48s", _ctx()) == (
        "[ 38/56] PASS  diff  340 cases  48s"
    )


# ------------------------------------------------------ the independent validator

ROOTS = {"home": "/Users/testuser", "repo": "/repo", "tmp": "/tmp"}


# The exact bytes `bin/_lib-golden-diff.sh` emits — the shared chokepoint every
# fixture harness compares through — plus the indentation-only lines that fill
# pytest and diff output. Rejecting any of these leaves no export file at all
# on essentially every real failing run, which is the deliverable being absent
# exactly when it is needed.
GOLDEN_DIFF_LITERALS = (
    "FAIL diff: stdout diverged",
    "FAIL share: json diverged",
    "FAIL forecast: stderr diverged",
    "FAIL project: stdout diverged (diff error rc=2 — harness IO trouble, "
    "not a content diff)",
    "FAIL statusline: json diverged (mktemp failed under <tmp>)",
    "    ",
    "        ",
    "",
)


@pytest.mark.parametrize("line", GOLDEN_DIFF_LITERALS)
def test_the_validator_accepts_the_canonical_failure_line(line):
    # A failed validation leaves no export at all, so a false positive here is
    # an availability failure, not a conservative one: it converts a detector
    # into an outage on exactly the runs the extract exists for.
    scrubbed = K.scrub_line(line, _ctx())
    assert K.validate_export([scrubbed], ROOTS) == [], (line, scrubbed)


def test_the_validator_accepts_every_shape_f1_taught_the_transformer_to_keep():
    # The four newly retained shapes, driven through the transformer so the
    # test cannot pass against bytes the transformer never emits.
    emitted = [
        K.scrub_line(line, _ctx(is_public_path=lambda candidate: True))
        for line in [
            "FAILED tests/x.py::test_y - TimeoutError: timed out",
            "  FAILED tests/x.py::test_y - TimeoutError: timed out",
            "FAILED tests/x.py::test_y[acme_private_token] - ValueError: bad",
            "E   TimeoutError: timed out",
            "1 failed, 100 passed, 3 skipped in 45.67s",
            "=========== 1 failed, 100 passed in 12.34s ===========",
            "==== 1 failed, 340 passed, 3 skipped in 506.20s (0:08:26) ====",
        ]
    ]
    assert K.validate_export(emitted, ROOTS) == [], emitted


def test_validator_refuses_a_parameter_value_the_transformer_would_leak():
    assert K.validate_export(
        ["FAILED tests/x.py::test_y[acme_private_token] - ValueError: boom"])


@pytest.mark.parametrize("leaked", [
    "FAILED <path>::test_reason[elapsed_hours<24-less",
    "FAILED tests/x.py::test_reason[elapsed_hours<24-less",
    "  ERROR <path>::test_y[the client hated the quarterly",
])
def test_validator_refuses_an_unterminated_parameter_span(leaked):
    # The measured leak: a truncated capture leaves a span with no closing
    # bracket, which the previous leg required and therefore never judged.
    violations = K.validate_export([leaked], ROOTS)
    assert [v["reason"] for v in violations] == ["unnormalized-parameter"], (
        leaked, violations)


@pytest.mark.parametrize("ctx_name", ("closed", "open"))
def test_a_closing_bracket_before_the_opening_one_is_normalized(ctx_name):
    # The node group's `[^\s\[]+` excludes `[` and admits `]`, so a name whose
    # FIRST bracket closes reached the normalizer intact — the normalizer
    # fired only on `[` — and `]leak` was published. pytest cannot emit this
    # name, but the rule is that no fragment of a parameter id is ever
    # retained, and a member of that class the rule does not reach is a hole.
    ctx = _closed_ctx() if ctx_name == "closed" else _open_ctx()
    out = K.scrub_line("FAILED tests/x.py::test_y]leak[", ctx)
    assert "leak" not in out, out
    assert "[<param>]" in out, out
    assert K.validate_export([out], ROOTS) == [], out


def test_validator_refuses_a_node_name_whose_bracket_closes_first():
    # The validator's own half of the same hole: `rest.find("[")` returned -1,
    # so the leg did not judge the line either.
    violations = K.validate_export(["FAILED <path>::test_y]leak"], ROOTS)
    assert [v["reason"] for v in violations] == ["unnormalized-parameter"], violations


@pytest.mark.parametrize("line", [
    "note: see foo::bar[baz]",
    "[cctally-test-all] pool::worker[3] finished",
])
def test_the_validator_does_not_judge_a_span_that_is_not_a_node_id(line):
    # `unnormalized-parameter` is a structural reason, so a false positive
    # here costs one redacted line rather than the whole file — but the line
    # it redacts is one an operator needs, and a leg that judges spans it was
    # never meant to reach is wrong however cheaply it fails. The leg judges a
    # pytest node identifier, not every `::…[…]` span on every line.
    assert K.validate_export([line], ROOTS) == [], line


def test_validator_refuses_trailing_prose_after_the_exception():
    assert K.validate_export(
        ["FAILED <path>::test_y - ValueError: [REDACTED: exception message] on host alpha"])


def test_validator_refuses_an_unknown_counter_word():
    assert K.validate_export(["1 failed, 100 sprocketed in 45.67s"])


@pytest.mark.parametrize("line", [
    # Both measured against the shipped kernel. The transformer redacts each
    # of them correctly today, so these are the BACKSTOP being blind, not a
    # live leak: the leg restated the transformer's end-anchored shape, so a
    # line that differed in the shape dimension was never judged at all.
    "1 failed, 100 passed in 45.67s -- on host alpha",
    "1 failed, 100 passed in 45.67s (1:01:01) === SECRET",
])
def test_validator_refuses_a_foreign_token_on_a_counters_line(line):
    violations = K.validate_export([line], ROOTS)
    assert [v["reason"] for v in violations] == ["unknown-counter-word"], (
        line, violations)


def test_validator_refuses_a_counters_line_that_lost_its_leading_anchor():
    # Measured against the shipped transformer by removing its leading
    # `^\s*=*\s*` anchor: it emitted this line and the validator said nothing,
    # because the leg's trigger restated that same anchor character for
    # character. The trigger now also fires on a counter pair plus the
    # `in <duration>` tail, wherever on the line they appear.
    line = "acme-holdings-billing 1 failed, 100 passed in 45.67s"
    violations = K.validate_export([line], ROOTS)
    assert [v["reason"] for v in violations] == ["unknown-counter-word"], violations


def test_the_counters_leg_does_not_judge_a_progress_line_that_carries_a_count():
    # The reason the trigger is not simply "a counter pair appears anywhere".
    # The aggregator prints this line for every harness on every run, and it
    # carries `3 failed`; judging it against pytest's closed vocabulary would
    # redact the progress line out of every extract.
    line = "[ 12/56] FAIL  share             product      3 failed   112s"
    assert K.validate_export([line], ROOTS) == [], line


def test_the_counters_leg_does_not_judge_a_line_that_merely_counts_something():
    # The extract's own header and the retention notice both carry a
    # counter-shaped token. Judging every such line against pytest's closed
    # vocabulary would refuse the whole export on essentially every run.
    lines = [
        "[cctally-test-all] run r-1: sanitized failure extract over 3 subjects",
        "EVIDENCE EVICTED: 5 runs, 1024 bytes (2 pass, 3 fail); "
        "reasons: age; 1 coverage gaps",
        "[cctally-test-all] evidence coverage is degraded: 3 gaps across "
        "12 retained runs",
        # The orphan-only and combined forms of the same notice, which reach
        # the extract HEADER through `EV_COVERAGE_NOTE` and are therefore
        # validated rather than scrubbed.
        "[cctally-test-all] evidence coverage is degraded: 1 orphan directory "
        "with no readable manifest across 12 retained runs",
        "[cctally-test-all] evidence coverage is degraded: 3 gaps and "
        "2 orphan directories with no readable manifest across 12 retained runs",
        "[OMITTED: 42 lines outside the retained blocks; "
        "full log retained at logs/share.log]",
    ]
    assert K.validate_export(lines, ROOTS) == []


# Inputs that reach the transformer's gutter rule with another gutter inside
# them AND really come out with both gutters. `FormattedExcinfo.get_source`
# prefixes every line of a multi-line assertion message with `E `, and this
# repository's own meta-tests embed a captured inner pytest run in an
# assertion message, so the outer run's log really does carry
# `E       E   assert …`.
#
# Every member must double under BOTH contexts — see the non-vacuity twin.
# The corpus previously mixed doubling and collapsing inputs, and the twin
# asked only that ONE member double, which the assert forms satisfy
# trivially; twelve of the twenty parameter combinations therefore asserted
# something about a line with a single gutter, and the corpus could have
# drifted to all-collapsing without the twin noticing.
DOUBLED_GUTTER_INPUTS = (
    "E   E   assert 1 == 2",
    "  E   E   assert record[0] == 1",
    "E       E   assert 2 + 2 == 5",
    "E   E   raise ValueError",
)

# Inputs whose inner gutter the transformer COLLAPSES: the body classifies as
# nothing on its own terms, so the non-recursion guard treats the inner `E `
# as ordinary content and the whole body is redacted behind one gutter. These
# are the shapes the doubled corpus above must NOT contain, kept because the
# collapse itself is the behaviour under test.
COLLAPSED_GUTTER_INPUTS = (
    "E   E   share diverged",
    "E   E   SECRET PAYLOAD",
    "E   E   E   assert 1 == 2",
    "E   E   1 failed, 2 passed in 3.21s",
    "E   E   FAILED tests/x.py::test_y[abc]",
    "E   E   ValueError: boom",
)

# A SINGLE `E ` gutter wrapping pytest's own counters line, which the
# transformer retains WHOLE: the gutter rule strips `E `, the body matches the
# counters rule, and the line comes back byte for byte. The validator's
# counters leg then judged the `E` against pytest's closed counter vocabulary
# and refused it — a leg refusing correct transformer output, the third such
# false positive in #630 S1 and the same fault that got the nested-gutter leg
# withdrawn.
#
# Reachable with today's estate: `tests/test_isolation_contract.py` runs a
# nested `python3 -m pytest -q` and puts `proc.stdout` in its assertion
# message, so a failure writes the nested run's summary into the outer log as
# `E         1 failed in 0.42s` — the single most diagnostic line there is.
SINGLE_GUTTER_COUNTERS_INPUTS = (
    "E         1 failed in 0.42s",
    "E   1 failed, 2 passed in 3.21s",
    "  E   3 passed in 1.00s",
    "E       1 failed, 100 passed in 45.67s (0:01:01)",
)


@pytest.mark.parametrize("raw", DOUBLED_GUTTER_INPUTS + COLLAPSED_GUTTER_INPUTS
                         + SINGLE_GUTTER_COUNTERS_INPUTS)
@pytest.mark.parametrize("permissive", [False, True])
def test_the_validator_accepts_every_gutter_the_transformer_emits(
    raw, permissive
):
    """The transformer decides; the validator must not refuse what it decided.

    Written by DRIVING the transformer rather than by hand-writing the
    validator's input, which is exactly what the withdrawn `nested-gutter`
    leg's own test failed to do: it fed the validator `E   E   SECRET
    PAYLOAD`, a string the transformer redacts, so the false-positive branch
    was never exercised and a leg that refused correct output shipped.

    A refusal here is not a cosmetic defect. It flags a line the transformer
    published on purpose, and it costs that line's bytes on every surface.
    """
    ctx = _ctx() if permissive else _closed_ctx()
    emitted = K.scrub_line(raw, ctx)
    assert K.validate_export([emitted], ROOTS) == [], (raw, emitted)


@pytest.mark.parametrize("raw", DOUBLED_GUTTER_INPUTS)
@pytest.mark.parametrize("permissive", [False, True])
def test_every_doubled_gutter_input_really_doubles(raw, permissive):
    """Non-vacuity for the acceptance test above, member by member.

    Asserted per member rather than over the corpus as a whole. A twin that
    asks only whether SOME member doubles cannot see a member that stopped,
    and six of the ten members had stopped without the twin failing.
    """
    ctx = _ctx() if permissive else _closed_ctx()
    emitted = K.scrub_line(raw, ctx)
    assert re.match(r"^\s*E\s+E\s+\S", emitted), (raw, emitted)


@pytest.mark.parametrize("raw", COLLAPSED_GUTTER_INPUTS)
@pytest.mark.parametrize("permissive", [False, True])
def test_every_collapsed_gutter_input_really_collapses(raw, permissive):
    """The complement, so a member cannot silently move between the corpora."""
    ctx = _ctx() if permissive else _closed_ctx()
    emitted = K.scrub_line(raw, ctx)
    assert not re.match(r"^\s*E\s+E\s+\S", emitted), (raw, emitted)
    assert K.UNCLASSIFIED_PLACEHOLDER in emitted, (raw, emitted)


@pytest.mark.parametrize("raw", SINGLE_GUTTER_COUNTERS_INPUTS)
@pytest.mark.parametrize("permissive", [False, True])
def test_a_gutter_wrapped_counters_line_is_retained_whole(raw, permissive):
    """Non-vacuity: these really are retained byte for byte, so a refusal of
    the emitted line is a refusal of the raw one."""
    ctx = _ctx() if permissive else _closed_ctx()
    assert K.scrub_line(raw, ctx) == raw, raw


# A gutter-wrapped line that OPENS with an integer but is not a summary. The
# counters leg must not reach these. Dropping the gutter before the trigger
# rather than only before the refusal scan let `_counters_opening` see a body
# it could never reach before, and every one of these fired the leg — a help
# table's exit-code row is the measured case, and `cctally-dedup-audit --help`
# really does print one. The cost was one redacted line rather than the whole
# export, because `unknown-counter-word` is a structural reason, which is the
# per-line degradation doing its job; the line is still one an operator needs.
#
# Every member is built from words THIS FILE's vocabulary vouches for, because
# the case only exists where the transformer retains the line: under the
# fail-closed context the gutter body is unvouched and the line is redacted,
# so the validator never sees it and there is nothing to get wrong. A first
# version of this corpus used a real help table's `2  usage error`, which the
# repository vocabulary vouches for and this fixture's does not — every
# parametrization skipped, and the guard below is what caught it.
GUTTERED_NON_COUNTERS_INPUTS = (
    "E   3 harness logs",
    "E   2  shell pool",
    "E       3 golden mismatch",
    "E   008 stdout log",
    "E           2  verdict product",
    "E   12 line details",
)


@pytest.mark.parametrize("raw", GUTTERED_NON_COUNTERS_INPUTS)
def test_a_guttered_line_opening_with_a_count_is_not_a_summary(raw):
    ctx = _ctx()
    emitted = K.scrub_line(raw, ctx)
    # Retention is asserted, not assumed: a redacted line would make the
    # refusal check below pass over a placeholder and prove nothing.
    assert emitted == raw, (raw, emitted)
    assert K.validate_export([emitted]) == [], (raw, emitted)


@pytest.mark.parametrize("raw", GUTTERED_NON_COUNTERS_INPUTS)
def test_the_guttered_non_counters_corpus_would_have_tripped_the_old_trigger(raw):
    """Non-vacuity: each member really does open with a count once the gutter
    is dropped, so it is a case the withdrawn trigger reached and the shipped
    one must not. A member that could never trigger would assert nothing."""
    assert K._counters_opening(K._strip_leading_gutter(raw.split())), raw
    assert not K._counters_opening(raw.split()), raw


@pytest.mark.parametrize("line", [
    # The gutter is tolerated at the LEADING position and nowhere else, so a
    # foreign token on a gutter-wrapped counters line is still refused …
    "E   1 failed, 100 passed in 45.67s -- on host alpha",
    "E   1 failed, 100 sprocketed in 45.67s",
    # … and an `E` that is not the gutter is still a token nothing vouched for.
    "1 failed, 100 passed in 45.67s E SECRET",
])
def test_the_counters_leg_still_refuses_a_foreign_token_behind_a_gutter(line):
    violations = K.validate_export([line], ROOTS)
    assert [v["reason"] for v in violations] == ["unknown-counter-word"], (
        line, violations)


@pytest.mark.parametrize("line", ["5 -foo", "5 -3", "3 + 4", "12 (a)"])
def test_the_counters_opening_trigger_needs_a_LETTER_after_the_count(line):
    """The condition the regex this helper replaced really carried.

    `_COUNTERS_OPENING_RE` required the token after the count to BEGIN with a
    letter; the helper that replaced it asked only that the token CONTAIN one,
    which fired on `5 -foo` and refused a line no counters producer emits.
    """
    assert K.validate_export([line], ROOTS) == [], line


def test_a_doubled_gutter_is_not_itself_a_violation():
    """RECORDED PLAINLY, because a leg was withdrawn to make it true.

    `E   E   SECRET PAYLOAD` is refused by nothing here, and the transformer
    emits it in no context — the assertions below drive all three. The
    doubling is not what would make such a line dangerous: this validator has
    no gutter awareness in any other leg, the single-gutter form is accepted
    too, and the payload is judged by the root, denylist and free-text legs
    identically either way. A leg refusing the doubled form would therefore
    close no disclosure class while refusing the `assert` bodies pytest
    really produces.
    """
    assert K.validate_export(["E   E   SECRET PAYLOAD"], ROOTS) == []
    assert K.validate_export(["E   SECRET PAYLOAD"], ROOTS) == []
    # The transformer, which DOES hold the vocabulary, redacts it in every
    # context — including one whose vocabulary vouches for the gutter letter
    # itself, which is the only reading under which the withdrawn leg's
    # `E   E   share diverged` row could ever have been a measurement.
    redacted = "E   " + K.UNCLASSIFIED_PLACEHOLDER
    assert K.scrub_line("E   E   SECRET PAYLOAD", _closed_ctx()) == redacted
    assert K.scrub_line("E   E   SECRET PAYLOAD", _open_ctx()) == redacted
    assert K.scrub_line(
        "E   E   SECRET PAYLOAD",
        _ctx(known_tokens=set(KNOWN_TOKENS) | {"e"}, is_public_path=lambda p: True),
    ) == redacted
    # And the row itself: `e` is not in this repository's vocabulary, so the
    # permissive context redacts the line the comment used to call verbatim.
    assert K.scrub_line("E   E   share diverged", _open_ctx()) == redacted
    assert "e" not in KNOWN_TOKENS


def _redaction_corpus(bad_lines, good_lines):
    lines = list(good_lines)
    for offset, bad in enumerate(bad_lines):
        lines.insert(min(offset * 3 + 1, len(lines)), bad)
    return lines


GOOD_EXTRACT_LINE = "[cctally-test-all] run r-1: sanitized failure extract over 3 subjects"
# A STRUCTURAL violation: the counters leg, one of the three legs whose false
# positives motivated per-line degradation.
BAD_EXTRACT_LINE = "1 failed, 100 sprocketed in 45.67s"
# A CONTENT violation: production prose the transformer was supposed to have
# removed. This is what a partial transformer fault leaks, and one of them is
# enough to withhold the whole export.
LEAKED_CONTENT_LINE = (
    "the project reported an unexpected balance for this customer today"
)


def test_a_flagged_line_is_replaced_and_the_rest_of_the_extract_survives():
    """The structural half of the nested-gutter regression.

    A validator violation used to destroy the whole export, so one false
    positive cost the operator every byte of the failure extract — the same
    class as over-redaction, at the file level.
    """
    lines = _redaction_corpus([BAD_EXTRACT_LINE], [GOOD_EXTRACT_LINE] * 20)
    violations = K.validate_export(lines, ROOTS)
    assert len(violations) == 1, violations
    out, record = K.apply_validation_redactions(lines, violations, ROOTS)
    assert record["refused"] is False, record
    assert record["redacted"] == 1 and record["total"] == len(lines), record
    assert record["reasons"] == ["unknown-counter-word"], record
    # Fail-closed for the offending line: its bytes are gone.
    assert BAD_EXTRACT_LINE not in out
    assert "sprocketed" not in "\n".join(out)
    # And the rest of the extract really did survive.
    assert out.count(GOOD_EXTRACT_LINE) == 20, out
    # Never silent: the count and the distinct reasons are stated in the file.
    assert record["notice"] and "1 of 21" in record["notice"], record
    assert "unknown-counter-word" in record["notice"], record
    # The replacement and the notice are output, so they clear the validator.
    assert K.validate_export(out + [record["notice"]], ROOTS) == []


def test_one_content_violation_refuses_the_whole_export():
    """The systemic-breakage escape, decided by KIND rather than by volume.

    A content violation means the transformer emitted a payload it was
    supposed to have removed, so the UNFLAGGED lines cannot be trusted either
    and there is nothing safe to publish. One is enough, and the rate it
    arrives at says nothing about whether it is safe.
    """
    lines = [GOOD_EXTRACT_LINE] * 99 + [LEAKED_CONTENT_LINE]
    violations = K.validate_export(lines, ROOTS)
    assert [v["reason"] for v in violations] == ["free-form-text"], violations
    out, record = K.apply_validation_redactions(lines, violations, ROOTS)
    assert record["refused"] is True, record
    assert record["refusal"] == "free-form-text", record
    assert record["notice"] is None, record
    assert out == lines


def test_a_structural_violation_degrades_per_line_at_any_rate():
    """The complement, and the reason the proportional escape was withdrawn.

    A structural leg's false positive costs its own line however often it
    fires. Half the extract is refused here and the other half still
    publishes, which the previous quarter-of-the-lines threshold would have
    turned into no export file at all.
    """
    lines = [BAD_EXTRACT_LINE] * 5 + [GOOD_EXTRACT_LINE] * 5
    violations = K.validate_export(lines, ROOTS)
    assert len(violations) == 5, violations
    out, record = K.apply_validation_redactions(lines, violations, ROOTS)
    assert record["refused"] is False, record
    assert record["refusal"] is None, record
    assert BAD_EXTRACT_LINE not in out, out
    assert out.count(GOOD_EXTRACT_LINE) == 5, out
    assert record["notice"] and "5 of 10" in record["notice"], record


def test_a_content_violation_refuses_even_beside_structural_ones():
    """The mixed case, and the reason the refusal cause is recorded.

    The content violation is not first in the list, so a caller reporting
    `violations[0]["reason"]` would name the structural leg — a leg that did
    not cause the refusal and whose false positives are precisely why the
    per-line degradation exists.
    """
    lines = [BAD_EXTRACT_LINE] * 3 + [LEAKED_CONTENT_LINE] + [
        GOOD_EXTRACT_LINE] * 20
    violations = K.validate_export(lines, ROOTS)
    assert violations[0]["reason"] == "unknown-counter-word", violations
    _out, record = K.apply_validation_redactions(lines, violations, ROOTS)
    assert record["refused"] is True, record
    assert record["refusal"] == "free-form-text", record


@pytest.mark.parametrize("reason", sorted(
    {name for name, _ in K._FORBIDDEN} | {"unsubstituted-root",
                                          "free-form-text"}))
def test_every_content_leg_is_outside_the_structural_set(reason):
    """The classification, checked against the leg table rather than against a
    transcription of it. A leg added to `_FORBIDDEN` later is a content leg
    by default, which is the fail-closed direction; this pins that it stays
    one."""
    assert reason not in K.STRUCTURAL_VIOLATION_REASONS, reason


# A failing pytest log of the shape this estate really produces, used to
# measure what a PARTIAL transformer fault publishes.
PARTIAL_FAULT_LOG = (
    "============================= test session starts ==============================",
    "tests/test_share_render.py::test_share_product FAILED",
    "    def test_share_product(tmp_path):",
    ">       assert result == expected",
    "E       AssertionError: the golden did not match",
    "E       assert 'acme holdings quarterly merger summary' == 'the expected text'",
    "tests/test_share_render.py:118: AssertionError",
    "--------------------------- Captured stdout call ---------------------------",
    "the project reported an unexpected balance for this customer today",
    "FAIL share: product diverged",
    "[ 12/56] FAIL  share  product  3 failed  112s",
    "Verdict: FAIL",
    "Total: 56 harnesses",
    "1 failed, 511 passed in 45.67s (0:01:01)",
)
PARTIAL_FAULT_LEAK = (
    "E       assert 'acme holdings quarterly merger summary' == 'the expected text'"
)


def test_a_vocabulary_stage_fault_withholds_the_whole_extract(monkeypatch):
    """The measured case that retired the proportional threshold.

    The transformer's stages fail independently. A fault confined to the
    vocabulary stage leaves root substitution and the path predicate working,
    so neither of those legs fires and only `free-form-text` catches anything
    — a leg needing six words, no digits and a 0.95 letters-and-spaces ratio,
    which most real log lines miss. The flagged proportion is therefore far
    below any threshold worth setting, while production content publishes
    verbatim.
    """
    ctx = _open_ctx()
    monkeypatch.setattr(
        K, "unknown_vocabulary", lambda text, c, spans=(): []
    )
    out = [K.scrub_line(line, ctx) for line in PARTIAL_FAULT_LOG]
    # Non-vacuity: production content really did survive the transformer, and
    # no leg flagged the line carrying it. Single-quoted prose is invisible to
    # both quoted-span legs, which are written over double quotes.
    assert PARTIAL_FAULT_LEAK in out, out
    violations = K.validate_export(out, ROOTS)
    assert [v["reason"] for v in violations] == ["free-form-text"], violations
    assert not any(v["excerpt"] == PARTIAL_FAULT_LEAK for v in violations)
    # Well under the quarter the withdrawn threshold escaped at.
    assert len(violations) / len(out) < 0.25, violations
    _lines, record = K.apply_validation_redactions(out, violations, ROOTS)
    assert record["refused"] is True, record
    assert record["refusal"] == "free-form-text", record


def test_an_unclassifiable_violation_refuses_rather_than_degrading():
    # A violation with no reason at all is not a structural one. It is a
    # verdict this function cannot read, and it takes the conservative branch.
    _out, record = K.apply_validation_redactions(
        [GOOD_EXTRACT_LINE] * 10, [{"index": 0}], ROOTS)
    assert record["refused"] is True, record
    assert record["refusal"] == "unclassified-violation", record


@pytest.mark.parametrize("reason", sorted(
    {name for name, _ in K._FORBIDDEN}
    | {"unsubstituted-root", "free-form-text", "unnormalized-parameter",
       "text-after-exception-placeholder", "unknown-counter-word"}
))
def test_every_redaction_placeholder_clears_the_validator(reason):
    # A reason string that did not clear the validator would publish through
    # the very gate this replacement stands in for.
    placeholder = K.VALIDATION_REDACTION_TEMPLATE % reason
    assert K.validate_export([placeholder], ROOTS) == [], placeholder
    notice = K.VALIDATION_REDACTION_NOTICE % (2, 27, reason)
    assert K.validate_export([notice], ROOTS) == [], notice


def test_a_violation_index_that_addresses_no_line_refuses_wholesale():
    # There is nothing safe to publish around a violation that cannot be
    # located, so this escalates rather than writing the extract untouched.
    # The reason is a STRUCTURAL one on purpose: a content reason would take
    # the refusal branch above and this branch would never be reached.
    lines = [GOOD_EXTRACT_LINE] * 10
    _out, record = K.apply_validation_redactions(
        lines, [{"index": 99, "reason": "unknown-counter-word"}], ROOTS)
    assert record["refused"] is True, record
    assert record["refusal"] == "unlocatable-violation", record


def test_no_violations_leaves_the_extract_and_the_record_untouched():
    lines = [GOOD_EXTRACT_LINE] * 3
    out, record = K.apply_validation_redactions(lines, [], ROOTS)
    assert out == lines
    assert record == {
        "redacted": 0, "total": 3, "reasons": [], "notice": None,
        "refused": False, "refusal": None,
    }


def test_a_single_gutter_the_transformer_really_emits_is_still_accepted():
    assert K.validate_export(
        ["E   TimeoutError: [REDACTED: exception message]",
         "E   [REDACTED: unclassified line]",
         "E       assert 2 + 2 == 5",
         # The counters shapes the corpus above drives through the
         # transformer, stated here as literals too: this test is what the
         # session's third false positive was measured against, and it passed
         # while refusing every one of them.
         "E         1 failed in 0.42s",
         "E   1 failed, 2 passed in 3.21s",
         "  E   3 passed in 1.00s",
         "E       1 failed, 100 passed in 45.67s (0:01:01)"], ROOTS) == []


# The classifiers the transformer decides with. The validator may share the
# EMITTED placeholder constants — those are output, not decisions — but not
# one of these.
TRANSFORMER_CLASSIFIERS = (
    "_PYTEST_NODE_RE", "_PYTEST_GUTTER_RE", "_PYTEST_COUNTERS_RE",
    "_PYTEST_COUNT_WORD", "_PYTEST_SHORT_FRAME_RE", "_PYTEST_ASSERTION_RE",
    "_STRUCTURED_VERBATIM", "_STRUCTURED_PREFIX", "_SECTION_RULE_RE",
    "_MARKER_PREFIX_RE", "_EXCEPTION_RE", "_TRACEBACK_RE", "_DIFF_HEADER_RE",
    "scrub_line", "_is_ordinary", "unknown_vocabulary",
)


def _names_reachable_from(tree, function_name):
    """Every module-level name `function_name` reads, transitively.

    Searching the function's own source text is not enough: a leg formulated
    as a module-level constant is invisible to it, which is how
    `_COUNTERS_SHAPE_RE`, `_NODE_PARAM_SPAN_RE` and `_EXCEPTION_PLACEHOLDER_TEXT`
    escaped the previous version of this check entirely.
    """
    assignments = {}

    def _record(target, value):
        """One binding, over every module-level assignment FORM.

        Collecting `ast.Assign` with an `ast.Name` target alone left two forms
        of the very counterexample this check exists to close.
        `SHAPE: object = _PYTEST_COUNTERS_RE` is an `AnnAssign` and resolved
        to nothing but its own name; `A, B = X, Y` binds through an
        `ast.Tuple` target and did the same.
        """
        if isinstance(target, ast.Name):
            assignments[target.id] = value
            return
        if not isinstance(target, (ast.Tuple, ast.List)):
            return
        paired = (
            value.elts
            if isinstance(value, (ast.Tuple, ast.List))
            and len(value.elts) == len(target.elts)
            else None
        )
        for position, element in enumerate(target.elts):
            # An unpacking whose right-hand side is not a matching literal
            # sequence binds every name to the WHOLE value. That
            # over-approximates reachability, which is the safe direction for
            # a disjointness check: it can only report a restatement that is
            # not there, never miss one that is.
            _record(element, paired[position] if paired else value)

    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                _record(target, node.value)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            _record(node.target, node.value)
        elif isinstance(node, ast.FunctionDef):
            # A HELPER the leg calls is part of the leg. Without this, moving
            # a restatement one call deep would hide it from this check, and
            # the leg's own trigger is written as two helpers.
            assignments.setdefault(node.name, node)
    start = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == function_name
    )
    reached, seen, queue = set(), set(), [start]
    while queue:
        for sub in ast.walk(queue.pop()):
            if not isinstance(sub, ast.Name):
                continue
            reached.add(sub.id)
            if sub.id in assignments and sub.id not in seen:
                seen.add(sub.id)
                queue.append(assignments[sub.id])
    return reached


def test_the_validator_legs_are_written_independently_of_the_transformer():
    # The disjointness the mutation canaries above prove behaviourally,
    # asserted statically as well. It follows the module-level names the
    # function really references rather than searching its body text, so a leg
    # that assigned `_COUNTERS_SHAPE_RE = _PYTEST_COUNTERS_RE` is caught.
    tree = ast.parse((REPO / "bin" / "_lib_test_evidence.py").read_text())
    reached = _names_reachable_from(tree, "_structural_violation")
    # Non-vacuity: the walk must really have reached the leg constants, and
    # must have descended into the trigger's helpers — `_COUNTERS_BANNER_CHAR`
    # is referenced from `_counters_opening` and from nowhere else.
    assert "_COUNTERS_ALLOWED_WORDS" in reached, sorted(reached)
    assert "_NODE_TOKEN_RE" in reached, sorted(reached)
    assert "_COUNTERS_BANNER_CHAR" in reached, sorted(reached)
    shared = sorted(reached.intersection(TRANSFORMER_CLASSIFIERS))
    assert not shared, shared


@pytest.mark.parametrize("binding", [
    # The counterexample the first version of the check would have passed:
    # a leg constant assigned straight from the transformer's own classifier.
    "_COUNTERS_SHAPE_RE = _PYTEST_COUNTERS_RE",
    # The same restatement in an ANNOTATED assignment. The walk collected
    # `ast.Assign` only, so this resolved to its own name and stopped there.
    "_COUNTERS_SHAPE_RE: object = _PYTEST_COUNTERS_RE",
    # And through tuple unpacking, element-wise…
    "_COUNTERS_SHAPE_RE, _OTHER = _PYTEST_COUNTERS_RE, None",
    # …and where the right-hand side is not a matching literal sequence, so
    # every bound name reaches the whole value.
    "_COUNTERS_SHAPE_RE, _OTHER = (_PYTEST_COUNTERS_RE, None, None)[:2]",
])
def test_the_static_disjointness_check_sees_a_module_level_restatement(binding):
    tree = ast.parse(
        "import re\n"
        "_PYTEST_COUNTERS_RE = re.compile('x')\n"
        + binding + "\n"
        "def _structural_violation(text):\n"
        "    return _COUNTERS_SHAPE_RE.match(text)\n"
    )
    reached = _names_reachable_from(tree, "_structural_violation")
    assert "_PYTEST_COUNTERS_RE" in reached, sorted(reached)


def test_the_static_disjointness_check_follows_a_helper_the_leg_calls():
    # A restatement moved one call deep. The walk collected module-level
    # ASSIGNMENTS only, so a helper's body was never entered and the leg's own
    # trigger — which is written as two helpers — was checked at its call site
    # and nowhere else.
    tree = ast.parse(
        "import re\n"
        "_PYTEST_COUNTERS_RE = re.compile('x')\n"
        "def _trigger(text):\n"
        "    return _PYTEST_COUNTERS_RE.match(text)\n"
        "def _structural_violation(text):\n"
        "    return _trigger(text)\n"
    )
    reached = _names_reachable_from(tree, "_structural_violation")
    assert "_PYTEST_COUNTERS_RE" in reached, sorted(reached)


@pytest.mark.parametrize("name,raw", sorted(CANARIES.items()))
def test_the_validator_rejects_every_raw_canary(name, raw):
    violations = K.validate_export([raw], ROOTS)
    assert violations, f"{name} passed the validator unscrubbed"


def test_the_validator_accepts_a_correctly_scrubbed_export():
    lines = K.build_scrubbed_extract(
        {"share": ["FAIL: a", CANARIES["prose_1"], CANARIES["email_1"]]},
        _ctx(),
        {"share": 4},
    )
    assert K.validate_export(lines, ROOTS) == []


def test_the_validator_accepts_the_generated_forms_the_transformer_emits():
    # A validator that rejects lines the transformer legitimately produces
    # would refuse every export, so the two must agree on real output.
    emitted = [
        K.scrub_line(line, _ctx())
        for line in [
            "[ 12/56] FAIL  share             product      3 failed   112s",
            "[cctally-test-all] shell pool complete",
            "Timing: total=1054s  shell-pool=548s  pytest=506s",
            "Verdict: product",
            "passed: 340   failed: 0",
            "case-floor-unmet",
            "Traceback (most recent call last):",
            '  File "/repo/bin/cctally-test-all", line 42, in main',
            "ValueError: user maintainer@example.invalid is unknown",
            "FAIL: dedup /Users/testuser/.claude/projects/x/a.jsonl",
            "@@ -1,4 +1,4 @@",
            "---- cctally-diff-test FAIL details ----",
        ]
    ]
    assert K.validate_export(emitted, ROOTS) == []


@pytest.mark.parametrize("name,raw", sorted(CANARIES.items()))
def test_a_transformer_that_admits_a_canary_is_caught_by_the_validator(name, raw):
    # Force the transformer's unsafe branch to return the raw line, as a
    # classifier bug would. The independently written validator must still
    # catch it, or the second check proves nothing.
    original = K.scrub_line
    try:
        K.scrub_line = lambda line, ctx: line          # type: ignore[assignment]
        lines = K.build_scrubbed_extract({"s": [raw]}, _ctx(), {"s": 2})
        # Non-vacuity: the mutation really fired and the raw line is present.
        assert raw in lines, lines
        assert K.validate_export(lines, ROOTS), f"{name} passed the validator"
    finally:
        K.scrub_line = original                        # type: ignore[assignment]


@pytest.mark.parametrize("path", [
    "/root/secret/x",
    "/Volumes/EXTERNAL/repos/cctally-dev/x",
    "/Library/Keychains/login.keychain-db",
    "/etc/passwd",
    "/mnt/data/exports/q3",
    "/srv/backups/stats.db",
])
def test_the_validator_rejects_any_absolute_path_not_only_known_roots(path):
    # Enumerating known-bad roots misses the rest of the filesystem, and this
    # maintainer's checkout physically lives under one of the misses.
    assert K.validate_export([f"read {path} ok"], ROOTS), path


def test_the_validator_still_admits_relative_and_placeholder_paths():
    # The inverted rule must not reject what the transformer legitimately
    # emits, or every export would be refused and the detector becomes an
    # outage.
    assert K.validate_export([
        "bin/cctally-test-all:373: boom",
        '  File "bin/cctally-test-all", line 42, in main',
        "FAIL: dedup <home><path>",
        "--- <path>",
        "[ 38/56] PASS  diff  340 cases  48s",
        "[TRUNCATED: 4 lines omitted; full log retained at logs/share.log]",
    ], ROOTS) == []


# Each entry is rejected by exactly one leg of the transformer's ordinariness
# test. Disabling that leg makes the transformer return the line unchanged,
# which is what a classifier bug does, and the independently written validator
# must then be the thing that catches it.
LEG_CORPUS = (
    ("safe_line", "status ok\x07 continue"),
    # No space before the run: a word boundary there would let the typed
    # `<b64>` substitution fire first, and the opaque-run leg would never be
    # reached, so the entry would test a different leg than the one it names.
    ("opaque_run", "trace_" + "q" * 44),
    ("json_key", 'seen {"cwd":1} here'),
    ("quoted_free_text", '"we do not know"'),
    ("vocabulary", "the client hated the quarterly revenue numbers"),
)


def _leg_ctx(leg, raw):
    """A context that isolates the leg under test.

    Every entry except the vocabulary one is given a vocabulary that vouches
    for its words, because otherwise the vocabulary leg would reject all five
    and no other leg's removal would be observable — the corpus would pass
    while proving nothing about the leg it names.
    """
    if leg == "vocabulary":
        return _ctx()
    return _ctx(known_tokens=set(re.findall(r"[A-Za-z]+", raw)))


def _disable_leg(monkeypatch, leg):
    always = re.compile(r"")
    never = re.compile(r"(?!x)x")
    if leg == "safe_line":
        monkeypatch.setattr(K, "_SAFE_LINE_RE", always)
    elif leg == "opaque_run":
        monkeypatch.setattr(K, "_OPAQUE_RUN_RE", never)
    elif leg == "json_key":
        monkeypatch.setattr(K, "_JSON_KEY_RE", never)
    elif leg == "quoted_free_text":
        monkeypatch.setattr(K, "_has_quoted_free_text", lambda text: False)
    elif leg == "vocabulary":
        monkeypatch.setattr(
            K, "unknown_vocabulary", lambda text, ctx, path_spans=(): []
        )
    else:                                              # pragma: no cover
        raise AssertionError(f"unknown leg {leg!r}")


@pytest.mark.parametrize("leg,raw", LEG_CORPUS)
def test_every_leg_corpus_entry_is_redacted_with_every_leg_in_place(leg, raw):
    # The premise of the mutation test below: with every leg in place the line
    # is redacted, and with only THIS leg disabled it is not.
    assert K.scrub_line(raw, _leg_ctx(leg, raw)) == K.UNCLASSIFIED_PLACEHOLDER, raw


@pytest.mark.parametrize("leg,raw", LEG_CORPUS)
def test_a_mutated_classifier_leg_is_caught_by_the_independent_validator(
    leg, raw, monkeypatch
):
    _disable_leg(monkeypatch, leg)
    out = K.scrub_line(raw, _leg_ctx(leg, raw))
    # Non-vacuity: the mutation really fired, so the transformer admitted
    # content it must not have.
    assert out != K.UNCLASSIFIED_PLACEHOLDER, (leg, out)
    assert K.validate_export([out], ROOTS), (leg, out)


def test_the_validator_rejects_a_structurally_unknown_form():
    # Not a placeholder, not a generated line, not a recognised shape.
    assert K.validate_export(["\x00\x01 binary garbage \x02"], ROOTS)


def test_the_validator_reports_index_reason_and_excerpt():
    violations = K.validate_export(["ok", CANARIES["email_1"]], ROOTS)
    assert len(violations) == 1
    assert violations[0]["index"] == 1
    assert violations[0]["reason"] == "email"
    assert CANARIES["email_1"] in violations[0]["excerpt"]


def test_the_validator_rejects_an_unsubstituted_root_even_without_a_secret():
    assert K.validate_export(["/repo/bin/x"], ROOTS)


# ------------------------------------------------------------------ retention


def _run(rid, state="completed", outcome="pass", started=0, size=1000,
         pid=None, pid_start=None):
    return {
        "run_id": rid, "remote_dir": "cctally-dev", "state": state,
        "outcome": outcome, "started_epoch": started, "finished_epoch": started + 60,
        "bytes": size, "pid": pid, "pid_start": pid_start,
    }


def test_an_active_run_whose_process_is_gone_becomes_abandoned():
    runs = [_run("r1", state="active", pid=999, pid_start="Sat Aug  9 14:04:07 2026")]
    out = K.reconcile_run_states(runs, live_pids={})
    assert out[0]["state"] == "abandoned"


def test_an_active_run_whose_process_matches_stays_active():
    runs = [_run("r1", state="active", pid=999, pid_start="Sat Aug  9 14:04:07 2026")]
    out = K.reconcile_run_states(runs, live_pids={999: "Sat Aug  9 14:04:07 2026"})
    assert out[0]["state"] == "active"


def test_an_active_run_with_no_recorded_start_identity_becomes_abandoned():
    # `live_pids.get(999)` and a missing `pid_start` are both None, so an
    # equality test read the run as live and protected it from eviction for
    # good. An identity that cannot be corroborated is gone, not alive.
    runs = [_run("r1", state="active", pid=999, pid_start=None)]
    out = K.reconcile_run_states(runs, live_pids={})
    assert out[0]["state"] == "abandoned"


def test_reconcile_run_states_refuses_a_state_outside_the_registry():
    runs = [_run("r1", state="running")]
    with pytest.raises(ValueError) as exc:
        K.reconcile_run_states(runs, live_pids={})
    assert "running" in str(exc.value)
    for state in K.RUN_STATES:
        K.reconcile_run_states([_run("r1", state=state)], live_pids={})


def test_an_active_run_whose_pid_was_reused_becomes_abandoned():
    # A recycled pid with a different process-start identity is a different
    # process, so the run it claims to own is gone.
    runs = [_run("r1", state="active", pid=999, pid_start="Sat Aug  9 14:04:07 2026")]
    out = K.reconcile_run_states(runs, live_pids={999: "Sun Aug 10 09:00:00 2026"})
    assert out[0]["state"] == "abandoned"


def test_reconcile_run_states_does_not_mutate_its_input():
    runs = [_run("r1", state="active", pid=999, pid_start="X")]
    K.reconcile_run_states(runs, live_pids={})
    assert runs[0]["state"] == "active"


def test_age_eviction_removes_runs_past_the_window():
    day = 86400
    runs = [_run("old", started=0), _run("new", started=10 * day)]
    plan = K.plan_evidence_evictions(runs, live_pids={}, now_epoch=10 * day, max_age_days=7)
    assert [r["run_id"] for r in plan["evict"]] == ["old"]


def test_cap_eviction_takes_passing_runs_before_failing_ones():
    runs = [
        _run("p1", outcome="pass", started=1, size=600),
        _run("f1", outcome="fail", started=2, size=600),
        _run("p2", outcome="pass", started=3, size=600),
    ]
    plan = K.plan_evidence_evictions(runs, live_pids={}, now_epoch=100, max_bytes=1300)
    evicted = [r["run_id"] for r in plan["evict"]]
    assert evicted[0] == "p1"
    assert "f1" not in evicted or evicted.index("f1") > evicted.index("p2")


def test_a_failing_run_is_evicted_only_after_every_passing_one():
    runs = [
        _run("p1", outcome="pass", started=1, size=600),
        _run("f1", outcome="fail", started=2, size=600),
        _run("p2", outcome="pass", started=3, size=600),
    ]
    plan = K.plan_evidence_evictions(runs, live_pids={}, now_epoch=100, max_bytes=500)
    evicted = [r["run_id"] for r in plan["evict"]]
    assert evicted == ["p1", "p2", "f1"], evicted


def test_a_live_active_run_and_the_current_run_are_never_evicted():
    runs = [
        _run("live", state="active", pid=4821, pid_start="X", size=10**9),
        _run("cur", size=10**9),
    ]
    plan = K.plan_evidence_evictions(
        runs, live_pids={4821: "X"}, now_epoch=100, max_bytes=1000,
        protect_ids=("cur",),
    )
    assert plan["evict"] == []
    assert plan["over_cap"] is True


def test_eviction_reconciles_stale_active_runs_before_it_applies_the_cap():
    # Spec section 2 fixes the order: age, then reconcile active to
    # abandoned, then cap. A caller that reconciled afterwards left a
    # dead-but-active run protected from eviction for good and the cap
    # unenforceable, so the ordering is inside the function rather than a
    # rule the caller has to remember.
    runs = [
        _run("dead", state="active", pid=999, pid_start="X", started=1, size=10**9),
        _run("cur", started=2, size=10),
    ]
    plan = K.plan_evidence_evictions(
        runs, live_pids={}, now_epoch=100, max_bytes=1000, protect_ids=("cur",)
    )
    assert [r["run_id"] for r in plan["evict"]] == ["dead"]
    assert plan["over_cap"] is False
    assert [r["state"] for r in plan["keep"]] == ["completed"]


def test_plan_evidence_evictions_will_not_run_without_the_live_process_map():
    # Reconciliation cannot be skipped by forgetting an argument.
    with pytest.raises(TypeError):
        K.plan_evidence_evictions([_run("r1")], now_epoch=100)


def test_eviction_records_gaps_rather_than_a_single_boundary():
    day = 86400
    runs = [_run("a", started=1 * day), _run("b", started=2 * day),
            _run("c", started=3 * day)]
    plan = K.plan_evidence_evictions(runs, live_pids={}, now_epoch=4 * day, max_bytes=1500)
    assert plan["gaps"], "evicting a middle run must be reported as a gap"


def test_a_hole_is_reported_with_its_full_extent_not_its_last_record():
    # Two adjacent evictions are one hole, and the hole starts at the first
    # record lost, not the last. A denominator computed from the wrong
    # boundary overstates coverage.
    day = 86400
    runs = [
        _run("a", outcome="fail", started=1 * day, size=100),
        _run("b", outcome="pass", started=2 * day, size=100),
        _run("c", outcome="pass", started=3 * day, size=100),
        _run("d", outcome="fail", started=4 * day, size=100),
    ]
    plan = K.plan_evidence_evictions(runs, live_pids={}, now_epoch=5 * day, max_bytes=250)
    assert sorted(r["run_id"] for r in plan["evict"]) == ["b", "c"]
    assert len(plan["gaps"]) == 1, plan["gaps"]
    assert plan["gaps"][0]["from_epoch"] == 2 * day
    assert plan["gaps"][0]["to_epoch"] == 4 * day


def test_coverage_is_complete_when_nothing_was_evicted():
    runs = [_run("a", started=1), _run("b", started=2)]
    plan = K.plan_evidence_evictions(runs, live_pids={}, now_epoch=100, max_bytes=10**9)
    assert plan["evict"] == []
    assert plan["gaps"] == []
    assert plan["coverage"] == "complete"


def test_coverage_is_degraded_once_a_gap_exists():
    day = 86400
    runs = [_run("a", started=1 * day, size=1000), _run("b", started=2 * day, size=1000)]
    plan = K.plan_evidence_evictions(runs, live_pids={}, now_epoch=3 * day, max_bytes=1000)
    assert plan["coverage"] == "degraded"


def test_render_retention_notice_states_counts_bytes_and_classes():
    plan = {
        "evict": [_run("p1", outcome="pass", size=600)],
        "keep": [], "over_cap": False, "gaps": [], "bytes_after": 0,
    }
    text = K.render_retention_notice(plan)
    assert "EVIDENCE EVICTED" in text
    assert "1" in text and "600" in text and "pass" in text


def test_render_retention_notice_is_empty_when_nothing_was_evicted():
    plan = {"evict": [], "keep": [], "over_cap": False, "gaps": [], "bytes_after": 0}
    assert K.render_retention_notice(plan) == ""


def test_render_retention_notice_names_the_reason_and_the_over_cap_state():
    day = 86400
    runs = [_run("old", started=0, size=10), _run("cur", started=10 * day, size=10**9)]
    plan = K.plan_evidence_evictions(
        runs, live_pids={}, now_epoch=10 * day, max_bytes=1000, protect_ids=("cur",)
    )
    text = K.render_retention_notice(plan)
    assert "age" in text
    assert "over cap" in text


# --------------------------------------------------------- the mirror boundary


def _kernel_source():
    return (REPO / "bin" / "_lib_test_evidence.py").read_text()


# The tripwires that forbid the maintainer's real identity and this estate's
# private vocabulary live in `tests/test_test_remote_observability.py`, which is
# private by omission from `.mirror-allowlist`. A tripwire must hardcode the
# literal it forbids, so a tripwire in a PUBLISHED file publishes exactly what
# it exists to keep unpublished; a tripwire does not have to live in the file it
# scans, and the private one scans the whole public tree rather than two named
# files. Nothing identity-shaped may be added back here.


def test_the_public_kernel_imports_only_the_standard_library():
    # The published tree carries this module and no dependency of it beyond
    # the standard library, so an import of anything else breaks the mirror.
    tree = ast.parse(_kernel_source())
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    # Enumerated rather than probed, so a NEW name has to be justified here
    # before it can ship. `gzip`, `json` and `os` were added by #630 S1 F3's
    # `merge_duration_legs`, which is the module's one I/O function. `zlib` is
    # the same function's error vocabulary: a leg file left by a killed process
    # is a truncated gzip member, and the decompressor raises `zlib.error` for
    # one whose bytes no longer decode. Both are built-in C modules of the
    # standard library, and `gzip` imports `zlib` itself, so neither adds a
    # dependency the published tree does not already carry.
    assert modules <= {"__future__", "gzip", "json", "os", "re", "zlib"}, modules


# ------------------------------------------------ per-test durations merge (#630 S1)


def _leg_file(tmp_path, leg, finished=True, records=2, name=None,
              exit_status=0, omit_exit_status=False):
    """One leg's intermediate file, in exactly the shape the pytest plugin
    writes: gzip JSONL, one record per phase, terminated by a footer only when
    the session reached `pytest_sessionfinish`.

    `finished=False` writes a CLEANLY CLOSED file with the footer omitted.
    That is a synthetic state — the plugin either writes a footer and closes,
    or is killed and leaves a truncated stream — so it exercises the
    missing-footer branch and nothing about truncation.
    `_truncated_leg_file` below covers what the producer really leaves.
    """
    path = tmp_path / (name or f"durations-{leg}.jsonl.gz")
    with gzip.open(path, "wt", encoding="utf-8", compresslevel=6) as handle:
        for idx in range(records):
            handle.write(json.dumps({
                "nodeId": f"tests/x.py::test_{idx}",
                "phase": "call",
                "durationSeconds": 0.125,
                "outcome": "passed",
                "leg": leg,
            }) + "\n")
        if finished:
            footer = {
                "footer": True,
                "leg": leg,
                "records": records,
                "sessionFinished": True,
            }
            if not omit_exit_status:
                footer["exitStatus"] = exit_status
            handle.write(json.dumps(footer) + "\n")
    return str(path)


def _truncated_leg_file(tmp_path, leg, keep=0.6, records=400):
    """What a KILLED process really leaves: a gzip member cut mid-stream.

    Enough records that the surviving prefix still decodes into some of them,
    because a fixture whose prefix decodes to nothing cannot show that the
    merge keeps what it could read.
    """
    path = pathlib.Path(_leg_file(tmp_path, leg, records=records))
    raw = path.read_bytes()
    path.write_bytes(raw[: int(len(raw) * keep)])
    return str(path)


def _read_merged(path):
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def test_merge_marks_an_unfinished_leg_incomplete(tmp_path):
    # A leg whose process died before pytest_sessionfinish has no footer.
    # Publishing it as complete would let a consumer read a truncated run
    # as a run in which those tests were simply fast.
    out = tmp_path / "merged.jsonl.gz"
    result = K.merge_duration_legs(
        [_leg_file(tmp_path, "pytest", finished=False)], str(out))
    assert result["complete"] is False


def test_merge_survives_a_truncated_leg_and_publishes_it_incomplete(tmp_path):
    # The one failure mode this completeness contract exists for. A member cut
    # mid-stream raises `EOFError`, which is NOT an `OSError`, so the merge
    # used to die with a traceback and publish no artifact at all — strictly
    # worse than publishing one marked incomplete.
    out = tmp_path / "merged.jsonl.gz"
    result = K.merge_duration_legs(
        [_truncated_leg_file(tmp_path, "pytest")], str(out))
    assert result["complete"] is False, result
    assert out.exists(), sorted(p.name for p in tmp_path.iterdir())
    footers = [r for r in _read_merged(out) if r.get("footer")]
    assert footers[0]["complete"] is False, footers


def test_merge_keeps_the_records_a_truncated_leg_did_decode(tmp_path):
    # Non-vacuity for the test above, and the point of absorbing the error
    # rather than reporting the leg absent: a partial record of a run that
    # died is what this session exists to preserve.
    out = tmp_path / "merged.jsonl.gz"
    result = K.merge_duration_legs(
        [_truncated_leg_file(tmp_path, "pytest", records=400)], str(out))
    assert 0 < result["records"] < 400, result["records"]
    assert result["legs"][0]["present"] is True, result["legs"]


@pytest.mark.parametrize("status", [2, 3, 4])
def test_merge_marks_a_leg_whose_session_did_not_finish_its_work(tmp_path, status):
    # 2 interrupted, 3 INTERNALERROR, 4 usage error. `pytest_sessionfinish`
    # runs on all three — `wrap_session` calls it from its `finally` block
    # whenever `initstate >= 2` — so the footer is written and says the
    # session finished. It did; it just did not run what it collected.
    out = tmp_path / "merged.jsonl.gz"
    result = K.merge_duration_legs(
        [_leg_file(tmp_path, "pytest", exit_status=status)], str(out))
    assert result["complete"] is False, result
    leg = result["legs"][0]
    assert leg["sessionFinished"] is True, leg
    assert leg["exitStatus"] == status, leg
    assert leg["populationWhole"] is False, leg


@pytest.mark.parametrize("status", [0, 1, 5])
def test_an_ordinary_run_is_complete_whatever_its_verdict(tmp_path, status):
    # Exit 1 is "tests failed", which is a NORMAL, complete run and the one
    # this artifact is most often read for. 5 is "nothing collected".
    out = tmp_path / "merged.jsonl.gz"
    result = K.merge_duration_legs(
        [_leg_file(tmp_path, "pytest", exit_status=status)], str(out))
    assert result["complete"] is True, result


def test_a_footer_predating_the_exit_status_is_taken_at_its_word(tmp_path):
    # Before the field existed the footer's presence WAS the signal, and
    # refusing those artifacts would rewrite history as incomplete.
    out = tmp_path / "merged.jsonl.gz"
    result = K.merge_duration_legs(
        [_leg_file(tmp_path, "pytest", omit_exit_status=True)], str(out))
    assert result["complete"] is True, result


def test_merge_marks_two_finished_legs_complete(tmp_path):
    out = tmp_path / "merged.jsonl.gz"
    result = K.merge_duration_legs(
        [_leg_file(tmp_path, "pytest", finished=True),
         _leg_file(tmp_path, "benchmark", finished=True)], str(out))
    assert result["complete"] is True


def test_merge_publishes_atomically(tmp_path):
    # No partially written artifact may ever be visible at the final path.
    out = tmp_path / "merged.jsonl.gz"
    K.merge_duration_legs([_leg_file(tmp_path, "pytest", finished=True)], str(out))
    assert out.exists()
    assert not list(tmp_path.glob("merged.jsonl.gz.tmp*"))


def test_merge_records_the_incomplete_state_in_the_artifact_itself(tmp_path):
    # The caller's return value is not enough: S6 and S7 read the ARTIFACT,
    # and a truncated file that carries no such statement reads as a complete
    # one in which the missing tests were simply fast.
    out = tmp_path / "merged.jsonl.gz"
    K.merge_duration_legs(
        [_leg_file(tmp_path, "pytest", finished=False)], str(out))
    footers = [r for r in _read_merged(out) if r.get("footer")]
    assert len(footers) == 1, footers
    assert footers[0]["complete"] is False, footers


def test_merge_orders_records_by_leg_then_node_then_phase(tmp_path):
    out = tmp_path / "merged.jsonl.gz"
    K.merge_duration_legs(
        [_leg_file(tmp_path, "pytest"), _leg_file(tmp_path, "benchmark")],
        str(out),
    )
    records = [r for r in _read_merged(out) if "nodeId" in r]
    keys = [(r["leg"], r["nodeId"], r["phase"]) for r in records]
    assert keys == sorted(keys), keys
    assert {r["leg"] for r in records} == {"pytest", "benchmark"}


def test_merge_counts_every_record_it_published(tmp_path):
    out = tmp_path / "merged.jsonl.gz"
    result = K.merge_duration_legs(
        [_leg_file(tmp_path, "pytest", records=3),
         _leg_file(tmp_path, "benchmark", records=2)],
        str(out),
    )
    assert result["records"] == 5
    assert len([r for r in _read_merged(out) if "nodeId" in r]) == 5


def test_merge_treats_a_leg_file_that_never_appeared_as_incomplete(tmp_path):
    # The leg RAN — the caller only hands in legs that ran — so a missing file
    # means the process died before it could open one.
    out = tmp_path / "merged.jsonl.gz"
    result = K.merge_duration_legs(
        [_leg_file(tmp_path, "pytest"), str(tmp_path / "absent.jsonl.gz")],
        str(out),
    )
    assert result["complete"] is False
    legs = {leg["leg"]: leg for leg in result["legs"]}
    assert legs["absent.jsonl.gz"]["present"] is False, result["legs"]


def test_merge_of_one_leg_is_complete_when_that_leg_finished(tmp_path):
    # The benchmark leg only runs when a serial target exists, so a single-leg
    # merge is the ordinary case and must not read as a truncated run.
    out = tmp_path / "merged.jsonl.gz"
    result = K.merge_duration_legs([_leg_file(tmp_path, "pytest")], str(out))
    assert result["complete"] is True
    assert [leg["leg"] for leg in result["legs"]] == ["pytest"]


# ------------------------------------------- the retention budget (#630 S1, F5)
#
# Every constant below is a MEASUREMENT, and each one names where it came from.
# The spec's 10 MiB per-run estimate for the durations artifact was an
# estimate, not a bound; these replace it.

# One authoritative full-suite run, tested head 8608d3167, TZ=Etc/UTC,
# verdict PASS. 40,426 records across both pytest legs.
MEASURED_DURATIONS_COMPRESSED_BYTES = 531_404
# The same records as uncompressed JSONL. Retention sums real `st_size`, so the
# compressed figure is what the cap actually meets — but a compression ratio is
# not a correctness guarantee, so the cap must still hold at this figure.
MEASURED_DURATIONS_UNCOMPRESSED_BYTES = 7_902_147
# Read from the run ledger over its own 11.06-day window, per runner.
CANONICAL_RUNS_IN_WINDOW = (152, 116)
# Bytes retained today under the seven-day horizon, per runner.
RETAINED_BASELINE_BYTES = (67_500_000, 70_300_000)


def _projected_working_set_bytes(per_run_bytes=MEASURED_DURATIONS_COMPRESSED_BYTES):
    """The worst host's projected footprint at the new horizon.

    Today's footprint scaled by 12/7 — the horizon is what the baseline was
    measured under — plus one durations artifact per canonical run the horizon
    now holds.
    """
    return max(
        int(baseline * 12 / 7) + runs * per_run_bytes
        for baseline, runs in zip(RETAINED_BASELINE_BYTES, CANONICAL_RUNS_IN_WINDOW)
    )


def test_age_horizon_covers_the_ledger_window():
    # 11.06 measured days; the cutoff is whole days * 86400, so 11 does not
    # cover it.
    assert K.DEFAULT_MAX_AGE_DAYS >= 12


def test_byte_cap_exceeds_the_projected_working_set():
    projected = _projected_working_set_bytes()
    assert K.DEFAULT_MAX_BYTES > projected, (K.DEFAULT_MAX_BYTES, projected)


def test_byte_cap_still_holds_if_the_durations_artifact_never_compressed():
    # The one place the cap could be set too low by trusting a ratio. This is
    # also what rules out keeping the previous 1 GiB cap: the uncompressed
    # worst case exceeds it.
    projected = _projected_working_set_bytes(
        MEASURED_DURATIONS_UNCOMPRESSED_BYTES)
    assert K.DEFAULT_MAX_BYTES > projected, (K.DEFAULT_MAX_BYTES, projected)
    assert projected > 1073741824, projected


def test_the_cap_stays_within_twice_the_uncompressed_worst_case():
    # A LOOSE upper bound on the safety valve, not a tightness claim. The name
    # this test used to carry ("not raised further than the measurement
    # justifies") over-claimed: the plugin always writes gzip level 6, so the
    # uncompressed case cannot occur, and against the real expected steady
    # state of about 187 MiB the 2 GiB cap has roughly eleven times the
    # headroom. What is actually asserted is that the cap did not float free
    # of the measurement altogether — it is still anchored to the largest
    # figure the measurement produced.
    projected = _projected_working_set_bytes(
        MEASURED_DURATIONS_UNCOMPRESSED_BYTES)
    assert K.DEFAULT_MAX_BYTES <= projected * 2, (K.DEFAULT_MAX_BYTES, projected)

