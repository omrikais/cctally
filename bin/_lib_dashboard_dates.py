"""Pure date-range parsing for dashboard browse filters (spec §2).

The CLI's ``_parse_cli_date_range`` is argparse-coupled and returns a CLI exit
code (Codex pre-plan P1 #3), so the dashboard cannot reuse it directly. This
module is the decoupled sibling: it maps a ``(date_from, date_to)`` pair to
UTC-ISO boundary strings that lexicographically compare against the stored
``last_activity_utc`` / ``MAX(timestamp_utc)`` value, or raises ``ValueError`` —
which the handler maps to HTTP 400.

HALF-OPEN, precision-safe bounds (Task 2 review Finding 1)
----------------------------------------------------------
Stored timestamps are RAW JSONL passthrough of MIXED precision: both
whole-second ``YYYY-MM-DDTHH:MM:SSZ`` and millisecond
``YYYY-MM-DDTHH:MM:SS.mmmZ`` occur in real ``~/.claude/projects`` data. A
lexicographic SQL string compare on those values is only correct if the bounds
are chosen so the lex order matches the chronological order at the day edges.
ASCII ``Z`` (0x5A) sorts AFTER ``.`` (0x2E) and after the digits ``0``-``9``
(0x30-0x39), so a whole-second ``...00:00:00Z`` lower bound and an inclusive
``...23:59:59.999999Z`` upper bound BOTH mis-compare at the boundaries:

  * a midnight row ``...T00:00:00.000Z`` is wrongly EXCLUDED by ``>=
    ...T00:00:00Z`` (``.000Z`` < ``00Z`` because ``.`` < ``Z``);
  * a last-ms row ``...T23:59:59.999Z`` is wrongly EXCLUDED by ``<=
    ...T23:59:59.999999Z`` (``.999Z`` > ``.999999Z`` because ``Z`` > ``9``);
  * a whole-second ``...T23:59:59Z`` row is wrongly EXCLUDED by the same upper
    bound (``Z`` > ``.``).

The fix is a HALF-OPEN interval ``[start_of_day(date_from),
start_of_next_day(date_to))``:

  * lower bound = start-of-day of ``date_from`` (in ``display.tz`` → UTC),
    compared with ``>=``;
  * upper bound = start-of-day of ``(date_to + 1 day)`` (in ``display.tz`` →
    UTC), compared with a STRICT ``<`` (NOT the old inclusive end-of-day).

Both bounds are formatted with 6-digit microseconds ``...THH:MM:SS.000000Z``.
The lex algebra then holds for stored values of EVERY precision: ``.000000`` is
``<=`` any real fractional part and ``Z`` follows it, so a ``>=`` lower bound of
``<day>T00:00:00.000000Z`` includes the same-day midnight row at any precision,
and a strict-``<`` upper bound of ``<nextday>T00:00:00.000000Z`` includes every
same-day row (any precision) while excluding next-day midnight. The SQL callers
(``_lib_conversation_query._rollup_where`` and ``_live_having``) therefore use
``last_activity_utc >= ?`` for the lower bound and ``last_activity_utc < ?`` for
the upper.

Semantics mirror the CLI's display-tz date posture (``docs/dashboard-gotchas``):
a naive *date-only* bound is interpreted in ``display.tz`` (start-of-day for the
lower bound, start-of-NEXT-day for the upper), then converted to UTC for the
stored-UTC comparison; a *full-ISO* bound carries its own explicit offset and
bypasses the dual-form parse (tz-independent), used as a precise instant.
"""
import datetime as dt
from zoneinfo import ZoneInfo

# Dual-form date spellings the CLI's ``_try_dual_form_date`` accepts for a
# naive date-only value (no time component): ISO ``YYYY-MM-DD`` and the compact
# ``YYYYMMDD``. Anything else is rejected as a malformed date.
_DUAL_FORMS = ("%Y-%m-%d", "%Y%m%d")


def _has_explicit_offset(raw):
    """True iff ``raw`` carries an explicit UTC offset / ``Z`` — i.e. it is a
    PRECISE instant, not a wall-clock date(-time) needing localization.

    Finding 3: an offset-less ``T`` form like ``2026-06-15T08:30:00`` parses to a
    NAIVE datetime; the old predicate routed it to the full-ISO bypass and then
    the day-bound logic overwrote its time, silently discarding ``08:30:00``. We
    require a trailing ``Z`` or a ``+``/``-`` sign in the TIME portion (after the
    ``T``) before treating the value as a precise timestamp; an offset-less ``T``
    falls through to the date-only day-bound path (its date wins, its naive time
    is intentionally not used as a sub-day cut)."""
    if raw.endswith("Z"):
        return True
    t = raw.find("T")
    if t == -1:
        return False
    # A '+' or '-' in the time portion is an explicit offset (the date portion's
    # own '-' separators are before the 'T', so they never match here).
    tail = raw[t + 1:]
    return "+" in tail or "-" in tail


