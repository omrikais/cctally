"""Tests for the golden-harness fixture cache (#281 S11 / R9).

Drives bin/_fixture_cache.py: the pure key core (Task 1), the cache-entry
engine + `run` dispatch (Task 2), and the VERIFY audit mode (Task 3). Loaded
via importlib so bin/ need not be on sys.path.
"""
import importlib.util
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"
spec = importlib.util.spec_from_file_location("_fixture_cache", BIN / "_fixture_cache.py")
fc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fc)

WEEKLY = BIN / "build-weekly-fixtures.py"


# --------------------------------------------------------------------------
# Task 1 — pure key core
# --------------------------------------------------------------------------

def test_label_for():
    assert fc.label_for(WEEKLY) == "weekly"
    assert fc.label_for(BIN / "build-codex-fixtures.py") == "codex"


def test_transitive_scan_includes_builder_and_fixture_builders():
    srcs = {p.name for p in fc.transitive_bin_sources(WEEKLY)}
    assert "build-weekly-fixtures.py" in srcs
    assert "_fixture_builders.py" in srcs  # imported by the builder


def test_transitive_scan_is_deterministic_and_sorted():
    a = fc.transitive_bin_sources(WEEKLY)
    assert a == sorted(a) and a == fc.transitive_bin_sources(WEEKLY)


def test_key_stable_and_sensitive():
    kw = dict(sqlite_version="3.45.0", compile_options=("ENABLE_FTS5",),
              fts5_available=True, python_id="cpython-311|(3, 11, 9)", env={})
    base = fc.compute_key(WEEKLY, **kw)
    assert base == fc.compute_key(WEEKLY, **kw)                       # stable
    assert base != fc.compute_key(WEEKLY, **{**kw, "sqlite_version": "3.46.0"})
    assert base != fc.compute_key(WEEKLY, **{**kw, "fts5_available": False})
    assert base != fc.compute_key(WEEKLY, **{**kw, "compile_options": ()})
    assert base != fc.compute_key(WEEKLY, **{**kw, "python_id": "x"})


def test_key_sensitive_to_transitive_import(tmp_path):
    # Copy the builder + its bin/-local import graph, mutate a TRANSITIVE dep,
    # assert the key changes — proves the AST recursion covers imports.
    import shutil
    stage = tmp_path / "bin"; stage.mkdir()
    for p in fc.transitive_bin_sources(WEEKLY):
        shutil.copy2(p, stage / p.name)
    # Point the module's BIN_DIR resolution at the stage by copying the module too:
    shutil.copy2(BIN / "_fixture_cache.py", stage / "_fixture_cache.py")
    spec2 = importlib.util.spec_from_file_location("_fc2", stage / "_fixture_cache.py")
    fc2 = importlib.util.module_from_spec(spec2); spec2.loader.exec_module(fc2)
    b = stage / "build-weekly-fixtures.py"
    kw = dict(sqlite_version="3.45.0", compile_options=(), fts5_available=True,
              python_id="p", env={})
    before = fc2.compute_key(b, **kw)
    (stage / "_fixture_builders.py").write_bytes(
        (stage / "_fixture_builders.py").read_bytes() + b"\n# mutate\n")
    assert fc2.compute_key(b, **kw) != before


# --------------------------------------------------------------------------
# Task 1 (#529 S4) — the environment policy map and the key's environment leg
# --------------------------------------------------------------------------

_SEMANTIC = ("CCTALLY_DISABLE_DEV_AUTODETECT", "LANG", "LC_ALL", "LC_CTYPE", "TZ")
_OPERATIONAL = ("HOME", "PATH", "TEMP", "TMP", "TMPDIR")


def _kw(env=None):
    return dict(sqlite_version="3.45.0", compile_options=("A",),
                fts5_available=True, python_id="cpython-313",
                env=dict(env or {}))


@pytest.mark.parametrize("name", _SEMANTIC)
def test_a_semantic_variable_changes_the_key(name):
    base = fc.compute_key(WEEKLY, **_kw({name: "one"}))
    other = fc.compute_key(WEEKLY, **_kw({name: "two"}))
    assert base != other, f"{name} is semantic but did not change the key"


@pytest.mark.parametrize("name", _SEMANTIC)
def test_unset_and_empty_are_different_keys(name):
    unset = fc.compute_key(WEEKLY, **_kw({}))
    empty = fc.compute_key(WEEKLY, **_kw({name: ""}))
    assert unset != empty, f"{name} unset and empty collapsed to one key"


