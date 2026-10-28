"""Every bin/cctally-*-test harness must source the dev-autodetect
suppressor preamble (directly, or transitively via _lib-fixture-harness.sh).
A new harness added without it relocates its data dir to cctally-dev on a
dev machine and its goldens break on direct runs — this test makes that a
hard, drift-proof failure rather than something to remember."""
import os
import pathlib
import re
import shlex
import subprocess
import sys

import pytest

BIN = pathlib.Path(__file__).resolve().parent.parent / "bin"


def _harnesses():
    return sorted(p for p in BIN.glob("cctally-*-test") if p.is_file())


def test_every_harness_sources_the_suppressor_preamble():
    missing = []
    for h in _harnesses():
        text = h.read_text()
        sources_preamble = "_lib-harness-env.sh" in text
        sources_fixture_lib = "_lib-fixture-harness.sh" in text
        if not (sources_preamble or sources_fixture_lib):
            missing.append(h.name)
    assert not missing, (
        "these harnesses source neither _lib-harness-env.sh nor "
        f"_lib-fixture-harness.sh: {missing}"
    )


def test_preamble_exports_the_suppressor():
    preamble = BIN / "_lib-harness-env.sh"
    assert preamble.is_file()
    assert "export CCTALLY_DISABLE_DEV_AUTODETECT=1" in preamble.read_text()


def test_preamble_suppresses_detached_post_command_workers():
    """All THREE of them. The artifact-retention sweep (#496 S6) joined the
    update check and the telemetry beat, and it is the one that broke a
    harness: `cctally-rederive-test` fingerprints the whole data directory
    before and after a no-op apply, which no asynchronous writer can satisfy.
    The literals are hardcoded rather than imported so this fails if the
    preamble drops one.
    """
    preamble = (BIN / "_lib-harness-env.sh").read_text()
    assert "export CCTALLY_DISABLE_UPDATE_CHECK=1" in preamble
    assert "export CCTALLY_DISABLE_RETENTION_SWEEP=1" in preamble


def test_fixture_lib_sources_the_preamble():
    lib = (BIN / "_lib-fixture-harness.sh").read_text()
    assert "_lib-harness-env.sh" in lib


# ── #965: the test retention switch at every non-pytest boundary (5.7) ─────

TESTS = pathlib.Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

import _lib_test_isolation as iso  # noqa: E402 -- bin/ is on sys.path via conftest
import _retention_entry_points as rep  # noqa: E402
from _retention_fixtures import (  # noqa: E402
    LAUNCH_TIMEOUT_S, SWITCH, counts, seed, stage, write_retention)


def _rel(path):
    return str(path.relative_to(rep.REPO_ROOT))


def test_965_preamble_exports_the_retention_switch():
    assert "export CCTALLY_TEST_CONVERSATION_RETENTION_DEFAULT_OFF=1" in (
        BIN / "_lib-harness-env.sh").read_text()


_PREAMBLE = BIN / "_lib-harness-env.sh"


def test_965_every_shell_entry_point_keeps_the_switch():
    points = rep.shell_entry_points()
    scanned = {path for path, _ in points}
    # The preamble is checked as itself (kind "preamble": its own export, and
    # no later clear), not only by the substring test above.
    assert (_PREAMBLE, "preamble") in points, "the preamble is not under the static check"
    extras = {rep.FIXTURE_LIB, rep.AGGREGATOR, rep.SERVE, _PREAMBLE}
    assert all(path.is_file() for path in extras), sorted(map(_rel, extras))
    present = {path for path in BIN.glob("cctally-*-test") if path.is_file()}
    assert present, "the harness glob found nothing, so the scan proves nothing"
    assert scanned == present | extras, "the scan does not cover exactly the files present"
    problems = []
    for path, kind in points:
        problems += rep.shell_violations(path.read_text(errors="replace"),
                                         name=_rel(path), kind=kind)
    assert not problems, "\n".join(problems)


def test_965_every_transcript_builder_assigns_the_switch_before_loading_cctally():
    problems = []
    for name in rep.covered_builders():
        problems += rep.builder_violations((BIN / name).read_text(encoding="utf-8"), name)
    assert not problems, "\n".join(problems)


def test_965_the_builder_population_is_derived_not_listed():
    """The derived half of a pair: tests/test_transcript_builders_independent_start.py::
    test_every_transcript_builder_has_an_independent_start_test compares the
    written-down sets, and only this test ties them to the builders on disk."""
    assert rep.covered_builders() == rep.TRANSCRIPT_BUILDERS
    # The runtime guard's own population (bin/_lib_test_isolation.py) is the
    # same derived set, so a new builder cannot be covered by one guard only.
    assert iso.RETENTION_COVERED_BUILDERS == rep.covered_builders()


