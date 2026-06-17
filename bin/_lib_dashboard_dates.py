"""Pure date-range parsing for dashboard browse filters (spec §2).

The CLI's ``_parse_cli_date_range`` is argparse-coupled and returns a CLI exit
code (Codex pre-plan P1 #3), so the dashboard cannot reuse it directly. This
module is the decoupled sibling: it maps a ``(date_from, date_to)`` pair to
UTC-ISO boundary strings that lexicographically compare against the stored
``last_activity_utc`` format (``YYYY-MM-DDTHH:MM:SS`` + a trailing ``Z``, whole
seconds when no microseconds), or raises ``ValueError`` — which the handler maps
to HTTP 400.

Semantics mirror the CLI's display-tz date posture (``docs/dashboard-gotchas``):
a naive *date-only* bound is interpreted in ``display.tz`` (start-of-day for the
lower bound, end-of-day for the upper), then converted to UTC for the
stored-UTC comparison; a *full-ISO* bound carries its own offset and bypasses
the dual-form parse (tz-independent). JSON timestamps stay ``...Z`` throughout.
"""
import datetime as dt
from zoneinfo import ZoneInfo

# Dual-form date spellings the CLI's ``_try_dual_form_date`` accepts for a
# naive date-only value (no time component): ISO ``YYYY-MM-DD`` and the compact
# ``YYYYMMDD``. Anything else is rejected as a malformed date.
_DUAL_FORMS = ("%Y-%m-%d", "%Y%m%d")


def _parse_one(raw):
    """``raw`` -> a naive ``datetime`` (date-only spelling, to be localized in
    display.tz by ``_to_utc_iso``) OR an aware ``datetime`` (full-ISO spelling,
    carries its own offset). ``None``/empty passes through as ``None``. Raises
    ``ValueError`` on an unrecognized spelling."""
    if raw is None or raw == "":
        return None
    # Full ISO (carries a time component and/or an explicit offset) — bypass the
    # dual-form parse so it stays tz-independent, matching the CLI's posture.
    if "T" in raw or "+" in raw or raw.endswith("Z"):
        return dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    for fmt in _DUAL_FORMS:
        try:
            return dt.datetime.strptime(raw, fmt)
        except ValueError:
            continue
    raise ValueError(f"bad date: {raw!r}")


def _to_utc_iso(d, tz, *, end_of_day):
    """An aware-or-naive ``datetime`` -> a UTC-ISO boundary string matching the
    stored ``last_activity_utc`` format (``...Z``, microseconds appended only
    when non-zero). A naive (date-only) value is localized in ``tz`` first:
    start-of-midnight for the lower bound, end-of-day (``23:59:59.999999``) for
    the upper, so the inclusive day boundary lands one tick before the next
    day's midnight."""
    if d.tzinfo is None:  # naive date-only -> localize in display tz
        if end_of_day:
            d = d.replace(hour=23, minute=59, second=59, microsecond=999999)
        d = d.replace(tzinfo=tz)
    u = d.astimezone(dt.timezone.utc)
    s = u.strftime("%Y-%m-%dT%H:%M:%S")
    if u.microsecond:
        s += f".{u.microsecond:06d}"
    return s + "Z"


def parse_filter_date_range(date_from, date_to, *, tz_name):
    """``(date_from, date_to)`` -> ``(start_iso|None, end_iso|None)``.

    Each input is an optional ``YYYY-MM-DD`` / ``YYYYMMDD`` (date-only,
    localized in ``tz_name``) or a full ISO-8601 string (carries its own
    offset). ``tz_name`` is an IANA key (e.g. ``Etc/UTC``, ``America/New_York``);
    a falsy ``tz_name`` defaults to UTC. Raises ``ValueError`` on a malformed
    date — the handler maps that to HTTP 400."""
    tz = ZoneInfo(tz_name) if tz_name else dt.timezone.utc
    df = _parse_one(date_from)
    dtt = _parse_one(date_to)
    start = _to_utc_iso(df, tz, end_of_day=False) if df is not None else None
    end = _to_utc_iso(dtt, tz, end_of_day=True) if dtt is not None else None
    return start, end
