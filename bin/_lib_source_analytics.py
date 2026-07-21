"""Pure source-aware accounting contracts for the #294 S3 adapter."""
from __future__ import annotations

import datetime as dt
import hashlib
import math
from collections import defaultdict
from dataclasses import dataclass, replace
from typing import Generic, Iterable, Literal, TypeVar


Availability = Literal["ok", "empty", "partial", "unavailable"]
_T = TypeVar("_T")


@dataclass(frozen=True)
class SourceWarning:
    """A privacy-safe, source-scoped degradation diagnostic."""

    code: str
    message: str


@dataclass(frozen=True)
class SourceResult(Generic[_T]):
    """One provider result without collapsing provider-native state."""

    source: str
    status: Availability
    data: _T
    warnings: tuple[SourceWarning, ...] = ()


QUALIFIED_METADATA_WARNING = SourceWarning(
    "qualified_metadata_unavailable",
    "Codex qualified project metadata is unavailable.",
)
QUOTA_STATE_WARNING = SourceWarning(
    "quota_state_unavailable",
    "Codex quota state is unavailable.",
)


@dataclass(frozen=True)
class QualifiedCodexEntry:
    """One accounting row joined to its S1-qualified project identity."""

    timestamp: dt.datetime
    source_root_key: str
    conversation_key: str
    project_key: str
    project_label: str
    model: str
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int
    total_tokens: int
    cost_usd: float
    # The raw label remains available for ambiguity diagnostics.  The emitted
    # label is assigned once over the complete qualified population before any
    # project/model filtering, so a rendered ``label (N)`` is selectable.
    display_label: str | None = None
    # Dashboard source read models reuse this same qualified accounting stream
    # for the shipped Codex period/session kernels.  These are internal-only;
    # source-analytics renderers never expose either raw identity.
    session_id: str = ""
    source_path: str = ""


def emitted_project_label(entry: QualifiedCodexEntry) -> str:
    """Return the deterministic privacy-safe label exposed to users."""
    return entry.display_label or entry.project_label


def assign_collision_safe_project_labels(
    entries: Iterable[QualifiedCodexEntry],
) -> tuple[QualifiedCodexEntry, ...]:
    """Annotate a complete qualified population with stable display labels.

    Labels are deterministic by opaque project key.  Keeping the annotation on
    the entry (rather than adding it while rendering rows) gives project
    selection the exact string that terminal, JSON, and share emit.
    """
    values = tuple(entries)
    assigned = collision_safe_project_label_map(
        (entry.project_key, entry.project_label) for entry in values
    )
    return tuple(
        replace(entry, display_label=assigned.get(entry.project_key, entry.project_label))
        for entry in values
    )


def collision_safe_project_label_map(
    identities_and_labels: Iterable[tuple[str, str]],
) -> dict[str, str]:
    """Allocate the shared deterministic privacy-safe label contract.

    Callers supply opaque internal identities. Only the returned labels are
    presentation data; the identity keys must remain inside the adapter.
    """
    keys_by_label: dict[str, set[str]] = defaultdict(set)
    for identity, label in identities_and_labels:
        keys_by_label[label].add(identity)
    # Never let an allocator-owned ``label (N)`` shadow a literal project
    # label.  Reserving every raw label also makes a later presentation pass
    # unnecessary, which avoids producing ``label (N) (N)``.
    reserved_labels = set(keys_by_label)
    used_labels: set[str] = set()
    assigned: dict[str, str] = {}
    for label in sorted(keys_by_label):
        keys = sorted(keys_by_label[label])
        if len(keys) == 1:
            assigned[keys[0]] = label
            used_labels.add(label)
            continue
        ordinal = 1
        for key in keys:
            candidate = f"{label} ({ordinal})"
            while candidate in reserved_labels or candidate in used_labels:
                ordinal += 1
                candidate = f"{label} ({ordinal})"
            assigned[key] = candidate
            used_labels.add(candidate)
            ordinal += 1
    return assigned


def opaque_project_key(source: str, root_key: str, resolved_key: str) -> str:
    """Return the stable opaque identity for one provider-qualified project."""
    if not all(isinstance(value, str) and value for value in (source, root_key, resolved_key)):
        raise ValueError("source, root_key, and resolved_key must be non-empty strings")
    payload = f"{source}\0{root_key}\0{resolved_key}".encode("utf-8")
    return "project:" + hashlib.sha256(payload).hexdigest()[:24]


UTC = dt.timezone.utc


def _require_aware(value: dt.datetime, name: str) -> dt.datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _opaque_key(kind: str, *parts: str) -> str:
    if not kind or any(not isinstance(part, str) or not part for part in parts):
        raise ValueError("opaque key parts must be non-empty strings")
    digest = hashlib.sha256("\0".join((kind, *parts)).encode("utf-8")).hexdigest()[:24]
    return f"{kind}:{digest}"


def opaque_quota_key(
    source: str, source_root_key: str, logical_limit_key: str,
    observed_slot: str, window_minutes: int,
) -> str:
    """Return an opaque full-native-identity key for a quota series."""
    if not isinstance(window_minutes, int) or isinstance(window_minutes, bool) or window_minutes <= 0:
        raise ValueError("window_minutes must be a positive integer")
    return _opaque_key(
        "quota", source, source_root_key, logical_limit_key, observed_slot,
        str(window_minutes),
    )


@dataclass(frozen=True)
class TokenTotals:
    """Explicit Codex-native accounting totals.

    Codex input is inclusive of cache and output is inclusive of reasoning.
    The separate fields make that vocabulary visible instead of inventing a
    Claude cache-hit analogue.
    """

    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0

    @property
    def non_cached_input_tokens(self) -> int:
        return max(0, self.input_tokens - self.cached_input_tokens)

    @property
    def cached_input_percent(self) -> float | None:
        if self.input_tokens <= 0:
            return None
        return self.cached_input_tokens * 100.0 / self.input_tokens


def _totals(entries: Iterable[QualifiedCodexEntry]) -> TokenTotals:
    values = assign_collision_safe_project_labels(entries)
    return TokenTotals(
        input_tokens=sum(entry.input_tokens for entry in values),
        cached_input_tokens=sum(entry.cached_input_tokens for entry in values),
        output_tokens=sum(entry.output_tokens for entry in values),
        reasoning_output_tokens=sum(entry.reasoning_output_tokens for entry in values),
        total_tokens=sum(entry.total_tokens for entry in values),
        cost_usd=math.fsum(entry.cost_usd for entry in values),
    )


