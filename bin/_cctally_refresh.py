"""OAuth-usage refresh: API fetch, UA discovery, renderers, `cctally refresh-usage` entry point.

Eager I/O sibling: bin/cctally loads this at startup and re-exports
every public symbol so bare-name callers (the dashboard
`POST /api/sync` handler, `cmd_hook_tick`'s oauth-refresh path, the
record-usage milestone gate, …) all resolve unchanged. Tests reaching
in via ``ns["X"]`` direct-dict access (extensive — see
`tests/test_refresh_usage_helpers.py`, `tests/test_refresh_usage_cmd.py`,
`tests/test_refresh_usage_inproc.py`, `tests/test_oauth_usage_config.py`,
`tests/test_ua_discovery.py`, `tests/test_hook_tick_rate_limit.py`,
`tests/test_dashboard_api_sync_refresh.py`) still work because the
re-export populates cctally's namespace at module-load time.

Stays in bin/cctally (reached via the ``_cctally()`` accessor):
  - ``_resolve_oauth_token`` / ``_read_keychain_oauth_blob`` —
    auth-layer primitives also consumed outside refresh.
  - ``_seconds_since_iso``, ``_select_last_known_snapshot``,
    ``_newest_snapshot_age_seconds`` — generic time / DB helpers
    used by dashboard, doctor, and freshness chips.
  - ``cmd_record_usage`` — hot-path record-usage entry (Phase D).
  - ``load_config`` — already in ``_cctally_config.py``; we read it
    through cctally's namespace so test monkeypatches propagate.
  - ``HOOK_TICK_DEFAULT_THROTTLE_SECONDS`` — config-fallback constant.
  - Tiny helpers: ``eprint``, ``now_utc_iso``, ``_iso_to_epoch``,
    ``_normalize_percent``, ``_forecast_color_enabled``,
    ``_format_short_duration``.

§5.6 Option C call-site rewrites (call-time lookup so test
monkeypatches on cctally's namespace propagate into this module):
  - `_discover_cc_version`, `_fetch_oauth_usage`,
    `_bust_statusline_cache`, `_refresh_usage_inproc`,
    `_get_oauth_usage_config` — all monkeypatched in at least one
    test; their internal call sites route through `c.X`.

Spec: docs/superpowers/specs/2026-05-13-bin-cctally-split-design.md
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import os
import pathlib
import re
import socket
import subprocess
import sys
import urllib.error
import urllib.request


def _cctally():
    """Resolve the current `cctally` module at call-time (spec §5.5)."""
    return sys.modules["cctally"]


# =========================================================================
# Exception classes
# =========================================================================

class RefreshUsageNetworkError(Exception):
    """Raised when the OAuth usage API can't be reached or returns non-2xx."""


class RefreshUsageRateLimitError(RefreshUsageNetworkError):
    """Raised when the OAuth usage API returns HTTP 429.

    Subclass of RefreshUsageNetworkError for backward compatibility:
    callers that already except RefreshUsageNetworkError continue to
    catch this; specific handlers can except RefreshUsageRateLimitError
    first to branch on the rate-limit case.
    """


class RefreshUsageMalformedError(Exception):
    """Raised when the OAuth usage API response is unparseable or missing
    required seven_day fields (utilization or resets_at)."""


# =========================================================================
# _RefreshUsageResult + URL
# =========================================================================

@dataclasses.dataclass
class _RefreshUsageResult:
    """Outcome of a single _refresh_usage_inproc() invocation.

    status enum:
      ok              - fetch + record-usage succeeded.
      rate_limited    - Anthropic 429 (fallback=True; last-known data still valid).
      no_oauth_token  - _resolve_oauth_token returned None.
      fetch_failed    - RefreshUsageNetworkError (DNS, connection, non-429 HTTP error).
      parse_failed    - RefreshUsageMalformedError or seven_day field extraction raised.
      record_failed   - cmd_record_usage returned non-zero or raised.

    payload (success only): the dict normally passed to
    _serialize_refresh_usage_json / _render_refresh_usage_text by
    cmd_refresh_usage. None on non-ok statuses. Tests examining only
    status/fallback/reason can ignore this field.

    warnings (success only): non-fatal degradations that occurred during
    the fetch (e.g. unparseable five_hour fields silently dropped). The
    CLI command emits these via eprint; structured callers may surface
    them however they like. Empty list on no warnings.
    """
    status: str
    fallback: bool = False
    reason: "str | None" = None
    payload: "dict | None" = None
    warnings: list = dataclasses.field(default_factory=list)


