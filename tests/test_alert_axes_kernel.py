import importlib.util
import pathlib
import sys

import pytest

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


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    """``_load`` clobbers ``sys.modules[name]`` with a fresh instance (needed
    for the ``@dataclass`` introspection during exec). Restore afterwards so a
    clobbered sibling never leaks into the shared module cache and pollutes
    later tests under a single-process (non-xdist) run. ``_lib_alert_axes`` is
    pure (no path constants) so the risk here is lower than the ``_cctally_core``
    case, but the discipline is uniform."""
    saved = dict(sys.modules)
    yield
    for name in list(sys.modules):
        if name not in saved:
            del sys.modules[name]
    for name, mod in saved.items():
        if sys.modules.get(name) is not mod:
            sys.modules[name] = mod


def test_registry_has_five_axes_in_order():
    m = _load("_lib_alert_axes")
    assert [d.id for d in m.AXIS_REGISTRY] == [
        "weekly", "five_hour", "budget", "projected", "project_budget"
    ]


def test_severity_for_three_tier_bands():
    m = _load("_lib_alert_axes")
    assert m.severity_for(0) == "info"
    assert m.severity_for(89) == "info"
    assert m.severity_for(90) == "warn"
    assert m.severity_for(95) == "warn"
    assert m.severity_for(99) == "warn"      # non-vacuity: just below the floor
    assert m.severity_for(100) == "critical" # the >=100 floor
    assert m.severity_for(150) == "critical"


def test_descriptor_exposes_chip_and_table_metadata():
    m = _load("_lib_alert_axes")
    by_id = {d.id: d for d in m.AXIS_REGISTRY}
    assert by_id["weekly"].chip_label == "WEEKLY"
    assert by_id["five_hour"].chip_label == "5H-BLOCK"
    assert by_id["budget"].chip_label == "BUDGET"
    assert by_id["projected"].chip_label == "PROJECTED"
    assert by_id["budget"].milestone_table == "budget_milestones"
    assert by_id["projected"].milestone_table == "projected_milestones"


def test_project_budget_axis_registered():
    """The per-project budget axis (5th) is registered with the PROJECT chip,
    its own milestone table, and the axis-uniform 3-tier severity policy."""
    m = _load("_lib_alert_axes")
    by_id = {d.id: d for d in m.AXIS_REGISTRY}
    assert by_id["project_budget"].chip_label == "PROJECT"
    assert by_id["project_budget"].title_label == "Project budget"
    assert by_id["project_budget"].milestone_table == "project_budget_milestones"
    # Severity reuses the shared 3-tier policy unchanged.
    assert m.severity_for(89) == "info"
    assert m.severity_for(90) == "warn"
    assert m.severity_for(100) == "critical"
