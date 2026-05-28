"""Pure-function render kernel for ``cctally statusline``.

No I/O — every side-effecting dependency is dataclass-injected (cache.db
query fns, HWM-clamp fn, transcript-reader fn, ``now``). Keeps unit tests
injection-driven and golden tests reproducible.

See docs/superpowers/specs/2026-05-28-issue-86-session-g-statusline-design.md
for the full design.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional


# ---- Stdin payload (subset we care about) ----------------------------------


@dataclass(frozen=True)
class StatuslineInput:
    """Parsed Claude Code hook stdin. Every field is optional — see §3.1 of
    the spec for the graceful-degradation contract. The two exit-1 paths
    (parse failure, non-object root) are handled BEFORE this dataclass is
    constructed; if you have a ``StatuslineInput`` instance, the payload
    was at least a valid JSON object.
    """
    session_id: Optional[str] = None
    model_id: Optional[str] = None
    model_display_name: Optional[str] = None
    transcript_path: Optional[str] = None
    workspace_current_dir: Optional[str] = None
    cost_total_usd: Optional[float] = None
    rate_limits_5h_pct: Optional[float] = None
    rate_limits_5h_resets_at: Optional[int] = None  # unix epoch
    rate_limits_7d_pct: Optional[float] = None
    rate_limits_7d_resets_at: Optional[int] = None  # unix epoch
    raw: dict = field(default_factory=dict)  # full parsed JSON for diagnostics


# ---- CLI args (post-config-resolution) -------------------------------------


@dataclass(frozen=True)
class StatuslineArgs:
    """Effective configuration AFTER CLI > config.json > built-in default
    precedence has been resolved by the I/O layer. The kernel sees a fully
    resolved view.
    """
    visual_burn_rate: str  # "off" | "emoji" | "text" | "emoji-text"
    cost_source: str  # "auto" | "cctally" | "cc" | "both"
    context_low_threshold: int
    context_medium_threshold: int
    cctally_extensions: bool
    color: bool  # ANSI on/off after auto-detect resolved
    display_tz_name: str  # IANA name; resolved upstream via
                          # get_display_tz_pref(cfg) — defaults to
                          # DISPLAY_TZ_DEFAULT ("local") when no config
                          # nor CLI override, then converted to a real
                          # IANA via _local_tz_name() before reaching
                          # the kernel.
    debug: bool


# ---- Injection-ports (no defaults — every field MUST be supplied) ----------


@dataclass(frozen=True)
class StatuslineInjections:
    """Side-effecting callables. Unit tests pass simple lambdas; the I/O
    layer in ``cmd_statusline`` passes DB- and filesystem-backed
    implementations.
    """
    # Sum of session_entries.cost WHERE session_id = ? (merged-resumed).
    # Returns None if session_id unknown or cache miss.
    cctally_session_cost: Callable[[Optional[str]], Optional[float]]
    # Sum of session_entries.cost WHERE date(timestamp, tz) == today.
    today_cost: Callable[[str, datetime], float]
    # Active 5h block: returns (cost_usd, time_remaining_seconds, elapsed_seconds)
    # or None if no active block.
    active_block: Callable[[datetime], "Optional[tuple[float, int, int]]"]
    # Returns (5h_hwm_pct, 7d_hwm_pct), both may be None.
    hwm_clamp: Callable[
        [Optional[int], Optional[int]],  # five_resets, seven_resets epochs
        "tuple[Optional[float], Optional[float]]",
    ]
    # Latest weekly_usage_snapshots row as (five_pct, five_resets, seven_pct, seven_resets)
    # or None.
    db_latest_rate_limits: Callable[
        [],
        "Optional[tuple[Optional[float], Optional[int], Optional[float], Optional[int]]]",
    ]
    # transcript_path → context % (0.0..100.0) or None if unreadable/unknown.
    context_pct: Callable[[Optional[str], Optional[str]], Optional[float]]
    # Emits one-shot stderr warnings (deduped by message — caller maintains set).
    warn_once: Callable[[str], None]


# ---- ParseError sentinel ---------------------------------------------------


@dataclass(frozen=True)
class ParseError:
    """Returned by parse_statusline_stdin on JSON parse failure or
    non-object root. The I/O layer maps this to exit 1 with a stderr
    message and empty stdout.
    """
    message: str


def parse_statusline_stdin(raw: "bytes | str") -> "StatuslineInput | ParseError":
    """Parse the Claude Code hook stdin payload.

    Returns ``StatuslineInput`` on success (every field optional), or
    ``ParseError`` if stdin is not parseable JSON OR not an object root.
    Field-level absences are NOT errors — they degrade gracefully per
    spec §3.1.
    """
    try:
        text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
        parsed = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return ParseError(f"invalid JSON: {exc}")
    if not isinstance(parsed, dict):
        typ = type(parsed).__name__
        return ParseError(f"expected JSON object, got {typ}")

    def _get(d, *path):
        cur = d
        for k in path:
            if not isinstance(cur, dict):
                return None
            cur = cur.get(k)
        return cur

    def _to_epoch(v) -> Optional[int]:
        if v is None:
            return None
        if isinstance(v, bool):  # bool is an int subclass — exclude
            return None
        if isinstance(v, (int, float)):
            return int(v)
        if isinstance(v, str):
            try:
                # iso8601 with Z or offset
                s = v.replace("Z", "+00:00")
                dt_obj = datetime.fromisoformat(s)
                if dt_obj.tzinfo is None:
                    dt_obj = dt_obj.replace(tzinfo=timezone.utc)
                return int(dt_obj.timestamp())
            except ValueError:
                return None
        return None

    def _to_float(v) -> Optional[float]:
        if v is None:
            return None
        if isinstance(v, bool):
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def _to_str(v) -> Optional[str]:
        return v if isinstance(v, str) and v else None

    return StatuslineInput(
        session_id=_to_str(parsed.get("session_id")),
        model_id=_to_str(_get(parsed, "model", "id")),
        model_display_name=_to_str(_get(parsed, "model", "display_name")),
        transcript_path=_to_str(parsed.get("transcript_path")),
        workspace_current_dir=_to_str(_get(parsed, "workspace", "current_dir")),
        cost_total_usd=_to_float(_get(parsed, "cost", "total_cost_usd")),
        rate_limits_5h_pct=_to_float(
            _get(parsed, "rate_limits", "five_hour", "used_percentage")
        ),
        rate_limits_5h_resets_at=_to_epoch(
            _get(parsed, "rate_limits", "five_hour", "resets_at")
        ),
        rate_limits_7d_pct=_to_float(
            _get(parsed, "rate_limits", "seven_day", "used_percentage")
        ),
        rate_limits_7d_resets_at=_to_epoch(
            _get(parsed, "rate_limits", "seven_day", "resets_at")
        ),
        raw=parsed,
    )


# ---- Segment 1: model -----------------------------------------------------


def resolve_model_segment(inp: StatuslineInput) -> str:
    """Segment 1: `🤖 <model>`. display_name > id > 'Unknown model'."""
    name = inp.model_display_name or inp.model_id or "Unknown model"
    return f"🤖 {name}"


# ---- Segment 2 components -------------------------------------------------


def _fmt_usd(v: float) -> str:
    return f"${v:.2f}"


def resolve_session_cost(
    inp: StatuslineInput,
    cost_source: str,
    inj: StatuslineInjections,
) -> str:
    """Segment 2 prefix — the `session` slot.

    `cctally`/`auto` (when transcript+session_id available and cache hit):
        sum session_entries WHERE session_id = ?
    `auto` falls through to `cc` when:
        - session_id absent, OR
        - transcript_path absent, OR
        - cache miss (cctally_session_cost returns None)
    `cc`: stdin cost.total_cost_usd (absent → $0.00)
    `both`: side-by-side `($X cc / $Y cctally) session`
    """
    def _cctally_usable() -> Optional[float]:
        # We require BOTH session_id (for the cache lookup key) AND
        # transcript_path (proxy for "we trust the local cache" — its
        # presence means CC believes a local transcript exists, so the
        # session-entry cache should have ingested it). Future readers:
        # don't drop the transcript guard without re-thinking that
        # invariant.
        if not inp.session_id or not inp.transcript_path:
            return None
        return inj.cctally_session_cost(inp.session_id)

    cc = float(inp.cost_total_usd) if inp.cost_total_usd is not None else 0.0

    if cost_source == "cctally":
        v = _cctally_usable()
        return f"{_fmt_usd(v if v is not None else 0.0)} session"
    if cost_source == "cc":
        return f"{_fmt_usd(cc)} session"
    if cost_source == "both":
        cct = _cctally_usable()
        cct_val = cct if cct is not None else 0.0
        return f"({_fmt_usd(cc)} cc / {_fmt_usd(cct_val)} cctally) session"
    # auto (and any other value falls into auto behavior)
    cct = _cctally_usable()
    if cct is not None:
        return f"{_fmt_usd(cct)} session"
    return f"{_fmt_usd(cc)} session"


def resolve_today_cost(
    inp: StatuslineInput,
    display_tz_name: str,
    now: datetime,
    inj: StatuslineInjections,
) -> str:
    """Segment 2 middle slot — `today`. Always cctally-source."""
    cost = inj.today_cost(display_tz_name, now)
    return f"{_fmt_usd(cost)} today"


def _fmt_block_remaining(seconds: int) -> str:
    s = max(seconds, 0)
    h = s // 3600
    m = (s % 3600) // 60
    return f"{h}h {m}m left"


def resolve_block_segment(
    inp: StatuslineInput,
    now: datetime,
    inj: StatuslineInjections,
) -> "tuple[str, tuple[float, int]]":
    """Segment 2 tail slot — `block (Xh Ym left)`.

    Returns the formatted segment string AND a tuple
    ``(block_cost, elapsed_seconds)`` for the downstream burn-rate
    resolver.
    """
    blk = inj.active_block(now)
    if blk is None:
        # No active block — clamp to 5h0m left, $0.00.
        return ("$0.00 block (5h 0m left)", (0.0, 1))
    cost, remaining_s, elapsed_s = blk
    seg = f"{_fmt_usd(cost)} block ({_fmt_block_remaining(remaining_s)})"
    return (seg, (cost, max(elapsed_s, 1)))


# ---- Segment 3: burn rate -------------------------------------------------


# Burn rate bands (mirrors ccusage at the time of writing). A future
# bump-to-match-ccusage PR is a one-tuple edit.
STATUSLINE_BURN_RATE_BANDS = (
    # (upper_bound_exclusive_usd_per_hr, emoji, text)
    (15.00, "🟢", "Normal"),
    (30.00, "🟡", "Moderate"),
    (float("inf"), "🔴", "High"),
)


def resolve_burn_rate(
    block_cost: float,
    elapsed_seconds: int,
    visual: str,
    color: bool,  # color injection deferred to render_statusline; passthrough here
) -> str:
    """Segment 3 — `🔥 $X.XX/hr [visual]`.

    ``visual`` ∈ {off, emoji, text, emoji-text}.
    """
    rate = block_cost / max(elapsed_seconds, 1) * 3600.0
    base = f"🔥 {_fmt_usd(rate)}/hr"
    if visual == "off":
        return base
    # Find band.
    emoji = text = ""
    for upper, e, t in STATUSLINE_BURN_RATE_BANDS:
        if rate < upper:
            emoji, text = e, t
            break
    if visual == "emoji":
        return f"{base} {emoji}"
    if visual == "text":
        return f"{base} ({text})"
    # emoji-text
    return f"{base} {emoji} ({text})"


# ---- Segment 4: context % -------------------------------------------------


def resolve_context_pct(
    inp: StatuslineInput,
    args: StatuslineArgs,
    inj: StatuslineInjections,
) -> str:
    """Segment 4 — `🧠 X%` or `🧠 N/A`.

    Color band selection is the render kernel's job, not this resolver —
    this function only returns the plain `🧠 X%` form. ``render_statusline``
    wraps the result in ANSI color codes per ``args.color``.
    """
    pct = inj.context_pct(inp.transcript_path, inp.model_id)
    if pct is None:
        return "🧠 N/A"
    return f"🧠 {int(round(pct))}%"


# ---- Segment 5: cctally extensions ----------------------------------------


def _fmt_countdown(seconds: int) -> str:
    """Human-friendly countdown — same shape as the user's bash
    statusline-command.sh: `Xd Yh`, `Xh Ym`, or `Xm`.
    """
    s = max(seconds, 0)
    days = s // 86400
    hours = (s % 86400) // 3600
    minutes = (s % 3600) // 60
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def resolve_cctally_extensions(
    inp: StatuslineInput,
    now: datetime,
    inj: StatuslineInjections,
) -> Optional[str]:
    """Segment 5 — cctally-only `5h X% (...) · 7d Y% (...)`.

    Source priority chain (spec §3.5):
        1. stdin rate_limits (preferred — freshest)
        2. DB latest weekly_usage_snapshots row (if stdin EMPTY)
        3. HWM monotonic clamp (within window only)
        4. If all empty → return None (segment 5 suppressed)
    """
    five_pct = inp.rate_limits_5h_pct
    five_resets = inp.rate_limits_5h_resets_at
    seven_pct = inp.rate_limits_7d_pct
    seven_resets = inp.rate_limits_7d_resets_at

    # If stdin entirely empty, try DB fallback.
    stdin_empty = (
        five_pct is None and five_resets is None
        and seven_pct is None and seven_resets is None
    )
    if stdin_empty:
        db = inj.db_latest_rate_limits()
        if db is not None:
            five_pct, five_resets, seven_pct, seven_resets = db

    # HWM clamp — monotonic UP only.
    hwm_5h, hwm_7d = inj.hwm_clamp(five_resets, seven_resets)
    if five_pct is not None and hwm_5h is not None and hwm_5h > five_pct:
        five_pct = hwm_5h
    if seven_pct is not None and hwm_7d is not None and hwm_7d > seven_pct:
        seven_pct = hwm_7d

    # Suppress segment 5 if nothing to render.
    if five_pct is None and seven_pct is None:
        return None

    now_epoch = int(now.timestamp())
    parts = []
    if five_pct is not None:
        s = f"5h {int(round(five_pct))}%"
        if five_resets is not None:
            s += f" ({_fmt_countdown(five_resets - now_epoch)})"
        parts.append(s)
    if seven_pct is not None:
        s = f"7d {int(round(seven_pct))}%"
        if seven_resets is not None:
            s += f" ({_fmt_countdown(seven_resets - now_epoch)})"
        parts.append(s)
    return " · ".join(parts)


# ---- Top-level render -----------------------------------------------------


# ANSI color codes (only emitted when args.color is True).
_ANSI = {
    "green": "\033[32m",
    "yellow": "\033[33m",
    "red": "\033[31m",
    "reset": "\033[0m",
}


def _wrap_color(text: str, color: Optional[str], enable: bool) -> str:
    if not enable or color is None:
        return text
    return f"{_ANSI[color]}{text}{_ANSI['reset']}"


_PERCENT_INT_RE = re.compile(r"(\d+)%")


def render_statusline(
    inp: StatuslineInput,
    args: StatuslineArgs,
    inj: StatuslineInjections,
    now: datetime,
) -> str:
    """Top-level render chokepoint. Joins segments with ` | `; suppresses
    None segments (currently only segment 5). See spec §1 for the exact
    layout and §3 for the data flow.
    """
    seg1 = resolve_model_segment(inp)

    # Segment 2: 💰 ... session / ... today / ... block (Xh Ym left)
    session = resolve_session_cost(inp, args.cost_source, inj)
    today = resolve_today_cost(inp, args.display_tz_name, now, inj)
    block, burn_kwargs = resolve_block_segment(inp, now, inj)
    seg2 = f"💰 {session} / {today} / {block}"

    # Segment 3: 🔥 $X.XX/hr [visual]
    seg3 = resolve_burn_rate(
        burn_kwargs[0], burn_kwargs[1], args.visual_burn_rate, args.color
    )

    # Segment 4: 🧠 X% with color band
    pct_text = resolve_context_pct(inp, args, inj)
    if pct_text == "🧠 N/A":
        seg4 = pct_text
    else:
        m = _PERCENT_INT_RE.search(pct_text)
        n = int(m.group(1)) if m else 0
        if n < args.context_low_threshold:
            color = "green"
        elif n < args.context_medium_threshold:
            color = "yellow"
        else:
            color = "red"
        seg4 = _wrap_color(pct_text, color, args.color)

    # Segment 5: cctally extension (may be None)
    seg5 = None
    if args.cctally_extensions:
        ext = resolve_cctally_extensions(inp, now, inj)
        if ext is not None:
            # Color by the higher of (5h%, 7d%) using cctally bands:
            # <60 green, <85 yellow, >=85 red.
            nums = [int(x) for x in _PERCENT_INT_RE.findall(ext)]
            mx = max(nums) if nums else 0
            if mx < 60:
                color = "green"
            elif mx < 85:
                color = "yellow"
            else:
                color = "red"
            seg5 = _wrap_color(ext, color, args.color)

    segs = [seg1, seg2, seg3, seg4]
    if seg5 is not None:
        segs.append(seg5)
    return " | ".join(segs)
