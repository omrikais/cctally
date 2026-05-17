"""Diff kernel for the ``cctally diff`` subcommand.

Pure-fn layer (no I/O at import time): holds every window parser,
aggregator, builder, cell formatter, table/JSON renderer, and anchor
resolver used by ``cmd_diff`` (which itself stays in ``bin/cctally``
as the CLI ingress). One contiguous source region collapses into this
sibling (was bin/cctally L8379-L9825, ~1,447 LOC).

Symbol inventory:

* Dataclasses / exceptions: ``ParsedWindow``, ``WindowMismatchError``,
  ``NoAnchorError``, ``MetricBundle``, ``DeltaBundle``, ``ColumnSpec``,
  ``DiffRow``, ``DiffSection``, ``NoiseThreshold``, ``DiffResult``.
* Window parsing: ``_parse_diff_window`` plus the five
  ``_DIFF_*_RE`` regex constants.
* Numeric helper: ``_humanize_tokens``.
* Aggregators: ``_diff_iter_claude_entries``,
  ``_diff_aggregate_overall``, ``_diff_aggregate_models``,
  ``_diff_aggregate_projects``, ``_diff_aggregate_cache``,
  ``_diff_resolve_used_pct``.
* Default column tables: ``_DIFF_DEFAULT_COLUMNS_{OVERALL,MODELS,PROJECTS,CACHE}``.
* Section builders: ``_diff_sort_rows``, ``_apply_noise_threshold``,
  ``_diff_build_section``, ``_normalize_metric_bundle_per_day``,
  ``_sum_metric_bundles``, ``_build_diff_result``,
  ``_check_diff_invariants``.
* Cell formatters: ``_DIFF_EM_DASH``, ``_diff_or_emdash``,
  ``_diff_fmt_cost_cell``, ``_diff_fmt_delta_cost_cell``,
  ``_diff_fmt_pct_cell``, ``_diff_fmt_pp_cell``,
  ``_diff_fmt_tokens_cell``, ``_diff_fmt_delta_tokens_cell``,
  ``_diff_color_for_delta``.
* Renderers: ``_diff_render_banner``, ``_diff_render_window_header``,
  ``_diff_box_chars``, ``_diff_section_heading``,
  ``_diff_render_section_table``, ``_diff_render_full_output``.
* JSON shapers: ``_diff_metric_to_json``, ``_diff_delta_to_json``,
  ``_diff_window_to_json``, ``_diff_to_json_payload``,
  ``_diff_render_json``.
* Anchor resolution: ``_diff_resolve_anchor``.

Sibling dependencies (loaded at module-load time via ``_load_lib``):

* ``_lib_pricing`` — ``_calculate_entry_cost`` (cost computation used
  by every aggregator).
* ``_lib_display_tz`` — ``_resolve_tz`` (IANA tz resolution for
  month/range parsers) and ``format_display_dt`` (date labels in the
  diff window header).

``bin/cctally`` back-references via module-level callable shims
(spec §5.5; same precedent as ``bin/_lib_render.py``'s 7 shims and
``bin/_cctally_record.py``'s 34 shims):

* ``get_claude_session_entries`` — JSONL/cache reader shared with
  every JSONL-walking subcommand.
* ``_resolve_project_key`` — git-root resolver used by the projects
  aggregator.
* ``open_db`` — sqlite3 connection helper for the stats DB.
* ``_iso_z`` — ISO-8601 ``Z`` suffix formatter.
* ``_supports_unicode_stdout`` / ``_style_ansi`` — terminal-capability
  primitives used by the banner / window-header / section-table /
  box-char renderers.
* ``_command_as_of`` — ``CCTALLY_AS_OF`` env hook for deterministic
  ``generated_at`` in JSON output (fixture testing).
* ``_canonicalize_optional_iso`` — ISO-canonicalizer used to look up
  ``week_reset_events`` rows in the anchor resolver.
* ``parse_iso_datetime`` — strict ISO-8601 parser used to decode
  ``effective_reset_at_utc`` from the same.

Each shim resolves ``sys.modules['cctally'].X`` at CALL TIME (not
bind time), so monkeypatches on cctally's namespace propagate into
the moved code unchanged.

``bin/cctally`` eager-re-exports every public symbol below so the
~7 internal ``cmd_diff`` call sites + the extensive SourceFileLoader
test surface (``tests/test_diff_*.py``: ``ns["ParsedWindow"]``,
``ns["MetricBundle"]``, ``ns["_build_diff_result"]``, etc.) resolve
unchanged. Eager pattern is mandatory per spec §4.8 carve-out — PEP
562 ``__getattr__`` does NOT fire for ``mod.__dict__["X"]`` dict
access, which is how every ``test_diff_*.py`` reaches in.

Spec: docs/superpowers/specs/2026-05-13-bin-cctally-split-design.md
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import pathlib
import re
import sqlite3
import sys
from dataclasses import dataclass, field


def _cctally():
    """Resolve the current ``cctally`` module at call-time (spec §5.5)."""
    return sys.modules["cctally"]


def _load_lib(name: str):
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    import importlib.util as _ilu
    p = pathlib.Path(__file__).resolve().parent / f"{name}.py"
    spec = _ilu.spec_from_file_location(name, p)
    mod = _ilu.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_lib_pricing = _load_lib("_lib_pricing")
_calculate_entry_cost = _lib_pricing._calculate_entry_cost

_lib_display_tz = _load_lib("_lib_display_tz")
_resolve_tz = _lib_display_tz._resolve_tz
format_display_dt = _lib_display_tz.format_display_dt


# === Honest imports from extracted homes ===================================
# Spec 2026-05-17-cctally-core-kernel-extraction.md §3.3: kernel symbols
# (Z-leaf + Z-mid) import from _cctally_core. The legacy shim functions
# for these names are deleted.
from _cctally_core import (
    open_db,
    _command_as_of,
    _canonicalize_optional_iso,
    parse_iso_datetime,
)


# === Module-level back-ref shims for helpers that STAY in bin/cctally ======
# Each shim resolves ``sys.modules['cctally'].X`` at CALL TIME (not bind
# time), so monkeypatches on cctally's namespace propagate into the moved
# code unchanged. `get_claude_session_entries` STAYS as a shim even though
# its natural home is _cctally_cache — tests monkeypatch it via ``ns["X"]``
# (audited 2026-05-17); a direct import would silently bypass the patches.
# See spec §3.5 (carve-out) and §3.7 (stays-on-shim allowlist).
def get_claude_session_entries(*args, **kwargs):
    return sys.modules["cctally"].get_claude_session_entries(*args, **kwargs)


def _resolve_project_key(*args, **kwargs):
    return sys.modules["cctally"]._resolve_project_key(*args, **kwargs)


def _iso_z(*args, **kwargs):
    return sys.modules["cctally"]._iso_z(*args, **kwargs)


def _supports_unicode_stdout(*args, **kwargs):
    return sys.modules["cctally"]._supports_unicode_stdout(*args, **kwargs)


def _style_ansi(*args, **kwargs):
    return sys.modules["cctally"]._style_ansi(*args, **kwargs)


# Private eprint shim per spec §5.3 (pure layer does not back-import
# cctally for ubiquitous helpers; eprint isn't actually called by the
# moved code, but kept here as the canonical pure-layer pattern so
# follow-up edits that need stderr have it ready).
def _eprint(*args):
    print(*args, file=sys.stderr)


# Optional dependency: zoneinfo.ZoneInfo is referenced only as a
# string annotation in moved code; no runtime import needed.


@dataclass(frozen=True)
class ParsedWindow:
    """A single resolved diff window. See spec §2."""
    label: str
    start_utc: dt.datetime
    end_utc: dt.datetime          # exclusive (half-open)
    length_days: float
    kind: str                     # "week" | "month" | "day-range" | "explicit-range"
    week_aligned: bool
    full_weeks_count: int


class WindowMismatchError(ValueError):
    """Two windows have different lengths and --allow-mismatch was not set."""


class NoAnchorError(RuntimeError):
    """Cannot resolve a subscription-week token: no anchor available."""


@dataclass(frozen=True)
class MetricBundle:
    """Per-row metric values for one window. See spec §4."""
    cost_usd: float
    tokens_input: "int | None"
    tokens_output: "int | None"
    tokens_cache_read: "int | None"
    tokens_cache_write: "int | None"
    cache_hit_pct: "float | None"
    used_pct: "float | None"


@dataclass(frozen=True)
class DeltaBundle:
    """Per-row delta values (b - a). See spec §4.

    Asymmetric-row encoding: when one window is missing (new/dropped row),
    the absolute delta is the full b-side value (or the negation of the
    a-side value), and the percent column is None — there's no defined
    relative change against zero or a missing baseline.
    """
    cost_usd: "float | None"
    cost_usd_pct: "float | None"
    tokens_input: "int | None"
    tokens_input_pct: "float | None"
    tokens_output: "int | None"
    tokens_output_pct: "float | None"
    tokens_cache_read: "int | None"
    tokens_cache_read_pct: "float | None"
    tokens_cache_write: "int | None"
    tokens_cache_write_pct: "float | None"
    cache_hit_pct_pp: "float | None"
    used_pct_pp: "float | None"


def _build_delta_bundle(
    a: "MetricBundle | None", b: "MetricBundle | None"
) -> DeltaBundle:
    """Compute b - a for every metric, applying the asymmetric-row rules
    from spec §4 (full value when one side is None; None for the percent
    column when a is None or a-side metric is zero)."""

    def _scalar(av, bv):
        if av is None and bv is None:
            return None, None
        if av is None:
            return bv, None
        if bv is None:
            return -av, None
        delta = bv - av
        if av == 0:
            return delta, None
        return delta, (delta / av) * 100.0

    def _pp(av, bv):
        if av is None or bv is None:
            return None
        return bv - av

    a_cost = a.cost_usd if a else None
    b_cost = b.cost_usd if b else None
    cost, cost_pct = _scalar(a_cost, b_cost)
    ti, ti_pct = _scalar(a.tokens_input if a else None,
                         b.tokens_input if b else None)
    to, to_pct = _scalar(a.tokens_output if a else None,
                         b.tokens_output if b else None)
    tcr, tcr_pct = _scalar(a.tokens_cache_read if a else None,
                           b.tokens_cache_read if b else None)
    tcw, tcw_pct = _scalar(a.tokens_cache_write if a else None,
                           b.tokens_cache_write if b else None)
    return DeltaBundle(
        cost_usd=cost, cost_usd_pct=cost_pct,
        tokens_input=ti, tokens_input_pct=ti_pct,
        tokens_output=to, tokens_output_pct=to_pct,
        tokens_cache_read=tcr, tokens_cache_read_pct=tcr_pct,
        tokens_cache_write=tcw, tokens_cache_write_pct=tcw_pct,
        cache_hit_pct_pp=_pp(a.cache_hit_pct if a else None,
                             b.cache_hit_pct if b else None),
        used_pct_pp=_pp(a.used_pct if a else None,
                        b.used_pct if b else None),
    )


@dataclass(frozen=True)
class ColumnSpec:
    """A single rendered column in a diff section. See spec §4."""
    field: str
    header: str
    format: str          # "usd" | "pct" | "tokens"
    show_in_overall: bool


@dataclass
class DiffRow:
    """One rendered row inside a DiffSection. See spec §4.

    `status` ∈ {"changed", "new", "dropped"}. `sort_key` is the
    magnitude used by the default delta-sort (typically |Δ$|).
    """
    key: str
    label: str
    status: str
    a: "MetricBundle | None"
    b: "MetricBundle | None"
    delta: DeltaBundle
    sort_key: float


@dataclass
class DiffSection:
    """One named section (e.g. overall / models / projects / cache).

    `hidden_count` is how many changed rows were filtered out by the
    noise threshold and reported as "(N hidden, N% of total)" in the
    table renderer.
    """
    name: str
    scope: str
    rows: list
    hidden_count: int
    columns: list


@dataclass(frozen=True)
class NoiseThreshold:
    """Filter parameters for hiding tiny changed rows. See spec §4."""
    min_delta_usd: float = 0.10
    min_delta_pct: float = 1.0
    show_all: bool = False
    user_override: bool = False


@dataclass
class DiffResult:
    """Top-level container produced by `_build_diff_result`. See spec §4."""
    window_a: ParsedWindow
    window_b: ParsedWindow
    mismatched_length: bool
    normalization: str
    used_pct_mode_a: str
    used_pct_mode_b: str
    sections: list
    threshold: NoiseThreshold
    auto_normalized: bool = False
    raw_totals: "dict[str, tuple[MetricBundle | None, MetricBundle | None]]" = field(default_factory=dict)


_DIFF_NW_AGO_RE = re.compile(r"^(\d+)w-ago$")
_DIFF_NM_AGO_RE = re.compile(r"^(\d+)m-ago$")
_DIFF_LAST_ND_RE = re.compile(r"^last-(\d+)d$")
_DIFF_PREV_ND_RE = re.compile(r"^prev-(\d+)d$")
_DIFF_RANGE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})$")


def _parse_diff_window(
    token: str,
    *,
    now_utc: dt.datetime,
    anchor_resets_at: dt.datetime | None,
    anchor_week_start: dt.datetime | None,
    tz_name: str,
) -> ParsedWindow:
    """Resolve a window token to a ParsedWindow. See spec §2.

    `anchor_resets_at` and `anchor_week_start` are the subscription-week
    boundary helpers (most-recent reset and its corresponding week-start).
    Both may be None when the user has never run `record-usage`; in that
    case week tokens raise NoAnchorError.
    """
    if token == "this-week" or token == "last-week" or _DIFF_NW_AGO_RE.match(token):
        if anchor_week_start is None or anchor_resets_at is None:
            raise NoAnchorError(
                f"cannot resolve week token {token!r}: no subscription-week "
                f"anchor available (run record-usage first)"
            )
        if token == "this-week":
            start = anchor_week_start
            end = min(now_utc, anchor_resets_at)
            n = 0
        elif token == "last-week":
            start = anchor_week_start - dt.timedelta(days=7)
            end = anchor_week_start
            n = 1
        else:
            n = int(_DIFF_NW_AGO_RE.match(token).group(1))
            start = anchor_week_start - dt.timedelta(days=7 * n)
            end = start + dt.timedelta(days=7)
        if n == 0:
            # this-week: clamped to now if mid-week, else to the next reset.
            week_aligned = end == anchor_resets_at
            full_weeks_count = 1 if week_aligned else 0
        else:
            # last-week / Nw-ago: both endpoints are subscription-week boundaries.
            week_aligned = True
            full_weeks_count = 1
        length = (end - start).total_seconds() / 86400.0
        return ParsedWindow(
            label=token, start_utc=start, end_utc=end,
            length_days=length, kind="week",
            week_aligned=week_aligned,
            full_weeks_count=full_weeks_count,
        )

    if token == "this-month" or token == "last-month" or _DIFF_NM_AGO_RE.match(token):
        tz = _resolve_tz(tz_name, strict_iana=True, fallback=dt.timezone.utc)
        now_local = now_utc.astimezone(tz)
        if token == "this-month":
            n = 0
        elif token == "last-month":
            n = 1
        else:
            n = int(_DIFF_NM_AGO_RE.match(token).group(1))
        y, m = now_local.year, now_local.month
        for _ in range(n):
            m -= 1
            if m == 0:
                m = 12
                y -= 1
        start_local = dt.datetime(y, m, 1, tzinfo=tz)
        end_y, end_m = (y + 1, 1) if m == 12 else (y, m + 1)
        end_local = dt.datetime(end_y, end_m, 1, tzinfo=tz)
        start = start_local.astimezone(dt.timezone.utc)
        end = end_local.astimezone(dt.timezone.utc)
        length = (end - start).total_seconds() / 86400.0
        return ParsedWindow(
            label=token, start_utc=start, end_utc=end,
            length_days=length, kind="month",
            week_aligned=False,
            full_weeks_count=max(1, int(round(length / 7.0))),
        )

    m = _DIFF_LAST_ND_RE.match(token) or _DIFF_PREV_ND_RE.match(token)
    if m:
        n = int(m.group(1))
        if token.startswith("last-"):
            end = now_utc
            start = now_utc - dt.timedelta(days=n)
        else:
            end = now_utc - dt.timedelta(days=n)
            start = end - dt.timedelta(days=n)
        return ParsedWindow(
            label=token, start_utc=start, end_utc=end,
            length_days=float(n), kind="day-range",
            week_aligned=False, full_weeks_count=0,
        )

    m = _DIFF_RANGE_RE.match(token)
    if m:
        start_d = dt.date.fromisoformat(m.group(1))
        end_d = dt.date.fromisoformat(m.group(2))
        if start_d > end_d:
            raise ValueError(
                f"invalid range {token!r}: range start must be on or before end"
            )
        tz = _resolve_tz(tz_name, strict_iana=True, fallback=dt.timezone.utc)
        start = dt.datetime.combine(start_d, dt.time(0, 0), tzinfo=tz).astimezone(dt.timezone.utc)
        end = dt.datetime.combine(
            end_d + dt.timedelta(days=1), dt.time(0, 0), tzinfo=tz
        ).astimezone(dt.timezone.utc)
        length = (end - start).total_seconds() / 86400.0
        return ParsedWindow(
            label=token, start_utc=start, end_utc=end,
            length_days=length, kind="explicit-range",
            week_aligned=False, full_weeks_count=0,
        )

    raise ValueError(f"invalid window token: {token!r}")


def _humanize_tokens(n: "int | None") -> str:
    """Compact int rendering for diff cells: 1234 -> '1.2K', 1_500_000 -> '1.5M'."""
    if n is None:
        return "—"
    a = abs(n)
    sign = "-" if n < 0 else ""
    if a < 1_000:
        return f"{sign}{a}"
    if a < 1_000_000:
        return f"{sign}{a / 1_000:.1f}K"
    if a < 1_000_000_000:
        return f"{sign}{a / 1_000_000:.1f}M"
    return f"{sign}{a / 1_000_000_000:.1f}B"


def _diff_iter_claude_entries(window: ParsedWindow, *, skip_sync: bool):
    """Honor ParsedWindow's half-open semantics by trimming end_utc by 1 µs
    before passing into the inclusive-end shared cache helper.

    `get_claude_session_entries` is shared with daily/monthly/blocks/
    range-cost/cache-report/sync-week, all of which rely on inclusive
    end-of-day semantics for date-only inputs — so we cannot tighten the
    helper's SQL. `ParsedWindow.end_utc` is documented exclusive, so trim
    by one microsecond locally to bridge the convention gap.
    """
    end_exclusive = window.end_utc - dt.timedelta(microseconds=1)
    return get_claude_session_entries(
        window.start_utc, end_exclusive, skip_sync=skip_sync
    )


def _diff_aggregate_overall(
    window: ParsedWindow,
    *,
    skip_sync: bool = False,
) -> MetricBundle:
    """Sum cost, tokens, and cache stats across all entries in `window`.

    `cache_hit_pct` follows cache-report semantics: cache_read /
    (cache_read + non_cached_input) * 100. used_pct is intentionally
    None — populated separately by `_diff_resolve_used_pct` so we
    only hit the stats DB once per window.
    """
    cost = 0.0
    ti = to = tcr = tcw = 0
    for e in _diff_iter_claude_entries(window, skip_sync=skip_sync):
        if e.model == "<synthetic>":
            continue
        cost += _calculate_entry_cost(
            e.model,
            {
                "input_tokens": e.input_tokens,
                "output_tokens": e.output_tokens,
                "cache_creation_input_tokens": e.cache_creation_tokens,
                "cache_read_input_tokens": e.cache_read_tokens,
            },
            mode="auto",
            cost_usd=e.cost_usd,
        )
        ti += e.input_tokens
        to += e.output_tokens
        tcr += e.cache_read_tokens
        tcw += e.cache_creation_tokens
    denom = tcr + ti
    cache_hit = (tcr / denom * 100.0) if denom > 0 else None
    return MetricBundle(
        cost_usd=cost, tokens_input=ti, tokens_output=to,
        tokens_cache_read=tcr, tokens_cache_write=tcw,
        cache_hit_pct=cache_hit,
        used_pct=None,
    )


def _diff_aggregate_models(
    window: ParsedWindow,
    *,
    skip_sync: bool = False,
) -> dict:
    """Group entries by model id, aggregate to per-model MetricBundle."""
    buckets: dict = {}
    for e in _diff_iter_claude_entries(window, skip_sync=skip_sync):
        if e.model == "<synthetic>":
            continue
        b = buckets.setdefault(e.model, {
            "cost": 0.0, "ti": 0, "to": 0, "tcr": 0, "tcw": 0,
        })
        b["cost"] += _calculate_entry_cost(
            e.model,
            {"input_tokens": e.input_tokens, "output_tokens": e.output_tokens,
             "cache_creation_input_tokens": e.cache_creation_tokens,
             "cache_read_input_tokens": e.cache_read_tokens},
            mode="auto", cost_usd=e.cost_usd,
        )
        b["ti"] += e.input_tokens
        b["to"] += e.output_tokens
        b["tcr"] += e.cache_read_tokens
        b["tcw"] += e.cache_creation_tokens
    out: dict = {}
    for model, b in buckets.items():
        denom = b["tcr"] + b["ti"]
        cache_hit = (b["tcr"] / denom * 100.0) if denom > 0 else None
        out[model] = MetricBundle(
            cost_usd=b["cost"], tokens_input=b["ti"], tokens_output=b["to"],
            tokens_cache_read=b["tcr"], tokens_cache_write=b["tcw"],
            cache_hit_pct=cache_hit, used_pct=None,
        )
    return out


def _diff_aggregate_projects(
    window: ParsedWindow,
    *,
    skip_sync: bool = False,
    group_mode: str = "git-root",
) -> dict:
    """Group entries by ProjectKey.display_key (git-root resolved)."""
    resolver_cache: dict = {}
    buckets: dict = {}
    for e in _diff_iter_claude_entries(window, skip_sync=skip_sync):
        if e.model == "<synthetic>":
            continue
        key = _resolve_project_key(e.project_path, group_mode, resolver_cache)
        b = buckets.setdefault(key.display_key, {
            "cost": 0.0, "ti": 0, "to": 0, "tcr": 0, "tcw": 0,
        })
        b["cost"] += _calculate_entry_cost(
            e.model,
            {"input_tokens": e.input_tokens, "output_tokens": e.output_tokens,
             "cache_creation_input_tokens": e.cache_creation_tokens,
             "cache_read_input_tokens": e.cache_read_tokens},
            mode="auto", cost_usd=e.cost_usd,
        )
        b["ti"] += e.input_tokens
        b["to"] += e.output_tokens
        b["tcr"] += e.cache_read_tokens
        b["tcw"] += e.cache_creation_tokens
    out: dict = {}
    for proj, b in buckets.items():
        denom = b["tcr"] + b["ti"]
        cache_hit = (b["tcr"] / denom * 100.0) if denom > 0 else None
        out[proj] = MetricBundle(
            cost_usd=b["cost"], tokens_input=b["ti"], tokens_output=b["to"],
            tokens_cache_read=b["tcr"], tokens_cache_write=b["tcw"],
            cache_hit_pct=cache_hit, used_pct=None,
        )
    return out


def _diff_aggregate_cache(
    window: ParsedWindow,
    *,
    skip_sync: bool = False,
) -> dict:
    """Cache-active-entries scope: only entries that touched the cache.

    Returns up to two keys:
      * `cache:overall` — every entry with cache_create_tokens > 0 OR
        cache_read_tokens > 0.
      * `cache:claude` — same set, since this codebase only reads Claude
        entries; provided for spec parity with future Codex extension.
    Returns {} when no entries touched the cache.
    """
    cost = 0.0
    tcr = tcw = ti = 0
    for e in _diff_iter_claude_entries(window, skip_sync=skip_sync):
        if e.model == "<synthetic>":
            continue
        if e.cache_creation_tokens == 0 and e.cache_read_tokens == 0:
            continue
        cost += _calculate_entry_cost(
            e.model,
            {"input_tokens": e.input_tokens, "output_tokens": e.output_tokens,
             "cache_creation_input_tokens": e.cache_creation_tokens,
             "cache_read_input_tokens": e.cache_read_tokens},
            mode="auto", cost_usd=e.cost_usd,
        )
        tcr += e.cache_read_tokens
        tcw += e.cache_creation_tokens
        ti += e.input_tokens
    if cost == 0.0 and tcr == 0 and tcw == 0:
        return {}
    denom = tcr + ti
    cache_hit = (tcr / denom * 100.0) if denom > 0 else None
    overall = MetricBundle(
        cost_usd=cost, tokens_input=ti, tokens_output=None,
        tokens_cache_read=tcr, tokens_cache_write=tcw,
        cache_hit_pct=cache_hit, used_pct=None,
    )
    # TODO(codex-diff): when Codex entries are walked, compute a separate
    # claude-scope MetricBundle (currently shares the overall ref because
    # claude is the only source).
    return {"cache:overall": overall, "cache:claude": overall}


def _diff_resolve_used_pct(window: ParsedWindow) -> tuple:
    """Return (used_pct_value, mode) for a window. See spec §1+§4.

    mode ∈ {"exact", "avg", "n/a"}.

    "exact" requires window.kind == "week" AND window.week_aligned AND
    window.full_weeks_count == 1 — i.e., a single complete subscription
    week. Anything partial (this-week mid-week) is "n/a" by design.

    "avg" fires for windows spanning >= 2 full weeks (e.g., last-30d
    over 4-5 weeks): we average max(weekly_percent) across the weeks
    that have snapshot rows. If any subscription week in [start, end)
    lacks a snapshot, mode is downgraded to `n/a`.

    The "exact" lookup is constrained to the target week's
    `week_start_date` so a window with no recorded snapshots correctly
    falls through to `n/a` instead of returning a stale cross-week value.
    (The `_apply_midweek_reset_override` gotcha doesn't apply on this
    code path: a mid-week `this-week` window has `week_aligned=False`
    and routes to `n/a`, never reaching the exact branch.)
    """
    if window.kind == "week" and window.week_aligned and window.full_weeks_count == 1:
        try:
            conn = open_db()
        except Exception:
            return None, "n/a"
        try:
            row = conn.execute(
                "SELECT weekly_percent FROM weekly_usage_snapshots "
                "WHERE week_start_date = ? "
                "  AND captured_at_utc <= ? "
                "ORDER BY captured_at_utc DESC, id DESC LIMIT 1",
                (window.start_utc.date().isoformat(), _iso_z(window.end_utc)),
            ).fetchone()
            if row is None or row[0] is None:
                return None, "n/a"
            return float(row[0]), "exact"
        finally:
            conn.close()
    if window.full_weeks_count >= 2:
        try:
            conn = open_db()
        except Exception:
            return None, "n/a"
        try:
            rows = conn.execute(
                "SELECT week_start_date, MAX(weekly_percent) "
                "FROM weekly_usage_snapshots "
                "WHERE captured_at_utc >= ? AND captured_at_utc < ? "
                "GROUP BY week_start_date",
                (_iso_z(window.start_utc), _iso_z(window.end_utc)),
            ).fetchall()
            vals = [r[1] for r in rows if r[1] is not None]
            if len(vals) < window.full_weeks_count:
                # Spec §9.3: missing-coverage weeks would skew the avg —
                # downgrade to n/a instead of reporting a misleading number.
                return None, "n/a"
            if not vals:
                return None, "n/a"
            return sum(vals) / len(vals), "avg"
        finally:
            conn.close()
    return None, "n/a"


_DIFF_DEFAULT_COLUMNS_OVERALL = [
    ColumnSpec("cost_usd",      "Cost",    "usd",    True),
    ColumnSpec("used_pct",      "Used %",  "pct",    True),
    ColumnSpec("cache_hit_pct", "Cache %", "pct",    True),
    ColumnSpec("tokens_input",  "Tokens",  "tokens", True),
]
_DIFF_DEFAULT_COLUMNS_MODELS = [
    ColumnSpec("cost_usd",      "Cost",    "usd",    False),
    ColumnSpec("cache_hit_pct", "Cache %", "pct",    False),
    ColumnSpec("tokens_input",  "Tokens",  "tokens", False),
]
_DIFF_DEFAULT_COLUMNS_PROJECTS = list(_DIFF_DEFAULT_COLUMNS_MODELS)
_DIFF_DEFAULT_COLUMNS_CACHE = [
    ColumnSpec("cost_usd",      "Cost",    "usd", False),
    ColumnSpec("cache_hit_pct", "Cache %", "pct", False),
]


def _diff_sort_rows(rows: list, sort: str) -> list:
    """Stable sort with deterministic tiebreak on label."""
    if sort == "delta":
        keyfn = lambda r: (-(r.sort_key or 0.0), r.label)
    elif sort == "cost-a":
        keyfn = lambda r: (-(r.a.cost_usd if r.a else 0.0), r.label)
    elif sort == "cost-b":
        keyfn = lambda r: (-(r.b.cost_usd if r.b else 0.0), r.label)
    elif sort == "name":
        keyfn = lambda r: (r.label,)
    elif sort == "status":
        order = {"dropped": 0, "changed": 1, "new": 2}
        keyfn = lambda r: (order.get(r.status, 3), -(r.sort_key or 0.0), r.label)
    else:
        keyfn = lambda r: (-(r.sort_key or 0.0), r.label)
    return sorted(rows, key=keyfn)


def _apply_noise_threshold(rows: list, threshold: NoiseThreshold) -> tuple:
    """Hide changed rows where |Δ$| < min_delta_usd AND |Δ%| < min_delta_pct.

    new/dropped rows are NEVER hidden — a wholly-appearing or wholly-
    disappearing model/project is always interesting regardless of $.

    Returns (visible_rows, hidden_count). With show_all=True, every row
    is visible and hidden_count is 0.
    """
    if threshold.show_all:
        return list(rows), 0
    visible: list = []
    hidden = 0
    for r in rows:
        if r.status != "changed":
            visible.append(r)
            continue
        d_usd = abs(r.delta.cost_usd or 0.0)
        d_pct = abs(r.delta.cost_usd_pct or 0.0)
        if d_usd < threshold.min_delta_usd and d_pct < threshold.min_delta_pct:
            hidden += 1
            continue
        visible.append(r)
    return visible, hidden


def _diff_build_section(
    name: str,
    scope: str,
    a_map: dict,
    b_map: dict,
    columns: list,
    threshold: NoiseThreshold,
    sort: str,
    *,
    label_for_key=None,
    top: "int | None" = None,
) -> DiffSection:
    """Build one section: union the keys of a_map and b_map, classify each
    as changed/new/dropped, sort, then apply the noise filter.

    `top` (when not None and >= 0) caps the number of `changed` rows kept
    after sort+filter. `new`/`dropped` rows are exempt — a wholly-appearing
    or wholly-disappearing entry is always interesting. Capped rows roll
    into `hidden_count` so the footer reflects them.
    """
    keys = set(a_map.keys()) | set(b_map.keys())
    rows: list = []
    for k in keys:
        a = a_map.get(k)
        b = b_map.get(k)
        if a is not None and b is not None:
            status = "changed"
        elif a is None:
            status = "new"
        else:
            status = "dropped"
        delta = _build_delta_bundle(a, b)
        sort_key = abs(delta.cost_usd or 0.0)
        label = label_for_key(k) if label_for_key else k
        full_key = f"{name}:{k}" if not k.startswith(name + ":") else k
        rows.append(DiffRow(
            key=full_key, label=label, status=status,
            a=a, b=b, delta=delta, sort_key=sort_key,
        ))
    sorted_rows = _diff_sort_rows(rows, sort)
    visible, hidden = _apply_noise_threshold(sorted_rows, threshold)
    if top is not None and top >= 0:
        # --top caps `changed` rows only; new/dropped rows are exempt.
        new_dropped = [r for r in visible if r.status != "changed"]
        changed = [r for r in visible if r.status == "changed"]
        capped = changed[:top]
        capped_count = len(changed) - len(capped)
        # Re-sort the union to keep the visual order stable under the
        # caller's chosen sort key.
        visible = _diff_sort_rows(new_dropped + capped, sort)
        # Roll capped rows into hidden_count so the footer is accurate.
        hidden += capped_count
    return DiffSection(
        name=name, scope=scope, rows=visible,
        hidden_count=hidden, columns=columns,
    )


def _normalize_metric_bundle_per_day(
    b: "MetricBundle", length_days: float,
) -> "MetricBundle":
    """Return a NEW MetricBundle with `cost_usd` divided by `length_days`.

    Per spec §2 (mismatched-length rule, line 121): "divide every
    absolute-cost / Δ$ value by `length_days` before display. Δ% stays
    a ratio (always meaningful). `Used %` is NOT normalized." Token
    counts are also left raw — the spec only mentions cost. Cache % is
    already a ratio. `used_pct` is preserved untouched (the overall
    section splices Used % AFTER normalization runs).
    """
    if length_days <= 0:
        return b
    return dataclasses.replace(b, cost_usd=b.cost_usd / length_days)


def _sum_metric_bundles(bundles) -> "MetricBundle | None":
    """Sum a sequence of MetricBundles into a single MetricBundle.
    Returns None if the sequence is empty."""
    bundles = list(bundles)
    if not bundles:
        return None
    cost = sum(b.cost_usd for b in bundles)
    ti = sum(b.tokens_input or 0 for b in bundles)
    to = sum(b.tokens_output or 0 for b in bundles)
    tcr = sum(b.tokens_cache_read or 0 for b in bundles)
    tcw = sum(b.tokens_cache_write or 0 for b in bundles)
    denom = tcr + ti
    cache_hit = (tcr / denom * 100.0) if denom > 0 else None
    return MetricBundle(
        cost_usd=cost, tokens_input=ti, tokens_output=to,
        tokens_cache_read=tcr, tokens_cache_write=tcw,
        cache_hit_pct=cache_hit, used_pct=None,
    )


def _build_diff_result(
    window_a: ParsedWindow,
    window_b: ParsedWindow,
    *,
    threshold: NoiseThreshold,
    sections_requested: list,
    sort: str,
    allow_mismatch: bool = False,
    skip_sync: bool = False,
    top: "int | None" = None,
) -> DiffResult:
    """Top-level diff builder: wire window_a vs window_b through every
    requested section. Raises WindowMismatchError when lengths differ
    unless allow_mismatch=True (then per-day normalization will be
    annotated for downstream renderers)."""
    mismatched = abs(window_a.length_days - window_b.length_days) > 0.01
    auto_normalized = False
    if mismatched:
        same_eligible_kind = (
            window_a.kind == window_b.kind
            and window_a.kind in {"week", "month"}
        )
        if same_eligible_kind:
            # Spec §2 rule 3: auto-normalize same-kind week/month pairs per-day,
            # no flag required. --allow-mismatch is silently a no-op here.
            auto_normalized = True
        elif not allow_mismatch:
            raise WindowMismatchError(
                f"window A is {window_a.length_days:.1f} days, "
                f"window B is {window_b.length_days:.1f} days; "
                f"pass --allow-mismatch to compare anyway with per-day normalization"
            )
    normalization = "per-day" if mismatched else "none"

    used_a, mode_a = _diff_resolve_used_pct(window_a)
    used_b, mode_b = _diff_resolve_used_pct(window_b)

    # Per spec §2 (line 121): when --allow-mismatch lets uneven windows
    # through, divide every absolute-cost value by length_days so cells
    # become "$ per day". Δ% is invariant under uniform scaling, so
    # percent-change cells stay correct. Used % is NEVER normalized
    # (it's already a per-week ratio against the subscription ceiling)
    # — that's why the Used % splice for the overall section runs AFTER
    # the per-bundle normalize below.
    def _norm_a(b: "MetricBundle") -> "MetricBundle":
        return _normalize_metric_bundle_per_day(b, window_a.length_days) if mismatched else b

    def _norm_b(b: "MetricBundle") -> "MetricBundle":
        return _normalize_metric_bundle_per_day(b, window_b.length_days) if mismatched else b

    sections: list = []
    raw_totals: "dict[str, tuple[MetricBundle | None, MetricBundle | None]]" = {}

    if "overall" in sections_requested:
        a_overall_raw = _norm_a(_diff_aggregate_overall(window_a, skip_sync=skip_sync))
        b_overall_raw = _norm_b(_diff_aggregate_overall(window_b, skip_sync=skip_sync))
        # Splice in the resolved Used% AFTER normalization — Used % is
        # never per-day-normalized (it's a weekly ceiling ratio).
        a_overall = dataclasses.replace(a_overall_raw, used_pct=used_a)
        b_overall = dataclasses.replace(b_overall_raw, used_pct=used_b)
        sections.append(_diff_build_section(
            "overall", "all",
            {"overall": a_overall}, {"overall": b_overall},
            _DIFF_DEFAULT_COLUMNS_OVERALL,
            threshold=NoiseThreshold(show_all=True),
            sort=sort,
            label_for_key=lambda k: "Overall",
        ))
        raw_totals["overall"] = (a_overall, b_overall)

    if "models" in sections_requested:
        a_map = {k: _norm_a(v) for k, v in
                 _diff_aggregate_models(window_a, skip_sync=skip_sync).items()}
        b_map = {k: _norm_b(v) for k, v in
                 _diff_aggregate_models(window_b, skip_sync=skip_sync).items()}
        sections.append(_diff_build_section(
            "models", "all", a_map, b_map,
            _DIFF_DEFAULT_COLUMNS_MODELS, threshold, sort,
            top=top,
        ))
        raw_totals["models"] = (
            _sum_metric_bundles(a_map.values()),
            _sum_metric_bundles(b_map.values()),
        )

    if "projects" in sections_requested:
        a_map = {k: _norm_a(v) for k, v in
                 _diff_aggregate_projects(window_a, skip_sync=skip_sync).items()}
        b_map = {k: _norm_b(v) for k, v in
                 _diff_aggregate_projects(window_b, skip_sync=skip_sync).items()}
        sections.append(_diff_build_section(
            "projects", "all", a_map, b_map,
            _DIFF_DEFAULT_COLUMNS_PROJECTS, threshold, sort,
            top=top,
        ))
        raw_totals["projects"] = (
            _sum_metric_bundles(a_map.values()),
            _sum_metric_bundles(b_map.values()),
        )

    if "cache" in sections_requested:
        a_map = {k: _norm_a(v) for k, v in
                 _diff_aggregate_cache(window_a, skip_sync=skip_sync).items()}
        b_map = {k: _norm_b(v) for k, v in
                 _diff_aggregate_cache(window_b, skip_sync=skip_sync).items()}
        sections.append(_diff_build_section(
            "cache", "cache-active-entries", a_map, b_map,
            _DIFF_DEFAULT_COLUMNS_CACHE,
            threshold=NoiseThreshold(show_all=True),
            sort=sort,
        ))
        raw_totals["cache"] = (
            a_map.get("cache:overall"),
            b_map.get("cache:overall"),
        )

    return DiffResult(
        window_a=window_a, window_b=window_b,
        mismatched_length=mismatched, normalization=normalization,
        used_pct_mode_a=mode_a, used_pct_mode_b=mode_b,
        sections=sections, threshold=threshold,
        auto_normalized=auto_normalized,
        raw_totals=raw_totals,
    )


def _check_diff_invariants(result: DiffResult) -> None:
    """Assert spec §4 runtime invariants. Raises AssertionError on drift.

    Invariant: for each side (a, b), sum(cost_usd) over models section
    rows == overall.cost_usd, and likewise for projects. Tolerance is
    1e-9 USD (per the reconcile-test pattern — IEEE-754 ULP drift on
    aggregation order is normal). Sums are skipped when hidden_count > 0
    because the noise filter changes the visible total.
    """
    sections = {s.name: s for s in result.sections}
    if "overall" not in sections:
        return
    overall = sections["overall"].rows[0]

    def _sum(side: str, section_name: str):
        s = sections.get(section_name)
        if s is None:
            return None
        if s.hidden_count > 0:
            # Filter changes the visible sum — invariant doesn't hold.
            return None
        total = 0.0
        for r in s.rows:
            mb = r.a if side == "a" else r.b
            if mb is not None:
                total += mb.cost_usd
        return total

    for side in ("a", "b"):
        m_sum = _sum(side, "models")
        p_sum = _sum(side, "projects")
        o_mb = overall.a if side == "a" else overall.b
        if o_mb is None:
            continue
        o_val = o_mb.cost_usd
        if m_sum is not None:
            assert abs(m_sum - o_val) < 1e-9, (
                f"models {side} sum {m_sum} != overall {o_val} "
                f"(Δ={m_sum - o_val})"
            )
        if p_sum is not None:
            assert abs(p_sum - o_val) < 1e-9, (
                f"projects {side} sum {p_sum} != overall {o_val} "
                f"(Δ={p_sum - o_val})"
            )


# ─────────────────────────────────────────────────────────────────────
# diff renderer — cell formatters
# ─────────────────────────────────────────────────────────────────────


# Single source of truth for the "missing value" glyph so a future style change
# (e.g. swapping em-dash for "n/a") only touches one constant. Cell formatters
# below either use the helper or reference the constant directly when the
# inline pattern reads more clearly.
_DIFF_EM_DASH = "—"


def _diff_or_emdash(value, fmt) -> str:
    """Format `value` via `fmt(value)`, or return em-dash if `value` is None."""
    if value is None:
        return _DIFF_EM_DASH
    return fmt(value)


def _diff_fmt_cost_cell(a: "float | None", b: "float | None") -> str:
    """`$X.XX → $Y.YY` with `—` for missing sides."""
    money = lambda v: f"${v:.2f}"  # noqa: E731
    return f"{_diff_or_emdash(a, money)} → {_diff_or_emdash(b, money)}"


def _diff_fmt_delta_cost_cell(delta: "float | None", pct: "float | None") -> str:
    if delta is None:
        return _DIFF_EM_DASH
    sign = "+" if delta >= 0 else "-"
    pct_s = _diff_or_emdash(
        pct, lambda v: f"{'+' if v >= 0 else ''}{v:.0f}%",
    )
    return f"{sign}${abs(delta):.2f} ({pct_s})"


def _diff_fmt_pct_cell(a: "float | None", b: "float | None") -> str:
    pct = lambda v: f"{v:.0f}%"  # noqa: E731
    return f"{_diff_or_emdash(a, pct)} → {_diff_or_emdash(b, pct)}"


def _diff_fmt_pp_cell(pp: "float | None") -> str:
    if pp is None:
        return _DIFF_EM_DASH
    sign = "+" if pp >= 0 else "-"
    return f"{sign}{abs(pp):.0f}pp"


def _diff_fmt_tokens_cell(a: "int | None", b: "int | None") -> str:
    def _fmt(n: "int | None") -> str:
        if n is None:
            return _DIFF_EM_DASH
        if abs(n) < 1000:
            return str(n)
        return _humanize_tokens(n)
    return f"{_fmt(a)} → {_fmt(b)}"


def _diff_fmt_delta_tokens_cell(delta: "int | None") -> str:
    if delta is None:
        return _DIFF_EM_DASH
    if abs(delta) < 1000:
        return f"{'+' if delta >= 0 else ''}{delta}"
    return f"{'+' if delta >= 0 else ''}{_humanize_tokens(delta)}"


def _diff_color_for_delta(metric: str, delta: "float | None", *, enabled: bool) -> str:
    """Return the ANSI code for a delta cell. Red for "spent more"; green
    for "spent less" or "more cache hits"; empty when disabled or zero."""
    if not enabled or delta is None or delta == 0:
        return ""
    if metric == "cost":
        return "31" if delta > 0 else "32"
    if metric == "cache_pp":
        return "32" if delta > 0 else "31"
    return ""


# ─────────────────────────────────────────────────────────────────────
# diff renderer — banner + window header
# ─────────────────────────────────────────────────────────────────────


def _diff_render_banner() -> str:
    """`╭─╮` title-banner box matching daily/weekly/monthly style."""
    title = "Claude Code Token Usage Report - Diff"
    inner_w = len(title) + 4
    if _supports_unicode_stdout():
        tl, tr, bl, br, h, v = "╭", "╮", "╰", "╯", "─", "│"
    else:
        tl, tr, bl, br, h, v = "+", "+", "+", "+", "-", "|"
    top = f" {tl}{h * inner_w}{tr}"
    blank = f" {v}{' ' * inner_w}{v}"
    mid = f" {v}  {title}  {v}"
    bot = f" {bl}{h * inner_w}{br}"
    return "\n".join([top, blank, mid, blank, bot])


def _diff_render_window_header(
    result: DiffResult, *, color: bool, tz: "ZoneInfo | None" = None,
) -> str:
    """Two-line A/B header + optional mismatched-length banner.

    ``tz`` is the resolved display zone for the date labels routed through
    ``format_display_dt``; ``tz=None`` means host-local.
    """
    lines: list = []
    for label_letter, pw, mode in (
        ("A", result.window_a, result.used_pct_mode_a),
        ("B", result.window_b, result.used_pct_mode_b),
    ):
        start_date = format_display_dt(pw.start_utc, tz, fmt="%Y-%m-%d", suffix=False)
        end_date = format_display_dt(pw.end_utc, tz, fmt="%Y-%m-%d", suffix=False)
        used_label = {
            "exact": "exact Used %",
            "avg": "avg Used %/wk",
            "n/a": "Used % n/a",
        }[mode]
        lines.append(
            f" {label_letter}: {pw.label:<14} "
            f"{start_date} → {end_date}  "
            f"({pw.length_days:.1f}d, {used_label})"
        )
    if result.mismatched_length:
        if result.auto_normalized:
            # Auto-fire (same-kind week/month pair): softer info banner
            # naming both sides + their lengths so the user understands
            # WHY values are per-day.
            if result.window_a.length_days < result.window_b.length_days:
                partial, full = result.window_a, result.window_b
            else:
                partial, full = result.window_b, result.window_a
            msg = (
                f" ℹ Comparing partial {partial.label} "
                f"({partial.length_days:.1f}d) against full {full.label} "
                f"({full.length_days:.1f}d) — values shown per-day."
            )
            if not _supports_unicode_stdout():
                msg = msg.replace("ℹ", "i").replace("—", "--")
            lines.append(_style_ansi(msg, "36", enabled=color))  # cyan
        else:
            # Explicit --allow-mismatch on a non-eligible pair: warning.
            warn = (
                " ⚠ Mismatched window lengths; absolute $ values are "
                "normalized per-day."
            )
            if not _supports_unicode_stdout():
                warn = warn.replace("⚠", "!!")
            lines.append(_style_ansi(warn, "33", enabled=color))  # yellow
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────
# diff renderer — section table
# ─────────────────────────────────────────────────────────────────────


def _diff_box_chars() -> dict:
    """Box-drawing dict; ASCII fallback when unicode isn't supported."""
    if _supports_unicode_stdout():
        return {
            "tl": "┌", "tm": "┬", "tr": "┐",
            "ml": "├", "mm": "┼", "mr": "┤",
            "bl": "└", "bm": "┴", "br": "┘",
            "h": "─", "v": "│",
        }
    return {
        "tl": "+", "tm": "+", "tr": "+",
        "ml": "+", "mm": "+", "mr": "+",
        "bl": "+", "bm": "+", "br": "+",
        "h": "-", "v": "|",
    }