def _codex_tokens_wire(totals: TokenTotals, *, cached_percent: bool = False) -> dict[str, object]:
    result: dict[str, object] = {
        "inputTokens": totals.input_tokens,
        "cachedInputTokens": totals.cached_input_tokens,
        "nonCachedInputTokens": totals.non_cached_input_tokens,
        "outputTokens": totals.output_tokens,
        "reasoningOutputTokens": totals.reasoning_output_tokens,
        "totalTokens": totals.total_tokens,
    }
    if cached_percent:
        result["cachedInputPercent"] = totals.cached_input_percent
    return result


def _metric_wire(totals: TokenTotals) -> dict[str, object]:
    return {
        "cost_usd": totals.cost_usd,
        "total_tokens": totals.total_tokens,
        "input_tokens": totals.input_tokens,
        "cached_input_tokens": totals.cached_input_tokens,
        "output_tokens": totals.output_tokens,
        "reasoning_output_tokens": totals.reasoning_output_tokens,
    }


@dataclass(frozen=True)
class AnalyticsWindow:
    """One resolved absolute analytics interval; its end is exclusive."""

    label: str
    kind: str
    start_at: dt.datetime
    end_at: dt.datetime
    used_pct_mode: str | None = None

    def __post_init__(self) -> None:
        start_at = _require_aware(self.start_at, "start_at")
        end_at = _require_aware(self.end_at, "end_at")
        if end_at <= start_at:
            raise ValueError("window end_at must be after start_at")
        object.__setattr__(self, "start_at", start_at)
        object.__setattr__(self, "end_at", end_at)


@dataclass(frozen=True)
class QuotaAttribution:
    quota_key: str | None
    slot: str | None
    window_minutes: int | None
    reset_at: dt.datetime | None
    used_percent: float | None
    attributed_used_percent: float | None
    cost_per_percent: float | None
    status: str = "ok"

    @classmethod
    def unavailable(cls) -> "QuotaAttribution":
        """Represent unavailable S2 attribution without inventing identity."""
        return cls(None, None, None, None, None, None, None, "unavailable")


@dataclass(frozen=True)
class CodexProjectRow:
    project_key: str
    display_label: str
    session_count: int
    first_seen: dt.datetime
    last_seen: dt.datetime
    totals: TokenTotals
    models: tuple[tuple[str, TokenTotals], ...]
    quota_attributions: tuple[QuotaAttribution, ...]


@dataclass(frozen=True)
class CodexProjectData:
    range_start: dt.datetime
    range_end: dt.datetime
    window_kind: str
    totals: TokenTotals
    projects: tuple[CodexProjectRow, ...]
    include_breakdown: bool = False


def _entry_in_half_open(entry: QualifiedCodexEntry, start: dt.datetime, end: dt.datetime) -> bool:
    timestamp = _require_aware(entry.timestamp, "entry timestamp")
    return start <= timestamp < end


def build_codex_project_result(
    entries: Iterable[QualifiedCodexEntry], *, range_start: dt.datetime,
    range_end: dt.datetime, blocks: Iterable[object] = (), as_of: dt.datetime | None = None,
    window_kind: str = "calendar", sort: str = "cost", order: str = "desc",
    include_breakdown: bool = False, allocation_entries: Iterable[QualifiedCodexEntry] | None = None,
    quota_available: bool = True,
) -> SourceResult[CodexProjectData]:
    """Group qualified Codex accounting without merging provider-root identity.

    Quota usage is allocated per physical root-qualified block by source-native
    cost only.  Each block remains a child attribution, rather than becoming a
    fabricated singular Codex subscription percentage.
    """
    start = _require_aware(range_start, "range_start")
    end = _require_aware(range_end, "range_end")
    if end <= start:
        raise ValueError("range_end must be after range_start")
    as_of_utc = _require_aware(as_of or end, "as_of")
    all_entries = tuple(entries)
    # Command adapters attach labels before filtering.  Preserve that complete
    # population metadata, while keeping the pure builder collision-safe for
    # direct callers that supply only raw labels.
    if all(entry.display_label is None for entry in all_entries):
        all_entries = assign_collision_safe_project_labels(all_entries)
    physical_population = (
        all_entries if allocation_entries is None else tuple(allocation_entries)
    )
    selected = tuple(entry for entry in all_entries if _entry_in_half_open(entry, start, end))
    grouped: dict[tuple[str, str], list[QualifiedCodexEntry]] = defaultdict(list)
    for entry in selected:
        grouped[(entry.source_root_key, entry.project_key)].append(entry)

    attributions: dict[tuple[str, str], list[QuotaAttribution]] = defaultdict(list)
    for block in blocks if quota_available else ():
        identity = block.identity
        if getattr(identity, "source", None) != "codex":
            continue
        nominal_start = _require_aware(block.nominal_start_at, "block nominal_start_at")
        reset_at = _require_aware(block.resets_at, "block resets_at")
        block_end = min(as_of_utc, reset_at)
        if block_end <= nominal_start:
            continue
        block_entries = tuple(
            entry for entry in physical_population
            if entry.source_root_key == identity.source_root_key
            and _entry_in_half_open(entry, nominal_start, block_end)
        )
        block_totals = _totals(block_entries)
        current_percent = float(block.current_percent)
        can_allocate = block_totals.cost_usd > 0 and current_percent > 0
        quota_key = opaque_quota_key(
            identity.source, identity.source_root_key, identity.logical_limit_key,
            identity.observed_slot, identity.window_minutes,
        )
        for (root_key, project_key), project_entries in grouped.items():
            if root_key != identity.source_root_key:
                continue
            project_cost = math.fsum(
                entry.cost_usd for entry in project_entries
                if _entry_in_half_open(entry, nominal_start, block_end)
            )
            if project_cost <= 0:
                continue
            attributions[(root_key, project_key)].append(QuotaAttribution(
                quota_key=quota_key,
                slot=identity.observed_slot,
                window_minutes=identity.window_minutes,
                reset_at=reset_at,
                used_percent=current_percent,
                attributed_used_percent=(
                    current_percent * project_cost / block_totals.cost_usd
                    if can_allocate else None
                ),
                cost_per_percent=(
                    block_totals.cost_usd / current_percent if can_allocate else None
                ),
            ))

    rows: list[CodexProjectRow] = []
    for (root_key, project_key), project_entries in grouped.items():
        ordered = tuple(sorted(project_entries, key=lambda entry: entry.timestamp))
        by_model: dict[str, list[QualifiedCodexEntry]] = defaultdict(list)
        for entry in ordered:
            by_model[entry.model].append(entry)
        rows.append(CodexProjectRow(
            project_key=project_key,
            display_label=emitted_project_label(ordered[0]),
            session_count=len({entry.conversation_key for entry in ordered}),
            first_seen=_require_aware(ordered[0].timestamp, "entry timestamp"),
            last_seen=_require_aware(ordered[-1].timestamp, "entry timestamp"),
            totals=_totals(ordered),
            models=tuple((model, _totals(by_model[model])) for model in sorted(by_model)),
            quota_attributions=(
                tuple(attributions[(root_key, project_key)])
                if quota_available else (QuotaAttribution.unavailable(),)
            ),
        ))
    if sort not in {"cost", "name", "last-seen"}:
        raise ValueError("Codex project sort must be cost, name, or last-seen")
    if order not in {"asc", "desc"}:
        raise ValueError("Codex project order must be asc or desc")
    if sort == "name":
        rows.sort(
            key=lambda row: (row.display_label.lower(), row.project_key),
            reverse=(order == "desc"),
        )
    elif sort == "last-seen":
        rows.sort(
            key=lambda row: (row.last_seen, row.display_label, row.project_key),
            reverse=(order == "desc"),
        )
    else:
        rows.sort(
            key=lambda row: (row.totals.cost_usd, row.display_label, row.project_key),
            reverse=(order == "desc"),
        )
    data = CodexProjectData(
        start, end, window_kind, _totals(selected), tuple(rows), include_breakdown,
    )
    status: Availability = "empty" if not selected else ("ok" if quota_available else "partial")
    warnings = () if quota_available else (QUOTA_STATE_WARNING,)
    return SourceResult("codex", status, data, warnings)


