"""Write-budget kernel for the dashboard's disk writes (#901 spec §5.6).

Stdlib-only leaf shared by the dashboard (`/api/debug/backend` `writeIo`), the
doctor check `performance.dashboard_disk_writes`, `dashboard-perf` and the
soak write-budget leg. Every function is pure: nothing here reads a clock, a
file, a counter or a database.

Two statistics with two windows:

* STEADY STATE (I2): a five-minute trailing window over one cumulative process
  write counter. Bytes inside a marked deletion operation (its BEGIN to the
  return of its COMMIT) are subtracted and reported beside it; everything else
  counts, a reclaim chunk and a delayed checkpoint copy included (Q7).
* MAINTENANCE (I4): the durable charge ledger — charged bytes, never measured
  bytes — over the current partial UTC hour plus the 24 complete hours before
  it. A correct pacer can never exceed its allowance, so exceeding it means
  pacing is broken.

The I2 caps are fixed and never move, and `LIMITS` equals them (Q20). The
§6.3 calibration (twice the largest healthy five-minute measurement of three
repetitions of each frozen workload, rounded up to whole MiB) is reported as a
diagnostic and never sets a limit: frozen inputs carry only the controlled
append, and the operator's real activity measured above it, within the caps.
A limit above a cap is refused by `validate_limits`; a measurement above a cap
means the outcome is not achieved, and that is escalated rather than absorbed.
"""
from __future__ import annotations

import dataclasses
import datetime as dt

KiB = 1024
MiB = 1024 * KiB
GiB = 1024 * MiB
UTC = dt.timezone.utc
_NS = 1_000_000_000

#: I2 caps — FIXED by the specification.
CAP_BYTES_PER_MINUTE = 16 * MiB
CAP_BYTES_PER_PUBLICATION = 8 * MiB


@dataclasses.dataclass(frozen=True)
class BudgetLimits:
    policy_version: int
    bytes_per_minute: int
    bytes_per_publication: int


def validate_limits(limits: BudgetLimits) -> BudgetLimits:
    """Refuse a non-positive limit and any limit above its I2 cap."""
    if limits.bytes_per_minute <= 0 or limits.bytes_per_publication <= 0:
        raise ValueError("write-budget limits must be positive")
    if limits.bytes_per_minute > CAP_BYTES_PER_MINUTE:
        raise ValueError(
            f"bytes_per_minute {limits.bytes_per_minute} exceeds the I2 cap "
            f"{CAP_BYTES_PER_MINUTE}")
    if limits.bytes_per_publication > CAP_BYTES_PER_PUBLICATION:
        raise ValueError(
            f"bytes_per_publication {limits.bytes_per_publication} exceeds "
            f"the I2 cap {CAP_BYTES_PER_PUBLICATION}")
    return limits


# ── Versioned limits: the I2 caps (Q20); calibration is diagnostic only ────
POLICY_VERSION = 1
LIMITS = validate_limits(BudgetLimits(
    policy_version=POLICY_VERSION,
    bytes_per_minute=16 * MiB,
    bytes_per_publication=8 * MiB,
))
# ───────────────────────────────────────────────────────────────────────────

WINDOW_SECONDS = 300
MIN_COUNTER_SAMPLES = 6
MIN_PUBLICATIONS = 10
MAX_SAMPLE_GAP_SECONDS = 60
EXCLUDED_TIME_MAX_FRACTION = 0.20
HYSTERESIS_SPACING_SECONDS = 60
CLEAR_FRACTION = 0.80

#: I4 allowance — the one home for the pacer and the statistic.
MAINTENANCE_RATE_BYTES_PER_MINUTE = 4 * MiB
MAINTENANCE_CREDIT_BYTES = 4 * MiB
LEDGER_BUCKETS = 25


@dataclasses.dataclass(frozen=True)
class CounterSample:
    t_ns: int
    bytes: "int | None"
    publications: int = 0


@dataclasses.dataclass(frozen=True)
class DeletionInterval:
    start_ns: int
    end_ns: int
    start_bytes: "int | None"
    end_bytes: "int | None"
    rows: int = 0
    ended_wall_s: "int | None" = None


@dataclasses.dataclass(frozen=True)
class SteadyStatistic:
    status: str                      # "qualified" | "insufficient" | "unavailable"
    reasons: "tuple[str, ...]" = ()
    window_seconds: "float | None" = None
    bytes_written: "int | None" = None
    bytes_per_minute: "int | None" = None
    rate_qualified: bool = False
    publications: "int | None" = None
    mean_bytes_per_publication: "int | None" = None
    publication_qualified: bool = False
    excluded_operations: int = 0
    excluded_rows: int = 0
    excluded_bytes: int = 0
    excluded_seconds: float = 0.0
    last_excluded_wall_s: "int | None" = None
    sample_count: int = 0


