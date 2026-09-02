"""Pure-function render kernel for ``cctally statusline``.

No I/O — every side-effecting dependency is dataclass-injected (cache.db
query fns, HWM-clamp fn, transcript-reader fn, ``now``). Keeps unit tests
injection-driven and golden tests reproducible.

See docs/superpowers/specs/2026-05-28-issue-86-session-g-statusline-design.md
for the full design.
"""
from __future__ import annotations

import json
import pathlib
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional


def _load_lib(name: str):
    """Late-import a sibling under ``bin/`` (the ``_lib_view_models`` recipe).

    Late rather than top-level, so this KERNEL carries no import-time edge to
    the quota modules and stays importable on its own.

    WHAT THAT DOES NOT BUY, corrected after the #661 S2 review. An earlier
    revision of this docstring claimed that a status line on an install which
    has never fitted a calibration "loads none of them". At runtime it loads
    all of them. ``_seven_day_projection`` imports ``_lib_forecast``
    unconditionally, and ``_lib_forecast`` imports ``_lib_quota_model`` at
    module scope, so the quota kernel arrives with the projection whether or
    not a calibration exists. ``_rate_change_marker`` then calls
    ``inj.quota_regimes()`` whenever any 5h or 7d reading exists, and that
    port imports ``_cctally_quota_model``; a withheld figure imports
    ``_lib_quota_copy``.

    MEASURED here, as marginal cost in ONE process in the order the status
    line pays it, three runs on an Apple M4 Max under CPython 3.13:
    ``_cctally_core`` 13.1-13.4ms, then ``_lib_forecast`` 12.1-13.5ms,
    ``_lib_quota_model`` 0.0ms because ``_lib_forecast`` already pulled it,
    ``_cctally_quota_model`` 1.5-1.8ms, and ``_lib_quota_copy`` 0.1-0.3ms.
    So about 14ms per prompt beyond ``_cctally_core``. Those durations are
    this machine's; the STRUCTURAL fact — four modules, of which the largest
    is on the unconditional projection path — is not.

    That is an unstated cost rather than a violated constraint. Spec §9's
    rule is about ``cache.db`` and ``analyse``, and neither is touched; see
    ``tests/test_statusline_quota.py``, which makes both explode rather than
    reading the source for their names. Gating the quota port behind a cheap
    ``Path.exists()`` on the calibration file was measured and NOT taken: it
    would remove ``_cctally_quota_model`` alone, 1.5ms of the 14, because
    ``_lib_forecast`` and the kernel it pulls are already loaded by then, and
    it would add a stat(2) to every prompt on an install that does have a
    calibration.
    """
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
    usage_only: bool
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
    # #661 S2 §9. The stored metering regimes for this install, oldest
    # first, as plain dicts — a dedicated FILESYSTEM port and deliberately
    # NOT the DB port beside it. The read takes no flock and no throttle:
    # `save_calibrations` writes through `os.replace` in the same directory,
    # so a lock-free reader always sees a complete old or new inode, and a
    # flock here would let a writer stall a prompt. It returns `()` for
    # every unusable state — absent, malformed, version-ahead, quarantined
    # — because the status line cannot distinguish them without scanning
    # quarantine sidecars and renders the same thing for all of them: no
    # marker. The default supplies no regimes, so a caller that has not
    # wired the port renders exactly what it rendered before.
    quota_regimes: Callable[[], tuple] = staticmethod(lambda: ())


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


#: The 7d meter's window length. The projection measures a rate over the
#: elapsed part of THIS window, so it needs the window's span, and the reset
#: epoch alone gives only its end.
_SEVEN_DAY_HOURS = 7 * 24.0

#: The marker §6.6's predicate raises. Short because it shares a line with
#: four other segments, and literal because a bare glyph would have to be
#: looked up: the word says the rate changed and `cctally quota` says how.
_RATE_CHANGE_MARKER = "Δrate"

#: How much of the window must have elapsed before a pace projection is
#: rendered here. NOT a new threshold: it is
#: `ForecastConfidenceCause.ELAPSED_HOURS`, the estate's existing rule for
#: when a week-pace projection is low-confidence, restated as a number
#: because the status line has no room for the `LOW CONF` qualifier
#: `forecast` prints beside one. Without it the slot publishes arithmetic
#: that is correct and useless: a 42% reading ten hours into a week
#: projects to 697%, and a per-prompt line is the worst place to put a
#: figure that moves by hundreds of points between prompts.
_PROJECTION_MIN_ELAPSED_HOURS = 24.0

@dataclass(frozen=True)
class _ProjectionInputs:
    """The duck-typed operand `select_projection_basis` reads.

    The selector is shared with `forecast` and the dashboard and takes
    whatever object carries these attributes, so the status line builds the
    smallest one that answers it rather than reaching for a forecast.

    `calibrated_projection_pct` is deliberately absent. Producing it needs a
    whole-week `session_entries` scan, and spec §9 forbids this surface from
    touching `cache.db` for quota at all, so the basis on this surface is
    always the corrected meter. The selector is still asked rather than
    bypassed, so the token the line prints names the basis it actually used
    and would name a different one the day the basis becomes cheap.
    """
    p_now: "float | None"
    p_now_corrected: "float | None"
    right_censored: bool
    elapsed_hours: "float | None"
    remaining_hours: "float | None"


