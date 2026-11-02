"""#991 T9 — golden scenarios 13–15, their paired control, and 01–12's guard.

Spec 4.11. The goldens record what `cctally quota` prints for three new-era
family shifts; `bin/cctally-quota-test` binds the command to those bytes. This
file binds the bytes to an INDEPENDENT hand computation, so a golden that was
regenerated over a wrong answer cannot pass for a right one, and it asserts
each rejection's premise explicitly (review finding 991-SPEC-007): the family
distance against the EFFECTIVE radius and the class axis passing, so no
scenario is withheld for a reason other than the one it names.

The hand values use the spec's literal numbers (the Sonnet 5.5 bound
0.09–0.61, so d = 0.91; the 0.03 family floor; the 0.05 effect limit). The
builder derives its shares from the shipped constants instead, so the two
sides are independent.
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "quota"

OPUS = "claude-opus-5-5"
SONNET = "claude-sonnet-5-5"
HAIKU = "claude-haiku-5-5"

#: Hand constants, from the spec's text rather than the kernel.
SONNET_LOW, SONNET_HIGH = 0.09, 0.61
SONNET_D = max(abs(SONNET_LOW - 1.0), abs(SONNET_HIGH - 1.0))   # 0.91
FAMILY_FLOOR = 0.03
EFFECT_LIMIT = 0.05

WITHHELD_FIELDS = (("calibration", "fitted"), ("currentWeek", "consumption"),
                   ("currentWeek", "headroom"), ("currentWeek", "projection"))


def _golden(name: str) -> tuple:
    """The JSON golden's payload and its recorded exit code."""
    text = (FIXTURES / name / "golden-json.txt").read_text()
    body, _, tail = text.rpartition("\nexit: ")
    return json.loads(body), int(tail.strip())


def _hand_effect(p: dict, q: dict) -> float:
    """E(p, q) for an Opus 5.5 reference and a Sonnet 5.5 bound, by hand."""
    lo = {OPUS: 1.0, SONNET: SONNET_LOW}
    d = {OPUS: 0.0, SONNET: SONNET_D}
    families = set(p) | set(q)
    numerator = sum(abs(p.get(f, 0.0) - q.get(f, 0.0)) * d[f]
                    for f in families)
    return numerator / sum(q.get(f, 0.0) * lo[f] for f in families)


def _tv(p: dict, q: dict) -> float:
    families = set(p) | set(q)
    return 0.5 * sum(abs(p.get(f, 0.0) - q.get(f, 0.0)) for f in families)


def _assert_shares(actual: dict, expected: dict) -> None:
    assert set(actual) == set(expected)
    for family, share in expected.items():
        assert actual[family] == pytest.approx(share, abs=1e-9)


def _assert_class_axis_passes(composition: dict) -> None:
    """Every scenario keeps one per-entry token-class split, so the class
    axis must pass: a rejection here would be for the wrong reason."""
    probe = composition["forecastProbe"]
    assert probe["classWithinRadius"] is True
    assert probe["classDistance"] <= composition["classRadiusEffective"]
    assert probe["classDistance"] == pytest.approx(0.0, abs=1e-9)


def _assert_family_outside_floor(composition: dict, tv: float) -> None:
    """The family distance exceeds the EFFECTIVE radius, which is the floor
    because every fit day has the same family split."""
    probe = composition["forecastProbe"]
    assert composition["familyRadiusEmpirical"] == pytest.approx(0.0,
                                                                 abs=1e-9)
    assert composition["familyRadiusEffective"] == pytest.approx(FAMILY_FLOOR)
    assert probe["familyDistance"] == pytest.approx(tv, abs=1e-9)
    assert probe["familyDistance"] > composition["familyRadiusEffective"]
    assert probe["familyWithinRadius"] is False


def _assert_withheld_with(payload: dict, qualifier: str) -> None:
    for section, field in WITHHELD_FIELDS:
        value = payload[section][field]
        assert value["state"] == "withheld", (section, field)
        assert qualifier in value["qualifications"], (section, field)


