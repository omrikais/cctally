"""#620 S2 — `cctally explain`, the wire adapter and the canonical projection.

`diagnosis_to_wire` is the ONE serializer both surfaces use, so the tests
here bind the wire shape, the privacy behaviour and the exit taxonomy in one
place rather than once per surface.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import os
import pathlib
import re
import shlex
import subprocess
import sys

import pytest

from conftest import load_script, redirect_paths
from test_620_s2_diagnosis_sources import (  # reuse the seeded corpora
    EQUAL_PRICED_MODELS, WINDOW_END, WINDOW_START, _drop_codex_entries,
    _seed_claude, _seed_claude_blocks, _seed_codex, _seed_codex_windows,
)


UTC = dt.timezone.utc
_REPO = pathlib.Path(__file__).resolve().parent.parent
_BIN = _REPO / "bin" / "cctally"


def _diag():
    return load_script()["_cctally_diagnosis"]


class _Run:
    def __init__(self, completed):
        self.exit_code = completed.returncode
        self.stdout = completed.stdout
        self.stderr = completed.stderr

    @property
    def json(self):
        return json.loads(self.stdout)


def _run_cli(argv, *, home, as_of=WINDOW_END):
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["CCTALLY_AS_OF"] = as_of.isoformat().replace("+00:00", "Z")
    env["CCTALLY_DISABLE_DEV_AUTODETECT"] = "1"
    env["CCTALLY_DATA_DIR"] = str(home / ".local" / "share" / "cctally")
    env["TZ"] = "Etc/UTC"
    env.pop("CLAUDE_CONFIG_DIR", None)
    return _Run(subprocess.run(
        [sys.executable, str(_BIN), *argv],
        capture_output=True, text=True, env=env,
    ))


_WINDOW_FLAGS = ["--window",
                 f"{WINDOW_START.date().isoformat()}..{WINDOW_END.date().isoformat()}"]


@pytest.fixture
def rich_home(tmp_path, monkeypatch):
    """A store with a dominant model, project, session and block."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _seed_claude(ns, models=(("claude-opus-4-20250514", 50),
                             ("claude-haiku-4-20250514", 10)))
    _seed_claude_blocks(ns)
    return tmp_path


@pytest.fixture
def both_providers_home(tmp_path, monkeypatch):
    """A store both providers can be diagnosed from.

    The next-step template of a class is rendered with the provider's own
    name, and the two providers do not accept the same flags, so a
    Claude-only fixture can only ever check half of the generated commands.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    # Spread across the seeded five-hour blocks and quota windows rather than
    # bunched into the first of each, so `five_hour_bursts` shapes more than
    # one subject and reports a contributor on BOTH providers. That class is
    # the only one whose next step is a per-subject override rather than the
    # registry template, and the two override forms differ by provider, so a
    # fixture that never reaches it leaves both of them unparsed.
    _seed_claude(ns, models=(("claude-opus-4-20250514", 50),
                             ("claude-haiku-4-20250514", 10)),
                 interval_minutes=20)
    _seed_claude_blocks(ns)
    _seed_codex(ns, rows=(
        [("gpt-5", 10 + index, "root-a") for index in range(10)]
        + [("gpt-5.3-codex", 310 + index, "root-a") for index in range(50)]
    ))
    _seed_codex_windows(ns, windows=[("root-a", "codex_standard", 0, 300),
                                     ("root-a", "codex_standard", 300, 600)])
    return tmp_path


@pytest.fixture
def unreadable_codex_home(tmp_path, monkeypatch):
    """A readable Claude store beside a Codex store that cannot be read.

    Dropping `codex_session_entries` is the shape an older `cache.db` has, and
    is the one condition that produces `provider_unavailable` rather than an
    empty-but-readable Codex population.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _seed_claude(ns, models=(("claude-opus-4-20250514", 50),
                             ("claude-haiku-4-20250514", 10)))
    _seed_claude_blocks(ns)
    _drop_codex_entries(ns)
    return tmp_path