def _seven_day_projection(seven_pct, seven_resets, now_epoch) -> Optional[str]:
    """The `→ …` token for the 7d slot, or None when there is none.

    None — rather than a withheld token — in the three cases where there is
    nothing to say rather than a typed reason not to say it: the reset epoch
    is unknown, so there is no window to project over; the reset instant has
    already passed, so the window it named has closed; or too little of the
    window has elapsed for a pace to mean anything. Otherwise the slot
    carries a value with its basis, or the withholding cause in the short
    register.
    """
    if seven_pct is None or seven_resets is None:
        return None
    if seven_resets <= now_epoch:
        # A CLOSED window. The clamp below would drive `remaining` to 0 and
        # `elapsed` to the whole 168 hours, so the projection would equal the
        # corrected reading and the line would print a projected end-of-week
        # percent for a week that has already ended. It is reachable from the
        # DB-latest fallback row and from a long-idle machine, and it is a
        # missing window rather than a statement about the reading — so it is
        # withheld the way an unknown reset is, above the censoring branch
        # and above the elapsed gate.
        return None
    fc = _load_lib("_lib_forecast")
    remaining = (seven_resets - now_epoch) / 3600.0
    remaining = max(0.0, min(_SEVEN_DAY_HOURS, remaining))
    elapsed = _SEVEN_DAY_HOURS - remaining
    shown = float(seven_pct)
    corrected = fc.corrected_percent_point(shown)
    selected = fc.select_projection_basis(_ProjectionInputs(
        p_now=shown,
        p_now_corrected=corrected,
        # Censoring is read off the correction rather than re-tested here.
        # `corrected_percent_point` returns None exactly when the reading is
        # right-censored — a displayed 100 denotes `[99, +inf)` and has no
        # finite point estimate — and a second spelling of that test would
        # be a second place to disagree with it.
        right_censored=corrected is None,
        elapsed_hours=elapsed,
        remaining_hours=remaining,
    ))
    copy = _load_lib("_lib_quota_copy")
    if selected.basis is fc.ProjectionBasis.WITHHELD:
        # A withholding is a statement about the READING and does not
        # depend on how much of the window has elapsed, so it is rendered
        # before the confidence gate below. A right-censored 100 is the
        # case: it has no point estimate at any point in the week.
        return f"→ {copy.short_form(selected.code)}"
    if elapsed < _PROJECTION_MIN_ELAPSED_HOURS:
        # Too early in the window for a pace to mean anything. The slot
        # keeps the shape it has always had rather than printing a token,
        # for the same reason an unknown reset epoch does: there is nothing
        # to say, and a per-prompt line must not fill with non-statements.
        return None
    basis = copy.basis_presentation(selected.basis.value).get(
        "short", selected.basis.value)
    return f"→ {int(round(selected.value))}% {basis}"


def _rate_change_marker(inj: StatuslineInjections) -> Optional[str]:
    """`Δrate` while §6.6's predicate holds, else None.

    The predicate is DERIVED — the active open regime has a confirmed
    predecessor — so the marker clears on its own when that regime closes
    and needs no durable state, which §1 forbids this session from adding.
    """
    try:
        regimes = list(inj.quota_regimes() or ())
    except Exception:                                  # noqa: BLE001
        # A port that raises must not take the prompt down with it. The
        # marker is information; the status line is the shell's.
        return None
    if not regimes:
        return None
    mrc = _load_lib("_lib_meter_rate_change")
    return _RATE_CHANGE_MARKER if mrc.active_rate_change(regimes) else None


def resolve_cctally_extensions(
    inp: StatuslineInput,
    now: datetime,
    inj: StatuslineInjections,
    *,
    include_countdowns: bool = True,
    include_projection: bool = True,
) -> Optional[str]:
    """Segment 5 — cctally-only `5h X% (...) · 7d Y% (...) → Z% meter`.

    Source priority chain (spec §3.5):
        1. stdin rate_limits (preferred — freshest)
        2. DB latest weekly_usage_snapshots row (if stdin EMPTY)
        3. HWM monotonic clamp (within window only)
        4. If all empty → return None (segment 5 suppressed)

    #661 S2 §9: the 7d slot also carries the projected end-of-week percent
    with the basis it came from, and a `Δrate` marker while §6.6's predicate
    holds. `include_projection` is False for `--usage-only`, which is the
    compact two-reading form other tools embed and keeps its shape.
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
        if include_countdowns and five_resets is not None:
            s += f" ({_fmt_countdown(five_resets - now_epoch)})"
        parts.append(s)
    if seven_pct is not None:
        s = f"7d {int(round(seven_pct))}%"
        if include_countdowns and seven_resets is not None:
            s += f" ({_fmt_countdown(seven_resets - now_epoch)})"
        if include_projection:
            projection = _seven_day_projection(
                seven_pct, seven_resets, now_epoch)
            if projection is not None:
                s += f" {projection}"
        parts.append(s)
    if include_projection:
        marker = _rate_change_marker(inj)
        if marker is not None:
            parts.append(marker)
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


def _colorize_usage_segment(ext: str, args: StatuslineArgs) -> str:
    """Color a 5h/7d usage segment using the cctally percent bands."""
    nums = [int(x) for x in _PERCENT_INT_RE.findall(ext)]
    mx = max(nums) if nums else 0
    if mx < 60:
        color = "green"
    elif mx < 85:
        color = "yellow"
    else:
        color = "red"
    return _wrap_color(ext, color, args.color)


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
    if args.usage_only:
        ext = resolve_cctally_extensions(
            inp, now, inj, include_countdowns=False, include_projection=False
        )
        return "" if ext is None else _colorize_usage_segment(ext, args)

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
            seg5 = _colorize_usage_segment(ext, args)

    segs = [seg1, seg2, seg3, seg4]
    if seg5 is not None:
        segs.append(seg5)
    return " | ".join(segs)
