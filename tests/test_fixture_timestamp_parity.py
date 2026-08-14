"""Production-format timestamp coverage for generated cache fixtures (#467)."""

from __future__ import annotations

import datetime as dt
import gc
import importlib.util
import sqlite3
import sys
import warnings
from pathlib import Path

import pytest


BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))

import _fixture_builders as fixtures  # noqa: E402


@pytest.fixture(autouse=True)
def _collect_fixture_builder_connections():
    """Collect helper-owned connections before pytest's unraisable check."""
    yield
    # The shared schema builders intentionally rely on their process-exit
    # closer so registered SQLite files can be header-normalized afterwards.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ResourceWarning)
        gc.collect()


def _load_builder(filename: str, module_name: str):
    spec = importlib.util.spec_from_file_location(
        module_name,
        BIN / filename,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cache_seeders_store_production_utc_offset(tmp_path: Path) -> None:
    cache_path = tmp_path / "cache.db"
    fixtures.create_cache_db(cache_path)

    with sqlite3.connect(cache_path) as conn:
        fixtures.seed_session_entry(
            conn,
            source_path="/fixture/claude.jsonl",
            line_offset=1,
            timestamp_utc="2026-04-20T12:00:00Z",
            model="claude-sonnet-4-6",
        )
        fixtures.seed_codex_session_entry(
            conn,
            source_path="/fixture/codex.jsonl",
            line_offset=2,
            timestamp_utc="2026-04-20T12:00:00Z",
            session_id="fixture-session",
            model="gpt-5",
        )
        claude_stamp = conn.execute(
            "SELECT timestamp_utc FROM session_entries"
        ).fetchone()[0]
        codex_stamp = conn.execute(
            "SELECT timestamp_utc FROM codex_session_entries"
        ).fetchone()[0]

    assert claude_stamp == "2026-04-20T12:00:00+00:00"
    assert codex_stamp == "2026-04-20T12:00:00+00:00"


def test_dashboard_codex_fixture_includes_exact_as_of_boundary(
    tmp_path: Path,
) -> None:
    builder = _load_builder(
        "build-dashboard-fixtures.py",
        "build_dashboard_fixtures_timestamp_test",
    )
    builder.FIXTURES_DIR = tmp_path
    as_of = dt.datetime(2026, 4, 20, 12, 0, tzinfo=dt.timezone.utc)
    builder._build_codex_cache_scenario(
        "boundary", as_of, day_offsets=[0]
    )

    cache_path = (
        tmp_path / "boundary" / ".local" / "share" / "cctally" / "cache.db"
    )
    with sqlite3.connect(cache_path) as conn:
        stamp = conn.execute(
            "SELECT timestamp_utc FROM codex_session_entries"
        ).fetchone()[0]
        upper_bound = (as_of + dt.timedelta(microseconds=1)).isoformat()
        included = conn.execute(
            "SELECT COUNT(*) FROM codex_session_entries WHERE timestamp_utc < ?",
            (upper_bound,),
        ).fetchone()[0]

    assert stamp == as_of.isoformat()
    assert included == 1


def test_direct_sql_builder_serializers_use_production_offset() -> None:
    stamp = dt.datetime(2026, 4, 20, 12, 0, tzinfo=dt.timezone.utc)
    for filename, module_name in (
        ("build-forecast-fixtures.py", "build_forecast_timestamp_test"),
        ("build-project-fixtures.py", "build_project_timestamp_test"),
        ("build-projects-fixtures.py", "build_projects_timestamp_test"),
    ):
        builder = _load_builder(filename, module_name)
        assert builder._iso(stamp) == stamp.isoformat()


def test_direct_sql_cache_builders_store_production_offset(
    tmp_path: Path,
) -> None:
    pricing = _load_builder(
        "build-pricing-check-fixtures.py",
        "build_pricing_check_timestamp_test",
    )
    pricing_path = tmp_path / "pricing.db"
    pricing._seed_cache(pricing_path, unpriced=True)
    with sqlite3.connect(pricing_path) as conn:
        assert conn.execute(
            "SELECT timestamp_utc FROM session_entries"
        ).fetchone()[0] == "2026-05-01T00:00:00+00:00"

    doctor = _load_builder(
        "build-doctor-fixtures.py",
        "build_doctor_timestamp_test",
    )
    doctor_path = tmp_path / "doctor.db"
    doctor._emit_codex_cache(doctor_path)
    with sqlite3.connect(doctor_path) as conn:
        assert conn.execute(
            "SELECT timestamp_utc FROM codex_session_entries"
        ).fetchone()[0].endswith("+00:00")

    quota = _load_builder(
        "build-codex-quota-fixtures.py",
        "build_codex_quota_timestamp_test",
    )
    quota_path = tmp_path / "quota.db"
    fixtures.create_cache_db(quota_path)
    with sqlite3.connect(quota_path) as conn:
        quota._seed_entries(
            conn,
            root="fixture-root",
            source_path="/fixture/codex.jsonl",
        )
        stamps = [
            row[0]
            for row in conn.execute(
                "SELECT timestamp_utc FROM codex_session_entries ORDER BY line_offset"
            )
        ]
    assert stamps == [
        "2026-07-15T10:00:00+00:00",
        "2026-07-15T11:00:00+00:00",
        "2026-07-15T11:30:00+00:00",
    ]


def test_source_timestamp_z_preserves_the_fraction() -> None:
    base = dt.datetime(2026, 4, 20, 12, 0, tzinfo=dt.timezone.utc)
    assert fixtures.fixture_source_timestamp_z(base) == "2026-04-20T12:00:00Z"
    assert fixtures.fixture_source_timestamp_z(
        base + dt.timedelta(microseconds=1)
    ) == "2026-04-20T12:00:00.000001Z"
    assert fixtures.fixture_source_timestamp_z(
        base.replace(microsecond=123000)
    ) == "2026-04-20T12:00:00.123000Z"
    assert fixtures.fixture_source_timestamp_z(
        base.replace(microsecond=123456)
    ) == "2026-04-20T12:00:00.123456Z"


def test_source_timestamp_z_normalizes_a_non_utc_offset() -> None:
    plus_two = dt.timezone(dt.timedelta(hours=2))
    assert fixtures.fixture_source_timestamp_z(
        dt.datetime(2026, 4, 20, 14, 0, tzinfo=plus_two)
    ) == "2026-04-20T12:00:00Z"


def test_source_timestamp_z_rejects_naive_and_non_datetime() -> None:
    with pytest.raises(ValueError):
        fixtures.fixture_source_timestamp_z(dt.datetime(2026, 4, 20, 12, 0))
    with pytest.raises(TypeError):
        fixtures.fixture_source_timestamp_z("2026-04-20T12:00:00Z")


_Z_FORM_BUILDERS = (
    "build-alerts-fixtures.py",
    "build-blocks-fixtures.py",
    "build-budget-fixtures.py",
    "build-cache-report-fixtures.py",
    "build-codex-fixtures.py",
    "build-daily-instances-fixtures.py",
    "build-dashboard-fixtures.py",
    "build-diff-fixtures.py",
    "build-mode-fixtures.py",
    "build-readme-fixtures.py",
    "build-session-fixtures.py",
    "build-weekly-fixtures.py",
)


@pytest.mark.parametrize("filename", _Z_FORM_BUILDERS)
def test_builder_iso_discriminates_one_microsecond(filename: str) -> None:
    """A sentinel one microsecond off a bound must not serialize ONTO it.

    #556 S2 seeded exactly such a sentinel to prove a half-open range excluded
    it; `strftime` truncated the fraction, the sentinel landed on the bound, the
    assertion passed for the wrong reason, and the golden recorded that pass.
    """
    module_name = filename.replace("-", "_").removesuffix(".py") + "_boundary_test"
    builder = _load_builder(filename, module_name)
    bound = dt.datetime(2026, 4, 20, 12, 0, tzinfo=dt.timezone.utc)
    just_past = bound + dt.timedelta(microseconds=1)

    assert builder._iso(just_past) == "2026-04-20T12:00:00.000001Z"
    assert builder._iso(just_past) != builder._iso(bound)