@pytest.fixture
def diffuse_with_unreadable_codex_home(tmp_path, monkeypatch):
    """Six equally priced models over six projects and six sessions, and no
    five-hour blocks at all, beside an unreadable Codex store.

    Every subject holds one sixth of the window, which is below the floor, so
    the three accounting classes MEASURE `no_contributor`. `five_hour_bursts`
    has no blocks to shape subjects from and is withheld.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _seed_claude(
        ns,
        models=tuple((model, 10) for model in EQUAL_PRICED_MODELS),
        projects=tuple(f"/repo/p{index}" for index in range(6)),
        sessions=tuple(f"sess-{index}" for index in range(6)),
    )
    # stats.db must EXIST and hold no `five_hour_blocks` rows. An absent file
    # is a store failure, which withholds the whole provider and would prove
    # nothing about a class-level measurement.
    ns["open_db"]().close()
    _drop_codex_entries(ns)
    return tmp_path


@pytest.fixture
def empty_home(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    ns["open_cache_db"]().close()
    ns["open_db"]().close()
    return tmp_path


# --- the exit taxonomy --------------------------------------------------

def test_account_with_source_all_exits_2(rich_home):
    """Account keys are provider-scoped; one selector cannot address both."""
    run = _run_cli(["explain", "--source", "all", "--account", "x"],
                   home=rich_home)
    assert run.exit_code == 2, run.stderr
    assert "provider-scoped" in run.stderr


def test_healthy_report_exits_0(rich_home):
    run = _run_cli(["explain", "--json", *_WINDOW_FLAGS], home=rich_home)
    assert run.exit_code == 0, run.stderr


def test_fully_withheld_report_still_exits_0(empty_home):
    """A withheld answer is a correct answer, not a failure."""
    run = _run_cli(["explain", "--json", *_WINDOW_FLAGS], home=empty_home)
    assert run.exit_code == 0, run.stderr
    assert run.json["overallVerdict"] == "withheld"


def test_a_malformed_window_exits_2(rich_home):
    run = _run_cli(["explain", "--window", "nonsense"], home=rich_home)
    assert run.exit_code == 2, run.stdout


def test_an_absent_store_is_reported_and_exits_3(tmp_path):
    """A store that could not be read is an infrastructure failure, so the
    command does BOTH things: it prints the withheld report, naming the typed
    cause a person needs, and exits 3, which is what a script reads. Exiting
    without a report left the user with a code and no statement of why."""
    (tmp_path / ".local" / "share" / "cctally").mkdir(parents=True)
    run = _run_cli(["explain", *_WINDOW_FLAGS], home=tmp_path)
    assert run.exit_code == 3, (run.stdout, run.stderr)
    assert "provider_unavailable" in run.stdout, run.stdout


def test_a_withheld_report_that_is_not_a_store_failure_still_exits_0(
        empty_home):
    """The pair with the test above. `insufficient_population` is a correct
    answer about what the store holds, not an infrastructure failure, and
    reporting it as one trains a user to ignore exit 3."""
    run = _run_cli(["explain", "--json", *_WINDOW_FLAGS], home=empty_home)
    assert run.exit_code == 0, run.stderr
    assert run.json["overallVerdict"] == "withheld"
    assert run.json["overallCode"] != "provider_unavailable"


def test_one_unreadable_provider_beside_one_that_answers_exits_0(
        unreadable_codex_home):
    """Under `--source all` a provider that answered makes this a report
    rather than a failure, whatever the other provider could not read.

    The Codex store must genuinely be UNREADABLE. `open_cache_db()` creates
    `codex_session_entries` unconditionally, so a store seeded only with
    Claude rows still answers for Codex — with zero rows, withheld as
    `insufficient_population`. This test then passed because Claude found a
    contributor, and never exercised the predicate's second half at all. The
    body is asserted for the typed cause so it cannot pass that way again.
    """
    run = _run_cli(["explain", "--json", "--source", "all", *_WINDOW_FLAGS],
                   home=unreadable_codex_home)
    assert run.exit_code == 0, run.stderr
    by_source = {r["source"]: r for r in run.json["results"]}
    assert by_source["codex"]["code"] == "provider_unavailable"
    assert by_source["claude"]["verdict"] == "contributor_detected"


def test_an_unreadable_provider_beside_a_measured_no_contributor_exits_0(
        diffuse_with_unreadable_codex_home):
    """"Produced a measurement" is a CLASS-level question.

    Claude here measures three classes as `no_contributor` and withholds
    `five_hour_bursts` as `insufficient_population`, because this install
    retains no `five_hour_blocks` — the shape of a machine whose status-line
    hook was never wired. Its OVERALL verdict is therefore `withheld`, and a
    predicate keyed on the overall verdict counted a complete Claude answer as
    nothing having answered: exit 3 for a report that measured three classes.
    """
    run = _run_cli(["explain", "--json", "--source", "all", *_WINDOW_FLAGS],
                   home=diffuse_with_unreadable_codex_home)
    by_source = {r["source"]: r for r in run.json["results"]}
    assert by_source["codex"]["code"] == "provider_unavailable"
    claude = by_source["claude"]
    assert claude["verdict"] == "withheld"
    measured = [c["contributorClass"] for c in claude["classes"]
                if c["verdict"] == "no_contributor"]
    assert len(measured) == 3, claude["classes"]
    assert run.exit_code == 0, (run.stderr, run.stdout)


def _write_blob_week_anchor(home):
    """Put a BLOB in `weekly_usage_snapshots.week_start_at`.

    stats.db is an ordinary, non-STRICT SQLite database, so the declared TEXT
    type is an affinity rather than a constraint. TEXT affinity converts an
    inserted INTEGER or REAL to text, so the one storage class that survives
    beside TEXT in that column is BLOB — and `sqlite3` returns it as `bytes`,
    which `str.replace` cannot take. This is the shape of a partially
    corrupted store, written here rather than monkeypatched: a test that
    replaces `_diff_resolve_anchor` with a raiser proves only that the handler
    catches what the test told it to raise, never that the real read reaches
    it.
    """
    import sqlite3 as _sqlite3

    path = home / ".local" / "share" / "cctally" / "stats.db"
    conn = _sqlite3.connect(path)
    try:
        conn.execute("DELETE FROM weekly_usage_snapshots")
        conn.execute(
            "INSERT INTO weekly_usage_snapshots("
            " captured_at_utc, week_start_date, week_end_date,"
            " week_start_at, week_end_at, weekly_percent, payload_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("2026-08-16T00:00:00Z", "2026-08-10", "2026-08-17",
             _sqlite3.Binary(b"2026-08-10T00:00:00Z"),
             "2026-08-17T00:00:00Z", 50.0, "{}"),
        )
        conn.commit()
    finally:
        conn.close()


def test_a_non_text_week_anchor_is_a_store_failure_and_never_exit_1(rich_home):
    """Spec §4 states that exit 1 is not used, and this path used it.

    `_diff_resolve_anchor` calls `str.replace` on the stored timestamp with no
    type guard. A BLOB in that column therefore raises `TypeError`, which a
    catch naming only `sqlite3.Error` and `ValueError` does not hold, so the
    exception escaped `cmd_explain` into the CLI's top-level handler and
    printed `Error: a bytes-like object is required, not 'str'` at exit 1.

    A store whose timestamp column holds bytes is a corrupt store, not a code
    bug, so the honest classification is `store_unavailable` at exit 3.
    """
    _write_blob_week_anchor(rich_home)
    run = _run_cli(["explain", "--window", "this-week"], home=rich_home)
    assert run.exit_code == 3, (run.exit_code, run.stdout, run.stderr)
    assert "store_unavailable" in run.stderr, run.stderr


def test_an_unresolvable_account_is_exit_2_even_without_a_registry(tmp_path):
    """Selector validation is exit 2, and a ref that cannot be resolved is
    unresolvable whatever the reason. On a machine with no stats.db the
    read-only open failed first and the selector error became exit 3."""
    (tmp_path / ".local" / "share" / "cctally").mkdir(parents=True)
    run = _run_cli(["explain", "--account", "nobody", *_WINDOW_FLAGS],
                   home=tmp_path)
    assert run.exit_code == 2, (run.stdout, run.stderr)
    assert "--account" in run.stderr


# --- the terminal surface -----------------------------------------------

def test_terminal_header_states_the_scope_sentence_and_the_floor(rich_home):
    out = _run_cli(["explain", *_WINDOW_FLAGS], home=rich_home).stdout
    assert "locally retained cost and tokens" in out
    assert "0.20" in out
    assert "do not sum" in out


def test_the_terminal_states_the_half_open_bounds_and_the_zone(rich_home):
    out = _run_cli(["explain", *_WINDOW_FLAGS], home=rich_home).stdout
    assert "half-open" in out
    assert "Measured at" in out


def test_terminal_output_is_useful_without_json(rich_home):
    out = _run_cli(["explain", *_WINDOW_FLAGS], home=rich_home).stdout
    assert "Reported contributors" in out
    assert "Overall:" in out


def test_a_reported_contributor_states_the_coverage_it_rests_on(rich_home):
    """Spec §4: a reported contributor states observed USD, share, baseline,
    COVERAGE, confidence and one next step.

    The class lines already state their population, so a contributor row that
    states none is the one figure on the surface rendered over a population
    the reader cannot see.
    """
    out = _run_cli(["explain", *_WINDOW_FLAGS], home=rich_home).stdout
    contributors = out.split("Reported contributors")[1]
    assert "coverage: support 60 units" in contributors, contributors
    assert "cost coverage 100%" in contributors, contributors


def test_an_absent_coverage_dimension_is_never_rendered_as_a_zero(rich_home,
                                                                  monkeypatch):
    """Absence is "not measured"; zero is a measurement.

    A dimension the adapter could not measure is `None`, and a row that
    printed `identity coverage 0%` for it would state a measurement nobody
    made — the repository's Class A failure.
    """
    ns = load_script()
    diagnosis = ns["_cctally_diagnosis"]
    kernel = ns["_load_sibling"]("_lib_diagnosis")
    population = kernel.PopulationCoverage(
        requested_start="2026-08-10T00:00:00Z",
        requested_end="2026-08-17T00:00:00Z",
        usd_coverage=1.0, support_units=None,
    )
    phrase = diagnosis._coverage_phrase(population)
    assert phrase == "support not measured, cost coverage 100%"
    assert "identity coverage" not in phrase
    # A word boundary: `cost coverage 100%` contains the substring `0%`, and a
    # bare `in` check would pass straight over a real `0%`.
    assert re.search(r"\b0%", phrase) is None, phrase


def _population(**over):
    kernel = load_script()["_load_sibling"]("_lib_diagnosis")
    fields = {"requested_start": "2026-08-10T00:00:00Z",
              "requested_end": "2026-08-17T00:00:00Z"}
    fields.update(over)
    return kernel.PopulationCoverage(**fields)


def test_a_coverage_percentage_rounds_the_way_the_client_rounds_it():
    """One published number must not print two different percentages.

    Python's format spec rounds a tie to even and JavaScript's `toFixed`
    rounds a tie away from zero, so an `identityCoverage` of exactly 0.125 —
    five of forty attributed entries resolving an identity — printed `12%` in
    the terminal and `13%` in the modal. Half-up is what a reader expects and
    it is what the client already does, so the terminal moved.
    """
    diagnosis = _diag()
    phrase = diagnosis._coverage_phrase(
        _population(identity_coverage=0.125, usd_coverage=0.625,
                    support_units=40))
    assert "identity coverage 13%" in phrase, phrase
    assert "cost coverage 63%" in phrase, phrase


def test_a_share_percentage_rounds_the_way_the_client_rounds_it():
    """The same divergence, one decimal place down.

    `_fmt_share` and `_fmt_baseline` print a tenth of a percent, and a share
    of exactly 0.1125 is 11.25 percent — a tie at that place too.
    """
    diagnosis = _diag()
    kernel = load_script()["_load_sibling"]("_lib_diagnosis")
    field = kernel.EvidenceField("available", 0.1125, _population())
    assert diagnosis._fmt_share(field) == "11.3%"
    assert diagnosis._fmt_baseline(field) == "11.3% previously"


def test_a_single_support_unit_is_stated_in_the_singular():
    """The client already wrote `support 1 unit`; the terminal wrote
    `support 1 units`, which is the same fact in two spellings."""
    diagnosis = _diag()
    assert (diagnosis._support_phrase(_population(support_units=1))
            == "support 1 unit")
    assert (diagnosis._support_phrase(_population(support_units=2))
            == "support 2 units")


def test_a_measured_class_line_states_its_coverage_and_its_confidence(
        diffuse_with_unreadable_codex_home):
    """One vocabulary across the two surfaces.

    The terminal stated support and confidence and no coverage dimensions;
    the modal stated the four coverage dimensions and no confidence. A reader
    moving between them saw each fact on only one surface.
    """
    out = _run_cli(["explain", *_WINDOW_FLAGS],
                   home=diffuse_with_unreadable_codex_home).stdout
    lines = [line.strip() for line in out.splitlines()
             if line.strip().startswith("Expensive model mix (")]
    assert len(lines) == 1, out
    line = lines[0]
    assert "cost coverage" in line, line
    assert "confidence" in line, line


def test_a_measured_class_publishes_its_confidence_and_a_withheld_one_does_not(
        diffuse_with_unreadable_codex_home):
    """Confidence is a statement ABOUT a measurement.

    The client cannot state it without being told it, and it must not
    recompute a server rule, so the class entry carries it — `null` for a
    class that measured nothing, which is not the same as `low`.
    """
    payload = _run_cli(["explain", "--json", *_WINDOW_FLAGS],
                       home=diffuse_with_unreadable_codex_home).json
    classes = {c["contributorClass"]: c
               for c in payload["results"][0]["classes"]}
    assert classes["model_mix"]["verdict"] == "no_contributor"
    assert classes["model_mix"]["confidence"] in ("low", "medium", "high")
    assert classes["five_hour_bursts"]["verdict"] == "withheld"
    assert classes["five_hour_bursts"]["confidence"] is None


def test_a_rendered_report_has_a_non_zero_row_count(rich_home):
    """Assert the rendered result, not the stored inputs."""
    out = _run_cli(["explain", *_WINDOW_FLAGS], home=rich_home).stdout
    rows = [line for line in out.splitlines()
            if line.strip().startswith(("1.", "2.", "3.", "4.", "5."))]
    assert rows


# --- the wire -----------------------------------------------------------

def test_the_envelope_is_stamped_first(rich_home):
    payload = _run_cli(["explain", "--json", *_WINDOW_FLAGS],
                       home=rich_home).json
    assert next(iter(payload)) == "schemaVersion"
    assert payload["schemaVersion"] == 1


def test_the_constants_are_published_on_the_wire(rich_home):
    constants = _run_cli(["explain", "--json", *_WINDOW_FLAGS],
                         home=rich_home).json["constants"]
    assert constants["contributorShareFloor"] == 0.20
    assert constants["withholdMinSupport"] == 2
    # The four accounting-native classes keep their positions; #620 S3 appends
    # three conversation-derived classes after them.
    assert [r["contributorClass"] for r in constants["registry"]][:4] == [
        "model_mix", "project_concentration",
        "session_concentration", "five_hour_bursts",
    ]


def test_the_published_rule_reproduces_the_published_verdict(rich_home):
    """The constants are published so a reader can apply the rule that
    produced the verdict rather than infer it. The floor alone does not
    reproduce it: a share of exactly one fifth publishes as
    0.19999999999999998, and `share >= floor` computes `no_contributor` on a
    row the server published as `contributor`. Both halves of the rule —
    floor and slack — are therefore on the wire."""
    payload = _run_cli(["explain", "--json", *_WINDOW_FLAGS],
                       home=rich_home).json
    constants = payload["constants"]
    floor = constants["contributorShareFloor"]
    epsilon = constants["shareFloorEpsilon"]
    assert epsilon > 0.0
    for result in payload["results"]:
        for class_result in result["classes"]:
            if class_result["verdict"] != "contributor":
                continue
            top = max(row["share"]["value"] for row in class_result["rows"])
            assert top >= floor - epsilon, (class_result["contributorClass"],
                                            top, floor, epsilon)


def test_no_percentage_point_attribution_anywhere(rich_home):
    """P1: the diagnosis must never project a weekly ratio onto a slice."""
    body = json.dumps(_run_cli(["explain", "--json", *_WINDOW_FLAGS],
                               home=rich_home).json)
    assert "percentagePoints" not in body
    assert "pctPoints" not in body
    assert "usedPct" not in body


def test_every_share_states_its_named_denominator(rich_home):
    payload = _run_cli(["explain", "--json", *_WINDOW_FLAGS],
                       home=rich_home).json
    for result in payload["results"]:
        assert result["denominator"]["identity"] == "totalExplainedRetainedCost"
        assert result["denominator"]["source"] == result["source"]


def test_the_wire_states_the_overlap_and_scope_sentences(rich_home):
    notes = _run_cli(["explain", "--json", *_WINDOW_FLAGS],
                     home=rich_home).json["notes"]
    assert "locally retained cost and tokens" in notes["scope"]
    assert "do not sum" in notes["overlap"]


def test_generation_is_published_with_its_id(rich_home):
    payload = _run_cli(["explain", "--json", *_WINDOW_FLAGS],
                       home=rich_home).json
    generation = payload["results"][0]["generation"]
    assert set(generation) == {"stats", "cache", "configuration", "generationId"}


# --- privacy ------------------------------------------------------------

def _rows(payload):
    return [row for result in payload["results"]
            for row in result["contributors"]]


def _session_keys(payload):
    return sorted(r["subjectKey"] for r in _rows(payload)
                  if r["subjectKind"] == "session")


def test_projects_are_aliased_by_default_and_sessions_in_every_mode(rich_home):
    plain = _run_cli(["explain", "--json", *_WINDOW_FLAGS], home=rich_home).json
    revealed = _run_cli(["explain", "--json", "--reveal-projects",
                         *_WINDOW_FLAGS], home=rich_home).json
    assert "/repo/" not in json.dumps(plain)
    assert "/repo/" not in json.dumps(revealed)     # labels, never paths
    assert _session_keys(plain) == _session_keys(revealed)   # opaque both ways


def test_reveal_projects_widens_the_label_and_not_the_key(rich_home):
    revealed = _run_cli(["explain", "--json", "--reveal-projects",
                         *_WINDOW_FLAGS], home=rich_home).json
    project_rows = [r for result in revealed["results"]
                    for cls in result["classes"] for r in cls["rows"]
                    if r["subjectKind"] == "project"]
    for row in project_rows:
        assert row["subjectKey"].startswith("project-")
        assert "/" not in row["subjectLabel"]


def test_the_default_alias_is_deterministic_across_runs(rich_home):
    first = _run_cli(["explain", "--json", *_WINDOW_FLAGS], home=rich_home).json
    second = _run_cli(["explain", "--json", *_WINDOW_FLAGS], home=rich_home).json
    assert _session_keys(first) == _session_keys(second)


# --- next steps ---------------------------------------------------------

def test_every_reported_contributor_carries_a_next_step(rich_home):
    payload = _run_cli(["explain", "--json", *_WINDOW_FLAGS],
                       home=rich_home).json
    rows = _rows(payload)
    assert rows
    assert all(r["nextStep"].startswith("cctally ") for r in rows)


def test_no_next_step_is_behavioural_advice(rich_home):
    """D4 is acceptance-binding: a next step is evidence navigation."""
    payload = _run_cli(["explain", "--json", *_WINDOW_FLAGS],
                       home=rich_home).json
    for row in _rows(payload):
        lowered = row["nextStep"].lower()
        for banned in ("consider", "try ", "you should", "reduce", "switch to"):
            assert banned not in lowered


def _parse_next_step(parser, command):
    """Parse one generated next step with the REAL CLI parser.

    Returns the parsed namespace, or fails the calling test naming the
    command and what the parser said about it. `--help` cannot stand in for
    this: argparse fires the help action the moment it reaches it and exits
    0, before the unrecognized-argument check runs, so `cctally daily
    --source claude --help` exits 0 on a `daily` parser that has no
    `--source` at all.
    """
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


def test_every_generated_next_step_is_accepted_by_the_cli_it_names(
        both_providers_home):
    """A next step the CLI rejects is not navigation, it is a dead end.

    Parse-level rather than executed: the question is whether the command
    names a real subcommand with flags that subcommand really takes, not what
    running it prints.
    """
    payload = _run_cli(["explain", "--json", "--source", "all",
                        *_WINDOW_FLAGS], home=both_providers_home).json
    commands = sorted({row["nextStep"] for row in _rows(payload)})
    assert commands, "the fixture produced no reported contributor at all"
    # What this run actually reached, stated rather than assumed. The
    # `five_hour_bursts` next step is a per-subject override rather than the
    # registry template — and a DIFFERENT override per provider — so it is the
    # one form the exhaustive registry test below cannot see. A fixture that
    # stopped reaching it would leave both overrides unparsed and this test
    # would narrow in silence.
    reached = {(result["source"], row["contributorClass"])
               for result in payload["results"]
               for row in result["contributors"]}
    assert ("claude", "five_hour_bursts") in reached, sorted(reached)
    assert ("codex", "five_hour_bursts") in reached, sorted(reached)
    parser = load_script()["build_parser"]()
    for command in commands:
        _parse_next_step(parser, command)
        _assert_shell_safe(command)


def _assert_shell_safe(command):
    """A next step is text a person is told to paste into a shell.

    `shlex.join(shlex.split(cmd)) == cmd` holds exactly when every token
    survives the shell unquoted. The five-hour template rendered
    `--block-start claude|standard|<iso>`, whose unquoted `|` a shell reads as
    a pipe, so the pasted line ran two commands instead of one.
    """
    assert shlex.join(shlex.split(command)) == command, (
        f"{command!r}: a shell would re-interpret this")


def _production_subject_keys(sources):
    """One production-shaped subject key per registry class.

    The five-hour key is DERIVED from the builder that produces it rather than
    written out here, because a hand-written shape is how the hazard survived:
    the previous version of this test passed the bare instant
    `2026-08-10T00:00:00Z`, and production never produces that — every block
    subject carries the composite root, pool and instant.
    """
    return {
        "model_mix": "claude-opus-4-20250514",
        # A real git root, which may contain a space.
        "project_concentration": "/Users/o/My Repos/alpha",
        "session_concentration": "v1.root-a.0",
        "five_hour_bursts": sources._native_block_key("claude", None,
                                                      WINDOW_START),
        # Each #620 S3 class contributes ONE aggregate subject whose key is a
        # constant of the class, never anything derived from a member.
        "cache_churn": kernel_module().SUBJECT_CACHE_CHURN,
        "short_high_context": kernel_module().SUBJECT_SHORT_HIGH_CONTEXT,
        "subagent_fanout": kernel_module().SUBJECT_SUBAGENT_FANOUT,
    }


def kernel_module():
    return load_script()["_load_sibling"]("_lib_diagnosis")


def test_no_registry_next_step_template_interpolates_a_subject_key():
    """The rule, rather than a list of the keys that happen to be safe today.

    A subject key is store data — a model name, a git root, a conversation
    key, a composite block key — and none of it is guaranteed free of
    characters a shell interprets. A class whose next step must name its
    subject carries a per-subject override built by the same code that built
    the subject, which is the only place the safe form is known.
    """
    kernel = load_script()["_load_sibling"]("_lib_diagnosis")
    for spec in kernel.CONTRIBUTOR_REGISTRY:
        template = spec.next_step_template or ""
        assert "{subject_key}" not in template, spec.kind
        assert "{subject_label}" not in template, spec.kind


def test_every_registry_next_step_template_is_accepted_by_the_cli(
        both_providers_home):
    """Exhaustive over the registry and both providers.

    The test above only reaches the classes whose subjects happened to
    dominate in the seeded corpus. A class that reported no contributor there
    would leave its template unchecked, which is exactly how two of the four
    shipped naming a `--source` flag their target subcommand does not take.

    Every subject here carries the PRODUCTION key shape for its class. Passing
    a shape production never produces certifies the template against an input
    it never receives, which is what let the five-hour template ship a command
    the CLI rejects and a shell splits in two.
    """
    ns = load_script()
    kernel = ns["_load_sibling"]("_lib_diagnosis")
    sources = ns["_load_sibling"]("_cctally_diagnosis_sources")
    parser = ns["build_parser"]()
    keys = _production_subject_keys(sources)
    assert set(keys) == {spec.kind for spec in kernel.CONTRIBUTOR_REGISTRY}, (
        "a registry class has no production subject key stated here")
    for source in ("claude", "codex"):
        scope = sources.DiagnosisScope(
            source=source, account_key=None,
            window_start=WINDOW_START, window_end=WINDOW_END,
            effective_speed=None, display_tz="Etc/UTC", label="",
        )
        context = sources._next_step_context(scope)
        for spec in kernel.CONTRIBUTOR_REGISTRY:
            key = keys[spec.kind]
            subject = kernel.SubjectFacts(
                subject_key=key, subject_label=key,
                observed_usd=1.0, priced_entry_count=20,
            )
            command = kernel._render_next_step(spec, subject, context)
            namespace = _parse_next_step(parser, command)
            _assert_shell_safe(command)
            block_start = getattr(namespace, "block_start", None)
            if block_start is not None:
                # `cctally five-hour-breakdown` parses this value with
                # `datetime.fromisoformat`, so a value argparse accepted as a
                # string can still be refused by the command it names.
                dt.datetime.fromisoformat(block_start)


def test_a_next_step_reproduces_the_diagnosis_own_accounting(
        both_providers_home):
    """D4: a next step must not contradict the evidence it navigates from.

    `explain` reprices every Claude accounting entry at read time, but
    `cctally daily` and `cctally session` default to `-m auto`, which returns
    a non-null stored `session_entries.cost_usd_raw` verbatim. On a store
    holding stored costs, following the next step then shows different
    dollars from the figure that sent the reader there.
    """
    payload = _run_cli(["explain", "--json", "--source", "all",
                        *_WINDOW_FLAGS], home=both_providers_home).json
    commands = sorted({row["nextStep"] for row in _rows(payload)})
    assert commands
    parser = load_script()["build_parser"]()
    checked = 0
    for command in commands:
        namespace = _parse_next_step(parser, command)
        # `mode` exists on the namespace only when the target subcommand
        # registered `-m/--mode`. Where it exists it must be pinned; where it
        # does not, there is no divergence to close.
        if hasattr(namespace, "mode"):
            assert namespace.mode == "calculate", command
            checked += 1
    assert checked, "no generated next step targets a mode-accepting command"


# --- the canonical projection ------------------------------------------

def test_canonical_projection_drops_measured_at_and_fixes_key_order(rich_home):
    a = _run_cli(["explain", "--json", *_WINDOW_FLAGS], home=rich_home).json
    b = dict(a, measuredAt="1999-01-01T00:00:00Z")
    d = _diag()
    assert d.canonical_projection(a) == d.canonical_projection(b)


def test_canonical_projection_still_sees_a_real_difference(rich_home):
    """A projection that erased everything would pass the test above."""
    a = _run_cli(["explain", "--json", *_WINDOW_FLAGS], home=rich_home).json
    b = json.loads(json.dumps(a))
    b["results"][0]["denominator"]["usd"]["value"] += 1.0
    d = _diag()
    assert d.canonical_projection(a) != d.canonical_projection(b)


def test_canonical_projection_is_key_order_independent(rich_home):
    a = _run_cli(["explain", "--json", *_WINDOW_FLAGS], home=rich_home).json
    reversed_keys = {k: a[k] for k in reversed(list(a))}
    d = _diag()
    assert d.canonical_projection(a) == d.canonical_projection(reversed_keys)


def test_the_projection_names_its_exclusions(rich_home):
    d = _diag()
    assert "measuredAt" in d.CANONICAL_EXCLUDED_PATHS


# --- registration -------------------------------------------------------

def test_explain_is_registered_as_a_top_level_command():
    ns = load_script()
    parser_mod = ns["_cctally_parser"]
    names = [reg.name for reg in parser_mod._REGISTRATION]
    assert "explain" in names


def test_the_wrapper_exists_and_is_executable():
    wrapper = _REPO / "bin" / "cctally-explain"
    assert wrapper.exists()
    assert os.access(wrapper, os.X_OK)


def test_source_all_renders_one_section_per_provider(rich_home):
    payload = _run_cli(["explain", "--json", "--source", "all",
                        *_WINDOW_FLAGS], home=rich_home).json
    assert [r["source"] for r in payload["results"]] == ["claude", "codex"]
    assert len({r["denominator"]["source"] for r in payload["results"]}) == 2


# --- the oracle is independent -----------------------------------------

def test_oracle_manifest_is_not_produced_by_diagnosis_code():
    """A gate that clones its result to build its baseline is an arithmetic
    identity: it would pass over any diagnosis at all."""
    src = (_REPO / "bin" / "build-explain-fixtures.py").read_text()
    for banned in ("_lib_diagnosis", "_cctally_diagnosis"):
        assert f"import {banned}" not in src, f"oracle must not import {banned}"
        assert f"from {banned}" not in src, f"oracle must not import {banned}"


def test_the_builder_restates_the_classification_rule_rather_than_reading_it():
    src = (_REPO / "bin" / "build-explain-fixtures.py").read_text()
    assert "ORACLE_SHARE_FLOOR = 0.20" in src
    assert "ORACLE_MIN_PRICED_ENTRIES = 20" in src


def test_every_explain_golden_is_non_empty_and_committed():
    """#625 records seven harnesses reporting PASS when a first-run golden
    write failed, so the bytes are checked rather than the run trusted."""
    import subprocess

    fixtures = _REPO / "tests" / "fixtures" / "explain"
    goldens = sorted(fixtures.glob("*/golden-*.txt"))
    assert len(goldens) >= 30, [str(g) for g in goldens]
    for golden in goldens:
        assert golden.stat().st_size > 100, golden
    tracked = subprocess.run(
        ["git", "-C", str(_REPO), "ls-files", "tests/fixtures/explain"],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    for golden in goldens:
        rel = golden.relative_to(_REPO).as_posix()
        assert rel in tracked, f"{rel} is not committed"


def test_every_explain_scenario_has_an_input_env():
    fixtures = _REPO / "tests" / "fixtures" / "explain"
    scenarios = sorted(p for p in fixtures.iterdir() if p.is_dir())
    assert len(scenarios) >= 15
    for scenario in scenarios:
        assert (scenario / "input.env").is_file(), scenario
        assert (scenario / "golden-terminal.txt").is_file(), scenario
        assert (scenario / "golden-json.txt").is_file(), scenario


def test_the_manifest_registers_the_explain_harness():
    manifest = json.loads(
        (_REPO / "tests" / "authoritative-test-manifest.json").read_text()
    )
    rows = [h for h in manifest["harnesses"] if h["name"] == "explain"]
    assert rows and rows[0]["countPolicy"] == "fixed"
    assert manifest["minHarnessRows"] >= 61


# --- the denominator is an EvidenceField --------------------------------

@pytest.fixture
def unpriceable_home(tmp_path, monkeypatch):
    """A population nothing in the embedded table can price."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _seed_claude(ns, models=(("claude-from-the-future", 40),))
    _seed_claude_blocks(ns)
    return tmp_path


