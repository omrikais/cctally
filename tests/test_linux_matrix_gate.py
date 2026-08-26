"""Release-blocking local Linux multi-interpreter gate (#595)."""
from __future__ import annotations
import types

import datetime
import importlib.machinery
import importlib.util
import io
import json
import os
import pathlib
import re
import signal
import subprocess
import sys
import time

import pytest


REPO = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = REPO / "bin" / "cctally-test-linux-matrix"


def _load_gate():
    assert SCRIPT.is_file(), "bin/cctally-test-linux-matrix is missing"
    loader = importlib.machinery.SourceFileLoader(
        "_cctally_test_linux_matrix", str(SCRIPT)
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


def test_matrix_covers_every_supported_python_on_hosted_linux_shape():
    gate = _load_gate()
    assert gate.PYTHON_VERSIONS == ("3.11", "3.12", "3.13")
    assert gate.CONTAINER_PLATFORM == "linux/arm64"
    assert gate.CONTAINER_CHECKOUT == "/workspace/cctally-dev"
    assert gate.CONTAINER_DISTRO == "trixie"


def test_dockerfile_carries_the_provisioning_fidelity_contract():
    gate = _load_gate()
    dockerfile = REPO / "bin" / "cctally-test-linux-matrix.Dockerfile"
    assert dockerfile.is_file(), "bin/cctally-test-linux-matrix.Dockerfile is missing"
    text = dockerfile.read_text()

    # Every pin below moved verbatim out of the generated container script and
    # is the provisioning half of the fidelity contract.
    assert "apt-get install" in text and " git " in text
    assert (
        "ca-certificates curl git jq bsdextrautils procps bc sqlite3 unzip "
        "xz-utils locales rsync gcc libc6-dev" in text
    )
    assert "localedef -i en_US -f UTF-8 en_US.UTF-8" in text
    assert "node-v${node_version}-linux-arm64.tar.xz" in text
    assert "SHASUMS256.txt" in text
    assert "sqlite-autoconf-3530300.tar.gz" in text
    assert "c917d7db16648ec95f714974ace5e5dcf46b7dc70e26600a0a102a3141125db0" in text
    assert "SQLITE_ENABLE_DBPAGE_VTAB" in text
    assert "useradd" in text
    assert "--no-deps" in text
    assert "pip check" in text
    assert "npm ci" in text

    # Both dependency trees are baked at their FINAL in-checkout paths. A
    # symlink from /opt was falsified: a trailing-slash gitignore rule matches a
    # directory and not a symlink, so `git status` would report both untracked.
    assert "${CCTALLY_CHECKOUT}/.venv" in text
    assert "${CCTALLY_CHECKOUT}/dashboard/web/" in text
    assert "ln -s" not in text

    # The image is labelled with its own baked toolchain manifest digest, so
    # drift in the cached environment is observable in release evidence.
    assert "cctally.image.inputs" in text
    assert "cctally.image.apt-epoch" in text
    assert "cctally.image.toolchain" in text

    # The freshness epoch must be consumed BEFORE apt-get update, or Docker
    # reuses the cached layer forever and the image freezes against a moving
    # Debian repository. Comment lines are excluded deliberately: this is an
    # assertion about instruction order, and prose describing the rule would
    # otherwise satisfy it.
    instructions = [
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    ]
    epoch_line = next(
        index
        for index, line in enumerate(instructions)
        if "${CCTALLY_APT_EPOCH}" in line and not line.startswith("ARG ")
    )
    update_line = next(
        index for index, line in enumerate(instructions) if "apt-get update" in line
    )
    assert epoch_line < update_line


def test_container_command_carries_the_runtime_fidelity_contract(tmp_path):
    gate = _load_gate()
    image = "sha256:" + "a" * 64
    command = gate._container_command("docker", "3.11", tmp_path, image)
    rendered = " ".join(command)
    body = gate._container_script("3.11", "deadbeef" * 5)

    assert command[:2] == ["docker", "run"]
    assert "--platform linux/arm64" in rendered
    assert "--tmpfs /tmp:" in rendered
    assert f"{tmp_path}:/source-repo:ro" in rendered
    # The validated image is run by its IMMUTABLE id, never by the mutable tag,
    # and it is never re-pulled: it is a locally built image.
    assert "--pull=never" in rendered
    assert "--pull=always" not in rendered
    assert command[-4:-1] == [image, "bash", "-lc"]
    assert command[-1] == body
    # The release runbook reaps lane containers by this label on an interrupt,
    # scoped to the tested commit so it cannot remove a concurrent run's.
    assert f"--label {gate.LANE_LABEL}={'deadbeef' * 5}" in rendered

    # Provisioning is the image's job now; the lane must not redo any of it.
    assert "apt-get" not in body
    assert "localedef" not in body
    assert "npm ci" not in body
    assert "python -m venv" not in body
    assert "pip install" not in body

    # The baked trees are DESTINATION-owned. A detached clone cannot contain
    # ignored untracked files but CAN contain force-added tracked content under
    # a protected root, so the lane refuses that ownership conflict before it
    # copies anything. `-e` alone would accept a dangling symlink, so each root
    # is checked with `-L` as well.
    assert (
        "git -C /source-repo ls-files -z -- .venv dashboard/web/node_modules" in body
    )
    assert "test ! -e /source-repo/.venv" in body
    assert "test ! -L /source-repo/.venv" in body
    assert "test ! -e /source-repo/dashboard/web/node_modules" in body
    assert "test ! -L /source-repo/dashboard/web/node_modules" in body

    # Ownership is repaired with the two baked trees PRUNED. A plain `chown -R`
    # would walk and copy up every baked dependency file on every run, giving
    # back most of the saving this change exists to produce.
    assert "-prune" in body
    assert f"chown -R 10001:10001 {gate.CONTAINER_CHECKOUT}\n" not in body
    # `-h` is load-bearing. `chown -R` defaults to -P and never dereferences,
    # but `find -exec chown` hands it a path, and tests/fixtures contains
    # symlinks whose targets do not exist, so a dereferencing chown fails the
    # whole lane on the first one. Observed in a real container, not inferred.
    assert "-exec chown -h 10001:10001 {} +" in body

    # Both roots survive the merge as real directories holding the same objects.
    assert "stat -c '%d:%i' /workspace/cctally-dev/.venv" in body
    assert (
        "stat -c '%d:%i' /workspace/cctally-dev/dashboard/web/node_modules" in body
    )
    assert "test -d /workspace/cctally-dev/.venv" in body
    assert "test ! -L /workspace/cctally-dev/.venv" in body
    assert "test -d /workspace/cctally-dev/dashboard/web/node_modules" in body
    assert "test ! -L /workspace/cctally-dev/dashboard/web/node_modules" in body
    assert "! -uid 10001 -o ! -gid 10001" in body

    # The suite owns a real indexed checkout at the non-shadowing path and runs
    # as a non-root user with a fresh /tmp.
    assert "runuser -u cctally" in body
    assert "TMPDIR=/opt/cctally-setup-tmp" in body.split(
        "runuser -u cctally -- env", 1
    )[1]
    assert "cp -a /source-repo/. /workspace/cctally-dev/" in body
    assert "git rev-parse HEAD" in body
    assert "git status --porcelain" in body

    # Match the private manual Linux lane's hard capabilities and explicit
    # agentmem boundary rather than silently skipping missing dependencies.
    assert ". .venv/bin/activate" in body
    assert "python -m pip check" in body
    assert "sqlite3 :memory: '.recover'" in body
    assert "_lib-fts5-probe.sh require python" in body
    assert "dashboard/web/node_modules/.bin/vitest" in body
    assert "GITHUB_ACTIONS=true" in body
    assert "CCTALLY_AUTHORITATIVE_RUN=1" in body
    assert "CCTALLY_AGENTMEM_TEST_POLICY=hosted-private-unavailable" in body
    assert "CCTALLY_LINUX_MATRIX_RUN=1" in body
    assert "CCTALLY_OUTER_JOBS=2" in body
    assert "CCTALLY_PYTEST_JOBS=2" in body
    assert "TZ=Etc/UTC" in body
    assert "bin/cctally-test-all" in body


def test_the_public_profile_suppresses_the_private_linux_profile():
    """Running the projected PUBLIC tree with CCTALLY_LINUX_MATRIX_RUN=1 would
    reproduce the exact production refusal this session repairs: the public
    profile has no `test-remote` to omit, so `bin/cctally-test-all` exits 2
    (#630 S5). The public lane runs ordinary COVERAGE_MODE=full instead.
    """
    gate = _load_gate()
    body = gate._container_script("3.13", "deadbeef" * 5, profile="public")

    assert "CCTALLY_LINUX_MATRIX_RUN" not in body
    assert "CCTALLY_OUTER_JOBS=2" in body
    assert "CCTALLY_PYTEST_JOBS=2" in body
    assert "bin/cctally-test-all" in body


def test_the_private_profile_is_the_default_and_still_exports_it():
    """Non-vacuity for the case above: the suppression must be the PROFILE's
    doing and not the export having been deleted outright, and every existing
    caller passes no profile at all."""
    gate = _load_gate()

    assert (
        gate._container_script("3.13", "deadbeef" * 5)
        == gate._container_script("3.13", "deadbeef" * 5, profile="private")
    )
    assert "CCTALLY_LINUX_MATRIX_RUN=1" in gate._container_script(
        "3.13", "deadbeef" * 5)


def test_the_candidate_label_is_separable_from_the_expected_head():
    """The release trap reaps interrupted lanes by
    `label=cctally.linux-matrix.head=$MATRIX_HEAD`, where MATRIX_HEAD is the
    SOURCE head. Under a projected tree the in-container expected head is a
    synthetic commit, so without this split an interrupted matrix stops being
    reapable (#630 S5).
    """
    gate = _load_gate()
    command = gate._container_command(
        "docker", "3.13", pathlib.Path("/src"), "img",
        expected_head="b" * 40, candidate_label="a" * 40,
    )
    label = command[command.index("--label") + 1]

    assert label == f"{gate.LANE_LABEL}={'a' * 40}"
    assert "b" * 40 not in label
    # The expected head is NOT dropped: the in-container assertion still pins
    # the tree that actually runs, which is the projected commit.
    assert f"test \"$(git rev-parse HEAD)\" = {'b' * 40}" in command[-1]


def test_the_candidate_label_defaults_to_the_expected_head():
    """Every caller that does not project keeps the behaviour it had."""
    gate = _load_gate()
    command = gate._container_command(
        "docker", "3.13", pathlib.Path("/src"), "img", expected_head="b" * 40)

    assert command[command.index("--label") + 1] == f"{gate.LANE_LABEL}={'b' * 40}"


def test_dirty_tree_refuses_before_container_engine_probe(monkeypatch, tmp_path):
    gate = _load_gate()
    calls: list[list[str]] = []

    monkeypatch.setattr(gate, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(gate, "_git_head", lambda root: "a" * 40)
    monkeypatch.setattr(gate, "_git_status", lambda root: " M bin/cctally")

    def _unexpected(command, **kwargs):
        calls.append(command)
        raise AssertionError("dirty-tree refusal must precede external commands")

    monkeypatch.setattr(gate, "_run", _unexpected)
    assert gate.main(["--engine", "docker"]) == 2
    assert calls == []


def _stub_lanes(gate, monkeypatch, lane_exits=None, record=None):
    def _lanes(
        engine, versions, images, source_checkout, expected_head, outdir,
        acceptance=None, **kwargs,
    ):
        if record is not None:
            record.append(("lanes", tuple(versions)))
        for version in versions:
            (outdir / f"{version}.log").write_text(
                f"linux-matrix stub lane {version}\n"
            )
        if lane_exits is not None:
            return dict(lane_exits)
        return {version: 0 for version in versions}

    monkeypatch.setattr(gate, "_run_lanes", _lanes)


def _stub_successful_matrix(monkeypatch, gate, tmp_path, lane_exits=None, record=None):
    monkeypatch.setattr(gate, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(gate, "_materialize_clean_head", lambda *args: None)

    def _resolve(engine, version, epoch, root):
        if record is not None:
            record.append(("resolve", version))
        return f"sha256:{version}"

    monkeypatch.setattr(gate, "_resolve_image", _resolve)
    _stub_lanes(gate, monkeypatch, lane_exits=lane_exits, record=record)
    monkeypatch.setattr(
        gate,
        "_run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0),
    )


def test_focused_python_selection_is_explicitly_incomplete(
    monkeypatch, tmp_path, capsys,
):
    gate = _load_gate()
    _stub_successful_matrix(monkeypatch, gate, tmp_path)
    monkeypatch.setattr(gate, "_git_head", lambda root: "a" * 40)
    monkeypatch.setattr(gate, "_git_status", lambda root: "")

    assert gate.main(["--python", "3.11"]) == 3
    captured = capsys.readouterr()
    assert "INCOMPLETE" in captured.err
    assert "release gate" in captured.err


def test_matrix_refuses_if_head_changes_before_pass(
    monkeypatch, tmp_path, capsys,
):
    gate = _load_gate()
    _stub_successful_matrix(monkeypatch, gate, tmp_path)
    heads = iter(("a" * 40, "b" * 40))
    monkeypatch.setattr(gate, "_git_head", lambda root: next(heads))
    monkeypatch.setattr(gate, "_git_status", lambda root: "")

    assert gate.main([]) == 2
    captured = capsys.readouterr()
    assert "HEAD changed during matrix" in captured.err
    assert "PASS" not in captured.out


def test_matrix_refuses_if_tree_becomes_dirty_before_pass(
    monkeypatch, tmp_path, capsys,
):
    gate = _load_gate()
    _stub_successful_matrix(monkeypatch, gate, tmp_path)
    statuses = iter(("", " M bin/cctally"))
    monkeypatch.setattr(gate, "_git_head", lambda root: "a" * 40)
    monkeypatch.setattr(gate, "_git_status", lambda root: next(statuses))

    assert gate.main([]) == 2
    captured = capsys.readouterr()
    assert "tree changed during matrix" in captured.err
    assert "PASS" not in captured.out


def test_a_pre_materialized_source_tree_is_run_without_cloning(
    monkeypatch, tmp_path,
):
    """`--source-tree` runs a tree somebody else materialized. The projector
    that builds the public projection is PRIVATE and this driver is public, so
    the driver accepts the result rather than learning to project (#630 S5).
    """
    gate = _load_gate()
    projected = tmp_path / "public"
    projected.mkdir()
    _stub_successful_matrix(monkeypatch, gate, tmp_path)

    def _refuse(*args, **kwargs):
        raise AssertionError("--source-tree must not clone anything")

    monkeypatch.setattr(gate, "_materialize_clean_head", _refuse)
    heads = {tmp_path: "a" * 40, projected: "c" * 40}
    monkeypatch.setattr(
        gate, "_git_head", lambda root: heads[pathlib.Path(root)])
    monkeypatch.setattr(gate, "_git_status", lambda root: "")
    observed: dict[str, object] = {}

    def _lanes(engine, versions, images, source_checkout, expected_head, outdir,
               acceptance=None, **kwargs):
        observed["source"] = pathlib.Path(source_checkout)
        observed["expected_head"] = expected_head
        observed.update(kwargs)
        for version in versions:
            (outdir / f"{version}.log").write_text("stub\n")
        return {version: 0 for version in versions}

    monkeypatch.setattr(gate, "_run_lanes", _lanes)

    assert gate.main([
        "--source-tree", str(projected),
        "--profile", "public",
        "--candidate-label", "a" * 40,
    ]) == 0
    assert observed["source"] == projected
    # The in-container assertion pins the tree that actually runs, so it is the
    # PROJECTED head. The label stays the SOURCE head, which is what the release
    # runbook's interrupt trap reaps by.
    assert observed["expected_head"] == "c" * 40
    assert observed["profile"] == "public"
    assert observed["candidate_label"] == "a" * 40


@pytest.fixture(autouse=True)
def release_record_dir(monkeypatch, tmp_path):
    """Redirect APP_DIR so no test writes an advisory record into the real one.

    Autouse and module-wide, because EVERY `main()` path writes a record on
    entry — including the refusals that return before a single lane runs. The
    repository's write-isolation guard caught exactly that and named this fix:
    `_record_dir` reads `_cctally_core.APP_DIR` at call time, so patching the
    kernel reaches every gate module a test loads afterwards.
    """
    import _cctally_core

    root = tmp_path / "app"
    monkeypatch.setattr(_cctally_core, "APP_DIR", root)
    return root / "release-records"


def _only_record(directory):
    written = sorted(directory.glob("*.json"))
    assert len(written) == 1, written
    return json.loads(written[0].read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("lane_exits", "signal", "exit_code"),
    (
        ({"3.11": 0, "3.12": 0, "3.13": 0}, "green", 0),
        ({"3.11": 0, "3.12": 1, "3.13": 0}, "productRed", 1),
        ({"3.11": 0, "3.12": 3, "3.13": 1}, "incomplete", 3),
    ),
)
def test_the_record_maps_each_composed_exit_to_its_signal(
    monkeypatch, tmp_path, release_record_dir, lane_exits, signal, exit_code,
):
    """The record reuses the driver's existing four-outcome vocabulary rather
    than inventing one, and preserves every lane's OWN code — a merged verdict
    of 3 must not erase which lane produced the 1 (#630 S5)."""
    gate = _load_gate()
    directory = release_record_dir
    _stub_successful_matrix(monkeypatch, gate, tmp_path, lane_exits=lane_exits)
    monkeypatch.setattr(gate, "_git_head", lambda root: "a" * 40)
    monkeypatch.setattr(gate, "_git_status", lambda root: "")

    assert gate.main([]) == exit_code
    record = _only_record(directory)

    assert record["schemaVersion"] == 1
    assert record["state"] == "finished"
    assert record["signal"] == signal
    assert record["exitCode"] == exit_code
    assert record["lanes"] == lane_exits
    assert record["sourceHead"] == "a" * 40
    assert record["projectedTree"] == "a" * 40
    assert record["startedAt"].endswith("Z")
    assert record["finishedAt"].endswith("Z")


def test_an_invalidated_candidate_is_recorded_as_invalidated(
    monkeypatch, tmp_path, release_record_dir,
):
    """Exit 2's own signal. Every lane may have passed and the record must
    still not read green, because the candidate changed underneath them."""
    gate = _load_gate()
    directory = release_record_dir
    _stub_successful_matrix(monkeypatch, gate, tmp_path)
    heads = iter(("a" * 40, "b" * 40))
    monkeypatch.setattr(gate, "_git_head", lambda root: next(heads))
    monkeypatch.setattr(gate, "_git_status", lambda root: "")

    assert gate.main([]) == 2
    record = _only_record(directory)

    assert record["signal"] == "invalidated"
    assert record["lanes"] == {"3.11": 0, "3.12": 0, "3.13": 0}
    assert gate.record_is_green(record) is False


def test_a_refusal_before_any_lane_records_null_lanes(
    monkeypatch, tmp_path, release_record_dir,
):
    """`lanes` is nullable because a dirty-tree or engine refusal happens
    before any lane exit code exists. An empty MAPPING would read as "three
    lanes, none reported", which is a different claim."""
    gate = _load_gate()
    directory = release_record_dir
    monkeypatch.setattr(gate, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(gate, "_git_head", lambda root: "a" * 40)
    monkeypatch.setattr(gate, "_git_status", lambda root: " M bin/cctally")

    assert gate.main(["--engine", "docker"]) == 2
    record = _only_record(directory)

    assert record["lanes"] is None
    assert record["state"] == "finished"
    assert record["signal"] == "invalidated"
    assert gate.record_is_green(record) is False


def test_a_started_only_record_cannot_be_read_as_green(
    monkeypatch, tmp_path, release_record_dir,
):
    """A driver that dies mid-run leaves `started` rather than nothing, and an
    advisory gate that fails quietly is indistinguishable from one that passed.
    Only one of those is safe to ship on."""
    gate = _load_gate()
    directory = release_record_dir
    _stub_successful_matrix(monkeypatch, gate, tmp_path)
    monkeypatch.setattr(gate, "_git_head", lambda root: "a" * 40)
    monkeypatch.setattr(gate, "_git_status", lambda root: "")

    def _die(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(gate, "_run_lanes", _die)
    with pytest.raises(KeyboardInterrupt):
        gate.main([])

    record = _only_record(directory)
    assert record["state"] == "finished"
    assert record["signal"] != "green"
    assert gate.record_is_green(record) is False
    # And the on-entry shape itself, which is what an unwound process leaves.
    entry = {"state": "started", "signal": None, "exitCode": None, "lanes": None}
    assert gate.record_is_green(entry) is False


def test_the_entry_record_exists_before_any_lane_runs(
    monkeypatch, tmp_path, release_record_dir,
):
    """The `started` write is not a nicety: without it a driver killed between
    entry and the first lane leaves no record at all, and "no run" would be
    indistinguishable from "record not yet written"."""
    gate = _load_gate()
    directory = release_record_dir
    _stub_successful_matrix(monkeypatch, gate, tmp_path)
    monkeypatch.setattr(gate, "_git_head", lambda root: "a" * 40)
    monkeypatch.setattr(gate, "_git_status", lambda root: "")
    seen: list[dict] = []

    def _peek(engine, versions, images, source_checkout, expected_head, outdir,
              acceptance=None, **kwargs):
        seen.append(_only_record(directory))
        for version in versions:
            (outdir / f"{version}.log").write_text("stub\n")
        return {version: 0 for version in versions}

    monkeypatch.setattr(gate, "_run_lanes", _peek)

    assert gate.main([]) == 0
    assert seen and seen[0]["state"] == "started"
    assert seen[0]["lanes"] is None
    assert seen[0]["signal"] is None
    assert seen[0]["finishedAt"] is None


def test_two_runs_write_two_distinct_records(
    monkeypatch, tmp_path, release_record_dir,
):
    """Records are immutable and per-run, keyed by run id rather than by a
    candidate version — no version is stamped at preflight time, and a single
    mutable path cannot survive two interleaved runs."""
    gate = _load_gate()
    directory = release_record_dir
    _stub_successful_matrix(monkeypatch, gate, tmp_path)
    monkeypatch.setattr(gate, "_git_head", lambda root: "a" * 40)
    monkeypatch.setattr(gate, "_git_status", lambda root: "")

    assert gate.main([]) == 0
    assert gate.main([]) == 0

    written = sorted(directory.glob("*.json"))
    assert len(written) == 2, written
    ids = {json.loads(p.read_text(encoding="utf-8"))["runId"] for p in written}
    assert len(ids) == 2, ids


def test_the_record_is_published_by_an_atomic_replace(
    monkeypatch, tmp_path, release_record_dir,
):
    """A write interrupted mid-publication must never leave truncated JSON in
    place of a readable record, so the record is written to a sibling temporary
    file and renamed."""
    gate = _load_gate()
    directory = release_record_dir
    real_replace = os.replace
    calls: list[tuple[str, str]] = []

    def _spy(src, dst, **kwargs):
        calls.append((str(src), str(dst)))
        return real_replace(src, dst, **kwargs)

    monkeypatch.setattr(gate.os, "replace", _spy)
    _stub_successful_matrix(monkeypatch, gate, tmp_path)
    monkeypatch.setattr(gate, "_git_head", lambda root: "a" * 40)
    monkeypatch.setattr(gate, "_git_status", lambda root: "")

    assert gate.main([]) == 0
    assert calls, "the record was written in place rather than renamed"
    for source, destination in calls:
        assert source != destination
        assert destination.endswith(".json")


def test_the_record_path_is_printed_so_the_operator_block_can_capture_it(
    monkeypatch, tmp_path, release_record_dir, capsys,
):
    """The release runbook echoes the record path beside the matrix exit."""
    gate = _load_gate()
    directory = release_record_dir
    _stub_successful_matrix(monkeypatch, gate, tmp_path)
    monkeypatch.setattr(gate, "_git_head", lambda root: "a" * 40)
    monkeypatch.setattr(gate, "_git_status", lambda root: "")

    assert gate.main([]) == 0
    written = sorted(directory.glob("*.json"))
    assert str(written[0]) in capsys.readouterr().out


def test_the_record_directory_is_derived_from_the_app_dir_chokepoint():
    """Records live under APP_DIR, outside the git tree. Inside it they would
    be untracked files that Gate 0's own cleanliness selection would then see,
    and the two gates would disagree about whether the tree is clean.

    Asserted as the RELATION to `_cctally_core.APP_DIR` rather than as a
    literal path: the resolution order (explicit override, then dev checkout,
    then prod) has one home and this must not become a second.
    """
    import _cctally_core

    gate = _load_gate()
    directory = gate._record_dir()

    assert directory == _cctally_core.APP_DIR / "release-records"
    assert REPO not in directory.parents and directory != REPO


OVERLAP_MARKER = "# cctally release Gate 0 / Gate 0.25 overlap block"


def _overlap_block(text):
    fences = re.findall(r"```bash\n(.*?)```", text, re.DOTALL)
    matching = [fence for fence in fences if OVERLAP_MARKER in fence]
    assert len(matching) == 1, f"expected exactly one overlap block, got {len(matching)}"
    return matching[0]


def test_the_matrix_is_advisory_and_still_printed():
    """Gate 0 and its receipt are the only release-blocking test gates.

    The matrix leaves the refusal predicate and keeps its printed line, joined
    by a line naming the record path. Removing it from the predicate is the
    whole of "advisory"; dropping the print as well would make a red matrix
    indistinguishable from a green one, which is the failure this session's
    record exists to prevent (#630 S5).
    """
    skill = REPO / ".agents/skills/release-cctally/SKILL.md"
    if not skill.exists():
        pytest.skip("private release skill absent from public mirror")
    text = skill.read_text()
    gate0 = text.index("**Gate 0 —")
    matrix = text.index("**Gate 0.25 —")
    pricing = text.index("**Gate 0.5 —")
    assert gate0 < matrix < pricing
    section = text[matrix:pricing]
    assert "bin/cctally-test-linux-matrix" in section

    block = _overlap_block(text)

    # The refusal predicate. Asserted on the line itself rather than on the
    # whole block, because `MATRIX_RC` is still assigned and still echoed.
    lines = block.splitlines()
    refuse_at = [i for i, line in enumerate(lines) if "REFUSE" in line]
    assert len(refuse_at) == 1, refuse_at
    guards = [
        line for line in lines[:refuse_at[0]] if line.startswith("if [")
    ]
    predicate = guards[-1]

    assert "GATE0_RC" in predicate, predicate
    assert "RECEIPT_RC" in predicate, predicate
    assert "MATRIX_RC" not in predicate, predicate

    # Both the matrix exit and the record path are always printed.
    assert 'echo "Gate 0.25 Linux matrix:' in block
    assert "MATRIX_RECORD" in block
    assert "release record" in block

    # The one local run validates the PROJECTED PUBLIC tree, and the label
    # stays the SOURCE head so the interrupt trap can still reap by it.
    assert "_cctally_public_projection" in block
    assert "--source-tree" in block
    assert "--profile public" in block
    assert '--candidate-label "$MATRIX_HEAD"' in block
    # Gate 0 is backgrounded in its own process group and Gate 0.25 runs in the
    # foreground, so the preflight costs the longer of the two rather than the
    # sum. Gate 0 owns the exit-75 watch-retry loop, which encapsulates cleanly
    # inside the one background job.
    assert ") &" in block
    assert "GATE0_PGID=$!" in block
    assert "MATRIX_PGID=$!" in block
    assert 'wait "$MATRIX_PGID"' in block
    assert "bin/cctally-test-all" in block
    assert 'while [ "$gate0_rc" -eq 75 ]' in block

    # The matrix runs BEFORE the wait, or the two gates would be sequential
    # again, and a failing matrix must never abandon a running Gate 0.
    started = block.index("bin/cctally-test-linux-matrix")
    waited = block.index('wait "$GATE0_PGID"')
    assert started < waited

    # Both statuses are collected through `if ...; then ... else rc=$?; fi`
    # rather than bare substitution, because a non-zero status from either gate
    # is an expected outcome to report rather than a reason to abort.
    assert "MATRIX_RC=$?" in block
    assert "GATE0_RC=" in block
    assert "RECEIPT_RC=" in block

    # Receipt verification stays blocking, runs after the wait, and only when
    # Gate 0's terminal status is zero.
    verified = block.index("--verify-receipt")
    assert waited < verified
    assert 'if [ "$GATE0_RC" -eq 0 ]' in block

    # An interrupt reaps both process groups and any lane containers.
    assert "trap " in block
    assert "INT" in block and "TERM" in block and "EXIT" in block
    assert "docker ps -aq --filter" in block

    # The matrix's git reads suppress optional index refreshes so it cannot
    # rewrite the index while Gate 0 copies it.
    assert "GIT_OPTIONAL_LOCKS=0" in block


# A module-level assignment rather than an inline parametrize tuple, so the
# waiver marker below has a simple statement to scope itself to — a decorator
# belongs to the function definition, which spans its whole body. These four
# name maintainer-only documentation in order to assert what it no longer
# CONTAINS, and the test skips on a checkout that does not carry them, so none
# of them is a dependency. The marker has to sit INSIDE the statement's own
# source span; a comment above it is not part of the node. That span covers the
# WHOLE tuple, so an entry added here inherits the waiver silently — check any
# new path against the same test before adding it.
_RETIRED_PROSE_FILES = (  # mirror-private-ok
    ".agent-workflows/skills/shared/release-cctally/SKILL.md",
    ".agents/skills/release-cctally/SKILL.md",
    ".claude/skills/release-cctally/SKILL.md",
    "docs/remote-testing.md",
)


@pytest.mark.parametrize("relative", _RETIRED_PROSE_FILES)
def test_the_expired_known_red_discharge_is_gone(relative):
    """#622 and #623 are both closed, so the section's own expiry has fired,
    and #624 — which would have mechanized it — closed NOT_PLANNED. A waiver
    that outlives its condition turns into policy (#630 S5).

    The two generated projections are included so a missed
    `bin/check-agent-workflows --sync` cannot leave the retired text shipping
    in the copy an agent actually loads.
    """
    path = REPO / relative
    if not path.is_file():
        pytest.skip("maintainer-only documentation absent from this checkout")
    text = path.read_text(encoding="utf-8")

    assert "must be exactly" not in text
    assert "known-red" not in text
    assert "#624" not in text
    assert "58 of 59" not in text
    assert "workflow_dispatch" not in text


def _stub_release_root(tmp_path):
    root = tmp_path / "release-root"
    (root / "bin").mkdir(parents=True)
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    (root / "bin" / "cctally-test-remote").write_text(
        "#!/bin/bash\n"
        'echo "remote $*" >> "$STUB_LOG"\n'
        'if [ "$1" = "--verify-receipt" ]; then exit "${STUB_RECEIPT_RC:-0}"; fi\n'
        'if [ "$1" = "--watch" ]; then\n'
        '  sleep "${STUB_GATE0_SLEEP:-0}"\n'
        '  printf \'{"receipt": {"runId": "RUN-1"}}\\n\'\n'
        '  exit "${STUB_GATE0_RC:-0}"\n'
        "fi\n"
        "exit 3\n"
    )
    (root / "bin" / "cctally-test-linux-matrix").write_text(
        "#!/bin/bash\n"
        'echo "matrix $*" >> "$STUB_LOG"\n'
        'if [ -n "${STUB_RECORD_PATH:-}" ]; then\n'
        '  echo "{}" > "$STUB_RECORD_PATH"\n'
        '  echo "linux-matrix: release record $STUB_RECORD_PATH"\n'
        "fi\n"
        'sleep "${STUB_MATRIX_SLEEP:-0}"\n'
        'exit "${STUB_MATRIX_RC:-0}"\n'
    )
    # The PRIVATE projector, stubbed. It is invoked as a python module path
    # rather than through PATH, so it lives beside the other bin stubs.
    (root / "bin" / "_cctally_public_projection.py").write_text(
        "import os, sys\n"
        "with open(os.environ['STUB_LOG'], 'a') as handle:\n"
        "    handle.write('project %s\\n' % ' '.join(sys.argv[1:]))\n"
        "rc = int(os.environ.get('STUB_PROJECT_RC', '0'))\n"
        "if rc:\n"
        "    sys.exit(rc)\n"
        "os.makedirs(sys.argv[2], exist_ok=True)\n"
        "print('projected-sha')\n"
    )
    (stubs / "git").write_text(
        "#!/bin/bash\n"
        'case "$*" in\n'
        '  *"rev-parse HEAD"*) echo "abc123def456" ;;\n'
        '  *"--show-toplevel"*) echo "$RELEASE_ROOT" ;;\n'
        "  *) : ;;\n"
        "esac\n"
        "exit 0\n"
    )
    (stubs / "docker").write_text(
        "#!/bin/bash\n"
        'echo "docker $*" >> "$STUB_LOG"\n'
        # A lane container of the tested commit. Without it `docker ps -aq`
        # prints nothing, `docker rm -f` is never reached, and the interrupt
        # test asserts only that the query ran.
        'if [ "$1" = "ps" ]; then echo "cafefeed0001"; fi\n'
        "exit 0\n"
    )
    for script in (
        root / "bin" / "cctally-test-remote",
        root / "bin" / "cctally-test-linux-matrix",
        stubs / "git",
        stubs / "docker",
    ):
        script.chmod(0o755)
    return root, stubs


def _run_overlap_block(tmp_path, env_overrides, signal_after=None):
    skill = REPO / ".agents/skills/release-cctally/SKILL.md"
    if not skill.exists():
        pytest.skip("private release skill absent from public mirror")
    block = _overlap_block(skill.read_text())
    root, stubs = _stub_release_root(tmp_path)
    log = tmp_path / "stub.log"
    log.write_text("")
    env = dict(os.environ)
    env.update(
        {
            "PATH": f"{stubs}:{env.get('PATH', '')}",
            "RELEASE_ROOT": str(root),
            "GATE0_OUT": str(tmp_path / "gate0.json"),
            "GATE0_STATUS": str(tmp_path / "gate0.status"),
            "STUB_LOG": str(log),
            "STUB_RECORD_PATH": str(tmp_path / "record.json"),
            # The block `rm -f`s this path and then `tee`s through it, so an
            # unpinned default is shared state: xdist distributes this module's
            # tests across workers, and one invocation's rm can unlink the file
            # another is still writing.
            "MATRIX_OUT": str(tmp_path / "matrix.log"),
        }
    )
    env.update(env_overrides)
    process = subprocess.Popen(
        ["bash", "-c", block],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if signal_after is not None:
        time.sleep(signal_after)
        process.terminate()
    out, _ = process.communicate(timeout=120)
    return process.returncode, out, log.read_text()


def test_overlap_block_waits_for_gate_zero_even_when_the_matrix_fails(tmp_path):
    """A failing matrix must never abandon a running Gate 0, and both statuses
    must survive `set -e` semantics rather than aborting the collection.

    The matrix is ADVISORY since #630 S5, so its red is reported and the block
    still succeeds. Gate 0 and its receipt are the only blocking test gates.
    """
    status, out, log = _run_overlap_block(
        tmp_path,
        {"STUB_MATRIX_RC": "1", "STUB_GATE0_RC": "0", "STUB_GATE0_SLEEP": "2"},
    )
    assert status == 0, out
    assert "REFUSE" not in out
    assert "Gate 0.25 Linux matrix:" in out
    assert "exit 1" in out
    assert "Gate 0 authoritative suite:" in out
    assert "remote --watch bin/cctally-test-all" in log
    # Gate 0 passed, so its receipt verification was attempted.
    assert "--verify-receipt RUN-1" in log


def test_overlap_block_projects_the_public_tree_for_the_matrix(tmp_path):
    """The single local run validates the projected PUBLIC tree, which is what
    users install and the only choice that would have caught the live break."""
    status, out, log = _run_overlap_block(
        tmp_path, {"STUB_GATE0_RC": "0", "STUB_MATRIX_RC": "0"}
    )
    assert status == 0, out
    assert "project HEAD " in log, log
    matrix_calls = [line for line in log.splitlines() if line.startswith("matrix ")]
    assert len(matrix_calls) == 1, log
    assert "--source-tree " in matrix_calls[0]
    assert "--profile public" in matrix_calls[0]
    assert "--candidate-label abc123def456" in matrix_calls[0]


def test_overlap_block_reports_a_matrix_that_left_no_record(tmp_path):
    """An advisory gate that fails quietly is indistinguishable from one that
    passed. A missing record is stated rather than omitted."""
    status, out, _ = _run_overlap_block(
        tmp_path,
        {"STUB_GATE0_RC": "0", "STUB_MATRIX_RC": "0", "STUB_RECORD_PATH": ""},
    )
    assert status == 0, out
    assert "NONE" in out, out


def test_overlap_block_survives_a_projection_that_cannot_be_built(tmp_path):
    """A projection failure leaves no Linux evidence, and that is reported —
    but it must not block a cut that Gate 0 and its receipt both cleared."""
    status, out, log = _run_overlap_block(
        tmp_path,
        {"STUB_GATE0_RC": "0", "STUB_MATRIX_RC": "0", "STUB_PROJECT_RC": "1"},
    )
    assert status == 0, out
    assert "REFUSE" not in out
    assert "matrix " not in log, log
    assert "Gate 0.25 Linux matrix:      exit 3" in out


def test_overlap_block_skips_receipt_verification_when_gate_zero_failed(tmp_path):
    status, out, log = _run_overlap_block(
        tmp_path, {"STUB_GATE0_RC": "1", "STUB_MATRIX_RC": "0"}
    )
    assert status == 2, out
    assert "--verify-receipt" not in log
    assert "REFUSE" in out


def test_overlap_block_refuses_when_only_the_receipt_fails(tmp_path):
    status, out, log = _run_overlap_block(
        tmp_path,
        {"STUB_GATE0_RC": "0", "STUB_MATRIX_RC": "0", "STUB_RECEIPT_RC": "3"},
    )
    assert status == 2, out
    assert "--verify-receipt RUN-1" in log
    assert "REFUSE" in out


def test_overlap_block_reports_success_when_all_three_pass(tmp_path):
    status, out, log = _run_overlap_block(
        tmp_path, {"STUB_GATE0_RC": "0", "STUB_MATRIX_RC": "0"}
    )
    assert status == 0, out
    assert "REFUSE" not in out


def test_overlap_block_reaps_both_gates_on_a_termination_signal(tmp_path):
    """An interrupt cancels both gates and reaps their children and containers;
    the operator restarts rather than resumes."""
    status, out, log = _run_overlap_block(
        tmp_path,
        {
            "STUB_GATE0_SLEEP": "20",
            "STUB_MATRIX_SLEEP": "20",
            "STUB_GATE0_RC": "0",
            "STUB_MATRIX_RC": "0",
        },
        signal_after=1.0,
    )
    assert status != 0
    assert "docker ps -aq --filter" in log
    # The query returned a lane container, so the reap must have removed it.
    assert "docker rm -f cafefeed0001" in log
    # Neither gate reached its own completion. Bash defers a trap until the
    # running FOREGROUND command returns, so a matrix run in the foreground
    # would have delayed the whole reap until it finished on its own; the
    # matrix therefore runs in its own process group and the shell waits for
    # it, because `wait` is interruptible.
    assert not (tmp_path / "gate0.status").exists()


def test_remote_testing_manual_names_the_container_exception_and_boundaries():
    manual = REPO / "docs/remote-testing.md"
    if not manual.exists():
        pytest.skip("private remote-testing manual absent from public mirror")
    text = manual.read_text()
    assert "Local Linux multi-interpreter advisory gate" in text
    assert re.search(r"Python 3\.11, 3\.12,\s+and 3\.13", text)
    assert "non-root" in text
    assert "CCTALLY_AGENTMEM_TEST_POLICY=hosted-private-unavailable" in text
    # #630 S5 replaced the private hosted lane and the release-blocking framing.
    # The manual must state what took their place, or the retirement would read
    # as prose merely deleted.
    assert "advisory" in text
    assert "projected public tree" in text.lower()
    assert "release-records" in text
    # The image the lanes run is provisioned automatically rather than by an
    # added operator step, and the manual records the measurement that decided
    # the lane schedule rather than only asserting the schedule it landed on.
    assert "content-addressed" in text
    assert "freshness epoch" in text
    assert "--acceptance" in text
    assert "one at a time" in text
    assert "falsified" in text
    assert "The measured acceptance runs" in text


@pytest.mark.parametrize(
    "lane_exits,expected",
    [
        ({"3.11": 0, "3.12": 0, "3.13": 0}, 0),
        ({"3.11": 1, "3.12": 0, "3.13": 0}, 1),
        ({"3.11": 1, "3.12": 1, "3.13": 1}, 1),
        ({"3.11": 3, "3.12": 0, "3.13": 0}, 3),
        # Infrastructure outranks product failure: an incomplete lane makes the
        # product verdict unknowable, so 1 must not win over 3.
        ({"3.11": 1, "3.12": 0, "3.13": 3}, 3),
        ({"3.11": 2, "3.12": 1, "3.13": 0}, 3),
        ({"3.11": 137, "3.12": 0, "3.13": 0}, 3),
    ],
)
def test_merge_lane_exit_codes(lane_exits, expected):
    gate = _load_gate()
    assert gate._merge_lane_exit_codes(lane_exits) == expected


def test_merge_lane_exit_codes_rejects_an_empty_result_set():
    gate = _load_gate()
    with pytest.raises(ValueError):
        gate._merge_lane_exit_codes({})


@pytest.mark.parametrize(
    "merged,postcondition_failed,diagnostic,expected",
    [
        (0, False, False, 0),
        (1, False, False, 1),
        (3, False, False, 3),
        # A candidate that changed underneath the matrix invalidates every lane,
        # so the precondition refusal dominates whatever the lanes reported.
        (0, True, False, 2),
        (1, True, False, 2),
        (3, True, False, 2),
        # A focused selection can never discharge the gate, even all-green.
        (0, False, True, 3),
        (1, False, True, 3),
        (0, True, True, 2),
    ],
)
def test_compose_gate_exit(merged, postcondition_failed, diagnostic, expected):
    gate = _load_gate()
    assert gate._compose_gate_exit(merged, postcondition_failed, diagnostic) == expected


def test_image_input_digest_is_deterministic():
    gate = _load_gate()
    records = [("schema", "1"), ("python", "3.12"), ("dockerfile", "abc")]
    assert gate._image_input_digest(records) == gate._image_input_digest(list(records))


def test_image_input_digest_changes_when_any_single_record_changes():
    gate = _load_gate()
    base = [
        ("schema", "1"),
        ("python", "3.12"),
        ("dockerfile", "abc"),
        ("epoch", "2026-W34"),
    ]
    baseline = gate._image_input_digest(base)
    for index in range(len(base)):
        mutated = list(base)
        name, value = mutated[index]
        mutated[index] = (name, value + "x")
        assert gate._image_input_digest(mutated) != baseline, (
            f"record {name} does not affect the digest"
        )


def test_image_input_digest_frames_records_against_concatenation_collisions():
    gate = _load_gate()
    # Without framing, ("a", "bc") + ("d", "e") and ("a", "b") + ("cd", "e")
    # would serialize to the same bytes.
    left = gate._image_input_digest([("a", "bc"), ("d", "e")])
    right = gate._image_input_digest([("a", "b"), ("cd", "e")])
    assert left != right


def test_image_input_digest_is_order_sensitive_or_normalized():
    gate = _load_gate()
    forward = gate._image_input_digest([("a", "1"), ("b", "2")])
    reverse = gate._image_input_digest([("b", "2"), ("a", "1")])
    assert forward == reverse, (
        "records must be normalized by name so ordering cannot change the tag"
    )


def test_freshness_epoch_is_a_weekly_iso_year_week():
    gate = _load_gate()
    moment = datetime.datetime(2026, 8, 18, 12, 0, tzinfo=datetime.timezone.utc)
    assert gate._freshness_epoch(moment) == "2026-W34"


def test_freshness_epoch_is_stable_within_a_week_and_changes_across_one():
    gate = _load_gate()
    monday = datetime.datetime(2026, 8, 17, 0, 0, tzinfo=datetime.timezone.utc)
    sunday = datetime.datetime(2026, 8, 23, 23, 59, tzinfo=datetime.timezone.utc)
    next_monday = datetime.datetime(2026, 8, 24, 0, 0, tzinfo=datetime.timezone.utc)
    assert gate._freshness_epoch(monday) == gate._freshness_epoch(sunday)
    assert gate._freshness_epoch(next_monday) != gate._freshness_epoch(monday)


def test_base_images_are_digest_pinned_for_every_supported_python():
    gate = _load_gate()
    assert set(gate.PYTHON_BASE_IMAGES) == set(gate.PYTHON_VERSIONS)
    for version, reference in gate.PYTHON_BASE_IMAGES.items():
        assert reference.startswith(f"python:{version}-{gate.CONTAINER_DISTRO}@sha256:")
        assert len(reference.rsplit("sha256:", 1)[1]) == 64


def test_image_records_cover_every_input_that_can_change_the_image():
    gate = _load_gate()
    records = dict(gate._image_records("3.12", "2026-W34", gate._repo_root()))
    for required in (
        "schema", "python", "platform", "distro", "base", "dockerfile",
        "requirements", "package-json", "package-lock", "nvmrc",
        "node-archive-sha256", "apt-packages", "sqlite-url", "sqlite-version",
        "sqlite-sha256", "sqlite-flags", "uid", "gid", "checkout", "apt-epoch",
    ):
        assert required in records, f"missing image input record: {required}"
    assert records["apt-epoch"] == "2026-W34"
    assert records["python"] == "3.12"


def test_image_records_change_when_the_dockerfile_changes():
    gate = _load_gate()
    root = gate._repo_root()
    before = gate._image_input_digest(gate._image_records("3.12", "2026-W34", root))
    dockerfile = root / "bin" / "cctally-test-linux-matrix.Dockerfile"
    original = dockerfile.read_bytes()
    try:
        dockerfile.write_bytes(original + b"\n# provoke a digest change\n")
        after = gate._image_input_digest(gate._image_records("3.12", "2026-W34", root))
    finally:
        dockerfile.write_bytes(original)
    assert before != after


def test_the_freshness_epoch_is_part_of_the_image_key():
    gate = _load_gate()
    root = gate._repo_root()
    this_week = gate._image_input_digest(gate._image_records("3.12", "2026-W34", root))
    next_week = gate._image_input_digest(gate._image_records("3.12", "2026-W35", root))
    assert this_week != next_week
    assert gate._image_tag("3.12", this_week) != gate._image_tag("3.12", next_week)


def _fake_inspect(payload):
    def _inspect(engine, reference):
        return payload
    return _inspect


def test_resolve_image_returns_the_immutable_id_not_the_tag(monkeypatch):
    gate = _load_gate()
    root = gate._repo_root()
    digest = gate._image_input_digest(gate._image_records("3.12", "2026-W34", root))
    image_id = "sha256:" + "b" * 64
    monkeypatch.setattr(
        gate,
        "_inspect_image",
        _fake_inspect(
            (
                image_id,
                {
                    gate.IMAGE_INPUTS_LABEL: digest,
                    gate.IMAGE_EPOCH_LABEL: "2026-W34",
                    gate.IMAGE_TOOLCHAIN_LABEL: "c" * 64,
                },
            )
        ),
    )

    def _never(*args, **kwargs):
        raise AssertionError("a present, validated image must not be rebuilt")

    monkeypatch.setattr(gate, "_build_image", _never)
    assert gate._resolve_image("docker", "3.12", "2026-W34", root) == image_id


def test_resolve_image_refuses_an_image_whose_input_label_disagrees(monkeypatch):
    gate = _load_gate()
    root = gate._repo_root()
    monkeypatch.setattr(
        gate,
        "_inspect_image",
        _fake_inspect(
            (
                "sha256:" + "b" * 64,
                {
                    gate.IMAGE_INPUTS_LABEL: "d" * 64,
                    gate.IMAGE_EPOCH_LABEL: "2026-W34",
                    gate.IMAGE_TOOLCHAIN_LABEL: "c" * 64,
                },
            )
        ),
    )
    monkeypatch.setattr(gate, "_build_image", lambda *a, **k: None)
    with pytest.raises(gate.GateError):
        gate._resolve_image("docker", "3.12", "2026-W34", root)


def test_resolve_image_refuses_an_epoch_outside_the_bound(monkeypatch):
    gate = _load_gate()
    root = gate._repo_root()
    digest = gate._image_input_digest(gate._image_records("3.12", "2026-W34", root))
    monkeypatch.setattr(
        gate,
        "_inspect_image",
        _fake_inspect(
            (
                "sha256:" + "b" * 64,
                {
                    gate.IMAGE_INPUTS_LABEL: digest,
                    gate.IMAGE_EPOCH_LABEL: "2020-W01",
                    gate.IMAGE_TOOLCHAIN_LABEL: "c" * 64,
                },
            )
        ),
    )
    monkeypatch.setattr(gate, "_build_image", lambda *a, **k: None)
    with pytest.raises(gate.GateError):
        gate._resolve_image("docker", "3.12", "2026-W34", root)


def test_resolve_image_builds_exactly_once_when_the_derived_tag_is_missing(monkeypatch):
    gate = _load_gate()
    root = gate._repo_root()
    digest = gate._image_input_digest(gate._image_records("3.12", "2026-W34", root))
    image_id = "sha256:" + "b" * 64
    states = iter(
        [
            None,
            (
                image_id,
                {
                    gate.IMAGE_INPUTS_LABEL: digest,
                    gate.IMAGE_EPOCH_LABEL: "2026-W34",
                    gate.IMAGE_TOOLCHAIN_LABEL: "c" * 64,
                },
            ),
        ]
    )
    monkeypatch.setattr(gate, "_inspect_image", lambda engine, ref: next(states))
    builds: list[str] = []
    monkeypatch.setattr(
        gate,
        "_build_image",
        lambda engine, version, epoch, root_, digest_, tag: builds.append(tag),
    )
    assert gate._resolve_image("docker", "3.12", "2026-W34", root) == image_id
    assert builds == [gate._image_tag("3.12", digest)]


def test_the_gate_and_its_dockerfile_are_both_public_on_the_mirror():
    allowlist = REPO / ".mirror-allowlist"
    # One path string rather than a `/ ".githooks" / ...` join: the join
    # creates an intermediate node naming the private directory itself, which
    # the public/private dependency gate reads as an ungated reference.
    matcher = REPO / ".githooks/_match.py"
    if not allowlist.exists() or not matcher.exists():
        pytest.skip("private mirror tooling absent from the public mirror")
    paths = [
        "bin/cctally-test-linux-matrix",
        "bin/cctally-test-linux-matrix.Dockerfile",
        "bin/_lib-linux-matrix-manifest.sh",
        "bin/cctally-test-linux-matrix-sampler.py",
        "bin/cctally-test-linux-matrix-acceptance.py",
    ]
    proc = subprocess.run(
        [sys.executable, str(matcher), str(allowlist)],
        input="\n".join(paths) + "\n",
        capture_output=True, text=True, cwd=REPO, check=True,
    )
    classified = dict(line.split("\t") for line in proc.stdout.splitlines())
    for path in paths:
        assert classified[path] == "public", (
            f"{path} must stay public: tests/test_linux_matrix_gate.py is public via "
            "`tests/**`, so a private Dockerfile would fail collection on the mirror "
            "and turn the public CI red"
        )


def _load_projector():
    """Import the PRIVATE public-tree projector, or skip.

    Existence-gated rather than imported at module scope: this test module is
    public via `tests/**`, and `bin/_cctally_public_projection.py` is private
    because it imports the maintainer-only `bin/cctally-mirror-public`. A
    module-scope import would fail public pytest COLLECTION, which is the
    dependency class `tests/test_public_test_dep_closure.py` guards (#630 S5).
    """
    module_path = REPO / "bin/_cctally_public_projection.py"
    mirror = REPO / "bin/cctally-mirror-public"
    if not module_path.is_file() or not mirror.is_file():
        pytest.skip("the public-tree projector is maintainer-only")
    loader = importlib.machinery.SourceFileLoader(
        "_cctally_public_projection_under_test", str(module_path)
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


def test_the_projection_is_a_root_commit_with_no_private_ancestry(tmp_path):
    """A clone-and-delete projection keeps the private HEAD as its parent and
    the private object database, and the container copies `.git` wholesale, so
    `git show HEAD^:.mirror-allowlist` would recover a private file from a tree
    that claims to be the public subset (#630 S5).
    """
    projector = _load_projector()
    dest = tmp_path / "public"
    sha = projector.project_public_tree("HEAD", REPO, dest)

    def git(*args):
        return subprocess.run(("git", "-C", str(dest)) + args,
                              capture_output=True, text=True, check=True).stdout

    assert git("rev-parse", "HEAD").strip() == sha
    assert git("rev-list", "--count", "HEAD").strip() == "1"
    assert git("status", "--porcelain", "--untracked-files=all") == ""

    tree = set(git("ls-tree", "-r", "--name-only", "HEAD").split())
    # Membership in the COMMITTED tree rather than `(dest / …).exists()`. It is
    # the stronger statement — a file could be absent from the worktree and
    # still recoverable from the tree — and it also keeps this public module
    # from spelling a mirror-private path as an ungated filesystem probe, which
    # `tests/test_public_test_dep_closure.py` refuses on sight.
    assert ".mirror-allowlist" not in tree
    # The marker below must sit inside the statement's own source span, which
    # is why it is a trailing comment: it names a private path in order to
    # assert its ABSENCE from a temporary projection, so it is not a dependency
    # on the file and still holds on a clone that does not carry it.
    assert "bin/cctally-test-remote" not in tree  # mirror-private-ok
    assert "bin/cctally-test-all" in tree
    # git's leaf semantics, not `is_file()`'s. `is_file()` follows the link, so
    # a DANGLING symlink answers False and drops out of the comparison —
    # tests/fixtures/setup carries one, pointing at /opt/cctally-prior. A
    # symlink to a directory is likewise one leaf to git and is not descended.
    disk = {
        str(p.relative_to(dest)) for p in dest.rglob("*")
        if p.relative_to(dest).parts[0] != ".git"
        and (p.is_symlink() or p.is_file())
    }
    assert tree == disk


def test_the_projection_retains_no_ref_that_could_recover_a_private_blob(
    tmp_path,
):
    """The reason the clone-and-delete form was rejected, asserted directly.
    A clone keeps the private object database reachable from its refs, so the
    projection must carry exactly one ref and no remote, tag or stash."""
    projector = _load_projector()
    dest = tmp_path / "public"
    projector.project_public_tree("HEAD", REPO, dest)

    def git(*args):
        return subprocess.run(("git", "-C", str(dest)) + args,
                              capture_output=True, text=True, check=True).stdout

    refs = git("for-each-ref", "--format=%(refname)").split()
    assert len(refs) == 1, refs
    assert git("rev-list", "--parents", "-n", "1", "HEAD").split() == [
        git("rev-parse", "HEAD").strip()
    ]
    # The private allowlist is not merely absent from the worktree — it is
    # unreachable from any object this repository holds.
    recovered = subprocess.run(
        ("git", "-C", str(dest), "cat-file", "-e", "HEAD^{tree}:.mirror-allowlist"),
        capture_output=True, text=True,
    )
    assert recovered.returncode != 0, recovered.stdout


def test_the_projection_refuses_a_destination_that_already_holds_files(tmp_path):
    """This is the privacy boundary, so it fails loudly rather than merging
    into whatever was already there."""
    projector = _load_projector()
    dest = tmp_path / "public"
    dest.mkdir()
    (dest / "leftover").write_text("x")

    with pytest.raises(projector.ProjectionError):
        projector.project_public_tree("HEAD", REPO, dest)


class _FakeLaneProcess:
    """A container lane that produces its output and then terminates.

    `stdout` is a real readable stream because the driver tees each lane through
    it: the operator watches the suite live and the file keeps the deterministic
    footer.
    """

    def __init__(self, version, status, started, observed):
        self.version = version
        self.returncode = None
        self.stdout = io.BytesIO(f"lane output for {version}\n".encode())
        self._status = status
        self._started = started
        self._observed = observed

    def poll(self):
        self.returncode = self._status
        return self._status

    def wait(self, timeout=None):
        self._observed.append(len(self._started))
        self.returncode = self._status
        return self._status


def _fake_popen_factory(gate, started, observed, statuses):
    def _fake_popen(command, **kwargs):
        rendered = " ".join(str(part) for part in command)
        version = next(
            candidate
            for candidate in gate.PYTHON_VERSIONS
            if f"CCTALLY_TEST_MATRIX_ID={candidate}" in rendered
        )
        started.append(version)
        return _FakeLaneProcess(version, statuses.get(version, 0), started, observed)

    return _fake_popen


def test_each_lane_is_released_before_the_next_one_starts(monkeypatch, tmp_path):
    """Three-way concurrency was measured and falsified — two concurrent runs
    produced load-sensitive dashboard and config HTTP failures that two
    single-lane runs over the same images did not — so the lanes run one at a
    time. A concurrent driver would have started every lane before the first was
    waited on and observed three, not one."""
    gate = _load_gate()
    started: list[str] = []
    observed: list[int] = []
    # #630 S2: patch the IMPORTER's reference, never the shared
    # stdlib module object, which every other importer and every
    # concurrent thread resolves through.
    _iso_subprocess = types.SimpleNamespace(**vars(gate.subprocess))
    _iso_subprocess.Popen = _fake_popen_factory(gate, started, observed, {})
    monkeypatch.setattr(gate, "subprocess", _iso_subprocess)

    exits = gate._run_lanes(
        "docker",
        gate.PYTHON_VERSIONS,
        {version: f"sha256:{version}" for version in gate.PYTHON_VERSIONS},
        tmp_path,
        "d" * 40,
        tmp_path,
    )

    assert exits == {version: 0 for version in gate.PYTHON_VERSIONS}
    assert started == list(gate.PYTHON_VERSIONS)
    assert observed == [1, 2, 3]


def test_one_failing_lane_does_not_prevent_the_others_completing(
    monkeypatch, tmp_path
):
    """The accidental fail-fast returned inside the loop on the first failing
    lane, discarding whatever the other two would have reported. Sequential
    execution is not a licence to reintroduce it: 3.11 fails first here, and
    3.12 and 3.13 must still run and still be reported."""
    gate = _load_gate()
    started: list[str] = []
    observed: list[int] = []
    # #630 S2: patch the IMPORTER's reference, never the shared
    # stdlib module object, which every other importer and every
    # concurrent thread resolves through.
    _iso_subprocess = types.SimpleNamespace(**vars(gate.subprocess))
    _iso_subprocess.Popen = _fake_popen_factory(gate, started, observed, {"3.11": 1})
    monkeypatch.setattr(gate, "subprocess", _iso_subprocess)

    exits = gate._run_lanes(
        "docker",
        gate.PYTHON_VERSIONS,
        {version: f"sha256:{version}" for version in gate.PYTHON_VERSIONS},
        tmp_path,
        "d" * 40,
        tmp_path,
    )

    assert exits == {"3.11": 1, "3.12": 0, "3.13": 0}
    assert started == list(gate.PYTHON_VERSIONS)


def test_completion_order_does_not_reorder_rendered_blocks(tmp_path, capsys):
    """The footer renders in PYTHON_VERSIONS order whatever order the exit codes
    arrived in, because the output is release evidence and must read the same way
    on every cut.

    The sequential driver cannot itself finish 3.13 first, so this pins
    `_render_lane_blocks` directly with a lane-exit mapping in the opposite
    order rather than staging a run.
    """
    gate = _load_gate()
    for version in gate.PYTHON_VERSIONS:
        (tmp_path / f"{version}.log").write_text(f"body of lane {version}\n")
    lane_exits = {"3.13": 0, "3.12": 0, "3.11": 0}

    gate._render_lane_blocks(gate.PYTHON_VERSIONS, tmp_path, lane_exits)

    out = capsys.readouterr().out
    positions = [out.index(f"body of lane {v}") for v in gate.PYTHON_VERSIONS]
    assert positions == sorted(positions)


def test_every_missing_image_is_built_before_any_lane_starts(monkeypatch, tmp_path):
    """Phase 2 is serialized ahead of phase 3: three concurrent docker builds
    would contend for the daemon and the build cache, and a lane must never run
    against an unverified image."""
    gate = _load_gate()
    events: list[tuple] = []
    _stub_successful_matrix(monkeypatch, gate, tmp_path, record=events)
    monkeypatch.setattr(gate, "_git_head", lambda root: "a" * 40)
    monkeypatch.setattr(gate, "_git_status", lambda root: "")

    assert gate.main([]) == 0
    assert [kind for kind, _ in events] == ["resolve", "resolve", "resolve", "lanes"]


def test_postconditions_run_even_when_a_lane_failed(monkeypatch, tmp_path, capsys):
    """The old early return skipped the HEAD and clean-tree rechecks entirely,
    so a candidate that changed underneath a failing matrix went unreported."""
    gate = _load_gate()
    _stub_successful_matrix(
        monkeypatch, gate, tmp_path, lane_exits={"3.11": 1, "3.12": 0, "3.13": 0}
    )
    heads = iter(("a" * 40, "b" * 40))
    monkeypatch.setattr(gate, "_git_head", lambda root: next(heads))
    monkeypatch.setattr(gate, "_git_status", lambda root: "")

    assert gate.main([]) == 2
    captured = capsys.readouterr()
    assert "HEAD changed during matrix" in captured.err
    assert "PASS" not in captured.out


def test_a_failing_lane_still_reports_every_lane(monkeypatch, tmp_path, capsys):
    gate = _load_gate()
    _stub_successful_matrix(
        monkeypatch, gate, tmp_path, lane_exits={"3.11": 1, "3.12": 0, "3.13": 0}
    )
    monkeypatch.setattr(gate, "_git_head", lambda root: "a" * 40)
    monkeypatch.setattr(gate, "_git_status", lambda root: "")

    assert gate.main([]) == 1
    out = capsys.readouterr().out
    for version in gate.PYTHON_VERSIONS:
        assert f"linux-matrix stub lane {version}" in out
    assert "PASS" not in out


def test_an_infrastructure_lane_outranks_a_product_lane(monkeypatch, tmp_path):
    gate = _load_gate()
    _stub_successful_matrix(
        monkeypatch, gate, tmp_path, lane_exits={"3.11": 1, "3.12": 0, "3.13": 3}
    )
    monkeypatch.setattr(gate, "_git_head", lambda root: "a" * 40)
    monkeypatch.setattr(gate, "_git_status", lambda root: "")

    assert gate.main([]) == 3


def test_the_pass_line_names_the_image_input_and_toolchain_evidence(
    monkeypatch, tmp_path, capsys
):
    gate = _load_gate()
    _stub_successful_matrix(monkeypatch, gate, tmp_path)
    monkeypatch.setattr(gate, "_git_head", lambda root: "a" * 40)
    monkeypatch.setattr(gate, "_git_status", lambda root: "")
    monkeypatch.setattr(
        gate,
        "_inspect_image",
        lambda engine, reference: (
            reference,
            {
                gate.IMAGE_INPUTS_LABEL: "inputs-digest",
                gate.IMAGE_EPOCH_LABEL: "2026-W34",
                gate.IMAGE_TOOLCHAIN_LABEL: "toolchain-digest",
            },
        ),
    )

    assert gate.main([]) == 0
    out = capsys.readouterr().out
    for version in gate.PYTHON_VERSIONS:
        assert (
            f"Python {version} PASS image=sha256:{version} inputs=inputs-digest "
            "toolchain=toolchain-digest aptEpoch=2026-W34" in out
        )


def test_an_ordinary_run_is_byte_identical_to_the_pre_acceptance_behaviour(tmp_path):
    """Acceptance mode changes the container lifecycle. An ordinary gate run
    must not inherit any of it, or the gate would no longer be the thing the
    acceptance run measured."""
    gate = _load_gate()
    image = "sha256:" + "a" * 64
    default = gate._container_command("docker", "3.11", tmp_path, image)
    explicit = gate._container_command(
        "docker", "3.11", tmp_path, image, acceptance=None
    )
    assert default == explicit
    rendered = " ".join(default)
    assert "--rm" in rendered
    assert "--cidfile" not in rendered
    assert "/metrics" not in rendered
    body = default[-1]
    assert "PYTEST_ADDOPTS" not in body
    assert "sampler" not in body


def test_acceptance_mode_retains_the_container_and_mounts_the_metrics_directory(
    tmp_path,
):
    """`docker run --rm` deletes the container the moment it exits, after which
    `docker inspect` can recover neither its exit code nor its OOM state."""
    gate = _load_gate()
    image = "sha256:" + "a" * 64
    cidfile = tmp_path / "3.11.cid"
    metrics = tmp_path / "metrics"
    command = gate._container_command(
        "docker",
        "3.11",
        tmp_path,
        image,
        acceptance={"cidfile": cidfile, "metrics": metrics},
    )
    rendered = " ".join(str(part) for part in command)
    assert "--rm" not in command
    assert f"--cidfile {cidfile}" in rendered
    assert f"{metrics}:/metrics" in rendered
    body = command[-1]
    # --durations=0 has no route into the hard-coded pytest invocation in
    # bin/cctally-test-all, so acceptance mode injects it through PYTEST_ADDOPTS
    # rather than editing the aggregator.
    assert "PYTEST_ADDOPTS=--durations=0" in body
    assert gate.SAMPLER_CONTAINER_PATH in body


def test_acceptance_mode_removes_every_retained_container_even_after_a_failure(
    monkeypatch, tmp_path
):
    gate = _load_gate()
    removed: list[str] = []

    def _fake_run(command, **kwargs):
        if len(command) >= 2 and command[1] == "rm":
            removed.extend(command[2:])
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(gate, "_run", _fake_run)
    gate._remove_containers("docker", {"3.11": "cid-a", "3.13": "cid-c"})
    assert sorted(removed) == ["cid-a", "cid-c"]


def test_acceptance_cleanup_runs_when_the_lanes_raise(monkeypatch, tmp_path):
    gate = _load_gate()
    events: list[str] = []
    _stub_successful_matrix(monkeypatch, gate, tmp_path)
    monkeypatch.setattr(gate, "_git_head", lambda root: "a" * 40)
    monkeypatch.setattr(gate, "_git_status", lambda root: "")

    def _boom(*args, **kwargs):
        raise gate.GateError("lane launch failed")

    monkeypatch.setattr(gate, "_run_lanes", _boom)
    monkeypatch.setattr(
        gate,
        "_remove_containers",
        lambda engine, cids: events.append("cleanup"),
    )
    assert gate.main(["--acceptance"]) == 3
    assert events == ["cleanup"]


def test_acceptance_mode_reports_threshold_violations_and_refuses(
    monkeypatch, tmp_path, capsys
):
    gate = _load_gate()
    _stub_successful_matrix(monkeypatch, gate, tmp_path)
    monkeypatch.setattr(gate, "_git_head", lambda root: "a" * 40)
    monkeypatch.setattr(gate, "_git_status", lambda root: "")
    monkeypatch.setattr(gate, "_remove_containers", lambda engine, cids: None)
    monkeypatch.setattr(
        gate,
        "_collect_acceptance_samples",
        lambda *args, **kwargs: {
            "lanes": {},
            "imageBuildSeconds": 0.0,
            "pytestDurations": [],
        },
    )
    assert gate.main(["--acceptance"]) == 3
    captured = capsys.readouterr()
    assert "acceptance" in captured.err.lower()


def test_lane_output_reaches_stdout_while_the_lane_runs(monkeypatch, tmp_path, capsys):
    """On `main` each lane inherited stdout and the operator watched the suite
    live; redirecting to a file printed nothing until the lane had exited.

    The file layer exists for the deterministic footer, and with one lane
    running at a time it can be a tee rather than a replacement. Nothing here
    calls `_render_lane_blocks`, so any lane text on stdout came from the lane
    itself rather than from the footer.
    """
    gate = _load_gate()
    started: list[str] = []
    observed: list[int] = []
    # #630 S2: patch the IMPORTER's reference, never the shared
    # stdlib module object, which every other importer and every
    # concurrent thread resolves through.
    _iso_subprocess = types.SimpleNamespace(**vars(gate.subprocess))
    _iso_subprocess.Popen = _fake_popen_factory(gate, started, observed, {})
    monkeypatch.setattr(gate, "subprocess", _iso_subprocess)

    gate._run_lanes(
        "docker",
        gate.PYTHON_VERSIONS,
        {version: f"sha256:{version}" for version in gate.PYTHON_VERSIONS},
        tmp_path,
        "d" * 40,
        tmp_path,
    )

    out = capsys.readouterr().out
    for version in gate.PYTHON_VERSIONS:
        assert f"lane output for {version}" in out
        # The same bytes still reach the file, or the footer would lose them.
        assert (tmp_path / f"{version}.log").read_text() == (
            f"lane output for {version}\n"
        )


def _lane_state(started, finished, exit_code=0):
    return {
        "startedAt": started,
        "finishedAt": finished,
        "exitCode": exit_code,
        "oomKilled": False,
        "samples": [],
    }


@pytest.mark.parametrize(
    "intervals, expected",
    [
        ({"3.11": (0.0, 10.0), "3.12": (11.0, 20.0), "3.13": (21.0, 30.0)}, "sequential"),
        ({"3.11": (0.0, 10.0), "3.12": (10.0, 20.0)}, "sequential"),
        ({"3.11": (0.0, 10.0), "3.12": (9.5, 20.0), "3.13": (21.0, 30.0)}, "concurrent"),
        ({"3.11": (0.0, 30.0), "3.12": (1.0, 5.0), "3.13": (6.0, 9.0)}, "concurrent"),
        ({"3.11": (0.0, 10.0)}, None),
        ({}, None),
    ],
)
def test_the_lane_schedule_is_derived_from_measured_lane_overlap(intervals, expected):
    """The schedule the acceptance oracle applies must come from what the lanes
    did, not from a constant a future concurrency re-attempt can forget to
    change. Two lanes whose container intervals intersect ran concurrently; two
    that do not ran one at a time. Fewer than two measured intervals observe no
    schedule at all, which is None rather than a guess."""
    gate = _load_gate()
    lanes = {
        version: _lane_state(started, finished)
        for version, (started, finished) in intervals.items()
    }
    assert gate._derive_lane_schedule(lanes) == expected


def test_a_lane_missing_its_instants_is_excluded_from_the_derivation():
    """A lane whose container state could not be read contributes no interval.
    Treating its absent instants as zero would place it against every other lane
    and report concurrency the run never had."""
    gate = _load_gate()
    lanes = {
        "3.11": _lane_state(0.0, 10.0),
        "3.12": _lane_state(None, None),
        "3.13": _lane_state(11.0, 20.0),
    }
    assert gate._derive_lane_schedule(lanes) == "sequential"


def test_the_collector_records_the_schedule_it_measured(monkeypatch, tmp_path):
    """The derivation is part of collection, so the artifact carries a measured
    schedule beside the samples it was measured from."""
    gate = _load_gate()
    monkeypatch.setattr(
        gate,
        "_container_state",
        lambda engine, cid: {
            "startedAt": 0.0 if cid == "cid-3.11" else 50.0,
            "finishedAt": 100.0 if cid == "cid-3.11" else 150.0,
            "exitCode": 0,
            "oomKilled": False,
        },
    )
    (tmp_path / "3.11.log").write_text("")
    (tmp_path / "3.12.log").write_text("")
    collected = gate._collect_acceptance_samples(
        "docker",
        ("3.11", "3.12"),
        {"3.11": "cid-3.11", "3.12": "cid-3.12"},
        tmp_path,
        tmp_path,
        {"3.11": 0, "3.12": 0},
        0.0,
    )
    assert collected["schedule"] == "concurrent"


def test_the_gate_refuses_when_the_lanes_did_not_run_the_declared_schedule(
    monkeypatch, tmp_path, capsys
):
    """`LANE_SCHEDULE` selects which threshold set applies, and the three
    concurrency-only checks plus a phase ceiling three times tighter hang off
    it. A concurrency re-attempt that rewrites `_run_lanes` and leaves the
    constant alone would evaluate concurrent lanes under sequential thresholds
    and skip exactly the checks that police concurrency, silently. The gate
    therefore refuses rather than reporting a verdict it cannot support."""
    gate = _load_gate()
    _stub_successful_matrix(monkeypatch, gate, tmp_path)
    monkeypatch.setattr(gate, "_git_head", lambda root: "a" * 40)
    monkeypatch.setattr(gate, "_git_status", lambda root: "")
    monkeypatch.setattr(gate, "_remove_containers", lambda engine, cids: None)
    monkeypatch.setattr(
        gate,
        "_collect_acceptance_samples",
        lambda *args, **kwargs: {
            "lanes": {
                "3.11": _lane_state(0.0, 100.0),
                "3.12": _lane_state(50.0, 150.0),
            },
            "imageBuildSeconds": 0.0,
            "pytestDurations": [],
        },
    )
    destination = tmp_path / "verdict.json"

    assert gate.main(["--acceptance", "--acceptance-output", str(destination)]) == 3
    captured = capsys.readouterr()
    assert "concurrent" in captured.err
    assert "sequential" in captured.err
    assert not destination.exists(), (
        "an artifact written under the wrong threshold set is worse than none"
    )


def test_the_gate_evaluates_acceptance_under_the_schedule_it_actually_ran(
    monkeypatch, tmp_path
):
    """The analyzer's concurrent thresholds — a five-second lane-start spread
    and a one-lane phase ceiling — refuse a healthy sequential run, so the
    driver states the schedule it ran rather than inheriting a default. The
    lanes below do not overlap, so the declared schedule survives the
    derivation check above and reaches the analyzer."""
    gate = _load_gate()
    _stub_successful_matrix(monkeypatch, gate, tmp_path)
    monkeypatch.setattr(gate, "_git_head", lambda root: "a" * 40)
    monkeypatch.setattr(gate, "_git_status", lambda root: "")
    monkeypatch.setattr(gate, "_remove_containers", lambda engine, cids: None)
    monkeypatch.setattr(
        gate,
        "_collect_acceptance_samples",
        lambda *args, **kwargs: {
            "lanes": {
                "3.11": _lane_state(0.0, 100.0),
                "3.12": _lane_state(101.0, 200.0),
            },
            "imageBuildSeconds": 0.0,
            "pytestDurations": [],
        },
    )
    real = gate._acceptance_module()
    seen: list[str] = []

    class _Recorder:
        def evaluate_acceptance(self, samples, *, schedule):
            seen.append(schedule)
            return real.evaluate_acceptance(samples, schedule=schedule)

        def __getattr__(self, name):
            return getattr(real, name)

    monkeypatch.setattr(gate, "_acceptance_module", lambda: _Recorder())
    gate.main(["--acceptance", "--acceptance-output", str(tmp_path / "verdict.json")])

    assert gate.LANE_SCHEDULE == "sequential"
    assert seen == ["sequential"]


def test_a_sigterm_unwinds_the_cleanup_block_instead_of_leaking_containers(
    monkeypatch, tmp_path
):
    """SIGTERM's default disposition terminates the process without running
    `finally`, so an acceptance run interrupted anywhere but the release runbook
    — which is the only thing that reaps lane containers by label — leaves every
    retained container behind."""
    gate = _load_gate()
    _stub_successful_matrix(monkeypatch, gate, tmp_path)
    monkeypatch.setattr(gate, "_git_head", lambda root: "a" * 40)
    monkeypatch.setattr(gate, "_git_status", lambda root: "")
    events: list[str] = []
    monkeypatch.setattr(
        gate, "_remove_containers", lambda engine, cids: events.append("cleanup")
    )
    before = signal.getsignal(signal.SIGTERM)

    def _deliver(*args, **kwargs):
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler), "the gate installed no SIGTERM handler"
        handler(signal.SIGTERM, None)

    monkeypatch.setattr(gate, "_run_lanes", _deliver)

    with pytest.raises(SystemExit) as raised:
        gate.main(["--acceptance"])

    assert raised.value.code == 143
    assert events == ["cleanup"]
    # The previous disposition is restored, so running the gate in-process does
    # not change the signal behaviour of whatever called it.
    assert signal.getsignal(signal.SIGTERM) is before


def test_the_sigterm_disposition_is_restored_before_the_cleanup_removes_containers(
    monkeypatch, tmp_path
):
    """A second SIGTERM arriving while `_remove_containers` runs would raise out
    of the cleanup block, truncating the removal AND skipping the restore that
    follows it. Restoring the previous disposition first bounds the damage to
    the containers the first signal already left, and it is the operator's own
    second interrupt that then terminates the process."""
    gate = _load_gate()
    _stub_successful_matrix(monkeypatch, gate, tmp_path)
    monkeypatch.setattr(gate, "_git_head", lambda root: "a" * 40)
    monkeypatch.setattr(gate, "_git_status", lambda root: "")
    before = signal.getsignal(signal.SIGTERM)
    seen: dict[str, object] = {}

    def _remove(engine, container_ids):
        seen["handler"] = signal.getsignal(signal.SIGTERM)

    monkeypatch.setattr(gate, "_remove_containers", _remove)
    monkeypatch.setattr(
        gate,
        "_collect_acceptance_samples",
        lambda *args, **kwargs: {
            "lanes": {},
            "imageBuildSeconds": 0.0,
            "pytestDurations": [],
        },
    )

    gate.main(["--acceptance", "--acceptance-output", str(tmp_path / "verdict.json")])

    assert seen["handler"] is before
    assert signal.getsignal(signal.SIGTERM) is before


def test_image_build_seconds_excludes_the_image_store_prune(monkeypatch, tmp_path):
    """`imageBuildSeconds` is release evidence for what a rebuild costs. Garbage
    collection runs on every cut and builds on almost none, so charging the
    prune to the build reports a cost no cut actually pays."""
    gate = _load_gate()
    _stub_successful_matrix(monkeypatch, gate, tmp_path)
    monkeypatch.setattr(gate, "_git_head", lambda root: "a" * 40)
    monkeypatch.setattr(gate, "_git_status", lambda root: "")
    monkeypatch.setattr(gate, "_remove_containers", lambda engine, cids: None)
    clock = {"now": 0.0}

    def _advance(seconds):
        clock["now"] += seconds

    class _Clock:
        @staticmethod
        def monotonic():
            return clock["now"]

    monkeypatch.setattr(gate, "time", _Clock)

    def _resolve(engine, version, epoch, root):
        _advance(10.0)
        return f"sha256:{version}"

    monkeypatch.setattr(gate, "_resolve_image", _resolve)
    monkeypatch.setattr(
        gate, "_prune_stale_images", lambda engine, version, keep: _advance(100.0)
    )
    monkeypatch.setattr(
        gate, "_prune_first_pass_images", lambda engine: _advance(100.0)
    )
    seen: dict[str, float] = {}

    def _collect(*args, **kwargs):
        seen["seconds"] = args[6]
        return {
            "lanes": {
                "3.11": _lane_state(0.0, 1.0),
                "3.12": _lane_state(2.0, 3.0),
            },
            "imageBuildSeconds": args[6],
            "pytestDurations": [],
        }

    monkeypatch.setattr(gate, "_collect_acceptance_samples", _collect)

    gate.main(["--acceptance", "--acceptance-output", str(tmp_path / "verdict.json")])

    assert seen["seconds"] == 30.0


class _RaisingLaneProcess:
    """A lane whose output stream is interrupted part-way through."""

    def __init__(self, chunks, exception):
        self.stdout = _RaisingStream(chunks, exception)
        self.returncode = None
        self.killed = 0
        self.waited = 0

    def kill(self):
        self.killed += 1

    def wait(self, timeout=None):
        self.waited += 1
        self.returncode = -9
        return self.returncode

    def poll(self):
        return self.returncode


class _RaisingStream:
    def __init__(self, chunks, exception):
        self._chunks = list(chunks)
        self._exception = exception

    def readline(self):
        if self._chunks:
            return self._chunks.pop(0)
        raise self._exception

    def close(self):
        pass


def test_an_interrupted_lane_is_reaped_rather_than_left_running(monkeypatch, tmp_path):
    """The SIGTERM handler raises, and it can raise inside the readline loop.
    Unwinding from there without waiting on the lane leaves a zombie behind and,
    outside acceptance mode, a container nothing else removes."""
    gate = _load_gate()
    process = _RaisingLaneProcess([b"partial output\n"], SystemExit(143))
    # #630 S2: patch the IMPORTER's reference, never the shared
    # stdlib module object, which every other importer and every
    # concurrent thread resolves through.
    _iso_subprocess = types.SimpleNamespace(**vars(gate.subprocess))
    _iso_subprocess.Popen = lambda command, **kwargs: process
    monkeypatch.setattr(gate, "subprocess", _iso_subprocess)

    with pytest.raises(SystemExit):
        gate._run_lanes(
            "docker",
            ("3.11",),
            {"3.11": "sha256:3.11"},
            tmp_path,
            "d" * 40,
            tmp_path,
        )

    assert process.killed == 1
    assert process.waited == 1
    # What the lane did emit before the interrupt is still on disk.
    assert (tmp_path / "3.11.log").read_bytes() == b"partial output\n"


def test_lane_output_is_forwarded_as_bytes(monkeypatch, tmp_path, capsysbinary):
    """Decoding every chunk to text adds two abort paths to a 73-minute gate —
    `UnicodeEncodeError` when the operator's stdout encoding cannot represent a
    replacement character, and `BrokenPipeError` from the text layer — and it
    rewrites bytes the lane emitted. The byte stream is forwarded unchanged."""
    gate = _load_gate()
    payload = b"progress \xff\xfe done\n"

    class _Lane:
        def __init__(self):
            self.stdout = io.BytesIO(payload)
            self.returncode = None

        def wait(self, timeout=None):
            self.returncode = 0
            return 0

        def poll(self):
            return self.returncode

    # #630 S2: patch the IMPORTER's reference, never the shared
    # stdlib module object, which every other importer and every
    # concurrent thread resolves through.
    _iso_subprocess = types.SimpleNamespace(**vars(gate.subprocess))
    _iso_subprocess.Popen = lambda command, **kwargs: _Lane()
    monkeypatch.setattr(gate, "subprocess", _iso_subprocess)

    gate._run_lanes(
        "docker",
        ("3.11",),
        {"3.11": "sha256:3.11"},
        tmp_path,
        "d" * 40,
        tmp_path,
    )

    assert payload in capsysbinary.readouterr().out
    assert (tmp_path / "3.11.log").read_bytes() == payload


def test_prunable_image_tags_keeps_the_current_and_one_prior_generation():
    """`_freshness_epoch` puts the ISO year-week in the image key, so the first
    cut of every new week mints a fresh three-image set and orphans the previous
    one — about four gigabytes a week with nothing removing it. One prior
    generation is retained so a bad pin bump can be rolled back."""
    gate = _load_gate()
    ordered = [
        ("cctally-linux-matrix:3.12-current", "sha256:current"),
        ("cctally-linux-matrix:3.12-prior", "sha256:prior"),
        ("cctally-linux-matrix:3.12-older", "sha256:older"),
        ("cctally-linux-matrix:3.12-oldest", "sha256:oldest"),
    ]
    assert gate._prunable_image_tags(ordered, "sha256:current") == [
        "cctally-linux-matrix:3.12-older",
        "cctally-linux-matrix:3.12-oldest",
    ]


def test_prunable_image_tags_identifies_the_kept_image_by_id_not_by_position():
    """A rebuilt tag can carry an older creation time than an orphan, so the
    image the lane actually runs is retained wherever it sits in the listing,
    and every tag pointing at it is retained with it."""
    gate = _load_gate()
    ordered = [
        ("cctally-linux-matrix:3.12-newest", "sha256:newest"),
        ("cctally-linux-matrix:3.12-orphan", "sha256:orphan"),
        ("cctally-linux-matrix:3.12-inuse", "sha256:inuse"),
        ("cctally-linux-matrix:3.12-alias", "sha256:inuse"),
    ]
    assert gate._prunable_image_tags(ordered, "sha256:inuse") == [
        "cctally-linux-matrix:3.12-orphan"
    ]


def test_prunable_image_tags_removes_nothing_below_the_retention_count():
    gate = _load_gate()
    ordered = [
        ("cctally-linux-matrix:3.12-current", "sha256:current"),
        ("cctally-linux-matrix:3.12-prior", "sha256:prior"),
    ]
    assert gate._prunable_image_tags(ordered, "sha256:current") == []
    assert gate._prunable_image_tags([], "sha256:current") == []


def test_created_at_ordering_falls_back_to_the_engine_listing_order():
    """`docker images` already lists newest first. If any timestamp fails to
    parse, every row keeps that order rather than mixing two ranking schemes."""
    gate = _load_gate()
    rows = [
        ("tag-a", "2026-08-19 00:05:32 +0300 IDT"),
        ("tag-b", "2026-08-19 03:49:10 +0300 IDT"),
    ]
    assert gate._order_tags_newest_first(rows) == ["tag-b", "tag-a"]
    unparsable = [("tag-a", "yesterday"), ("tag-b", "2026-08-19 03:49:10 +0300 IDT")]
    assert gate._order_tags_newest_first(unparsable) == ["tag-a", "tag-b"]


def _fake_engine(gate, monkeypatch, listings, removed, rmi_status=0):
    def _fake_run(command, **kwargs):
        if command[1] == "images":
            key = "dangling" if "dangling=true" in command else "reference"
            return subprocess.CompletedProcess(command, 0, stdout=listings.get(key, ""))
        if command[1] == "rmi":
            removed.extend(command[2:])
            return subprocess.CompletedProcess(command, rmi_status, stdout="")
        return subprocess.CompletedProcess(command, 0, stdout="")

    monkeypatch.setattr(gate, "_run", _fake_run)


def test_stale_generations_are_removed_after_the_image_resolves(monkeypatch):
    gate = _load_gate()
    removed: list[str] = []
    listing = (
        "cctally-linux-matrix:3.12-cur|sha256:cur|2026-08-19 03:49:10 +0300 IDT\n"
        "cctally-linux-matrix:3.12-old|sha256:old|2026-08-19 02:21:46 +0300 IDT\n"
        "cctally-linux-matrix:3.12-ancient|sha256:ancient|2026-08-18 00:05:32 +0300 IDT\n"
    )
    _fake_engine(gate, monkeypatch, {"reference": listing}, removed)
    gate._prune_stale_images("docker", "3.12", "sha256:cur")
    assert removed == ["cctally-linux-matrix:3.12-ancient"]


def test_the_untagged_first_pass_images_are_pruned_by_label(monkeypatch):
    """The two-pass label build retags onto a new image id and orphans the
    first-pass image. The label filter is what makes the removal safe: an
    untagged image without `cctally.image.inputs` belongs to something else on
    this host."""
    gate = _load_gate()
    removed: list[str] = []
    _fake_engine(
        gate, monkeypatch, {"dangling": "sha256:first-a\nsha256:first-b\n"}, removed
    )
    gate._prune_first_pass_images("docker")
    assert removed == ["sha256:first-a", "sha256:first-b"]


def test_a_failing_removal_never_fails_the_gate(monkeypatch):
    """Garbage collection must never refuse a cut: an image another container
    still references, or a layer a kept tag shares, is a reason to leave it
    alone rather than to fail the release gate."""
    gate = _load_gate()
    removed: list[str] = []
    listing = (
        "cctally-linux-matrix:3.12-cur|sha256:cur|2026-08-19 03:49:10 +0300 IDT\n"
        "cctally-linux-matrix:3.12-old|sha256:old|2026-08-19 02:21:46 +0300 IDT\n"
        "cctally-linux-matrix:3.12-ancient|sha256:ancient|2026-08-18 00:05:32 +0300 IDT\n"
    )
    _fake_engine(gate, monkeypatch, {"reference": listing}, removed, rmi_status=1)
    gate._prune_stale_images("docker", "3.12", "sha256:cur")
    assert removed == ["cctally-linux-matrix:3.12-ancient"]

    def _explode(command, **kwargs):
        raise OSError("engine went away")

    monkeypatch.setattr(gate, "_run", _explode)
    gate._prune_stale_images("docker", "3.12", "sha256:cur")
    gate._prune_first_pass_images("docker")


def test_the_gate_prunes_once_per_resolved_image(monkeypatch, tmp_path):
    gate = _load_gate()
    _stub_successful_matrix(monkeypatch, gate, tmp_path)
    monkeypatch.setattr(gate, "_git_head", lambda root: "a" * 40)
    monkeypatch.setattr(gate, "_git_status", lambda root: "")
    pruned: list[tuple[str, str]] = []
    first_pass: list[str] = []
    monkeypatch.setattr(
        gate,
        "_prune_stale_images",
        lambda engine, version, keep: pruned.append((version, keep)),
    )
    monkeypatch.setattr(
        gate, "_prune_first_pass_images", lambda engine: first_pass.append(engine)
    )

    assert gate.main([]) == 0
    assert pruned == [(version, f"sha256:{version}") for version in gate.PYTHON_VERSIONS]
    assert first_pass == ["docker"]