def _parse_one(raw):
    """``raw`` -> a naive ``datetime`` (date-only / offset-less spelling, to be
    localized in display.tz by ``_to_utc_iso``) OR an aware ``datetime``
    (full-ISO spelling WITH an explicit offset, carries its own instant).
    ``None``/empty passes through as ``None``. Raises ``ValueError`` on an
    unrecognized spelling.

    Finding 3: only an input with an explicit offset (``Z`` or a ``+``/``-`` in
    the time portion, or a parsed non-None ``tzinfo``) is treated as a precise
    instant; an offset-less ``…THH:MM:SS`` is parsed but returned NAIVE so the
    day-bound localization applies (its date is what matters)."""
    if raw is None or raw == "":
        return None
    # Precise instant: explicit offset present — bypass the dual-form parse so it
    # stays tz-independent, matching the CLI's full-ISO posture.
    if _has_explicit_offset(raw):
        d = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if d.tzinfo is not None:
            return d
        # Belt-and-suspenders: a recognized-offset spelling that still parsed
        # naive (should not happen) degrades to the day-bound path below.
        return d
    if "T" in raw:
        # Offset-less datetime (e.g. 2026-06-15T08:30:00) -> NAIVE; the day-bound
        # path uses only its date (Finding 3 — do not silently keep the time).
        return dt.datetime.fromisoformat(raw)
    for fmt in _DUAL_FORMS:
        try:
            return dt.datetime.strptime(raw, fmt)
        except ValueError:
            continue
    raise ValueError(f"bad date: {raw!r}")


def _utc_micro_iso(u):
    """Format an aware UTC ``datetime`` as the lex-safe boundary string
    ``YYYY-MM-DDTHH:MM:SS.000000Z`` (always 6-digit microseconds + ``Z``)."""
    return u.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _to_utc_iso(d, tz, *, upper):
    """An aware-or-naive ``datetime`` -> a half-open UTC-ISO boundary string.

    Naive (date-only / offset-less) value: localized in ``tz``, snapped to
    start-of-day; for the UPPER bound it is the start of the NEXT day (so the
    strict-``<`` comparison covers the whole requested last day). Aware value
    (explicit-offset full-ISO): used as the precise instant verbatim, converted
    to UTC. Always emits 6-digit-microsecond ``...Z`` so the lex compare is
    precision-safe against any stored value (see module docstring)."""
    if d.tzinfo is None:  # naive date-only / offset-less -> localize in display tz
        d = d.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=tz)
        if upper:
            # start-of-NEXT-day: half-open exclusive upper. Add a day in the LOCAL
            # zone (DST-correct: re-localize the resulting wall date so a 23/25h
            # day still lands on the next midnight) before converting to UTC.
            nxt = (d + dt.timedelta(days=1)).date()
            d = dt.datetime(nxt.year, nxt.month, nxt.day, tzinfo=tz)
    u = d.astimezone(dt.timezone.utc)
    return _utc_micro_iso(u)


def parse_filter_date_range(date_from, date_to, *, tz_name):
    """``(date_from, date_to)`` -> ``(start_iso|None, end_iso|None)``.

    The returned pair is a HALF-OPEN interval ``[start, end)``: callers compare
    ``stored >= start`` (inclusive lower) and ``stored < end`` (EXCLUSIVE upper —
    ``end`` is the start of the day AFTER ``date_to``, NOT an inclusive
    end-of-day). Both bounds are 6-digit-microsecond ``...Z`` strings chosen so a
    lexicographic compare against the mixed-precision stored timestamps is
    chronologically correct at the day edges (see the module docstring).

    Each input is an optional ``YYYY-MM-DD`` / ``YYYYMMDD`` (date-only,
    localized in ``tz_name``), an offset-less ``...THH:MM:SS`` (date-only day
    bound — its naive time is NOT used, Finding 3), or a full ISO-8601 string
    WITH an explicit offset (used as a precise instant; the upper stays
    exclusive). ``tz_name`` is an IANA key (e.g. ``Etc/UTC``,
    ``America/New_York``); a falsy ``tz_name`` defaults to UTC. Raises
    ``ValueError`` on a malformed date — the handler maps that to HTTP 400."""
    tz = ZoneInfo(tz_name) if tz_name else dt.timezone.utc
    df = _parse_one(date_from)
    dtt = _parse_one(date_to)
    start = _to_utc_iso(df, tz, upper=False) if df is not None else None
    end = _to_utc_iso(dtt, tz, upper=True) if dtt is not None else None
    return start, end
