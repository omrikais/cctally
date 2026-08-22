"""#620 S3 — the exact-bound window flags and the next steps they generate.

D-C requires a next step that reproduces the same population, and `--window`
cannot express it: date grammar names calendar days, and a window can begin and
end at any instant. `explain` therefore gains paired `--start-at` / `--end-at`
exact-instant flags, mutually exclusive with `--window`, and every S3 row's
next step is `cctally explain` over its own scope with those bounds.

**This file exists because a golden that pins a generated command's TEXT does
not prove the command parses, let alone that it measures the same thing.** S2
shipped two next steps that never parsed behind exactly such a golden, and
Task 2 committed six goldens carrying `--start-at`/`--end-at` against a parser
that rejected both flags. The gate here therefore does four things the golden
cannot: it names the class kinds it reached, so the corpus cannot narrow in
silence; it parses each command with the real CLI parser; it asserts each is
shell-safe; and it EXECUTES it and compares the normalized bounds and the
population digest against the report that generated it.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import pathlib
import shlex
import subprocess
import sys

import pytest

from conftest import load_script, redirect_paths

_REPO = pathlib.Path(__file__).resolve().parent.parent
_BIN = _REPO / "bin" / "cctally"
_BUILDER = _REPO / "bin" / "build-explain-fixtures.py"

# The dominant-signal corpora that reach an S3 contributor, plus one
# accounting-only scenario so the gate also covers the four S2 templates and
# both providers' forms of them.
_S3_SCENARIOS = (
    ("cache-churn-dominant", "claude"),
    ("short-context-dominant", "claude"),
    ("fanout-dominant", "claude"),
    ("codex-fanout", "codex"),
    ("codex-short-context", "codex"),
    ("model-dominant", "claude"),
)

_AS_OF = "2026-08-17T00:00:00Z"
_WINDOW_FLAGS = ("--window", "2026-08-10..2026-08-16")


class _Run:
    def __init__(self, completed):
        self.exit_code = completed.returncode
        self.stdout = completed.stdout
        self.stderr = completed.stderr

    @property
    def json(self):
        return json.loads(self.stdout)


def _run_cli(argv, *, home):
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["CCTALLY_AS_OF"] = _AS_OF
    env["CCTALLY_DISABLE_DEV_AUTODETECT"] = "1"
    env["CCTALLY_DATA_DIR"] = str(home / ".local" / "share" / "cctally")
    env["TZ"] = "Etc/UTC"
    env.pop("CLAUDE_CONFIG_DIR", None)
    env.pop("CODEX_HOME", None)
    return _Run(subprocess.run(
        [sys.executable, str(_BIN), *argv],
        capture_output=True, text=True, env=env,
    ))


@pytest.fixture(scope="module")
def scenario_homes(tmp_path_factory):
    """The committed dominant-signal corpora, built once for this module.

    Built rather than hand-seeded: these are the same fixtures the harness
    validates against an independent oracle, so a gate over them cannot drift
    from the corpus the rest of the suite measures.
    """
    root = tmp_path_factory.mktemp("explain-next-steps")
    argv = [sys.executable, str(_BUILDER), "--out", str(root)]
    for name, _source in _S3_SCENARIOS:
        argv += ["--scenario", name]
    completed = subprocess.run(argv, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    return {name: root / name for name, _source in _S3_SCENARIOS}


def _report(scenario_homes, name, source):
    run = _run_cli(["explain", "--json", "--source", source, *_WINDOW_FLAGS],
                   home=scenario_homes[name])
    assert run.exit_code == 0, run.stderr
    return run.json


def _rows(payload):
    return [row for result in payload["results"]
            for row in result["contributors"]]


# --- the two flags ------------------------------------------------------

def test_start_at_and_end_at_are_mutually_exclusive_with_window(
        scenario_homes):
    run = _run_cli(["explain", "--window", "this-week",
                    "--start-at", "2026-08-01T00:00:00Z",
                    "--end-at", "2026-08-08T00:00:00Z"],
                   home=scenario_homes["model-dominant"])
    assert run.exit_code == 2, (run.stdout, run.stderr)
    assert "--window" in run.stderr


def test_start_at_and_end_at_must_be_given_together(scenario_homes):
    for flag in ("--start-at", "--end-at"):
        run = _run_cli(["explain", flag, "2026-08-01T00:00:00Z"],
                       home=scenario_homes["model-dominant"])
        assert run.exit_code == 2, (flag, run.stdout, run.stderr)
        assert "together" in run.stderr, flag


def test_exact_bounds_are_honoured_and_half_open(scenario_homes):
    run = _run_cli(["explain", "--json", "--source", "claude",
                    "--start-at", "2026-08-10T00:00:00Z",
                    "--end-at", "2026-08-17T00:00:00Z"],
                   home=scenario_homes["model-dominant"])
    assert run.exit_code == 0, run.stderr
    window = run.json["window"]
    assert window["startAt"] == "2026-08-10T00:00:00Z"
    assert window["endAt"] == "2026-08-17T00:00:00Z"


def test_an_offset_bearing_instant_resolves_to_the_same_window(
        scenario_homes):
    """A full-ISO form carries its own offset and is timezone-independent, so
    `+03:00` names the same instant as the `Z` form three hours earlier."""
    run = _run_cli(["explain", "--json", "--source", "claude",
                    "--start-at", "2026-08-10T03:00:00+03:00",
                    "--end-at", "2026-08-17T03:00:00+03:00"],
                   home=scenario_homes["model-dominant"])
    assert run.exit_code == 0, run.stderr
    assert run.json["window"]["startAt"] == "2026-08-10T00:00:00Z"
    assert run.json["window"]["endAt"] == "2026-08-17T00:00:00Z"


def test_an_inverted_exact_range_is_a_selector_error(scenario_homes):
    run = _run_cli(["explain", "--start-at", "2026-08-17T00:00:00Z",
                    "--end-at", "2026-08-10T00:00:00Z"],
                   home=scenario_homes["model-dominant"])
    assert run.exit_code == 2, (run.stdout, run.stderr)


def test_a_malformed_exact_bound_is_a_selector_error(scenario_homes):
    run = _run_cli(["explain", "--start-at", "not-an-instant",
                    "--end-at", "2026-08-17T00:00:00Z"],
                   home=scenario_homes["model-dominant"])
    assert run.exit_code == 2, (run.stdout, run.stderr)


def test_a_date_only_exact_bound_is_refused(scenario_homes):
    """`five-hour-breakdown --block-start` rejects a date-only value for the
    same reason: a date is not an instant, and silently reading it as midnight
    in some zone is how a window nobody asked for gets measured."""
    run = _run_cli(["explain", "--start-at", "2026-08-10",
                    "--end-at", "2026-08-17"],
                   home=scenario_homes["model-dominant"])
    assert run.exit_code == 2, (run.stdout, run.stderr)


# --- the gate over the generated next steps -----------------------------

def _parse_next_step(parser, command):
    argv = shlex.split(command)
    assert argv[0] == "cctally", command
    stderr = io.StringIO()
    try:
        with contextlib.redirect_stderr(stderr):
            namespace, unrecognized = parser.parse_known_args(argv[1:])
    except SystemExit as exc:  # pragma: no cover - only on a rejected command
        pytest.fail(f"{command!r}: the CLI refused it (exit {exc.code}): "
                    f"{stderr.getvalue().strip()}")
    assert not unrecognized, (
        f"{command!r}: the CLI does not accept {unrecognized}")
    return namespace


def _assert_shell_safe(command):
    """A next step is text a person is told to paste into a shell.

    `shlex.join(shlex.split(cmd)) == cmd` holds exactly when every token
    survives the shell unquoted. An ISO instant carrying a `+` offset is a
    re-interpretation candidate, which is why the S3 form is run through this
    as well as the S2 forms.
    """
    assert shlex.join(shlex.split(command)) == command, (
        f"{command!r}: a shell would re-interpret this")


@pytest.fixture(scope="module")
def reached(scenario_homes):
    """Every `(source, class, nextStep, digest, window)` the corpora reach."""
    out = []
    for name, source in _S3_SCENARIOS:
        payload = _report(scenario_homes, name, source)
        digests = {r["source"]: r["denominator"]["populationDigest"]
                   for r in payload["results"]}
        for row in _rows(payload):
            out.append({
                "home": name,
                "source": source,
                "class": row["contributorClass"],
                "nextStep": row["nextStep"],
                "digest": digests[source],
                "window": payload["window"],
            })
    return out


def test_the_corpora_reach_every_class_kind_including_all_three_s3_ones(
        reached):
    """The fixture cannot narrow in silence.

    The recorded defect class is a gate whose corpus stopped producing the
    rows it was written to check: the S2 parse gate drew from a home with no
    conversations store, so all three S3 classes were withheld, contributed no
    row, and the exhaustive registry loop rendered the subjectless fallback
    instead. Naming the kinds is what makes that visible.
    """
    kinds = {row["class"] for row in reached}
    for kind in ("cache_churn", "short_high_context", "subagent_fanout"):
        assert kind in kinds, sorted(kinds)
    for kind in ("model_mix", "project_concentration",
                 "session_concentration", "five_hour_bursts"):
        assert kind in kinds, sorted(kinds)
    sources = {(row["source"], row["class"]) for row in reached}
    assert ("codex", "subagent_fanout") in sources, sorted(sources)
    assert ("codex", "short_high_context") in sources, sorted(sources)


def test_every_generated_next_step_parses_and_is_shell_safe(reached):
    parser = load_script()["build_parser"]()
    commands = sorted({row["nextStep"] for row in reached})
    assert commands
    for command in commands:
        _parse_next_step(parser, command)
        _assert_shell_safe(command)


def test_every_s3_next_step_carries_the_exact_bounds_and_the_source(reached):
    """D-C: transcript-free, valid in every configuration, and pointing at the
    same population the row measured."""
    parser = load_script()["build_parser"]()
    checked = 0
    for row in reached:
        if row["class"] not in ("cache_churn", "short_high_context",
                                "subagent_fanout"):
            continue
        namespace = _parse_next_step(parser, row["nextStep"])
        assert namespace.command == "explain", row["nextStep"]
        assert namespace.source == row["source"], row["nextStep"]
        assert namespace.start_at == row["window"]["startAt"], row["nextStep"]
        assert namespace.end_at == row["window"]["endAt"], row["nextStep"]
        assert namespace.window is None, row["nextStep"]
        checked += 1
    assert checked >= 3, checked


def test_executing_the_generated_next_step_reproduces_the_same_population(
        scenario_homes, reached):
    """C13. A golden that pins a generated command's TEXT does not prove it
    parses, let alone that it measures the same thing. EXECUTE it.

    Every next step is executed and must exit 0. The `cctally explain` forms —
    the three S3 classes — must additionally reproduce the normalized bounds
    and the population digest of the report that generated them, which is the
    property D-C names and the only one a shell-level parse can never show.
    """
    executed = 0
    reproduced = 0
    for row in reached:
        argv = shlex.split(row["nextStep"])
        assert argv[0] == "cctally"
        run = _run_cli(argv[1:], home=scenario_homes[row["home"]])
        assert run.exit_code == 0, (row["nextStep"], run.stderr)
        executed += 1
        if argv[1] != "explain":
            continue
        payload = run.json
        assert payload["window"]["startAt"] == row["window"]["startAt"]
        assert payload["window"]["endAt"] == row["window"]["endAt"]
        digests = {r["source"]: r["denominator"]["populationDigest"]
                   for r in payload["results"]}
        assert digests[row["source"]] == row["digest"], row["nextStep"]
        reproduced += 1
    assert executed >= 7, executed
    assert reproduced >= 3, reproduced


# --- C14: the command is side-effect-free -------------------------------

def _tripwire(name, tripped):
    def _fire(*_args, **_kwargs):
        tripped.append(name)
    return _fire


_MUTATION_ENTRY_POINTS = (
    "_spawn_background_update_check",
    "_spawn_background_telemetry_beat",
    "save_config",
)


def _hook_tripwires(ns, monkeypatch):
    tripped: list[str] = []
    for name in _MUTATION_ENTRY_POINTS:
        monkeypatch.setitem(ns, name, _tripwire(name, tripped))
    return tripped


def test_explain_reaches_no_update_telemetry_or_configuration_write(
        tmp_path, monkeypatch):
    """C14. Every command falls through `_post_command_update_hooks`, which
    may spawn a background update request and a telemetry beat unless the
    command is among the read-only early returns."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    # The suppressor short-circuits the WHOLE hook, so a run under it would
    # prove nothing about whether `explain` is listed. The root conftest sets
    # it for every test, which is exactly why it is removed here.
    monkeypatch.delenv("CCTALLY_DISABLE_UPDATE_CHECK", raising=False)
    tripped = _hook_tripwires(ns, monkeypatch)
    ns["_post_command_update_hooks"]("explain", argparse.Namespace(
        command="explain", emit_json=True))
    assert tripped == [], tripped


