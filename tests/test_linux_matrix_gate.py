"""Release-blocking local Linux multi-interpreter gate (#595)."""
from __future__ import annotations
import types

import ast
import datetime
import importlib.machinery
import importlib.util
import io
import json
import os
import pathlib
import re
import shlex
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

    # #630 S2 seam: patch the IMPORTER's reference, never the shared stdlib
    # module object. `gate.os` IS `os`, so rebinding `replace` on it swapped
    # the callable that every other importer and every concurrent thread in
    # the process resolves, for the whole length of this body — the F7 class
    # the isolation plugin's state detector exists to catch. It reported this
    # site once that detector ran in the load-invariance lane (#638).
    _iso_os = types.SimpleNamespace(**vars(gate.os))
    _iso_os.replace = _spy
    monkeypatch.setattr(gate, "os", _iso_os)
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


# The three tracked copies of the release skill. The two generated projections
# are included so a missed `bin/check-agent-workflows --sync` cannot leave a
# stale claim shipping in the copy an agent actually loads. The waiver marker
# has to sit INSIDE the statement's own source span, and that span covers the
# WHOLE tuple, so an entry added here inherits it silently.
_RELEASE_SKILL_COPIES = (  # mirror-private-ok
    ".agent-workflows/skills/shared/release-cctally/SKILL.md",
    ".agents/skills/release-cctally/SKILL.md",
    ".claude/skills/release-cctally/SKILL.md",
)

#: Sentence-final punctuation followed by whitespace. Splitting on a bare
#: period would cut "Gate 0.25" in half, because that period is followed by a
#: digit rather than by whitespace.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")

#: The retired universal-quantifier claim. #630 S5 removed MATRIX_RC from the
#: refusal predicate and left a sentence quantifying the refusal over all three
#: outcomes, which is the exact form this asserts is gone (#658).
_UNIVERSAL_REFUSAL = re.compile(
    r"\b(any failure|each|all three|every failure)\b[^.!?]*refus\w*\s+the\s+cut",
    re.I,
)

#: The carve-out the region must carry once it enumerates the third outcome.
_NON_BLOCKING_MARKER = re.compile(
    r"advisory|never changes the release exit status"
    r"|does not change the release exit status",
    re.I,
)


def _gate0_narrative(text):
    """The prose between the overlap block and the Gate 0.25 section heading.

    This is the region that describes what the block just printed, and it is
    where the stale claim lived. Bounded on the left by the end of the overlap
    block so the block's own comments — which are already correct — are not
    what satisfies the assertions.
    """
    block = _overlap_block(text)
    start = text.index(block) + len(block)
    end = text.index("**Gate 0.25 —", start)
    return text[start:end]


@pytest.mark.parametrize("relative", _RELEASE_SKILL_COPIES)
def test_the_gate0_narrative_states_which_gates_refuse_the_cut(relative):
    """The narrative must not describe the three outcomes without the carve-out.

    `test_the_matrix_is_advisory_and_still_printed` covers the executable
    predicate and nothing covered the prose fourteen lines below it, so #630 S5
    could change the predicate, leave the sentence claiming "any failure refuses
    the cut", and ship a runbook that contradicts itself. An operator reading
    that sentence would expect a red matrix to block, and would either abandon a
    cut the script would have allowed or conclude the script is broken when it
    does not refuse (#658).

    A test cannot certify that prose is true. What it certifies here is
    structural: the region enumerates all three outcomes, so it must also say
    which of them refuse and which does not, and it must not carry the retired
    universal-quantifier form.
    """
    path = REPO / relative
    if not path.is_file():
        pytest.skip("private release skill absent from public mirror")
    region = _gate0_narrative(path.read_text(encoding="utf-8"))

    # The region does enumerate the third outcome, which is what obliges it to
    # state the carve-out. Were this to stop holding, the assertions below
    # would be vacuous rather than failing.
    assert "Gate 0.25" in region, region

    # The positive half: it still says a failure refuses the cut, so a future
    # edit cannot satisfy the carve-out by deleting the refusal statement.
    assert re.search(r"refus\w*\s+the\s+cut", region, re.I), region

    # The carve-out itself, attached to the matrix rather than stated loosely
    # somewhere in the region.
    carved = [
        sentence
        for sentence in _SENTENCE_SPLIT.split(region)
        if "Gate 0.25" in sentence and _NON_BLOCKING_MARKER.search(sentence)
    ]
    assert carved, region

    # The retired claim.
    assert not _UNIVERSAL_REFUSAL.search(region), region


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
        '  [ -n "${STUB_GATE0_READY:-}" ] && : > "$STUB_GATE0_READY"\n'
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
        '[ -n "${STUB_MATRIX_READY:-}" ] && : > "$STUB_MATRIX_READY"\n'
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


#: The parent shell reaches this after its traps exist and both process-group
#: ids are assigned (.agents/skills/release-cctally/SKILL.md:81-83, :104, :143,
#: :146), which is the earliest point at which a signal can demonstrate that
#: the trap reaps two LIVE gates.
_WAIT_TARGET = 'if wait "$MATRIX_PGID"; then'

#: Well above the 30-second readiness deadline, so a gate cannot finish on its
#: own inside the window in which readiness is still being awaited. At the
#: previous 20 seconds there was a ten-second band in which readiness was
#: reported, the signal was sent, and `gate0.status` already existed.
_OVERLAP_STUB_SLEEP = "60"
_READINESS_DEADLINE_SECONDS = 30.0
_READINESS_POLL_SECONDS = 0.01


