"""doctor db.version_ahead check (#145) — pure kernel, no DB.

The check classifies a DB whose ``user_version`` exceeds the running
binary's registry head: stats.db ahead → FAIL (bricks commands),
cache.db-ahead-only → WARN (auto-heals on next open), neither → OK.

`_lib_doctor` is imported off ``bin/`` — the same path mechanism the
in-process dashboard doctor tests use (load_script() exposes the
bin/cctally ``__file__``; its parent is ``bin/``). No DB is opened: the
check is a pure function over a constructed ``DoctorState``.
"""
import pathlib
import sys

from conftest import load_script


# Make `import _lib_doctor` resolve from bin/ (mirrors test_doctor_dashboard.py).
sys.path.insert(0, str(pathlib.Path(load_script()["__file__"]).resolve().parent))


def _check(stats_uv, stats_rs, cache_uv, cache_rs):
    import _lib_doctor
    from dataclasses import fields

    kwargs = {f.name: None for f in fields(_lib_doctor.DoctorState)}
    kwargs["stats_db_status"] = {"user_version": stats_uv, "registry_size": stats_rs}
    kwargs["cache_db_status"] = {"user_version": cache_uv, "registry_size": cache_rs}
    s = _lib_doctor.DoctorState(**kwargs)
    return _lib_doctor._check_db_version_ahead(s)


def test_none_ahead_ok():
    r = _check(10, 10, 2, 2)
    assert r.severity == "ok" and r.id == "db.version_ahead"
    assert r.details["stats.db"]["ahead"] is False
    assert r.details["cache.db"]["ahead"] is False


def test_cache_ahead_warn():
    r = _check(10, 10, 3, 2)
    assert r.severity == "warn"
    assert "auto-heals" in r.summary and "db recover --db cache" in (r.remediation or "")
    assert r.details["cache.db"]["ahead"] is True
    assert r.details["stats.db"]["ahead"] is False


def test_stats_ahead_fail():
    r = _check(11, 10, 2, 2)
    assert r.severity == "fail"
    assert "db recover --db stats --yes" in (r.remediation or "")
    assert r.details["stats.db"]["ahead"] is True


def test_stats_ahead_dominates_cache():
    r = _check(11, 10, 3, 2)
    assert r.severity == "fail"
    assert "db recover --db stats --yes" in (r.remediation or "")