@dataclass(frozen=True)
class CodexRangeData:
    start: dt.datetime
    end: dt.datetime
    totals: TokenTotals
    models: tuple[tuple[str, TokenTotals], ...]
    include_breakdown: bool = False


def build_codex_range_result(
    entries: Iterable[QualifiedCodexEntry], start: dt.datetime, end: dt.datetime,
    *, include_breakdown: bool = False,
) -> SourceResult[CodexRangeData]:
    """Build the inclusive-end range-cost result for Codex accounting."""
    start_utc = _require_aware(start, "start")
    end_utc = _require_aware(end, "end")
    if end_utc < start_utc:
        raise ValueError("end must not be before start")
    selected = tuple(
        entry for entry in entries
        if start_utc <= _require_aware(entry.timestamp, "entry timestamp") <= end_utc
    )
    by_model: dict[str, list[QualifiedCodexEntry]] = defaultdict(list)
    for entry in selected:
        by_model[entry.model].append(entry)
    models = tuple((model, _totals(by_model[model])) for model in sorted(by_model))
    data = CodexRangeData(
        start_utc, end_utc, _totals(selected), models, include_breakdown,
    )
    return SourceResult("codex", "empty" if not selected else "ok", data)


@dataclass(frozen=True)
class ReuseRow:
    key: str
    label: str
    project_key: str | None
    project_label: str | None
    last_activity: dt.datetime
    totals: TokenTotals
    models: tuple[tuple[str, TokenTotals], ...]


@dataclass(frozen=True)
class CodexReuseData:
    start: dt.datetime | None
    end: dt.datetime | None
    group_by: str
    totals: TokenTotals
    rows: tuple[ReuseRow, ...]
    project_metadata_available: bool = True
    sort: str = "date"


def build_codex_reuse_result(
    entries: Iterable[QualifiedCodexEntry], *, group_by: Literal["date", "session", "model"] = "date",
    project_metadata_available: bool = True, sort: str | None = None,
    range_start: dt.datetime | None = None, range_end: dt.datetime | None = None,
) -> SourceResult[CodexReuseData]:
    """Build token-reuse rows with Codex-inclusive token semantics."""
    if (range_start is None) != (range_end is None):
        raise ValueError("Codex reuse range_start and range_end must be supplied together")
    if range_start is not None and range_end is not None:
        requested_start = _require_aware(range_start, "range_start")
        requested_end = _require_aware(range_end, "range_end")
        if requested_end <= requested_start:
            raise ValueError("Codex reuse range_end must be after range_start")
        values = tuple(
            entry for entry in entries
            if _entry_in_half_open(entry, requested_start, requested_end)
        )
    else:
        requested_start = requested_end = None
        values = tuple(entries)
    if group_by not in {"date", "session", "model"}:
        raise ValueError("group_by must be date, session, or model")
    grouped: dict[tuple[str, ...], list[QualifiedCodexEntry]] = defaultdict(list)
    for entry in values:
        timestamp = _require_aware(entry.timestamp, "entry timestamp")
        if group_by == "date":
            key = (timestamp.date().isoformat(),)
        elif group_by == "session":
            key = (entry.source_root_key, entry.conversation_key)
        else:
            key = (entry.model,)
        grouped[key].append(entry)

    rows: list[ReuseRow] = []
    for key, group in grouped.items():
        ordered = tuple(sorted(group, key=lambda entry: entry.timestamp))
        by_model: dict[str, list[QualifiedCodexEntry]] = defaultdict(list)
        for entry in ordered:
            by_model[entry.model].append(entry)
        if group_by == "date":
            row_key, label = _opaque_key("reuse-date", key[0]), key[0]
            project_key = project_label = None
        elif group_by == "session":
            row_key = _opaque_key("reuse-session", key[0], key[1])
            label = "Codex session"
            project_key = ordered[0].project_key if project_metadata_available else None
            project_label = emitted_project_label(ordered[0]) if project_metadata_available else None
        else:
            row_key, label = _opaque_key("reuse-model", key[0]), key[0]
            project_key = project_label = None
        rows.append(ReuseRow(
            key=row_key,
            label=label,
            project_key=project_key,
            project_label=project_label,
            last_activity=_require_aware(ordered[-1].timestamp, "entry timestamp"),
            totals=_totals(ordered),
            models=tuple((model, _totals(by_model[model])) for model in sorted(by_model)),
        ))
    resolved_sort = sort or ("recent" if group_by == "session" else "date")
    if resolved_sort not in {"date", "recent", "cost", "reuse"}:
        raise ValueError("Codex reuse sort must be date, recent, cost, or reuse")
    if resolved_sort == "recent":
        rows.sort(key=lambda row: (row.last_activity, row.label, row.key), reverse=True)
    elif resolved_sort == "cost":
        rows.sort(key=lambda row: (row.totals.cost_usd, row.label, row.key), reverse=True)
    elif resolved_sort == "reuse":
        rows.sort(
            key=lambda row: (
                row.totals.cached_input_percent if row.totals.cached_input_percent is not None else -1.0,
                row.label, row.key,
            ),
            reverse=True,
        )
    else:
        rows.sort(key=lambda row: (row.label, row.key))
    timestamps = sorted(_require_aware(entry.timestamp, "entry timestamp") for entry in values)
    data = CodexReuseData(
        requested_start if requested_start is not None else (timestamps[0] if timestamps else None),
        requested_end if requested_end is not None else (timestamps[-1] if timestamps else None),
        group_by,
        _totals(values),
        tuple(rows),
        project_metadata_available,
        resolved_sort,
    )
    status: Availability = "empty" if not values else "ok"
    warnings: tuple[SourceWarning, ...] = ()
    if not project_metadata_available:
        status = "partial"
        warnings = (QUALIFIED_METADATA_WARNING,)
    return SourceResult("codex", status, data, warnings)


