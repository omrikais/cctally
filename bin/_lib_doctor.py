"""Pure-function kernel for `cctally doctor`.

Module boundary: bin/cctally imports _lib_doctor — never the reverse.
Per the spec (docs/superpowers/specs/2026-05-13-doctor-design.md §7.1),
all I/O happens in `doctor_gather_state()` in bin/cctally; this
module operates on already-gathered DoctorState dataclasses and is
deterministic given its input.

User-facing reference: docs/commands/doctor.md.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import pathlib
import sys
from typing import Optional


@dataclasses.dataclass
class DoctorState:
    """All on-disk / in-DB inputs needed to run checks. Built by
    `doctor_gather_state()` in bin/cctally. Each field is independently
    optional so a failed read in one corner degrades the dependent
    check(s) without killing the rest of the report."""
    # Install
    symlink_state: Optional[list[tuple[str, str]]]
    path_includes_local_bin: Optional[bool]
    legacy_snippet: Optional[tuple[pathlib.Path, list[int]]]
    legacy_bespoke: Optional[dict]
    # Hooks
    # claude_settings is populated by doctor_gather_state() (Task 13) via
    # `_load_claude_settings()`; spec §7.1 includes it in the contract.
    # The kernel does not currently consume it — it feeds verbose render
    # and a future evaluator that walks the parsed settings tree.
    claude_settings: Optional[dict]
    hook_counts: Optional[dict[str, int]]
    log_activity_24h: Optional[dict]
    # Auth
    oauth_token_present: Optional[bool]
    # DB
    stats_db_status: Optional[dict]
    cache_db_status: Optional[dict]
    # Data
    latest_snapshot_at: Optional[dt.datetime]
    cache_entries_count: Optional[int]
    cache_last_entry_at: Optional[dt.datetime]
    claude_jsonl_present: bool
    codex_entries_count: Optional[int]
    codex_last_entry_at: Optional[dt.datetime]
    codex_jsonl_present: bool
    # Safety
    dashboard_bind_stored: str
    runtime_bind: Optional[str]
    config_json_error: Optional[str]
    update_state: Optional[dict]
    update_state_error: Optional[str]
    update_suppress: Optional[dict]
    update_suppress_error: Optional[str]
    # Meta
    now_utc: dt.datetime
    cctally_version: str


@dataclasses.dataclass(frozen=True)
class CheckResult:
    id: str
    title: str
    severity: str            # "ok" | "warn" | "fail"
    summary: str
    remediation: Optional[str]
    details: dict


@dataclasses.dataclass(frozen=True)
class CategoryResult:
    id: str
    title: str
    severity: str
    checks: tuple[CheckResult, ...]


@dataclasses.dataclass(frozen=True)
class DoctorReport:
    schema_version: int
    generated_at: dt.datetime
    cctally_version: str
    overall_severity: str
    counts: dict[str, int]
    categories: tuple[CategoryResult, ...]


SCHEMA_VERSION = 1
SEVERITY_ORDER = ("ok", "warn", "fail")


def _max_severity(severities: list[str]) -> str:
    """Return the highest severity in the list per SEVERITY_ORDER ordering."""
    if not severities:
        return "ok"
    return max(severities, key=SEVERITY_ORDER.index)


def _check_install_symlinks(s: DoctorState) -> CheckResult:
    if s.symlink_state is None:
        return CheckResult(
            id="install.symlinks", title="Symlinks",
            severity="fail", summary="state unavailable",
            remediation="See logs", details={"reason": "gather returned None"},
        )
    total = len(s.symlink_state)
    missing = [n for n, st in s.symlink_state if st != "ok"]
    ok_count = total - len(missing)
    if not missing:
        return CheckResult(
            id="install.symlinks", title="Symlinks",
            severity="ok", summary=f"{ok_count}/{total} present",
            remediation=None,
            details={"present": ok_count, "total": total, "missing": []},
        )
    return CheckResult(
        id="install.symlinks", title="Symlinks",
        severity="warn",
        summary=f"{ok_count}/{total} present; missing {', '.join(missing)}",
        remediation="Run `cctally setup`",
        details={"present": ok_count, "total": total, "missing": missing},
    )


def _check_install_path(s: DoctorState) -> CheckResult:
    if s.path_includes_local_bin:
        return CheckResult(
            id="install.path", title="PATH",
            severity="ok", summary="~/.local/bin on $PATH",
            remediation=None, details={},
        )
    return CheckResult(
        id="install.path", title="PATH",
        severity="warn", summary="~/.local/bin not on $PATH",
        remediation="Append `export PATH=\"$HOME/.local/bin:$PATH\"` to your shell rc",
        details={},
    )


def _check_install_legacy_snippet(s: DoctorState) -> CheckResult:
    if s.legacy_snippet is None:
        return CheckResult(
            id="install.legacy_snippet", title="Legacy status-line snippet",
            severity="ok", summary="not detected",
            remediation=None, details={},
        )
    path, lines = s.legacy_snippet
    location = f"{path}:{lines[0]}" if lines else str(path)
    return CheckResult(
        id="install.legacy_snippet", title="Legacy status-line snippet",
        severity="warn", summary=f"detected at {location}",
        remediation=f"Edit {path} to remove the cctally status-line snippet",
        details={"path": str(path), "line_numbers": list(lines)},
    )


def _check_install_legacy_bespoke(s: DoctorState) -> CheckResult:
    info = s.legacy_bespoke or {"detected": False, "settings_entries": [], "files": []}
    if not info.get("detected"):
        return CheckResult(
            id="install.legacy_bespoke_hooks", title="Legacy bespoke hooks",
            severity="ok", summary="not detected",
            remediation=None, details={},
        )
    n_entries = len(info.get("settings_entries") or [])
    n_files = len(info.get("files") or [])
    return CheckResult(
        id="install.legacy_bespoke_hooks", title="Legacy bespoke hooks",
        severity="warn",
        summary=f"detected ({n_entries} entries, {n_files} files)",
        remediation="Run `cctally setup --migrate-legacy-hooks`",
        details={"entries": n_entries, "files": n_files},
    )


_REQUIRED_HOOK_EVENTS = ("PostToolBatch", "Stop", "SubagentStop")


def _check_hooks_installed(s: DoctorState) -> CheckResult:
    counts = s.hook_counts or {}
    missing = [ev for ev in _REQUIRED_HOOK_EVENTS if counts.get(ev, 0) < 1]
    if not missing:
        return CheckResult(
            id="hooks.installed", title="Hook entries installed",
            severity="ok",
            summary=", ".join(_REQUIRED_HOOK_EVENTS),
            remediation=None,
            details={"counts": {ev: counts.get(ev, 0) for ev in _REQUIRED_HOOK_EVENTS}},
        )
    return CheckResult(
        id="hooks.installed", title="Hook entries installed",
        severity="warn",
        summary=f"missing {', '.join(missing)}",
        remediation="Run `cctally setup`",
        details={
            "counts": {ev: counts.get(ev, 0) for ev in _REQUIRED_HOOK_EVENTS},
            "missing": missing,
        },
    )


def _check_hooks_recent_activity_24h(s: DoctorState) -> CheckResult:
    act = s.log_activity_24h or {"fires": 0, "errors": 0,
                                  "by_event": {}, "last_fire_ago_s": None,
                                  "oauth_ok": 0, "throttled": 0}
    fires = act.get("fires") or 0
    errors = act.get("errors") or 0
    if fires == 0:
        return CheckResult(
            id="hooks.recent_activity_24h", title="Recent activity (24h)",
            severity="warn", summary="0 fires",
            remediation="No hook fired in last 24h. Restart Claude Code, or run `cctally setup`.",
            details={"fires": 0, "errors": errors, "by_event": act.get("by_event") or {},
                     "last_fire_age_s": act.get("last_fire_ago_s")},
        )
    ratio = errors / fires if fires else 0.0
    if ratio >= 0.5:
        return CheckResult(
            id="hooks.recent_activity_24h", title="Recent activity (24h)",
            severity="warn",
            summary=f"high error ratio ({errors}/{fires})",
            remediation="Check ~/.local/share/cctally/logs/hook-tick.log",
            details={"fires": fires, "errors": errors, "ratio": ratio,
                     "by_event": act.get("by_event") or {}},
        )
    return CheckResult(
        id="hooks.recent_activity_24h", title="Recent activity (24h)",
        severity="ok",
        summary=f"{fires} fires, {errors} errors",
        remediation=None,
        details={"fires": fires, "errors": errors,
                 "by_event": act.get("by_event") or {},
                 "last_fire_age_s": act.get("last_fire_ago_s")},
    )


def _check_hooks_last_fire_age(s: DoctorState) -> CheckResult:
    act = s.log_activity_24h or {}
    age = act.get("last_fire_ago_s")
    if age is None:
        return CheckResult(
            id="hooks.last_fire_age", title="Last hook fire",
            severity="warn", summary="never",
            remediation="No hook has fired yet. Restart Claude Code.",
            details={"last_fire_age_s": None},
        )
    if age > 3600:
        return CheckResult(
            id="hooks.last_fire_age", title="Last hook fire",
            severity="warn", summary=f"{int(age)}s ago",
            remediation="No hook fired in >1h. Claude Code may not be running.",
            details={"last_fire_age_s": int(age)},
        )
    return CheckResult(
        id="hooks.last_fire_age", title="Last hook fire",
        severity="ok", summary=f"{int(age)}s ago",
        remediation=None,
        details={"last_fire_age_s": int(age)},
    )


def _check_oauth_token_present(s: DoctorState) -> CheckResult:
    if s.oauth_token_present:
        return CheckResult(
            id="oauth.token_present", title="OAuth token",
            severity="ok", summary="present",
            remediation=None, details={},
        )
    return CheckResult(
        id="oauth.token_present", title="OAuth token",
        severity="fail", summary="missing",
        remediation="Log into Claude Code to populate the OAuth token",
        details={},
    )


def _db_file_check(label_id: str, label_title: str, status: Optional[dict],
                   rebuild_hint: str) -> CheckResult:
    if status is None:
        return CheckResult(
            id=label_id, title=label_title,
            severity="fail", summary="state unavailable",
            remediation="Re-run; see stderr",
            details={"reason": "gather returned None"},
        )
    if status.get("_open_error"):
        return CheckResult(
            id=label_id, title=label_title,
            severity="fail", summary=f"could not open: {status['_open_error']}",
            remediation=rebuild_hint,
            details={"exception": status["_open_error"], "path": status["path"]},
        )
    if status.get("_file_exists") is False:
        return CheckResult(
            id=label_id, title=label_title,
            severity="warn", summary="absent (fresh install)",
            remediation=None,
            details={"path": status["path"]},
        )
    return CheckResult(
        id=label_id, title=label_title,
        severity="ok",
        summary=f"version {status['user_version']} / {status['registry_size']} known",
        remediation=None,
        details={"path": status["path"],
                 "user_version": status["user_version"],
                 "registry_size": status["registry_size"]},
    )


def _check_db_stats_file(s: DoctorState) -> CheckResult:
    return _db_file_check("db.stats.file", "stats.db", s.stats_db_status,
                          "Restore from backup, or `cctally setup --uninstall --purge` + re-record")


def _check_db_cache_file(s: DoctorState) -> CheckResult:
    return _db_file_check("db.cache.file", "cache.db", s.cache_db_status,
                          "Run `cctally cache-sync --rebuild`")


def _migrations_by_status(status: Optional[dict]) -> dict[str, list[str]]:
    if not status:
        return {"applied": [], "skipped": [], "pending": [], "failed": []}
    out = {"applied": [], "skipped": [], "pending": [], "failed": []}
    for m in status.get("migrations") or []:
        out.setdefault(m["status"], []).append(m["name"])
    return out


def _check_db_migrations_applied(s: DoctorState) -> CheckResult:
    both = {}
    for db_label, st in (("stats.db", s.stats_db_status), ("cache.db", s.cache_db_status)):
        both[db_label] = _migrations_by_status(st)
    any_failed = any(both[d]["failed"] for d in both)
    any_skipped = any(both[d]["skipped"] for d in both)
    if any_failed:
        failed = [(d, n) for d, info in both.items() for n in info["failed"]]
        return CheckResult(
            id="db.migrations.applied", title="Migrations",
            severity="fail",
            summary=f"{len(failed)} failed",
            remediation="Run `cctally db status`; see ~/.local/share/cctally/logs/migration-errors.log",
            details={"failed": failed, "by_db": both},
        )
    if any_skipped:
        skipped = [(d, n) for d, info in both.items() for n in info["skipped"]]
        return CheckResult(
            id="db.migrations.applied", title="Migrations",
            severity="warn",
            summary=f"{len(skipped)} skipped",
            remediation="Run `cctally db unskip <name>` if you want to retry",
            details={"skipped": skipped, "by_db": both},
        )
    total_applied = sum(len(both[d]["applied"]) for d in both)
    total_registered = ((s.stats_db_status or {}).get("registry_size", 0)
                        + (s.cache_db_status or {}).get("registry_size", 0))
    return CheckResult(
        id="db.migrations.applied", title="Migrations",
        severity="ok",
        summary=f"{total_applied}/{total_registered} applied",
        remediation=None,
        details={"by_db": both},
    )


def _check_db_migrations_pending(s: DoctorState) -> CheckResult:
    both = {db: _migrations_by_status(st)
            for db, st in (("stats.db", s.stats_db_status),
                           ("cache.db", s.cache_db_status))}
    pending = [(d, n) for d, info in both.items() for n in info["pending"]]
    if not pending:
        return CheckResult(
            id="db.migrations.pending", title="Pending migrations",
            severity="ok", summary="none pending",
            remediation=None, details={},
        )
    return CheckResult(
        id="db.migrations.pending", title="Pending migrations",
        severity="warn",
        summary=f"{len(pending)} pending",
        remediation="Run any cctally command — opens the DB and applies pending migrations",
        details={"pending": pending},
    )


def _check_data_latest_snapshot_age(s: DoctorState) -> CheckResult:
    if s.latest_snapshot_at is None:
        return CheckResult(
            id="data.latest_snapshot_age", title="Latest snapshot",
            severity="fail", summary="never",
            remediation="Check hooks are installed and Claude Code is running",
            details={"latest_snapshot_at": None},
        )
    age_s = int((s.now_utc - s.latest_snapshot_at).total_seconds())
    if age_s <= 300:
        sev, rem = "ok", None
    elif age_s <= 3600:
        sev = "warn"
        rem = "Recent but not current. Check Claude Code session is active."
    else:
        sev = "fail"
        rem = "No snapshot in >1h. Hooks may be broken — check `cctally setup --status`."
    return CheckResult(
        id="data.latest_snapshot_age", title="Latest snapshot",
        severity=sev, summary=f"{age_s}s ago",
        remediation=rem,
        details={"latest_snapshot_at": s.latest_snapshot_at.isoformat(),
                 "latest_snapshot_age_s": age_s},
    )


def _check_data_cache_sync_state(s: DoctorState) -> CheckResult:
    count = s.cache_entries_count or 0
    if count == 0:
        if s.claude_jsonl_present:
            return CheckResult(
                id="data.cache_sync_state", title="Claude cache",
                severity="warn",
                summary="0 entries despite JSONL files present",
                remediation="Run `cctally cache-sync --rebuild`",
                details={"entries": 0, "claude_jsonl_present": True},
            )
        return CheckResult(
            id="data.cache_sync_state", title="Claude cache",
            severity="ok", summary="0 entries (no JSONL corpus)",
            remediation=None,
            details={"entries": 0, "claude_jsonl_present": False},
        )
    if s.cache_last_entry_at is None:
        age_s = None
    else:
        age_s = int((s.now_utc - s.cache_last_entry_at).total_seconds())
    if age_s is not None and age_s > 24 * 3600:
        return CheckResult(
            id="data.cache_sync_state", title="Claude cache",
            severity="warn",
            summary=f"{count:,} entries; last sync {age_s}s ago (>24h)",
            remediation="Run `cctally cache-sync --rebuild`",
            details={"entries": count, "cache_last_entry_age_s": age_s},
        )
    return CheckResult(
        id="data.cache_sync_state", title="Claude cache",
        severity="ok",
        summary=f"{count:,} entries; last sync {age_s}s ago" if age_s is not None
                else f"{count:,} entries",
        remediation=None,
        details={"entries": count, "cache_last_entry_age_s": age_s},
    )


def _check_data_codex_cache(s: DoctorState) -> CheckResult:
    count = s.codex_entries_count or 0
    if count == 0 and not s.codex_jsonl_present:
        return CheckResult(
            id="data.codex_cache", title="Codex cache",
            severity="ok", summary="none (no ~/.codex/sessions/)",
            remediation=None,
            details={"entries": 0, "codex_jsonl_present": False},
        )
    if count == 0 and s.codex_jsonl_present:
        return CheckResult(
            id="data.codex_cache", title="Codex cache",
            severity="warn",
            summary="0 entries despite Codex JSONL files present",
            remediation="Run `cctally cache-sync --source codex --rebuild`",
            details={"entries": 0, "codex_jsonl_present": True},
        )
    if s.codex_last_entry_at is None:
        age_s = None
    else:
        age_s = int((s.now_utc - s.codex_last_entry_at).total_seconds())
    if age_s is not None and age_s > 24 * 3600:
        return CheckResult(
            id="data.codex_cache", title="Codex cache",
            severity="warn",
            summary=f"{count:,} entries; last sync {age_s}s ago (>24h)",
            remediation="Run `cctally cache-sync --source codex --rebuild`",
            details={"entries": count, "codex_last_entry_age_s": age_s},
        )
    return CheckResult(
        id="data.codex_cache", title="Codex cache",
        severity="ok",
        summary=f"{count:,} entries; last sync {age_s}s ago" if age_s is not None
                else f"{count:,} entries",
        remediation=None,
        details={"entries": count, "codex_last_entry_age_s": age_s},
    )


_LOOPBACK_HOSTS = frozenset({"loopback", "127.0.0.1", "::1", "localhost"})


def _check_safety_dashboard_bind(s: DoctorState) -> CheckResult:
    stored_ok = s.dashboard_bind_stored in _LOOPBACK_HOSTS
    runtime_ok = (s.runtime_bind is None) or (s.runtime_bind in _LOOPBACK_HOSTS)
    if stored_ok and runtime_ok:
        suffix = f"; running: {s.runtime_bind}" if s.runtime_bind else ""
        return CheckResult(
            id="safety.dashboard_bind", title="Dashboard bind",
            severity="ok",
            summary=f"config: {s.dashboard_bind_stored}{suffix}",
            remediation=None,
            details={"config": s.dashboard_bind_stored,
                     "runtime_bind": s.runtime_bind},
        )
    notes = []
    if not stored_ok:
        notes.append(f"config: {s.dashboard_bind_stored}")
    if not runtime_ok:
        notes.append(f"running: {s.runtime_bind}")
    rem = "Run `cctally config set dashboard.bind loopback`"
    if not runtime_ok:
        rem += "; restart the dashboard process if it was launched with `--host`"
    rem += "."
    note = ("A separate running dashboard process may have overridden via --host; "
            "the CLI sees config only.") if s.runtime_bind is None else None
    return CheckResult(
        id="safety.dashboard_bind", title="Dashboard bind",
        severity="warn", summary="; ".join(notes),
        remediation=rem,
        details={"config": s.dashboard_bind_stored,
                 "runtime_bind": s.runtime_bind,
                 **({"note": note} if note else {})},
    )


def _check_safety_config_json_valid(s: DoctorState) -> CheckResult:
    if s.config_json_error is None:
        return CheckResult(
            id="safety.config_json_valid", title="config.json",
            severity="ok", summary="absent or parses cleanly",
            remediation=None, details={},
        )
    return CheckResult(
        id="safety.config_json_valid", title="config.json",
        severity="fail",
        summary=f"unreadable: {s.config_json_error}",
        remediation="Fix or remove ~/.local/share/cctally/config.json",
        details={"exception": s.config_json_error},
    )


# Required keys per spec §3.6 + the producer code at bin/cctally:9663-9695
# (_load_update_state). `_schema` is set on every write; `current_version` and
# `latest_version` are the two semantically-meaningful fields the banner predicate
# and the doctor summary line consume.
_UPDATE_STATE_REQUIRED_KEYS = ("current_version", "latest_version")

# Required shape per spec §3.6 + bin/cctally:9725-9753 (_load_update_suppress)
# default record: {"_schema": 1, "skipped_versions": [], "remind_after": None}.
# `remind_after` is allowed to be None per the default — only its presence and
# the type when non-None are validated.
_UPDATE_SUPPRESS_REQUIRED_KEYS = ("skipped_versions", "remind_after")


def _check_safety_update_state(s: DoctorState) -> CheckResult:
    if s.update_state_error is not None:
        return CheckResult(
            id="safety.update_state", title="update-state.json",
            severity="fail", summary=f"unreadable: {s.update_state_error}",
            remediation="`rm ~/.local/share/cctally/update-state.json` (will be regenerated)",
            details={"exception": s.update_state_error},
        )
    if s.update_state is None:
        return CheckResult(
            id="safety.update_state", title="update-state.json",
            severity="warn", summary="absent (first run)",
            remediation="Run `cctally update --check` to populate",
            details={},
        )
    # Spec §3.6: WARN when known fields are missing. Both keys are needed
    # for the version-comparison banner predicate; without them the file
    # exists but is semantically unusable.
    missing = [k for k in _UPDATE_STATE_REQUIRED_KEYS if k not in s.update_state]
    if missing:
        return CheckResult(
            id="safety.update_state", title="update-state.json",
            severity="warn",
            summary=f"missing fields: {', '.join(missing)}",
            remediation="Run `cctally update --check` to refresh",
            details={"missing_keys": missing,
                     "current_version": s.update_state.get("current_version"),
                     "latest_version": s.update_state.get("latest_version")},
        )
    return CheckResult(
        id="safety.update_state", title="update-state.json",
        severity="ok",
        summary=f"v{s.update_state.get('current_version', '?')}",
        remediation=None,
        details={"current_version": s.update_state.get("current_version"),
                 "latest_version": s.update_state.get("latest_version")},
    )


def _check_safety_update_suppress(s: DoctorState) -> CheckResult:
    if s.update_suppress_error is not None:
        return CheckResult(
            id="safety.update_suppress", title="update-suppress.json",
            severity="fail", summary=f"unreadable: {s.update_suppress_error}",
            remediation="`rm ~/.local/share/cctally/update-suppress.json`",
            details={"exception": s.update_suppress_error},
        )
    if s.update_suppress is None:
        return CheckResult(
            id="safety.update_suppress", title="update-suppress.json",
            severity="ok", summary="absent (no deferrals)",
            remediation=None, details={},
        )
    # Spec §3.6: WARN on "known fields missing or unexpected types". The
    # producer's default record (bin/cctally:9731) defines the canonical
    # shape: {"skipped_versions": [], "remind_after": None}. Anything else
    # — a partial dict, wrong types — means a hand-edit or older binary
    # corrupted the file.
    missing = [k for k in _UPDATE_SUPPRESS_REQUIRED_KEYS if k not in s.update_suppress]
    bad_types: list[str] = []
    if "skipped_versions" in s.update_suppress:
        if not isinstance(s.update_suppress["skipped_versions"], list):
            bad_types.append("skipped_versions")
    if "remind_after" in s.update_suppress:
        v = s.update_suppress["remind_after"]
        if v is not None and not isinstance(v, (str, int, float)):
            bad_types.append("remind_after")
    if missing or bad_types:
        bits = []
        if missing:
            bits.append(f"missing: {', '.join(missing)}")
        if bad_types:
            bits.append(f"bad types: {', '.join(bad_types)}")
        return CheckResult(
            id="safety.update_suppress", title="update-suppress.json",
            severity="warn",
            summary="; ".join(bits),
            remediation="`rm ~/.local/share/cctally/update-suppress.json` (will be regenerated)",
            details={"missing_keys": missing, "bad_types": bad_types},
        )
    return CheckResult(
        id="safety.update_suppress", title="update-suppress.json",
        severity="ok", summary="parses cleanly",
        remediation=None,
        details={"skipped_versions": s.update_suppress.get("skipped_versions") or [],
                 "remind_after": s.update_suppress.get("remind_after")},
    )


def _check_safety_update_available(s: DoctorState) -> CheckResult:
    st = s.update_state or {}
    cur = st.get("current_version")
    lat = st.get("latest_version")
    if not cur or not lat or cur == lat:
        return CheckResult(
            id="safety.update_available", title="Update available",
            severity="ok", summary="no",
            remediation=None,
            details={"current_version": cur, "latest_version": lat},
        )
    return CheckResult(
        id="safety.update_available", title="Update available",
        severity="warn",
        summary=f"v{lat} (you are on v{cur})",
        remediation="Run `cctally update`",
        details={"current_version": cur, "latest_version": lat},
    )


# Each entry is (category_id, category_title, ((check_id, evaluator_fn_name), ...)).
# The dotted check_id is the stable JSON-contract ID (spec §5.2) AND the
# fingerprint identity-slice key (spec §5.5). When an evaluator raises,
# `_evaluate_one` uses this id — not the function name — so the synthesized
# FAIL CheckResult retains the contract id and fingerprint stays stable across
# success-vs-raise transitions.
_CATEGORY_DEFINITIONS: tuple[tuple[str, str, tuple[tuple[str, str], ...]], ...] = (
    ("install", "Install", (
        ("install.symlinks", "_check_install_symlinks"),
        ("install.path", "_check_install_path"),
        ("install.legacy_snippet", "_check_install_legacy_snippet"),
        ("install.legacy_bespoke_hooks", "_check_install_legacy_bespoke"),
    )),
    ("hooks", "Hooks", (
        ("hooks.installed", "_check_hooks_installed"),
        ("hooks.recent_activity_24h", "_check_hooks_recent_activity_24h"),
        ("hooks.last_fire_age", "_check_hooks_last_fire_age"),
    )),
    ("auth", "Auth", (
        ("oauth.token_present", "_check_oauth_token_present"),
    )),
    ("db", "Database", (
        ("db.stats.file", "_check_db_stats_file"),
        ("db.cache.file", "_check_db_cache_file"),
        ("db.migrations.applied", "_check_db_migrations_applied"),
        ("db.migrations.pending", "_check_db_migrations_pending"),
    )),
    ("data", "Data", (
        ("data.latest_snapshot_age", "_check_data_latest_snapshot_age"),
        ("data.cache_sync_state", "_check_data_cache_sync_state"),
        ("data.codex_cache", "_check_data_codex_cache"),
    )),
    ("safety", "Safety", (
        ("safety.dashboard_bind", "_check_safety_dashboard_bind"),
        ("safety.config_json_valid", "_check_safety_config_json_valid"),
        ("safety.update_state", "_check_safety_update_state"),
        ("safety.update_suppress", "_check_safety_update_suppress"),
        ("safety.update_available", "_check_safety_update_available"),
    )),
)


def _evaluate_one(check_id: str, check_fn_name: str,
                  state: DoctorState) -> CheckResult:
    """Invoke a single check evaluator by name, catching any exception so
    one bad check does not crash the whole report. Spec §7.1, §5.4.

    On exception, the synthesized FAIL CheckResult uses the canonical
    dotted ``check_id`` (NOT the function name) so the JSON contract
    (spec §5.2) and fingerprint identity slice (spec §5.5) stay stable
    across success-vs-raise transitions.
    """
    mod = sys.modules[__name__]
    fn = getattr(mod, check_fn_name, None)
    if fn is None:
        return CheckResult(
            id=check_id, title=check_id,
            severity="fail",
            summary=f"evaluator not found: {check_fn_name}",
            remediation="Internal error; see bin/_lib_doctor.py",
            details={"exception": f"NameError: {check_fn_name}"},
        )
    try:
        return fn(state)
    except Exception as exc:  # noqa: BLE001 — deliberate broad catch per spec §7.1
        return CheckResult(
            id=check_id, title=check_id,
            severity="fail",
            summary=f"{type(exc).__name__}: {exc}",
            remediation="See details.exception",
            details={"exception": f"{type(exc).__name__}: {exc}"},
        )


def run_checks(state: DoctorState) -> DoctorReport:
    categories: list[CategoryResult] = []
    counts = {"ok": 0, "warn": 0, "fail": 0}
    for cat_id, cat_title, check_specs in _CATEGORY_DEFINITIONS:
        results: list[CheckResult] = []
        for check_id, fn_name in check_specs:
            r = _evaluate_one(check_id, fn_name, state)
            results.append(r)
            counts[r.severity] = counts.get(r.severity, 0) + 1
        cat_sev = _max_severity([r.severity for r in results])
        categories.append(CategoryResult(
            id=cat_id, title=cat_title,
            severity=cat_sev,
            checks=tuple(results),
        ))
    overall = _max_severity([c.severity for c in categories])
    return DoctorReport(
        schema_version=SCHEMA_VERSION,
        generated_at=state.now_utc,
        cctally_version=state.cctally_version,
        overall_severity=overall,
        counts=counts,
        categories=tuple(categories),
    )


def _iso_z(d: dt.datetime) -> str:
    """Render a UTC datetime as ISO 8601 with trailing 'Z' (share-v2 convention)."""
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    s = d.astimezone(dt.timezone.utc).isoformat()
    return s.replace("+00:00", "Z")


def _serialize_check(c: CheckResult) -> dict:
    out = {
        "id": c.id,
        "title": c.title,
        "severity": c.severity,
        "summary": c.summary,
        "details": c.details,
    }
    if c.severity != "ok" and c.remediation:
        out["remediation"] = c.remediation
    return out


def serialize_json(report: DoctorReport) -> dict:
    """Produce the stable JSON payload per spec §5.1-§5.2.

    Top-level fields are contract; the per-check `details` block is
    unstable (consumers MUST tolerate unknown keys). schema_version
    bumps only on a breaking change to the stable fields.
    """
    return {
        "schema_version": report.schema_version,
        "generated_at": _iso_z(report.generated_at),
        "cctally_version": report.cctally_version,
        "overall": {
            "severity": report.overall_severity,
            "counts": dict(report.counts),
        },
        "categories": [
            {
                "id": cat.id,
                "title": cat.title,
                "severity": cat.severity,
                "checks": [_serialize_check(c) for c in cat.checks],
            }
            for cat in report.categories
        ],
    }


def _identity_slice(report: DoctorReport) -> dict:
    """The fields the fingerprint hashes over. Excludes generated_at,
    cctally_version, summary text, remediation, and the entire details
    block — those carry volatile values that change tick-to-tick even
    when severity doesn't flip. See spec §5.5."""
    return {
        "schema_version": report.schema_version,
        "overall_severity": report.overall_severity,
        "counts": dict(report.counts),
        "checks": [
            [c.id, c.severity]
            for cat in report.categories
            for c in cat.checks
        ],
    }


