"""Unit tests for the oauth_usage config block helpers."""
import pytest
from conftest import load_script


@pytest.fixture(scope="module")
def ns():
    return load_script()


def test_defaults_when_block_absent(ns):
    fn = ns["_get_oauth_usage_config"]
    cfg = fn({})
    assert cfg == {
        "user_agent": None,
        "throttle_seconds": 15,
        "fresh_threshold_seconds": 30,
        "stale_after_seconds": 90,
    }


def test_defaults_when_block_is_null(ns):
    fn = ns["_get_oauth_usage_config"]
    cfg = fn({"oauth_usage": None})
    assert cfg["throttle_seconds"] == 15
    assert cfg["user_agent"] is None


def test_explicit_overrides_pass_through(ns):
    fn = ns["_get_oauth_usage_config"]
    cfg = fn({"oauth_usage": {
        "user_agent": "cctally/0.1",
        "throttle_seconds": 60,
        "fresh_threshold_seconds": 45,
        "stale_after_seconds": 120,
    }})
    assert cfg == {
        "user_agent": "cctally/0.1",
        "throttle_seconds": 60,
        "fresh_threshold_seconds": 45,
        "stale_after_seconds": 120,
    }


def test_throttle_below_min_rejected(ns):
    fn = ns["_get_oauth_usage_config"]
    err_cls = ns["OauthUsageConfigError"]
    with pytest.raises(err_cls) as exc:
        fn({"oauth_usage": {"throttle_seconds": 4}})
    assert "throttle_seconds" in str(exc.value)


def test_throttle_above_max_rejected(ns):
    fn = ns["_get_oauth_usage_config"]
    err_cls = ns["OauthUsageConfigError"]
    with pytest.raises(err_cls):
        fn({"oauth_usage": {"throttle_seconds": 601}})


def test_fresh_not_less_than_stale_rejected(ns):
    fn = ns["_get_oauth_usage_config"]
    err_cls = ns["OauthUsageConfigError"]
    with pytest.raises(err_cls) as exc:
        fn({"oauth_usage": {
            "fresh_threshold_seconds": 90,
            "stale_after_seconds": 90,
        }})
    assert "fresh_threshold_seconds" in str(exc.value)


def test_fresh_strictly_greater_than_stale_rejected(ns):
    fn = ns["_get_oauth_usage_config"]
    with pytest.raises(ns["OauthUsageConfigError"]):
        fn({"oauth_usage": {"fresh_threshold_seconds": 100, "stale_after_seconds": 90}})


def test_user_agent_empty_rejected(ns):
    fn = ns["_get_oauth_usage_config"]
    err_cls = ns["OauthUsageConfigError"]
    with pytest.raises(err_cls):
        fn({"oauth_usage": {"user_agent": ""}})


def test_user_agent_too_long_rejected(ns):
    fn = ns["_get_oauth_usage_config"]
    err_cls = ns["OauthUsageConfigError"]
    with pytest.raises(err_cls):
        fn({"oauth_usage": {"user_agent": "x" * 257}})


def test_user_agent_max_length_accepted(ns):
    fn = ns["_get_oauth_usage_config"]
    cfg = fn({"oauth_usage": {"user_agent": "x" * 256}})
    assert cfg["user_agent"] == "x" * 256


def test_unknown_key_ignored(ns):
    """Forward compat: unknown sub-keys must not raise."""
    fn = ns["_get_oauth_usage_config"]
    cfg = fn({"oauth_usage": {"future_field": "ignored"}})
    assert cfg["throttle_seconds"] == 15
