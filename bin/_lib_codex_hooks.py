"""Codex native-hook planning, state observation, and lifecycle locks.

Two layers live here. The planners stay pure — they receive Codex's
``[hooks.state]`` table as a ``state_table`` argument rather than reading
it — while the observation layer around them reads ``hooks.json``,
``config.toml`` and the lifecycle markers, and holds the bounded flocks
that serialize hook writers.
"""
from __future__ import annotations

import copy
import datetime as dt
import fcntl
import json
import os
import pathlib
import shlex
import shutil
import tomllib
from dataclasses import dataclass, field
from typing import Callable

from _lib_source_identity import source_root_key


CODEX_HOOK_EVENTS = ("Stop", "SubagentStop")
CODEX_HOOK_TIMEOUT_SECONDS = 30
CODEX_HOOK_THROTTLE_SECONDS = 15

# Codex writes its hook trust record under lowercase snake-case event tokens
# while ``hooks.json`` spells the same events in CamelCase.
CODEX_HOOK_EVENT_STATE_TOKENS = {"Stop": "stop", "SubagentStop": "subagent_stop"}
CODEX_CONFIG_FILENAME = "config.toml"

CODEX_HOOK_INSTALLED_STATES = (
    "installed_enabled",
    "installed_unverified",
    "installed_disabled",
    "installed_untrusted",
    "installed_trust_unobservable",
)

_CODEX_HOOK_REMEDIATIONS = {
    "installed_enabled": None,
    "installed_unverified": (
        "Review the cctally handler in Codex /hooks; it changed after the "
        "last recorded trust decision."
    ),
    "installed_disabled": "Re-enable the cctally handler in Codex /hooks.",
    "installed_untrusted": "Review and trust the cctally handler in Codex /hooks.",
    "installed_trust_unobservable": (
        "Verify the cctally handler in Codex /hooks; cctally cannot read the "
        "recorded state."
    ),
    "absent": "Run `cctally setup` to install the native Codex handler.",
    "malformed": "Fix the malformed hooks.json before cctally can manage it.",
    "feature_disabled": (
        "Unset CCTALLY_DISABLE_CODEX_HOOKS to enable native Codex hooks."
    ),
    # Retained for compatibility only. `codex_hook_roots` drops a configured
    # home that is not a directory, so no row is ever classified here.
    "unavailable": (
        "Ensure this configured Codex home is available, then re-run setup."
    ),
}

_CODEX_HOOK_REQUIRES_REVIEW = {
    "installed_enabled": False,
    "installed_unverified": True,
    "installed_disabled": False,
    "installed_untrusted": True,
    "installed_trust_unobservable": None,
    "absent": False,
    "malformed": False,
    "feature_disabled": False,
}


class CodexHooksError(ValueError):
    """A hooks.json shape is unsafe to modify."""


