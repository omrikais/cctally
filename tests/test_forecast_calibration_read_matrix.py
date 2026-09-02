"""#661 S2 §13 — the calibration-read matrix ACROSS the read, not after it.

`tests/test_forecast_quota_envelope.py` covers the matrix downstream of the
read: it builds a `ForecastInputs` with `calibrated_withheld_code` already set
and asserts the envelope echoes it. That answers "given a cause code, the
envelope renders it" and says nothing about "an absent, unreadable,
quarantined, version-ahead, stale or unsupported-population calibration file
actually produces that code". Before this module, `_calibrated_week_detail`
and `_CALIBRATION_REJECTION_CODES` appeared in no test at all, so the
dashboard's leg of §13's matrix was covered to a different depth than the
status line's — whose leg is real, seeding a corrupt file under
`tests/fixtures/statusline/extensions-quota-file-malformed/`.

Every case here writes a REAL file at the shipped `calibration_path()` and
calls the shipped `_calibrated_week_detail`. Nothing about the reader, the
validator or the apply adapter is stubbed. The one stub is the entry
population for the support leg, and it is a real list of kernel records.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib

import sys
from pathlib import Path

import pytest

import _cctally_core
from conftest import load_script, redirect_paths

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 6, 12, 0, tzinfo=UTC)
WEEK_START = dt.datetime(2026, 6, 29, tzinfo=UTC)
WEEK_END = WEEK_START + dt.timedelta(days=7)

SUPPORTED_MODEL = "claude-opus-4-5"
#: A different family, so `aggregate_composition` puts the whole population's
#: weighted units on another share and the support distance reaches 1.0.
DRIFTED_MODEL = "claude-sonnet-4-5"


@pytest.fixture
def ns(monkeypatch, tmp_path):
    namespace = load_script()
    redirect_paths(namespace, monkeypatch, tmp_path)
    monkeypatch.setenv("CCTALLY_DISABLE_UPDATE_CHECK", "1")
    monkeypatch.setenv("CCTALLY_DISABLE_TELEMETRY", "1")
    return namespace


def _path():
    import _cctally_quota_model as qmg
    return qmg.calibration_path()


def _records(model):
    import _lib_quota_model as qm
    return [qm.EntryRecord(at=WEEK_START + dt.timedelta(days=1), model=model,
                           fresh=200_000, output=40_000,
                           cache_create_total=0, cache_1h=0, cache_read=0)]


def _regime(**over):
    """One prediction-ready stored regime, centred on `SUPPORTED_MODEL`."""
    import _lib_quota_model as qm
    probe = _records(SUPPORTED_MODEL)
    family_shares, class_shares = qm.aggregate_composition(probe)
    per_point = qm.weighted_units(probe[0]) * 3.0 / 40.0
    record = {
        "effectiveFrom": (WEEK_START - dt.timedelta(days=30)).isoformat(),
        "effectiveUntil": None,
        "fingerprint": qm.QUOTA_MODEL_CONSTANTS_FINGERPRINT,
        "algorithmRevision": qm.QUOTA_MODEL_ALGORITHM_REVISION,
        "unitsPerPoint": per_point,
        "interval": {"lo": per_point * 0.95, "hi": per_point * 1.05},
        "support": {"days": 26, "segments": 4},
        "status": "ok",
        "asOf": WEEK_START.isoformat(),
        "qualifications": [],
        "familyShares": dict(family_shares),
        "classShares": dict(class_shares),
        "familyRadius": 0.05,
        "classRadius": 0.05,
    }
    record.update(over)
    return record


def _write(state):
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    return path


def _write_regime(**over):
    import _cctally_quota_model as qmg
    return _write({
        "schemaVersion": qmg.CALIBRATION_STATE_SCHEMA_VERSION,
        "accounts": {qmg.MERGED_STATE_KEY: {"regimes": [_regime(**over)]}},
    })


def _detail(ns, *, population=None):
    """Drive the shipped producer. The entry read is the ONLY substitution,
    and it hands the real kernel records the real adapter consumes."""
    forecast = sys.modules["_cctally_forecast"]
    records = _records(population) if population is not None else []
    original = forecast._week_entry_records
    forecast._week_entry_records = (
        lambda _start, _end, **_kwargs: list(records))
    try:
        return forecast._calibrated_week_detail(
            NOW, WEEK_START, WEEK_END, account_key=None)
    finally:
        forecast._week_entry_records = original


# --------------------------------------------------------------------------
# The six states
# --------------------------------------------------------------------------
def test_an_absent_file_states_unavailable(ns):
    assert not _path().exists()
    assert _detail(ns).code == "unavailable"


def test_an_unreadable_file_states_unavailable(ns):
    """The `OSError`-on-open leg — the one `read_calibration_file` maps to
    `RegimeRejection.UNREADABLE` rather than to `ABSENT`.

    A symlink loop rather than `chmod 000`, because the errno must not depend
    on the uid the suite runs under: root reads a mode-0 file and the case
    would then exercise the usable path while still passing under a
    less-privileged runner. `ELOOP` raises for every user.

    The reader deliberately cannot tell an unreadable file from an absent one
    without scanning quarantine sidecars, and it does not scan them, so both
    render the same word.
    """
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(str(path), str(path))
    assert _detail(ns, population=SUPPORTED_MODEL).code == "unavailable"


def test_a_quarantined_file_states_unavailable(ns):
    """A quarantine renames the primary name aside. The sidecar is present,
    the primary is not, and this reader never scans for the sidecar."""
    primary = _write_regime()
    primary.rename(primary.with_name(
        f"{primary.name}.quarantined-20260706T120000Z"))
    assert not primary.exists()
    assert _detail(ns, population=SUPPORTED_MODEL).code == "unavailable"


def test_a_version_ahead_file_states_unavailable(ns):
    """`_CALIBRATION_REJECTION_CODES` maps only the fingerprint and revision
    mismatches to `stale`; every other rejection is reported as `unavailable`.
    A version-ahead file is NOT reported as `future` — that member of
    `EVIDENCE_CODES` is the kernel's word for an observation captured ahead of
    the clock, not for a file written by a newer binary."""
    import _cctally_quota_model as qmg
    _write({
        "schemaVersion": qmg.CALIBRATION_STATE_SCHEMA_VERSION + 1,
        "accounts": {qmg.MERGED_STATE_KEY: {"regimes": [_regime()]}},
    })
    assert _detail(ns, population=SUPPORTED_MODEL).code == "unavailable"


def test_a_malformed_file_states_unavailable(ns):
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert _detail(ns, population=SUPPORTED_MODEL).code == "unavailable"


@pytest.mark.parametrize("field,value", [
    ("fingerprint", "not-this-binarys-fingerprint"),
    ("algorithmRevision", 999_999),
])
def test_a_regime_fitted_under_other_constants_states_stale(ns, field, value):
    """The one rejection with its own word: `stale` is the union's term for
    "fitted under other constants"."""
    _write_regime(**{field: value})
    assert _detail(ns, population=SUPPORTED_MODEL).code == "stale"


def test_an_unsupported_population_states_the_apply_rejection(ns):
    """The regime validates; the week's own composition sits outside its
    recorded radii, so the APPLY adapter refuses and its own value is the
    cause. This is the leg the read-only states cannot reach."""
    _write_regime()
    assert _detail(ns, population=DRIFTED_MODEL).code == \
        "unsupported-composition"


def test_an_empty_population_states_no_local_history(ns):
    """The apply adapter's other rejection, on the same real file."""
    _write_regime()
    assert _detail(ns, population=None).code == "no-local-history"


