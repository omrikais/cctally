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
import dataclasses
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
import pathlib
import re
import secrets
import sqlite3
import sys
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import _cctally_core
import _lib_statusline
import _lib_statusline_candidates as _candidates
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

    usage_only = _resolve(args.usage_only, "usage_only", False)
    if not isinstance(usage_only, bool):
        warn_once(
            f"cctally statusline: invalid statusline.usage_only="
            f"{usage_only!r}; using False"
        )
        usage_only = False

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
        usage_only=bool(usage_only),
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

    # Persist the CC-provided rate_limits as the PRIMARY automatic usage
    # writer (spec 2026-07-17-usage-statusline-fallback). This is a pure
    # side effect that runs AFTER the line is printed and is FULLY guarded:
    # persistence must NEVER break or slow rendering, so the whole call is
    # wrapped here (the feeder itself forks detached + swallows its own
    # errors, but this is belt-and-suspenders). Absence of rate_limits, a
    # lost persist lock, or the throttle window all degrade to a clean
    # no-op; the OAuth backfill covers the "no statusline feeding" case.
    try:
        _statusline_persist(inp)
    except Exception:
        pass
    return 0


# =========================================================================
# Statusline usage-persistence feeder (spec 2026-07-17)
# =========================================================================
#
# The statusline is the PRIMARY automatic writer of weekly/5h usage
# snapshots: it persists the CC-provided stdin `rate_limits` (which stay
# current as a side effect of inference, even while /api/oauth/usage is
# 429-banned) through the UNCHANGED cmd_record_usage kernel. Guards:
#   - a cross-process flock (STATUSLINE_PERSIST_LOCK_PATH) so a multi-
#     session render herd yields at most one snapshot per throttle window;
#   - a liveness throttle keyed off the observation marker (NOT snapshot
#     age — the kernel dedups unchanged percentages without refreshing
#     captured_at, so snapshot age keeps growing while the statusline is
#     actively feeding);
#   - a DETACHED child so the render stays fast (cmd_record_usage may sync
#     weekly cost, scan 5h totals, and evaluate budget axes).


def _try_acquire_persist_lock() -> "int | None":
    """Non-blocking acquire of the cross-process statusline persist lock.

    Returns the open fd on success; ``None`` when another render already
    holds it (EWOULDBLOCK) or the lock file can't be opened/locked — in
    which case the caller renders without forking."""
    try:
        _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)
        fd = os.open(
            _cctally_core.STATUSLINE_PERSIST_LOCK_PATH,
            os.O_WRONLY | os.O_CREAT, 0o644,
        )
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        try:
            os.close(fd)
        except OSError:
            pass
        return None
    return fd


def _release_persist_lock(fd: "int | None") -> None:
    """Release + close a persist-lock fd (best-effort)."""
    if fd is None or fd < 0:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass


class _AuthoritativeRecordResult:
    """Outcome of the selected-state authoritative writer protocol.

    This deliberately carries a small status surface rather than leaking a
    SQLite result.  A non-``ok`` result means the durable tombstone remains
    inflight, so callers must not touch selected freshness or clear OAuth
    backoff state.
    """

    def __init__(self, status: str, reason: str | None = None):
        self.status = status
        self.reason = reason


class _SelectedStateLock:
    """Blocking owner of the one selected-state writer critical section."""

    def __init__(self):
        self._fd = -1

    def __enter__(self):
        _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(
            _cctally_core.STATUSLINE_PERSIST_LOCK_PATH,
            os.O_WRONLY | os.O_CREAT,
            0o644,
        )
        fcntl.flock(self._fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc, traceback):
        if self._fd < 0:
            return False
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(self._fd)
        except OSError:
            pass
        self._fd = -1
        return False


def _selected_state_lock() -> _SelectedStateLock:
    """Return the blocking lock shared by every selected-state writer."""
    return _SelectedStateLock()


def _record_args(*, percent, resets_at, five_hour_percent, five_hour_resets_at,
                 source):
    """Build the cmd_record_usage Namespace the feeder passes to the kernel.

    Percents pass through UNTOUCHED — the kernel's ingress `_normalize_percent`
    is the single clamp site (no new clamp here). Epochs are stringified to
    match the shape cmd_record_usage expects (`int(args.resets_at)`)."""
    return argparse.Namespace(
        percent=percent,
        resets_at=str(int(resets_at)),
        five_hour_percent=five_hour_percent,
        five_hour_resets_at=(
            str(int(five_hour_resets_at))
            if five_hour_resets_at is not None else None
        ),
        source=source,
    )