class CodexHookSlotCollision(Exception):
    """A reconcile would land a handler on a foreign positional trust key.

    Deliberately NOT a :class:`CodexHooksError`: `_setup_manage_codex_hooks`
    converts every ``CodexHooksError`` into a per-root warning row, and this
    refusal must abort the whole run instead.
    """

    def __init__(self, hooks_path, findings, stale_keys=(), *,
                 config_path=None, after_mutation=False, changed_paths=(),
                 backup_paths=None):
        self.hooks_path = pathlib.Path(hooks_path)
        self.findings = list(findings)
        self.stale_keys = sorted(stale_keys)
        # `codex_config_path` puts config.toml beside hooks.json in the same
        # Codex home, so the planners need not carry a second path argument.
        self.config_path = (
            pathlib.Path(config_path) if config_path is not None
            else self.hooks_path.parent / CODEX_CONFIG_FILENAME
        )
        # True only on the apply path, where the read-only preflight already
        # passed and the earlier steps of the run have therefore landed.
        self.after_mutation = bool(after_mutation)
        # The hooks files this run had already rewritten when the collision
        # was detected. With more than one Codex root the write loop can
        # replace an earlier root's document and only then hit a racing state
        # table in a later one, so a blanket "the Codex hooks file itself was
        # not changed" would be a false statement about that earlier root.
        self.changed_paths = [pathlib.Path(path) for path in changed_paths]
        # The dated backup `_write_hooks_document_atomic` wrote beside each
        # rewritten document, keyed by that document's path. Named in the
        # refusal because telling an operator a file was replaced without
        # telling them where the previous content went leaves them no way
        # back. A rewritten path can legitimately be missing from this map:
        # the writer takes no backup when it CREATES a document, because there
        # was no previous content to save.
        self.backup_paths = {
            str(key): pathlib.Path(value)
            for key, value in dict(backup_paths or {}).items()
        }
        super().__init__(self._render())

    def _mutation_preamble(self) -> str:
        """State what this run actually changed before it refused."""
        if not self.after_mutation:
            return "No configuration file was changed."
        if not self.changed_paths:
            # Deliberately "rewrote the contents of no Codex hooks file"
            # rather than "no Codex hooks file was changed". `changed_paths`
            # is populated from `planned != current`, so it tracks CONTENT
            # replacement and nothing else. A root whose document already
            # matched the plan still had `harden_hooks_permissions` run
            # against it, which can change the file's mode and its parent
            # directory's mode, and the broader claim would deny that.
            return (
                "Earlier steps of this run already completed; cctally "
                "rewrote the contents of no Codex hooks file."
            )
        rewritten = ", ".join(
            self._describe_rewrite(path) for path in self.changed_paths)
        return (
            "Earlier steps of this run already completed, and cctally had "
            f"already rewritten {rewritten}; {self.hooks_path} itself was "
            "not rewritten."
        )

    def _describe_rewrite(self, path: pathlib.Path) -> str:
        """One rewritten document, with the backup that holds what it replaced."""
        backup = self.backup_paths.get(str(path))
        if backup is None:
            return str(path)
        return f"{path} (previous contents saved at {backup})"

    def _closing(self) -> str:
        preamble = self._mutation_preamble()
        orphans = [
            finding["new_key"] for finding in self.findings
            if finding.get("kind") == "reused" and finding.get("new_key")
        ]
        if self.findings and len(orphans) == len(self.findings):
            # Every finding is a record for a handler that does not exist, so
            # Codex `/hooks` shows nothing to review, and D1 forbids cctally
            # from removing the entry. Naming the manual edit is the only
            # remedy this state has.
            noun = "entry" if len(orphans) == 1 else "entries"
            return (
                f"{preamble} No handler occupies "
                f"{'that key' if len(orphans) == 1 else 'those keys'}, so "
                f"Codex /hooks has nothing to review: remove the orphaned "
                f"[hooks.state] {noun} for {', '.join(orphans)} from "
                f"{self.config_path} by hand, then re-run cctally setup."
            )
        return (
            f"{preamble} Review the cctally handler in Codex /hooks, then "
            f"re-run cctally setup."
        )

    def _render(self) -> str:
        lines = [
            f"Codex hook trust slots would change at {self.hooks_path}:",
        ]
        for finding in self.findings:
            event = finding["event"]
            if finding["kind"] == "moved":
                lines.append(
                    f"  {event}: a surviving handler would move from "
                    f"{finding['old_key']} to {finding['new_key']}"
                )
            elif finding["kind"] == "reused":
                lines.append(
                    f"  {event}: {finding['new_key']} is recorded in "
                    f"[hooks.state] for no current handler"
                )
            elif finding["kind"] == "state_unreadable":
                lines.append(
                    f"  {finding['config_path']} could not be read or parsed, "
                    f"so cctally cannot prove this reconcile lands on the "
                    f"right trust slots"
                )
            else:
                lines.append(
                    f"  {event}: the handler content at {finding['new_key']} "
                    f"would change under a recorded trust decision"
                )
        if self.stale_keys:
            lines.append(
                "  stale [hooks.state] keys (left as they are, cctally never "
                "writes config.toml): " + ", ".join(self.stale_keys)
            )
        lines.append(self._closing())
        return "\n".join(lines)


def codex_hooks_feature_enabled() -> bool:
    """Mirror ``_cctally_core._truthy_env`` without importing the core module."""
    value = os.environ.get("CCTALLY_DISABLE_CODEX_HOOKS")
    disabled = value is not None and value.strip().lower() not in (
        "", "0", "false", "no",
    )
    return not disabled


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


_CODEX_MANAGED_BASENAMES = {"cctally", "cctally-npm-shim.js"}
_CODEX_CANONICAL_TAIL = ["hook-tick", "--foreground", "--source", "codex"]