def _await_overlap_readiness(process, markers, log_path):
    """Block until all three markers exist, the process dies, or 30s elapse."""
    deadline = time.monotonic() + _READINESS_DEADLINE_SECONDS
    while True:
        missing = [name for name, path in markers.items() if not path.exists()]
        if not missing:
            return
        if process.poll() is not None:
            raise AssertionError(
                "the overlap block exited (rc=%s) before reaching readiness; "
                "missing %s\nstub log:\n%s"
                % (process.returncode, missing, log_path.read_text()))
        if time.monotonic() >= deadline:
            process.kill()
            process.wait(timeout=30)
            raise AssertionError(
                "overlap block did not reach interruptible wait within %.0fs "
                "(missing: %s)\nstub log:\n%s"
                % (_READINESS_DEADLINE_SECONDS, missing, log_path.read_text()))
        time.sleep(_READINESS_POLL_SECONDS)


def _run_overlap_block(tmp_path, env_overrides, signal_on_readiness=False):
    """Run the release skill's overlap block against the stub gates.

    `signal_on_readiness` is a flag, not a duration: when set, the caller waits
    for all three readiness markers and then terminates the block. It replaced
    a `signal_after=<seconds>` sleep in #659, so it is read for TRUTH — under
    the old contract `0` meant "signal immediately", and an `is not None` test
    carried over from it would make `False` signal too.
    """
    skill = REPO / ".agents/skills/release-cctally/SKILL.md"
    if not skill.exists():
        pytest.skip("private release skill absent from public mirror")
    block = _overlap_block(skill.read_text())
    root, stubs = _stub_release_root(tmp_path)
    log = tmp_path / "stub.log"
    log.write_text("")
    markers = {
        "gate0": tmp_path / "gate0.ready",
        "matrix": tmp_path / "matrix.ready",
        "wait": tmp_path / "wait.ready",
    }
    if signal_on_readiness:
        # Absolute, because `bash -c` runs with pytest's working directory —
        # the repository root — while the gate stubs run after `cd
        # "$RELEASE_ROOT"`. A relative marker would be written into the live
        # tree from one of them and into the synthetic root from the other.
        stale = [str(path) for path in markers.values() if path.exists()]
        assert not stale, "stale readiness marker(s) present: %r" % stale
        assert block.count(_WAIT_TARGET) == 1, (
            "expected exactly one %r in the overlap block" % _WAIT_TARGET)
        block = block.replace(
            _WAIT_TARGET,
            "touch %s\n  %s" % (shlex.quote(str(markers["wait"])), _WAIT_TARGET))
    env = dict(os.environ)
    env.update(
        {
            "PATH": f"{stubs}:{env.get('PATH', '')}",
            "RELEASE_ROOT": str(root),
            "GATE0_OUT": str(tmp_path / "gate0.json"),
            "GATE0_STATUS": str(tmp_path / "gate0.status"),
            "STUB_LOG": str(log),
            "STUB_RECORD_PATH": str(tmp_path / "record.json"),
            "STUB_GATE0_READY": str(markers["gate0"]),
            "STUB_MATRIX_READY": str(markers["matrix"]),
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
    if signal_on_readiness:
        _await_overlap_readiness(process, markers, log)
        process.terminate()
    out, _ = process.communicate(timeout=120)
    return process.returncode, out, log.read_text()


def test_the_readiness_substitution_matches_the_block_exactly_once():
    """A future edit to SKILL.md must not silently turn the readiness wait into
    a thirty-second timeout."""
    skill = REPO / ".agents/skills/release-cctally/SKILL.md"
    if not skill.exists():
        pytest.skip("private release skill absent from public mirror")
    block = _overlap_block(skill.read_text())
    assert block.count(_WAIT_TARGET) == 1, (
        "the readiness marker is injected before %r, which must be unique"
        % _WAIT_TARGET)


def test_a_stale_readiness_marker_is_refused_rather_than_believed(tmp_path):
    """A leftover marker would let the wait return before the block reached
    anything, which is the wall-clock defect wearing a different hat."""
    skill = REPO / ".agents/skills/release-cctally/SKILL.md"
    if not skill.exists():
        pytest.skip("private release skill absent from public mirror")
    (tmp_path / "gate0.ready").write_text("")
    with pytest.raises(AssertionError, match="stale readiness marker"):
        _run_overlap_block(
            tmp_path,
            {"STUB_GATE0_RC": "0", "STUB_MATRIX_RC": "0"},
            signal_on_readiness=True,
        )


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
    the operator restarts rather than resumes.

    The signal is sent once the block has installed its traps, launched both
    gates and reached its interruptible `wait` — observed, not assumed after a
    fixed second (#659). The stub backstops sit at 60s against a 30s readiness
    deadline, so no gate can complete inside the window in which readiness is
    still being awaited.
    """
    status, out, log = _run_overlap_block(
        tmp_path,
        {
            "STUB_GATE0_SLEEP": _OVERLAP_STUB_SLEEP,
            "STUB_MATRIX_SLEEP": _OVERLAP_STUB_SLEEP,
            "STUB_GATE0_RC": "0",
            "STUB_MATRIX_RC": "0",
        },
        signal_on_readiness=True,
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


#: Path methods that mutate the path they are called on. `symlink_to` and
#: `hardlink_to` belong here because each CREATES the path it is called on.
_PATH_MUTATORS = frozenset({
    "write_bytes", "write_text", "touch", "unlink", "mkdir",
    "rename", "replace", "chmod", "rmdir", "symlink_to", "hardlink_to",
})

#: Free and dotted functions that mutate a path passed as an argument, and the
#: argument positions each one can mutate. Every `_PATH_MUTATORS` name that has
#: an `os` free spelling is carried here under that spelling — `chmod`,
#: `unlink`, `mkdir`, `rmdir`, `rename`, `replace`, `symlink_to` as
#: `os.symlink` and `hardlink_to` as `os.link` — so a mutation cannot escape
#: the guard by choosing one syntax over the other. `os.utime` has no
#: `_PATH_MUTATORS` counterpart and is here anyway, because a metadata write
#: into a tracked file is the same observable mutation as a content write.
#: `os.symlink` and `os.link` mutate only their SECOND argument: each creates
#: the destination and requires the source to exist already. `open` is
#: deliberately absent: it mutates only under a writing mode, which is a
#: separate check.
_FUNCTION_MUTATORS = {
    "os.remove": (0,), "os.unlink": (0,), "os.rmdir": (0,),
    "os.removedirs": (0,), "os.mkdir": (0,), "os.makedirs": (0,),
    "os.truncate": (0,), "shutil.rmtree": (0,),
    "os.chmod": (0,), "os.utime": (0,),
    "os.rename": (0, 1), "os.replace": (0, 1), "shutil.move": (0, 1),
    "os.symlink": (1,), "os.link": (1,),
    "shutil.copy": (1,), "shutil.copy2": (1,), "shutil.copyfile": (1,),
    "shutil.copytree": (1,),
}

_WRITING_MODE_FLAGS = ("w", "a", "x", "+")

#: Every character a `builtins.open` mode string may contain. A literal outside
#: this alphabet, or longer than four characters, is not a mode at all. The
#: bound is four rather than three, the longest legal mode, so that a mode this
#: comment failed to anticipate is admitted rather than read as a filename.
_MODE_ALPHABET = frozenset("rwxab+tU")


def _repository_writers(source):
    """Every call in `source` that mutates a path rooted at the repository.

    Extracted from the guard below so the guard's own scope can be tested
    against sources it does not itself contain. Applied to this module it must
    return an empty list; applied to a source carrying a known offender it must
    name it.

    ROOTEDNESS is resolved to a fixpoint over names, seeded from the MODULE's
    own assignments and extended per function. The module seed matters: this
    file binds `SCRIPT = REPO / "bin" / "cctally-test-linux-matrix"` at module
    level, so a `SCRIPT.write_text(...)` anywhere in it is the most likely
    future offender and a function-local walk never sees the binding at all.

    Names are bound by more than `ast.Assign`. `for path in REPO.glob(...)`
    binds through a `for` target and `with open(REPO / "x", "w") as handle`
    through a `withitem`, and a collector that reads assignments alone reports
    nothing on either.

    MUTATION is any of four forms: a `_PATH_MUTATORS` method called on a rooted
    expression; `open` as a free function on a rooted path under a writing
    mode, taking the path from the first positional argument or `file=` and the
    mode from the second or `mode=`; the same test applied to the bound
    `rooted.open(...)`, whose mode is instead its FIRST positional argument;
    and a `_FUNCTION_MUTATORS` entry called with a rooted path in a position it
    mutates. The attribute set alone misses `open`, `os.remove` and
    `shutil.rmtree` entirely. The two ATTRIBUTE tests run first, the
    `_PATH_MUTATORS` one and then the bound `rooted.open(...)` one, and each
    FALLS THROUGH rather than answering, because six `_FUNCTION_MUTATORS`
    keys — `os.chmod`, `os.unlink`, `os.mkdir`, `os.rmdir`, `os.rename` and
    `os.replace` — are dotted names whose attribute is also a `_PATH_MUTATORS`
    entry, and an attribute test that answered would answer `False` for all six
    on the rootedness of the module `os`. The free-`open` test runs after both
    of them and ANSWERS in either direction, and the `_FUNCTION_MUTATORS` test
    is last and terminal, so neither of those two can fall through.

    `replace` is the one name shared by a path method and a string method, and
    the two are told apart by ARITY: `pathlib.Path.replace(target)` takes one
    positional argument and `str.replace(old, new[, count])` takes two or more,
    so a `replace` call carrying two or more positional arguments is refused.
    That is what lets rootedness propagate through a content read — the case
    `test_the_write_guard_reaches_a_path_read_out_of_the_tree` pins — without
    reporting `block.replace(old, new)` on a string read out of the tree.

    THE ROOT IS MATCHED STRUCTURALLY — a `REPO` name, a `.REPO` attribute or a
    `_repo_root()` call — rather than by looking for "REPO" in the unparsed
    text. The textual form reports this module's own synthetic guard corpus,
    because those sources are string literals whose text contains `REPO`.

    COVERAGE IS NOT COMPLETE, AND THE COMPLEMENT IS OPEN RATHER THAN
    ENUMERATED. The positive scope is stated above and is the whole of it: a
    call reached by none of `_PATH_MUTATORS`, `_FUNCTION_MUTATORS` and the
    three `open` spellings is silently unreported, and those two tables are the
    entire inventory. `shutil.unpack_archive`, `shutil.make_archive`,
    `ZipFile(...).extractall(rooted)`, `tarfile.open(...).extractall(rooted)`
    and `tempfile.NamedTemporaryFile(dir=rooted)` are five such forms, and
    naming them does not shorten the list either, because nothing bounds it.

    Four forms were considered and DELIBERATELY left outside, and
    `_GUARD_UNCOVERED_FORMS` pins those four so that widening the guard to
    reach one of them fails a test until this paragraph is corrected too. A
    mutator bound to a bare name by `from os import remove` or aliased by
    `import shutil as sh` is not reported, because `_FUNCTION_MUTATORS` is
    keyed on the dotted text the call site writes. A write performed by a child
    process — `subprocess.run(["rm", "-f", str(REPO / "x")])` — is not
    reported, because nothing in the call is a path mutation. A
    descriptor-level `os.open(path, os.O_WRONLY)` is not reported, because its
    flags are an integer expression and the writing-mode test reads a string
    literal. A `_FUNCTION_MUTATORS` argument passed by keyword rather than by
    position is not reported, because the table records positions. The
    `subprocess` one matters most, because this module DOES spawn child
    processes: a child that wrote into the tree would pass this guard silently.
    Reading a shell argument vector for path mutation is a second guard rather
    than a widening of this one, and no offender of that shape has appeared
    yet.

    THE INTERPROCEDURAL FORM IS THE ONE A READER WOULD WRONGLY ASSUME IS
    COVERED. Rootedness is resolved per function and never crosses a call
    boundary, so `_helper(REPO / "bin" / "x")` where `_helper` writes to its
    own parameter is reported in neither function: the caller performs no
    mutation, and the callee's parameter is not rooted. This module already
    has helpers that take a path and write through it, so the shape is
    reachable here rather than hypothetical.

    ONE KNOWN FALSE-POSITIVE SHAPE is kept deliberately. Rootedness propagates
    through a content read and carries no type, so any name derived from a
    repository file is rooted whatever it holds. A zero-positional `.replace()`
    on such a value — `stamp.replace(tzinfo=datetime.timezone.utc)` on a
    `datetime` parsed out of a tracked file — is a `_PATH_MUTATORS` hit that
    the arity rule does not refuse, because that rule refuses two or more
    positional arguments. Nothing in this module writes that shape. Narrowing
    rootedness by receiver kind is what opened the hole
    `test_the_write_guard_reaches_a_path_read_out_of_the_tree` closes, so the
    false positive is the cheaper of the two.
    """
    tree = ast.parse(source)

    def _names_in(node):
        return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}

    def _mentions_the_root(node):
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and sub.id == "REPO":
                return True
            if isinstance(sub, ast.Attribute) and sub.attr == "REPO":
                return True
            if isinstance(sub, ast.Call) and getattr(
                    sub.func, "attr", getattr(sub.func, "id", "")) == "_repo_root":
                return True
        return False

    def _rooted(node, names):
        if node is None:
            return False
        if _mentions_the_root(node):
            return True
        return bool(_names_in(node) & names)

    def _bound_names(target):
        if isinstance(target, ast.Name):
            return {target.id}
        found = set()
        if isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                found |= _bound_names(element)
        elif isinstance(target, ast.Starred):
            found |= _bound_names(target.value)
        return found

    def _module_bindings():
        pairs = []
        for node in tree.body:
            if isinstance(node, ast.Assign):
                pairs.extend((target, node.value) for target in node.targets)
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                pairs.append((node.target, node.value))
        return pairs

    def _bindings(scope):
        pairs = []
        for node in ast.walk(scope):
            if isinstance(node, ast.Assign):
                pairs.extend((target, node.value) for target in node.targets)
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                if node.value is not None:
                    pairs.append((node.target, node.value))
            elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
                pairs.append((node.target, node.iter))
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    if item.optional_vars is not None:
                        pairs.append((item.optional_vars, item.context_expr))
        return pairs

    def _resolve(pairs, seed):
        names = set(seed)
        changed = True
        while changed:
            changed = False
            for target, value in pairs:
                if not _rooted(value, names):
                    continue
                for name in _bound_names(target) - names:
                    names.add(name)
                    changed = True
        return names

    def _writing_mode(call, position):
        """Whether the mode argument at `position` (or `mode=`) opens for write.

        The literal must LOOK like a mode before its flags are read. The bound
        `rooted.open(...)` branch takes the mode from the FIRST positional
        argument, and other `open` methods put something else there:
        `ZipFile(rooted).open("member.txt")` passes a member name, and a name
        carrying an `x`, a `w`, an `a` or a `+` would otherwise be read as a
        writing mode and reported as a repository write.
        """
        mode = call.args[position] if len(call.args) > position else next(
            (kw.value for kw in call.keywords if kw.arg == "mode"), None)
        literal = (mode.value if isinstance(mode, ast.Constant)
                   and isinstance(mode.value, str) else "")
        if len(literal) > 4 or not set(literal) <= _MODE_ALPHABET:
            return False
        return any(flag in literal for flag in _WRITING_MODE_FLAGS)

    def _writes(call, names):
        target = call.func
        if isinstance(target, ast.Attribute):
            # Every attribute test FALLS THROUGH when it does not match, because
            # `os.chmod`, `os.unlink`, `os.mkdir`, `os.rmdir`, `os.rename` and
            # `os.replace` are dotted names whose attribute is also a
            # `_PATH_MUTATORS` entry. Returning the receiver's rootedness here
            # answers `False` for all six — the receiver is the module `os` —
            # and `_FUNCTION_MUTATORS` never gets to see them.
            if target.attr in _PATH_MUTATORS and _rooted(target.value, names):
                # `str.replace(old, new[, count])`, not `Path.replace(target)`.
                if not (target.attr == "replace" and len(call.args) > 1):
                    return True
            if (target.attr == "open" and _writing_mode(call, 0)
                    and _rooted(target.value, names)):
                # `Path.open(mode, ...)` puts the mode FIRST; the free `open`
                # puts it second, behind the path.
                return True
        rendered = ast.unparse(target)
        if rendered == "open":
            if not _writing_mode(call, 1):
                return False
            path = call.args[0] if call.args else next(
                (kw.value for kw in call.keywords if kw.arg == "file"), None)
            return _rooted(path, names)
        positions = _FUNCTION_MUTATORS.get(rendered)
        if positions is None:
            return False
        return any(_rooted(call.args[index], names)
                   for index in positions if index < len(call.args))

    def _innermost_owners():
        """Each call's innermost enclosing function name.

        A call inside a nested function is reached twice, because the outer
        function is walked with `ast.walk` and that descends into the inner
        one. Both visits are wanted — the outer scope is the only one that
        resolves a name the inner function closes over — but the finding is
        one finding, and it names the function the call is written in.
        """
        owners = {}

        def _descend(node, name):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    _descend(child, child.name)
                    continue
                if isinstance(child, ast.Call):
                    owners[child] = name
                _descend(child, name)

        _descend(tree, "<module>")
        return owners

    module_names = _resolve(_module_bindings(), set())
    owners = _innermost_owners()
    found = {}
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        rooted = _resolve(_bindings(func), module_names)
        for node in ast.walk(func):
            if isinstance(node, ast.Call) and _writes(node, rooted):
                found[(node.lineno, node.col_offset)] = "%s:%d %s" % (
                    owners.get(node, func.name), node.lineno,
                    ast.unparse(node))
    # Source order, because `ast.walk` is breadth-first: a call nested inside a
    # `with` is reported after the statements that follow it, which makes the
    # findings read out of order and an expected list arbitrary.
    return [text for _, text in sorted(found.items())]


def test_no_test_in_this_module_writes_into_the_repository_tree():
    """The #659 defect class. A test that mutates a tracked file is observable
    by every concurrently scheduled xdist worker, and `write_bytes` truncates
    before writing, so a reader can see an EMPTY file rather than a merely
    different one.

    The scope this applies is `_repository_writers` above, and the cases below
    pin what that scope reaches. This assertion is the one that matters: no
    call in THIS module writes into the tree.
    """
    offenders = _repository_writers(pathlib.Path(__file__).read_text())
    assert offenders == [], (
        "these mutate the live repository tree, which every concurrently "
        "scheduled xdist worker can observe: %r" % (offenders,))


#: The original #659 offender, reduced. Rootedness reaches `dockerfile` only
#: through `root`, which is why the guard resolves names transitively.
_GUARD_TRANSITIVE_LOCAL = """
def test_image_records_change_when_the_dockerfile_changes():
    gate = _load_gate()
    root = gate._repo_root()
    dockerfile = root / "bin" / "cctally-test-linux-matrix.Dockerfile"
    original = dockerfile.read_bytes()
    try:
        dockerfile.write_bytes(original + b"# provoke a digest change")
    finally:
        dockerfile.write_bytes(original)
"""

#: The most likely FUTURE offender, and the one a function-local walk misses.
_GUARD_MODULE_LEVEL_CONSTANT = """
REPO = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = REPO / "bin" / "cctally-test-linux-matrix"

def test_writes_through_the_module_constant():
    SCRIPT.write_text("mutated")
"""

_GUARD_LOOP_TARGET = """
REPO = pathlib.Path(__file__).resolve().parents[1]

def test_writes_through_a_loop_target():
    for path in REPO.glob("*.scratch"):
        path.unlink()
"""

_GUARD_FREE_FUNCTION_MUTATORS = """
REPO = pathlib.Path(__file__).resolve().parents[1]

def test_writes_without_a_path_method():
    with open(REPO / "bin" / "cctally-test-linux-matrix", "w") as handle:
        handle.write("mutated")
    os.remove(REPO / "bin" / "leftover")
    shutil.rmtree(REPO / "scratch")
"""

#: `open` as a BOUND method, and the free `open` addressed entirely by
#: keyword. Both are `open(rooted, "w")` wearing a different syntax.
_GUARD_OPEN_VARIANTS = """
REPO = pathlib.Path(__file__).resolve().parents[1]

def test_writes_through_the_other_two_spellings_of_open():
    with (REPO / "bin" / "cctally-test-linux-matrix").open("w") as handle:
        handle.write("mutated")
    with open(file=REPO / "bin" / "leftover", mode="a") as handle:
        handle.write("appended")
"""

#: Link creation. Each of these CREATES the path it is called on, so a tracked
#: file replaced by a symlink is the same observable defect as a rewrite.
_GUARD_LINK_CREATORS = """
REPO = pathlib.Path(__file__).resolve().parents[1]

def test_creates_links_in_the_tree(tmp_path):
    (REPO / "bin" / "soft").symlink_to(tmp_path / "elsewhere")
    (REPO / "bin" / "hard").hardlink_to(tmp_path / "elsewhere")
"""

#: A write inside a nested function, through a name the inner function CLOSES
#: OVER. `ast.walk` reaches the call from the outer function too, and the outer
#: scope is the only one that resolves `target`, so the outer visit is the only
#: one that reports. This case pins the NAMING rule, not the deduplication.
_GUARD_NESTED_FUNCTION = """
REPO = pathlib.Path(__file__).resolve().parents[1]

def test_writes_from_a_closure():
    target = REPO / "bin" / "cctally-test-linux-matrix"

    def _mutate():
        target.write_text("mutated")

    _mutate()
"""

#: The same nesting over a MODULE-LEVEL constant, which is the case that
#: actually exercises the deduplication. Every function's rooted set is seeded
#: with the module's names, so `SCRIPT` resolves in the outer scope AND in the
#: inner one, both visits report, and the collector emits the finding twice
#: unless it is keyed by source position.
_GUARD_NESTED_MODULE_CONSTANT = """
REPO = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = REPO / "bin" / "cctally-test-linux-matrix"

def test_writes_from_a_closure_over_a_module_constant():
    def _mutate():
        SCRIPT.write_text("mutated")

    _mutate()
"""

#: The `os` free spellings of the path methods. A mutation must not escape the
#: guard by being written as `os.chmod(p, ...)` rather than `p.chmod(...)`, and
#: `os.symlink`/`os.link` create their SECOND argument, so a rooted source with
#: a destination outside the tree is not a repository write.
_GUARD_OS_FREE_MUTATORS = """
REPO = pathlib.Path(__file__).resolve().parents[1]

def test_writes_through_the_os_spellings(tmp_path):
    os.chmod(REPO / "bin" / "cctally-test-linux-matrix", 0o755)
    os.utime(REPO / "bin" / "cctally-test-linux-matrix", None)
    os.unlink(REPO / "bin" / "leftover")
    os.mkdir(REPO / "bin" / "created")
    os.rmdir(REPO / "bin" / "obsolete")
    os.rename(REPO / "bin" / "before", tmp_path / "after")
    os.replace(tmp_path / "before", REPO / "bin" / "after")
    os.symlink(tmp_path / "elsewhere", REPO / "bin" / "soft")
    os.link(tmp_path / "elsewhere", REPO / "bin" / "hard")
    os.symlink(REPO / "bin" / "cctally-test-linux-matrix", tmp_path / "copy")
"""

#: The false positive the mode-plausibility rule must suppress. `ZipFile.open`
#: and `TarFile.extractfile` put a MEMBER NAME where `Path.open` puts the mode,
#: and `member.txt` contains an `x`, so a flag test alone reads it as a write.
_GUARD_NON_MODE_FIRST_ARGUMENT = """
REPO = pathlib.Path(__file__).resolve().parents[1]

def test_reads_a_zip_member_out_of_the_tree():
    archive = zipfile.ZipFile(REPO / "bin" / "bundle.zip")
    with archive.open("member.txt") as handle:
        first = handle.read()
    with archive.open("wax!") as handle:
        second = handle.read()
    with archive.open("r++++") as handle:
        third = handle.read()
    return first, second, third
"""

#: Five concrete examples of the documented OPEN complement. They are not a
#: complete inventory; they pin that broadening the guard to cover one requires
#: the coverage paragraph and the test to move together.
_GUARD_OPEN_COMPLEMENT = """
REPO = pathlib.Path(__file__).resolve().parents[1]

def test_uses_mutators_outside_the_guard_inventory():
    shutil.unpack_archive(REPO / "bundle.zip", REPO / "unpacked")
    shutil.make_archive(str(REPO / "bundle"), "zip", REPO / "payload")
    zipfile.ZipFile(REPO / "bundle.zip").extractall(REPO / "zip-out")
    tarfile.open(REPO / "bundle.tar").extractall(REPO / "tar-out")
    tempfile.NamedTemporaryFile(dir=REPO / "scratch")
"""

#: The false positive the `replace` arity rule must keep suppressing: a
#: substitution on a string read out of the tree is `str.replace`, not
#: `pathlib.Path.replace`.
_GUARD_STRING_REPLACE = """
REPO = pathlib.Path(__file__).resolve().parents[1]

def test_substitutes_in_memory():
    skill = REPO / ".agents/skills/release-cctally/SKILL.md"
    block = _overlap_block(skill.read_text())
    block = block.replace("marker", "touch marker")
    original = (REPO / "bin" / "leftover").read_bytes()
    trimmed = original.replace(b"a", b"b")
    return block, trimmed
"""

#: Reads, and writes outside the tree. Neither is the guard's business.
_GUARD_READS_AND_TMP_PATH = """
REPO = pathlib.Path(__file__).resolve().parents[1]

def test_reads_and_writes_elsewhere(tmp_path):
    with open(REPO / "bin" / "cctally-test-linux-matrix") as handle:
        handle.read()
    root = tmp_path / "synthetic"
    root.mkdir(parents=True)
    for name in ("a", "b"):
        (root / name).write_text("x")
"""

#: The hole the guard carried until the `replace` arity rule replaced the
#: `_reads_content` narrowing that caused it.
_GUARD_CONTENT_DERIVED_PATH = """
REPO = pathlib.Path(__file__).resolve().parents[1]

def test_writes_through_a_path_read_out_of_the_tree():
    target = pathlib.Path((REPO / "bin" / "pointer").read_text())
    target.write_text("mutated")
"""

#: The forms the docstring states are OUTSIDE the guard. Asserted so that the
#: stated coverage and the real coverage cannot drift apart silently.
_GUARD_UNCOVERED_FORMS = """
from os import remove
REPO = pathlib.Path(__file__).resolve().parents[1]

def test_forms_the_guard_does_not_reach():
    remove(REPO / "bin" / "leftover")
    subprocess.run(["rm", "-f", str(REPO / "bin" / "leftover")])
    os.close(os.open(REPO / "bin" / "leftover", os.O_WRONLY))
    shutil.copy(REPO / "bin" / "a", dst=REPO / "bin" / "b")
"""


def test_the_write_guard_names_the_original_transitively_rooted_offender():
    """The true positive the guard was built for, pinned against every later
    widening. A widening that stops naming this is worse than the blind spots
    it closes."""
    assert _repository_writers(_GUARD_TRANSITIVE_LOCAL) == [
        "test_image_records_change_when_the_dockerfile_changes:8"
        " dockerfile.write_bytes(original + b'# provoke a digest change')",
        "test_image_records_change_when_the_dockerfile_changes:10"
        " dockerfile.write_bytes(original)",
    ]


def test_the_write_guard_sees_a_module_level_path_constant():
    """Blind spot 1. `SCRIPT` is assigned at module level in this very file, so
    a collector that walks only function bodies cannot resolve it and reports
    nothing on the most likely future offender."""
    assert _repository_writers(_GUARD_MODULE_LEVEL_CONSTANT) == [
        "test_writes_through_the_module_constant:6 SCRIPT.write_text('mutated')",
    ]


def test_the_write_guard_sees_a_name_bound_by_a_loop_target():
    """Blind spot 3. `for path in REPO.glob(...)` binds no `ast.Assign`."""
    assert _repository_writers(_GUARD_LOOP_TARGET) == [
        "test_writes_through_a_loop_target:6 path.unlink()",
    ]


def test_the_write_guard_sees_open_and_the_free_function_mutators():
    """Blind spot 4. A mutator set of attribute methods alone leaves `open`,
    `os.remove` and `shutil.rmtree` entirely outside the guard."""
    assert _repository_writers(_GUARD_FREE_FUNCTION_MUTATORS) == [
        "test_writes_without_a_path_method:5"
        " open(REPO / 'bin' / 'cctally-test-linux-matrix', 'w')",
        "test_writes_without_a_path_method:7 os.remove(REPO / 'bin' / 'leftover')",
        "test_writes_without_a_path_method:8 shutil.rmtree(REPO / 'scratch')",
    ]


def test_the_write_guard_sees_both_other_spellings_of_open():
    """Blind spot 5. The `open` branch keyed on the unparsed text `open`, so
    the bound `(REPO / "x").open("w")` could not reach it at all, and the free
    form read the path from `args[0]` after reading the mode from `mode=`,
    which refuses `open(file=..., mode="w")` for having no positional path."""
    assert _repository_writers(_GUARD_OPEN_VARIANTS) == [
        "test_writes_through_the_other_two_spellings_of_open:5"
        " (REPO / 'bin' / 'cctally-test-linux-matrix').open('w')",
        "test_writes_through_the_other_two_spellings_of_open:7"
        " open(file=REPO / 'bin' / 'leftover', mode='a')",
    ]


def test_the_write_guard_sees_link_creation_in_the_tree():
    """Blind spot 6. `symlink_to` and `hardlink_to` create the path they are
    called on, and neither was in the mutator set."""
    assert _repository_writers(_GUARD_LINK_CREATORS) == [
        "test_creates_links_in_the_tree:5"
        " (REPO / 'bin' / 'soft').symlink_to(tmp_path / 'elsewhere')",
        "test_creates_links_in_the_tree:6"
        " (REPO / 'bin' / 'hard').hardlink_to(tmp_path / 'elsewhere')",
    ]


def test_the_write_guard_reports_a_nested_write_once_under_the_inner_function():
    """Two properties, and they need two corpora because one case cannot prove
    both.

    NAMING. `ast.walk(tree)` yields the nested function too, and `ast.walk`
    over the outer function descends into it, so the same call is reached
    twice. Only the outer scope resolves a name the inner function CLOSES OVER,
    so on `_GUARD_NESTED_FUNCTION` the inner visit resolves nothing and one
    visit reports whatever the collector does about duplicates. What that case
    pins is the name: `_innermost_owners` reports the finding under `_mutate`,
    the function the call is written in, not under the test that encloses it.

    DEDUPLICATION. `_GUARD_NESTED_MODULE_CONSTANT` is the case that exercises
    it. Every function's rooted set is SEEDED with the module's names, so
    `SCRIPT` resolves in the outer scope and in the inner one alike, both
    visits report the same call, and the finding appears twice unless `found`
    is keyed by `(lineno, col_offset)`.
    """
    assert _repository_writers(_GUARD_NESTED_FUNCTION) == [
        "_mutate:8 target.write_text('mutated')",
    ]
    assert _repository_writers(_GUARD_NESTED_MODULE_CONSTANT) == [
        "_mutate:7 SCRIPT.write_text('mutated')",
    ]


def test_the_write_guard_sees_the_os_free_spellings_of_the_path_methods():
    """P3-5. `_PATH_MUTATORS` gained `symlink_to` and `hardlink_to`, and the
    free spellings of the same mutations were not in `_FUNCTION_MUTATORS`.

    Six of the table's keys are dotted names whose attribute is itself a
    `_PATH_MUTATORS` entry, so this also pins the fall-through: an attribute
    test that ANSWERED instead of falling through would report none of
    `os.chmod`, `os.unlink`, `os.mkdir`, `os.rmdir`, `os.rename` or
    `os.replace`, because their receiver is the module `os`.

    The last line is the negative direction. `os.symlink(src, dst)` creates
    `dst` and requires `src` to exist, so a rooted SOURCE is a read.
    """
    assert _repository_writers(_GUARD_OS_FREE_MUTATORS) == [
        "test_writes_through_the_os_spellings:5"
        " os.chmod(REPO / 'bin' / 'cctally-test-linux-matrix', 493)",
        "test_writes_through_the_os_spellings:6"
        " os.utime(REPO / 'bin' / 'cctally-test-linux-matrix', None)",
        "test_writes_through_the_os_spellings:7"
        " os.unlink(REPO / 'bin' / 'leftover')",
        "test_writes_through_the_os_spellings:8"
        " os.mkdir(REPO / 'bin' / 'created')",
        "test_writes_through_the_os_spellings:9"
        " os.rmdir(REPO / 'bin' / 'obsolete')",
        "test_writes_through_the_os_spellings:10"
        " os.rename(REPO / 'bin' / 'before', tmp_path / 'after')",
        "test_writes_through_the_os_spellings:11"
        " os.replace(tmp_path / 'before', REPO / 'bin' / 'after')",
        "test_writes_through_the_os_spellings:12"
        " os.symlink(tmp_path / 'elsewhere', REPO / 'bin' / 'soft')",
        "test_writes_through_the_os_spellings:13"
        " os.link(tmp_path / 'elsewhere', REPO / 'bin' / 'hard')",
    ]


def test_the_write_guard_does_not_read_a_zip_member_name_as_a_writing_mode():
    """P3-2. The bound-`open` branch takes the mode from the FIRST positional
    argument, which is where `ZipFile.open` puts a member name instead. A flag
    test alone reads `member.txt` as a write, because it contains an `x`, and
    reports a read out of the tree as a repository mutation.
    """
    assert _repository_writers(_GUARD_NON_MODE_FIRST_ARGUMENT) == []


def test_the_write_guard_reports_neither_a_read_nor_a_write_outside_the_tree():
    """The widening must not start reporting a `str.replace` or a
    `bytes.replace` on content read out of the tree, an `open` for reading, or
    a write under `tmp_path`."""
    assert _repository_writers(_GUARD_STRING_REPLACE) == []
    assert _repository_writers(_GUARD_READS_AND_TMP_PATH) == []
    assert _repository_writers(_GUARD_OPEN_COMPLEMENT) == []


def test_the_write_guard_reaches_a_path_read_out_of_the_tree():
    """The hole #659's second review round closed.

    Rootedness used to stop at a content read, so a path whose value came out
    of a repository file escaped the guard. It no longer stops there, because
    the one collision that narrowing existed to suppress — `str.replace`
    against `pathlib.Path.replace` — is now told apart by arity instead:
    `Path.replace(target)` takes one positional argument and
    `str.replace(old, new[, count])` takes two or more.
    """
    assert _repository_writers(_GUARD_CONTENT_DERIVED_PATH) == [
        "test_writes_through_a_path_read_out_of_the_tree:6"
        " target.write_text('mutated')",
    ]


def test_the_write_guard_reports_none_of_the_forms_its_docstring_excludes():
    """The stated coverage, asserted against the code that implements it.

    A completeness claim broader than the guard is the #659 defect class in
    documentation form. These four forms are excluded by name in
    `_repository_writers`, and this pins the exclusion so that widening the
    guard to reach one of them fails here until the docstring is corrected too.
    """
    assert _repository_writers(_GUARD_UNCOVERED_FORMS) == []


#: Every file `_image_records` reads (bin/cctally-test-linux-matrix:211-254).
#: A synthetic root missing any one of them makes `_image_records` raise.
_IMAGE_INPUT_RELPATHS = (
    "bin/cctally-test-linux-matrix.Dockerfile",
    "bin/_lib-linux-matrix-manifest.sh",
    "bin/cctally-test-linux-matrix-sampler.py",
    "tests/requirements-dev.txt",
    "dashboard/web/package.json",
    "dashboard/web/package-lock.json",
    "dashboard/web/.nvmrc",
)


def _synthetic_image_root(tmp_path):
    """A copy of the real image inputs, which the test may then mutate.

    Reads the live tree ONCE and writes only under `tmp_path`. Nothing in this
    estate writes these files any more, which is what makes the single read
    safe; before #659 this module's own Dockerfile test did.
    """
    root = tmp_path / "image-root"
    for relative in _IMAGE_INPUT_RELPATHS:
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((REPO / relative).read_bytes())
    return root


def _pinned_records():
    """A fixed record list for tests about `_resolve_image`, not about inputs.

    Those tests concern immutable-ID selection, epoch rejection and build-once
    behaviour. Pinning the records here rather than `_image_input_digest` keeps
    the digest's framing and its link to the derived tag under test; inventory
    fidelity stays with `test_image_records_cover_every_input_...`.
    """
    return [("fixture", "stable"), ("python", "3.12"), ("apt-epoch", "2026-W34")]


def test_image_records_cover_every_input_that_can_change_the_image(tmp_path):
    gate = _load_gate()
    records = dict(gate._image_records("3.12", "2026-W34",
                                       _synthetic_image_root(tmp_path)))
    for required in (
        "schema", "python", "platform", "distro", "base", "dockerfile",
        # `manifest-script` and `sampler-script` were absent from this tuple
        # until #659, so the test named for covering every input did not.
        "manifest-script", "sampler-script",
        "requirements", "package-json", "package-lock", "nvmrc",
        "node-archive-sha256", "apt-packages", "sqlite-url", "sqlite-version",
        "sqlite-sha256", "sqlite-flags", "uid", "gid", "checkout", "apt-epoch",
    ):
        assert required in records, f"missing image input record: {required}"
    assert records["apt-epoch"] == "2026-W34"
    assert records["python"] == "3.12"


def test_image_records_change_when_the_dockerfile_changes(tmp_path):
    gate = _load_gate()
    root = _synthetic_image_root(tmp_path)
    before = gate._image_input_digest(gate._image_records("3.12", "2026-W34", root))
    dockerfile = root / "bin" / "cctally-test-linux-matrix.Dockerfile"
    dockerfile.write_bytes(dockerfile.read_bytes() + b"\n# provoke a digest change\n")
    after = gate._image_input_digest(gate._image_records("3.12", "2026-W34", root))
    assert before != after


def test_the_freshness_epoch_is_part_of_the_image_key(tmp_path):
    gate = _load_gate()
    root = _synthetic_image_root(tmp_path)
    this_week = gate._image_input_digest(gate._image_records("3.12", "2026-W34", root))
    next_week = gate._image_input_digest(gate._image_records("3.12", "2026-W35", root))
    assert this_week != next_week
    assert gate._image_tag("3.12", this_week) != gate._image_tag("3.12", next_week)


def _fake_inspect(payload):
    def _inspect(engine, reference):
        return payload
    return _inspect


def test_resolve_image_returns_the_immutable_id_not_the_tag(monkeypatch, tmp_path):
    gate = _load_gate()
    monkeypatch.setattr(gate, "_image_records", lambda *a, **k: _pinned_records())
    root = tmp_path
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


def test_resolve_image_refuses_an_image_whose_input_label_disagrees(monkeypatch,
                                                                    tmp_path):
    gate = _load_gate()
    monkeypatch.setattr(gate, "_image_records", lambda *a, **k: _pinned_records())
    root = tmp_path
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
    # `match=` because `_resolve_image` compares the digest at
    # bin/cctally-test-linux-matrix:553 and raises before the epoch check at
    # :558, so a bare `raises(GateError)` cannot tell the two branches apart.
    with pytest.raises(gate.GateError, match="records inputs"):
        gate._resolve_image("docker", "3.12", "2026-W34", root)


def test_resolve_image_refuses_an_epoch_outside_the_bound(monkeypatch, tmp_path):
    gate = _load_gate()
    monkeypatch.setattr(gate, "_image_records", lambda *a, **k: _pinned_records())
    root = tmp_path
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
    # The epoch message, not merely a `GateError`: the digest branch above runs
    # first, so without this the test was satisfied by the wrong refusal.
    with pytest.raises(gate.GateError, match="apt freshness epoch"):
        gate._resolve_image("docker", "3.12", "2026-W34", root)


def test_resolve_image_builds_exactly_once_when_the_derived_tag_is_missing(
        monkeypatch, tmp_path):
    gate = _load_gate()
    monkeypatch.setattr(gate, "_image_records", lambda *a, **k: _pinned_records())
    root = tmp_path
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
    # #648 D7 sanitizes PYTEST_ADDOPTS out of both pytest execution legs,
    # because `--ignore` through it narrowed an authoritative run that admission
    # had observed in full. That closed the route this lane used, so the
    # aggregator now recognises a variable that can only ADD a reporting flag.
    assert "CCTALLY_PYTEST_DURATIONS=1" in body
    assert "PYTEST_ADDOPTS" not in body
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