@dataclass(frozen=True)
class DiffWindowTotals:
    window: AnalyticsWindow
    totals: TokenTotals


@dataclass(frozen=True)
class DiffRow:
    key: str
    label: str
    status: Literal["changed", "new", "dropped"]
    a: TokenTotals
    b: TokenTotals


@dataclass(frozen=True)
class DiffSection:
    key: str
    label: str
    status: Literal["ok", "empty", "unavailable"]
    rows: tuple[DiffRow, ...]


@dataclass(frozen=True)
class CodexDiffData:
    windows: tuple[DiffWindowTotals, DiffWindowTotals]
    combined_a: TokenTotals
    combined_b: TokenTotals
    sections: tuple[DiffSection, ...]
    only: str | None = None
    smart_filter: bool = False
    sort: str = "cost"
    limit: int | None = None
    normalization: Literal["none", "per-day"] | None = None
    min_delta_usd: float | None = None
    min_delta_pct: float | None = None


@dataclass(frozen=True)
class CodexReportRow:
    block_start: dt.datetime
    reset_at: dt.datetime
    used_percent: float | None
    cost_usd: float
    cost_per_percent: float | None
    status: Literal["ok", "unavailable"]
    detail: tuple["CodexReportDetailRow", ...] = ()


@dataclass(frozen=True)
class CodexReportDetailRow:
    """One native quota-percent crossing within a selected reset block."""

    percent_threshold: int
    captured_at: dt.datetime
    cumulative_cost_usd: float
    marginal_cost_usd: float


@dataclass(frozen=True)
class CodexReportSeries:
    quota_key: str
    slot: str
    window_minutes: int
    rows: tuple[CodexReportRow, ...]


@dataclass(frozen=True)
class CodexReportData:
    as_of: dt.datetime
    series: tuple[CodexReportSeries, ...]
    quota_status: Literal["ok", "empty", "unavailable"]
    quota_warnings: tuple[SourceWarning, ...] = ()


def build_codex_report_result(
    entries: Iterable[QualifiedCodexEntry], blocks: Iterable[object], *,
    as_of: dt.datetime, quota_available: bool = True, include_detail: bool = False,
) -> SourceResult[CodexReportData | None]:
    """Build per-logical-limit quota accounting without cross-limit arithmetic.

    A physical Codex accounting entry can truthfully participate in more than
    one logical-limit series.  This function therefore never sums series, has
    no combined field, and deliberately does not receive project metadata.
    """
    as_of_utc = _require_aware(as_of, "as_of")
    if not quota_available:
        data = CodexReportData(
            as_of_utc, (), "unavailable", (QUOTA_STATE_WARNING,),
        )
        return SourceResult("codex", "partial", data, (QUOTA_STATE_WARNING,))
    values = tuple(entries)
    grouped: dict[tuple[str, str, str, str, int], list[CodexReportRow]] = defaultdict(list)
    for block in blocks:
        identity = block.identity
        if getattr(identity, "source", None) != "codex":
            continue
        nominal_start = _require_aware(block.nominal_start_at, "block nominal_start_at")
        reset_at = _require_aware(block.resets_at, "block resets_at")
        block_end = min(as_of_utc, reset_at)
        if block_end <= nominal_start:
            continue
        block_entries = tuple(
            entry for entry in values
            if entry.source_root_key == identity.source_root_key
            and _entry_in_half_open(entry, nominal_start, block_end)
        )
        totals = _totals(block_entries)
        used_percent = float(block.current_percent)
        key = (
            identity.source, identity.source_root_key, identity.logical_limit_key,
            identity.observed_slot, identity.window_minutes,
        )
        detail: tuple[CodexReportDetailRow, ...] = ()
        if include_detail:
            observations = tuple(sorted(
                getattr(block, "observations", ()),
                key=lambda observation: observation.captured_at,
            ))
            if observations:
                prior_percent = math.floor(float(observations[0].used_percent) + 1e-9)
                prior_boundary = observations[0].captured_at
                cumulative_cost = 0.0
                detail_rows: list[CodexReportDetailRow] = []
                for observation in observations[1:]:
                    current_percent = math.floor(float(observation.used_percent) + 1e-9)
                    if current_percent <= prior_percent:
                        continue
                    marginal_cost = math.fsum(
                        entry.cost_usd for entry in block_entries
                        if prior_boundary < entry.timestamp <= observation.captured_at
                    )
                    cumulative_cost += marginal_cost
                    for threshold in range(prior_percent + 1, current_percent + 1):
                        detail_rows.append(CodexReportDetailRow(
                            percent_threshold=threshold,
                            captured_at=observation.captured_at,
                            cumulative_cost_usd=cumulative_cost,
                            marginal_cost_usd=(
                                marginal_cost if threshold == prior_percent + 1 else 0.0
                            ),
                        ))
                    prior_percent = current_percent
                    prior_boundary = observation.captured_at
                detail = tuple(detail_rows)
        grouped[key].append(CodexReportRow(
            block_start=nominal_start,
            reset_at=reset_at,
            used_percent=used_percent,
            cost_usd=totals.cost_usd,
            cost_per_percent=(totals.cost_usd / used_percent if used_percent > 0 else None),
            status="ok",
            detail=detail,
        ))
    series: list[CodexReportSeries] = []
    for (source, root_key, limit_key, slot, window_minutes), rows in sorted(grouped.items()):
        series.append(CodexReportSeries(
            quota_key=opaque_quota_key(source, root_key, limit_key, slot, window_minutes),
            slot=slot,
            window_minutes=window_minutes,
            rows=tuple(sorted(rows, key=lambda row: (row.reset_at, row.block_start))),
        ))
    status: Literal["ok", "empty", "unavailable"] = "empty" if not series else "ok"
    data = CodexReportData(as_of_utc, tuple(series), status)
    return SourceResult("codex", status, data)