def steady_statistic(samples, deletions=(), *, now_ns, warm_admitted_ns,
                     counter_status="ok") -> SteadyStatistic:
    """The trailing five-minute statistic, or the typed reason it has none."""
    if counter_status != "ok":
        return SteadyStatistic("unavailable", (str(counter_status),))
    start_ns = now_ns - WINDOW_SECONDS * _NS
    gap_ns = MAX_SAMPLE_GAP_SECONDS * _NS
    if warm_admitted_ns is None or warm_admitted_ns > start_ns:
        return SteadyStatistic("insufficient", ("warming_up",))
    ordered = sorted(samples, key=lambda s: s.t_ns)
    if any(s.bytes is None and start_ns - gap_ns <= s.t_ns <= now_ns
           for s in ordered):
        return SteadyStatistic("insufficient", ("invalid_samples",))
    valid = [s for s in ordered if s.bytes is not None and s.t_ns <= now_ns]
    base = None
    for sample in valid:
        if sample.t_ns <= start_ns:
            base = sample
    if base is None or start_ns - base.t_ns > gap_ns:
        return SteadyStatistic("insufficient", ("insufficient_coverage",))
    span = [s for s in valid if s.t_ns >= base.t_ns]
    end = span[-1]
    if now_ns - end.t_ns > gap_ns or any(
            b.t_ns - a.t_ns > gap_ns for a, b in zip(span, span[1:])):
        return SteadyStatistic("insufficient", ("insufficient_coverage",))
    if len(span) < MIN_COUNTER_SAMPLES:
        return SteadyStatistic("insufficient", ("insufficient_samples",))
    if any(b.bytes < a.bytes for a, b in zip(span, span[1:])):
        return SteadyStatistic("unavailable", ("counter_reset",))
    excluded_bytes = 0
    excluded_ns = 0
    operations = 0
    rows = 0
    last_wall = None
    for d in deletions:
        overlap_ns = max(0, min(d.end_ns, end.t_ns) - max(d.start_ns, base.t_ns))
        if d.start_bytes is not None and d.end_bytes is not None:
            overlap_bytes = max(0, min(d.end_bytes, end.bytes)
                                - max(d.start_bytes, base.bytes))
        else:
            overlap_bytes = 0
        if overlap_ns <= 0 and overlap_bytes <= 0:
            continue
        operations += 1
        rows += max(0, int(d.rows))
        excluded_ns += overlap_ns
        excluded_bytes += overlap_bytes
        if d.ended_wall_s is not None:
            last_wall = max(last_wall or 0, int(d.ended_wall_s))
    elapsed_ns = end.t_ns - base.t_ns
    written = end.bytes - base.bytes - excluded_bytes
    publications = end.publications - base.publications
    common = dict(
        window_seconds=elapsed_ns / _NS,
        bytes_written=written,
        bytes_per_minute=int(round(written * 60 * _NS / elapsed_ns)),
        publications=publications,
        mean_bytes_per_publication=(
            int(round(written / publications)) if publications > 0 else None),
        excluded_operations=operations,
        excluded_rows=rows,
        excluded_bytes=excluded_bytes,
        excluded_seconds=excluded_ns / _NS,
        last_excluded_wall_s=last_wall,
        sample_count=len(span),
    )
    if excluded_ns > EXCLUDED_TIME_MAX_FRACTION * elapsed_ns:
        return SteadyStatistic("insufficient", ("excluded_time_over_limit",),
                               **common)
    return SteadyStatistic(
        "qualified", (), rate_qualified=True,
        publication_qualified=publications >= MIN_PUBLICATIONS, **common)


#: PR-8: the plain phrase for each reason `steady_statistic` gives no rate
#: or verdict for. Only `warming_up` is the first five minutes; the others
#: can hold on a dashboard that has run for hours. The doctor summary and the
#: `dashboard-perf` row share them.
STATISTIC_REASON_PHRASES = {
    "warming_up": "no samples yet (needs 5 minutes)",
    "insufficient_samples": "not enough samples in the last 5 minutes",
    "insufficient_coverage": "the last 5 minutes were not fully sampled",
    "invalid_samples": "a counter reading in the last 5 minutes failed",
    "counter_reset": "the write counter went backwards; measuring again",
    "excluded_time_over_limit": (
        "transcript maintenance filled over 20% of the last 5 minutes"),
}


def statistic_reason_phrase(reason) -> str:
    """The phrase for a statistic reason; an unknown one is named."""
    phrase = STATISTIC_REASON_PHRASES.get(str(reason))
    if phrase is not None:
        return phrase
    return f"not measured ({str(reason or 'unavailable').replace('_', ' ')})"


