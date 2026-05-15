"""Alert-payload constructors and notification text builders.

Pure-fn layer (no I/O at import time): holds the deterministic helpers that
shape an alert's structured payload (`_build_alert_payload_*`) and render
its (title, subtitle, body) triple (`_alert_text_*`), plus the AppleScript
string-escape used to embed the rendered triple in an `osascript` literal
(`_escape_applescript_string`).

The companion I/O surface — `_alerts_log_path`, `_dispatch_alert_notification`,
the alerts-config validators — stays in `bin/cctally` because it touches
the filesystem (mkdir + append-log), spawns subprocesses, and reads
`os.environ` for the integration-harness escape hatch.

Cross-sibling dependency: `_alert_text_five_hour` calls `format_display_dt`,
which lives in `_lib_display_tz`. We load it via a local `_load_lib` helper
(spec_from_file_location, same shape as `bin/cctally:_load_sibling`) so this
pure layer remains independent of `bin/`'s sys.path posture and free of any
back-import of `cctally`.

`bin/cctally` re-exports every public symbol below so internal call sites
and `SourceFileLoader`-based tests (e.g. `bin/cctally-alerts-dispatch-test`)
resolve unchanged. A private `_eprint` duplicates `bin/cctally:eprint` per
the split design's §5.3 contract.

Spec: docs/superpowers/specs/2026-05-13-bin-cctally-split-design.md
"""
from __future__ import annotations

import datetime as dt
import pathlib
import sys
from typing import Any

from zoneinfo import ZoneInfo


def _eprint(*args: Any) -> None:
    print(*args, file=sys.stderr)


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


_lib_display_tz = _load_lib("_lib_display_tz")
format_display_dt = _lib_display_tz.format_display_dt


def _alert_text_weekly(payload: dict, tz: "ZoneInfo | None") -> tuple[str, str, str]:
    """Build (title, subtitle, body) for a weekly threshold alert.

    Most datetime renders go through ``format_display_dt`` (chokepoint
    rule), but ``week_start_date`` is a CALENDAR DAY (``YYYY-MM-DD``),
    not a clock instant — tagging it as UTC-midnight and then routing
    through ``format_display_dt(..., tz)`` would shift the wall-clock
    day for non-UTC ``display.tz`` (e.g. "Sun, Apr 27" → "Sat, Apr 26"
    in ``America/Los_Angeles``). Render the date directly via
    ``dt.date.fromisoformat`` so the rendered weekday/day matches the
    calendar date the user thinks of as "this week".
    """
    threshold = int(payload["threshold"])
    title = f"cctally - Weekly usage {threshold}% reached"
    ctx = payload.get("context") or {}
    week_start_date = ctx.get("week_start_date")
    if week_start_date:
        # Calendar-day render: ``week_start_date`` is a date, not an
        # instant; bypass tz conversion to avoid the off-by-one shift
        # documented above. ``tz`` is accepted for signature parity
        # with peer alert builders and intentionally unused here.
        subtitle = "Week starting " + dt.date.fromisoformat(
            week_start_date
        ).strftime("%a, %b %d")
    else:
        subtitle = "Current week"
    cumulative = float(ctx.get("cumulative_cost_usd") or 0.0)
    dpp = ctx.get("dollars_per_percent")
    if dpp is not None:
        body = f"${cumulative:.2f} spent so far - ${float(dpp):.2f} per 1%"
    else:
        body = f"${cumulative:.2f} spent so far"
    return title, subtitle, body


def _alert_text_five_hour(payload: dict, tz: "ZoneInfo | None") -> tuple[str, str, str]:
    """Build (title, subtitle, body) for a 5h-block threshold alert.

    All datetime renders go through ``format_display_dt`` (chokepoint rule).
    """
    threshold = int(payload["threshold"])
    title = f"cctally - 5h-block usage {threshold}% reached"
    ctx = payload.get("context") or {}
    bsa_iso = ctx.get("block_start_at")
    if bsa_iso:
        bsa = dt.datetime.fromisoformat(str(bsa_iso).replace("Z", "+00:00"))
        subtitle = "Block started " + format_display_dt(
            bsa, tz, fmt="%H:%M", suffix=False
        )
    else:
        subtitle = "Current 5h block"
    cost = float(ctx.get("block_cost_usd") or 0.0)
    model = ctx.get("primary_model")
    if model:
        body = f"${cost:.2f} spent in this block - current model: {model}"
    else:
        body = f"${cost:.2f} spent in this block"
    return title, subtitle, body


def _escape_applescript_string(s: str) -> str:
    """Escape ``s`` for embedding inside an AppleScript double-quoted literal.

    Order matters: backslashes first (otherwise the inserted backslashes
    from the double-quote escape get re-escaped), then double quotes,
    then newlines/CRs collapse to spaces (AppleScript chokes on raw NL).
    """
    return (
        s.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", " ")
        .replace("\r", " ")
    )


def _build_alert_payload_weekly(
    *,
    threshold: int,
    crossed_at_utc: str,
    week_start_date: str,
    cumulative_cost_usd: float,
    dollars_per_percent: "float | None",
) -> dict:
    """Build the alert payload for a weekly threshold crossing.

    ``alerted_at`` mirrors ``crossed_at`` here because the production caller
    sets the DB ``alerted_at`` BEFORE invoking ``_dispatch_alert_notification``
    (set-then-dispatch invariant, spec §3.2). Consumers (envelope builders,
    test inspectors) read the ``alerted_at`` field as the authoritative
    "alert was attempted" timestamp.
    """
    return {
        "id": f"weekly:{week_start_date}:{threshold}",
        "axis": "weekly",
        "threshold": int(threshold),
        "crossed_at": crossed_at_utc,
        "alerted_at": crossed_at_utc,  # set-then-dispatch
        "context": {
            "week_start_date": week_start_date,
            "cumulative_cost_usd": float(cumulative_cost_usd),
            "dollars_per_percent": (
                float(dollars_per_percent) if dollars_per_percent is not None else None
            ),
        },
    }


def _build_alert_payload_five_hour(
    *,
    threshold: int,
    crossed_at_utc: str,
    five_hour_window_key: int,
    block_start_at: str,
    block_cost_usd: float,
    primary_model: "str | None",
) -> dict:
    """Build the alert payload for a 5h-block threshold crossing.

    See ``_build_alert_payload_weekly`` for the ``alerted_at == crossed_at``
    rationale. ``primary_model`` is the highest-cost model active in the
    block (resolved via ``_resolve_primary_model_for_block``); ``None`` when
    the rollup-children child table is empty (e.g., direct-JSONL-fallback
    path before lazy backfill).
    """
    return {
        "id": f"five_hour:{five_hour_window_key}:{threshold}",
        "axis": "five_hour",
        "threshold": int(threshold),
        "crossed_at": crossed_at_utc,
        "alerted_at": crossed_at_utc,  # set-then-dispatch
        "context": {
            "five_hour_window_key": int(five_hour_window_key),
            "block_start_at": block_start_at,
            "block_cost_usd": float(block_cost_usd),
            "primary_model": primary_model,
        },
    }
