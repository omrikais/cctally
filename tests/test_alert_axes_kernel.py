import importlib.util
import pathlib
import sys

BIN = pathlib.Path(__file__).resolve().parent.parent / "bin"


def _load(name):
    # Mirror the repo's canonical kernel loader (tests/test_budget.py):
    # register in sys.modules BEFORE exec so the @dataclass
    # sys.modules[cls.__module__] introspection resolves on Python 3.14.
    from importlib.machinery import SourceFileLoader

    loader = SourceFileLoader(name, str(BIN / f"{name}.py"))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    loader.exec_module(mod)
    return mod


def test_registry_has_four_axes_in_order():
    m = _load("_lib_alert_axes")
    assert [d.id for d in m.AXIS_REGISTRY] == [
        "weekly", "five_hour", "budget", "projected"
    ]


def test_severity_policy_matches_legacy_amber_red_split():
    m = _load("_lib_alert_axes")
    # Legacy hardcoded rule: <95 amber, >=95 red (axis-uniform).
    assert m.severity_for(90) == "amber"
    assert m.severity_for(94) == "amber"
    assert m.severity_for(95) == "red"
    assert m.severity_for(100) == "red"


def test_descriptor_exposes_chip_and_table_metadata():
    m = _load("_lib_alert_axes")
    by_id = {d.id: d for d in m.AXIS_REGISTRY}
    assert by_id["weekly"].chip_label == "WEEKLY"
    assert by_id["five_hour"].chip_label == "5H-BLOCK"
    assert by_id["budget"].chip_label == "BUDGET"
    assert by_id["projected"].chip_label == "PROJECTED"
    assert by_id["budget"].milestone_table == "budget_milestones"
    assert by_id["projected"].milestone_table == "projected_milestones"
