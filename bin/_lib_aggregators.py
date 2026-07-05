"""Daily / monthly / weekly / session aggregators for Claude + Codex.

Pure-fn layer (no I/O at import time): holds every helper that groups a
list of session entries into per-bucket or per-session records for the
`daily`, `monthly`, `weekly`, `session`, `codex-daily`, `codex-monthly`,
`codex-weekly`, and `codex-session` subcommands, plus the four
dataclasses they produce (`BucketUsage`, `CodexBucketUsage`,
`CodexSessionUsage`, `ClaudeSessionUsage`) and the Codex
session-path-parsing helper (`_session_path_parts`).

Sibling dependencies (loaded at module-load time via `_load_lib`):
- `_lib_jsonl.UsageEntry`, `_lib_jsonl.CodexEntry` — the dataclasses
  the aggregators iterate over.
- `_lib_pricing._calculate_entry_cost`, `_calculate_codex_entry_cost`,
  `_is_codex_fallback` — per-entry cost computation.
- `_lib_display_tz._resolve_tz` — IANA tz resolution for codex date
  bucketing (Claude aggregators take a `ZoneInfo` directly).
- `_lib_subscription_weeks.SubWeek` — typing for `_aggregate_weekly`'s
  `weeks` parameter.

bin/cctally back-references via `_cctally()` (spec §5.5 pattern, same as
`bin/_lib_subscription_weeks.py`):
- `CODEX_SESSIONS_DIR` — base path used by `_session_path_parts` for
  upstream-compatible relative-path computation.
- `_decode_escaped_cwd` — Claude `project_path` fallback when
  `session_files.project_path` is NULL.

`_JoinedClaudeEntry` (the input type for `_aggregate_claude_sessions`)
is referenced only as a string annotation — no runtime import needed.

`bin/cctally` re-exports every public symbol below so the ~30 internal
call sites + SourceFileLoader-based tests
(`tests/test_lib_share`, `tests/test_dashboard_daily_panel`) resolve
unchanged.

Spec: docs/superpowers/specs/2026-05-13-bin-cctally-split-design.md
"""
from __future__ import annotations

import datetime as dt
import os
import pathlib
import sys
from dataclasses import dataclass
from typing import Any, Callable


def _cctally():
    """Resolve the current `cctally` module at call-time (spec §5.5)."""
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


_lib_jsonl = _load_lib("_lib_jsonl")
UsageEntry = _lib_jsonl.UsageEntry
CodexEntry = _lib_jsonl.CodexEntry

_lib_pricing = _load_lib("_lib_pricing")
_calculate_entry_cost = _lib_pricing._calculate_entry_cost
_calculate_codex_entry_cost = _lib_pricing._calculate_codex_entry_cost
_is_codex_fallback = _lib_pricing._is_codex_fallback

_lib_display_tz = _load_lib("_lib_display_tz")
_resolve_tz = _lib_display_tz._resolve_tz

_lib_subscription_weeks = _load_lib("_lib_subscription_weeks")
SubWeek = _lib_subscription_weeks.SubWeek
_lib_fmt = _load_lib("_lib_fmt")
stable_sum = _lib_fmt.stable_sum


# === Honest imports from extracted homes ===================================
# Spec 2026-05-17-cctally-core-kernel-extraction.md §3.3: kernel symbols
# import from _cctally_core. `CODEX_SESSIONS_DIR` (path constant) and
# `_decode_escaped_cwd` (out-of-scope) stay on the _cctally() accessor.
from _cctally_core import parse_iso_datetime


@dataclass
class BucketUsage:
    """Aggregated usage for one time bucket.

    `bucket` holds the bucket identifier in a format chosen by the caller
    (e.g., "YYYY-MM-DD" for daily, "YYYY-MM" for monthly).
    """
    bucket: str
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    total_tokens: int
    cost_usd: float
    models: list[str]          # Distinct full model names seen (first-seen order)
    model_breakdowns: list[dict[str, Any]]  # Sorted by cost desc


