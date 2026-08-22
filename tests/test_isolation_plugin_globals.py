"""#630 S2 — the process-global state detector, and the F7 reproduction."""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import threading
import types

from tests._pytest_isolation_plugin import (
    PROJECT_MANIFEST, STDLIB_MANIFEST, UNREACHABLE, _comparison_key,
)


def test_the_manifest_covers_both_halves():
    stdlib_targets = {name for name, _attr in STDLIB_MANIFEST}
    assert {"sqlite3", "subprocess", "os", "shutil", "json", "time",
            "threading", "sys"} <= stdlib_targets
    assert PROJECT_MANIFEST, (
        "an empty project manifest cannot answer F9: the five autouse reset "
        "fixtures restore project module state, not stdlib attributes"
    )


def test_the_project_manifest_names_the_reset_fixtures_targets():
    """F9 is answered by this half, so it must name real attributes.

    A manifest entry for an attribute that does not exist is silently skipped
    by the snapshot, so a typo would make the detector claim coverage it does
    not have.
    """
    import importlib

    for module_name, attr in PROJECT_MANIFEST:
        module = importlib.import_module(module_name)
        assert hasattr(module, attr), (
            f"{module_name}.{attr} does not exist, so the detector silently "
            f"covers nothing for it"
        )


def test_the_unreachable_list_is_stated_rather_than_empty():
    """The limitation is recorded, not hidden.

    Identity comparison cannot see a mutation inside an object whose identity
    is unchanged, and some reset targets are reached only through named reset
    functions. Those are listed rather than silently claimed as covered.
    """
    assert UNREACHABLE
    assert all(isinstance(entry, str) and entry.strip() for entry in UNREACHABLE)


def test_the_support_modules_own_process_global_is_registered():
    """The detector's estate includes the module the detector's tests import.

    `tests/_support_http.py` grew `_RESIDUE`, a module-level
    `WeakKeyDictionary` of unreturned socket bytes — a process-global in
    exactly the class this detector guards, added by the same session that
    wrote the detector, and named in neither manifest nor in the record of what
    the manifests cannot reach.

    It belongs in `UNREACHABLE` rather than in `PROJECT_MANIFEST`, and the
    reason is the point: a `WeakKeyDictionary` is not a `dict`, so
    `_comparison_key` would file it under identity, and its identity never
    changes because nothing rebinds the name. The entry would compare equal on
    every item forever — a manifest row that cannot fail, which is worse than
    an admitted gap because it reads as coverage.
    """
    named = [entry for entry in UNREACHABLE if "_RESIDUE" in entry]
    assert named, (
        "tests/_support_http._RESIDUE is a module-global this detector does "
        "not compare, and UNREACHABLE does not say so")


def _blind_spot_mismatch(project_keys, live_phase_keys, unreachable):
    """Why `UNREACHABLE` disagrees with what the code actually compares, or "".

    Reads in both directions on purpose. A manifest key the live phases skip
    must be admitted in `UNREACHABLE`; a manifest with nothing left out must
    not still carry the admission, or the record outlives the limitation it
    describes.
    """
    teardown_only = set(project_keys) - set(live_phase_keys)
    stated = [entry for entry in unreachable if "_LIVE_PHASE_KEYS" in entry]
    if teardown_only and not stated:
        return (f"{sorted(teardown_only)} are compared at teardown only, and "
                f"no UNREACHABLE entry mentions _LIVE_PHASE_KEYS, so the gap "
                f"is left implied by the code")
    if not teardown_only and stated:
        return ("every project-manifest key is now compared in the live "
                "phases, so the _LIVE_PHASE_KEYS entry in UNREACHABLE "
                "describes a limitation that no longer exists and must be "
                "deleted")
    return ""


def test_the_project_halfs_live_phase_blind_spot_is_recorded():
    """The record of what the detector cannot see must match what it does.

    This pins that the limitation is DOCUMENTED, not that it still exists. The
    earlier form asserted `not (set(PROJECT_MANIFEST) & _LIVE_PHASE_KEYS)`,
    which is a claim that the gap is still open: a later change that correctly
    extended live-phase comparison to the project half would have failed here,
    pushing its author to revert the improvement rather than delete the
    `UNREACHABLE` entry it retires.
    """
    from tests._pytest_isolation_plugin import _LIVE_PHASE_KEYS

    mismatch = _blind_spot_mismatch(
        PROJECT_MANIFEST, _LIVE_PHASE_KEYS, UNREACHABLE)
    assert mismatch == "", mismatch