def _diff_rows(
    a_entries: Iterable[QualifiedCodexEntry], b_entries: Iterable[QualifiedCodexEntry],
    *, key_fn, label_fn, classify_presence: bool,
) -> tuple[DiffRow, ...]:
    a_groups: dict[str, list[QualifiedCodexEntry]] = defaultdict(list)
    b_groups: dict[str, list[QualifiedCodexEntry]] = defaultdict(list)
    labels: dict[str, str] = {}
    for entry in a_entries:
        key = key_fn(entry)
        a_groups[key].append(entry)
        labels.setdefault(key, label_fn(entry))
    for entry in b_entries:
        key = key_fn(entry)
        b_groups[key].append(entry)
        labels.setdefault(key, label_fn(entry))
    rows: list[DiffRow] = []
    for key in sorted(set(a_groups) | set(b_groups)):
        has_a, has_b = key in a_groups, key in b_groups
        a = _totals(a_groups[key])
        b = _totals(b_groups[key])
        status: Literal["changed", "new", "dropped"] = "changed"
        if classify_presence:
            if not has_a:
                status = "new"
            elif not has_b:
                status = "dropped"
        rows.append(DiffRow(key, labels[key], status, a, b))
    return tuple(rows)


def _diff_window_length_days(window: AnalyticsWindow) -> float:
    seconds = (window.end_at - window.start_at).total_seconds()
    if seconds <= 0:
        raise ValueError("Codex diff windows must have a positive length")
    return seconds / 86_400.0


def resolve_codex_diff_normalization(
    window_a: AnalyticsWindow, window_b: AnalyticsWindow, *, allow_mismatch: bool,
) -> Literal["none", "per-day"]:
    """Validate two source windows before provider reads and select their units."""
    a_days = _diff_window_length_days(window_a)
    b_days = _diff_window_length_days(window_b)
    mismatched = abs(a_days - b_days) > 0.01
    if not mismatched:
        return "none"
    same_eligible_kind = (
        window_a.kind == window_b.kind and window_a.kind in {"week", "month"}
    )
    if not same_eligible_kind and not allow_mismatch:
        raise ValueError(
            f"window A is {a_days:.1f} days, window B is {b_days:.1f} days; "
            "pass --allow-mismatch to compare anyway with per-day normalization"
        )
    return "per-day"


def _normalized_diff_totals(totals: TokenTotals, *, days: float, per_day: bool) -> TokenTotals:
    if not per_day:
        return totals
    return TokenTotals(
        input_tokens=totals.input_tokens,
        cached_input_tokens=totals.cached_input_tokens,
        output_tokens=totals.output_tokens,
        reasoning_output_tokens=totals.reasoning_output_tokens,
        total_tokens=totals.total_tokens,
        cost_usd=totals.cost_usd / days,
    )


def _normalized_diff_rows(
    rows: Iterable[DiffRow], *, a_days: float, b_days: float, per_day: bool,
) -> tuple[DiffRow, ...]:
    return tuple(
        DiffRow(
            row.key, row.label, row.status,
            _normalized_diff_totals(row.a, days=a_days, per_day=per_day),
            _normalized_diff_totals(row.b, days=b_days, per_day=per_day),
        )
        for row in rows
    )


def _diff_row_cost_pct(row: DiffRow) -> float:
    delta = row.b.cost_usd - row.a.cost_usd
    if row.a.cost_usd == 0:
        return 0.0 if delta == 0 else math.inf
    return delta / row.a.cost_usd * 100.0


def _sort_diff_rows(rows: Iterable[DiffRow], sort: str) -> tuple[DiffRow, ...]:
    if sort == "delta":
        key = lambda row: (-abs(row.b.cost_usd - row.a.cost_usd), row.label, row.key)
    elif sort in {"cost", "cost-a"}:
        key = lambda row: (-row.a.cost_usd, row.label, row.key)
    elif sort == "cost-b":
        key = lambda row: (-row.b.cost_usd, row.label, row.key)
    elif sort == "name":
        key = lambda row: (row.label, row.key)
    elif sort == "status":
        order = {"dropped": 0, "changed": 1, "new": 2}
        key = lambda row: (
            order[row.status], -abs(row.b.cost_usd - row.a.cost_usd), row.label, row.key,
        )
    else:
        raise ValueError("Codex diff sort must be delta, cost-a, cost-b, name, or status")
    return tuple(sorted(rows, key=key))


def _visible_diff_rows(
    rows: Iterable[DiffRow], *, smart_filter: bool, min_delta_usd: float,
    min_delta_pct: float, sort: str, top: int | None,
) -> tuple[DiffRow, ...]:
    """Mirror the legacy diff filter/sort/top semantics for one provider leg."""
    visible: list[DiffRow] = []
    for row in rows:
        if (
            smart_filter
            and row.status == "changed"
            and abs(row.b.cost_usd - row.a.cost_usd) < min_delta_usd
            and abs(_diff_row_cost_pct(row)) < min_delta_pct
        ):
            continue
        visible.append(row)
    visible = list(_sort_diff_rows(visible, sort))
    if top is not None and top >= 0:
        changed = [row for row in visible if row.status == "changed"]
        terminal = [row for row in visible if row.status != "changed"]
        visible = list(_sort_diff_rows((*terminal, *changed[:top]), sort))
    return tuple(visible)


