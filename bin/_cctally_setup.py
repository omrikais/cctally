"""Setup machinery for cctally (install / uninstall / status / dry-run + legacy migration).

Lazy I/O sibling: every function that drives `cctally setup` and the
legacy-bespoke-hook migration lives here. Symlink plumbing, settings.json
mutation builders (`_settings_merge_install` / `_settings_merge_uninstall`
/ `_settings_merge_unwire_legacy`), detection (status-line snippet +
legacy bespoke hooks), the prompt / decision helpers, the four
`_legacy_*` migration primitives, the four `_setup_status` /
`_setup_uninstall` / `_setup_dry_run` / `_setup_install` mode handlers,
and the `cmd_setup` entry point.

The settings.json I/O primitives (`_load_claude_settings`,
`_write_claude_settings_atomic`, `_backup_claude_settings`), `SetupError`,
`_is_cctally_hook_command`, and `cmd_repair_symlinks` now LIVE HERE
(#125 Batch E, C10 — relocated from `bin/cctally`). bin/cctally
eager-re-exports each so doctor's `except c.SetupError`, the parser's
`set_defaults(func=c.cmd_repair_symlinks)`, and the setup tests'
`monkeypatch.setitem(ns, "_load_claude_settings", …)` resolve unchanged.
The `_LEGACY_*` constants and `CLAUDE_SETTINGS_PATH` stay in
`bin/cctally` / `_cctally_core` — preserves the existing `_e2e_pin_paths`
test workaround verbatim (§5.4), plus keeps the monkeypatch-sensitive
`_LEGACY_BESPOKE_HOOKS_DIR` / `_LEGACY_POLLER_PID_FILE` /
`_LEGACY_POLLER_COUNT_FILE` constants on `cctally` where
`monkeypatch.setitem(ns, ...)` lands. Helpers in this module reach the
`_LEGACY_*` constants via `_cctally().<NAME>` (call-time lookup,
monkeypatch propagates); the C10 helpers above are same-module locals but
THIS module's existing call sites deliberately keep reaching them via
`c.<NAME>` so ns-level `setitem` patches stay visible (Codex round-1 P1).

bin/cctally back-references via `_cctally()` (spec §5.5 pattern, same as
`bin/_lib_subscription_weeks.py` and `bin/_lib_aggregators.py`):
- Path / log constants: `APP_DIR`, `HOOK_TICK_LOG_PATH`,
  `HOOK_TICK_LOG_ROTATED_PATH`, `CLAUDE_SETTINGS_PATH`,
  `LEGACY_STATUSLINE_PATHS`, `LEGACY_STATUSLINE_NEEDLE`,
  `SETUP_HOOK_EVENTS`, `SETUP_SYMLINK_NAMES`.
- Legacy constants: `_LEGACY_BESPOKE_HOOKS_DIR`, `_LEGACY_BESPOKE_COMMANDS`,
  `_LEGACY_BESPOKE_FILENAMES`, `_LEGACY_POLLER_PID_FILE`,
  `_LEGACY_POLLER_COUNT_FILE`, `_LEGACY_BACKUP_DIR_PREFIX`,
  `_LEGACY_POLLER_SIGTERM_GRACE_S`.
- Shared helpers: `eprint`, `_resolve_oauth_token`,
  `_hook_tick_throttle_age_seconds`, `_hook_tick_oauth_refresh`,
  `_hook_tick_throttle_touch`, `_command_as_of`, `open_cache_db`,
  `sync_cache`. (`SetupError`, `_is_cctally_hook_command`,
  `_load_claude_settings`, `_write_claude_settings_atomic`,
  `_backup_claude_settings` are now LOCAL to this module — C10 — but the
  call sites here still reach them via `c.<NAME>` for the monkeypatch
  contract, so functionally they read like the back-references above.)

bin/cctally re-exports every public symbol below so tests that drive
`cmd_setup` and the legacy-migration helpers via `ns["X"](...)` resolve
unchanged (eager-load pattern per spec §4.8: tests use direct dict
access on the cctally namespace, which bypasses PEP 562 `__getattr__`).

Spec: docs/superpowers/specs/2026-05-13-bin-cctally-split-design.md
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time


def _cctally():
    """Resolve the current `cctally` module at call-time (spec §5.5)."""
    return sys.modules["cctally"]


# === Honest imports from extracted homes ===================================
# Spec 2026-05-17 §3.3: kernel symbols import from _cctally_core. Path
# constants (APP_DIR, CLAUDE_SETTINGS_PATH, HOOK_TICK_LOG_PATH, etc.)
# moved to _cctally_core 2026-05-22 (#84) and are accessed via call-time
# ``_cctally_core.X``. The setup-specific helpers (legacy migration, hook
# surgery, OAuth token, sync_cache, …) that live in bin/cctally itself
# stay on the _cctally() accessor.
import _cctally_core
from _cctally_core import (
    eprint,
    _command_as_of,
)


# Dev-instance isolation (§3): refusal message when `cctally setup` is run
# from a git checkout without --force-dev. {data_dir} is the resolved
# APP_DIR for context (cctally-dev in plain dev mode, the override path if
# CCTALLY_DATA_DIR was set — the guard keys on _is_dev_checkout(), not the
# data dir, so the override still cannot rewrite prod's hooks).
_DEV_SETUP_REFUSAL_MSG = (
    "cctally setup: refusing to run from a dev checkout (data dir: {data_dir}).\n"
    "This would rewrite the hooks in ~/.claude/settings.json that point at your\n"
    "installed (prod) cctally. Run setup from the installed binary instead, or\n"
    "pass --force-dev to override (e.g. to install dev-pointing hooks on purpose)."
)


# Dev-instance isolation (§3, P2): warning when `--force-dev` installs hooks
# while CCTALLY_DATA_DIR is set. The hook command saved into settings.json is
# just `<binary> hook-tick` — it does NOT carry the override env. A hook fire
# that doesn't inherit the override (GUI-launched Claude, a different shell)
# resolves APP_DIR via dev-checkout auto-detect ({autodetect_dir}), while
# interactive runs in this shell use {override_dir} — silently splitting one
# intended instance across two DBs. CCTALLY_DATA_DIR is an interactive-only
# hatch (spec "Out of scope / accepted"); baking it into the global hook
# command would persist a transient path machine-wide, so we warn instead.
_DEV_SETUP_FORCE_DEV_OVERRIDE_WARNING = (
    "cctally setup: warning: installing hooks with --force-dev while "
    "CCTALLY_DATA_DIR is set.\n"
    "  Interactive runs in this shell use: {override_dir}\n"
    "  Background hook fires (no env inherited) will use: {autodetect_dir}\n"
    "The hook command can't carry CCTALLY_DATA_DIR, so these two paths split "
    "your\ndata across separate DBs. CCTALLY_DATA_DIR is an interactive-only "
    "override."
)


# ── settings/hook glue (#125 Batch E, C10) ─────────────────────────────
# Moved here from bin/cctally. bin/cctally re-exports each via the
# _cctally_setup load site so doctor's `except c.SetupError`, the parser's
# `set_defaults(func=c.cmd_repair_symlinks)`, and the setup tests'
# `monkeypatch.setitem(ns, "_load_claude_settings", …)` all resolve to
# these objects. The sibling's OWN reaches to these helpers deliberately
# STAY `c.<name>` (NOT local) so ns-level monkeypatches propagate.


def _is_cctally_hook_command(cmd: str) -> bool:
    """Return True if `cmd` is one of OUR hook entries (Section 4 of spec).

    Identification is shlex-aware so quoted absolute paths (with spaces)
    and bare names both match. The discriminator is the LAST TWO tokens
    after stripping any trailing `&` and surrounding whitespace:

      tokens[-2] = a path whose basename is ``cctally`` or
                   the npm shim (``_CCTALLY_NPM_SHIM_BASENAME``; npm install
                   layout — the hook command points at the Node shim so
                   ``CCTALLY_PYTHON`` propagates from the user's shell env
                   into hook fires)
      tokens[-1] = "hook-tick"

    Examples that match:
        cctally hook-tick
        cctally hook-tick &
        /Users/me/.local/bin/cctally hook-tick &
        '/Users/My Name/.local/bin/cctally' hook-tick &
        /usr/local/lib/node_modules/cctally/bin/<npm-shim> hook-tick
    """
    import shlex
    if not isinstance(cmd, str) or not cmd.strip():
        return False
    stripped = cmd.strip()
    # Strip trailing &; allow whitespace before it.
    while stripped.endswith("&"):
        stripped = stripped[:-1].rstrip()
        if not stripped:
            return False
    try:
        tokens = shlex.split(stripped)
    except ValueError:
        return False
    if len(tokens) < 2:
        return False
    last = tokens[-1]
    prev = tokens[-2]
    if last != "hook-tick":
        return False
    return pathlib.PurePosixPath(prev).name in (
        "cctally", _CCTALLY_NPM_SHIM_BASENAME,
    )


class SetupError(RuntimeError):
    """Raised when setup hits a hard prerequisite failure (Section 2 of spec)."""


def _load_claude_settings(path: pathlib.Path | None = None) -> dict:
    """Read ~/.claude/settings.json. Empty/missing → {}. Malformed → SetupError.

    ``path`` resolves to ``_cctally_core.CLAUDE_SETTINGS_PATH`` at CALL
    TIME when omitted, so ``monkeypatch.setattr(_cctally_core,
    "CLAUDE_SETTINGS_PATH", tmp)`` propagates without needing to swap
    out this callable. Capturing the default at def-time would silently
    pin the maintainer's real ``~/.claude/settings.json``.
    """
    if path is None:
        path = _cctally_core.CLAUDE_SETTINGS_PATH
    if not path.exists():
        return {}
    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SetupError(
            f"settings.json at {path} is not valid JSON: {exc}. Fix it and re-run."
        ) from exc
    if not isinstance(data, dict):
        raise SetupError(f"settings.json at {path} is not a JSON object — fix and re-run.")
    return data


def _backup_claude_settings(path: pathlib.Path | None = None) -> pathlib.Path | None:
    """Best-effort daily backup; return backup path or None.

    ``path`` resolves to ``_cctally_core.CLAUDE_SETTINGS_PATH`` at CALL
    TIME when omitted (see ``_load_claude_settings`` for rationale).
    """
    if path is None:
        path = _cctally_core.CLAUDE_SETTINGS_PATH
    if not path.exists():
        return None
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d")
    backup = path.with_name(path.name + f".cctally-backup-{today}")
    if backup.exists():
        return backup
    try:
        shutil.copy2(path, backup)
    except OSError as exc:
        eprint(f"[setup] backup failed (continuing): {exc}")
        return None
    return backup


def _write_claude_settings_atomic(
    settings: dict, path: pathlib.Path | None = None
) -> None:
    """Atomic write with 2-space indent, trailing newline.

    ``path`` resolves to ``_cctally_core.CLAUDE_SETTINGS_PATH`` at CALL
    TIME when omitted (see ``_load_claude_settings`` for rationale).
    """
    if path is None:
        path = _cctally_core.CLAUDE_SETTINGS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# ── settings.json hook surgery ─────────────────────────────────────────


def _settings_merge_install(settings: dict, abs_cctally_path: str) -> dict:
    """Append our hook entries idempotently. Returns a (possibly mutated) dict.

    Raises SetupError if hooks structure has wrong shape.

    Legacy upgrade: existing matching entries whose `command` field still
    carries the trailing `&` (or any other variant) are rewritten in place
    to the bare form. Writing the same value is a no-op, so this is safe
    on already-current installs. The trailing `&` was dropped because POSIX
    async-list semantics in non-interactive shells redirect stdin to
    /dev/null, which blanked the hook event payload — `cmd_hook_tick`
    forks internally after reading stdin instead.
    """
    import shlex
    c = _cctally()
    hooks_root = settings.setdefault("hooks", {})
    if not isinstance(hooks_root, dict):
        raise c.SetupError("settings.json: `hooks` is not a dict — fix and re-run.")
    quoted = shlex.quote(abs_cctally_path)
    cmd = f"{quoted} hook-tick"
    for event in c.SETUP_HOOK_EVENTS:
        event_list = hooks_root.setdefault(event, [])
        if not isinstance(event_list, list):
            raise c.SetupError(
                f"settings.json: `hooks.{event}` is not a list — fix and re-run."
            )
        already = False
        for grp in event_list:
            if not isinstance(grp, dict):
                continue
            for h in grp.get("hooks", []) or []:
                if not isinstance(h, dict):
                    continue
                existing_cmd = h.get("command", "")
                if c._is_cctally_hook_command(existing_cmd):
                    already = True
                    # Legacy upgrade: rewrite to bare form if it differs.
                    # Idempotent — writing the same string is a no-op.
                    if existing_cmd != cmd:
                        h["command"] = cmd
        if already:
            continue
        new_group = {
            "matcher": "*" if event == "PostToolBatch" else "",
            "hooks": [{"type": "command", "command": cmd}],
        }
        event_list.append(new_group)
    return settings


def _settings_merge_uninstall(settings: dict) -> tuple[dict, int]:
    """Drop our hook entries. Returns (mutated_settings, removed_count)."""
    c = _cctally()
    hooks_root = settings.get("hooks")
    if not isinstance(hooks_root, dict):
        return settings, 0
    removed = 0
    for event in c.SETUP_HOOK_EVENTS:
        event_list = hooks_root.get(event)
        if not isinstance(event_list, list):
            continue
        new_list: list = []
        for grp in event_list:
            if not isinstance(grp, dict):
                new_list.append(grp)
                continue
            inner = grp.get("hooks", [])
            if not isinstance(inner, list):
                new_list.append(grp)
                continue
            kept = [
                h for h in inner
                if not (isinstance(h, dict) and c._is_cctally_hook_command(h.get("command", "")))
            ]
            removed += len(inner) - len(kept)
            if kept:
                grp["hooks"] = kept
                new_list.append(grp)
            # else: matcher group's only entry was ours → drop the group
        if new_list:
            hooks_root[event] = new_list
        else:
            del hooks_root[event]
    return settings, removed


def _settings_merge_unwire_legacy(settings: dict) -> tuple[dict, int]:
    """Remove legacy-bespoke hook entries from ``settings`` in place.

    Mirrors _settings_merge_uninstall's structure but matches against
    the legacy command set rather than the cctally one. Returns
    (mutated_settings, removed_count). Empty event lists are dropped.
    Trailing '&' is stripped before tokenizing so legacy installs that
    background the daemon-start hook still match.
    """
    import shlex as _shlex
    c = _cctally()
    canonical = {(ev, tuple(_shlex.split(cmd))) for ev, cmd in c._LEGACY_BESPOKE_COMMANDS}
    canonical_raw = {(ev, cmd) for ev, cmd in c._LEGACY_BESPOKE_COMMANDS}
    hooks_root = settings.get("hooks")
    if not isinstance(hooks_root, dict):
        return settings, 0
    removed = 0
    for ev in [k for k in hooks_root.keys()]:  # snapshot keys; we may del
        lst = hooks_root.get(ev)
        if not isinstance(lst, list):
            continue
        new_list: list = []
        for grp in lst:
            if not isinstance(grp, dict):
                new_list.append(grp)
                continue
            inner = grp.get("hooks", [])
            if not isinstance(inner, list):
                new_list.append(grp)
                continue
            kept_inner = []
            for h in inner:
                if not isinstance(h, dict):
                    kept_inner.append(h)
                    continue
                raw = h.get("command", "")
                if not isinstance(raw, str):
                    kept_inner.append(h)
                    continue
                stripped = raw.strip().rstrip("&").strip()
                try:
                    tokens = tuple(_shlex.split(stripped))
                except ValueError:
                    kept_inner.append(h)
                    continue
                if (ev, tokens) in canonical or (ev, stripped) in canonical_raw:
                    removed += 1
                    continue
                kept_inner.append(h)
            if kept_inner:
                grp["hooks"] = kept_inner
                new_list.append(grp)
            # else: matcher group's only entry was a legacy one → drop the group
        if new_list:
            hooks_root[ev] = new_list
        else:
            del hooks_root[ev]
    return settings, removed


# ── symlink + path helpers ─────────────────────────────────────────────


def _setup_resolve_repo_root() -> pathlib.Path:
    """Resolve the cctally checkout root from __file__."""
    # __file__ here is bin/_cctally_setup.py; the cctally checkout
    # root is two parents up (same as bin/cctally), so the resolution
    # is identical to the pre-extraction behavior.
    return pathlib.Path(__file__).resolve().parent.parent


def _setup_local_bin_dir() -> pathlib.Path:
    return pathlib.Path.home() / ".local" / "bin"


@dataclasses.dataclass
class _SetupSymlinkResult:
    name: str
    status: str   # "created" | "already" | "replaced" | "failed" | "removed-stale"
    detail: str = ""


# Symlink names cctally USED to install but no longer does. We keep
# cleaning them up for one major-version's worth of upgraders so
# `~/.local/bin/` doesn't accumulate dangling symlinks pointing at
# scripts the current cctally checkout doesn't ship. Removal is
# conservative: we only unlink symlinks whose `readlink()` target's
# basename matches the stale name — a user's hand-rolled symlink with
# the same name pointing elsewhere is left alone.
_SETUP_STALE_SYMLINK_NAMES = (
    "cctally-release",  # Removed in v1.9.0; release tooling went private.
)


# The npm package ships a Node shim as the `cctally` entry point. Its
# basename differs from the command name, so any code matching a
# symlink's readlink target by basename must special-case it.
_CCTALLY_NPM_SHIM_BASENAME = "cctally-npm-shim.js"


def _setup_resolve_symlink_source(repo_root: pathlib.Path, name: str) -> pathlib.Path:
    """Resolve the symlink target for a given PATH-name.

    For `cctally`, prefer `bin/cctally-npm-shim.js` ONLY when the
    package layout indicates an npm install — i.e. ``repo_root`` sits
    somewhere under a ``node_modules/`` directory (npm global at
    ``<prefix>/lib/node_modules/cctally/``, npm local at
    ``<project>/node_modules/cctally/``, plus pnpm/yarn variants that
    all keep the segment). The shim is committed to the source tree so
    the npm-publish layout doesn't need a build step, but file presence
    alone is not a reliable channel signal: source clones and brew
    installs ship the shim too, and Node is not a runtime dependency
    of either path. Falling back to ``bin/cctally`` (the Python script)
    in those cases keeps source/brew installs Python-only as
    documented. All other names map directly to ``bin/<name>``.
    """
    if name == "cctally" and "node_modules" in repo_root.parts:
        shim = repo_root / "bin" / _CCTALLY_NPM_SHIM_BASENAME
        if shim.exists():
            return shim
    return repo_root / "bin" / name


def _setup_resolve_hook_target(repo_root: pathlib.Path) -> pathlib.Path:
    """Absolute path recorded in Claude Code hook entries.

    Brew installs: return the version-stable `<prefix>/bin/cctally` (the
    formula's symlink, which self-heals on `brew upgrade`) WITHOUT
    `.resolve()` — resolving would pin the hook to the versioned keg and
    dangle after `brew cleanup` (issue #119). Source/npm: keep the
    `.resolve()` semantics (survives clone/node_modules rearrangement and
    the npm-shim branch in `_setup_resolve_symlink_source`).
    """
    if _setup_is_brew_install(repo_root):
        prefix = _setup_brew_prefix(repo_root)
        stable = pathlib.Path(prefix) / "bin" / "cctally"
        if stable.exists():
            return stable
    return _setup_resolve_symlink_source(repo_root, "cctally").resolve()


def _setup_create_symlinks(
    repo_root: pathlib.Path, dst_dir: pathlib.Path, *, names: tuple[str, ...] | None = None,
) -> list[_SetupSymlinkResult]:
    if names is None:
        names = _cctally().SETUP_SYMLINK_NAMES
    dst_dir.mkdir(parents=True, exist_ok=True)
    results: list[_SetupSymlinkResult] = []
    for name in names:
        src = _setup_resolve_symlink_source(repo_root, name)
        dst = dst_dir / name
        if not src.exists():
            results.append(_SetupSymlinkResult(name, "failed", f"source not found: {src}"))
            continue
        if dst.is_symlink():
            existing = os.readlink(dst)
            if pathlib.Path(existing) == src:
                results.append(_SetupSymlinkResult(name, "already"))
                continue
            try:
                dst.unlink()
                os.symlink(src, dst)
                results.append(_SetupSymlinkResult(name, "replaced"))
            except OSError as exc:
                results.append(_SetupSymlinkResult(name, "failed", str(exc)))
            continue
        if dst.exists():
            results.append(_SetupSymlinkResult(
                name, "failed",
                f"non-symlink file at {dst} — remove manually then re-run",
            ))
            continue
        try:
            os.symlink(src, dst)
            results.append(_SetupSymlinkResult(name, "created"))
        except OSError as exc:
            results.append(_SetupSymlinkResult(name, "failed", str(exc)))
    return results


@dataclasses.dataclass
class _RepairResult:
    gated: bool
    created: list[str]
    failed: list[tuple[str, str]]  # (name, detail)


def _setup_repair_symlinks(
    repo_root: pathlib.Path, dst_dir: pathlib.Path,
) -> _RepairResult:
    """Additively reconcile ``dst_dir`` to ``SETUP_SYMLINK_NAMES`` (issue #114).

    Existing-install gate: acts only when at least one
    ``SETUP_SYMLINK_NAMES`` symlink is already present in ``dst_dir`` — a
    fresh install (zero links) is left untouched so onboarding stays
    opt-in. Strictly additive: creates only *genuinely empty* slots and
    leaves present / wrong-target / dangling / non-symlink slots alone.
    Touches nothing but ``~/.local/bin/`` symlinks (no hooks /
    settings.json / cache). Filesystem-only — deliberately NOT PATH-aware
    (unlike :func:`_setup_compute_symlink_state`), so it stays
    deterministic regardless of the live ``PATH``.
    """
    names = _cctally().SETUP_SYMLINK_NAMES
    present = [n for n in names if (dst_dir / n).is_symlink()]
    if not present:
        return _RepairResult(gated=True, created=[], failed=[])
    missing = [
        n for n in names
        if not (dst_dir / n).is_symlink() and not (dst_dir / n).exists()
    ]
    if not missing:
        return _RepairResult(gated=False, created=[], failed=[])
    results = _setup_create_symlinks(repo_root, dst_dir, names=tuple(missing))
    created = [r.name for r in results if r.status == "created"]
    failed = [(r.name, r.detail) for r in results if r.status == "failed"]
    return _RepairResult(gated=False, created=created, failed=failed)


def cmd_repair_symlinks(args: argparse.Namespace) -> int:
    """Hidden: additively create missing ~/.local/bin/ symlinks on upgrade.

    Invoked best-effort by the npm postinstall (issue #114). Refuses from
    a dev checkout (would point ~/.local/bin at the dev tree). Touches
    only symlinks — see _setup_repair_symlinks. Exempted from main()'s
    post-command update hooks (see _post_command_update_hooks).
    """
    if _cctally_core._is_dev_checkout():
        eprint(
            "repair-symlinks: refusing to run from a dev checkout "
            "(would point ~/.local/bin at the dev tree)"
        )
        return 2
    repo_root = _setup_resolve_repo_root()
    dst_dir = _setup_local_bin_dir()
    result = _setup_repair_symlinks(repo_root, dst_dir)
    if result.created:
        print(
            f"cctally: linked {len(result.created)} new command symlink(s): "
            + ", ".join(result.created)
        )
    for name, detail in result.failed:
        eprint(f"repair-symlinks: {name}: {detail}")
    return 1 if result.failed else 0


def _setup_cleanup_stale_symlinks(
    dst_dir: pathlib.Path,
) -> list[_SetupSymlinkResult]:
    """Unlink stale `cctally-*` symlinks left over from prior versions.

    For each entry in :data:`_SETUP_STALE_SYMLINK_NAMES`, removes the
    matching symlink in ``dst_dir`` when its readlink target basename
    matches the stale name AND the target points at a "retired" cctally
    install — meaning either (a) the target is *dangling* (no longer
    resolves to a real file, e.g. an old checkout that was deleted) OR
    (b) the target lives under a *foreign* cctally install root
    (Homebrew keg, npm ``node_modules/cctally/``), i.e. an install root
    other than the one currently running ``cctally setup``.

    The foreign-root clause is what handles the common upgrade path:
    after ``brew upgrade cctally`` or ``npm i -g cctally@<new>``, the
    *prior* keg / module dir often lingers on disk until ``brew
    cleanup`` (or the next npm install GC), so a legacy
    ``~/.local/bin/cctally-release`` symlink from a pre-v1.9.0 ``cctally
    setup`` still resolves to an existing file under the *old* install
    root. The dangling-only predicate would skip it, leaving the retired
    command on PATH; the foreign-root check retires it instead.

    Both clauses still preserve the maintainer's intentional manual
    link to the *current* checkout's still-shipped retired tooling
    (e.g. ``~/.local/bin/cctally-release ->
    <cctally-dev>/bin/cctally-release``) — that target is neither
    dangling nor foreign, so it's left alone. Likewise a hand-rolled
    link pointing somewhere unrelated (``~/scripts/...``) survives.

    Returns a list of :class:`_SetupSymlinkResult` entries for the
    actions taken (so callers can fold them into install output).
    """
    results: list[_SetupSymlinkResult] = []
    repo_root = _setup_resolve_repo_root()
    retired_names = set(_SETUP_STALE_SYMLINK_NAMES)
    for name in dict.fromkeys(_SETUP_STALE_SYMLINK_NAMES + tuple(_cctally().SETUP_SYMLINK_NAMES)):
        dst = dst_dir / name
        if not _setup_symlink_is_retired(dst, name, repo_root):
            continue
        if name in retired_names:
            should_remove = True                       # retired command: unconditional
        else:                                           # active name: reachability-gated
            is_dangling = not dst.resolve(strict=False).exists() if dst.is_symlink() else False
            should_remove = is_dangling or _reachable_elsewhere(name)
        if not should_remove:
            continue
        try:
            dst.unlink()
            results.append(_SetupSymlinkResult(name, "removed-stale", "stale (issue #119 cleanup)"))
        except OSError as exc:
            results.append(_SetupSymlinkResult(name, "failed", f"unlink failed: {exc}"))
    return results


# Path tokens that identify a "foreign" cctally install root — i.e. a
# directory tree managed by a different distribution channel (or a
# different version of the same channel) than the one currently running
# ``cctally setup``. A symlink whose target sits under one of these is
# almost certainly a legacy auto-installed link from a prior version
# whose install root the current run does NOT manage. Pre-resolved (no
# trailing slash) so substring matching works on either UNIX or
# resolved forms.
_SETUP_FOREIGN_INSTALL_ROOT_TOKENS = (
    # Homebrew keeps every installed version under
    # ``<prefix>/Cellar/cctally/<version>/``. A target inside any of
    # these directories points at a brew install — either the current
    # one (when ``cctally setup`` runs from a brew install — possible
    # but rare) or an older keg still on disk pending ``brew cleanup``.
    "/Cellar/cctally/",
    # npm globals land at ``<prefix>/lib/node_modules/cctally/``; npm
    # locals at ``<project>/node_modules/cctally/``. Either way the
    # ``/node_modules/cctally/`` segment is the discriminator. pnpm and
    # yarn variants keep the segment, so this catches them too.
    "/node_modules/cctally/",
)


def _setup_symlink_is_retired(
    dst: pathlib.Path, name: str, repo_root: pathlib.Path,
) -> bool:
    """Detection predicate for retired auto-symlinks at install time.

    Returns True iff ``dst`` is a symlink, its readlink target basename
    matches ``name``, AND the target points at a "retired" cctally
    install — i.e. one the current ``cctally setup`` run does not
    manage. Two retirement classes:

      1. **Dangling target** — readlink target does not resolve to an
         existing filesystem entry. Covers the deleted-checkout case.
      2. **Foreign install root** — target path contains one of
         :data:`_SETUP_FOREIGN_INSTALL_ROOT_TOKENS` (``/Cellar/cctally/``
         or ``/node_modules/cctally/``) AND that token does NOT also
         appear in the current ``repo_root``. The second clause is what
         keeps the (rare) case of running ``cctally setup`` *from* an
         npm/brew install correctly — a symlink at the same install root
         is NOT foreign, so it's left to the active-symlinks loop in
         :func:`_setup_install` / :func:`_setup_uninstall`.

    Targets that exist but live outside any recognized install root
    (e.g. a maintainer's manual link to ``<checkout>/bin/cctally-release``
    or a hand-rolled link at ``~/scripts/cctally-release``) are
    preserved — they're either explicit operator setups or genuine
    user-managed scripts that happen to share the name.

    Shared by the cleanup site (``_setup_cleanup_stale_symlinks``) and
    the read-only detection site (``_setup_detect_stale_symlinks``) so
    ``--status`` and ``setup`` agree on what they call "stale".
    """
    if not dst.is_symlink():
        return False
    try:
        target = os.readlink(dst)
    except OSError:
        return False
    target_basename = pathlib.Path(target).name
    accepted = {name}
    if name == "cctally":
        accepted.add(_CCTALLY_NPM_SHIM_BASENAME)
    if target_basename not in accepted:
        # User-managed symlink that happens to share the name; leave alone.
        return False
    # Resolve target relative to the symlink's parent so relative
    # readlinks (rare for cctally setup-installed links, but possible
    # for hand-rolled ones) classify correctly. Use lexists()-style
    # check via Path.exists() — broken links return False, which is
    # the "dangling, treat as stale" branch we want.
    target_path = pathlib.Path(target)
    if not target_path.is_absolute():
        target_path = dst.parent / target_path
    try:
        target_exists = target_path.exists()
    except OSError:
        # Permission errors etc. — be conservative and don't remove.
        return False
    if not target_exists:
        return True
    # Target exists — retire only if it lives under a *foreign* install
    # root (a different brew keg / npm module dir than the one running
    # this setup). Compare against ``repo_root`` so a setup running
    # *from* an npm/brew install doesn't classify its own siblings as
    # foreign — that's the active-symlinks loop's job.
    target_str = str(target_path)
    # "Same install root" means the target lives UNDER the current
    # ``repo_root`` tree. Use a separator-anchored prefix check rather
    # than a bare substring of the token: a same-``repo_root`` npm install
    # has ``repo_root == <…>/node_modules/cctally`` (no trailing slash),
    # so the raw ``"/node_modules/cctally/" in repo_root_str`` test would
    # spuriously miss and classify the live channel's own link as foreign
    # (issue #119 finding #7 — the npm `cctally` shim under its own
    # ``node_modules/cctally`` must be preserved, not retired).
    repo_root_str = str(repo_root)
    repo_root_prefix = repo_root_str.rstrip(os.sep) + os.sep
    target_under_repo_root = (
        target_str == repo_root_str or target_str.startswith(repo_root_prefix)
    )
    for token in _SETUP_FOREIGN_INSTALL_ROOT_TOKENS:
        if token not in target_str:
            continue
        # Homebrew keg links are NEVER owned by ~/.local/bin under the
        # issue-#119 policy, so retire them regardless of which keg /
        # whether repo_root is itself a keg. Other foreign roots
        # (node_modules) retire only when the target is NOT under the
        # current install root.
        if token == _SETUP_FOREIGN_INSTALL_ROOT_TOKENS[0]:   # "/Cellar/cctally/"
            return True
        if not target_under_repo_root:
            return True
    return False


def _setup_path_includes_local_bin() -> bool:
    local_bin = str(_setup_local_bin_dir())
    return local_bin in os.environ.get("PATH", "").split(os.pathsep)


def _setup_is_brew_install(repo_root: pathlib.Path) -> bool:
    """True when this cctally runs from a Homebrew keg.

    `_setup_resolve_repo_root()` `.resolve()`s `__file__`, so a brew
    install reliably carries the `/Cellar/cctally/` token (Apple Silicon,
    Intel, Linuxbrew all funnel through it). Reuses the single token
    source in `_SETUP_FOREIGN_INSTALL_ROOT_TOKENS[0]`. Cheap — no
    `npm prefix` subprocess; we only need the brew yes/no.
    """
    return _SETUP_FOREIGN_INSTALL_ROOT_TOKENS[0] in str(repo_root)


def _setup_brew_prefix(repo_root: pathlib.Path) -> str:
    """The Homebrew `<prefix>` (e.g. `/opt/homebrew`) for a brew keg
    `repo_root`. Splits on the single-source brew token so we never spell
    the Cellar path a third way. Callers must guard with
    `_setup_is_brew_install(repo_root)` first; off a keg this returns the
    unchanged string."""
    return str(repo_root).split(_SETUP_FOREIGN_INSTALL_ROOT_TOKENS[0])[0]


def _reachable_elsewhere(name: str) -> bool:
    """Would `<name>` still be found on PATH if the ~/.local/bin slot
    didn't exist? Excludes the ~/.local/bin directory (realpath-compared)
    so a stale link can't satisfy its own reachability check (issue #119
    finding #6)."""
    local_bin = _setup_local_bin_dir()
    try:
        local_real = os.path.realpath(local_bin)
    except OSError:
        local_real = str(local_bin)
    dirs = [
        d for d in os.environ.get("PATH", "").split(os.pathsep)
        if d and os.path.realpath(d) != local_real
    ]
    return shutil.which(name, path=os.pathsep.join(dirs)) is not None


def _setup_shell_rc_hint() -> str:
    shell = os.environ.get("SHELL", "")
    if "zsh" in shell:
        return "~/.zshrc"
    if "bash" in shell:
        return "~/.bashrc"
    return "your shell rc"


# ── legacy snippet + bespoke-hook detection ────────────────────────────


def _setup_detect_legacy_snippet() -> tuple[pathlib.Path, list[int]] | None:
    """Return (path, [line_numbers]) of the first file containing the snippet, or None."""
    c = _cctally()
    for path in c.LEGACY_STATUSLINE_PATHS:
        if not path.exists() or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Skip lines whose first non-whitespace char is `#`: a shell comment
        # that merely references the legacy command in prose (e.g. a NOTE
        # documenting its removal) is not an executing snippet (issue #115).
        # POSIX shell only treats `#` as a comment marker at start-of-token,
        # so this covers natural-language comments without parsing the shell.
        hits = [
            i + 1
            for i, ln in enumerate(text.splitlines())
            if c.LEGACY_STATUSLINE_NEEDLE in ln and not ln.lstrip().startswith("#")
        ]
        if hits:
            return (path, hits)
    return None


def _setup_detect_legacy_bespoke_hooks(settings: dict) -> dict:
    """Detect legacy bespoke hook state per spec Section 1.

    Detection fires when ANY of the 3 canonical settings.json command
    strings matches an installed entry, OR ANY of the 4 canonical
    .py files exists at its canonical path under _LEGACY_BESPOKE_HOOKS_DIR.

    Returns a dict with keys:
      detected: bool
      settings_entries: list of {"event": str, "command": str}
      files: list of str (rendered with ~/.claude/hooks/ prefix)
    """
    import shlex as _shlex
    c = _cctally()
    canonical_cmds = {(ev, cmd) for ev, cmd in c._LEGACY_BESPOKE_COMMANDS}
    canonical_tokens = {(ev, tuple(_shlex.split(cmd))) for ev, cmd in c._LEGACY_BESPOKE_COMMANDS}

    found_entries: list[dict] = []
    hooks_root = settings.get("hooks", {}) if isinstance(settings, dict) else {}
    if isinstance(hooks_root, dict):
        for event, lst in hooks_root.items():
            if not isinstance(lst, list):
                continue
            matched_for_this_event = False
            for grp in lst:
                if matched_for_this_event:
                    break  # already recorded one row for this event; don't double-count
                if not isinstance(grp, dict):
                    continue
                inner = grp.get("hooks", [])
                if not isinstance(inner, list):
                    # Mirrors the unwire helper's defensive guard: malformed
                    # `hooks` value (None / int / dict) must not crash iteration.
                    continue
                for h in inner:
                    if not isinstance(h, dict):
                        continue
                    raw = h.get("command", "")
                    if not isinstance(raw, str):
                        continue
                    stripped = raw.strip().rstrip("&").strip()
                    try:
                        tokens = tuple(_shlex.split(stripped))
                    except ValueError:
                        continue
                    if (event, tokens) in canonical_tokens or (event, stripped) in canonical_cmds:
                        # Record the canonical (clean) form for stable JSON output,
                        # not the user's possibly-decorated raw command.
                        clean_cmd = next(
                            cmd for ev, cmd in c._LEGACY_BESPOKE_COMMANDS if ev == event
                        )
                        found_entries.append({"event": event, "command": clean_cmd})
                        matched_for_this_event = True
                        break  # one entry per matcher group is enough

    found_files: list[str] = []
    for name in c._LEGACY_BESPOKE_FILENAMES:
        p = c._LEGACY_BESPOKE_HOOKS_DIR / name
        if p.exists():
            # Render with the ~ prefix the spec uses for stable JSON.
            found_files.append(f"~/.claude/hooks/{name}")

    return {
        "detected": bool(found_entries) or bool(found_files),
        "settings_entries": found_entries,
        "files": found_files,
    }


# ── legacy migration primitives (move / stop / cleanup / backup-dir) ───


def _legacy_resolve_backup_dir() -> pathlib.Path:
    """Return ~/.claude/cctally-legacy-hook-backup-<UTC YYYYMMDD-HHMMSS>/.

    Honors CCTALLY_AS_OF for fixture stability via _command_as_of(). Created
    on demand. Idempotent within the same wall-second (mkdir(exist_ok=True)).

    See spec Section 1 ("What gets touched on accept" → step 2) and
    Section 2 ("Sequence position", step 6a). Backup dir is timestamped
    so a re-run never overwrites a prior migration's snapshot.
    """
    c = _cctally()
    now = _command_as_of()
    stamp = now.strftime("%Y%m%d-%H%M%S")
    base = pathlib.Path.home() / ".claude" / f"{c._LEGACY_BACKUP_DIR_PREFIX}{stamp}"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _legacy_move_files_to_backup(backup_dir: pathlib.Path) -> list[pathlib.Path]:
    """Move present canonical .py files from `_LEGACY_BESPOKE_HOOKS_DIR` into backup_dir.

    Each canonical filename is moved only if present at its canonical path;
    missing files are silent no-ops (per spec Section 1: "Missing files are
    silent no-ops in the move loop"). Returns the list of destination paths
    actually written, in canonical (`_LEGACY_BESPOKE_FILENAMES`) order.

    Uses `shutil.move` (canonical Python idiom): same-filesystem renames
    go through `os.rename`, cross-device moves fall through to
    `copy2 + unlink` atomically — and a failure on the unlink leg raises
    `OSError` instead of silently leaving a duplicate at both src and dst
    (which the prior hand-rolled try/except/inner-try did).
    """
    c = _cctally()
    moved: list[pathlib.Path] = []
    for name in c._LEGACY_BESPOKE_FILENAMES:
        src = c._LEGACY_BESPOKE_HOOKS_DIR / name
        if not src.exists():
            continue
        dst = backup_dir / name
        try:
            shutil.move(str(src), str(dst))
        except OSError:
            # Best-effort: a failed move is silent; spec Section 1 step 2
            # treats the move loop's failures as no-ops (the daemon-stop
            # follow-up handles user-facing damage control).
            continue
        moved.append(dst)
    return moved


def _legacy_stop_active_poller() -> str:
    """Best-effort SIGTERM (then SIGKILL) the bespoke daemon if alive.

    Per spec Section 1 step 3: read /tmp/claude-usage-poller.pid, send
    SIGTERM, wait `_LEGACY_POLLER_SIGTERM_GRACE_S`, send SIGKILL if still
    alive. All steps are best-effort and silent on failure — the daemon
    may already be dead, the PID may be stale, the rlimits may forbid
    signaling, or the file may simply be absent.

    Returns one of:
      "no-pid-file"       — no /tmp/claude-usage-poller.pid present
      "stale-pid"         — PID file exists but the PID isn't a live
                            process, parse failed, OR the live PID's
                            cmdline doesn't reference usage-poller.py
                            (collapsed: don't signal an unrelated process)
      "sigterm-took"      — SIGTERM landed and the process exited within
                            the grace window
      "sigkill-took"      — SIGTERM did not stop it; SIGKILL landed
      "permission-denied" — kernel refused to signal the PID (EPERM)
    """
    import signal as _signal
    c = _cctally()

    if not c._LEGACY_POLLER_PID_FILE.exists():
        return "no-pid-file"
    try:
        raw = c._LEGACY_POLLER_PID_FILE.read_text(encoding="utf-8", errors="replace").strip()
        pid = int(raw)
    except (OSError, ValueError):
        # Unreadable or non-numeric content → treat as stale (a corrupted
        # PID file is functionally indistinguishable from a stale one;
        # the cleanup helper will unlink it next).
        return "stale-pid"

    # Aliveness probe: signal 0 doesn't deliver but does the permission
    # + existence check. ProcessLookupError → stale; PermissionError →
    # we'd fail the actual signal too, surface that distinctly.
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "stale-pid"
    except PermissionError:
        return "permission-denied"
    except OSError:
        return "stale-pid"

    # Ownership probe: the PID file is at a predictable /tmp path that
    # outlives the daemon on uncleanly exit, and macOS PIDs cycle in a
    # narrow space — verify the live process is actually our legacy
    # poller before signaling. `-o command=` emits the cmdline with no
    # header on both macOS BSD ps and Linux util-linux ps; `-ww` forces
    # UNLIMITED width so the cmdline is never truncated. Without it,
    # Linux util-linux ps clamps the column to ~80 chars (macOS BSD ps
    # does not), so a poller launched from a long path drops the
    # "usage-poller.py" token off the end → a false "stale-pid".
    try:
        probe = subprocess.run(
            ["ps", "-ww", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=2.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        # Can't verify → don't signal. Treat as stale: a corrupted /tmp
        # sentinel is functionally equivalent to a missing process here.
        return "stale-pid"
    if probe.returncode != 0 or "usage-poller.py" not in probe.stdout:
        return "stale-pid"

    # Process is alive AND owned by the legacy poller. SIGTERM, then poll
    # for exit within the grace.
    try:
        os.kill(pid, _signal.SIGTERM)
    except ProcessLookupError:
        # Race: process exited between probe and signal — treat as success.
        return "sigterm-took"
    except PermissionError:
        return "permission-denied"
    except OSError:
        # Residual OSError after ProcessLookupError/PermissionError are caught
        # specifically — exotic kernel refusal (ENOMEM during signal queueing,
        # LSM denial, etc.). Map to permission-denied: spec contract forbids
        # raising, and "we couldn't deliver the signal" is the closest existing
        # outcome.
        return "permission-denied"

    deadline = time.monotonic() + c._LEGACY_POLLER_SIGTERM_GRACE_S
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return "sigterm-took"
        except OSError:
            return "sigterm-took"
        time.sleep(0.01)

    # Still alive after grace → SIGKILL fallback.
    try:
        os.kill(pid, _signal.SIGKILL)
    except ProcessLookupError:
        # Exited just at the grace boundary — count as SIGTERM-took.
        return "sigterm-took"
    except PermissionError:
        return "permission-denied"
    except OSError:
        # Same residual-OSError category as the SIGTERM site above.
        return "permission-denied"
    return "sigkill-took"


def _legacy_cleanup_tmp_sentinels() -> list[str]:
    """Unlink the bespoke poller's PID + count files. Best-effort; missing
    files are silent no-ops (FileNotFoundError) and so are unwritable
    parents (OSError). Returns the paths actually unlinked, as strings,
    in canonical (pid, count) order.

    Per spec Section 1 step 3 and Section 2 step 6b — runs after the
    SIGTERM/SIGKILL helper so a successful daemon stop also clears the
    sentinels it left on /tmp.
    """
    c = _cctally()
    unlinked: list[str] = []
    for p in (c._LEGACY_POLLER_PID_FILE, c._LEGACY_POLLER_COUNT_FILE):
        try:
            p.unlink()
        except FileNotFoundError:
            continue
        except OSError:
            continue
        unlinked.append(str(p))
    return unlinked


# ── prompt + decision + auxiliary counters ─────────────────────────────


def _setup_read_legacy_prompt_input(stream, reprompt: str | None = None) -> bool:
    """Read a y/N answer from `stream` per spec Section 2 prompt rules.

    Empty input (just Enter) → True (the documented default).
    'y'/'yes' (any case) → True.
    'n'/'no' (any case) → False.
    EOF before any character → False (decline; explicitly NOT default-Y, so
    non-TTY callers can't auto-accept via inherited stdin closure).
    Anything else → re-prompt up to 3 times, then False with a stderr warning.

    `reprompt`: optional text to emit to stderr before each attempt AFTER the
    first (the caller already printed the original prompt body before calling
    us). When None (test default), no reprompt is emitted — useful for unit
    tests that drive `stream` from io.StringIO.
    """
    yes_words = {"y", "yes"}
    no_words = {"n", "no"}
    for attempt in range(3):
        if attempt > 0 and reprompt is not None:
            eprint(reprompt)
        line = stream.readline()
        if line == "":
            return False  # EOF → decline
        token = line.strip().lower()  # whitespace-only counts as "just Enter" → default-Y
        if token == "":
            return True
        if token in yes_words:
            return True
        if token in no_words:
            return False
    eprint("setup: invalid responses 3 times; skipping migration")
    return False


def _setup_legacy_decide_action(args, detected: bool, stdin_isatty: bool) -> tuple[str, str | None]:
    """Decide migration action without performing prompt I/O.

    Returns (decision, reason) where decision is one of:
      - "migrate" — proceed with migration
      - "skip" — do not migrate; reason is one of "not_detected" /
        "no_migrate_flag" / "user_declined". This helper never returns
        "user_declined"; that reason is set by the caller after a
        "prompt" decision yields a No answer from
        _setup_read_legacy_prompt_input.
      - "prompt" — caller must read user input via the prompt helper.

    Spec Section 2 prompt rules: detection short-circuits, explicit flags
    are decisive, --yes implies migrate, --json or non-TTY without a flag
    skips silently (the JSON envelope and unattended runs both need a
    no-blocking-input contract). When none of those hold, the caller is
    in interactive install with detected hooks → prompt.
    """
    if not detected:
        return ("skip", "not_detected")
    if getattr(args, "no_migrate_legacy_hooks", False):
        return ("skip", "no_migrate_flag")
    if getattr(args, "migrate_legacy_hooks", False):
        return ("migrate", None)
    if getattr(args, "yes", False):
        return ("migrate", None)
    if not stdin_isatty:
        return ("skip", "no_migrate_flag")
    if getattr(args, "json", False):
        return ("skip", "no_migrate_flag")
    return ("prompt", None)


def _setup_oauth_token_present() -> bool:
    try:
        return bool(_cctally()._resolve_oauth_token())
    except Exception:
        return False


def _setup_count_hook_entries(settings: dict) -> dict[str, int]:
    """Return {event_name: count_of_our_entries} for the three events."""
    c = _cctally()
    counts = {ev: 0 for ev in c.SETUP_HOOK_EVENTS}
    hooks_root = settings.get("hooks") if isinstance(settings, dict) else None
    if not isinstance(hooks_root, dict):
        return counts
    for ev in c.SETUP_HOOK_EVENTS:
        ev_list = hooks_root.get(ev)
        if not isinstance(ev_list, list):
            continue
        for grp in ev_list:
            if not isinstance(grp, dict):
                continue
            inner = grp.get("hooks", [])
            if not isinstance(inner, list):
                continue
            for h in inner:
                if isinstance(h, dict) and c._is_cctally_hook_command(h.get("command", "")):
                    counts[ev] += 1
    return counts


def _setup_data_dir_size_bytes() -> int:
    app_dir = _cctally_core.APP_DIR
    total = 0
    if not app_dir.exists():
        return 0
    for root, _dirs, files in os.walk(app_dir):
        for f in files:
            try:
                total += (pathlib.Path(root) / f).stat().st_size
            except OSError:
                pass
    return total


def _setup_format_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} TB"


def _setup_recent_log_stats(seconds: float = 24 * 3600) -> dict:
    """Parse hook-tick.log + .log.1; return counts of fires/oauth/errors in window."""
    c = _cctally()
    cutoff = time.time() - seconds
    counts = {"fires": 0, "by_event": {}, "oauth_ok": 0, "throttled": 0,
              "errors": 0, "last_fire_ago_s": None}
    last_ts = 0.0
    for path in (_cctally_core.HOOK_TICK_LOG_ROTATED_PATH, _cctally_core.HOOK_TICK_LOG_PATH):
        if not path.exists():
            continue
        try:
            for ln in path.read_text(encoding="utf-8", errors="replace").splitlines():
                if not ln.strip():
                    continue
                try:
                    ts_iso = ln.split(" ", 1)[0]
                    ts = dt.datetime.fromisoformat(ts_iso).timestamp()
                except (ValueError, IndexError):
                    continue
                if ts < cutoff:
                    continue
                counts["fires"] += 1
                last_ts = max(last_ts, ts)
                # event=NAME
                ev = "unknown"
                for tok in ln.split():
                    if tok.startswith("event="):
                        ev = tok.split("=", 1)[1]
                        break
                counts["by_event"][ev] = counts["by_event"].get(ev, 0) + 1
                if "oauth=ok(" in ln:
                    counts["oauth_ok"] += 1
                elif "oauth=throttled" in ln:
                    counts["throttled"] += 1
                elif "oauth=err" in ln:
                    counts["errors"] += 1
        except OSError:
            continue
    if last_ts:
        counts["last_fire_ago_s"] = max(0, int(time.time() - last_ts))
    return counts


# ── status / uninstall / dry-run / install mode handlers ───────────────


def _setup_compute_symlink_state(
    repo_root: pathlib.Path, dst_dir: pathlib.Path,
) -> "list[tuple[str, str]]":
    """Per-symlink (name, state) for `_setup_status` + `doctor_gather_state`.

    state ∈ {"ok", "stale", "wrong", "missing"}.
      - ok:      resolvable non-retired link, OR empty slot reachable elsewhere.
      - stale:   retired link (Cellar/foreign/dangling) AND command reachable
                 via a non-~/.local/bin dir (safely cleanable). Issue #119.
      - wrong:   non-symlink file in slot; dangling non-retired link; OR a
                 retired link with no reachable_elsewhere fallback (broken, or
                 the pathological only-path-live-Cellar case).
      - missing: empty slot, not reachable elsewhere.
    Retired check precedes the resolve()->ok branch so a LIVE Cellar link is
    classed stale, not masked as ok.
    """
    out: list[tuple[str, str]] = []
    for name in _cctally().SETUP_SYMLINK_NAMES:
        dst = dst_dir / name
        reachable = _reachable_elsewhere(name)
        if dst.is_symlink():
            if _setup_symlink_is_retired(dst, name, repo_root):
                out.append((name, "stale" if reachable else "wrong"))
                continue
            try:
                dst.resolve(strict=True)
                out.append((name, "ok"))
            except (FileNotFoundError, OSError):
                out.append((name, "wrong"))
        elif dst.exists():
            out.append((name, "wrong"))
        elif reachable:
            out.append((name, "ok"))
        else:
            out.append((name, "missing"))
    return out


def _setup_detect_stale_symlinks(dst_dir: pathlib.Path) -> list[str]:
    """Return names of stale-but-still-present cctally symlinks in ``dst_dir``.

    Mirrors :func:`_setup_cleanup_stale_symlinks` detection (without
    removing): a symlink is "stale" when its name appears in
    :data:`_SETUP_STALE_SYMLINK_NAMES`, its readlink target basename
    matches that name, AND the target is "retired" — either dangling
    OR pointing at a foreign cctally install root (see
    :func:`_setup_symlink_is_retired`). Routing through the same
    predicate keeps ``--status`` and ``setup`` in lockstep so a
    manually-maintained link to a still-shipped retired tool
    (``~/.local/bin/cctally-release -> <checkout>/bin/cctally-release``)
    is neither reported as stale nor removed, while a legacy link left
    over from a pre-v1.9.0 brew/npm install (target still resolves into
    the old keg / ``node_modules`` tree) IS retired.
    """
    found: list[str] = []
    repo_root = _setup_resolve_repo_root()
    for name in _SETUP_STALE_SYMLINK_NAMES:
        dst = dst_dir / name
        if _setup_symlink_is_retired(dst, name, repo_root):
            found.append(name)
    return found


def _setup_status(args: argparse.Namespace) -> int:
    c = _cctally()
    repo_root = _setup_resolve_repo_root()
    dst_dir = _setup_local_bin_dir()
    sym_state = _setup_compute_symlink_state(repo_root, dst_dir)
    sym_ok = sum(1 for _, s in sym_state if s in ("ok", "stale"))     # available = ok + stale
    active_stale = [n for n, s in sym_state if s == "stale"]
    retired_stale = _setup_detect_stale_symlinks(dst_dir)
    stale_syms = list(dict.fromkeys(active_stale + retired_stale))    # union, order-stable
    is_brew = _setup_is_brew_install(repo_root)
    on_path = _setup_path_includes_local_bin()
    try:
        settings = c._load_claude_settings()
    except c.SetupError as exc:
        eprint(f"setup: warning: {exc}")
        settings = {}
    hook_counts = _setup_count_hook_entries(settings)
    oauth = _setup_oauth_token_present()
    throttle_age = c._hook_tick_throttle_age_seconds()
    activity = _setup_recent_log_stats()
    legacy = _setup_detect_legacy_snippet()
    bespoke = _setup_detect_legacy_bespoke_hooks(settings)
    data_bytes = _setup_data_dir_size_bytes()

    if getattr(args, "json", False):
        envelope = {
            "schema_version": 1,
            "install": {
                "symlinks_present": sym_ok,
                "symlinks_total": len(c.SETUP_SYMLINK_NAMES),
                "symlinks_stale": stale_syms,
                "path_includes": on_path,
            },
            "hooks": {ev: hook_counts[ev] for ev in c.SETUP_HOOK_EVENTS},
            "auth": {
                "oauth_token_present": oauth,
                "last_fetch_age_s": (
                    None if throttle_age == float("inf") else int(throttle_age)
                ),
            },
            "activity_24h": activity,
            "legacy": {
                "statusline_snippet": str(legacy[0]) if legacy else None,
                "bespoke_hooks": {
                    "detected": bespoke["detected"],
                    "settings_entries": bespoke["settings_entries"],
                    "files": bespoke["files"],
                },
            },
            "data": {"path": str(_cctally_core.APP_DIR), "size_bytes": data_bytes},
        }
        print(json.dumps(envelope, indent=2))
        return 0

    out: list[str] = []
    out.append("Install")
    sym_marker = "✓" if sym_ok == len(c.SETUP_SYMLINK_NAMES) else "✗"
    out.append(f"  Symlinks       {sym_ok}/{len(c.SETUP_SYMLINK_NAMES)} available at {dst_dir}/  {sym_marker}")
    if stale_syms:
        out.append(
            f"  Stale symlinks {len(stale_syms)} from prior version: {', '.join(stale_syms)}  ⚠"
        )
        out.append("                 run `cctally setup` to remove")
    if is_brew and (on_path or any(s in ("ok", "stale") for _, s in sym_state)):
        prefix = _setup_brew_prefix(repo_root)
        out.append(f"  PATH           brew: commands via {prefix}/bin                 ✓")
    else:
        out.append(f"  PATH includes  {'yes' if on_path else 'no'}                                   "
                   f"{'✓' if on_path else '⚠'}")
    out.append(f"Hooks ({_cctally_core.CLAUDE_SETTINGS_PATH})")
    for ev in c.SETUP_HOOK_EVENTS:
        marker = "✓" if hook_counts[ev] >= 1 else "✗"
        word = "installed" if hook_counts[ev] >= 1 else "missing"
        out.append(f"  {ev:14s} {word:24s} {marker}")
    out.append("Auth")
    out.append(f"  OAuth token    {'present' if oauth else 'missing'}                              "
               f"{'✓' if oauth else '⚠'}")
    if throttle_age == float("inf"):
        out.append("  Last fetch     never")
    else:
        out.append(f"  Last fetch     {int(throttle_age)}s ago")
    out.append("Hook activity (last 24h)")
    by_ev = ", ".join(f"{ev} {activity['by_event'].get(ev, 0)}" for ev in c.SETUP_HOOK_EVENTS)
    out.append(f"  Fires          {activity['fires']}  ({by_ev})")
    out.append(f"  OAuth          {activity['oauth_ok']} ({activity['throttled']} throttled)")
    out.append(f"  Errors         {activity['errors']}")
    if activity["last_fire_ago_s"] is None:
        out.append("  Last fire      none")
    else:
        out.append(f"  Last fire      {activity['last_fire_ago_s']}s ago")
    out.append("Legacy")
    if legacy is None:
        out.append("  status-line snippet  not detected                     ✓")
    else:
        out.append(f"  status-line snippet  detected at {legacy[0]}:{legacy[1][0]}  ⚠")
    if not bespoke["detected"]:
        out.append("  bespoke hooks        not detected                     ✓")
    else:
        n_entries = len(bespoke["settings_entries"])
        n_files = len(bespoke["files"])
        out.append(
            f"  bespoke hooks        detected ({n_entries} entries, {n_files} files)    ⚠"
        )
        out.append("                       run `cctally setup --migrate-legacy-hooks` to migrate")
    out.append("Data")
    out.append(f"  {_cctally_core.APP_DIR}/    {_setup_format_bytes(data_bytes)}")
    _setup_emit_text(out)
    return 0


def _setup_uninstall(args: argparse.Namespace) -> int:
    c = _cctally()
    purge = bool(getattr(args, "purge", False))
    yes = bool(getattr(args, "yes", False))
    is_json = bool(getattr(args, "json", False))

    out: list[str] = []
    try:
        settings = c._load_claude_settings()
    except c.SetupError as exc:
        eprint(f"setup: {exc}")
        return 1
    settings, removed = _settings_merge_uninstall(settings)
    if removed:
        try:
            c._write_claude_settings_atomic(settings)
        except OSError as exc:
            eprint(f"setup: failed to write {_cctally_core.CLAUDE_SETTINGS_PATH}: {exc}")
            return 2
    out.append(f"Removed {removed} hook entries from {_cctally_core.CLAUDE_SETTINGS_PATH}")

    repo_root = _setup_resolve_repo_root()
    dst_dir = _setup_local_bin_dir()
    sym_removed = 0
    for name in c.SETUP_SYMLINK_NAMES:
        dst = dst_dir / name
        if dst.is_symlink():
            try:
                target = pathlib.Path(os.readlink(dst))
            except OSError:
                target = None
            expected = _setup_resolve_symlink_source(repo_root, name)
            if target == expected:
                try:
                    dst.unlink()
                    sym_removed += 1
                except OSError as exc:
                    eprint(f"setup: failed to remove {dst}: {exc}")
    # Also clean up legacy symlinks that older cctally versions used to
    # install but the current version no longer manages (see
    # :data:`_SETUP_STALE_SYMLINK_NAMES`). Without this loop, an
    # upgrader who ran ``cctally setup`` on a prior version and now runs
    # ``cctally setup --uninstall`` would keep e.g.
    # ``~/.local/bin/cctally-release`` on PATH.
    #
    # Two removal predicates compose here:
    #   1. ``target == _setup_resolve_symlink_source(repo_root, name)``
    #      — the legacy auto-installed link points at *this* install
    #      root's binary (the common case for git-checkout upgraders
    #      who ran ``cctally setup`` on a prior version, then ``git
    #      pull``ed; symmetric with how the active-symlink loop above
    #      treats user-pointed `cctally` itself).
    #   2. ``_setup_symlink_is_retired`` — target is dangling OR lives
    #      under a *foreign* cctally install root (old brew keg /
    #      ``node_modules/cctally/`` left behind by ``brew upgrade``
    #      or ``npm i -g cctally@<new>``). Without this, the common
    #      brew/npm upgrade-then-uninstall path leaves the legacy
    #      symlink in place because the prior keg's binary still
    #      exists on disk (``target != expected``).
    #
    # A maintainer's hand-rolled link pointing somewhere unrelated
    # (``~/scripts/...``) is preserved by both predicates.
    for name in _SETUP_STALE_SYMLINK_NAMES:
        dst = dst_dir / name
        if not dst.is_symlink():
            continue
        try:
            target = pathlib.Path(os.readlink(dst))
        except OSError:
            continue
        expected = _setup_resolve_symlink_source(repo_root, name)
        should_remove = (
            target == expected
            or _setup_symlink_is_retired(dst, name, repo_root)
        )
        if not should_remove:
            continue
        try:
            dst.unlink()
            sym_removed += 1
        except OSError as exc:
            eprint(f"setup: failed to remove {dst}: {exc}")
    out.append(f"Removed {sym_removed} symlinks from {dst_dir}/")

    legacy = _setup_detect_legacy_snippet()
    if legacy is not None:
        out.append(
            f"Note: legacy status-line snippet found in {legacy[0]} — leaving untouched."
        )

    data_bytes = _setup_data_dir_size_bytes()
    if purge:
        if not yes:
            if is_json:
                # Spec: under --json without --yes, auto-decline. Script with --yes instead.
                print(json.dumps({
                    "schema_version": 1,
                    "mode": "uninstall",
                    "result": "purge_declined",
                    "reason": "json_without_yes",
                    "hooks_removed": removed,
                    "symlinks_removed": sym_removed,
                    "purged": False,
                    "data_path": str(_cctally_core.APP_DIR),
                    "data_size_bytes": data_bytes,
                    "legacy": {
                        "statusline_snippet_path": str(legacy[0]) if legacy else None,
                    },
                    "exit_code": 3,
                }, indent=2))
                return 3
            if data_bytes > 0:
                try:
                    resp = input(
                        f"Wipe {_setup_format_bytes(data_bytes)} of usage history at "
                        f"{_cctally_core.APP_DIR}/? [y/N] "
                    )
                except EOFError:
                    resp = "n"
                if resp.strip().lower() not in ("y", "yes"):
                    out.append("Purge declined.")
                    _setup_emit_text(out)
                    return 3
        if _cctally_core.APP_DIR.exists():
            try:
                shutil.rmtree(_cctally_core.APP_DIR)
                out.append(f"Wiped {_cctally_core.APP_DIR}/")
            except OSError as exc:
                if is_json:
                    print(json.dumps({
                        "schema_version": 1,
                        "mode": "uninstall",
                        "result": "err",
                        "reason": "rmtree_failed",
                        "error": str(exc),
                        "data_path": str(_cctally_core.APP_DIR),
                        "data_size_bytes": data_bytes,
                        "legacy": {
                            "statusline_snippet_path": str(legacy[0]) if legacy else None,
                        },
                        "exit_code": 1,
                    }, indent=2))
                else:
                    eprint(f"setup: failed to wipe {_cctally_core.APP_DIR}: {exc}")
                return 1
    else:
        out.append(
            f"Note: usage history kept at {_cctally_core.APP_DIR}/ "
            f"({_setup_format_bytes(data_bytes)}). Use --purge to remove."
        )
    if is_json:
        envelope = {
            "schema_version": 1,
            "mode": "uninstall",
            "result": "ok",
            "hooks_removed": removed,
            "symlinks_removed": sym_removed,
            "purged": purge,
            "data_path": str(_cctally_core.APP_DIR),
            "data_size_bytes": data_bytes,
            "legacy": {
                "statusline_snippet_path": str(legacy[0]) if legacy else None,
            },
            "exit_code": 0,
        }
        print(json.dumps(envelope, indent=2))
        return 0
    _setup_emit_text(out)
    return 0


def _setup_dry_run(args: argparse.Namespace) -> int:
    c = _cctally()
    repo_root = _setup_resolve_repo_root()
    dst_dir = _setup_local_bin_dir()
    try:
        settings = c._load_claude_settings()
    except c.SetupError as exc:
        # Malformed settings.json — preview still proceeds; legacy detection
        # against an empty dict simply yields detected=False for entries (files
        # detection is independent of settings). Mirror _setup_status's pattern
        # so the user sees the same condition that would fail _setup_install.
        eprint(f"setup: warning: {exc}")
        settings = {}
    detection = _setup_detect_legacy_bespoke_hooks(settings)
    sym_results = []
    for name in c.SETUP_SYMLINK_NAMES:
        dst = dst_dir / name
        src = _setup_resolve_symlink_source(repo_root, name)
        if dst.is_symlink() and pathlib.Path(os.readlink(dst)) == src:
            sym_results.append((name, "already"))
        elif dst.exists() and not dst.is_symlink():
            sym_results.append((name, "blocked"))
        else:
            sym_results.append((name, "would-create"))
    new = sum(1 for _, s in sym_results if s == "would-create")
    same = sum(1 for _, s in sym_results if s == "already")
    blocked = [name for name, s in sym_results if s == "blocked"]
    is_brew = _setup_is_brew_install(repo_root)
    out: list[str] = []
    if is_brew:
        prefix = _setup_brew_prefix(repo_root)
        out.append(f"Brew install — would skip ~/.local/bin/ symlinks "
                   f"(commands on PATH via {prefix}/bin/)")
    else:
        out.append(
            f"Would symlink {len(c.SETUP_SYMLINK_NAMES)} files to {dst_dir}/ "
            f"({same} already correct, {new} new)"
        )
        if blocked:
            out.append(f"⚠ Blocked (non-symlink files exist): {', '.join(blocked)}")
            out.append("  Remove them manually then re-run.")

    out.append(f"Would add {len(c.SETUP_HOOK_EVENTS)} hook entries to {_cctally_core.CLAUDE_SETTINGS_PATH}:")
    abs_path = str(_setup_resolve_hook_target(repo_root))
    import shlex
    quoted = shlex.quote(abs_path)
    for ev in c.SETUP_HOOK_EVENTS:
        matcher = '"*"' if ev == "PostToolBatch" else '""'
        out.append(
            f"  hooks.{ev}[*] += {{ matcher: {matcher}, "
            f"command: \"{quoted} hook-tick\" }}"
        )
    # Spec §2 mode×flag matrix — three distinct dry-run rendering paths
    # when legacy is detected:
    #   --dry-run --no-migrate-legacy-hooks → migration block omitted entirely
    #   --dry-run --migrate-legacy-hooks (or --yes) → full migration plan
    #   --dry-run (no migrate flag) → full plan prefixed with the
    #       "would prompt; pass --migrate-legacy-hooks…" note
    # `--yes` is treated as equivalent to `--migrate-legacy-hooks` to
    # match `_setup_decide_legacy_migration` (bin/cctally:22094-22101).
    no_migrate_flag = bool(getattr(args, "no_migrate_legacy_hooks", False))
    migrate_flag = bool(getattr(args, "migrate_legacy_hooks", False))
    yes_flag = bool(getattr(args, "yes", False))
    show_full_migration_plan = migrate_flag or yes_flag
    show_migration_block = detection["detected"] and not no_migrate_flag
    if show_migration_block:
        if not show_full_migration_plan:
            # No-flag dry-run: prefix the block with the would-prompt note.
            out.append(
                "Would prompt for migration; pass --migrate-legacy-hooks to "
                "preview the migration plan."
            )
        out.append("Would migrate legacy bespoke hooks:")
        if detection["settings_entries"]:
            out.append(
                f"  Would remove {len(detection['settings_entries'])} "
                f"entries from settings.json:"
            )
            for e in detection["settings_entries"]:
                out.append(f"    hooks.{e['event']:13s} ← {e['command']}")
        files_present = [f.split('/')[-1] for f in detection["files"]]
        if files_present:
            out.append(
                f"  Would move {len(files_present)} files to "
                f"~/.claude/cctally-legacy-hook-backup-<UTC ts>/:"
            )
            out.append(f"    {', '.join(files_present)}")
        out.append("  Would attempt cleanup of /tmp/claude-usage-poller.{pid,count}")
    out.append("Would not modify ~/.claude/statusline-command.sh")
    out.append("Would not delete any data")
    out.append("")
    out.append("Re-run without --dry-run to apply.")

    if getattr(args, "json", False):
        # Decision label mirrors `_setup_decide_legacy_migration`'s output:
        # `migrate` (full plan / explicit opt-in or --yes), `skip`
        # (--no-migrate-legacy-hooks), or `prompt` (no flag — install would
        # prompt the user). When no legacy is detected the label is
        # `not_detected` so consumers can distinguish "no-op" from
        # "explicit skip."
        if not detection["detected"]:
            decision = "not_detected"
        elif no_migrate_flag:
            decision = "skip"
        elif show_full_migration_plan:
            decision = "migrate"
        else:
            decision = "prompt"
        legacy_path = _setup_detect_legacy_snippet()
        envelope = {
            "schema_version": 1,
            "mode": "dry-run",
            "symlinks": (
                {"skipped": True, "reason": "brew", "would_create": 0,
                 "already": 0, "blocked": [], "destination": str(dst_dir),
                 "total": 0,
                 "would_remove_stale": [
                     n for n, s in _setup_compute_symlink_state(repo_root, dst_dir)
                     if s == "stale"
                 ]}
                if is_brew else
                {"would_create": new, "already": same, "blocked": blocked,
                 "destination": str(dst_dir), "total": len(c.SETUP_SYMLINK_NAMES)}
            ),
            "hooks": {
                "would_add": [
                    {
                        "event": ev,
                        "matcher": "*" if ev == "PostToolBatch" else "",
                        "command": f"{quoted} hook-tick",
                    }
                    for ev in c.SETUP_HOOK_EVENTS
                ],
                "settings_path": str(_cctally_core.CLAUDE_SETTINGS_PATH),
            },
            # Sibling parity with `_setup_status` and `_setup_install`
            # JSON envelopes (`legacy.bespoke_hooks` shape). Lets the same
            # consumer query bespoke-hook state from any of the three
            # commands uniformly.
            "legacy": {
                "statusline_snippet": str(legacy_path[0]) if legacy_path else None,
                "bespoke_hooks": {
                    "detected": detection["detected"],
                    "settings_entries": detection["settings_entries"],
                    "files": detection["files"],
                },
            },
            # Flag-aware preview block. `decision` records what the
            # install path would do; `would_remove_entries` /
            # `would_move_files` are the rendered plan (empty when
            # decision == "skip" or "not_detected").
            "migration_preview": {
                "detected": detection["detected"],
                "decision": decision,
                "would_remove_entries": (
                    []
                    if decision in ("skip", "not_detected")
                    else [
                        {"event": e["event"], "command": e["command"]}
                        for e in detection["settings_entries"]
                    ]
                ),
                "would_move_files": (
                    []
                    if decision in ("skip", "not_detected")
                    else list(detection["files"])
                ),
            },
            "exit_code": 0,
        }
        print(json.dumps(envelope, indent=2))
        return 0

    _setup_emit_text(out)
    return 0


def _setup_emit_text(lines: list[str]) -> None:
    for ln in lines:
        print(ln)


def _setup_render_legacy_prompt(detection: dict) -> str:
    """Return the multi-line prompt body per spec Section 2.

    Renders the ⚠ header, one row per detected (event → file) settings
    entry, an optional daemon-source line for usage-poller.py, the
    explanation of the silent failure mode, and the [Y/n] question.
    Caller is expected to print the body once and then dispatch to
    `_setup_read_legacy_prompt_input` for the actual answer.
    """
    lines = ["⚠ Detected legacy bespoke hooks (predate `cctally setup`):"]
    by_event = {e["event"]: e["command"] for e in detection["settings_entries"]}
    for ev in ("Stop", "SubagentStart", "SubagentStop"):
        cmd = by_event.get(ev, "")
        if cmd:
            file_part = cmd.replace("python3 ", "")
            lines.append(f"    {file_part:38s}  →  hooks.{ev}")
    if any("usage-poller.py" in f for f in detection["files"]):
        lines.append("    ~/.claude/hooks/usage-poller.py            (daemon spawned by usage-poller-start.py)")
    lines += [
        "",
        "  Their delegate binary isn't on PATH on this system — every fire has",
        "  been silently failing.",
        "",
        "  Migrate now? Will unwire the settings.json entries and move the .py files",
        "  to ~/.claude/cctally-legacy-hook-backup-<UTC ts>/. Reversible.",
        "",
        "  Migrate? [Y/n]",
    ]
    return "\n".join(lines)


def _setup_install(args: argparse.Namespace) -> int:
    """Install path. Returns exit code per Section 2 of spec."""
    c = _cctally()
    out: list[str] = []
    warnings = 0

    claude_dir = pathlib.Path.home() / ".claude"
    if not claude_dir.exists():
        eprint(
            f"~/.claude/ does not exist. If Claude Code isn't installed yet, "
            f"install it first. If it is installed, run `claude` once to "
            f"initialize, then re-run cctally setup."
        )
        return 1

    out.append(f"✓ Detected Claude Code at {claude_dir}")

    repo_root = _setup_resolve_repo_root()
    dst_dir = _setup_local_bin_dir()
    abs_path = str(_setup_resolve_hook_target(repo_root))

    # Validate settings.json BEFORE creating symlinks so a malformed
    # settings file leaves the filesystem untouched (spec §2.2 — exit
    # code 1 for "settings.json malformed"). Both calls are pure: load
    # only reads, merge mutates the in-memory dict only. The actual
    # backup + atomic write still happen after symlinks succeed.
    try:
        settings = c._load_claude_settings()
    except c.SetupError as exc:
        eprint(f"setup: {exc}")
        return 1

    # ── Legacy bespoke hook detection + migration decision (spec §1, §2) ──
    # Detection is read-only on the in-memory settings dict; decision is
    # pure (no I/O); the prompt fires only when the decision helper
    # returns "prompt" (TTY + no flag + not --json). All three must run
    # BEFORE `_settings_merge_install` so the unwire+add land in the same
    # atomic write at sequence position 6.
    detection = _setup_detect_legacy_bespoke_hooks(settings)
    decision, reason = _setup_legacy_decide_action(
        args,
        detected=detection["detected"],
        stdin_isatty=sys.stdin.isatty(),
    )
    if decision == "prompt":
        print(_setup_render_legacy_prompt(detection))
        accepted = _setup_read_legacy_prompt_input(
            sys.stdin,
            reprompt="Please answer y or n. Migrate? [Y/n]",
        )
        decision = "migrate" if accepted else "skip"
        if not accepted:
            reason = "user_declined"

    migration_summary: dict = {
        "performed": False,
        "reason": reason or "not_detected",
    }

    backup_dir: pathlib.Path | None = None
    if decision == "migrate":
        # Resolve the backup dir BEFORE mutating settings.json so a
        # mkdir failure (parent unwriteable, name collision with a
        # regular file, ENOSPC, …) doesn't leave the on-disk settings
        # in a half-applied state — legacy entries gone but .py files
        # never moved. Pre-resolving also pins the timestamp shared
        # between the dir name and JSON envelope.
        try:
            # Route through `cctally` (call-time lookup) so the existing
            # `monkeypatch.setitem(ns, "_legacy_resolve_backup_dir", ...)`
            # in `tests/test_setup_legacy_migrate.py::TestLegacyMigrationE2EBackupDirFail`
            # still propagates into this code path post-extraction (§5.6 option C).
            backup_dir = c._legacy_resolve_backup_dir()
        except OSError as exc:
            eprint(f"setup: cannot create migration backup dir: {exc}")
            return 1
        # Unwire BEFORE the merge so the same atomic write removes legacy
        # entries and adds cctally entries (spec §2 step 6).
        settings, n_unwired = _settings_merge_unwire_legacy(settings)
        migration_summary = {
            "performed": True,
            "settings_entries_removed": n_unwired,
            "files_moved": 0,
            "backup_dir": None,
            "active_poller_pid_signaled": None,
            "active_poller_kill_outcome": None,
            "tmp_files_unlinked": [],
        }

    try:
        _settings_merge_install(settings, abs_path)
    except c.SetupError as exc:
        eprint(f"setup: {exc}")
        return 1

    # Clean up symlinks left behind by prior cctally versions whose
    # subcommand surface has changed (e.g. v1.9.0 retired
    # `cctally-release` when release tooling went private). Only unlinks
    # symlinks whose target's basename matches the stale name; never
    # disturbs a user's hand-rolled symlink sharing the name.
    stale_results = _setup_cleanup_stale_symlinks(dst_dir)

    # Issue #119: brew owns <prefix>/bin/, never ~/.local/bin/. On a brew
    # install, skip symlink CREATION entirely (commands reach PATH via the
    # formula's <prefix>/bin/) but keep everything else — cleanup, hook
    # wiring, cache bootstrap. The new/same/repl counts are initialized to
    # 0 here so the install JSON envelope references are always bound on
    # the brew branch (sym_results stays empty).
    is_brew = _setup_is_brew_install(repo_root)
    new_count = same_count = repl_count = 0
    if is_brew:
        prefix = _setup_brew_prefix(repo_root)
        out.append(
            f"✓ Brew install detected — commands are on PATH via {prefix}/bin/; "
            f"skipping ~/.local/bin/ symlinks"
        )
        sym_results = []
    else:
        sym_results = _setup_create_symlinks(repo_root, dst_dir)
        failed = [r for r in sym_results if r.status == "failed"]
        if failed:
            for r in failed:
                eprint(f"setup: symlink {r.name} failed: {r.detail}")
            return 1
        new_count = sum(1 for r in sym_results if r.status == "created")
        same_count = sum(1 for r in sym_results if r.status == "already")
        repl_count = sum(1 for r in sym_results if r.status == "replaced")
        detail_parts = []
        if new_count:
            detail_parts.append(f"{new_count} newly created")
        if same_count:
            detail_parts.append(f"{same_count} already correct")
        if repl_count:
            detail_parts.append(f"{repl_count} re-pointed")
        detail = ", ".join(detail_parts) or "no changes"
        out.append(f"✓ Symlinks at {dst_dir}/: {len(sym_results)}/{len(sym_results)} ({detail})")
    removed_stale = [r for r in stale_results if r.status == "removed-stale"]
    failed_stale = [r for r in stale_results if r.status == "failed"]
    if removed_stale:
        out.append(
            "✓ Cleaned up stale symlink(s) from prior version: "
            + ", ".join(r.name for r in removed_stale)
        )
    for r in failed_stale:
        out.append(f"⚠ Could not remove stale {r.name}: {r.detail}")
        warnings += 1

    if not is_brew and not _setup_path_includes_local_bin():
        warnings += 1
        rc = _setup_shell_rc_hint()
        out.append(f"⚠ {dst_dir} is not on your PATH. Add to {rc}:")
        out.append(f"    export PATH=\"$HOME/.local/bin:$PATH\"")
        out.append("  Then reload (`source ...`) or open a new terminal.")
        out.append("  (Hooks still work — we used absolute paths in settings.json.)")

    # Pinned-only-path guidance (issue #119 finding #10): if any active
    # name's slot is a live retired link with no reachable_elsewhere
    # fallback, setup deliberately won't remove it (would break the only
    # reachable copy) — surface the actionable PATH fix instead of silence.
    pinned = [
        n for n, s in _setup_compute_symlink_state(repo_root, dst_dir)
        if s == "wrong" and (dst_dir / n).is_symlink()
        and _setup_symlink_is_retired(dst_dir / n, n, repo_root)
        and (dst_dir / n).resolve(strict=False).exists()
    ]
    if pinned:
        prefix = _setup_brew_prefix(repo_root) if is_brew else "<prefix>"
        out.append(
            f"⚠ cctally is reachable only via a legacy ~/.local/bin link. "
            f"Put {prefix}/bin on your PATH (eval \"$(brew shellenv)\"), then "
            f"re-run cctally setup to clean it."
        )
        warnings += 1

    c._backup_claude_settings()
    try:
        c._write_claude_settings_atomic(settings)
    except OSError as exc:
        eprint(f"setup: failed to write {_cctally_core.CLAUDE_SETTINGS_PATH}: {exc}")
        return 2

    # ── Post-write migration apply (spec §2 steps 6a, 6b) ──
    # Settings.json is now durable. File moves, poller stop, and tmp
    # cleanup are best-effort and may emit a partial-move warning, but
    # do NOT roll back the on-disk settings.json. Per spec §2 exit-code
    # table, partial-move failures are uniformly exit-0-with-warning.
    if decision == "migrate":
        # `backup_dir` was resolved early (pre-write) so the mkdir
        # failure path can fail fast with no settings.json mutation.
        assert backup_dir is not None
        # Snapshot what we expected to move BEFORE the move so we can
        # detect partial failure cleanly (post-loop, src files are gone).
        expected_to_move = [
            n for n in c._LEGACY_BESPOKE_FILENAMES
            if (c._LEGACY_BESPOKE_HOOKS_DIR / n).exists()
        ]
        moved = _legacy_move_files_to_backup(backup_dir)
        migration_summary["files_moved"] = len(moved)
        migration_summary["backup_dir"] = str(backup_dir)
        if len(moved) < len(expected_to_move):
            orphans = sorted(set(expected_to_move) - {p.name for p in moved})
            out.append(
                f"⚠ Partial file move: {len(moved)} of {len(expected_to_move)} expected "
                f"files moved. Orphans: {', '.join(orphans)}"
            )
            warnings += 1

        # Active-poller stop + tmp-sentinel cleanup (best-effort, silent
        # on failure per spec §2 step 6b). Capture the pre-stop PID for
        # the JSON envelope since the helper itself returns only the
        # outcome string.
        pid_signaled: int | None = None
        if c._LEGACY_POLLER_PID_FILE.exists():
            try:
                pid_signaled = int(
                    c._LEGACY_POLLER_PID_FILE.read_text(encoding="utf-8", errors="replace").strip()
                )
            except (OSError, ValueError):
                pass
        kill_outcome = _legacy_stop_active_poller()
        # Per spec §3 (`active_poller_pid_signaled` semantics): record the
        # PID only when we actually attempted to deliver a signal. Stale-PID
        # and no-pid-file outcomes are read-only paths, so the JSON envelope
        # should reflect "no signal sent" with a null PID.
        if kill_outcome not in {"sigterm-took", "sigkill-took", "permission-denied"}:
            pid_signaled = None
        migration_summary["active_poller_pid_signaled"] = pid_signaled
        migration_summary["active_poller_kill_outcome"] = kill_outcome
        migration_summary["tmp_files_unlinked"] = _legacy_cleanup_tmp_sentinels()

        out.append(
            f"✓ Migrated {migration_summary['settings_entries_removed']} legacy hook entries "
            f"→ moved {len(moved)} files to {backup_dir}/"
        )

    # The "✓ Wrote …" line follows any migrate-summary line so the
    # narrative reads "we did the migration, then wrote the new entries"
    # — matches the spec's success-path sample (Section 2).
    out.append(f"✓ Wrote {len(c.SETUP_HOOK_EVENTS)} hook entries to {_cctally_core.CLAUDE_SETTINGS_PATH}")

    if decision == "skip" and reason in {"user_declined", "no_migrate_flag"}:
        files_str = "{record-usage-stop,usage-poller{,-start,-stop}}.py"
        out.append(
            f"⚠ Legacy bespoke hooks detected (predate `cctally setup`; failing "
            f"silently on this system). Skipped at your request. Re-run "
            f"`cctally setup --migrate-legacy-hooks` later, or remove them yourself. "
            f"The four `.py` files are at ~/.claude/hooks/{files_str}."
        )
        warnings += 1

    oauth = _setup_oauth_token_present()
    if oauth:
        out.append("✓ Detected OAuth token")
    else:
        warnings += 1
        out.append("⚠ No Claude OAuth token detected.")
        out.append("  Run `claude` once to authenticate. After that, the next assistant")
        out.append("  message in any Claude Code session will start collecting data")
        out.append("  automatically — no need to re-run `cctally setup`.")

    legacy = _setup_detect_legacy_snippet()
    if legacy is not None:
        warnings += 1
        path, hits = legacy
        line_str = ":".join(str(h) for h in hits[:1])
        out.append(f"⚠ Found legacy status-line snippet at {path}:{line_str}")
        out.append("  No need for it anymore — hooks now handle this. It's harmless to")
        out.append("  leave (data is funneled correctly either way), but you can remove")
        out.append("  it whenever you want. We won't touch the file.")

    # Bootstrap (non-fatal). sync_cache requires a connection arg — mirror
    # the pattern from cmd_hook_tick (Task 2 fix).
    bootstrap_rows: int | None = None
    bootstrap_oauth_status: str | None = None
    try:
        cache_conn = c.open_cache_db()
        try:
            stats = c.sync_cache(cache_conn)
            rows = int(stats.rows_changed)
        finally:
            try:
                cache_conn.close()
            except Exception:
                pass
        bootstrap_rows = rows
        # `rows` counts both genuine INSERTs and ccusage-parity DO UPDATE
        # replacements (see IngestStats.rows_changed). On first install
        # this is always 0-vs-N pure inserts (cache is empty), so "N new
        # entries" is exactly accurate. On a re-install / upgrade path
        # with active sessions, `rows` also counts UPSERT replacements
        # (streaming-vs-final tiebreaker swaps), so the count is more
        # accurately "ingest activity" than "rows newly added" — but
        # we keep "new entries" because (a) it's still a useful signal
        # to the operator that the cache is alive, and (b) the dominant
        # case (first install) reads literally.
        out.append(f"✓ Synced session cache ({rows} new entries)")
    except Exception as exc:
        out.append(f"⚠ sync_cache during bootstrap failed: {exc}")
        warnings += 1
    if oauth:
        try:
            status, _ = c._hook_tick_oauth_refresh()
            bootstrap_oauth_status = status
            if status.startswith("ok"):
                c._hook_tick_throttle_touch()
                out.append(f"✓ Bootstrapped weekly usage ({status})")
            else:
                out.append(f"⚠ Bootstrap OAuth fetch: {status}")
                warnings += 1
        except Exception as exc:
            bootstrap_oauth_status = f"err({type(exc).__name__})"
            out.append(f"⚠ Bootstrap OAuth failed: {exc}")
            warnings += 1

    out.append("")
    if warnings:
        out.append(f"cctally is ready (with {warnings} warning(s) above).")
    else:
        out.append("cctally is ready.")
    out.append("")
    # Settings.json was modified — CC caches it at session start. The
    # warning fires unconditionally because `_setup_install` always
    # rewrites settings.json (legacy migration, fresh install, repair).
    out.append("⚠ Restart Claude Code for the new hooks to take effect in any currently")
    out.append("  open sessions. New sessions launched after this point pick them up")
    out.append("  automatically. (settings.json is cached at session start.)")
    out.append("")
    out.append("  Try:")
    out.append("    cctally daily              # last 30 days")
    out.append("    cctally dashboard          # live web dashboard")
    out.append("    cctally tui                # terminal dashboard")
    out.append("    cctally setup --status     # verify install state")

    # Install-time telemetry disclosure (spec 2026-07-07 §4). Text summary
    # only — the --json envelope carries a structured `telemetry` field
    # instead (never prose). Shown unconditionally as a factual disclosure of
    # the on-by-default, opt-out install-count beat; the opt-out command +
    # docs link are always surfaced so the fact is discoverable even when the
    # interactive first-run notice never fires (headless / statusline-only).
    out.append("")
    out.append("cctally counts anonymous active installs to gauge real usage.")
    out.append("What's sent: a rotating, un-linkable token + version + OS family.")
    out.append("No identity, no paths, no usage data, no IP stored. Auto-expires monthly.")
    out.append("Opt out anytime:  cctally telemetry off   (or CCTALLY_DISABLE_TELEMETRY=1)")
    out.append("How it works:     https://github.com/omrikais/cctally/blob/main/docs/telemetry.md")

    if getattr(args, "json", False):
        # JSON-safe telemetry disclosure (spec 2026-07-07 §4): a structured
        # field, never prose. Resolved READ-ONLY via `resolve_telemetry_state`
        # (side-effect-free — mints no install_id, writes no config), so the
        # envelope reports the opt-out state without arming telemetry.
        tele_enabled, tele_reason = c.resolve_telemetry_state(c.load_config())
        envelope = {
            "schema_version": 1,
            "mode": "install",
            "result": "warn" if warnings else "ok",
            "symlinks": {
                "created": new_count,
                "already": same_count,
                "replaced": repl_count,
                "total": len(sym_results),          # 0 on brew (sym_results == [])
                "destination": str(dst_dir),
                **({"skipped": True, "reason": "brew",
                    "stale_removed": [r.name for r in stale_results if r.status == "removed-stale"]}
                   if is_brew else {}),
            },
            "hooks": {
                "events_added": list(c.SETUP_HOOK_EVENTS),
                "settings_path": str(_cctally_core.CLAUDE_SETTINGS_PATH),
            },
            "auth": {
                "oauth_token_present": oauth,
            },
            "path_includes_local_bin": _setup_path_includes_local_bin(),
            "legacy": {
                "statusline_snippet_path": str(legacy[0]) if legacy else None,
                "bespoke_hooks": {
                    "detected": detection["detected"],
                    "settings_entries": detection["settings_entries"],
                    "files": detection["files"],
                },
            },
            "migration": migration_summary,
            "bootstrap": {
                "session_cache_rows": bootstrap_rows,
                "oauth_status": bootstrap_oauth_status,
            },
            "telemetry": {
                "enabled": tele_enabled,
                "reason": tele_reason,
            },
            "warnings_count": warnings,
            "exit_code": 0,
        }
        print(json.dumps(envelope, indent=2))
        return 0

    _setup_emit_text(out)
    return 0


# ── entry point ────────────────────────────────────────────────────────


def cmd_setup(args: argparse.Namespace) -> int:
    # Dev-instance isolation (§3): refuse the MUTATING modes (install +
    # uninstall) when run from a git checkout, unless --force-dev. Those
    # rewrite ~/.claude/settings.json (prod's hooks), which is NOT under
    # APP_DIR — from the dev checkout this would repoint prod's hooks at the
    # dev binary or remove them. --status / --dry-run are read-only previews
    # (they never write settings.json) and stay usable from a checkout, so
    # the guard is scoped to the write modes only. The three mode flags are a
    # mutually-exclusive argparse group, so the write modes (uninstall +
    # default install) are exactly the complement of {status, dry_run}.
    # Keyed on _is_dev_checkout() (NOT DEV_MODE / the cctally-dev path
    # string), so a per-branch CCTALLY_DATA_DIR override relocates the data
    # dir but still cannot rewrite prod's hooks (the F1 fix). The test
    # suppressor forces _is_dev_checkout() False, so the setup tests +
    # golden harness behave exactly like prod.
    mode_is_mutating = not (
        getattr(args, "status", False) or getattr(args, "dry_run", False)
    )
    if (
        mode_is_mutating
        and _cctally_core._is_dev_checkout()
        and not getattr(args, "force_dev", False)
    ):
        eprint(_DEV_SETUP_REFUSAL_MSG.format(data_dir=_cctally_core.APP_DIR))
        return 2
    # P2: --force-dev install on a checkout with CCTALLY_DATA_DIR set splits
    # interactive runs (override dir) from background hook fires (auto-detect
    # dir, since the saved hook command can't carry the override env). Only
    # the install path writes hooks, so scope the warning to it (uninstall
    # removes hooks; --status/--dry-run don't write). Fires only on the
    # doubly-rare --force-dev + CCTALLY_DATA_DIR combination.
    is_install = not (
        getattr(args, "status", False)
        or getattr(args, "dry_run", False)
        or getattr(args, "uninstall", False)
    )
    override_dir = os.environ.get("CCTALLY_DATA_DIR", "").strip()
    if (
        is_install
        and _cctally_core._is_dev_checkout()
        and getattr(args, "force_dev", False)
        and override_dir
    ):
        autodetect_dir = pathlib.Path.home() / ".local" / "share" / "cctally-dev"
        eprint(_DEV_SETUP_FORCE_DEV_OVERRIDE_WARNING.format(
            override_dir=pathlib.Path(override_dir).expanduser(),
            autodetect_dir=autodetect_dir,
        ))
    # Migration flags are install-mode-only. Reject combinations with
    # --status or --uninstall (per spec Section 2 mode×flag matrix). The
    # mutex group on the parser already prevents both flags being set
    # together; here we guard the mode-axis pairing that argparse can't
    # express in a single mutex group.
    mig_flag = (
        "--migrate-legacy-hooks" if getattr(args, "migrate_legacy_hooks", False)
        else "--no-migrate-legacy-hooks" if getattr(args, "no_migrate_legacy_hooks", False)
        else None
    )
    if mig_flag and (getattr(args, "status", False) or getattr(args, "uninstall", False)):
        eprint(f"setup: {mig_flag} is install-mode only")
        return 2
    if getattr(args, "uninstall", False):
        return _setup_uninstall(args)
    if getattr(args, "status", False):
        return _setup_status(args)
    if getattr(args, "dry_run", False):
        return _setup_dry_run(args)
    return _setup_install(args)