def _diff_section_heading(name: str, width: int) -> str:
    h = "─" if _supports_unicode_stdout() else "-"
    pretty = name.capitalize()
    return f"{h * 3} {pretty} {h * max(0, width - len(pretty) - 5)}"


def _diff_render_section_table(
    section: DiffSection,
    *,
    total_a: "MetricBundle | None",
    total_b: "MetricBundle | None",
    width: int,
    color: bool,
    used_pct_mode_a: str,
    used_pct_mode_b: str,
    threshold: "NoiseThreshold | None" = None,
) -> str:
    """Render one bordered table for a section. The Total row sums all rows
    (visible + hidden) — the caller passes the unfiltered aggregate map as
    total_a/total_b so hidden rows still contribute (spec §4 invariant)."""
    boxes = _diff_box_chars()
    out: list = [_diff_section_heading(section.name, width), ""]

    header_cells: list = ["Model" if section.name == "models"
                          else "Project" if section.name == "projects"
                          else "Scope" if section.name in ("cache", "overall")
                          else section.name.capitalize(),
                          "Cost (A → B)", "Δ Cost"]
    has_used = used_pct_mode_a == used_pct_mode_b and used_pct_mode_a != "n/a"
    if has_used:
        header_cells += [
            "Avg Used %/wk (A → B)" if used_pct_mode_a == "avg" else "Used % (A → B)",
            "Δ pp",
        ]
    header_cells += ["Cache % (A → B)", "Δ pp", "Tokens (A → B)", "Δ Tokens"]

    # Each row is a list of (raw_text, ansi_code) tuples. ansi_code="" means
    # render the cell as plain text. Width math runs on raw_text only, so
    # styling never affects column widths.
    body_cells: list = []

    def _row_cells(label: str,
                   a: "MetricBundle | None", b: "MetricBundle | None",
                   delta) -> list:
        delta_cost_code = _diff_color_for_delta("cost", delta.cost_usd, enabled=color)
        delta_cache_pp_code = _diff_color_for_delta(
            "cache_pp", delta.cache_hit_pct_pp, enabled=color,
        )
        delta_tokens_code = _diff_color_for_delta(
            "cost", delta.tokens_input, enabled=color,
        )
        cells: list = [
            (label, ""),
            (_diff_fmt_cost_cell(
                a.cost_usd if a else None, b.cost_usd if b else None,
            ), ""),
            (_diff_fmt_delta_cost_cell(delta.cost_usd, delta.cost_usd_pct),
             delta_cost_code),
        ]
        if has_used:
            cells += [
                (_diff_fmt_pct_cell(a.used_pct if a else None,
                                    b.used_pct if b else None), ""),
                # Used % Δ pp is NOT styled — only the cache Δ pp slot is.
                (_diff_fmt_pp_cell(delta.used_pct_pp), ""),
            ]
        cells += [
            (_diff_fmt_pct_cell(a.cache_hit_pct if a else None,
                                b.cache_hit_pct if b else None), ""),
            (_diff_fmt_pp_cell(delta.cache_hit_pct_pp), delta_cache_pp_code),
            (_diff_fmt_tokens_cell(a.tokens_input if a else None,
                                   b.tokens_input if b else None), ""),
            (_diff_fmt_delta_tokens_cell(delta.tokens_input), delta_tokens_code),
        ]
        return cells

    for r in section.rows:
        label = r.label
        if r.status in ("new", "dropped"):
            label = f"{label}\n({r.status})"
        body_cells.append(_row_cells(label, r.a, r.b, r.delta))

    if total_a is not None or total_b is not None:
        total_delta = _build_delta_bundle(total_a, total_b)
        body_cells.append(_row_cells("Total", total_a, total_b, total_delta))

    n_cols = len(header_cells)
    # Header has no styling — represent it as (text, "") tuples uniformly.
    header_row = [(h, "") for h in header_cells]
    col_w = [len(h) for h in header_cells]
    all_rows = [header_row] + body_cells
    for row in all_rows:
        for i, (raw, _code) in enumerate(row):
            for line in raw.split("\n"):
                col_w[i] = max(col_w[i], len(line))

    def _line(left, mid, right, fill=None):
        fill = fill or boxes["h"]
        parts = [left]
        for i, w in enumerate(col_w):
            parts.append(fill * (w + 2))
            parts.append(right if i == n_cols - 1 else mid)
        return "".join(parts)

    def _render_row(cells: list) -> str:
        per_cell_lines = []
        max_lines = 1
        for i, (raw, _code) in enumerate(cells):
            lines = raw.split("\n")
            per_cell_lines.append(lines)
            max_lines = max(max_lines, len(lines))
        out_lines: list = []
        for li in range(max_lines):
            parts = [boxes["v"]]
            for i, lines in enumerate(per_cell_lines):
                line = lines[li] if li < len(lines) else ""
                padded = line.ljust(col_w[i])
                code = cells[i][1]
                # ljust runs on raw text first, then ANSI wraps the padded
                # result. Spaces stay outside the ANSI escape so column rules
                # align identically with or without color.
                styled = _style_ansi(padded, code, enabled=bool(code))
                parts.append(f" {styled} ")
                parts.append(boxes["v"])
            out_lines.append("".join(parts))
        return "\n".join(out_lines)

    out.append(_line(boxes["tl"], boxes["tm"], boxes["tr"]))
    out.append(_render_row(header_row))
    out.append(_line(boxes["ml"], boxes["mm"], boxes["mr"]))
    for i, row in enumerate(body_cells):
        out.append(_render_row(row))
        if i < len(body_cells) - 1:
            out.append(_line(boxes["ml"], boxes["mm"], boxes["mr"]))
    out.append(_line(boxes["bl"], boxes["bm"], boxes["br"]))

    if section.hidden_count > 0:
        if threshold is not None:
            usd_lit = f"${threshold.min_delta_usd:.2f}"
            pct_lit = f"{threshold.min_delta_pct:.1f}"
        else:
            usd_lit = "$0.10"
            pct_lit = "1.0"
        out.append(
            f"  ({section.hidden_count} rows hidden; "
            f"|Δ$| < {usd_lit} AND |Δ%| < {pct_lit}. "
            f"Pass --all to show, or --min-delta to override.)"
        )
    return "\n".join(out)