@pytest.mark.parametrize("name", _OPERATIONAL)
def test_an_operational_variable_leaves_the_key_alone(name):
    base = fc.compute_key(WEEKLY, **_kw({name: "/one"}))
    other = fc.compute_key(WEEKLY, **_kw({name: "/a/much/longer/two"}))
    assert base == other, f"{name} is operational but changed the key"


def test_the_policy_map_matches_a_literal_expectation():
    # The literal is written out here on purpose: an eleventh forwarded
    # variable must fail this test until a human classifies it. Comparing
    # against the IMPORTED map is what gives the literal something to
    # disagree with.
    expected = {n: "semantic" for n in _SEMANTIC}
    expected.update({n: "operational" for n in _OPERATIONAL})
    assert fc._ENV_POLICY == expected


def test_the_forward_set_is_derived_from_the_policy_map():
    assert tuple(sorted(fc._ENV_POLICY)) == fc._ENV_KEEP


def test_sanitized_env_forwards_exactly_the_policy_keys(monkeypatch):
    # `_sanitized_env` keeps only names present in os.environ, so a name it
    # forwards but this process does not set would be invisible to an
    # assertion made against the real environment. Under a mapping that
    # reports every name as present, the result is exactly the set of names
    # the function iterates, which is what makes this a derivation check:
    # widening the loop source without touching _ENV_POLICY fails here.
    class _EveryNamePresent(dict):
        def __contains__(self, key):
            return True

        def __getitem__(self, key):
            return "v"

    monkeypatch.setattr(os, "environ", _EveryNamePresent())
    assert set(fc._sanitized_env()) == set(fc._ENV_POLICY)


def test_the_format_version_was_bumped_for_the_key_change():
    assert fc.CACHE_FORMAT_VERSION == 3


# --------------------------------------------------------------------------
# Task 2 (#529 S4) — the operational builder-contract differential
# --------------------------------------------------------------------------

REPO_ROOT = Path(fc.__file__).resolve().parent.parent


def _cache_wired_builders(repo_root):
    """Every builder invoked through build_fixtures_cached anywhere in bin/."""
    import re
    names = set()
    for script in (repo_root / "bin").iterdir():
        if not script.is_file():
            continue
        try:
            text = script.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        names.update(re.findall(
            r"build_fixtures_cached\s+\S*?/bin/(build-[a-z0-9-]+-fixtures\.py)",
            text))
    assert names, "found no cache-wired builders — the scan predicate is wrong"
    return sorted(names)


def _operational_differential(builder, scratch):
    """Build `builder` twice under structurally different operational
    environments and return the two output manifests.

    All five operational variables are perturbed. Arm B's PATH is an empty
    directory ALONE, not the inherited PATH with something prepended: a PATH
    that differs only as a string is observable just to a builder that copies
    it into its output, whereas a builder that RESOLVES a tool through PATH —
    the leak class this contract exists to exclude — reveals itself only when
    the resolution fails. The values also differ in length and directory
    depth, because two scratch roots of equal length can produce
    byte-identical output while a leak is genuinely present.
    """
    trees = []
    for tag, depth, inherit_path in (("a", "s", True),
                                     ("bb", "deep/deeper/deepest", False)):
        root = scratch / tag / depth
        (root / "home").mkdir(parents=True)
        (root / "tmp").mkdir(parents=True)
        (root / "bin").mkdir(parents=True)
        out = root / "out"
        path = str(root / "bin")
        if inherit_path:
            path = os.pathsep.join(
                [path, os.environ.get("PATH", "/usr/bin:/bin")])
        env = {
            "HOME": str(root / "home"),
            "TMPDIR": str(root / "tmp"),
            "TMP": str(root / "tmp"),
            "TEMP": str(root / "tmp"),
            "PATH": path,
            "TZ": "Etc/UTC",
            "LANG": "C", "LC_ALL": "C", "LC_CTYPE": "C",
            "CCTALLY_DISABLE_DEV_AUTODETECT": "1",
        }
        proc = subprocess.run([sys.executable, str(builder), "--out", str(out)],
                              env=env, stdout=subprocess.DEVNULL,
                              stderr=subprocess.PIPE, text=True)
        assert proc.returncode == 0, (
            f"{builder.name} failed under a sanitized environment:\n{proc.stderr}")
        trees.append(fc.build_manifest(out))
    return trees