_CANDIDATE_FINAL_RE = re.compile(r"[0-9a-f]{64}\.json\Z")


class ProjectionUnstable(RuntimeError):
    """stats.db changed while its selected projection was being read."""


def _candidate_identity_token(parsed) -> str:
    if isinstance(parsed.session_id, str) and parsed.session_id:
        kind, value = "session_id", parsed.session_id
    elif isinstance(parsed.transcript_path, str) and parsed.transcript_path:
        kind, value = "transcript_path", parsed.transcript_path
    else:
        kind, value = "anonymous", ""
    return hashlib.sha256(f"{kind}\0{value}".encode("utf-8")).hexdigest()


def _statusline_reset_is_plausible(axis: str, epoch: int, now_epoch: int) -> bool:
    if not isinstance(epoch, int) or isinstance(epoch, bool):
        return False
    if axis == "fiveHour":
        return now_epoch - 600 <= epoch <= now_epoch + 6 * 3600
    return now_epoch - 30 * 86400 <= epoch <= now_epoch + 8 * 86400


def _candidate_from_input(parsed, *, received_at: int) -> "_candidates.Candidate | None":
    def axis(percent, resets_at, name):
        if isinstance(percent, bool) or not isinstance(percent, (int, float)):
            return None
        if not math.isfinite(float(percent)) or not 0 <= float(percent) <= 100:
            return None
        if not _statusline_reset_is_plausible(name, resets_at, received_at):
            return None
        return _candidates.AxisValue(float(percent), int(resets_at))

    five = axis(parsed.rate_limits_5h_pct, parsed.rate_limits_5h_resets_at, "fiveHour")
    seven = axis(parsed.rate_limits_7d_pct, parsed.rate_limits_7d_resets_at, "sevenDay")
    if five is None and seven is None:
        return None
    return _candidates.Candidate(
        token=_candidate_identity_token(parsed),
        received_at=received_at,
        five_hour=five,
        seven_day=seven,
    )


def _candidate_path(token: str) -> pathlib.Path:
    return _cctally_core.STATUSLINE_CANDIDATE_DIR / f"{token}.json"