def build_codex_diff_result(
    entries: Iterable[QualifiedCodexEntry], window_a: AnalyticsWindow,
    window_b: AnalyticsWindow, *, project_metadata_available: bool = True,
    only: str | None = None, allow_mismatch: bool = False, show_all: bool = True,
    min_delta_usd: float = 0.10, min_delta_pct: float = 1.0,
    sort: str = "cost", top: int | None = None,
    normalization: Literal["none", "per-day"] | None = None,
    classify_presence: bool = False,
) -> SourceResult[CodexDiffData]:
    """Compare two half-open windows without a Claude cache section."""
    # The command validates exposed windows before provider I/O.  Keep this
    # pure builder permissive for direct in-process callers that use it to
    # inspect arbitrary half-open windows; explicit normalisation is the
    # command-to-kernel contract.
    if normalization is None:
        normalization = (
            resolve_codex_diff_normalization(
                window_a, window_b, allow_mismatch=True,
            ) if allow_mismatch else "none"
        )
    per_day = normalization == "per-day"
    a_days, b_days = _diff_window_length_days(window_a), _diff_window_length_days(window_b)
    values = tuple(entries)
    a_entries = tuple(entry for entry in values if _entry_in_half_open(entry, window_a.start_at, window_a.end_at))
    b_entries = tuple(entry for entry in values if _entry_in_half_open(entry, window_b.start_at, window_b.end_at))
    overall_rows = _diff_rows(
        a_entries, b_entries, key_fn=lambda _entry: "overall", label_fn=lambda _entry: "Overall",
        classify_presence=classify_presence,
    )
    model_rows = _diff_rows(
        a_entries, b_entries, key_fn=lambda entry: entry.model, label_fn=lambda entry: entry.model,
        classify_presence=classify_presence,
    )
    project_rows = _diff_rows(
        a_entries, b_entries, key_fn=lambda entry: entry.project_key,
        label_fn=emitted_project_label,
        classify_presence=classify_presence,
    )
    reuse_rows = _diff_rows(
        a_entries, b_entries, key_fn=lambda entry: entry.model, label_fn=lambda entry: entry.model,
        classify_presence=classify_presence,
    )
    overall_rows = _normalized_diff_rows(
        overall_rows, a_days=a_days, b_days=b_days, per_day=per_day,
    )
    model_rows = _visible_diff_rows(
        _normalized_diff_rows(model_rows, a_days=a_days, b_days=b_days, per_day=per_day),
        smart_filter=not show_all, min_delta_usd=min_delta_usd,
        min_delta_pct=min_delta_pct, sort=sort, top=top,
    )
    project_rows = _visible_diff_rows(
        _normalized_diff_rows(project_rows, a_days=a_days, b_days=b_days, per_day=per_day),
        smart_filter=not show_all, min_delta_usd=min_delta_usd,
        min_delta_pct=min_delta_pct, sort=sort, top=top,
    )
    reuse_rows = _visible_diff_rows(
        _normalized_diff_rows(reuse_rows, a_days=a_days, b_days=b_days, per_day=per_day),
        smart_filter=not show_all, min_delta_usd=min_delta_usd,
        min_delta_pct=min_delta_pct, sort=sort, top=top,
    )
    sections = (
        DiffSection("overall", "Overall", "empty" if not overall_rows else "ok", overall_rows),
        DiffSection("models", "Models", "empty" if not model_rows else "ok", model_rows),
        DiffSection(
            "projects", "Projects",
            "unavailable" if not project_metadata_available else ("empty" if not project_rows else "ok"),
            () if not project_metadata_available else project_rows,
        ),
        DiffSection("token-reuse", "Token reuse", "empty" if not reuse_rows else "ok", reuse_rows),
    )
    selected = None if only is None else {
        item.strip() for item in only.split(",") if item.strip()
    }
    if selected is not None:
        supported = {section.key for section in sections}
        unknown = selected - supported
        if unknown:
            raise ValueError(
                "Codex diff --only contains unknown section(s): "
                + ", ".join(sorted(unknown))
            )
        sections = tuple(section for section in sections if section.key in selected)
    data = CodexDiffData(
        (
            DiffWindowTotals(
                window_a, _normalized_diff_totals(_totals(a_entries), days=a_days, per_day=per_day),
            ),
            DiffWindowTotals(
                window_b, _normalized_diff_totals(_totals(b_entries), days=b_days, per_day=per_day),
            ),
        ),
        _normalized_diff_totals(_totals(a_entries), days=a_days, per_day=per_day),
        _normalized_diff_totals(_totals(b_entries), days=b_days, per_day=per_day), sections,
        only=only,
        smart_filter=not show_all,
        sort=sort,
        limit=top,
        normalization=(normalization if normalization != "none" else None),
        min_delta_usd=(min_delta_usd if classify_presence else None),
        min_delta_pct=(min_delta_pct if classify_presence else None),
    )
    status: Availability = "empty" if not a_entries and not b_entries else "ok"
    warnings: tuple[SourceWarning, ...] = ()
    if any(section.status == "unavailable" for section in sections):
        status = "partial"
        warnings = (QUALIFIED_METADATA_WARNING,)
    return SourceResult("codex", status, data, warnings)


def _iso_z(value: dt.datetime) -> str:
    return _require_aware(value, "timestamp").isoformat().replace("+00:00", "Z")


def _project_wire(row: CodexProjectRow, *, include_breakdown: bool) -> dict[str, object]:
    result: dict[str, object] = {
        "projectKey": row.project_key,
        "displayLabel": row.display_label,
        "sessionCount": row.session_count,
        "firstSeen": _iso_z(row.first_seen),
        "lastSeen": _iso_z(row.last_seen),
        "tokens": _codex_tokens_wire(row.totals),
        "costUsd": row.totals.cost_usd,
        "quotaAttributions": [
            {
                "quotaKey": attribution.quota_key,
                "slot": attribution.slot,
                "windowMinutes": attribution.window_minutes,
                "resetAt": _iso_z(attribution.reset_at) if attribution.reset_at else None,
                "usedPercent": attribution.used_percent,
                "attributedUsedPercent": attribution.attributed_used_percent,
                "costPerPercent": attribution.cost_per_percent,
                "status": attribution.status,
            }
            for attribution in row.quota_attributions
        ],
    }
    if include_breakdown:
        result["models"] = [
            {"model": model, "costUsd": totals.cost_usd,
             **_codex_tokens_wire(totals)}
            for model, totals in row.models
        ]
    return result


