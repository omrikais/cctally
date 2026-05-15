"""Update subsystem for cctally (subcommand + dashboard worker).

Eager I/O sibling: bin/cctally loads this at startup. Owns the
``cctally update`` user-facing surface and the hidden
``_update-check`` background worker, plus the dashboard's update
worker / polling thread:

- ``cmd_update`` — ``cctally update`` entry point. Routes by mode
  flag (``--check`` / ``--skip`` / ``--remind-later`` / install).
  Mode flags are mutually exclusive; ``--version`` is install-mode
  only. Argparse-enforced for the user-facing surface; the
  dispatcher's redundant check is defense in depth for programmatic
  callers and a clearer error message.
- ``cmd_update_check_internal`` — hidden ``_update-check``
  subcommand (``argparse.SUPPRESS``ed). The detached-refresh
  worker — not user-facing. Logs lifecycle events to
  ``update.log`` and rotates if needed. Always returns 0 (any
  error is logged but the process exits cleanly so the parent
  spawn-and-forget contract holds).
- ``UpdateError`` + 6 subclasses — typed exception hierarchy
  consumed at the command boundary (``cmd_update``,
  ``cmd_update_check_internal``) AND by dashboard ``/api/update*``
  handlers in bin/cctally. ``UpdateValidationError`` (rc=2),
  ``UpdateInProgressError`` (carries prior PID), and the four
  ``UpdateCheck*`` types (network / rate-limited / HTTP / parse).
  Because the classes are defined here, ``raise`` in moved code
  and ``except`` in moved code both resolve to the SAME class
  object; the eager re-export means dashboard catch sites in
  bin/cctally also see the same object — no class-identity
  mismatch under ``isinstance``/``except``.
- State/lock/log primitives: ``_load_update_state``,
  ``_save_update_state``, ``_load_update_suppress``,
  ``_save_update_suppress``, ``_read_lock_pid``,
  ``_acquire_update_lock``, ``_release_update_lock``,
  ``_rotate_update_log_if_needed``, ``_log_update_event``.
  Atomic write idiom (PID-suffixed tmp + ``os.replace``) +
  schema-versioned-JSON contract per spec §1; mirrors
  ``save_config``'s idiom. ``_acquire_update_lock`` uses
  kernel-authoritative ``kill(pid, 0)`` for stale-lock reclaim.
- Install-method detection (spec §2): ``InstallMethod``
  (``@dataclass(frozen=True)``), ``_resolve_npm_prefix`` (three-tier
  $env → state-file → ``npm prefix -g`` resolution),
  ``_detect_install_method`` (path heuristic over
  ``realpath(sys.argv[0])``), ``_persist_npm_prefix_to_state``,
  ``_persist_install_method_to_state``,
  ``_stamp_install_success_to_state`` (post-install state stamp so
  the banner + dashboard auto-close fire immediately),
  ``_self_heal_current_version`` (reconciles ``current_version``
  with running binary CHANGELOG; closes the
  ``update-state.json lies after manual upgrade`` memory gotcha;
  dev-clone guard via ``.git/`` parent check per issue #42).
- Version-check pipeline (spec §3): ``_update_user_agent``,
  ``_fetch_url`` (typed-exception urllib wrapper),
  ``_check_npm_latest_version``, ``_check_brew_latest_version``
  (priority regex chain ``_BREW_VERSION_RE_LIST``),
  ``_is_update_check_due`` (TTL gate),
  ``_do_update_check`` (the single chokepoint — touches the
  throttle marker FIRST for crash safety),
  ``_spawn_background_update_check`` (detached worker spawner),
  ``cmd_update_check_internal``.
- ``--check`` rendering (spec §4.4): ``_format_update_command``,
  ``_prerelease_note``, ``_format_update_check_json``,
  ``_format_update_check_human``, plus the
  ``_UPDATE_METHOD_HUMAN_LABEL`` map and
  ``_UPDATE_CHECK_JSON_UNAVAILABLE_ENVELOPE`` for the
  state-unavailable fallback.
- User-facing flows: ``_do_update_skip`` /
  ``_do_update_remind_later`` (suppress mutations),
  ``_do_update_check_user`` (user-mode --check that bypasses TTL
  when ``--force``).
- Install execution (spec §5): ``_preflight_install`` (ordered
  gates: method≠unknown, semver-valid, brew+version reject,
  npm-prefix-writable), ``_build_update_steps`` (brew is two steps
  for diagnostic clarity per §5.2; npm is one), ``_run_streaming``
  (two-thread pump → callbacks + log lines),
  ``_do_update_install`` (acquire lock → run steps → release →
  rotate log; dry-run path skips lock + subprocesses),
  ``_resolve_execvp_target`` (npm shim re-entry path, spec §5.7).
- Dashboard surface: ``UpdateWorker`` (single-slot orchestrator,
  spec §5.6; idempotent-release contract per §5.6.1),
  ``_DashboardUpdateCheckThread`` (poll cadence ≠ network-call
  frequency; self-heal + TTL probe + SSE republish).
- Update banner (spec §4.2): ``_args_emit_json`` /
  ``_args_emit_machine_stdout`` (the two predicate primitives
  ``_should_show_update_banner`` delegates to so a new --json dest
  variant or status-line flag inherits suppression automatically —
  codex finding #8 invariant), ``_semver_gt``,
  ``_compute_effective_update_available`` (single source of truth
  for "is there a *real* pending update?" shared by the banner
  predicate AND ``cctally doctor``'s ``safety.update_available``
  check), ``_should_show_update_banner``, ``_format_update_banner``,
  and the ``_UPDATE_BANNER_EXTRA_SUPPRESSED`` set. These were
  specifically called out in Appendix A row #16 as
  over-extracted-then-restored during the _cctally_db split; they
  move with the update vertical here.
- Update config validators: ``_normalize_update_check_enabled_value``
  / ``_validate_update_check_ttl_hours_value`` +
  ``_UPDATE_CHECK_TTL_HOURS_MIN`` / ``_UPDATE_CHECK_TTL_HOURS_MAX``.
  Consumed by ``_cctally_config`` (CLI ``config get/set/unset``
  + dashboard ``POST /api/settings``) via the
  ``c = _cctally(); c._validate_update_check_ttl_hours_value``
  accessor; eager re-export from this sibling means the cctally
  namespace exposes them unchanged.

What stays in bin/cctally:
- All ``UPDATE_*`` path constants (source-of-truth at L2001-2023);
  consumed via ``c = _cctally(); c.UPDATE_STATE_PATH`` etc. in moved
  code so ``monkeypatch.setitem(ns, "UPDATE_STATE_PATH", tmp)`` in
  ``tests/test_update.py`` propagates transparently — no sibling-side
  patches needed. Mirrors Phase D #17/#18 precedent.
- ``ORIGINAL_SYS_ARGV`` / ``ORIGINAL_ENTRYPOINT`` /
  ``_UPDATE_WORKER`` — module-level globals written by
  ``cmd_dashboard`` at boot (``global`` statement at L23205);
  read by moved ``_resolve_execvp_target`` and dashboard's
  ``/api/update*`` handlers. Stays in cctally so the existing
  write surface in cmd_dashboard works unchanged; moved code
  reads via ``c.X``.
- ``eprint``, ``_now_utc`` (used by moved code via shim/accessor),
  ``_release_read_latest_release_version`` (stays in cctally per
  spec §6.7 — 6+ external callers, file I/O over CHANGELOG.md),
  ``_release_parse_semver`` / ``_release_semver_sort_key`` (lives
  in ``_lib_semver`` and re-exported by cctally),
  ``load_config`` (lives in ``_cctally_config``; re-exported),
  ``_BANNER_SUPPRESSED_COMMANDS`` (lives in ``_cctally_db``;
  re-exported by cctally — composed with the update-only
  ``_UPDATE_BANNER_EXTRA_SUPPRESSED`` inside
  ``_should_show_update_banner``),
  ``CHANGELOG_PATH``, ``PUBLIC_REPO`` (cctally module-level
  constants used in moved code via ``c.X``),
  ``_normalize_alerts_enabled_value`` (alerts vertical helper
  reused by update.check.enabled normalizer; stays in cctally per
  task brief).
- ``SSEHub`` / ``_SnapshotRef`` types referenced only in
  ``_DashboardUpdateCheckThread.__init__`` annotations as
  string-typed forward refs — not resolved at runtime, no
  ``import cctally`` needed.

§5.6 audit on this extraction's monkeypatch surface
(``tests/test_update.py`` is the primary site; 21 distinct
``ns["X"]`` symbol-access points + 21 ``monkeypatch.setitem(ns, "X", …)``
mutation points). Forces the **eager re-export** carve-out
per spec §4.8 (same precedent as Phase E #19/#20):

- ``ns["X"]`` reads on dataclass / function objects propagate
  via eager re-export; PEP 562 ``__getattr__`` does NOT fire on
  ``ns["X"]`` dict-key access because ``ns`` is the module's
  ``__dict__``, not the module proxy. Re-export at module-load
  time means cctally's ``__dict__`` carries the same object the
  sibling defines.
- ``monkeypatch.setitem(ns, "X", mock)`` mutates cctally's
  namespace. For a moved symbol that is ALSO called bare-name by
  another moved body (e.g. ``_DashboardUpdateCheckThread.run`` →
  ``_do_update_check`` / ``_is_update_check_due`` /
  ``_self_heal_current_version`` / ``_load_update_state``;
  ``cmd_update_check_internal`` → ``_do_update_check``;
  ``_acquire_update_lock`` → ``_read_lock_pid``; etc.), the
  internal bare-name lookup resolves in this sibling's
  ``__dict__``, NOT cctally's — so the mock would not propagate.
  Pattern matches Phase D #17/#18: every cross-call from one
  moved function to another moved function that's also a
  monkeypatch target routes through ``c.X`` (alias for
  ``sys.modules['cctally'].X``) at call time. The accessor
  resolves at every call so the latest binding wins; mocks
  propagate without sibling-side patches.

Spec: docs/superpowers/specs/2026-05-13-bin-cctally-split-design.md §7.2
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import pathlib
import queue
import re
import secrets
import shlex
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable


def _cctally():
    """Resolve the current ``cctally`` module at call-time (spec §5.5)."""
    return sys.modules["cctally"]


# === Module-level back-ref shims for helpers that STAY in bin/cctally ======
# Each shim resolves ``sys.modules['cctally'].X`` at CALL TIME (not bind
# time), so monkeypatches on cctally's namespace propagate into the moved
# code unchanged. Mirrors the precedent established in
# ``bin/_cctally_record.py`` (34 shims), ``bin/_cctally_cache.py``
# (4 shims), and ``bin/_cctally_db.py`` (4 shims).
def eprint(*args, **kwargs):
    return sys.modules["cctally"].eprint(*args, **kwargs)


def _now_utc(*args, **kwargs):
    return sys.modules["cctally"]._now_utc(*args, **kwargs)


def load_config(*args, **kwargs):
    return sys.modules["cctally"].load_config(*args, **kwargs)


def save_config(*args, **kwargs):
    return sys.modules["cctally"].save_config(*args, **kwargs)


def _release_read_latest_release_version(*args, **kwargs):
    return sys.modules["cctally"]._release_read_latest_release_version(
        *args, **kwargs
    )


def _release_parse_semver(*args, **kwargs):
    return sys.modules["cctally"]._release_parse_semver(*args, **kwargs)


def _release_semver_sort_key(*args, **kwargs):
    return sys.modules["cctally"]._release_semver_sort_key(*args, **kwargs)


def _normalize_alerts_enabled_value(*args, **kwargs):
    return sys.modules["cctally"]._normalize_alerts_enabled_value(
        *args, **kwargs
    )


# === Exception hierarchy (spec §1) ========================================


class UpdateError(Exception):
    """User-facing error from the update subcommand. Caught at command boundary.

    Default rc when caught at the boundary is 1 (runtime / environment
    failure: unknown install method, npm prefix not writable, etc.).
    Validation errors (invalid --version syntax, --version+brew combo)
    use the :class:`UpdateValidationError` subclass so the boundary can
    map them to rc=2 — preserving the rc=1-vs-rc=2 distinction the
    Task-4 inline gates exposed.
    """


class UpdateValidationError(UpdateError):
    """Subclass of UpdateError marking input-validation failures (rc=2).

    Two cases per spec §5.1: invalid --version syntax (must match
    _SEMVER_RE), and --version with method=brew (no versioned formulae).
    Carved out of UpdateError so cmd_update's try/except can branch on
    type rather than message — the inline gates that Task 4 used both
    returned rc=2; preserving that contract is the test invariant.
    """


class UpdateInProgressError(UpdateError):
    """Another update is already running. Carries the prior PID for the operator
    message ("Another update is in progress (PID 12345)."). Raised by
    _acquire_update_lock when a live PID still holds update.lock.

    ``prior_pid`` is ``None`` when the lock body was unparseable (no
    ``PID=`` line, or non-integer value) — rendered as
    "(PID unknown)" rather than a sentinel like ``0`` (which is a real
    PID in POSIX semantics: the kernel scheduler on Linux)."""

    def __init__(self, prior_pid: int | None):
        if prior_pid is None:
            super().__init__("Another update is in progress (PID unknown).")
        else:
            super().__init__(f"Another update is in progress (PID {prior_pid}).")
        self.prior_pid = prior_pid


class UpdateCheckNetworkError(UpdateError):
    """DNS / connection / non-rate-limit HTTP failure during version check."""


class UpdateCheckRateLimited(UpdateError):
    """HTTP 429 from npm registry or GitHub raw-content host. Treated as
    non-error (last-known `latest_version` preserved); banner predicate is
    still evaluated against the cached value."""


class UpdateCheckHTTPError(UpdateError):
    """Non-200, non-429 HTTP status from a version-check endpoint."""


class UpdateCheckParseError(UpdateError):
    """Endpoint returned a body we couldn't parse (npm JSON missing
    `version` field, formula ruby missing `version "X.Y.Z"` line)."""


# === update.check config validators ========================================
# Bounds: 1 hour minimum (avoids accidental DDOS of the registry on a tight
# loop), 720 hour (= 30 days) maximum. Out-of-range returns ValueError so
# callers in both the CLI (``_cmd_config_set`` in ``_cctally_config``) and
# the dashboard (``_handle_post_settings``) can map to their own exit-code /
# HTTP-status semantics.
_UPDATE_CHECK_TTL_HOURS_MIN = 1
_UPDATE_CHECK_TTL_HOURS_MAX = 720


def _normalize_update_check_enabled_value(raw: str) -> bool:
    """Normalize the CLI string for update.check.enabled. Reuses the
    alerts.enabled string vocabulary so users don't have to remember a
    second set of valid words.
    """
    try:
        return _normalize_alerts_enabled_value(raw)
    except ValueError:
        # Re-raise with the right key name in the message so the user
        # sees `update.check.enabled` not `alerts.enabled`.
        raise ValueError(
            f"invalid boolean value for update.check.enabled: {raw!r} "
            "(expected true|false|yes|no|1|0|on|off)"
        )


def _validate_update_check_ttl_hours_value(raw) -> int:
    """Validate update.check.ttl_hours (int hours). Accepts an int or a
    string of digits; rejects bools (Python ``True`` is an int subclass
    so callers pre-validating JSON shapes must NOT pass a bool through
    here). Range bound: ``[_UPDATE_CHECK_TTL_HOURS_MIN, _MAX]``.
    """
    if isinstance(raw, bool):
        raise ValueError(
            "invalid value for update.check.ttl_hours: "
            f"{raw!r} (expected integer in "
            f"[{_UPDATE_CHECK_TTL_HOURS_MIN}, {_UPDATE_CHECK_TTL_HOURS_MAX}])"
        )
    if isinstance(raw, int):
        n = raw
    elif isinstance(raw, str):
        s = raw.strip()
        try:
            n = int(s, 10)
        except ValueError:
            raise ValueError(
                f"invalid integer for update.check.ttl_hours: {raw!r}"
            )
    else:
        raise ValueError(
            "invalid value for update.check.ttl_hours: "
            f"{raw!r} (expected integer in "
            f"[{_UPDATE_CHECK_TTL_HOURS_MIN}, {_UPDATE_CHECK_TTL_HOURS_MAX}])"
        )
    if n < _UPDATE_CHECK_TTL_HOURS_MIN or n > _UPDATE_CHECK_TTL_HOURS_MAX:
        raise ValueError(
            "update.check.ttl_hours out of range: "
            f"{n} (must be in [{_UPDATE_CHECK_TTL_HOURS_MIN}, "
            f"{_UPDATE_CHECK_TTL_HOURS_MAX}])"
        )
    return n


# === update-subcommand state-file / lock / log helpers (spec §1) =========
# These live next to load_config / save_config because they share the
# atomic-write idiom (PID-suffixed tmp + os.replace) and the schema-
# versioned-JSON contract. Kept stdlib-only per the project's zero-dep
# ethos.

_UPDATE_STATE_SCHEMA_MAX = 1
_UPDATE_SUPPRESS_SCHEMA_MAX = 1


def _load_update_state() -> dict[str, Any] | None:
    """Read ``update-state.json``. Returns None when the file is absent
    so callers can distinguish "never checked" from "checked, no update."

    Raises :class:`UpdateError` when ``_schema`` exceeds the highest
    version this binary knows about — forward-compat invariant from
    spec §1.7. An older cctally must NOT silently drop fields that a
    newer cctally wrote (that would invert the suppress-versions list,
    miss new check_status enum values, etc.).

    JSON-decode errors also raise :class:`UpdateError`; the writer's
    atomic os.replace guarantees readers never see partial bytes, so a
    parse failure means the file was already corrupt before our read.
    """
    c = _cctally()
    try:
        text = c.UPDATE_STATE_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise UpdateError(f"update-state.json is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise UpdateError(
            f"update-state.json must be a JSON object, got {type(data).__name__}"
        )
    schema = data.get("_schema", 0)
    if not isinstance(schema, int) or schema > _UPDATE_STATE_SCHEMA_MAX:
        raise UpdateError(
            f"update-state.json has _schema={schema!r}; this cctally is older "
            f"than the state file. Upgrade cctally."
        )
    return data


def _save_update_state(state: dict[str, Any]) -> None:
    """Persist ``update-state.json`` atomically.

    Mirrors :func:`save_config`: PID-suffixed tmp sibling, fsync the
    bytes, then ``os.replace`` onto the final path. POSIX rename(2) is
    atomic on the same filesystem, so concurrent readers see either the
    pre-rename or post-rename contents but never partial bytes.
    Concurrent writers don't race the bytes themselves but may stomp
    each other's logical updates — the update subcommand serializes
    writers via ``UPDATE_LOCK_PATH`` (spec §5.3).
    """
    c = _cctally()
    c.UPDATE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(state, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    tmp = c.UPDATE_STATE_PATH.with_name(
        f"{c.UPDATE_STATE_PATH.name}.tmp.{os.getpid()}"
    )
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(str(tmp), str(c.UPDATE_STATE_PATH))


def _load_update_suppress() -> dict[str, Any]:
    """Read ``update-suppress.json``. Returns a default empty record when
    the file is absent (spec §1.3) so the banner predicate doesn't have
    to None-guard every read. Same forward-compat schema check as
    :func:`_load_update_state`.
    """
    c = _cctally()
    default = {"_schema": 1, "skipped_versions": [], "remind_after": None}
    try:
        text = c.UPDATE_SUPPRESS_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise UpdateError(
            f"update-suppress.json is not valid JSON: {e}"
        ) from e
    if not isinstance(data, dict):
        raise UpdateError(
            f"update-suppress.json must be a JSON object, got "
            f"{type(data).__name__}"
        )
    schema = data.get("_schema", 0)
    if not isinstance(schema, int) or schema > _UPDATE_SUPPRESS_SCHEMA_MAX:
        raise UpdateError(
            f"update-suppress.json has _schema={schema!r}; this cctally is "
            f"older than the suppress file. Upgrade cctally."
        )
    return data


def _save_update_suppress(suppress: dict[str, Any]) -> None:
    """Persist ``update-suppress.json`` atomically. Same idiom as
    :func:`_save_update_state`."""
    c = _cctally()
    c.UPDATE_SUPPRESS_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(suppress, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    tmp = c.UPDATE_SUPPRESS_PATH.with_name(
        f"{c.UPDATE_SUPPRESS_PATH.name}.tmp.{os.getpid()}"
    )
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(str(tmp), str(c.UPDATE_SUPPRESS_PATH))


def _read_lock_pid(fd: int) -> int | None:
    """Parse ``PID=<n>`` out of an open update.lock fd. Returns None on
    any failure (file empty, missing PID line, non-integer value) — the
    caller treats "unknown holder" the same as a stale lock and
    attempts a second LOCK_NB acquire."""
    try:
        os.lseek(fd, 0, 0)
        body = os.read(fd, 1024).decode("utf-8")
    except OSError:
        return None
    for line in body.splitlines():
        if line.startswith("PID="):
            try:
                return int(line[4:])
            except ValueError:
                return None
    return None


def _acquire_update_lock() -> int:
    """Acquire the singleton update.lock under spec §5.3 contract.

    Returns the open fd on success. Caller MUST pass the fd to
    :func:`_release_update_lock` to drop the flock + unlink the file.

    Raises :class:`UpdateInProgressError` when a *live* PID still holds
    the lock. Stale locks (writer crashed without releasing) are
    silently reclaimed: ``kill(pid, 0)`` raising ``ProcessLookupError``
    is the only signal we trust for reclaim — kernel-authoritative,
    free of read-the-file-then-stat races.

    Body format (text, line-oriented for ``cat update.lock``)::

        PID=12345
        STARTED_AT_UTC=2026-05-10T13:05:23+00:00
        COMMAND=cctally update
    """
    c = _cctally()
    c.UPDATE_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(
        str(c.UPDATE_LOCK_PATH), os.O_CREAT | os.O_RDWR, 0o644
    )
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        # _read_lock_pid is module-local (no monkeypatch surface — its
        # contract is pure file-read on the supplied fd), so the bare
        # name is fine here.
        prior = _read_lock_pid(fd)
        if prior is not None:
            try:
                os.kill(prior, 0)
            except ProcessLookupError:
                pass  # stale → fall through to reclaim attempt
            else:
                # Live PID still holds the lock — refuse.
                os.close(fd)
                raise UpdateInProgressError(prior)
        # Stale (or unparseable PID): retry the non-blocking acquire.
        # If it still fails, another process raced us into the same
        # reclaim path; surface it as in-progress with the best PID
        # we observed (or 0 if we couldn't read one).
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise UpdateInProgressError(prior)
    os.ftruncate(fd, 0)
    body = (
        f"PID={os.getpid()}\n"
        f"STARTED_AT_UTC={_now_utc().isoformat()}\n"
        f"COMMAND=cctally update\n"
    ).encode("utf-8")
    os.write(fd, body)
    return fd


def _release_update_lock(fd: int) -> None:
    """Drop the flock and close the fd. The lock file persists.

    Defensive on every step: a double-release (or a release after the
    fd has been closed by an earlier error path) must not raise.

    The file at ``UPDATE_LOCK_PATH`` is deliberately NOT unlinked.
    ``flock`` locks the inode behind the fd, not the path: unlinking
    after release lets a peer that ``O_CREAT``ed a new inode at the
    same path hold a "lock" on a different inode from a peer that
    still references the old one — concurrent updates. Leaving the
    file in place pins all acquires to a single inode; the kernel's
    flock state is the sole synchronization primitive. ``_acquire_..``
    handles the persistent-file case (O_CREAT + ftruncate + rewrite
    on every acquire).
    """
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass


def _rotate_update_log_if_needed() -> None:
    """Rotate ``update.log`` → ``update.log.1`` when the live log
    crosses :data:`UPDATE_LOG_ROTATE_BYTES` (1 MB, spec §1.5).

    Single rotation slot: a second rotation overwrites the first.
    Failed-install logs are preserved on disk only until the next
    successful run grows the live log past 1 MB — operators chasing a
    historical failure should grab ``update.log.1`` while it's still
    around.

    No-op when the file is absent or below threshold.
    """
    c = _cctally()
    try:
        size = c.UPDATE_LOG_PATH.stat().st_size
    except FileNotFoundError:
        return
    if size < c.UPDATE_LOG_ROTATE_BYTES:
        return
    try:
        c.UPDATE_LOG_ROTATED_PATH.unlink()
    except FileNotFoundError:
        pass
    c.UPDATE_LOG_PATH.rename(c.UPDATE_LOG_ROTATED_PATH)


def _log_update_event(log_fd, event: str, **kv: Any) -> None:
    """Append one event line to ``update.log``.

    Format: ``<iso-utc> <EVENT> k=v k=v ...``. Strings containing
    spaces are wrapped with ``repr`` so the log stays grep-friendly;
    integers are emitted bare so size/elapsed columns can be
    arithmetic-parsed. ``log_fd`` is any text-mode writable file-like
    (``open(UPDATE_LOG_PATH, "a", encoding="utf-8")`` is the production
    caller from Task 5).
    """
    parts = [_now_utc().isoformat(), event]
    for k, v in kv.items():
        if isinstance(v, str) and " " in v:
            parts.append(f"{k}={v!r}")
        else:
            parts.append(f"{k}={v}")
    log_fd.write(" ".join(parts) + "\n")
    log_fd.flush()


# === Update subcommand: install-method detection (spec §2) =================
# Path-based heuristic over `realpath(sys.argv[0])`:
#   - "/Cellar/cctally/" substring → method="brew" (Apple Silicon, Intel,
#     and Linuxbrew all funnel through `<root>/Cellar/cctally/`).
#   - "<npm-prefix>/lib/node_modules/cctally/" prefix → method="npm".
#   - Anything else (source install, pnpm/yarn-global/volta, dev symlink)
#     → method="unknown" → manual-fallback bucket per spec §2.4.
# `mutate=False` is the dry-run contract (§5.5): every tier still
# computes, but tier-C cache writes to update-state.json are skipped.


@dataclass(frozen=True)
class InstallMethod:
    """Resolved install method for the running cctally binary (spec §2.1).

    ``method`` is one of ``"brew"``, ``"npm"``, ``"unknown"``;
    ``realpath`` is ``os.path.realpath(sys.argv[0])`` (the resolved
    target of any symlinks on $PATH); ``npm_prefix`` is populated only
    when ``method == "npm"`` so callers don't have to special-case it.
    """

    method: str
    realpath: str
    npm_prefix: str | None


def _resolve_npm_prefix(*, mutate: bool = True) -> str | None:
    """Three-tier ``npm prefix -g`` resolution (spec §2.2).

    Tier A: ``$npm_config_prefix`` env var (rarely set; free).
    Tier B: cached ``install.npm_prefix`` from update-state.json,
        7-day TTL (one ``os.stat`` via ``_load_update_state``).
    Tier C: ``subprocess.run(["npm", "prefix", "-g"], timeout=2.0)``
        (200–300 ms cold). Tier-C success populates tier-B only when
        ``mutate=True``; failure (npm not on PATH, timeout, non-zero
        exit) returns ``None`` regardless of ``mutate``.
    """
    c = _cctally()
    # Tier A — env var short-circuit.
    env_pref = os.environ.get("npm_config_prefix")
    if env_pref and pathlib.Path(env_pref).is_dir():
        return env_pref
    # Tier B — cached state-file value within 7-day TTL.
    # `_load_update_state` routed through cctally so a
    # `monkeypatch.setitem(ns, "_load_update_state", mock)` propagates.
    state = c._load_update_state()
    if state and isinstance(state.get("install"), dict):
        cached = state["install"].get("npm_prefix")
        detected_iso = state["install"].get("detected_at_utc")
        if cached and detected_iso:
            try:
                detected = dt.datetime.fromisoformat(detected_iso)
                age = (_now_utc() - detected).total_seconds()
                if age < c.UPDATE_NPM_PREFIX_TTL_DAYS * 86400:
                    return cached
            except (ValueError, TypeError):
                # Malformed timestamp → fall through to tier C.
                pass
    # Tier C — subprocess. Treat any failure as "unknown npm prefix"
    # rather than raising; the caller maps None → method="unknown".
    try:
        result = subprocess.run(
            ["npm", "prefix", "-g"],
            timeout=c.UPDATE_NPM_PREFIX_TIMEOUT_S,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    prefix = result.stdout.strip()
    if not prefix:
        return None
    if mutate:
        c._persist_npm_prefix_to_state(prefix)
    return prefix


def _persist_npm_prefix_to_state(prefix: str) -> None:
    """Write ``install.npm_prefix`` + ``install.detected_at_utc`` to
    update-state.json, preserving every other field. Used only by
    tier-C of :func:`_resolve_npm_prefix` when ``mutate=True``.
    """
    c = _cctally()
    state = c._load_update_state() or {"_schema": 1}
    state.setdefault("install", {})
    state["install"]["npm_prefix"] = prefix
    state["install"]["detected_at_utc"] = _now_utc().isoformat()
    c._save_update_state(state)


def _detect_install_method(*, mutate: bool = True) -> InstallMethod:
    """Detect how the running cctally was installed (spec §2.1).

    Path-based heuristic — see module-level comment above
    :class:`InstallMethod` for the algorithm. ``mutate=False`` honours
    the ``--dry-run`` "touch nothing" contract: detection still runs,
    but neither the npm-prefix tier-B cache nor the install block is
    persisted to update-state.json.
    """
    c = _cctally()
    real = os.path.realpath(sys.argv[0])
    if "/Cellar/cctally/" in real:
        method = InstallMethod(method="brew", realpath=real, npm_prefix=None)
    else:
        prefix = c._resolve_npm_prefix(mutate=mutate)
        if prefix:
            nm_root = os.path.join(prefix, "lib", "node_modules", "cctally")
            if real == nm_root or real.startswith(nm_root + os.sep):
                method = InstallMethod(
                    method="npm", realpath=real, npm_prefix=prefix
                )
            else:
                method = InstallMethod(
                    method="unknown", realpath=real, npm_prefix=None
                )
        else:
            method = InstallMethod(
                method="unknown", realpath=real, npm_prefix=None
            )
    if mutate:
        c._persist_install_method_to_state(method)
    return method


def _persist_install_method_to_state(method: InstallMethod) -> None:
    """Replace the ``install`` block in update-state.json with a fresh
    detection result, preserving every other field (e.g. ``latest_version``
    written by the version-check pipeline in Task 3). ``current_version``
    is also re-stamped from the CHANGELOG so the running binary's
    self-version stays in sync with the install block.
    """
    c = _cctally()
    state = c._load_update_state() or {"_schema": 1}
    state["install"] = {
        "method": method.method,
        "realpath": method.realpath,
        "npm_prefix": method.npm_prefix,
        "detected_at_utc": _now_utc().isoformat(),
    }
    cur = _release_read_latest_release_version()
    if cur:
        state["current_version"] = cur[0]
    c._save_update_state(state)


def _stamp_install_success_to_state(
    installed_version: str | None,
    method: "InstallMethod | None" = None,
) -> None:
    """Stamp ``update-state.json`` with the just-installed version so the
    post-install banner predicate + dashboard auto-close fire immediately.

    Without this, both surfaces are stuck for up to ``ttl_hours`` (24h
    default): ``_do_update_check`` touched the throttle marker before
    install began, so ``_is_update_check_due`` returns False on every
    subsequent boot until the TTL expires; ``current_version`` would
    keep its pre-install value, and ``_semver_gt(latest, current)``
    stays True. Banner re-fires on every CLI command; dashboard's
    ``refreshUpdateState`` auto-close (``current === latest``) never
    matches.

    Resolution order:
      1. ``installed_version`` — caller passed an explicit ``--version``.
      2. For brew (when ``method.method == "brew"`` and no explicit
         version), ``state.latest_version`` — the freshly-probed value
         that drove the install. The running process's ``CHANGELOG_PATH``
         resolved to the OLD Cellar at boot, so a CHANGELOG read here
         returns the pre-upgrade version and would stamp the wrong
         ``current_version`` until the next dashboard self-heal (up to
         30 min) or the next CLI invocation. The stale-probe regression
         that pushed CHANGELOG ahead of ``latest_version`` (1.6.0-after-
         installing-1.6.3) does not apply on the brew path: brew's
         install probe ran inside the user's just-issued
         ``cctally update``, so ``latest_version`` is current.
      3. Freshly-installed CHANGELOG (``_release_read_latest_release_version``).
         For npm the install overwrites ``CHANGELOG.md`` in place, so
         this read inside the same Python process returns the just-
         installed version. Skipped on brew for the reason above.
      4. ``state.latest_version`` — last resort, also covers the npm
         path when CHANGELOG is unreadable.
    """
    c = _cctally()
    state = c._load_update_state() or {"_schema": 1}
    cur = installed_version
    if cur is None and method is not None and method.method == "brew":
        # Brew: prefer the cached probe (just observed by `cctally
        # update`) over CHANGELOG, which reads from the OLD Cellar.
        cur = state.get("latest_version")
    if cur is None:
        fresh = _release_read_latest_release_version()
        if fresh:
            cur = fresh[0]
    if cur is None:
        cur = state.get("latest_version")
    if cur:
        state["current_version"] = cur
        state["last_install_success_at_utc"] = _now_utc().isoformat()
        c._save_update_state(state)


def _self_heal_current_version() -> None:
    """Reconcile ``update-state.json``'s ``current_version`` with the
    running binary's CHANGELOG when they disagree.

    Closes the documented gap (memory:
    ``gotcha_update_state_cache_lies_after_version_bump``) where a user
    upgrades via ``npm install -g cctally@latest`` (or any out-of-band
    path that bypasses ``cctally update``) and ``current_version``
    stays frozen on the pre-upgrade value until the next TTL probe
    fires (24h default). The dashboard's brand-version label and the
    CLI banner predicate both read ``current_version``, so users see
    a stale "you're on <old>" indefinitely.

    Best-effort: any failure — state missing/corrupt, CHANGELOG
    unreadable, save fails — is silently swallowed. The caller is in
    a post-command hook and must never break the parent command.

    Why not bootstrap when state is missing: a ``None`` state means no
    update probe has ever run, so we have no ``latest_version`` /
    ``install`` block to seed alongside ``current_version``. Writing a
    partial state would mask the missing-probe condition that
    ``_check_safety_update_state`` and the doctor report rely on; the
    next ``_do_update_check`` creates the file fully.

    Dev-clone guard (issue #42): when ``CHANGELOG_PATH``'s parent
    contains a ``.git/`` directory, the running binary is a development
    checkout, not the globally-installed one. The CHANGELOG describes
    the working tree (e.g. a release-cut Phase 1 stamp), so stamping
    the global state from it would corrupt ``current_version`` to a
    version that is NOT what is installed. Production tarballs (npm
    tar, brew archive) never ship ``.git/``, so this heuristic only
    ever skips dev clones; legitimate out-of-band upgrades on npm/brew
    still self-heal as before.
    """
    c = _cctally()
    try:
        if (c.CHANGELOG_PATH.parent / ".git").exists():
            return
        fresh = _release_read_latest_release_version()
        if fresh is None:
            return
        running = fresh[0]
        state = c._load_update_state()
        if state is None:
            return
        if state.get("current_version") == running:
            return
        state["current_version"] = running
        c._save_update_state(state)
    except Exception:
        pass


# === Update subcommand: version-check pipeline (spec §3) ====================
# Per-vector parsers, TTL gate, and the chokepoint `_do_update_check` that
# touches the throttle marker FIRST (crash safety) before attempting any
# remote fetch. Failures preserve the prior state's `latest_version` so the
# banner predicate can still fire on the last-known-good value.

# Priority regex chain for `_check_brew_latest_version`. First match wins:
#   1. Explicit `version "X.Y.Z"` line (homebrew's preferred form).
#   2. Archive URL `/vX.Y.Z[-prerelease.N].tar` (auto-archive form).
#   3. Tag form `tag: "[v]X.Y.Z"` (occasionally seen in head/url blocks).
_BREW_VERSION_RE_LIST = (
    re.compile(r'^\s*version\s+"([^"]+)"\s*$', re.MULTILINE),
    re.compile(
        r'url\s+"[^"]*/v(\d+\.\d+\.\d+(?:-[a-zA-Z][a-zA-Z0-9-]*\.\d+)?)\.tar',
        re.MULTILINE,
    ),
    re.compile(
        r'tag:\s*"v?(\d+\.\d+\.\d+(?:-[a-zA-Z][a-zA-Z0-9-]*\.\d+)?)"',
        re.MULTILINE,
    ),
)


def _update_user_agent() -> str:
    """User-Agent for `_fetch_url` HTTP requests.

    Format: ``cctally-update-check/<version>``. Sources the version from
    the CHANGELOG (same chokepoint as every other "what version am I"
    callsite); falls back to ``"dev"`` for pre-release / unstamped trees.
    """
    cur = _release_read_latest_release_version()
    ver = cur[0] if cur else "dev"
    return f"cctally-update-check/{ver}"


def _fetch_url(url: str, *, timeout: float | None = None) -> tuple[int, bytes]:
    """Stdlib urllib HTTP GET. Raises typed exceptions on failure.

    Returns ``(status_code, body_bytes)`` on success. Maps urllib failures
    to the four `UpdateCheck*` exception types so callers can distinguish
    "rate-limited (try again later)" from "HTTP fetch failed (treat as
    last-known-good)" from "DNS / network down".
    """
    c = _cctally()
    if timeout is None:
        timeout = c.UPDATE_NETWORK_TIMEOUT_S
    req = urllib.request.Request(url, headers={
        "User-Agent": _update_user_agent(),
        "Accept": "*/*",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return (resp.status, resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise UpdateCheckRateLimited(str(e))
        raise UpdateCheckHTTPError(f"HTTP {e.code}: {e}")
    except (urllib.error.URLError, TimeoutError) as e:
        # URLError covers connection-setup failures; TimeoutError
        # (socket.timeout's alias since 3.10) covers stalls during
        # resp.read() — that path raises directly through http.client
        # without urllib wrapping. Both must funnel to
        # UpdateCheckNetworkError so _do_update_check translates them
        # into check_status="fetch_failed" instead of letting a slow
        # registry crash --check with an uncaught traceback.
        raise UpdateCheckNetworkError(str(e))


def _check_npm_latest_version() -> str:
    """Fetch the npm-registry `latest` JSON and return its `version` field.

    Endpoint: :data:`UPDATE_NPM_REGISTRY_URL` (env-overridable via
    ``CCTALLY_TEST_UPDATE_NPM_URL`` for fixture testing). JSON decode
    errors and missing-key errors raise :class:`UpdateCheckParseError`.
    """
    c = _cctally()
    status, body = c._fetch_url(c.UPDATE_NPM_REGISTRY_URL)
    try:
        data = json.loads(body.decode("utf-8"))
        return data["version"]
    except (json.JSONDecodeError, KeyError, UnicodeDecodeError) as e:
        raise UpdateCheckParseError(f"npm registry parse failed: {e}")


def _check_brew_latest_version() -> str:
    """Fetch the brew formula raw blob and extract the version.

    Endpoint: :data:`UPDATE_BREW_FORMULA_URL`. Applies
    :data:`_BREW_VERSION_RE_LIST` in priority order; first match wins.
    No regex matches → :class:`UpdateCheckParseError`.
    """
    c = _cctally()
    status, body = c._fetch_url(c.UPDATE_BREW_FORMULA_URL)
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as e:
        raise UpdateCheckParseError(f"brew formula decode failed: {e}")
    for pattern in _BREW_VERSION_RE_LIST:
        m = pattern.search(text)
        if m:
            return m.group(1)
    raise UpdateCheckParseError("brew formula version not found")


def _is_update_check_due(config: dict) -> bool:
    """TTL gate (spec §3.4).

    Reads ``update.check.enabled`` (default True) and
    ``update.check.ttl_hours`` (default :data:`UPDATE_DEFAULT_TTL_HOURS`)
    from the config. Returns False if disabled. Returns True if the
    throttle marker (:data:`UPDATE_CHECK_LAST_FETCH_PATH`) is missing.
    Otherwise: ``(now - mtime) >= ttl * 3600``.
    """
    c = _cctally()
    check_cfg = (config.get("update", {}) or {}).get("check", {}) or {}
    enabled = check_cfg.get("enabled", True)
    if not enabled:
        return False
    ttl_hours = check_cfg.get("ttl_hours", c.UPDATE_DEFAULT_TTL_HOURS)
    try:
        mtime = c.UPDATE_CHECK_LAST_FETCH_PATH.stat().st_mtime
    except FileNotFoundError:
        return True
    return (time.time() - mtime) >= ttl_hours * 3600


def _do_update_check() -> None:
    """Single chokepoint for a version-check fetch (spec §3.5).

    Touches the throttle marker FIRST (crash-safety: if the process
    dies mid-fetch, we still won't refetch for the full TTL window —
    avoids hammering the registry on a flapping host). Then resolves
    install method, ensures `current_version` is stamped from CHANGELOG,
    preserves prior `latest_version` if any, and dispatches to the
    per-vector check by `method.method`. On success: write
    `check_status="ok"` + `latest_version_url`. On failure: map the
    typed exception to a `check_status` enum (`rate_limited` /
    `fetch_failed` / `parse_failed`); never lose the prior
    `latest_version`. State is saved unconditionally on the way out.
    """
    c = _cctally()
    # Touch marker FIRST — crash safety: a dead process mid-fetch must
    # not trigger another fetch within the TTL window.
    c.UPDATE_CHECK_LAST_FETCH_PATH.parent.mkdir(parents=True, exist_ok=True)
    c.UPDATE_CHECK_LAST_FETCH_PATH.touch()

    method = c._detect_install_method(mutate=True)

    state = c._load_update_state() or {"_schema": 1}
    cur = _release_read_latest_release_version()
    if cur:
        state["current_version"] = cur[0]
    # Preserve prior `latest_version`; default to current_version if
    # nothing was ever recorded (so banner predicate has a comparable).
    state.setdefault("latest_version", state.get("current_version"))
    state["checked_at_utc"] = _now_utc().isoformat()
    state["check_error"] = None

    try:
        if method.method == "npm":
            latest = c._check_npm_latest_version()
            state["latest_version"] = latest
            state["source"] = "npm-registry"
        elif method.method == "brew":
            latest = c._check_brew_latest_version()
            state["latest_version"] = latest
            state["source"] = "github-formula"
        else:
            # Unknown install method — no remote check possible
            # (manual-fallback bucket per §2.4). Reset `latest_version`
            # to `current_version` so the banner predicate's
            # `_semver_gt(lat, cur)` returns False; preserving a prior
            # npm/brew latest here would advertise an update that
            # `cctally update` cannot apply (install method is now
            # unknown). The setdefault above is insufficient because
            # state may already carry a `latest_version` from an
            # earlier npm/brew install before the user switched to a
            # source checkout. Same suppression flows to the dashboard
            # amber badge, which reads `latest_version` directly.
            state["latest_version"] = state.get("current_version")
            state["check_status"] = "unavailable"
            c._save_update_state(state)
            return
        # Success: build the public-mirror release tag URL.
        state["latest_version_url"] = (
            f"https://github.com/{c.PUBLIC_REPO}/releases/tag/v{state['latest_version']}"
        )
        state["check_status"] = "ok"
    except UpdateCheckRateLimited as e:
        state["check_status"] = "rate_limited"
        state["check_error"] = str(e)[:200]
    except (UpdateCheckNetworkError, UpdateCheckHTTPError) as e:
        state["check_status"] = "fetch_failed"
        state["check_error"] = str(e)[:200]
    except UpdateCheckParseError as e:
        state["check_status"] = "parse_failed"
        state["check_error"] = str(e)[:200]
    finally:
        c._save_update_state(state)


def _spawn_background_update_check() -> None:
    """Fire-and-forget the hidden `_update-check` worker.

    Detached `subprocess.Popen` with `start_new_session=True` so a
    parent exit (the user closes the shell) doesn't propagate SIGHUP
    to the child. stdin/stdout/stderr are all `/dev/null` so the child
    can't accidentally pollute the parent's terminal. Exceptions are
    swallowed: a failed spawn must not break the parent command.
    """
    try:
        subprocess.Popen(
            [sys.executable, os.path.realpath(sys.argv[0]), "_update-check"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except Exception:
        # Fire-and-forget: never let a spawn failure propagate.
        pass


def cmd_update_check_internal(args) -> int:
    """Hidden ``_update-check`` subcommand handler (spec §3.6).

    The detached-refresh worker — not user-facing. Logs lifecycle
    events to ``update.log`` and rotates if needed. Always returns 0
    (any error is logged but the process exits cleanly so the parent
    spawn-and-forget contract holds).
    """
    c = _cctally()
    # Ensure APP_DIR exists so log + state writes succeed on first run.
    c.UPDATE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(c.UPDATE_LOG_PATH, "a", encoding="utf-8") as log_fd:
            _log_update_event(log_fd, "CHECK_START")
            c._do_update_check()
            _log_update_event(log_fd, "CHECK_EXIT", rc=0)
    except Exception as e:
        try:
            with open(c.UPDATE_LOG_PATH, "a", encoding="utf-8") as log_fd:
                _log_update_event(log_fd, "CHECK_EXIT", rc=1, error=str(e)[:200])
        except Exception:
            pass
    c._rotate_update_log_if_needed()
    return 0


# === User-facing `cctally update` (spec §4) ===
# `cmd_update` routes by mode flag. Mode flags are mutually exclusive
# (argparse enforces it; the dispatcher's redundant check is defense in
# depth for programmatic callers and a clearer error message). The
# install path is staged across two tasks: Task 4 lands the validation
# gates and the user-mode `--check` rendering, then raises
# NotImplementedError for actual execution. Task 5 fills in execvp +
# streaming.

# Sentinel for `--skip` with no positional argument — argparse `const`
# doesn't accept `None` (collides with the absent-flag default). At
# dispatch time the sentinel is replaced with `state.latest_version`.
SKIP_USE_STATE_LATEST = "_USE_STATE_LATEST"


def _format_update_command(method: str, version: str | None) -> str:
    """One-line shell recipe used by both --check renderers and the
    install-path manual fallback. Brew has no versioned formulae, so
    the version arg is ignored there (callers gate it earlier)."""
    if method == "brew":
        return "brew update --quiet && brew upgrade cctally"
    if method == "npm":
        v = version if version else "latest"
        return f"npm install -g cctally@{v}"
    return ""


def _prerelease_note(current: str) -> str | None:
    """Spec §1.8 — prerelease users get a one-shot informational note in
    `--check` output. Returns the canned two-line message verbatim per
    spec when `current` looks like a prerelease (`X.Y.Z-id.N` form), else
    None. Wording is exact-string contract — tests pin it."""
    if "-" not in current:
        return None
    return (
        f"You're on prerelease {current}; this banner suggests stable.\n"
        "To track prereleases, manage manually: npm install -g cctally@next"
    )


def _format_update_check_json(
    state: dict[str, Any], suppress: dict[str, Any]
) -> dict[str, Any]:
    """JSON shape for `cctally update --check --json` (spec §4.4)."""
    c = _cctally()
    cur = state.get("current_version")
    lat = state.get("latest_version")
    method = (state.get("install") or {}).get("method", "unknown")
    skipped = lat in suppress.get("skipped_versions", []) if lat else False
    in_remind_window = False
    remind = suppress.get("remind_after")
    if remind is not None and lat is not None:
        try:
            if not c._semver_gt(lat, remind["version"]):
                until = dt.datetime.fromisoformat(remind["until_utc"])
                if _now_utc() < until:
                    in_remind_window = True
        except (KeyError, ValueError):
            pass
    available = False
    if cur and lat:
        try:
            available = (
                c._semver_gt(lat, cur)
                and not skipped
                and not in_remind_window
            )
        except ValueError:
            available = False
    return {
        "_schema": 1,
        "current_version": cur,
        "latest_version": lat,
        "available": available,
        "method": method,
        "update_command": c._format_update_command(method, None),
        "release_notes_url": state.get("latest_version_url"),
        "check_status": state.get("check_status"),
        "check_error": state.get("check_error"),
        "checked_at_utc": state.get("checked_at_utc"),
        "suppress": {
            "skipped": skipped,
            "remind_after_utc": (
                remind.get("until_utc") if isinstance(remind, dict) else None
            ),
        },
        "prerelease_note": c._prerelease_note(cur) if cur else None,
    }


_UPDATE_METHOD_HUMAN_LABEL = {
    "brew": "Homebrew",
    "npm": "npm",
    "unknown": "unknown",
}


def _format_update_check_human(
    state: dict[str, Any], suppress: dict[str, Any]
) -> str:
    """Multi-line plaintext block for `cctally update --check` (spec §4.4).

    Two-space-column table layout: every label left-padded to width 10
    (`Will run` is the longest at 8 chars + 2-space gutter). Method row
    appends `  (auto-detected)` per spec example. Up-to-date / unknown
    variants append a fallback line below the table.
    """
    c = _cctally()
    cur = state.get("current_version") or "unknown"
    lat = state.get("latest_version") or "unknown"
    method = (state.get("install") or {}).get("method", "unknown")
    url = state.get("latest_version_url")
    status = state.get("check_status")
    err = state.get("check_error")
    cooked_available = False
    if state.get("current_version") and state.get("latest_version"):
        try:
            cooked_available = c._semver_gt(lat, cur) and \
                lat not in suppress.get("skipped_versions", [])
        except ValueError:
            cooked_available = False

    method_label = _UPDATE_METHOD_HUMAN_LABEL.get(method, method)
    lines = [
        f"{'Current':<10}{cur}",
        f"{'Latest':<10}{lat}",
        f"{'Method':<10}{method_label}  (auto-detected)",
    ]
    will_run = c._format_update_command(method, None)
    if will_run:
        lines.append(f"{'Will run':<10}{will_run}")
    if url:
        lines.append(f"{'Notes':<10}{url}")
    if status and status != "ok":
        status_value = status + (f" ({err})" if err else "")
        lines.append(f"{'Status':<10}{status_value}")
    lines.append("")
    if method == "unknown":
        # No remote check is possible for source / dev installs; render
        # the manual fallback rather than the "you're up to date" lie.
        lines.append(
            "Automatic update unavailable for this install. Visit "
            f"{url or 'https://github.com/' + c.PUBLIC_REPO + '/releases'} "
            "to install manually."
        )
    elif cooked_available:
        lines.append("Run `cctally update` to install.")
    else:
        lines.append("You're up to date.")
    note = c._prerelease_note(cur)
    if note:
        lines.append("")
        lines.append(note)
    return "\n".join(lines)


def _do_update_skip(version_arg: str) -> int:
    """`cctally update --skip [VERSION]` — record a skipped version."""
    c = _cctally()
    if version_arg == SKIP_USE_STATE_LATEST:
        state = c._load_update_state()
        if state is None or not state.get("latest_version"):
            print(
                "cctally update: no version in cache to skip; run "
                "`cctally update --check` first",
                file=sys.stderr,
            )
            return 1
        version = state["latest_version"]
    else:
        if not c._SEMVER_RE.match(version_arg):
            print(
                f"cctally update: invalid version {version_arg!r} "
                "(expected X.Y.Z[-id.N])",
                file=sys.stderr,
            )
            return 2
        version = version_arg
    suppress = c._load_update_suppress()
    skipped = list(suppress.get("skipped_versions", []))
    if version not in skipped:
        skipped.append(version)
    suppress["skipped_versions"] = skipped
    suppress.setdefault("_schema", 1)
    c._save_update_suppress(suppress)
    print(
        f"Skipped cctally {version}. You won't be reminded about this version."
    )
    return 0


def _do_update_remind_later(days: int) -> int:
    """`cctally update --remind-later [DAYS]` — defer banner for N days."""
    c = _cctally()
    if not (1 <= days <= 365):
        print(
            f"cctally update: --remind-later must be 1..365 (got {days})",
            file=sys.stderr,
        )
        return 2
    state = c._load_update_state()
    if state is None or not state.get("latest_version"):
        print(
            "cctally update: no version in cache to defer; run "
            "`cctally update --check` first",
            file=sys.stderr,
        )
        return 1
    until = (_now_utc() + dt.timedelta(days=days)).isoformat()
    suppress = c._load_update_suppress()
    suppress["remind_after"] = {
        "version": state["latest_version"],
        "until_utc": until,
    }
    suppress.setdefault("_schema", 1)
    c._save_update_suppress(suppress)
    print(
        f"Will remind in {days} day{'s' if days != 1 else ''} "
        "(or sooner if a newer version drops)."
    )
    return 0


_UPDATE_CHECK_JSON_UNAVAILABLE_ENVELOPE = {
    "_schema": 1,
    "current_version": None,
    "latest_version": None,
    "available": False,
    "method": "unknown",
    "update_command": None,
    "release_notes_url": None,
    "check_status": "unavailable",
    "check_error": "state unavailable",
    "checked_at_utc": None,
    "suppress": {"skipped": False, "remind_after_utc": None},
    "prerelease_note": None,
}


def _do_update_check_user(*, force: bool, output_json: bool) -> int:
    """`cctally update --check` — user-facing version-check render.

    `_do_update_check()` translates known failure modes (network,
    parse, rate-limit) into `check_status` fields on the state file
    via its own internal try/except (Task 3, spec §3.5); any unexpected
    exception that escapes is a real bug and is left to surface in the
    outer error log. The post-command banner hook in `main()` already
    isolates banner failures.

    Refresh gate matches the user-facing docs (`docs/commands/update.md`):
    `--check` refreshes when TTL has elapsed; `--force` bypasses the TTL
    gate to refresh even on a fresh cache. Synchronous refresh here also
    pre-empts the post-command background spawn — `_do_update_check`
    touches `update-check.last-fetch` first, so `_is_update_check_due`
    returns False by the time the hook runs.
    """
    c = _cctally()
    config = load_config()
    if force or c._is_update_check_due(config):
        c._do_update_check()
    state = c._load_update_state()
    if state is None:
        # Still nothing on disk — try once even with a fresh TTL marker
        # so the first invocation isn't a content-free "state unavailable".
        c._do_update_check()
        state = c._load_update_state()
    if state is None:
        if output_json:
            # Emit a parseable minimal envelope so JSON consumers always
            # get a payload; rc stays 0 (best-effort, matches the
            # `cmd_refresh_usage` precedent that network failures are
            # not user-actionable errors). Spec §4.4.
            print(
                json.dumps(_UPDATE_CHECK_JSON_UNAVAILABLE_ENVELOPE, indent=2)
            )
            return 0
        print("cctally update: state unavailable", file=sys.stderr)
        return 0
    suppress = c._load_update_suppress()
    if output_json:
        print(json.dumps(c._format_update_check_json(state, suppress), indent=2))
    else:
        print(c._format_update_check_human(state, suppress))
    return 0


def _preflight_install(method: InstallMethod, version: str | None) -> None:
    """Validate the install plan before any subprocess runs (spec §5.1).

    Ordered checks (each raises and short-circuits the rest):
      1. method != "unknown" — manual-fallback bucket per §2.4.
      2. version (if not None) matches `_SEMVER_RE` — `X.Y.Z` or
         `X.Y.Z-prerelease`.
      3. (method, version) compatibility — brew has no versioned
         formulae; pinned-version installs must be done manually.
      4. npm-only: the `<prefix>/bin` directory must be writable; if
         not, surface the sudo / `npm config set prefix` recipes
         instead of letting npm fail with EACCES inside the run.

    Raises :class:`UpdateValidationError` for input-validation failures
    (rc=2 at the boundary): invalid --version syntax, --version+brew
    combo. Raises :class:`UpdateError` for environment / runtime
    failures (rc=1 at the boundary): unknown install method, npm
    prefix not writable.

    Brew preflight is intentionally a no-op beyond the version-combo
    check (codex review #2): homebrew installs into ``libexec/bin/``,
    so ``realpath`` lands inside the keg, not the brew bin prefix; brew
    has its own permission model and ``brew doctor`` is the diagnostic
    users already know.
    """
    c = _cctally()
    if method.method == "unknown":
        raise UpdateError(
            "Install method is 'unknown' — automatic update unavailable.\n"
            "If you installed from source: cd <your cctally repo> && git pull && bin/symlink"
        )
    if version is not None and not c._SEMVER_RE.match(version):
        raise UpdateValidationError(
            f"Invalid version: {version!r} (expected X.Y.Z or X.Y.Z-id.N)"
        )
    if method.method == "brew" and version is not None:
        raise UpdateValidationError(
            "Pinned-version install is not supported on Homebrew "
            "(no versioned formulae).\n"
            "To install a specific version manually:\n"
            f"  brew uninstall cctally\n"
            f"  brew install https://github.com/{c.PUBLIC_REPO}/releases/download/v{version}/cctally-{version}.tar.gz"
        )
    if method.method == "npm":
        prefix_bin = os.path.join(method.npm_prefix, "bin")
        if not os.access(prefix_bin, os.W_OK):
            raise UpdateError(
                f"npm prefix '{prefix_bin}' is not writable.\n"
                f"Run with sudo: sudo npm install -g cctally@{version or 'latest'}\n"
                "Or relocate: npm config set prefix ~/.npm-global"
            )
    # brew: NO preflight beyond the --version combo check above
    # (codex review #2 amendment to spec §5.1).


def _build_update_steps(
    method: InstallMethod, version: str | None
) -> list[tuple[str, list[str]]]:
    """Build the ordered list of subprocess steps for an install plan.

    Each step is ``(human_name, argv)`` where ``human_name`` is the
    label rendered in dry-run output and the dashboard live-stream
    modal, and ``argv`` is the list passed to ``subprocess.Popen`` (no
    shell). Brew is two steps (``brew update`` then ``brew upgrade
    cctally``) per spec §5.2 + Q6a — splitting them gives diagnostic
    clarity (a stale tap manifesting as a hung ``brew update`` is
    distinguishable from an ``upgrade`` failure). npm is one step.
    """
    if method.method == "brew":
        return [
            ("brew update", ["brew", "update", "--quiet"]),
            ("brew upgrade cctally", ["brew", "upgrade", "cctally"]),
        ]
    if method.method == "npm":
        target = f"cctally@{version}" if version else "cctally@latest"
        return [("npm install -g", ["npm", "install", "-g", target])]
    raise AssertionError(
        f"step builder called with method={method.method!r} "
        "(should have been rejected by _preflight_install)"
    )


def _run_streaming(
    cmd: list[str],
    *,
    on_stdout: Callable[[str], None],
    on_stderr: Callable[[str], None],
    log_fd,
) -> int:
    """Run ``cmd``, line-buffer stdout/stderr to callbacks, append to log.

    Two-thread pump (one per stream) → callbacks + log lines. Each log
    line is ``<iso-utc> <STREAM> <raw-line>`` so a grep for the stream
    label still recovers the chronological order even when stdout and
    stderr interleave at sub-line resolution. ``proc.wait()`` is the
    synchronization point; the pump threads are daemons so a crash in
    the parent doesn't leave them lingering.

    Used by:
      - the CLI install path (Task 5; this file) where ``on_stdout`` /
        ``on_stderr`` print to the parent's stdout/stderr;
      - the dashboard ``UpdateWorker`` thread (Task 6) where the
        callbacks push lines into a per-stream ring buffer for SSE.

    Stdin is inherited from the parent — the wrapped commands
    (``brew update``, ``npm install -g``) take no input; piping
    ``DEVNULL`` would just add a syscall.
    """
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=1,
        text=True,
    )

    def pump(stream, cb, label):
        for line in stream:
            cb(line.rstrip("\n"))
            if log_fd is not None:
                log_fd.write(f"{_now_utc().isoformat()} {label} {line}")
                log_fd.flush()
        stream.close()

    t_out = threading.Thread(
        target=pump, args=(proc.stdout, on_stdout, "STDOUT"), daemon=True
    )
    t_err = threading.Thread(
        target=pump, args=(proc.stderr, on_stderr, "STDERR"), daemon=True
    )
    t_out.start()
    t_err.start()
    proc.wait()
    t_out.join()
    t_err.join()
    return proc.returncode


def _do_update_install(
    *, version: str | None, dry_run: bool, output_json: bool
) -> int:
    """`cctally update` (no mode flag) — install execution (spec §5).

    Task-4 inline gates moved into :func:`_preflight_install`. Real
    install: acquire lock → log INSTALL_START → run each step (logging
    STEP_START/STEP_EXIT), bail on the first non-zero rc → log
    INSTALL_SUCCESS → release lock + rotate log in finally.

    Dry-run path passes ``mutate=False`` to detection (codex review
    fix #4), prints "Would run: ..." (or one JSON-line per step) for
    each planned step, and exits 0 without touching the lock or
    running any subprocesses.

    Raises :class:`UpdateError` (rc=1 at boundary) for unknown method
    / write-perm-denied; :class:`UpdateValidationError` (rc=2) for
    invalid --version / --version+brew. The boundary distinction is
    enforced by :func:`cmd_update`'s try/except below.
    """
    c = _cctally()
    method = c._detect_install_method(mutate=not dry_run)
    c._preflight_install(method, version)
    steps = c._build_update_steps(method, version)
    if dry_run:
        for name, cmd in steps:
            if output_json:
                print(json.dumps({"step": name, "would_run": cmd}))
            else:
                quoted = " ".join(shlex.quote(c2) for c2 in cmd)
                print(f"Would run: {quoted}")
        return 0
    c.UPDATE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = c._acquire_update_lock()
    try:
        with open(c.UPDATE_LOG_PATH, "a", encoding="utf-8") as log_fd:
            _log_update_event(log_fd, "INSTALL_START", method=method.method)
            for step_name, cmd in steps:
                _log_update_event(log_fd, "STEP_START", name=step_name)
                rc = c._run_streaming(
                    cmd,
                    on_stdout=lambda line: print(line, file=sys.stdout, flush=True),
                    on_stderr=lambda line: print(line, file=sys.stderr, flush=True),
                    log_fd=log_fd,
                )
                _log_update_event(log_fd, "STEP_EXIT", name=step_name, rc=rc)
                if rc != 0:
                    return 1
            _log_update_event(log_fd, "INSTALL_SUCCESS")
            c._stamp_install_success_to_state(version, method)
            return 0
    finally:
        c._release_update_lock(lock_fd)
        c._rotate_update_log_if_needed()


# === Dashboard execvp re-entry (spec §5.7) ===
# ORIGINAL_SYS_ARGV / ORIGINAL_ENTRYPOINT are captured at dashboard
# server boot in cmd_dashboard (in bin/cctally, written via
# ``global ORIGINAL_SYS_ARGV, ORIGINAL_ENTRYPOINT`` so the running
# binary's view of argv survives the in-place execvp). They stay
# defined in bin/cctally so cmd_dashboard's write site is unchanged
# and tests that ``monkeypatch.setitem(ns, "ORIGINAL_SYS_ARGV", …)``
# propagate to this read site.
#
# _resolve_execvp_target uses them to return (entrypoint, exec_argv)
# for os.execvp:
#   - npm: entrypoint = <prefix>/bin/cctally → Node shim, which
#     re-resolves CCTALLY_PYTHON before re-spawning Python (so a
#     custom interpreter setting survives the restart).
#   - brew: entrypoint = <brew>/bin/cctally → symlink into the
#     post-upgrade Python script with its rewritten shebang.
#   - Fallback when shutil.which("cctally") returned None: use
#     sys.argv[0] directly. Loses the npm shim layer; we accept the
#     degraded edge case rather than guess.


def _resolve_execvp_target() -> tuple[str, list[str]]:
    """Return (entrypoint, exec_argv) per spec §5.7.

    Re-enters the npm shim by execvp'ing the PATH-resolved ``cctally``
    (Node shim for npm, brew symlink for brew). Falls back to
    ``sys.argv[0]`` only when ``shutil.which`` returned ``None`` at
    dashboard boot (rare absolute-path invocation).
    """
    c = _cctally()
    if c.ORIGINAL_ENTRYPOINT is not None:
        return (
            c.ORIGINAL_ENTRYPOINT,
            [c.ORIGINAL_ENTRYPOINT, *c.ORIGINAL_SYS_ARGV[1:]],
        )
    return (c.ORIGINAL_SYS_ARGV[0], list(c.ORIGINAL_SYS_ARGV))


class UpdateWorker:
    """Single-slot dashboard-side update orchestrator (spec §5.6).

    A single instance lives on the dashboard server (created in
    cmd_dashboard, exposed as the module-level ``_UPDATE_WORKER``).
    ``start()`` returns ``(True, run_id)`` on accept and
    ``(False, current_run_id)`` when a run is already in progress —
    serializes concurrent button clicks without taking the install lock
    on the rejected path. ``_run`` runs preflight → lock → streamed
    steps → execvp on success / error_event on failure / done on
    non-zero subprocess exit. The ``released`` flag enforces the
    idempotent-release contract from spec §5.6.1: success path releases
    pre-execvp and skips the finally release; pre-execvp failure path
    releases in finally.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current_id: "str | None" = None
        # run_id -> queue.Queue of event dicts. Each subscriber drains
        # via ``stream(run_id)``; the worker thread enqueues via
        # ``_emit``. A single subscriber per run is the dashboard
        # contract; multi-subscriber broadcast is out of scope. The
        # producer (``_run``) intentionally does NOT pop its entry —
        # that would race a late consumer (#32): the worker thread can
        # complete its finally before the consumer enters ``stream()``,
        # leaving the consumer to look up a missing key. Cleanup
        # ownership now belongs to ``stream()``'s finally; if no
        # consumer ever subscribes, ``start()`` reaps stale entries on
        # the next run.
        self._streams: dict[str, "queue.Queue"] = {}

    def start(self, version: "str | None") -> tuple[bool, str]:
        """Begin a run. Returns (accepted, run_id).

        ``accepted=False`` when another run is in progress; the
        returned ``run_id`` is the in-progress one (so the caller can
        surface it as ``run_id_in_progress`` to the client).
        """
        with self._lock:
            if self._current_id is not None:
                return (False, self._current_id)
            # Reap any stale entries from prior no-consumer runs. Safe
            # under the lock: ``_current_id is None`` here, so no live
            # stream() generator holds a reference into the dict by
            # run_id (only by local-variable q ref, which survives the
            # pop).
            self._streams.clear()
            run_id = secrets.token_hex(8)
            self._current_id = run_id
            self._streams[run_id] = queue.Queue()
        threading.Thread(
            target=self._run, args=(run_id, version), daemon=True,
            name="cctally-update-worker",
        ).start()
        return (True, run_id)

    def status(self) -> dict:
        """Return ``{"current_run_id": <run_id|None>}`` for /api/update/status."""
        with self._lock:
            return {"current_run_id": self._current_id}

    def _emit(self, run_id: str, event: dict) -> None:
        q = self._streams.get(run_id)
        if q is not None:
            q.put(event)

    def stream(self, run_id: str):
        """Generator yielding events for the given run_id.

        Yields a ``{"type": "heartbeat"}`` event every 15 s of idle so
        the SSE proxy / EventSource keep-alive stays warm. Closes
        (returns) on the terminal events: ``execvp`` (success path),
        ``error_event`` (preflight or other UpdateError), ``done``
        (non-zero subprocess exit). Yields nothing and returns
        immediately for unknown run_ids — the HTTP handler then closes
        the SSE connection.
        """
        q = self._streams.get(run_id)
        if q is None:
            return
        try:
            while True:
                try:
                    ev = q.get(timeout=15)
                except queue.Empty:
                    yield {"type": "heartbeat"}
                    continue
                yield ev
                if ev["type"] in ("execvp", "error_event", "done"):
                    return
        finally:
            # Only reap when the worker is no longer the active producer
            # for this run_id. A mid-run modal close unwinds this
            # generator while ``_current_id == run_id`` and ``_run`` is
            # still emitting — popping here would silently drop those
            # events, and a modal reopen (slice.runId is preserved per
            # spec §6) would re-subscribe against a missing queue.
            # Cleanup still happens on the first ``stream()`` exit AFTER
            # the worker terminates (its finally clears _current_id), or
            # via ``start()``'s reap on the next run.
            with self._lock:
                if self._current_id != run_id:
                    self._streams.pop(run_id, None)

    def _run(self, run_id: str, version: "str | None") -> None:
        c = _cctally()
        lock_fd = None
        released = False  # idempotent-release guard per §5.6.1
        log_fd = None
        try:
            method = c._detect_install_method(mutate=True)
            c._preflight_install(method, version)
            c.UPDATE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            lock_fd = c._acquire_update_lock()
            log_fd = open(c.UPDATE_LOG_PATH, "a", encoding="utf-8")
            _log_update_event(log_fd, "INSTALL_START", method=method.method)
            for step_name, cmd in c._build_update_steps(method, version):
                self._emit(run_id, {"type": "step", "name": step_name})
                _log_update_event(log_fd, "STEP_START", name=step_name)
                rc = c._run_streaming(
                    cmd,
                    on_stdout=lambda line, rid=run_id: self._emit(
                        rid, {"type": "stdout", "data": line}
                    ),
                    on_stderr=lambda line, rid=run_id: self._emit(
                        rid, {"type": "stderr", "data": line}
                    ),
                    log_fd=log_fd,
                )
                _log_update_event(log_fd, "STEP_EXIT", name=step_name, rc=rc)
                self._emit(run_id, {"type": "exit", "rc": rc, "step": step_name})
                if rc != 0:
                    self._emit(run_id, {"type": "done", "success": False})
                    return
            _log_update_event(log_fd, "INSTALL_SUCCESS")
            c._stamp_install_success_to_state(version, method)
            entrypoint, exec_argv = c._resolve_execvp_target()
            self._emit(run_id, {"type": "execvp", "argv": exec_argv})
            try:
                log_fd.close()
            finally:
                log_fd = None
            # 0.5 s breathing room so the SSE pump flushes the final
            # ``execvp`` event to the browser before we hand the
            # process over to the new image. If the browser misses it
            # the polling fallback (/api/update/status) covers reentry.
            time.sleep(0.5)
            c._release_update_lock(lock_fd)
            released = True
            os.execvp(entrypoint, exec_argv)
        except UpdateError as e:
            self._emit(run_id, {"type": "error_event", "message": str(e)})
        except Exception as e:
            self._emit(
                run_id, {"type": "error_event", "message": f"unexpected: {e!r}"}
            )
        finally:
            if log_fd is not None:
                try:
                    log_fd.close()
                except Exception:
                    pass
            if lock_fd is not None and not released:
                try:
                    c._release_update_lock(lock_fd)
                except Exception:
                    pass
            with self._lock:
                self._current_id = None
                # _streams[run_id] intentionally retained — see class
                # docstring. Cleanup is owned by stream()'s finally;
                # start() sweeps stale entries on the next run.


class _DashboardUpdateCheckThread(threading.Thread):
    """Dedicated update-check polling thread (spec §3.5).

    Independent of the data-sync thread so it runs even under
    ``--no-sync`` (codex review fix #5). Wakes once per
    :data:`UPDATE_DASHBOARD_CHECK_POLL_S` (30 min), consults
    :func:`_is_update_check_due`, runs :func:`_do_update_check` if so.
    The poll cadence is NOT the network-call frequency — actual TTL
    gate (default 24 h) lives in ``_is_update_check_due``. Disabling
    via ``update.check.enabled = false`` is honoured inside the gate
    so the thread becomes a no-op without needing teardown.

    After a successful check, republishes the current snapshot via the
    SSE hub so long-open dashboard tabs in ``--no-sync`` mode pick up
    the fresh ``latest_version`` written to ``update-state.json``. The
    snapshot itself is unchanged — ``snapshot_to_envelope`` re-reads
    the state file per envelope build, so a bare publish is enough to
    refresh the badge for every live subscriber.
    """

    daemon = True

    def __init__(
        self,
        stop_event: "threading.Event",
        *,
        hub: "SSEHub | None" = None,
        snapshot_ref: "_SnapshotRef | None" = None,
    ) -> None:
        super().__init__(name="cctally-update-check")
        self._stop = stop_event
        self._hub = hub
        self._ref = snapshot_ref

    def run(self) -> None:
        c = _cctally()
        while not self._stop.is_set():
            try:
                # Self-heal runs every tick (every 30 min by default),
                # NOT gated by `_is_update_check_due`'s 24h TTL. Catches
                # the case where the user upgrades the npm package
                # out-of-band (no `cctally update` invocation) — the
                # dashboard's brand-version label needs to reflect the
                # new binary without waiting up to 24h for the next
                # TTL probe. Re-publish the snapshot after a self-heal
                # write so live SSE subscribers pick up the corrected
                # `current_version` on their next envelope.
                healed_before = c._load_update_state()
                c._self_heal_current_version()
                healed_after = c._load_update_state()
                if (
                    healed_before != healed_after
                    and self._hub is not None
                    and self._ref is not None
                ):
                    snap = self._ref.get()
                    if snap is not None:
                        self._hub.publish(snap)
                config = load_config()
                if c._is_update_check_due(config):
                    c._do_update_check()
                    if self._hub is not None and self._ref is not None:
                        snap = self._ref.get()
                        if snap is not None:
                            self._hub.publish(snap)
            except Exception as e:
                # Log but never propagate — this thread must keep
                # ticking so a transient registry hiccup doesn't
                # silently disable the polling cadence for the rest
                # of the dashboard's lifetime.
                try:
                    c.UPDATE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
                    with open(c.UPDATE_LOG_PATH, "a", encoding="utf-8") as log_fd:
                        _log_update_event(
                            log_fd, "CHECK_FAILED", error=str(e)[:200]
                        )
                except Exception:
                    pass
            self._stop.wait(c.UPDATE_DASHBOARD_CHECK_POLL_S)


def cmd_update(args) -> int:
    """`cctally update` entry point — routes by mode flag (spec §4.1)."""
    c = _cctally()
    skip_arg = getattr(args, "skip", None)
    remind_arg = getattr(args, "remind_later", None)
    check_arg = getattr(args, "check", False)
    # NOTE: `args.install_version`, not `args.version` — the subparser's
    # `--version X.Y.Z` is `dest="install_version"` to avoid colliding
    # with the top-level `--version` flag handled in `main()`.
    version_arg = getattr(args, "install_version", None)
    modes = sum(bool(x) for x in [
        check_arg,
        skip_arg is not None,
        remind_arg is not None,
    ])
    if modes > 1:
        print(
            "cctally update: --check / --skip / --remind-later are "
            "mutually exclusive",
            file=sys.stderr,
        )
        return 2
    if version_arg is not None and (
        check_arg or skip_arg is not None or remind_arg is not None
    ):
        print(
            "cctally update: --version is install-mode only",
            file=sys.stderr,
        )
        return 2
    if skip_arg is not None:
        return c._do_update_skip(skip_arg)
    if remind_arg is not None:
        return c._do_update_remind_later(remind_arg)
    if check_arg:
        return c._do_update_check_user(
            force=getattr(args, "force", False),
            output_json=getattr(args, "json", False),
        )
    try:
        return c._do_update_install(
            version=version_arg,
            dry_run=getattr(args, "dry_run", False),
            output_json=getattr(args, "json", False),
        )
    except UpdateValidationError as e:
        # Input validation failure (invalid --version syntax,
        # --version+brew combo). rc=2 preserves the Task-4 contract.
        print(f"cctally update: {e}", file=sys.stderr)
        return 2
    except UpdateError as e:
        # Runtime / environment failure (unknown install method, npm
        # prefix not writable, lock contention). rc=1.
        print(f"cctally update: {e}", file=sys.stderr)
        return 1


# === Update banner (spec §4.2) =============================================


def _args_emit_json(args: argparse.Namespace) -> bool:
    """True if this command's STDOUT will be JSON.

    Subcommands declare --json with inconsistent dest names: most use
    dest="json" (default), but `diff` uses dest="emit_json". This helper
    centralizes the detection so banner routing doesn't accidentally
    corrupt JSON envelopes by missing a dest variant.

    If you add a new subcommand with a non-default --json dest, add it
    here AND consider whether the convention should be normalized.
    """
    return bool(
        getattr(args, "json", False)
        or getattr(args, "emit_json", False)
    )


def _args_emit_machine_stdout(args: argparse.Namespace) -> bool:
    """True if STDOUT is consumed programmatically (JSON, status-line, etc).

    Commands matching this predicate must NOT have any banner injected
    into their STDOUT, and stderr-routing isn't viable either (status-line
    integration is `$(cmd 2>/dev/null)` — stderr is discarded). The banner
    is suppressed entirely for these.

    Currently: status_line only — extend here if new single-line scripted
    modes are added (e.g. a future --script or --raw flag).

    JSON callers are NOT in this set — they get the banner on stderr
    (Q2 default), which scripts can grep without contaminating JSON.
    """
    return bool(getattr(args, "status_line", False))


# Update-banner suppression set — parallel to ``_BANNER_SUPPRESSED_COMMANDS``
# (migration banner) but with its own membership. ``update`` itself shouldn't
# advertise an update; ``_update-check`` is the detached-refresh worker
# (silent by contract). Other suppressions (record-usage, hook-tick, sync-week,
# cache-sync, refresh-usage, tui, db) ride the existing migration set so
# the two banners stay aligned for those commands.
_UPDATE_BANNER_EXTRA_SUPPRESSED = frozenset({"_update-check", "update"})


def _semver_gt(a: str, b: str) -> bool:
    """SemVer comparison via :func:`_release_parse_semver` + the
    SemVer-§11.4-aware sort key. ``a > b`` returns True when ``a`` is
    a strictly higher version. Raises :class:`ValueError` on either
    input being malformed (callers wrap in try/except)."""
    return _release_semver_sort_key(_release_parse_semver(a)) > \
           _release_semver_sort_key(_release_parse_semver(b))


def _compute_effective_update_available(
    state: dict[str, Any] | None,
    suppress: dict[str, Any] | None,
    now_utc: "dt.datetime",
) -> "tuple[bool, str | None]":
    """Shared core of "is there a *real* pending update?"

    Returns ``(available, reason)`` where ``reason`` is:
      - ``"missing_state"`` — current/latest unknown (no probe yet)
      - ``"no_newer"`` — latest is not strictly greater than current
      - ``"skipped"`` — user has skipped the latest version
      - ``"reminded"`` — user has deferred and the window is still active
      - ``None`` — available (warn-worthy)

    Single source of truth for both ``_should_show_update_banner`` and
    ``cctally doctor``'s ``safety.update_available`` check. Keeping this
    shared avoids the bug where doctor would advertise an update the
    banner suppresses (see review finding "Respect skipped/reminded
    updates"). Malformed ``remind_after`` fails open — matches the
    banner's pre-extraction posture: better to show a real reminder
    than to silently drop one because of a corrupt suppress file.
    """
    c = _cctally()
    if state is None:
        return False, "missing_state"
    cur = state.get("current_version")
    lat = state.get("latest_version")
    if not cur or not lat:
        return False, "missing_state"
    try:
        if not c._semver_gt(lat, cur):
            return False, "no_newer"
    except ValueError:
        return False, "no_newer"
    sup = suppress or {}
    if lat in sup.get("skipped_versions", []):
        return False, "skipped"
    remind = sup.get("remind_after")
    if remind is not None:
        try:
            # Hide while the deferral is active AND the user-pinned version
            # is still the latest. A newer drop overrides the deferral.
            if not c._semver_gt(lat, remind["version"]):
                until = dt.datetime.fromisoformat(remind["until_utc"])
                if now_utc < until:
                    return False, "reminded"
        except (KeyError, ValueError):
            # Malformed remind_after: fail-open. Better to surface a
            # real update than to silently drop it.
            pass
    return True, None


def _should_show_update_banner(
    command: str | None,
    args: argparse.Namespace,
    state: dict[str, Any] | None,
    suppress: dict[str, Any],
    config: dict[str, Any],
) -> bool:
    """Return True iff a one-line update banner should land on stderr
    after this command's output (spec §4.2).

    Composition is the key invariant: the predicate **must** delegate
    machine-mode detection to the existing helpers
    (:func:`_args_emit_json`, :func:`_args_emit_machine_stdout`) so a
    new ``--json`` dest variant or status-line flag added to any
    subcommand inherits the suppression automatically. Adding a parallel
    list here would silently regress that invariant — the spec
    amendment for Codex finding #8 codifies this.

    Semver + skipped + remind logic is delegated to
    :func:`_compute_effective_update_available` so ``cctally doctor``
    stays in lockstep with this predicate.
    """
    c = _cctally()
    if command in c._BANNER_SUPPRESSED_COMMANDS or command in _UPDATE_BANNER_EXTRA_SUPPRESSED:
        return False
    if c._args_emit_json(args):
        return False
    if c._args_emit_machine_stdout(args):
        return False
    if getattr(args, "format", None) is not None:
        return False
    if not sys.stderr.isatty():
        return False
    if not config.get("update", {}).get("check", {}).get("enabled", True):
        return False
    available, _ = c._compute_effective_update_available(state, suppress, c._now_utc())
    return available


def _format_update_banner(state: dict[str, Any]) -> str:
    """One-line stderr banner. Spec §4.2.

    Includes the dismissal recipe inline so the user never has to
    consult docs to silence it.
    """
    cur = state["current_version"]
    lat = state["latest_version"]
    return (
        f"↑ cctally {lat} available (you're on {cur}). "
        f"Run `cctally update`. Skip: cctally update --skip {lat}"
    )