def _new_bucket_acc() -> dict[str, Any]:
    """A fresh running accumulator for one bucket (the pre-``BucketUsage`` shape).

    The single per-bucket accumulation state shared by the full-pass
    ``_aggregate_buckets`` and #271's incremental current-bucket accumulator, so
    the byte-identity proof reduces to "same primitive, entries appended in the
    same order" (spec §6).
    """
    return {
        "input": 0, "output": 0, "cache_create": 0, "cache_read": 0,
        "cost": 0.0, "models": {}, "models_order": [],
    }


def _fold_entry(acc: dict[str, Any], entry: UsageEntry, mode: str = "auto") -> None:
    """Fold one entry into ``acc`` (the exact per-entry logic of ``_aggregate_buckets``).

    Carries over verbatim: the ``<synthetic>`` skip (never touches ``acc`` for
    them, so it is safe over an unfiltered delta stream), the ``-fast``
    ``display_model`` suffix, the ``_calculate_entry_cost`` call, the four
    integer token ``+=``, the running float ``cost +=``, the per-model
    sub-bucket sums, and the first-seen ``models_order.append``. Order-sensitive:
    the running ``cost`` is a left-fold, so callers MUST fold in the same order
    the full pass would (the pinned ``(timestamp_utc, id)`` order, #271 §5).
    """
    if entry.model == "<synthetic>":
        return
    usage = entry.usage
    display_model = f"{entry.model}-fast" if usage.get("speed") == "fast" else entry.model
    inp = int(usage.get("input_tokens", 0) or 0)
    out = int(usage.get("output_tokens", 0) or 0)
    cc = int(usage.get("cache_creation_input_tokens", 0) or 0)
    cr = int(usage.get("cache_read_input_tokens", 0) or 0)
    cost = _calculate_entry_cost(
        entry.model, usage, mode=mode, cost_usd=entry.cost_usd,
    )
    acc["input"] += inp
    acc["output"] += out
    acc["cache_create"] += cc
    acc["cache_read"] += cr
    acc["cost"] += cost
    model_bucket = acc["models"].setdefault(display_model, {
        "input": 0, "output": 0, "cache_create": 0, "cache_read": 0, "cost": 0.0,
    })
    model_bucket["input"] += inp
    model_bucket["output"] += out
    model_bucket["cache_create"] += cc
    model_bucket["cache_read"] += cr
    model_bucket["cost"] += cost
    if display_model not in acc["models_order"]:
        acc["models_order"].append(display_model)


def _finalize_bucket(bucket_key: str, acc: dict[str, Any]) -> BucketUsage:
    """Build the immutable ``BucketUsage`` from a running accumulator.

    Copies ``models`` / ``model_breakdowns`` so the returned row shares no
    mutable state with ``acc`` (F7 — a live accumulator may keep being folded,
    e.g. #271's persisted current-bucket state).
    """
    model_breakdowns = [
        {
            "modelName": model,
            "inputTokens": mb["input"],
            "outputTokens": mb["output"],
            "cacheCreationTokens": mb["cache_create"],
            "cacheReadTokens": mb["cache_read"],
            "cost": mb["cost"],
        }
        for model, mb in acc["models"].items()
    ]
    model_breakdowns.sort(key=lambda m: m["cost"], reverse=True)
    total_tokens = (
        acc["input"] + acc["output"] + acc["cache_create"] + acc["cache_read"]
    )
    return BucketUsage(
        bucket=bucket_key,
        input_tokens=acc["input"],
        output_tokens=acc["output"],
        cache_creation_tokens=acc["cache_create"],
        cache_read_tokens=acc["cache_read"],
        total_tokens=total_tokens,
        cost_usd=acc["cost"],
        models=list(acc["models_order"]),
        model_breakdowns=model_breakdowns,
    )


def _aggregate_buckets(
    entries: list[UsageEntry],
    key_fn: Callable[[UsageEntry], str],
    mode: str = "auto",
) -> list[BucketUsage]:
    """Group UsageEntry list into per-bucket records.

    `key_fn(entry)` returns the bucket key (e.g. "2026-04-17" or "2026-04").
    The returned list is sorted by bucket key ascending — callers reverse
    for --order desc.  Model breakdowns within each bucket are sorted by
    descending cost, matching upstream ccusage.

    The per-entry accumulation is the shared ``_fold_entry`` primitive (spec
    §6): a ``<synthetic>`` entry is skipped BEFORE keying (so a synthetic-only
    key never materializes an empty bucket), and each surviving entry is folded
    into its bucket's running accumulator, finalized per key in ascending order.
    """
    by_bucket: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if entry.model == "<synthetic>":
            continue
        key = key_fn(entry)
        acc = by_bucket.get(key)
        if acc is None:
            by_bucket[key] = acc = _new_bucket_acc()
        _fold_entry(acc, entry, mode)

    return [_finalize_bucket(key, by_bucket[key]) for key in sorted(by_bucket.keys())]