def _atomic_write_json(path: pathlib.Path, document: dict) -> None:
    """Publish compact JSON with a unique same-directory exclusive temp file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent == _cctally_core.STATUSLINE_CANDIDATE_DIR:
        os.chmod(path.parent, 0o700)
    token = secrets.token_hex(16)
    temp = path.parent / f".{path.name}.tmp.{os.getpid()}.{token}"
    fd = -1
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            json.dump(document, handle, allow_nan=False, separators=(",", ":"))
        os.replace(temp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            temp.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _candidate_document(candidate: "_candidates.Candidate") -> dict:
    document = {"schemaVersion": 1, "receivedAt": candidate.received_at}
    if candidate.five_hour is not None:
        document["fiveHour"] = {
            "percent": candidate.five_hour.percent,
            "resetsAt": candidate.five_hour.raw_resets_at,
        }
    if candidate.seven_day is not None:
        document["sevenDay"] = {
            "percent": candidate.seven_day.percent,
            "resetsAt": candidate.seven_day.raw_resets_at,
        }
    return document


def _write_candidate(candidate: "_candidates.Candidate") -> None:
    _cctally_core.STATUSLINE_CANDIDATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(_cctally_core.STATUSLINE_CANDIDATE_DIR, 0o700)
    _atomic_write_json(_candidate_path(candidate.token), _candidate_document(candidate))


def _load_candidate_spool(*, now_epoch: int) -> tuple["_candidates.Candidate", ...]:
    directory = _cctally_core.STATUSLINE_CANDIDATE_DIR
    try:
        entries = tuple(directory.iterdir())
    except OSError:
        return ()
    result = []
    for path in entries:
        # Match the complete final grammar before touching an entry.  In-flight
        # peer temps are deliberately neither read nor pruned.
        if not _CANDIDATE_FINAL_RE.fullmatch(path.name):
            continue
        try:
            raw = path.read_bytes()
            candidate = _candidates.load_candidate_document(
                raw,
                now_epoch=now_epoch,
                reset_is_plausible=lambda axis, epoch: _statusline_reset_is_plausible(axis, epoch, now_epoch),
                token=path.stem,
            )
        except (OSError, _candidates.StateValidationError):
            try:
                path.unlink()
            except OSError:
                pass
            continue
        if not (-5 <= now_epoch - candidate.received_at < _cctally_core.STATUSLINE_CANDIDATE_TTL_SECONDS):
            try:
                path.unlink()
            except OSError:
                pass
            continue
        result.append(candidate)
    return tuple(result)


def _scan_active_candidate_spool(*, now_epoch: int) -> tuple["_candidates.Candidate", ...]:
    """Read valid active candidates without pruning or otherwise mutating.

    `doctor` needs a truthful active-count signal but retains its global
    read-only contract.  In contrast, the reducer loader above owns best-effort
    cleanup of malformed/expired final entries.
    """
    try:
        entries = tuple(_cctally_core.STATUSLINE_CANDIDATE_DIR.iterdir())
    except OSError:
        return ()
    result = []
    for path in entries:
        if not _CANDIDATE_FINAL_RE.fullmatch(path.name):
            continue
        try:
            candidate = _candidates.load_candidate_document(
                path.read_bytes(),
                now_epoch=now_epoch,
                reset_is_plausible=lambda axis, epoch: _statusline_reset_is_plausible(
                    axis, epoch, now_epoch
                ),
                token=path.stem,
            )
        except (OSError, _candidates.StateValidationError):
            continue
        if -5 <= now_epoch - candidate.received_at < _cctally_core.STATUSLINE_CANDIDATE_TTL_SECONDS:
            result.append(candidate)
    return tuple(result)


def _fingerprint_file(path: pathlib.Path) -> "_candidates.FileFingerprint | None":
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    except OSError:
        return None
    return _candidates.FileFingerprint(st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


def _db_file_fingerprint() -> dict[str, "_candidates.FileFingerprint | None"]:
    db = _cctally_core.DB_PATH
    return {
        "main": _fingerprint_file(db),
        "wal": _fingerprint_file(db.with_name(db.name + "-wal")),
    }


def _epoch_from_iso(value: object) -> int:
    return int(_cctally_core.parse_iso_datetime(str(value), "statusline projection").timestamp())


def _read_db_projection_once() -> "_candidates.DbProjection":
    conn = open_db()
    try:
        weekly_rows = conn.execute(
            "SELECT id, weekly_percent, week_start_date, week_start_at, week_end_at, "
            "       captured_at_utc, source "
            "FROM weekly_usage_snapshots "
            "WHERE weekly_percent IS NOT NULL AND week_end_at IS NOT NULL "
            "ORDER BY unixepoch(captured_at_utc) DESC, id DESC"
        ).fetchall()
        five_rows = conn.execute(
            "SELECT id, five_hour_percent, five_hour_resets_at, five_hour_window_key, "
            "       captured_at_utc, source "
            "FROM weekly_usage_snapshots "
            "WHERE five_hour_percent IS NOT NULL AND five_hour_resets_at IS NOT NULL "
            "ORDER BY unixepoch(captured_at_utc) DESC, id DESC"
        ).fetchall()

        weekly_groups: dict[int, list] = {}
        for row in weekly_rows:
            try:
                raw = _epoch_from_iso(row["week_end_at"])
            except (TypeError, ValueError):
                continue
            canonical = int(_cctally_core._normalize_week_boundary_dt(
                dt.datetime.fromtimestamp(raw, tz=dt.timezone.utc)
            ).timestamp())
            weekly_groups.setdefault(canonical, []).append(row)

        weekly_projection = None
        if weekly_groups:
            canonical = max(weekly_groups)
            rows = weekly_groups[canonical]
            reference = max(
                rows, key=lambda row: (_epoch_from_iso(row["captured_at_utc"]), int(row["id"]))
            )
            week_start_at = reference["week_start_at"]
            if not week_start_at:
                week_start_at = dt.datetime.fromtimestamp(
                    canonical - 7 * 86400, tz=dt.timezone.utc
                ).isoformat().replace("+00:00", "Z")
            week_end_at = reference["week_end_at"]
            floor = _cctally_core._reset_aware_floor(
                conn,
                str(reference["week_start_date"]),
                str(week_start_at),
                str(week_end_at),
            )
            floor_epoch = _epoch_from_iso(floor) if floor else 0
            eligible = [
                row for row in rows
                if floor_epoch == 0 or _epoch_from_iso(row["captured_at_utc"]) >= floor_epoch
            ]
            if eligible:
                weekly = max(
                    eligible,
                    key=lambda row: (_epoch_from_iso(row["captured_at_utc"]), int(row["id"])),
                )
                raw = _epoch_from_iso(weekly["week_end_at"])
                weekly_projection = _candidates.AxisProjection(
                    float(weekly["weekly_percent"]), raw, canonical,
                    _epoch_from_iso(weekly["captured_at_utc"]),
                    str(weekly["source"] or "statusline"), floor_epoch,
                )

        now_epoch = int(time.time())
        five_groups: dict[int, list] = {}
        for row in five_rows:
            try:
                raw = _epoch_from_iso(row["five_hour_resets_at"])
            except (TypeError, ValueError):
                continue
            if not _statusline_reset_is_plausible("fiveHour", raw, now_epoch):
                continue
            stored_key = row["five_hour_window_key"]
            canonical = (
                int(stored_key)
                if stored_key is not None
                else _cctally()._canonical_5h_window_key(raw)
            )
            five_groups.setdefault(canonical, []).append(row)

        five_projection = None
        if five_groups:
            canonical = max(five_groups)
            reset_event_id = _cctally()._resolve_active_five_hour_reset_event_id(conn, canonical)
            floor_epoch = 0
            if reset_event_id:
                event = conn.execute(
                    "SELECT effective_reset_at_utc FROM five_hour_reset_events WHERE id = ?",
                    (reset_event_id,),
                ).fetchone()
                if event is not None:
                    floor_epoch = _epoch_from_iso(event["effective_reset_at_utc"])
            eligible = [
                row for row in five_groups[canonical]
                if floor_epoch == 0 or _epoch_from_iso(row["captured_at_utc"]) >= floor_epoch
            ]
            if eligible:
                five = max(
                    eligible,
                    key=lambda row: (_epoch_from_iso(row["captured_at_utc"]), int(row["id"])),
                )
                raw = _epoch_from_iso(five["five_hour_resets_at"])
                five_projection = _candidates.AxisProjection(
                    float(five["five_hour_percent"]), raw, canonical,
                    _epoch_from_iso(five["captured_at_utc"]),
                    str(five["source"] or "statusline"), int(reset_event_id),
                )
    finally:
        conn.close()
    return _candidates.DbProjection(five_projection, weekly_projection)


def _read_db_projection_stable(*, attempts: int = 3) -> "_candidates.DbProjection":
    for _ in range(attempts):
        before = _db_file_fingerprint()
        projection = _read_db_projection_once()
        after = _db_file_fingerprint()
        if before == after and after["main"] is not None:
            return dataclasses.replace(projection, db_files=after)
    raise ProjectionUnstable("stats.db changed during projection")


def _fingerprint_document(value: "_candidates.FileFingerprint | None") -> dict | None:
    if value is None:
        return None
    return {"device": value.device, "inode": value.inode, "size": value.size, "mtimeNs": value.mtime_ns}


def _axis_projection_document(value: "_candidates.AxisProjection | None") -> dict | None:
    if value is None:
        return None
    return {
        "percent": value.percent,
        "rawResetsAt": value.raw_resets_at,
        "canonicalKey": value.canonical_key,
        "capturedAt": value.captured_at,
        "source": value.source,
        "resetGeneration": value.reset_generation,
    }


def _pending_document(value: "_candidates.PendingDrop | None") -> dict | None:
    if value is None:
        return None
    signature = value.retry_signature
    return {
        "canonicalKey": value.canonical_key,
        "reducedPercent": value.reduced_percent,
        "firstSeenAt": value.first_seen_at,
        "kernelStage": value.kernel_stage,
        "attempts": value.attempts,
        "contributors": {
            token: {"baselineReceivedAt": item.baseline_received_at, "satisfied": item.satisfied}
            for token, item in value.contributors.items()
        },
        "retrySignature": (
            None if signature is None else {
                "candidateKey": signature.candidate_key,
                "candidatePercent": signature.candidate_percent,
                "dbKey": signature.db_key,
                "dbPercent": signature.db_percent,
                "dbResetGeneration": signature.db_reset_generation,
            }
        ),
    }


def _control_document(control: "_candidates.ControlState") -> dict:
    files = control.db_projection.db_files or {"main": None, "wal": None}
    return {
        "schemaVersion": 1,
        "dbProjection": {
            "fiveHour": _axis_projection_document(control.db_projection.five_hour),
            "sevenDay": _axis_projection_document(control.db_projection.seven_day),
        },
        "dbFiles": {
            "main": _fingerprint_document(files.get("main")),
            "wal": _fingerprint_document(files.get("wal")),
        },
        "pendingDrops": {
            "fiveHour": _pending_document(control.pending_drops.get("fiveHour")),
            "sevenDay": _pending_document(control.pending_drops.get("sevenDay")),
        },
    }


def _read_control_state(*, now_epoch: int) -> "_candidates.ControlState | None":
    try:
        return _candidates.load_control_document(
            _cctally_core.STATUSLINE_SELECTED_PATH.read_bytes(), now_epoch=now_epoch
        )
    except (OSError, _candidates.StateValidationError):
        return None


def _statusline_control_db_agreement(*, now_epoch: int) -> bool | None:
    """Return control/DB-fingerprint agreement without opening SQLite.

    ``None`` means the derived control document is absent; ``False`` means an
    existing document was invalid or its stable fingerprint is stale.  This is
    intentionally stat-only so doctor remains a read-only diagnostic.
    """
    control = _read_control_state(now_epoch=now_epoch)
    if control is None:
        try:
            return False if _cctally_core.STATUSLINE_SELECTED_PATH.exists() else None
        except OSError:
            return None
    return control.db_projection.db_files == _db_file_fingerprint()


def _write_control_state(control: "_candidates.ControlState") -> None:
    _atomic_write_json(_cctally_core.STATUSLINE_SELECTED_PATH, _control_document(control))


def _reconcile_selected_control(
    projection: "_candidates.DbProjection", *, now_epoch: int, observed_axes=()
) -> None:
    """Rewrite DB-derived control while retaining only reducer pending state."""
    existing = _read_control_state(now_epoch=now_epoch)
    pending = dict(
        existing.pending_drops
        if existing is not None
        else {"fiveHour": None, "sevenDay": None}
    )
    for axis in observed_axes:
        if axis in _candidates.AXES:
            pending[axis] = None
    _write_control_state(_candidates.ControlState(projection, pending))


def _tombstone_path(axis: str) -> pathlib.Path:
    return (
        _cctally_core.STATUSLINE_AUTHORITATIVE_5H_PATH
        if axis == "fiveHour" else _cctally_core.STATUSLINE_AUTHORITATIVE_7D_PATH
    )


def _tombstone_document(value: "_candidates.Tombstone") -> dict:
    document = {"schemaVersion": 1, "axis": value.axis, "state": value.state}
    if value.state == "inflight":
        document["startedAt"] = value.started_at
        document["priorBlockReceivedAtThrough"] = value.prior_block_received_at_through
    else:
        document["blockReceivedAtThrough"] = value.block_received_at_through
    return document


def _read_tombstone(axis: str, *, now_epoch: int, fail_closed: bool = True) -> "_candidates.Tombstone | None":
    try:
        return _candidates.load_tombstone_document(
            _tombstone_path(axis).read_bytes(), expected_axis=axis, now_epoch=now_epoch
        )
    except FileNotFoundError:
        return None
    except (OSError, _candidates.StateValidationError):
        return _candidates.Tombstone(axis, "inflight") if fail_closed else None


def _write_tombstone(axis: str, value: "_candidates.Tombstone") -> None:
    _atomic_write_json(_tombstone_path(axis), _tombstone_document(value))


def _authoritative_begin(axes, *, now_epoch: int) -> dict[str, "_candidates.Tombstone"]:
    """Write fail-closed inflight tombstones before an authority mutation.

    Each axis retains a prior committed cutoff while it is inflight.  This
    means that a crash after an equality-deduplicated database call has the
    same safety posture as a crash before a write: no stale spool candidate
    can become selected until a later authoritative repair commits it.
    """
    requested = frozenset(axes)
    if not requested or not requested <= set(_candidates.AXES):
        raise ValueError("authoritative writer requires known observation axes")
    handles = {}
    for axis in sorted(requested):
        previous = _read_tombstone(axis, now_epoch=now_epoch, fail_closed=True)
        if previous is None:
            carried = None
        elif previous.state == "committed":
            carried = previous.block_received_at_through
        else:
            carried = previous.prior_block_received_at_through
        inflight = _candidates.Tombstone(
            axis=axis,
            state="inflight",
            started_at=now_epoch,
            prior_block_received_at_through=carried,
        )
        _write_tombstone(axis, inflight)
        handles[axis] = inflight
    return handles


def _authoritative_commit(
    handles: dict[str, "_candidates.Tombstone"], *, completion_epoch: int
) -> None:
    """Finalize every write-ahead tombstone with a monotonic cutoff."""
    for axis, inflight in handles.items():
        cutoff = max(
            inflight.prior_block_received_at_through or 0,
            completion_epoch + _cctally_core.STATUSLINE_CANDIDATE_FUTURE_SKEW_SECONDS,
        )
        _write_tombstone(
            axis,
            _candidates.Tombstone(
                axis=axis,
                state="committed",
                block_received_at_through=cutoff,
            ),
        )


def _after_authoritative_record() -> None:
    """Test seam immediately after the authority kernel returns success."""


def _authoritative_repair_required(*, now_epoch: int) -> bool:
    """Whether an inflight/invalid tombstone needs an OAuth repair attempt."""
    for axis in _candidates.AXES:
        value = _read_tombstone(axis, now_epoch=now_epoch, fail_closed=True)
        if value is not None and value.state == "inflight":
            return True
    return False


def _authoritative_record_usage(
    args: argparse.Namespace,
    observed_axes,
    *,
    lock_held: bool = False,
) -> _AuthoritativeRecordResult:
    """Record OAuth authority under write-ahead tombstones and reconcile it.

    ``lock_held`` is for OAuth refresh callers that already hold the selected
    lock across their fetch, authoritative publication, and matching backoff
    transition. All other callers acquire the same blocking lock here.
    """
    if not lock_held:
        try:
            with _selected_state_lock():
                return _authoritative_record_usage(
                    args, observed_axes, lock_held=True
                )
        except OSError as exc:
            return _AuthoritativeRecordResult("record_failed", str(exc))

    now_epoch = int(time.time())
    try:
        handles = _authoritative_begin(observed_axes, now_epoch=now_epoch)
    except (OSError, ValueError) as exc:
        return _AuthoritativeRecordResult("record_failed", str(exc))

    try:
        rc = _cctally().cmd_record_usage(args)
    except Exception as exc:
        return _AuthoritativeRecordResult("record_failed", str(exc))
    if rc != 0:
        return _AuthoritativeRecordResult("record_failed", f"exit {rc}")

    try:
        # Intentionally after an equality-deduplicated success: tests use this
        # seam to prove the write-ahead tombstone still fails closed on crash.
        _cctally()._after_authoritative_record()
        projection = _read_db_projection_stable()
        completion_epoch = int(time.time())
        _authoritative_commit(handles, completion_epoch=completion_epoch)
        _reconcile_selected_control(
            projection, now_epoch=completion_epoch, observed_axes=observed_axes
        )
        _cctally()._statusline_observe_touch()
    except Exception as exc:
        # Do not clean up the inflight tombstone on a post-record failure.
        # It is the durable proof that a later authoritative writer must repair
        # this axis before any spool candidate may be selected again.
        return _AuthoritativeRecordResult("record_failed", str(exc))
    return _AuthoritativeRecordResult("ok")


def _empty_control(projection: "_candidates.DbProjection") -> "_candidates.ControlState":
    return _candidates.ControlState(projection, {"fiveHour": None, "sevenDay": None})


def _canonicalize_candidates(
    candidates: tuple["_candidates.Candidate", ...],
    projection: "_candidates.DbProjection",
) -> tuple["_candidates.Candidate", ...]:
    c = _cctally()
    five_values = [candidate.five_hour for candidate in candidates if candidate.five_hour is not None]
    anchor = None
    if projection.five_hour is not None:
        anchor = (projection.five_hour.raw_resets_at, projection.five_hour.canonical_key)
    resolved = _candidates.canonicalize_five_hour_axes(
        five_values,
        db_anchor=anchor,
        canonicalize=lambda raw, prior: c._canonical_5h_window_key(
            raw,
            prior_epoch=None if prior is None else prior[0],
            prior_key=None if prior is None else prior[1],
        ),
    )
    # Canonicalization establishes a physical-window key, not a selected
    # candidate value.  Several sessions can share the exact raw reset while
    # reporting different percentages; mapping raw -> AxisValue would let the
    # final filesystem enumeration overwrite every session's percentage.
    by_raw_key = {value.raw_resets_at: value.canonical_key for value in resolved}
    result = []
    for candidate in candidates:
        seven = candidate.seven_day
        if seven is not None:
            canonical = int(_cctally_core._normalize_week_boundary_dt(
                dt.datetime.fromtimestamp(seven.raw_resets_at, tz=dt.timezone.utc)
            ).timestamp())
            seven = dataclasses.replace(seven, canonical_key=canonical)
        five = candidate.five_hour
        if five is not None:
            five = dataclasses.replace(five, canonical_key=by_raw_key[five.raw_resets_at])
        result.append(dataclasses.replace(candidate, five_hour=five, seven_day=seven))
    return tuple(result)


def _build_publication_plan(
    decision: "_candidates.ReductionDecision",
    projection: "_candidates.DbProjection",
    *,
    now_epoch: int,
) -> "_candidates.PublicationPlan | None":
    if decision.plan is None:
        return None
    seven = decision.plan.seven_day
    if seven is None and projection.seven_day is not None:
        source = projection.seven_day
        if now_epoch < source.raw_resets_at <= now_epoch + 8 * 86400:
            seven = _candidates.AxisValue(source.percent, source.raw_resets_at, source.canonical_key)
    if seven is None:
        return None
    five = decision.plan.five_hour
    if five is None and decision.plan.seven_day is not None and projection.five_hour is not None:
        source = projection.five_hour
        if now_epoch < source.raw_resets_at <= now_epoch + 6 * 3600:
            five = _candidates.AxisValue(source.percent, source.raw_resets_at, source.canonical_key)
    if five is not None and not (now_epoch < five.raw_resets_at <= now_epoch + 6 * 3600):
        five = None
    return _candidates.PublicationPlan(seven_day=seven, five_hour=five)


def _projection_changed(before: "_candidates.DbProjection", after: "_candidates.DbProjection") -> bool:
    return before.five_hour != after.five_hour or before.seven_day != after.seven_day


def _statusline_reduce_and_publish() -> "_candidates.ReductionDecision | None":
    now_epoch = int(time.time())
    candidates = _load_candidate_spool(now_epoch=now_epoch)
    if not candidates:
        existing = _read_control_state(now_epoch=now_epoch)
        if existing is not None and any(existing.pending_drops.values()):
            projection = _read_db_projection_stable()
            control = _empty_control(projection)
            _write_control_state(control)
            return _candidates.ReductionDecision("WRITE_CONTROL", control)
        return None
    projection = _read_db_projection_stable()
    existing = _read_control_state(now_epoch=now_epoch)
    control_repair_required = (
        existing is None or existing.db_projection.db_files != projection.db_files
    )
    control = (
        _candidates.ControlState(projection, existing.pending_drops)
        if existing is not None else _empty_control(projection)
    )
    tombstones = {
        axis: _read_tombstone(axis, now_epoch=now_epoch)
        for axis in _candidates.AXES
    }
    candidates = _canonicalize_candidates(candidates, projection)
    decision = _candidates.reduce_candidates(
        candidates, db=projection, control=control, tombstones=tombstones, now_epoch=now_epoch
    )
    if decision.action == "NOOP":
        if control_repair_required:
            decision = dataclasses.replace(decision, action="WRITE_CONTROL")
            _write_control_state(decision.control)
        return decision
    if decision.action == "WRITE_CONTROL":
        _write_control_state(decision.control)
        return decision
    plan = _build_publication_plan(decision, projection, now_epoch=now_epoch)
    if plan is None or plan.seven_day is None:
        _write_control_state(decision.control)
        return decision
    args = _record_args(
        percent=plan.seven_day.percent,
        resets_at=plan.seven_day.raw_resets_at,
        five_hour_percent=None if plan.five_hour is None else plan.five_hour.percent,
        five_hour_resets_at=None if plan.five_hour is None else plan.five_hour.raw_resets_at,
        source="statusline",
    )
    if _cctally().cmd_record_usage(args) != 0:
        return decision
    after = _read_db_projection_stable()
    if _projection_changed(projection, after):
        # A real DB change is authoritative selected truth, so re-reduce to
        # clear any satisfied pending axis whose projection now matches it.
        refreshed = _candidates.reduce_candidates(
            candidates, db=after, control=decision.control,
            tombstones=tombstones, now_epoch=int(time.time()),
        )
    else:
        # The record kernel can deliberately leave the DB unchanged: the first
        # zero arms its own debounce and unsupported small drops are HWM
        # rejected.  Preserve the precomputed bounded attempt state so the next
        # tick—not this post-record reconciliation—performs the required fresh
        # contributor-consensus pass before spending another kernel call.
        refreshed = _candidates.ReductionDecision("WRITE_CONTROL", decision.control)
    _write_control_state(refreshed.control)
    if _projection_changed(projection, after):
        _cctally()._statusline_observe_touch()
    return refreshed


def _preliminary_decision() -> "_candidates.ReductionDecision | None":
    now_epoch = int(time.time())
    candidates = _load_candidate_spool(now_epoch=now_epoch)
    if not candidates:
        return None
    control = _read_control_state(now_epoch=now_epoch)
    if control is None or control.db_projection.db_files != _db_file_fingerprint():
        return _candidates.ReductionDecision("WRITE_CONTROL", control or _empty_control(
            _candidates.DbProjection(None, None, _db_file_fingerprint())
        ))
    candidates = _canonicalize_candidates(candidates, control.db_projection)
    return _candidates.reduce_candidates(
        candidates,
        db=control.db_projection,
        control=control,
        tombstones={axis: _read_tombstone(axis, now_epoch=now_epoch) for axis in _candidates.AXES},
        now_epoch=now_epoch,
    )


def _fork_persist(parent_lock_fd: int) -> None:
    """Run the revalidated spool reducer in a detached serialized child."""
    try:
        pid = os.fork()
    except OSError:
        return
    if pid > 0:
        return

    # --- child ---
    try:
        try:
            os.close(parent_lock_fd)
        except OSError:
            pass
        try:
            os.setsid()
        except OSError:
            pass
        try:
            devnull = os.open(os.devnull, os.O_RDWR)
            os.dup2(devnull, 0)
            os.dup2(devnull, 1)
            os.dup2(devnull, 2)
            if devnull > 2:
                os.close(devnull)
        except OSError:
            pass

        child_fd = -1
        try:
            child_fd = os.open(
                _cctally_core.STATUSLINE_PERSIST_LOCK_PATH,
                os.O_WRONLY | os.O_CREAT, 0o644,
            )
            fcntl.flock(child_fd, fcntl.LOCK_EX)
        except OSError:
            child_fd = -1
        try:
            if child_fd >= 0:
                _statusline_reduce_and_publish()
        finally:
            if child_fd >= 0:
                try:
                    fcntl.flock(child_fd, fcntl.LOCK_UN)
                except OSError:
                    pass
                try:
                    os.close(child_fd)
                except OSError:
                    pass
    except BaseException:
        pass
    finally:
        os._exit(0)


def _statusline_persist(parsed, *, sync_for_test: bool = False) -> None:
    """Spool an eligible session candidate then reduce it opportunistically."""
    if _lib_statusline.is_alternate_pool_model_id(parsed.model_id):
        return
    candidate = _candidate_from_input(parsed, received_at=int(time.time()))
    if candidate is None:
        return
    try:
        _write_candidate(candidate)
        _cctally()._statusline_transport_touch()
    except OSError:
        return
    c = _cctally()
    lock_fd = c._try_acquire_persist_lock()
    if lock_fd is None:
        return
    try:
        preliminary = _preliminary_decision()
        if preliminary is None or preliminary.action == "NOOP":
            return
        if sync_for_test:
            _statusline_reduce_and_publish()
            return
        _fork_persist(lock_fd)
    finally:
        c._release_persist_lock(lock_fd)


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
