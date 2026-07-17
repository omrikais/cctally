"""`config.json` reader, writer, lock, validators, and `cctally config` entry point.

Eager I/O sibling: bin/cctally loads this at startup and re-exports
every public symbol so bare-name callers (the dashboard `/api/settings`
handler, `cmd_record_usage` reading `load_config()`, `cmd_refresh_usage`
gating on `_get_oauth_usage_config(load_config())`, the update-check
predicate, `sync-week`, …) all resolve unchanged. Tests that mock
`load_config` via ``monkeypatch.setitem(ns, "load_config", …)`` still
work because Python's bare-name lookup inside non-extracted bin/cctally
callers resolves in bin/cctally's namespace (where the re-export lives).

What lives in bin/_cctally_core (promoted 2026-05-22, #84):
  - ``CONFIG_PATH`` / ``CONFIG_LOCK_PATH`` path constants. Reads use
    call-time ``_cctally_core.CONFIG_PATH`` / ``_cctally_core.CONFIG_LOCK_PATH``;
    tests patch via ``monkeypatch.setattr(_cctally_core, "X", v)`` (or
    the conftest ``redirect_paths()`` helper). The legacy
    ``setitem(ns, "CONFIG_PATH", …)`` pattern is forbidden by
    ``test_no_old_style_test_patches_for_promoted_globals``.

What stays in bin/cctally:
  - ``_ALERTS_BAD_CONFIG_WARNED`` + ``_warn_alerts_bad_config_once`` —
    alerts-coupled warn-once flag/helper; the alerts-config readers
    (``_get_alerts_config`` / ``_AlertsConfigError``) still live in
    bin/cctally and these two travel with that block.
  - ``eprint`` / ``ensure_dirs`` / ``DEFAULT_WEEK_START`` ubiquitous
    helpers/constants.
  - Non-path validator/normalizer primitives
    (``_normalize_alerts_enabled_value``, ``_validate_dashboard_bind_value``,
    ``_normalize_update_check_enabled_value``,
    ``_validate_update_check_ttl_hours_value``,
    ``UPDATE_DEFAULT_TTL_HOURS``, ``get_display_tz_pref``) — these stay
    near the subsystem they belong to; we reach them via the
    ``_cctally()`` accessor (call-time lookup so test monkeypatches on
    bin/cctally's namespace still propagate, per spec §5.2).
    (``normalize_display_tz_value`` imports directly from
    ``_lib_display_tz`` — see the import block below.)

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
from pathlib import Path
from typing import Any


def _cctally():
    """Resolve the current `cctally` module at call-time (spec §5.5)."""
    return sys.modules["cctally"]


# === Honest imports from extracted homes ===================================
# Spec 2026-05-17 §3.3: kernel symbols import from _cctally_core; the
# Bucket-X helper ``normalize_display_tz_value`` imports from
# ``_lib_display_tz``. Path constants (``CONFIG_PATH``,
# ``CONFIG_LOCK_PATH``) moved to _cctally_core 2026-05-22 (#84) and are
# read via call-time ``_cctally_core.CONFIG_PATH`` etc. The out-of-scope
# non-path validators (``_normalize_alerts_enabled_value``,
# ``_validate_dashboard_bind_value``,
# ``_validate_update_check_ttl_hours_value``,
# ``_normalize_update_check_enabled_value``, ``get_display_tz_pref``,
# ``UPDATE_DEFAULT_TTL_HOURS``) stay on the _cctally() accessor.
import _cctally_core
from _cctally_core import (
    eprint,
    ensure_dirs,
    DEFAULT_WEEK_START,
    _get_alerts_config,
    _AlertsConfigError,
    _get_budget_config,
    _BudgetConfigError,
    _validate_codex_budget_block,
    CODEX_BUDGET_LEAVES,
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
        f"warning: ignoring corrupt {_cctally_core.CONFIG_PATH} ({reason}); "
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
    if not _cctally_core.CONFIG_PATH.exists():
        return None
    try:
        raw = _cctally_core.CONFIG_PATH.read_text(encoding="utf-8")
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
    _cctally_core.CONFIG_LOCK_PATH.touch()
    fh = open(_cctally_core.CONFIG_LOCK_PATH, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
    finally:
        fh.close()


def _load_config_from_explicit_path(path: "str | Path") -> dict[str, Any]:
    """Read config from an explicit per-invocation override path (issue #88).

    Contract differs from the default ``load_config()``:
      - Missing file → ``SystemExit(2)`` with a clear stderr message.
      - Unreadable / malformed JSON / non-object root → ``SystemExit(2)``
        with a clear stderr message.
      - Never writes, never acquires ``config_writer_lock``, never
        creates the on-disk default config — the override is read-only
        for this invocation.

    Used by the ccusage drop-in ``--config <path>`` flag wired onto the
    10 Claude reporting commands (spec §3 T1.6 / issue #86 Session A).
    """
    p = Path(path)
    if not p.exists():
        eprint(f"cctally: --config: file not found: {p}")
        raise SystemExit(2)
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError as exc:
        eprint(f"cctally: --config: read failed for {p}: {exc}")
        raise SystemExit(2) from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        eprint(f"cctally: --config: invalid JSON in {p}: {exc}")
        raise SystemExit(2) from exc
    if not isinstance(data, dict):
        eprint(
            f"cctally: --config: {p} top-level must be a JSON object"
        )
        raise SystemExit(2)
    return data


def load_config(path: "str | Path | None" = None) -> dict[str, Any]:
    """Read config.json, falling back to in-memory defaults on corruption.

    When ``path`` is None (default): reads the persisted user config at
    ``_cctally_core.CONFIG_PATH``, creating it on first run with a fresh
    collector token under the writer lock. Concurrent-safety: readers see
    either the pre-rename or post-rename contents thanks to save_config's
    atomic os.replace. On corrupt or non-object JSON, emits a one-shot
    stderr warning and returns in-memory defaults WITHOUT re-saving — the
    next legitimate save_config call (under config_writer_lock) will
    overwrite the bad bytes atomically.

    When ``path`` is set (issue #88 ccusage drop-in ``--config <path>``):
    reads from the explicit override path and bypasses the default-path
    branch entirely. Missing / unreadable / malformed paths surface as
    ``SystemExit(2)`` with a clear stderr message — see
    ``_load_config_from_explicit_path``. No writes, no first-run create,
    no mutation of the on-disk default config.

    DEADLOCK NOTE: `fcntl.flock` is per-fd even within the same
    process. Callers that already hold config_writer_lock MUST use
    `_load_config_unlocked()` instead — re-entering this function
    inside an outer lock would block forever (verified during issue
    #17 fix).
    """
    if path is not None:
        return _load_config_from_explicit_path(path)
    c = _cctally()
    ensure_dirs()
    parsed = _try_read_config()
    if parsed is not None:
        return parsed

    if _cctally_core.CONFIG_PATH.exists():
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
    tmp = _cctally_core.CONFIG_PATH.with_name(f"{_cctally_core.CONFIG_PATH.name}.tmp.{os.getpid()}")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(str(tmp), str(_cctally_core.CONFIG_PATH))


ALLOWED_CONFIG_KEYS = (
    "display.tz",
    "alerts.enabled",
    "alerts.projected_enabled",
    "alerts.notifier",
    "alerts.command_template",
    "alerts.quota",
    "dashboard.bind",
    "dashboard.expose_transcripts",
    "dashboard.cache_failure_markers",
    "dashboard.live_tail",
    "update.check.enabled",
    "update.check.ttl_hours",
    "statusline.visual_burn_rate",
    "statusline.cost_source",
    "statusline.cctally_extensions",
    "statusline.usage_only",
    "budget.weekly_usd",
    "budget.alerts_enabled",
    "budget.alert_thresholds",
    "budget.projected_enabled",
    "budget.period",
    "budget.projects",
    "budget.project_alerts_enabled",
    "budget.codex",
    "budget.codex.amount_usd",
    "budget.codex.period",
    "budget.codex.alerts_enabled",
    "budget.codex.alert_thresholds",
    "budget.codex.projected_enabled",
    "telemetry.enabled",
)


_CODEX_BUDGET_LEAF_PREFIX = "budget.codex."


def _config_codex_leaf_value(config: dict, key: str) -> object:
    """Read one nested Codex budget leaf with its public default.

    ``config get`` is deliberately forgiving of a hand-edited corrupt block,
    just like the existing whole-object ``budget.codex`` read: it reports the
    defaults without repairing the file. Mutations use the strict setter below.
    """
    if not key.startswith(_CODEX_BUDGET_LEAF_PREFIX):
        raise ValueError(f"not a Codex budget leaf: {key!r}")
    leaf = key.removeprefix(_CODEX_BUDGET_LEAF_PREFIX)
    if leaf not in CODEX_BUDGET_LEAVES:
        raise ValueError(f"unknown Codex budget leaf: {key!r}")
    try:
        block = _get_budget_config(config)["codex"]
    except _BudgetConfigError:
        block = None
    if block is None:
        defaults = {
            "amount_usd": None,
            "period": "calendar-month",
            "alerts_enabled": False,
            "alert_thresholds": [90, 100],
            "projected_enabled": False,
        }
        value = defaults[leaf]
    else:
        value = block[leaf]
    return list(value) if isinstance(value, list) else value


def _parse_codex_budget_leaf_value(leaf: str, raw_value: str) -> object:
    """Parse exactly the command-line wire format for one Codex leaf."""
    if leaf == "amount_usd":
        try:
            return float(raw_value)
        except ValueError as exc:
            raise _BudgetConfigError(
                "budget.codex.amount_usd must be a finite number > 0"
            ) from exc
    if leaf == "period":
        return raw_value.strip()
    if leaf in {"alerts_enabled", "projected_enabled"}:
        normalized = raw_value.strip().lower()
        if normalized in {"true", "yes", "on", "1"}:
            return True
        if normalized in {"false", "no", "off", "0"}:
            return False
        raise _BudgetConfigError(f"budget.codex.{leaf} must be a boolean")
    if leaf == "alert_thresholds":
        if not raw_value.strip():
            return []
        parsed: list[int] = []
        for part in raw_value.split(","):
            try:
                parsed.append(int(part.strip(), 10))
            except ValueError as exc:
                raise _BudgetConfigError(
                    "budget.codex.alert_thresholds must be a comma-separated "
                    "list of integers"
                ) from exc
        return parsed
    raise _BudgetConfigError(f"unknown Codex budget leaf {leaf!r}")


def _set_codex_budget_leaf(config: dict, key: str, raw_value: str) -> dict:
    """Return the canonical prospective nested Codex budget block.

    The caller owns the writer lock and persistence. This pure merge helper
    validates existing state before mutating it, so malformed user state never
    becomes a partially repaired write.
    """
    if not key.startswith(_CODEX_BUDGET_LEAF_PREFIX):
        raise _BudgetConfigError(f"unknown Codex budget leaf {key!r}")
    leaf = key.removeprefix(_CODEX_BUDGET_LEAF_PREFIX)
    if leaf not in CODEX_BUDGET_LEAVES:
        raise _BudgetConfigError(f"unknown Codex budget leaf {key!r}")
    budget = config.get("budget")
    if budget is not None and not isinstance(budget, dict):
        raise _BudgetConfigError("budget must be an object")
    existing = (budget or {}).get("codex")
    if existing is None:
        if leaf != "amount_usd":
            raise _BudgetConfigError(
                "budget.codex.amount_usd must be configured before setting "
                f"budget.codex.{leaf}"
            )
        prospective: dict = {}
    else:
        if not isinstance(existing, dict):
            raise _BudgetConfigError(
                f"budget.codex must be an object or null, got {type(existing).__name__}"
            )
        # Strictly validate first: malformed existing data must abort without
        # mutation, while valid unknown siblings retain the validator's
        # existing warn-and-drop behavior when the prospective block is built.
        validated_existing = _validate_codex_budget_block(existing)
        assert validated_existing is not None
        prospective = dict(validated_existing)
    prospective[leaf] = _parse_codex_budget_leaf_value(leaf, raw_value)
    validated = _validate_codex_budget_block(prospective)
    assert validated is not None
    return validated


# ``alerts.quota`` is an additive nested object owned by the provider-neutral
# quota projection. Keep the existing core alert reader byte-compatible for
# Claude axes while making this now-known child exempt from its unknown-key
# warning path.
_cctally_core._ALERTS_CONFIG_VALID_KEYS.add("quota")

_QUOTA_ALERT_KEYS = {
    "enabled", "actual_thresholds", "projected_thresholds", "rules",
}
_QUOTA_RULE_KEYS = {
    "source", "source_root_key", "logical_limit_key",
    "actual_thresholds", "projected_thresholds",
}


def _quota_alert_error(message: str) -> None:
    raise _cctally_core._AlertsConfigError(message)


def _validate_quota_thresholds(name: str, value: object) -> list[int]:
    """Validate a quota list; unlike legacy axis lists, [] deliberately silences."""
    if not isinstance(value, list):
        _quota_alert_error(f"alerts.quota.{name} must be a list of integers")
    result: list[int] = []
    prior = 0
    for item in value:
        if not isinstance(item, int) or isinstance(item, bool):
            _quota_alert_error(
                f"alerts.quota.{name} items must be integers, got "
                f"{type(item).__name__}: {item!r}"
            )
        if not 1 <= item <= 100:
            _quota_alert_error(
                f"alerts.quota.{name} items must be in [1, 100], got {item}"
            )
        if item <= prior:
            _quota_alert_error(f"alerts.quota.{name} must be strictly increasing")
        result.append(item)
        prior = item
    return result


def _get_quota_alerts_config(cfg: "dict | None") -> dict:
    """Return the strict, defaults-filled ``alerts.quota`` configuration."""
    alerts = (cfg or {}).get("alerts", {})
    if alerts is None:
        alerts = {}
    if not isinstance(alerts, dict):
        _quota_alert_error("alerts must be an object")
    quota = alerts.get("quota", {})
    if quota is None or not isinstance(quota, dict):
        _quota_alert_error("alerts.quota must be an object")
    for key in quota:
        if key not in _QUOTA_ALERT_KEYS:
            print(
                f"warning: ignoring unknown alerts.quota config key: {key}",
                file=sys.stderr,
            )
    enabled = quota.get("enabled", False)
    if not isinstance(enabled, bool):
        _quota_alert_error(
            "alerts.quota.enabled must be a JSON boolean, got "
            f"{type(enabled).__name__}: {enabled!r}"
        )
    actual = _validate_quota_thresholds(
        "actual_thresholds", quota.get("actual_thresholds", [90, 95])
    )
    projected = _validate_quota_thresholds(
        "projected_thresholds", quota.get("projected_thresholds", [])
    )
    raw_rules = quota.get("rules", [])
    if not isinstance(raw_rules, list):
        _quota_alert_error("alerts.quota.rules must be a list")
    rules: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for index, raw_rule in enumerate(raw_rules):
        prefix = f"alerts.quota.rules[{index}]"
        if not isinstance(raw_rule, dict):
            _quota_alert_error(f"{prefix} must be an object")
        if set(raw_rule) != _QUOTA_RULE_KEYS:
            _quota_alert_error(
                f"{prefix} must contain exactly {sorted(_QUOTA_RULE_KEYS)}"
            )
        normalized: dict[str, object] = {}
        for key in ("source", "source_root_key", "logical_limit_key"):
            value = raw_rule[key]
            if not isinstance(value, str) or not value:
                _quota_alert_error(f"{prefix}.{key} must be a non-empty string")
            normalized[key] = value
        normalized["actual_thresholds"] = _validate_quota_thresholds(
            f"rules[{index}].actual_thresholds", raw_rule["actual_thresholds"]
        )
        normalized["projected_thresholds"] = _validate_quota_thresholds(
            f"rules[{index}].projected_thresholds", raw_rule["projected_thresholds"]
        )
        identity = (
            str(normalized["source"]), str(normalized["source_root_key"]),
            str(normalized["logical_limit_key"]),
        )
        if identity in seen:
            _quota_alert_error(
                "alerts.quota.rules must be unique by source, source_root_key, "
                "and logical_limit_key"
            )
        seen.add(identity)
        rules.append(normalized)
    return {
        "enabled": enabled,
        "actual_thresholds": actual,
        "projected_thresholds": projected,
        "rules": rules,
    }


# === statusline config validators (issue #86 Session G) ===================

_STATUSLINE_VBR_VALUES = ("off", "emoji", "text", "emoji-text")
_STATUSLINE_COST_SOURCE_VALUES = ("auto", "cctally", "cc", "both")


def _validate_statusline_visual_burn_rate(value):
    """Validate ``statusline.visual_burn_rate``.

    Accepts any of ``off`` / ``emoji`` / ``text`` / ``emoji-text``. Other
    strings raise ``ValueError`` with a hint listing the valid values.
    """
    if isinstance(value, str) and value in _STATUSLINE_VBR_VALUES:
        return value
    raise ValueError(
        f"statusline.visual_burn_rate must be one of "
        f"{', '.join(_STATUSLINE_VBR_VALUES)} (got {value!r})"
    )


def _validate_statusline_cost_source(value):
    """Validate ``statusline.cost_source``.

    Accepts ``auto`` / ``cctally`` / ``cc`` / ``both``. The ``ccusage``
    value name is rejected at config set time too — the rename hint
    is surfaced both here AND at flag-parse time by the argparse choice
    rejection inside ``cmd_statusline``.
    """
    if isinstance(value, str) and value in _STATUSLINE_COST_SOURCE_VALUES:
        return value
    if value == "ccusage":
        raise ValueError(
            "statusline.cost_source 'ccusage' was renamed; use 'cctally'"
        )
    raise ValueError(
        f"statusline.cost_source must be one of "
        f"{', '.join(_STATUSLINE_COST_SOURCE_VALUES)} (got {value!r})"
    )


def _validate_statusline_cctally_extensions(value):
    """Validate ``statusline.cctally_extensions``.

    Accepts booleans (preferred) or canonical truthy/falsy strings
    (``true``/``false``/``yes``/``no``/``on``/``off``/``1``/``0``).
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lo = value.strip().lower()
        if lo in ("true", "yes", "on", "1"):
            return True
        if lo in ("false", "no", "off", "0"):
            return False
    raise ValueError(
        f"statusline.cctally_extensions must be boolean (got {value!r})"
    )


def _validate_statusline_usage_only(value):
    """Validate ``statusline.usage_only``.

    Accepts booleans (preferred) or canonical truthy/falsy strings
    (``true``/``false``/``yes``/``no``/``on``/``off``/``1``/``0``).
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lo = value.strip().lower()
        if lo in ("true", "yes", "on", "1"):
            return True
        if lo in ("false", "no", "off", "0"):
            return False
    raise ValueError(
        f"statusline.usage_only must be boolean (got {value!r})"
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
    if key == "alerts.projected_enabled":
        # Validated boolean (defaults to False when unset). A corrupt alerts
        # block surfaces the default — mirrors alerts.enabled.
        try:
            return bool(_get_alerts_config(config)["projected_enabled"])
        except c._AlertsConfigError:
            return False
    if key == "alerts.notifier":
        # Validated dispatch backend (defaults to 'auto' when unset). A corrupt
        # alerts block surfaces the default — mirrors alerts.enabled.
        try:
            return _get_alerts_config(config)["notifier"]
        except c._AlertsConfigError:
            return "auto"
    if key == "alerts.command_template":
        # Validated argv list or None (defaults to None when unset). A corrupt
        # alerts block surfaces the default. The plain-text render path JSON-
        # encodes this so `config get` round-trips through `config set`.
        try:
            return _get_alerts_config(config)["command_template"]
        except c._AlertsConfigError:
            return None
    if key == "alerts.quota":
        try:
            return _get_quota_alerts_config(config)
        except c._AlertsConfigError:
            return _get_quota_alerts_config({})
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
    if key == "dashboard.expose_transcripts":
        # Boolean opt-in (Plan 2, spec §5). Default False — transcript
        # endpoints are served only over loopback unless this is true (LAN
        # exposure). A hand-edited junk value surfaces the default, mirroring
        # dashboard.bind.
        block = config.get("dashboard") if isinstance(config, dict) else None
        if not isinstance(block, dict):
            block = {}
        stored = block.get("expose_transcripts")
        if stored is None:
            return False
        # Config stores a JSON bool; the shared string-normalizer
        # (_normalize_alerts_enabled_value) only tolerates str spellings,
        # so short-circuit a real bool here rather than re-forking it.
        if isinstance(stored, bool):
            return stored
        # Only str spellings are normalizable. Any other JSON scalar/container
        # (int/float/list/dict) must surface the default — NOT crash: the shared
        # normalizer does ``(raw or "").strip()``, which raises AttributeError
        # (uncaught by ``except ValueError``) on e.g. a hand-edited bare ``1``.
        if isinstance(stored, str):
            try:
                return c._normalize_alerts_enabled_value(stored)
            except ValueError:
                return False
        return False
    if key == "dashboard.cache_failure_markers":
        # Boolean opt-OUT (spec §5). Default TRUE — absence is treated as ON
        # (unlike dashboard.expose_transcripts, an opt-IN default of False).
        # A hand-edited junk value surfaces the True default rather than
        # crashing (mirrors dashboard.bind / expose_transcripts).
        block = config.get("dashboard") if isinstance(config, dict) else None
        if not isinstance(block, dict):
            block = {}
        stored = block.get("cache_failure_markers")
        if stored is None:
            return True
        if isinstance(stored, bool):
            return stored
        # Only str spellings are normalizable; any other JSON scalar surfaces
        # the default (the shared normalizer's .strip() would AttributeError on
        # a bare int — uncaught by `except ValueError`).
        if isinstance(stored, str):
            try:
                return c._normalize_alerts_enabled_value(stored)
            except ValueError:
                return True
        return True
    if key == "dashboard.live_tail":
        # Boolean opt-OUT (spec §4.2). Default TRUE — absence is ON. A
        # hand-edited junk value surfaces the True default. Mirrors
        # dashboard.cache_failure_markers exactly.
        block = config.get("dashboard") if isinstance(config, dict) else None
        if not isinstance(block, dict):
            block = {}
        stored = block.get("live_tail")
        if stored is None:
            return True
        if isinstance(stored, bool):
            return stored
        if isinstance(stored, str):
            try:
                return c._normalize_alerts_enabled_value(stored)
            except ValueError:
                return True
        return True
    if key == "telemetry.enabled":
        # Boolean opt-OUT (anonymous install-count telemetry, spec 2026-07-07).
        # Default TRUE — absence is ON. A hand-edited junk value surfaces the
        # True default. Mirrors dashboard.live_tail exactly, under a top-level
        # `telemetry` block instead of `dashboard`.
        block = config.get("telemetry") if isinstance(config, dict) else None
        if not isinstance(block, dict):
            block = {}
        stored = block.get("enabled")
        if stored is None:
            return True
        if isinstance(stored, bool):
            return stored
        if isinstance(stored, str):
            try:
                return c._normalize_alerts_enabled_value(stored)
            except ValueError:
                return True
        return True
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
    if key.startswith(_CODEX_BUDGET_LEAF_PREFIX):
        return _config_codex_leaf_value(config, key)
    if key in (
        "statusline.visual_burn_rate",
        "statusline.cost_source",
        "statusline.cctally_extensions",
        "statusline.usage_only",
    ):
        sl_block = config.get("statusline") if isinstance(config, dict) else None
        if not isinstance(sl_block, dict):
            sl_block = {}
        inner = key.split(".", 1)[1]
        stored = sl_block.get(inner)
        defaults = {
            "visual_burn_rate": "off",
            "cost_source": "auto",
            "cctally_extensions": True,
            "usage_only": False,
        }
        if stored is None:
            return defaults[inner]
        validator = {
            "visual_burn_rate": _validate_statusline_visual_burn_rate,
            "cost_source": _validate_statusline_cost_source,
            "cctally_extensions": _validate_statusline_cctally_extensions,
            "usage_only": _validate_statusline_usage_only,
        }[inner]
        try:
            return validator(stored)
        except ValueError:
            # Hand-edited junk: surface the default — mirrors dashboard.bind.
            return defaults[inner]
    if key in (
        "budget.weekly_usd",
        "budget.alerts_enabled",
        "budget.alert_thresholds",
        "budget.projected_enabled",
        "budget.period",
        "budget.projects",
        "budget.project_alerts_enabled",
        "budget.codex",
    ):
        inner = key.split(".", 1)[1]
        # Read the validated, defaults-filled block. A corrupt block falls
        # back to the canonical default leaf (mirrors alerts.enabled /
        # dashboard.bind, which surface the default on a hand-edited junk
        # block rather than erroring out of a plain `config get`).
        try:
            return _get_budget_config(config)[inner]
        except _BudgetConfigError:
            from _cctally_core import _BUDGET_DEFAULTS

            default = _BUDGET_DEFAULTS[inner]
            if isinstance(default, list):
                return list(default)
            if isinstance(default, dict):
                return dict(default)
            return default
    return None


def _cmd_config_get(args: argparse.Namespace, config: dict) -> int:
    key = args.key
    if key is not None and key not in ALLOWED_CONFIG_KEYS:
        eprint(f"cctally config: unknown config key {key!r}")
        return 2
    # `alerts.command_template` is JSON-shaped (a list of strings or null), and
    # `budget.projects` is JSON-shaped (an object), so their real values
    # (including None) must survive into the render layer — the generic
    # None->"" coercion below would break the JSON shape / round-trip.
    def _coerce(k: str, v: "object") -> "object":
        if k in (
            "alerts.command_template", "alerts.quota", "budget.projects",
            "budget.codex",
        ) or k.startswith(_CODEX_BUDGET_LEAF_PREFIX):
            return v
        return v if v is not None else ""

    pairs: "list[tuple[str, object]]" = []
    if key is None:
        for k in ALLOWED_CONFIG_KEYS:
            pairs.append((k, _coerce(k, _config_known_value(config, k))))
    else:
        pairs.append((key, _coerce(key, _config_known_value(config, key))))

    if getattr(args, "emit_json", False):
        # Walk every dot-delimited segment so keys deeper than two
        # segments (e.g. `update.check.enabled`) nest correctly. The
        # earlier `partition` form collapsed three-segment keys into
        # a flat tail (`{"update": {"check.enabled": ...}}`) and
        # diverged from `config set --json` / on-disk shape.
        out: "dict[str, object]" = {}
        nested_parents = {
            candidate
            for candidate, _value in pairs
            if any(child.startswith(candidate + ".") for child, _value in pairs)
        }
        for k, v in pairs:
            # A bulk read includes both `budget.codex` (null when not
            # configured) and its additive `budget.codex.*` leaves.  A scalar
            # parent cannot coexist with those children in JSON, so let the
            # specific leaves build their object.  Direct `config get
            # budget.codex --json` has no descendant pair and remains null.
            if k in nested_parents and not isinstance(v, dict):
                continue
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
            if k in (
                "alerts.command_template", "alerts.quota", "budget.projects",
                "budget.codex",
            ):
                # JSON-encoded so `config get` output round-trips through the
                # matching `config set` branch (both JSON-parse their value).
                # `alerts.command_template` is a list-of-strings|null;
                # `budget.projects` is an object {git-root: usd};
                # `budget.codex` is an object|null (the no-budget sentinel).
                rendered = json.dumps(v)
            elif k.startswith(_CODEX_BUDGET_LEAF_PREFIX) and v is None:
                rendered = "null"
            elif isinstance(v, bool):
                rendered = "true" if v else "false"
            elif isinstance(v, list):
                # Comma-joined so `config get budget.alert_thresholds` output
                # round-trips through `config set budget.alert_thresholds`.
                rendered = ",".join(str(x) for x in v)
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
    if key.startswith(_CODEX_BUDGET_LEAF_PREFIX):
        try:
            with config_writer_lock():
                config = _load_config_unlocked()
                configured = _set_codex_budget_leaf(config, key, raw)
                budget = config.get("budget")
                if budget is not None and not isinstance(budget, dict):
                    raise _BudgetConfigError("budget must be an object")
                block = dict(budget or {})
                block["codex"] = configured
                config["budget"] = block
                save_config(config)
        except _BudgetConfigError as exc:
            eprint(f"cctally config: {exc}")
            return 2
        # Deliberately outside the config lock: this helper opens and locks DBs.
        c._reconcile_codex_budget_on_config_write({"codex": configured})
        leaf = key.removeprefix(_CODEX_BUDGET_LEAF_PREFIX)
        value = configured[leaf]
        if getattr(args, "emit_json", False):
            print(json.dumps({"budget": {"codex": {leaf: value}}}, indent=2))
        else:
            if isinstance(value, bool):
                rendered = "true" if value else "false"
            elif isinstance(value, list):
                rendered = ",".join(str(item) for item in value)
            else:
                rendered = str(value)
            print(f"{key}={rendered}")
        return 0
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
    if key == "alerts.projected_enabled":
        # Projected-pace opt-in (#121). Same bool-normalizer + read-modify-write
        # posture as alerts.enabled (preserves sibling alerts.* keys).
        # _normalize_alerts_enabled_value hardcodes "alerts.enabled" in its
        # ValueError text, so catch + re-message with the actual key name
        # (mirrors _normalize_update_check_enabled_value's precedent) — the
        # budget side already names its own key correctly.
        try:
            normalized = c._normalize_alerts_enabled_value(raw)
        except ValueError:
            print(
                f"cctally: invalid boolean value for alerts.projected_enabled: "
                f"{raw!r} (expected true|false|yes|no|1|0|on|off)",
                file=sys.stderr,
            )
            return 2
        with config_writer_lock():
            config = _load_config_unlocked()
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
            alerts_block["projected_enabled"] = normalized
            try:
                _get_alerts_config({**config, "alerts": alerts_block})
            except _AlertsConfigError as exc:
                print(f"cctally: alerts config error: {exc}", file=sys.stderr)
                return 2
            config["alerts"] = alerts_block
            save_config(config)
        if getattr(args, "emit_json", False):
            print(
                json.dumps({"alerts": {"projected_enabled": normalized}}, indent=2)
            )
        else:
            print(
                f"alerts.projected_enabled={'true' if normalized else 'false'}"
            )
        return 0
    if key == "alerts.notifier":
        # Dispatch backend (Phase B). Plain string; the enum constraint is
        # enforced by the pre-persist _get_alerts_config validation (so we never
        # write a config that fails subsequent reads). Same read-modify-write
        # posture as alerts.enabled (preserves sibling alerts.* keys).
        normalized = raw.strip()
        with config_writer_lock():
            config = _load_config_unlocked()
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
            alerts_block["notifier"] = normalized
            try:
                _get_alerts_config({**config, "alerts": alerts_block})
            except _AlertsConfigError as exc:
                print(f"cctally: alerts config error: {exc}", file=sys.stderr)
                return 2
            config["alerts"] = alerts_block
            save_config(config)
        if getattr(args, "emit_json", False):
            print(json.dumps({"alerts": {"notifier": normalized}}, indent=2))
        else:
            print(f"alerts.notifier={normalized}")
        return 0
    if key == "alerts.command_template":
        # Dispatch argv template (Phase B). JSON-parsed value (a list of strings
        # or null to clear it); the shape + cross-field constraints are enforced
        # by the pre-persist _get_alerts_config validation. Same read-modify-
        # write posture as alerts.enabled (preserves sibling alerts.* keys).
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError) as exc:
            print(
                f"cctally: alerts.command_template must be JSON (a list of "
                f"strings or null): {exc}",
                file=sys.stderr,
            )
            return 2
        with config_writer_lock():
            config = _load_config_unlocked()
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
            alerts_block["command_template"] = parsed
            try:
                _get_alerts_config({**config, "alerts": alerts_block})
            except _AlertsConfigError as exc:
                print(f"cctally: alerts config error: {exc}", file=sys.stderr)
                return 2
            config["alerts"] = alerts_block
            save_config(config)
        if getattr(args, "emit_json", False):
            print(json.dumps({"alerts": {"command_template": parsed}}, indent=2))
        else:
            print(f"alerts.command_template={json.dumps(parsed)}")
        return 0
    if key == "alerts.quota":
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError) as exc:
            print(
                f"cctally: alerts.quota must be a JSON object: {exc}",
                file=sys.stderr,
            )
            return 2
        if not isinstance(parsed, dict):
            print("cctally: alerts.quota must be a JSON object", file=sys.stderr)
            return 2
        with config_writer_lock():
            config = _load_config_unlocked()
            existing_alerts = config.get("alerts")
            if existing_alerts is not None and not isinstance(existing_alerts, dict):
                print(
                    "cctally: alerts config error: alerts must be an object",
                    file=sys.stderr,
                )
                return 2
            alerts_block = dict(existing_alerts or {})
            alerts_block["quota"] = parsed
            candidate = {**config, "alerts": alerts_block}
            try:
                _get_alerts_config(candidate)
                normalized = _get_quota_alerts_config(candidate)
            except _AlertsConfigError as exc:
                print(f"cctally: alerts config error: {exc}", file=sys.stderr)
                return 2
            alerts_block["quota"] = normalized
            config["alerts"] = alerts_block
            save_config(config)
        if getattr(args, "emit_json", False):
            print(json.dumps({"alerts": {"quota": normalized}}, indent=2))
        else:
            print(f"alerts.quota={json.dumps(normalized)}")
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
    if key == "dashboard.expose_transcripts":
        # Same read-modify-write posture as dashboard.bind: validate first,
        # then write under config_writer_lock with _load_config_unlocked
        # (calling load_config inside the writer-lock self-deadlocks per the
        # CLAUDE.md gotcha — fcntl.flock is per-fd, not per-process). Preserves
        # a sibling dashboard.bind in the same parent block.
        # Reuse the shared bool-normalizer (DRY with alerts.enabled); it
        # hardcodes "alerts.enabled" in its ValueError text, so catch +
        # re-message with the actual key name (mirrors alerts.projected_enabled).
        try:
            canonical = c._normalize_alerts_enabled_value(raw)
        except ValueError:
            print(
                f"cctally: invalid boolean value for dashboard.expose_transcripts: "
                f"{raw!r} (expected true|false|yes|no|1|0|on|off)",
                file=sys.stderr,
            )
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
            block["expose_transcripts"] = canonical
            config["dashboard"] = block
            save_config(config)
        if getattr(args, "emit_json", False):
            print(
                json.dumps(
                    {"dashboard": {"expose_transcripts": canonical}}, indent=2
                )
            )
        else:
            print(
                f"dashboard.expose_transcripts="
                f"{'true' if canonical else 'false'}"
            )
        return 0
    if key == "dashboard.cache_failure_markers":
        # Same read-modify-write posture as dashboard.expose_transcripts:
        # validate first, then write under config_writer_lock with
        # _load_config_unlocked (load_config inside the writer-lock
        # self-deadlocks — fcntl.flock is per-fd). Preserves sibling
        # dashboard.bind / dashboard.expose_transcripts. Reuse the shared
        # bool-normalizer; catch + re-message with the actual key name (it
        # hardcodes "alerts.enabled" in its ValueError text).
        try:
            canonical = c._normalize_alerts_enabled_value(raw)
        except ValueError:
            print(
                f"cctally: invalid boolean value for "
                f"dashboard.cache_failure_markers: "
                f"{raw!r} (expected true|false|yes|no|1|0|on|off)",
                file=sys.stderr,
            )
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
            block["cache_failure_markers"] = canonical
            config["dashboard"] = block
            save_config(config)
        if getattr(args, "emit_json", False):
            print(
                json.dumps(
                    {"dashboard": {"cache_failure_markers": canonical}}, indent=2
                )
            )
        else:
            print(
                f"dashboard.cache_failure_markers="
                f"{'true' if canonical else 'false'}"
            )
        return 0
    if key == "dashboard.live_tail":
        # Mirror dashboard.cache_failure_markers exactly: validate the bool
        # first, then read-modify-write under config_writer_lock with
        # _load_config_unlocked (load_config under the lock self-deadlocks).
        # Preserves sibling dashboard.bind / expose_transcripts /
        # cache_failure_markers. Re-message the shared normalizer's ValueError
        # with the actual key name.
        try:
            canonical = c._normalize_alerts_enabled_value(raw)
        except ValueError:
            print(
                f"cctally: invalid boolean value for dashboard.live_tail: "
                f"{raw!r} (expected true|false|yes|no|1|0|on|off)",
                file=sys.stderr,
            )
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
            block["live_tail"] = canonical
            config["dashboard"] = block
            save_config(config)
        if getattr(args, "emit_json", False):
            print(json.dumps({"dashboard": {"live_tail": canonical}}, indent=2))
        else:
            print(f"dashboard.live_tail={'true' if canonical else 'false'}")
        return 0
    if key == "telemetry.enabled":
        # Anonymous install-count telemetry opt-out (spec 2026-07-07). Mirror
        # dashboard.live_tail exactly: validate the bool first, then
        # read-modify-write under config_writer_lock with _load_config_unlocked
        # (load_config under the lock self-deadlocks). Preserves any sibling
        # telemetry.* keys. Re-message the shared normalizer's ValueError with
        # the actual key name.
        try:
            canonical = c._normalize_alerts_enabled_value(raw)
        except ValueError:
            print(
                f"cctally: invalid boolean value for telemetry.enabled: "
                f"{raw!r} (expected true|false|yes|no|1|0|on|off)",
                file=sys.stderr,
            )
            return 2
        with config_writer_lock():
            config = _load_config_unlocked()
            existing = config.get("telemetry")
            if existing is not None and not isinstance(existing, dict):
                print(
                    "cctally: telemetry config error: telemetry must be an object",
                    file=sys.stderr,
                )
                return 2
            block = dict(existing or {})
            block["enabled"] = canonical
            config["telemetry"] = block
            save_config(config)
        if getattr(args, "emit_json", False):
            print(json.dumps({"telemetry": {"enabled": canonical}}, indent=2))
        else:
            print(f"telemetry.enabled={'true' if canonical else 'false'}")
        return 0
    if key in (
        "statusline.visual_burn_rate",
        "statusline.cost_source",
        "statusline.cctally_extensions",
        "statusline.usage_only",
    ):
        inner_key = key.split(".", 1)[1]
        validator = {
            "visual_burn_rate": _validate_statusline_visual_burn_rate,
            "cost_source": _validate_statusline_cost_source,
            "cctally_extensions": _validate_statusline_cctally_extensions,
            "usage_only": _validate_statusline_usage_only,
        }[inner_key]
        try:
            normalized = validator(raw)
        except ValueError as exc:
            print(f"cctally: {exc}", file=sys.stderr)
            return 2
        with config_writer_lock():
            config = _load_config_unlocked()
            existing = config.get("statusline")
            if existing is not None and not isinstance(existing, dict):
                print(
                    "cctally: statusline config error: statusline must be an object",
                    file=sys.stderr,
                )
                return 2
            block = dict(existing or {})
            block[inner_key] = normalized
            config["statusline"] = block
            save_config(config)
        if getattr(args, "emit_json", False):
            print(json.dumps({"statusline": {inner_key: normalized}}, indent=2))
        else:
            if isinstance(normalized, bool):
                rendered = "true" if normalized else "false"
            else:
                rendered = str(normalized)
            print(f"{key}={rendered}")
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
    if key in (
        "budget.weekly_usd",
        "budget.alerts_enabled",
        "budget.alert_thresholds",
        "budget.projected_enabled",
        "budget.period",
        "budget.projects",
        "budget.project_alerts_enabled",
        "budget.codex",
    ):
        inner_key = key.split(".", 1)[1]
        # Parse + normalize the raw value per key BEFORE acquiring the lock so
        # rejection short-circuits. The whole merged block is re-validated via
        # _get_budget_config under the lock so we never persist a config that
        # fails subsequent reads. _load_config_unlocked is mandatory inside the
        # writer lock (load_config would self-deadlock on the same fcntl fd).
        if inner_key == "weekly_usd":
            if raw.strip().lower() in {"null", "none", ""}:
                new_val: object = None
            else:
                try:
                    new_val = float(raw)
                except ValueError:
                    eprint(
                        "cctally config: budget.weekly_usd must be a number or "
                        f"null, got {raw!r}"
                    )
                    return 2
        elif inner_key in (
            "alerts_enabled", "projected_enabled", "project_alerts_enabled"
        ):
            lo = raw.strip().lower()
            if lo in ("true", "yes", "on", "1"):
                new_val = True
            elif lo in ("false", "no", "off", "0"):
                new_val = False
            else:
                eprint(
                    f"cctally config: budget.{inner_key} must be a boolean, "
                    f"got {raw!r}"
                )
                return 2
        elif inner_key == "period":
            # `budget.period` is a plain string leaf. The enum check lives in
            # _get_budget_config under the lock below (so a bad value is a
            # clean exit-2 with the canonical message); here we just pass the
            # raw token through.
            new_val = raw.strip()
        elif inner_key == "codex":
            # `budget.codex` is a nested object (or null = no Codex budget),
            # which the plain leaves can't round-trip — JSON-parse it (mirrors
            # the budget.projects branch). The shape/period/amount rules are
            # enforced by _get_budget_config under the lock below; here we only
            # reject non-JSON and coerce the null sentinel.
            if raw.strip().lower() in {"null", "none"}:
                new_val = None
            else:
                try:
                    parsed_codex = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    eprint(
                        "cctally config: budget.codex must be a JSON object or "
                        f"null, got {raw!r}"
                    )
                    return 2
                if parsed_codex is not None and not isinstance(parsed_codex, dict):
                    eprint(
                        "cctally config: budget.codex must be a JSON object or "
                        "null"
                    )
                    return 2
                new_val = parsed_codex
        elif inner_key == "projects":
            # `budget.projects` is a dict {git-root: usd}, which the plain
            # number/bool/list leaves can't round-trip — JSON-parse it (mirrors
            # the alerts.command_template branch). The per-value numeric rule is
            # enforced by _get_budget_config under the lock below; here we only
            # reject non-JSON / non-object shape.
            try:
                parsed_obj = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                eprint(
                    "cctally config: budget.projects must be a JSON object, "
                    f"got {raw!r}"
                )
                return 2
            if not isinstance(parsed_obj, dict):
                eprint("cctally config: budget.projects must be a JSON object")
                return 2
            # Canonicalize each project key to its resolved git-root, mirroring
            # the `budget set --project` CLI path (`_resolve_project_budget_-
            # target`). `_sum_cost_by_project` buckets spend under the realpath'd
            # `ProjectKey.bucket_path`, so a `~`/relative/sub-dir/trailing-slash
            # key stored verbatim would NEVER match → a permanent $0 row that
            # silently never alerts. Resolving here makes the JSON-object surface
            # match the per-project CLI surface. Non-string keys (impossible from
            # json.loads, defensive) and the `__CWD__`-non-git None case fall
            # back to the raw key for `_get_budget_config` to handle.
            c = _cctally()
            new_val = {
                (
                    c._resolve_project_budget_target(pk)
                    if isinstance(pk, str)
                    else pk
                )
                or pk: pv
                for pk, pv in parsed_obj.items()
            }
        else:  # alert_thresholds — comma-separated int list (empty = silenced)
            stripped = raw.strip()
            parsed: "list[int]" = []
            if stripped:
                for part in stripped.split(","):
                    tok = part.strip()
                    try:
                        parsed.append(int(tok))
                    except ValueError:
                        eprint(
                            "cctally config: budget.alert_thresholds must be a "
                            f"comma-separated list of integers, got {raw!r}"
                        )
                        return 2
            new_val = parsed
        with config_writer_lock():
            config = _load_config_unlocked()
            existing = config.get("budget")
            if existing is not None and not isinstance(existing, dict):
                eprint("cctally config: budget must be an object")
                return 2
            block = dict(existing or {})
            block[inner_key] = new_val
            config["budget"] = block
            try:
                validated = _get_budget_config(config)
            except _BudgetConfigError as exc:
                eprint(f"cctally config: {exc}")
                return 2
            # Persist the canonicalized leaf (e.g. sorted/deduped thresholds,
            # float-coerced weekly_usd) so config.json matches what reads apply.
            block[inner_key] = validated[inner_key]
            config["budget"] = block
            save_config(config)
        # Forward-only reconcile (mirrors `budget set`): enabling/raising a
        # budget while already past a threshold must record the crossed
        # thresholds as already-alerted so the next record-usage tick does NOT
        # dispatch retroactive alerts. Runs OUTSIDE config_writer_lock — the
        # helper opens stats.db and must not nest under the config lock
        # (fcntl.flock is per-fd; the helper has its own open_db locking).
        c = _cctally()
        # Gate each forward-only reconcile (spec §6.8) on the keys it actually
        # consumes. Running unconditionally on an UNRELATED write — e.g. the
        # global axis on `config set budget.projects`, or the per-project axis
        # on `budget.weekly_usd` — would latch a currently-over-but-not-yet-
        # dispatched threshold as already-alerted, permanently suppressing the
        # next record-usage tick's dispatch. The global axis feeds on
        # weekly_usd/alerts_enabled/alert_thresholds/period; the per-project axis
        # on projects/project_alerts_enabled/alert_thresholds (alert_thresholds
        # is shared; projected_enabled belongs to neither reconcile). `period` is
        # in the global set because changing it re-keys the milestone window
        # (calendar period-start instant vs subscription-week); without the
        # reconcile, switching period while already over a threshold would
        # instant-popup on the next record-usage tick — the exact case the
        # forward-only-from-set reconcile prevents (`budget set --period` already
        # reconciles via the same helper). Both run OUTSIDE config_writer_lock
        # (each helper has its own open_db lock).
        if inner_key in (
            "weekly_usd", "alerts_enabled", "alert_thresholds", "period"
        ):
            c._reconcile_budget_on_config_write(validated)
        if inner_key in (
            "projects", "project_alerts_enabled", "alert_thresholds"
        ):
            c._reconcile_project_budget_milestones_on_write(validated)
        # Codex budget axis (spec §6): the nested budget.codex block is set
        # wholesale via `config set budget.codex '<json>'`, so the only key that
        # touches it is `codex` itself. Gated on the codex block carrying
        # alerts_enabled + thresholds (the helper re-checks); records nothing
        # otherwise.
        if inner_key == "codex":
            c._reconcile_codex_budget_on_config_write(validated)
        out_val = validated[inner_key]
        if getattr(args, "emit_json", False):
            print(json.dumps({"budget": {inner_key: out_val}}, indent=2))
        else:
            if isinstance(out_val, bool):
                rendered = "true" if out_val else "false"
            elif inner_key in ("projects", "codex"):
                # JSON so `config get budget.{projects,codex}` round-trips back
                # through this branch (str(dict)/None is not valid JSON; the
                # codex no-budget sentinel renders as `null`).
                rendered = json.dumps(out_val)
            else:
                rendered = str(out_val)
            print(f"{key}={rendered}")
        return 0
    return 2  # unreachable given the gate above


def _cmd_config_unset(args: argparse.Namespace) -> int:
    key = args.key
    if key not in ALLOWED_CONFIG_KEYS:
        eprint(f"cctally config: unknown config key {key!r}")
        return 2
    if key.startswith(_CODEX_BUDGET_LEAF_PREFIX):
        c = _cctally()
        configured: dict | None = None
        try:
            with config_writer_lock():
                config = _load_config_unlocked()
                budget = config.get("budget")
                if budget is not None and not isinstance(budget, dict):
                    raise _BudgetConfigError("budget must be an object")
                existing = (budget or {}).get("codex")
                if existing is None:
                    return 0
                if not isinstance(existing, dict):
                    raise _BudgetConfigError(
                        f"budget.codex must be an object or null, got {type(existing).__name__}"
                    )
                validated_existing = _validate_codex_budget_block(existing)
                assert validated_existing is not None
                leaf = key.removeprefix(_CODEX_BUDGET_LEAF_PREFIX)
                block = dict(budget or {})
                if leaf == "amount_usd":
                    block.pop("codex", None)
                    if block:
                        config["budget"] = block
                    else:
                        config.pop("budget", None)
                    save_config(config)
                    return 0
                prospective = dict(validated_existing)
                prospective.pop(leaf, None)
                configured = _validate_codex_budget_block(prospective)
                assert configured is not None
                block["codex"] = configured
                config["budget"] = block
                save_config(config)
        except _BudgetConfigError as exc:
            eprint(f"cctally config: {exc}")
            return 2
        # An optional leaf leaves a configured block, even when it already
        # held its default; the forward-only state must be reconciled once.
        assert configured is not None
        c._reconcile_codex_budget_on_config_write({"codex": configured})
        return 0
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
    if key in (
        "alerts.enabled",
        "alerts.projected_enabled",
        "alerts.notifier",
        "alerts.command_template",
        "alerts.quota",
    ):
        # Mirror the display.tz branch: writer-lock + _load_config_unlocked
        # (NOT load_config — fcntl.flock is per-fd so re-entry would
        # self-deadlock per the gotcha in CLAUDE.md). Unsetting just the
        # named key preserves any user-customized threshold lists
        # (`weekly_thresholds`, `five_hour_thresholds`) and the sibling
        # enabled/projected_enabled/notifier/command_template keys. For
        # enabled/projected_enabled/notifier the read-time validator
        # (`_get_alerts_config`) re-applies the canonical default
        # (`False` / `"auto"`) for the missing key on next get. NOT so for
        # command_template when notifier == "command": the cross-field
        # constraint makes notifier="command" REQUIRE a template, so dropping
        # the template would leave a config that _get_alerts_config REJECTS on
        # the next read. The pre-persist guard below catches exactly that case.
        inner_key = key.split(".", 1)[1]
        with config_writer_lock():
            config = _load_config_unlocked()
            block = config.get("alerts")
            if isinstance(block, dict) and inner_key in block:
                del block[inner_key]
                if not block:
                    config.pop("alerts", None)
                # Pre-persist guard (mirrors the set branches): unsetting a key
                # that participates in a cross-field constraint
                # (alerts.command_template while alerts.notifier == "command")
                # would leave a config that _get_alerts_config rejects on the
                # next read. Validate the TOP-LEVEL config (so a pruned/empty
                # alerts block correctly validates to defaults) and refuse
                # rather than persist an unreadable config.
                try:
                    _get_alerts_config(config)
                except _AlertsConfigError as exc:
                    print(
                        f"cctally: alerts config error: {exc}", file=sys.stderr
                    )
                    return 2
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
    if key == "dashboard.expose_transcripts":
        # Mirror the dashboard.bind unset branch: drop only the
        # expose_transcripts leaf; if the dashboard block ends up empty, drop
        # the parent too so config.json stays tidy. A sibling dashboard.bind
        # survives.
        with config_writer_lock():
            config = _load_config_unlocked()
            block = config.get("dashboard")
            if isinstance(block, dict) and "expose_transcripts" in block:
                del block["expose_transcripts"]
                if not block:
                    config.pop("dashboard", None)
                save_config(config)
            # idempotent: silent on missing key
        return 0
    if key == "dashboard.cache_failure_markers":
        # Mirror the dashboard.expose_transcripts unset branch: drop only the
        # cache_failure_markers leaf; if the dashboard block ends up empty, drop
        # the parent too so config.json stays tidy. Sibling dashboard.bind /
        # expose_transcripts survive. Unsetting restores the True (opt-out)
        # default at read time.
        with config_writer_lock():
            config = _load_config_unlocked()
            block = config.get("dashboard")
            if isinstance(block, dict) and "cache_failure_markers" in block:
                del block["cache_failure_markers"]
                if not block:
                    config.pop("dashboard", None)
                save_config(config)
            # idempotent: silent on missing key
        return 0
    if key == "dashboard.live_tail":
        # Mirror the cache_failure_markers unset branch: drop only the
        # live_tail leaf; if the dashboard block ends up empty, drop the parent
        # too. Sibling dashboard.bind / expose_transcripts / cache_failure_markers
        # survive. Unsetting restores the True (opt-out) default at read time.
        with config_writer_lock():
            config = _load_config_unlocked()
            block = config.get("dashboard")
            if isinstance(block, dict) and "live_tail" in block:
                del block["live_tail"]
                if not block:
                    config.pop("dashboard", None)
                save_config(config)
            # idempotent: silent on missing key
        return 0
    if key == "telemetry.enabled":
        # Mirror the dashboard.live_tail unset branch: drop only the enabled
        # leaf; if the telemetry block ends up empty, drop the parent too.
        # Unsetting restores the True (opt-out) default at read time.
        with config_writer_lock():
            config = _load_config_unlocked()
            block = config.get("telemetry")
            if isinstance(block, dict) and "enabled" in block:
                del block["enabled"]
                if not block:
                    config.pop("telemetry", None)
                save_config(config)
            # idempotent: silent on missing key
        return 0
    if key in (
        "statusline.visual_burn_rate",
        "statusline.cost_source",
        "statusline.cctally_extensions",
        "statusline.usage_only",
    ):
        inner_key = key.split(".", 1)[1]
        with config_writer_lock():
            config = _load_config_unlocked()
            block = config.get("statusline")
            if isinstance(block, dict) and inner_key in block:
                del block[inner_key]
                if not block:
                    config.pop("statusline", None)
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
    if key in (
        "budget.weekly_usd",
        "budget.alerts_enabled",
        "budget.alert_thresholds",
        "budget.projected_enabled",
        "budget.period",
        "budget.projects",
        "budget.project_alerts_enabled",
        "budget.codex",
    ):
        # Drop only the named leaf; preserve sibling budget.* keys (e.g.
        # unsetting weekly_usd keeps a customized alert_thresholds). If the
        # `budget` block ends up empty, drop the parent so config.json stays
        # tidy. Mirrors the alerts.enabled / dashboard.bind unset branches.
        inner_key = key.split(".", 1)[1]
        with config_writer_lock():
            config = _load_config_unlocked()
            block = config.get("budget")
            if isinstance(block, dict) and inner_key in block:
                del block[inner_key]
                if not block:
                    config.pop("budget", None)
                save_config(config)
            # idempotent: silent on missing key
        return 0
    return 2  # unreachable given the gate above