def test_a_withheld_denominator_prints_its_cause_not_a_dollar_figure(
        unpriceable_home):
    """`$0.00 of locally retained cost` above four classes withheld as
    `pricing_unavailable` states a figure the data does not support: the true
    retained cost there is unknown, not zero."""
    run = _run_cli(["explain", *_WINDOW_FLAGS], home=unpriceable_home)
    assert run.exit_code == 0, run.stderr
    denominator_line = next(
        line for line in run.stdout.splitlines()
        if line.startswith("Denominator ")
    )
    assert "pricing_unavailable" in denominator_line
    assert "$0.00" not in denominator_line


def test_the_wire_carries_the_denominator_state_too(unpriceable_home):
    run = _run_cli(["explain", "--json", *_WINDOW_FLAGS],
                   home=unpriceable_home)
    usd = run.json["results"][0]["denominator"]["usd"]
    assert usd["state"] == "withheld"
    assert usd["code"] == "pricing_unavailable"
    assert usd["value"] is None
    assert usd["population"] is not None


def test_an_available_denominator_still_publishes_its_value(rich_home):
    run = _run_cli(["explain", "--json", *_WINDOW_FLAGS], home=rich_home)
    usd = run.json["results"][0]["denominator"]["usd"]
    assert usd["state"] == "available"
    assert usd["value"] > 0.0