def test_965_the_cleared_environment_scan_sees_the_known_launches():
    """Non-vacuity and quote awareness, measured at HEAD (population (e))."""
    seen = {path.name for path, _ in rep.shell_entry_points()
            if rep.cleared_env_launches(path.read_text(errors="replace"))}
    required = {"cctally-npm-shim-test", "cctally-npm-postinstall-test"}
    assert required <= seen, seen
    assert "cctally-preflight-test" not in seen, "quoted test data read as a launch"


def _without(text, pattern):
    lines = text.splitlines()
    kept = [line for line in lines if not re.search(pattern, line)]
    assert len(kept) < len(lines), f"mutation found nothing to remove: {pattern}"
    return "\n".join(kept) + "\n"


def _after_first(text, pattern, inserted):
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if re.search(pattern, line):
            return "\n".join(lines[:index + 1] + [inserted] + lines[index + 1:]) + "\n"
    raise AssertionError(f"mutation anchor not found: {pattern}")


def _before_first(text, pattern, inserted):
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if re.search(pattern, line):
            return "\n".join(lines[:index] + [inserted] + lines[index:]) + "\n"
    raise AssertionError(f"mutation anchor not found: {pattern}")


_SOURCE = r"^\s*(\.|source)\s+.*_lib-(harness-env|fixture-harness)\.sh"
_EXPORT = r"^\s*export\s+" + SWITCH + "=1"
_ASSIGN = r"os\.environ\[\"" + SWITCH + r"\"\]\s*=\s*\"1\""


def test_965_a_mutated_harness_is_rejected():
    name, path = "bin/cctally-conversation-test", BIN / "cctally-conversation-test"
    original = path.read_text()
    assert not rep.shell_violations(original, name=name, kind="harness")
    assert rep.shell_violations(_without(original, _SOURCE), name=name, kind="harness"), (
        "a harness that never assigns the switch was accepted")
    assert rep.shell_violations(_after_first(original, _SOURCE, f"unset {SWITCH}"),
                                name=name, kind="harness"), (
        "a harness that clears the switch after initialization was accepted")
    cleared = original + 'env -i HOME="$HOME" PATH=/usr/bin:/bin "$BIN" --version\n'
    assert rep.shell_violations(cleared, name=name, kind="harness"), (
        "a cleared-environment launch that drops the switch was accepted")
    restored = original + f'env -i HOME="$HOME" PATH=/usr/bin:/bin {SWITCH}=1 "$BIN" --version\n'
    assert not rep.shell_violations(restored, name=name, kind="harness")


_UNRESTORED = "clears the environment without restoring"


@pytest.mark.parametrize("launch", [
    f'{SWITCH}=1 env -i HOME="$HOME" PATH=/usr/bin:/bin "$BIN" cache-sync --rebuild',
    f'export {SWITCH}=1; env -i HOME="$HOME" PATH=/usr/bin:/bin "$BIN" --version',
    f'env -i HOME="$HOME" PATH=/usr/bin:/bin "$BIN" --version {SWITCH}=1',
    f'env -i {SWITCH}=1 env -i PATH=/usr/bin:/bin "$BIN" --version',
    'env -u FOO -i PATH=/usr/bin:/bin "$BIN" --version',
    'env --unset=FOO --ignore-environment PATH=/usr/bin:/bin "$BIN" --version',
    '/usr/bin/env -v -i PATH=/usr/bin:/bin "$BIN" --version',
    f'env -S "-i {SWITCH}=1" "$BIN" --version',
    'exec -c "$BIN" --version',
    f'{SWITCH}=1 exec -c "$BIN" --version',
], ids=["assigned-before-env-i", "exported-before-env-i", "after-the-program",
        "a-second-clear", "clear-after-unset", "long-clear-after-unset",
        "clear-after-verbose", "split-string", "exec-c", "assigned-before-exec-c"])
def test_965_restoration_counts_only_after_the_clear(launch):
    """965-DIFF-001: `SWITCH=1` restores a cleared environment only as an
    operand of the clearing command itself, after the clear and before the
    program. An assignment before `env -i`, an `export` earlier on the line, a
    program argument or a later second clear never reaches the launched
    program, and a clear written after another option (or one the check cannot
    see into, `env -S`) is still a clear."""
    name, path = "bin/cctally-conversation-test", BIN / "cctally-conversation-test"
    problems = rep.shell_violations(path.read_text() + launch + "\n", name=name,
                                    kind="harness")
    assert any(_UNRESTORED in problem for problem in problems), problems


