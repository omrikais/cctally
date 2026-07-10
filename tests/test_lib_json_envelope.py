"""_lib_json_envelope kernel (#279 S6 W1): the schemaVersion stamp helper +
canonical None-safe UTC-Z serializer.

stamp_schema_version() is the single chokepoint for the additive camelCase
``schemaVersion: 1`` envelope adopted across reporting ``--json`` surfaces.
Contract (spec gate F6): always returns a NEW shallow copy, never mutates
its input, inserts the key FIRST when absent, and is a value- AND
order-preserving no-op when the key is already present.

_iso_z() is the canonical None-safe seconds-precision UTC-Z serializer
(the forecast/dashboard-envelope behavior; _lib_doctor._iso_z deliberately
diverges — naive=UTC + microseconds — see its docstring).
"""
import datetime as dt
import importlib.util
import pathlib

import pytest

BIN = pathlib.Path(__file__).resolve().parents[1] / "bin"


def _load_env():
    spec = importlib.util.spec_from_file_location(
        "_lib_json_envelope", BIN / "_lib_json_envelope.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def env():
    return _load_env()


def test_stamp_inserts_first(env):
    out = env.stamp_schema_version({"a": 1, "b": 2})
    assert list(out.keys()) == ["schemaVersion", "a", "b"]
    assert out["schemaVersion"] == 1


def test_stamp_present_is_order_and_value_preserving_copy(env):
    src = {"a": 1, "schemaVersion": 7, "b": 2}
    out = env.stamp_schema_version(src)
    assert out == src
    assert list(out.keys()) == list(src.keys())
    assert out is not src


def test_stamp_never_mutates_input(env):
    src = {"a": 1}
    env.stamp_schema_version(src)
    assert "schemaVersion" not in src


def test_stamp_custom_version_and_key(env):
    out = env.stamp_schema_version({}, version=3, key="schema_version")
    assert out == {"schema_version": 3}


def test_iso_z_none_safe(env):
    assert env._iso_z(None) is None


def test_iso_z_utc_seconds_precision(env):
    d = dt.datetime(2026, 7, 10, 12, 0, 5, 123456, tzinfo=dt.timezone.utc)
    assert env._iso_z(d) == "2026-07-10T12:00:05Z"


def test_iso_z_converts_aware_non_utc(env):
    tz = dt.timezone(dt.timedelta(hours=3))
    d = dt.datetime(2026, 7, 10, 15, 0, 0, tzinfo=tz)
    assert env._iso_z(d) == "2026-07-10T12:00:00Z"