def _aggregate_daily(
    entries: list[UsageEntry],
    mode: str = "auto",
    *,
    tz: "Any | None" = None,
) -> list[BucketUsage]:
    """Daily grouping: tz-localized date (YYYY-MM-DD).

    Day boundaries follow the resolved display tz (`tz=None` -> host local
    via bare astimezone(); explicit ZoneInfo -> that zone). Per spec
    Q5/F6 this is intentional: setting `display.tz=utc` makes daily
    buckets cut at UTC midnight even when the host is in a different zone.
    """
    return _aggregate_buckets(
        entries,
        key_fn=lambda e: e.timestamp.astimezone(tz).strftime("%Y-%m-%d"),
        mode=mode,
    )


def _aggregate_monthly(
    entries: list[UsageEntry],
    mode: str = "auto",
    *,
    tz: "Any | None" = None,
) -> list[BucketUsage]:
    """Monthly grouping: tz-localized calendar month (YYYY-MM).

    See ``_aggregate_daily`` re: day-boundary semantics.
    """
    return _aggregate_buckets(
        entries,
        key_fn=lambda e: e.timestamp.astimezone(tz).strftime("%Y-%m"),
        mode=mode,
    )


def _aggregate_daily_by_project(
    keyed_entries: list[tuple[Any, UsageEntry]],
    *,
    tz: "Any | None" = None,
    mode: str = "auto",
) -> list[tuple[Any, list[BucketUsage]]]:
    """Group ``(project_key, UsageEntry)`` pairs into per-project daily buckets.

    Returns ``[(project_key, [BucketUsage date-asc]), ...]`` ordered by each
    project's total cost descending, ties broken by ``project_key.display_key``
    ascending. ``project_key`` is opaque/hashable (a ``ProjectKey``); resolution
    happened in the caller, so this stays pure (no filesystem).

    Reuses ``_aggregate_daily`` per group, so per-model breakdowns, token sums,
    and ``mode``/``cost_usd`` threading are identical to the non-instances path.
    """
    grouped: dict[Any, list[UsageEntry]] = {}
    order: list[Any] = []
    for key, entry in keyed_entries:
        bucket = grouped.get(key)
        if bucket is None:
            grouped[key] = bucket = []
            order.append(key)
        bucket.append(entry)

    ranked: list[tuple[Any, list[BucketUsage], float]] = []
    for key in order:
        buckets = _aggregate_daily(grouped[key], mode=mode, tz=tz)  # date-asc
        total = stable_sum(b.cost_usd for b in buckets)
        ranked.append((key, buckets, total))

    ranked.sort(key=lambda t: (-t[2], t[0].display_key))
    return [(key, buckets) for key, buckets, _ in ranked]