def _handler_command_tokens(handler: object) -> list[str] | None:
    """Tokens of an executable handler's command, or None when unmanageable."""
    if not isinstance(handler, dict) or handler.get("type") != "command":
        return None
    tokens = _command_tokens(handler.get("command"))
    if not tokens or not pathlib.Path(tokens[0]).is_absolute():
        return None
    return tokens


def is_canonical_codex_hook_form(handler: object) -> bool:
    """The five-token form cctally installs and Codex actually routes."""
    tokens = _handler_command_tokens(handler)
    return bool(
        tokens
        and len(tokens) == 5
        and pathlib.Path(tokens[0]).name in _CODEX_MANAGED_BASENAMES
        and tokens[1:] == _CODEX_CANONICAL_TAIL
    )


def is_legacy_codex_hook_form(handler: object) -> bool:
    """The two-token npm-shim form observed on the live machine.

    It parses as ``<shim> hook-tick`` with no ``--source``, and `hook-tick`
    defaults to ``--source claude``, so this handler runs the Claude leg on a
    Codex event. It is manageable, never functioning.
    """
    tokens = _handler_command_tokens(handler)
    return bool(
        tokens
        and len(tokens) == 2
        and pathlib.Path(tokens[0]).name == "cctally-npm-shim.js"
        and tokens[1:] == ["hook-tick"]
    )


def is_managed_codex_hook_handler(handler: object) -> bool:
    """Whether setup owns this handler, independent of the installing binary.

    #720 F1: ownership relative to today's binary left a handler written by a
    different install channel unmanaged, so the planner appended its own
    alongside it. The rule is deliberately narrow — an executable handler, an
    absolute path, one of two reserved basenames, and an exact argument
    vector — because the residual risk is claiming another tool's handler.
    """
    return is_canonical_codex_hook_form(handler) or is_legacy_codex_hook_form(handler)


def codex_config_path(root: "CodexHookRoot") -> pathlib.Path:
    return root.codex_home / CODEX_CONFIG_FILENAME


def codex_hook_state_key(
    hooks_path, event: str, group_index: int, handler_index: int,
) -> str:
    """Codex's positional trust key for one handler.

    The ``<file>`` component is sometimes an absolute path and sometimes a
    plugin spec (`plugin@source:hooks/hooks.json`), so every lookup builds an
    exact key rather than splitting a recorded one on ':'.
    """
    token = CODEX_HOOK_EVENT_STATE_TOKENS[event]
    return f"{hooks_path}:{token}:{group_index}:{handler_index}"


def read_codex_hook_state(config_path) -> tuple[str, dict]:
    """Read Codex's ``[hooks.state]`` table.

    Returns ``(status, table)`` with status in ``absent`` / ``ok`` /
    ``unreadable``. `unreadable` is the evidence-unavailable verdict: cctally
    cannot prove it is not landing on a stale key, so a mutating plan refuses.
    """
    path = pathlib.Path(config_path)
    # No `Path.exists()` probe, for the reason spelled out in
    # `_read_hooks_document`: on Python 3.14 it answers False for EACCES too,
    # which would report an unreadable `config.toml` as `absent` rather than as
    # `unreadable`, and those two verdicts differ — `absent` reaches
    # `installed_untrusted` while `unreadable` refuses a mutating plan.
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return "absent", {}
    except OSError:
        return "unreadable", {}
    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError, ValueError):
        return "unreadable", {}
    hooks = document.get("hooks")
    if hooks is None:
        return "ok", {}
    if not isinstance(hooks, dict):
        return "unreadable", {}
    state = hooks.get("state")
    if state is None:
        return "ok", {}
    if not isinstance(state, dict):
        return "unreadable", {}
    return "ok", {
        key: value for key, value in state.items() if isinstance(value, dict)
    }


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


@dataclass(frozen=True)
class CodexHookObservation:
    """One root's classified hook state, per spec §2.1's ordered algorithm."""
    root: "CodexHookRoot"
    state: str
    observed_enabled: bool
    requires_review: bool | None
    remediation: str | None
    counts: dict = field(default_factory=dict)
    error: str | None = None
    state_status: str = "absent"


def codex_hook_remediation(state: str) -> str | None:
    return _CODEX_HOOK_REMEDIATIONS.get(state)


