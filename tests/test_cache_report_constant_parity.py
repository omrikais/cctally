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
VERDICT_TS = ROOT / "dashboard" / "web" / "src" / "lib" / "cacheReportVerdict.ts"
SETTINGS_TSX = ROOT / "dashboard" / "web" / "src" / "modals" / "CacheReportSettings.tsx"
ENVELOPE_TS = ROOT / "dashboard" / "web" / "src" / "types" / "envelope.ts"

_BIN = ROOT / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))


def _read_ts_int(const_name: str) -> int:
    source = CONSTANTS_TS.read_text(encoding="utf-8")
    m = re.search(rf"export const {const_name}\s*=\s*(\d+)\s*;", source)
    assert m, f"could not locate {const_name} in cache-report-constants.ts"
    return int(m.group(1))


def _read_ts_string_list(path: pathlib.Path, const_name: str) -> tuple[str, ...]:
    """Parse an `export const NAME[: type] = ['a', 'b'];` array literal.

    Light regex, no node — same trade the int helper above already makes.
    """
    source = path.read_text(encoding="utf-8")
    m = re.search(
        rf"export const {const_name}\s*(?::[^=]*)?=\s*\[(.*?)\]\s*;",
        source,
        re.DOTALL,
    )
    assert m, f"could not locate {const_name} in {path.name}"
    return tuple(re.findall(r"'([^']*)'", m.group(1)))


_TS_THRESHOLD_GUARD = r"n\s*<\s*(\d+)\s*\|\|\s*n\s*>\s*(\d+)"


def _read_ts_threshold_bounds(source: str) -> tuple[int, int]:
    """Parse `n < 1 || n > 100` out of the client form's guard.

    #443 S3 F17: the client form is a UX gate, not a correctness gate —
    the server rejects an out-of-range value regardless, so drift here
    degrades an inline error into a server error rather than admitting a
    bad value. It is pinned rather than unified into shared constants so
    no dashboard/web source edit (and therefore no bundle rebuild, and no
    real-browser QA gate) is needed for a behavior-preserving refactor.
    """
    m = re.search(_TS_THRESHOLD_GUARD, source)
    assert m, "could not locate the threshold range guard in CacheReportSettings.tsx"
    return int(m.group(1)), int(m.group(2))


def test_client_threshold_bounds_match_the_kernel():
    import _lib_cache_report as crk
    lo, hi = _read_ts_threshold_bounds(SETTINGS_TSX.read_text(encoding="utf-8"))
    assert (lo, hi) == (
        crk.CACHE_REPORT_THRESHOLD_MIN_PP,
        crk.CACHE_REPORT_THRESHOLD_MAX_PP,
    )


def test_client_bounds_helper_fails_loudly_when_the_guard_moves():
    """Non-vacuity: the regex must raise rather than silently return."""
    original = SETTINGS_TSX.read_text(encoding="utf-8")
    mangled = re.sub(_TS_THRESHOLD_GUARD, "SOMETHING_ELSE", original)
    assert mangled != original, "fixture precondition: the guard must be present"
    with pytest.raises(AssertionError, match="could not locate the threshold range guard"):
        _read_ts_threshold_bounds(mangled)


def test_min_baseline_days_matches_python():
    import _lib_cache_report as crk
    assert _read_ts_int("CACHE_REPORT_MIN_BASELINE_DAYS") == \
        crk.CACHE_REPORT_MIN_BASELINE_DAYS


def test_parity_helper_fails_loudly_on_a_missing_constant():
    """Non-vacuity: the regex must raise rather than silently skip."""
    with pytest.raises(AssertionError, match="could not locate"):
        _read_ts_int("CACHE_REPORT_NO_SUCH_CONSTANT")


def test_wire_builder_claude_predicates_match_the_kernel():
    """#443 S2: the Claude predicate list is now written down TWICE in Python.

    `_lib_cache_report_wire` is a pure module and must not import the
    kernel, so the duplication is structurally forced — but nothing else
    pins the two equal. Drift would not raise: the wire builder's filter
    would simply start discarding a predicate the kernel still evaluates,
    silently dropping a real Claude verdict from the envelope.
    """
    import _lib_cache_report as crk
    import _lib_cache_report_wire as wire
    assert tuple(wire.CLAUDE_PREDICATES) == tuple(crk.CACHE_ANOMALY_PREDICATES)


def test_ts_claude_predicates_match_the_kernel():
    """#443 S2: the Claude predicate list is written down THREE times.

    Python kernel, Python wire builder, and now the TypeScript mirror
    `CACHE_ANOMALY_PREDICATES` — which S2 promoted from a private constant
    into the DEFAULT of `cacheRowVerdict`'s new predicate-set parameter, so
    an absent `anomaly_predicates` on the wire resolves against it. Two of
    the three were pinned; drift in the third would silently change how
    every pre-S2 envelope's rows resolve, with nothing raising.
    """
    import _lib_cache_report as crk
    assert _read_ts_string_list(VERDICT_TS, "CACHE_ANOMALY_PREDICATES") == \
        tuple(crk.CACHE_ANOMALY_PREDICATES)


def test_ts_list_helper_fails_loudly_on_a_missing_constant():
    """Non-vacuity: the array regex must raise rather than return ()."""
    with pytest.raises(AssertionError, match="could not locate"):
        _read_ts_string_list(VERDICT_TS, "CACHE_NO_SUCH_PREDICATES")


def _read_ts_union(path: pathlib.Path, type_name: str) -> frozenset[str]:
    """Parse `export type X = 'a' | 'b';` into a set of members.

    Order is deliberately discarded: a TypeScript union is unordered, so
    pinning order would fail on a harmless reordering. The predicate
    ARRAY test above compares tuples instead, because that one encodes
    reason-append order.
    """
    source = path.read_text(encoding="utf-8")
    m = re.search(rf"export type {type_name}\s*=\s*([^;]+);", source)
    assert m, f"could not locate type {type_name} in {path.name}"
    return frozenset(re.findall(r"'([^']*)'", m.group(1)))


def test_ts_anomaly_reason_union_matches_the_python_literal():
    """#443 S3 F19: nothing pinned the union itself.

    The existing array test compares the TS array with
    CACHE_ANOMALY_PREDICATES, not with `typing.get_args(CacheAnomalyReason)`,
    so adding a member to the Python Literal alone fails nothing today —
    the chain protects predicate-array changes, not Literal-only changes.
    """
    import typing
    import _lib_cache_report as crk
    assert _read_ts_union(ENVELOPE_TS, "CacheAnomalyReason") == frozenset(
        typing.get_args(crk.CacheAnomalyReason)
    )


def test_ts_union_helper_fails_loudly_on_a_missing_type():
    """Non-vacuity: the regex must raise rather than return an empty set."""
    with pytest.raises(AssertionError, match="could not locate type"):
        _read_ts_union(ENVELOPE_TS, "CacheNoSuchReason")


def test_all_predicates_spans_every_provider():
    """`ALL_PREDICATES` is what makes the filter's identity shortcut safe.

    If it ever stopped covering a provider's set, `filter_inapplicable`
    would take the early return for that provider and discard nothing —
    the precise failure the filter exists to prevent.
    """
    import _lib_cache_report_wire as wire
    for provider in ("claude", "codex"):
        assert set(wire.applicable_predicates(provider)).issubset(
            set(wire.ALL_PREDICATES)
        ), f"{provider} carries a predicate missing from ALL_PREDICATES"