def test_a_detection_only_regime_states_the_stored_status(ns):
    """S1 holds a successor fit below its prediction gate and records the
    blocking status. The producer states that status rather than a generic
    word, so the user can reconcile it against `cctally quota`."""
    _write_regime(qualifications=["detection-only"], status="unstable-fit")
    assert _detail(ns, population=SUPPORTED_MODEL).code == "unstable-fit"


def test_a_detection_only_regime_with_an_alien_status_falls_back(ns):
    """The floor under the stored status: a status outside the closed union
    cannot be published as a cause."""
    _write_regime(qualifications=["detection-only"], status="ok")
    assert _detail(ns, population=SUPPORTED_MODEL).code == \
        "insufficient-history"


# --------------------------------------------------------------------------
# The usable state, so none of the above is vacuous
# --------------------------------------------------------------------------
def test_a_usable_calibration_yields_the_modelled_triple_and_no_code(ns):
    """Non-vacuity for the whole module. A producer that answered a cause on
    every input would satisfy every case above."""
    _write_regime()
    detail = _detail(ns, population=SUPPORTED_MODEL)
    assert detail.code is None
    assert detail.projection_pct is not None
    assert detail.consumption_pct is not None
    assert detail.headroom_pct is not None


# --------------------------------------------------------------------------
# Every return path yields the declared type
# --------------------------------------------------------------------------
def test_every_return_path_of_the_producer_yields_a_calibrated_week():
    """`_calibrated_week_detail` is annotated `-> CalibratedWeek` and its
    caller `_calibrated_projection` reads `detail.projection_pct`. One early
    return still yielded the two-value tuple the pre-`CalibratedWeek` shape
    used, which raises `AttributeError` rather than withholding — the same
    defect class as a `persist_and_detect` early return whose arity does not
    match the rest of the function. That function now returns THREE values,
    and its unreadable-state path returns `None, (), ()` for this reason.
    """
    import ast
    # Resolved from `_cctally_core`, which is imported at module scope, rather
    # than from `sys.modules["_cctally_forecast"]`. Reading the latter passes
    # only when a sibling test in this module has already called
    # `load_script`, and `bin/cctally-test-all` runs pytest under `-n` with
    # xdist's default `load` scheduler, which distributes individual tests --
    # so this one can land in a worker where no sibling ran and raise
    # `KeyError`. The twin structural test in
    # `tests/test_meter_rate_change_journal.py` already spells it this way.
    source = (pathlib.Path(_cctally_core.__file__).resolve().parent
              / "_cctally_forecast.py").read_text(encoding="utf-8")
    target = next(
        node for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef)
        and node.name == "_calibrated_week_detail")
    returns = [n for n in ast.walk(target) if isinstance(n, ast.Return)]
    assert returns
    for node in returns:
        assert isinstance(node.value, ast.Call), (
            f"line {node.lineno}: returns a {type(node.value).__name__}, "
            f"not a CalibratedWeek(...)")
        assert isinstance(node.value.func, ast.Name), node.lineno
        assert node.value.func.id == "CalibratedWeek", (
            f"line {node.lineno}: returns {node.value.func.id}(...)")