def iter_codex_hook_slots(document: object, event: str):
    """Yield ``(group_index, handler_index, handler)`` for one event."""
    if not isinstance(document, dict):
        return
    hooks = document.get("hooks")
    if not isinstance(hooks, dict):
        return
    groups = hooks.get(event)
    if not isinstance(groups, list):
        return
    for group_index, group in enumerate(groups):
        if not isinstance(group, dict):
            continue
        handlers = group.get("hooks")
        if not isinstance(handlers, list):
            continue
        for handler_index, handler in enumerate(handlers):
            yield group_index, handler_index, handler


def _observe_classified_state(root: "CodexHookRoot"):
    """The ordered classification of §2.1. The first matching rule wins."""
    counts = {event: 0 for event in CODEX_HOOK_EVENTS}

    # 1. The feature switch outranks every observation.
    if not codex_hooks_feature_enabled():
        return "feature_disabled", counts, None, "absent"

    # 2. A hooks.json cctally cannot read or validate.
    try:
        document = _read_hooks_document(root.hooks_path)
    except CodexHooksError as exc:
        return "malformed", counts, str(exc), "absent"

    managed: dict[str, list[tuple[int, int, dict]]] = {}
    for event in CODEX_HOOK_EVENTS:
        found = [
            (group_index, handler_index, handler)
            for group_index, handler_index, handler
            in iter_codex_hook_slots(document, event)
            if is_managed_codex_hook_handler(handler)
        ]
        managed[event] = found
        counts[event] = len(found)

    config_path = codex_config_path(root)
    state_status, table = read_codex_hook_state(config_path)

    # 3/4/5. A missing, duplicated, or legacy-only registration is the
    # condition `setup` exists to reconcile, never an installed state.
    for event in CODEX_HOOK_EVENTS:
        if len(managed[event]) != 1:
            return "absent", counts, None, state_status
    if any(
        not is_canonical_codex_hook_form(handler)
        for event in CODEX_HOOK_EVENTS
        for _g, _h, handler in managed[event]
    ):
        return "absent", counts, None, state_status

    keys = [
        codex_hook_state_key(root.hooks_path, event, group_index, handler_index)
        for event in CODEX_HOOK_EVENTS
        for group_index, handler_index, _handler in managed[event]
    ]
    entries = [table.get(key) for key in keys]

    # 6. The trust record cannot be observed.
    if state_status == "unreadable":
        return "installed_trust_unobservable", counts, None, state_status
    if any(
        entry is not None
        and "enabled" in entry
        and not isinstance(entry["enabled"], bool)
        for entry in entries
    ):
        return "installed_trust_unobservable", counts, None, state_status

    # 7. A proven-dead hook, regardless of what else is missing.
    if any(
        entry is not None and entry.get("enabled") is False for entry in entries
    ):
        return "installed_disabled", counts, None, state_status

    # 8. No usable trust record. Absent `enabled` reads as enabled — the legs
    # that fire on the live machine carry no `enabled` key at all — so only
    # the hash decides here.
    if state_status == "absent" or any(
        entry is None or not isinstance(entry.get("trusted_hash"), str)
        for entry in entries
    ):
        return "installed_untrusted", counts, None, state_status

    # 9. Trust was recorded, but the handler changed afterwards. §1.5: the
    # recorded hash cannot be reproduced, so freshness is the only available
    # test of whether the record still describes the current content.
    try:
        hooks_mtime = root.hooks_path.stat().st_mtime_ns
        config_mtime = config_path.stat().st_mtime_ns
    except OSError:
        return "installed_trust_unobservable", counts, None, state_status
    if hooks_mtime > config_mtime:
        return "installed_unverified", counts, None, state_status

    # 10.
    return "installed_enabled", counts, None, state_status


def observe_codex_hook_root(root: "CodexHookRoot") -> CodexHookObservation:
    """The one shared classification every consumer reads (spec §2.6)."""
    state, counts, error, state_status = _observe_classified_state(root)
    return CodexHookObservation(
        root=root,
        state=state,
        observed_enabled=(state == "installed_enabled"),
        requires_review=_CODEX_HOOK_REQUIRES_REVIEW.get(state, False),
        remediation=codex_hook_remediation(state),
        counts=counts,
        error=error,
        state_status=state_status,
    )


def codex_hook_roots_all_enabled(roots) -> bool:
    """Fail-closed frontier predicate: at least one root, every one enabled."""
    roots = list(roots)
    if not roots:
        return False
    try:
        return all(observe_codex_hook_root(root).observed_enabled for root in roots)
    except Exception:
        return False


