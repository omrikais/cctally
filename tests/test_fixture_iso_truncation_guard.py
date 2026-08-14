"""No fixture builder may re-introduce a second-truncating timestamp helper (#568).

`strftime` with `%S` and no `%f`, and `isoformat(timespec="seconds")`, both drop
microseconds. A boundary sentinel seeded one microsecond off an exclusive bound
then lands ON the bound and cannot discriminate it, so the test passes for the
wrong reason and the golden records that pass. #556 S2 hit this for real.

The scan is AST rather than textual because `build-speed-fixtures.py` split its
`strftime(` call from its format literal across two lines, so a grep-shaped guard
reported a live offender as clean.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


BIN = Path(__file__).resolve().parents[1] / "bin"

# Truncating expressions that are CORRECT, each because the field it writes
# carries its own precision contract. Keyed by (filename, normalized source) and
# mapped to the reason, following the entry-plus-reason shape of
# `tests/test_cost_usage_dict_chokepoint.py`.
#
# The filename is part of the key deliberately: without it, any file containing
# an expression that normalizes to the same text would inherit the exemption.
#
# Known limitation, stated rather than papered over: `_normalize` collapses
# whitespace but preserves token boundaries, so re-wrapping an allowlisted
# expression across different lines changes its key. That does not create a
# silent hole — `test_allowlist_is_non_vacuous` reports the entry as stale and
# the main guard reports the site as an offender — but it does mean a pure
# reflow costs two red tests and an allowlist edit.
ALLOWLIST: dict[tuple[str, str], str] = {
    (
        "build-bench-fixtures.py",
        'base.strftime("%Y-%m-%dT%H:%M:%S.000Z")',
    ): (
        "takes an int minute-offset, never a datetime, so no caller fraction "
        "can reach it; the .000 is a fixed literal (spec §4.4 exclusion 2)"
    ),
    (
        "build-codex-fixtures.py",
        'utc.strftime("%Y-%m-%dT%H:%M:%S")',
    ): (
        "_iso_ms — emitting milliseconds is its documented purpose; the "
        "millisecond field is appended by the caller (spec §4.4 exclusion 1)"
    ),
    (
        "build-journal-benchmark-fixture.py",
        '(block_start + dt.timedelta(hours=5)).isoformat( timespec="seconds")'
        '.replace("+00:00", "Z")',
    ): (
        "writes five_hour_resets_at, which production renders with "
        "isoformat(timespec='seconds') at bin/_cctally_record.py:4255-4257 "
        "(spec §4.4 exclusion 5)"
    ),
    (
        "build-journal-benchmark-fixture.py",
        'block_start.isoformat( timespec="seconds").replace("+00:00", "Z")',
    ): (
        "writes block_start_at, which production renders with "
        "isoformat(timespec='seconds') at bin/_cctally_record.py:2062 "
        "(spec §4.4 exclusion 5)"
    ),
}


def _builder_sources():
    """Every Python fixture builder.

    `build-*.py`, NOT `build-*`: the latter also matches the shell script
    `bin/build-readme-screenshots.sh`, which `ast.parse` cannot parse. The
    `.py` glob still includes `build-journal-benchmark-fixture.py`, which the
    narrower `build-*-fixtures.py` pattern omits — and that omission is exactly
    how three offenders escaped the original issue's enumeration.
    """
    return sorted(BIN.glob("build-*.py"))


def _normalize(segment: str | None) -> str:
    return " ".join(segment.split()) if segment else ""


def _truncating_strftime_calls(tree: ast.AST):
    """Yield every `X.strftime(fmt)` whose literal `fmt` has `%S` but no `%f`.

    Deliberately wider than the single `"%Y-%m-%dT%H:%M:%SZ"` literal the #568
    sweep removed. The likeliest way to reintroduce the defect is to copy the
    adjacent `_iso_ms` in `build-codex-fixtures.py` and drop its millisecond
    suffix, which yields `strftime("%Y-%m-%dT%H:%M:%S") + "Z"` — a different
    literal and a different expression shape, but the same lost microsecond.
    Matching on the format's own semantics catches that whole family.

    A format carrying `%f` keeps the fraction and is fine; a date-only format
    has no `%S` and never matches.
    """
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "strftime"
            and len(node.args) == 1
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            continue
        fmt = node.args[0].value
        if "%S" in fmt and "%f" not in fmt:
            yield node


def _truncating_isoformat_z_calls(tree: ast.AST):
    """Yield every `X.isoformat(timespec="seconds").replace("+00:00", "Z")`.

    Both halves are REQUIRED. Without `timespec="seconds"` the call preserves
    the fraction and is correct — several such calls exist in the doctor and
    e2e builders. Without the `.replace(...)` the expression yields the
    `+00:00` spelling, which is how production's `_canonicalize_optional_iso`
    and `now_utc_iso` render second-precision boundary and audit fields:
    legitimate mirrors, not offenders.
    """
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "replace"
            and len(node.args) == 2
        ):
            continue
        first, second = node.args
        if not (
            isinstance(first, ast.Constant)
            and first.value == "+00:00"
            and isinstance(second, ast.Constant)
            and second.value == "Z"
        ):
            continue
        inner = node.func.value
        if (
            isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Attribute)
            and inner.func.attr == "isoformat"
            and any(
                kw.arg == "timespec"
                and isinstance(kw.value, ast.Constant)
                and kw.value.value == "seconds"
                for kw in inner.keywords
            )
        ):
            yield node


def _truncating_sites(path: Path, src: str):
    """Yield `(node, label)` for every truncating expression in one builder."""
    tree = ast.parse(src)
    for node in _truncating_strftime_calls(tree):
        yield node, 'strftime with %S and no %f'
    for node in _truncating_isoformat_z_calls(tree):
        yield node, 'isoformat(timespec="seconds") + Z'


def _offenders():
    """Every truncating site that is not allowlisted."""
    found = []
    for path in _builder_sources():
        src = path.read_text(encoding="utf-8")
        for node, label in _truncating_sites(path, src):
            key = (path.name, _normalize(ast.get_source_segment(src, node)))
            if key in ALLOWLIST:
                continue
            found.append(f"{path.name}:{node.lineno}  {label}")
    return found


def test_no_truncating_timestamp_helpers_in_fixture_builders():
    offenders = _offenders()
    assert not offenders, (
        "Second-truncating timestamp expression found in a fixture builder — "
        "use _fixture_builders.fixture_source_timestamp_z, which preserves the "
        "microsecond fraction and is byte-identical for whole-second inputs. "
        "If the field you are writing genuinely carries its own precision "
        "contract, add a (filename, normalized-source) entry to ALLOWLIST "
        "whose reason cites the production file:line that establishes it.\n  "
        + "\n  ".join(offenders)
    )


_MULTILINE_STRFTIME = '''
def _iso(ts):
    return ts.astimezone(dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
'''

_STRFTIME_THEN_CONCATENATED_Z = '''
def _iso(ts):
    return ts.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + "Z"
'''

_DATE_ONLY_STRFTIME = '''
def _day(ts):
    return ts.strftime("%Y-%m-%d")
'''

_FRACTION_PRESERVING_STRFTIME = '''
def _iso_us(ts):
    return ts.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
'''

_PRODUCTION_OFFSET_MIRROR = '''
def _iso_canon(d):
    return d.astimezone(dt.timezone.utc).isoformat(timespec="seconds")
'''

_FRACTION_PRESERVING_ISOFORMAT = '''
def _iso(ts):
    return ts.isoformat().replace("+00:00", "Z")
'''

_TRUNCATING_Z_ISOFORMAT = '''
def _iso(minutes):
    return (_BASE + dt.timedelta(minutes=minutes)).isoformat(
        timespec="seconds").replace("+00:00", "Z")
'''


def test_detector_catches_a_strftime_split_across_lines():
    """build-speed-fixtures.py:95 had exactly this shape. A grep-based guard
    reports it clean, which is why this guard parses instead of matching text."""
    tree = ast.parse(_MULTILINE_STRFTIME)
    assert len(list(_truncating_strftime_calls(tree))) == 1


def test_detector_catches_a_concatenated_z_suffix():
    """The likeliest reintroduction: copy `_iso_ms` from
    build-codex-fixtures.py and drop its millisecond suffix. A guard keyed on
    the single literal `"%Y-%m-%dT%H:%M:%SZ"` is blind to this."""
    tree = ast.parse(_STRFTIME_THEN_CONCATENATED_Z)
    assert len(list(_truncating_strftime_calls(tree))) == 1


def test_detector_catches_the_z_suffixed_isoformat():
    tree = ast.parse(_TRUNCATING_Z_ISOFORMAT)
    assert len(list(_truncating_isoformat_z_calls(tree))) == 1


@pytest.mark.parametrize(
    "source, label",
    [
        (_DATE_ONLY_STRFTIME, "date-only strftime (build-readme-fixtures.py)"),
        (_FRACTION_PRESERVING_STRFTIME, "%f-bearing strftime keeps the fraction"),
        (_PRODUCTION_OFFSET_MIRROR, "+00:00 mirror (build-dashboard:1053)"),
        (_FRACTION_PRESERVING_ISOFORMAT, "isoformat without timespec (doctor/e2e)"),
    ],
)
def test_detector_ignores_legitimate_forms(source, label):
    tree = ast.parse(source)
    assert not list(_truncating_strftime_calls(tree)), label
    assert not list(_truncating_isoformat_z_calls(tree)), label


def test_allowlist_does_not_excuse_another_file():
    """An allowlist entry is scoped to the file that earned it.

    The same expression text in a different builder is a NEW truncating site
    and must still be reported — an allowlist keyed on source text alone would
    silently excuse it.
    """
    borrowed = next(
        source for (name, source) in ALLOWLIST
        if name == "build-journal-benchmark-fixture.py"
    )
    assert ("build-journal-benchmark-fixture.py", borrowed) in ALLOWLIST
    assert ("build-impostor-fixtures.py", borrowed) not in ALLOWLIST


def test_allowlist_is_non_vacuous():
    """Every allowlist entry must still match a real site, so a stale entry
    cannot sit there silently excusing nothing."""
    seen = set()
    for path in _builder_sources():
        src = path.read_text(encoding="utf-8")
        for node, _label in _truncating_sites(path, src):
            seen.add((path.name, _normalize(ast.get_source_segment(src, node))))
    stale = set(ALLOWLIST) - seen
    assert not stale, f"allowlist entries match nothing: {sorted(stale)}"


def test_allowlist_entries_carry_a_reason():
    """A bare exemption is an undocumented decision. Mirrors the reason
    requirement in tests/test_cost_usage_dict_chokepoint.py."""
    missing = [key for key, reason in ALLOWLIST.items() if not reason.strip()]
    assert not missing, f"allowlist entries without a reason: {sorted(missing)}"


def test_guard_is_non_vacuous():
    """The converted builders must actually call the shared helper, so the guard
    above cannot pass merely because the surface it scans went away (mass revert,
    renamed helper, moved files). Mirrors tests/test_stable_sum_chokepoint.py:118.

    The floor sits below the 16 sites the #568 sweep created, so removing one
    legitimately does not turn the guard red — the precedent sets its floor the
    same way, and for the same reason.
    """
    calls = 0
    for path in _builder_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            named = (
                (isinstance(func, ast.Name) and func.id == "fixture_source_timestamp_z")
                or (
                    isinstance(func, ast.Attribute)
                    and func.attr == "fixture_source_timestamp_z"
                )
            )
            if named:
                calls += 1
    assert calls >= 12, (
        f"expected >=12 fixture_source_timestamp_z call sites across the "
        f"builders, found {calls} — the #568 sweep may have been reverted"
    )
