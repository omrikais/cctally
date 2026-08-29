"""The non-mutating read of the persisted quota calibration (#661 S2).

The only I/O half of the §1.1 adapters: open the sidecar, hand its bytes to
the pure validator in `bin/_lib_quota_calibration.py`, and report a typed
cause when anything about it is unusable.

WHY THIS EXISTS RATHER THAN A CALL TO `load_calibrations`. That loader
QUARANTINES a malformed or version-ahead file by renaming it aside. `doctor`
is documented read-only, the status line runs per prompt, and the dashboard
serves a read request — a rename from any of them makes a documented reader a
writer, and the rename is invisible to the user who triggered it. This module
therefore never imports or calls `load_calibrations`, `_quarantine`, or
`save_calibrations`, and takes no lock: `save_calibrations` publishes through
`os.replace` in the same directory, so a lock-free reader always observes a
complete old or new inode, and a flock here would let a writer stall a prompt.

Spec: docs/superpowers/specs/2026-08-28-661-s2-quota-insight-surfaces.md §1.1
"""
from __future__ import annotations

import dataclasses
import json

import _cctally_quota_model
import _lib_quota_model as qm
from _lib_quota_calibration import (  # re-exported for callers and tests
    AppliedQuota, ApplyRejection, RegimeRejection, ValidatedRegime,
    apply_regime, validate_regime,
)

__all__ = [
    "AppliedQuota", "ApplyRejection", "CalibrationRead", "RegimeRejection",
    "ValidatedRegime", "apply_regime", "read_calibration_file",
    "validate_regime",
]


@dataclasses.dataclass(frozen=True)
class CalibrationRead:
    """Exactly one of `regime` and `rejection` is set.

    `regime_status` carries the stored `status` of the regime the prediction
    gate refused, and is `None` in every other outcome. S1 records WHY it held
    a successor fit below the gate, and a surface that states
    `insufficient-history` while the store says `unstable-fit` names a cause
    the reader cannot reconcile with `cctally quota`.
    """

    regime: "ValidatedRegime | None"
    rejection: "RegimeRejection | None"
    regime_status: "str | None" = None


def _read_text(path) -> str:
    """Open and read in one step, with NO prior existence check.

    A concurrent quarantine rename can remove the primary name between a
    check and an open, so a `path.exists()` guard would turn a race into a
    traceback. The absent case arrives here as `OSError` like every other
    failure and is separated by its errno at the call site.
    """
    with open(str(path), "r", encoding="utf-8") as handle:
        return handle.read()


def _state_key(account_key) -> str:
    """The stored bucket name for an account. `None` means the merged view."""
    return (_cctally_quota_model.MERGED_STATE_KEY if account_key is None
            else str(account_key))


def read_calibration_file(*, account_key, path=None,
                          expected_fingerprint=None,
                          expected_revision=None,
                          require_prediction_ready=True) -> CalibrationRead:
    """The open regime for one account, validated, or a typed cause.

    `expected_fingerprint` and `expected_revision` default to THIS binary's
    constants, so a caller that names neither still refuses a regime fitted
    under other coefficients rather than applying it silently.

    `require_prediction_ready` defaults to True, so every projection consumer
    is refused a regime S1 marked `detection-only`. A detection consumer —
    section 6's rate-change alert — passes False and reads the same record.
    """
    if path is None:
        path = _cctally_quota_model.calibration_path()
    if expected_fingerprint is None:
        expected_fingerprint = qm.QUOTA_MODEL_CONSTANTS_FINGERPRINT
    if expected_revision is None:
        expected_revision = qm.QUOTA_MODEL_ALGORITHM_REVISION

    try:
        raw = _read_text(path)
    except FileNotFoundError:
        return CalibrationRead(None, RegimeRejection.ABSENT)
    except (IsADirectoryError, NotADirectoryError):
        return CalibrationRead(None, RegimeRejection.ABSENT)
    except OSError:
        return CalibrationRead(None, RegimeRejection.UNREADABLE)

    try:
        state = json.loads(raw)
    except ValueError:
        return CalibrationRead(None, RegimeRejection.MALFORMED)
    if not isinstance(state, dict):
        return CalibrationRead(None, RegimeRejection.MALFORMED)

    version = state.get("schemaVersion")
    if isinstance(version, bool) or not isinstance(version, int) \
            or version > _cctally_quota_model.CALIBRATION_STATE_SCHEMA_VERSION:
        return CalibrationRead(None, RegimeRejection.SCHEMA_VERSION)

    accounts = state.get("accounts")
    if not isinstance(accounts, dict):
        return CalibrationRead(None, RegimeRejection.MALFORMED)
    bucket = accounts.get(_state_key(account_key))
    regimes = bucket.get("regimes") if isinstance(bucket, dict) else None
    if not isinstance(regimes, list):
        return CalibrationRead(None, RegimeRejection.ABSENT)

    # The LAST open regime, matching `_cctally_quota_model._open_regime`. A
    # store carrying several open records is malformed history rather than a
    # choice this reader makes, and the newest one is what a writer would
    # have updated in place.
    open_regime = None
    for candidate in reversed(regimes):
        if isinstance(candidate, dict) and candidate.get("effectiveUntil") \
                is None:
            open_regime = candidate
            break
    if open_regime is None:
        return CalibrationRead(None, RegimeRejection.ABSENT)

    outcome = validate_regime(
        open_regime, account_key=account_key,
        expected_fingerprint=expected_fingerprint,
        expected_revision=expected_revision,
        require_prediction_ready=require_prediction_ready)
    if isinstance(outcome, RegimeRejection):
        status = None
        if outcome is RegimeRejection.DETECTION_ONLY:
            stored = open_regime.get("status")
            status = str(stored) if stored is not None else None
        return CalibrationRead(None, outcome, status)
    return CalibrationRead(outcome, None)