# --- the overall verdict discloses its incompleteness -------------------

def test_the_overall_line_states_how_many_classes_were_withheld(rich_home):
    run = _run_cli(["explain", *_WINDOW_FLAGS], home=rich_home)
    overall = [line for line in run.stdout.splitlines()
               if line.startswith("Overall: ")]
    assert overall
    assert "applicable classes withheld" in overall[0]


def test_the_wire_publishes_the_withheld_class_counts(rich_home):
    payload = _run_cli(["explain", "--json", *_WINDOW_FLAGS],
                       home=rich_home).json
    # Seven classes since #620 S3, all applicable on Claude: prompt-cache
    # churn is `not_applicable` on Codex only.
    assert payload["applicableClassCount"] == 7
    assert isinstance(payload["withheldClassCount"], int)
    assert payload["results"][0]["applicableClassCount"] == 7


# --- every class line states its population and confidence --------------

def _lines_under(lines, heading):
    if heading not in lines:
        return []
    index = lines.index(heading) + 1
    out = []
    while index < len(lines) and lines[index].startswith("  "):
        out.append(lines[index])
        index += 1
    return out


def test_a_class_reporting_no_contributor_states_support_and_confidence(
        rich_home):
    """A class admitted at 0.50 dollar coverage can report `no_contributor` at
    `low` confidence, and a bare class label said nothing about either."""
    run = _run_cli(["explain", *_WINDOW_FLAGS], home=rich_home)
    lines = run.stdout.splitlines()
    reported = _lines_under(lines, "Classes reporting no contributor:")
    for line in reported:
        assert "support " in line, line
        assert "confidence " in line, line


