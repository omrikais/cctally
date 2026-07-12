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
              fts5_available=True, python_id="cpython-311|(3, 11, 9)")
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
    kw = dict(sqlite_version="3.45.0", compile_options=(), fts5_available=True, python_id="p")
    before = fc2.compute_key(b, **kw)
    (stage / "_fixture_builders.py").write_bytes(
        (stage / "_fixture_builders.py").read_bytes() + b"\n# mutate\n")
    assert fc2.compute_key(b, **kw) != before


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