def codex_frontier_guard_paths(roots) -> tuple[pathlib.Path, ...]:
    """Every file whose change must retire a Codex frontier certificate.

    `hooks.json` alone is not enough (§2.6a): a certificate seeded while the
    handler was enabled stays valid when Codex later flips `enabled = false`
    in `config.toml`, which is exactly the reported incident.
    """
    paths: list[pathlib.Path] = []
    for root in roots:
        paths.append(root.hooks_path)
        paths.append(codex_config_path(root))
    return tuple(paths)


def _normalize_owned_handler(handler: dict, command: str) -> None:
    handler.clear()
    handler.update(_owned_handler(command))


def _hook_groups(document: dict) -> dict:
    hooks = document.get("hooks")
    if hooks is None:
        hooks = {}
        document["hooks"] = hooks
    return hooks


def _empty_delta() -> dict[str, int]:
    return {"added": 0, "removed": 0, "unchanged": 0}


def _current_slots(document: object, event: str) -> dict[tuple[int, int], dict]:
    return {
        (group_index, handler_index): handler
        for group_index, handler_index, handler
        in iter_codex_hook_slots(document, event)
    }


def _planned_slots(kept_groups) -> dict[tuple[int, int], tuple[dict, object]]:
    """Positional map of the rebuilt event, keyed by its NEW indices."""
    slots: dict[tuple[int, int], tuple[dict, object]] = {}
    for group_index, (_origin_group, handlers) in enumerate(kept_groups):
        for handler_index, (origin, handler) in enumerate(handlers):
            slots[(group_index, handler_index)] = (handler, origin)
    return slots


def _detect_slot_collisions(event, hooks_path, table, current, planned):
    """The three surviving-collision conditions of spec §2.3.

    Every condition is evaluated against the recorded state table, which is
    what makes the evidence gate coherent: with no `config.toml` there is no
    recorded decision to strand or inherit, so no collision is possible.
    """
    findings: list[dict] = []
    current_keys = {
        codex_hook_state_key(hooks_path, event, group_index, handler_index)
        for group_index, handler_index in current
    }
    for (group_index, handler_index), (handler, origin) in sorted(planned.items()):
        new_key = codex_hook_state_key(
            hooks_path, event, group_index, handler_index)
        # C1 — a surviving handler acquires a different positional key.
        if origin is not None:
            old_key = codex_hook_state_key(hooks_path, event, *origin)
            if old_key != new_key and (old_key in table or new_key in table):
                findings.append({
                    "kind": "moved", "event": event,
                    "old_key": old_key, "new_key": new_key,
                })
                continue
        # C2 — a newly occupied key already recorded for no current handler.
        if new_key not in current_keys:
            if new_key in table:
                findings.append({
                    "kind": "reused", "event": event,
                    "old_key": None, "new_key": new_key,
                })
            continue
        # C3 — content changes in place under a live trust decision. The
        # `enabled = false` carve-out is asymmetric on purpose: inheriting a
        # disable is fail-safe (doctor FAILs on it loudly), inheriting trust
        # would run a command the operator never approved.
        if new_key in table and handler != current[(group_index, handler_index)]:
            if table[new_key].get("enabled") is not False:
                findings.append({
                    "kind": "replaced", "event": event,
                    "old_key": new_key, "new_key": new_key,
                })
    return findings


def _stale_state_keys(hooks_path, table, occupied_keys) -> list[str]:
    """Recorded keys for this hooks file that no CURRENT handler occupies.

    #720 acceptance 3: these are reported so the operator can see which
    decisions Codex is still holding for handlers that are gone. They are
    never cleaned up, because D1 keeps `config.toml` read-only.
    """
    prefixes = tuple(
        f"{hooks_path}:{token}:"
        for token in CODEX_HOOK_EVENT_STATE_TOKENS.values()
    )
    return [
        key for key in table
        if key.startswith(prefixes) and key not in occupied_keys
    ]


def _finish_plan(planned, per_event, hooks_path, table, changes):
    """Rebuild every event from its provenance-carrying slots, then refuse."""
    findings: list[dict] = []
    occupied: set[str] = set()
    for event, (current, kept_groups) in per_event.items():
        slots = _planned_slots(kept_groups)
        occupied.update(
            codex_hook_state_key(hooks_path, event, group_index, handler_index)
            for group_index, handler_index in current
        )
        if table is not None and hooks_path is not None:
            findings.extend(_detect_slot_collisions(
                event, hooks_path, table, current, slots))
    if findings:
        raise CodexHookSlotCollision(
            hooks_path, findings,
            _stale_state_keys(hooks_path, table, occupied),
        )
    return planned, changes