_OAUTH_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"


# =========================================================================
# Claude Code version discovery (User-Agent surface)
# =========================================================================

# Strict semver match with optional prerelease. Three numeric parts;
# 4-part form like "2.1.116.4" is intentionally rejected (the spec
# requires a valid `claude-code/<semver>` UA only).
_CC_SEMVER_RE = re.compile(
    r"(?<!\d)(?<!\d\.)(\d+\.\d+\.\d+(?:-[A-Za-z0-9.]+)?)(?!\.?\d)"
)


def _parse_cc_semver(s) -> str | None:
    """Extract the first valid `MAJOR.MINOR.PATCH(-prerelease)?` token from
    `s`. Rejects 4-part forms (e.g. "2.1.116.4") and non-numeric prefixes.
    Returns None if no match.
    """
    if not isinstance(s, str) or not s:
        return None
    m = _CC_SEMVER_RE.search(s)
    return m.group(1) if m else None


CLAUDE_CODE_UA_FALLBACK_VERSION = "2.1.116"


def _discover_cc_version() -> str:
    """Discover the active Claude Code version for our `claude-code/<X>` UA.

    Order: `claude --version` (5s timeout) → highest semver under
    `~/.local/share/claude/versions/` → CLAUDE_CODE_UA_FALLBACK_VERSION.
    Never raises.
    """
    # Tier 1: active executable.
    try:
        proc = subprocess.run(
            ["claude", "--version"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        v = _parse_cc_semver(proc.stdout) if proc.returncode == 0 else None
        if v:
            return v
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass

    # Tier 2: versions/ directory.
    try:
        versions_dir = pathlib.Path.home() / ".local" / "share" / "claude" / "versions"
        if versions_dir.is_dir():
            candidates = []
            for entry in versions_dir.iterdir():
                if not entry.is_dir():
                    continue
                v = _parse_cc_semver(entry.name)
                if v:
                    candidates.append(v)
            if candidates:
                # Sort by tuple-of-int parts; prerelease tuples sort lower.
                def _key(s):
                    base, _, pre = s.partition("-")
                    base_parts = tuple(int(x) for x in base.split("."))
                    # Prerelease present -> sorts lower than no-prerelease for same base.
                    return (base_parts, 0 if pre else 1, pre)
                candidates.sort(key=_key)
                return candidates[-1]
    except OSError:
        pass

    return CLAUDE_CODE_UA_FALLBACK_VERSION


def _resolve_oauth_usage_user_agent(
    oauth_usage_cfg: dict,
    *,
    version_resolver=None,
) -> str:
    """Return the User-Agent string for /api/oauth/usage requests.

    Honors `oauth_usage_cfg["user_agent"]` override; otherwise builds
    `claude-code/<version>` via `version_resolver` (injectable for tests).
    """
    if version_resolver is None:
        # §5.6 Option C: route through cctally's namespace so
        # `monkeypatch.setitem(ns, "_discover_cc_version", …)` propagates
        # into this caller (tests/test_refresh_usage_helpers.py).
        version_resolver = _cctally()._discover_cc_version
    override = oauth_usage_cfg.get("user_agent")
    if override:
        return override
    return f"claude-code/{version_resolver()}"


# =========================================================================
# Core OAuth fetch
# =========================================================================

def _fetch_oauth_usage(token: str, timeout_seconds: float) -> dict:
    """GET the OAuth usage API and return the parsed JSON object.

    Raises ``RefreshUsageNetworkError`` for any network-layer failure
    (connection, DNS, timeout, non-2xx HTTP). Raises
    ``RefreshUsageMalformedError`` when the response body is not JSON
    or is missing the required ``seven_day.utilization`` or
    ``seven_day.resets_at`` fields.
    """
    c = _cctally()
    cfg = c._get_oauth_usage_config(c.load_config())
    user_agent = _resolve_oauth_usage_user_agent(cfg)
    req = urllib.request.Request(
        _OAUTH_USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "Content-Type": "application/json",
            "User-Agent": user_agent,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        snippet = ""
        try:
            snippet = (e.read() or b"").decode("utf-8", errors="replace")[:200]
        except Exception:
            pass
        msg = f"HTTP {e.code} {e.reason}" + (f": {snippet}" if snippet else "")
        if e.code == 429:
            raise RefreshUsageRateLimitError(msg) from e
        raise RefreshUsageNetworkError(msg) from e
    except urllib.error.URLError as e:
        raise RefreshUsageNetworkError(f"URLError: {e.reason}") from e
    except socket.timeout as e:
        raise RefreshUsageNetworkError(
            f"timed out after {timeout_seconds}s"
        ) from e
    except OSError as e:
        raise RefreshUsageNetworkError(f"OSError: {e}") from e

    try:
        data = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise RefreshUsageMalformedError("response was not JSON") from e

    seven = data.get("seven_day") if isinstance(data, dict) else None
    if not isinstance(seven, dict) or "utilization" not in seven:
        raise RefreshUsageMalformedError("response missing seven_day.utilization")
    resets_at = seven.get("resets_at")
    if not isinstance(resets_at, str) or not resets_at.strip():
        raise RefreshUsageMalformedError("response missing seven_day.resets_at")

    return data


# =========================================================================
# Renderers (text + JSON)
# =========================================================================

# ANSI codes for refresh-usage's compact one-liner. Mirrors the
# yellow/orange/dim palette used by ~/.claude/statusline-command.sh.
_REFRESH_USAGE_ANSI = {
    "yellow": "\033[33m",
    "orange": "\033[38;5;208m",
    "dim": "\033[2m",
    "reset": "\033[0m",
}


def _render_refresh_usage_text(payload: dict, color: bool, now_epoch: int) -> str:
    """Render the compact one-liner.

    Format: ``refresh-usage: 7d N% (in Xd Yh) | 5h N% (in Zh)  [src:S cache:C]``.
    The ``5h`` segment is omitted entirely when ``payload["five_hour"]`` is None.
    Color codes are emitted only when ``color`` is True.
    """
    c = _cctally()
    a = _REFRESH_USAGE_ANSI if color else {k: "" for k in _REFRESH_USAGE_ANSI}

    seven = payload["seven_day"]
    seven_pct = seven["used_percent"]
    seven_resets = seven.get("resets_at_epoch")
    if seven_resets is not None:
        seven_ttl = c._format_short_duration(seven_resets - now_epoch)
        seven_seg = (
            f"{a['yellow']}7d {seven_pct:.0f}%{a['reset']}"
            f" {a['orange']}(in {seven_ttl}){a['reset']}"
        )
    else:
        seven_seg = f"{a['yellow']}7d {seven_pct:.0f}%{a['reset']}"

    five = payload.get("five_hour")
    if five is not None:
        five_pct = five["used_percent"]
        five_resets = five.get("resets_at_epoch")
        if five_resets is not None:
            five_ttl = c._format_short_duration(five_resets - now_epoch)
            five_seg = (
                f" | {a['yellow']}5h {five_pct:.0f}%{a['reset']}"
                f" {a['orange']}(in {five_ttl}){a['reset']}"
            )
        else:
            five_seg = f" | {a['yellow']}5h {five_pct:.0f}%{a['reset']}"
    else:
        five_seg = ""

    tag = (
        f"  {a['dim']}[src:{payload['source']} "
        f"cache:{payload['statusline_cache']}]{a['reset']}"
    )

    return f"{a['dim']}refresh-usage:{a['reset']} {seven_seg}{five_seg}{tag}"


def _serialize_refresh_usage_json(payload: dict) -> str:
    """Serialize the JSON-mode payload deterministically (sorted keys, 2-space indent)."""
    return json.dumps(payload, indent=2, sort_keys=True)


# =========================================================================
# OAuth-usage config block (validator + defaults)
# =========================================================================

class OauthUsageConfigError(ValueError):
    """Raised by _get_oauth_usage_config on invalid oauth_usage block."""


_OAUTH_USAGE_DEFAULTS = {
    "user_agent": None,
    "throttle_seconds": 15,
    "fresh_threshold_seconds": 30,
    "stale_after_seconds": 90,
}
_OAUTH_USAGE_THROTTLE_MIN = 5
_OAUTH_USAGE_THROTTLE_MAX = 600
_OAUTH_USAGE_USER_AGENT_MAX_LEN = 256


def _get_oauth_usage_config(cfg: dict) -> dict:
    """Return the validated, defaults-filled oauth_usage block.

    Raises OauthUsageConfigError on invalid values. Unknown sub-keys are
    silently ignored to preserve forward compatibility.
    """
    block = cfg.get("oauth_usage") if isinstance(cfg, dict) else None
    if block is None:
        return dict(_OAUTH_USAGE_DEFAULTS)
    if not isinstance(block, dict):
        raise OauthUsageConfigError(
            f"oauth_usage must be an object, got {type(block).__name__}"
        )

    out = dict(_OAUTH_USAGE_DEFAULTS)

    if "user_agent" in block and block["user_agent"] is not None:
        ua = block["user_agent"]
        if not isinstance(ua, str) or not ua:
            raise OauthUsageConfigError(
                "oauth_usage.user_agent must be a non-empty string or null"
            )
        if len(ua) > _OAUTH_USAGE_USER_AGENT_MAX_LEN:
            raise OauthUsageConfigError(
                f"oauth_usage.user_agent exceeds {_OAUTH_USAGE_USER_AGENT_MAX_LEN} chars"
            )
        out["user_agent"] = ua

    for key in ("throttle_seconds", "fresh_threshold_seconds", "stale_after_seconds"):
        if key in block and block[key] is not None:
            v = block[key]
            if not isinstance(v, int) or isinstance(v, bool):
                raise OauthUsageConfigError(
                    f"oauth_usage.{key} must be an integer"
                )
            if v < 1:
                raise OauthUsageConfigError(
                    f"oauth_usage.{key} must be >= 1"
                )
            out[key] = v

    t = out["throttle_seconds"]
    if t < _OAUTH_USAGE_THROTTLE_MIN or t > _OAUTH_USAGE_THROTTLE_MAX:
        raise OauthUsageConfigError(
            f"oauth_usage.throttle_seconds must be in [{_OAUTH_USAGE_THROTTLE_MIN}, "
            f"{_OAUTH_USAGE_THROTTLE_MAX}], got {t}"
        )
    if out["fresh_threshold_seconds"] >= out["stale_after_seconds"]:
        raise OauthUsageConfigError(
            "oauth_usage.fresh_threshold_seconds must be < stale_after_seconds"
        )

    return out


# =========================================================================
# Statusline cache bust + freshness + rate-limit handler
# =========================================================================

_STATUSLINE_OAUTH_CACHE = "/tmp/claude-statusline-usage-cache.json"


def _bust_statusline_cache(path: str = _STATUSLINE_OAUTH_CACHE) -> str:
    """Best-effort delete of the statusline OAuth cache file.

    Returns one of: ``"busted"`` (file existed and was removed),
    ``"absent"`` (file did not exist), ``"error"`` (delete failed for
    a non-FileNotFoundError reason — logged via eprint, does NOT raise).
    """
    c = _cctally()
    try:
        os.remove(path)
        return "busted"
    except FileNotFoundError:
        return "absent"
    except OSError as exc:
        c.eprint(f"refresh-usage: cache-bust failed: {exc}")
        return "error"


def _freshness_label(age_seconds: float, oauth_usage_cfg: dict) -> str:
    """Stub — replaced in Task C1 with full three-tier logic."""
    if age_seconds <= oauth_usage_cfg["fresh_threshold_seconds"]:
        return "fresh"
    if age_seconds <= oauth_usage_cfg["stale_after_seconds"]:
        return "aging"
    return "stale"


def _cmd_refresh_usage_handle_rate_limit(args: argparse.Namespace, exc) -> int:
    """Implements the §3.2(c) fallback contract: serve last-known
    snapshot from DB, exit 0 in all 429 cases."""
    c = _cctally()
    snap = c._select_last_known_snapshot()
    cfg = _get_oauth_usage_config(c.load_config())
    json_mode = bool(getattr(args, "json", False))
    quiet = bool(getattr(args, "quiet", False))

    if snap is None:
        c.eprint("refresh-usage: rate-limited; no last-known data; "
                 "status-line will populate on next CC tick")
        if json_mode:
            print(json.dumps({
                "status": "rate_limited",
                "fallback": None,
                "freshness": None,
                "reason": "no prior snapshot",
            }, sort_keys=True, indent=2))
        return 0

    captured_iso = snap.pop("captured_at_utc", None)
    age_s = c._seconds_since_iso(captured_iso) if captured_iso else None
    label = (
        _freshness_label(age_s, cfg) if age_s is not None else "stale"
    )

    c.eprint(f"refresh-usage: rate-limited; using last-known "
             f"(captured {int(age_s) if age_s is not None else '?'}s ago)")

    if json_mode:
        envelope = {
            "status": "rate_limited",
            "fallback": snap,
            "freshness": {
                "label": label,
                "captured_at": captured_iso,
                "age_seconds": int(age_s) if age_s is not None else None,
            },
            "reason": "user-agent rate-limit gate",
        }
        print(json.dumps(envelope, sort_keys=True, indent=2))
    elif not quiet:
        # Reuse the standard text renderer with the fallback payload.
        color_mode = getattr(args, "color", "auto") or "auto"
        color = c._forecast_color_enabled(color_mode, sys.stdout)
        now_epoch = int(dt.datetime.now(dt.timezone.utc).timestamp())
        print(_render_refresh_usage_text(snap, color=color, now_epoch=now_epoch))
    return 0


# =========================================================================
# In-process refresh + cmd_refresh_usage
# =========================================================================

def _refresh_usage_inproc(timeout_seconds: float = 5.0) -> _RefreshUsageResult:
    """Force-fetch the OAuth usage API and persist via cmd_record_usage.

    This is the in-process counterpart to ``cmd_refresh_usage`` (force-fetch
    semantics: NO local throttle, busts statusline cache on success). NOT the
    same path as ``_hook_tick_oauth_refresh`` - that one honors
    ``oauth_usage.throttle_seconds`` and would silently skip an explicit user
    chip-click within the throttle window.

    Returns a structured ``_RefreshUsageResult`` so callers (chiefly
    ``cmd_refresh_usage`` for stdout printing and the dashboard's
    ``POST /api/sync`` handler for JSON envelope construction) can branch on
    a 6-value status enum without parsing free-form stderr.

    The harness env var ``CCTALLY_TEST_REFRESH_RESULT`` short-circuits to a
    deterministic outcome (``ok``/``rate_limited``/``no_oauth_token``/
    ``fetch_failed``/``parse_failed``/``record_failed``) so bash-level golden
    harnesses can exercise warning paths without faking OAuth on the wire.
    """
    c = _cctally()
    forced = os.environ.get("CCTALLY_TEST_REFRESH_RESULT")
    if forced:
        return _RefreshUsageResult(
            status=forced,
            fallback=(forced == "rate_limited"),
            reason="test stub",
        )

    token = c._resolve_oauth_token()
    if not token:
        return _RefreshUsageResult(status="no_oauth_token", reason="no token")

    try:
        api = c._fetch_oauth_usage(token=token, timeout_seconds=timeout_seconds)
    except RefreshUsageRateLimitError as exc:
        return _RefreshUsageResult(status="rate_limited", fallback=True,
                                    reason=str(exc))
    except RefreshUsageNetworkError as exc:
        return _RefreshUsageResult(status="fetch_failed", reason=str(exc))
    except RefreshUsageMalformedError as exc:
        return _RefreshUsageResult(status="parse_failed", reason=str(exc))
    except OauthUsageConfigError as exc:
        return _RefreshUsageResult(
            status="fetch_failed",
            reason=f"invalid oauth_usage config: {exc}",
        )

    seven = api.get("seven_day") or {}
    try:
        # Normalize at the OAuth ingress so the payload JSON published
        # on the SSE envelope (`used_percent` field) is clean even when
        # this code path doesn't reach cmd_record_usage (e.g. payload
        # built then a downstream cmd_record_usage call fails).
        seven_pct = c._normalize_percent(float(seven["utilization"]))
        seven_resets_iso = seven["resets_at"]
        seven_resets_epoch = c._iso_to_epoch(seven_resets_iso)
    except (TypeError, ValueError, KeyError) as exc:
        return _RefreshUsageResult(
            status="parse_failed",
            reason=f"OAuth response had unparseable seven_day fields: {exc}",
        )

    five = api.get("five_hour") if isinstance(api.get("five_hour"), dict) else None
    five_pct = None
    five_resets_iso = None
    five_resets_epoch = None
    warnings: list = []
    if five is not None and "utilization" in five and "resets_at" in five:
        try:
            five_pct = c._normalize_percent(float(five["utilization"]))
            five_resets_iso = five["resets_at"]
            five_resets_epoch = c._iso_to_epoch(five_resets_iso)
        except (TypeError, ValueError) as exc:
            # 5h is optional - silently degrade rather than fail the command
            # (parity with the previous cmd_refresh_usage behavior; the eprint
            # warning is emitted by cmd_refresh_usage when consuming
            # result.warnings, so /api/sync callers don't get stderr noise).
            five_pct = None
            five_resets_iso = None
            five_resets_epoch = None
            warnings.append(
                f"ignoring unparseable five_hour fields: {exc}"
            )

    record_args = argparse.Namespace(
        percent=seven_pct,
        resets_at=str(seven_resets_epoch),
        five_hour_percent=five_pct,
        five_hour_resets_at=(
            str(five_resets_epoch) if five_resets_epoch is not None else None
        ),
    )
    try:
        rc = c.cmd_record_usage(record_args)
    except Exception as exc:
        return _RefreshUsageResult(status="record_failed", reason=str(exc))
    if rc != 0:
        return _RefreshUsageResult(status="record_failed", reason=f"exit {rc}")

    # §5.6 Option C: route through cctally's namespace so
    # `monkeypatch.setitem(ns, "_bust_statusline_cache", …)` propagates
    # (tests/test_refresh_usage_cmd.py:55, test_refresh_usage_inproc.py:18).
    cache_state = c._bust_statusline_cache()

    fetched_at = c.now_utc_iso()
    fresh_envelope = {
        "label": "fresh",
        "captured_at": fetched_at,
        "age_seconds": 0,
    }
    payload = {
        "schema_version": 1,
        "fetched_at": fetched_at,
        "seven_day": {
            "used_percent": seven_pct,
            "resets_at": seven_resets_iso,
            "resets_at_epoch": seven_resets_epoch,
        },
        "five_hour": (
            {
                "used_percent": five_pct,
                "resets_at": five_resets_iso,
                "resets_at_epoch": five_resets_epoch,
            }
            if five_pct is not None
            else None
        ),
        "freshness": fresh_envelope,
        "source": "api",
        "statusline_cache": cache_state,
    }
    return _RefreshUsageResult(status="ok", payload=payload, warnings=warnings)


def cmd_refresh_usage(args: argparse.Namespace) -> int:
    """Force-fetch the OAuth usage API and persist via cmd_record_usage.

    Returns: 0 success OR rate-limited (graceful fallback), 2 token missing,
    3 network/HTTP non-429 failure, 4 malformed response, 5 cmd_record_usage
    internal failure.

    Thin shell over ``_refresh_usage_inproc``: dispatches the structured
    result to the appropriate stderr/stdout renderer while preserving every
    pre-refactor exit code and user-facing message.
    """
    c = _cctally()
    timeout = float(getattr(args, "timeout", 5.0) or 5.0)
    # §5.6 Option C: route through cctally's namespace so
    # `monkeypatch.setitem(ns, "_refresh_usage_inproc", _spy)` propagates
    # (tests/test_dashboard_api_sync_refresh.py:199).
    result = c._refresh_usage_inproc(timeout_seconds=timeout)

    if result.status == "ok":
        # Surface non-fatal degradations (e.g. dropped five_hour fields).
        # Emit BEFORE rendering so stderr flushes consistently for harnesses
        # that grep across both streams.
        for warning in result.warnings:
            c.eprint(f"refresh-usage: {warning}")
        payload = result.payload or {}
        if getattr(args, "json", False):
            print(_serialize_refresh_usage_json(payload))
        elif not getattr(args, "quiet", False):
            color_mode = getattr(args, "color", "auto") or "auto"
            color = c._forecast_color_enabled(color_mode, sys.stdout)
            now_epoch = int(dt.datetime.now(dt.timezone.utc).timestamp())
            print(_render_refresh_usage_text(payload, color=color, now_epoch=now_epoch))
        return 0

    if result.status == "rate_limited":
        return _cmd_refresh_usage_handle_rate_limit(
            args, RefreshUsageRateLimitError(result.reason or "rate limited"))

    if result.status == "no_oauth_token":
        c.eprint("refresh-usage: no OAuth token found "
                 "(run 'claude' once to authenticate)")
        return 2

    if result.status == "fetch_failed":
        # Distinguish the OauthUsageConfigError sub-case (returns 2) from
        # genuine network/HTTP failures (returns 3). The helper carries the
        # config-error reason string verbatim so we can detect it here.
        reason = result.reason or ""
        if reason.startswith("invalid oauth_usage config:"):
            c.eprint(f"cctally: {reason}")
            return 2
        c.eprint(f"refresh-usage: OAuth fetch failed: {reason}")
        return 3

    if result.status == "parse_failed":
        # Five-hour parse failures are silently degraded inside the helper;
        # only seven_day failures (RefreshUsageMalformedError or unparseable
        # field extraction) propagate here. Preserve the original exact
        # error-line shape for bash harnesses that grep stderr.
        c.eprint(f"refresh-usage: {result.reason or ''}")
        return 4

    if result.status == "record_failed":
        reason = result.reason or ""
        if reason.startswith("exit "):
            try:
                rc = int(reason.split()[1])
            except (IndexError, ValueError):
                rc = -1
            c.eprint(f"refresh-usage: failed to record usage (exit {rc})")
        else:
            c.eprint(f"refresh-usage: failed to record usage: {reason}")
        return 5

    # Defensive: unknown status from _refresh_usage_inproc -> treat as parse error.
    c.eprint(f"refresh-usage: unexpected status {result.status!r}")
    return 4


# =========================================================================
# Hook-tick OAuth refresh path
# =========================================================================

def _hook_tick_oauth_refresh(
    timeout_seconds: float = 5.0,
    throttle_seconds: float | None = None,
) -> tuple[str, dict | None]:
    """Run the same OAuth fetch + record-usage path as cmd_refresh_usage,
    BUT do NOT call _bust_statusline_cache().

    `throttle_seconds` controls the DB-snapshot freshness gate (skip the
    fetch if the newest weekly_usage_snapshots row is younger than this).
    `None` => read `oauth_usage.throttle_seconds` from config (falling back
    to HOOK_TICK_DEFAULT_THROTTLE_SECONDS on validation error). An explicit
    value bypasses config so `cmd_hook_tick`'s already-resolved
    `--throttle-seconds` override (including the `0` escape hatch) reaches
    this gate end-to-end.

    Returns (status_str, payload_or_none) where status_str is one of:
        "ok(7d=N,5h=M)"  | "ok(7d=N)"
        "skipped-no-token" | "skipped(fresh:Ns)"
        "err(network)"   | "err(parse)" | "err(record-usage=K)"
    payload_or_none is the raw OAuth-API response dict (`seven_day` /
    `five_hour`) on success, or None on any non-ok branch.
    """
    c = _cctally()
    token = c._resolve_oauth_token()
    if not token:
        return "skipped-no-token", None
    if throttle_seconds is None:
        try:
            throttle_seconds = float(_get_oauth_usage_config(c.load_config())["throttle_seconds"])
        except OauthUsageConfigError:
            throttle_seconds = float(c.HOOK_TICK_DEFAULT_THROTTLE_SECONDS)
    age_s = c._newest_snapshot_age_seconds()
    if age_s is not None and age_s < throttle_seconds:
        return f"skipped(fresh:{int(age_s)}s)", None
    try:
        api = c._fetch_oauth_usage(token=token, timeout_seconds=timeout_seconds)
    except RefreshUsageRateLimitError:
        return "err(rate-limit)", None
    except RefreshUsageNetworkError:
        return "err(network)", None
    except RefreshUsageMalformedError:
        return "err(parse)", None
    seven = api["seven_day"]
    try:
        seven_pct = c._normalize_percent(float(seven["utilization"]))
        seven_resets_epoch = c._iso_to_epoch(seven["resets_at"])
    except (TypeError, ValueError, KeyError):
        return "err(parse)", None
    five = api.get("five_hour") if isinstance(api.get("five_hour"), dict) else None
    five_pct: float | None = None
    five_resets_epoch: int | None = None
    if five is not None and "utilization" in five and "resets_at" in five:
        try:
            five_pct = c._normalize_percent(float(five["utilization"]))
            five_resets_epoch = c._iso_to_epoch(five["resets_at"])
        except (TypeError, ValueError):
            five_pct = None
            five_resets_epoch = None
    record_args = argparse.Namespace(
        percent=seven_pct,
        resets_at=str(seven_resets_epoch),
        five_hour_percent=five_pct,
        five_hour_resets_at=str(five_resets_epoch) if five_resets_epoch is not None else None,
    )
    try:
        rc = c.cmd_record_usage(record_args)
    except Exception:
        return "err(record-usage=exc)", None
    if rc != 0:
        return f"err(record-usage={rc})", None
    parts = [f"7d={int(round(seven_pct))}"]
    if five_pct is not None:
        parts.append(f"5h={int(round(five_pct))}")
    return f"ok({','.join(parts)})", api


def _hook_tick_make_mock_refresh(payload_str: str):
    """Return a stand-in for `_hook_tick_oauth_refresh` driven by a fixed
    JSON payload (or sentinel string). Used only by the test harness via
    the hidden `--mock-oauth-response` flag.
    """
    def _mock(timeout_seconds: float = 5.0, throttle_seconds: float | None = None):
        if payload_str == "NETWORK_ERROR":
            return "err(network)", None
        if payload_str == "NO_TOKEN":
            return "skipped-no-token", None
        try:
            data = json.loads(payload_str)
        except ValueError:
            return "err(parse)", None
        seven = data.get("seven_day", {})
        five = data.get("five_hour")
        seven_pct = float(seven.get("utilization", 0))
        parts = [f"7d={int(round(seven_pct))}"]
        if isinstance(five, dict) and "utilization" in five:
            parts.append(f"5h={int(round(float(five['utilization'])))}")
        return f"ok({','.join(parts)})", data
    return _mock
