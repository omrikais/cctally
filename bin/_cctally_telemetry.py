"""Anonymous install-count telemetry kernel (see spec 2026-07-07).

Pure-ish: state resolution, token derivation, and payload construction are
side-effect-free; only the beat path mints an ``install_id`` / touches the
on-disk markers / makes a network call. Stdlib only.

Privacy posture (spec 2026-07-07):
  - No IP, no username, no path, no session content ever leaves the machine.
  - The only durable identifier is a random UUID ``install_id`` stored 0600
    under APP_DIR; it is NEVER transmitted. What we send is a per-month
    rotating token ``sha256(install_id:YYYY-MM:pepper)[:32]`` — one-way and
    unlinkable across months, so the server can de-dupe within a month
    without ever seeing the id.
  - Fully opt-out: ``CCTALLY_DISABLE_TELEMETRY``, the ``DO_NOT_TRACK``
    convention, dev checkouts, and a ``telemetry.enabled = false`` config
    key each disable it (see ``resolve_telemetry_state``).
  - Network errors are swallowed — telemetry never affects the user's UX.

Cross-module symbols (``_is_dev_checkout``, ``resolve_client_version``,
``resolve_os_family``, ``_release_read_latest_release_version``) are reached
through the call-time ``_cctally()`` accessor so tests' ``monkeypatch`` on the
``cctally`` module namespace propagates into these bodies. Path/constant
reads go through the ``_core()`` accessor so a redirected APP_DIR (tests,
dev-instance isolation) is honored.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import platform
import sys
import uuid
import urllib.request


def _cctally():
    """Resolve the current ``cctally`` module at call-time (spec §5.5)."""
    return sys.modules["cctally"]


def _core():
    import _cctally_core
    return _cctally_core


def _truthy_env(name: str) -> bool:
    """A ``1``/``true``/``yes``/any-non-empty env value is truthy; unset,
    empty, ``0``, ``false``, ``no`` are falsey."""
    v = os.environ.get(name)
    return v is not None and v.strip().lower() not in ("", "0", "false", "no")


def resolve_telemetry_state(config: dict) -> tuple[bool, str]:
    """Return ``(enabled, reason)`` — side-effect-free.

    Precedence (first match wins): env opt-out, DO_NOT_TRACK, dev checkout,
    config opt-out, else enabled. Never mints or touches any file.
    """
    c = _cctally()
    if _truthy_env("CCTALLY_DISABLE_TELEMETRY"):
        return (False, "env-disabled")
    if _truthy_env("DO_NOT_TRACK"):
        return (False, "do-not-track")
    if c._is_dev_checkout():
        return (False, "dev-checkout")
    tele = (config or {}).get("telemetry") or {}
    if tele.get("enabled") is False:
        return (False, "config-disabled")
    return (True, "enabled")


def current_period(now: _dt.datetime | None = None) -> str:
    """Current rotation period as ``"YYYY-MM"`` in UTC."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    return now.astimezone(_dt.timezone.utc).strftime("%Y-%m")


def telemetry_token(install_id: str, period: str) -> str:
    """One-way, month-rotating token: ``sha256(id:period:pepper)[:32]``.

    Deterministic within a period, unlinkable across periods, and never
    reveals ``install_id``."""
    pepper = _core().TELEMETRY_PEPPER
    raw = f"{install_id}:{period}:{pepper}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