def fingerprint(report: DoctorReport) -> str:
    """Stable SHA1 over the identity slice. Same identity slice → same
    fingerprint, even when ages and rendered summaries change."""
    payload = json.dumps(_identity_slice(report), sort_keys=True, separators=(",", ":"))
    h = hashlib.sha1(payload.encode("utf-8")).hexdigest()
    return f"sha1:{h}"


_GLYPH = {"ok": "✓", "warn": "⚠", "fail": "✗"}


def render_text(report: DoctorReport, *, quiet: bool = False, verbose: bool = False) -> str:
    if quiet and verbose:
        raise ValueError("render_text: --quiet and --verbose are mutually exclusive")
    lines: list[str] = []
    ts = _iso_z(report.generated_at).replace("T", " ").replace("Z", " UTC")
    lines.append(f"cctally doctor — {ts}")
    lines.append("")
    for cat in report.categories:
        lines.append(cat.title)
        for c in cat.checks:
            if quiet and c.severity == "ok":
                continue
            glyph = _GLYPH.get(c.severity, "?")
            lines.append(f"  {glyph} {c.title:<24s} {c.summary}")
            if c.remediation:
                lines.append(f"      → {c.remediation}")
            if verbose and c.details:
                lines.append("      details:")
                for k, v in c.details.items():
                    lines.append(f"        {k}: {v}")
    lines.append("")
    counts = report.counts
    lines.append(
        f"Summary: {counts.get('ok', 0)} OK · "
        f"{counts.get('warn', 0)} WARN · "
        f"{counts.get('fail', 0)} FAIL"
    )
    return "\n".join(lines) + "\n"
