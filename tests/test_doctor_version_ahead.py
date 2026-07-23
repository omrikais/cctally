"""doctor db.version_ahead check — pure kernel, no DB.

DB journal redesign §7.1: stats.db is a DISPOSABLE index stamped at
``STATS_INDEX_EPOCH`` (1000), NOT a versioned migration target. So the
version leg classifies stats.db by the EPOCH model, not "ahead of the
registry head":

  * ``user_version == epoch`` (a cut-over install)      → OK  (steady state)
  * ``user_version <= legacy_head`` (pre-cutover, ≤13)  → OK  (cuts over on open)
  * ``user_version > legacy_head`` AND ``!= epoch``      → WARN (§7.1 mismatch;
    self-heals by journal REBUILD on next open — remediation `db rebuild
    --db stats`, NOT the retired `db recover --db stats`).

cache.db is unchanged: user_version past the cache registry head → WARN
(auto-heals on next open).

`_lib_doctor` is imported off ``bin/`` — the same path mechanism the
in-process dashboard doctor tests use. No DB is opened: the check is a
pure function over a constructed ``DoctorState``. The gather layer
(`_cctally_doctor.doctor_gather_state`) injects ``epoch`` into
``stats_db_status``; the tests supply it directly.
"""
import pathlib
import sys

from conftest import load_script


# Make `import _lib_doctor` resolve from bin/ (mirrors test_doctor_dashboard.py).
sys.path.insert(0, str(pathlib.Path(load_script()["__file__"]).resolve().parent))


def _check(stats_uv, stats_rs, cache_uv, cache_rs, *, stats_epoch=1000):
    import _lib_doctor
    from dataclasses import fields

    kwargs = {f.name: None for f in fields(_lib_doctor.DoctorState)}
    kwargs["stats_db_status"] = {
        "user_version": stats_uv, "registry_size": stats_rs,
        "epoch": stats_epoch,
    }
    kwargs["cache_db_status"] = {"user_version": cache_uv, "registry_size": cache_rs}
    s = _lib_doctor.DoctorState(**kwargs)
    return _lib_doctor._check_db_version_ahead(s)


def test_epoch_install_is_healthy():
    # A cut-over install sits at user_version 1000 > legacy head 13 — that is
    # the HEALTHY steady state, not a version-ahead FAIL (the merge-blocking bug).
    r = _check(1000, 13, 2, 2)
    assert r.severity == "ok" and r.id == "db.version_ahead"
    assert r.details["stats.db"]["mismatch"] is False
    assert r.details["cache.db"]["ahead"] is False


def test_legacy_pre_cutover_is_healthy():
    # A pre-journal install (user_version 0 ≤ 13) cuts over on the next open —
    # not a problem to report.
    r = _check(0, 13, 2, 2)
    assert r.severity == "ok"
    assert r.details["stats.db"]["mismatch"] is False


def test_stats_epoch_mismatch_warns_and_points_at_rebuild():
    # user_version 99 (> 13 and ≠ 1000): a §7.1 index mismatch. It self-heals by
    # journal rebuild on next open — WARN, and the remediation is `db rebuild`,
    # never the retired `db recover --db stats`.
    r = _check(99, 13, 2, 2)
    assert r.severity == "warn"
    assert "db rebuild --db stats" in (r.remediation or "")
    assert "db recover --db stats" not in (r.remediation or "")
    assert r.details["stats.db"]["mismatch"] is True


def test_cache_ahead_warn():
    r = _check(1000, 13, 3, 2)
    assert r.severity == "warn"
    assert "auto-heals" in r.summary and "db recover --db cache" in (r.remediation or "")
    assert r.details["cache.db"]["ahead"] is True
    assert r.details["stats.db"]["mismatch"] is False


def test_stats_mismatch_dominates_cache_ahead():
    # Both are WARN now; the stats mismatch remediation wins (checked first).
    r = _check(99, 13, 3, 2)
    assert r.severity == "warn"
    assert "db rebuild --db stats" in (r.remediation or "")
    assert r.details["stats.db"]["mismatch"] is True
    assert r.details["cache.db"]["ahead"] is True