def test_scenario_13_sonnet_shift_is_admitted_by_materiality():
    """Detects a kernel that withholds a 5% Sonnet 5.5 shift the meter
    evidence bounds (E = 0.05 x 0.91 = 0.0455 <= 0.05), or admits it by the
    radius it actually fails, or publishes it without the fit-weighted
    reference."""
    payload, exit_code = _golden("13-opus55-sonnet-shift-admitted")
    composition = payload["composition"]
    probe = composition["forecastProbe"]
    p = {OPUS: 0.95, SONNET: 0.05}
    q = {OPUS: 1.0}
    assert _hand_effect(p, q) == pytest.approx(0.0455)
    assert _hand_effect(p, q) <= EFFECT_LIMIT
    assert exit_code == 0 and payload["exitCode"] == 0
    assert payload["status"] == "ok"
    _assert_shares(probe["familyShares"], p)
    _assert_shares(composition["familyMateriality"]["fitFamilyShares"], q)
    _assert_family_outside_floor(composition, _tv(p, q))
    _assert_class_axis_passes(composition)
    assert probe["familyEffectBound"] == pytest.approx(_hand_effect(p, q),
                                                       abs=1e-9)
    assert probe["familyEffectUnavailable"] is None
    assert probe["familyAdmittedBy"] == "materiality"
    assert probe["supported"] is True
    for section, field in WITHHELD_FIELDS:
        assert payload[section][field]["state"] == "available", field
    # The same known budget as scenario 11, under the same ceiling rule.
    assert payload["calibration"]["fitted"]["value"] == pytest.approx(
        1_000_000.0, rel=0.01)


def test_scenario_14_sonnet_shift_over_the_limit_is_withheld():
    """Detects a kernel that admits a 10% Sonnet 5.5 shift whose bounded
    effect (0.091) exceeds the 5% limit, or withholds it under the
    unbounded qualifier instead of the over-limit one."""
    payload, exit_code = _golden("14-opus55-sonnet-shift-over-limit")
    composition = payload["composition"]
    probe = composition["forecastProbe"]
    p = {OPUS: 0.90, SONNET: 0.10}
    q = {OPUS: 1.0}
    assert _hand_effect(p, q) == pytest.approx(0.091)
    assert _hand_effect(p, q) > EFFECT_LIMIT
    assert exit_code == 3 and payload["exitCode"] == 3
    _assert_shares(probe["familyShares"], p)
    _assert_family_outside_floor(composition, _tv(p, q))
    _assert_class_axis_passes(composition)
    assert probe["familyEffectBound"] == pytest.approx(_hand_effect(p, q),
                                                       abs=1e-9)
    assert probe["familyEffectUnavailable"] is None
    assert probe["familyAdmittedBy"] is None
    assert probe["supported"] is False
    _assert_withheld_with(payload, "forecast-family-effect-over-limit")


def test_scenario_15_unbounded_haiku_shift_is_withheld():
    """Detects a kernel that counts a changing unbounded family (Haiku 5.5,
    1% -> 5%, TV 0.04 against the 0.03 floor) as zero effect and admits it,
    or withholds it for the class axis, which passes here."""
    payload, exit_code = _golden("15-opus55-haiku-shift-unbounded")
    composition = payload["composition"]
    probe = composition["forecastProbe"]
    p = {OPUS: 0.95, HAIKU: 0.05}
    q = {OPUS: 0.99, HAIKU: 0.01}
    assert _tv(p, q) == pytest.approx(0.04)
    assert exit_code == 3 and payload["exitCode"] == 3
    _assert_shares(probe["familyShares"], p)
    _assert_shares(composition["familyMateriality"]["fitFamilyShares"], q)
    _assert_family_outside_floor(composition, _tv(p, q))
    _assert_class_axis_passes(composition)
    assert probe["familyEffectBound"] is None
    assert probe["familyEffectUnavailable"] == {
        "reason": "unevidenced-family-change", "families": [HAIKU]}
    assert probe["familyAdmittedBy"] is None
    assert probe["supported"] is False
    _assert_withheld_with(payload, "forecast-family-unbounded")