def test_the_same_hook_does_fire_for_a_command_that_is_not_read_only(
        tmp_path, monkeypatch):
    """The discriminating half. Without it the assertion above would pass on a
    build where the hook never ran at all."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.delenv("CCTALLY_DISABLE_UPDATE_CHECK", raising=False)
    tripped = _hook_tripwires(ns, monkeypatch)
    ns["_post_command_update_hooks"]("daily", argparse.Namespace(
        command="daily"))
    assert tripped, "the hook fired nothing at all, so the pair proves nothing"


def test_explain_writes_no_update_state_into_a_fresh_data_dir(
        tmp_path, monkeypatch):
    """The artifacts, not only the entry points. `_spawn_background_update_check`
    writes `update-state.json` and `update.log`; a fresh data directory holding
    neither after the hook ran is the observable form of the early return."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.delenv("CCTALLY_DISABLE_UPDATE_CHECK", raising=False)
    ns["_post_command_update_hooks"]("explain", argparse.Namespace(
        command="explain", emit_json=True))
    import _cctally_core
    app_dir = pathlib.Path(_cctally_core.APP_DIR)
    for name in ("update-state.json", "update.log"):
        assert not (app_dir / name).exists(), name


def test_the_read_only_early_return_names_explain():
    """The mechanism, not only its effect. The tests above pin the behaviour;
    this pins where it comes from, so a future reordering that moved `explain`
    below the `load_config()` call fails here rather than silently."""
    source = (_REPO / "bin" / "cctally").read_text()
    marker = "def _post_command_update_hooks"
    body = source[source.index(marker):]
    body = body[:body.index("\ndef ", 1)]
    assert '"explain"' in body, (
        "`explain` is not among the read-only early returns")