@pytest.mark.parametrize("name", _cache_wired_builders(REPO_ROOT))
def test_no_cache_wired_builder_leaks_an_operational_variable(name, tmp_path):
    """The operational half of _ENV_POLICY is a CONTRACT ON BUILDERS, not a
    property of the variables. Nothing else enforces it, so this differential
    is what stops the key being sound only by assertion.

    Parametrized per builder so one leaking builder does not mask the rest.
    """
    first, second = _operational_differential(
        REPO_ROOT / "bin" / name, tmp_path)
    assert first == second, (
        f"{name} produced different output trees under two sanitized "
        f"environments. Either it reads an operational variable "
        f"(HOME/PATH/TMPDIR/TMP/TEMP) or it is not relocatable — the two arms "
        f"also differ in their --out path. Both disqualify it from the cache.")


def test_the_operational_differential_detects_a_deliberate_leak(tmp_path):
    """Proves the tripwire above can go red at all.

    A differential that produced identical manifests because the builders
    never ran, or because build_manifest ignored the difference, would be
    indistinguishable from a genuinely clean estate.
    """
    leaky = tmp_path / "build-leaky-fixtures.py"
    leaky.write_text(textwrap.dedent('''\
        #!/usr/bin/env python3
        import argparse, os, pathlib
        p = argparse.ArgumentParser(); p.add_argument("--out", type=pathlib.Path)
        a = p.parse_args(); a.out.mkdir(parents=True, exist_ok=True)
        (a.out / "leak.txt").write_text(os.environ["HOME"])
    '''))
    os.chmod(leaky, 0o755)
    first, second = _operational_differential(leaky, tmp_path / "run")
    assert first != second, (
        "the differential did not observe a builder that writes $HOME into "
        "its own output, so it cannot observe a real leak either")


def test_the_differential_observes_a_path_resolved_tool(tmp_path):
    """Proves what arm B's empty-directory PATH buys.

    Prepending a directory to the inherited PATH leaves every tool
    resolvable, so a builder that reaches an external tool THROUGH PATH
    renders identically in both arms and the differential stays green over a
    real dependency. This builder resolves a tool rather than copying $PATH
    into its output, which is the leak class the operational class exists to
    exclude and the one an inherited-PATH perturbation cannot see.
    """
    resolver = tmp_path / "build-resolver-fixtures.py"
    resolver.write_text(textwrap.dedent('''\
        #!/usr/bin/env python3
        import argparse, pathlib, shutil
        p = argparse.ArgumentParser(); p.add_argument("--out", type=pathlib.Path)
        a = p.parse_args(); a.out.mkdir(parents=True, exist_ok=True)
        (a.out / "tool.txt").write_text(str(shutil.which("env")))
    '''))
    os.chmod(resolver, 0o755)
    first, second = _operational_differential(resolver, tmp_path / "run")
    assert first != second, (
        "the differential did not observe a builder that resolves a tool "
        "through PATH, so arm B is still leaving PATH effectively inherited")


# --------------------------------------------------------------------------
# Task 2 — cache entry engine + run dispatch
# --------------------------------------------------------------------------

def _mini_builder(tmp_path, marker=b"hello"):
    b = tmp_path / "build-mini-fixtures.py"
    b.write_text(textwrap.dedent(f'''\
        #!/usr/bin/env python3
        import argparse, pathlib
        p = argparse.ArgumentParser(); p.add_argument("--out", type=pathlib.Path)
        a = p.parse_args(); a.out.mkdir(parents=True, exist_ok=True)
        (a.out / "scenario").mkdir(exist_ok=True)
        (a.out / "scenario" / "data.db").write_bytes({marker!r})
    '''))
    os.chmod(b, 0o755); return b


def _run(fc, builder, out, **env):
    # Strip any ambient cache-control vars so each test controls exactly the
    # knobs it passes — immune to `CCTALLY_FIXTURE_CACHE=0` (or _VERIFY/_DIR)
    # exported suite-wide by the caller (e.g. cache-off cctally-test-all).
    e = {k: v for k, v in os.environ.items()
         if k not in ("CCTALLY_FIXTURE_CACHE", "CCTALLY_FIXTURE_CACHE_DIR",
                      "CCTALLY_FIXTURE_CACHE_VERIFY")}
    e.update(env)
    r = subprocess.run([sys.executable, str(BIN / "_fixture_cache.py"),
                        "run", "--builder", str(builder), "--out", str(out)],
                       env=e, capture_output=True, text=True)
    return r


def test_bypass(tmp_path):
    b = _mini_builder(tmp_path); out = tmp_path / "o"; cache = tmp_path / "c"
    r = _run(fc, b, out, CCTALLY_FIXTURE_CACHE="0",
             CCTALLY_FIXTURE_CACHE_DIR=str(cache))
    assert r.returncode == 0 and "BYPASS mini" in r.stderr
    assert (out / "scenario" / "data.db").read_bytes() == b"hello"
    assert not cache.exists() or not any(cache.iterdir())