def test_a_withheld_class_names_its_shortfall_and_claims_no_confidence(
        rich_home):
    """Confidence is a statement ABOUT a measurement, and a withheld class
    made none: `insufficient_population (support 60 units, confidence high)`
    claimed high confidence in an answer that does not exist, and put an ample
    support count beside a cause whose real shortfall was the distinct-subject
    minimum."""
    run = _run_cli(["explain", *_WINDOW_FLAGS], home=rich_home)
    lines = run.stdout.splitlines()
    withheld = _lines_under(lines, "Classes with no measurement to report:")
    assert withheld, run.stdout
    for line in withheld:
        assert "confidence " not in line, line
    named = [line for line in withheld if "insufficient_population" in line]
    assert named, withheld
    for line in named:
        assert "fewer than" in line or "coverage below" in line or (
            "no priced dollars" in line), line


# --- the exit taxonomy for establishment failures -----------------------

def _explain_raising(monkeypatch, code):
    """Drive the real `cmd_explain` with the adapter raising one code."""
    ns = load_script()
    diagnosis = ns["_cctally_diagnosis"]
    sources = ns["_load_sibling"]("_cctally_diagnosis_sources")

    def _raise(*_args, **_kwargs):
        raise ns["_lib_diagnosis"].EstablishmentFailure(code, "injected")

    monkeypatch.setattr(sources, "build_diagnosis", _raise)
    return diagnosis, ns


