"""Tests for bin/_lib_test_evidence.py (#529 S2)."""
from __future__ import annotations

import ast
import importlib.util
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
    return K.ScrubContext(
        roots={"home": "/Users/testuser", "repo": "/repo", "tmp": "/tmp"}, **kw
    )


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
    assert modules <= {"__future__", "re"}, modules