def _diff_render_full_output(
    result: DiffResult,
    *,
    color: bool,
    width: int,
    raw_aggregates: dict,
    tz: "ZoneInfo | None" = None,
) -> str:
    """Compose banner + window header + each section's table.

    ``tz`` is forwarded to ``_diff_render_window_header`` for the date
    labels; ``tz=None`` means host-local.
    """
    parts: list = [
        _diff_render_banner(), "",
        _diff_render_window_header(result, color=color, tz=tz),
    ]
    for section in result.sections:
        ta, tb = raw_aggregates.get(section.name, (None, None))
        parts.append("")
        parts.append(_diff_render_section_table(
            section, total_a=ta, total_b=tb,
            width=width, color=color,
            used_pct_mode_a=result.used_pct_mode_a,
            used_pct_mode_b=result.used_pct_mode_b,
            threshold=result.threshold,
        ))
    return "\n".join(parts)


# ─────────────────────────────────────────────────────────────────────
# diff renderer — JSON
# ─────────────────────────────────────────────────────────────────────


def _diff_metric_to_json(mb: "MetricBundle | None") -> "dict | None":
    if mb is None:
        return None
    return {
        "cost_usd": round(mb.cost_usd, 6),
        "tokens_input": mb.tokens_input,
        "tokens_output": mb.tokens_output,
        "tokens_cache_read": mb.tokens_cache_read,
        "tokens_cache_write": mb.tokens_cache_write,
        "cache_hit_pct": (None if mb.cache_hit_pct is None
                          else round(mb.cache_hit_pct, 3)),
        "used_pct": (None if mb.used_pct is None
                     else round(mb.used_pct, 3)),
    }