def _aggregate_weekly(
    entries: list[UsageEntry],
    weeks: list[SubWeek],
    mode: str = "auto",
) -> list[BucketUsage]:
    """Group UsageEntry list into per-week buckets aligned to `weeks`.

    Entries outside every SubWeek's interval are dropped upstream (before
    handing off to `_aggregate_buckets`, which does not itself tolerate a
    `None` key — it would place a `None` key in the dict and then blow up
    on the final `sorted(by_bucket.keys())`). The returned
    `BucketUsage.bucket` equals the week's `start_date.isoformat()`.
    First-match-wins for overlapping SubWeeks (can occur at Anthropic
    reset-day-drift boundaries — see `_compute_subscription_weeks`).
    """
    # Pre-parse week bounds once. Both `parsed_bounds` (sorted by
    # `start_dt` ASC via `_compute_subscription_weeks`) and the entry
    # list (sorted by `timestamp_utc` ASC from SQL) are sorted, so we
    # can use bisect on a parallel `starts` list to locate the
    # candidate week in O(log W) per entry rather than the linear
    # scan that previously ran ~130k x ~54 = 7M comparisons.
    import bisect
    parsed_bounds: list[tuple[dt.datetime, dt.datetime, str]] = []
    for w in weeks:
        start_dt = parse_iso_datetime(w.start_ts, "week.start_ts")
        end_dt = parse_iso_datetime(w.end_ts, "week.end_ts")
        parsed_bounds.append((start_dt, end_dt, w.start_date.isoformat()))

    starts = [b[0] for b in parsed_bounds]

    def _week_key_or_none(entry: UsageEntry) -> str | None:
        ts = entry.timestamp  # TZ-aware datetime (enforced by _parse_usage_entries)
        # Rightmost week whose start_dt <= ts.
        idx = bisect.bisect_right(starts, ts) - 1
        if idx < 0:
            return None
        # Preserve first-match-wins semantics for the rare overlap
        # regions that appear at Anthropic reset-day-drift boundaries:
        # walk back while prior weeks also contain ts. Non-overlap
        # case exits this loop immediately.
        while idx > 0:
            prev_start, prev_end, _prev_key = parsed_bounds[idx - 1]
            if prev_start <= ts < prev_end:
                idx -= 1
            else:
                break
        start_dt, end_dt, key = parsed_bounds[idx]
        if start_dt <= ts < end_dt:
            return key
        return None

    # Precompute key for each entry and drop Nones; avoids scanning
    # parsed_bounds twice (once to filter, once again inside the closure
    # `_aggregate_buckets` calls).
    keyed: list[tuple[UsageEntry, str]] = []
    for e in entries:
        k = _week_key_or_none(e)
        if k is not None:
            keyed.append((e, k))

    key_lookup = {id(e): k for e, k in keyed}
    in_range_entries = [e for e, _ in keyed]

    return _aggregate_buckets(
        in_range_entries,
        key_fn=lambda e: key_lookup[id(e)],
        mode=mode,
    )


@dataclass
class CodexBucketUsage:
    """Aggregated Codex usage for one time bucket (date or month)."""
    bucket: str
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int
    total_tokens: int
    cost_usd: float
    models: list[str]                         # Distinct full model names (first-seen order)
    model_breakdowns: list[dict[str, Any]]    # Sorted by cost desc


@dataclass
class CodexSessionUsage:
    """Aggregated Codex usage for one session.

    `session_id_path` is the upstream-compatible identifier: relative path
    under ~/.codex/sessions/ WITHOUT the .jsonl extension
    (e.g. "2025/12/25/rollout-..."). `session_file` is the basename without
    .jsonl. `directory` is the relative parent path. `session_id` is the
    inner UUID (from JSONL session_meta), retained for debug/display but
    not used as a grouping key.
    """
    session_id: str
    session_id_path: str
    session_file: str
    directory: str
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int
    total_tokens: int
    cost_usd: float
    models: list[str]
    model_breakdowns: list[dict[str, Any]]
    last_activity: dt.datetime
    # Issue #110: the matched $CODEX_HOME root in home-root form
    # (e.g. "<root>/.codex", or "<root>" for a direct-JSONL root). Used ONLY
    # to disambiguate the displayed / JSON label when two cross-root sessions
    # share the same relative `session_id_path`. "" for the bare-relative
    # fixture form (which cannot collide cross-root). Single-root data leaves
    # every row's `codex_root` constant, so the renderers' collision check
    # never fires and output stays byte-identical.
    codex_root: str = ""


@dataclass
class ClaudeSessionUsage:
    """Aggregated Claude usage for one sessionId (may span multiple JSONL files)."""
    session_id: str
    project_path: str
    source_paths: list[str]
    first_activity: dt.datetime
    last_activity: dt.datetime
    input_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    output_tokens: int
    total_tokens: int
    cost_usd: float
    models: list[str]                       # first-seen order
    model_breakdowns: list[dict[str, Any]]  # sorted by cost desc