@pytest.mark.parametrize("code", ["range_unresolved", "account_unresolved",
                                  "store_unavailable", "generation_incoherent"])
def test_every_establishment_failure_exits_3(code, rich_home, monkeypatch,
                                             capsys):
    """An `EstablishmentFailure` is ALWAYS exit 3. Argument-shaped validation
    stays exit 2 and fails at argument-parse time, never by becoming an
    establishment failure."""
    monkeypatch.setenv("HOME", str(rich_home))
    monkeypatch.setenv("CCTALLY_DATA_DIR",
                       str(rich_home / ".local" / "share" / "cctally"))
    monkeypatch.setenv("CCTALLY_AS_OF",
                       WINDOW_END.isoformat().replace("+00:00", "Z"))
    diagnosis, ns = _explain_raising(monkeypatch, code)

    class _Args:
        source = "claude"
        account = None
        window = (f"{WINDOW_START.date().isoformat()}.."
                  f"{WINDOW_END.date().isoformat()}")
        speed = None
        emit_json = False
        json = False
        reveal_projects = False
        tz = None

    assert diagnosis.cmd_explain(_Args()) == 3
    captured = capsys.readouterr()
    assert code in captured.err


def test_a_store_failure_under_the_window_anchor_is_not_a_selector_error(
        rich_home, monkeypatch, capsys):
    """Exit 2 means the user wrote the selector wrong.

    `_resolve_window` reaches stats.db through the ordinary opener for any
    token that needs the week anchor, so a malformed or locked store raises
    here. Reporting that as `explain: <sqlite message>` and exit 2 tells the
    user to fix a `--window` token that was never wrong, and hides an
    infrastructure failure inside the argument taxonomy.
    """
    import sqlite3

    ns = load_script()
    diagnosis = ns["_cctally_diagnosis"]
    dk = ns["_load_sibling"]("_lib_diff_kernel")

    def _raise(*_a, **_kw):
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(dk, "_diff_resolve_anchor", _raise)
    monkeypatch.setenv("HOME", str(rich_home))
    monkeypatch.setenv("CCTALLY_DATA_DIR",
                       str(rich_home / ".local" / "share" / "cctally"))
    monkeypatch.setenv("CCTALLY_AS_OF",
                       WINDOW_END.isoformat().replace("+00:00", "Z"))

    class _Args:
        source = "claude"
        account = None
        window = "this-week"
        speed = None
        emit_json = False
        json = False
        reveal_projects = False
        tz = None

    assert diagnosis.cmd_explain(_Args()) == 3
    assert "store_unavailable" in capsys.readouterr().err


