"""Provider samples: the raw api observation as an OAuth sample (#797/#823/#840
spec 4.3, D8 and D9).

Pure-fn layer (no I/O, no sibling imports). Every OAuth fetch this binary
performs records ``q``, the wall-clock instant immediately before the request
starts, and publishes it as the additive raw api ``obs`` payload key
``oauth_request_started_at`` (:data:`REQUEST_STARTED_KEY`). The provider
sampled the response at some instant at or after ``q`` (assumption A-Q), so a
sample whose ``q`` is after a contributor's provider-activity instant is a
reading the contributor could not have seen. This module turns one raw
payload into one :class:`OAuthSample` — the row shape of the stats table
``claude_oauth_samples`` — and states window identity once.

Values are the RAW payload values, never the MAX-clamped stored snapshot
values: the weekly percent and its reset (nearest-hour normalized exactly as
the week-boundary code does), the five-hour percent and its raw reset, and the
authoritative five-hour window key. That key is the producer's frozen planned
key when the payload carries a valid one (an OAuth publication always does
when it carries five-hour fields), else the pure canonical floor; a planned
key that contradicts its own raw reset is conflicting identity and yields no
key, so it can never supply a five-hour proof.

Instants: ``requested_at_utc`` is stored in one canonical full-precision form
(``YYYY-MM-DDTHH:MM:SS.ffffffZ``), so lexicographic order is chronological and
a strict ``q > t`` comparison is exact. ``captured_at_utc`` is the payload's
capture stamp verbatim (seconds precision, truncated): for an integral reset
``R``, ``captured < R`` holds exactly when the true completion instant is
before ``R``, so the truncation never weakens the reset bound.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import math
import re

#: The raw api observation payload key carrying ``q``.
REQUEST_STARTED_KEY = "oauth_request_started_at"
#: The one source an OAuth publication records.
API_SOURCE = "api"
#: `_lib_five_hour._FIVE_HOUR_JITTER_FLOOR_SECONDS`, restated because this
#: module imports no sibling; `tests/test_oauth_sample_projection_797.py`
#: pins the equality.
FIVE_HOUR_JITTER_FLOOR_SECONDS = 600
#: The tolerance a planned five-hour key may sit from its raw reset — the
#: same bound `_pipeline_claude_usage` validates a planned key against.
PLANNED_KEY_TOLERANCE_SECONDS = FIVE_HOUR_JITTER_FLOOR_SECONDS * 3
#: The capture stamp is truncated to the second, so a genuine ``q`` can sit
#: up to one second after it.
_CAPTURE_TRUNCATION_SECONDS = 1

_REQUEST_STARTED = re.compile(
    r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d{1,6})?Z$")
_CANONICAL_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
_SECONDS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


@dataclasses.dataclass(frozen=True)
class OAuthSample:
    """One provider sample: a row of ``claude_oauth_samples``."""

    account_key: str
    observation_id: str
    requested_at_utc: str
    captured_at_utc: str
    weekly_percent: "float | None"
    weekly_resets_at_utc: "str | None"
    five_hour_percent: "float | None"
    five_hour_resets_at_utc: "str | None"
    five_hour_window_key: "int | None"

    def row(self) -> tuple:
        """The column values in table order."""
        return tuple(getattr(self, f.name) for f in dataclasses.fields(self))


def format_request_started(instant: dt.datetime) -> str:
    """``instant`` (tz-aware) in the carrier's canonical full-precision UTC
    form. A naive datetime is a programming error and raises."""
    if instant.tzinfo is None:
        raise ValueError("the request-start instant must be timezone-aware")
    return instant.astimezone(dt.timezone.utc).strftime(_CANONICAL_FORMAT)


def _parse_aware(value) -> "dt.datetime | None":
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(
            value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(dt.timezone.utc)


def canonical_instant(value) -> str:
    """Any tz-aware ISO-8601 instant (or tz-aware datetime) in the canonical
    full-precision form used for ``requested_at_utc``; raises ``ValueError``
    on anything else. Callers compare their own instants against stored
    ``q`` values through this form."""
    if isinstance(value, dt.datetime):
        return format_request_started(value)
    parsed = _parse_aware(value)
    if parsed is None:
        raise ValueError(f"not a timezone-aware ISO-8601 instant: {value!r}")
    return parsed.strftime(_CANONICAL_FORMAT)


def parse_request_started(value) -> "str | None":
    """The canonical ``q`` of a raw payload value, or None when it is missing
    or unparsable. Only the carrier's own shape is accepted (UTC with ``Z``,
    up to microsecond precision); a bool, a number or a naive instant is not
    a key."""
    if not isinstance(value, str) or not _REQUEST_STARTED.match(value):
        return None
    parsed = _parse_aware(value)
    return None if parsed is None else parsed.strftime(_CANONICAL_FORMAT)


def normalize_weekly_reset(resets_at_epoch: int) -> str:
    """The weekly window identity of a raw ``resets_at`` epoch: rounded to the
    nearest hour exactly as `_cctally_core._normalize_week_boundary_dt` does
    (minutes 0..29 down, 30..59 up), as ``YYYY-MM-DDTHH:00:00Z``."""
    value = dt.datetime.fromtimestamp(int(resets_at_epoch), tz=dt.timezone.utc)
    normalized = value.replace(minute=0, second=0, microsecond=0)
    if value.minute >= 30:
        normalized += dt.timedelta(hours=1)
    return normalized.strftime(_SECONDS_FORMAT)


def weekly_window(window) -> str:
    """A weekly window argument (a raw or normalized reset as an epoch int or
    a tz-aware ISO instant) as its normalized identity."""
    if isinstance(window, bool):
        raise ValueError("a weekly window is a reset instant, not a bool")
    if isinstance(window, int):
        return normalize_weekly_reset(window)
    parsed = _parse_aware(window)
    if parsed is None:
        raise ValueError(f"not a weekly reset instant: {window!r}")
    return normalize_weekly_reset(int(parsed.timestamp()))


def five_hour_window_key(resets_at_epoch: int, planned) -> "int | None":
    """The authoritative five-hour window key: the producer's planned key
    when it is an int within the planned-key tolerance of the raw reset,
    None when a planned key is present but contradicts it, else the pure
    canonical floor (`_canonical_5h_window_key` without an anchor)."""
    if planned is not None:
        if (type(planned) is not int
                or abs(planned - resets_at_epoch)
                > PLANNED_KEY_TOLERANCE_SECONDS):
            return None
        return planned
    return resets_at_epoch - (resets_at_epoch % FIVE_HOUR_JITTER_FLOOR_SECONDS)


def _percent(value) -> "float | None":
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _bound_account(payload: dict) -> "str | None":
    binding = payload.get("account_binding")
    if not isinstance(binding, dict):
        return None
    key = binding.get("accountKey")
    return key if isinstance(key, str) and key else None


def sample_from_observation(payload: dict, *, account_key: str,
                            observation_id: str,
                            captured_at_utc: str) -> "OAuthSample | None":
    """The provider sample of one raw Claude usage observation payload, or
    None: for a non-api source, a missing or unparsable request-start key, a
    request instant after the capture, a missing or unparsable capture
    instant, or no Claude account binding naming ``account_key``.

    Each axis is independent: an unparsable weekly or five-hour field leaves
    that axis's columns None (no identity, so no proof on that axis), and a
    sample with neither axis is no sample."""
    if not isinstance(payload, dict) or payload.get("source") != API_SOURCE:
        return None
    if not isinstance(account_key, str) or not account_key:
        return None
    if not isinstance(observation_id, str) or not observation_id:
        return None
    if _bound_account(payload) != account_key:
        return None
    requested = parse_request_started(payload.get(REQUEST_STARTED_KEY))
    captured = _parse_aware(captured_at_utc)
    if requested is None or captured is None:
        return None
    requested_dt = _parse_aware(requested)
    if (requested_dt - captured).total_seconds() > _CAPTURE_TRUNCATION_SECONDS:
        return None

    weekly_percent = _percent(payload.get("weekly_percent"))
    weekly_reset = None
    resets_at = payload.get("resets_at")
    if not isinstance(resets_at, bool):
        try:
            weekly_reset = normalize_weekly_reset(int(resets_at))
        except (TypeError, ValueError, OverflowError, OSError):
            weekly_reset = None
    if weekly_reset is None:
        weekly_percent = None

    five_percent = _percent(payload.get("five_hour_percent"))
    five_reset = None
    five_key = None
    five_reset_dt = _parse_aware(payload.get("five_hour_resets_at"))
    if five_reset_dt is not None:
        five_reset = five_reset_dt.strftime(_SECONDS_FORMAT)
        five_key = five_hour_window_key(
            int(five_reset_dt.timestamp()),
            payload.get("five_hour_window_key"))
    if five_percent is None:
        five_reset = None
        five_key = None

    if weekly_percent is None and five_percent is None:
        return None
    return OAuthSample(
        account_key=account_key,
        observation_id=observation_id,
        requested_at_utc=requested,
        captured_at_utc=captured_at_utc,
        weekly_percent=weekly_percent,
        weekly_resets_at_utc=weekly_reset,
        five_hour_percent=five_percent,
        five_hour_resets_at_utc=five_reset,
        five_hour_window_key=five_key,
    )