# --------------------------------------------------------------------------
# The paired control, built from scenario 15's history
# --------------------------------------------------------------------------
def _builder():
    spec = importlib.util.spec_from_file_location(
        "build_quota_fixtures_991", ROOT / "bin" / "build-quota-fixtures.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _scenario_as_of(name: str) -> str:
    for line in (FIXTURES / name / "input.env").read_text().splitlines():
        if line.startswith("AS_OF="):
            return line.split("=", 1)[1].strip()
    raise AssertionError(f"{name}/input.env sets no AS_OF")


def _run_quota_json(home: pathlib.Path, as_of: str) -> tuple:
    env = {k: v for k, v in os.environ.items()
           if k not in {"CODEX_HOME", "DO_NOT_TRACK",
                        "CCTALLY_DISABLE_TELEMETRY", "CCTALLY_DATA_DIR"}}
    env.update({
        "HOME": str(home), "NO_COLOR": "1", "TZ": "Etc/UTC", "COLUMNS": "120",
        "CCTALLY_AS_OF": as_of,
        "CCTALLY_DISABLE_DEV_AUTODETECT": "1",
        "CCTALLY_DISABLE_UPDATE_CHECK": "1",
        "CCTALLY_DISABLE_RETENTION_SWEEP": "1",
    })
    proc = subprocess.run([sys.executable, str(ROOT / "bin" / "cctally"),
                           "quota", "--json"],
                          env=env, capture_output=True, text=True)
    return json.loads(proc.stdout), proc.returncode


def test_haiku_shift_inside_the_radius_stays_supported(tmp_path):
    """Detects a kernel that lets the unavailable materiality test veto a
    shift the family radius admits: Haiku 5.5 at 2% over scenario 15's
    history is TV 0.01 <= 0.03 and must stay supported by `radius`."""
    builder = _builder()
    name = "15-opus55-haiku-shift-unbounded"
    home = tmp_path / "control"
    home.mkdir()
    fit = builder.HAIKU_SHIFT_FIT
    inside = builder.haiku_shift_forecast(builder.HAIKU_INSIDE_RADIUS_SHARE)
    builder.seed_family_shift(home, fit=fit, forecast=inside)
    payload, exit_code = _run_quota_json(home, _scenario_as_of(name))
    composition = payload["composition"]
    probe = composition["forecastProbe"]
    p = {OPUS: 0.98, HAIKU: 0.02}
    q = {OPUS: 0.99, HAIKU: 0.01}
    assert _tv(p, q) == pytest.approx(0.01)
    _assert_shares(probe["familyShares"], p)
    assert probe["familyDistance"] == pytest.approx(_tv(p, q), abs=1e-9)
    assert probe["familyDistance"] <= composition["familyRadiusEffective"]
    assert composition["familyRadiusEffective"] == pytest.approx(FAMILY_FLOOR)
    _assert_class_axis_passes(composition)
    assert probe["familyEffectUnavailable"] == {
        "reason": "unevidenced-family-change", "families": [HAIKU]}
    assert probe["familyAdmittedBy"] == "radius"
    assert probe["supported"] is True
    assert exit_code == 0 and payload["status"] == "ok"
    assert "forecast-family-" not in json.dumps(payload)


# --------------------------------------------------------------------------
# Scenarios 01–12 never gain a family qualifier
# --------------------------------------------------------------------------
PRE_991_SCENARIOS = sorted(
    p.name for p in FIXTURES.iterdir()
    if (p / "input.env").is_file() and p.name[:2].isdigit()
    and int(p.name[:2]) <= 12)


def test_the_twelve_pre_991_scenarios_are_all_present():
    """Detects a renamed or dropped scenario directory, which would let the
    qualifier guard below pass over nothing."""
    assert len(PRE_991_SCENARIOS) == 12


@pytest.mark.parametrize("name", PRE_991_SCENARIOS)
def test_no_pre_991_golden_carries_a_family_qualifier(name):
    """Detects the family evaluator reaching a scenario it must not change
    (spec 4.11): 05 and 06 are unsupported through an unrecognised family
    whose entries never reach it, and 11 and 12 pass on the radius, so a
    `forecast-family-*` qualifier in any 01–12 golden is a defect."""
    for mode in ("json", "terminal"):
        text = (FIXTURES / name / f"golden-{mode}.txt").read_text()
        assert "forecast-family-" not in text, (name, mode)