def _diff_delta_to_json(d: DeltaBundle) -> dict:
    return {
        "cost_usd": (None if d.cost_usd is None else round(d.cost_usd, 6)),
        "cost_usd_pct": (None if d.cost_usd_pct is None
                         else round(d.cost_usd_pct, 3)),
        "tokens_input": d.tokens_input,
        "tokens_input_pct": (None if d.tokens_input_pct is None
                             else round(d.tokens_input_pct, 3)),
        "tokens_output": d.tokens_output,
        "tokens_output_pct": (None if d.tokens_output_pct is None
                              else round(d.tokens_output_pct, 3)),
        "tokens_cache_read": d.tokens_cache_read,
        "tokens_cache_read_pct": (None if d.tokens_cache_read_pct is None
                                  else round(d.tokens_cache_read_pct, 3)),
        "tokens_cache_write": d.tokens_cache_write,
        "tokens_cache_write_pct": (None if d.tokens_cache_write_pct is None
                                   else round(d.tokens_cache_write_pct, 3)),
        "cache_hit_pct_pp": (None if d.cache_hit_pct_pp is None
                             else round(d.cache_hit_pct_pp, 3)),
        "used_pct_pp": (None if d.used_pct_pp is None
                        else round(d.used_pct_pp, 3)),
    }


