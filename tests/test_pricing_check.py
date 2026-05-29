import importlib.util, pathlib, sys
import datetime as dt

_BIN = pathlib.Path(__file__).resolve().parents[1] / "bin"


def _load(modname):
    spec = importlib.util.spec_from_file_location(modname, _BIN / f"{modname}.py")
    mod = importlib.util.module_from_spec(spec)
    # Register before exec so @dataclasses.dataclass can resolve cls.__module__
    # (matches the repo's other spec_from_file_location loaders).
    sys.modules[modname] = mod
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


pc = _load("_lib_pricing_check")

# Fakes mirroring the real predicates' contracts.
_CLAUDE_PRICED = {"claude-known"}


def _resolve_claude(m):  # returns dict|None
    return {"input_cost_per_token": 1e-6} if m in _CLAUDE_PRICED else None


_CODEX_PRICED = {"gpt-5", "gpt-5.3-codex"}


def _is_codex_fallback(m):  # True iff unknown
    return m not in _CODEX_PRICED


def test_classify_coverage_flags_unpriced_claude_and_codex_fallback():
    observed = [
        ("claude", "claude-known", 10, 1000),     # priced -> no gap
        ("claude", "claude-mystery", 3, 5000),     # unpriced -> gap
        ("codex", "gpt-5.3-codex", 4, 400),        # priced -> no gap
        ("codex", "gpt-7-brandnew", 2, 9000),      # fallback -> gap
    ]
    gaps = pc.classify_coverage(observed, _resolve_claude, _is_codex_fallback)
    by_model = {g.model: g for g in gaps}
    assert set(by_model) == {"claude-mystery", "gpt-7-brandnew"}
    assert by_model["claude-mystery"].kind == "unpriced"
    assert by_model["claude-mystery"].entry_count == 3
    assert by_model["claude-mystery"].token_total == 5000
    assert by_model["gpt-7-brandnew"].kind == "fallback"
    assert by_model["gpt-7-brandnew"].provider == "codex"


def test_classify_coverage_empty_when_all_priced():
    observed = [("claude", "claude-known", 1, 1), ("codex", "gpt-5", 1, 1)]
    assert pc.classify_coverage(observed, _resolve_claude, _is_codex_fallback) == []