def test_a_malformed_window_token_is_still_a_selector_error(rich_home,
                                                            monkeypatch,
                                                            capsys):
    """The pair with the test above: narrowing the catch must not turn a
    genuinely wrong token into an infrastructure failure."""
    ns = load_script()
    diagnosis = ns["_cctally_diagnosis"]
    monkeypatch.setenv("HOME", str(rich_home))
    monkeypatch.setenv("CCTALLY_DATA_DIR",
                       str(rich_home / ".local" / "share" / "cctally"))
    monkeypatch.setenv("CCTALLY_AS_OF",
                       WINDOW_END.isoformat().replace("+00:00", "Z"))

    class _Args:
        source = "claude"
        account = None
        window = "nonsense"
        speed = None
        emit_json = False
        json = False
        reveal_projects = False
        tz = None

    assert diagnosis.cmd_explain(_Args()) == 2
    assert "explain:" in capsys.readouterr().err


def test_a_generation_incoherence_reaches_the_command_as_exit_3(rich_home,
                                                                monkeypatch):
    """The retry predicate is proved at the adapter; this proves the whole
    command path, which no golden fixture can: a component moving twice
    mid-read is a race, not a state a seeded store can hold."""
    run = _run_cli(["explain", *_WINDOW_FLAGS, "--json"], home=rich_home)
    assert run.exit_code == 0, run.stderr

    ns = load_script()
    sources = ns["_load_sibling"]("_cctally_diagnosis_sources")
    probes = iter(["a", "b", "b", "c"] * 20)
    monkeypatch.setattr(sources, "_probe_component",
                        lambda *_a, **_kw: next(probes))
    diagnosis = ns["_cctally_diagnosis"]
    monkeypatch.setenv("HOME", str(rich_home))
    monkeypatch.setenv("CCTALLY_DATA_DIR",
                       str(rich_home / ".local" / "share" / "cctally"))

    class _Args:
        source = "claude"
        account = None
        window = (f"{WINDOW_START.date().isoformat()}.."
                  f"{WINDOW_END.date().isoformat()}")
        speed = None
        emit_json = False
        json = False
        reveal_projects = False
        tz = None

    assert diagnosis.cmd_explain(_Args()) == 3