def _aggregate_codex_buckets(
    entries: list[CodexEntry],
    key_fn: Callable[[CodexEntry], str],
    speed: str = "standard",
) -> list[CodexBucketUsage]:
    """Group CodexEntry list into per-bucket records sorted by key ascending.

    Model breakdowns within each bucket are sorted by descending cost —
    matches upstream ccusage-codex.
    """
    by_bucket: dict[str, dict[str, Any]] = {}
    models_order: dict[str, list[str]] = {}

    for entry in entries:
        key = key_fn(entry)
        bucket = by_bucket.setdefault(key, {
            "input": 0, "cached_input": 0, "output": 0,
            "reasoning": 0, "total": 0, "cost": 0.0, "models": {},
        })
        order = models_order.setdefault(key, [])

        cost = _calculate_codex_entry_cost(
            entry.model,
            entry.input_tokens,
            entry.cached_input_tokens,
            entry.output_tokens,
            entry.reasoning_output_tokens,
            speed=speed,
        )

        bucket["input"] += entry.input_tokens
        bucket["cached_input"] += entry.cached_input_tokens
        bucket["output"] += entry.output_tokens
        bucket["reasoning"] += entry.reasoning_output_tokens
        bucket["total"] += entry.total_tokens
        bucket["cost"] += cost

        mb = bucket["models"].setdefault(entry.model, {
            "input": 0, "cached_input": 0, "output": 0,
            "reasoning": 0, "cost": 0.0,
        })
        mb["input"] += entry.input_tokens
        mb["cached_input"] += entry.cached_input_tokens
        mb["output"] += entry.output_tokens
        mb["reasoning"] += entry.reasoning_output_tokens
        mb["cost"] += cost

        if entry.model not in order:
            order.append(entry.model)

    result: list[CodexBucketUsage] = []
    for key in sorted(by_bucket.keys()):
        b = by_bucket[key]
        model_breakdowns = [
            {
                "modelName": model,
                "inputTokens": mb["input"],
                "cachedInputTokens": mb["cached_input"],
                "outputTokens": mb["output"],
                "reasoningOutputTokens": mb["reasoning"],
                "totalTokens": mb["input"] + mb["output"],
                "cost": mb["cost"],
                "isFallback": _is_codex_fallback(model),
            }
            for model, mb in b["models"].items()
        ]
        model_breakdowns.sort(key=lambda m: m["cost"], reverse=True)
        result.append(CodexBucketUsage(
            bucket=key,
            input_tokens=b["input"],
            cached_input_tokens=b["cached_input"],
            output_tokens=b["output"],
            reasoning_output_tokens=b["reasoning"],
            total_tokens=b["input"] + b["output"],
            cost_usd=b["cost"],
            models=models_order[key],
            model_breakdowns=model_breakdowns,
        ))
    return result


def _aggregate_codex_daily(
    entries: list[CodexEntry], *, tz_name: str | None = None,
    speed: str = "standard",
) -> list[CodexBucketUsage]:
    """Daily grouping. Default: local tz. With ``tz_name``: that IANA zone."""
    tz = _resolve_tz(tz_name)
    if tz is not None:
        key_fn = lambda e: e.timestamp.astimezone(tz).strftime("%Y-%m-%d")  # noqa: E731
    else:
        key_fn = lambda e: e.timestamp.astimezone().strftime("%Y-%m-%d")    # noqa: E731
    return _aggregate_codex_buckets(entries, key_fn=key_fn, speed=speed)


def _aggregate_codex_monthly(
    entries: list[CodexEntry], *, tz_name: str | None = None,
    speed: str = "standard",
) -> list[CodexBucketUsage]:
    """Monthly grouping. Default: local tz. With ``tz_name``: that IANA zone."""
    tz = _resolve_tz(tz_name)
    if tz is not None:
        key_fn = lambda e: e.timestamp.astimezone(tz).strftime("%Y-%m")  # noqa: E731
    else:
        key_fn = lambda e: e.timestamp.astimezone().strftime("%Y-%m")    # noqa: E731
    return _aggregate_codex_buckets(entries, key_fn=key_fn, speed=speed)


