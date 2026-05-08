"""Layer A unit tests for bin/_lib_share.py."""
from __future__ import annotations

import importlib.util
import pathlib
import sys
from datetime import datetime, timezone

# Load _lib_share by path (same pattern bin/cctally uses for its peers).
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_LIB_SHARE_PATH = _REPO_ROOT / "bin" / "_lib_share.py"
_spec = importlib.util.spec_from_file_location("_lib_share", _LIB_SHARE_PATH)
_lib_share = importlib.util.module_from_spec(_spec)
# Register in sys.modules BEFORE exec_module: Python 3.14's `dataclass`
# decorator looks up `cls.__module__` in `sys.modules` for KW_ONLY type checks
# during class processing, which fails if the module isn't registered yet.
sys.modules["_lib_share"] = _lib_share
_spec.loader.exec_module(_lib_share)

# Re-export for terse test bodies.
ShareSnapshot = _lib_share.ShareSnapshot
PeriodSpec = _lib_share.PeriodSpec
ColumnSpec = _lib_share.ColumnSpec
Row = _lib_share.Row
TextCell = _lib_share.TextCell
MoneyCell = _lib_share.MoneyCell
PercentCell = _lib_share.PercentCell
DateCell = _lib_share.DateCell
DeltaCell = _lib_share.DeltaCell
ProjectCell = _lib_share.ProjectCell
Totalled = _lib_share.Totalled
ChartPoint = _lib_share.ChartPoint
LineChart = _lib_share.LineChart
BarChart = _lib_share.BarChart
HorizontalBarChart = _lib_share.HorizontalBarChart


def _make_minimal_snapshot() -> ShareSnapshot:
    return ShareSnapshot(
        cmd="report",
        title="Weekly $ / % trend — last 4 weeks",
        subtitle="Apr 11 → May 9 (UTC) · light · projects anonymized",
        period=PeriodSpec(
            start=datetime(2026, 4, 11, tzinfo=timezone.utc),
            end=datetime(2026, 5, 9, tzinfo=timezone.utc),
            display_tz="UTC",
            label="Apr 11 → May 9 (UTC)",
        ),
        columns=(
            ColumnSpec(key="week", label="Week", align="left"),
            ColumnSpec(key="cost", label="$ Cost", align="right"),
        ),
        rows=(
            Row(cells={
                "week": TextCell("Apr 11"),
                "cost": MoneyCell(123.45),
            }),
        ),
        chart=None,
        totals=(Totalled(label="Sum", value="$123.45"),),
        notes=(),
        generated_at=datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc),
        version="1.4.0",
    )


def test_snapshot_constructs_and_is_frozen():
    snap = _make_minimal_snapshot()
    assert snap.cmd == "report"
    assert snap.rows[0].cells["cost"].usd == 123.45
    # Frozen — should raise on mutation.
    import dataclasses
    try:
        snap.cmd = "daily"  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        return
    raise AssertionError("ShareSnapshot must be frozen")


def test_xml_escape_handles_all_xml_chars():
    assert _lib_share._xml_escape("a&b") == "a&amp;b"
    assert _lib_share._xml_escape("a<b") == "a&lt;b"
    assert _lib_share._xml_escape("a>b") == "a&gt;b"
    assert _lib_share._xml_escape('a"b') == "a&quot;b"
    assert _lib_share._xml_escape("a'b") == "a&#39;b"
    assert _lib_share._xml_escape("plain") == "plain"
    # Adversarial.
    assert _lib_share._xml_escape("Project<script>") == "Project&lt;script&gt;"


def test_attr_escape_normalizes_newlines():
    # Same as xml plus newline normalization.
    assert _lib_share._attr_escape("a\nb") == "a b"
    assert _lib_share._attr_escape("a&\nb") == "a&amp; b"


def test_md_escape_covers_html_and_md_chars():
    # HTML chars (Codex finding M8): markdown surfaces interpret raw HTML.
    assert _lib_share._md_escape("a<b") == "a&lt;b"
    assert _lib_share._md_escape("a>b") == "a&gt;b"
    assert _lib_share._md_escape("a&b") == "a&amp;b"
    # Markdown formatting chars.
    assert _lib_share._md_escape("a|b") == "a\\|b"
    assert _lib_share._md_escape("a*b") == "a\\*b"
    assert _lib_share._md_escape("a_b") == "a\\_b"
    assert _lib_share._md_escape("a`b") == "a\\`b"
    assert _lib_share._md_escape("a[b") == "a\\[b"
    assert _lib_share._md_escape("a]b") == "a\\]b"
    # Adversarial: HTML+md combo.
    assert _lib_share._md_escape("evil<img onerror=x>") == "evil&lt;img onerror=x&gt;"
