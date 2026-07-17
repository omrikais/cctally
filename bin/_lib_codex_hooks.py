"""Pure Codex native-hook planning and bounded lifecycle-lock primitives."""
from __future__ import annotations

import copy
import datetime as dt
import fcntl
import json
import os
import pathlib
import shlex
import shutil
from dataclasses import dataclass
from typing import Callable

from _lib_source_identity import source_root_key


CODEX_HOOK_EVENTS = ("Stop", "SubagentStop")
CODEX_HOOK_TIMEOUT_SECONDS = 30
CODEX_HOOK_THROTTLE_SECONDS = 15


class CodexHooksError(ValueError):
    """A hooks.json shape is unsafe to modify."""


@dataclass(frozen=True)
class CodexHookRoot:
    source_root_key: str
    codex_home: pathlib.Path
    hooks_path: pathlib.Path


@dataclass
class CodexLifecycleLock:
    root: CodexHookRoot
    marker_path: pathlib.Path
    lock_path: pathlib.Path
    fd: int


def codex_hook_command(binary: str) -> str:
    path = pathlib.Path(binary)
    if not path.is_absolute():
        raise CodexHooksError("Codex hook target must be an absolute path")
    return f"{shlex.quote(str(path))} hook-tick --foreground --source codex"


def _command_tokens(command: object) -> list[str] | None:
    if not isinstance(command, str) or not command.strip():
        return None
    try:
        return shlex.split(command)
    except ValueError:
        return None


def is_owned_codex_hook_command(command: object, binary: str) -> bool:
    # The absolute installed path changes across package-manager upgrades, so
    # ownership is the exact argument tail plus an absolute ``cctally`` binary,
    # not a string-equality check against today's install location.
    tokens = _command_tokens(command)
    return bool(
        tokens
        and len(tokens) == 5
        and pathlib.Path(tokens[0]).is_absolute()
        and pathlib.Path(tokens[0]).name == "cctally"
        and tokens[1:] == ["hook-tick", "--foreground", "--source", "codex"]
    )


def _validate_document(document: object) -> dict:
    if not isinstance(document, dict):
        raise CodexHooksError("hooks.json must be a JSON object")
    hooks = document.get("hooks")
    if hooks is None:
        return document
    if not isinstance(hooks, dict):
        raise CodexHooksError("hooks.json: `hooks` is not an object")
    for event, groups in hooks.items():
        if not isinstance(event, str):
            raise CodexHooksError("hooks.json: event name is not a string")
        if not isinstance(groups, list):
            raise CodexHooksError(f"hooks.json: `hooks.{event}` is not a list")
        for index, group in enumerate(groups):
            if not isinstance(group, dict):
                raise CodexHooksError(
                    f"hooks.json: `hooks.{event}[{index}]` is not an object"
                )
            handlers = group.get("hooks")
            if not isinstance(handlers, list):
                raise CodexHooksError(
                    f"hooks.json: `hooks.{event}[{index}].hooks` is not a list"
                )
            if any(not isinstance(handler, dict) for handler in handlers):
                raise CodexHooksError(
                    f"hooks.json: `hooks.{event}[{index}].hooks` has a non-object handler"
                )
    return document


def _owned_handler(command: str) -> dict:
    return {"type": "command", "command": command, "timeout": CODEX_HOOK_TIMEOUT_SECONDS}


def _is_owned_handler(handler: dict, binary: str) -> bool:
    return is_owned_codex_hook_command(handler.get("command"), binary)


def is_canonical_owned_codex_hook_handler(handler: object, binary: str) -> bool:
    """Return true only for the one exact handler shape setup owns today."""
    if not isinstance(handler, dict):
        return False
    return handler == _owned_handler(codex_hook_command(binary))


def _normalize_owned_handler(handler: dict, command: str) -> None:
    handler.clear()
    handler.update(_owned_handler(command))


def _hook_groups(document: dict) -> dict:
    hooks = document.get("hooks")
    if hooks is None:
        hooks = {}
        document["hooks"] = hooks
    return hooks


def _plan_install(document: object, binary: str) -> tuple[dict, dict[str, int]]:
    _validate_document(document)
    planned = copy.deepcopy(document)
    hooks = _hook_groups(planned)
    command = codex_hook_command(binary)
    added: dict[str, int] = {}
    for event in CODEX_HOOK_EVENTS:
        groups = hooks.setdefault(event, [])
        found = False
        kept_groups: list[dict] = []
        for group in groups:
            kept_handlers: list[dict] = []
            for handler in group["hooks"]:
                if _is_owned_handler(handler, binary):
                    if not found:
                        _normalize_owned_handler(handler, command)
                        kept_handlers.append(handler)
                        found = True
                    continue
                kept_handlers.append(handler)
            if kept_handlers:
                group["hooks"] = kept_handlers
                kept_groups.append(group)
        hooks[event] = kept_groups
        if not found:
            kept_groups.append({"hooks": [_owned_handler(command)]})
            added[event] = 1
        else:
            added[event] = 0
    return planned, added


