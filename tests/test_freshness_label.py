"""_freshness_label boundary tests at default and custom configs."""
import pytest
from conftest import load_script


@pytest.fixture(scope="module")
def ns():
    return load_script()


@pytest.fixture
def default_cfg(ns):
    return ns["_get_oauth_usage_config"]({})


def test_zero_age_is_fresh(ns, default_cfg):
    assert ns["_freshness_label"](0.0, default_cfg) == "fresh"


def test_at_fresh_boundary_is_fresh(ns, default_cfg):
    """fresh_threshold = 30s default; age == 30 is inclusive `fresh`."""
    assert ns["_freshness_label"](30.0, default_cfg) == "fresh"


def test_just_past_fresh_is_aging(ns, default_cfg):
    assert ns["_freshness_label"](31.0, default_cfg) == "aging"


def test_at_stale_boundary_is_aging(ns, default_cfg):
    """stale_after = 90s default; age == 90 is inclusive `aging`."""
    assert ns["_freshness_label"](90.0, default_cfg) == "aging"


def test_just_past_stale_is_stale(ns, default_cfg):
    assert ns["_freshness_label"](91.0, default_cfg) == "stale"


def test_huge_age_is_stale(ns, default_cfg):
    assert ns["_freshness_label"](999999.0, default_cfg) == "stale"


def test_custom_thresholds(ns):
    cfg = ns["_get_oauth_usage_config"]({"oauth_usage": {
        "fresh_threshold_seconds": 5,
        "stale_after_seconds": 15,
    }})
    assert ns["_freshness_label"](5.0, cfg) == "fresh"
    assert ns["_freshness_label"](6.0, cfg) == "aging"
    assert ns["_freshness_label"](15.0, cfg) == "aging"
    assert ns["_freshness_label"](16.0, cfg) == "stale"