def _range_wire(data: CodexRangeData) -> dict[str, object]:
    result: dict[str, object] = {
        "start": _iso_z(data.start),
        "end": _iso_z(data.end),
        "totals": {"costUsd": data.totals.cost_usd, **_codex_tokens_wire(data.totals)},
    }
    result["models"] = (
        [
            {"model": model, "costUsd": totals.cost_usd, **_codex_tokens_wire(totals)}
            for model, totals in data.models
        ]
        if data.include_breakdown else []
    )
    return result


def _reuse_row_wire(row: ReuseRow) -> dict[str, object]:
    return {
        "key": row.key,
        "label": row.label,
        "projectKey": row.project_key,
        "projectLabel": row.project_label,
        "lastActivity": _iso_z(row.last_activity),
        "costUsd": row.totals.cost_usd,
        **_codex_tokens_wire(row.totals, cached_percent=True),
        "models": [
            {"model": model, "costUsd": totals.cost_usd,
             **_codex_tokens_wire(totals, cached_percent=True)}
            for model, totals in row.models
        ],
    }


def _reuse_wire(data: CodexReuseData) -> dict[str, object]:
    return {
        "start": _iso_z(data.start) if data.start else None,
        "end": _iso_z(data.end) if data.end else None,
        "groupBy": data.group_by,
        "totals": {
            "costUsd": data.totals.cost_usd,
            **_codex_tokens_wire(data.totals, cached_percent=True),
        },
        "sections": [
            {
                "key": "token-reuse",
                "status": "empty" if not data.rows else "ok",
                "data": {"rows": [_reuse_row_wire(row) for row in data.rows]},
                "warnings": [],
            },
            unavailable_section("project-metadata", QUALIFIED_METADATA_WARNING)
            if not data.project_metadata_available else {
                "key": "project-metadata",
                "status": "empty" if not data.rows else "ok",
                "data": {"projects": []},
                "warnings": [],
            },
        ],
    }


def _diff_delta(a: TokenTotals, b: TokenTotals) -> dict[str, object]:
    return {
        "cost_usd": b.cost_usd - a.cost_usd,
        "total_tokens": b.total_tokens - a.total_tokens,
        "input_tokens": b.input_tokens - a.input_tokens,
        "cached_input_tokens": b.cached_input_tokens - a.cached_input_tokens,
        "output_tokens": b.output_tokens - a.output_tokens,
        "reasoning_output_tokens": b.reasoning_output_tokens - a.reasoning_output_tokens,
    }


def _diff_wire(data: CodexDiffData) -> dict[str, object]:
    window_a, window_b = data.windows
    options: dict[str, object] = {
        "only": data.only,
        "smart_filter": data.smart_filter,
        "sort": data.sort,
        "limit": data.limit,
    }
    if data.normalization is not None:
        options["normalization"] = data.normalization
    if data.min_delta_usd is not None:
        options["min_delta_usd"] = data.min_delta_usd
    if data.min_delta_pct is not None:
        options["min_delta_pct"] = data.min_delta_pct
    return {
        "windows": {
            "a": {
                "label": window_a.window.label,
                "kind": window_a.window.kind,
                "start_at": _iso_z(window_a.window.start_at),
                "end_at": _iso_z(window_a.window.end_at),
                "used_pct_mode": window_a.window.used_pct_mode,
            },
            "b": {
                "label": window_b.window.label,
                "kind": window_b.window.kind,
                "start_at": _iso_z(window_b.window.start_at),
                "end_at": _iso_z(window_b.window.end_at),
                "used_pct_mode": window_b.window.used_pct_mode,
            },
        },
        "combined": {
            "cost_usd": {
                "a": data.combined_a.cost_usd,
                "b": data.combined_b.cost_usd,
                "delta": data.combined_b.cost_usd - data.combined_a.cost_usd,
            },
            "total_tokens": {
                "a": data.combined_a.total_tokens,
                "b": data.combined_b.total_tokens,
                "delta": data.combined_b.total_tokens - data.combined_a.total_tokens,
            },
        },
        "sections": [
            unavailable_section(section.key, QUALIFIED_METADATA_WARNING)
            if section.status == "unavailable" else {
                "key": section.key,
                "label": section.label,
                "status": section.status,
                "data": {"rows": [
                    {
                        "key": row.key,
                        "label": row.label,
                        "status": row.status,
                        "a": _metric_wire(row.a),
                        "b": _metric_wire(row.b),
                        "delta": _diff_delta(row.a, row.b),
                    }
                    for row in section.rows
                ]},
                "warnings": [],
            }
            for section in data.sections
        ],
        "options": options,
    }


def unavailable_section(key: str, warning: SourceWarning) -> dict[str, object]:
    """Return the frozen unavailable sibling-section shape."""
    return {
        "key": key,
        "status": "unavailable",
        "data": None,
        "warnings": [{"code": warning.code, "message": warning.message}],
    }


def _report_wire(data: CodexReportData) -> dict[str, object]:
    section_data: dict[str, object] | None = {
        "series": [
            {
                "quotaKey": series.quota_key,
                "sourceLabel": "Codex",
                "slot": series.slot,
                "windowMinutes": series.window_minutes,
                "rows": [
                    {
                        "blockStart": _iso_z(row.block_start),
                        "resetAt": _iso_z(row.reset_at),
                        "usedPercent": row.used_percent,
                        "costUsd": row.cost_usd,
                        "costPerPercent": row.cost_per_percent,
                        "status": row.status,
                        "detail": [
                            {
                                "percentThreshold": detail.percent_threshold,
                                "cumulativeCostUSD": detail.cumulative_cost_usd,
                                "marginalCostUSD": detail.marginal_cost_usd,
                                "capturedAt": _iso_z(detail.captured_at),
                            }
                            for detail in row.detail
                        ],
                    }
                    for row in series.rows
                ],
            }
            for series in data.series
        ],
    } if data.quota_status != "unavailable" else None
    return {
        "asOf": _iso_z(data.as_of),
        "sections": [{
            "key": "quota-series",
            "status": data.quota_status,
            "data": section_data,
            "warnings": [
                {"code": warning.code, "message": warning.message}
                for warning in data.quota_warnings
            ],
        }],
    }