def _diff_window_to_json(pw: ParsedWindow, used_pct_mode: str) -> dict:
    return {
        "label": pw.label,
        "kind": pw.kind,
        "start_at": _iso_z(pw.start_utc),
        "end_at": _iso_z(pw.end_utc),
        "length_days": round(pw.length_days, 3),
        "week_aligned": pw.week_aligned,
        "full_weeks_count": pw.full_weeks_count,
        "used_pct_mode": used_pct_mode,
    }


def _diff_to_json_payload(
    result: DiffResult,
    *,
    options: dict,
    now: "dt.datetime | None" = None,
) -> dict:
    """Render a DiffResult as the spec §6 envelope shape.

    The `now` kwarg defaults to `_command_as_of()` so a CCTALLY_AS_OF env var
    pins `generated_at` deterministically in tests/fixtures. Pass an explicit
    `now` to override.
    """
    if now is None:
        now = _command_as_of()
    sections_json: list = []
    for s in result.sections:
        rows_json: list = []
        for r in s.rows:
            rows_json.append({
                "key": r.key,
                "label": r.label,
                "status": r.status,
                "a": _diff_metric_to_json(r.a),
                "b": _diff_metric_to_json(r.b),
                "delta": _diff_delta_to_json(r.delta),
                "sort_key": r.sort_key,
            })
        sections_json.append({
            "name": s.name,
            "scope": s.scope,
            "rows": rows_json,
            "hidden_count": s.hidden_count,
            "columns": [
                {"field": c.field, "header": c.header,
                 "format": c.format, "show_in_overall": c.show_in_overall}
                for c in s.columns
            ],
        })
    return {
        "schema_version": 1,
        "generated_at": _iso_z(now),
        "subcommand": "diff",
        "windows": {
            "a": _diff_window_to_json(result.window_a, result.used_pct_mode_a),
            "b": _diff_window_to_json(result.window_b, result.used_pct_mode_b),
        },
        "mismatched_length": result.mismatched_length,
        "normalization": result.normalization,
        "options": options,
        "sections": sections_json,
    }