def _aggregate_codex_weekly(
    entries: list[CodexEntry],
    tz_name: str | None,
    week_start_idx: int,
    speed: str = "standard",
) -> list[CodexBucketUsage]:
    """Group Codex entries by calendar week.

    Week-start day is controlled by ``week_start_idx`` (0=Mon..6=Sun), which
    the caller resolves from config.json via ``get_week_start_name`` +
    ``WEEKDAY_MAP``. Bucket key is the ISO date of the week's first day
    in the display timezone (local tz when ``tz_name`` is None).
    """
    tz = _resolve_tz(tz_name)

    def _week_key(entry: CodexEntry) -> str:
        # internal fallback: host-local intentional (else branch)
        local_dt = entry.timestamp.astimezone(tz) if tz is not None else entry.timestamp.astimezone()
        local_date = local_dt.date()
        diff = (local_date.weekday() - week_start_idx) % 7
        week_start = local_date - dt.timedelta(days=diff)
        return week_start.isoformat()

    return _aggregate_codex_buckets(entries, key_fn=_week_key, speed=speed)


def _session_path_parts(source_path: str) -> tuple[str, str, str]:
    """Return (session_id_path, session_file, directory) from a full path.

    session_id_path = relative path under the matched $CODEX_HOME session
                      root with .jsonl stripped (e.g. "2025/12/25/rollout-...").
    session_file    = basename without .jsonl extension.
    directory       = relative parent path under the matched root.

    Tries each root in _codex_session_roots() order (the same list/order the
    discovery walkers use, so overlapping/prefix roots resolve to the FIRST
    matching root deterministically); first relative_to() that succeeds wins.
    Falls back to the bare-relative ".codex/sessions/<rest>" fixture form (the
    shape emitted by build-codex-fixtures.py so committed fixture cache.db
    files stay free of maintainer absolute paths), then basename. Direct-JSONL
    roots yield an id relative to <entry> itself (no sessions/ prefix).
    """
    roots = _cctally()._codex_session_roots()
    p = pathlib.Path(source_path)
    rel: pathlib.PurePath | None = None
    for root in roots:
        try:
            rel = p.relative_to(root)
            break
        except ValueError:
            continue
    if rel is None:
        # Bare-relative ".codex/sessions/<rest>" (fixture form), else basename.
        # Use PurePosixPath to avoid Windows-style drive parsing on unusual
        # inputs; fixture-emitted paths are always POSIX.
        parts = pathlib.PurePosixPath(source_path).parts
        if len(parts) >= 3 and parts[0] == ".codex" and parts[1] == "sessions":
            rel = pathlib.PurePosixPath(*parts[2:])
        else:
            rel = pathlib.Path(p.name)
    stem = rel.with_suffix("")  # strip .jsonl
    return str(stem), stem.name, str(stem.parent)


def _codex_home_root_from_prefix(root_prefix: str) -> str:
    """Normalize the aggregator's `root_prefix` to the matched $CODEX_HOME entry.

    `root_prefix` is `source_path` with the relative `id_path` tail removed, so a
    Codex-home match looks like "<root>/.codex/sessions/" and a direct-JSONL
    match like "<root>/". Strip the trailing slash and any "/sessions" tail to
    recover the home root the user configured — the unit the issue #110
    disambiguator labels by. The bare-relative fixture prefix ".codex/sessions/"
    normalizes to ".codex" (constant across fixtures, so it never collides).
    """
    s = root_prefix.rstrip("/")
    if s.endswith("/sessions"):
        s = s[: -len("/sessions")]
    return s