@pytest.mark.parametrize("launch", [
    f'env -i HOME="$HOME" PATH=/usr/bin:/bin {SWITCH}=1 "$BIN" --version',
    f'env -u FOO -i PATH=/usr/bin:/bin {SWITCH}="1" "$BIN" --version',
    f'/usr/bin/env --ignore-environment {SWITCH}=1 "$BIN" --version',
    f'exec -c env {SWITCH}=1 "$BIN" --version',
], ids=["env-i", "clear-after-unset", "long-clear", "exec-c-then-env"])
def test_965_restoration_after_the_clear_is_accepted(launch):
    name, path = "bin/cctally-conversation-test", BIN / "cctally-conversation-test"
    assert not rep.shell_violations(path.read_text() + launch + "\n", name=name,
                                    kind="harness")


def test_965_a_mutated_preamble_is_rejected():
    name, original = _rel(_PREAMBLE), _PREAMBLE.read_text()
    assert not rep.shell_violations(original, name=name, kind="preamble")
    commented = "\n".join("# " + line if re.match(_EXPORT, line) else line
                          for line in original.splitlines()) + "\n"
    assert f"# export {SWITCH}=1" in commented, "mutation found no export to comment out"
    assert rep.shell_violations(commented, name=name, kind="preamble"), (
        "a preamble whose export is commented out was accepted")
    assert rep.shell_violations(_without(original, _EXPORT), name=name, kind="preamble"), (
        "a preamble without its export was accepted")
    assert rep.shell_violations(_after_first(original, _EXPORT, f"unset {SWITCH}"),
                                name=name, kind="preamble"), (
        "a preamble that unsets the switch after exporting it was accepted")
    assert rep.shell_violations(original + f"export {SWITCH}=0\n", name=name,
                                kind="preamble"), (
        "a preamble that overrides the switch at its end was accepted")


@pytest.mark.parametrize("path,kind", [(rep.AGGREGATOR, "aggregator"), (rep.SERVE, "serve")],
                         ids=["test-all", "serve"])
def test_965_a_mutated_exporting_entry_point_is_rejected(path, kind):
    original, name = path.read_text(), _rel(path)
    assert not rep.shell_violations(original, name=name, kind=kind)
    assert rep.shell_violations(_without(original, _EXPORT), name=name, kind=kind), (
        f"{name} without its own export was accepted")
    assert rep.shell_violations(_after_first(original, _EXPORT, f"export {SWITCH}=0"),
                                name=name, kind=kind), (
        f"{name} overriding the switch after initialization was accepted")


def test_965_a_mutated_builder_is_rejected():
    name = "build-codex-reader-fixtures.py"
    original = (BIN / name).read_text(encoding="utf-8")
    assert not rep.builder_violations(original, name)
    assert rep.builder_violations(_without(original, _ASSIGN), name), (
        "a builder that never assigns the switch was accepted")
    indent = re.search(r"^(\s*)" + _ASSIGN, original, re.M).group(1)
    cleared = _after_first(original, _ASSIGN, f'{indent}os.environ.pop("{SWITCH}", None)')
    assert rep.builder_violations(cleared, name), (
        "a builder that clears the switch after initialization was accepted")
    early = _before_first(original, _ASSIGN, f"{indent}import _cctally_core")
    assert rep.builder_violations(early, name), (
        "a builder that imports a cctally module before assigning the switch was accepted")


#: A helper defined (and so runnable) before the function that assigns the
#: switch, per builder: `python3 bin/build-*.py` already has bin/ as
#: sys.path[0], so a sibling import inside it resolves without any sys.path
#: extension of the builder's own.
_EARLY_HELPERS = {"build-codex-reader-fixtures.py": r"^def _synthetic_key\(",
                  "build-bench-fixtures.py": r"^def _iso\("}


@pytest.mark.parametrize("name", sorted(_EARLY_HELPERS))
def test_965_a_builder_helper_importing_cctally_early_is_rejected(name):
    original = (BIN / name).read_text(encoding="utf-8")
    assert not rep.builder_violations(original, name)
    helper = _after_first(original, _EARLY_HELPERS[name], "    import _cctally_cache")
    assert rep.builder_violations(helper, name), (
        "a builder helper that imports a cctally module ahead of the switch "
        "assignment was accepted")
    klass = _before_first(original, r"^def ", "class _Probe:\n    import _cctally_core\n")
    assert rep.builder_violations(klass, name), (
        "a class body, which runs at import, imported a cctally module unseen")