# --- the canonical projection drops exactly its named paths -------------

def test_the_projection_keeps_the_registry_labels(rich_home):
    """The projection is driven by `CANONICAL_EXCLUDED_PATHS` itself, so the
    two cannot disagree. Dropping every key NAMED `label` at every depth also
    dropped `constants.registry[].label`, which is a fact of the contract."""
    payload = _run_cli(["explain", "--json", *_WINDOW_FLAGS],
                       home=rich_home).json
    projected = json.loads(_diag().canonical_projection(payload))
    assert projected["constants"]["registry"][0]["label"]
    assert "measuredAt" not in projected
    assert "notes" not in projected
    assert "label" not in projected["window"]
    for row in projected["results"][0]["contributors"]:
        assert "subjectLabel" not in row
    for class_result in projected["results"][0]["classes"]:
        for row in class_result["rows"]:
            assert "subjectLabel" not in row


def test_the_projection_names_every_path_it_drops(rich_home):
    """Every excluded path must exist in the unprojected payload, or the
    constant is naming something that never ships."""
    payload = _run_cli(["explain", "--json", *_WINDOW_FLAGS],
                       home=rich_home).json

    def _reachable(node, parts):
        if not parts:
            return True
        head, rest = parts[0], parts[1:]
        if head.endswith("[]"):
            key = head[:-2]
            children = node.get(key) if isinstance(node, dict) else None
            if not children:
                return False
            return any(_reachable(child, rest) for child in children)
        if not isinstance(node, dict) or head not in node:
            return False
        return _reachable(node[head], rest)

    for path in _diag().CANONICAL_EXCLUDED_PATHS:
        assert _reachable(payload, path.split(".")), path


# --- setup exposes the wrapper ------------------------------------------

def test_setup_symlinks_the_explain_wrapper():
    """`cctally-explain` ships in package.json, .mirror-allowlist and the
    shellcheck target list. Absent from SETUP_SYMLINK_NAMES, `setup` would
    never expose it and `repair-symlinks` would never heal it."""
    ns = load_script()
    assert "cctally-explain" in ns["SETUP_SYMLINK_NAMES"]


# --- fallback pricing reaches a reported row ----------------------------

def test_is_fallback_pricing_reaches_a_reported_row(tmp_path, monkeypatch):
    """`classify_class` reports the top subject and its exact ties, so a
    fallback-priced model that is outspent never reaches a row at all and the
    qualification is proved nowhere."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _seed_codex(ns, rows=(
        [("gpt-5.3-codex", 10 + i, "root-a") for i in range(20)]
        + [("a-model-from-the-future", 40 + i, "root-a") for i in range(40)]
    ))
    _seed_codex_windows(ns, windows=[("root-a", "codex_standard", 0, 300)])
    run = _run_cli(["explain", "--json", "--source", "codex", *_WINDOW_FLAGS],
                   home=tmp_path)
    assert run.exit_code == 0, run.stderr
    rows = run.json["results"][0]["contributors"]
    assert rows
    assert any(row["isFallbackPricing"] for row in rows), rows
    terminal = _run_cli(["explain", "--source", "codex", *_WINDOW_FLAGS],
                        home=tmp_path)
    assert "fallback pricing" in terminal.stdout


def test_a_programming_error_elsewhere_in_the_command_still_propagates(
        rich_home, monkeypatch):
    """The pair with the test above, in the other direction.

    Converting EVERY exception anywhere in the command into
    `EstablishmentFailure(store_unavailable)` would report a code bug as an
    infrastructure failure the user is told to fix. The widening is bounded to
    the anchor read, where a type error is a statement about what the store
    holds; a defect in the render path is still a defect and must propagate
    rather than be dressed up as a store failure.
    """
    ns = load_script()
    diagnosis = ns["_cctally_diagnosis"]

    def _raise(*_a, **_kw):
        raise TypeError("too many values to unpack")

    monkeypatch.setattr(diagnosis, "render_terminal", _raise)
    monkeypatch.setenv("HOME", str(rich_home))
    monkeypatch.setenv("CCTALLY_DATA_DIR",
                       str(rich_home / ".local" / "share" / "cctally"))
    monkeypatch.setenv("CCTALLY_AS_OF",
                       WINDOW_END.isoformat().replace("+00:00", "Z"))

    class _Args:
        source = "claude"
        account = None
        window = (f"{WINDOW_START.date().isoformat()}.."
                  f"{WINDOW_END.date().isoformat()}")
        speed = None
        emit_json = False
        json = False
        reveal_projects = False
        tz = None

    with pytest.raises(TypeError):
        diagnosis.cmd_explain(_Args())
