"""Pure kernel: alert-axis descriptors + registry + shared severity policy.

Single source of truth for axis *metadata* — id, chip/title labels (kept
byte-identical with dashboard/web/src/lib/alertAxis.ts), the milestone-table
name used by the dashboard envelope, and the axis-uniform severity policy
(amber <95 / red >=95). This kernel does NOT own the write/transaction path:
each axis keeps its own detect-and-arm code in bin/_cctally_record.py. The
descriptor is the metadata/render contract, not the write engine.

Stdlib-only, no I/O at import time. bin/cctally re-exports the public symbols.
Spec: docs/superpowers/specs/2026-06-01-alerts-axis-registry-projected-pace-design.md
"""
from __future__ import annotations

from dataclasses import dataclass

# Severity boundary: thresholds at or above this render red, below it amber.
# Mirrors the legacy hardcoded amber<95 / red>=95 split (axis-uniform v1).
_SEVERITY_RED_FLOOR = 95


def severity_for(threshold: int) -> str:
    """Map a crossed integer threshold to a severity color ('amber' | 'red')."""
    return "red" if int(threshold) >= _SEVERITY_RED_FLOOR else "amber"


@dataclass(frozen=True)
class AlertAxisDescriptor:
    """Axis-agnostic metadata shared by the record path + dashboard envelope."""

    id: str            # 'weekly' | 'five_hour' | 'budget' | 'projected'
    chip_label: str    # SHOUT form, byte-identical with alertAxis.ts AXIS_CHIP_LABEL
    title_label: str   # sentence-case form, byte-identical with AXIS_TITLE_LABEL
    milestone_table: str  # SQLite table the dashboard envelope SELECTs from


AXIS_REGISTRY: "tuple[AlertAxisDescriptor, ...]" = (
    AlertAxisDescriptor("weekly", "WEEKLY", "Weekly", "percent_milestones"),
    AlertAxisDescriptor("five_hour", "5H-BLOCK", "5h-block", "five_hour_milestones"),
    AlertAxisDescriptor("budget", "BUDGET", "Budget", "budget_milestones"),
    AlertAxisDescriptor("projected", "PROJECTED", "Projected", "projected_milestones"),
)

AXIS_BY_ID: "dict[str, AlertAxisDescriptor]" = {d.id: d for d in AXIS_REGISTRY}