# ── #965 T5: the real preamble and launch path ────────────────────────────

#: What a scrubbed (env -i) launch keeps: the isolated tree, plus the three
#: post-command worker suppressors. Under CCTALLY_DISABLE_DEV_AUTODETECT=1 the
#: dev-checkout telemetry opt-out no longer applies
#: (bin/_cctally_telemetry.py, resolve_telemetry_state), and a detached update,
#: telemetry or retention-sweep worker would outlive this test's scratch tree.
_SCRUB_KEEP = ("HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "CCTALLY_DATA_DIR", "TZ",
               "TMPDIR", "CCTALLY_DISABLE_UPDATE_CHECK",
               "CCTALLY_DISABLE_RETENTION_SWEEP", "CCTALLY_DISABLE_TELEMETRY")


def _shell_script(staged, args, *, scrubbed):
    cli, py = shlex.quote(str(BIN / "cctally")), shlex.quote(sys.executable)
    command = " ".join([py, cli, "cache-sync", *map(shlex.quote, args)])
    if scrubbed:
        keep = " ".join(f"{k}={shlex.quote(staged.env[k])}"
                        for k in _SCRUB_KEEP if k in staged.env)
        # /usr/sbin carries lsof on macOS: a conversations rebuild verifies
        # that no process holds the store family open (_cctally_db, through
        # shutil.which("lsof")) and fails closed without it.
        return (f"env -i {keep} PATH=/usr/bin:/bin:/usr/sbin:/sbin "
                f"CCTALLY_DISABLE_DEV_AUTODETECT=1 {SWITCH}=1 {command}")
    # The preamble runs `unset CODEX_HOME`, so this launch reads $HOME/.codex,
    # where stage() put the Codex tree, exactly as a real Codex harness does.
    preamble = shlex.quote(str(BIN / "_lib-harness-env.sh"))
    return f". {preamble} && {command}"


def _shell_cache_sync(staged, *args, scrubbed):
    env = {k: v for k, v in staged.env.items() if k != SWITCH}  # the shell sets it
    argv = ["bash", "-c", _shell_script(staged, args, scrubbed=scrubbed)]
    if scrubbed:   # the env -i segment restores the switch: no declaration needed
        # timing-budget: #965 hang bound for one cctally launch, LAUNCH_TIMEOUT_S (45 s) from tests/_retention_fixtures.py; a node makes at most two in sequence, under the 120 s cap
        proc = subprocess.run(argv, env=env, capture_output=True, text=True,
                              timeout=LAUNCH_TIMEOUT_S)
    else:
        with iso.retention_launch_control(
                "independent-start", reason="#965 T5: the preamble sets the switch"):
            # timing-budget: #965 hang bound for one cctally launch, LAUNCH_TIMEOUT_S (45 s) from tests/_retention_fixtures.py; a node makes at most two in sequence, under the 120 s cap
            proc = subprocess.run(argv, env=env, capture_output=True, text=True,
                                  timeout=LAUNCH_TIMEOUT_S)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def _shell_observation(tmp_path, *, scrubbed, window):
    """Seed through the SAME shell path with retention explicitly off, which
    proves this path reads a Codex root holding expired transcripts (a preamble
    launch that read none would fail here, not pass a prune control), then
    rebuild through it under ``window`` (None = no setting)."""
    staged = stage(tmp_path)
    seed(staged.data, lambda *args: _shell_cache_sync(staged, *args, scrubbed=scrubbed))
    write_retention(staged.data, window)
    _shell_cache_sync(staged, "--source", "all", "--rebuild", scrubbed=scrubbed)
    return counts(staged.data)


@pytest.mark.parametrize("scrubbed", [False, True], ids=["preamble", "scrubbed-restored"])
def test_965_a_shell_launch_keeps_expired_transcripts(tmp_path, scrubbed):
    got = _shell_observation(tmp_path, scrubbed=scrubbed, window=None)
    assert got["recent_claude"] > 0, got
    assert got["expired_claude"] > 0 and got["expired_codex"] > 0, (
        f"the shell launch deleted expired transcripts: {got}")


def test_965_a_shell_launch_with_a_window_still_prunes(tmp_path):
    got = _shell_observation(tmp_path, scrubbed=False, window=30)
    assert got["recent_claude"] > 0, got
    assert got["expired_claude"] == 0 and got["expired_codex"] == 0, got
