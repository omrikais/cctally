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
import contextvars
import dataclasses
import datetime as dt
import fcntl
import functools
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
import _lib_five_hour
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
    "claude-opus-5[1m]": 1_000_000,
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
    # errors, but this is belt-and-suspenders). Absence of rate_limits or a
    # lost persist lock degrades to a clean no-op; the authoritative OAuth
    # confirmation below covers the missing/stale-payload case.
    try:
        _statusline_persist(inp)
    except Exception:
        pass
    # Claude Code may replay an unchanged rate_limits object across timer
    # renders even after the provider counters have advanced. Confirm against
    # the authoritative OAuth surface once per account-wide timer cycle. The
    # helper is fully detached, shares hook-tick's throttle lock/marker, and
    # honors the existing selected-freshness and 429-backoff gates.
    try:
        c._statusline_oauth_tick()
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


# =========================================================================
# #825/#832 account-bound coordination state (spec 4.2–4.4, section 7)
# =========================================================================
#
# Every current statusline coordination file lives under ONE sibling root,
# `APP_DIR/statusline-account-bound-v2/`, partitioned by the exact account:
#
#   selected.lock                       the selected-state / persist flock
#   transport.last                      render-path transport marker
#   accounts/<account_key>/
#     candidates/<token>.json           bound candidates (spec 4.2)
#     transition.json                   the ONE recoverable per-account
#                                       transition document (spec 4.3)
#     tombstones/<axis>-<window>.json   authoritative write-ahead fences
#     observe.last                      the account's selected freshness
#     account-observed.json             account-observation dedup marker
#
# The previous release's paths (`statusline-candidates/`,
# `statusline-selected.json`, the authoritative tombstone files, the observe
# and transport markers and `statusline-persist.lock`) are never read or
# written by the current protocol. They are only compared, as a whole, with
# the journal-anchored legacy spool baseline (spec 5.5 step 6, section 7), so
# no legacy marker can suppress a current write and no current write can look
# like legacy activity.

TRANSITION_SCHEMA_VERSION = 1
_ACCOUNT_KEY_RE = re.compile(r"[0-9a-f]{32}\Z")
_PUBLICATION_ID_RE = re.compile(r"[0-9a-f]{64}\Z")
_EVIDENCE_RE = re.compile(r"[0-9a-f]{32}\Z")
_COVERAGE_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_TOMBSTONE_NAME_RE = re.compile(r"(fiveHour|sevenDay)-(-?[0-9]{1,19})\.json\Z")
#: Canonical window keys can round below the raw reset, so a window is still
#: "live" for this long after its key (five-hour jitter floor; weekly hour
#: normalization).
_TOMBSTONE_WINDOW_GRACE = {"fiveHour": 600, "sevenDay": 3600}


def _bound_root() -> pathlib.Path:
    return _cctally_core.STATUSLINE_ACCOUNT_BOUND_DIR


def _require_account_key(account_key) -> str:
    """An exact account namespace: the 32-hex key or the `unattributed`
    sentinel. Anything else (including a path fragment) is refused."""
    if account_key == "unattributed":
        return account_key
    if not isinstance(account_key, str) or _ACCOUNT_KEY_RE.fullmatch(account_key) is None:
        raise ValueError("invalid exact account namespace")
    return account_key


def _accounts_root() -> pathlib.Path:
    return _bound_root() / "accounts"


def _account_dir(account_key) -> pathlib.Path:
    return _accounts_root() / _require_account_key(account_key)


def _candidate_dir(account_key) -> pathlib.Path:
    return _account_dir(account_key) / "candidates"


def _candidate_path(token: str, *, account_key: str) -> pathlib.Path:
    return _candidate_dir(account_key) / f"{token}.json"


def _transition_path(account_key: str) -> pathlib.Path:
    return _account_dir(account_key) / "transition.json"


def _tombstone_dir(account_key: str) -> pathlib.Path:
    return _account_dir(account_key) / "tombstones"


def _tombstone_path(axis: str, *, account_key: str, window: int) -> pathlib.Path:
    if axis not in _candidates.AXES:
        raise ValueError("unknown observation axis")
    return _tombstone_dir(account_key) / f"{axis}-{int(window)}.json"


def _observe_marker_path(account_key: str) -> pathlib.Path:
    return _account_dir(account_key) / "observe.last"


def _transport_marker_path() -> pathlib.Path:
    return _bound_root() / "transport.last"


def _selected_lock_path() -> pathlib.Path:
    return _bound_root() / "selected.lock"


def _account_keys_on_disk() -> list[str]:
    """Every exact account partition present, in bytewise key order."""
    try:
        entries = tuple(_accounts_root().iterdir())
    except OSError:
        return []
    keys = []
    for entry in entries:
        try:
            _require_account_key(entry.name)
        except ValueError:
            continue
        if entry.is_dir():
            keys.append(entry.name)
    return sorted(keys, key=lambda key: key.encode("utf-8"))


def _capture_binding(*, credential: str | None = None):
    """One `AccountBinding` from the stable-read resolver, or None when the
    identity is torn, unreadable or inconsistent (spec 4.1)."""
    import _lib_account_binding

    try:
        identity = _cctally_core._resolve_active_claude_identity()
    except Exception:
        return None
    return _lib_account_binding.capture(identity, credential=credential)


def _try_acquire_persist_lock() -> "int | None":
    """Non-blocking acquire of the current selected-state lock.

    Returns the open fd on success; ``None`` when another render already
    holds it (EWOULDBLOCK) or the lock file can't be opened/locked — in
    which case the caller renders without forking."""
    try:
        _durable_mkdir(_bound_root())
        fd = os.open(_selected_lock_path(), os.O_WRONLY | os.O_CREAT, 0o600)
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
    """Blocking owner of the one selected-state writer critical section.

    The lock file is the current protocol's own (`selected.lock` under the
    account-bound root). A previous-release process uses its own lock file, so
    the two never serialize — by design: its writes are excluded."""

    def __init__(self):
        self._fd = -1
        self._depth = None

    def __enter__(self):
        _durable_mkdir(_bound_root())
        self._fd = os.open(_selected_lock_path(), os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX)
        except BaseException:
            os.close(self._fd)
            self._fd = -1
            raise
        self._depth = _SELECTED_DEPTH.set(_SELECTED_DEPTH.get() + 1)
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
        if self._depth is not None:
            _SELECTED_DEPTH.reset(self._depth)
            self._depth = None
        if _SELECTED_DEPTH.get() == 0:
            # #825/#832 spec 4.3 revision 2: a completed OAuth authority
            # reconciliation resumes deferred statusline work once the
            # selected state is released, and a wake this context recorded
            # under the lock is dispatched now.
            if _OAUTH_RECONCILED.get():
                _OAUTH_RECONCILED.set(False)
                post_wake("oauth")
            _flush_owed_dispatch()
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


class ProjectionUnavailable(RuntimeError):
    """The index could not be read at all: its rebuild is deferred to a
    detached worker (#825/#832 fix round D, P1-2). Unavailable evidence, never
    an empty projection — a drop decision must not read "no baseline" from it."""


def _candidate_identity_token(parsed) -> str:
    if isinstance(parsed.session_id, str) and parsed.session_id:
        kind, value = "session_id", parsed.session_id
    elif isinstance(parsed.transcript_path, str) and parsed.transcript_path:
        kind, value = "transcript_path", parsed.transcript_path
    else:
        kind, value = "anonymous", ""
    return hashlib.sha256(f"{kind}\0{value}".encode("utf-8")).hexdigest()


# A 5h reset this far in the past is no longer attributable to capture jitter.
# Shared by the plausibility window and the #755 staleness proof below.
_FIVE_HOUR_EXPIRY_GRACE_SECONDS = 600


def _statusline_reset_is_plausible(axis: str, epoch: int, now_epoch: int) -> bool:
    if not isinstance(epoch, int) or isinstance(epoch, bool):
        return False
    if axis == "fiveHour":
        return (now_epoch - _FIVE_HOUR_EXPIRY_GRACE_SECONDS
                <= epoch <= now_epoch + 6 * 3600)
    return now_epoch - 30 * 86400 <= epoch <= now_epoch + 8 * 86400


def _five_hour_payload_is_expired(parsed, *, received_at: int) -> bool:
    """True when the payload carries a 5h window that has already closed.

    ``received_at`` is when cctally received the payload, not when Claude Code
    obtained the numbers in it, so it bounds nothing about how old the DATA is:
    an idle session re-renders its status line from a cached ``rate_limits``
    block indefinitely. Because ``_reduced_candidate`` takes the MAXIMUM
    percent across active candidates, one such session pins the 7d consensus
    at its stale pre-reset value and a post-reset drop can never publish.

    A 5h window that has already closed is the one content-derived proof of
    staleness available here, so it condemns the whole block rather than only
    its own axis (issue #755).
    """
    resets_at = parsed.rate_limits_5h_resets_at
    if isinstance(resets_at, bool) or not isinstance(resets_at, int):
        return False
    return resets_at < received_at - _FIVE_HOUR_EXPIRY_GRACE_SECONDS


def _candidate_from_input(parsed, *, received_at: int) -> "_candidates.Candidate | None":
    """Build the bound candidate for one render, or None.

    The binding is captured HERE, once, in the render process (spec 4.2). A
    torn or inconsistent identity yields no candidate: the caller then writes
    nothing, mutates no control state and consumes no retry attempt (case M3).
    A stably-absent identity binds `unattributed` in its own partition (M4).
    Raw resets are kept raw; no five-hour window key is chosen at capture."""
    def axis(percent, resets_at, name):
        if isinstance(percent, bool) or not isinstance(percent, (int, float)):
            return None
        if not math.isfinite(float(percent)) or not 0 <= float(percent) <= 100:
            return None
        if not _statusline_reset_is_plausible(name, resets_at, received_at):
            return None
        return _candidates.AxisValue(float(percent), int(resets_at))

    if _five_hour_payload_is_expired(parsed, received_at=received_at):
        return None

    five = axis(parsed.rate_limits_5h_pct, parsed.rate_limits_5h_resets_at, "fiveHour")
    seven = axis(parsed.rate_limits_7d_pct, parsed.rate_limits_7d_resets_at, "sevenDay")
    if five is None and seven is None:
        return None
    binding = _capture_binding()
    if binding is None:
        return None
    token = _candidate_identity_token(parsed)
    return _candidates.Candidate(
        token=token,
        received_at=received_at,
        five_hour=five,
        seven_day=seven,
        binding=binding,
        activity=_capture_activity(parsed, token),
    )


def _activity_cache_path(token: str) -> pathlib.Path:
    """The contributor's provider-activity scan cache in the v2 spool (#797
    spec 4.1, D10a): one accelerator document per contributor, never read by
    acknowledgement, detached reduction or frozen-plan recovery."""
    if _candidates.CANDIDATE_TOKEN_RE.fullmatch(token) is None:
        raise ValueError("invalid candidate token")
    return _bound_root() / "activity-cache" / f"{token}.json"


def _capture_activity(parsed, token: str):
    """The arrival's frozen provider-activity witness (#797 spec 4.1, 4.2),
    captured once here: in the render process, after Claude Code composed the
    input and before the intake commit, outside the intake critical section
    (its leaf lock forbids further locks beneath it). The transcript path is
    read here and goes nowhere else."""
    import _lib_provider_activity

    path = parsed.transcript_path if isinstance(parsed.transcript_path, str) else None
    return _lib_provider_activity.capture_activity(
        path, cache_path=_activity_cache_path(token))


#: Completion markers of the account-bound root's persistence barriers
#: (#825/#832 review finding CD4-D01, cycle 4 verification): one empty file
#: per directory or intake segment whose entry has been made durable, kept in
#: ONE directory beside the partitions so no partition listing ever sees one.
_DURABLE_MARKER_DIR = ".durable"


def _durable_marker(path) -> "pathlib.Path | None":
    """The completion marker of ``path`` (a directory or an intake segment
    at or under the account-bound root), or None outside that root, where
    no markers are kept."""
    root = _bound_root()
    try:
        relative = pathlib.Path(path).relative_to(root)
    except ValueError:
        return None
    return root / _DURABLE_MARKER_DIR / ("~" + "~".join(relative.parts))


def _durable_marked(path) -> bool:
    """Has ``path``'s entry — and, for a directory, every ancestor's up to
    the account-bound root — been made durable? One `stat`."""
    marker = _durable_marker(path)
    if marker is None:
        return False
    try:
        os.stat(marker)
    except OSError:
        return False
    return True


def _mark_durable(path) -> None:
    """Record that ``path``'s persistence barrier COMPLETED. Written only
    after the barrier returned, so a marker any process can see proves the
    fsync ran; the marker itself need not be durable, because a lost marker
    only makes the next caller repeat the barrier. Best effort for the same
    reason."""
    marker = _durable_marker(path)
    if marker is None:
        return
    try:
        marker.parent.mkdir(mode=0o700, exist_ok=True)
        os.close(os.open(marker, os.O_WRONLY | os.O_CREAT, 0o600))
    except OSError:
        pass


def _durable_mkdir(path) -> None:
    """Create ``path`` and its missing ancestors (mode 0700), making each
    one's directory entry durable before returning (#825/#832 review finding
    CD4-D01): its parent is fsynced right after the `mkdir`, the journal's
    `_fsync_dir` rule for a newly created directory entry.

    A directory's existence does not prove its creator finished that
    barrier: a creator that died between its `mkdir` and its parent's fsync
    leaves an entry a host crash can discard, together with every capture a
    later writer committed beneath it (cycle 4 verification). So under the
    account-bound root durability is proven by a completion marker
    (`_mark_durable`), written only after the barrier returned and only once
    every ancestor up to the root is durable too. A directory without one —
    created by an interrupted creator, by a lock or freshness write that
    used a plain `mkdir`, or by a release before the markers — is made
    durable by the first caller that relies on it. The common case is one
    `stat` of the marker and no sync. Above the root no markers are kept and
    an existing directory is trusted, as before."""
    import _cctally_journal

    path = pathlib.Path(path)
    if _durable_marked(path):
        return
    unproven = []
    probe = path
    while True:
        if _durable_marker(probe) is not None:
            if probe != path and _durable_marked(probe):
                break
        elif probe.exists():
            break
        unproven.append(probe)
        if probe.parent == probe:
            break
        probe = probe.parent
    for directory in reversed(unproven):
        try:
            # The leaf is private (0700); an ancestor keeps the default mode
            # `mkdir(parents=True)` always gave it.
            directory.mkdir(mode=0o700 if directory == path else 0o777)
        except FileExistsError:
            # It exists but nothing proves its entry durable (a peer may
            # have died before its sync): this caller makes it so.
            pass
        _cctally_journal._fsync_dir(directory.parent)
        _mark_durable(directory)


