"""`config.json` reader, writer, lock, validators, and `cctally config` entry point.

Eager I/O sibling: bin/cctally loads this at startup and re-exports
every public symbol so bare-name callers (the dashboard `/api/settings`
handler, `cmd_record_usage` reading `load_config()`, `cmd_refresh_usage`
gating on `_get_oauth_usage_config(load_config())`, the update-check
predicate, `sync-week`, …) all resolve unchanged. Tests that mock
`load_config` via ``monkeypatch.setitem(ns, "load_config", …)`` still
work because Python's bare-name lookup inside non-extracted bin/cctally
callers resolves in bin/cctally's namespace (where the re-export lives).

What stays in bin/cctally:
  - ``_ALERTS_BAD_CONFIG_WARNED`` + ``_warn_alerts_bad_config_once`` —
    alerts-coupled warn-once flag/helper; the alerts-config readers
    (``_get_alerts_config`` / ``_AlertsConfigError``) still live in
    bin/cctally and these two travel with that block.
  - ``CONFIG_PATH`` / ``CONFIG_LOCK_PATH`` path constants (spec §86–92
    keeps every path constant in bin/cctally so monkeypatched
    `cctally.CONFIG_PATH = …` redirects propagate everywhere).
  - ``eprint`` / ``ensure_dirs`` / ``DEFAULT_WEEK_START`` ubiquitous
    helpers/constants.
  - All validator/normalizer primitives (``normalize_display_tz_value``,
    ``_get_alerts_config``, ``_AlertsConfigError``,
    ``_normalize_alerts_enabled_value``, ``_validate_dashboard_bind_value``,
    ``_normalize_update_check_enabled_value``,
    ``_validate_update_check_ttl_hours_value``,
    ``UPDATE_DEFAULT_TTL_HOURS``, ``get_display_tz_pref``) — these stay
    near the subsystem they belong to; we reach them via the
    ``_cctally()`` accessor (call-time lookup so test monkeypatches on
    bin/cctally's namespace still propagate, per spec §5.2).

Spec: docs/superpowers/specs/2026-05-13-bin-cctally-split-design.md
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import secrets
import sys
from typing import Any


def _cctally():
    """Resolve the current `cctally` module at call-time (spec §5.5)."""
    return sys.modules["cctally"]


# === Honest imports from extracted homes ===================================
# Spec 2026-05-17-cctally-core-kernel-extraction.md §3.3: kernel symbols
# import from _cctally_core; the Bucket-X helper `normalize_display_tz_value`
# imports from `_lib_display_tz`. Path constants (`CONFIG_PATH`,
# `CONFIG_LOCK_PATH`) plus out-of-scope validators
# (`_normalize_alerts_enabled_value`, `_validate_dashboard_bind_value`,
# `_validate_update_check_ttl_hours_value`, `_normalize_update_check_enabled_value`,
# `get_display_tz_pref`, `UPDATE_DEFAULT_TTL_HOURS`) stay on the
# _cctally() accessor.
from _cctally_core import (
    eprint,
    ensure_dirs,
    DEFAULT_WEEK_START,
    _get_alerts_config,
    _AlertsConfigError,
)
from _lib_display_tz import normalize_display_tz_value


_CONFIG_CORRUPT_WARNED = False  # one-shot warn flag for load_config


def _warn_config_corrupt_once(reason: str) -> None:
    """Emit a single stderr warning per process when config.json is
    unreadable. Mirrors the warn-once pattern used by
    `_DISPLAY_TZ_BAD_CONFIG_WARNED` for malformed display.tz values.
    """
    global _CONFIG_CORRUPT_WARNED
    if _CONFIG_CORRUPT_WARNED:
        return
    _CONFIG_CORRUPT_WARNED = True
    c = _cctally()
    eprint(
        f"warning: ignoring corrupt {c.CONFIG_PATH} ({reason}); "
        "using in-memory defaults"
    )


def _default_config_data() -> dict[str, Any]:
    return {
        "collector": {
            "host": "127.0.0.1",
            "port": 17321,
            "token": secrets.token_hex(16),
            "week_start": DEFAULT_WEEK_START,
        }
    }


def _try_read_config() -> "dict[str, Any] | None":
    """Read+parse CONFIG_PATH. Returns None when missing OR corrupt.

    Corrupt cases (non-JSON or non-object root) emit a one-shot stderr
    warning and return None — caller decides whether to fall back to
    in-memory defaults or to overwrite with fresh defaults under the
    config writer lock.
    """
    c = _cctally()
    if not c.CONFIG_PATH.exists():
        return None
    try:
        raw = c.CONFIG_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        _warn_config_corrupt_once(f"read failed: {exc}")
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        _warn_config_corrupt_once(f"JSONDecodeError: {exc}")
        return None
    if not isinstance(data, dict):
        _warn_config_corrupt_once("non-object JSON root")
        return None
    return data


@contextlib.contextmanager
def config_writer_lock():
    """Exclusive fcntl.flock around config.json read-modify-write.

    Mirrors the cache.db.lock pattern (see sync_cache) but uses blocking
    LOCK_EX rather than LOCK_NB: config writes are millisecond-scale, so
    a brief wait is preferable to silently dropping a writer's update.
    Used by:
      - cctally config set / unset (CLI path)
      - dashboard POST /api/settings handler
      - load_config first-run create path
    External readers (load_config in the no-write path) do NOT acquire
    this lock — atomic os.replace in save_config guarantees readers see
    either the pre-rename or post-rename file, never partial bytes.
    """
    c = _cctally()
    ensure_dirs()
    c.CONFIG_LOCK_PATH.touch()
    fh = open(c.CONFIG_LOCK_PATH, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
    finally:
        fh.close()


def load_config() -> dict[str, Any]:
    """Read config.json, falling back to in-memory defaults on corruption.

    Concurrent-safety: readers see either the pre-rename or post-rename
    contents thanks to save_config's atomic os.replace. On corrupt or
    non-object JSON, emits a one-shot stderr warning and returns
    in-memory defaults WITHOUT re-saving — the next legitimate
    save_config call (under config_writer_lock) will overwrite the bad
    bytes atomically. On first run (file missing), creates the file
    with a fresh collector token under the writer lock so two parallel
    first-run processes don't clobber each other.

    DEADLOCK NOTE: `fcntl.flock` is per-fd even within the same
    process. Callers that already hold config_writer_lock MUST use
    `_load_config_unlocked()` instead — re-entering this function
    inside an outer lock would block forever (verified during issue
    #17 fix).
    """
    c = _cctally()
    ensure_dirs()
    parsed = _try_read_config()
    if parsed is not None:
        return parsed

    if c.CONFIG_PATH.exists():
        # Corrupt file: warning already emitted by _try_read_config.
        # Return in-memory defaults; do NOT persist — a transient
        # corruption is recoverable by the next legitimate
        # `cctally config set` (which now runs under the writer lock
        # with an atomic write).
        return _default_config_data()

    # First-run create: hold the writer lock so two simultaneous
    # first-runners agree on a single committed token.
    with config_writer_lock():
        parsed = _try_read_config()
        if parsed is not None:
            return parsed
        data = _default_config_data()
        save_config(data)
        return data


def _load_config_unlocked() -> dict[str, Any]:
    """`load_config` variant for use INSIDE an already-held
    config_writer_lock. Skips the first-run lock acquisition (which
    would self-deadlock — `fcntl.flock` is per-fd, not per-process)
    and never persists: the writer that already holds the lock will
    do its own save_config call atomically. Corrupt-file path returns
    in-memory defaults (caller's save will overwrite cleanly).
    """
    ensure_dirs()
    parsed = _try_read_config()
    if parsed is not None:
        return parsed
    return _default_config_data()


def save_config(data: dict[str, Any]) -> None:
    """Persist `data` to config.json atomically.

    Writes JSON to a sibling temp path (unique per writer PID so two
    unsynchronized writers cannot clobber each other's tmp), fsyncs the
    contents to disk, then `os.replace`s onto CONFIG_PATH. POSIX
    rename(2) is atomic on the same filesystem, so concurrent readers
    see either the old or the new file contents — never partial bytes.

    Concurrent writers must additionally serialize via
    config_writer_lock; the atomic rename alone protects readers but
    not the read-modify-write semantics of `cctally config set`.
    """
    c = _cctally()
    ensure_dirs()
    payload = (json.dumps(data, indent=2) + "\n").encode("utf-8")
    tmp = c.CONFIG_PATH.with_name(f"{c.CONFIG_PATH.name}.tmp.{os.getpid()}")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(str(tmp), str(c.CONFIG_PATH))


ALLOWED_CONFIG_KEYS = (
    "display.tz",
    "alerts.enabled",
    "dashboard.bind",
    "update.check.enabled",
    "update.check.ttl_hours",
)


def cmd_config(args: argparse.Namespace) -> int:
    """Get/set/unset persisted user preferences in config.json.

    Currently the only allowed key is "display.tz". Future keys join
    via ALLOWED_CONFIG_KEYS without changing the gate.

    Read-modify-write paths (set/unset) acquire config_writer_lock and
    re-read config.json INSIDE the lock so concurrent invocations are
    serialized; a stale pre-lock copy would lose updates.
    """
    c = _cctally()
    action = args.action

    if action == "get":
        return _cmd_config_get(args, c.load_config())
    if action == "set":
        return _cmd_config_set(args)
    if action == "unset":
        return _cmd_config_unset(args)
    eprint(f"cctally config: unknown action {action!r}")
    return 2


def _config_known_value(config: dict, key: str) -> "object":
    """Return the stored value for one of ALLOWED_CONFIG_KEYS.

    For ``display.tz`` returns the canonicalized form via
    ``get_display_tz_pref`` so the user sees what cctally actually
    applies. For ``alerts.enabled`` returns the validated boolean from
    ``_get_alerts_config`` (defaults to False when unset). Returns
    ``None`` only for unknown keys (caller treats as "missing").
    """
    c = _cctally()
    if key == "display.tz":
        return c.get_display_tz_pref(config)
    if key == "alerts.enabled":
        return bool(_get_alerts_config(config)["enabled"])
    if key == "dashboard.bind":
        # Default semantic alias is 'loopback' (resolves to 127.0.0.1 at
        # bind time). LAN exposure is opt-in via `set dashboard.bind lan`
        # or per-call `--host 0.0.0.0`.
        block = config.get("dashboard") if isinstance(config, dict) else None
        if not isinstance(block, dict):
            block = {}
        stored = block.get("bind")
        if not stored:
            return "loopback"
        try:
            return c._validate_dashboard_bind_value(stored)
        except ValueError:
            # Hand-edited junk: surface the default rather than the bad value;
            # `cmd_dashboard` warns at server-start when it hits the same path.
            return "loopback"
    if key in ("update.check.enabled", "update.check.ttl_hours"):
        # Defaults mirror `_is_update_check_due` (True / 24 hours).
        # Hand-edited junk surfaces as the default — matches dashboard.bind.
        update_block = (
            config.get("update") if isinstance(config, dict) else None
        )
        if not isinstance(update_block, dict):
            update_block = {}
        check_block = update_block.get("check")
        if not isinstance(check_block, dict):
            check_block = {}
        if key == "update.check.enabled":
            stored = check_block.get("enabled", True)
            return bool(stored) if isinstance(stored, bool) else True
        # update.check.ttl_hours
        stored = check_block.get("ttl_hours", c.UPDATE_DEFAULT_TTL_HOURS)
        try:
            return c._validate_update_check_ttl_hours_value(stored)
        except ValueError:
            return c.UPDATE_DEFAULT_TTL_HOURS
    return None


def _cmd_config_get(args: argparse.Namespace, config: dict) -> int:
    key = args.key
    if key is not None and key not in ALLOWED_CONFIG_KEYS:
        eprint(f"cctally config: unknown config key {key!r}")
        return 2
    pairs: "list[tuple[str, object]]" = []
    if key is None:
        for k in ALLOWED_CONFIG_KEYS:
            v = _config_known_value(config, k)
            pairs.append((k, v if v is not None else ""))
    else:
        v = _config_known_value(config, key)
        pairs.append((key, v if v is not None else ""))

    if getattr(args, "emit_json", False):
        # Walk every dot-delimited segment so keys deeper than two
        # segments (e.g. `update.check.enabled`) nest correctly. The
        # earlier `partition` form collapsed three-segment keys into
        # a flat tail (`{"update": {"check.enabled": ...}}`) and
        # diverged from `config set --json` / on-disk shape.
        out: "dict[str, object]" = {}
        for k, v in pairs:
            segments = k.split(".")
            node: dict = out
            for seg in segments[:-1]:
                node = node.setdefault(seg, {})
            node[segments[-1]] = v
        print(json.dumps(out, indent=2))
    else:
        for k, v in pairs:
            # Preserve canonical bool stringification (true/false) so
            # round-trips via `config set alerts.enabled <plain-text>` work.
            if isinstance(v, bool):
                rendered = "true" if v else "false"
            else:
                rendered = str(v)
            print(f"{k}={rendered}")
    return 0


def _cmd_config_set(args: argparse.Namespace) -> int:
    c = _cctally()
    key, raw = args.key, args.value
    if key not in ALLOWED_CONFIG_KEYS:
        eprint(f"cctally config: unknown config key {key!r}")
        return 2
    if key == "display.tz":
        try:
            canonical = normalize_display_tz_value(raw)
        except ValueError:
            eprint(f"cctally config: invalid IANA zone {raw!r}")
            return 2
        with config_writer_lock():
            config = _load_config_unlocked()
            config.setdefault("display", {})["tz"] = canonical
            save_config(config)
        if getattr(args, "emit_json", False):
            print(json.dumps({"display": {"tz": canonical}}, indent=2))
        else:
            print(f"display.tz={canonical}")
        return 0
    if key == "alerts.enabled":
        try:
            normalized = c._normalize_alerts_enabled_value(raw)
        except ValueError as exc:
            print(f"cctally: {exc}", file=sys.stderr)
            return 2
        # Read-modify-write under config_writer_lock, preserving any
        # other alerts.* keys (e.g. user-customized weekly_thresholds).
        # _load_config_unlocked is mandatory here — calling load_config
        # would self-deadlock on the same fcntl.flock fd.
        with config_writer_lock():
            config = _load_config_unlocked()
            # Pre-merge type guard: a hand-edited config with a non-dict
            # alerts block (e.g. ``"alerts": "bad"``) makes the dict()
            # copy below raise ValueError before _get_alerts_config can
            # surface a controlled error. Surface the same message
            # _AlertsConfigError would so the user sees a recoverable
            # rc=2 instead of an uncaught ValueError.
            existing_alerts = config.get("alerts")
            if existing_alerts is not None and not isinstance(
                existing_alerts, dict
            ):
                print(
                    "cctally: alerts config error: alerts must be an object",
                    file=sys.stderr,
                )
                return 2
            alerts_block = dict(existing_alerts or {})
            alerts_block["enabled"] = normalized
            # Validate the would-be merged block before persisting so
            # we never write a config that fails subsequent reads.
            try:
                _get_alerts_config({**config, "alerts": alerts_block})
            except _AlertsConfigError as exc:
                print(f"cctally: alerts config error: {exc}", file=sys.stderr)
                return 2
            config["alerts"] = alerts_block
            save_config(config)
        if getattr(args, "emit_json", False):
            print(json.dumps({"alerts": {"enabled": normalized}}, indent=2))
        else:
            print(f"alerts.enabled={'true' if normalized else 'false'}")
        return 0
    if key == "dashboard.bind":
        # Validation rejects whitespace / empty / non-string up front;
        # write proceeds under config_writer_lock with _load_config_unlocked
        # (calling load_config inside the writer-lock self-deadlocks per the
        # CLAUDE.md gotcha — fcntl.flock is per-fd, not per-process).
        try:
            canonical = c._validate_dashboard_bind_value(raw)
        except ValueError as exc:
            print(f"cctally: {exc}", file=sys.stderr)
            return 2
        with config_writer_lock():
            config = _load_config_unlocked()
            existing = config.get("dashboard")
            if existing is not None and not isinstance(existing, dict):
                print(
                    "cctally: dashboard config error: dashboard must be an object",
                    file=sys.stderr,
                )
                return 2
            block = dict(existing or {})
            block["bind"] = canonical
            config["dashboard"] = block
            save_config(config)
        if getattr(args, "emit_json", False):
            print(json.dumps({"dashboard": {"bind": canonical}}, indent=2))
        else:
            print(f"dashboard.bind={canonical}")
        return 0
    if key in ("update.check.enabled", "update.check.ttl_hours"):
        # Validate first; rejection short-circuits before lock acquisition.
        if key == "update.check.enabled":
            try:
                normalized: object = c._normalize_update_check_enabled_value(raw)
            except ValueError as exc:
                print(f"cctally: {exc}", file=sys.stderr)
                return 2
            inner_key = "enabled"
        else:
            try:
                normalized = c._validate_update_check_ttl_hours_value(raw)
            except ValueError as exc:
                print(f"cctally: {exc}", file=sys.stderr)
                return 2
            inner_key = "ttl_hours"
        with config_writer_lock():
            config = _load_config_unlocked()
            existing_update = config.get("update")
            if existing_update is not None and not isinstance(existing_update, dict):
                print(
                    "cctally: update config error: update must be an object",
                    file=sys.stderr,
                )
                return 2
            update_block = dict(existing_update or {})
            existing_check = update_block.get("check")
            if existing_check is not None and not isinstance(existing_check, dict):
                print(
                    "cctally: update config error: update.check must be an object",
                    file=sys.stderr,
                )
                return 2
            check_block = dict(existing_check or {})
            check_block[inner_key] = normalized
            update_block["check"] = check_block
            config["update"] = update_block
            save_config(config)
        if getattr(args, "emit_json", False):
            print(json.dumps({"update": {"check": {inner_key: normalized}}}, indent=2))
        else:
            if isinstance(normalized, bool):
                rendered = "true" if normalized else "false"
            else:
                rendered = str(normalized)
            print(f"{key}={rendered}")
        return 0
    return 2  # unreachable given the gate above


def _cmd_config_unset(args: argparse.Namespace) -> int:
    key = args.key
    if key not in ALLOWED_CONFIG_KEYS:
        eprint(f"cctally config: unknown config key {key!r}")
        return 2
    if key == "display.tz":
        with config_writer_lock():
            config = _load_config_unlocked()
            block = config.get("display")
            if isinstance(block, dict) and "tz" in block:
                del block["tz"]
                if not block:
                    config.pop("display", None)
                save_config(config)
            # idempotent: silent on missing key
        return 0
    if key == "alerts.enabled":
        # Mirror the display.tz branch: writer-lock + _load_config_unlocked
        # (NOT load_config — fcntl.flock is per-fd so re-entry would
        # self-deadlock per the gotcha in CLAUDE.md). Unsetting just the
        # `enabled` key preserves any user-customized threshold lists
        # (`weekly_thresholds`, `five_hour_thresholds`); the read-time
        # validator (`_get_alerts_config`) re-applies the canonical
        # default of `enabled = False` for the missing key on next get.
        with config_writer_lock():
            config = _load_config_unlocked()
            block = config.get("alerts")
            if isinstance(block, dict) and "enabled" in block:
                del block["enabled"]
                if not block:
                    config.pop("alerts", None)
                save_config(config)
            # idempotent: silent on missing key
        return 0
    if key == "dashboard.bind":
        # Mirror the display.tz / alerts.enabled branches: writer-lock +
        # _load_config_unlocked. Drops only the `bind` key; if `dashboard`
        # ends up empty, drop the parent block too so config.json stays tidy.
        with config_writer_lock():
            config = _load_config_unlocked()
            block = config.get("dashboard")
            if isinstance(block, dict) and "bind" in block:
                del block["bind"]
                if not block:
                    config.pop("dashboard", None)
                save_config(config)
            # idempotent: silent on missing key
        return 0
    if key in ("update.check.enabled", "update.check.ttl_hours"):
        # Mirror the dashboard.bind branch: drop the leaf, then prune
        # empty `check` and empty `update` so config.json stays tidy.
        inner_key = (
            "enabled" if key == "update.check.enabled" else "ttl_hours"
        )
        with config_writer_lock():
            config = _load_config_unlocked()
            update_block = config.get("update")
            if isinstance(update_block, dict):
                check_block = update_block.get("check")
                if isinstance(check_block, dict) and inner_key in check_block:
                    del check_block[inner_key]
                    if not check_block:
                        del update_block["check"]
                    if not update_block:
                        config.pop("update", None)
                    save_config(config)
            # idempotent: silent on missing key
        return 0
    return 2  # unreachable given the gate above