def _aggregate_codex_sessions(entries: list[CodexEntry], speed: str = "standard") -> list[CodexSessionUsage]:
    """Group by session file path (upstream-compatible).

    Sessions are keyed by the full relative-path-without-.jsonl rather than
    the inner UUID. Result is sorted by last_activity descending (most
    recent first), matching upstream's default view.

    Per-model breakdowns include `isFallback: bool` — true when the model is
    absent from CODEX_MODEL_PRICING.
    """
    by_session: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in entries:
        id_path, file_name, directory = _session_path_parts(entry.source_path)
        # Disambiguate identical relative paths under DIFFERENT $CODEX_HOME
        # roots (issue #108). _session_path_parts strips the matched root, so
        # <rootA>/sessions/2026/04/17/rollout-x.jsonl and the same relative
        # path under <rootB> both yield id_path "2026/04/17/rollout-x";
        # grouping on id_path alone would silently merge two distinct sessions
        # (summed tokens, one UUID). Key on (root_prefix, id_path), where
        # root_prefix is source_path with the id_path tail removed. Single-root
        # data — and the bare-relative fixture form — has a constant prefix, so
        # the grouping, insertion order, and every golden stay byte-identical;
        # only a genuine cross-root collision splits into separate rows.
        suffix = id_path + ".jsonl"
        sp = entry.source_path
        root_prefix = sp[: -len(suffix)] if sp.endswith(suffix) else sp
        sess = by_session.setdefault((root_prefix, id_path), {
            "session_id_uuid": entry.session_id,
            "session_id_path": id_path,
            "session_file": file_name,
            "directory": directory,
            # Matched $CODEX_HOME root (home-root form) — issue #110 display
            # disambiguator. Derived from the same root_prefix that keys the
            # group, so it's constant per group.
            "codex_root": _codex_home_root_from_prefix(root_prefix),
            "input": 0, "cached_input": 0, "output": 0, "reasoning": 0,
            "cost": 0.0, "models": {}, "models_order": [],
            "last": entry.timestamp,
        })
        cost = _calculate_codex_entry_cost(
            entry.model, entry.input_tokens, entry.cached_input_tokens,
            entry.output_tokens, entry.reasoning_output_tokens, speed=speed,
        )
        sess["input"] += entry.input_tokens
        sess["cached_input"] += entry.cached_input_tokens
        sess["output"] += entry.output_tokens
        sess["reasoning"] += entry.reasoning_output_tokens
        sess["cost"] += cost

        mb = sess["models"].setdefault(entry.model, {
            "input": 0, "cached_input": 0, "output": 0, "reasoning": 0, "cost": 0.0,
        })
        mb["input"] += entry.input_tokens
        mb["cached_input"] += entry.cached_input_tokens
        mb["output"] += entry.output_tokens
        mb["reasoning"] += entry.reasoning_output_tokens
        mb["cost"] += cost

        if entry.model not in sess["models_order"]:
            sess["models_order"].append(entry.model)
        if entry.timestamp > sess["last"]:
            sess["last"] = entry.timestamp

    result: list[CodexSessionUsage] = []
    for _group_key, s in by_session.items():
        model_breakdowns = [
            {
                "modelName": model,
                "inputTokens": mb["input"],
                "cachedInputTokens": mb["cached_input"],
                "outputTokens": mb["output"],
                "reasoningOutputTokens": mb["reasoning"],
                "totalTokens": mb["input"] + mb["output"],
                "cost": mb["cost"],
                "isFallback": _is_codex_fallback(model),
            }
            for model, mb in s["models"].items()
        ]
        model_breakdowns.sort(key=lambda m: m["cost"], reverse=True)
        result.append(CodexSessionUsage(
            session_id=s["session_id_uuid"],
            session_id_path=s["session_id_path"],
            session_file=s["session_file"],
            directory=s["directory"],
            input_tokens=s["input"],
            cached_input_tokens=s["cached_input"],
            output_tokens=s["output"],
            reasoning_output_tokens=s["reasoning"],
            # Codex `input` is cache-inclusive (LiteLLM convention; see the
            # "Codex token semantics" gotcha in CLAUDE.md) and `output`
            # subsumes reasoning, so `input + output` already counts ALL
            # tokens processed — the same "all tokens" semantic the Claude
            # session roll-up reaches via input+output+cache (issue #104).
            # Adding cache here would double-count. Matches upstream.
            total_tokens=s["input"] + s["output"],
            cost_usd=s["cost"],
            models=list(s["models_order"]),
            model_breakdowns=model_breakdowns,
            last_activity=s["last"],
            codex_root=s["codex_root"],
        ))
    result.sort(key=lambda x: x.last_activity, reverse=True)
    return result


