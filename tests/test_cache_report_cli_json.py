"""CLI cache-report JSON preserves anomaly evaluation state (#474)."""
from __future__ import annotations

import pathlib
import sys


ROOT = pathlib.Path(__file__).resolve().parent.parent
BIN = ROOT / "bin"
if str(BIN) not in sys.path:
    sys.path.insert(0, str(BIN))


def test_json_distinguishes_evaluated_clean_from_unevaluated_without_schema_bump(
    cctally_module,
):
    import _cctally_cache_report as cli
    import _lib_cache_report as crk

    evaluated = crk.CacheRow(date="2026-08-01")
    unevaluated = crk.CacheRow(
        date="2026-08-02",
        anomaly_unevaluated=["cache_drop"],
    )

    payload = cli._cache_report_json_payload([evaluated, unevaluated], "day")

    assert payload["schemaVersion"] == 1
    assert payload["days"][0]["anomaly"] == {
        "triggered": False,
        "reasons": [],
        "unevaluated": [],
    }
    assert payload["days"][1]["anomaly"] == {
        "triggered": False,
        "reasons": [],
        "unevaluated": ["cache_drop"],
    }
