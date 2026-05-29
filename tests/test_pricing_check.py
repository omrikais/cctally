import importlib.util, pathlib
import datetime as dt

_BIN = pathlib.Path(__file__).resolve().parents[1] / "bin"


def _load(modname):
    spec = importlib.util.spec_from_file_location(modname, _BIN / f"{modname}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pricing = _load("_lib_pricing")


def test_pricing_constants_present_and_wellformed():
    assert isinstance(pricing.PRICING_SNAPSHOT_DATE, str)
    # parses as ISO date
    dt.date.fromisoformat(pricing.PRICING_SNAPSHOT_DATE)
    assert isinstance(pricing.PRICING_STALENESS_DAYS, int) and pricing.PRICING_STALENESS_DAYS == 60
    assert pricing.LITELLM_PRICES_URL.startswith("https://")
    assert isinstance(pricing.PRICING_DRIFT_ALLOWLIST, list)
    for e in pricing.PRICING_DRIFT_ALLOWLIST:
        assert set(e) <= {"model", "field", "reason"}
        assert e["model"] and e["reason"]