def _aggregate_claude_sessions(
    entries: list["_JoinedClaudeEntry"],
    mode: str = "auto",
) -> list[ClaudeSessionUsage]:
    """Group entries by session_id, collapsing resumed-across-files sessions.

    Entries with session_id=None fall back to filename UUID (derived from
    source_path). Cost is computed fresh from CLAUDE_MODEL_PRICING.
    Returns descending-by-last_activity; caller reverses for --order asc.
    """
    _decode_escaped_cwd = _cctally()._decode_escaped_cwd
    by_session: dict[str, dict[str, Any]] = {}
    warn_count = 0

    for entry in entries:
        # Skip synthetic entries (Claude Code internal markers, not real
        # model calls). Mirrors `_aggregate_buckets` (line ~2176). Must
        # occur before the session_id fallback so synthetic entries don't
        # inflate warn_count either.
        if entry.model == "<synthetic>":
            continue
        sid = entry.session_id
        if sid is None:
            stem = os.path.splitext(os.path.basename(entry.source_path))[0]
            sid = stem
            warn_count += 1

        sess = by_session.setdefault(sid, {
            "session_id": sid,
            "project_path": entry.project_path or _decode_escaped_cwd(
                os.path.basename(os.path.dirname(entry.source_path))
            ),
            "source_paths": set(),
            "first": entry.timestamp,
            "last": entry.timestamp,
            "input": 0, "cache_create": 0, "cache_read": 0, "output": 0,
            "cost": 0.0,
            "models_order": [],
            "models": {},
            "latest_source_path": entry.source_path,
            "latest_ts": entry.timestamp,
        })

        sess["source_paths"].add(entry.source_path)
        if entry.timestamp < sess["first"]:
            sess["first"] = entry.timestamp
        if entry.timestamp > sess["last"]:
            sess["last"] = entry.timestamp
        # Track latest source_path for tie-breaker when resume crosses cwd.
        if entry.timestamp >= sess["latest_ts"]:
            sess["latest_ts"] = entry.timestamp
            sess["latest_source_path"] = entry.source_path
            if entry.project_path:
                sess["project_path"] = entry.project_path

        usage = {
            "input_tokens": entry.input_tokens,
            "output_tokens": entry.output_tokens,
            "cache_creation_input_tokens": entry.cache_creation_tokens,
            "cache_read_input_tokens": entry.cache_read_tokens,
        }
        cost = _calculate_entry_cost(entry.model, usage, mode=mode, cost_usd=entry.cost_usd)

        sess["input"] += entry.input_tokens
        sess["cache_create"] += entry.cache_creation_tokens
        sess["cache_read"] += entry.cache_read_tokens
        sess["output"] += entry.output_tokens
        sess["cost"] += cost

        if entry.model not in sess["models"]:
            sess["models_order"].append(entry.model)
        mb = sess["models"].setdefault(entry.model, {
            "model": entry.model,
            "input": 0, "cache_create": 0, "cache_read": 0, "output": 0,
            "cost": 0.0,
        })
        mb["input"] += entry.input_tokens
        mb["cache_create"] += entry.cache_creation_tokens
        mb["cache_read"] += entry.cache_read_tokens
        mb["output"] += entry.output_tokens
        mb["cost"] += cost

    if warn_count:
        print(
            f"Warning: {warn_count} entries lacked session_files rows "
            f"(cache may be catching up).",
            file=sys.stderr,
        )

    # Materialize and sort.
    results: list[ClaudeSessionUsage] = []
    for sess in by_session.values():
        breakdowns = sorted(
            [sess["models"][m] for m in sess["models_order"]],
            key=lambda mb: -mb["cost"],
        )
        # Issue #104: Total Tokens sums ALL four components (input + output
        # + cache create + cache read), matching `daily`/`monthly` and
        # upstream ccusage v20. (Supersedes the original Spec A2.8
        # input+output-only convention.) The `codex-session` parallel is
        # preserved at the SEMANTIC level — both report "all tokens
        # processed" — even though its surface formula stays `input+output`
        # (Codex `input_tokens` is already cache-inclusive; see line ~593).
        total_tokens = (
            sess["input"] + sess["output"]
            + sess["cache_create"] + sess["cache_read"]
        )
        results.append(ClaudeSessionUsage(
            session_id=sess["session_id"],
            project_path=sess["project_path"],
            source_paths=sorted(sess["source_paths"]),
            first_activity=sess["first"],
            last_activity=sess["last"],
            input_tokens=sess["input"],
            cache_creation_tokens=sess["cache_create"],
            cache_read_tokens=sess["cache_read"],
            output_tokens=sess["output"],
            total_tokens=total_tokens,
            cost_usd=sess["cost"],
            models=sess["models_order"],
            model_breakdowns=breakdowns,
        ))
    results.sort(key=lambda s: s.last_activity, reverse=True)
    return results