def _plan_uninstall(document: object, binary: str) -> tuple[dict, dict[str, int]]:
    _validate_document(document)
    planned = copy.deepcopy(document)
    hooks = planned.get("hooks")
    removed: dict[str, int] = {event: 0 for event in CODEX_HOOK_EVENTS}
    if not isinstance(hooks, dict):
        return planned, removed
    for event in CODEX_HOOK_EVENTS:
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        kept_groups: list[dict] = []
        for group in groups:
            kept_handlers = [
                handler for handler in group["hooks"]
                if not _is_owned_handler(handler, binary)
            ]
            removed[event] += len(group["hooks"]) - len(kept_handlers)
            if kept_handlers:
                group["hooks"] = kept_handlers
                kept_groups.append(group)
        if kept_groups:
            hooks[event] = kept_groups
        else:
            hooks.pop(event, None)
    if not hooks:
        planned.pop("hooks", None)
    return planned, removed


def _read_hooks_document(path: pathlib.Path) -> dict:
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CodexHooksError(f"cannot read {path}: {exc}") from exc
    if not raw.strip():
        return {}
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CodexHooksError(f"{path} is not valid JSON: {exc}") from exc
    return _validate_document(document)


def _backup_path(path: pathlib.Path) -> pathlib.Path:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d")
    return path.with_name(path.name + f".cctally-backup-{stamp}")


def harden_hooks_permissions(path: pathlib.Path) -> None:
    """Enforce the private-file posture even when a reinstall is content-idempotent."""
    try:
        os.chmod(path.parent, 0o700)
        if path.exists():
            os.chmod(path, 0o600)
    except OSError as exc:
        raise CodexHooksError(f"cannot secure {path}: {exc}") from exc


def _write_hooks_document_atomic(
    path: pathlib.Path,
    document: object | None = None,
    *,
    transform: Callable[[dict], tuple[dict, dict[str, int]]] | None = None,
    harden_unchanged: bool = False,
) -> pathlib.Path | None | tuple[dict, dict[str, int], bool, pathlib.Path | None]:
    if (document is None) == (transform is None):
        raise ValueError("provide exactly one of document or transform")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = path.with_name(path.name + ".cctally.lock")
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as exc:
        raise CodexHooksError(f"cannot lock {path}: {exc}") from exc
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        changes: dict[str, int] | None = None
        changed = True
        if transform is not None:
            current = _read_hooks_document(path)
            planned, changes = transform(current)
            _validate_document(planned)
            changed = planned != current
        else:
            planned = _validate_document(document)
        # Snapshot only after holding the writer lock: otherwise two setup
        # processes can both back up an obsolete pre-update document before
        # either reaches the atomic replacement below.
        backup: pathlib.Path | None = None
        if changed and path.exists():
            backup = _backup_path(path)
            if not backup.exists():
                try:
                    shutil.copy2(path, backup)
                    os.chmod(backup, 0o600)
                except OSError as exc:
                    raise CodexHooksError(f"cannot back up {path}: {exc}") from exc
        if changed:
            tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
            try:
                tmp.write_text(
                    json.dumps(planned, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                os.chmod(tmp, 0o600)
                os.replace(tmp, path)
                harden_hooks_permissions(path)
            except OSError as exc:
                try:
                    tmp.unlink()
                except OSError:
                    pass
                raise CodexHooksError(f"cannot write {path}: {exc}") from exc
        elif harden_unchanged and path.exists():
            harden_hooks_permissions(path)
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
    if transform is not None:
        assert changes is not None
        return planned, changes, changed, backup
    return backup


def codex_hook_roots(paths: list[pathlib.Path]) -> list[CodexHookRoot]:
    roots: dict[str, CodexHookRoot] = {}
    for raw in paths:
        try:
            home = raw.resolve()
        except OSError:
            home = raw.absolute()
        if not home.is_dir():
            continue
        key = source_root_key(str(home))
        roots.setdefault(key, CodexHookRoot(key, home, home / "hooks.json"))
    return [roots[key] for key in sorted(roots)]


def acquire_due_lifecycle_locks(
    app_dir: pathlib.Path,
    roots: list[CodexHookRoot],
    *,
    now: float,
    throttle_seconds: float = CODEX_HOOK_THROTTLE_SECONDS,
) -> list[CodexLifecycleLock]:
    base = app_dir / "codex-hook-tick"
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(base, 0o700)
    except OSError:
        pass
    held: list[CodexLifecycleLock] = []
    for root in sorted(roots, key=lambda item: item.source_root_key):
        lock_path = base / f"{root.source_root_key}.lock"
        marker_path = base / f"{root.source_root_key}.last-success"
        fd = -1
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            try:
                if fd >= 0:
                    os.close(fd)
            except OSError:
                pass
            continue
        try:
            age = now - marker_path.stat().st_mtime
        except FileNotFoundError:
            age = float("inf")
        except OSError:
            age = float("inf")
        if age < throttle_seconds:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
            continue
        held.append(CodexLifecycleLock(root, marker_path, lock_path, fd))
    return held


def mark_lifecycle_success(locks: list[CodexLifecycleLock]) -> None:
    for lock in locks:
        fd = os.open(lock.marker_path, os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.utime(lock.marker_path, None)
            os.chmod(lock.marker_path, 0o600)
        finally:
            os.close(fd)


def release_lifecycle_locks(locks: list[CodexLifecycleLock]) -> None:
    for lock in reversed(locks):
        try:
            fcntl.flock(lock.fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(lock.fd)
        except OSError:
            pass
