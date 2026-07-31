"""CACHE_REPORT_MIN_BASELINE_DAYS is defined in two languages.

Python — bin/_lib_cache_report.py
TypeScript — dashboard/web/src/lib/cache-report-constants.ts

Issue #443 F16 finishes #83 QUAL-10: the duplication was the shipped fix,
the automated check was what went missing. This parses the TS constant with
a light regex rather than running node, matching the alert-axes precedent.
"""
import pathlib
import re
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONSTANTS_TS = ROOT / "dashboard" / "web" / "src" / "lib" / "cache-report-constants.ts"

_BIN = ROOT / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))


def _read_ts_int(const_name: str) -> int:
    source = CONSTANTS_TS.read_text(encoding="utf-8")
    m = re.search(rf"export const {const_name}\s*=\s*(\d+)\s*;", source)
    assert m, f"could not locate {const_name} in cache-report-constants.ts"
    return int(m.group(1))


def test_min_baseline_days_matches_python():
    import _lib_cache_report as crk
    assert _read_ts_int("CACHE_REPORT_MIN_BASELINE_DAYS") == \
        crk.CACHE_REPORT_MIN_BASELINE_DAYS


def test_parity_helper_fails_loudly_on_a_missing_constant():
    """Non-vacuity: the regex must raise rather than silently skip."""
    with pytest.raises(AssertionError, match="could not locate"):
        _read_ts_int("CACHE_REPORT_NO_SUCH_CONSTANT")
