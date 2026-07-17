"""diff command handler + its two-window debug-sample reporter.

Eager I/O sibling: bin/cctally loads this at startup (AFTER the _lib_diff_kernel
block) and re-exports cmd_diff + _emit_diff_debug_samples onto the cctally
namespace. The parser dispatches via c.cmd_diff; test_debug_sample_emission
reaches mod._emit_diff_debug_samples on the ns.

Accessor discipline (spec §2): _cctally_core kernel symbols are honest-imported;
the dedicated pure kernel _lib_diff_kernel is honest-imported as `dk`; every
OTHER cctally helper (display-tz, color, config bridge, the pricing-mismatch
debug cluster, and the module-level _DEBUG_REPORT_EMITTED flag) is reached via
the call-time _cctally() accessor. No _cctally_* sibling is imported directly.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import shutil
import sqlite3
import sys

import _lib_diff_kernel as dk

from _cctally_core import _command_as_of, eprint


def _cctally():
    """Call-time accessor to the cctally module namespace (ns-patchable)."""
    return sys.modules["cctally"]


def _emit_diff_debug_samples(args, window_a, window_b) -> None:
    """Two-window diff report (spec §7.2.2 Pattern D).

    ``cmd_diff`` aggregates two windows; emitting a single union-report
    would conflate per-window stats. This helper emits two separate
    reports labeled by window token, then sets ``_DEBUG_REPORT_EMITTED``
    so a downstream cmd_* composition doesn't double-emit.

    Bypasses ``_emit_debug_samples_if_set``'s one-time guard internally
    (it would short-circuit the second window).
    """
    c = _cctally()
    if c._DEBUG_REPORT_EMITTED:
        return
    if not getattr(args, "debug", False):
        return
    sample_limit = int(getattr(args, "debug_samples", 5))
    # Sync intent must match `_build_diff_result` (`skip_sync=not args.sync`).
    # Under `diff --sync --debug` the rendered diff reflects freshly-synced
    # JSONL while these debug stats, if they skipped sync, would be computed
    # from the STALE cache — misleading in precisely the stale-cache case
    # `--sync` exists to fix. So honor `--sync` here too: the first
    # `get_entries(skip_sync=False)` runs the delta ingest, and every
    # subsequent read (the second window here + `_build_diff_result`'s own
    # reads) is a cheap delta no-op (file size/offset unchanged) — no
    # redundant full walk. Without `--sync`, keep skipping (debug observes
    # the cache as-is).
    skip_sync = not bool(getattr(args, "sync", False))
    try:
        for window, label_letter, token in (
            (window_a, "A", getattr(args, "a", "")),
            (window_b, "B", getattr(args, "b", "")),
        ):
            try:
                # Reuse the SAME half-open window accessor the rendered diff
                # aggregation uses (`_diff_iter_claude_entries`): `ParsedWindow`
                # exposes `start_utc`/`end_utc` (NOT `.start`/`.end`), and its
                # `end_utc` is documented exclusive — the helper trims by 1 µs
                # before hitting the inclusive-end shared cache reader so the
                # debug report scopes to exactly the entries the rendered diff
                # counts. Reading via `get_entries(window.start, window.end)`
                # both raised AttributeError (wrong field names) and would have
                # over-counted the exclusive end boundary.
                entries = list(
                    dk._diff_iter_claude_entries(window, skip_sync=skip_sync)
                )
            except (sqlite3.DatabaseError, OSError) as exc:
                eprint(
                    f"cctally --debug: window {label_letter} report "
                    f"unavailable: {exc}"
                )
                continue
            # `_diff_iter_claude_entries` yields `_JoinedClaudeEntry`, which
            # has no `.usage` attribute; adapt to `UsageEntry` before the
            # mismatch compute, mirroring cmd_project / cmd_session. The stats
            # helper reads `entry.usage`, so passing raw joined entries here
            # crashed every priced entry with AttributeError — and the inner
            # `try/except` only catches DatabaseError/OSError, so the crash
            # escaped to main()'s generic handler (exit 1, zero diff output).
            stats = c._compute_pricing_mismatch_stats(
                c._usage_entry_from_joined(je) for je in entries
            )
            stats.command_label = f"diff (Window {label_letter}: {token})"
            for line in c._render_pricing_mismatch_report(stats, sample_limit):
                eprint(line)
    finally:
        # P1.2 (issue #89 review-loop): set the guard in finally so a
        # downstream cmd_* composition doesn't double-emit even if one
        # window raised — the partial output we did emit is enough.
        c._DEBUG_REPORT_EMITTED = True


def cmd_diff(args: argparse.Namespace) -> int:
    """Compare Claude usage between two windows."""
    c = _cctally()
    c._share_validate_args(args)
    now_utc = _command_as_of()
    if getattr(args, "debug_now", False):
        print(f"now_utc={c._iso_z(now_utc)}")
        return 0

    # The source-aware all-provider path resolves calendar week tokens once
    # before dispatching either provider.  Reuse those absolute, half-open
    # windows for Claude instead of attempting to reinterpret the original
    # tokens against its subscription-week anchor.  Ordinary Claude invocations
    # keep the established anchor/parser path byte-for-byte.
    supplied_windows = getattr(args, "_source_analytics_windows", None)
    if supplied_windows is None:
        # Resolve anchors (None when no snapshots exist; week tokens then
        # raise NoAnchorError in the parser).
        anchor_week_start, anchor_resets_at = dk._diff_resolve_anchor(now_utc)
    else:
        anchor_week_start, anchor_resets_at = None, None

    # Validation already happened via _argparse_tz; resolve now to a ZoneInfo
    # (or None for "local") and derive the IANA name for window resolution.
    config = c._load_claude_config_for_args(args)
    # Session A (spec §7.2): bridge -z/--timezone into args.tz so the
    # existing resolve_display_tz precedence absorbs the new alias.
    c._bridge_z_into_tz(args, config)
    tz_obj = c.resolve_display_tz(args, config)
    args._resolved_tz = tz_obj
    tz_name = (tz_obj.key if tz_obj is not None else c._local_tz_name())

    try:
        if supplied_windows is not None:
            def _from_source_window(window):
                start = window.start_at
                end = window.end_at
                return dk.ParsedWindow(
                    label=window.label,
                    start_utc=start,
                    end_utc=end,
                    length_days=(end - start).total_seconds() / 86400,
                    kind=window.kind,
                    week_aligned=False,
                    full_weeks_count=0,
                )

            window_a = _from_source_window(supplied_windows[0])
            window_b = _from_source_window(supplied_windows[1])
        else:
            window_a = dk._parse_diff_window(
                args.a, now_utc=now_utc,
                anchor_resets_at=anchor_resets_at,
                anchor_week_start=anchor_week_start,
                tz_name=tz_name,
            )
            window_b = dk._parse_diff_window(
                args.b, now_utc=now_utc,
                anchor_resets_at=anchor_resets_at,
                anchor_week_start=anchor_week_start,
                tz_name=tz_name,
            )
    except dk.NoAnchorError as exc:
        print(f"diff: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"diff: {exc}", file=sys.stderr)
        return 2

    # Validate the remaining CLI surface (`--only` / `--with`) BEFORE the
    # `--debug` emission below. `_emit_diff_debug_samples` prints reports and,
    # under `--sync`, runs a cache ingest (a local-state mutation). A
    # fail-fast usage error like `diff ... --only bogus --debug --sync` must
    # not print unrelated debug output or touch the cache before returning
    # exit 2 — so the validation gate has to precede the debug scan.
    sections_requested = ["overall", "models", "projects", "cache"]
    if args.only is not None:
        sections_requested = [s.strip() for s in args.only.split(",") if s.strip()]
        SUPPORTED_SECTIONS = {"overall", "models", "projects", "cache"}
        if not sections_requested:
            eprint(
                "diff: --only specified no sections. "
                f"Supported: {', '.join(sorted(SUPPORTED_SECTIONS))}"
            )
            return 2
        unknown = [s for s in sections_requested if s not in SUPPORTED_SECTIONS]
        if unknown:
            eprint(
                f"diff: --only contains unknown section(s): {', '.join(unknown)}. "
                f"Supported: {', '.join(sorted(SUPPORTED_SECTIONS))}"
            )
            return 2
    if args.with_extra:
        for extra in (s.strip() for s in args.with_extra.split(",")):
            if extra in ("trend", "time"):
                print(
                    f"diff: --with {extra} is not yet implemented (deferred to v1.1)",
                    file=sys.stderr,
                )
                return 1

    # Issue #89 spec §7.2.2 Pattern D: emit one --debug report per window
    # before any rendering, with window-A then window-B labels. Runs only
    # after the validation gate above so a usage error fails fast without
    # debug output or a cache sync.
    if getattr(args, "debug", False):
        _emit_diff_debug_samples(args, window_a, window_b)

    threshold = dk.NoiseThreshold(
        show_all=bool(args.show_all),
        user_override=(args.min_delta_usd is not None
                       or args.min_delta_pct is not None),
    )
    if args.min_delta_usd is not None:
        threshold = dataclasses.replace(threshold, min_delta_usd=args.min_delta_usd)
    if args.min_delta_pct is not None:
        threshold = dataclasses.replace(threshold, min_delta_pct=args.min_delta_pct)

    try:
        result = dk._build_diff_result(
            window_a, window_b,
            threshold=threshold,
            sections_requested=sections_requested,
            sort=args.sort,
            allow_mismatch=bool(args.allow_mismatch),
            skip_sync=not bool(args.sync),
            top=args.top,
        )
    except dk.WindowMismatchError as exc:
        print(f"diff: {exc}", file=sys.stderr)
        return 2

    dk._check_diff_invariants(result)

    options = {
        "allow_mismatch": bool(args.allow_mismatch),
        "show_all": bool(args.show_all),
        "min_delta_usd": threshold.min_delta_usd,
        "min_delta_pct": threshold.min_delta_pct,
        "user_override_threshold": threshold.user_override,
        "sort": args.sort,
        "top": args.top,
        "sections_requested": sections_requested,
        "sync_run": bool(args.sync),
    }

    if args.emit_json:
        payload = dk._diff_to_json_payload(result, options=options)
        sink = getattr(args, "_source_result_sink", None)
        if sink is not None:
            sink(payload)
        else:
            print(json.dumps(payload, indent=2))
        return 0

    # Session A (spec §7.3): route through the new color resolver so
    # the bool --color flag overrides NO_COLOR env, --no-color overrides
    # FORCE_COLOR env, and deny-wins on the --color + --no-color clash.
    # The old computation (sys.stdout.isatty() and not args.no_color)
    # only honored --no-color + isatty; the resolver supersedes both
    # with the full spec §7.3 precedence.
    color = c._resolve_color_enabled(args)
    width = args.width or shutil.get_terminal_size().columns
    width = max(80, min(width, 160))
    print(dk._diff_render_full_output(
        result, color=color, width=width, raw_aggregates=result.raw_totals,
        tz=tz_obj, compact=args.compact,
    ))
    return 0