def test_cold_miss_then_warm_hit(tmp_path):
    b = _mini_builder(tmp_path); cache = tmp_path / "c"
    o1 = tmp_path / "o1"
    r1 = _run(fc, b, o1, CCTALLY_FIXTURE_CACHE_DIR=str(cache))
    assert r1.returncode == 0 and "MISS mini" in r1.stderr
    o2 = tmp_path / "o2"
    r2 = _run(fc, b, o2, CCTALLY_FIXTURE_CACHE_DIR=str(cache))
    assert r2.returncode == 0 and "HIT mini" in r2.stderr
    assert (o2 / "scenario" / "data.db").read_bytes() == b"hello"


def test_failed_build_not_cached(tmp_path):
    b = tmp_path / "build-boom-fixtures.py"
    b.write_text("#!/usr/bin/env python3\nimport sys; sys.exit(7)\n"); os.chmod(b, 0o755)
    cache = tmp_path / "c"
    r = _run(fc, b, tmp_path / "o", CCTALLY_FIXTURE_CACHE_DIR=str(cache))
    assert r.returncode == 7
    assert not cache.exists() or not any(cache.glob("boom__*"))


# --------------------------------------------------------------------------
# Task 3 — VERIFY audit mode
# --------------------------------------------------------------------------

def test_poison_normal_rebuilds(tmp_path):
    b = _mini_builder(tmp_path); cache = tmp_path / "c"
    _run(fc, b, tmp_path / "o1", CCTALLY_FIXTURE_CACHE_DIR=str(cache))
    entry = next(p for p in cache.glob("mini__*") if p.is_dir())  # not the .lock sibling
    victim = entry / "scenario" / "data.db"; victim.write_bytes(b"CORRUPT")
    r = _run(fc, b, tmp_path / "o2", CCTALLY_FIXTURE_CACHE_DIR=str(cache))
    assert r.returncode == 0 and "POISONED mini" in r.stderr
    assert (tmp_path / "o2" / "scenario" / "data.db").read_bytes() == b"hello"


def test_poison_verify_is_red(tmp_path):
    b = _mini_builder(tmp_path); cache = tmp_path / "c"
    _run(fc, b, tmp_path / "o1", CCTALLY_FIXTURE_CACHE_DIR=str(cache))
    entry = next(p for p in cache.glob("mini__*") if p.is_dir())  # not the .lock sibling
    (entry / "scenario" / "data.db").write_bytes(b"CORRUPT")
    r = _run(fc, b, tmp_path / "o2", CCTALLY_FIXTURE_CACHE_DIR=str(cache),
             CCTALLY_FIXTURE_CACHE_VERIFY="1")
    assert r.returncode == 3 and "AUDIT FAILURE" in r.stderr


def test_verify_clean_hit_passes(tmp_path):
    b = _mini_builder(tmp_path); cache = tmp_path / "c"
    _run(fc, b, tmp_path / "o1", CCTALLY_FIXTURE_CACHE_DIR=str(cache))
    r = _run(fc, b, tmp_path / "o2", CCTALLY_FIXTURE_CACHE_DIR=str(cache),
             CCTALLY_FIXTURE_CACHE_VERIFY="1")
    assert r.returncode == 0 and "HIT mini" in r.stderr


def test_verify_detects_nonrelocatable(tmp_path):
    # A builder that embeds its --out path is caught by the clean-hit audit.
    b = tmp_path / "build-badreloc-fixtures.py"
    b.write_text(textwrap.dedent('''\
        #!/usr/bin/env python3
        import argparse, pathlib
        p = argparse.ArgumentParser(); p.add_argument("--out", type=pathlib.Path)
        a = p.parse_args(); a.out.mkdir(parents=True, exist_ok=True)
        (a.out / "path.txt").write_text(str(a.out.resolve()))
    '''))
    os.chmod(b, 0o755); cache = tmp_path / "c"
    _run(fc, b, tmp_path / "o1", CCTALLY_FIXTURE_CACHE_DIR=str(cache))
    r = _run(fc, b, tmp_path / "o2", CCTALLY_FIXTURE_CACHE_DIR=str(cache),
             CCTALLY_FIXTURE_CACHE_VERIFY="1")
    assert r.returncode == 3 and "AUDIT FAILURE" in r.stderr
