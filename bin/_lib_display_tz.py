"""Display-timezone primitives — the datetime render chokepoint.

Pure-fn layer (no I/O at import time): holds every helper that resolves a
display tz from CLI args / config, localizes a datetime through that
resolution, and formats it for human display. `format_display_dt` is the
chokepoint per CLAUDE.md — all human-displayed datetimes route through it.

`bin/cctally` re-exports every symbol below so internal call sites resolve
unchanged. Future pure layers (alerts payload, render, aggregators) import
the chokepoint from here directly via `_load_sibling("_lib_display_tz")`,
so they stay pure without back-importing `cctally`.

Module-level flags `_DISPLAY_TZ_BAD_CONFIG_WARNED` and
`_DISPLAY_TZ_RESOLVE_WARNED` move with their owning functions; the
`global` declarations work fine in the new module. A private `_eprint`
duplicates `bin/cctally:eprint` (two-line stderr helper) so this pure
layer carries zero back-imports per the split design's §5.3 contract.

Spec: docs/superpowers/specs/2026-05-13-bin-cctally-split-design.md
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from typing import Any

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _eprint(*args: Any) -> None:
    print(*args, file=sys.stderr)


def _local_tz_name() -> str:
    """Return an IANA timezone name if resolvable, else a best-effort fallback.

    Used in Codex command title banners ("(Timezone: <name>)"). Upstream
    uses IANA names (e.g. "Asia/Jerusalem"); we mirror when possible.
    """
    # Preferred: TZ env var if IANA-looking ("/" in the value).
    tz = os.environ.get("TZ", "")
    if tz and "/" in tz:
        return tz
    try:
        tz_path = os.readlink("/etc/localtime")
        if "zoneinfo/" in tz_path:
            return tz_path.split("zoneinfo/", 1)[1]
    except (OSError, ValueError):
        pass
    # Fallback: time.tzname[0] (e.g. "IST"); may be non-IANA.
    try:
        import time as _time
        return _time.tzname[0] or ""
    except Exception:
        return ""


def _resolve_tz(tz_name: str | None, *, strict_iana: bool = False, fallback: Any = None) -> Any:
    """Return ZoneInfo(tz_name) or ``fallback``. Callers have already validated
    the tz via _parse_cli_date_range; this is a defensive re-resolve that
    falls back to ``fallback`` on any error so aggregators never crash.

    With ``strict_iana=True``, names lacking a "/" (e.g. bare "UTC") are
    rejected up-front and ``fallback`` is returned instead — mirrors
    ``_local_tz_name``'s gating to avoid the bare-UTC gotcha (see CLAUDE.md).
    Default behavior (strict_iana=False, fallback=None) preserves the prior
    contract for existing callers.
    """
    if not tz_name:
        return fallback
    if strict_iana and "/" not in tz_name:
        return fallback
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return fallback


DISPLAY_TZ_DEFAULT = "local"
_DISPLAY_TZ_BAD_CONFIG_WARNED = False  # one-shot warning flag


def normalize_display_tz_value(raw: "str | None") -> str:
    """Canonicalize a display-tz value to "local" | "utc" | <IANA>.

    - None / "" / case-insensitive "local" -> "local"
    - case-insensitive "utc" -> "utc"
    - anything else: must be a valid IANA name (validated via _resolve_tz
      with strict_iana=True). Returns the trimmed value verbatim.
    Raises ValueError on invalid input.
    """
    if raw is None:
        return "local"
    s = str(raw).strip()
    if not s:
        return "local"
    low = s.lower()
    if low == "local":
        return "local"
    if low == "utc":
        return "utc"
    sentinel = object()
    if _resolve_tz(s, strict_iana=True, fallback=sentinel) is sentinel:
        raise ValueError(f"invalid IANA zone: {s!r}")
    return s


def _config_has_explicit_display_tz(config: "dict | None") -> bool:
    """Return True iff `config["display"]["tz"]` is present and non-None.

    Used by codex command tz-name resolution (F2): we need to distinguish
    "user pinned a display tz" from "default 'local' fallback because no
    config block is set" so upstream's `--timezone` can still apply in the
    drop-in-parity case where neither --tz nor display.tz was specified.
    """
    if not isinstance(config, dict):
        return False
    block = config.get("display")
    return isinstance(block, dict) and block.get("tz") is not None


def get_display_tz_pref(config: "dict | None") -> str:
    """Read config['display']['tz']; default DISPLAY_TZ_DEFAULT.

    Malformed value -> fall back to default with one-shot stderr warning.
    Never raises.
    """
    global _DISPLAY_TZ_BAD_CONFIG_WARNED
    if not isinstance(config, dict):
        return DISPLAY_TZ_DEFAULT
    block = config.get("display")
    if not isinstance(block, dict):
        return DISPLAY_TZ_DEFAULT
    raw = block.get("tz")
    if raw is None:
        return DISPLAY_TZ_DEFAULT
    try:
        return normalize_display_tz_value(raw)
    except ValueError:
        if not _DISPLAY_TZ_BAD_CONFIG_WARNED:
            _eprint(
                f"warning: ignoring malformed display.tz {raw!r} in config; "
                f"using {DISPLAY_TZ_DEFAULT!r}"
            )
            _DISPLAY_TZ_BAD_CONFIG_WARNED = True
        return DISPLAY_TZ_DEFAULT


def resolve_display_tz(args: argparse.Namespace,
                       config: "dict | None") -> "ZoneInfo | None":
    """Returns ZoneInfo for fixed zones, None for "local" (caller does
    bare astimezone()). Precedence: --tz flag > config > default.
    Invalid --tz values are normalized via normalize_display_tz_value
    (which raises ValueError -- caller is responsible for catching at
    argparse-time via the shared _argparse_tz type=callable).
    """
    flag = getattr(args, "tz", None)
    if flag is not None and str(flag).strip() != "":
        canonical = normalize_display_tz_value(flag)  # may raise
    else:
        canonical = get_display_tz_pref(config)
    if canonical == "local":
        return None
    if canonical == "utc":
        return ZoneInfo("Etc/UTC")
    return ZoneInfo(canonical)


def display_tz_label(localized: dt.datetime) -> str:
    """Returns the suffix label for an already-localized datetime.

    Prefer ``localized.tzname()`` if alphanumeric and len <= 5; otherwise
    fall back to a numeric offset via ``%z`` trimmed to "+HH" or "+HHMM".
    Works uniformly across local / utc / explicit IANA -- caller has
    already done astimezone() before passing in.
    """
    if localized.tzinfo is None:
        # Defensive: shouldn't happen because callers localize first
        localized = localized.replace(tzinfo=dt.timezone.utc)
    name = localized.tzname() or ""
    if (name
            and name.replace("+", "").replace("-", "").isalnum()
            and len(name) <= 5
            and any(c.isalpha() for c in name)):
        return name
    # Numeric fallback. %z gives "+HHMM"; trim trailing "00" -> "+HH".
    raw = localized.strftime("%z")
    if not raw:
        return "UTC"
    if raw.endswith("00") and len(raw) == 5:
        return raw[:-2]   # "+0500" -> "+05"
    # Insert colon for readability when minutes != 00: "+0530" -> "+05:30"
    if len(raw) == 5:
        return f"{raw[:3]}:{raw[3:]}"
    return raw


def _localize(d: dt.datetime, tz: "ZoneInfo | None") -> dt.datetime:
    """Localize a tz-aware datetime through the resolved display tz.

    Mirrors ``format_display_dt``'s tz handling for callers that need a
    localized ``dt.datetime`` (not a formatted string) — e.g. when the
    same instant is fed to multiple ``strftime`` formats. ``tz=None``
    falls back to host local via bare ``astimezone()``.
    """
    return d.astimezone(tz) if tz is not None else d.astimezone()


_DISPLAY_TZ_RESOLVE_WARNED = False  # one-shot host-zone-fallback warning


def _resolve_display_tz_obj(config: dict) -> ZoneInfo:
    """Resolve config.display.tz to a concrete ZoneInfo object.

    Single source of truth used by ``_compute_display_block`` and the
    dashboard snapshot builders (``_tui_build_snapshot``,
    ``_handle_get_block_detail``). Local-fallback case emits a one-shot
    warning via ``_DISPLAY_TZ_RESOLVE_WARNED``. Returns a ZoneInfo
    (never None).
    """
    global _DISPLAY_TZ_RESOLVE_WARNED
    pref = get_display_tz_pref(config)
    if pref == "local":
        host_iana = _local_tz_name()
        if host_iana and "/" in host_iana:
            return ZoneInfo(host_iana)
        if not _DISPLAY_TZ_RESOLVE_WARNED:
            _eprint(
                "warning: display.tz='local' but host IANA zone could "
                "not be resolved; using Etc/UTC. Set TZ to an IANA "
                "name (e.g. America/New_York) to fix."
            )
            _DISPLAY_TZ_RESOLVE_WARNED = True
        return ZoneInfo("Etc/UTC")
    if pref == "utc":
        return ZoneInfo("Etc/UTC")
    try:
        return ZoneInfo(pref)
    except Exception:
        # Defense in depth -- pref was already canonicalized via
        # normalize_display_tz_value, but if a stale config slipped
        # through, fall back rather than crash.
        if not _DISPLAY_TZ_RESOLVE_WARNED:
            _eprint(
                f"warning: display.tz={pref!r} could not be loaded as "
                f"a ZoneInfo; using Etc/UTC"
            )
            _DISPLAY_TZ_RESOLVE_WARNED = True
        return ZoneInfo("Etc/UTC")


def _apply_display_tz_override(
    config: dict,
    override: "str | None",
) -> dict:
    """Return a shallow-copied config with `display.tz` substituted from
    the override (F3 fix).

    `--tz` on `cctally dashboard` should win over the persisted
    `config.display.tz` for the lifetime of the server. Plumbing the
    canonicalized string here lets every reader that already calls
    `load_config()` (envelope builder, snapshot builder, block-detail
    handler) pick up the override by routing the loaded config through
    this thin wrapper -- without changing their existing
    `_resolve_display_tz_obj(load_config())` shape.

    Override semantics: a canonical token from
    ``normalize_display_tz_value`` (``"local"`` / ``"utc"`` / IANA
    name). ``None`` means "no override; let config win" -- the input
    config is returned unchanged. Always returns a fresh dict so callers
    can't mutate the passed-in config via the result.
    """
    if override is None:
        return config
    out = dict(config)
    out["display"] = dict(out.get("display") or {})
    out["display"]["tz"] = override
    out["display"]["pinned"] = True
    return out


def _compute_display_block(config: dict, generated_at: dt.datetime) -> dict:
    """Compute the dashboard envelope's ``display`` block.

    Resolves ``config.display.tz`` to a CONCRETE IANA name server-side
    (F1 fix per spec) so the browser never has to guess "local". Also
    computes the offset label and signed offset seconds at the snapshot
    timestamp, so the client can label times consistently with the
    server-rendered week_lbl / block labels.

    Resolution flows through ``_resolve_display_tz_obj`` -- the same
    helper that ``_tui_build_snapshot`` and ``_handle_get_block_detail``
    use, so all three sites share warn-once stderr semantics.
    """
    pref = get_display_tz_pref(config)
    resolved_obj = _resolve_display_tz_obj(config)
    resolved_iana = resolved_obj.key

    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=dt.timezone.utc)
    localized = generated_at.astimezone(resolved_obj)
    offset = localized.utcoffset()
    offset_seconds = int(offset.total_seconds()) if offset is not None else 0
    block: dict = {
        "tz":              pref,
        "resolved_tz":     resolved_iana,
        "offset_label":    display_tz_label(localized),
        "offset_seconds":  offset_seconds,
    }
    # F3: surface --tz-pin so the React client can render a read-only
    # state for the Settings UI when the operator launched the server
    # with an explicit --tz override. Override application leaves the
    # `pinned` key on `config["display"]`; we forward it as a runtime
    # signal (NOT persisted into config.json -- POST /api/settings is
    # blocked under pin in this mode).
    display_cfg = config.get("display") if isinstance(config, dict) else None
    if isinstance(display_cfg, dict) and display_cfg.get("pinned"):
        block["pinned"] = True
    return block


def format_display_dt(value: "str | dt.datetime",
                      tz: "ZoneInfo | None",
                      *, fmt: str, suffix: bool = True) -> str:
    """Targeted-swap chokepoint. Naive value treated as UTC.

    Output: "<strftime fmt> <suffix>" when suffix=True, else just the
    strftime. Suffix is computed from the localized datetime via
    display_tz_label.
    """
    if isinstance(value, str):
        s = value.replace("Z", "+00:00")
        parsed = dt.datetime.fromisoformat(s)
    else:
        parsed = value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    localized = parsed.astimezone(tz) if tz is not None else parsed.astimezone()
    body = localized.strftime(fmt)
    if not suffix:
        return body
    return f"{body} {display_tz_label(localized)}"


def _argparse_tz(value: str) -> str:
    """argparse ``type=`` callable for ``--tz`` on any subcommand.

    Canonicalizes via ``normalize_display_tz_value``: returns "local",
    "utc", or a verbatim IANA name. Bad input raises
    ``argparse.ArgumentTypeError`` so argparse formats a standard
    ``error: argument --tz: ...`` message and exits 2.
    """
    try:
        return normalize_display_tz_value(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid timezone {value!r} -- expected 'local', 'utc', "
            f"or an IANA name"
        )