def _diff_render_json(
    result: DiffResult,
    *,
    options: dict,
    now: "dt.datetime | None" = None,
) -> str:
    return json.dumps(
        _diff_to_json_payload(result, options=options, now=now),
        indent=2,
    )


def _diff_resolve_anchor(
    now_utc: dt.datetime,
) -> "tuple[dt.datetime | None, dt.datetime | None]":
    """Read the latest weekly_usage_snapshots row to obtain the
    (anchor_week_start, anchor_resets_at) pair, then apply two
    post-processing steps in order so week-token resolution stays
    correct even when the latest snapshot doesn't reflect the current
    subscription week:

    1. **Roll forward stale anchor.** If the latest snapshot's
       `week_end_at` is strictly earlier than `now_utc` — i.e. no
       `record-usage` invocation has fired since the most recent
       reset — synthesize the current week by advancing both endpoints
       by 7-day multiples until the window contains `now_utc`. The
       reset event lookup is skipped in this branch because any
       recorded event pertained to a now-past week. The synthesized
       week has no row in `weekly_usage_snapshots` for its
       `week_start_date`, so `_diff_resolve_used_pct`'s exact branch
       (constrained by `WHERE week_start_date = ?`) returns `n/a` —
       no extra plumbing needed for that to work.

    2. **Apply mid-week reset event override.** When the snapshot is
       current (`we > now_utc`), look up `week_reset_events` for a row
       whose `new_week_end_at` matches the snapshot's `week_end_at`.
       If found and the `effective_reset_at_utc` is later than the
       snapshot's `week_start_at`, override the start to the actual
       reset moment (mirrors `_apply_midweek_reset_override` /
       `_apply_reset_events_to_weekrefs` POST-reset rule).

    Returns (None, None) when the DB is unreachable or has no rows.
    """
    try:
        conn = open_db()
    except Exception:
        return None, None
    try:
        row = conn.execute(
            "SELECT week_start_at, week_end_at "
            "FROM weekly_usage_snapshots "
            "ORDER BY captured_at_utc DESC, id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None, None
        anchor_week_start = None
        anchor_resets_at = None
        if row[0]:
            anchor_week_start = dt.datetime.fromisoformat(
                row[0].replace("Z", "+00:00")
            ).astimezone(dt.timezone.utc)
        if row[1]:
            anchor_resets_at = dt.datetime.fromisoformat(
                row[1].replace("Z", "+00:00")
            ).astimezone(dt.timezone.utc)
        if anchor_week_start is None or anchor_resets_at is None:
            return anchor_week_start, anchor_resets_at

        # Step 1: roll a stale anchor forward by 7-day multiples until
        # the window contains now_utc. Bounded loop (100 iterations ≈
        # 700 days) so a clock skew can't spin forever.
        if anchor_resets_at < now_utc:
            week = dt.timedelta(days=7)
            for _ in range(100):
                if anchor_resets_at >= now_utc:
                    break
                anchor_week_start = anchor_week_start + week
                anchor_resets_at = anchor_resets_at + week
            return anchor_week_start, anchor_resets_at

        # Step 2: snapshot is current — apply mid-week reset event
        # override if one matches this window's end.
        try:
            end_iso = _canonicalize_optional_iso(
                anchor_resets_at.isoformat(timespec="seconds"),
                "diff.anchor.end",
            )
            if end_iso is not None:
                event_row = conn.execute(
                    "SELECT effective_reset_at_utc FROM week_reset_events "
                    "WHERE new_week_end_at = ?",
                    (end_iso,),
                ).fetchone()
                if event_row and event_row[0]:
                    reset_dt = parse_iso_datetime(
                        event_row[0], "reset_event.effective"
                    )
                    if reset_dt > anchor_week_start:
                        anchor_week_start = reset_dt
        except (sqlite3.DatabaseError, ValueError):
            pass
        return anchor_week_start, anchor_resets_at
    finally:
        conn.close()