def test_the_blind_spot_check_reads_in_both_directions():
    """Non-vacuity, and proof the check does not block the improvement.

    The first case is today's tree with the admission removed. The second is
    the improvement — every project key compared live — with the admission left
    behind. The third is that same improvement with the admission deleted,
    which must pass, because that is the state this check exists to permit.
    """
    keys = (("m", "a"),)
    entry = "compared at teardown only because _LIVE_PHASE_KEYS holds ..."

    assert _blind_spot_mismatch(keys, frozenset(), ()) != ""
    assert _blind_spot_mismatch(keys, frozenset(keys), (entry,)) != ""
    assert _blind_spot_mismatch(keys, frozenset(keys), ()) == ""
    assert _blind_spot_mismatch(keys, frozenset(), (entry,)) == ""


def test_an_equal_path_rebuilt_as_a_new_object_is_not_a_change():
    """A filesystem path is compared by where it points, not by identity.

    `_cctally_core.MIGRATION_ERROR_LOG_PATH` is re-derived as a fresh
    `pathlib.Path` whenever that module is re-initialised, which happens
    inside an ordinary `load_script()`. Comparing its identity reported a leak
    on every such item — a real observation, and a defect in the detector
    rather than in the test it blamed.
    """
    import pathlib as _pathlib

    a = _pathlib.Path("/tmp/one/two")
    b = _pathlib.Path("/tmp/one/two")
    assert a is not b
    assert _comparison_key(a) == _comparison_key(b)
    assert _comparison_key(a) != _comparison_key(_pathlib.Path("/tmp/one/three"))


def test_a_callable_is_still_compared_by_identity():
    """Non-vacuity for the rule above: F7's question is object identity."""
    def one():
        return 1

    def two():
        return 1

    assert _comparison_key(one) != _comparison_key(two)


def test_a_shared_stdlib_mutation_is_observed_by_a_concurrent_thread():
    """The F7 reproduction. Red under the shared mutation, green under a copy."""
    module = types.ModuleType("fake_importer")
    module.json = json

    armed, done = threading.Barrier(2), threading.Event()
    observed = {}

    def probe():
        armed.wait(timeout=10)
        observed["result"] = json.loads('{"ok": true}')
        done.set()

    worker = threading.Thread(target=probe, name="f7-probe", daemon=True)
    worker.start()

    original = module.json.loads
    try:
        # The DANGEROUS form: mutate the shared module object.
        module.json.loads = lambda *_a, **_k: {"poisoned": True}
        armed.wait(timeout=10)
        done.wait(timeout=10)
    finally:
        module.json.loads = original
    # timing-budget: the F7 probe thread has returned now that `done` is set
    worker.join(timeout=30)

    assert observed["result"] == {"poisoned": True}, (
        "this assertion documents the defect: an unrelated thread saw the "
        "patch, which is why every site is repaired"
    )


def test_the_importer_local_copy_is_invisible_to_a_concurrent_thread():
    """The GREEN half: the same scenario under the safe pattern."""
    module = types.ModuleType("fake_importer")
    module.json = json

    armed, done = threading.Barrier(2), threading.Event()
    observed = {}

    def probe():
        armed.wait(timeout=10)
        observed["result"] = json.loads('{"ok": true}')
        done.set()

    worker = threading.Thread(target=probe, name="f7-probe-safe", daemon=True)
    worker.start()

    local = types.SimpleNamespace(**vars(json))
    local.loads = lambda *_a, **_k: {"poisoned": True}
    module.json = local
    armed.wait(timeout=10)
    done.wait(timeout=10)
    # timing-budget: the safe-pattern probe thread has returned now that `done` is set
    worker.join(timeout=30)

    assert observed["result"] == {"ok": True}
    assert module.json.loads("{}") == {"poisoned": True}


LEAKY_GLOBAL = textwrap.dedent('''
    import sqlite3

    def test_this_one_rebinds_a_shared_stdlib_callable():
        sqlite3.connect = lambda *a, **k: None
''')

SAFE_GLOBAL = textwrap.dedent('''
    import sqlite3
    import types

    def test_this_one_rebinds_on_the_importer_instead():
        local = types.SimpleNamespace(**vars(sqlite3))
        local.connect = lambda *a, **k: None
        assert local.connect() is None
        assert sqlite3.connect is not local.connect
''')

