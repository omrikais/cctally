#!/usr/bin/env python3
"""Generate the cross-language anon parity fixture (#281 S4, spec §8.4).

``tests/fixtures/anon/parity.json`` is GENERATED from the production
``SECRET_PATTERNS`` + a FIXED fixture identity plan (the same inputs as
``tests/test_conversation_anon.make_plan``), so pytest, vitest, and this
generator all agree on one plan. The file carries::

    {"plan": {…plan_to_wire output…},
     "cases": [{"input": "…", "expected": "…scrub_text output…"}, …]}

pytest runs the Python applier and vitest runs the TS applier over the SAME
inputs/expected — so every PRODUCTION secret pattern executes in both runtimes,
and a Python/JS drift is a test failure, not a silent leak. A pytest golden
guard (``test_conversation_anon.test_parity_fixture_is_regenerated``) regenerates
this file in-process and byte-compares, so fixture↔production drift fails CI.

Usage: ``bin/build-anon-parity-fixture.py`` — writes the JSON, prints the path.
"""
import json
import pathlib
import sys

BIN = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BIN))
import _lib_conversation_anon as anon  # noqa: E402

FIXTURE_PATH = (BIN.parent / "tests" / "fixtures" / "anon" / "parity.json")

# The FIXED fixture identity plan — MUST match test_conversation_anon.make_plan
# so the three consumers (generator, pytest, vitest) share one plan.
_PROJECT_ROOTS = {
    "/Users/alice/repos/cctally-dev": "cctally-dev",
    "/Volumes/EXT/repos/project": "project",
}
_HOME_DIRS = ["/Users/alice"]
_USERNAMES = ["alice"]

# Representative inputs: one per identity token class + one per production secret
# pattern + an idempotence case + a boundary case. `expected` is computed by
# scrub_text (never hand-written), so the fixture can never disagree with the
# live kernel — the golden guard catches drift.
_INPUTS = [
    # identity token classes
    "edited /Users/alice/repos/cctally-dev/bin/x here",       # path literal
    "cache ~/.claude/projects/-Users-alice-repos-cctally-dev/x.jsonl",  # dash-encoded
    "the cctally-dev repo",                                    # label (bounded)
    "home /Users/alice done",                                  # home dir
    "hi alice bye",                                            # username (bounded)
    # one per production secret pattern (8)
    "Authorization: Bearer sometoken1234567890",              # authorization-header
    "Bearer abcdefghijklmnop1234",                            # bearer-token
    "key sk-ant-api03-abcdefgh1234 end",                      # anthropic-key
    "key sk-" + "a" * 24 + " end",                            # generic-sk-key
    "tok ghp_" + "A" * 20,                                    # github-token
    "tok github_pat_" + "B" * 20,                             # github-token (alt prefix)
    "aws AKIA" + "C" * 16 + " end",                           # aws-access-key
    "slack xoxb-1234567890-abc end",                          # slack-token
    'password = "hunter2secret"',                             # secret-assignment
    # idempotence + boundary stress
    "project /Volumes/EXT/repos/project again",               # idempotence (no cascade)
    "project-9 and project here",                             # boundary (generated token safe)
    # combined: identity + secret in one string (order matters)
    "in /Users/alice/repos/cctally-dev the key sk-ant-abcdefgh1234",
]


def build_fixture() -> dict:
    plan = anon.build_anon_plan(
        project_roots=_PROJECT_ROOTS, home_dirs=_HOME_DIRS, usernames=_USERNAMES)
    cases = [{"input": s, "expected": anon.scrub_text(s, plan)} for s in _INPUTS]
    return {"plan": anon.plan_to_wire(plan), "cases": cases}


def dumps_fixture(fixture: dict) -> str:
    """The single serialization shape — used by BOTH this generator and the
    pytest golden guard, so a byte-compare is meaningful."""
    return json.dumps(fixture, indent=2, ensure_ascii=False) + "\n"


def main() -> int:
    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE_PATH.write_text(dumps_fixture(build_fixture()), encoding="utf-8")
    print(str(FIXTURE_PATH))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