def _plan_install(
    document: object, binary: str, *, state_table=None, hooks_path=None,
) -> tuple[dict, dict[str, dict[str, int]]]:
    _validate_document(document)
    planned = copy.deepcopy(document)
    hooks = _hook_groups(planned)
    command = codex_hook_command(binary)
    desired = _owned_handler(command)
    changes: dict[str, dict[str, int]] = {}
    per_event: dict = {}
    for event in CODEX_HOOK_EVENTS:
        groups = hooks.setdefault(event, [])
        delta = _empty_delta()
        found = False
        kept_groups: list[tuple[int, list]] = []
        kept_group_objects: list[dict] = []
        for group_index, group in enumerate(groups):
            kept: list[tuple[object, dict]] = []
            for handler_index, handler in enumerate(group["hooks"]):
                if is_managed_codex_hook_handler(handler):
                    if found:
                        delta["removed"] += 1
                        continue
                    found = True
                    if handler == desired:
                        delta["unchanged"] += 1
                    else:
                        delta["removed"] += 1
                        delta["added"] += 1
                        _normalize_owned_handler(handler, command)
                    kept.append(((group_index, handler_index), handler))
                    continue
                kept.append(((group_index, handler_index), handler))
            if kept:
                group["hooks"] = [handler for _origin, handler in kept]
                kept_groups.append((group_index, kept))
                kept_group_objects.append(group)
        if not found:
            fresh = _owned_handler(command)
            kept_groups.append((None, [(None, fresh)]))
            kept_group_objects.append({"hooks": [fresh]})
            delta["added"] += 1
        hooks[event] = kept_group_objects
        per_event[event] = (_current_slots(document, event), kept_groups)
        changes[event] = delta
    return _finish_plan(planned, per_event, hooks_path, state_table, changes)


def _plan_uninstall(
    document: object, binary: str, *, state_table=None, hooks_path=None,
) -> tuple[dict, dict[str, dict[str, int]]]:
    _validate_document(document)
    planned = copy.deepcopy(document)
    hooks = planned.get("hooks")
    changes = {event: _empty_delta() for event in CODEX_HOOK_EVENTS}
    if not isinstance(hooks, dict):
        return planned, changes
    per_event: dict = {}
    for event in CODEX_HOOK_EVENTS:
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        kept_groups: list[tuple[int, list]] = []
        kept_group_objects: list[dict] = []
        for group_index, group in enumerate(groups):
            kept: list[tuple[object, dict]] = []
            for handler_index, handler in enumerate(group["hooks"]):
                if is_managed_codex_hook_handler(handler):
                    changes[event]["removed"] += 1
                    continue
                kept.append(((group_index, handler_index), handler))
            if kept:
                group["hooks"] = [handler for _origin, handler in kept]
                kept_groups.append((group_index, kept))
                kept_group_objects.append(group)
        if kept_group_objects:
            hooks[event] = kept_group_objects
        else:
            hooks.pop(event, None)
        per_event[event] = (_current_slots(document, event), kept_groups)
    if not hooks:
        planned.pop("hooks", None)
    return _finish_plan(planned, per_event, hooks_path, state_table, changes)


def _read_hooks_document(path: pathlib.Path) -> dict:
    # ONE read, and `FileNotFoundError` is the only swallowed failure. There is
    # deliberately no `Path.exists()` probe in front of it: on Python 3.14
    # `Path.exists()` delegates to `os.path.exists`, which swallows EVERY
    # `OSError` and returns False — EACCES and ENAMETOOLONG included — while on
    # 3.11 through 3.13 it re-raises everything outside the ENOENT class.
    # `#!/usr/bin/env python3` already resolves to 3.14 on some machines and
    # `bin/cctally` enforces only a 3.11 floor, so a probe would fail OPEN
    # exactly where it matters: a Codex home whose parent directory is
    # unreadable would read as `absent`, `observe_codex_hook_root` would
    # classify the root as uninstalled, and `doctor` would tell the operator to
    # run `cctally setup` over a document it could not read. `read_text` raises
    # `PermissionError` on every supported version, so carrying every other
    # `OSError` as a `CodexHooksError` classifies such a root `malformed` on
    # all of them.
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
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