def read_install_id() -> str | None:
    """Read the persisted install_id, or ``None`` if absent/empty. Never mints."""
    p = _core().TELEMETRY_INSTALL_ID_PATH
    try:
        return p.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def ensure_install_id() -> str:
    """Return the install_id, minting a random UUID at 0600 if missing."""
    p = _core().TELEMETRY_INSTALL_ID_PATH
    existing = read_install_id()
    if existing:
        return existing
    p.parent.mkdir(parents=True, exist_ok=True)
    iid = str(uuid.uuid4())
    # Write then chmod 0600 (match the cache-db sidecar-hardening posture).
    p.write_text(iid + "\n", encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return iid


def reset_install_id() -> str:
    """Discard any existing install_id and mint a fresh one."""
    p = _core().TELEMETRY_INSTALL_ID_PATH
    try:
        p.unlink()
    except OSError:
        pass
    return ensure_install_id()


def resolve_client_version() -> str:
    """The stamped client semver, or ``"unknown"`` when unstamped."""
    c = _cctally()
    cur = c._release_read_latest_release_version()
    return cur[0] if cur else "unknown"


def resolve_os_family() -> str:
    """Coarse OS family: ``macos`` | ``linux`` | ``windows`` | ``other``."""
    s = platform.system().lower()
    return {"darwin": "macos", "linux": "linux", "windows": "windows"}.get(s, "other")


def build_beat_payload(install_id: str, *, now: _dt.datetime | None = None) -> dict:
    """The minimal beat body: ``{t, v, os}`` — token, client version, OS family.

    Version/OS are read through the ``cctally`` accessor so a test's
    ``monkeypatch.setattr(cctally, "resolve_client_version", ...)`` drives the
    output (the re-export lives on the ``cctally`` module, not this kernel's
    namespace)."""
    c = _cctally()
    return {
        "t": telemetry_token(install_id, current_period(now)),
        "v": c.resolve_client_version(),
        "os": c.resolve_os_family(),
    }


def _marker_age_seconds(path, now: _dt.datetime | None = None) -> float | None:
    """Seconds since ``path``'s mtime, or ``None`` when the marker is absent."""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    now = now or _dt.datetime.now(_dt.timezone.utc)
    return now.timestamp() - mtime


def telemetry_beat_due(now=None) -> bool:
    """True when no beat has been *attempted* within the throttle window.

    The last-beat marker records the last beat ATTEMPT (not just the last
    successful send): ``do_telemetry_beat`` stamps it on every non-disabled
    run — arm, grace, sent, or failed — so this predicate bounds the parent
    spawn gate to at most one worker per window regardless of outcome (a
    network outage / undeployed endpoint can't churn re-spawns)."""
    age = _marker_age_seconds(_core().TELEMETRY_LAST_BEAT_PATH, now)
    return age is None or age >= _core().TELEMETRY_BEAT_THROTTLE_SECONDS


def first_beat_grace_elapsed(now=None) -> bool:
    """True once the first-seen marker is at least the grace window old."""
    age = _marker_age_seconds(_core().TELEMETRY_FIRST_SEEN_PATH, now)
    return age is not None and age >= _core().TELEMETRY_FIRST_BEAT_GRACE_SECONDS


def _touch(path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


def mark_first_seen(now=None) -> None:
    """Arm the first-seen marker once; subsequent calls are no-ops so the
    grace window is measured from genuine first eligibility."""
    p = _core().TELEMETRY_FIRST_SEEN_PATH
    if _marker_age_seconds(p) is None:
        _touch(p)


def touch_last_beat(now=None) -> None:
    """Stamp the last-beat-attempt marker to now (resets the throttle window).

    Called at the top of every non-disabled ``do_telemetry_beat`` run so the
    marker tracks the last ATTEMPT, not just the last successful send."""
    _touch(_core().TELEMETRY_LAST_BEAT_PATH)


def notice_already_shown() -> bool:
    return _core().TELEMETRY_NOTICE_SHOWN_PATH.exists()


def mark_notice_shown() -> None:
    _touch(_core().TELEMETRY_NOTICE_SHOWN_PATH)


def _endpoint() -> str:
    return os.environ.get("CCTALLY_TELEMETRY_ENDPOINT") or _core().TELEMETRY_ENDPOINT_DEFAULT


def do_telemetry_beat(config: dict, *, now=None, endpoint=None) -> str:
    """Orchestrate arm -> grace -> beat. Returns a status string:

    ``disabled:<reason>`` (opt-out), ``armed`` (first eligibility recorded),
    ``grace`` (still inside the first-beat grace window), ``sent`` (POST
    succeeded), or ``failed`` (network error, swallowed).

    Throttling to at most one beat per window is enforced at the PARENT
    spawn gate (``_post_command_update_hooks``), which only spawns this
    worker when ``telemetry_beat_due()`` is True. To make that bound hold
    regardless of outcome, the last-beat-attempt marker is stamped FIRST —
    immediately after the opt-out check, before the arm/grace/send branches
    (mirroring ``_do_update_check``'s touch-the-marker-first crash-safety
    pattern). Without this, the marker was stamped only on a successful
    send, so the parent gate stayed True through the entire 24h grace
    window and through any sustained outage (e.g. the endpoint not yet
    deployed) — re-spawning a fresh detached worker on EVERY command,
    including hot paths (statusline/record-usage/hook-tick).

    Only the network POST is wrapped, so this swallows connection / DNS /
    HTTP / timeout errors from the beat send (returning ``failed``); it is
    not a blanket "never raises" — a defect in the pure marker/id helpers
    would still surface (and the ``_telemetry-beat`` worker wraps the whole
    call in its own try/except as belt-and-suspenders)."""
    enabled, reason = resolve_telemetry_state(config)
    if not enabled:
        return f"disabled:{reason}"
    # Stamp the last-beat-attempt marker FIRST (crash-safe, outcome-
    # independent) so the parent spawn gate is bounded to <=1 worker per
    # throttle window whether this run arms, waits out grace, sends, or
    # fails. The internal ``telemetry_beat_due`` re-check that used to sit
    # below the grace branch is intentionally gone: the marker was just
    # touched, so it would always read fresh here.
    touch_last_beat(now)
    # Arm on first eligibility; do NOT beat until the grace window elapses.
    if _marker_age_seconds(_core().TELEMETRY_FIRST_SEEN_PATH) is None:
        mark_first_seen(now)
        ensure_install_id()
        return "armed"
    if not first_beat_grace_elapsed(now):
        return "grace"
    iid = ensure_install_id()
    payload = build_beat_payload(iid, now=now)
    try:
        req = urllib.request.Request(
            endpoint or _endpoint(),
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "User-Agent": f"cctally-telemetry/{payload['v']}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=3):
            pass
        # NOTE: the marker was already stamped at the top of this function
        # (touch-first), so no second touch is needed on the success path.
        return "sent"
    except Exception:
        return "failed"  # swallow — telemetry never affects UX


def _config_set_ns(key: str, value: str) -> argparse.Namespace:
    """Synthesize the ``argparse.Namespace`` ``cmd_config``'s ``set`` path
    consumes (``action``/``key``/``value``/``emit_json``). Routing ``on``/``off``
    through the real config setter keeps a single validation + locking
    chokepoint rather than re-implementing the read-modify-write here."""
    return argparse.Namespace(action="set", key=key, value=value, emit_json=False)


def cmd_telemetry(args) -> int:
    """`cctally telemetry [on|off|reset]` + bare status (`--json`).

    ``on``/``off`` flip the ``telemetry.enabled`` config key through the real
    ``cmd_config`` setter (its bool validation + atomic write). ``reset`` mints
    a fresh ``install_id``. The bare/status path is strictly READ-ONLY — it
    resolves the opt-out state from a RAW guarded ``config.json`` read (NOT
    ``load_config()``, which would auto-create config.json on a fresh install)
    and previews the month token WITHOUT ever minting an id (it calls
    ``read_install_id``, never ``ensure_install_id``). It writes nothing.
    """
    c = _cctally()
    action = getattr(args, "action", None)
    if action == "off":
        return c.cmd_config(_config_set_ns("telemetry.enabled", "false"))
    if action == "on":
        return c.cmd_config(_config_set_ns("telemetry.enabled", "true"))
    if action == "reset":
        reset_install_id()
        print("telemetry: install id reset")
        return 0

    # bare / status — strictly read-only. Resolve the opt-out state from a
    # RAW guarded config read (mirroring _cctally_doctor's telemetry gather),
    # NOT load_config(): load_config() calls ensure_dirs() and auto-creates
    # config.json on a fresh install, which would contradict the documented
    # "strictly read-only" / "never mints an install_id, writes config, or
    # sends a beat" contract (docs/telemetry.md, docs/commands/telemetry.md).
    # A missing/corrupt config degrades to `{}` (env/dev precedence still
    # resolves correctly).
    raw_config: dict = {}
    try:
        cfg_path = _core().CONFIG_PATH
        if cfg_path.exists():
            loaded = json.loads(cfg_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                raw_config = loaded
    except Exception:
        raw_config = {}
    enabled, reason = resolve_telemetry_state(raw_config)
    iid = read_install_id()
    period = current_period()
    token = telemetry_token(iid, period) if iid else None
    info = {
        "enabled": enabled,
        "reason": reason,
        "version": c.resolve_client_version(),
        "os": c.resolve_os_family(),
        "period": period,
        "token_preview": token,
        "fields": ["token", "version", "os"],
    }
    if getattr(args, "json", False):
        print(json.dumps(info))
        return 0
    print(f"telemetry: {'enabled' if enabled else 'disabled'} ({reason})")
    print(
        f"  sends: rotating monthly token + version ({info['version']}) "
        f"+ os ({info['os']})"
    )
    print(f"  token this month: {token or '(not yet armed)'}")
    print(
        "  opt out: cctally telemetry off  |  "
        "CCTALLY_DISABLE_TELEMETRY=1  |  DO_NOT_TRACK=1"
    )
    return 0