def _atomic_write_json(path: pathlib.Path, document: dict, *,
                       durable: bool = False) -> None:
    """Publish compact JSON with a unique same-directory exclusive temp file.

    ``durable`` (#825/#832 review finding CD4-D01) makes the publication
    survive a host crash, not only a process stop: the temp file's data is
    fsynced before the `os.replace`, and the directory after it — the
    journal's temp + fsync + replace + directory-fsync sequence. Every file
    that carries authority or membership uses it (fix round K3): the control
    checkpoint compaction relies on, the candidate slot that becomes the only
    copy of an eligible membership once compaction removes its intake, and
    the tombstone fence a frozen plan's supersession relies on."""
    _durable_mkdir(path.parent)
    token = secrets.token_hex(16)
    temp = path.parent / f".{path.name}.tmp.{os.getpid()}.{token}"
    fd = -1
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            json.dump(document, handle, allow_nan=False, separators=(",", ":"))
            if durable:
                handle.flush()
                os.fsync(handle.fileno())
        os.replace(temp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        if durable:
            import _cctally_journal

            _cctally_journal._fsync_dir(path.parent)
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
    """The current candidate document: the protocol generation and the
    immutable binding travel with the raw per-axis readings (spec 4.2)."""
    import _lib_provider_activity

    if candidate.binding is None:
        raise ValueError("a current candidate requires an account binding")
    if not isinstance(candidate.activity, _lib_provider_activity.ActivityWitness):
        raise ValueError("a current candidate requires its activity witness")
    document = {
        "schemaVersion": _candidates.SCHEMA_VERSION,
        "protocolGeneration": _candidates.PROTOCOL_GENERATION,
        "binding": candidate.binding.document(),
        "receivedAt": candidate.received_at,
        # #797 spec 4.2: the frozen witness travels inside the document, so
        # the intake identity hashes it and every reader revalidates it.
        "activity": _lib_provider_activity.witness_to_json(candidate.activity),
    }
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
    """Spool one bound candidate under its own account partition, keyed by
    (protocol generation, account key, contributor token).

    Durable (review finding 825-832-CD5-D01, fix round K3): compaction removes
    the acknowledged intake while its contributors are still eligible, so the
    slot can be the only remaining copy of a contributor's latest committed
    arrival — the membership every later evaluation reads. It passes its
    persistence barrier before the capture returns, so a host crash that
    keeps the compaction keeps the slot too. Its prune stays unsynced: a
    resurrected expired or malformed slot is ignored by membership."""
    if candidate.binding is None:
        raise ValueError("a current candidate requires an account binding")
    _atomic_write_json(
        _candidate_path(candidate.token, account_key=candidate.binding.account_key),
        _candidate_document(candidate),
        durable=True,
    )


def _parse_candidate(raw: bytes, *, account_key: str, token: str, now_epoch: int):
    """Validate one current candidate document from ``account_key``'s spool.
    Raises ``StateValidationError``/``ValueError`` on any mismatch, including a
    binding naming another account than the partition holding it."""
    import _lib_account_binding

    doc = _candidates._require_object(_candidates._load_json_document(
        raw, maximum_bytes=_candidates.CANDIDATE_DOCUMENT_MAX_BYTES), "candidate")
    if doc.pop("protocolGeneration", None) != _candidates.PROTOCOL_GENERATION:
        raise _candidates.StateValidationError("not a current candidate")
    binding = _lib_account_binding.load(doc.pop("binding", None))
    if binding.account_key != account_key:
        raise _candidates.StateValidationError("cross-account candidate")
    if "activity" not in doc:
        raise _candidates.StateValidationError("a current candidate requires its activity witness")
    activity = _candidates.validate_activity(doc.pop("activity"))
    candidate = _candidates.validate_candidate_document(
        doc,
        now_epoch=now_epoch,
        reset_is_plausible=lambda axis, epoch: _statusline_reset_is_plausible(
            axis, epoch, now_epoch),
        token=token,
    )
    return dataclasses.replace(candidate, binding=binding, activity=activity)


def _read_account_candidates(account_key: str, *, now_epoch: int, prune: bool):
    """Active candidates of exactly one account partition. ``prune`` deletes
    malformed or expired final entries of THIS partition only (M18)."""
    try:
        entries = tuple(_candidate_dir(account_key).iterdir())
    except OSError:
        return ()
    result = []
    for path in entries:
        # Match the complete final grammar before touching an entry.  In-flight
        # peer temps are deliberately neither read nor pruned.
        if not _CANDIDATE_FINAL_RE.fullmatch(path.name):
            continue
        try:
            candidate = _parse_candidate(
                path.read_bytes(), account_key=account_key, token=path.stem,
                now_epoch=now_epoch)
        except (OSError, ValueError, TypeError, KeyError):
            if prune:
                try:
                    path.unlink()
                except OSError:
                    pass
            continue
        if not (-5 <= now_epoch - candidate.received_at
                < _cctally_core.STATUSLINE_CANDIDATE_TTL_SECONDS):
            if prune:
                try:
                    path.unlink()
                except OSError:
                    pass
            continue
        result.append(candidate)
    return tuple(sorted(result, key=lambda c: c.token))


def _load_candidate_spool(*, now_epoch: int) -> tuple["_candidates.Candidate", ...]:
    """Every active bound candidate, across partitions in bytewise account
    order. The reducer loader owns best-effort cleanup of each partition's own
    malformed/expired entries — under that partition's intake leaf lock, so a
    prune can never remove a slot a capture installs between its read and
    its unlink."""
    result = []
    for account_key in _account_keys_on_disk():
        with _IntakeLock(account_key):
            result.extend(_read_account_candidates(
                account_key, now_epoch=now_epoch, prune=True))
    return tuple(result)


def _scan_active_candidate_spool(*, now_epoch: int) -> tuple["_candidates.Candidate", ...]:
    """Read valid active candidates without pruning or otherwise mutating.

    `doctor` needs a truthful active-count signal but retains its global
    read-only contract.  In contrast, the reducer loader above owns best-effort
    cleanup of malformed/expired final entries.
    """
    result = []
    for account_key in _account_keys_on_disk():
        result.extend(_read_account_candidates(
            account_key, now_epoch=now_epoch, prune=False))
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


def _statusline_active_account() -> "str | None":
    """The active Claude ``account_key`` to scope the statusline projection to
    (#341, spec §3 write-path semantics — statusline resolves the active account
    once per read and scopes its reset-aware clamps to it).

    Torn-handling (reviewer-specified): a TORN ``~/.claude.json`` read keeps the
    merged (``None``) behavior — never mis-stamp against a half-written file;
    *identified* returns the real key; *stably-absent* / api-key returns the
    ``unattributed`` sentinel. For a <=1-real-account install every stats row
    defaults to ``unattributed``, so the scoped clamp matches all of them and
    the rendered output stays byte-frozen (R8). Best-effort / never-raise: any
    failure falls back to the merged read."""
    try:
        ident = _cctally_core._resolve_active_claude_identity()
        return None if ident.get("status") == "torn" else ident.get("account_key")
    except Exception:
        return None


#: How far past the later of the evaluation's instant and the wall clock a
#: five-hour reset may lie and still be admitted (the future band).
_FIVE_HOUR_FUTURE_BAND_SECONDS = 6 * 3600


def _five_hour_reset_admitted(raw: int, instant: int, now_epoch: int) -> bool:
    """The five-hour plausibility filter of a projection read for an
    evaluation at ``instant`` (#825/#832 review finding P1-2, Codex
    fix-round-4 Q2). The expiry grace is judged at the evaluation's OWN
    instant, so a recovery after the reset plus the grace cannot erase the
    baseline that evaluation saw. The future band is the later of that
    instant and the wall clock: a window recorded after the evaluation is
    real, newer truth an old reading must not regress."""
    return (instant - _FIVE_HOUR_EXPIRY_GRACE_SECONDS <= raw
            <= max(instant, now_epoch) + _FIVE_HOUR_FUTURE_BAND_SECONDS)


def _read_db_projection_at(*, account_key: str, at: "int | None",
                           stale: "list | None" = None):
    """`_read_db_projection_once` plus the inclusive range of evaluation
    instants for which the same read yields the same projection: only the
    five-hour expiry grace depends on the instant, so the projection is
    identical for every instant that admits exactly the same rows (Q2: a
    classification may serve another evaluation only within that range).

    The third value is the range a LATER render may reduce against this
    projection (P1-2, cycle 3): the same grace boundaries, but not capped at
    this read's wall clock — unless a row lay beyond the future band, whose
    admission moves with the wall clock, when it is the capped range."""
    validity: list = []
    projection = _read_db_projection_once(
        account_key=account_key, at=at, validity=validity, stale=stale)
    return projection, validity[0], validity[1]


def _read_db_projection_once(*, account_key: str, at: "int | None" = None,
                             validity: "list | None" = None,
                             stale: "list | None" = None,
                             ) -> "_candidates.DbProjection":
    """Project the selected weekly and five-hour values of EXACTLY one account
    (#825/#832 spec 4.5), as seen by an evaluation at ``at`` (the wall clock
    when omitted). ``validity``, when given, receives the inclusive range of
    instants this read is identical for (see `_read_db_projection_at`).

    The account is explicit and required: there is no active-account
    resolution and no merged (``None``) projection on this path. Both candidate
    SELECTs, the reset-aware weekly floor and the five-hour reset generation are
    scoped to ``account_key``, so another account's rows or reset events can
    neither become this account's projection nor filter its rows. The returned
    projection names the account it was read for.

    ``stale``, when given, receives the account's `_StaleInputs` (#797 spec
    4.4) read in the SAME snapshot: the weekly window's provider samples,
    credits, accounted value and index identity the refusal facts and the
    receipts are resolved from."""
    if not isinstance(account_key, str) or not account_key:
        raise ValueError("the statusline projection requires an exact account")
    import _cctally_journal

    conn = open_db()
    _acct_pred = " AND account_key = ?"
    _acct_params: tuple = (account_key,)
    try:
        # #825/#832 spec 6: every projection value and the reset identity used
        # by reduction, retry and publication come from ONE snapshot proven to
        # be the current interpretation. The read transaction ends when the
        # connection closes below.
        conn.execute("BEGIN")
        import _cctally_store
        _cctally_store.require_current_stats_identity(conn)
        # The reductions below choose the canonical window, then the latest
        # (capture instant, id) explicitly; SQL encounter order is irrelevant.
        # Ordering every retained row here would add two history-sized sorts.
        # `weekly_observation_held = 0` (#769 S11, #824): the WEEKLY half of the
        # projection. The rendered percentage and the capture instant that
        # decides which candidate wins both come from the row selected here, so
        # a held row would project an older reading as this tick's observation.
        weekly_rows = conn.execute(
            "SELECT id, weekly_percent, week_start_date, week_start_at, week_end_at, "
            "       captured_at_utc, source "
            "FROM weekly_usage_snapshots "
            "WHERE weekly_percent IS NOT NULL AND week_end_at IS NOT NULL"
            "  AND weekly_observation_held = 0"
            + _acct_pred,
            _acct_params,
        ).fetchall()
        # HELD-INCLUSIVE, deliberately: the FIVE-HOUR half. A held row exists
        # because its five-hour reading grew, so dropping it here would hold
        # the status line's five-hour percentage at the last unclamped tick.
        five_rows = conn.execute(
            "SELECT id, five_hour_percent, five_hour_resets_at, five_hour_window_key, "
            "       captured_at_utc, source "
            "FROM weekly_usage_snapshots "
            "WHERE five_hour_percent IS NOT NULL AND five_hour_resets_at IS NOT NULL"
            + _acct_pred,
            _acct_params,
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
        weekly_retired = None
        weekly_selected = weekly_floor = reference = None
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
            # #825/#832 spec 4.7 revision 6: the floor and the row
            # eligibility the record fold uses, on this snapshot.
            weekly_floor = _cctally_journal.resolve_weekly_axis_floor(
                conn,
                account_key=account_key,
                week_start_date=str(reference["week_start_date"]),
                week_start_at=str(week_start_at),
                week_end_at=str(week_end_at),
            )
            floor_epoch = weekly_floor.floor_epoch or 0
            eligible = [
                row for row in rows
                if _cctally_journal.captured_at_or_after_floor(
                    row["captured_at_utc"], weekly_floor)
            ]
            if eligible:
                weekly = max(
                    eligible,
                    key=lambda row: (_epoch_from_iso(row["captured_at_utc"]), int(row["id"])),
                )
                weekly_selected = weekly
                raw = _epoch_from_iso(weekly["week_end_at"])
                weekly_projection = _candidates.AxisProjection(
                    float(weekly["weekly_percent"]), raw, canonical,
                    _epoch_from_iso(weekly["captured_at_utc"]),
                    str(weekly["source"] or "statusline"), floor_epoch,
                )
            else:
                weekly_retired = _weekly_floor_retired(
                    conn, account_key, rows=rows, canonical=canonical,
                    reference=reference, floor=weekly_floor)

        now_epoch = int(time.time())
        instant = now_epoch if at is None else int(at)
        five_groups: dict[int, list] = {}
        # Every row's grace boundary (its raw reset plus the grace): a row is
        # admitted for an instant at or before it. The projection is the same
        # for every instant no boundary separates from this one.
        below, above = None, None
        beyond_future_band = False
        # Spec 4.3 revision 5 (design-fix-round-5 Q1): why rows left the
        # five-hour projection. Only expiry at this instant is evidence of a
        # predecessor; a malformed row or a future-band rejection is not.
        five_unexplained = False
        expired_predecessor = None
        # Fix round K12 (K11R-10): when wall time alone admits the earliest
        # row the future band rejected (its reset minus the band).
        five_admissible_at = None
        for row in five_rows:
            try:
                raw = _epoch_from_iso(row["five_hour_resets_at"])
            except (TypeError, ValueError):
                five_unexplained = True
                continue
            boundary = raw + _FIVE_HOUR_EXPIRY_GRACE_SECONDS
            if boundary < instant:
                below = boundary if below is None else max(below, boundary)
            else:
                above = boundary if above is None else min(above, boundary)
            stored_key = row["five_hour_window_key"]
            canonical = (
                int(stored_key)
                if stored_key is not None
                else _cctally()._canonical_5h_window_key(raw)
            )
            if not _five_hour_reset_admitted(raw, instant, now_epoch):
                if boundary >= instant:
                    beyond_future_band = True
                    five_unexplained = True
                    admits = raw - _FIVE_HOUR_FUTURE_BAND_SECONDS
                    if five_admissible_at is None or admits < five_admissible_at:
                        five_admissible_at = admits
                elif expired_predecessor is None or canonical > expired_predecessor[0]:
                    expired_predecessor = (canonical, raw)
                continue
            five_groups.setdefault(canonical, []).append(row)

        five_projection = None
        five_retired = None
        if five_groups:
            canonical = max(five_groups)
            # #832: the exact account's event, by logical order. A database
            # error propagates (the projection is then unavailable), never
            # "no event". The generation is logical, so a rebuild that
            # renumbers rows cannot reopen a spent retry attempt.
            # Spec 4.7 revision 6: the same resolution and row eligibility
            # the record fold applies, so the fold, this projection and the
            # retry identity select one logical floor.
            five_floor = _cctally_journal.resolve_five_hour_axis_floor(
                conn, account_key=account_key, canonical_window_key=canonical)
            generation = five_floor.logical_generation
            eligible = [
                row for row in five_groups[canonical]
                if _cctally_journal.captured_at_or_after_floor(
                    row["captured_at_utc"], five_floor)
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
                    str(five["source"] or "statusline"), generation,
                )
            elif not five_unexplained:
                five_retired = _five_hour_floor_retired(
                    conn, account_key, rows=five_groups[canonical],
                    canonical=canonical, floor=five_floor)
        # Fix round K14c: the end bound of every weekly predecessor. Fix
        # round K15 (K14R-3): the bound is evidence whatever the instant. A
        # reading whose normalized weekly end is strictly greater than the
        # bound lies in a later window, even when it was captured before the
        # bound (a date-only row's two days of slack); one at or below the
        # bound stays unknown (`classify_absent_axis`). The evidence no longer
        # changes where the instant crosses the bound, so the bound does not
        # limit the read's validity.
        weekly_bound = (None if weekly_groups
                        else _weekly_predecessor_bound(conn, account_key))
        evidence = _projection_evidence(
            conn, account_key,
            weekly=weekly_projection, weekly_grouped=bool(weekly_groups),
            five=five_projection, five_grouped=bool(five_groups),
            five_rows=len(five_rows), five_unexplained=five_unexplained,
            expired_predecessor=expired_predecessor,
            five_admissible_at=five_admissible_at,
            weekly_retired=weekly_retired, five_retired=five_retired,
            weekly_bound=weekly_bound, instant=instant)
        if stale is not None:
            stale.append(_read_stale_inputs(
                conn, account_key, groups=weekly_groups, selected=weekly_selected,
                projection=weekly_projection, floor=weekly_floor,
                reference=reference,
                five_window=max(five_groups) if five_groups else None))
    finally:
        conn.close()
    if validity is not None:
        if instant > now_epoch:
            # The future band moves with an instant past the wall clock.
            validity.append((instant, instant))
        else:
            validity.append((-math.inf if below is None else below + 1,
                             min(now_epoch, math.inf if above is None else above)))
        # A later render's range: uncapped by this read's wall clock unless
        # a row lay beyond the future band (whose admission moves with it).
        validity.append(validity[0] if instant > now_epoch or beyond_future_band
                        else (validity[0][0], math.inf if above is None else above))
    return _candidates.DbProjection(
        five_projection, weekly_projection, account_key=account_key,
        evidence=evidence)


@dataclasses.dataclass(frozen=True)
class _StaleInputs:
    """What #797 spec 4.4 resolves for one account from a classification's
    snapshot: the latest weekly window, its accounted value
    (`_lib_stale_proof.Accounted`, None without a window), its credits, its
    admitted provider samples requested after ``samples_after`` (None: not
    read, because no member or receipt needed them), whether the sample
    projection covers the account, the stats index identity receipts are
    bound to, and the live support identities.

    The five-hour fields (Task 5) are the latest five-hour window's canonical
    key, its accounted value (the `statusline` source's detector baseline
    under the current credit generation, D11; None without one), its
    credits resolved through their causal facts, and its admitted provider
    samples requested after ``samples_after``."""

    window: "int | None"
    accounted: object
    credits: tuple
    samples: tuple
    samples_after: "str | None"
    samples_covered: bool
    index_identity: str
    supports: frozenset
    five_window: "int | None" = None
    five_accounted: object = None
    five_credits: tuple = ()
    five_samples: tuple = ()


#: #797 spec 4.4: ``(samples_after, receipts_held)`` for the partition the
#: detached reducer is working on — the earliest provider-activity instant
#: any of its members or receipts can need a sample after, and whether it
#: holds receipts (a refusal is then possible, so an uncovered sample
#: projection makes classification unavailable, never "no proof needed").
_STALE_SCOPE = contextvars.ContextVar("statusline_stale_scope", default=(None, False))


def _stale_index_identity(conn) -> str:
    """The stats index identity receipts are bound to: its epoch and its
    publication stamp, which every rebuild publication rewrites (an index
    never rebuilt has none). A rebuild, re-upgrade or epoch change gives a
    different identity and so invalidates every receipt."""
    epoch = int(conn.execute("PRAGMA user_version").fetchone()[0])
    try:
        stamp = sorted([str(v) for v in row] for row in conn.execute(
            "SELECT record_path, started_at_utc, stamped_at_utc "
            "FROM stats_publication_stamp"))
    except sqlite3.Error:
        stamp = None
    return hashlib.sha256(json.dumps(
        [epoch, stamp], separators=(",", ":")).encode("utf-8")).hexdigest()


def _weekly_window_of(value: "str | None") -> "int | None":
    try:
        raw = _epoch_from_iso(value)
    except (TypeError, ValueError):
        return None
    return int(_cctally_core._normalize_week_boundary_dt(
        dt.datetime.fromtimestamp(raw, tz=dt.timezone.utc)).timestamp())


def _credit_cause(conn, account_key: str, origin: "str | None") -> tuple:
    """A weekly credit's causal low (spec 4.4): its originating (dropping or
    first-zero) observation. A provider sample carries its request instant;
    an api reading without one is unordered; a status-line reading survives
    receipts (R15); a missing origin is legacy; anything else unknown."""
    if origin is None:
        return "legacy", None
    try:
        row = conn.execute(
            "SELECT requested_at_utc FROM claude_oauth_samples "
            "WHERE account_key = ? AND observation_id = ?",
            (account_key, origin)).fetchone()
    except sqlite3.Error:
        row = None
    if row is not None:
        return "provider", str(row[0])
    # A journalled snapshot's identity is ``"sa:" + <raw observation id>``.
    row = conn.execute(
        "SELECT source FROM weekly_usage_snapshots "
        "WHERE account_key = ? AND journal_id = ?",
        (account_key, "sa:" + origin)).fetchone()
    if row is None:
        return "unknown", None
    if row[0] == "statusline":
        return "statusline", None
    if row[0] == "api":
        return "provider", None
    return "unknown", None


def _window_credits(conn, account_key: str, window: int, reference) -> tuple:
    """Every credit of the account's weekly ``window``, resolved through its
    causal observation: the window's in-place reset events and the manual
    credit floors of its week."""
    import _lib_journal
    import _lib_stale_proof

    credits = []
    for row in conn.execute(
            "SELECT detected_at_utc, old_week_end_at, new_week_end_at, "
            "effective_reset_at_utc, observed_pre_credit_pct, origin_observation_id "
            "FROM week_reset_events WHERE account_key = ?", (account_key,)):
        if _weekly_window_of(row[2]) != window:
            continue
        kind, requested = _credit_cause(conn, account_key, row[5])
        credits.append(_lib_stale_proof.CreditTransition(
            axis="sevenDay",
            event_id=_lib_journal.evt_id("wr", *_lib_journal.week_reset_identity_parts(
                account_key, row[1], row[2], row[5])),
            event_revision=hashlib.sha256(json.dumps(
                [None if v is None else str(v) for v in row],
                separators=(",", ":")).encode("utf-8")).hexdigest(),
            causal_kind=kind, requested_at_utc=requested))
    if reference is not None:
        week = str(reference["week_start_date"])
        for row in conn.execute(
                "SELECT effective_at_utc, observed_pre_credit_pct, applied_at_utc "
                "FROM weekly_credit_floors WHERE account_key = ? AND week_start_date = ?",
                (account_key, week)):
            credits.append(_lib_stale_proof.CreditTransition(
                axis="sevenDay", event_id=f"wcf:{account_key}:{week}:{row[0]}",
                event_revision=hashlib.sha256(json.dumps(
                    [str(v) for v in row], separators=(",", ":")).encode(
                    "utf-8")).hexdigest(),
                causal_kind="manual", requested_at_utc=None))
    return tuple(sorted(credits, key=lambda c: c.event_id))


def _five_hour_accounted(conn, account_key: str, window: int):
    """The five-hour accounted value of the window (#797 spec 4.4 (i), D11):
    the `statusline` source's detector baseline, only when it was
    established under the window's current five-hour credit generation. A
    baseline of an older generation (a pre-credit high), one the historical
    detector wrote, or a missing state earns nothing."""
    import _cctally_journal
    import _cctally_record
    import _lib_stale_proof

    generation = _cctally_record.five_hour_credit_generation(
        conn, window, account_key=account_key)
    try:
        row = conn.execute(
            "SELECT baseline_pct, baseline_observation_id, baseline_generation "
            "FROM five_hour_credit_confirmation_state "
            "WHERE account_key = ? AND five_hour_window_key = ? AND source = ?",
            (account_key, int(window), "statusline")).fetchone()
    except sqlite3.Error:
        row = None
    current = row is not None and row[2] is not None and row[2] == generation
    return _lib_stale_proof.Accounted(
        account_key=account_key, axis="fiveHour", window=int(window),
        percent=float(row[0]) if current else None,
        support_id=row[1] if current else None,
        support_hash=hashlib.sha256(json.dumps(
            [float(row[0]), row[1], row[2]], separators=(",", ":")).encode(
            "utf-8")).hexdigest() if current else None,
        floor_epoch=None, credits=(), coverage=None,
        detector_policy=_cctally_journal.DETECTOR_POLICY)


def _five_hour_sample_facts(conn, account_key: str, observation_id) -> tuple:
    """``(q, raw five-hour percent)`` of an admitted provider sample, or
    ``(None, None)`` when the observation is not one."""
    import _cctally_oauth_samples

    sample = _cctally_oauth_samples.sample_for_observation(
        conn, account_key=account_key, observation_id=observation_id)
    if sample is None:
        return None, None
    return (sample.requested_at_utc,
            None if sample.five_hour_percent is None else float(sample.five_hour_percent))


def _five_hour_window_credits(conn, account_key: str, window: int) -> tuple:
    """Every five-hour credit of the window, resolved through its retained
    causal facts (#797 spec 4.4, 4.9): a credit the `statusline` source
    confirmed is a status-line credit; one the `api` source confirmed carries
    its baseline observation's request instant and RAW five-hour value and
    its ARMING observation's request instant, each read from the admitted
    provider sample of that exact observation (never a stored, MAX-clamped
    value, and never the later confirming observation). A credit without
    facts (a rebuild dropped them, or another source) is unknown."""
    import _cctally_record
    import _lib_journal
    import _lib_stale_proof

    try:
        facts = {}
        for row in conn.execute(
                "SELECT source, pending_observation_id, baseline_observation_id, "
                "       pending_generation "
                "FROM five_hour_credit_confirmation_state "
                "WHERE account_key = ? AND five_hour_window_key = ? "
                "  AND source LIKE '#credit|%'", (account_key, int(window))):
            facts[row[3]] = (str(row[0]).split("|", 2)[-1], row[1], row[2])
    except sqlite3.Error:
        facts = {}
    credits = []
    for row in conn.execute(
            "SELECT detected_at_utc, prior_percent, post_percent, effective_reset_at_utc "
            "FROM five_hour_reset_events WHERE account_key = ? AND five_hour_window_key = ?",
            (account_key, int(window))):
        event_id = _lib_journal.evt_id("fhc", account_key, int(window), str(row[3]))
        revision = _cctally_record._five_hour_credit_revision(row)
        fact = facts.get(f"{event_id}@{revision}")
        kind, arming, baseline, baseline_percent = "unknown", None, None, None
        if fact is not None and fact[0] == "statusline":
            kind = "statusline"
        elif fact is not None and fact[0] == "api":
            kind = "provider"
            arming, _ = _five_hour_sample_facts(conn, account_key, fact[1])
            baseline, baseline_percent = _five_hour_sample_facts(
                conn, account_key, fact[2])
        credits.append(_lib_stale_proof.CreditTransition(
            axis="fiveHour", event_id=event_id, event_revision=revision,
            causal_kind=kind, requested_at_utc=arming,
            baseline_requested_at_utc=baseline, baseline_percent=baseline_percent,
            arming_requested_at_utc=arming))
    return tuple(sorted(credits, key=lambda c: c.event_id))


def _read_stale_inputs(conn, account_key: str, *, groups, selected, projection,
                       floor, reference, five_window=None) -> _StaleInputs:
    """The account's `_StaleInputs` on the caller's snapshot (#797 spec 4.4).
    Provider samples are read only when the partition's members or receipts
    need them (`_STALE_SCOPE`); an uncovered sample projection with receipts
    held raises `ProjectionUnavailable`, so the classification is
    unavailable rather than silently "no proof"."""
    import _cctally_oauth_samples
    import _lib_stale_proof

    identity = _stale_index_identity(conn)
    if not groups and five_window is None:
        return _StaleInputs(None, None, (), (), None, True, identity, frozenset())
    after, receipts_held = _STALE_SCOPE.get()
    covered = _cctally_oauth_samples.projection_covered(conn, account_key=account_key)
    if not covered and receipts_held:
        raise _cctally_oauth_samples.ProjectionUnavailable(
            "the provider sample projection does not cover this account")
    five_accounted, five_credits, five_samples = _five_hour_stale_inputs(
        conn, account_key, five_window, after=after if covered else None)
    if not groups:
        # Review F-I5-1: the five-hour refusal inputs never depend on a
        # weekly reading. The weekly fields keep the no-window shape.
        return _StaleInputs(None, None, (), (), None, True, identity, frozenset(),
                            five_window, five_accounted, five_credits, five_samples)
    window = max(groups)
    supports = {f"row:{int(row['id'])}" for row in groups[window]}
    samples = ()
    if after is not None and covered:
        samples = _cctally_oauth_samples.read_samples(
            conn, account_key=account_key, axis="weekly", window=window,
            requested_after_utc=after)
    supports |= {sample.observation_id for sample in samples}
    support_id = support_hash = None
    if selected is not None:
        support_id = f"row:{int(selected['id'])}"
        support_hash = hashlib.sha256(json.dumps(
            [int(selected["id"]), float(selected["weekly_percent"]),
             str(selected["captured_at_utc"]), str(selected["source"])],
            separators=(",", ":")).encode("utf-8")).hexdigest()
    accounted = _lib_stale_proof.Accounted(
        account_key=account_key, axis="sevenDay", window=window,
        percent=None if projection is None else float(projection.percent),
        support_id=support_id, support_hash=support_hash,
        floor_epoch=None if floor is None else floor.floor_epoch,
        credits=_window_credits(conn, account_key, window, reference),
        coverage=None)
    return _StaleInputs(window, accounted, accounted.credits, tuple(samples),
                        after if covered else None, covered, identity,
                        frozenset(supports), five_window, five_accounted,
                        five_credits, five_samples)


def _five_hour_stale_inputs(conn, account_key: str, five_window, *, after) -> tuple:
    """``(accounted, credits, samples)`` of the five-hour window
    ``five_window`` (``(None, (), ())`` without one), its admitted provider
    samples read only when ``after`` is given (a covered projection)."""
    import _cctally_oauth_samples

    if five_window is None:
        return None, (), ()
    credits = _five_hour_window_credits(conn, account_key, five_window)
    accounted = dataclasses.replace(
        _five_hour_accounted(conn, account_key, five_window), credits=credits)
    samples = ()
    if after is not None:
        samples = _cctally_oauth_samples.read_samples(
            conn, account_key=account_key, axis="five_hour",
            window=int(five_window), requested_after_utc=after)
    return accounted, credits, tuple(samples)


def _account_rows(conn, table: str, account_key: str, extra: str = "") -> int:
    """How many rows of ``table`` belong to ``account_key`` (``extra`` a
    further literal predicate), in the caller's snapshot."""
    return int(conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE account_key = ?{extra}",
        (account_key,)).fetchone()[0])


def _capture_epoch(value) -> "int | None":
    """A retained capture instant as epoch seconds, or None when it does not
    parse (the eligibility rule `captured_at_or_after_floor` applies)."""
    try:
        return _epoch_from_iso(value)
    except (TypeError, ValueError):
        return None


def _weekly_floor_retired(conn, account_key: str, *, rows, canonical: int,
                          reference, floor) -> "_candidates.AxisEvidence | None":
    """The weekly `floor_retired` evidence of the projected group, or None
    (#825/#832 spec 4.3 revision 6, *Floor retired*; Codex
    design-fix-round-6 Q2), read in the projection's own snapshot.

    The group (its normalized weekly end) is known by construction. The
    proof needs the account's own effective weekly floor for that week
    (`resolve_weekly_axis_floor`, whose scope admits only effective,
    admitted events: an excluded, withheld, superseded or uncommitted floor
    is never materialized there); every accepted row of the group of one
    week-start date, with a capture that parses and lies before the floor;
    and no accepted date-only row of that week captured at or after the
    floor, which would be an observation the floor did not retire."""
    if floor.floor_epoch is None:
        return None
    start_date = str(reference["week_start_date"])
    for row in rows:
        captured = _capture_epoch(row["captured_at_utc"])
        if (captured is None or captured >= floor.floor_epoch
                or str(row["week_start_date"]) != start_date):
            return None
    unexplained = conn.execute(
        "SELECT COUNT(*) FROM weekly_usage_snapshots "
        "WHERE account_key = ? AND week_start_date = ? "
        "  AND weekly_observation_held = 0 AND weekly_percent IS NOT NULL "
        "  AND week_end_at IS NULL "
        "  AND (unixepoch(captured_at_utc) IS NULL "
        "       OR unixepoch(captured_at_utc) >= ?)",
        (account_key, start_date, int(floor.floor_epoch))).fetchone()[0]
    if unexplained:
        return None
    if not _weekly_retirement_proven(
            conn, account_key, canonical=int(canonical), start_date=start_date,
            floor=floor):
        return None
    return _candidates.AxisEvidence(
        _candidates.EVIDENCE_FLOOR_RETIRED, account_key=account_key,
        axis="sevenDay", group_key=int(canonical),
        floor_epoch=int(floor.floor_epoch),
        floor_identity=str(floor.floor_at_utc),
        anchor_raw=_epoch_from_iso(reference["week_end_at"]))


def _five_hour_floor_retired(conn, account_key: str, *, rows, canonical: int,
                             floor) -> "_candidates.AxisEvidence | None":
    """The five-hour `floor_retired` evidence of the projected window, or
    None (spec 4.3 revision 6): the exact account's logically ordered reset
    event of that window is its floor, every admitted row of the window
    carries its persisted account-local key (the anchor is known) and a
    capture that parses and lies before the floor. The caller has already
    excluded a malformed or future-band row anywhere on the axis, and
    `_projection_evidence` a half-filled one. The anchor evidence is the
    window's first raw reset, by capture. Spec 4.3 revision 7: the retained
    truth must also prove the retirement (`_five_hour_retirement_proven`)."""
    if floor.floor_epoch is None:
        return None
    first = None
    for row in rows:
        captured = _capture_epoch(row["captured_at_utc"])
        if (captured is None or captured >= floor.floor_epoch
                or row["five_hour_window_key"] is None):
            return None
        order = (captured, int(row["id"]))
        if first is None or order < first[0]:
            first = (order, row)
    if not _five_hour_retirement_proven(
            conn, account_key, canonical=int(canonical), floor=floor):
        return None
    return _candidates.AxisEvidence(
        _candidates.EVIDENCE_FLOOR_RETIRED, account_key=account_key,
        axis="fiveHour", group_key=int(canonical),
        floor_epoch=int(floor.floor_epoch),
        floor_identity=str(floor.logical_generation),
        anchor_raw=_epoch_from_iso(first[1]["five_hour_resets_at"]))


# ── Retirement proof (#825/#832 spec 4.3 revision 7, fix round K16b) ──────
#
# `floor_retired` says an admitted floor of the axis itself explains why the
# projected group holds no accepted observation at or after the floor. The
# materialized rows alone cannot say that: a row a different axis's cleanup
# removed (the previous release's whole-row suppression, or any damaged
# materialization) looks exactly like one this floor retired. The proof is
# therefore read from persisted records in the same snapshot: every effective
# admitted `snapshot_accept` fact of the account and group (`sa:` decisions
# and `b:` bootstrap facts) at or after the floor must be materialized, or be
# named by a stale-replica suppression of the SAME axis associated with that
# floor. A deletion tombstone alone proves nothing, a weekly suppression never
# retires five-hour evidence and a five-hour suppression never retires weekly
# evidence. A fact this does not explain fails the proof, so the axis stays
# unknown until a reconstruction from retained truth (a rebuild, whose
# publication wakes the head) restores it and the head is reclassified.

_RETAINED_SNAPSHOT_FACTS_SQL = (
    "SELECT event_id, event_json FROM journal_effective_events "
    "WHERE status = 'active' AND event_json IS NOT NULL "
    "  AND ((event_id >= 'sa:' AND event_id < 'sa;') "
    "    OR (event_id >= 'b:weekly_usage_snapshots:' "
    "        AND event_id < 'b:weekly_usage_snapshots;')) "
    "  AND COALESCE(json_extract(event_json, '$.payload.account_key'), "
    "               'unattributed') = ?"
)


def _retained_snapshot_facts(conn, account_key: str):
    """``(event_id, payload)`` of every effective admitted snapshot fact of
    the account, or None when one does not decode (malformed evidence, which
    never establishes a proof). A store without the effective-event index
    has no retained fact to account for."""
    try:
        rows = conn.execute(_RETAINED_SNAPSHOT_FACTS_SQL, (account_key,)).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return []
        if "malformed JSON" in str(exc):
            # The account filter's `json_extract` met an undecodable fact:
            # malformed evidence, never a proof.
            return None
        raise
    facts = []
    for event_id, event_json in rows:
        try:
            record = json.loads(event_json)
        except (TypeError, ValueError):
            return None
        payload = record.get("payload") if isinstance(record, dict) else None
        if not isinstance(payload, dict):
            return None
        if payload.get("kind") != "snapshot_accept":
            continue
        facts.append((str(event_id), payload))
    return facts


def _suppression_of(conn, event_id) -> "set[str] | None":
    """The logical ids the effective event ``event_id`` suppresses (empty
    when it has no list or is not effective), or None when its list does not
    decode."""
    if not event_id:
        return set()
    row = conn.execute(
        "SELECT event_json FROM journal_effective_events "
        "WHERE event_id = ? AND status = 'active'", (str(event_id),)).fetchone()
    if row is None or row[0] is None:
        return set()
    try:
        payload = (json.loads(row[0]) or {}).get("payload") or {}
    except (TypeError, ValueError, AttributeError):
        return None
    if (payload.get("suppression_table", "weekly_usage_snapshots")
            != "weekly_usage_snapshots"):
        return set()
    listed = payload.get("suppression") or []
    if not isinstance(listed, list) or not all(isinstance(i, str) for i in listed):
        return None
    return set(listed)


def _materialized(conn, event_id: str, predicate: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM weekly_usage_snapshots WHERE journal_id = ?" + predicate
        + " LIMIT 1", (event_id,)).fetchone() is not None


def _five_hour_retirement_proven(conn, account_key: str, *, canonical: int,
                                 floor) -> bool:
    """Whether the retained truth explains a five-hour group with no row at
    or after its floor (see the section comment above). The same-axis
    suppression associated with the floor is the floor event's own: the
    `five_hour_credit` whose stale-replica band starts at it."""
    floor_epoch = int(floor.floor_epoch)
    event = getattr(floor, "event", None)
    named: "set[str] | None" = set()
    row_id = getattr(event, "row_id", 0)
    if row_id:
        row = conn.execute(
            "SELECT journal_id FROM five_hour_reset_events WHERE id = ?",
            (int(row_id),)).fetchone()
        named = _suppression_of(conn, row[0] if row is not None else None)
    if named is None:
        return False
    facts = _retained_snapshot_facts(conn, account_key)
    if facts is None:
        return False
    for event_id, payload in facts:
        percent = payload.get("five_hour_percent")
        resets = payload.get("five_hour_resets_at")
        if percent is None or resets is None:
            continue
        key = payload.get("five_hour_window_key")
        try:
            group = (int(key) if key is not None else int(
                _cctally()._canonical_5h_window_key(_epoch_from_iso(resets))))
        except (TypeError, ValueError):
            return False
        if group != canonical:
            continue
        captured = _capture_epoch(payload.get("captured_at_utc"))
        if captured is None:
            return False
        if captured < floor_epoch:
            continue
        if _materialized(conn, event_id, " AND five_hour_percent IS NOT NULL"):
            # Accepted post-floor evidence remains: nothing is retired.
            return False
        if event_id not in named:
            return False
    return True


def _weekly_retirement_proven(conn, account_key: str, *, canonical: int,
                              start_date: str, floor) -> bool:
    """Whether the retained truth explains a weekly group with no accepted
    row at or after its floor (see the section comment above). The same-axis
    suppressions associated with the floor are those of the weekly credits
    effective exactly at it: an in-place `week_reset` (with the effects-only
    recovery event of its originating observation) and a manual credit's
    `weekly_credit_effects`."""
    floor_epoch = int(floor.floor_epoch)
    named: set = set()
    sources: list = []
    for journal_id, origin in conn.execute(
            "SELECT journal_id, origin_observation_id FROM week_reset_events "
            "WHERE account_key = ? "
            "  AND unixepoch(effective_reset_at_utc) = ?",
            (account_key, floor_epoch)).fetchall():
        sources.append(journal_id)
        if origin:
            sources.extend(r[0] for r in conn.execute(
                "SELECT event_id FROM journal_effective_events "
                "WHERE event_id >= ? AND event_id < ?",
                (f"wce:replay:{origin}:", f"wce:replay:{origin};")).fetchall())
    for (journal_id,) in conn.execute(
            "SELECT journal_id FROM weekly_credit_floors "
            "WHERE account_key = ? AND week_start_date = ? "
            "  AND unixepoch(effective_at_utc) = ?",
            (account_key, start_date, floor_epoch)).fetchall():
        if journal_id:
            sources.append(f"wce:{journal_id}")
    for source in sources:
        listed = _suppression_of(conn, source)
        if listed is None:
            return False
        named |= listed
    facts = _retained_snapshot_facts(conn, account_key)
    if facts is None:
        return False
    for event_id, payload in facts:
        if payload.get("weekly_observation_held") not in (None, 0):
            continue
        if payload.get("weekly_percent") is None:
            continue
        end = payload.get("week_end_at")
        if end:
            try:
                group = int(_cctally_core._normalize_week_boundary_dt(
                    dt.datetime.fromtimestamp(_epoch_from_iso(end),
                                              tz=dt.timezone.utc)).timestamp())
            except (TypeError, ValueError, OverflowError):
                return False
            if group != canonical:
                continue
        elif str(payload.get("week_start_date")) != start_date:
            continue
        captured = _capture_epoch(payload.get("captured_at_utc"))
        if captured is None:
            return False
        if captured < floor_epoch:
            continue
        if _materialized(conn, event_id, " AND weekly_observation_held = 0"):
            return False
        if event_id not in named:
            return False
    return True


def _weekly_predecessor_bound(conn, account_key: str) -> "int | None":
    """The end bound of every weekly predecessor of an account whose
    retained weekly rows yield no projected group (fix round K14c, spec 4.3
    *Releases*), or None when the rows cannot fix one: every row is either
    held, carrying its accepted basis's boundary, or date-only, and each of
    them, with each weekly reset and credit floor of the account, provably
    lies in a week ending at or before the bound. A date-only row's bound
    comes from its own dates with two days of slack past the later of its
    end date and a full week after its start, so no hour is invented from
    dates or the configuration. Anything else (an unparseable value, a row
    with no weekly value) proves nothing. `_projection_evidence` turns the
    bound into an expired weekly predecessor whatever the evaluation's
    instant (fix round K15, K14R-3): a reading whose normalized weekly end
    is strictly greater than the bound is a new window, and one at or below
    it stays unknown. A bound is reconstructible, so it is never residual
    11; residual 11 is history inside the reading's own window, whose bound
    the reading's key does not exceed."""
    bound = None

    def widen(value):
        nonlocal bound
        bound = value if bound is None else max(bound, value)

    for row in conn.execute(
            "SELECT week_start_date, week_end_date, week_end_at, "
            "       weekly_percent, weekly_observation_held "
            "FROM weekly_usage_snapshots WHERE account_key = ?",
            (account_key,)):
        if row["weekly_percent"] is None:
            return None
        try:
            if row["week_end_at"] is not None:
                if not row["weekly_observation_held"]:
                    return None
                widen(int(_cctally_core._normalize_week_boundary_dt(
                    dt.datetime.fromtimestamp(
                        _epoch_from_iso(row["week_end_at"]), tz=dt.timezone.utc)
                ).timestamp()))
                continue
            start = dt.date.fromisoformat(str(row["week_start_date"]))
            end = (dt.date.fromisoformat(str(row["week_end_date"]))
                   if row["week_end_date"] else start)
        except (TypeError, ValueError):
            return None
        last = max(end, start + dt.timedelta(days=7)) + dt.timedelta(days=2)
        widen(int(dt.datetime(last.year, last.month, last.day,
                              tzinfo=dt.timezone.utc).timestamp()))
    for (value,) in conn.execute(
            "SELECT effective_reset_at_utc FROM week_reset_events "
            "WHERE account_key = ?", (account_key,)):
        epoch = _capture_epoch(value)
        if epoch is None:
            return None
        widen(epoch)
    for (value,) in conn.execute(
            "SELECT effective_at_utc FROM weekly_credit_floors "
            "WHERE account_key = ?", (account_key,)):
        epoch = _capture_epoch(value)
        if epoch is None:
            return None
        widen(epoch)
    return bound


def _projection_evidence(conn, account_key: str, *, weekly, weekly_grouped: bool,
                         five, five_grouped: bool, five_rows: int,
                         five_unexplained: bool, expired_predecessor,
                         five_admissible_at: "int | None" = None,
                         weekly_retired=None, five_retired=None,
                         weekly_bound: "int | None" = None,
                         instant: "int | None" = None) -> dict:
    """Each axis's classification evidence (#825/#832 spec 4.3 revision 5,
    Codex design-fix-round-5 Q1), from the SAME snapshot the projection was
    read from — never inferred from an absent value.

    The account's raw state is inspected BEFORE the presentation and
    eligibility filters: an axis is proven empty only when the account holds
    no row carrying that axis at all (held, unparseable or half-filled rows
    included) and no reset or credit state of its own; zero ELIGIBLE rows is
    not that proof. A five-hour axis is an expired predecessor only when it
    held rows, every one of them parsed and left the projection solely by
    expiry at the evaluation's instant — no future-band rejection — and no
    window survived to the reset-floor filter. Everything else, reset-floor
    filtering included, is unknown — unless the filtering is an admitted
    floor that retired the whole projected group (``weekly_retired`` /
    ``five_retired``, spec 4.3 revision 6 *Floor retired*). An unknown
    five-hour axis that a row beyond the future band leaves unexplained
    carries the instant wall time alone admits that row
    (``five_admissible_at``, fix round K12). A weekly axis with no projected
    group whose retained rows provably all lie in weeks ending at or before
    one bound carries that bound (``weekly_bound``,
    `_weekly_predecessor_bound`, fix round K14c), whatever the evaluation's
    ``instant`` (fix round K15)."""
    present = _candidates.AxisEvidence(_candidates.EVIDENCE_PRESENT)
    unknown = _candidates.EVIDENCE_UNKNOWN_AXIS
    empty = _candidates.AxisEvidence(_candidates.EVIDENCE_PROVEN_EMPTY)
    evidence = {}
    if weekly is not None:
        evidence["sevenDay"] = present
    elif weekly_grouped:
        evidence["sevenDay"] = weekly_retired or unknown
    else:
        raw_weekly = _account_rows(conn, "weekly_usage_snapshots", account_key)
        resets = (_account_rows(conn, "week_reset_events", account_key)
                  + _account_rows(conn, "weekly_credit_floors", account_key))
        evidence["sevenDay"] = (
            empty if raw_weekly == 0 and resets == 0
            else _candidates.AxisEvidence(
                _candidates.EVIDENCE_EXPIRED_PREDECESSOR,
                predecessor_key=int(weekly_bound))
            if weekly_bound is not None
            else unknown)
    if five is not None:
        evidence["fiveHour"] = present
    elif five_retired is not None and not _account_rows(
            conn, "weekly_usage_snapshots", account_key,
            " AND ((five_hour_percent IS NULL) <> (five_hour_resets_at IS NULL))"):
        evidence["fiveHour"] = five_retired
    elif five_grouped or five_unexplained:
        evidence["fiveHour"] = (
            unknown if five_admissible_at is None else _candidates.AxisEvidence(
                _candidates.EVIDENCE_UNKNOWN,
                admissible_at=int(five_admissible_at)))
    else:
        half_filled = _account_rows(
            conn, "weekly_usage_snapshots", account_key,
            " AND ((five_hour_percent IS NULL) <> (five_hour_resets_at IS NULL))")
        if half_filled:
            evidence["fiveHour"] = unknown
        elif five_rows == 0:
            state = (_account_rows(conn, "five_hour_reset_events", account_key)
                     + _account_rows(conn, "five_hour_blocks", account_key))
            evidence["fiveHour"] = empty if state == 0 else unknown
        elif expired_predecessor is not None:
            evidence["fiveHour"] = _candidates.AxisEvidence(
                _candidates.EVIDENCE_EXPIRED_PREDECESSOR,
                predecessor_key=int(expired_predecessor[0]),
                predecessor_raw=int(expired_predecessor[1]))
        else:
            evidence["fiveHour"] = unknown
    return evidence


def _read_db_projection_stable(
        *, account_key: str, attempts: int = 3,
        require_available: bool = False) -> "_candidates.DbProjection":
    """The account's selected projection from one stable index read.

    A deferred rebuild yields an axis-less projection for display and
    control bookkeeping. A caller deciding whether something is a DROP passes
    ``require_available`` and gets `ProjectionUnavailable` instead: "the index
    is rebuilding" and "the index holds no baseline" are different evidence
    (#825/#832 fix round D, P1-2)."""
    import _cctally_store
    for _ in range(attempts):
        before = _db_file_fingerprint()
        try:
            projection = _read_db_projection_once(account_key=account_key)
        except _cctally().StatsRebuildDeferred as exc:
            if require_available:
                raise ProjectionUnavailable(str(exc)) from None
            return _candidates.DbProjection(
                None, None, db_files=before, account_key=account_key)
        except _cctally_store.StatsIndexNotCurrent:
            # A previous-release generation: withheld, never used. Retried
            # within this invocation's own bound, then withheld (spec 6).
            continue
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
    signature_document = None
    if signature is not None:
        signature_document = {
            "candidateKey": signature.candidate_key,
            "candidatePercent": signature.candidate_percent,
            "dbKey": signature.db_key,
            "dbPercent": signature.db_percent,
            "dbResetGeneration": signature.db_reset_generation,
        }
        if signature.account_key is not None:
            # The bound retry namespace (#825/#832 spec 4.3).
            signature_document.update(
                protocolGeneration=signature.protocol_generation,
                accountKey=signature.account_key,
                axis=signature.axis,
            )
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
        "retrySignature": signature_document,
        "deadlineAt": value.deadline_at,
        "retained": (
            None if value.retained is None else {
                "percent": value.retained.percent,
                "rawResetsAt": value.retained.raw_resets_at,
                "canonicalKey": value.retained.canonical_key,
                "observations": [
                    {
                        "observationId": item.observation_id,
                        "token": item.token,
                        "percent": item.percent,
                        "rawResetsAt": item.raw_resets_at,
                        "receivedAt": item.received_at,
                    }
                    for item in value.retained.observations
                ],
            }
        ),
    }