INSTALLED_GLOBAL = textwrap.dedent('''
    import sqlite3

    def test_an_ordinary_monkeypatch_is_installed_while_the_body_runs(monkeypatch):
        monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: None)
        assert sqlite3.connect() is None
''')

FIXTURE_INSTALLED_GLOBAL = textwrap.dedent('''
    import sqlite3

    import pytest


    @pytest.fixture
    def patched(monkeypatch):
        monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: None)
        yield


    def test_a_fixture_installed_patch_is_live_before_the_body_starts(patched):
        assert sqlite3.connect() is None
''')


def _run_child(tmp_path, source, name):
    test_file = tmp_path / name
    test_file.write_text(source, encoding="utf-8")
    return subprocess.run(
        [sys.executable, "-m", "pytest", str(test_file), "-p",
         "tests._pytest_isolation_plugin", "-q", "-p", "no:randomly"],
        capture_output=True, text=True, timeout=120,
    )


def test_a_residual_stdlib_rebind_fails_its_own_test(tmp_path):
    result = _run_child(tmp_path, LEAKY_GLOBAL, "test_child_global.py")
    combined = result.stdout + result.stderr
    assert result.returncode == 1, combined
    assert "test_this_one_rebinds_a_shared_stdlib_callable" in combined
    assert "sqlite3.connect" in combined


def test_the_safe_importer_local_rebind_is_not_reported(tmp_path):
    """The detector must not flag the pattern every repair adopts."""
    result = _run_child(tmp_path, SAFE_GLOBAL, "test_child_safe.py")
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "1 passed" in combined


def test_an_ordinary_reverting_monkeypatch_is_reported_at_the_call_phase(tmp_path):
    """The F7 class IS an ordinary reverting monkeypatch, so it must be seen.

    `monkeypatch` reverts before the teardown report is built, so a detector
    that only compares after teardown cannot observe this at all — and this is
    the exact shape of the class the repairs address. The call phase compares
    after the test body has run and before any finalizer, which is the window
    in which every concurrent thread in the process sees the patched object.
    """
    result = _run_child(tmp_path, INSTALLED_GLOBAL, "test_child_installed.py")
    combined = result.stdout + result.stderr
    assert result.returncode == 1, combined
    assert "test_an_ordinary_monkeypatch_is_installed_while_the_body_runs" in combined
    assert "sqlite3.connect" in combined
    assert "call phase" in combined, (
        "a call-phase finding and a teardown finding call for different "
        "repairs, so the phase must be named"
    )


def test_a_fixture_installed_patch_is_reported_at_the_setup_phase(tmp_path):
    """The setup phase is a report, not only a baseline.

    A patch a fixture installs is live for the whole test body too, and it is
    invisible to a call-phase comparison taken against the post-setup snapshot.
    Without this comparison the fixture form of the F7 class is undetected.
    """
    result = _run_child(tmp_path, FIXTURE_INSTALLED_GLOBAL,
                        "test_child_fixture.py")
    combined = result.stdout + result.stderr
    assert result.returncode == 1, combined
    assert "test_a_fixture_installed_patch_is_live_before_the_body_starts" in combined
    assert "sqlite3.connect" in combined
    assert "setup phase" in combined


def test_the_snapshot_never_imports_a_module_that_was_absent(monkeypatch, tmp_path):
    """The detector must observe the process, not change it.

    Importing a manifest target that is not loaded yet makes the plugin the
    FIRST importer of it, and some of those module bodies do real work —
    `_cctally_core`'s calls `_init_paths_from_env()`. In the load lane the
    plugin runs at every setup and teardown, so it would win that race against
    the item's own `load_script()`. Reading `sys.modules` answers the same
    question without touching the process, and it also makes the
    "target absent, so skipped" branch honest.
    """
    from tests import _pytest_isolation_plugin as plugin

    (tmp_path / "_iso_probe_module.py").write_text("MARKER = 1\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    assert "_iso_probe_module" not in sys.modules, "precondition"

    # The manifest is passed rather than monkeypatched onto the module: this
    # plugin reads its module-level manifest on every hook of every item, so
    # shrinking it mid-test makes every real target read as absent.
    seen = plugin._snapshot_state((("_iso_probe_module", "MARKER"),))

    assert "_iso_probe_module" not in sys.modules, (
        "the snapshot imported a module that was not loaded, so the detector "
        "changed the process it is supposed to be observing")
    assert ("_iso_probe_module", "MARKER") not in seen, (
        "an unloaded target must be recorded as uncovered, not covered")