def _exceeds(stat, limits) -> "tuple[bool, bool]":
    rate = bool(stat.rate_qualified and stat.bytes_per_minute is not None
                and stat.bytes_per_minute > limits.bytes_per_minute)
    per_pub = bool(stat.publication_qualified
                   and stat.mean_bytes_per_publication is not None
                   and stat.mean_bytes_per_publication
                   > limits.bytes_per_publication)
    return rate, per_pub


def _below_clear(stat, limits) -> bool:
    if not stat.rate_qualified or stat.bytes_per_minute is None:
        return False
    if stat.bytes_per_minute >= CLEAR_FRACTION * limits.bytes_per_minute:
        return False
    if stat.publication_qualified and stat.mean_bytes_per_publication is not None:
        return (stat.mean_bytes_per_publication
                < CLEAR_FRACTION * limits.bytes_per_publication)
    return True


@dataclasses.dataclass(frozen=True)
class HysteresisState:
    verdict: str = "ok"              # "ok" | "over"
    pending: "str | None" = None     # "over" | "ok" | None
    since_ns: "int | None" = None


def advance(state: HysteresisState, stat: SteadyStatistic, limits: BudgetLimits,
            now_ns: int) -> HysteresisState:
    """One evaluation. Only a qualified statistic moves the state; a
    transition needs two qualifying evaluations at least a minute apart, and
    any evaluation that disagrees cancels the pending candidate."""
    if stat.status != "qualified":
        return state
    spacing = HYSTERESIS_SPACING_SECONDS * _NS
    if state.verdict == "ok":
        if not any(_exceeds(stat, limits)):
            return HysteresisState("ok")
        if state.pending == "over":
            if now_ns - state.since_ns >= spacing:
                return HysteresisState("over")
            return state
        return HysteresisState("ok", "over", now_ns)
    if not _below_clear(stat, limits):
        return HysteresisState("over")
    if state.pending == "ok":
        if now_ns - state.since_ns >= spacing:
            return HysteresisState("ok")
        return state
    return HysteresisState("over", "ok", now_ns)


def steady_verdict(stat: SteadyStatistic, state: HysteresisState,
                   limits: BudgetLimits = LIMITS) -> "tuple[str, tuple[str, ...]]":
    """`(verdict, reasons)` for the surfaces; verdict in VERDICTS."""
    if stat.status == "unavailable":
        return "unavailable", tuple(stat.reasons)
    if stat.status == "insufficient":
        return "insufficient", tuple(stat.reasons)
    rate, per_pub = _exceeds(stat, limits)
    reasons = []
    if rate:
        reasons.append("rate_over_limit")
    if per_pub:
        reasons.append("publication_over_limit")
    if not stat.publication_qualified:
        reasons.append("insufficient_publications")
    if state.pending == "over":
        reasons.append("pending_over")
    elif state.pending == "ok":
        reasons.append("pending_clear")
    if not reasons:
        reasons.append("within_limits")
    return state.verdict, tuple(reasons)


VERDICTS = ("ok", "over", "insufficient", "unavailable")


# ── maintenance ledger and statistic ──────────────────────────────────────

@dataclasses.dataclass(frozen=True)
class LedgerBucket:
    hour: dt.datetime
    charged: int
    largest: int