def _control_document(control: "_candidates.ControlState") -> dict:
    files = control.db_projection.db_files or {"main": None, "wal": None}
    return {
        # Versioned independently of the candidate and tombstone documents, so
        # that a control shape change cannot invalidate the spool documents a
        # peer session running the previous binary is still writing.
        "schemaVersion": _candidates.CONTROL_SCHEMA_VERSION,
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


# ── durable per-account intake (#825/#832 spec 4.3 revision 2, Codex
# design-fix-round-3 Q1) ────────────────────────────────────────────────────
#
# Deferred work lives in an append-only intake beneath the account's own
# directory, `accounts/<key>/intake/<first-sequence>.jsonl` — never in the
# size-limited `transition.json`, and never as a journal record kind. Each
# line is ONE evaluation: the reducer pass's sequence number, its original
# instant and its exact membership, each member carrying the immutable
# arrival (contributor token, the bound candidate document with its binding,
# protocol generation, receipt time and both raw axes) and a distinct intake
# identity over all of it (a token and a receipt second are not enough: two
# readings share a second). An evaluation with no members is logged too
# whenever it can matter to an existing deadline.
#
# Enrollment precedes classification, frozen-plan recovery and anything else
# that can defer, so an expired or overwritten spool entry is never the only
# copy of a reading. The transition records the acknowledgement checkpoint
# (`intakeAcknowledged`, written in the SAME atomic document as the control
# state and its generation) and a frozen plan records the sequence it was
# frozen from (`intakeSequence`). Compaction then removes whole segments of
# the acknowledged prefix and nothing else: each line is self-contained, so
# nothing a later evaluation, a pending low or a frozen plan needs can live in
# a removed segment. Lines, segments and memory are bounded; the total
# unacknowledged backlog is not (spec residual 8) — it grows with outstanding
# work and is never collapsed by equal values, membership or signature.
#
# Capture before the selected-state lock (spec 4.3 revision 3, Codex
# design-fix-round-4 Q4). The RENDER captures every arrival itself, before it
# tries the selected-state lock: under the account's short intake leaf lock
# it appends the arrival together with an actual evaluation — that
# evaluation's instant, a unique sequence number and its complete
# account-local membership — commits it, and only then installs the
# contributor's spool slot. Membership is per contributor: each one's latest
# committed arrival eligible at the instant, the incoming arrival
# substituted for its own contributor exactly once. The slots are a cache of
# those committed arrivals; every append first reinstalls the slots of the
# tail evaluation's members (a crash between a commit and its slot leaves
# only the tail behind), so membership read from the slots is membership from
# committed intake. Render captures and the reducer's own evaluations draw
# from ONE allocator, the tail under the same leaf lock; every intake
# mutation (append, slot install, spool prune, compaction) takes it, and it
# is a LEAF: nothing else is acquired while it is held. Lock order:
# selected-state, then intake; `continuation.lock` is never taken under it.

_INTAKE_SCHEMA_VERSION = 2
#: Version 1 lines carry no `journalAt` (fix round K5): their enrollment
#: point is unknown, so same-second order against them is never established.
_INTAKE_SCHEMA_VERSIONS_READ = (1, 2)
#: One evaluation line. A partition with this many concurrent contributors is
#: far beyond any real install; such a pass fails (retryable, nothing
#: consumed) rather than truncating its membership.
_INTAKE_RECORD_MAX_BYTES = _candidates.CONTROL_DOCUMENT_MAX_BYTES
#: A segment rotates once it reaches this size, so the acknowledged prefix can
#: be compacted while later work is still outstanding.
_INTAKE_SEGMENT_MAX_BYTES = 1024 * 1024
_INTAKE_SEGMENT_RE = re.compile(r"([0-9]{20})\.jsonl\Z")


@dataclasses.dataclass(frozen=True)
class _Evaluation:
    """One logged reducer evaluation: its intake sequence number, its
    original instant and exactly the bound readings active then."""

    sequence: int
    at: int
    members: "tuple[_candidates.Candidate, ...]"
    #: The journal end read just before the line was appended (fix round K5,
    #: 825-832-CD5-D02): every observation that ends at or before it was
    #: appended before this evaluation existed. ``("", 0)`` is an empty
    #: journal; None is unknown (a version 1 line, or an unreadable journal).
    journal_at: "tuple[str, int] | None" = None


def _crash_point(name: str) -> None:
    """Test seam at each durable intake, publication and acknowledgement
    boundary; a no-op in production."""


def _intake_dir(account_key: str) -> pathlib.Path:
    return _account_dir(account_key) / "intake"


def _intake_lock_path(account_key: str) -> pathlib.Path:
    return _account_dir(account_key) / "intake.lock"


#: The accounts whose intake leaf lock this context holds (re-entry is a
#: no-op: a second flock on a new descriptor would deadlock this process).
_INTAKE_HELD = contextvars.ContextVar("statusline_intake_held", default=frozenset())


class _IntakeLock:
    """The short per-account intake LEAF lock (spec 4.3 revision 3): held for
    one capture, enrollment, spool prune or compaction, never while taking
    another lock. A process that dies holding it releases it with the
    descriptor."""

    def __init__(self, account_key: str):
        self._key = _require_account_key(account_key)
        self._fd = -1
        self._token = None

    def __enter__(self):
        if self._key in _INTAKE_HELD.get():
            return self
        _durable_mkdir(_account_dir(self._key))
        fd = os.open(_intake_lock_path(self._key), os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        self._token = _INTAKE_HELD.set(_INTAKE_HELD.get() | {self._key})
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
        _INTAKE_HELD.reset(self._token)
        return False


def _intake_identity(candidate: "_candidates.Candidate") -> str:
    """The distinct identity of one immutable arrival: the contributor token
    and its complete bound document (binding, receipt time, both raw axes).
    Two different readings in one second differ; an identical one is the same
    immutable object. Never a provider-observation identity."""
    return hashlib.sha256(json.dumps(
        {"token": candidate.token, "candidate": _candidate_document(candidate)},
        sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()[:32]


def _membership_evidence(members) -> str:
    """The identity of an evaluation's exact evidence: its members' intake
    identities, order-free. Two evaluations with equal evidence saw the same
    immutable arrivals, whatever their instants."""
    return hashlib.sha256("\n".join(
        sorted(map(_intake_identity, members))).encode("ascii")).hexdigest()[:32]


def _evaluation_line(sequence: int, at: int, members,
                     journal_at: "tuple[str, int] | None" = None) -> bytes:
    document = {
        "schemaVersion": _INTAKE_SCHEMA_VERSION,
        "kind": "evaluation",
        "sequence": int(sequence),
        "at": int(at),
        "journalAt": (None if journal_at is None else
                      {"segment": str(journal_at[0]), "offset": int(journal_at[1])}),
        "members": [
            {"intakeId": _intake_identity(c), "token": c.token,
             "candidate": _candidate_document(c)}
            for c in sorted(members, key=lambda c: c.token)],
    }
    data = json.dumps(document, separators=(",", ":"), allow_nan=False).encode(
        "utf-8") + b"\n"
    if len(data) > _INTAKE_RECORD_MAX_BYTES:
        raise ValueError("an intake evaluation exceeds its record bound")
    return data


def _parse_evaluation(line: bytes, *, account_key: str) -> _Evaluation:
    """Strict: exact keys, increasing tokens, every member a current bound
    candidate of THIS account whose identity re-derives."""
    doc = _candidates._require_object(_candidates._load_json_document(
        line, maximum_bytes=_INTAKE_RECORD_MAX_BYTES), "intake evaluation")
    version = doc.get("schemaVersion")
    keys = {"schemaVersion", "kind", "sequence", "at", "members"}
    _candidates._require_exact_keys(
        doc, keys if version == 1 else keys | {"journalAt"})
    journal_at = None if version == 1 else _journal_point(doc["journalAt"])
    if (version not in _INTAKE_SCHEMA_VERSIONS_READ
            or doc["kind"] != "evaluation"
            or not _candidates._is_nonnegative_int(doc["sequence"])
            or not _candidates._is_nonnegative_int(doc["at"])
            or not isinstance(doc["members"], list)):
        raise _candidates.StateValidationError("invalid intake evaluation")
    members = []
    for item in doc["members"]:
        item = _candidates._require_object(item, "intake member")
        _candidates._require_exact_keys(item, {"intakeId", "token", "candidate"})
        token = item["token"]
        if (not isinstance(token, str)
                or _candidates.CANDIDATE_TOKEN_RE.fullmatch(token) is None):
            raise _candidates.StateValidationError("invalid intake token")
        received = _candidates._require_object(
            item["candidate"], "intake candidate").get("receivedAt")
        if not _candidates._is_nonnegative_int(received):
            raise _candidates.StateValidationError("invalid intake receipt")
        # Plausibility is judged at the reading's own receipt, as the spool
        # judged it: a retained five-hour reset may have closed since, and
        # that does not make the retained evidence malformed.
        raw = json.dumps(item["candidate"], allow_nan=False).encode("utf-8")
        candidate = _parse_candidate(
            raw, account_key=account_key, token=token, now_epoch=received)
        if _intake_identity(candidate) != item["intakeId"]:
            raise _candidates.StateValidationError("intake identity mismatch")
        members.append(candidate)
    tokens = [c.token for c in members]
    if tokens != sorted(set(tokens)):
        raise _candidates.StateValidationError("invalid intake membership")
    return _Evaluation(doc["sequence"], doc["at"], tuple(members), journal_at)


def _journal_point(value) -> "tuple[str, int] | None":
    """A recorded journal coordinate (``{"segment", "offset"}``), or None."""
    if value is None:
        return None
    value = _candidates._require_object(value, "journal point")
    _candidates._require_exact_keys(value, {"segment", "offset"})
    if (not isinstance(value["segment"], str)
            or not _candidates._is_nonnegative_int(value["offset"])):
        raise _candidates.StateValidationError("invalid journal point")
    return (value["segment"], value["offset"])


def _journal_tail_unlocked() -> "tuple[str, int] | None":
    """The journal end as one listing and stat see it — the render path's own
    read (`_journal_extents_unlocked`, C4: no journal file is opened and no
    lock is taken). ``("", 0)`` when no segment exists yet, or when the
    journal directory itself does not exist; None when the listing or any
    segment's stat fails. Read before an evaluation is appended, it bounds
    from below where that evaluation entered the journal's order: an
    observation ending at or before it was appended first.

    Only the failure to open an absent journal DIRECTORY is a known empty
    point (fix round K7, 825-832-CD6-V01). A segment the listing named and
    its stat could not find — a restored empty future segment that a
    concurrent append's recovery removed in between — is unknown: reading
    it as empty would place every earlier observation after this
    evaluation."""
    try:
        extents = _journal_extents_unlocked()
    except _JournalEnumerationError:
        return None
    except FileNotFoundError:
        return ("", 0)
    except OSError:
        return None
    return extents[-1] if extents else ("", 0)


def _intake_segments(account_key: str) -> "list[tuple[int, pathlib.Path]]":
    try:
        entries = tuple(_intake_dir(account_key).iterdir())
    except OSError:
        return []
    found = []
    for path in entries:
        match = _INTAKE_SEGMENT_RE.fullmatch(path.name)
        if match is not None:
            found.append((int(match.group(1)), path))
    return sorted(found)


def _intake_lines(path: pathlib.Path):
    """Every complete line of one segment, streamed. A final line without a
    newline is a torn write and is not a record; an over-long line fails
    closed."""
    try:
        handle = open(path, "rb")
    except FileNotFoundError:
        return
    with handle:
        while True:
            line = handle.readline(_INTAKE_RECORD_MAX_BYTES + 1)
            if not line:
                return
            if not line.endswith(b"\n"):
                if len(line) > _INTAKE_RECORD_MAX_BYTES:
                    raise _candidates.StateValidationError(
                        "an intake line exceeds its record bound")
                return
            yield line


def _segment_last_line(path: pathlib.Path) -> "bytes | None":
    """The segment's last COMPLETE line, read backwards from its end (the
    render reads the tail on every capture, so it never streams the whole
    segment). A torn final line is not a record, as in `_intake_lines`; an
    over-long line fails closed."""
    try:
        handle = open(path, "rb")
    except FileNotFoundError:
        return None
    with handle:
        end = os.fstat(handle.fileno()).st_size
        buffer = b""
        while True:
            start = max(0, end - 8192)
            handle.seek(start)
            buffer = handle.read(end - start) + buffer
            end = start
            last = buffer.rfind(b"\n")
            if last < 0:
                if len(buffer) > _INTAKE_RECORD_MAX_BYTES:
                    raise _candidates.StateValidationError(
                        "an intake line exceeds its record bound")
                if end == 0:
                    return None
                continue
            if len(buffer) - last - 1 > _INTAKE_RECORD_MAX_BYTES:
                raise _candidates.StateValidationError(
                    "an intake line exceeds its record bound")
            previous = buffer.rfind(b"\n", 0, last)
            if previous >= 0 or end == 0:
                line = buffer[previous + 1:last + 1]
                if len(line) > _INTAKE_RECORD_MAX_BYTES + 1:
                    raise _candidates.StateValidationError(
                        "an intake line exceeds its record bound")
                return line
            if last + 1 > _INTAKE_RECORD_MAX_BYTES + 1:
                raise _candidates.StateValidationError(
                    "an intake line exceeds its record bound")


def _segment_last_sequence(path: pathlib.Path, *, account_key: str) -> "int | None":
    last = _segment_last_line(path)
    return None if last is None else _parse_evaluation(
        last, account_key=account_key).sequence


def _intake_tail_evaluation(account_key: str) -> "_Evaluation | None":
    """The last logged evaluation, or None for an empty intake."""
    for _first, path in reversed(_intake_segments(account_key)):
        last = _segment_last_line(path)
        if last is not None:
            return _parse_evaluation(last, account_key=account_key)
    return None


def _intake_tail_sequence(account_key: str) -> "int | None":
    """The last logged sequence number, or None for an empty intake."""
    tail = _intake_tail_evaluation(account_key)
    return None if tail is None else tail.sequence


def _intake_evaluations(account_key: str, *, after: int, through: "int | None" = None):
    """The logged evaluations with ``after < sequence <= through``, in
    order, read one line at a time (memory stays bounded by one record)."""
    segments = _intake_segments(account_key)
    previous = None
    for index, (_first, path) in enumerate(segments):
        if index + 1 < len(segments) and segments[index + 1][0] - 1 <= after:
            continue
        for line in _intake_lines(path):
            evaluation = _parse_evaluation(line, account_key=account_key)
            if previous is not None and evaluation.sequence <= previous:
                raise _candidates.StateValidationError("intake out of order")
            previous = evaluation.sequence
            if evaluation.sequence <= after:
                continue
            if through is not None and evaluation.sequence > through:
                return
            yield evaluation


def _intake_backlog(account_key: str, acknowledged: int) -> bool:
    tail = _intake_tail_sequence(account_key)
    return tail is not None and tail > acknowledged


def _unacknowledged_evaluations(account_key: str, *, now_epoch: "int | None" = None) -> list:
    """Every logged evaluation the account's checkpoint has not acknowledged
    (diagnostics and tests)."""
    transition = _read_transition(
        account_key, now_epoch=int(time.time()) if now_epoch is None else now_epoch)
    return list(_intake_evaluations(account_key, after=_acknowledged_through(transition)))


def _repair_intake_tail(fd: int) -> None:
    """Truncate a torn final line (a crashed append) before appending. Every
    append — a render capture or the reducer's own evaluation — runs under
    the account's intake leaf lock."""
    size = os.fstat(fd).st_size
    if size == 0:
        return
    os.lseek(fd, size - 1, os.SEEK_SET)
    if os.read(fd, 1) == b"\n":
        return
    floor = max(0, size - _INTAKE_RECORD_MAX_BYTES - 1)
    os.lseek(fd, floor, os.SEEK_SET)
    tail = os.read(fd, size - floor)
    newline = tail.rfind(b"\n")
    os.ftruncate(fd, 0 if newline < 0 else floor + newline + 1)


def _intake_append(account_key: str, *, at: int, members, acknowledged: int,
                   tail: "int | None" = None,
                   journal_at: "tuple[str, int] | None" = None) -> int:
    """Append one evaluation and return its sequence number: the ONE
    allocator, called under the account's intake leaf lock."""
    data_members = tuple(members)
    sequence = max(-1 if tail is None else tail, acknowledged) + 1
    data = _evaluation_line(sequence, at, data_members, journal_at)
    directory = _intake_dir(account_key)
    _durable_mkdir(directory)
    segments = _intake_segments(account_key)
    target = segments[-1][1] if segments else None
    try:
        rotate = target is None or target.stat().st_size >= _INTAKE_SEGMENT_MAX_BYTES
    except FileNotFoundError:
        rotate = True
    if rotate:
        target = directory / f"{sequence:020d}.jsonl"
    # A reused segment's entry is durable only if its creator finished the
    # directory sync, which its existence does not prove (cycle 4
    # verification): the creator may have died between its own data fsync
    # and the directory's. Its completion marker does prove it.
    entry_durable = not rotate and _durable_marked(target)
    fd = os.open(target, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        _repair_intake_tail(fd)
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        # Review finding CD4-D01: the append is COMMITTED only once it is
        # durable — the data (and any tail repair) fsynced, like every
        # journal append, and the segment's directory entry durable too —
        # before the capture installs its slot or returns. One fsync per
        # logged evaluation; the directory sync by the segment's creator and
        # once more by the first writer that reuses it without a marker.
        os.fsync(fd)
    finally:
        os.close(fd)
    if not entry_durable:
        import _cctally_journal

        _cctally_journal._fsync_dir(directory)
        if not rotate:
            # Only a reuser marks: the steady render's capture rotates into
            # a fresh segment that compaction removes again, so a creator's
            # marker would be written and unlinked on every render for no
            # reader.
            _mark_durable(target)
    return sequence


def _slot_candidate(account_key: str, token: str) -> "_candidates.Candidate | None":
    """The contributor's installed spool slot whatever its age, or None when
    it is absent or unreadable (judged at its own receipt, like a logged
    member)."""
    try:
        raw = _candidate_path(token, account_key=account_key).read_bytes()
        received = json.loads(raw).get("receivedAt")
        if not _candidates._is_nonnegative_int(received):
            return None
        return _parse_candidate(raw, account_key=account_key, token=token,
                                now_epoch=received)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


def _eligible_at(candidate: "_candidates.Candidate", at: int) -> bool:
    return -5 <= at - candidate.received_at < _cctally_core.STATUSLINE_CANDIDATE_TTL_SECONDS


def _reinstall_tail_slots(account_key: str, tail: "_Evaluation | None", *, at: int) -> None:
    """Idempotently reinstall the slot of every member of the tail
    evaluation that its contributor's slot does not hold. Every member of
    the last committed evaluation is its contributor's latest committed
    arrival, and every append runs this first, so a crash between a commit
    and its slot can leave at most the tail's arrivals uninstalled. A slot
    holding a later receipt is never replaced, and an absent slot is only
    restored while its arrival is still eligible."""
    if tail is None:
        return
    for member in tail.members:
        current = _slot_candidate(account_key, member.token)
        if current is not None:
            if (current.received_at > member.received_at
                    or _intake_identity(current) == _intake_identity(member)):
                continue
        elif not _eligible_at(member, at):
            continue
        _write_candidate(member)


def _same_arrival(a: "_candidates.Candidate", b: "_candidates.Candidate") -> bool:
    """One immutable arrival apart from its provider-activity witness: the
    same contributor, binding, receipt second and raw readings."""
    return dataclasses.replace(a, activity=None) == dataclasses.replace(b, activity=None)


def _intake_membership(account_key: str, *, at: int, incoming=None) -> tuple:
    """Under the intake lock, after `_reinstall_tail_slots`: each
    contributor's latest committed arrival eligible at ``at``, with the
    incoming arrival substituted for its own contributor exactly once and
    every other eligible contributor kept.

    Returns ``(members, incoming)``. An incoming arrival identical to its
    contributor's committed one except for a later capture's witness is the
    SAME arrival (#797 spec 4.2): it reuses the first committed witness, so
    its intake identity is the committed one's."""
    members = {c.token: c for c in _read_account_candidates(
        account_key, now_epoch=at, prune=False)}
    if incoming is not None:
        committed = members.get(incoming.token)
        if committed is not None and _same_arrival(committed, incoming):
            incoming = committed
        members[incoming.token] = incoming
    return tuple(members[token] for token in sorted(members)), incoming


def _intake_enroll(account_key: str, *, acknowledged: "int | None" = None,
                   incoming: "_candidates.Candidate | None" = None,
                   require_members: bool = False) -> "int | None":
    """Log ONE actual evaluation — its instant, its sequence number and its
    complete membership — under the account's intake leaf lock, and return
    its sequence number (None when ``require_members`` and nobody is
    eligible). A render capture passes its ``incoming`` arrival: the
    evaluation is committed first and the contributor's slot installed after
    it. The reducer's own evaluation is taken at the moment it is appended,
    so its membership is what the committed intake holds then — never a
    snapshot taken earlier in its pass.

    ``acknowledged`` is the checkpoint the sequence continues from when the
    intake is empty; left None it is read here, under the lock, so a
    compaction that emptied the intake cannot make a new sequence reuse an
    acknowledged one.

    The SAME evaluation is never logged twice: one identical to the tail —
    the same instant and the same committed arrivals, an unchanged reading
    re-rendered within one second, or the reducer's own evaluation in the
    second its render captured — logs nothing new and returns None. A later
    instant is a DISTINCT evaluation even when its membership is unchanged:
    it can cross a pending drop's deadline, so it is always logged (spec 4.3
    revision 3, review finding CD3-V01). That such an evaluation spends no
    second record-kernel attempt on evidence that is not distinct (#832) is
    the publication level's rule (`_repeated_attempt`), never retention's."""
    with _IntakeLock(account_key):
        at = incoming.received_at if incoming is not None else int(time.time())
        tail = _intake_tail_evaluation(account_key)
        _reinstall_tail_slots(account_key, tail, at=at)
        members, incoming = _intake_membership(account_key, at=at, incoming=incoming)
        if require_members and not members:
            return None
        if tail is None and acknowledged is None:
            acknowledged = _acknowledged_through(
                _read_transition(account_key, now_epoch=at))
        if tail is not None and tail.at == at and (
                _membership_evidence(tail.members) == _membership_evidence(members)):
            return None
        # Fix round K5 (825-832-CD5-D02): where this evaluation enters the
        # journal's order, read before it exists, so a same-second reading
        # accepted later can be told apart from one accepted earlier.
        sequence = _intake_append(
            account_key, at=at, members=members,
            acknowledged=-1 if acknowledged is None else acknowledged,
            tail=None if tail is None else tail.sequence,
            journal_at=_journal_tail_unlocked())
        if incoming is not None:
            _crash_point("after-capture-commit")
            _write_candidate(incoming)
        return sequence


def _capture_arrival(candidate: "_candidates.Candidate") -> "int | None":
    """The render's durable, file-only capture (spec 4.2 and 4.3 revision 3):
    append the arrival with the evaluation it actually saw, commit, then
    install its spool slot — all under the intake leaf lock, before the
    render tries the selected-state lock. No journal, cutover or index work
    (C4). None when the identical evaluation is already the tail: the render
    that logged it owns its registration."""
    if candidate.binding is None:
        raise ValueError("a current candidate requires an account binding")
    return _intake_enroll(candidate.binding.account_key, incoming=candidate)


def _intake_compact(account_key: str, acknowledged: int) -> None:
    """Remove the acknowledged contiguous prefix, whole segments only, under
    the intake leaf lock. Runs after the checkpoint is durable, so a crash
    here leaves only harmless, already-acknowledged entries; and every
    removed contributor that is still eligible keeps its latest committed
    arrival in its durable spool slot (CD5-D01), so membership survives the
    removal. The unlinks are unsynced: a segment a host crash brings back
    holds only sequences the durable checkpoint already acknowledges. The render's
    acknowledgement compacts too, so the segment a later drain or render
    streams holds only the unacknowledged suffix."""
    with _IntakeLock(account_key):
        segments = _intake_segments(account_key)
        for index, (_first, path) in enumerate(segments):
            if index + 1 < len(segments):
                last = segments[index + 1][0] - 1
            else:
                last = _segment_last_sequence(path, account_key=account_key)
                last = -1 if last is None else last
            if last > acknowledged:
                return
            # The marker goes first: a crash between the two unlinks leaves
            # an acknowledged segment without one (its next writer only
            # repeats the directory sync), never a marker without a segment.
            marker = _durable_marker(path)
            for doomed in (marker, path):
                try:
                    if doomed is not None:
                        doomed.unlink()
                except FileNotFoundError:
                    pass


def _acknowledged_through(transition) -> int:
    return -1 if transition is None else transition.intake_acknowledged


@dataclasses.dataclass(frozen=True)
class _Transition:
    """The ONE atomic, recoverable transition document of an account partition
    (spec 4.3): the reducer control state, the account-local five-hour anchor
    (spec 4.4), a monotonically advancing control generation, and at most one
    frozen publication plan with its journal recovery identity."""

    account_key: str
    binding: object
    generation: int
    five_hour_anchor: "tuple[int, int] | None"
    control: "_candidates.ControlState"
    publication: "dict | None" = None
    #: #825/#832 spec 5.6 item 7: ``{"authorityCommit": <observation id of
    #: the last bound authoritative publication whose control reconcile
    #: committed>, "dropWithheld": bool}``, or None. The control recovery
    #: certificate needs BOTH the journal-derived witness in the index and
    #: this transition's record of its commit; ``dropWithheld`` lets the
    #: render path's file-only fork decision see a withheld drop without
    #: opening the index.
    quarantine: "dict | None" = None
    #: #825/#832 spec 4.3 revision 2: the intake acknowledgement checkpoint —
    #: every logged evaluation with a sequence number at or below it has a
    #: durable outcome in THIS document's control state. -1 when none.
    intake_acknowledged: int = -1
    #: Review finding CD3-V01 (cycle 3): the `_membership_evidence` of the
    #: evaluation that froze the last publication plan — what its kernel
    #: attempt was spent on — so a later evaluation of the SAME evidence never
    #: spends the bounded repeat attempt (`_repeated_attempt`). None when none.
    attempt_evidence: "str | None" = None
    #: Review finding P1-2 (cycle 3): the journal coverage proof of the
    #: projection in ``control`` — `_coverage_document` of the covered
    #: classification it was read from — which a render needs, besides an
    #: unchanged index fingerprint, before it may acknowledge an evaluation
    #: against it (`_render_acknowledge`). Never carried over to a projection
    #: it was not proven for; None when absent.
    coverage: "dict | None" = None
    #: #797 spec 4.4 (D7b q3): the account's accounting receipts
    #: (`_lib_stale_proof.Receipt`), created, validated and pruned only by the
    #: detached reducer at a covered completion; every other writer carries
    #: them over unchanged.
    receipts: tuple = ()
    #: #797 spec 4.4 (D7c q3): the covered facts the render needs to tell
    #: whether a NOOP leaves receipt work (`_StaleCache`), written with the
    #: projection in ``control`` and, like ``coverage``, never carried over
    #: to a projection it was not read for. None when absent.
    stale_cache: "object | None" = None


@dataclasses.dataclass(frozen=True)
class _StaleCache:
    """The render's cached receipt-earning inputs (#797 spec 4.4, D7c q3):
    the accounted weekly window and value, and the compact provider samples
    read after ``samples_after`` (`_lib_stale_proof.compact_samples`)."""

    window: "int | None"
    accounted: object
    samples: tuple
    samples_after: "str | None"
    samples_covered: bool
    #: Task 5: the five-hour window and its accounted value (path (i) only,
    #: so no samples are needed to tell whether a member could earn).
    five_window: "int | None" = None
    five_accounted_percent: "float | None" = None
    #: F-I4-2: the sample identities whose receipts the completion that
    #: wrote this cache earned or held and then had to drop at the document
    #: limit. They are no longer receipt work for a render (eviction and
    #: omission still mean eligible) until a writer replaces this cache.
    omitted: frozenset = frozenset()


@dataclasses.dataclass(frozen=True)
class AccountResult:
    """One partition's outcome of a reducer invocation."""

    account_key: str
    status: str
    decision: "object | None" = None
    reason: "str | None" = None
    #: #825/#832 spec 4.3 revision 2: what a deferred partition is blocked by
    #: — the names of the locks its failed acquisitions lost to (one
    #: dependency waiter each), ``frontier`` (the journal grew past the
    #: checked high water) or ``rebuild-unowned``.
    blockers: frozenset = frozenset()
    #: Codex fix-round-4 Q3: the contended `_cctally_journal.LockBlock`s
    #: those failed acquisitions reported themselves — the obligations the
    #: continuation persists and one waiter per lock discharges.
    obligations: frozenset = frozenset()
    #: Spec 4.3 revision 5 (*Waiting heads*): True when a relevant dependency
    #: changed between the pass's observation and the installation of the
    #: head's wait descriptor. No descriptor was installed, so the head stays
    #: runnable and the holder makes another pass without another event.
    rerun: bool = False
    #: Fix round K12 (K11R-10): for a deferral on evidence that wall time
    #: alone can establish, the instant it does (the wait's non-sliding
    #: retry deadline); else None.
    retry_at: "int | None" = None


def _empty_control(projection: "_candidates.DbProjection") -> "_candidates.ControlState":
    return _candidates.ControlState(projection, {"fiveHour": None, "sevenDay": None})


def _transition_document(transition: _Transition) -> dict:
    anchor = transition.five_hour_anchor
    return {
        "schemaVersion": TRANSITION_SCHEMA_VERSION,
        "protocolGeneration": _candidates.PROTOCOL_GENERATION,
        "accountKey": transition.account_key,
        "binding": transition.binding.document(),
        "controlGeneration": transition.generation,
        "fiveHourAnchor": None if anchor is None else [anchor[0], anchor[1]],
        "control": _control_document(transition.control),
        "publication": transition.publication,
        "intakeAcknowledged": transition.intake_acknowledged,
        **({} if transition.quarantine is None
           else {"outcomeQuarantine": transition.quarantine}),
        **({} if transition.attempt_evidence is None
           else {"attemptEvidence": transition.attempt_evidence}),
        **({} if transition.coverage is None
           else {"journalCoverage": transition.coverage}),
        **({} if not transition.receipts
           else {"accountingReceipts": [_receipt_document(r)
                                        for r in transition.receipts]}),
        **({} if transition.stale_cache is None
           else {"staleContributorCache": _stale_cache_document(transition.stale_cache)}),
    }


def _receipt_document(receipt) -> dict:
    import _lib_stale_proof

    return _lib_stale_proof.receipt_to_json(receipt)


_SAMPLE_FIELDS = ("observationId", "requestedAt", "capturedAt", "weeklyPercent",
                  "weeklyResetsAt")


def _stale_cache_document(cache: _StaleCache) -> dict:
    accounted = cache.accounted
    return {
        "window": cache.window,
        "accountedPercent": None if accounted is None else accounted.percent,
        "floorEpoch": None if accounted is None else accounted.floor_epoch,
        "samplesAfter": cache.samples_after,
        "samplesCovered": cache.samples_covered,
        "samples": [[s.observation_id, s.requested_at_utc, s.captured_at_utc,
                     s.weekly_percent, s.weekly_resets_at_utc] for s in cache.samples],
        "fiveHourWindow": cache.five_window,
        "fiveHourAccountedPercent": cache.five_accounted_percent,
        "omitted": sorted(cache.omitted),
    }


def _validate_stale_cache(value, *, account_key: str):
    """The transition's render cache of receipt-earning inputs, or None."""
    import _lib_oauth_samples
    import _lib_stale_proof

    if value is None:
        return None
    doc = _candidates._require_object(value, "staleContributorCache")
    base = {"window", "accountedPercent", "floorEpoch", "samplesAfter",
            "samplesCovered", "samples"}
    five_keys = {"fiveHourWindow", "fiveHourAccountedPercent", "omitted"}
    if set(doc) != base and set(doc) != base | five_keys:
        raise _candidates.StateValidationError("unexpected stale contributor cache keys")
    five_window = doc.get("fiveHourWindow")
    five_percent = doc.get("fiveHourAccountedPercent")
    omitted = doc.get("omitted", [])
    if (not (five_window is None or _candidates._is_int(five_window))
            or not isinstance(omitted, list)
            or not all(isinstance(k, str) and len(k) == 64 for k in omitted)):
        raise _candidates.StateValidationError("invalid stale contributor cache")
    if five_percent is not None:
        five_percent = _candidates._percent(five_percent)
    window, percent, floor = doc["window"], doc["accountedPercent"], doc["floorEpoch"]
    after, covered, rows = doc["samplesAfter"], doc["samplesCovered"], doc["samples"]
    if (not (window is None or _candidates._is_int(window))
            or not (floor is None or _candidates._is_int(floor))
            or not isinstance(covered, bool) or not isinstance(rows, list)
            or not (after is None or isinstance(after, str))):
        raise _candidates.StateValidationError("invalid stale contributor cache")
    if percent is not None:
        percent = _candidates._percent(percent)
    samples = []
    for row in rows:
        if (not isinstance(row, list) or len(row) != len(_SAMPLE_FIELDS)
                or not all(isinstance(row[i], str) for i in (0, 1, 2, 4))
                or isinstance(row[3], bool) or not isinstance(row[3], (int, float))):
            raise _candidates.StateValidationError("invalid cached provider sample")
        samples.append(_lib_oauth_samples.OAuthSample(
            account_key, row[0], row[1], row[2], float(row[3]), row[4], None, None, None))
    accounted = None
    if window is not None:
        accounted = _lib_stale_proof.Accounted(
            account_key=account_key, axis="sevenDay", window=window, percent=percent,
            support_id=None, support_hash=None, floor_epoch=floor, credits=(),
            coverage=None)
    return _StaleCache(window, accounted, tuple(samples), after, covered,
                       five_window, five_percent, frozenset(omitted))


def _validate_receipts(value, *, account_key: str) -> tuple:
    import _lib_stale_proof

    if value is None:
        return ()
    if not isinstance(value, list) or not value:
        raise _candidates.StateValidationError("invalid accounting receipts")
    try:
        receipts = tuple(_lib_stale_proof.receipt_from_json(doc) for doc in value)
    except _lib_stale_proof.MalformedReceipt as exc:
        raise _candidates.StateValidationError(f"invalid accounting receipt: {exc}")
    if any(r.account_key != account_key for r in receipts):
        raise _candidates.StateValidationError("cross-account accounting receipt")
    return receipts


def _validate_publication(value, *, account_key: str):
    """A frozen publication plan document, or None. Strict: every axis it
    publishes names this account and its own window in its provenance."""
    import _lib_account_binding

    if value is None:
        return None
    doc = _candidates._require_object(value, "publication")
    import _cctally_journal

    keys = {"publicationId", "capturedAt", "binding", "sevenDay", "fiveHour",
            "fiveHourWindowKey", "provenance", "control", "journalHighWater",
            "intakeSequence"}
    # #825/#832 spec 4.7 revision 6: a plan frozen since revision 6 records
    # its fold-policy revision; one frozen earlier has none and keeps the
    # historical policy through recovery.
    policy_key = _cctally_journal.PLAN_FOLD_POLICY_KEY
    # #797/#823/#840 spec 4.9 (D12): a v2 plan also freezes its detector
    # revision, which must be exactly the integer 2; a plan frozen earlier has
    # none and keeps the historical detector through recovery.
    detector_key = _cctally_journal.PLAN_DETECTOR_POLICY_KEY
    if set(doc) - {policy_key, detector_key} != keys:
        raise _candidates.StateValidationError("unexpected publication keys")
    if policy_key in doc and (
            type(doc[policy_key]) is not int
            or doc[policy_key] != _cctally_journal.FOLD_POLICY_FIRST_POST_FLOOR):
        raise _candidates.StateValidationError("invalid publication fold policy")
    if detector_key in doc and (
            type(doc[detector_key]) is not int
            or doc[detector_key] != _cctally_journal.DETECTOR_POLICY):
        raise _candidates.StateValidationError("invalid publication detector policy")
    if (not isinstance(doc["publicationId"], str)
            or _PUBLICATION_ID_RE.fullmatch(doc["publicationId"]) is None):
        raise _candidates.StateValidationError("invalid publication identity")
    if not _candidates._is_nonnegative_int(doc["capturedAt"]):
        raise _candidates.StateValidationError("invalid publication capture instant")
    if not _candidates._is_nonnegative_int(doc["intakeSequence"]):
        raise _candidates.StateValidationError("invalid publication intake reference")
    binding = _lib_account_binding.load(doc["binding"])
    if binding.account_key != account_key:
        raise _candidates.StateValidationError("cross-account publication")
    provenance = _candidates._require_object(doc["provenance"], "provenance")
    published = set()
    for axis in _candidates.AXES:
        axis_doc = doc[axis]
        if axis_doc is None:
            continue
        axis_doc = _candidates._require_object(axis_doc, "publication axis")
        _candidates._require_exact_keys(axis_doc, {"percent", "rawResetsAt", "canonicalKey"})
        _candidates._percent(axis_doc["percent"])
        if not all(_candidates._is_int(axis_doc[k]) for k in ("rawResetsAt", "canonicalKey")):
            raise _candidates.StateValidationError("invalid publication window")
        source = _candidates._require_object(provenance.get(axis), "provenance axis")
        if (source.get("account") != account_key
                or source.get("window") != axis_doc["canonicalKey"]
                or source.get("kind") not in ("candidate", "projection", "retained_low")):
            raise _candidates.StateValidationError("cross-account or unclassified provenance")
        published.add(axis)
    if "sevenDay" not in published or set(provenance) != published:
        raise _candidates.StateValidationError("incomplete publication axes")
    five = doc["fiveHour"]
    if (five is None) != (doc["fiveHourWindowKey"] is None) or (
            five is not None and doc["fiveHourWindowKey"] != five["canonicalKey"]):
        raise _candidates.StateValidationError("inconsistent planned five-hour window")
    high_water = doc["journalHighWater"]
    if high_water is not None:
        high_water = _candidates._require_object(high_water, "journalHighWater")
        _candidates._require_exact_keys(high_water, {"segment", "offset"})
        if (not isinstance(high_water["segment"], str)
                or not _candidates._is_nonnegative_int(high_water["offset"])):
            raise _candidates.StateValidationError("invalid journal recovery identity")
    return doc


def _read_transition(account_key: str, *, now_epoch: int) -> "_Transition | None":
    """The account's transition, or None when absent. A present but invalid
    document RAISES: fail-closed state is never silently reset, so a corrupt
    partition cannot regain a spent retry budget."""
    import _lib_account_binding

    path = _transition_path(account_key)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    doc = _candidates._require_object(_candidates._load_json_document(
        raw, maximum_bytes=_candidates.CONTROL_DOCUMENT_MAX_BYTES), "transition")
    base_keys = {
        "schemaVersion", "protocolGeneration", "accountKey", "binding",
        "controlGeneration", "fiveHourAnchor", "control", "publication",
        "intakeAcknowledged"}
    _candidates._require_exact_keys(
        doc, base_keys | ({"outcomeQuarantine", "attemptEvidence",
                           "journalCoverage", "accountingReceipts",
                           "staleContributorCache"} & set(doc)))
    quarantine = _validate_transition_quarantine(doc.get("outcomeQuarantine"))
    attempt_evidence = doc.get("attemptEvidence")
    if attempt_evidence is not None and (
            not isinstance(attempt_evidence, str)
            or _EVIDENCE_RE.fullmatch(attempt_evidence) is None):
        raise _candidates.StateValidationError("invalid attempt evidence")
    coverage = _validate_coverage(doc.get("journalCoverage"))
    receipts = _validate_receipts(doc.get("accountingReceipts"), account_key=account_key)
    stale_cache = _validate_stale_cache(doc.get("staleContributorCache"),
                                        account_key=account_key)
    acknowledged = doc["intakeAcknowledged"]
    if not _candidates._is_int(acknowledged) or acknowledged < -1:
        raise _candidates.StateValidationError("invalid intake checkpoint")
    if (doc["schemaVersion"] != TRANSITION_SCHEMA_VERSION
            or doc["protocolGeneration"] != _candidates.PROTOCOL_GENERATION
            or doc["accountKey"] != account_key):
        raise _candidates.StateValidationError("not this account's current transition")
    binding = _lib_account_binding.load(doc["binding"])
    if binding.account_key != account_key:
        raise _candidates.StateValidationError("cross-account transition binding")
    if not _candidates._is_nonnegative_int(doc["controlGeneration"]):
        raise _candidates.StateValidationError("invalid control generation")
    anchor = doc["fiveHourAnchor"]
    if anchor is not None:
        if (not isinstance(anchor, list) or len(anchor) != 2
                or not all(_candidates._is_int(v) for v in anchor)):
            raise _candidates.StateValidationError("invalid account-local anchor")
        anchor = (anchor[0], anchor[1])
    control = _candidates.validate_control_document(doc["control"], now_epoch=now_epoch)
    control = dataclasses.replace(
        control,
        db_projection=dataclasses.replace(control.db_projection, account_key=account_key),
    )
    for axis, pending in control.pending_drops.items():
        signature = None if pending is None else pending.retry_signature
        if signature is not None and signature.account_key not in (None, account_key):
            raise _candidates.StateValidationError("cross-account retry signature")
    return _Transition(
        account_key=account_key,
        binding=binding,
        generation=doc["controlGeneration"],
        five_hour_anchor=anchor,
        control=control,
        publication=_validate_publication(doc["publication"], account_key=account_key),
        quarantine=quarantine,
        intake_acknowledged=acknowledged,
        attempt_evidence=attempt_evidence,
        coverage=coverage,
        receipts=receipts,
        stale_cache=stale_cache,
    )


def _validate_coverage(value):
    """The transition's journal coverage proof, or None. Strict: a digest
    and the inclusive range of evaluation instants (an absent bound is
    unbounded) for which the proven projection is the one a covered
    classification reads."""
    if value is None:
        return None
    doc = _candidates._require_object(value, "journalCoverage")
    _candidates._require_exact_keys(doc, {"digest", "from", "through"})
    if (not isinstance(doc["digest"], str)
            or _COVERAGE_DIGEST_RE.fullmatch(doc["digest"]) is None
            or not all(doc[k] is None or _candidates._is_int(doc[k])
                       for k in ("from", "through"))):
        raise _candidates.StateValidationError("invalid journal coverage")
    return {"digest": doc["digest"], "from": doc["from"], "through": doc["through"]}


def _validate_transition_quarantine(value):
    """The transition's outcome-quarantine document, or None. Strict."""
    if value is None:
        return None
    doc = _candidates._require_object(value, "outcomeQuarantine")
    _candidates._require_exact_keys(doc, {"authorityCommit", "dropWithheld"})
    commit = doc["authorityCommit"]
    if commit is not None and (not isinstance(commit, str) or not commit):
        raise _candidates.StateValidationError("invalid authority commit")
    if not isinstance(doc["dropWithheld"], bool):
        raise _candidates.StateValidationError("invalid drop-withheld flag")
    return {"authorityCommit": commit, "dropWithheld": doc["dropWithheld"]}


def _write_transition(transition: _Transition) -> None:
    _require_account_key(transition.account_key)
    if transition.binding.account_key != transition.account_key:
        raise ValueError("cross-account transition")
    # Durable (CD4-D01): this is the control checkpoint compaction relies on,
    # and the frozen plan it persists precedes its journal append.
    _atomic_write_json(
        _transition_path(transition.account_key), _transition_document(transition),
        durable=True)


def _read_transitions(*, now_epoch: int, errors: dict) -> dict:
    """Every readable partition transition; an unreadable one is recorded in
    ``errors`` under its account and left untouched on disk."""
    result = {}
    for account_key in _account_keys_on_disk():
        try:
            transition = _read_transition(account_key, now_epoch=now_epoch)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            errors[account_key] = exc
            continue
        if transition is not None:
            result[account_key] = transition
    return result


def _read_control_state(*, now_epoch: int, account_key: str) -> "_candidates.ControlState | None":
    """One account's reducer control state (None when absent or invalid)."""
    try:
        transition = _read_transition(account_key, now_epoch=now_epoch)
    except (OSError, ValueError, TypeError, KeyError):
        return None
    return None if transition is None else transition.control


def _statusline_control_db_agreement(*, now_epoch: int) -> bool | None:
    """Return control/DB-fingerprint agreement without opening SQLite.

    ``None`` means no partition has a derived control document; ``False``
    means an existing document was invalid or a stable fingerprint is stale.
    This is intentionally stat-only so doctor remains a read-only diagnostic.
    """
    errors: dict = {}
    transitions = _read_transitions(now_epoch=now_epoch, errors=errors)
    if errors:
        return False
    if not transitions:
        return None
    fingerprint = _db_file_fingerprint()
    return all(t.control.db_projection.db_files == fingerprint
               for t in transitions.values())


def _replace_transition(transition: "_Transition | None", *, account_key: str,
                        binding, projection, control=None, anchor=..., publication=...,
                        quarantine=..., acknowledged=..., attempt_evidence=...,
                        coverage=None, receipts=..., stale_cache=None):
    """The next generation of ``account_key``'s transition. Every field left
    as ``...`` carries over — the intake checkpoint in particular survives
    every writer that does not acknowledge (an OAuth reconcile), and so do
    the accounting receipts (#797 spec 4.4: only a covered detached
    completion replaces them). The journal coverage proof and the render's
    stale-contributor cache are the exception: each is written only by a
    caller that proves it for THIS projection, and is otherwise cleared
    (P1-2)."""
    if transition is None:
        base_control = _empty_control(projection)
        generation = 0
        prior_anchor = None
        prior_publication = None
        prior_quarantine = None
        prior_acknowledged = -1
        prior_evidence = None
        prior_receipts = ()
    else:
        base_control = transition.control
        generation = transition.generation
        prior_anchor = transition.five_hour_anchor
        prior_publication = transition.publication
        prior_quarantine = transition.quarantine
        prior_acknowledged = transition.intake_acknowledged
        prior_evidence = transition.attempt_evidence
        prior_receipts = transition.receipts
    return _Transition(
        account_key=account_key,
        binding=binding,
        generation=generation + 1,
        five_hour_anchor=prior_anchor if anchor is ... else anchor,
        control=control if control is not None else dataclasses.replace(
            base_control, db_projection=projection),
        publication=prior_publication if publication is ... else publication,
        quarantine=prior_quarantine if quarantine is ... else quarantine,
        intake_acknowledged=(prior_acknowledged if acknowledged is ...
                             else int(acknowledged)),
        attempt_evidence=(prior_evidence if attempt_evidence is ...
                          else attempt_evidence),
        coverage=coverage,
        receipts=prior_receipts if receipts is ... else tuple(receipts),
        stale_cache=stale_cache,
    )


def _write_control_state(control: "_candidates.ControlState", *, binding) -> None:
    """Replace the reducer control state of ``binding``'s account."""
    existing = None
    try:
        existing = _read_transition(binding.account_key, now_epoch=int(time.time()))
    except (OSError, ValueError, TypeError, KeyError):
        existing = None
    _write_transition(_replace_transition(
        existing, account_key=binding.account_key, binding=binding,
        projection=control.db_projection, control=control))


def _reconcile_selected_control(
    projection: "_candidates.DbProjection", *, binding, now_epoch: int,
    observed_axes=(), authority_commit=None,
) -> None:
    """Rewrite one account's DB-derived control after an authoritative write,
    retaining only that account's reducer pending state (spec 4.6: an OAuth
    publication for A reconciles only A's exact state). A corrupt transition
    is replaced, because the authoritative publication is the truth it
    reconciles to."""
    if projection.account_key not in (None, binding.account_key):
        raise ValueError("cross-account reconciliation")
    try:
        existing = _read_transition(binding.account_key, now_epoch=now_epoch)
    except (OSError, ValueError, TypeError, KeyError):
        existing = None
    pending = dict(
        existing.control.pending_drops if existing is not None
        else {"fiveHour": None, "sevenDay": None}
    )
    for axis in observed_axes:
        if axis in _candidates.AXES:
            pending[axis] = None
    anchor = _retained_anchor(
        projection, None if existing is None else existing.five_hour_anchor)
    # #825/#832 spec 5.6 item 7: the control transition records the commit of
    # this bound authoritative publication — half of the control recovery
    # certificate (the index holds the journal-ordered other half).
    quarantine = ... if authority_commit is None else {
        "authorityCommit": authority_commit, "dropWithheld": False}
    _write_transition(_replace_transition(
        existing, account_key=binding.account_key, binding=binding,
        projection=projection,
        control=_candidates.ControlState(projection, pending), anchor=anchor,
        quarantine=quarantine))


def _tombstone_document(value: "_candidates.Tombstone", *, account_key: str,
                        window: int) -> dict:
    document = {
        "schemaVersion": _candidates.SCHEMA_VERSION,
        "protocolGeneration": _candidates.PROTOCOL_GENERATION,
        "accountKey": account_key,
        "canonicalWindowKey": int(window),
        "axis": value.axis,
        "state": value.state,
    }
    if value.state == "inflight":
        document["startedAt"] = value.started_at
        document["priorBlockReceivedAtThrough"] = value.prior_block_received_at_through
    else:
        document["blockReceivedAtThrough"] = value.block_received_at_through
    return document


def _read_tombstone(axis: str, *, account_key: str, window: int, now_epoch: int,
                    fail_closed: bool = True) -> "_candidates.Tombstone | None":
    """The exact (account, axis, window) fence. A corrupt fence fails closed."""
    try:
        raw = _tombstone_path(axis, account_key=account_key, window=window).read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        return _candidates.Tombstone(axis, "inflight") if fail_closed else None
    try:
        doc = _candidates._require_object(_candidates._load_json_document(
            raw, maximum_bytes=_candidates.TOMBSTONE_DOCUMENT_MAX_BYTES), "tombstone")
        if (doc.pop("protocolGeneration", None) != _candidates.PROTOCOL_GENERATION
                or doc.pop("accountKey", None) != account_key
                or doc.pop("canonicalWindowKey", None) != int(window)):
            raise _candidates.StateValidationError("cross-account/window tombstone")
        return _candidates.validate_tombstone_document(
            doc, expected_axis=axis, now_epoch=now_epoch)
    except (ValueError, TypeError, KeyError):
        return _candidates.Tombstone(axis, "inflight") if fail_closed else None


def _write_tombstone(axis: str, value: "_candidates.Tombstone", *, account_key: str,
                     window: int) -> None:
    """Publish one (account, axis, window) fence through a persistence
    barrier (review finding 825-832-CD5-D02, #951, fix round K3). A fence is
    authority: the inflight one suppresses the axis while an authoritative
    writer repairs it, and the committed cutoff is what supersedes a frozen
    plan the reconciled control keeps. Each is durable before the transition
    it fences is considered complete — the inflight fence before the record
    kernel appends, the committed one before `_reconcile_selected_control`
    writes the durable control — so no host crash keeps a frozen plan or a
    reconciled transition and loses its fence. A crash between the rename and
    the directory sync leaves the previous fence: for the inflight write none
    or an earlier committed one, with nothing appended yet; for the commit the
    inflight fence, which fails closed."""
    _atomic_write_json(
        _tombstone_path(axis, account_key=account_key, window=window),
        _tombstone_document(value, account_key=account_key, window=window),
        durable=True)


def _account_tombstone_windows(account_key: str):
    """``(axis, window, path)`` for every fence file of one account."""
    try:
        entries = tuple(_tombstone_dir(account_key).iterdir())
    except OSError:
        return []
    found = []
    for path in entries:
        match = _TOMBSTONE_NAME_RE.fullmatch(path.name)
        if match is not None:
            found.append((match.group(1), int(match.group(2)), path))
    return sorted(found)


def _tombstone_window_live(axis: str, window: int, now_epoch: int) -> bool:
    return window + _TOMBSTONE_WINDOW_GRACE[axis] > now_epoch


def _expire_account_tombstones(account_key: str, *, now_epoch: int) -> None:
    """Remove fences of windows that have closed, in THIS account only (M18).
    The caller passes the OLDEST instant its retained work still evaluates at,
    so a fence that work references is never pruned (Q2).

    The unlink is deliberately left unsynced (fix round K3): a host crash
    that brings a pruned fence back leaves the state of a prune not yet run.
    Its window closed before the oldest retained instant, no plan can name
    it (a plan needs a reset still in the future), a repair request ignores
    a closed window, and a closed-window candidate it suppresses could not
    publish anyway — at worst it fails closed."""
    for axis, window, path in _account_tombstone_windows(account_key):
        if not _tombstone_window_live(axis, window, now_epoch):
            try:
                path.unlink()
            except OSError:
                pass


def _statusline_tombstone_states(*, now_epoch: int) -> dict:
    """Doctor's per-axis summary over every live fence (read-only)."""
    result = {"fiveHour": "absent", "sevenDay": "absent"}
    rank = {"absent": 0, "committed": 1, "invalid": 2, "inflight": 3}
    for account_key in _account_keys_on_disk():
        for axis, window, _path in _account_tombstone_windows(account_key):
            if not _tombstone_window_live(axis, window, now_epoch):
                continue
            value = _read_tombstone(axis, account_key=account_key, window=window,
                                    now_epoch=now_epoch, fail_closed=False)
            state = "invalid" if value is None else value.state
            if rank.get(state, 2) > rank[result[axis]]:
                result[axis] = state
    return result


def _authoritative_windows(args: argparse.Namespace, binding, *, now_epoch: int) -> dict:
    """The exact (axis -> canonical window) keys an OAuth publication for
    ``binding``'s account fences. The five-hour key uses the account-local
    rolling anchor (spec 4.4); the chosen raw/key pair is written into the
    account's transition BEFORE it keys any tombstone."""
    seven = int(_cctally_core._normalize_week_boundary_dt(
        dt.datetime.fromtimestamp(int(args.resets_at), tz=dt.timezone.utc)
    ).timestamp())
    windows = {"sevenDay": seven}
    if getattr(args, "five_hour_resets_at", None) is None:
        return windows
    raw = int(args.five_hour_resets_at)
    account_key = binding.account_key
    try:
        existing = _read_transition(account_key, now_epoch=now_epoch)
    except (OSError, ValueError, TypeError, KeyError):
        existing = None
    projection = _read_db_projection_stable(account_key=account_key)
    prior = _retained_anchor(
        projection, None if existing is None else existing.five_hour_anchor)
    key = int(_cctally()._canonical_5h_window_key(
        raw,
        prior_epoch=None if prior is None else prior[0],
        prior_key=None if prior is None else prior[1],
    ))
    anchor = prior if prior is not None and prior[1] == key else (raw, key)
    _write_transition(_replace_transition(
        existing, account_key=account_key, binding=binding,
        projection=projection if existing is None else existing.control.db_projection,
        anchor=anchor))
    windows["fiveHour"] = key
    return windows


def _authoritative_begin(axes, *, binding, windows: dict, now_epoch: int) -> dict:
    """Write fail-closed inflight fences for exactly ``binding``'s account and
    the given windows before an authority mutation.

    Each fence retains a prior committed cutoff while it is inflight, so a
    crash after an equality-deduplicated database call has the same safety
    posture as a crash before a write: no stale spool candidate of THIS
    account/window can become selected until a later authoritative repair
    commits it. No global axis-only fence is ever written (spec 4.6)."""
    requested = frozenset(axes)
    if not requested or not requested <= set(_candidates.AXES):
        raise ValueError("authoritative writer requires known observation axes")
    account_key = binding.account_key
    handles = {}
    for axis in sorted(requested):
        window = int(windows[axis])
        previous = _read_tombstone(axis, account_key=account_key, window=window,
                                   now_epoch=now_epoch, fail_closed=True)
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
        _write_tombstone(axis, inflight, account_key=account_key, window=window)
        handles[axis] = (account_key, window, inflight)
    return handles


def _authoritative_commit(handles: dict, *, completion_epoch: int) -> None:
    """Finalize every write-ahead fence with a monotonic cutoff."""
    for axis, (account_key, window, inflight) in handles.items():
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
            account_key=account_key,
            window=window,
        )


def _after_authoritative_record() -> None:
    """Test seam immediately after the authority kernel returns success."""


def _authoritative_repair_required(*, now_epoch: int, binding=None) -> bool:
    """Whether ``binding``'s account has an inflight/invalid fence on a live
    window that needs an OAuth repair attempt. No other account's state is
    inspected; a closed window never requests repair."""
    if binding is None:
        binding = _capture_binding()
    if binding is None:
        return False
    account_key = binding.account_key
    for axis, window, _path in _account_tombstone_windows(account_key):
        if not _tombstone_window_live(axis, window, now_epoch):
            continue
        value = _read_tombstone(axis, account_key=account_key, window=window,
                                now_epoch=now_epoch, fail_closed=True)
        if value is not None and value.state == "inflight":
            return True
    return False


def _authoritative_record_usage(
    args: argparse.Namespace,
    observed_axes,
    *,
    binding,
    lock_held: bool = False,
    nudge_dashboard: bool = True,
    nudge_sink=None,
) -> _AuthoritativeRecordResult:
    """Record OAuth authority for ``binding``'s account under write-ahead
    fences and reconcile exactly that account (spec 4.6).

    ``binding`` is the binding captured with the selected credential before
    the fetch and revalidated after it; every step below uses it and nothing
    re-resolves the active identity.

    ``lock_held`` is for OAuth refresh callers that already hold the selected
    lock across their fetch, authoritative publication, and matching backoff
    transition. All other callers acquire the same blocking lock here.

    ``nudge_dashboard`` (#583 S2 §5.3) is passed straight through to
    ``cmd_record_usage``. It defaults on, so the statusline / hook path keeps
    nudging; ``_refresh_usage_inproc`` passes False.

    ``nudge_sink`` defers the nudge out of the selected-state critical section.
    The nudge is a loopback POST with a multi-second timeout, and this lock is
    an ``fcntl.flock`` every cctally process contends on, so firing it here
    would let one unresponsive listener stall every other process's
    selected-state writes. This frame owns the lock only on the
    ``lock_held=False`` path, so that is the only path that can fire the
    deferred nudge itself; a caller passing ``lock_held=True`` owns the lock
    and must supply its own sink. The fail-closed guard below enforces that
    contract before any tombstone or journal write begins.
    """
    if binding is None:
        return _AuthoritativeRecordResult(
            "record_failed", "authoritative record requires an account binding")
    if lock_held and nudge_dashboard and nudge_sink is None:
        return _AuthoritativeRecordResult(
            "record_failed",
            "lock_held authoritative record requires nudge_sink",
        )

    if not lock_held:
        deferred = []
        try:
            with _selected_state_lock():
                result = _authoritative_record_usage(
                    args, observed_axes, binding=binding, lock_held=True,
                    nudge_dashboard=nudge_dashboard,
                    nudge_sink=(nudge_sink or (lambda: deferred.append(1))),
                )
        except OSError as exc:
            return _AuthoritativeRecordResult("record_failed", str(exc))
        if deferred:
            _cctally()._nudge_dashboard_repaint()
        return result

    now_epoch = int(time.time())
    try:
        _ensure_cutover_for_publication()
        # #825/#832 spec 5.5 step 6: rescan the legacy spool before ANY current
        # publication — this OAuth one included — under the selected-state
        # lock held here, so a change is journaled as `legacy_spool_activity`
        # ahead of the observation this publication appends. Spec revision 5:
        # an incomplete inventory journals its `inventory-incomplete` trigger
        # here and this publication continues; only an uncertainty op that
        # could not be made durable raises, and then nothing is published.
        _rescan_legacy_spool()
        windows = _authoritative_windows(args, binding, now_epoch=now_epoch)
        if "fiveHour" in windows:
            args.five_hour_window_key = windows["fiveHour"]
        handles = _authoritative_begin(
            observed_axes, binding=binding, windows=windows, now_epoch=now_epoch)
    except Exception as exc:
        return _AuthoritativeRecordResult("record_failed", str(exc))

    appended: list = []
    try:
        rc = _cctally().cmd_record_usage(
            args, binding=binding, nudge_dashboard=nudge_dashboard,
            nudge_sink=nudge_sink, appended=appended)
    except _cctally().StatsRebuildDeferred as exc:
        return _AuthoritativeRecordResult("record_failed", str(exc))
    except Exception as exc:
        return _AuthoritativeRecordResult("record_failed", str(exc))
    if rc != 0:
        return _AuthoritativeRecordResult("record_failed", f"exit {rc}")

    try:
        # Intentionally after an equality-deduplicated success: tests use this
        # seam to prove the write-ahead tombstone still fails closed on crash.
        _cctally()._after_authoritative_record()
        projection = _read_db_projection_stable(account_key=binding.account_key)
        completion_epoch = int(time.time())
        _authoritative_commit(handles, completion_epoch=completion_epoch)
        _reconcile_selected_control(
            projection, binding=binding, now_epoch=completion_epoch,
            observed_axes=observed_axes,
            authority_commit=(appended[-1] if appended else None),
        )
        _cctally()._statusline_observe_touch(binding=binding)
    except Exception as exc:
        # Do not clean up the inflight tombstone on a post-record failure.
        # It is the durable proof that a later authoritative writer must repair
        # this axis before any spool candidate may be selected again.
        return _AuthoritativeRecordResult("record_failed", str(exc))
    # #825/#832 spec 4.3 revision 2: the selected-state lock's owner posts
    # the reconciliation's wake once it releases that lock.
    _OAUTH_RECONCILED.set(True)
    return _AuthoritativeRecordResult("ok")


def _retained_anchor(projection: "_candidates.DbProjection", prior):
    """The account-local five-hour anchor to continue from: the persisted one
    while it still names the projected window, else the projection's own
    (raw, key) pair. A percentage winner's raw reset is evidence for its
    value, not a new physical-window anchor."""
    five = projection.five_hour
    if five is None:
        # Fix round K14c (spec 4.3 revision 6, 4.4): with no projected row
        # the classification evidence of the covered read carries the
        # account's own anchor — a floor-retired window's first raw reset or
        # the greatest expired predecessor's — so a reading that jitters
        # across a ten-minute bucket keys the same physical window. The
        # control state's anchor stands unless that evidence is newer.
        evidence = (projection.evidence or {}).get("fiveHour")
        anchor = _evidence_anchor(evidence)
        if anchor is not None and (prior is None or prior[1] < anchor[1]):
            return anchor
        return prior
    if prior is not None and prior[1] == five.canonical_key:
        return prior
    return (five.raw_resets_at, five.canonical_key)


def _evidence_anchor(evidence) -> "tuple[int, int] | None":
    """The (raw, key) anchor a five-hour classification evidence carries."""
    if not isinstance(evidence, _candidates.AxisEvidence):
        return None
    if (evidence.kind == _candidates.EVIDENCE_FLOOR_RETIRED
            and evidence.anchor_raw is not None and evidence.group_key is not None):
        return (int(evidence.anchor_raw), int(evidence.group_key))
    if (evidence.kind == _candidates.EVIDENCE_EXPIRED_PREDECESSOR
            and evidence.predecessor_raw is not None
            and evidence.predecessor_key is not None):
        return (int(evidence.predecessor_raw), int(evidence.predecessor_key))
    return None


def _rolling_anchor(candidates, prior):
    """Advance the account-local anchor over the account's canonicalized raw
    resets, ascending (contributor token breaks ties): a NEW key makes its
    first raw reset the next anchor; jitter reusing a key keeps the first."""
    anchor = prior
    ordered = sorted(
        (c.five_hour.raw_resets_at, c.token, c.five_hour.canonical_key)
        for c in candidates if c.five_hour is not None
    )
    for raw, _token, key in ordered:
        if anchor is None or key != anchor[1]:
            anchor = (raw, key)
    return anchor


def _canonicalize_candidates(
    candidates: tuple["_candidates.Candidate", ...],
    anchor,
) -> tuple["_candidates.Candidate", ...]:
    """Key ONE account's candidates by window with that account's own anchor."""
    c = _cctally()
    five_values = [candidate.five_hour for candidate in candidates if candidate.five_hour is not None]
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


def _axis_value_document(value) -> "dict | None":
    if value is None:
        return None
    return {"percent": value.percent, "rawResetsAt": value.raw_resets_at,
            "canonicalKey": value.canonical_key}


def _bound_publication_plan(decision, projection, candidates, binding, *,
                            generation: int, now_epoch: int,
                            intake_sequence: int,
                            refused=None) -> "dict | None":
    """Freeze one partition's combined two-axis publication plan.

    Every published axis records its provenance — ``candidate`` (the winning
    contributor token and observation identity), ``projection`` (the account,
    window and logical reset generation it was filled from) or
    ``retained_low`` (the account, window and the control generation holding
    the evidence) — and every one names THIS account. The plan carries its
    binding, its captured instant and a stable publication identity; the
    decision's control state rides along so a recovered plan commits the
    same control replacement.

    ``refused`` maps an axis to the contributor tokens the reducer refused
    on it (#797 spec 4.4): a candidate axis's provenance names the selected
    ELIGIBLE winner, never a refused member that happens to tie its value.
    Refusal filters only a rise: an accepted drop (consensus or deadline,
    below the same window's baseline) publishes exactly as today, its
    provenance included (review RV-3), because the reducer selects it from
    the unfiltered set."""
    import _cctally_journal

    refused = refused or {}
    account_key = binding.account_key
    if projection.account_key not in (None, account_key):
        raise ValueError("cross-account publication projection")
    base = _build_publication_plan(decision, projection, now_epoch=now_epoch)
    if base is None or base.seven_day is None:
        return None
    provenance = {}
    for axis, value in (("sevenDay", base.seven_day), ("fiveHour", base.five_hour)):
        if value is None:
            continue
        chosen = _candidates._candidate_axis(decision.plan, axis)
        if chosen is None:
            source = _candidates._projection_axis(projection, axis)
            provenance[axis] = {
                "kind": "projection", "account": account_key,
                "window": value.canonical_key,
                "logicalResetGeneration": source.reset_generation,
            }
            continue
        baseline = _candidates._projection_axis(projection, axis)
        drop = (baseline is not None
                and baseline.canonical_key == value.canonical_key
                and value.percent < baseline.percent)
        excluded = () if drop else refused.get(axis, ())
        winners = sorted(
            (candidate for candidate in candidates
             if (axis_value := _candidates._candidate_axis(candidate, axis)) is not None
             and axis_value.canonical_key == value.canonical_key
             and axis_value.percent == value.percent
             and candidate.token not in excluded),
            key=lambda candidate: candidate.token,
        )
        if winners:
            provenance[axis] = {
                "kind": "candidate", "account": account_key,
                "window": value.canonical_key, "token": winners[0].token,
                "observationIdentity": _candidates.observation_identity(
                    winners[0].token, axis, value),
                **_activity_provenance(winners[0]),
            }
        else:
            provenance[axis] = {
                "kind": "retained_low", "account": account_key,
                "window": value.canonical_key, "controlGeneration": generation,
            }
    plan = {
        "capturedAt": now_epoch,
        "binding": binding.document(),
        "sevenDay": _axis_value_document(base.seven_day),
        "fiveHour": _axis_value_document(base.five_hour),
        "fiveHourWindowKey": None if base.five_hour is None else base.five_hour.canonical_key,
        "provenance": provenance,
        # #825/#832 spec 4.7 revision 6: the fold policy the plan's
        # observation is decided under, frozen with (and inside the identity
        # of) the plan so recovery never re-policies it.
        _cctally_journal.PLAN_FOLD_POLICY_KEY: _cctally_journal.CURRENT_FOLD_POLICY,
        # #797/#823/#840 spec 4.9 (D12): the detector revision, frozen beside
        # the fold policy and inside the identity for the same reason.
        _cctally_journal.PLAN_DETECTOR_POLICY_KEY: _cctally_journal.DETECTOR_POLICY,
    }
    plan["publicationId"] = hashlib.sha256(json.dumps(
        {"protocolGeneration": _candidates.PROTOCOL_GENERATION,
         "account": account_key, **plan},
        sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()
    plan["control"] = _control_document(decision.control)
    plan["journalHighWater"] = None
    # #825/#832 spec 4.3 revision 2: the evaluation this plan was frozen
    # from — its acknowledgement advances the intake checkpoint past it.
    plan["intakeSequence"] = int(intake_sequence)
    return plan


def _activity_provenance(candidate: "_candidates.Candidate") -> dict:
    """The selected contributor's frozen provider-activity evidence as plan
    provenance (#797 spec 4.5): its instant (or null), its typed status and
    the evidence revision. Part of the publication identity; never a
    transcript path or session id."""
    import _lib_provider_activity

    activity = candidate.activity
    if not isinstance(activity, _lib_provider_activity.ActivityWitness):
        raise ValueError("a current candidate requires its activity witness")
    return {
        "providerActivityAt": activity.instant_utc,
        "providerActivityStatus": activity.status,
        "providerActivityRevision": activity.evidence_revision,
    }


# --- #797 spec 4.4: refusal facts and accounting receipts --------------------


def _axis_members(candidates, axis: str) -> list:
    """The ``axis`` members with known, supported provider-activity evidence
    as `_lib_stale_proof.Member`s, in their window: the normalized weekly
    reset, or the canonical five-hour window key (the candidate's own when
    it carries one). A member with unknown evidence is absent: it is always
    eligible and never earns (O5)."""
    import _lib_stale_proof

    members = []
    for candidate in candidates:
        value = candidate.seven_day if axis == "sevenDay" else candidate.five_hour
        if value is None:
            continue
        if axis == "sevenDay":
            window = int(_cctally_core._normalize_week_boundary_dt(
                dt.datetime.fromtimestamp(value.raw_resets_at, tz=dt.timezone.utc)
            ).timestamp())
        elif value.canonical_key is not None:
            window = int(value.canonical_key)
        else:
            window = int(_cctally()._canonical_5h_window_key(int(value.raw_resets_at)))
        sigma = _lib_stale_proof.sample_identity(
            candidate.token, candidate.activity, axis=axis,
            percent=value.percent, window=window)
        if sigma is not None:
            members.append(_lib_stale_proof.Member(sigma, int(value.raw_resets_at)))
    return members


def _weekly_members(candidates) -> list:
    return _axis_members(candidates, "sevenDay")


def _all_members(candidates) -> list:
    return [*_axis_members(candidates, "sevenDay"), *_axis_members(candidates, "fiveHour")]


def _stale_axes(stale: "_StaleInputs") -> tuple:
    """Per axis: ``(axis, window, samples, credits, supports)``. The
    five-hour axis checks no supports (its accounted value is detector
    state)."""
    return (("sevenDay", stale.window, stale.samples, stale.credits, stale.supports),
            ("fiveHour", stale.five_window, stale.five_samples, stale.five_credits, None))


def _stale_member_facts(stale: "_StaleInputs | None", receipts, candidates):
    """The reducer's per-axis member facts (#797 spec 4.4), or None when no
    member is refused, so such a window reduces exactly as before. Each axis
    is decided on its own window's members, samples, credits and receipts;
    an axis without a refused member is absent (all eligible)."""
    import _lib_stale_proof

    if stale is None or not receipts:
        return None
    out = {}
    for axis, window, samples, credits, supports in _stale_axes(stale):
        if window is None:
            continue
        members = [m for m in _axis_members(candidates, axis) if m.sigma.window == window]
        if not members:
            continue
        facts = _lib_stale_proof.refusal_facts(
            axis, members, samples, receipts, credits,
            index_identity=stale.index_identity, supports=supports)
        if any(f.refused for f in facts.values()):
            out[axis] = facts
    return out or None


def _refused_tokens(member_facts) -> dict:
    return {axis: frozenset(token for token, f in facts.items() if f.refused)
            for axis, facts in (member_facts or {}).items()}


def _completion_instant() -> str:
    """``e_σ``: the wall clock at a covered completion, in the samples' ``q``
    form. It is an upper bound on the completion, taken after it; an earlier
    one is never reconstructed (from journal capture times or otherwise)."""
    import _lib_oauth_samples

    return _lib_oauth_samples.format_request_started(
        dt.datetime.fromtimestamp(time.time(), tz=dt.timezone.utc))


def _completion_receipts(transition, classified, members):
    """The receipt map and render cache a covered, completed derivation
    installs (#797 spec 4.4, D7b q3): the transition's receipts revalidated
    and pruned against ``classified``, merged with what ``members`` earn
    now. Without stale inputs the map is carried over unchanged and no cache
    is written."""
    import _lib_stale_proof

    existing = () if transition is None else transition.receipts
    stale = getattr(classified, "stale", None)
    if stale is None:
        return existing, None
    kept, earned = [], []
    completed = _completion_instant()
    for axis, window, samples, credits, supports in _stale_axes(stale):
        kept.extend(_lib_stale_proof.prune_receipts(
            [r for r in existing if r.sigma.axis == axis], credits,
            index_identity=stale.index_identity, window=window, supports=supports))
        accounted = stale.accounted if axis == "sevenDay" else stale.five_accounted
        if accounted is None or window is None:
            continue
        # The five-hour axis earns by exact equality alone (path (i)); its
        # samples feed refusal, never earning.
        earned.extend(_lib_stale_proof.earn_receipts(
            [m for m in _axis_members(members, axis) if m.sigma.window == window],
            accounted,
            stale.samples if axis == "sevenDay" and stale.samples_covered else (),
            completed_at_utc=completed, index_identity=stale.index_identity))
    receipts = _lib_stale_proof.latest_per_token(
        _lib_stale_proof.merge_receipts(kept, earned))
    floor = None if stale.accounted is None else stale.accounted.floor_epoch
    cache = _StaleCache(
        stale.window, stale.accounted,
        () if stale.window is None else _lib_stale_proof.compact_samples(
            stale.samples, window=stale.window, floor_epoch=floor),
        stale.samples_after, stale.samples_covered,
        stale.five_window,
        None if stale.five_accounted is None else stale.five_accounted.percent)
    return receipts, cache


def _fit_transition(transition: _Transition, *, prior=(), referenced=()) -> _Transition:
    """``transition`` within the 1 MiB control document limit (#797 spec
    4.4): the oldest receipts no outstanding work references are evicted
    first, then the receipts new since ``prior`` are omitted, and finally
    the render cache is dropped — accounting is never blocked, and eviction
    means eligible."""
    import _lib_stale_proof

    def size(t) -> int:
        return len(json.dumps(_transition_document(t), sort_keys=True,
                              separators=(",", ":"), allow_nan=False).encode("utf-8"))

    limit = _candidates.CONTROL_DOCUMENT_MAX_BYTES
    if size(transition) <= limit:
        return transition
    before = {r.sigma.key() for r in prior}
    evaluated = frozenset(r.sigma.key() for r in transition.receipts)

    def with_receipts(kept) -> _Transition:
        # F-I4-2: every receipt this completion evaluated and could not keep
        # (evicted or omitted) is recorded in the render cache, so a render
        # does not count it as receipt work again until a writer replaces
        # the cache. Its member stays eligible.
        fitted = dataclasses.replace(transition, receipts=tuple(kept))
        if fitted.stale_cache is not None:
            fitted = dataclasses.replace(fitted, stale_cache=dataclasses.replace(
                fitted.stale_cache,
                omitted=evaluated - {r.sigma.key() for r in kept}))
        return fitted

    receipts = _lib_stale_proof.bounded_receipts(
        [r for r in transition.receipts if r.sigma.key() in before],
        [r for r in transition.receipts if r.sigma.key() not in before],
        referenced_tokens=frozenset(referenced),
        fits=lambda kept: size(with_receipts(kept)) <= limit)
    fitted = with_receipts(receipts)
    if size(fitted) > limit:
        fitted = dataclasses.replace(fitted, stale_cache=None)
    return fitted


def _refusal_authority_lost(classified, facts, receipts, members) -> bool:
    """Whether a member ``facts`` refused is no longer refused against
    ``receipts`` — the same classification and members, only the receipt
    map changed (review RV-4: the document limit evicted the receipt a
    refusal rested on)."""
    refused = _refused_tokens(facts)
    if not any(refused.values()):
        return False
    after = _refused_tokens(_stale_member_facts(
        getattr(classified, "stale", None), receipts, members))
    return any(tokens - after.get(axis, frozenset())
               for axis, tokens in refused.items())


def _referenced_tokens(transition, members) -> frozenset:
    tokens = {m.token for m in members}
    if transition is not None:
        for pending in transition.control.pending_drops.values():
            if pending is not None:
                tokens.update(pending.contributors)
    return frozenset(tokens)


def _receipt_work(transition: _Transition, members) -> bool:
    """Whether a covered detached completion over ``members`` could earn a
    receipt that ``transition`` does not hold (#797 spec 4.4, D7c q3), judged
    from the transition's cached facts alone (C4: the render reads no
    index). Without a cache any member with known evidence is work; a member
    with unknown evidence is never work."""
    import _lib_stale_proof

    known = _all_members(members)
    if not known:
        return False
    cache = transition.stale_cache
    if cache is None:
        return True
    # F-I4-2: a receipt the writer evaluated and had to omit is not work.
    held = {r.sigma.key() for r in transition.receipts} | set(cache.omitted)
    for member in known:
        if member.sigma.axis != "fiveHour" or member.sigma.key() in held:
            continue
        # Path (i) only: exact equality with the cached five-hour baseline.
        if (cache.five_window is not None and member.sigma.window == cache.five_window
                and cache.five_accounted_percent is not None
                and member.sigma.percent == cache.five_accounted_percent):
            return True
    if cache.accounted is None:
        return False
    for member in known:
        if (member.sigma.axis != "sevenDay" or member.sigma.window != cache.window
                or member.sigma.key() in held):
            continue
        if member.sigma.percent == cache.accounted.percent:
            return True
        if not cache.samples_covered:
            continue
        bound = _lib_stale_proof.floor_microsecond(member.sigma.activity_at_utc)
        if cache.samples_after is None or bound is None or cache.samples_after > bound:
            return True
        if _lib_stale_proof.earn_receipts(
                [member], cache.accounted, cache.samples,
                completed_at_utc="1970-01-01T00:00:00.000000Z",
                index_identity="render"):
            return True
    return False


def _stale_scope(account_key: str, candidates, transition) -> tuple:
    """``(samples_after, receipts_held)`` for one partition's detached work
    (`_STALE_SCOPE`): the earliest instant any member of its logged and
    current evaluations, or any receipt it holds, needs a sample after."""
    import _lib_stale_proof

    instants = [r.sigma.activity_at_utc
                for r in (() if transition is None else transition.receipts)]
    sets = [candidates]
    try:
        sets.extend(e.members for e in _intake_evaluations(
            account_key, after=_acknowledged_through(transition)))
    except (OSError, ValueError, TypeError, KeyError):
        pass
    for members in sets:
        instants.extend(m.sigma.activity_at_utc for m in _all_members(members))
    bounds = [b for b in (_lib_stale_proof.floor_microsecond(i) for i in instants)
              if b is not None]
    return (min(bounds) if bounds else None,
            bool(transition is not None and transition.receipts))


def _plan_members(account_key: str, plan: dict) -> tuple:
    """The members of a frozen plan's own intake evaluation, or () when it
    cannot be read back (nothing is then earned from it)."""
    sequence = int(plan["intakeSequence"])
    try:
        evaluations = list(_intake_evaluations(
            account_key, after=sequence - 1, through=sequence))
    except (OSError, _candidates.StateValidationError, ValueError, TypeError, KeyError):
        return ()
    if len(evaluations) != 1 or evaluations[0].sequence != sequence:
        return ()
    return tuple(evaluations[0].members)


def _plan_winner_refused(plan: dict, member_facts) -> bool:
    """Whether a frozen plan's candidate provenance names a member the
    current facts refuse on that axis (#797 spec 4.4, T4)."""
    refused = _refused_tokens(member_facts)
    for axis, source in (plan.get("provenance") or {}).items():
        if (isinstance(source, dict) and source.get("kind") == "candidate"
                and source.get("token") in refused.get(axis, ())):
            return True
    return False


def _plan_record_args(plan: dict) -> argparse.Namespace:
    five = plan["fiveHour"]
    return _record_args(
        percent=plan["sevenDay"]["percent"],
        resets_at=plan["sevenDay"]["rawResetsAt"],
        five_hour_percent=None if five is None else five["percent"],
        five_hour_resets_at=None if five is None else five["rawResetsAt"],
        source="statusline",
    )


def _journal_high_water() -> "dict | None":
    import _cctally_journal

    segments = _cctally_journal.list_segments()
    if not segments:
        return None
    try:
        size = os.path.getsize(_cctally_core.JOURNAL_DIR / segments[-1])
    except OSError:
        return None
    return {"segment": segments[-1], "offset": size}


class _PublicationScanUnavailable(OSError):
    """The journal could not be listed or a segment it holds could not be
    read while looking for a plan's appended observation (review finding
    K9K10-R12). Whether the plan was appended is then unknown: never "not
    appended", which would publish it a second time."""


def _publication_appended(plan: dict) -> bool:
    """Whether the plan's usage observation is already durable: a bounded scan
    of only the journal bytes appended after the plan's recorded high water
    for ONE complete, newline-terminated, decodable observation carrying the
    plan's 64-hex publication identity (the journal recovery identity) and
    its binding.

    The identity's bytes alone prove nothing (#825/#832 fix round D,
    CD2-001): a write interrupted after them but before the record's newline
    leaves a torn final line that ingest never yields and the next append's
    torn-tail repair truncates. Such a plan is not appended; it is recovered
    from the saved plan, which rebuilds the same observation. The scan starts
    at the beginning of the line holding the high water, so an observation
    that a torn-tail repair moved below it is still found.

    Fail closed (review finding K9K10-R12): a listing or segment read that
    fails raises `_PublicationScanUnavailable` — the answer is unknown, and
    the caller defers rather than re-publishing. Only a segment that vanished
    between the listing and its open (a removed empty future segment) is
    skipped: it holds no observation."""
    import _cctally_journal
    import _lib_journal

    needle = plan["publicationId"].encode("ascii")
    high_water = plan.get("journalHighWater")
    start_segment = None if high_water is None else high_water["segment"]
    start_key = None if start_segment is None else _lib_journal.segment_sort_key(start_segment)
    try:
        segments = _cctally_journal.list_segments()
    except OSError as exc:
        raise _PublicationScanUnavailable(str(exc)) from exc
    for segment in segments:
        if start_key is not None and _lib_journal.segment_sort_key(segment) < start_key:
            continue
        offset = high_water["offset"] if segment == start_segment else 0
        try:
            with open(_cctally_core.JOURNAL_DIR / segment, "rb") as handle:
                handle.seek(_line_start_at_or_before(handle, offset))
                data = handle.read()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise _PublicationScanUnavailable(str(exc)) from exc
        if needle not in data:
            continue
        for line in data[:data.rfind(b"\n") + 1].splitlines():
            if needle in line and _is_plan_observation(
                    _lib_journal.decode_line(line), plan):
                return True
    return False


def _line_start_at_or_before(handle, offset: int) -> int:
    """The start of the journal line containing byte ``offset - 1`` — or
    ``offset`` itself when it is already a line boundary."""
    import _cctally_journal

    if offset <= 0:
        return 0
    floor = max(0, offset - _cctally_journal._MAX_LINE_BYTES)
    handle.seek(floor)
    newline = handle.read(offset - floor).rfind(b"\n")
    return floor if newline < 0 else floor + newline + 1


def _is_plan_observation(rec, plan: dict) -> bool:
    """Whether a decoded journal record is this plan's usage observation: a
    current-stamped Claude observation with the plan's publication identity,
    its exact binding and the binding's account, carrying the plan's own
    payload — the weekly reading, its raw reset and the plan's provenance
    (#825/#832 spec 4.3 revision 2, Codex design-fix-round-3 Q4). Matching
    identity bytes or an account-observation record alone are not a receipt.
    The five-hour fields are not compared: the record kernel drops an
    implausible five-hour reset at append time, and that is still this
    plan's one publication."""
    import _lib_account_protocol
    import _lib_accounts
    import _lib_credit

    if not isinstance(rec, dict) or rec.get("t") != "obs":
        return False
    payload = rec.get("payload")
    if not isinstance(payload, dict):
        return False
    binding = plan["binding"]
    seven = plan["sevenDay"]
    return (rec.get("provider") == "claude"
            and rec.get(_lib_account_protocol.WRITER_GENERATION_KEY)
            == _lib_account_protocol.PROTOCOL_GENERATION
            and payload.get("publication_id") == plan["publicationId"]
            and payload.get("account_binding") == binding
            and (rec.get("account") or _lib_accounts.UNATTRIBUTED)
            == binding.get("accountKey")
            and payload.get("weekly_percent")
            == _lib_credit._normalize_percent(seven["percent"])
            and payload.get("resets_at") == int(seven["rawResetsAt"])
            and payload.get("publication") == plan["provenance"])


#: The three answers to "was a same-second reading accepted after this
#: work?" (fix round K6, 825-832-CD6-V01). Unknown is never "later": K5
#: answered an order it could not establish as later, and the fence that
#: answer built acknowledged a legitimate rise for good.
_ORDER_LATER = "later"
_ORDER_EARLIER = "earlier"
_ORDER_UNKNOWN = "unknown"

#: Why a partition's pass stopped at work of unknown same-second order.
_ORDER_UNKNOWN_REASON = "same-second order not yet established"

#: Why a partition's pass stopped at work whose baseline cannot be
#: established (fix round K10, review finding P1-2).
_BASELINE_UNKNOWN_REASON = "baseline not yet established"


def _evidence_retry_at(classified) -> "int | None":
    """The earliest instant wall time alone makes an unknown axis of
    ``classified``'s projection admissible (fix round K12, K11R-10), or
    None when no unknown axis depends on time."""
    evidence = getattr(getattr(classified, "projection", None),
                       "evidence", None) or {}
    times = [item.admissible_at for item in evidence.values()
             if isinstance(item, _candidates.AxisEvidence)
             and item.admissible_at is not None]
    return min(times) if times else None

#: How far before the month of a reading's capture its observation's
#: segment can lie when no journal point bounds the scan: an observation is
#: appended at or after its capture, into the segment of its append's UTC
#: month, and a month of slack covers a clock stepped back across a month
#: boundary.
_SAME_SECOND_SCAN_SLACK_S = 31 * 86400


def _same_second_order(account_key: str, axis: str, at: int,
                       bound: "tuple[str, int] | None") -> str:
    """Whether an accepted reading of ``account_key`` on ``axis`` captured in
    second ``at`` was accepted AFTER the statusline work whose journal point
    is ``bound`` (fix rounds K5 and K6, 825-832-CD5-D02 and CD6-V01): a
    frozen plan's classified high water, or the journal end an evaluation
    was enrolled at. Returns `_ORDER_LATER`, `_ORDER_EARLIER` or
    `_ORDER_UNKNOWN`.

    Captures keep one-second resolution, so the instant alone cannot order
    such a reading against that work. Its journal position can, and it is
    the same in live ingest and in any rebuild: a Claude usage observation of
    the account captured in that second, with a value on the axis, that
    starts at or past ``bound`` (or straddles it) was appended after the
    work existed — later. A statusline publication is not one: the
    partition publishes its evaluations in recorded order, so a publication
    already in the journal came from earlier work. With a bound and no such
    observation past it, every same-second reading came first — earlier.

    Without a bound (an evaluation enrolled while its journal listing
    failed, or a version 1 line) the order of any such observation is
    unknown, so the scan covers every segment the observation could be in:
    finding none proves the order irrelevant — earlier — and finding one
    leaves it unknown. A journal that cannot be listed or read is unknown
    too: the caller retains the work and retries on a later event rather
    than guess (C5). The scan streams lines, reads only the bytes appended
    since ``bound``, the way `_publication_appended` does, and runs only
    when an instant ties; it is never on the render path (C4).

    Fix round K7 (Codex round 14). Only an ACCEPTED reading orders anything
    (825-832-SR4-001): an observation the authoritative admission predicate
    excludes — a previous release's unstamped write after the cutover —
    never reached any projection, so it is neither later nor conflicting.
    And a recorded bound is ordering proof only while its segment still
    exists with at least the recorded size (825-832-SR4-002): a restored
    empty future segment that a current append's recovery removed no longer
    marks where later appends land — they go to the current month, which
    sorts before it. Such a bound is no bound: the scan then runs as for an
    unknown point, so a conflicting reading leaves the order unknown, never
    earlier.

    Fix round K8 (825-832-SR4-001, Codex verification of K7). The admission
    boundary must itself be available. Since fix round K9
    (825-832-CD3-J02) `find_account_bound_cutover` raises
    `AccountBoundCutoverUnavailable` (an OSError) when its scan could not
    read a segment, and that is unknown order here. It still answers None
    for an absent cutover, and `_is_admitted` admits every record against
    None. Outside
    a store still awaiting its one-time journal cutover — the only state in
    which the reducer proceeds without establishing the boundary, and in
    which every record is historical prefix — a None boundary is therefore
    unavailable, and a same-second reading found without it leaves the
    order unknown. Finding none still proves the order irrelevant."""
    import _cctally_journal
    import _lib_journal

    field = "five_hour_percent" if axis == "fiveHour" else "weekly_percent"
    needle = dt.datetime.fromtimestamp(int(at), tz=dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S").encode("ascii")
    try:
        segments = _cctally_journal.list_segments()
        admission = _cctally_journal.find_account_bound_cutover()
        # Fix round K8 (825-832-SR4-001): a failed lookup raises (fix round
        # K9) and lands in the OSError branch below; None means no cutover
        # was found. Only a store still awaiting its one-time journal
        # cutover can legitimately have none here — the reducer establishes
        # the boundary before it classifies anything (spec 5.5) — so a None
        # anywhere else is an UNAVAILABLE boundary, never permission to
        # admit: a same-second reading found without one is unknown order.
        unavailable = (admission is None
                       and not _cctally_journal._stats_journal_cutover_pending())
    except (OSError, ValueError):
        return _ORDER_UNKNOWN
    if bound is not None and bound[0]:
        try:
            valid = (bound[0] in segments and os.stat(
                _cctally_core.JOURNAL_DIR / bound[0]).st_size >= bound[1])
        except FileNotFoundError:
            valid = False
        except OSError:
            return _ORDER_UNKNOWN
        if not valid:
            bound = None
    start_segment = None if bound is None else (bound[0] or None)
    start_key = (None if start_segment is None
                 else _lib_journal.segment_sort_key(start_segment))
    floor = None
    if bound is None:
        floor = _lib_journal.segment_sort_key(_lib_journal.segment_name(
            dt.datetime.fromtimestamp(int(at) - _SAME_SECOND_SCAN_SLACK_S,
                                      tz=dt.timezone.utc)))
    for segment in segments:
        key = _lib_journal.segment_sort_key(segment)
        if start_key is not None and key < start_key:
            continue
        if floor is not None and key[0] == floor[0] and key < floor:
            continue
        offset = bound[1] if segment == start_segment else 0
        try:
            with open(_cctally_core.JOURNAL_DIR / segment, "rb") as handle:
                position = _line_start_at_or_before(handle, offset)
                handle.seek(position)
                for line in handle:
                    line_start, position = position, position + len(line)
                    if (needle in line and line.endswith(b"\n")
                            and _is_same_second_reading(
                                line, account_key=account_key, field=field,
                                at=at, coordinate=(segment, line_start),
                                admission=admission)):
                        return (_ORDER_UNKNOWN if bound is None or unavailable
                                else _ORDER_LATER)
        except OSError:
            return _ORDER_UNKNOWN
    return _ORDER_EARLIER


def _is_same_second_reading(line: bytes, *, account_key: str, field: str,
                            at: int, coordinate: "tuple[str, int]",
                            admission) -> bool:
    """Whether one complete journal line, starting at ``coordinate``, is an
    accepted Claude usage observation of ``account_key`` captured in second
    ``at`` with a value in ``field``, and not a statusline publication.
    Accepted means admitted by the one admission predicate current ingest
    applies (spec 5.3) against the cutover boundary ``admission`` (fix
    round K7, 825-832-SR4-001)."""
    import _cctally_journal
    import _lib_accounts
    import _lib_journal

    rec = _lib_journal.decode_line(line)
    if not isinstance(rec, dict) or rec.get("t") != "obs":
        return False
    payload = rec.get("payload")
    if (rec.get("provider") != "claude" or not isinstance(payload, dict)
            or (rec.get("account") or _lib_accounts.UNATTRIBUTED) != account_key
            or payload.get(field) is None
            or payload.get("publication_id") is not None):
        return False
    try:
        if _epoch_from_iso(payload.get("captured_at")) != int(at):
            return False
    except (TypeError, ValueError):
        return False
    return _cctally_journal._is_admitted(
        rec, coordinate[0], coordinate[1], admission)


def _publication_supersession(plan: dict, *, account_key: str, now_epoch: int,
                              projection: "_candidates.DbProjection | None" = None,
                              ) -> str:
    """``"superseded"``, ``"current"`` or ``"unknown"`` (fix round K6,
    825-832-CD6-V01).

    An authoritative publication for the SAME account and window committed
    after this plan was captured supersedes it — and so does any reading of
    the same account and axis that ``projection`` (the plan's covered
    classification) shows was captured after the plan's instant, fenced or
    not, in any window (fix round K4, 825-832-CD5-D02). The record kernel's
    detector takes the latest predecessor BY CAPTURE TIME, so appending the
    plan behind such a reading would compare the later reading with the
    earlier one: the manual `record-usage` of 100 after a frozen 70 became a
    30-point credit, and an OAuth reading in the next week a reset back into
    the plan's week. The uniform trace published the plan before that
    reading existed, so neither outcome is in it.

    A reading captured in the plan's own second is later too when its
    observation lies at or past the high water the plan's classification
    covered (fix round K5): it was appended after the plan was decided, and
    the detector orders equal instants by row, so it would still become the
    plan's predecessor. One captured in that second but appended before the
    high water was part of the classification the plan was decided against,
    and does not supersede it (`_same_second_order`). When that order
    cannot be established and nothing else proves supersession, the answer
    is unknown: the plan is neither published nor superseded until a later
    pass can tell."""
    captured = int(plan["capturedAt"])
    unknown = False
    for axis in _candidates.AXES:
        value = plan[axis]
        if value is None:
            continue
        if projection is not None:
            later = _candidates._projection_axis(projection, axis)
            if later is not None and later.captured_at > captured:
                return "superseded"
            if later is not None and later.captured_at == captured:
                order = _same_second_order(
                    account_key, axis, captured,
                    _journal_point(plan.get("journalHighWater")))
                if order == _ORDER_LATER:
                    return "superseded"
                unknown = unknown or order == _ORDER_UNKNOWN
        fence = _read_tombstone(axis, account_key=account_key,
                                window=value["canonicalKey"], now_epoch=now_epoch)
        if fence is None:
            continue
        if fence.state == "inflight":
            return "superseded"
        if plan["capturedAt"] <= (fence.block_received_at_through or 0):
            return "superseded"
    return "unknown" if unknown else "current"


def _publication_superseded(plan: dict, *, account_key: str, now_epoch: int,
                            projection: "_candidates.DbProjection | None" = None,
                            ) -> bool:
    """Whether supersession of the plan is PROVEN
    (`_publication_supersession`); an unknown order is not."""
    return _publication_supersession(
        plan, account_key=account_key, now_epoch=now_epoch,
        projection=projection) == "superseded"


def _commit_publication(transition: _Transition, plan: dict, *, before, classified,
                        now_epoch: int, refreshed_control=None,
                        members=None) -> _Transition:
    """Acknowledge one publication (#825/#832 spec 4.3 revision 2, Codex
    design-fix-round-3 Q4): replace the partition's transition with the
    committed control, read from ``classified`` — a covered classification
    taken AFTER the append, so the observation's derivation is complete — and
    advance the intake checkpoint past the plan's evaluation in the SAME
    atomic write. Nothing else is evidence of completion: not the record
    kernel's exit code, not an opportunistic ``ran=False``, not the raw
    observation alone.

    #797 spec 4.4 (D7b q3): this is the publication's completion, so the
    receipts its evaluation's ``members`` earn (read back from the plan's
    intake evaluation when not given) are installed in the same write, merged
    into the CURRENT receipt map revalidated against ``classified`` — never a
    map restored from when the plan was frozen."""
    after = classified.projection
    if members is None:
        members = _plan_members(transition.account_key, plan)
    receipts, stale_cache = _completion_receipts(transition, classified, members)
    if refreshed_control is None:
        control = _candidates.validate_control_document(plan["control"], now_epoch=now_epoch)
        refreshed_control = dataclasses.replace(control, db_projection=after)
    else:
        refreshed_control = refreshed_control(after)
    committed = _replace_transition(
        transition, account_key=transition.account_key,
        binding=transition.binding, projection=after,
        control=refreshed_control, publication=None,
        acknowledged=max(transition.intake_acknowledged, plan["intakeSequence"]),
        coverage=_coverage_document(classified), receipts=receipts,
        stale_cache=stale_cache)
    committed = _fit_transition(committed, prior=transition.receipts,
                                referenced=_referenced_tokens(committed, members))
    _write_transition(committed)
    _crash_point("after-checkpoint")
    if before is None or _projection_changed(before, after):
        _cctally()._statusline_observe_touch(binding=transition.binding)
    return committed


@dataclasses.dataclass(frozen=True)
class _Resolution:
    """One step of a partition's ordered work: the transition it left, the
    covered classification later steps may reuse (None when they must
    reclassify), its status, and — when the partition must stop here — the
    result to report."""

    transition: object
    classified: "_Classified | None"
    status: str
    stop: "AccountResult | None" = None
    decision: object = None


def _blocked(account_key: str, status: str, exc: "_Unclassifiable",
             decision=None) -> AccountResult:
    return AccountResult(account_key, status, decision, exc.reason,
                         blockers=exc.blockers, obligations=exc.obligations)


def _frontier_conflict(account_key: str, bound, exc, decision=None) -> AccountResult:
    """The journal advanced past the checked frontier between the coverage
    check and the append. The frozen plan stays durable, so the next pass's
    recovery revalidates it under the SAME publication identity. Only a
    journal that really grew past the bound is a frontier event — any other
    append failure manufactures no wake."""
    current = _journal_high_water()
    advanced = current is not None and (current["segment"], current["offset"]) != (
        tuple(bound) if bound is not None else None)
    return AccountResult(account_key, "deferred", decision, str(exc),
                         blockers=frozenset({"frontier"}) if advanced else frozenset())


def _acknowledge_publication(transition, plan: dict, *, frontier, before, status,
                             refreshed_control=None, decision=None) -> _Resolution:
    """Finish an appended publication without a fresh append: prove the
    complete observation is in the journal, then that one covered snapshot
    has folded it, then commit its control transition and checkpoint."""
    account_key = transition.account_key
    try:
        appended = _publication_appended(plan)
    except _PublicationScanUnavailable:
        # Unknown, never "not appended" (K9K10-R12): the frozen plan waits
        # for a due retry of the scan.
        return _Resolution(transition, None, "deferred", AccountResult(
            account_key, "deferred", decision, _PUBLICATION_SCAN_REASON))
    if not appended:
        # The frozen plan survives; its recovery re-publishes it under the
        # same identity.
        return _Resolution(transition, None, "deferred", AccountResult(
            account_key, "deferred", decision, "publication not observed"))
    try:
        classified = _classify(account_key, frontier, at=int(plan["capturedAt"]))
    except _Unclassifiable as exc:
        # Appended, but its derivation is unproven: no later evaluation may
        # use it as a predecessor yet.
        return _Resolution(transition, None, "deferred",
                           _blocked(account_key, "deferred", exc, decision))
    committed = _commit_publication(
        transition, plan, before=before, classified=classified,
        now_epoch=int(time.time()), refreshed_control=refreshed_control)
    return _Resolution(committed, classified, status, decision=decision)


def _publish_plan(transition, plan: dict, binding, classified: "_Classified", *,
                  frontier, nudge_sink, before, status, refreshed_control=None,
                  decision=None) -> _Resolution:
    """Append one frozen plan, bound to its classified high water, and
    acknowledge it. The plan and its intake reference are durable before the
    record kernel runs."""
    import _cctally_journal

    account_key = transition.account_key
    _crash_point("before-append")
    # 6f mode wiring (P2 exit-code fix): the statusline persist fork's publish
    # step ingests OPPORTUNISTICALLY. A render tick must NEVER crash on a
    # derivation error — the opportunistic path swallows a cycle exception
    # (loud log only) instead of propagating.
    try:
        rc = _cctally().cmd_record_usage(
            _plan_record_args(plan), binding=binding, publication=plan,
            ingest_mode="opportunistic", nudge_sink=nudge_sink,
            **_bound_append_kwargs(classified.high_water))
    except _cctally_journal.JournalError as exc:
        return _Resolution(transition, None, "deferred", _frontier_conflict(
            account_key, classified.high_water, exc, decision))
    except _cctally().StatsRebuildDeferred as exc:
        # Appended, and then its own ingest found the index rebuilding. The
        # frozen plan recovers the appended observation later.
        deferral = _rebuild_unclassifiable()
        return _Resolution(transition, None, "deferred", AccountResult(
            account_key, "deferred", decision, str(exc),
            blockers=deferral.blockers, obligations=deferral.obligations))
    if rc != 0:
        # A deterministic rejection (an implausible reset): a terminal
        # disposition. The pre-attempt control stays — no retry is consumed —
        # and the evaluation is acknowledged so it cannot block later work.
        rejected = _replace_transition(
            transition, account_key=account_key, binding=transition.binding,
            projection=transition.control.db_projection, publication=None,
            acknowledged=max(transition.intake_acknowledged, plan["intakeSequence"]))
        _write_transition(rejected)
        return _Resolution(rejected, None, "record_failed", decision=decision)
    _crash_point("after-append")
    return _acknowledge_publication(
        transition, plan, frontier=frontier, before=before, status=status,
        refreshed_control=refreshed_control, decision=decision)


def _resolve_frozen_publication(transition: _Transition, *, now_epoch: int, nudge_sink,
                                frontier: "_PublicationFrontier | None") -> _Resolution:
    """Resolve a partition's frozen publication plan under its ORIGINAL
    binding, captured instant and publication identity — the active identity
    is never consulted (M16) — BEFORE any later evaluation is reduced
    (#825/#832 spec 4.3 revision 2, Codex design-fix-round-3 Q3).

    An appended observation is only derived and committed (M17: never a
    second observation). An unappended plan is classified exactly like a
    fresh reduction (spec 4.3 revision 1, M16): unclassifiable, it stays
    durable under the same identity. A drop the control gate withholds, or a
    plan a same-account authoritative fence committed after its capture
    supersedes, gets an explicit disposition — the plan is dropped and its
    evaluation, still unacknowledged, is reduced again against the new state,
    so no other axis's reading is discarded with it."""
    import _lib_account_binding

    plan = transition.publication
    account_key = transition.account_key
    binding = _lib_account_binding.load(plan["binding"])
    before = transition.control.db_projection
    try:
        appended = _publication_appended(plan)
    except _PublicationScanUnavailable:
        # Fail closed (K9K10-R12): whether the plan was appended is unknown,
        # so it is neither re-published nor acknowledged. It stays frozen
        # under its identity and waits for a due retry of the scan.
        return _Resolution(transition, None, "deferred", AccountResult(
            account_key, "deferred", None, _PUBLICATION_SCAN_REASON))
    if appended:
        return _acknowledge_publication(
            transition, plan, frontier=frontier, before=before, status="recovered")
    try:
        # Classified at the plan's ORIGINAL instant (Q2).
        classified = _classify(account_key, frontier, at=int(plan["capturedAt"]))
    except _Unclassifiable as exc:
        return _Resolution(transition, None, "deferred",
                           _blocked(account_key, "deferred", exc))
    # #797 spec 4.4 (T4): an unappended plan's dependencies are revalidated
    # before publication. A plan whose selected contributor the current facts
    # refuse is dropped like a superseded plan, earning nothing, and its
    # evaluation, still unacknowledged, is reduced again with those facts.
    facts = _stale_member_facts(classified.stale, transition.receipts,
                                _plan_members(account_key, plan))
    if _plan_winner_refused(plan, facts):
        dropped = _replace_transition(
            transition, account_key=account_key, binding=transition.binding,
            projection=before, publication=None)
        _write_transition(dropped)
        return _Resolution(dropped, classified, "superseded")
    refused = _refused_tokens(facts)
    authority = _plan_drop_authority(
        plan, classified.projection,
        receipts=lambda: _plan_axis_receipts(account_key, plan, refused=refused))
    if authority == _PLAN_STALE:
        # Spec 4.3 revision 6 (*Floor retired*, *Stale expired window*):
        # every reading the plan publishes on some axis is older than that
        # axis's admitted floor or expired predecessor, so the plan is stale.
        # It is dropped like a superseded plan and its evaluation, still
        # unacknowledged, is reduced again against the current state.
        dropped = _replace_transition(
            transition, account_key=account_key, binding=transition.binding,
            projection=before, publication=None)
        _write_transition(dropped)
        return _Resolution(dropped, classified, "superseded")
    if authority == _PLAN_UNKNOWN:
        # Spec 4.3 revision 5 (design-fix-round-5 Q1 and Q2): an axis the
        # covered classification cannot classify is unknown work, not a
        # withheld drop. The plan stays frozen under its publication
        # identity, nothing is acknowledged, and a later pass revalidates it
        # once its evidence is established (case M16). Fix round K13 (review
        # cycle 7 P1-2): whatever the gate — an open gate is no
        # classification evidence, and a classified drop on the other axis
        # never outranks the unknown one.
        return _Resolution(transition, classified, "deferred", AccountResult(
            account_key, "deferred", None, _BASELINE_UNKNOWN_REASON,
            retry_at=_evidence_retry_at(classified)))
    if authority is not None and not _drop_permitted(classified, transition, frontier):
        # #825/#832 spec 5.6 item 7: a plan frozen before a trigger must not
        # publish a withheld drop after it. Dropped durably without spending
        # an attempt; the pending drop itself stays in the control state.
        withheld = _replace_transition(
            transition, account_key=account_key, binding=transition.binding,
            projection=before, publication=None,
            quarantine={
                "authorityCommit": (
                    None if transition.quarantine is None
                    else transition.quarantine.get("authorityCommit")),
                "dropWithheld": True})
        _write_transition(withheld)
        return _Resolution(withheld, classified, "withheld")
    supersession = _publication_supersession(
        plan, account_key=account_key, now_epoch=now_epoch,
        projection=classified.projection)
    if supersession == "unknown":
        # Fix round K6 (825-832-CD6-V01): neither published nor superseded
        # on an order that cannot be established. The plan stays frozen under
        # its identity, and a later pass decides once the order is readable
        # or a later-second reading supersedes it.
        return _Resolution(transition, classified, "deferred", AccountResult(
            account_key, "deferred", None, _ORDER_UNKNOWN_REASON))
    if supersession == "superseded":
        dropped = _replace_transition(
            transition, account_key=account_key, binding=transition.binding,
            projection=before, publication=None)
        _write_transition(dropped)
        return _Resolution(dropped, classified, "superseded")
    return _publish_plan(transition, plan, binding, classified, frontier=frontier,
                         nudge_sink=nudge_sink, before=before, status="recovered")


#: `_plan_drop_authority` answers.
_PLAN_DROP = "drop"
_PLAN_UNKNOWN = "unknown"
_PLAN_STALE = "stale"


def _plan_axis_receipts(account_key: str, plan: dict, *,
                        refused=None) -> "dict | None":
    """Per axis, the (window, percent, receipt instant) of every member of
    the plan's own intake evaluation (fix round K14c): what a floor-retired
    window needs to decide whether the plan's reading was captured at or
    after the floor. None when the evaluation cannot be read back. A member
    ``refused`` (axis to tokens, #797 spec 4.4) on an axis is excluded from
    that axis's post-floor set, exactly as `CLASS_FLOOR` excludes it."""
    refused = refused or {}
    sequence = int(plan["intakeSequence"])
    try:
        evaluations = list(_intake_evaluations(
            account_key, after=sequence - 1, through=sequence))
    except (OSError, _candidates.StateValidationError):
        return None
    if len(evaluations) != 1 or evaluations[0].sequence != sequence:
        return None
    receipts: dict = {axis: [] for axis in _candidates.AXES}
    for member in evaluations[0].members:
        for axis in _candidates.AXES:
            value = _candidates._candidate_axis(member, axis)
            if value is not None and member.token not in refused.get(axis, ()):
                receipts[axis].append(
                    (value.canonical_key, value.raw_resets_at, value.percent,
                     int(member.received_at)))
    return receipts


def _plan_reading_after_floor(plan: dict, axis: str, evidence, receipts) -> "bool | None":
    """Whether the plan's ``axis`` reading in a floor-retired window was
    captured at or after the floor (spec 4.3 revision 6): some member of the
    plan's evaluation carries that window and value and was received at or
    after it. A value the plan filled from the projection or the retained
    low predates the floor by construction. None when unknown."""
    value = plan[axis]
    provenance = (plan.get("provenance") or {}).get(axis) or {}
    if provenance.get("kind") != "candidate":
        return False
    loaded = receipts()
    if loaded is None:
        return None
    # A member's own key may still be unassigned (the reduction keys it
    # against the anchor); its raw reset then identifies the reading.
    return any(
        (int(value["canonicalKey"]) == key if key is not None
         else int(value["rawResetsAt"]) == raw)
        and percent == float(value["percent"])
        and received >= int(evidence.floor_epoch)
        for key, raw, percent, received in loaded[axis])


def _plan_drop_authority(plan: dict, projection: "_candidates.DbProjection", *,
                         receipts=None) -> "str | None":
    """Whether a frozen plan needs drop authority, revalidated against the
    plan's covered classification (spec 4.3 revision 5): ``"drop"`` when it
    publishes some axis BELOW the classified database baseline of the same
    window (a drop-consensus outcome); else ``"unknown"`` when some axis it
    publishes has no classified baseline and no classification evidence
    makes it a first reading or a new window — an absent projection alone
    never authorizes a publication the control gate withholds (Codex
    fix-round-4 Q2), and never classifies one either; else None.

    Fix round K13 (review cycle 7 P1-2): ``"unknown"`` outranks ``"drop"``.
    A classified drop on one axis beside unknown evidence on the other is
    still unknown work (spec 4.3: an unknown axis beside a drop defers the
    whole evaluation and keeps the frozen identity), never a withheld drop
    whose disposition would discard the plan.

    Fix round K14c (spec 4.3 revision 6): ``"stale"`` when some published
    axis's reading is older than that axis's floor-retired group or expired
    five-hour predecessor (`_candidates.classify_absent_axis`); a reading in
    the floor-retired group itself publishes when it was captured at or
    after the floor (``receipts``, the plan's own evaluation members), and is
    stale otherwise. ``"unknown"`` outranks ``"stale"``, which outranks
    ``"drop"``."""
    unknown = stale = drop = False
    for axis in _candidates.AXES:
        value = plan.get(axis)
        if value is None:
            continue
        db_axis = _candidates._projection_axis(projection, axis)
        if db_axis is None:
            evidence = _candidates.axis_evidence(projection, axis)
            disposition = _candidates.classify_absent_axis(
                evidence, int(value["canonicalKey"]), axis=axis)
            if disposition == _candidates.CLASS_FLOOR:
                after = (None if receipts is None
                         else _plan_reading_after_floor(plan, axis, evidence, receipts))
                disposition = (_candidates.CLASS_UNKNOWN if after is None
                               else _candidates.CLASS_PUBLISH if after
                               else _candidates.CLASS_STALE)
            if disposition == _candidates.CLASS_UNKNOWN:
                unknown = True
            elif disposition == _candidates.CLASS_STALE:
                stale = True
            continue
        if (db_axis.canonical_key == value["canonicalKey"]
                and float(value["percent"]) < db_axis.percent):
            drop = True
    if unknown:
        return _PLAN_UNKNOWN
    if stale:
        return _PLAN_STALE
    return _PLAN_DROP if drop else None


def _partition_binding(candidates, transition):
    if candidates:
        newest = max(candidates, key=lambda c: (c.received_at, c.token))
        return newest.binding
    return None if transition is None else transition.binding


def _partition_tombstones(account_key, candidates, control, projection, *, now_epoch):
    """Each axis's fence for the window this partition is deciding."""
    tombstones = {}
    for axis in _candidates.AXES:
        values = [v for c in candidates
                  if (v := _candidates._candidate_axis(c, axis)) is not None]
        pending = control.pending_drops.get(axis)
        db_axis = _candidates._projection_axis(projection, axis)
        window = max(
            (v.canonical_key for v in values),
            default=(pending.canonical_key if pending is not None
                     else (db_axis.canonical_key if db_axis is not None else None)),
        )
        tombstones[axis] = (
            None if window is None else _read_tombstone(
                axis, account_key=account_key, window=window, now_epoch=now_epoch))
    return tombstones


def _later_reading_fence(axis: str, tombstone, db_axis, *, at: int,
                         same_second_later: bool = False):
    """The fence a reduction at instant ``at`` applies on ``axis``: the
    account's real fence, tightened when the projection holds a reading
    captured AFTER ``at`` (fix round K4, 825-832-CD5-D02).

    Only an evaluation that a later reading overtook reaches that case — a
    retained evaluation, or the one a superseded frozen plan leaves behind,
    reduced at its original instant against a projection the later reading
    has since moved. An evaluation reduced at or after the newest accepted
    capture keeps exactly its real fence, so the live tick, which reduces
    at its own instant, is unchanged. The uniform trace reduced that evaluation before
    the later reading existed; comparing its candidates with it instead
    arms a pending drop from evidence the reading superseded, and the
    drop's deadline then publishes it — behind the later reading, whose
    capture time makes it the detector's predecessor — as a credit nobody
    observed. So a candidate received before that capture is ineligible,
    exactly as a committed fence would make it, whether or not an
    authoritative writer fenced the reading. An inflight fence stays
    inflight, and a committed one is only ever extended.

    A reading captured in the evaluation's own second is later when
    ``same_second_later`` says its observation entered the journal after the
    evaluation was enrolled (fix round K5, 825-832-CD5-D02). Every member of
    the evaluation was received by then, so the fence reaches the instant
    itself. An axis whose order cannot be established is passed here only
    to compare the fenced reduction with the unfenced one (fix round K6,
    825-832-CD6-V01): the evaluation is decided only when both agree."""
    if db_axis is None or db_axis.captured_at < at or (
            db_axis.captured_at == at and not same_second_later):
        return tombstone
    if tombstone is not None and tombstone.state != "committed":
        return tombstone
    cutoff = db_axis.captured_at - 1 if db_axis.captured_at > at else at
    if tombstone is not None and (tombstone.block_received_at_through or 0) >= cutoff:
        return tombstone
    return _candidates.Tombstone(axis=axis, state="committed",
                                 block_received_at_through=cutoff)


def _reduce_partition(account_key, candidates, transition, *, now_epoch, projection,
                      drop_consensus_allowed=True, fence_epoch=None,
                      same_second_later=frozenset(), member_facts=None):
    """One partition's pure transition: (decision, anchor, canonical
    candidates, tombstones). Only this account's candidates, control,
    anchor and fences are inputs. ``now_epoch`` is the reduction instant (a
    replayed retained evaluation's own); fences are read at ``fence_epoch``
    (the wall clock), since a fence written after a retained instant is
    still a fence for it. ``same_second_later`` names the axes whose
    projected reading shares the instant and was accepted after this
    evaluation (`_later_reading_fence`); only the detached reducer, which
    can read the journal, ever supplies it. ``member_facts`` are the
    per-axis refusal facts (#797 spec 4.4); only the detached reducer, which
    reads the provider samples, ever supplies them."""
    control = (
        _candidates.ControlState(projection, transition.control.pending_drops)
        if transition is not None else _empty_control(projection)
    )
    prior = _retained_anchor(
        projection, None if transition is None else transition.five_hour_anchor)
    active = _canonicalize_candidates(candidates, prior)
    anchor = _rolling_anchor(active, prior)
    tombstones = _partition_tombstones(
        account_key, active, control, projection,
        now_epoch=now_epoch if fence_epoch is None else fence_epoch)
    tombstones = {
        axis: _later_reading_fence(
            axis, tombstones.get(axis),
            _candidates._projection_axis(projection, axis), at=now_epoch,
            same_second_later=axis in same_second_later)
        for axis in _candidates.AXES}
    decision = _candidates.reduce_candidates(
        active, db=projection, control=control, tombstones=tombstones,
        now_epoch=now_epoch, account_key=account_key,
        drop_consensus_allowed=drop_consensus_allowed, member_facts=member_facts)
    return decision, anchor, active, tombstones


@dataclasses.dataclass(frozen=True)
class _PublicationFrontier:
    """The journal a publication planned from one reducer snapshot must have
    folded before it may publish a drop (#825/#832 fix-round-1 Q3).

    ``extents`` is every segment and its byte size, in canonical order,
    captured right after the legacy-spool rescan — so it includes any
    `legacy_spool_activity` op that rescan appended. ``local_activity`` is
    True when this pass appended one: every certificate is then void at a
    position no index has folded yet, so a drop is withheld outright."""

    extents: tuple
    local_activity: bool


#: How many opportunistic ingest attempts classification makes before the
#: pass is unknown classification: a cycle consumes up to the high water it
#: snapshotted, and the events it emits land past it, so a second cycle
#: normally completes the prefix. Never an authoritative wait: the caller
#: holds the selected-state lock. Waiting is the continuation's job, after
#: that lock is released.
_DROP_GATE_INGEST_ATTEMPTS = 3
#: An appended line inside an earlier segment's post-cutover growth is found
#: by its newline within this many bytes; a longer newline-free tail counts as
#: growth too (fail closed).
_EARLIER_GROWTH_PROBE_BYTES = 64 * 1024

_UNCOVERED = object()


def _journal_extents() -> tuple:
    """Every journal segment and its size, in canonical order, read under the
    journal's leaf lock so no append is half-observed."""
    import _cctally_journal

    lock_fd = _cctally_journal._acquire_leaf_lock()
    try:
        return tuple(
            (segment, os.path.getsize(_cctally_core.JOURNAL_DIR / segment))
            for segment in _cctally_journal.list_segments())
    finally:
        _cctally_journal._release_leaf_lock(lock_fd)


def _capture_publication_frontier(*, local_activity: bool) -> "_PublicationFrontier | None":
    try:
        return _PublicationFrontier(_journal_extents(), bool(local_activity))
    except OSError:
        return None


class _JournalEnumerationError(OSError):
    """The journal directory opened, but enumerating or statting its segments
    failed (fix round K7, 825-832-CD6-V01). Deliberately not a
    `FileNotFoundError`, even when one caused it: a segment that vanished
    after the listing named it says nothing about whether the journal is
    empty."""


def _journal_extents_unlocked() -> tuple:
    """`_journal_extents` for the RENDER path (C4): the same canonical
    segments and sizes from one directory listing and one stat each — no
    journal file is opened and no lock is taken. An append only grows a
    segment and a new segment is a new name, so a listing equal to a proven
    one shows that nothing landed since; a half-written append differs, and
    so fails closed.

    Opening the directory raises its own error (`FileNotFoundError` when the
    journal directory does not exist); any failure after that, while
    enumerating or statting a segment, raises `_JournalEnumerationError`."""
    import _lib_journal

    found = []
    with os.scandir(_cctally_core.JOURNAL_DIR) as entries:
        try:
            for entry in entries:
                name = entry.name
                if not name.endswith(".jsonl") or not (
                        name.startswith(_lib_journal.BOOTSTRAP_PREFIX)
                        or name.startswith(_lib_journal.SEGMENT_PREFIX)):
                    continue
                if entry.is_file():
                    found.append((name, entry.stat().st_size))
        except OSError as exc:
            raise _JournalEnumerationError(
                f"journal segment enumeration failed: {exc}") from exc
    return tuple(sorted(found, key=lambda item: _lib_journal.segment_sort_key(item[0])))


def _coverage_digest(db_files, extents) -> str:
    """One digest of an index fingerprint and the journal extents a snapshot
    of THAT index was proven to cover."""
    files = {name: _fingerprint_document(value)
             for name, value in sorted((db_files or {}).items())}
    return hashlib.sha256(json.dumps(
        {"dbFiles": files, "extents": [[name, int(size)] for name, size in extents]},
        sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()


def _coverage_document(classified: "_Classified") -> "dict | None":
    """The journal coverage proof a transition records for the projection of
    ``classified`` (review finding P1-2, cycle 3): the digest of its index
    fingerprint with the journal extents its snapshot covered, and the
    inclusive range of evaluation instants a render may reduce against it.
    A render may acknowledge an evaluation only while the live fingerprint
    and the unlocked journal listing still hash to the same digest — so no
    journal growth the index has not folded can hide behind an unchanged
    fingerprint."""
    if not classified.extents or classified.render_valid is None:
        return None
    low, high = classified.render_valid
    return {
        "digest": _coverage_digest(classified.projection.db_files, classified.extents),
        "from": None if low == -math.inf else int(low),
        "through": None if high == math.inf else int(high),
    }


def _extents_extend(required: tuple, current: tuple) -> bool:
    """Whether ``current`` is ``required`` plus appends at or after its last
    segment: every earlier segment is byte-for-byte the same size and no new
    segment sorts below the frontier. A change inside an earlier segment or a
    new bootstrap segment invalidates prefix coverage, which the largest
    coordinate alone cannot show (F4)."""
    import _lib_journal

    if not required:
        return True
    have = dict(current)
    last_segment, last_size = required[-1]
    for segment, size in required[:-1]:
        if have.get(segment) != size:
            return False
    if have.get(last_segment, -1) < last_size:
        return False
    floor = _lib_journal.segment_sort_key(last_segment)
    known = dict(required)
    return all(_lib_journal.segment_sort_key(segment) >= floor
               for segment in have if segment not in known)


def _earlier_segment_growth(boundary, current: tuple) -> bool:
    """Whether a segment the cutover froze, sorting before the cutover's own
    segment, now holds complete lines past its frozen extent — or a segment
    absent at the cutover sorts below it. Live ingest's scalar cursor never
    revisits such a segment, so no index can have folded those records."""
    import _lib_journal

    floor = _lib_journal.segment_sort_key(boundary.segment)
    for segment, size in current:
        if _lib_journal.segment_sort_key(segment) >= floor:
            continue
        extent = boundary.frozen_extents.get(segment)
        if extent is None:
            return True
        if size <= extent:
            continue
        if size - extent > _EARLIER_GROWTH_PROBE_BYTES:
            return True
        try:
            with open(_cctally_core.JOURNAL_DIR / segment, "rb") as handle:
                handle.seek(extent)
                if b"\n" in handle.read(size - extent):
                    return True
        except OSError:
            return True
    return False


def _covered_drop_status(account_key: str, frontier: _PublicationFrontier):
    """``(status, high_water, extents)`` from ONE validated stats snapshot
    (epoch and protocol identity) whose applied cursor, admission summary and
    outcome quarantine state cover the whole journal through ``frontier``; or
    `_UNCOVERED`. ``high_water`` is the checked coordinate the publication's
    append is then bound to, and ``extents`` every segment and its size that
    the snapshot was proven to cover. Raises on an unreadable index."""
    import _cctally_journal
    import _cctally_quarantine
    import _cctally_store
    import _lib_account_protocol

    conn = open_db()
    try:
        with _cctally_store.current_stats_snapshot(conn):
            cursor_row = conn.execute(
                "SELECT segment, offset, applied_segment, applied_offset "
                "FROM journal_cursor WHERE id = 1").fetchone()
            summary = _cctally_journal._read_admission_summary(conn)
            proof = _cctally_journal.read_derivation_proof(conn)
            status = _cctally_quarantine.account_status(conn, account_key)
            # Read after the snapshot opened: the journal only grows, so an
            # end equal to the snapshot's cursor proves nothing landed
            # between the two reads.
            extents = _journal_extents()
    finally:
        conn.close()
    if cursor_row is None or any(value is None for value in cursor_row):
        return _UNCOVERED
    cursor = (str(cursor_row[2]), int(cursor_row[3]))
    if (str(cursor_row[0]), int(cursor_row[1])) != cursor:
        return _UNCOVERED
    if not _cctally_journal.derivation_complete_through(proof, cursor):
        return _UNCOVERED
    if (summary is None or summary.boundary is None
            or summary.cutover_id != _lib_account_protocol.CUTOVER_OP_ID
            or summary.complete_through != cursor
            or summary.scanned_high_water != cursor):
        return _UNCOVERED
    if not extents:
        return _UNCOVERED
    # The index covers decoded records, so coverage holds once the cursor
    # reaches the last COMPLETE line. A newline-less tail read under the leaf
    # lock is a dead torn write (no append is ever half-observed there): no
    # record lies in it, an ingest may scan past it, and the next append's
    # torn-tail repair truncates it. The bound high water stays the RAW
    # extent, which is what the append's precondition compares.
    last_segment, last_size = extents[-1]
    complete = _cctally_journal._complete_line_offset(
        _cctally_core.JOURNAL_DIR / last_segment, last_size)
    if cursor[0] != last_segment or not complete <= cursor[1] <= last_size:
        return _UNCOVERED
    if not _extents_extend(frontier.extents, extents):
        return _UNCOVERED
    if _earlier_segment_growth(summary.boundary, extents):
        return _UNCOVERED
    return status, extents[-1], extents


class _Unclassifiable(Exception):
    """#825/#832 spec 4.3 revision 1: the partition's projection cannot
    classify a reduction. ``reason`` is ``rebuild`` (an epoch transition or a
    corrupt-index heal is running), ``coverage`` (no validated snapshot
    covers the journal through the pass frontier: a busy or lagging ingest),
    ``busy`` (the store guard's bounded maintenance wait expired) or
    ``unreadable``.

    Codex fix-round-4 Q3: ``obligations`` are the contended
    `_cctally_journal.LockBlock`s the failed acquisitions themselves reported
    — never a later probe — and ``unowned`` an epoch rebuild no worker owns.
    A failure no lock caused (unreadable or uncovered state, an I/O error)
    carries no obligation, so it can never manufacture a release wake."""

    def __init__(self, reason: str, obligations=(), *, unowned: bool = False):
        super().__init__(reason)
        self.reason = reason
        self.obligations = frozenset(
            block for block in obligations
            if block is not None and block.contended)
        self.unowned = bool(unowned)

    @property
    def blockers(self) -> frozenset:
        names = {block.lock for block in self.obligations}
        if self.unowned:
            names.add("rebuild-unowned")
        return frozenset(names)


@dataclasses.dataclass(frozen=True)
class _Classified:
    """An exact-account projection read from a current, validated index
    whose applied cursor covers the journal through the pass frontier, the
    account's quarantine status from that same index, and the checked journal
    high water every publication planned from it is bound to."""

    projection: "_candidates.DbProjection"
    status: dict
    high_water: tuple
    #: The evaluation instant the projection was read for, and the inclusive
    #: range of instants for which that read is identical (Q2).
    at: int
    valid: tuple
    #: Every journal segment and its size the snapshot was proven to cover,
    #: and the range of instants a later render may reduce against the
    #: projection (`_coverage_document`, P1-2 cycle 3).
    extents: tuple = ()
    render_valid: "tuple | None" = None
    #: #797 spec 4.4: the stale-contributor inputs read in the same snapshot
    #: (`_StaleInputs`), or None when they were not read.
    stale: "_StaleInputs | None" = None

    def serves(self, at: int) -> bool:
        """Whether this classification is exactly the one an evaluation at
        ``at`` would read (#825/#832 review finding P1-2, Codex fix-round-4
        Q2): never merely because it was cached for another evaluation."""
        return self.valid[0] <= int(at) <= self.valid[1]


def _heal_torn_journal_tail() -> None:
    """Heal a dead torn final line before this process ingests.

    The repair is the one every append performs first, under the same leaf
    lock, where no append is ever half-observed. It runs ahead of the
    reducer's own ingests because an ingest scans to the RAW high water: once
    its cursor sits past a torn tail, the repair truncates beneath it and the
    next appended record — a recovered plan's observation — starts below the
    cursor and is never folded. The unlocked last-byte probe keeps the
    healthy case to one read."""
    import _cctally_journal

    segments = _cctally_journal.list_segments()
    if not segments:
        return
    path = _cctally_core.JOURNAL_DIR / segments[-1]
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            if size == 0:
                return
            handle.seek(size - 1)
            if handle.read(1) == b"\n":
                return
    except OSError:
        return
    lock_fd = _cctally_journal._acquire_leaf_lock()
    try:
        segments = _cctally_journal.list_segments()
        if not segments:
            return
        fd = os.open(_cctally_core.JOURNAL_DIR / segments[-1], os.O_RDWR)
        try:
            before = os.fstat(fd).st_size
            _cctally_journal._repair_torn_tail(fd)
            if os.fstat(fd).st_size != before:
                os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        _cctally_journal._release_leaf_lock(lock_fd)


def _classify(account_key: str, frontier: "_PublicationFrontier | None", *,
              at: int) -> _Classified:
    """The projection a reduction may be classified against, or
    `_Unclassifiable` (#825/#832 spec 4.3 revision 1, Codex
    design-fix-round-2 Q2).

    Unavailable or insufficiently covered state is UNKNOWN classification:
    a deferred rebuild, a previous-release generation, or a snapshot whose
    cursor does not cover the journal through ``frontier`` proves neither a
    rise nor a drop nor an empty account. An opportunistic ingest result
    (`ran=False` for a busy lock) is never evidence; only one validated
    snapshot that demonstrably covers the prefix is — another ingester that
    completed it suffices. The projection is read between two identical
    database fingerprints around that snapshot, so it IS the covered state.

    ``at`` is the ORIGINAL instant of the evaluation (or frozen plan) being
    classified (Codex fix-round-4 Q2): the projection's five-hour baseline is
    read as that evaluation saw it, never at the recovery's wall time."""
    import _cctally_journal
    import _cctally_store

    if frontier is None:
        raise _Unclassifiable("unreadable")
    # Codex fix-round-4 Q3: the lock each failed opportunistic acquisition
    # lost to, as that try-lock itself reported it. A snapshot that stays
    # uncovered while every ingest RAN was not refused by any lock.
    blocks = set()
    for _attempt in range(_DROP_GATE_INGEST_ATTEMPTS):
        try:
            _heal_torn_journal_tail()
            result = _cctally_journal.run_stats_ingest(mode="opportunistic")
            block = getattr(result, "blocked_by", None)
            if block is not None:
                blocks.add(block)
            before = _db_file_fingerprint()
            # Spec 4.3 *Waiting heads*: the index state this classification
            # observed, after its own ingest, so the reducer's own folding
            # is never mistaken for a relevant change. Fix round K12
            # (K11R-8): only the file fingerprint; the dependency tokens
            # are read only if a wait is installed (`_settle_wait`).
            _CLASSIFIED_FINGERPRINTS[account_key] = before
            stale: list = []
            projection, valid, render_valid = _read_db_projection_at(
                account_key=account_key, at=at, stale=stale)
            checked = _covered_drop_status(account_key, frontier)
            after = _db_file_fingerprint()
        except _cctally().StatsRebuildDeferred:
            raise _rebuild_unclassifiable() from None
        except _cctally_store.StatsIndexNotCurrent:
            continue
        except Exception as exc:
            # The store guard's expired maintenance wait names the lock it
            # lost to; any other failure is unreadable state no lock caused.
            block = getattr(exc, "blocked_by", None)
            if block is not None:
                raise _Unclassifiable("busy", blocks | {block}) from None
            raise _Unclassifiable("unreadable", blocks) from None
        if checked is _UNCOVERED or before != after or after["main"] is None:
            continue
        status, high_water, extents = checked
        return _Classified(dataclasses.replace(projection, db_files=after),
                           status, high_water, int(at), valid,
                           extents=tuple(extents), render_valid=render_valid,
                           stale=stale[0] if stale else None)
    raise _Unclassifiable("coverage", blocks)


def _rebuild_unclassifiable() -> _Unclassifiable:
    """A rebuild deferral (Q3): an obligation for each detached rebuild worker
    lock its failed try-lock found held, or — when no worker holds the epoch
    rebuild that is pending — ``rebuild-unowned``, which the deferring
    process runs itself once it holds no continuation ownership."""
    import _cctally_store

    try:
        blocks = tuple(_cctally_store.stats_rebuild_worker_blocks())
    except Exception:
        blocks = ()
    unowned = False
    if not any(block.contended for block in blocks):
        try:
            unowned = bool(_cctally_store.stats_epoch_rebuild_pending())
        except Exception:
            unowned = False
    return _Unclassifiable("rebuild", blocks, unowned=unowned)


def _drop_permitted(classified: _Classified, transition,
                    frontier: "_PublicationFrontier | None") -> bool:
    """The control gate of the outcome quarantine (#825/#832 spec 5.6 item
    7). Consulted only when a reduction would publish a drop below the
    classified baseline. A `legacy_spool_activity` op this pass appended
    withholds outright. Otherwise allowed when no trigger exists, or when the
    account holds BOTH certificates and its control transition records the
    commit of the very publication that witnesses the control certificate."""
    import _cctally_quarantine

    if frontier is None or frontier.local_activity:
        return False
    commit = (None if transition is None or transition.quarantine is None
              else transition.quarantine.get("authorityCommit"))
    return _cctally_quarantine.control_recovered(classified.status, commit)


def _bound_append_kwargs(bound) -> dict:
    """The record kernel's frontier binding for a gated drop publication."""
    return {} if bound is None else {"expected_high_water": bound}


def _gated_reduction(account_key, candidates, transition, *, eval_epoch,
                     fence_epoch, classified: _Classified, frontier=None,
                     same_second_later=frozenset(), member_facts=None):
    """Reduce once with drop consensus open and once closed, against the
    classified projection. Only when they differ — a drop below the baseline
    would publish — is the quarantine gate consulted, so the ordinary tick
    pays nothing for it. Returns the reduction, whether a drop was withheld,
    and the axes whose baseline that withholding leaves unestablished
    (`_unestablished_axes`)."""
    opened = _reduce_partition(account_key, candidates, transition,
                               now_epoch=eval_epoch, fence_epoch=fence_epoch,
                               projection=classified.projection,
                               same_second_later=same_second_later,
                               member_facts=member_facts)
    closed = _reduce_partition(account_key, candidates, transition,
                               now_epoch=eval_epoch, fence_epoch=fence_epoch,
                               projection=classified.projection,
                               drop_consensus_allowed=False,
                               same_second_later=same_second_later,
                               member_facts=member_facts)
    # Fix round K13 (review cycle 7 finding P1-2): unknown classification
    # defers the whole evaluation whatever the gate says — an open gate is
    # no classification evidence (spec 4.3 *Classification evidence*).
    unknown = opened[0].unknown_axes | closed[0].unknown_axes
    if opened[0] == closed[0]:
        return opened, False, unknown
    if _drop_permitted(classified, transition, frontier):
        return opened, False, unknown
    return closed, True, _unestablished_axes(
        opened[0], closed[0], classified.projection)


def _unestablished_axes(opened, closed,
                        projection: "_candidates.DbProjection") -> frozenset:
    """Fix round K10 (review finding P1-2, spec 4.3 *Unknown
    classification*, binding fix-round-4 Q2): the axes the open gate would
    publish and the closed gate refuses where the classified projection
    holds no baseline at all. Since revision 5 (design-fix-round-5 Q1) the
    closed gate already publishes such an axis when the projection's
    classification evidence proves a first reading or a new window, so an
    axis left here is genuinely unknown: such a reading cannot be told from
    a drop below a baseline the projection does not show, so refusing it is
    no classification, and unlike a withheld drop it leaves no pending
    evidence in the control state either. The evaluation is unknown work,
    never acknowledged as a no-change reading (Q2: the whole evaluation
    defers). Fix round K13 (review cycle 7 P1-2): the kernel names such an
    axis in ``unknown_axes`` under either gate, which this includes."""
    return opened.unknown_axes | closed.unknown_axes | frozenset(
        axis for axis in _candidates.AXES
        if _candidates._projection_axis(projection, axis) is None
        and _plan_axis(opened.plan, axis) is not None
        and _plan_axis(closed.plan, axis) is None)


def _plan_axis(plan, axis: str):
    """The value a reduction's publication plan publishes on ``axis``."""
    if plan is None:
        return None
    return plan.seven_day if axis == "sevenDay" else plan.five_hour


def _repeated_attempt(transition, decision, evidence: str) -> bool:
    """Whether ``decision`` would spend the bounded REPEAT kernel attempt on
    exactly the evidence its first attempt was spent on (review finding
    CD3-V01, cycle 3; #832): every axis it publishes moves a pending drop
    from its first attempt (``ready`` or ``zero_armed``) to the final one
    (``suppressed``) under an unchanged retry signature, and the evaluation's
    membership is the one that froze the previous plan. For a zero that
    repeat is the confirmation, so a later evaluation of one unchanged
    reading would confirm a reset from no new evidence.

    This is publication deduplication only: the evaluation is still logged,
    retained and reduced at its own instant, so a deadline it crosses or a
    first attempt it earns is never lost."""
    if (transition is None or decision.action != "PUBLISH_DB"
            or transition.attempt_evidence != evidence):
        return False
    published = [axis for axis in _candidates.AXES
                 if _plan_axis(decision.plan, axis) is not None]
    for axis in published:
        before = transition.control.pending_drops.get(axis)
        after = decision.control.pending_drops.get(axis)
        if (before is None or after is None
                or before.kernel_stage not in ("ready", "zero_armed")
                or after.kernel_stage != "suppressed"
                or after.retry_signature != before.retry_signature):
            return False
    return bool(published)


def _projection_changed(before: "_candidates.DbProjection", after: "_candidates.DbProjection") -> bool:
    return before.five_hour != after.five_hour or before.seven_day != after.seven_day


def _reduce_evaluation(account_key, evaluation: _Evaluation, transition, *,
                       classified: _Classified, fence_epoch: int, nudge_sink,
                       frontier) -> _Resolution:
    """Reduce ONE logged evaluation at its original instant against the
    classified projection, and acknowledge it.

    A reduction that publishes nothing is acknowledged once its resulting
    control state — pending evidence and unchanged deadlines included — is in
    the same atomic transition write as the checkpoint. A publication is
    frozen with its intake reference first, appended bound to the classified
    high water, and acknowledged only by its complete observation plus a
    covered classification after it (spec 4.3 revision 2, Q4)."""
    members = evaluation.members
    binding = _partition_binding(members, transition)
    if binding is None:
        raise ValueError("an intake evaluation without an owning binding")
    projection = classified.projection
    bound = classified.high_water
    # Fix round K5 (825-832-CD5-D02): a projected reading captured in this
    # evaluation's own second is ordered against it by the journal point the
    # evaluation was enrolled at.
    orders = {
        axis: _same_second_order(
            account_key, axis, evaluation.at, evaluation.journal_at)
        for axis in _candidates.AXES
        if (db_axis := _candidates._projection_axis(projection, axis)) is not None
        and db_axis.captured_at == evaluation.at}
    same_second_later = frozenset(
        axis for axis, order in orders.items() if order == _ORDER_LATER)
    unknown = frozenset(
        axis for axis, order in orders.items() if order == _ORDER_UNKNOWN)
    # #797 spec 4.4: the refusal facts, resolved before the pure kernel from
    # this classification's snapshot and the transition's receipts.
    facts = _stale_member_facts(
        classified.stale, () if transition is None else transition.receipts, members)
    (decision, anchor, active, tombstones), withheld, unestablished = (
        _gated_reduction(
            account_key, members, transition, eval_epoch=evaluation.at,
            fence_epoch=fence_epoch, classified=classified, frontier=frontier,
            same_second_later=same_second_later | unknown, member_facts=facts))
    if unknown:
        # Fix round K6 (825-832-CD6-V01): an order that cannot be established
        # is neither later nor earlier. When the reduction is the same either
        # way, the order is irrelevant to the outcome and the evaluation is
        # decided. Otherwise it keeps its identity and its acknowledgement
        # position — no fence, no acknowledgement, no compaction past it —
        # and a later pass decides it once the order is readable, or a
        # later-second reading supersedes it whatever the order.
        unfenced, unfenced_withheld, unfenced_unestablished = _gated_reduction(
            account_key, members, transition, eval_epoch=evaluation.at,
            fence_epoch=fence_epoch, classified=classified, frontier=frontier,
            same_second_later=same_second_later, member_facts=facts)
        if (unfenced[0] != decision or unfenced_withheld != withheld
                or unfenced_unestablished != unestablished):
            return _Resolution(transition, classified, "deferred", AccountResult(
                account_key, "deferred", None, _ORDER_UNKNOWN_REASON))
    if unestablished:
        # Fix round K10 (review finding P1-2): a reading the closed gate
        # refuses on an axis with no baseline is unknown classification, not
        # a no-change reading. It is deferred exactly like an unknown
        # same-second order: no publication, no fence, no acknowledgement and
        # no compaction past it, so slot replacement or expiry never leaves
        # the spool as its only copy. A later pass decides it once a baseline
        # is established (an authoritative reading, a later accepted reading
        # that supersedes it) or the gate is open.
        return _Resolution(transition, classified, "deferred", AccountResult(
            account_key, "deferred", None, _BASELINE_UNKNOWN_REASON,
            retry_at=_evidence_retry_at(classified)))
    evidence = _membership_evidence(members)
    if _repeated_attempt(transition, decision, evidence):
        # CD3-V01: the evaluation is retained and reduced at its own instant,
        # but its bounded repeat attempt is not spent on the very evidence the
        # previous attempt was spent on (#832): the pending drops stay as that
        # attempt left them, for the next distinct reading.
        decision = dataclasses.replace(
            decision, action="WRITE_CONTROL", plan=None,
            control=dataclasses.replace(decision.control, pending_drops={
                axis: (transition.control.pending_drops.get(axis)
                       if _plan_axis(decision.plan, axis) is not None
                       else decision.control.pending_drops.get(axis))
                for axis in _candidates.AXES}))
    prior_withheld = bool(transition is not None and transition.quarantine
                          and transition.quarantine.get("dropWithheld"))
    quarantine = ...
    if withheld != prior_withheld:
        quarantine = {
            "authorityCommit": (None if transition is None
                                or transition.quarantine is None
                                else transition.quarantine.get("authorityCommit")),
            "dropWithheld": withheld,
        }
    control_repair_required = (
        transition is None
        or transition.control.db_projection.db_files != projection.db_files
        or transition.five_hour_anchor != anchor
        or transition.binding != binding
        or quarantine is not ...
    )
    generation = 0 if transition is None else transition.generation
    acknowledged = max(_acknowledged_through(transition), evaluation.sequence)
    plan = None
    if decision.action == "PUBLISH_DB":
        plan = _bound_publication_plan(
            decision, projection, active, binding, generation=generation + 1,
            now_epoch=evaluation.at, intake_sequence=evaluation.sequence,
            refused=_refused_tokens(facts))
    if plan is None:
        if decision.action == "NOOP" and control_repair_required:
            decision = dataclasses.replace(decision, action="WRITE_CONTROL")
        # #797 spec 4.4 (D7a-D7c): a covered detached evaluation that
        # publishes nothing is a completion — its receipts are earned and
        # installed in the same atomic write as its control and checkpoint.
        receipts, stale_cache = _completion_receipts(transition, classified, members)
        updated = _fit_transition(_replace_transition(
            transition, account_key=account_key, binding=binding,
            projection=projection, control=decision.control, anchor=anchor,
            quarantine=quarantine, acknowledged=acknowledged,
            coverage=_coverage_document(classified), receipts=receipts,
            stale_cache=stale_cache),
            prior=() if transition is None else transition.receipts,
            referenced=_referenced_tokens(transition, members))
        if (transition is not None
                and _refusal_authority_lost(classified, facts, updated.receipts,
                                            members)):
            # Review RV-4 (spec 4.4: dependencies of outstanding work are
            # retained or their refusal authority is invalidated and the
            # work re-reduced). The document limit's last resort evicted a
            # receipt this very reduction refused a member on. That refusal
            # has nothing left to rest on, so the evaluation is not
            # acknowledged with it: the fitted receipts are installed with
            # the checkpoint left below this evaluation (a crash leaves it
            # unacknowledged, for the next pass) and it is reduced again
            # now, the evicted member eligible. Each round evicts at least
            # one of the receipts it started from, so this terminates.
            invalidated = _replace_transition(
                transition, account_key=account_key, binding=transition.binding,
                projection=transition.control.db_projection,
                receipts=updated.receipts)
            _write_transition(invalidated)
            return _reduce_evaluation(
                account_key, evaluation, invalidated, classified=classified,
                fence_epoch=fence_epoch, nudge_sink=nudge_sink, frontier=frontier)
        _write_transition(updated)
        _crash_point("after-checkpoint")
        return _Resolution(updated, classified, "noop", decision=decision)
    # The frozen plan is durable BEFORE the record kernel runs, next to the
    # anchor that keys it and with its intake reference. The pre-attempt
    # control stays current until commit, so a rejected or failed attempt
    # never consumes a retry, and the checkpoint stays below the plan's
    # evaluation until the publication is acknowledged.
    plan["journalHighWater"] = {"segment": bound[0], "offset": bound[1]}
    planned = _replace_transition(
        transition, account_key=account_key, binding=binding,
        projection=projection,
        control=(transition.control if transition is not None
                 else _empty_control(projection)),
        anchor=anchor, publication=plan, quarantine=quarantine,
        attempt_evidence=evidence)
    _write_transition(planned)
    _crash_point("after-freeze")

    def refreshed(after):
        if _projection_changed(projection, after):
            # A real DB change is authoritative selected truth, so re-reduce
            # to clear any satisfied pending axis whose projection now
            # matches — at the evaluation's own instant.
            return _candidates.reduce_candidates(
                active, db=after, control=decision.control,
                tombstones=tombstones, now_epoch=evaluation.at,
                account_key=account_key,
                drop_consensus_allowed=not withheld, member_facts=facts).control
        # The record kernel can deliberately leave the DB unchanged: the
        # first zero arms its own debounce and unsupported small drops are HWM
        # rejected. Preserve the precomputed bounded attempt state so the next
        # pass performs the required fresh contributor-consensus pass before
        # spending another kernel call.
        return dataclasses.replace(decision.control, db_projection=after)

    return _publish_plan(planned, plan, binding, classified, frontier=frontier,
                         nudge_sink=nudge_sink, before=projection, status="ok",
                         refreshed_control=refreshed, decision=decision)


def _oldest_retained_instant(account_key, transition, acknowledged, *,
                             now_epoch: int) -> int:
    """The earliest instant any of this partition's retained work — a frozen
    plan, every unacknowledged evaluation — is still to be reduced at, or
    ``now_epoch`` when there is none. Streamed, one record at a time. An
    intake that cannot be read proves no instant safe, so nothing is pruned
    (0) and the drain below reports the failure."""
    oldest = now_epoch
    if transition is not None and transition.publication is not None:
        oldest = min(oldest, int(transition.publication["capturedAt"]))
    try:
        for evaluation in _intake_evaluations(account_key, after=acknowledged):
            oldest = min(oldest, evaluation.at)
    except (OSError, ValueError, TypeError, KeyError):
        return 0
    return oldest


def _reduce_and_publish_partition(account_key, candidates, transition, *,
                                  now_epoch: int, nudge_sink,
                                  frontier: "_PublicationFrontier | None" = None,
                                  drained: "dict | None" = None,
                                  ) -> AccountResult:
    """`_reduce_and_publish_partition_work` under this partition's #797
    stale scope (`_STALE_SCOPE`): which provider samples its
    classifications read for the refusal facts and receipts."""
    try:
        scope = _stale_scope(account_key, candidates, transition)
    except (OSError, ValueError, TypeError, KeyError):
        scope = (None, bool(transition is not None and transition.receipts))
    token = _STALE_SCOPE.set(scope)
    try:
        return _reduce_and_publish_partition_work(
            account_key, candidates, transition, now_epoch=now_epoch,
            nudge_sink=nudge_sink, frontier=frontier, drained=drained)
    finally:
        _STALE_SCOPE.reset(token)


def _reduce_and_publish_partition_work(account_key, candidates, transition, *,
                                       now_epoch: int, nudge_sink,
                                       frontier: "_PublicationFrontier | None" = None,
                                       drained: "dict | None" = None,
                                       ) -> AccountResult:
    """One partition's ordered work (#825/#832 spec 4.3 revision 2, Codex
    design-fix-round-3 Q3):

    1. enroll this pass's evaluation — its instant and its exact membership,
       taken from the committed intake when it is appended (revision 3) — in
       the durable intake, before anything can defer; the render-captured
       evaluations before it are loaded in their recorded order;
    2. resolve the existing frozen publication under its original binding,
       capture time and publication identity;
    3. commit that publication's control transition, or its explicit
       disposition;
    4. reduce the logged evaluations in recorded order, each at its original
       instant, through the prefix captured when this drain started;
    5. reclassify against a covered projection before every later
       publication.

    Recovery never refreshes a receipt time, extends a pending deadline or
    expires logged intake. The first item that cannot be classified ends the
    partition's pass (one attempt per unchanged item per pass); its blockers
    tell the continuation what to wait for.

    ``drained`` receives the last sequence number this drain covers, the
    prefix captured when it started: an arrival logged after it is new work
    the holder's final release check must answer (revision 3, Q4)."""
    acknowledged = _acknowledged_through(transition)
    backlog = _intake_backlog(account_key, acknowledged)
    frozen = transition is not None and transition.publication is not None
    pending = transition is not None and any(transition.control.pending_drops.values())
    # An empty spool is NOT a retraction (#755): every contributor supporting
    # a pending low can age out of its 90-second active window well before
    # that drop's own 180-second deadline, so an empty evaluation is still
    # logged and reduced, and the pending record decides from its own
    # retained evidence.
    if not candidates and not backlog and not frozen and not pending:
        return AccountResult(account_key, "noop", None)
    # Fences are pruned on the wall clock, but never beneath retained work:
    # an evaluation reduced at its original instant still needs every fence
    # live at that instant (Codex fix-round-4 Q2).
    _expire_account_tombstones(account_key, now_epoch=_oldest_retained_instant(
        account_key, transition, acknowledged, now_epoch=now_epoch))
    enrolled = _intake_enroll(account_key, acknowledged=acknowledged,
                              require_members=transition is None)
    through = _intake_tail_sequence(account_key)
    if enrolled is not None or frozen or (
            through is not None and through > acknowledged):
        # Pending before anything can defer it, so a crash after enrollment
        # can never leave unacknowledged work marked idle — and so is the
        # work this pass found already captured, whoever logged it (review
        # finding CD2-007, cycle 3): a render that won the selected state
        # registered nothing, and a completion wake posted while ``pending``
        # was false is discarded, so a deferral below could otherwise outlive
        # the only event that would have resumed it.
        _mark_continuation_pending()
        if enrolled is not None:
            _crash_point("after-intake")
    if drained is not None and through is not None:
        drained[account_key] = through
    status = None
    classified = None
    decision = None
    if frozen:
        resolution = _resolve_frozen_publication(
            transition, now_epoch=now_epoch, nudge_sink=nudge_sink, frontier=frontier)
        if resolution.stop is not None:
            return resolution.stop
        transition, classified, status = (
            resolution.transition, resolution.classified, resolution.status)
        decision = resolution.decision
    # #825/#832 spec 4.3 revision 1 (Codex design-fix-round-2 Q2): every
    # reduction is classified against an exact-account, current, covered
    # projection, or not at all. Neither the projection a control transition
    # kept nor an axis-less one stands in for an index that is rebuilding or
    # has not folded the journal prefix: either could turn a drop into an
    # apparent rise or a first reading.
    for evaluation in _intake_evaluations(
            account_key, after=_acknowledged_through(transition), through=through):
        if classified is None or not classified.serves(evaluation.at):
            try:
                classified = _classify(account_key, frontier, at=evaluation.at)
            except _Unclassifiable as exc:
                return _blocked(account_key, "deferred" if status == "deferred"
                                else "retained", exc)
        resolution = _reduce_evaluation(
            account_key, evaluation, transition, classified=classified,
            fence_epoch=now_epoch, nudge_sink=nudge_sink, frontier=frontier)
        if resolution.stop is not None:
            return resolution.stop
        transition, classified = resolution.transition, resolution.classified
        decision = resolution.decision
        if resolution.status != "noop" or status is None:
            status = resolution.status
    if transition is not None:
        _intake_compact(account_key, _acknowledged_through(transition))
    return AccountResult(account_key, status or "noop", decision)


def _statusline_reduce_and_publish(*, nudge_sink=None,
                                   drained: "dict | None" = None) -> tuple:
    """Reduce every account partition and publish each one's plans.

    The detached reducer (spec 4.3) establishes the account-bound cutover
    before any authoritative projection or derivation decision (spec 5.5),
    then snapshots every partition under the selected-state lock its caller
    holds, rescans the legacy spool against its journal-anchored baseline
    BEFORE any publication planned from that snapshot (spec 5.5 step 6,
    section 7), and processes every partition in bytewise account-key order.
    Each evaluation yields at most one combined two-axis plan and one control
    replacement; a failure in one partition leaves it retryable and never
    prevents, reads or clears another partition's transition. The pass ends
    by recording, under the same lock, whether work is still pending and what
    blocked it (spec 4.3 revision 2).

    ``nudge_sink`` (#583 S2) is threaded to the record kernel. EVERY caller
    runs this inside the selected-state flock, and the dashboard nudge is a
    loopback POST with a multi-second timeout; the caller that acquired the
    lock fires the real nudge once it has released it.

    Returns one `AccountResult` per partition considered, in bytewise
    account-key order. The pass's own ingest commits, appends and lock
    releases are its own work, never continuation wakes.

    ``drained`` (spec 4.3 revision 3) receives each partition's drained
    intake prefix, for the holder's final release check."""
    token = _WAKES_SUPPRESSED.set(True)
    try:
        return _statusline_reduce_and_publish_pass(nudge_sink, drained)
    finally:
        _WAKES_SUPPRESSED.reset(token)


def _account_has_work(account_key: str, candidates, transition) -> bool:
    """What `_reduce_and_publish_partition` would not answer with an
    immediate no-op: active candidates, a frozen plan, pending drops or
    unacknowledged intake."""
    if candidates or (transition is not None and (
            transition.publication is not None
            or any(transition.control.pending_drops.values()))):
        return True
    try:
        return _intake_backlog(account_key, _acknowledged_through(transition))
    except (OSError, ValueError, TypeError, KeyError):
        return True


def _settle_wait(account_key: str, result: "AccountResult", *, observed: dict,
                 now_epoch: int) -> "AccountResult":
    """After a partition's work: install the wait descriptor of a head that
    stays blocked, or remove one that no longer describes a waiting head.

    The descriptor is installed only when the dependencies the partition's
    classification observed (or, without one, the pass observed before it
    ran) are unchanged now (spec 4.3 *Waiting
    heads*, finding SR5-004): a relevant change that landed meanwhile —
    whoever made it, wake or no wake — leaves the head runnable and the
    result marked for another pass.

    Fix round K12 (K11R-8): ``observed`` is the index file fingerprint at
    that observation. Every dependency token is read from the index, so an
    unchanged fingerprint proves unchanged tokens; the tokens themselves
    are read only here, when a wait is actually installed."""
    wait = _wait_class(result)
    try:
        transition = _read_transition(account_key, now_epoch=now_epoch)
    except (OSError, ValueError, TypeError, KeyError):
        return result
    if wait is None:
        if result.status not in ("deferred", "retained"):
            _clear_wait(account_key)
        return result
    _crash_point("before-wait-install")
    if _db_file_fingerprint() != observed:
        return dataclasses.replace(result, rerun=True)
    current = _wait_tokens(account_key, reason=wait["reason"])
    if _db_file_fingerprint() != observed:
        return dataclasses.replace(result, rerun=True)
    try:
        _install_wait(account_key, transition, wait, tokens=current,
                      now_epoch=now_epoch)
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return result


def _statusline_reduce_and_publish_pass(nudge_sink, drained=None) -> tuple:
    now_epoch = int(time.time())
    candidates = _load_candidate_spool(now_epoch=now_epoch)
    failures: dict = {}
    transitions = _read_transitions(now_epoch=now_epoch, errors=failures)
    partitions: dict = {}
    for candidate in candidates:
        partitions.setdefault(candidate.binding.account_key, []).append(candidate)
    keys = sorted(set(partitions) | set(transitions) | set(failures)
                  | set(_account_keys_on_disk()),
                  key=lambda key: key.encode("utf-8"))
    # Spec 4.3 revision 5 (*Waiting heads*, design-fix-round-5 Q3):
    # eligibility is decided BEFORE the cutover, the legacy spool digest and
    # any ingest. A waiting head whose descriptor is unchanged, whose
    # dependencies read the same, whose retry is not due and whose lock
    # obligations are still held is retained but not run.
    state = _read_continuation_state()
    observed: dict = {}
    waiting: set = set()
    _CLASSIFIED_FINGERPRINTS.clear()
    pass_fingerprint = _db_file_fingerprint()
    for account_key in keys:
        if account_key in failures:
            continue
        transition = transitions.get(account_key)
        if not _account_has_work(account_key, partitions.get(account_key, ()),
                                 transition):
            continue
        try:
            head = _wait_head(account_key, transition)
        except (OSError, ValueError, TypeError, KeyError):
            continue
        if head is None:
            continue
        observed[account_key] = pass_fingerprint
        # Fix round K12 (K11R-8): the tokens are read only when a valid
        # descriptor still describes the head and every cheaper check
        # leaves it waiting.
        if _head_waiting(account_key, transition, now_epoch=now_epoch,
                         state=state,
                         tokens=functools.partial(_wait_tokens, account_key),
                         probe=True, head=head):
            waiting.add(account_key)
    runnable = [key for key in keys
                if key not in waiting and key not in failures
                and _account_has_work(key, partitions.get(key, ()),
                                      transitions.get(key))]
    if waiting and not runnable and not failures:
        # Every head with work waits: no cutover, no spool digest, no ingest
        # and no continuation write (the pass stays blocked). A pass with no
        # head at all still runs, so it inventories the legacy spool.
        return tuple(AccountResult(key, "waiting", None, "waiting head")
                     for key in sorted(waiting, key=lambda k: k.encode("utf-8")))
    import _cctally_journal

    def every_head_waits(reason: str) -> tuple:
        # Nothing may publish this pass: every head waits for a due retry of
        # the failed I/O (spec 4.3 *Waiting heads*); spool-only work holds
        # no head and simply stays in the spool.
        results = []
        for account_key in keys:
            if account_key in failures:
                results.append(AccountResult(
                    account_key, "record_failed", None, str(failures[account_key])))
                continue
            if account_key in waiting:
                results.append(AccountResult(account_key, "waiting", None,
                                             "waiting head"))
                continue
            if account_key not in runnable:
                continue
            result = AccountResult(account_key, "deferred", None, reason)
            if account_key in observed:
                result = _settle_wait(
                    account_key, result, observed=observed[account_key],
                    now_epoch=now_epoch)
            results.append(result)
        _record_pass_outcome(results)
        return tuple(results)

    try:
        _ensure_cutover_for_publication()
    except _cctally_journal.AccountBoundCutoverUnavailable:
        # Fix round K12 (K11R-6, spec 4.3 *Waiting heads*): an unavailable
        # cutover lookup is a wait reason, not an aborted pass. The journal
        # it needs is unreadable, which only a retry can observe.
        return every_head_waits("unreadable")
    # Before the frontier is captured: a dead torn tail healed later would
    # shrink the journal beneath that frontier.
    _heal_torn_journal_tail()
    # #825/#832 fix-round-1 Q3: the frontier every drop planned from this
    # snapshot must see folded is captured right after the rescan, so it
    # includes the activity op the rescan may have just appended.
    try:
        local_activity = _rescan_legacy_spool()
    except OSError:
        # The inventory could not be completed and its trigger could not be
        # made durable (spec 5.5 step 6, section 7): nothing may publish
        # without it. Every head waits for a due retry of the inventory.
        return every_head_waits(_LEGACY_INVENTORY_REASON)
    frontier = _capture_publication_frontier(local_activity=local_activity)
    results = []
    for account_key in keys:
        if account_key in failures:
            # Fail-closed state is left untouched: it cannot regain a retry.
            results.append(AccountResult(
                account_key, "record_failed", None, str(failures[account_key])))
            continue
        if account_key in waiting:
            results.append(AccountResult(account_key, "waiting", None,
                                         "waiting head"))
            continue
        before_partition = _db_file_fingerprint()
        try:
            result = _reduce_and_publish_partition(
                account_key, tuple(partitions.get(account_key, ())),
                transitions.get(account_key), now_epoch=now_epoch,
                nudge_sink=nudge_sink, frontier=frontier, drained=drained)
        except Exception as exc:
            result = AccountResult(account_key, "record_failed", None, str(exc))
        if account_key in observed or _wait_path(account_key).exists():
            seen = _CLASSIFIED_FINGERPRINTS.pop(account_key, None)
            if seen is None:
                seen = before_partition
            result = _settle_wait(account_key, result, observed=seen,
                                  now_epoch=now_epoch)
        if (result.status != "noop" or result.decision is not None
                or account_key in partitions or account_key in transitions):
            results.append(result)
    _record_pass_outcome(results)
    return tuple(results)


def _ensure_cutover_for_publication() -> None:
    """Establish the account-bound cutover before any authoritative projection
    or derivation decision (#825/#832 spec 5.5, C3).

    Called by the detached reducer and the authoritative OAuth writer — never
    by the render path (C4). While the store still awaits its one-time
    journal cutover, opening the index performs it, and the establishment is
    retried once; a store that still defers simply records inside what will
    become the historical prefix."""
    import _cctally_journal

    try:
        _cctally_journal.ensure_account_bound_cutover()
        return
    except _cctally_journal.AccountBoundCutoverDeferred:
        pass
    try:
        open_db().close()
    except (Exception, _cctally().StatsRebuildDeferred):
        return
    try:
        _cctally_journal.ensure_account_bound_cutover()
    except _cctally_journal.AccountBoundCutoverDeferred:
        return


def _rescan_legacy_spool() -> bool:
    """Compare the legacy spool with its journal-anchored baseline inside the
    critical section that snapshots current candidates, BEFORE any
    publication planned from that snapshot (#825/#832 spec 5.5 step 6,
    section 7). Any difference — or a missing, unreadable or unanchored
    baseline — is journaled as one current-stamped `legacy_spool_activity`
    op, which advances the anchored baseline to the observed state. The
    comparison never reads a legacy value as authority; the op is the
    trigger the outcome quarantine consumes. Returns True when it appended
    one.

    Spec revision 5 (section 7, operator decision on Codex
    design-fix-round-5 Q4): an inventory that cannot complete is an
    uncertainty trigger. The first failure of an episode appends one
    `inventory-incomplete` op (no digest, no baseline advance) and returns
    True; a later failure of the same episode appends nothing and returns
    False. Either way the caller proceeds: classifiable ordinary recording
    continues, while the unresolved marker withholds drop consensus and
    automatic outcomes. A complete inventory ends the episode and may anchor
    a complete baseline, but never clears the marker. After a cutover that
    recorded inventory uncertainty (it anchored no baseline), the first
    complete inventory anchors one under an `inventory-complete` op (fix
    round K12, K11R-4), never a `baseline-unavailable` trigger.

    Raises an `OSError` only when the pass must defer (a waiting head with
    I/O-retry semantics, reason ``legacy-inventory``): the uncertainty op
    could not be made durable (`LegacySpoolUncertaintyNotDurable`), or the
    journal the anchor lookup needs is unreadable
    (`AccountBoundCutoverUnavailable`)."""
    import _cctally_journal
    import _lib_account_protocol
    import _lib_legacy_spool

    cutover = _cctally_journal.find_account_bound_cutover()
    if cutover is None:
        return False
    try:
        # #797/#823/#840 spec 4.6 B4: the v1 coordination state joins the
        # inventory at the revision-2 boundary, never before it, so a store
        # whose revision is not yet established compares like for like.
        state = _cctally_journal.capture_legacy_spool_state(
            include_v1=cutover.revision is not None)
    except _cctally_journal.LegacySpoolInventoryIncomplete as failure:
        return _cctally_journal.record_legacy_spool_inventory_incomplete(
            failure) is not None
    anchor = _cctally_journal.legacy_spool_anchor_state()
    if anchor is not None and anchor.digest is None:
        # Fix round K12 (K11R-4; spec section 7): the latest anchor is a
        # cutover that recorded inventory uncertainty, so no baseline was
        # ever anchored and none is "unavailable". This first complete
        # inventory ends that episode by anchoring one under its own op,
        # which manufactures no legacy-activity cause and clears nothing.
        _cctally_journal.append_legacy_spool_activity(
            changed=tuple(sorted(entry[0] for entry in state.entries)),
            new_state=state,
            reason=_lib_account_protocol.INVENTORY_COMPLETE_REASON)
        return True
    baseline = (None if anchor is None else
                _cctally_journal.read_anchored_legacy_spool_baseline(
                    anchor.digest))
    if baseline is None:
        changed = tuple(sorted(entry[0] for entry in state.entries))
        _cctally_journal.append_legacy_spool_activity(
            changed=changed, new_state=state, reason="baseline-unavailable")
        return True
    if baseline.digest() == state.digest():
        if anchor.episode_open:
            # A complete inventory ends the failure episode; the spool is
            # unchanged, so it anchors nothing new and clears nothing.
            _cctally_journal.close_legacy_spool_inventory_episode(
                anchor.uncertain)
        return False
    _cctally_journal.append_legacy_spool_activity(
        changed=_lib_legacy_spool.diff(baseline, state), new_state=state,
        reason="changed")
    return True


# ── event-driven continuation (#825/#832 spec 4.3 revision 2, Codex
# design-fix-round-3 Q2) ─────────────────────────────────────────────────────
#
# Continuation never polls and never trusts a lock holder to notify. Its state
# is `continuation.json` under the account-bound root, read-modify-written
# under `continuation.lock` — a LEAF lock: nothing else is ever acquired while
# it is held, and it is held for one small file write:
#
#   pending            some partition holds unacknowledged intake or a frozen
#                      plan (set at enrollment, before anything can defer;
#                      recomputed at the end of every pass, under the
#                      selected-state lock)
#   pendingGeneration  bumped by every pass that leaves work pending
#   wakeGeneration     bumped by every activating event while work is pending
#   blockers           what the last pass was blocked by
#   obligationGeneration
#                      a store-wide counter, never reset, that numbers every
#                      failed acquisition registered below
#   obligations        per dependency lock: the latest failure generation, the
#                      latest one that requested the lock EXCLUSIVELY, and the
#                      generation its waiter has observed released through
#
# The ONE reducer reservation is an exclusive flock on
# `continuation.reducer.lock`, taken non-blocking under the state lock BEFORE a
# worker is spawned and inherited by it. A resumer records a wake and spawns
# only when it gets the reservation; otherwise the reserved or running worker
# sees the wake at its final release-or-hand-off decision, which is made under
# the state lock, so no wake falls between them. A worker's own commits and
# appends never count as wakes. A dead worker's reservation is released by the
# kernel and reclaimed at the next activating event (spec residual 8).
#
# A failed acquisition reports the lock it lost to itself (Codex fix-round-4
# Q3: `IngestResult.blocked_by` — including a cache-leg prefix stop's cache
# writer flock on a cycle that ran, cycle-4 CD2-007 — the store guard's
# `blocked_by`, the rebuild worker probes), never a later probe, and
# contention is told apart from an
# I/O error. Once the failing pass has released every accounting lock, the
# contended failure is registered as an OBLIGATION (a new failure generation)
# and the ONE waiter of that lock is reserved (`continuation.waiter-<lock>.lock`)
# under the same state-lock critical section — even if the holder has already
# let go. The waiter makes one blocking acquisition in a mode that conflicts
# with the holder that refused the request, reads the failure generations it
# thereby satisfied while it still holds the lock, releases and closes it,
# records them released and posts ONE release wake. An immediate acquisition
# satisfies an obligation registered before it; an unlocked probe without an
# obligation proves nothing. A failure registered after that read stays
# outstanding (the release watermark only rises to what was read), and the
# waiter's final release-or-continue decision is made under the state lock,
# so a newer failure is either served by it or finds its reservation free.
# It does no accounting work and takes no other lock while it holds one;
# because the kernel releases a lock when its holder exits or dies, that
# acquisition observes every exit path of every holder, current binary or
# previous release.
#
# Bounds: one bounded pass per coalesced activating event; one attempt per
# unchanged item per pass; a drain processes the intake prefix captured when
# it starts; a failed attempt never manufactures a wake (only its waiter's
# observed release does); a failure no lock caused registers nothing; at most
# one reducer reservation and one waiter per dependency lock, each waiter
# acquisition answering a failure registered before it. Nothing here holds the
# maintenance or ingest lock in a retry loop or waits under the selected-state
# lock, so it cannot recreate the September 2026 incident (a retry loop that
# held the maintenance lock about 80% of the time for seven hours).
#
# Everything here runs in a detached process (the reducer child, a spawned
# worker or waiter, a rebuild worker, a CLI command after its own locks are
# released), never in the render path (C4).

_CONTINUATION_SCHEMA_VERSION = 2
#: Documents written before obligations existed read as having none.
_CONTINUATION_SCHEMA_VERSIONS_READ = (1, 2)
_CONTINUE_COMMAND = "_statusline-continue"
#: The locks a deferral can depend on, by the name its waiter is reserved
#: under. ``cache`` and ``cache-codex`` are the global and Codex provider
#: cache writer flocks a cache-leg prefix stop can lose to (cycle-4 finding
#: CD2-007): the stop leaves its records unconsumed until that lock's release,
#: which is no stats event, so only its waiter can observe it.
_DEPENDENCY_LOCKS = ("maintenance", "ingest", "epoch-worker", "heal-worker",
                     "cache", "cache-codex")
_OBLIGATION_KEYS = frozenset({"failure", "exclusiveFailure", "released"})

#: True while this context runs a reducer pass: its own ingest commits,
#: appends and lock releases are the pass's own work, never wakes.
_WAKES_SUPPRESSED = contextvars.ContextVar("statusline_wakes_suppressed", default=False)
#: Nesting depth of this context's selected-state lock.
_SELECTED_DEPTH = contextvars.ContextVar("statusline_selected_depth", default=0)
#: A wake recorded while this context held an accounting lock: its dispatch
#: is owed until the lock is released.
_DISPATCH_OWED = contextvars.ContextVar("statusline_dispatch_owed", default=False)
#: A bound OAuth publication reconciled under the selected-state lock: its
#: wake is posted once that lock is released.
_OAUTH_RECONCILED = contextvars.ContextVar("statusline_oauth_reconciled", default=False)


# ---------------------------------------------------------------------------
# Waiting heads (#825/#832 spec 4.3 revision 5, Codex design-fix-round-5 Q3,
# review finding K9K10-R2). Retained work is not the same as runnable work:
# an account whose oldest unresolved evaluation or frozen publication waits
# on something outside the reducer keeps a durable wait descriptor, and an
# unchanged waiting head is not runnable. Neither the render nor the reducer
# forks, digests the legacy spool, ingests or rewrites continuation state for
# it until a relevant change or a due retry makes it runnable.
# ---------------------------------------------------------------------------

_WAIT_SCHEMA_VERSION = 1
#: The classification semantics a wait descriptor was decided under (fix
#: round K14c, spec 4.3 revision 6 *Waiting heads*). A descriptor of any other
#: generation — every one written before revision 6, which carries none — is
#: invalid, so its head is runnable again and the current rules reclassify
#: it; the head's intake, original instant and frozen identity are untouched.
_WAIT_SEMANTIC_GENERATION = 1
_WAIT_DOCUMENT_MAX_BYTES = 16 * 1024
#: What a wait is waiting for. ``evidence``: only a relevant change of the
#: account's classification evidence (its admitted rows, its reset state,
#: its quarantine, the index identity, its control) makes it runnable.
#: ``lock``: additionally the observed release of an actual blocking-lock
#: obligation. ``io``: additionally a due retry of the failed I/O evidence.
_WAIT_CLASSES = ("evidence", "lock", "io")
#: Why a pass stopped at the head: the inventory of the legacy spool could
#: not be completed (spec 5.5 step 6, section 7; the trigger op could not be
#: made durable), or the journal could not be read to find a plan's appended
#: observation (review finding K9K10-R12).
_LEGACY_INVENTORY_REASON = "legacy-inventory"
_PUBLICATION_SCAN_REASON = "publication scan unavailable"
#: The token value of a dependency read that failed: unchanged evidence of
#: nothing, so a head waiting on it stays waiting until the read succeeds.
_TOKENS_UNAVAILABLE = "unavailable"
#: The index file fingerprint each account's latest classification in this
#: pass observed (read right after its opportunistic ingest). A wait is
#: installed only when the index still reads the same (fix round K12,
#: K11R-8: the tokens themselves are read only then).
_CLASSIFIED_FINGERPRINTS: dict = {}


def _wait_path(account_key: str) -> pathlib.Path:
    return _account_dir(account_key) / "wait.json"


def _wait_head(account_key: str, transition) -> "tuple | None":
    """The account's head: its frozen publication's identity and its oldest
    unacknowledged evaluation's sequence, or None when it holds neither.
    File-only: the checkpoint and one intake line."""
    publication = (None if transition is None or transition.publication is None
                   else transition.publication["publicationId"])
    sequence = None
    for evaluation in _intake_evaluations(
            account_key, after=_acknowledged_through(transition)):
        sequence = evaluation.sequence
        break
    if publication is None and sequence is None:
        return None
    return (publication, sequence)


def _wait_class(result: "AccountResult") -> "dict | None":
    """The wait a deferred partition result describes, or None when the
    result is no wait (progress, a frontier conflict whose journal growth is
    itself the event, or a publication the next pass simply re-publishes)."""
    if result.status not in ("deferred", "retained"):
        return None
    if "frontier" in (result.blockers or ()):
        return None
    reason = result.reason or ""
    locks = {block.lock: block.mode for block in (result.obligations or ())
             if block.lock in _DEPENDENCY_LOCKS}
    if locks:
        kind = "lock"
    elif reason == _BASELINE_UNKNOWN_REASON:
        kind = "evidence"
    elif reason in ("unreadable", "coverage", "rebuild", "busy",
                    _ORDER_UNKNOWN_REASON, _LEGACY_INVENTORY_REASON,
                    _PUBLICATION_SCAN_REASON):
        # Unknown same-second order resolves when the journal becomes
        # readable, which only a retry can observe, as well as on a later
        # accepted reading (a relevant evidence change).
        kind = "io"
    else:
        return None
    return {"reason": reason, "class": kind, "locks": locks,
            "retryAt": result.retry_at if kind == "evidence" else None}


def _wait_tokens(account_key: str, reason: "str | None" = None) -> str:
    """The dependency tokens of ``account_key``'s classification, from ONE
    read snapshot of the index and no ingest: the index identity (epoch and
    publication stamp, so an index replacement changes it), the account's
    admitted rows and reset state on both axes, and its quarantine status.
    Unrelated journal growth, another account's publication and this
    reducer's own writes for other accounts leave it unchanged. A failed
    read is `_TOKENS_UNAVAILABLE`.

    Fix round K12 (K11R-9): a wait whose ``reason`` is coverage also
    depends on the index's coverage high-water (its journal cursor), so
    coverage advancement makes it runnable before its retry is due."""
    try:
        conn = open_db()
    except (Exception, _cctally().StatsRebuildDeferred):
        return _TOKENS_UNAVAILABLE
    return _tokens_from(conn, account_key, reason)


def _wait_tokens_without_waiting(account_key: str, reason: "str | None" = None) -> str:
    """`_wait_tokens` for an activating event's eligibility check (fix round
    K13, review cycle 7 825-832-CD7-V02): the same tokens from the same
    index, read without waiting on anything. The maintenance lock is taken
    SHARED and non-blocking — so no destructive maintenance can begin while
    the guarded opener runs, and its own shared request is granted at once
    — and the open recovers nothing (no interrupted-rebuild recovery, no
    pending quarantine resume, no epoch rebuild, no migration). Any doubt is
    `_TOKENS_UNAVAILABLE`, which never equals a recorded token, so the head
    is then treated as runnable exactly as before this check existed."""
    import _cctally_db
    import _cctally_store

    db_path = pathlib.Path(_cctally_core.DB_PATH)
    try:
        lock = open(_cctally_core.STATS_LOCK_MAINTENANCE_PATH, "a+")
    except OSError:
        return _TOKENS_UNAVAILABLE
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError:
            return _TOKENS_UNAVAILABLE
        try:
            if (not db_path.exists()
                    or _cctally_store._stats_repair_marker(db_path).exists()
                    or _cctally_db._quarantine_pending_path(db_path).exists()
                    or _cctally_store._stats_rebuild_artifact_bases(db_path)
                    or _cctally_store._stats_publication_marker(db_path).exists()
                    or _cctally_store.stats_epoch_rebuild_pending(db_path)):
                return _TOKENS_UNAVAILABLE
            conn = _cctally_store.stats_open_guarded(
                db_path, recover_interruptions=False,
                connect=lambda path, **kwargs: sqlite3.connect(
                    path, timeout=0, **kwargs))
        except (Exception, _cctally().StatsRebuildDeferred):
            return _TOKENS_UNAVAILABLE
        return _tokens_from(conn, account_key, reason)
    finally:
        try:
            fcntl.flock(lock, fcntl.LOCK_UN)
        except OSError:
            pass
        lock.close()


def _tokens_from(conn, account_key: str, reason: "str | None") -> str:
    """The dependency tokens `_wait_tokens` documents, read in one snapshot
    of the open index ``conn``, which this closes."""
    import _cctally_quarantine

    try:
        conn.execute("BEGIN")
        index = [int(conn.execute("PRAGMA user_version").fetchone()[0])]
        try:
            index.append([list(row) for row in conn.execute(
                "SELECT record_path, started_at_utc, stamped_at_utc "
                "FROM stats_publication_stamp ORDER BY rowid")])
        except sqlite3.Error:
            index.append(None)
        rows = {}
        for table in ("weekly_usage_snapshots", "five_hour_reset_events",
                      "week_reset_events", "weekly_credit_floors",
                      "five_hour_blocks"):
            count, top = conn.execute(
                f"SELECT COUNT(*), MAX(rowid) FROM {table} WHERE account_key = ?",
                (account_key,)).fetchone()
            rows[table] = [int(count), None if top is None else int(top)]
        status = _cctally_quarantine.account_status(conn, account_key)
        document = {"index": index, "rows": rows,
                    "quarantine": {k: status[k] for k in sorted(status)}}
        if reason == "coverage":
            try:
                cursor = conn.execute(
                    "SELECT segment, offset, applied_segment, applied_offset "
                    "FROM journal_cursor WHERE id = 1").fetchone()
            except sqlite3.Error:
                cursor = None
            document["coverage"] = None if cursor is None else list(cursor)
        return hashlib.sha256(json.dumps(
            document, sort_keys=True, separators=(",", ":"), default=str,
        ).encode("utf-8")).hexdigest()
    except (Exception, _cctally().StatsRebuildDeferred):
        return _TOKENS_UNAVAILABLE
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _read_wait(account_key: str) -> "dict | None":
    """The account's wait descriptor, or None when it is absent, unreadable
    or invalid (an invalid descriptor makes its head runnable)."""
    try:
        raw = _wait_path(account_key).read_bytes()
    except OSError:
        return None
    try:
        doc = _candidates._require_object(_candidates._load_json_document(
            raw, maximum_bytes=_WAIT_DOCUMENT_MAX_BYTES), "wait")
        _candidates._require_exact_keys(doc, {
            "schemaVersion", "semanticGeneration", "account", "head", "axes",
            "reason", "class", "tokens", "locks", "control", "retry"})
        head = doc["head"]
        retry = doc["retry"]
        if (doc["schemaVersion"] != _WAIT_SCHEMA_VERSION
                or doc["semanticGeneration"] != _WAIT_SEMANTIC_GENERATION
                or doc["account"] != account_key
                or not isinstance(head, list) or len(head) != 2
                or not (head[0] is None or isinstance(head[0], str))
                or not (head[1] is None or _candidates._is_nonnegative_int(head[1]))
                or doc["class"] not in _WAIT_CLASSES
                or not isinstance(doc["reason"], str)
                or not isinstance(doc["tokens"], str)
                or not isinstance(doc["axes"], list)
                or not isinstance(doc["locks"], dict)
                or not all(name in _DEPENDENCY_LOCKS and mode in ("shared", "exclusive")
                           for name, mode in doc["locks"].items())
                or not _candidates._is_int(doc["control"])
                or not (retry is None or (
                    isinstance(retry, dict) and set(retry) == {"attempts", "deadline"}
                    and _candidates._is_nonnegative_int(retry["attempts"])
                    and _candidates._is_nonnegative_int(retry["deadline"])))):
            return None
        return doc
    except (ValueError, TypeError, KeyError):
        return None


def _control_token(transition) -> int:
    return -1 if transition is None else int(transition.generation)


def _wait_retry_delay(attempts: int) -> int:
    """The non-sliding backoff after the ``attempts``-th failed I/O probe:
    60 s doubling to 3,600 s — the residual derivation's own bounds."""
    import _cctally_completion

    base = int(_cctally_completion._RESIDUAL_RETRY_BASE_S)
    ceiling = int(_cctally_completion._RESIDUAL_RETRY_MAX_S)
    return min(ceiling, base * 2 ** min(max(attempts, 1) - 1, 16))


def _lock_released(name: str, mode: str) -> bool:
    """One non-blocking acquisition of dependency lock ``name`` in the mode
    the failed request used (so it conflicts with every holder that could
    have refused it), released at once: an observation of the release an
    outstanding obligation waits for, the way its waiter's immediate
    acquisition is. Never blocks; any error observes nothing."""
    try:
        path = _dependency_lock_paths()[name]
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    except (OSError, KeyError, Exception):
        return False
    try:
        try:
            fcntl.flock(fd, (fcntl.LOCK_EX if mode == "exclusive" else fcntl.LOCK_SH)
                        | fcntl.LOCK_NB)
        except OSError:
            return False
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    finally:
        os.close(fd)


def _head_waiting(account_key: str, transition, *, now_epoch: int, state: dict,
                  tokens=None, probe: bool = False,
                  head=...) -> bool:
    """Whether ``account_key``'s head is a waiting head that is NOT runnable
    now (spec 4.3 *Waiting heads*): its descriptor is present and valid and
    still describes the head and its control generation, no I/O retry is
    due, every blocking-lock obligation it waits on is still outstanding
    (and, with ``probe``, still held), and its dependency tokens are
    unchanged (when the caller can read them: the reducer, never the
    render).

    ``tokens`` is None (not checked) or a callable taking the descriptor's
    reason; it is called last, only when every cheaper check leaves the head
    waiting (fix round K12, K11R-8)."""
    doc = _read_wait(account_key)
    if doc is None:
        return False
    if head is ...:
        head = _wait_head(account_key, transition)
    if head is None or list(head) != doc["head"]:
        return False
    if doc["control"] != _control_token(transition):
        return False
    retry = doc["retry"]
    if retry is not None and now_epoch >= retry["deadline"]:
        return False
    for name, mode in doc["locks"].items():
        ob = state["obligations"].get(name)
        if ob is None or ob["failure"] <= ob["released"]:
            return False
        if probe and _lock_released(name, mode):
            return False
    if tokens is not None and tokens(reason=doc["reason"]) != doc["tokens"]:
        return False
    return True


def _render_head_waiting(account_key: str, *, now_epoch: int,
                         state: "dict | None" = None) -> bool:
    """The render's file-only check (C4): is this account's head waiting and
    not runnable? Anything unreadable is not a wait, so the render behaves
    as before."""
    try:
        if not _wait_path(account_key).exists():
            return False
        transition = _read_transition(account_key, now_epoch=now_epoch)
        return _head_waiting(
            account_key, transition, now_epoch=now_epoch,
            state=_read_continuation_state() if state is None else state)
    except (OSError, ValueError, TypeError, KeyError):
        return False


def _install_wait(account_key: str, transition, wait: dict, *, tokens: str,
                  now_epoch: int, axes=()) -> bool:
    """Persist the head's wait descriptor; False when the account holds no
    head. A retry due on the same head and reason doubles the delay; the
    deadline never slides on its own. Written only when it changes."""
    head = _wait_head(account_key, transition)
    if head is None:
        _clear_wait(account_key)
        return False
    previous = _read_wait(account_key)
    retry = None
    if wait["class"] == "io":
        attempts = 1
        if (previous is not None and previous["head"] == list(head)
                and previous["reason"] == wait["reason"]
                and previous["retry"] is not None):
            attempts = previous["retry"]["attempts"] + 1
        retry = {"attempts": attempts,
                 "deadline": int(now_epoch) + _wait_retry_delay(attempts)}
    elif wait.get("retryAt") is not None:
        # Fix round K12 (K11R-10): evidence that wall time alone establishes
        # is retried at that instant; the deadline is the evidence's own, so
        # a re-install never slides it.
        retry = {"attempts": 0, "deadline": max(0, int(wait["retryAt"]))}
    doc = {"schemaVersion": _WAIT_SCHEMA_VERSION,
           "semanticGeneration": _WAIT_SEMANTIC_GENERATION, "account": account_key,
           "head": list(head), "axes": sorted(axes), "reason": wait["reason"],
           "class": wait["class"], "tokens": tokens,
           "locks": dict(sorted(wait["locks"].items())),
           "control": _control_token(transition), "retry": retry}
    if previous != doc:
        _atomic_write_json(_wait_path(account_key), doc, durable=True)
    return True


def _clear_wait(account_key: str) -> None:
    """Remove a descriptor that no longer describes a waiting head; the
    steady state, with none, writes nothing."""
    try:
        _wait_path(account_key).unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _continuation_state_path() -> pathlib.Path:
    return _bound_root() / "continuation.json"


def _continuation_lock_path() -> pathlib.Path:
    return _bound_root() / "continuation.lock"


def _reducer_reservation_path() -> pathlib.Path:
    return _bound_root() / "continuation.reducer.lock"


def _dirty_notification_path() -> pathlib.Path:
    """The ONE durable dirty notification captures coalesce into while a
    worker holds the reducer reservation (spec 4.3 revision 5, *Waiting
    heads*). Created and consumed under `continuation.lock` only."""
    return _bound_root() / "continuation.dirty"


def _consume_dirty_notification() -> None:
    """Under `continuation.lock`: the pass's disk scan that follows covers
    every capture coalesced so far (each committed its intake first)."""
    try:
        _dirty_notification_path().unlink()
    except FileNotFoundError:
        pass


def _waiter_reservation_path(name: str) -> pathlib.Path:
    if name not in _DEPENDENCY_LOCKS:
        raise ValueError("unknown dependency lock")
    return _bound_root() / f"continuation.waiter-{name}.lock"


def _dependency_lock_paths() -> dict:
    import _cctally_store

    return {
        "maintenance": _cctally_core.STATS_LOCK_MAINTENANCE_PATH,
        "ingest": _cctally_core.JOURNAL_INGEST_LOCK_PATH,
        "epoch-worker": _cctally_store._stats_epoch_rebuild_worker_path(),
        "heal-worker": _cctally_store._stats_heal_worker_path(),
        # Every cache writer takes these with `flock`, exclusively, so the
        # waiter's acquisition conflicts with whichever holder refused the
        # leg.
        "cache": _cctally_core.CACHE_LOCK_PATH,
        "cache-codex": _cctally_core.CACHE_LOCK_CODEX_PATH,
    }


def _default_continuation_state() -> dict:
    return {"schemaVersion": _CONTINUATION_SCHEMA_VERSION, "pending": False,
            "pendingGeneration": 0, "wakeGeneration": 0, "blockers": [],
            "obligationGeneration": 0, "obligations": {}}


def _valid_obligations(doc: dict) -> bool:
    obligations = doc["obligations"]
    if (not isinstance(obligations, dict)
            or not _candidates._is_nonnegative_int(doc["obligationGeneration"])):
        return False
    for name, ob in obligations.items():
        if (name not in _DEPENDENCY_LOCKS or not isinstance(ob, dict)
                or set(ob) != _OBLIGATION_KEYS
                or not all(_candidates._is_nonnegative_int(ob[key])
                           and ob[key] <= doc["obligationGeneration"]
                           for key in _OBLIGATION_KEYS)
                or ob["exclusiveFailure"] > ob["failure"]):
            return False
    return True


def _read_continuation_state() -> dict:
    """The continuation state; absent means nothing pending. An unreadable or
    invalid document reads as PENDING (with no obligation), so a damaged file
    can only cost one extra pass, never strand work."""
    try:
        raw = _continuation_state_path().read_bytes()
    except FileNotFoundError:
        return _default_continuation_state()
    except OSError:
        return dict(_default_continuation_state(), pending=True)
    try:
        doc = _candidates._require_object(
            _candidates._load_json_document(raw, maximum_bytes=64 * 1024),
            "continuation")
        version = doc.get("schemaVersion")
        keys = {"schemaVersion", "pending", "pendingGeneration",
                "wakeGeneration", "blockers"}
        if version == 1:
            _candidates._require_exact_keys(doc, keys)
            doc = dict(doc, schemaVersion=_CONTINUATION_SCHEMA_VERSION,
                       obligationGeneration=0, obligations={})
        else:
            _candidates._require_exact_keys(
                doc, keys | {"obligationGeneration", "obligations"})
        if (version not in _CONTINUATION_SCHEMA_VERSIONS_READ
                or not isinstance(doc["pending"], bool)
                or not _candidates._is_nonnegative_int(doc["pendingGeneration"])
                or not _candidates._is_nonnegative_int(doc["wakeGeneration"])
                or not isinstance(doc["blockers"], list)
                or not all(isinstance(b, str) for b in doc["blockers"])
                or not _valid_obligations(doc)):
            raise _candidates.StateValidationError("invalid continuation state")
        return doc
    except (ValueError, TypeError, KeyError):
        return dict(_default_continuation_state(), pending=True)


class _ContinuationStateLock:
    """The leaf lock around one read-modify-write of the continuation state.
    With ``blocking=False`` a held lock raises `BlockingIOError` at once
    (the render's waiter check never waits, fix round K12)."""

    def __init__(self, *, blocking: bool = True):
        self._blocking = blocking

    def __enter__(self):
        _durable_mkdir(_bound_root())
        self._fd = os.open(_continuation_lock_path(), os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX
                        | (0 if self._blocking else fcntl.LOCK_NB))
        except BaseException:
            os.close(self._fd)
            raise
        return self

    def __exit__(self, exc_type, exc, traceback):
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(self._fd)
        return False


def _write_continuation_state(state: dict) -> None:
    """Durable (review finding CD2-007, cycle 4 verification): a host crash
    must neither restore an earlier document — which would reuse the pending,
    wake and obligation generations already handed out — nor tear this one.
    Only paths with work write it; the steady state writes nothing (#901)."""
    _atomic_write_json(_continuation_state_path(), state, durable=True)


def _try_reserve(path: pathlib.Path) -> "int | None":
    """One non-blocking exclusive reservation flock, or None when held."""
    try:
        _durable_mkdir(_bound_root())
        fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def _release_reservation(fd: "int | None") -> None:
    if fd is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass


def _residual_derivation_owed() -> bool:
    """File-only (one listing): does a derivation completion owe the ONE
    post-unlock ingest of the inputs its published prefix stopped before
    (#825/#832 fix round J4), and is an attempt due now? That work has no
    statusline intake, so the worker answers it itself
    (`_run_owed_residual_derivation`). An obligation waiting out the backoff
    of its own failed attempt (fix round K6) is durable but not pending: it
    is answered by the first activating event after the backoff ends, so a
    persistently failing ingest is never retried once per render."""
    try:
        import _cctally_completion
        return _cctally_completion.residual_derivation_due()
    except Exception:
        return False


def _work_pending_on_disk() -> bool:
    """File-only: does any partition hold unacknowledged intake or a frozen
    plan, or does a completion owe its residual ingest? A partition whose
    transition cannot be read is not counted: it cannot progress until it is
    repaired, so waking for it would only spin."""
    if _residual_derivation_owed():
        return True
    now_epoch = int(time.time())
    for account_key in _account_keys_on_disk():
        try:
            transition = _read_transition(account_key, now_epoch=now_epoch)
            if transition is not None and transition.publication is not None:
                return True
            if _intake_backlog(account_key, _acknowledged_through(transition)):
                return True
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return False


def _unregistered_work_on_disk() -> bool:
    """File-only and lock-free, for an activating event that found the
    coordination state idle or absent (review finding CD2-007, cycle 4
    verification): does any partition hold unacknowledged intake? That is
    the authoritative record of retained work — a frozen plan always sits
    above the checkpoint, so its evaluation is unacknowledged intake too.
    The cost is one listing of the accounts root and one of each
    partition's intake directory, which compaction leaves empty in the
    steady state; only a partition that HAS a segment costs a checkpoint
    read and a backwards tail read. A partition whose state cannot be read
    is not counted, exactly as `_work_pending_on_disk` does not count it:
    waking for it would only spin."""
    now_epoch = int(time.time())
    for account_key in _account_keys_on_disk():
        if not _intake_segments(account_key):
            continue
        try:
            transition = _read_transition(account_key, now_epoch=now_epoch)
            if transition is not None and transition.publication is not None:
                return True
            if _intake_backlog(account_key, _acknowledged_through(transition)):
                return True
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return False


def _intake_tails() -> dict:
    """File-only: each readable partition's last logged sequence number."""
    tails = {}
    for account_key in _account_keys_on_disk():
        try:
            tail = _intake_tail_sequence(account_key)
        except (OSError, ValueError, TypeError, KeyError):
            continue
        if tail is not None:
            tails[account_key] = tail
    return tails


def _intake_arrived(start: dict, drained: dict) -> bool:
    """File-only: did any partition log an evaluation its holder's pass did
    not drain and nobody has acknowledged — an arrival captured after the
    drain began (``drained``) or, for a partition the pass never reached,
    after the pass started (``start``)? This is how unacknowledged intake
    participates in the holder's final release-or-hand-off check (spec 4.3
    revision 3, Q4): a capture that committed and then died before its wake
    registration is still answered. A partition whose state cannot be read
    is not counted: waking for it would only spin."""
    now_epoch = int(time.time())
    state = None
    for account_key in _account_keys_on_disk():
        try:
            tail = _intake_tail_sequence(account_key)
            if tail is None:
                continue
            seen = drained.get(account_key, start.get(account_key, -1))
            if tail <= seen:
                continue
            transition = _read_transition(account_key, now_epoch=now_epoch)
            if tail <= _acknowledged_through(transition):
                continue
            if state is None:
                state = _read_continuation_state()
            if _head_waiting(account_key, transition, now_epoch=now_epoch,
                             state=state):
                # Intake behind a waiting head is retained, never runnable
                # (spec 4.3 revision 5).
                continue
            return True
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return False


def _register_capture() -> None:
    """A render capture that lost the selected-state try-lock: register its
    pending work and its wake through the continuation protocol, under
    `continuation.lock` — the same lock the holder's final
    release-or-hand-off check compares the wake generation in — then
    dispatch a worker only when none is reserved or running. Never raises,
    never waits.

    Spec 4.3 revision 5 (*Waiting heads*): while a worker holds the one
    reducer reservation, captures coalesce into ONE durable dirty
    notification — pending set once, nothing more. The capture's intake is
    committed before this check, and the worker's final release check reads
    the authoritative intake tails under this same lock, so it cannot clear
    the newer work."""
    coalesced = False
    try:
        with _ContinuationStateLock():
            fd = _try_reserve(_reducer_reservation_path())
            if fd is None:
                coalesced = True
                state = _read_continuation_state()
                if not state["pending"]:
                    state["pending"] = True
                    state["pendingGeneration"] += 1
                    _write_continuation_state(state)
                if not _dirty_notification_path().exists():
                    _atomic_write_json(_dirty_notification_path(),
                                       {"schemaVersion": 1}, durable=True)
            else:
                _release_reservation(fd)
    except (OSError, ValueError):
        pass
    if coalesced:
        # Registered; the running worker answers it, so no wake follows.
        _crash_point("before-capture-wake")
        return
    try:
        _mark_continuation_pending(capture=True)
        _crash_point("before-capture-wake")
    except (OSError, ValueError):
        pass
    post_wake("intake")


def _mark_continuation_pending(*, capture: bool = False) -> None:
    """New durable intake: pending before anything can defer it, so a crash
    after enrollment can never leave unacknowledged work marked idle.

    A render capture (``capture``) always advances the pending generation,
    in the same `continuation.lock` protocol as a pass's end-of-pass record:
    a pass that scanned the disk before this capture committed sees the
    generation move and cannot clear the newer arrival's obligation (spec
    4.3 revision 3, Q4). Other new durable work registers the same way
    (`register_durable_work`)."""
    if not capture and _read_continuation_state()["pending"]:
        return
    with _ContinuationStateLock():
        state = _read_continuation_state()
        if state["pending"] and not capture:
            return
        state["pending"] = True
        state["pendingGeneration"] += 1
        _write_continuation_state(state)


def register_durable_work() -> None:
    """New durable work that is not statusline intake — a derivation
    completion's residual obligation (#825/#832 spec 4.7) — registered
    through the generation protocol a render capture uses, once the work is
    durable and before its wake is posted (fix round K7, Codex round 14
    P1-1). A pass whose disk scan ran before this work was written sees the
    pending generation move and keeps pending, so the worker's final
    hand-off answers the wake that follows instead of releasing past it. A
    registration a pass already cleared sets pending again by itself.

    Taken under the caller's accounting locks: `continuation.lock` is a leaf
    (nothing is acquired while it is held but the non-blocking reservation
    try-lock), so the lock-order law is unchanged. File-only, never raises,
    and posts no wake: the publisher posts its completion wake after it has
    released its locks.

    Fix round K13 (review cycle 7 P1-1): a failed registration is not
    absorbed. It falls back to the durable dirty notification a coalesced
    capture leaves, which a pass's end-of-pass record honours under the same
    lock; and the record also keeps pending for a wake recorded after its
    scan began, so a registration lost even to that fallback is answered by
    the publication wake that follows. It returns nothing (fix round K15,
    K14R-11): a registration lost even to the fallback is answered by that
    wake, so no caller has a decision to make from the outcome."""
    try:
        _mark_continuation_pending(capture=True)
        return
    except (OSError, ValueError):
        pass
    try:
        with _ContinuationStateLock():
            _atomic_write_json(_dirty_notification_path(),
                               {"schemaVersion": 1}, durable=True)
    except (OSError, ValueError):
        pass


def _pass_blocked(results) -> bool:
    """Whether every partition of a pass stayed blocked: skipped as a
    waiting head, or deferred into a wait whose descriptor it installed."""
    return bool(results) and all(
        result.status == "waiting"
        or (not result.rerun and _wait_class(result) is not None)
        for result in results)


def _record_pass_outcome(results) -> None:
    """At the end of every reducer pass, under its selected-state lock: what
    is still pending and what blocked it. Best effort: a failure leaves the
    previous (pending-biased) state, which costs at most one extra pass.

    The disk is scanned before the state lock is taken, so a render capture
    can commit and register after the scan: its registration advances the
    pending generation, and a scan older than that generation never clears
    pending (spec 4.3 revision 3, Q4)."""
    try:
        start = _read_continuation_state()
        if start["pending"] and _pass_blocked(results):
            # Spec 4.3 revision 5 (*Waiting heads*): a pass that stays
            # blocked neither advances the pending-work generation nor
            # rewrites continuation state; its heads' descriptors hold what
            # they wait for.
            return
        if _dirty_notification_path().exists():
            # The scan below covers every capture coalesced so far; one
            # coalesced after this point recreates the notification.
            with _ContinuationStateLock():
                _consume_dirty_notification()
                start = _read_continuation_state()
        pending = _work_pending_on_disk()
        blockers = sorted({name for result in results
                           for name in (getattr(result, "blockers", ()) or ())})
        if (not pending and not start["pending"] and not start["blockers"]
                and not _satisfied_obligations(start)):
            # The steady state writes nothing (#901).
            return
        with _ContinuationStateLock():
            state = _read_continuation_state()
            if (state["pendingGeneration"] != start["pendingGeneration"]
                    or state["wakeGeneration"] != start["wakeGeneration"]
                    or _dirty_notification_path().exists()):
                # Registered (or coalesced) after this pass's scan began:
                # not its to clear. Fix round K13 (review cycle 7 P1-1): so
                # is a wake recorded after the scan began — durable work
                # whose registration was lost still posts one, and the
                # worker's final release answers it only while pending.
                pending = True
            if pending:
                state["pendingGeneration"] += 1
            state["pending"] = pending
            state["blockers"] = blockers if pending else []
            # A discharged obligation is dropped; an outstanding one stays
            # until its waiter observes the release. Generations are never
            # reused, so an older waiter cannot match a newer entry.
            for name in _satisfied_obligations(state):
                del state["obligations"][name]
            _write_continuation_state(state)
    except (OSError, ValueError):
        pass


def _holds_accounting_locks() -> bool:
    if _SELECTED_DEPTH.get() > 0 or _cctally_core.holds_stats_maintenance():
        return True
    try:
        import _cctally_store
        return bool(_cctally_store.holds_ingest_lock())
    except Exception:
        return False


def _record_wake() -> bool:
    """Durably record one activating event; False when nothing is pending."""
    with _ContinuationStateLock():
        state = _read_continuation_state()
        if not state["pending"]:
            return False
        state["wakeGeneration"] += 1
        _write_continuation_state(state)
        return True


def _dispatch_or_owe() -> None:
    """Make sure one worker will act on the recorded wakes: take the ONE
    reducer reservation and spawn a worker that inherits it, unless a worker
    is already reserved or running. Under an accounting lock of this context
    the dispatch is owed until that lock is released."""
    if _holds_accounting_locks():
        _DISPATCH_OWED.set(True)
        return
    _DISPATCH_OWED.set(False)
    with _ContinuationStateLock():
        if not _read_continuation_state()["pending"]:
            return
        fd = _try_reserve(_reducer_reservation_path())
    if fd is not None:
        _spawn_owned("worker", fd)


def _flush_owed_dispatch() -> None:
    if _DISPATCH_OWED.get() and not _holds_accounting_locks():
        try:
            _dispatch_or_owe()
        except Exception:
            pass


#: Activating events a process posts that may already read the stats index
#: (fix round K13, review cycle 7 825-832-CD7-V02): an ingest cycle's holder,
#: a live index publication or rederive or journal-repair, a completion's
#: resume and an OAuth reconciliation. Never the render.
_INDEX_READING_WAKES = frozenset({
    "holder:ingest", "holder:rederive", "holder:journal-repair",
    "index-publication", "resume", "oauth"})


def _wake_runnable(reason: str, state: dict) -> bool:
    """Whether an activating event finds RUNNABLE work, not merely retained
    work (fix round K13, review cycle 7 825-832-CD7-V02; spec 4.3 *Waiting
    heads*): a due residual derivation, or an account whose head is not a
    waiting head that only a relevant change, a lock release or a due retry
    makes runnable. Eligibility is decided before a wake is recorded, so an
    event that finds only unchanged waiting heads records no wake, rewrites
    no continuation state and spawns no worker; another account's runnable
    work still dispatches.

    File-only first, exactly the render's check (C4). Only when every head
    still waits there, and only for an event posted where the index may
    already be read (`_INDEX_READING_WAKES`) and with no accounting lock of
    this context held, are the heads' dependency tokens compared — read
    without waiting (`_wait_tokens_without_waiting`), so a relevant change
    such as the account's own publication, coverage advancement, an index
    replacement or a quarantine change makes its head runnable at once. A
    render capture never reads the index: intake behind a waiting head is
    retained, not runnable. Any other event, and any doubt, answers True —
    the behaviour before this check."""
    if reason not in _INDEX_READING_WAKES and reason != "intake":
        return True
    try:
        if _residual_derivation_owed():
            return True
        now_epoch = int(time.time())
        waiting = []
        for account_key in _account_keys_on_disk():
            try:
                transition = _read_transition(account_key, now_epoch=now_epoch)
                head = _wait_head(account_key, transition)
            except (OSError, ValueError, TypeError, KeyError):
                # Unreadable: not counted, as `_work_pending_on_disk` does
                # not count it.
                continue
            if head is None:
                continue
            if not _head_waiting(account_key, transition, now_epoch=now_epoch,
                                 state=state, head=head):
                return True
            waiting.append(account_key)
        if not waiting:
            # Pending with no head at all: the worker reconciles it.
            return True
        if reason == "intake":
            return False
        if _holds_accounting_locks():
            return True
        for account_key in waiting:
            doc = _read_wait(account_key)
            if doc is None or _wait_tokens_without_waiting(
                    account_key, reason=doc["reason"]) != doc["tokens"]:
                return True
        return False
    except Exception:  # noqa: BLE001 — an activating event never raises
        return True


def post_wake(reason: str) -> None:
    """One activating event (spec 4.3 revision 2): an ingest cycle that held
    its locks, a live index publication, an OAuth reconciliation, a journal
    append while a frontier conflict is pending, new intake, or a dependency
    release. A cheap, file-only no-op when no work is pending. Never raises,
    never blocks on an accounting lock and never waits for the work.

    Idle or absent coordination state is not proof that nothing is retained
    (review finding CD2-007, cycle 4 verification): a host crash can keep a
    durable capture and lose the registration that followed it, or the
    render can die between its intake commit and that registration. So
    every activating event except a plain journal append (which only
    matters to a pending frontier conflict) reconstructs pending work from
    the committed intake (`_unregistered_work_on_disk`) and then takes the
    ordinary path — one recorded wake and one reservation-guarded dispatch.
    A derivation completion's residual-ingest obligation (#825/#832 fix
    round J4) is reconstructed the same way, from its durable marker: it has
    no intake, and the recorder that appended its input may have exited.
    The check takes no lock and costs two directory listings and one stat
    when nothing is retained.

    Fix round K13 (review cycle 7 825-832-CD7-V02): retained work is not
    runnable work. Before it records a wake, the event decides eligibility
    (`_wake_runnable`), so one that finds only unchanged waiting heads
    records nothing and dispatches nothing."""
    try:
        _flush_owed_dispatch()
        if _WAKES_SUPPRESSED.get():
            return
        state = _read_continuation_state()
        if not state["pending"]:
            if reason == "append" or not (_unregistered_work_on_disk()
                                          or _residual_derivation_owed()):
                return
            if not _wake_runnable(reason, state):
                # Fix round K13 (825-832-CD7-V02): reconstructed retained
                # work that only waits is not registered by this event.
                return
            _mark_continuation_pending()
            state = _read_continuation_state()
            if not state["pending"]:
                return
        if _outstanding_obligations(state):
            # A waiter that died (or whose spawn failed) left its obligation
            # outstanding with its reservation free: reclaimed here.
            _ensure_obligation_waiters()
        if reason == "append" and "frontier" not in state["blockers"]:
            return
        if not _wake_runnable(reason, state):
            # Fix round K13 (review cycle 7 825-832-CD7-V02, spec 4.3
            # *Waiting heads*): retained work is not runnable work. An event
            # that finds only unchanged waiting heads records no wake,
            # rewrites no continuation state and spawns no worker.
            return
        if _record_wake():
            _dispatch_or_owe()
    except Exception:
        pass


def resume_retained_work() -> None:
    """The rebuild-finisher and resumer entry point: a completion wake."""
    post_wake("resume")


def _spawn_owned(kind: str, fd: int, argument: "str | None" = None) -> None:
    """Hand ``fd`` — the reservation the spawned process runs under — to it.
    `_spawn_continuation` owns the descriptor from here on in every outcome
    (it closes its copy, and a failed spawn thereby releases the
    reservation); the wake stays durable and the next activating event
    reclaims a reservation nobody took."""
    try:
        _spawn_continuation(kind, fd, argument)
    except Exception:
        pass


def _spawn_continuation(kind: str, fd: int, argument: "str | None" = None) -> bool:
    """Detach one continuation process (`cctally _statusline-continue`) that
    inherits ``fd``, the reservation it runs under. The parent's copy is
    closed here without unlocking, so the child alone holds it. The child
    is told the store explicitly, so it can only act on this one."""
    command = [sys.executable,
               str(pathlib.Path(__file__).resolve().with_name("cctally")),
               _CONTINUE_COMMAND, "--reservation-fd", str(fd)]
    if kind == "wait":
        command += ["--wait", str(argument)]
    env = dict(os.environ)
    env["CCTALLY_DATA_DIR"] = str(_cctally_core.APP_DIR)
    import subprocess

    try:
        subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True,
            pass_fds=(fd,), env=env)
        return True
    except Exception:
        return False
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _outstanding_obligations(state: dict) -> list:
    """The dependency locks with a registered failure no waiter has yet
    observed released."""
    return sorted(name for name, ob in state["obligations"].items()
                  if ob["failure"] > ob["released"])


def _satisfied_obligations(state: dict) -> list:
    return sorted(name for name, ob in state["obligations"].items()
                  if ob["failure"] <= ob["released"])


def _reserve_obligation_waiters(state: dict) -> list:
    """Under the state lock: the ONE waiter reservation of every lock with an
    outstanding obligation, taken non-blocking; a lock whose waiter is already
    reserved or running is left to it."""
    reserved = []
    for name in _outstanding_obligations(state):
        fd = _try_reserve(_waiter_reservation_path(name))
        if fd is not None:
            reserved.append((name, fd))
    return reserved


def _spawn_waiters(reserved) -> None:
    for name, fd in reserved:
        _spawn_owned("wait", fd, name)


def _ensure_obligation_waiters() -> None:
    with _ContinuationStateLock():
        reserved = _reserve_obligation_waiters(_read_continuation_state())
    _spawn_waiters(reserved)


def _render_ensure_lock_waiters(account_key: str) -> None:
    """Fix round K12 (K11R-5, residual 8): a render behind a head that waits
    on a blocking lock ensures that lock's one dependency waiter, so a
    waiter that died or never spawned is reclaimed at this statusline tick.
    File-only and non-blocking (C4): a busy state lock or a reserved waiter
    is left alone, and nothing opens the index or waits. Never raises."""
    try:
        doc = _read_wait(account_key)
        if doc is None or not doc["locks"]:
            return
        with _ContinuationStateLock(blocking=False):
            reserved = _reserve_obligation_waiters(_read_continuation_state())
    except Exception:  # noqa: BLE001 — the render's check is best effort
        return
    _spawn_waiters(reserved)


def register_lock_obligations(blocks) -> None:
    """Persist one obligation per contended failed acquisition and reserve the
    single waiter of each lock (Codex fix-round-4 Q3), even when its holder
    has already released. Called only once the failing pass has released
    every accounting lock. A block that is not contention (an I/O error)
    registers nothing, so it can never manufacture a release wake.

    This is the coordination protocol's obligation half: the failure
    generation is allocated, recorded and its waiter reserved in ONE
    state-lock critical section, the same one a finishing waiter makes its
    release-or-continue decision in, so a newer failure is either served by a
    running waiter or finds its reservation free (never neither)."""
    blocks = [block for block in blocks
              if block is not None and block.contended
              and block.lock in _DEPENDENCY_LOCKS]
    if not blocks:
        return
    with _ContinuationStateLock():
        state = _read_continuation_state()
        for block in sorted(blocks, key=lambda b: (b.lock, b.mode)):
            state["obligationGeneration"] += 1
            generation = state["obligationGeneration"]
            ob = state["obligations"].setdefault(
                block.lock, {"failure": 0, "exclusiveFailure": 0, "released": 0})
            ob["failure"] = generation
            if block.mode == "exclusive":
                ob["exclusiveFailure"] = generation
        _write_continuation_state(state)
        reserved = _reserve_obligation_waiters(state)
    _spawn_waiters(reserved)


def _settle_obligation(name: str, through: int) -> None:
    """Record the failures through generation ``through`` released. The
    watermark only rises, so a waiter finishing an older obligation can
    never erase a newer failure."""
    with _ContinuationStateLock():
        state = _read_continuation_state()
        ob = state["obligations"].get(name)
        if ob is None or ob["released"] >= through:
            return
        ob["released"] = min(through, ob["failure"])
        _write_continuation_state(state)


def _satisfied_through(start: dict, held: "dict | None", *, exclusive: bool) -> int:
    """The failure generation an acquisition in the chosen mode satisfies.

    Every failure registered before the acquisition began (``start``, read
    when this round chose its mode) was refused by a holder conflicting with
    that mode, so it is released. While the lock is still held, a failure
    registered meanwhile (``held``) was refused only by this waiter's own
    hold — unless it requested the lock exclusively and this round holds it
    shared, when another shared holder may have refused it: such a failure,
    and everything after it, stays outstanding for the next round."""
    through = start["failure"]
    if held is None:
        return through
    if exclusive or held["exclusiveFailure"] <= start["released"]:
        return max(through, held["failure"])
    return max(through, held["exclusiveFailure"] - 1)


def _dependency_wait(name: str, reservation_fd: int) -> None:
    """The waiter of one dependency lock (spec 4.3 revision 2, Codex
    fix-round-4 Q3). For each round of outstanding obligations: ONE blocking
    acquisition in a mode that conflicts with the holder that refused them,
    a read of what it satisfied while still holding the lock, release and
    close, then one recorded release and one wake. It ends — releasing its
    reservation under the state lock — once nothing is outstanding. No
    ingest, no rebuild, no other lock held, and every round answers a
    failure registered before it, so it never retries on its own."""
    released = False
    try:
        path = _dependency_lock_paths()[name]
        while True:
            with _ContinuationStateLock():
                state = _read_continuation_state()
                ob = state["obligations"].get(name)
                if ob is None or ob["failure"] <= ob["released"]:
                    _release_reservation(reservation_fd)
                    released = True
                    return
                start = dict(ob)
            exclusive = start["exclusiveFailure"] > start["released"]
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
                try:
                    held = _read_continuation_state()["obligations"].get(name)
                finally:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
            _settle_obligation(
                name, _satisfied_through(start, held, exclusive=exclusive))
            post_wake(f"release:{name}")
    finally:
        if not released:
            _release_reservation(reservation_fd)


def _run_reducer_pass(drained: "dict | None" = None) -> tuple:
    """One reducer pass under the selected-state lock, its own wakes
    suppressed; the dashboard nudge fires after the lock is released."""
    nudges = []
    try:
        with _selected_state_lock():
            results = _statusline_reduce_and_publish(
                nudge_sink=lambda: nudges.append(1), drained=drained)
    except _cctally().StatsRebuildDeferred as exc:
        deferral = _rebuild_unclassifiable()
        results = (AccountResult("", "deferred", None, str(exc),
                                 blockers=deferral.blockers,
                                 obligations=deferral.obligations),)
    if nudges:
        _cctally()._nudge_dashboard_repaint()
    return results


def _after_reducer_pass(results, *, worker: bool) -> "str | None":
    """Outside every statusline lock, once a pass has ended and released every
    accounting lock: register each contended failed acquisition as an
    obligation and reserve its lock's one waiter (Q3), follow a frontier
    conflict (the conflicting append is a genuine event), and report an epoch
    rebuild no worker owns (``"inline-rebuild"``) for the caller to run once
    it holds no continuation ownership. A failure no lock caused records
    nothing and manufactures no wake."""
    blockers = set()
    obligations = set()
    for result in results:
        blockers |= set(getattr(result, "blockers", ()) or ())
        obligations |= set(getattr(result, "obligations", ()) or ())
    if not blockers or not _read_continuation_state()["pending"]:
        return None
    register_lock_obligations(obligations)
    if "frontier" in blockers and _record_wake() and not worker:
        _dispatch_or_owe()
    return "inline-rebuild" if "rebuild-unowned" in blockers else None


def _run_inline_rebuild() -> None:
    """The epoch rebuild no worker owns, run by the deferring process while
    it holds no continuation ownership. Its finisher
    (`cmd_stats_epoch_rebuild_internal`) posts the durable completion wake
    after releasing its locks; a worker that took the rebuild first gets a
    dependency waiter instead."""
    import _cctally_store

    try:
        if not _cctally_store.stats_epoch_rebuild_pending():
            return
        if not _cctally_store._stats_epoch_rebuild_worker_active():
            _cctally_store.cmd_stats_epoch_rebuild_internal(None)
        if _cctally_store.stats_epoch_rebuild_pending():
            # A worker that took the rebuild first refused this one's
            # try-lock: an obligation for that worker lock's release.
            register_lock_obligations(_cctally_store.stats_rebuild_worker_blocks())
    except (Exception, _cctally().StatsRebuildDeferred):
        pass


def _run_owed_residual_derivation() -> None:
    """The ONE post-unlock ingest a derivation completion owes for the
    inputs its published prefix stopped before (#825/#832 fix round J4),
    run by the worker before its reducer pass and outside every
    selected-state and accounting lock (the reservation is a try-lock no
    lock holder ever waits for). The ingest's own wakes are suppressed
    because the pass that follows answers them. Never raises.

    The attempt keeps every obligation its applied cursor does not cover
    (fix rounds K5 and K6, Codex cycle 6 P1-1 and its verification). When it
    returned a lock-contention stop — the maintenance or ingest lock refused
    it, or its cache leg prefix-stopped on a cache writer flock — the lock
    that refused it becomes an obligation of that lock's one-shot waiter,
    registered here once every accounting lock is released, exactly as a
    deferred statusline reading's is: the waiter's release wake dispatches
    the next attempt, and nothing retries meanwhile. An obligation that
    stays owed also keeps the coordination state pending
    (`_work_pending_on_disk`), so no pass can clear the work it stands for —
    except while it waits out a failed attempt's backoff, when nothing is to
    be done until an activating event after it."""
    attempt = None
    token = _WAKES_SUPPRESSED.set(True)
    try:
        import _cctally_completion
        attempt = _cctally_completion.run_owed_residual_derivation()
    except Exception:
        pass
    finally:
        _WAKES_SUPPRESSED.reset(token)
    block = getattr(attempt, "blocked_by", None)
    if block is not None and getattr(attempt, "disposition", None) == "blocked":
        try:
            register_lock_obligations([block])
        except Exception:
            pass


def _continuation_worker(reservation_fd: int) -> None:
    """Drive pending work while holding the ONE reducer reservation. Every
    pass answers an event: the one that spawned this worker, or a wake
    recorded while the previous pass ran. A residual ingest a derivation
    completion owes runs first, so the pass classifies against an index
    that has derived it."""
    released = False
    try:
        while True:
            captured = _read_continuation_state()["wakeGeneration"]
            _run_owed_residual_derivation()
            start = _intake_tails()
            drained: dict = {}
            results = _run_reducer_pass(drained)
            follow_up = _after_reducer_pass(results, worker=True)
            if follow_up == "inline-rebuild":
                # Ownership is released BEFORE the worker runs, so the
                # finisher's completion wake can dispatch a fresh worker.
                _release_reservation(reservation_fd)
                released = True
                _run_inline_rebuild()
                return
            # A head whose dependency changed before its wait could be
            # installed is still runnable (spec 4.3 revision 5): another
            # pass, without another event.
            rerun = any(getattr(result, "rerun", False) for result in results)
            _crash_point("before-final-release")
            with _ContinuationStateLock():
                state = _read_continuation_state()
                # Unacknowledged intake logged after this pass's drain began
                # participates too (Q4): a capture that died before
                # registering its wake is still answered. So does a capture
                # that coalesced into the running worker's dirty notification
                # instead of dispatching (`_register_capture`): the intake
                # tails, not the notification, are authoritative.
                if not (rerun or (state["pending"]
                                  and state["wakeGeneration"] != captured)
                        or _intake_arrived(start, drained)):
                    _release_reservation(reservation_fd)
                    released = True
                    return
    finally:
        if not released:
            _release_reservation(reservation_fd)


def _run_spawned_continuation(kind: str, fd: int, argument: "str | None" = None) -> None:
    """The body of a spawned continuation process, which owns ``fd``."""
    if kind == "wait":
        _dependency_wait(str(argument), fd)
    else:
        _continuation_worker(fd)


def cmd_statusline_continue_internal(args) -> int:
    """Hidden `_statusline-continue`: a reserved continuation worker, or one
    dependency waiter (`--wait <lock>`). Always returns 0."""
    fd = getattr(args, "reservation_fd", None)
    try:
        if isinstance(fd, int) and fd >= 0:
            os.fstat(fd)
            wait = getattr(args, "wait", None)
            _run_spawned_continuation("wait" if wait else "worker", fd, wait)
    except BaseException:  # noqa: BLE001 — a detached worker exits cleanly
        pass
    finally:
        _finalize_wal_checkpoints()
    return 0


#: The most unacknowledged evaluations a render reduces before it leaves the
#: backlog to the detached reducer instead.
_RENDER_ACKNOWLEDGE_LIMIT = 16


def _render_acknowledge(account_key: str, transition: _Transition, *,
                        now_epoch: int, coverage: str,
                        journal_tail: "tuple[str, int] | None" = None) -> bool:
    """The render, holding the selected state, acknowledges the evaluations
    it and its peers captured when every one of them reduces to nothing
    against the checkpointed control, exactly as the file-only fork decision
    has always judged an unchanged reading — so an unchanged render stays
    child-free and the intake stays bounded (C4). Each is reduced at its own
    instant with its own membership; any publication, control change, anchor
    or binding repair, or a backlog past the limit, is the detached
    reducer's. Returns True when nothing is left for it.

    An unchanged index fingerprint does not prove that the journal holds no
    input the index has not folded (review finding P1-2, cycle 3): a current
    producer may have appended and not yet ingested. So the checkpointed
    projection must carry its covered classification's journal coverage
    proof, ``coverage`` — the digest of the live fingerprint with the
    unlocked journal listing — must still equal it, and each evaluation's
    instant must lie in the range that proof was read for. Anything else —
    growth, a new segment, an absent or unreadable proof — leaves the
    evaluation to detached classification."""
    proof = transition.coverage
    if proof is None or proof["digest"] != coverage:
        return False
    withheld = bool(transition.quarantine and transition.quarantine.get("dropWithheld"))
    last = None
    for count, evaluation in enumerate(_intake_evaluations(
            account_key, after=transition.intake_acknowledged)):
        if count >= _RENDER_ACKNOWLEDGE_LIMIT:
            return False
        if ((proof["from"] is not None and evaluation.at < proof["from"])
                or (proof["through"] is not None and evaluation.at > proof["through"])):
            return False
        if (evaluation.journal_at is None
                or evaluation.journal_at != journal_tail) and any(
                (db_axis := _candidates._projection_axis(
                    transition.control.db_projection, axis)) is not None
                and db_axis.captured_at == evaluation.at
                for axis in _candidates.AXES):
            # A reading in the evaluation's own second is ordered against it
            # by the journal (fix round K5). The render never reads the
            # journal (C4); it only knows that nothing was appended since the
            # evaluation was enrolled when the listing it already took still
            # ends where the evaluation recorded — then the reading precedes
            # it. Otherwise the detached reducer decides.
            return False
        decision, anchor, _active, _fences = _reduce_partition(
            account_key, evaluation.members, transition, now_epoch=evaluation.at,
            fence_epoch=now_epoch, projection=transition.control.db_projection,
            drop_consensus_allowed=not withheld)
        if (decision.action != "NOOP" or decision.unknown_axes
                or anchor != transition.five_hour_anchor
                or _partition_binding(evaluation.members, transition)
                != transition.binding):
            # Fix round K13 (review cycle 7 P1-2): a NOOP on an axis the
            # checkpointed projection cannot classify is unknown work under
            # either gate, never an unchanged reading.
            return False
        if withheld and _unestablished_axes(_reduce_partition(
                account_key, evaluation.members, transition,
                now_epoch=evaluation.at, fence_epoch=now_epoch,
                projection=transition.control.db_projection)[0], decision,
                transition.control.db_projection):
            # Fix round K10 (P1-2): a NOOP only because the gate is closed on
            # an axis with no baseline is unknown classification, never an
            # unchanged reading. The checkpointed projection carries no
            # classification evidence (revision 5) and the render cannot read
            # the index (C4), so the detached reducer classifies it: it
            # publishes a proven-empty or new-window reading, retains an
            # unknown one.
            return False
        if _receipt_work(transition, evaluation.members):
            # #797 spec 4.4 (D7c q3): a NOOP that leaves receipt work for
            # either earning path is the detached reducer's to complete.
            return False
        last = evaluation.sequence
    if last is None:
        return True
    # The checkpoint and the unchanged control state in ONE atomic write,
    # then compaction of the acknowledged prefix.
    _write_transition(_replace_transition(
        transition, account_key=account_key, binding=transition.binding,
        projection=transition.control.db_projection, control=transition.control,
        acknowledged=last, coverage=proof, stale_cache=transition.stale_cache))
    _crash_point("after-render-acknowledgement")
    _intake_compact(account_key, last)
    return True


def _preliminary_decision() -> "_candidates.ReductionDecision | None":
    """The render path's file-only fork decision (no journal, no index work):
    any partition whose transition is missing, stale, invalid or carries a
    pending plan, or whose reduction is not a NOOP, needs the detached
    reducer.

    Captured evaluations the render can see reduce to nothing are
    acknowledged here (`_render_acknowledge`) — only while no continuation
    work is pending or blocked, so a deferral's retained evaluations are
    always the detached reducer's to classify, and only against a projection
    whose journal coverage proof still holds (P1-2, cycle 3)."""
    now_epoch = int(time.time())
    state = _read_continuation_state()
    quiet = not (state["pending"] or state["blockers"]
                 or _outstanding_obligations(state))
    by_account: dict = {}
    for candidate in _load_candidate_spool(now_epoch=now_epoch):
        by_account.setdefault(candidate.binding.account_key, []).append(candidate)
    fingerprint = _db_file_fingerprint()
    keys = sorted(set(by_account) | set(_account_keys_on_disk()),
                  key=lambda key: key.encode("utf-8"))
    fork = _candidates.ReductionDecision(
        "WRITE_CONTROL", _empty_control(_candidates.DbProjection(None, None, fingerprint)))
    coverage = None
    journal_tail = None
    for account_key in keys:
        try:
            transition = _read_transition(account_key, now_epoch=now_epoch)
        except (OSError, ValueError, TypeError, KeyError):
            continue
        active = tuple(by_account.get(account_key, ()))
        try:
            backlog = _intake_backlog(account_key, _acknowledged_through(transition))
            # Spec 4.3 revision 5 (*Waiting heads*): an account whose head
            # waits on an unchanged dependency is retained, not runnable —
            # the render never forks for it. File-only (C4).
            if (backlog or (transition is not None
                            and transition.publication is not None)) and _head_waiting(
                    account_key, transition, now_epoch=now_epoch, state=state):
                continue
        except (OSError, ValueError, TypeError, KeyError):
            # A damaged intake fails closed like a damaged transition: the
            # partition is left untouched rather than re-forked every render.
            continue
        if transition is None:
            if active or backlog:
                return fork
            continue
        if transition.publication is not None:
            # A frozen plan is resumed by the detached reducer; a render can
            # never classify it (C4). The check is file-only.
            return fork
        if backlog:
            # Logged work the render cannot prove unchanged is the detached
            # reducer's, which drains it in recorded order.
            if not quiet or transition.control.db_projection.db_files != fingerprint:
                return fork
            try:
                if coverage is None:
                    # One directory listing and a stat per segment, no open.
                    extents = _journal_extents_unlocked()
                    coverage = _coverage_digest(fingerprint, extents)
                    journal_tail = extents[-1] if extents else ("", 0)
                if not _render_acknowledge(account_key, transition,
                                           now_epoch=now_epoch, coverage=coverage,
                                           journal_tail=journal_tail):
                    return fork
            except (OSError, ValueError, TypeError, KeyError):
                return fork
        if not active and not any(transition.control.pending_drops.values()):
            continue
        if transition.control.db_projection.db_files != fingerprint:
            return fork
        # A drop the detached reducer last withheld stays withheld here: the
        # render path cannot open the index to re-check the quarantine (C4),
        # and re-forking every render for it would be pure disk cost.
        decision, _anchor, _active, _fences = _reduce_partition(
            account_key, active, transition, now_epoch=now_epoch,
            projection=transition.control.db_projection,
            drop_consensus_allowed=not (
                transition.quarantine
                and transition.quarantine.get("dropWithheld")))
        if decision.action != "NOOP":
            return decision
        if decision.unknown_axes:
            # Fix round K13 (review cycle 7 P1-2): a reading the checkpointed
            # projection cannot classify is the detached reducer's to
            # classify, under either gate (C4: the render reads no index).
            return fork
        if _receipt_work(transition, active):
            # #797 spec 4.4 (D7c q3): the NOOP leaves receipt work, so the
            # preliminary NOOP exit must not swallow it: the detached reducer
            # completes the evaluation and earns.
            return fork
    return None


def _finalize_wal_checkpoints() -> None:
    """The W9 finalizer before a forked child's ``os._exit`` (#901 Q14).

    `os._exit` skips `atexit`. The persist child records usage and the OAuth
    child may too, and either can reach a cache sync that arms a store; the
    finalizer then attempts one PASSIVE per armed store and closes the
    keepers. With nothing armed it does nothing."""
    try:
        import _lib_wal_checkpoint

        _lib_wal_checkpoint.finalize()
    except BaseException:  # noqa: BLE001 — the child exits regardless
        pass


def _fork_persist(parent_lock_fd: int, reservation_fd: int) -> None:
    """Run the revalidated spool reducer in a detached serialized child.

    Spec 4.3 revision 5: the render's dispatch goes through the ONE reducer
    reservation, ``reservation_fd``, which the child inherits and runs the
    continuation worker under — the pass under the selected-state lock it
    takes once the render releases it, the dashboard nudge after that lock
    is released (#583 S2), the hand-off of what it could not finish (a
    dependency waiter per held lock, a frontier wake, the epoch rebuild no
    worker owns) and the final release check — so a render's child and a
    dispatched worker never overlap. The parent's copy is closed without
    unlocking."""
    try:
        pid = os.fork()
    except OSError:
        _release_reservation(reservation_fd)
        return
    if pid > 0:
        try:
            os.close(reservation_fd)
        except OSError:
            pass
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
        _continuation_worker(reservation_fd)
    except BaseException:
        pass
    finally:
        _finalize_wal_checkpoints()
        os._exit(0)


def _statusline_persist(parsed, *, sync_for_test: bool = False) -> None:
    """Capture an eligible bound candidate, then reduce it opportunistically.

    This is the RENDER path (C4): it captures one binding, logs the arrival
    with the evaluation it actually saw in the account's durable intake and
    only then installs its spool slot (spec 4.2 and 4.3 revision 3, Codex
    design-fix-round-4 Q4), and decides from files alone whether to fork the
    detached reducer. Capture precedes the selected-state try-lock, so a
    render that loses it leaves a durable arrival and registers its wake for
    the running holder or a worker — it never returns silently on a busy
    lock. It never scans the journal, never establishes the cutover and
    never touches the index."""
    candidate = _candidate_from_input(parsed, received_at=int(time.time()))
    if candidate is None:
        return
    try:
        captured = _capture_arrival(candidate)
        _cctally()._statusline_transport_touch()
    except (OSError, ValueError, TypeError, KeyError):
        return
    _crash_point("after-capture")
    # Spec 4.3 revision 5 (*Waiting heads*, design-fix-round-5 Q3): a
    # capture behind a waiting head makes its required durable intake
    # append and nothing more — no fork, no registration. File-only. A head
    # waiting on a blocking lock has that lock's waiter ensured (fix round
    # K12, K11R-5), non-blocking.
    if _render_head_waiting(candidate.binding.account_key,
                            now_epoch=candidate.received_at):
        _render_ensure_lock_waiters(candidate.binding.account_key)
        return
    c = _cctally()
    lock_fd = c._try_acquire_persist_lock()
    if lock_fd is None:
        if captured is not None:
            _register_capture()
        return
    reservation = None
    try:
        preliminary = _preliminary_decision()
        if preliminary is None or preliminary.action == "NOOP":
            return
        # The render's dispatch goes through the one reducer reservation. A
        # worker that holds it answers this capture in its final release
        # check, so the render only coalesces its notification.
        reservation = _try_reserve(_reducer_reservation_path())
        if reservation is None:
            c._release_persist_lock(lock_fd)
            lock_fd = None
            _register_capture()
            return
        if sync_for_test:
            # The synchronous twin of the detached child: the worker body,
            # once this frame no longer holds the selected state.
            c._release_persist_lock(lock_fd)
            lock_fd = None
            fd, reservation = reservation, None
            _continuation_worker(fd)
            return
        fd, reservation = reservation, None
        _fork_persist(lock_fd, fd)
    finally:
        if lock_fd is not None:
            c._release_persist_lock(lock_fd)
        if reservation is not None:
            _release_reservation(reservation)


def _try_acquire_statusline_oauth_lock() -> "int | None":
    """Take hook-tick's account-wide OAuth throttle lock without blocking."""
    try:
        _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)
        fd = os.open(
            _cctally_core.HOOK_TICK_THROTTLE_LOCK_PATH,
            os.O_WRONLY | os.O_CREAT,
            0o644,
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


def _release_statusline_oauth_lock(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass


def _run_statusline_oauth_refresh() -> None:
    """Run one already-throttled automatic OAuth confirmation."""
    c = _cctally()
    status, _ = c._hook_tick_oauth_refresh(
        throttle_seconds=float(_cctally_core.STATUSLINE_OAUTH_POLL_SECONDS)
    )
    if status.startswith("ok("):
        c._hook_tick_throttle_touch()


def _fork_statusline_oauth_refresh(lock_fd: int) -> bool:
    """Detach one OAuth confirmation while keeping ``lock_fd`` in the child."""
    try:
        pid = os.fork()
    except OSError:
        return False
    if pid > 0:
        # Do not explicitly unlock: the child inherited the same open file
        # description and owns the account-wide lock until its refresh ends.
        try:
            os.close(lock_fd)
        except OSError:
            pass
        return True

    try:
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
        _run_statusline_oauth_refresh()
    except BaseException:
        pass
    finally:
        _release_statusline_oauth_lock(lock_fd)
        _finalize_wal_checkpoints()
        os._exit(0)


def _statusline_oauth_tick(*, sync_for_test: bool = False) -> None:
    """Schedule one bounded authoritative refresh from a statusline timer.

    Fast prechecks avoid forks while selected data is fresh, a shared 429
    deadline is active, or another session/hook owns the account-wide poll.
    Every condition is rechecked under hook-tick's throttle lock before the
    detached child starts.
    """
    c = _cctally()
    interval = float(_cctally_core.STATUSLINE_OAUTH_POLL_SECONDS)
    if c._statusline_observe_age_seconds() < interval:
        return
    if c._oauth_backoff_remaining_seconds() > 0:
        return
    if c._hook_tick_throttle_age_seconds() < interval:
        return
    lock_fd = _try_acquire_statusline_oauth_lock()
    if lock_fd is None:
        return
    try:
        if c._statusline_observe_age_seconds() < interval:
            return
        if c._oauth_backoff_remaining_seconds() > 0:
            return
        if c._hook_tick_throttle_age_seconds() < interval:
            return
        if sync_for_test:
            _run_statusline_oauth_refresh()
            return
        if _fork_statusline_oauth_refresh(lock_fd):
            lock_fd = -1
    finally:
        if lock_fd >= 0:
            _release_statusline_oauth_lock(lock_fd)


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
                "  se.cost_usd_raw, se.cache_create_1h_tokens, se.speed "
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
            usage = c.claude_usage_dict(   # #195 chokepoint
                input_tokens=r[2],
                output_tokens=r[3],
                cache_creation_tokens=r[4],
                cache_read_tokens=r[5],
                cache_1h_tokens=r[7],
                speed=r[8],
            )
            # #413: speed is materialized, so the hot statusline path keeps the
            # effective tier without parsing the legacy usage_extra_json blob.
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
        except c.StatsRebuildDeferred:
            recorded_windows, block_start_overrides, canonical_intervals = (
                [], {}, {},
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
        # #341 (spec §3): scope the 7d reset-aware clamp to the active Claude
        # account (torn -> merged None; identified -> real key; absent ->
        # `unattributed`). Byte-frozen for <=1 real account (all rows default
        # `unattributed`). `_resolve_active_claude_identity` is mtime-cached, so
        # resolving here (as well as in `_read_db_projection_once`) is a cache hit.
        _sl_account = _statusline_active_account()
        _acct_pred = "" if _sl_account is None else " AND account_key = ?"
        _acct_params = () if _sl_account is None else (_sl_account,)
        try:
            conn = open_db()
        except c.StatsRebuildDeferred:
            return (None, None)
        except Exception:
            return (None, None)
        try:
            if five_resets is not None:
                try:
                    key = c._canonical_5h_window_key(int(five_resets))
                    # HELD-INCLUSIVE (#769 S11, #824): a five-hour maximum over
                    # a five-hour window, and a held row's five-hour value is
                    # the writing tick's own.
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
                        account_key=_sl_account,
                    )
                    # `weekly_observation_held = 0` (#769 S11, #824): a WEEKLY
                    # maximum, and the floored leg is why it matters. A held
                    # row written after a credit floor carries the PRE-credit
                    # value forward under a post-floor capture instant, so
                    # counting it would re-clamp the status line to a
                    # high-water mark the credit retired — the same defence
                    # `_resolve_reset_aware_hwm` carries, restated here because
                    # this is an independent implementation of that maximum.
                    # Both legs take the predicate: the two must not disagree
                    # about what a held row is.
                    if floor_iso is not None:
                        row = conn.execute(
                            "SELECT MAX(weekly_percent) "
                            "FROM weekly_usage_snapshots "
                            "WHERE week_start_date = ?" + _acct_pred + " "
                            "  AND weekly_observation_held = 0 "
                            "  AND unixepoch(captured_at_utc) >= unixepoch(?)",
                            (week_start_date, *_acct_params, floor_iso),
                        ).fetchone()
                    else:
                        row = conn.execute(
                            "SELECT MAX(weekly_percent) "
                            "FROM weekly_usage_snapshots "
                            "WHERE week_start_date = ?" + _acct_pred +
                            "  AND weekly_observation_held = 0",
                            (week_start_date, *_acct_params),
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
        except _cctally().StatsRebuildDeferred:
            return None
        except Exception:
            return None
        try:
            # Prefer `week_end_at` (ISO timestamp; sub-day precision) over
            # `week_end_date` (date-only; UTC-midnight). Older snapshots
            # may have `week_end_at` NULL — fall back to the date column
            # in that case. See the neighbor query in `pick_week_selection`
            # (bin/cctally:3849) for the precedent.
            #
            # TWO queries, one per axis (#769 S11, #824). This used to be one
            # row because one row carried both readings, and a
            # `weekly_observation_held` row is the case where it does not: its
            # five-hour fields are this tick's own while its weekly value and
            # boundary were copied from an older row. The five-hour leg takes
            # the newest row of either kind; the weekly leg takes the newest
            # genuinely observed one. Asking one row for both would either hold
            # the five-hour percentage back or present a carried-forward weekly
            # percentage as this tick's reading.
            #
            # #825/#832 fix round K15 (K14R-10), K16c (spec 4.7 revision 7):
            # the five-hour leg takes the newest row that CARRIES a five-hour
            # reading, through the one shared selection. A five-hour credit
            # retires a stale replica in place (its five-hour fields NULL, its
            # weekly evidence kept), and a lagging capture can make that
            # retired row the newest one; a weekly-only observation is the
            # same shape. Neither is a five-hour reading. This reader selects
            # no account, so the selection adds no account predicate either.
            five_reading = _lib_five_hour.latest_five_hour_reading(conn)
            weekly_row = conn.execute(
                "SELECT weekly_percent, week_end_at, week_end_date "
                "FROM weekly_usage_snapshots "
                "WHERE weekly_observation_held = 0 "
                "ORDER BY captured_at_utc DESC, id DESC LIMIT 1"
            ).fetchone()
            if five_reading is None and not weekly_row:
                return None
            five_pct = (
                five_reading.percent if five_reading is not None else None)
            five_resets = (
                five_reading.window_key if five_reading is not None else None)
            seven_pct = (
                float(weekly_row[0])
                if weekly_row and weekly_row[0] is not None else None)
            seven_resets = None
            week_end_at = weekly_row[1] if weekly_row else None
            week_end_date = weekly_row[2] if weekly_row else None
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

    def _quota_regimes() -> tuple:
        """#661 S2 §9. The stored metering regimes, read without mutating.

        Three properties this read must have, and each is a decision:

        * It takes NO flock and NO throttle. `save_calibrations` writes
          through `os.replace` in the same directory, so a lock-free reader
          always sees a complete old or new inode; a flock here would let a
          writer stall a prompt.
        * It goes through `read_stored_state_readonly`, not through
          `load_calibrations`. The loader renames a malformed or
          version-ahead file aside through `_quarantine`, and a status line
          that quarantines the user's calibration once per prompt would be a
          writer on the hottest path in the product.
        * It catches `OSError` on the open and the read rather than
          pre-checking existence, which `read_stored_state_readonly` already
          does — a quarantine rename can remove the primary name between a
          check and an open, and the reader cannot tell "quarantined" from
          "absent" without scanning sidecars. It does not scan them; both
          render the same thing, which is no marker.

        It also NEVER opens `cache.db` and never calls `analyse`. The
        marker is derived from the regime pair alone.
        """
        try:
            glue = _cctally()._load_sibling("_cctally_quota_model")
            state = glue.read_stored_state_readonly()
            if state is None:
                return ()
            return tuple(glue.stored_regimes(state, None))
        except Exception:                              # noqa: BLE001
            return ()

    return _lib_statusline.StatuslineInjections(
        cctally_session_cost=_cctally_session_cost,
        today_cost=_today_cost,
        active_block=_active_block,
        hwm_clamp=_hwm_clamp,
        db_latest_rate_limits=_db_latest_rate_limits,
        context_pct=_context_pct,
        warn_once=warn_once,
        quota_regimes=_quota_regimes,
    )
