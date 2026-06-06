"""Python↔TypeScript chip/title-label parity for the alert-axis registry.

The chip + title labels live in TWO places that MUST stay byte-identical:

* Python — ``bin/_lib_alert_axes.py`` ``AXIS_REGISTRY`` (each descriptor's
  ``chip_label`` / ``title_label``), consumed by the dashboard envelope.
* TypeScript — ``dashboard/web/src/lib/alertAxis.ts`` ``AXIS_CHIP_LABEL`` /
  ``AXIS_TITLE_LABEL``, consumed by Toast / RecentAlertsPanel /
  RecentAlertsModal.

There was NO cross-language test covering this until the per-project budget
axis landed (``tests/test_alert_axes_kernel.py`` is Python-only; spec §5.5 /
Codex P2-3). This test parses the TS maps out of ``alertAxis.ts`` (a light
regex parse — the dashboard build is the only Node surface in the repo and we
do not want a Node dependency in the Python suite) and asserts byte-equality
with the Python registry for ALL SIX axes. A future axis that drifts the chip
text on one side fails here.
"""
from __future__ import annotations

import importlib.util
import pathlib
import re
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
BIN = ROOT / "bin"
ALERT_AXIS_TS = ROOT / "dashboard" / "web" / "src" / "lib" / "alertAxis.ts"


def _load(name):
    from importlib.machinery import SourceFileLoader

    loader = SourceFileLoader(name, str(BIN / f"{name}.py"))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    saved = dict(sys.modules)
    yield
    for name in list(sys.modules):
        if name not in saved:
            del sys.modules[name]
    for name, mod in saved.items():
        if sys.modules.get(name) is not mod:
            sys.modules[name] = mod


def _parse_ts_record(source: str, const_name: str) -> dict[str, str]:
    """Extract a ``Record<AlertAxis, string>`` object literal from the TS
    source. Matches ``export const <name>: Record<...> = { ... };`` and pulls
    ``key: 'value'`` pairs (single- or double-quoted values; bare or quoted
    keys). Deliberately strict so a malformed parse surfaces as an empty dict
    (which then fails the equality assertion) rather than silently passing.
    """
    m = re.search(
        r"export const "
        + re.escape(const_name)
        + r"\s*:\s*Record<[^>]*>\s*=\s*\{(.*?)\}\s*;",
        source,
        re.DOTALL,
    )
    assert m, f"could not locate {const_name} in alertAxis.ts"
    body = m.group(1)
    pairs = re.findall(
        r"""['"]?(\w+)['"]?\s*:\s*['"]([^'"]*)['"]""",
        body,
    )
    return {k: v for k, v in pairs}


# Axes whose chip/title labels exist on BOTH the Python and TS sides. As of
# Task 4 (dashboard) this is the FULL registry: `codex_budget` landed in the
# Python registry in Task 3 (alert axis) and its TS entry (alertAxis.ts) landed
# in Task 4, so the cross-language byte-equality assertion below now covers every
# axis (including codex_budget). A future axis that drifts the chip text on one
# side — or lands on only one side — fails here.
_TS_PRESENT_AXES = {
    "weekly", "five_hour", "budget", "projected", "project_budget",
    "codex_budget",
}


def test_chip_and_title_labels_match_python_registry():
    m = _load("_lib_alert_axes")
    py_chip = {d.id: d.chip_label for d in m.AXIS_REGISTRY}
    py_title = {d.id: d.title_label for d in m.AXIS_REGISTRY}

    source = ALERT_AXIS_TS.read_text(encoding="utf-8")
    ts_chip = _parse_ts_record(source, "AXIS_CHIP_LABEL")
    ts_title = _parse_ts_record(source, "AXIS_TITLE_LABEL")

    # The TS side carries exactly the cross-language-shared axes (a one-sided
    # add on EITHER side fails here). As of Task 4 this is the full registry, so
    # the two sides are now equal sets (not Python-superset).
    assert set(ts_chip) == _TS_PRESENT_AXES
    assert set(py_chip) == _TS_PRESENT_AXES
    # Byte-identical chip + title labels for every axis present on BOTH sides.
    assert {k: py_chip[k] for k in _TS_PRESENT_AXES} == ts_chip, (py_chip, ts_chip)
    assert {k: py_title[k] for k in _TS_PRESENT_AXES} == ts_title, (py_title, ts_title)


def test_project_budget_axis_chip_is_PROJECT():
    # The distinguishing assertion: the per-project axis uses the "PROJECT"
    # chip (vs the global budget's "BUDGET"), so the two read apart at a glance.
    m = _load("_lib_alert_axes")
    desc = m.AXIS_BY_ID["project_budget"]
    assert desc.chip_label == "PROJECT"
    assert desc.title_label == "Project budget"
    source = ALERT_AXIS_TS.read_text(encoding="utf-8")
    assert _parse_ts_record(source, "AXIS_CHIP_LABEL")["project_budget"] == "PROJECT"
    assert (
        _parse_ts_record(source, "AXIS_TITLE_LABEL")["project_budget"]
        == "Project budget"
    )