def hour_floor(t: dt.datetime) -> dt.datetime:
    return t.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def iso_z(t: dt.datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_hour(value) -> "dt.datetime | None":
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return hour_floor(parsed)


def parse_ledger(raw) -> "tuple[LedgerBucket, ...]":
    """A legacy, absent or malformed ledger reads as no charges."""
    if not isinstance(raw, (list, tuple)):
        return ()
    buckets = {}
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        hour = _parse_hour(entry.get("hour"))
        charged = entry.get("charged")
        largest = entry.get("largest", charged)
        if (hour is None or isinstance(charged, bool)
                or not isinstance(charged, int) or charged < 0
                or isinstance(largest, bool) or not isinstance(largest, int)
                or largest < 0):
            continue
        prior = buckets.get(hour)
        if prior is not None:
            charged += prior.charged
            largest = max(largest, prior.largest)
        buckets[hour] = LedgerBucket(hour, charged, largest)
    return tuple(sorted(buckets.values(), key=lambda b: b.hour))


def serialize_ledger(buckets) -> "list[dict]":
    return [{"hour": iso_z(b.hour), "charged": int(b.charged),
             "largest": int(b.largest)} for b in buckets]


def ledger_add(raw, *, at_utc: dt.datetime, charge_bytes: int) -> "list[dict]":
    """Add one operation's charge to the bucket of its start, then drop every
    bucket more than 24 complete hours old (at most 25 remain)."""
    hour = hour_floor(at_utc)
    buckets = {b.hour: b for b in parse_ledger(raw)}
    prior = buckets.get(hour)
    charge = max(0, int(charge_bytes))
    buckets[hour] = LedgerBucket(
        hour,
        (prior.charged if prior else 0) + charge,
        max(prior.largest if prior else 0, charge),
    )
    floor = hour - dt.timedelta(hours=LEDGER_BUCKETS - 1)
    kept = sorted((b for b in buckets.values() if b.hour >= floor),
                  key=lambda b: b.hour)[-LEDGER_BUCKETS:]
    return serialize_ledger(kept)


@dataclasses.dataclass(frozen=True)
class MaintenanceStatistic:
    charged_bytes: int
    largest_charge_bytes: int
    window_start: dt.datetime
    window_end: dt.datetime
    window_minutes: int
    allowance_bytes: int
    verdict: str

    def as_wire(self) -> dict:
        return {
            "chargedBytes": self.charged_bytes,
            "largestChargeBytes": self.largest_charge_bytes,
            "windowStart": iso_z(self.window_start),
            "windowEnd": iso_z(self.window_end),
            "windowMinutes": self.window_minutes,
            "allowanceBytes": self.allowance_bytes,
            "verdict": self.verdict,
        }

    def as_details(self) -> dict:
        return {
            "charged_bytes": self.charged_bytes,
            "largest_charge_bytes": self.largest_charge_bytes,
            "window_start": iso_z(self.window_start),
            "window_end": iso_z(self.window_end),
            "window_minutes": self.window_minutes,
            "allowance_bytes": self.allowance_bytes,
            "verdict": self.verdict,
        }


def maintenance_statistic(raw_ledger, now_utc: dt.datetime) -> MaintenanceStatistic:
    """Charged bytes over the current partial UTC hour plus the 24 complete
    hours before it, against 4 MiB x (L + 1) + the largest charge in it."""
    now = now_utc.astimezone(UTC)
    current = hour_floor(now)
    start = current - dt.timedelta(hours=24)
    minutes = 1440 + int((now - current).total_seconds() // 60)
    inside = [b for b in parse_ledger(raw_ledger) if start <= b.hour <= current]
    charged = sum(b.charged for b in inside)
    largest = max((b.largest for b in inside), default=0)
    allowance = MAINTENANCE_RATE_BYTES_PER_MINUTE * (minutes + 1) + largest
    return MaintenanceStatistic(
        charged_bytes=charged, largest_charge_bytes=largest,
        window_start=start, window_end=now, window_minutes=minutes,
        allowance_bytes=allowance,
        verdict="over" if charged > allowance else "ok")


# ── operator wording (spec §5.7) ──────────────────────────────────────────

def _one_decimal(value: float, unit: str) -> str:
    if abs(value) >= 100 or float(value).is_integer():
        return f"{value:.0f} {unit}"
    return f"{value:.1f} {unit}"


def format_mib(n) -> str:
    """MiB with one decimal below 100 and whole numbers at or above it."""
    return _one_decimal(n / MiB, "MiB")


def format_size(n) -> str:
    """GiB from one GiB up, MiB below it (``format_mib``).

    GiB carries three significant figures with trailing zeros dropped, the
    precision spec §5.7 states (`allowance 5.98 GiB`, `charged 7.9 GiB`); one
    decimal rounded a 6,122 MiB allowance up to `6.0 GiB` (#901 Amendment 19
    PR-12)."""
    if abs(n) >= GiB:
        value = n / GiB
        if abs(value) >= 100:
            text = f"{value:.0f}"
        elif abs(value) >= 10:
            text = f"{value:.1f}"
        else:
            text = f"{value:.2f}"
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return f"{text} GiB"
    return format_mib(n)


def format_kib(n) -> str:
    return f"{n / KiB:.1f} KiB"


def format_window_hm(minutes: int) -> str:
    return f"{int(minutes) // 60} h {int(minutes) % 60} m"


def format_window_ms(seconds: float) -> str:
    total = int(round(seconds))
    return f"{total // 60}m {total % 60:02d}s"


def format_since(window_start: dt.datetime, window_end: dt.datetime) -> str:
    start = window_start.astimezone(UTC)
    end = window_end.astimezone(UTC)
    clock = start.strftime("%H:%M")
    if start.date() == end.date():
        return f"{clock} UTC today"
    if start.date() == end.date() - dt.timedelta(days=1):
        return f"{clock} UTC yesterday"
    return f"{clock} UTC {start.date().isoformat()}"
