"""`cctally statusline` command — one-line status summary for CC hooks.

Holds `cmd_statusline` + its resolvers (`_resolve_statusline_tz`,
`_resolve_context_window`, `_read_last_assistant_usage`,
`_build_statusline_injections`) AND the two per-model context-window
constant dicts (`CLAUDE_MODEL_CONTEXT_WINDOWS`,
`CLAUDE_MODEL_CONTEXT_WINDOW_DEFAULT_FAMILY`), co-located with their sole
consumer `_resolve_context_window`.

Honest *name* imports are KERNEL-ONLY (`_cctally_core`: `eprint`, plus the
kernel symbols `open_db` / `_command_as_of` per the kernel-extraction
invariant in `tests/test_kernel_extraction_invariants.py`). `_lib_statusline`
is the eagerly-preloaded library kernel (bin/cctally:416), imported
qualified and referenced as a module object (`_lib_statusline.render_statusline`,
`_lib_statusline.ParseError`, …). Every other sibling/kernel-homed symbol
is reached via the call-time `_cctally()` accessor so test monkeypatches
through `cctally`'s namespace are preserved (spec §3.2). The 5h seam —
`_load_recorded_five_hour_windows`, `_maybe_swap_active_block_to_canonical`
— lives in `_cctally_five_hour.py` and is reached via `c.<name>` (spec §3.3).

bin/cctally re-exports `cmd_statusline` (parser `c.cmd_statusline`) plus the
resolvers/dicts; `_resolve_statusline_tz` is retrieved by `tests/test_statusline.py`
off the `cctally` namespace.

Spec: docs/superpowers/specs/2026-05-30-extract-five-hour-statusline-cmd-design.md
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import sys
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import _lib_statusline
from _cctally_core import _command_as_of, eprint, open_db


def _cctally():
    """Resolve the current `cctally` module at call-time (spec §3.2)."""
    return sys.modules["cctally"]


# Per-model context window (used by `cctally statusline` segment 4).
# Keep in sync with Anthropic's docs:
#   https://docs.anthropic.com/en/docs/about-claude/models
# Unknown model id → segment renders `🧠 N/A` + one-shot stderr warn.
CLAUDE_MODEL_CONTEXT_WINDOWS = {
    # 1M-token variants (explicit IDs override the family default).
    "claude-opus-4-8[1m]": 1_000_000,
    "claude-opus-4-7[1m]": 1_000_000,
    "claude-sonnet-4-5[1m]": 1_000_000,
    # Default 200K for every other Sonnet/Opus/Haiku family member.
    # The resolver does a substring match on the family token if the
    # exact id is missing — see _resolve_context_window.
}

CLAUDE_MODEL_CONTEXT_WINDOW_DEFAULT_FAMILY = {
    # Substring (case-insensitive) → window. Order matters; first hit wins.
    "sonnet": 200_000,
    "opus":   200_000,
    "haiku":  200_000,
}


def _resolve_statusline_tz(cli_tz, cfg, warn_once):
    """Resolve the IANA tz_name for cmd_statusline using the same 3-rung
    precedence as every other reporting command:

        CLI ``--timezone`` > ``config.display.tz`` > DISPLAY_TZ_DEFAULT ("local")

    "local" is converted to a real IANA via ``_local_tz_name()`` before
    returning. Unknown IANA names emit a one-shot warning and fall back
    to ``"UTC"``. Returns a real IANA name (or ``"UTC"``) — never the
    literal sentinel ``"local"``.

    Prior to #86 G follow-up, this defaulted to ``"UTC"`` when no config
    was set, so ``today`` computed on the UTC calendar day while
    ``cctally daily`` (and every other reporting command) used the local
    day — UTC-offset users saw a multi-hour lag between statusline and
    daily. Regression: tests/test_statusline.py::TestTzResolution.
    """
    c = _cctally()
    tz_name = cli_tz
    if not tz_name:
        tz_name = c.get_display_tz_pref(cfg)
    if tz_name in ("local", "LOCAL"):
        try:
            tz_name = c._local_tz_name() or "UTC"
        except Exception:
            tz_name = "UTC"
    elif tz_name and tz_name.lower() == "utc":
        # Canonical "utc" (the value get_display_tz_pref / normalize_display_tz_value
        # emit) -> the portable IANA key "UTC". macOS's case-insensitive
        # filesystem resolves ZoneInfo("utc") to UTC, but Linux's case-sensitive
        # /usr/share/zoneinfo raises ZoneInfoNotFoundError, which would emit a
        # spurious "invalid timezone 'utc'" warning below. Mirrors
        # resolve_display_tz, which maps canonical "utc" -> ZoneInfo("Etc/UTC").
        tz_name = "UTC"
    try:
        ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, Exception):
        warn_once(
            f"cctally statusline: invalid timezone {tz_name!r}; using 'UTC'"
        )
        tz_name = "UTC"
    return tz_name


def cmd_statusline(args: argparse.Namespace) -> int:
    """`cctally statusline` — one-line status summary for CC hooks.

    See docs/superpowers/specs/2026-05-28-issue-86-session-g-statusline-design.md
    for the full design.

    Exit codes:
        0  success (every absent stdin field degrades gracefully)
        1  stdin is not parseable JSON OR root is not a JSON object
        2  argparse rejected a flag (e.g. --cost-source ccusage), OR
           --config PATH unreadable
    """
    c = _cctally()
    # NOTE: `--cost-source ccusage` is rejected at argparse-time by
    # `_CostSourceAction` in `_build_statusline_parser`; it exits 2 with
    # the rename hint before we get here, so no explicit re-check is
    # needed in this function.

    # Validate `--context-{low,medium}-threshold` BEFORE reading stdin
    # so a misconfigured invocation fails fast without consuming the
    # CC hook's stdin payload.
    low = args.context_low_threshold
    med = args.context_medium_threshold
    if not isinstance(low, int) or low < 0 or low > 100:
        eprint(
            "cctally statusline: --context-low-threshold must be in [0, 100]"
        )
        return 2
    if not isinstance(med, int) or med < 0 or med > 100:
        eprint(
            "cctally statusline: --context-medium-threshold must be in [0, 100]"
        )
        return 2
    if low >= med:
        eprint(
            "cctally statusline: --context-low-threshold must be < "
            "--context-medium-threshold"
        )
        return 2

    # Silently clamp `--refresh-interval` to [0, 600]. The flag is a
    # no-op alias for ccusage drop-in compat; users never observe the
    # effect, but the spec mandates the clamp for forward-compat (when
    # we promote it to a real flag, the clamped value should be the one
    # propagated downstream).
    try:
        args.refresh_interval = max(0, min(600, int(args.refresh_interval)))
    except (TypeError, ValueError):
        args.refresh_interval = 1

    # Read stdin once.
    raw = sys.stdin.buffer.read()
    parse_result = _lib_statusline.parse_statusline_stdin(raw)
    if isinstance(parse_result, _lib_statusline.ParseError):
        eprint(f"cctally statusline: {parse_result.message}")
        return 1
    inp = parse_result

    # Resolve effective config: CLI > config.json > built-in default.
    # `_load_claude_config_for_args` honors `--config PATH` (issue #88
    # plumbing); a missing/invalid PATH raises SystemExit(2) inside
    # `_load_config_from_explicit_path` so this call already enforces
    # exit-2 on a bad --config.
    cfg = c._load_claude_config_for_args(args)
    sl_cfg = (cfg.get("statusline") or {}) if isinstance(cfg, dict) else {}
    if not isinstance(sl_cfg, dict):
        sl_cfg = {}

    # Validate config values; on invalid, one-shot stderr warn + use default.
    _warned: set = set()

    def warn_once(msg: str) -> None:
        if msg in _warned:
            return
        _warned.add(msg)
        eprint(msg)

    def _resolve(cli_val, cfg_key, default):
        if cli_val is not None:
            return cli_val
        cv = sl_cfg.get(cfg_key)
        if cv is None:
            return default
        return cv

    vbr = _resolve(args.visual_burn_rate, "visual_burn_rate", "off")
    if vbr not in ("off", "emoji", "text", "emoji-text"):
        warn_once(
            f"cctally statusline: invalid statusline.visual_burn_rate={vbr!r}; "
            f"using 'off'"
        )
        vbr = "off"

    cs = _resolve(args.cost_source, "cost_source", "auto")
    if cs not in ("auto", "cctally", "cc", "both"):
        warn_once(
            f"cctally statusline: invalid statusline.cost_source={cs!r}; "
            f"using 'auto'"
        )
        cs = "auto"

    ext_on = _resolve(args.cctally_extensions, "cctally_extensions", True)
    if not isinstance(ext_on, bool):
        warn_once(
            f"cctally statusline: invalid statusline.cctally_extensions="
            f"{ext_on!r}; using True"
        )
        ext_on = True

    tz_name = _resolve_statusline_tz(getattr(args, "timezone", None), cfg, warn_once)

    # Color: explicit CLI > NO_COLOR env > TTY detect.
    if args.color is True or args.color is False:
        color = args.color
    else:
        color = (os.environ.get("NO_COLOR", "") == "") and sys.stdout.isatty()

    sargs = _lib_statusline.StatuslineArgs(
        visual_burn_rate=vbr,
        cost_source=cs,
        context_low_threshold=int(args.context_low_threshold),
        context_medium_threshold=int(args.context_medium_threshold),
        cctally_extensions=bool(ext_on),
        color=bool(color),
        display_tz_name=tz_name,
        debug=bool(args.debug),
    )

    # Build injections (DB + transcript file IO).
    inj = _build_statusline_injections(warn_once)

    # `_command_as_of()` honors the `CCTALLY_AS_OF` testing hook so the
    # golden harness can pin "now" for deterministic block-remaining and
    # 5h/7d countdown numbers. Falls back to wall-clock UTC otherwise.
    now = _command_as_of()
    try:
        line = _lib_statusline.render_statusline(inp, sargs, inj, now)
    except Exception as exc:  # pragma: no cover — defensive
        eprint(f"cctally statusline: render failed: {exc}")
        return 1
    print(line)
    return 0


def _resolve_context_window(model_id, warn_once) -> "int | None":
    """Look up ``model_id`` in ``CLAUDE_MODEL_CONTEXT_WINDOWS``; fall back
    to a family-substring match against
    ``CLAUDE_MODEL_CONTEXT_WINDOW_DEFAULT_FAMILY``. Unknown id → ``None`` +
    one-shot stderr warning.
    """
    if not model_id:
        return None
    if model_id in CLAUDE_MODEL_CONTEXT_WINDOWS:
        return CLAUDE_MODEL_CONTEXT_WINDOWS[model_id]
    mid_lower = model_id.lower()
    for family, window in CLAUDE_MODEL_CONTEXT_WINDOW_DEFAULT_FAMILY.items():
        if family in mid_lower:
            return window
    warn_once(
        f"cctally statusline: unknown model {model_id!r}; context % unavailable"
    )
    return None


def _read_last_assistant_usage(transcript_path):
    """Tail-walk the transcript JSONL backwards to the most recent
    ``type=assistant`` line carrying ``message.usage``. Returns the usage
    dict or ``None``.

    Reads in 64 KB chunks from the end so multi-MB transcripts don't
    block the hot statusline path with a full-file parse.
    """
    if not transcript_path:
        return None
    path = pathlib.Path(transcript_path)
    if not path.exists():
        return None
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            tail = b""
            chunk = 65536
            # Read backwards in chunks until we have at least one full line
            # and the tail starts with a newline (so the first line is whole).
            while size > 0 and tail.count(b"\n") < 2:
                read_at = max(0, size - chunk)
                fh.seek(read_at)
                tail = fh.read(size - read_at) + tail
                size = read_at
            lines = tail.split(b"\n")
    except OSError:
        return None
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("type") != "assistant":
            continue
        msg = obj.get("message") or {}
        usage = msg.get("usage") if isinstance(msg, dict) else None
        if isinstance(usage, dict):
            return usage
    return None


def _build_statusline_injections(warn_once):
    """Wire DB- and FS-backed implementations for the kernel's injection ports.

    See ``_lib_statusline.StatuslineInjections`` for the contract. All
    callables fast-fail to "no data" on any exception — statusline must
    NEVER block the Claude Code hook tick.
    """
    def _cctally_session_cost(sid):
        c = _cctally()
        if not sid:
            return None
        try:
            conn = c.open_cache_db()
        except Exception:
            return None
        try:
            # Walk all entries via session_files join; sum costs whose
            # session_id matches. Stays read-only — does NOT call
            # sync_cache (too heavy for the hot statusline path; the
            # record-usage + hook-tick paths keep the cache warm).
            sql = (
                "SELECT se.timestamp_utc, se.model, "
                "  se.input_tokens, se.output_tokens, "
                "  se.cache_create_tokens, se.cache_read_tokens, "
                "  se.cost_usd_raw "
                "FROM session_entries se "
                "LEFT JOIN session_files sf ON sf.path = se.source_path "
                "WHERE sf.session_id = ?"
            )
            rows = list(conn.execute(sql, (sid,)))
        except Exception:
            return None
        finally:
            try:
                conn.close()
            except Exception:
                pass
        if not rows:
            return None
        total = 0.0
        for r in rows:
            usage = {
                "input_tokens":                r[2] or 0,
                "output_tokens":               r[3] or 0,
                "cache_creation_input_tokens": r[4] or 0,
                "cache_read_input_tokens":     r[5] or 0,
            }
            # #181: cost is token-only (_calculate_entry_cost ignores `speed`),
            # so the statusline cost path no longer selects or json.loads the
            # usage_extra_json blob — output is byte-identical. r[6] is still
            # cost_usd_raw (the dropped column was the trailing slot).
            try:
                total += c._calculate_entry_cost(
                    r[1], usage, mode="auto", cost_usd=r[6],
                )
            except Exception:
                continue
        return total

    def _today_cost(tz_name, now):
        c = _cctally()
        try:
            tz = ZoneInfo(tz_name) if tz_name and tz_name != "UTC" else dt.timezone.utc
        except Exception:
            tz = dt.timezone.utc
        local_now = now.astimezone(tz)
        day_start_local = local_now.replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        day_end_local = day_start_local + dt.timedelta(days=1)
        range_start = day_start_local.astimezone(dt.timezone.utc)
        range_end = day_end_local.astimezone(dt.timezone.utc)
        # Two-filter pattern (UTC half-open + display-tz date check):
        # the UTC range fetches the candidate window cheaply via the
        # cache's indexed `timestamp` column; the display-tz date check
        # then trims any entries that fall outside today's local
        # calendar day (the UTC window straddles two local dates when
        # the display tz has any UTC offset, so the SQL range alone is
        # slightly wider than the local day).
        try:
            entries = c.get_entries(range_start, range_end, skip_sync=True)
        except Exception:
            return 0.0
        total = 0.0
        for e in entries:
            try:
                # Filter to today in display tz (second-pass trim).
                ts_local = e.timestamp.astimezone(tz)
                if ts_local.date() != local_now.date():
                    continue
                total += c._calculate_entry_cost(
                    e.model, e.usage, mode="auto", cost_usd=e.cost_usd,
                )
            except Exception:
                continue
        return total

    def _active_block(now):
        c = _cctally()
        try:
            # Look at last 24h — captures the full active 5h window.
            range_start = now - dt.timedelta(hours=24)
            entries = c.get_entries(range_start, now, skip_sync=True)
        except Exception:
            return None
        if not entries:
            return None
        try:
            recorded_windows, block_start_overrides, canonical_intervals = (
                c._load_recorded_five_hour_windows(
                    range_start - c.BLOCK_DURATION, now + c.BLOCK_DURATION,
                )
            )
        except Exception:
            recorded_windows, block_start_overrides, canonical_intervals = (
                [], {}, {},
            )
        try:
            blocks = c._group_entries_into_blocks(
                entries,
                mode="auto",
                recorded_windows=recorded_windows,
                block_start_overrides=block_start_overrides,
                canonical_intervals=canonical_intervals,
                now=now,
            )
        except Exception:
            return None
        for b in blocks:
            if not b.is_gap and b.is_active:
                remaining_s = int((b.end_time - now).total_seconds())
                elapsed_s = int((now - b.start_time).total_seconds())
                return (float(b.cost_usd or 0.0), remaining_s, elapsed_s)
        return None

    def _hwm_clamp(five_resets, seven_resets):
        c = _cctally()
        five_hwm = None
        seven_hwm = None
        try:
            conn = open_db()
        except Exception:
            return (None, None)
        try:
            if five_resets is not None:
                try:
                    key = c._canonical_5h_window_key(int(five_resets))
                    row = conn.execute(
                        "SELECT MAX(five_hour_percent) "
                        "FROM weekly_usage_snapshots "
                        "WHERE five_hour_window_key = ?",
                        (key,),
                    ).fetchone()
                    if row and row[0] is not None:
                        five_hwm = float(row[0])
                except Exception:
                    pass
            if seven_resets is not None:
                try:
                    # Seven-day window bounds from the resets_at epoch:
                    # week_end = reset; week_start = reset - 7 days. The
                    # date form is the snapshot lookup key (week_start_date
                    # is deliberately NOT re-anchored across a mid-week
                    # reset — see _apply_reset_events_to_subweeks).
                    week_end_dt = dt.datetime.fromtimestamp(
                        int(seven_resets), tz=dt.timezone.utc,
                    )
                    week_start_dt = week_end_dt - dt.timedelta(days=7)
                    week_start_date = week_start_dt.date().isoformat()
                    # Reset-aware floor. An Anthropic mid-week reset / in-
                    # place credit leaves the pre-reset peak snapshots in
                    # this SAME week_start_date bucket (the boundary the
                    # snapshots carry does not change). A naive bucket-wide
                    # MAX(weekly_percent) would clamp the post-reset value
                    # UP to that stale peak — the statusline would show the
                    # pre-reset 7d %. Mirror the CLI/dashboard segmentation
                    # (_apply_reset_events_to_subweeks: post-reset window
                    # start_ts := effective_reset_at_utc) by flooring the
                    # MAX to snapshots captured at/after the latest reset
                    # effective WITHIN this window.
                    #
                    # The floor is the LATEST in-week effective across BOTH
                    # `week_reset_events` (Anthropic resets / >=25pp auto-
                    # credits) and `weekly_credit_floors` (manual `record-
                    # credit` partial credits — record-credit M2, #209): a
                    # partial credit lowers the clamp floor WITHOUT re-
                    # anchoring the week, so without the credit-floor leg the
                    # statusline would re-clamp the post-credit 31% back UP to
                    # the stale pre-credit 46% peak. `_reset_aware_floor`
                    # unions both legs with `unixepoch()` ordering (mixed
                    # Z / +00:00 offset spellings; lexical MAX would misorder
                    # them — same rule as the 5h-block cross-reset flag).
                    floor_iso = c._reset_aware_floor(
                        conn, week_start_date,
                        week_start_dt.isoformat(), week_end_dt.isoformat(),
                    )
                    if floor_iso is not None:
                        row = conn.execute(
                            "SELECT MAX(weekly_percent) "
                            "FROM weekly_usage_snapshots "
                            "WHERE week_start_date = ? "
                            "  AND unixepoch(captured_at_utc) >= unixepoch(?)",
                            (week_start_date, floor_iso),
                        ).fetchone()
                    else:
                        row = conn.execute(
                            "SELECT MAX(weekly_percent) "
                            "FROM weekly_usage_snapshots "
                            "WHERE week_start_date = ?",
                            (week_start_date,),
                        ).fetchone()
                    if row and row[0] is not None:
                        seven_hwm = float(row[0])
                except Exception:
                    pass
        finally:
            try:
                conn.close()
            except Exception:
                pass
        return (five_hwm, seven_hwm)

    def _db_latest_rate_limits():
        try:
            conn = open_db()
        except Exception:
            return None
        try:
            # Prefer `week_end_at` (ISO timestamp; sub-day precision) over
            # `week_end_date` (date-only; UTC-midnight). Older snapshots
            # may have `week_end_at` NULL — fall back to the date column
            # in that case. See the neighbor query in `pick_week_selection`
            # (bin/cctally:3849) for the precedent.
            row = conn.execute(
                "SELECT five_hour_percent, five_hour_window_key, "
                "  weekly_percent, week_end_at, week_end_date "
                "FROM weekly_usage_snapshots "
                "ORDER BY captured_at_utc DESC, id DESC LIMIT 1"
            ).fetchone()
            if not row:
                return None
            five_pct = float(row[0]) if row[0] is not None else None
            five_resets = int(row[1]) if row[1] is not None else None
            seven_pct = float(row[2]) if row[2] is not None else None
            seven_resets = None
            week_end_at = row[3]
            week_end_date = row[4]
            if week_end_at:
                try:
                    # `datetime.fromisoformat` accepts the trailing `Z`
                    # only on Python 3.11+; normalize to `+00:00` so 3.10
                    # checkouts (and any odd Z-suffixed snapshot) parse.
                    raw_iso = str(week_end_at)
                    if raw_iso.endswith("Z"):
                        raw_iso = raw_iso[:-1] + "+00:00"
                    end_dt = dt.datetime.fromisoformat(raw_iso)
                    if end_dt.tzinfo is None:
                        end_dt = end_dt.replace(tzinfo=dt.timezone.utc)
                    seven_resets = int(end_dt.timestamp())
                except Exception:
                    seven_resets = None
            if seven_resets is None and week_end_date:
                try:
                    end_dt = dt.datetime.fromisoformat(str(week_end_date))
                    if end_dt.tzinfo is None:
                        end_dt = end_dt.replace(tzinfo=dt.timezone.utc)
                    # week_end_date is exclusive — that's the reset moment.
                    seven_resets = int(end_dt.timestamp())
                except Exception:
                    seven_resets = None
            return (five_pct, five_resets, seven_pct, seven_resets)
        except Exception:
            return None
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _context_pct(transcript_path, model_id):
        if not transcript_path or not model_id:
            return None
        window = _resolve_context_window(model_id, warn_once)
        if window is None:
            return None
        try:
            usage = _read_last_assistant_usage(transcript_path)
        except Exception:
            return None
        if not isinstance(usage, dict):
            return None
        try:
            ctx_tokens = (
                int(usage.get("input_tokens", 0) or 0)
                + int(usage.get("cache_read_input_tokens", 0) or 0)
                + int(usage.get("cache_creation_input_tokens", 0) or 0)
            )
        except (TypeError, ValueError):
            return None
        if window <= 0:
            return None
        return ctx_tokens / window * 100.0

    return _lib_statusline.StatuslineInjections(
        cctally_session_cost=_cctally_session_cost,
        today_cost=_today_cost,
        active_block=_active_block,
        hwm_clamp=_hwm_clamp,
        db_latest_rate_limits=_db_latest_rate_limits,
        context_pct=_context_pct,
        warn_once=warn_once,
    )