def codex_hooks_lock_path(path: pathlib.Path) -> pathlib.Path:
    return path.with_name(path.name + ".cctally.lock")


def acquire_codex_hooks_write_locks(roots) -> list[tuple[object, int]]:
    """Hold every applicable root's writer lock, in sorted root-key order.

    An all-root plan must be decided while nobody can move a slot underneath
    it, so the locks are taken before the first read and released only after
    the last write (spec §2.3).
    """
    held: list[tuple[object, int]] = []
    try:
        for root in sorted(roots, key=lambda item: item.source_root_key):
            path = root.hooks_path
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                fd = os.open(
                    codex_hooks_lock_path(path), os.O_CREAT | os.O_RDWR, 0o600)
            except OSError as exc:
                raise CodexHooksError(f"cannot lock {path}: {exc}") from exc
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
            except OSError as exc:
                os.close(fd)
                raise CodexHooksError(f"cannot lock {path}: {exc}") from exc
            held.append((root, fd))
    except BaseException:
        release_codex_hooks_write_locks(held)
        raise
    return held


def release_codex_hooks_write_locks(held) -> None:
    for _root, fd in reversed(list(held)):
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass


def _write_hooks_document_atomic(
    path: pathlib.Path,
    document: object | None = None,
    *,
    transform: Callable[[dict], tuple[dict, dict[str, int]]] | None = None,
    harden_unchanged: bool = False,
    lock_fd: int | None = None,
) -> pathlib.Path | None | tuple[dict, dict[str, int], bool, pathlib.Path | None]:
    if (document is None) == (transform is None):
        raise ValueError("provide exactly one of document or transform")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = codex_hooks_lock_path(path)
    held_here = lock_fd is None
    if not held_here:
        fd = lock_fd
    else:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        except OSError as exc:
            raise CodexHooksError(f"cannot lock {path}: {exc}") from exc
    try:
        if held_here:
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
        if held_here:
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


def _resolve_codex_hook_account(root: CodexHookRoot) -> str:
    """Resolve one hook root's active account_key from ``<home>/auth.json`` via
    the stable-read protocol (#341, spec §1). identified -> the real key;
    stably-absent (no auth / api-key mode) OR torn -> the ``unattributed``
    sentinel (so the throttle marker keeps its byte-stable name). Read-only."""
    import _lib_accounts

    def _reader(data: bytes):
        try:
            obj = json.loads(data)
        except (ValueError, TypeError):
            raise _lib_accounts.TornRead()
        if not isinstance(obj, dict):
            raise _lib_accounts.TornRead()
        tokens = obj.get("tokens")
        id_token = (tokens.get("id_token") if isinstance(tokens, dict)
                    else obj.get("id_token"))
        payload = _lib_accounts.decode_id_token_payload(id_token)
        nat = _lib_accounts.codex_natural_id(payload)
        if nat is None:
            return None
        return _lib_accounts.account_key("codex", nat)

    result = _lib_accounts.stable_read_identity(
        str(root.codex_home / "auth.json"), _reader)
    if result.status == "identified":
        return str(result.value)
    return _lib_accounts.UNATTRIBUTED


def acquire_due_lifecycle_locks(
    app_dir: pathlib.Path,
    roots: list[CodexHookRoot],
    *,
    now: float,
    throttle_seconds: float = CODEX_HOOK_THROTTLE_SECONDS,
    account_resolver: "Callable[[CodexHookRoot], str] | None" = None,
) -> list[CodexLifecycleLock]:
    base = app_dir / "codex-hook-tick"
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(base, 0o700)
    except OSError:
        pass
    resolve_account = account_resolver or _resolve_codex_hook_account
    held: list[CodexLifecycleLock] = []
    for root in sorted(roots, key=lambda item: item.source_root_key):
        lock_path = base / f"{root.source_root_key}.lock"
        # Throttle marker keys by (source_root_key, account_key) so a mid-interval
        # account switch bypasses the PRIOR account's throttle (spec §1). The
        # ``unattributed`` sentinel keeps the byte-stable legacy marker name (no
        # account suffix) — single-account / no-auth installs are unchanged.
        import _lib_accounts
        account = resolve_account(root)
        suffix = "" if account == _lib_accounts.UNATTRIBUTED else f".{account}"
        marker_path = base / f"{root.source_root_key}{suffix}.last-success"
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