def _physical_totals(value: object) -> TokenTotals:
    if isinstance(value, SourceResult):
        if value.status == "unavailable" or value.data is None:
            return TokenTotals()
        value = value.data
    if isinstance(value, TokenTotals):
        return value
    if isinstance(value, (CodexProjectData, CodexRangeData, CodexReuseData)):
        return value.totals
    if isinstance(value, CodexDiffData):
        raise ValueError("diff has two windows and is not one physical accounting total")
    if isinstance(value, CodexReportData):
        raise ValueError("report quota series overlap and cannot be combined")
    if isinstance(value, dict):
        try:
            return TokenTotals(
                total_tokens=int(value.get("totalTokens", value.get("total_tokens", 0))),
                cost_usd=float(value.get("costUsd", value.get("cost_usd", 0.0))),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid physical accounting totals") from exc
    raise TypeError("unsupported physical accounting value")


def combine_physical_accounting(claude: object, codex: object) -> dict[str, object]:
    """Combine only one already-deduplicated physical total per provider."""
    claude_totals = _physical_totals(claude)
    codex_totals = _physical_totals(codex)
    return {
        "costUsd": claude_totals.cost_usd + codex_totals.cost_usd,
        "totalTokens": claude_totals.total_tokens + codex_totals.total_tokens,
    }


def _diff_combined_values(result: SourceResult[object]) -> tuple[TokenTotals, TokenTotals] | None:
    """Return one provider's truthful A/B totals without flattening its wire."""
    if result.status == "unavailable" or result.data is None:
        return None
    if isinstance(result.data, CodexDiffData):
        return result.data.combined_a, result.data.combined_b
    if not isinstance(result.data, dict):
        raise TypeError("diff source data must retain a combined object")
    combined = result.data.get("combined")
    if not isinstance(combined, dict):
        raise ValueError("diff source data is missing combined accounting")

    def totals_for(metric_name: str) -> tuple[float, float]:
        metric = combined.get(metric_name)
        if not isinstance(metric, dict):
            raise ValueError(f"diff source data is missing {metric_name}")
        try:
            return float(metric["a"]), float(metric["b"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"diff source data has invalid {metric_name}") from exc

    cost_a, cost_b = totals_for("cost_usd")
    tokens_a, tokens_b = totals_for("total_tokens")
    return (
        TokenTotals(cost_usd=cost_a, total_tokens=int(tokens_a)),
        TokenTotals(cost_usd=cost_b, total_tokens=int(tokens_b)),
    )


def combine_diff_accounting(
    claude: SourceResult[object], codex: SourceResult[object],
) -> dict[str, object]:
    """Combine truthful per-window diff accounting without scalar coercion.

    An unavailable source yields no inferred values; only the provider blocks
    with complete, truthful A/B accounting contribute to the all-source pair.
    """
    totals = [
        values for values in (_diff_combined_values(claude), _diff_combined_values(codex))
        if values is not None
    ]
    a_cost = math.fsum(values[0].cost_usd for values in totals)
    b_cost = math.fsum(values[1].cost_usd for values in totals)
    a_tokens = sum(values[0].total_tokens for values in totals)
    b_tokens = sum(values[1].total_tokens for values in totals)
    return {
        "cost_usd": {"a": a_cost, "b": b_cost, "delta": b_cost - a_cost},
        "total_tokens": {"a": a_tokens, "b": b_tokens, "delta": b_tokens - a_tokens},
    }


def source_result_wire(result: SourceResult[object], *, diff: bool = False) -> dict[str, object]:
    """Render the exact direct-provider envelope for implemented pure kernels."""
    if result.data is None:
        data: object = None
    elif isinstance(result.data, CodexProjectData):
        data: object = {
            "rangeStart": _iso_z(result.data.range_start),
            "rangeEnd": _iso_z(result.data.range_end),
            "windowKind": result.data.window_kind,
            "totals": {"costUsd": result.data.totals.cost_usd, "totalTokens": result.data.totals.total_tokens},
            "projects": [
                _project_wire(row, include_breakdown=result.data.include_breakdown)
                for row in result.data.projects
            ],
        }
    elif isinstance(result.data, CodexRangeData):
        data = _range_wire(result.data)
    elif isinstance(result.data, CodexReuseData):
        data = _reuse_wire(result.data)
    elif isinstance(result.data, CodexDiffData):
        data = _diff_wire(result.data)
        diff = True
    elif isinstance(result.data, CodexReportData):
        data = _report_wire(result.data)
    else:
        raise TypeError("unsupported source result data")
    warnings = [{"code": warning.code, "message": warning.message} for warning in result.warnings]
    if diff:
        return {
            "schema_version": 1,
            "source": result.source,
            "status": result.status,
            "data": data,
            "warnings": warnings,
        }
    return {
        "schemaVersion": 1,
        "source": result.source,
        "status": result.status,
        "data": data,
        "warnings": warnings,
    }


def _source_block_wire(result: SourceResult[object], *, diff: bool) -> dict[str, object]:
    """Return one source block without changing its provider-native data."""
    if result.source == "codex":
        direct = source_result_wire(result, diff=diff)
        return {
            "source": direct["source"],
            "status": direct["status"],
            "data": direct["data"],
            "warnings": direct["warnings"],
        }
    if result.source != "claude":
        raise ValueError("all-source results require claude and codex blocks")
    warnings = [{"code": warning.code, "message": warning.message} for warning in result.warnings]
    return {
        "source": "claude",
        "status": result.status,
        "data": result.data,
        "warnings": warnings,
    }


def all_source_result_wire(
    claude: SourceResult[object], codex: SourceResult[object], *,
    diff: bool = False, report: bool = False,
) -> dict[str, object]:
    """Compose separate provider blocks without flattening unlike semantics.

    Only the dedicated physical accounting combiner may supply the all-source
    aggregate.  Quota reports explicitly omit it because their logical-limit
    series can overlap the same physical accounting rows.
    """
    if claude.source != "claude" or codex.source != "codex":
        raise ValueError("all-source results require claude then codex")
    sources = [
        _source_block_wire(claude, diff=diff),
        _source_block_wire(codex, diff=diff),
    ]
    if diff:
        result: dict[str, object] = {
            "schema_version": 1,
            "source": "all",
        }
        if not report:
            result["combined"] = combine_diff_accounting(claude, codex)
        result["sources"] = sources
        result["warnings"] = []
        return result
    result = {
        "schemaVersion": 1,
        "source": "all",
    }
    if not report:
        result["combined"] = combine_physical_accounting(claude, codex)
    result["sources"] = sources
    result["warnings"] = []
    return result
