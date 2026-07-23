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
import math
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
    # Forked-bucket invariant counts (data.forked_buckets check).
    # Keys: "usage", "cost", "milestones" — each maps to the count of
    # rows in the respective table where ``week_start_at IS NOT NULL``
    # AND ``week_start_date != substr(week_start_at, 1, 10)``. None
    # means the stats.db couldn't be opened to check; the migration
    # ``004_heal_forked_week_start_date_buckets`` auto-merges any
    # detected rows on the next ``open_db()``, so a non-zero count
    # here indicates either (a) the migration is gated as
    # skipped/failed/pending or (b) a buggy writer slipped through
    # after the migration ran.
    forked_bucket_counts: Optional[dict]
    # v1.7.2 credited-week tracking. Each entry is a dict with:
    #   * ``week_start_date``   — the credited week's bucket key
    #   * ``latest_weekly_percent`` — most recent weekly_percent for that
    #     week (used to gate the WARN — a credit + 0% means the user
    #     hasn't started the new segment yet, which is the EXPECTED
    #     state and shouldn't warn)
    #   * ``post_credit_milestone_count`` — count of percent_milestones
    #     rows with ``reset_event_id`` matching the credit event for
    #     this week
    # None means the stats.db couldn't be opened to gather; check
    # degrades to OK rather than FAIL (consistent with the rest of
    # the doctor kernel's degradation posture).
    credited_weeks: Optional[list[dict]]
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
    # Precomputed by `doctor_gather_state` via the same predicate that
    # gates the update banner (`_compute_effective_update_available`).
    # Keeps the kernel free of release-version knowledge while staying
    # in lockstep with the banner: doctor must never warn about an
    # update the user has skipped or deferred.
    effective_update_available: Optional[bool]
    effective_update_reason: Optional[str]
    # Meta
    now_utc: dt.datetime
    cctally_version: str
    # Dev-instance isolation (2026-05-26): which data dir this process
    # resolved, and whether it was via dev-checkout auto-detect.
    # `is_dev_checkout` is the binary-location fact (running from a git
    # checkout), independent of `dev_mode` (which is False when an explicit
    # CCTALLY_DATA_DIR override won at step 1). The override-on-checkout case
    # is `is_dev_checkout=True, dev_mode=False` — distinct from installed.
    dev_mode: bool
    app_dir: str
    is_dev_checkout: bool = False
    # Issue #119: availability-aware install checks. Both precomputed by
    # `doctor_gather_state` (the I/O layer) so the kernel stays pure —
    # `shutil.which` and the on-disk legacy-link probe never run here.
    # Defaulted (and placed last, after `is_dev_checkout`) so existing
    # constructors that don't pass them still work and the dataclass's
    # non-default-then-default field ordering stays valid.
    #   * cctally_reachable_on_path — `shutil.which("cctally") is not None`;
    #     channel-agnostic (brew `<prefix>/bin`, npm prefix, source
    #     `~/.local/bin` all satisfy it). Lets `_check_install_path` pass
    #     whenever the command is reachable, not only when `~/.local/bin`
    #     is on PATH.
    #   * symlinks_path_pinned — true iff cctally is reachable ONLY through
    #     a legacy `~/.local/bin` link to a retired/foreign (e.g. Homebrew
    #     keg) install (a live retired link with no `reachable_elsewhere`
    #     fallback). The kernel can't tell this `wrong`-mode apart from an
    #     ordinary occupied slot from `(name, state)` alone, so it's
    #     precomputed; drives the PATH-fix remediation in
    #     `_check_install_symlinks`.
    #   * install_is_brew — true iff this cctally runs from a Homebrew keg
    #     (`_setup_is_brew_install(repo_root)`). Channel knowledge the
    #     kernel can't derive from `repo_root` (it does no I/O); drives the
    #     channel-aware `_check_install_path` WARN remediation so a brew
    #     install isn't told to fix a `~/.local/bin` it deliberately
    #     doesn't use (#119 made brew `~/.local/bin`-free).
    cctally_reachable_on_path: Optional[bool] = None
    symlinks_path_pinned: bool = False
    install_is_brew: bool = False
    # Pricing coverage (spec §5.1): the list[CoverageGap] of unpriced (Claude
    # $0) / fallback (Codex gpt-5) models observed in the trailing 30-day
    # window, populated by `doctor_gather_state` via `_pricing_observed_models`
    # + `classify_coverage`. None means the cache could not be read (or the
    # classification raised) — the check degrades to OK ("no cached usage to
    # assess"), consistent with the kernel's degradation posture. Each element
    # is a `_lib_pricing_check.CoverageGap` (provider/model/kind/entry_count/
    # token_total); the kernel only reads `.kind`/`.model`/`.entry_count`/
    # `.token_total`, so any duck-typed equivalent works for tests.
    pricing_coverage: Optional[list] = None
    # Conversation viewer (Plan 2, spec §5): the resolved
    # `dashboard.expose_transcripts` opt-in. Only consequential when the bind
    # is LAN — `_check_safety_dashboard_bind` then surfaces an extra
    # "transcripts exposed on LAN" detail on top of the existing LAN-bind
    # WARN. Defaulted False (placed last after the other defaulted fields) so
    # existing constructors stay valid and a loopback bind is byte-identical
    # whether or not expose is set.
    expose_transcripts: bool = False
    # Conversation-sessions rollup consistency (#217 S1 / U9). The browse-rail
    # rollup table `conversation_sessions` should carry one row per distinct
    # `conversation_messages.session_id`; a quiescent mismatch indicates the
    # rollup drifted from its source. `conv_sessions_rollup_count` =
    # COUNT(*) conversation_sessions; `conv_messages_distinct_sessions` =
    # COUNT(DISTINCT session_id) conversation_messages WHERE session_id IS NOT
    # NULL. Either is None when cache.db can't be opened / the table is absent
    # (pre-rollup) — the check degrades to OK. `conv_rollup_sync_in_progress`
    # is True when a writer holds the cache.db.lock (non-blocking probe) OR any
    # pending reingest/split/backfill `cache_meta` flag is present — sync_cache
    # commits `conversation_messages` per file BEFORE the rollup recompute, so a
    # mid-sync read transiently mismatches and must NOT WARN (Codex P2). All
    # defaulted (placed last) so existing constructors stay valid.
    conv_sessions_rollup_count: Optional[int] = None
    conv_messages_distinct_sessions: Optional[int] = None
    conv_rollup_sync_in_progress: bool = False
    # Preview channel (CCTALLY_CHANNEL=preview): "preview" when the binary runs
    # under the preview channel, else "prod". Populated by `doctor_gather_state`
    # from the env; surfaced in the install.mode check. Defaulted (placed last)
    # so existing constructors stay valid and the prod path is unchanged.
    channel: str = "prod"
    # Anonymous install-count telemetry (spec 2026-07-07): the resolved opt-out
    # state + precedence reason from `resolve_telemetry_state`, computed by
    # `doctor_gather_state` WITHOUT minting an install_id (read-only H1). Drives
    # the always-OK `telemetry.state` check — a diagnostic surface, never a
    # health failure. Defaulted (placed last) so existing constructors stay
    # valid and default to the enabled (opt-out) posture.
    telemetry_enabled: bool = True
    telemetry_reason: str = "enabled"
    # #279 S2 (F5a): rolling ingest parse-health records read from
    # cache_meta keys parse_health_claude / parse_health_codex (JSON
    # dicts; see _cctally_cache._update_parse_health_meta). None = key
    # absent (pre-first-sync) or cache unreadable — check degrades OK.
    parse_health_claude: Optional[dict] = None
    parse_health_codex: Optional[dict] = None
    # #279 S2 (F5b): PRAGMA quick_check(1) results, gathered ONLY under
    # doctor_gather_state(deep=True) (CLI cmd_doctor) — the dashboard
    # rebuild loop calls the gather every rebuild and quick_check on a
    # large cache.db costs seconds. "ok" | first error line |
    # "open failed: ..." | None = not run.
    stats_db_quick_check: Optional[str] = None
    cache_db_quick_check: Optional[str] = None
    # #279 S2 (F5c): non-blocking flock probes on the two sync lock files
    # (name -> True held / False free / None unreadable). Probe never
    # creates files (doctor read-only contract). None = probe errored.
    locks_held: Optional[dict] = None
    # #297: size in bytes of cache.db-wal, gathered read-only in
    # doctor_gather_state (getsize, OSError/absent -> None). Warns above
    # DOCTOR_WAL_WARN_BYTES (2x the WAL cap) — only when the journal_size_limit
    # + forced-checkpoint machinery has genuinely failed to contain the WAL.
    cache_db_wal_bytes: Optional[int] = None
    # #315: read-only PRAGMA page_count/freelist_count evidence. The pure
    # db.reclaimable check warns when free pages reach 25% of cache.db and
    # points at the already-guarded explicit vacuum command. None means the
    # cache was absent or unreadable; the check degrades to OK.
    cache_db_page_count: Optional[int] = None
    cache_db_freelist_count: Optional[int] = None
    # #320: the independently large transcript store needs the same reclaim
    # visibility, with remediation targeted at its own VACUUM surface.
    conversations_db_page_count: Optional[int] = None
    conversations_db_freelist_count: Optional[int] = None
    # #294 S2: root-qualified physical Codex quota freshness, per-root native
    # hook state, and lifecycle activity are gathered by _cctally_doctor.
    codex_quota_windows: Optional[list[dict]] = None
    codex_hook_roots: Optional[list[dict]] = None
    codex_lifecycle_activity_24h: Optional[dict] = None
    # #311: precomputed five-state classification of settings.json's
    # statusLine.refreshInterval (unavailable/absent/foreign/present/missing),
    # computed by doctor_gather_state via the setup I/O-layer classifier so the
    # kernel stays I/O-free (it never imports bin/_cctally_setup). Defaulted
    # (placed last after the other defaulted tail fields) so existing
    # constructors stay valid; the default "unavailable" is the always-OK,
    # never-WARN posture (the hooks.installed / settings warnings already
    # surface a genuinely-unreadable settings.json — no double-WARN here).
    statusline_refresh_state: str = "unavailable"
    # #318: read-only evidence for the per-session statusline candidate
    # arbitration pipeline.  None means the gather could not inspect it;
    # absent transport/tombstones are normal when Claude is closed.
    statusline_pipeline: Optional[dict] = None
    # #312: all-history, root-qualified Codex accounting metadata partition.
    # The I/O layer reports an explicit error instead of silently converting a
    # failed health query into zero rows, so doctor can distinguish an empty
    # retained corpus from unreadable metadata.
    codex_project_metadata_health: Optional[dict] = None
    codex_project_metadata_error: Optional[str] = None
    # Beta-channel (spec 2026-07-21 §3): the configured RELEASE channel
    # (stable|beta) that `cctally update` tracks — DISTINCT from the preview
    # `channel` (prod|preview) above. Populated by doctor_gather_state from a
    # raw config read (fail-soft to "stable"). Drives the `install.update_channel`
    # check (WARN on beta+brew, else OK). Defaulted (placed last) so existing
    # constructors stay valid and default to the stable posture.
    update_channel: str = "stable"
    # DB journal redesign §9: the append-only journal legs. All gathered by
    # `doctor_gather_state` (read-only), defaulted (tail) so existing
    # constructors stay valid and a pre-cutover install (no journal dir) reads
    # the always-OK "no journal" posture.
    #   * journal_present — the journal/ dir exists (a cut-over install has one;
    #     a legacy pre-cutover install does NOT — that is INFO, never FAIL).
    #   * journal_appendable — the dir is writable (os.access W_OK); None when
    #     absent or the probe errored.
    #   * journal_segment_count — number of segments (bootstrap + monthly).
    #   * journal_malformed_count / journal_torn_tail_count — mid-file malformed
    #     lines (external damage → WARN) and torn final lines (a known crash
    #     artifact healed by the next append → INFO). None = not scanned (the
    #     scan is `deep`-gated, like the quick_check legs, so the dashboard's
    #     per-rebuild gather never reads the whole journal).
    #   * journal_cursor_lag_bytes — unconsumed bytes between the stats index
    #     cursor and the journal high-water. None when there is no journal /
    #     cursor / the DB could not be read.
    #   * journal_hw_segment / journal_cursor_segment — the high-water segment
    #     and the cursor's segment, for the verbose detail.
    #   * journal_heal_incidents — most-recent-first list of the auto-heal
    #     artifacts (quarantine/ dirs + logs/<db>-corruption-forensics-*.json);
    #     each dict carries {kind, name, age_s}. None = the dirs were unreadable.
    journal_present: bool = False
    journal_appendable: Optional[bool] = None
    journal_segment_count: int = 0
    journal_malformed_count: Optional[int] = None
    journal_torn_tail_count: Optional[int] = None
    journal_cursor_lag_bytes: Optional[int] = None
    journal_hw_segment: Optional[str] = None
    journal_cursor_segment: Optional[str] = None
    journal_heal_incidents: Optional[list] = None


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
    # Issue #119: the symlink state grew a fourth value, `stale` — a
    # retired/foreign (e.g. Homebrew keg) link whose command IS still
    # reachable elsewhere, so the link is safely-cleanable cruft, not a
    # broken slot. Count `available = ok + stale` (both ⟹ reachable);
    # `bad = wrong + missing` is what is genuinely actionable.
    if s.symlink_state is None:
        return CheckResult(
            id="install.symlinks", title="Symlinks",
            severity="fail", summary="state unavailable",
            remediation="See logs", details={"reason": "gather returned None"},
        )
    total = len(s.symlink_state)
    stale = [n for n, st in s.symlink_state if st == "stale"]
    bad = [n for n, st in s.symlink_state if st in ("wrong", "missing")]
    available = total - len(bad)            # available = ok + stale
    # "missing" carries the full `bad` list (wrong + missing); the key name is
    # kept for JSON-schema stability even though it now spans both states.
    details = {"present": available, "total": total,
               "missing": bad, "stale": stale}
    if not bad and not stale:
        return CheckResult(
            id="install.symlinks", title="Symlinks",
            severity="ok", summary=f"{available}/{total} available",
            remediation=None, details=details,
        )
    if not bad:   # stale only
        return CheckResult(
            id="install.symlinks", title="Symlinks",
            severity="warn",
            summary=f"{available}/{total} available; {len(stale)} stale link(s) to clean",
            remediation="Run `cctally setup` to clean stale links",
            details=details,
        )
    # bad present
    if s.symlinks_path_pinned:
        # Pinned-only-path (finding #2/#10): cctally runs ONLY through a
        # legacy ~/.local/bin link to a keg, so its slot classes `wrong`
        # but the command works. `cctally setup` deliberately won't remove
        # the only reachable copy — the actionable fix is a PATH change.
        # Keep this message in sync with the pinned guidance in _setup_install
        # (bin/_cctally_setup.py).
        remediation = (
            "cctally is reachable only through a legacy ~/.local/bin link to a "
            "Homebrew keg. Put <prefix>/bin on your PATH (e.g. `eval \"$(brew shellenv)\"`), "
            "then run `cctally setup` to remove the legacy link."
        )
    else:
        remediation = "Run `cctally setup`"
    summary = f"{available}/{total} available; missing/broken {', '.join(bad)}"
    if stale:
        summary += f"; {len(stale)} stale"
    return CheckResult(
        id="install.symlinks", title="Symlinks",
        severity="warn", summary=summary, remediation=remediation, details=details,
    )


def _check_install_path(s: DoctorState) -> CheckResult:
    # Issue #119: availability-aware. OK iff cctally is ACTUALLY reachable
    # on $PATH via ANY channel — brew `<prefix>/bin`, npm prefix, or source
    # `~/.local/bin` (`shutil.which`, precomputed in the I/O layer). Mere
    # `~/.local/bin` membership is NOT sufficient: doctor can be launched by
    # absolute path or from another UI with `~/.local/bin` on $PATH yet no
    # `cctally` installed there (the brew-only #119 case), which must WARN.
    # `path_includes_local_bin` is only a fail-soft fallback for when the
    # reachability probe could not run (None), so a gather failure never
    # hard-WARNs an otherwise-working install.
    reachable = s.cctally_reachable_on_path
    if reachable is None:
        reachable = bool(s.path_includes_local_bin)
    if reachable:
        return CheckResult(
            id="install.path", title="PATH",
            severity="ok", summary="cctally reachable on $PATH",
            remediation=None, details={},
        )
    # Channel-aware remediation: a Homebrew keg keeps cctally on
    # `<prefix>/bin` and deliberately owns no `~/.local/bin` symlinks
    # (#119), so the `~/.local/bin` / `cctally setup` hint would be wrong
    # for it — point brew users at `brew shellenv` instead (matching the
    # pinned-only-path remediation in `_check_install_symlinks`). Source /
    # npm installs keep the `~/.local/bin` + `cctally setup` guidance.
    if s.install_is_brew:
        remediation = (
            "Put `<prefix>/bin` on your PATH (e.g. `eval \"$(brew shellenv)\"`)"
        )
    else:
        remediation = (
            "Append `export PATH=\"$HOME/.local/bin:$PATH\"` to your shell rc, "
            "or run `cctally setup`"
        )
    return CheckResult(
        id="install.path", title="PATH",
        severity="warn", summary="cctally not reachable on $PATH",
        remediation=remediation,
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


def _check_install_dev_mode(s: DoctorState) -> CheckResult:
    """Always-present, always-ok: reports the resolved data dir and whether
    this process is a dev-checkout or the installed binary.
    Dev-instance isolation (§4, P3).

    Three states, not two — `dev_mode` alone collapses the override case:
      - dev_mode                       → auto-detected checkout (cctally-dev)
      - is_dev_checkout, not dev_mode  → checkout + CCTALLY_DATA_DIR override
      - neither                        → installed (prod)
    Reporting the override case as "installed" was misleading exactly when a
    user runs the per-branch hatch and wants to confirm which instance they
    are on (the binary IS a checkout; setup still refuses it as one)."""
    if s.dev_mode:
        summary = "DEV (auto-detected git checkout)"
    elif s.is_dev_checkout:
        summary = "DEV (git checkout, custom data dir via CCTALLY_DATA_DIR)"
    else:
        summary = "installed"
    # Preview channel (CCTALLY_CHANNEL=preview): note the channel in the
    # summary. Gated → prod (channel="prod") summary is unchanged.
    if s.channel == "preview":
        summary += " · channel: preview"
    return CheckResult(
        id="install.mode", title="Mode",
        severity="ok", summary=summary, remediation=None,
        details={
            "dev_mode": s.dev_mode,
            "is_dev_checkout": s.is_dev_checkout,
            "app_dir": s.app_dir,
            "channel": s.channel,
        },
    )


def _check_install_update_channel(s: DoctorState) -> CheckResult:
    """Report the configured update (release) channel alongside the install
    method (beta-channel, spec 2026-07-21 §3).

    DISTINCT from the preview `channel` in install.mode: this reports the
    `update.channel` config leaf (stable|beta) that `cctally update` tracks.
    WARN on the brew+beta mismatch — Homebrew IS the stable channel (Q2), so
    a beta opt-in on brew silently resolves stable; the WARN makes that
    explicit and actionable. Otherwise always OK (a diagnostic surface, never
    a hard failure — doctor exits 2 only on FAIL)."""
    channel = s.update_channel or "stable"
    if channel == "beta" and s.install_is_brew:
        return CheckResult(
            id="install.update_channel", title="Update channel",
            severity="warn",
            summary="beta (unavailable on Homebrew — installs track stable)",
            remediation=(
                "Homebrew tracks stable only. Install via npm or source to "
                "receive beta releases, or run "
                "`cctally config set update.channel stable`."
            ),
            details={"channel": channel, "method": "brew"},
        )
    return CheckResult(
        id="install.update_channel", title="Update channel",
        severity="ok", summary=channel, remediation=None,
        details={"channel": channel},
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


_STATUSLINE_REFRESH_SUMMARIES = {
    "present": "set",
    "missing": "not set on a cctally statusLine",
    "absent": "no statusLine configured",
    "foreign": "n/a (custom statusLine command)",
    "unavailable": "settings.json unreadable",
}


def _check_statusline_refresh_interval(s: DoctorState) -> CheckResult:
    """WARN only when a recognized cctally ``statusLine`` command lacks a
    ``refreshInterval`` (#311): without it, statusline-fed usage persistence
    goes quiet while a parent session waits on a long subagent. Every other
    state is OK with its own STABLE summary — the not-applicable states
    (absent/foreign) say so, and `unavailable` says settings were unreadable
    (the hooks.installed / settings warnings already surface that failure, so
    no double-WARN). The kernel reads only the precomputed scalar; the setup
    classifier's I/O happens in doctor_gather_state."""
    state = s.statusline_refresh_state
    summary = _STATUSLINE_REFRESH_SUMMARIES.get(state, _STATUSLINE_REFRESH_SUMMARIES["unavailable"])
    if state == "missing":
        return CheckResult(
            id="hooks.statusline_refresh_interval",
            title="statusLine refreshInterval", severity="warn",
            summary=summary,
            remediation=(
                "Run `cctally setup` to add statusLine.refreshInterval, or set "
                "it manually — see docs/commands/statusline.md"
            ),
            details={"state": state},
        )
    return CheckResult(
        id="hooks.statusline_refresh_interval",
        title="statusLine refreshInterval", severity="ok",
        summary=summary, remediation=None, details={"state": state},
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


def _check_db_version_ahead(s: DoctorState) -> CheckResult:
    """Classify each DB's ``user_version`` versus what this binary expects.

    stats.db follows the EPOCH model (DB journal redesign §7.1): it is a
    DISPOSABLE index stamped at ``STATS_INDEX_EPOCH`` (injected as ``epoch``
    into ``stats_db_status`` by the gather layer), NOT a versioned migration
    target. Classification:
      * ``uv == epoch`` (a cut-over install)      → HEALTHY (steady state)
      * ``uv <= legacy_head`` (pre-cutover, ≤13)  → HEALTHY (cuts over on open)
      * ``uv > legacy_head`` AND ``!= epoch``      → §7.1 index MISMATCH: WARN.
        It self-heals by journal REBUILD on the next open (never bricks, unlike
        the retired #145 version-ahead FAIL), so the remediation points at
        `db rebuild --db stats`, NOT the retired `db recover --db stats`.

    cache.db is unchanged (issue #145): a ``user_version`` past the cache
    registry head auto-heals on the next open → WARN. doctor reads raw
    ``user_version`` (no dispatcher), so it reports without healing/bricking.
    """
    def _eval_stats(status):
        if not status:
            return None
        uv = status.get("user_version", 0) or 0
        legacy_head = status.get("registry_size", 0) or 0  # frozen stats head (13)
        epoch = status.get("epoch")
        if epoch is None:
            # Fallback kept in lockstep with _cctally_core.STATS_INDEX_EPOCH; the
            # gather layer injects the real constant, so this only guards a hand-
            # built DoctorState that omitted it.
            epoch = 1000
        mismatch = uv > legacy_head and uv != epoch
        return {"user_version": uv, "legacy_head": legacy_head, "epoch": epoch,
                "mismatch": mismatch}

    def _eval_cache(status):
        if not status:
            return None
        uv = status.get("user_version", 0) or 0
        rs = status.get("registry_size", 0) or 0
        return {"user_version": uv, "registry_size": rs, "ahead": uv > rs}

    stats = _eval_stats(s.stats_db_status)
    cache = _eval_cache(s.cache_db_status)
    details = {"stats.db": stats, "cache.db": cache}
    stats_mismatch = bool(stats and stats["mismatch"])
    cache_ahead = bool(cache and cache["ahead"])

    if stats_mismatch:
        return CheckResult(
            id="db.version_ahead", title="Version ahead", severity="warn",
            summary=(f"stats.db index mismatch (v{stats['user_version']} ≠ epoch "
                     f"v{stats['epoch']}) — rebuilds from journal"),
            remediation=("Auto-heals by journal rebuild on next command; if it "
                         "persists, run `cctally db rebuild --db stats`"),
            details=details,
        )
    if cache_ahead:
        return CheckResult(
            id="db.version_ahead", title="Version ahead", severity="warn",
            summary=f"cache.db ahead (v{cache['user_version']} > known v{cache['registry_size']}) — auto-heals",
            remediation="Auto-heals on next command, or run `cctally db recover --db cache`",
            details=details,
        )
    return CheckResult(
        id="db.version_ahead", title="Version ahead", severity="ok",
        summary="none ahead", remediation=None, details=details,
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


def _check_statusline_pipeline(s: DoctorState) -> CheckResult:
    """Report transport, selected, control, and authority health separately."""
    pipeline = s.statusline_pipeline if isinstance(s.statusline_pipeline, dict) else {}
    tombstones = pipeline.get("tombstones")
    tombstones = tombstones if isinstance(tombstones, dict) else {}
    details = {
        "transport_age_seconds": pipeline.get("transport_age_seconds"),
        "selected_age_seconds": pipeline.get("selected_age_seconds"),
        "active_candidate_count": pipeline.get("active_candidate_count", 0),
        "control_db_agrees": pipeline.get("control_db_agrees"),
        "tombstones": tombstones,
    }
    if any(value in {"invalid", "inflight"} for value in tombstones.values()):
        return CheckResult(
            id="data.statusline_pipeline", title="Statusline pipeline",
            severity="warn", summary="authoritative state needs repair",
            remediation="Run `cctally refresh-usage`", details=details,
        )
    if pipeline.get("control_db_agrees") is False:
        return CheckResult(
            id="data.statusline_pipeline", title="Statusline pipeline",
            severity="warn", summary="selected control disagrees with database",
            remediation="Run `cctally refresh-usage`, then inspect active sessions",
            details=details,
        )
    transport_age = pipeline.get("transport_age_seconds")
    selected_age = pipeline.get("selected_age_seconds")
    transport_age = (
        float(transport_age)
        if isinstance(transport_age, (int, float)) and not isinstance(transport_age, bool)
        else math.inf
    )
    selected_age = (
        float(selected_age)
        if isinstance(selected_age, (int, float)) and not isinstance(selected_age, bool)
        else math.inf
    )
    if transport_age < 90 and selected_age >= 300:
        return CheckResult(
            id="data.statusline_pipeline", title="Statusline pipeline",
            severity="warn", summary="timer active; selected usage stale",
            remediation="Run `cctally refresh-usage`, then inspect active sessions",
            details=details,
        )
    if transport_age >= 90:
        summary = "no recent regular-pool timer observed"
    elif selected_age < 300:
        summary = "timer and selected usage fresh"
    else:
        summary = "selected usage awaiting next active timer"
    return CheckResult(
        id="data.statusline_pipeline", title="Statusline pipeline",
        severity="ok", summary=summary, remediation=None, details=details,
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
            severity="ok", summary="none (no Codex session JSONL found)",
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


def _check_data_codex_project_metadata(s: DoctorState) -> CheckResult:
    """Report the identity-safe all-history Codex metadata partition."""
    if s.codex_project_metadata_error is not None:
        return CheckResult(
            id="data.codex_project_metadata", title="Codex project metadata",
            severity="fail", summary="metadata health could not be read",
            remediation="Run `cctally doctor --verbose` after checking cache.db.",
            details={"error": s.codex_project_metadata_error},
        )

    health = s.codex_project_metadata_health or {}
    total_rows = int(health.get("total_rows", 0))
    qualified_rows = int(health.get("qualified_rows", 0))
    missing_key_rows = int(health.get("missing_conversation_key_rows", 0))
    missing_join_rows = int(health.get("missing_thread_join_rows", 0))
    incomplete_rows = missing_key_rows + missing_join_rows
    details = {
        "total_rows": total_rows,
        "qualified_rows": qualified_rows,
        "missing_conversation_key_rows": missing_key_rows,
        "missing_thread_join_rows": missing_join_rows,
        "incomplete_rows": incomplete_rows,
    }
    if incomplete_rows:
        return CheckResult(
            id="data.codex_project_metadata", title="Codex project metadata",
            severity="warn",
            summary=f"{incomplete_rows} accounting row(s) need metadata repair",
            remediation="Run `cctally cache-sync --source codex --rebuild`.",
            details=details,
        )
    return CheckResult(
        id="data.codex_project_metadata", title="Codex project metadata",
        severity="ok", summary="qualified", remediation=None, details=details,
    )


def _check_data_codex_quota(s: DoctorState) -> CheckResult:
    """Report local physical Codex quota evidence without fabricating a window."""
    raw_windows = s.codex_quota_windows or []
    if not raw_windows:
        has_unsafe_corpus = bool(s.codex_jsonl_present)
        return CheckResult(
            id="data.codex_quota", title="Codex quota",
            severity="warn" if has_unsafe_corpus else "ok",
            summary=("no safely interpreted windows" if has_unsafe_corpus
                     else "not applicable"),
            remediation=("Run `cctally cache-sync --source codex --rebuild`"
                         if has_unsafe_corpus else None),
            details={
                "window_count": 0,
                "latest_capture_at": None,
                "freshness_state": "unavailable",
                "age_seconds": None,
                "stale_after_seconds": None,
                "responsible_identity": None,
                "windows": [],
            },
        )

    def identity_key(row: dict) -> tuple[str, str, str, str, int]:
        identity = row.get("identity") or {}
        return (
            str(identity.get("source") or ""),
            str(identity.get("source_root_key") or ""),
            str(identity.get("logical_limit_key") or ""),
            str(identity.get("observed_slot") or ""),
            int(identity.get("window_minutes") or 0),
        )

    freshness_order = {"future": 0, "stale": 1, "unavailable": 2, "fresh": 3}
    windows: list[dict] = []
    for row in sorted(raw_windows, key=identity_key):
        captured_at = row.get("latest_capture_at")
        windows.append({
            "identity": row.get("identity"),
            "latest_capture_at": (_iso_z(captured_at)
                                  if isinstance(captured_at, dt.datetime)
                                  else captured_at),
            "freshness_state": row.get("freshness_state") or "unavailable",
            "age_seconds": row.get("age_seconds"),
            "stale_after_seconds": row.get("stale_after_seconds"),
        })
    worst_rank = min(freshness_order.get(row["freshness_state"], 2) for row in windows)
    responsible = next(
        row for row in windows
        if freshness_order.get(row["freshness_state"], 2) == worst_rank
    )
    captures = [
        row.get("latest_capture_at") for row in raw_windows
        if isinstance(row.get("latest_capture_at"), dt.datetime)
    ]
    latest_capture_at = _iso_z(max(captures)) if captures else None
    state = responsible["freshness_state"]
    return CheckResult(
        id="data.codex_quota", title="Codex quota",
        severity="ok" if state == "fresh" else "warn",
        summary=f"{len(windows)} window(s); {state}",
        remediation=(None if state == "fresh"
                     else "Run `cctally cache-sync --source codex` after Codex activity."),
        details={
            "window_count": len(windows),
            "latest_capture_at": latest_capture_at,
            "freshness_state": state,
            "age_seconds": responsible["age_seconds"],
            "stale_after_seconds": responsible["stale_after_seconds"],
            "responsible_identity": responsible["identity"],
            "windows": windows,
        },
    )


def _check_hooks_codex_installed(s: DoctorState) -> CheckResult:
    """Summarize every configured Codex root without masking a bad sibling."""
    rows = sorted(
        (row for row in (s.codex_hook_roots or []) if isinstance(row, dict)),
        key=lambda row: str(row.get("source_root_key") or ""),
    )
    states = [
        {"source_root_key": row.get("source_root_key"), "state": row.get("state")}
        for row in rows
    ]
    installed_states = {
        "installed_review_required", "installed_trust_unobservable",
    }
    installed = [row for row in rows if row.get("state") in installed_states]
    requires_review = (
        True if any(row.get("state") == "installed_review_required" for row in rows)
        else None if installed
        else False
    )
    trust_state = (
        "not-applicable" if not rows
        else "review-required" if requires_review is True
        else "unobservable" if installed
        else "not-installed"
    )
    unhealthy = any(row.get("state") not in installed_states for row in rows)
    return CheckResult(
        id="hooks.codex_installed", title="Codex hooks installed",
        severity="warn" if unhealthy else "ok",
        summary=("not applicable" if not rows
                 else f"{len(installed)}/{len(rows)} root(s) installed"),
        remediation=("Run `cctally setup`, then review the handler in Codex /hooks."
                     if unhealthy else None),
        details={
            "root_count": len(rows),
            "installed_root_count": len(installed),
            "states": states,
            "requires_review": requires_review,
            "trust_state": trust_state,
        },
    )


def _check_hooks_codex_recent_activity(s: DoctorState) -> CheckResult:
    """Aggregate 24-hour lifecycle records only for installed root handlers."""
    installed_states = {
        "installed_review_required", "installed_trust_unobservable",
    }
    installed_keys = sorted(
        str(row.get("source_root_key"))
        for row in (s.codex_hook_roots or [])
        if isinstance(row, dict) and row.get("state") in installed_states
        and row.get("source_root_key")
    )
    if not installed_keys:
        return CheckResult(
            id="hooks.codex_recent_activity", title="Codex recent activity",
            severity="ok", summary="not applicable",
            remediation=None,
            details={
                "activity_state": "not-applicable",
                "last_tick_at": None,
                "age_seconds": None,
                "success_count_24h": 0,
                "error_count_24h": 0,
                "responsible_root_key": None,
                "roots": [],
            },
        )

    activity = s.codex_lifecycle_activity_24h or {}
    roots: list[dict] = []
    for key in installed_keys:
        row = activity.get(key) if isinstance(activity, dict) else None
        row = row if isinstance(row, dict) else {}
        last_tick_at = row.get("last_tick_at")
        if isinstance(last_tick_at, dt.datetime):
            age_seconds = max(0, int((s.now_utc - last_tick_at).total_seconds()))
            tick_wire = _iso_z(last_tick_at)
        else:
            age_seconds = None
            tick_wire = None
        if tick_wire is None:
            state = "never"
        elif age_seconds is not None and age_seconds > 24 * 3600:
            state = "stale"
        else:
            state = "recent"
        roots.append({
            "source_root_key": key,
            "activity_state": state,
            "last_tick_at": tick_wire,
            "age_seconds": age_seconds,
            "success_count_24h": int(row.get("success_count_24h") or 0),
            "error_count_24h": int(row.get("error_count_24h") or 0),
        })
    worst_order = {"never": 0, "stale": 1, "recent": 2}
    worst_rank = min(worst_order[row["activity_state"]] for row in roots)
    responsible = next(
        row for row in roots if worst_order[row["activity_state"]] == worst_rank
    )
    return CheckResult(
        id="hooks.codex_recent_activity", title="Codex recent activity",
        severity="ok" if responsible["activity_state"] == "recent" else "warn",
        summary=f"{len(roots)} installed root(s); {responsible['activity_state']}",
        remediation=(None if responsible["activity_state"] == "recent"
                     else "Trigger Codex activity, then verify `cctally setup` hooks."),
        details={
            "activity_state": responsible["activity_state"],
            "last_tick_at": responsible["last_tick_at"],
            "age_seconds": responsible["age_seconds"],
            "success_count_24h": responsible["success_count_24h"],
            "error_count_24h": responsible["error_count_24h"],
            "responsible_root_key": responsible["source_root_key"],
            "roots": roots,
        },
    )


def _check_data_forked_buckets(s: DoctorState) -> CheckResult:
    """Invariant: for every row with ``week_start_at IS NOT NULL``,
    ``week_start_date == substr(week_start_at, 1, 10)``.

    Pair with migration ``004_heal_forked_week_start_date_buckets``,
    which auto-merges any detected rows on the next ``open_db()``. A
    non-zero count here means either (a) the migration is gated as
    skipped/failed/pending or (b) a buggy writer slipped through
    after the migration ran. Either way the user has a fork that
    needs attention.
    """
    counts = s.forked_bucket_counts
    if counts is None:
        return CheckResult(
            id="data.forked_buckets", title="Forked week buckets",
            severity="fail", summary="state unavailable",
            remediation="Check stats.db opens (`cctally db status`)",
            details={"reason": "gather returned None"},
        )
    total = sum(int(counts.get(k, 0)) for k in ("usage", "cost", "milestones"))
    if total == 0:
        return CheckResult(
            id="data.forked_buckets", title="Forked week buckets",
            severity="ok", summary="none",
            remediation=None,
            details=dict(counts),
        )
    parts = [
        f"{counts.get(k, 0)} {k}"
        for k in ("usage", "cost", "milestones")
        if counts.get(k, 0)
    ]
    return CheckResult(
        id="data.forked_buckets", title="Forked week buckets",
        severity="fail",
        summary=f"{total} forked row(s): {', '.join(parts)}",
        remediation=(
            "Run any cctally command to trigger the auto-heal migration "
            "(`004_heal_forked_week_start_date_buckets`); if it's already "
            "applied, see `cctally db status`."
        ),
        details=dict(counts),
    )



def _check_data_post_credit_milestones(s: DoctorState) -> CheckResult:
    """Invariant: for every week with a ``week_reset_events`` row whose
    ``effective_reset_at_utc`` is in the past AND latest_weekly_percent
    >= 1.0, the percent_milestones ledger should have at least one row
    in the credit's segment.

    Pre-v1.7.2 the milestone writer didn't know about segments, so a
    credited week could have a non-empty pre-credit ledger but zero
    post-credit rows even after the user's usage climbed past 1%. This
    check surfaces that drift as a WARN (informational; no remediation
    — the next ``record-usage`` tick at >=1% will self-heal via the
    segment-aware probe).

    OK (silent) when:
      * No credited weeks exist (state.credited_weeks is empty/None).
      * Every credited week has at least one post-credit milestone row
        OR latest_weekly_percent < 1.0 (= "new segment not started yet,
        which is expected on a fresh credit").
    """
    weeks = s.credited_weeks
    if weeks is None:
        # Gather failed (stats.db open error). Don't double-warn; the
        # db.stats.file check already covers DB-open issues.
        return CheckResult(
            id="data.post_credit_milestones",
            title="Post-credit milestones",
            severity="ok",
            summary="no data",
            remediation=None,
            details={"reason": "credited_weeks gather returned None"},
        )
    stuck = [
        w for w in weeks
        if float(w.get("latest_weekly_percent") or 0.0) >= 1.0
        and int(w.get("post_credit_milestone_count") or 0) == 0
    ]
    if not stuck:
        return CheckResult(
            id="data.post_credit_milestones",
            title="Post-credit milestones",
            severity="ok",
            summary=(
                f"{len(weeks)} credited week(s); all tracked"
                if weeks else "no credited weeks"
            ),
            remediation=None,
            details={"credited_weeks": len(weeks)},
        )
    starts = ", ".join(sorted(w["week_start_date"] for w in stuck))
    return CheckResult(
        id="data.post_credit_milestones",
        title="Post-credit milestones",
        severity="warn",
        summary=(
            f"{len(stuck)} credited week(s) with no post-credit milestone "
            f"crossings yet: {starts}"
        ),
        remediation=None,
        details={
            "stuck_week_count": len(stuck),
            "stuck_week_starts": [w["week_start_date"] for w in stuck],
        },
    )


def _check_data_conversation_sessions_rollup(s: DoctorState) -> CheckResult:
    """Invariant: the browse-rail rollup ``conversation_sessions`` carries one
    row per distinct ``conversation_messages.session_id`` (#217 S1 / U9).

    OK when the two counts are equal, when either is ``None`` (pre-rollup or an
    unreadable conversations.db — consistent with the kernel's graceful-degrade
    posture),
    OR when a sync/reingest/backfill is in progress. WARN ONLY on a mismatch
    observed in a QUIESCENT cache.

    False-WARN avoidance (Codex P2): ``sync_claude_conversations`` commits
    ``conversation_messages`` per file *before* the ``conversation_sessions``
    recompute, and resumable reingest commits per file before its rollup
    completes — so an unsynchronized read can transiently mismatch. The
    in-progress signal (``conv_rollup_sync_in_progress``) is set by
    ``doctor_gather_state`` from a NON-BLOCKING ``conversations.db.lock`` flock probe
    (lock held ⇒ a writer is mid-flight) AND the presence of any pending
    ``cache_meta`` reingest/split/backfill flag; if either says in-progress, this
    stays OK. Doctor remains read-only and never blocks on the lock.

    Informational only (no remediation): the next conversation sync re-derives
    the rollup via its incremental DELETE+INSERT.
    """
    rollup = s.conv_sessions_rollup_count
    distinct = s.conv_messages_distinct_sessions
    if rollup is None or distinct is None:
        return CheckResult(
            id="data.conversation_sessions_rollup",
            title="Conversation rollup",
            severity="ok",
            summary="no data",
            remediation=None,
            details={"rollup_count": rollup,
                     "messages_distinct_sessions": distinct},
        )
    if s.conv_rollup_sync_in_progress or rollup == distinct:
        return CheckResult(
            id="data.conversation_sessions_rollup",
            title="Conversation rollup",
            severity="ok",
            summary=(
                "sync in progress"
                if (s.conv_rollup_sync_in_progress and rollup != distinct)
                else f"{rollup} session(s) tracked"
            ),
            remediation=None,
            details={"rollup_count": rollup,
                     "messages_distinct_sessions": distinct,
                     "sync_in_progress": s.conv_rollup_sync_in_progress},
        )
    return CheckResult(
        id="data.conversation_sessions_rollup",
        title="Conversation rollup",
        severity="warn",
        summary=(
            f"rollup has {rollup} session(s); messages span {distinct} "
            f"(quiescent mismatch)"
        ),
        remediation=(
            "Run any cctally command (or `cctally cache-sync --rebuild`) to "
            "re-derive the conversation_sessions rollup."
        ),
        details={"rollup_count": rollup,
                 "messages_distinct_sessions": distinct,
                 "sync_in_progress": False},
    )


def _check_pricing_coverage(s: DoctorState) -> CheckResult:
    """WARN when recent (30-day) session data contains a model cctally cannot
    price exactly (spec §5.1).

    Two gap kinds (classified upstream in `_lib_pricing_check.classify_coverage`,
    populated by `doctor_gather_state`):
      * ``unpriced`` — a Claude model `_resolve_model_pricing` returns None for;
        it silently contributes $0 (the serious undercount failure mode).
      * ``fallback`` — a Codex model approximated via `gpt-5` pricing.

    ``s.pricing_coverage is None`` means the cache could not be read (or the
    classification raised) → OK ("no cached usage to assess"), matching the
    rest of the kernel's degradation posture. An empty list → OK. Any gap →
    WARN (a data-quality signal, deliberately NOT FAIL — doctor FAIL exits 2;
    consistent with the other WARN-family Data checks).

    ``details`` is a structured dict (sibling-check convention): two lists of
    ``{model, entry_count, token_total}`` keyed by gap kind, so a `--json`
    consumer can machine-read each gap. The human summary + remediation point
    at `cctally pricing-check` and the pricing tables.
    """
    gaps = s.pricing_coverage
    if not gaps:
        return CheckResult(
            id="pricing.coverage", title="Coverage",
            severity="ok",
            summary="all observed models priced",
            remediation=None,
            details={"unpriced": [], "fallback": []},
        )

    def _row(g) -> dict:
        return {
            "model": g.model,
            "entry_count": g.entry_count,
            "token_total": g.token_total,
        }

    unpriced = [_row(g) for g in gaps if g.kind == "unpriced"]
    fallback = [_row(g) for g in gaps if g.kind == "fallback"]

    parts: list[str] = []
    if unpriced:
        parts.append(f"{len(unpriced)} unpriced (Claude $0)")
    if fallback:
        parts.append(f"{len(fallback)} fallback (Codex gpt-5)")
    # Defensive: a gap whose kind is neither (shouldn't happen) still WARNs.
    summary = "; ".join(parts) if parts else f"{len(gaps)} coverage gaps"

    return CheckResult(
        id="pricing.coverage", title="Coverage",
        severity="warn",
        summary=summary,
        remediation=(
            "Run `cctally pricing-check`, then update CLAUDE_MODEL_PRICING / "
            "CODEX_MODEL_PRICING in bin/_lib_pricing.py"
        ),
        details={"unpriced": unpriced, "fallback": fallback},
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
    # Conversation viewer (Plan 2, spec §5): a LAN bind WITH the
    # `dashboard.expose_transcripts` opt-in serves raw conversation prose to
    # the LAN. Surface that ONLY here (the bind already WARNs and is
    # non-loopback by construction), additively — a loopback bind never
    # reaches this branch, so the loopback report stays byte-identical
    # regardless of the expose flag.
    extra = {}
    if s.expose_transcripts:
        notes.append("transcripts exposed on LAN")
        extra["transcripts_exposed_on_lan"] = True
    return CheckResult(
        id="safety.dashboard_bind", title="Dashboard bind",
        severity="warn", summary="; ".join(notes),
        remediation=rem,
        details={"config": s.dashboard_bind_stored,
                 "runtime_bind": s.runtime_bind,
                 **({"note": note} if note else {}),
                 **extra},
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
        # The producer (bin/cctally `_do_update_remind_later`) writes
        # `remind_after` as a dict `{"version", "until_utc"}`; the
        # banner predicate consumes that shape. Accept it here so a
        # legitimate deferral doesn't render as "bad types: remind_after".
        # `None` (default record) and the legacy scalar form (older
        # binaries persisted a bare until-string) both stay valid.
        if v is not None and not isinstance(v, (str, int, float, dict)):
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
    # `effective_update_available` is precomputed by the I/O layer via
    # the same predicate the update banner uses (semver + skipped +
    # remind_after). If the user has skipped or deferred a newer
    # version, the banner stays silent — doctor must do the same.
    if not s.effective_update_available:
        details = {"current_version": cur, "latest_version": lat}
        reason = s.effective_update_reason
        # Surface the suppression reason only when it matters — i.e.
        # there *is* a newer version, but the user has opted out.
        # Preserves the byte-stable details shape for the common case
        # (no probe yet / no newer version) while informing verbose
        # readers when a real update is being held back.
        if reason in ("skipped", "reminded"):
            details["suppressed"] = True
            details["suppression_reason"] = reason
        return CheckResult(
            id="safety.update_available", title="Update available",
            severity="ok", summary="no",
            remediation=None,
            details=details,
        )
    return CheckResult(
        id="safety.update_available", title="Update available",
        severity="warn",
        summary=f"v{lat} (you are on v{cur})",
        remediation="Run `cctally update`",
        details={"current_version": cur, "latest_version": lat},
    )


def _check_telemetry(s: DoctorState) -> CheckResult:
    """Anonymous install-count telemetry opt-out state (spec 2026-07-07).

    A read-only DIAGNOSTIC surface — "is telemetry on, and if off, why" — that
    is ALWAYS ``ok``, never WARN/FAIL. Being disabled (env kill switch,
    DO_NOT_TRACK, a dev checkout, or ``telemetry.enabled = false``) is a valid
    user choice, not a health problem, so this check must never change doctor's
    severity counts or exit code. The gather layer resolves `enabled`/`reason`
    via `resolve_telemetry_state` WITHOUT minting an install_id.
    """
    enabled = bool(s.telemetry_enabled)
    reason = s.telemetry_reason
    return CheckResult(
        id="telemetry.state", title="State",
        severity="ok",
        summary=f"{'enabled' if enabled else 'disabled'} ({reason})",
        remediation=None,
        details={"enabled": enabled, "reason": reason},
    )


_PARSE_HEALTH_RECENCY_DAYS = 7


def _check_data_parse_health(s: DoctorState) -> CheckResult:
    details = {"claude": s.parse_health_claude, "codex": s.parse_health_codex}
    recent, historical_total = [], 0
    for label, ph in (("claude", s.parse_health_claude),
                      ("codex", s.parse_health_codex)):
        if not isinstance(ph, dict):
            continue
        malformed = int(ph.get("lines_malformed", 0) or 0)
        skipped = int(ph.get("lines_skipped", 0) or 0)
        historical_total += malformed + skipped
        last_anomaly = ph.get("last_anomaly_at")
        if not last_anomaly or (malformed + skipped) == 0:
            continue
        try:
            at = dt.datetime.fromisoformat(
                str(last_anomaly).replace("Z", "+00:00"))
            if at.tzinfo is None:
                at = at.replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue
        if (s.now_utc - at).total_seconds() <= \
                _PARSE_HEALTH_RECENCY_DAYS * 86400.0:
            reasons = ph.get("reasons") \
                if isinstance(ph.get("reasons"), dict) else {}
            top = max(reasons.items(), key=lambda kv: kv[1])[0] \
                if reasons else None
            recent.append((label, malformed, skipped, top))
    if recent:
        parts = []
        for label, malformed, skipped, top in recent:
            frag = f"{label}: {malformed} malformed / {skipped} drift-skipped"
            if top:
                frag += f" (top reason: {top})"
            parts.append(frag)
        return CheckResult(
            id="data.parse_health", title="Ingest parse health",
            severity="warn",
            summary="; ".join(parts)
                    + f" within {_PARSE_HEALTH_RECENCY_DAYS}d",
            remediation=(
                "JSONL lines are failing to parse — a Claude Code / Codex "
                "update may have changed the session format. Check for a "
                "cctally update or file an issue; `cctally cache-sync "
                "--rebuild` re-baselines the counters."),
            details=details,
        )
    if s.parse_health_claude is None and s.parse_health_codex is None:
        summary = "no parse-health data yet (pre-first-sync)"
    elif historical_total:
        summary = (f"no recent anomalies ({historical_total} historical; "
                   f"see details)")
    else:
        summary = "no parse anomalies"
    return CheckResult(
        id="data.parse_health", title="Ingest parse health",
        severity="ok", summary=summary, remediation=None, details=details,
    )


def _check_db_integrity(s: DoctorState) -> CheckResult:
    details = {"stats_quick_check": s.stats_db_quick_check,
               "cache_quick_check": s.cache_db_quick_check}
    if s.stats_db_quick_check is not None and s.stats_db_quick_check != "ok":
        return CheckResult(
            id="db.integrity", title="Integrity", severity="fail",
            summary=f"stats.db quick_check: {s.stats_db_quick_check}",
            remediation=(
                "stats.db (the non-re-derivable DB) reports corruption. "
                "Stop the dashboard and other cctally processes, then run "
                "`cctally db repair --db stats --yes`. The command preserves "
                "a backup of the corrupt original before replacing anything. "
                "Do not copy, restore, move, or delete the live DB by hand."),
            details=details,
        )
    if s.cache_db_quick_check is not None and s.cache_db_quick_check != "ok":
        return CheckResult(
            id="db.integrity", title="Integrity", severity="warn",
            summary=f"cache.db quick_check: {s.cache_db_quick_check}",
            remediation=("cache.db is re-derivable — run "
                         "`cctally cache-sync --rebuild`."),
            details=details,
        )
    if s.stats_db_quick_check is None and s.cache_db_quick_check is None:
        return CheckResult(
            id="db.integrity", title="Integrity", severity="ok",
            summary="not checked (fast gather — run `cctally doctor`)",
            remediation=None, details=details,
        )
    return CheckResult(
        id="db.integrity", title="Integrity", severity="ok",
        summary="quick_check ok", remediation=None, details=details,
    )


def _check_db_lock_state(s: DoctorState) -> CheckResult:
    locks = s.locks_held if isinstance(s.locks_held, dict) else {}
    held = sorted(k for k, v in locks.items() if v is True)
    details = {"locks": s.locks_held}
    if held:
        return CheckResult(
            id="db.lock_state", title="Locks", severity="ok",
            summary=(", ".join(held) + " held — an active sync or "
                     "dashboard is running; a hold persisting across "
                     "repeated doctor runs may indicate a wedged process"),
            remediation=None, details=details,
        )
    return CheckResult(
        id="db.lock_state", title="Locks", severity="ok",
        summary="free" if locks else "no lock files present",
        remediation=None, details=details,
    )


# #297: WARN when cache.db-wal exceeds 2x the Section 1 cap (128 MiB), so this
# only fires when the journal_size_limit + forced-checkpoint machinery has
# genuinely failed to contain the WAL — never in normal operation.
DOCTOR_WAL_WARN_BYTES = 256 * 1024 * 1024  # 268435456


def _check_db_wal_size(s: DoctorState) -> CheckResult:
    """Read-only backstop that makes a genuine cache.db WAL wedge visible and
    points at `cctally db checkpoint` (#297).

    The exact byte count lives ONLY in the (fingerprint-excluded) details block;
    the summary stays a stable string so a below-threshold byte count that
    drifts tick-to-tick does not flip the doctor fingerprint — only an OK<->WARN
    crossing does.
    """
    wal = s.cache_db_wal_bytes
    details = {"cache_db_wal_bytes": wal}
    if isinstance(wal, int) and wal > DOCTOR_WAL_WARN_BYTES:
        return CheckResult(
            id="db.wal_size", title="cache.db WAL size", severity="warn",
            summary="oversized — cache.db WAL far above its cap",
            remediation="Run `cctally db checkpoint` to drain the WAL.",
            details=details,
        )
    return CheckResult(
        id="db.wal_size", title="cache.db WAL size", severity="ok",
        summary="within limit", remediation=None, details=details,
    )


# #315: conservative advisory threshold. A quarter of cache.db being free is
# large enough to make an explicit, guarded VACUUM useful without nagging for
# ordinary page churn. This is a ratio, so it remains page-size independent.
DOCTOR_RECLAIMABLE_WARN_RATIO = 0.25


def _check_db_reclaimable(s: DoctorState) -> CheckResult:
    """Surface cache free pages without mutating or auto-vacuuming the DB."""
    page_count = s.cache_db_page_count
    freelist_count = s.cache_db_freelist_count
    ratio = None
    if (
        isinstance(page_count, int)
        and not isinstance(page_count, bool)
        and page_count > 0
        and isinstance(freelist_count, int)
        and not isinstance(freelist_count, bool)
        and 0 <= freelist_count <= page_count
    ):
        ratio = freelist_count / page_count
    details = {
        "cache_db_page_count": page_count,
        "cache_db_freelist_count": freelist_count,
        "cache_db_free_ratio": ratio,
        "warn_ratio": DOCTOR_RECLAIMABLE_WARN_RATIO,
    }
    if ratio is not None and ratio >= DOCTOR_RECLAIMABLE_WARN_RATIO:
        return CheckResult(
            id="db.reclaimable", title="Reclaimable cache space",
            severity="warn",
            summary=f"high — {ratio * 100:.1f}% of cache.db pages are free",
            remediation=(
                "Run `cctally db vacuum --db cache` to reclaim disk space."
            ),
            details=details,
        )
    return CheckResult(
        id="db.reclaimable", title="Reclaimable cache space", severity="ok",
        summary="below threshold", remediation=None, details=details,
    )


def _check_db_conversations_reclaimable(s: DoctorState) -> CheckResult:
    """Surface transcript-store free pages without mutating the DB (#320)."""
    page_count = s.conversations_db_page_count
    freelist_count = s.conversations_db_freelist_count
    ratio = None
    if (
        isinstance(page_count, int)
        and not isinstance(page_count, bool)
        and page_count > 0
        and isinstance(freelist_count, int)
        and not isinstance(freelist_count, bool)
        and 0 <= freelist_count <= page_count
    ):
        ratio = freelist_count / page_count
    details = {
        "conversations_db_page_count": page_count,
        "conversations_db_freelist_count": freelist_count,
        "conversations_db_free_ratio": ratio,
        "warn_ratio": DOCTOR_RECLAIMABLE_WARN_RATIO,
    }
    if ratio is not None and ratio >= DOCTOR_RECLAIMABLE_WARN_RATIO:
        return CheckResult(
            id="db.conversations_reclaimable",
            title="Reclaimable transcript space",
            severity="warn",
            summary=(
                f"high — {ratio * 100:.1f}% of conversations.db pages are free"
            ),
            remediation=(
                "Run `cctally db vacuum --db conversations` to reclaim disk space."
            ),
            details=details,
        )
    return CheckResult(
        id="db.conversations_reclaimable",
        title="Reclaimable transcript space",
        severity="ok",
        summary="below threshold",
        remediation=None,
        details=details,
    )


# ── DB journal redesign §9 — append-only journal legs ────────────────────
# A monthly segment is MB-scale (§4.5), so a multi-MB unconsumed cursor gap
# means no ingest cycle has run for a long stretch. An auto-heal incident within
# a week is worth surfacing loudly (the DB corrupted and self-healed).
_JOURNAL_CURSOR_LAG_WARN_BYTES = 4 * 1024 * 1024
_JOURNAL_HEAL_RECENT_SECONDS = 7 * 24 * 3600


def _check_journal_presence(s: DoctorState) -> CheckResult:
    """The journal directory exists and is appendable. A pre-cutover (legacy)
    install has NO journal yet — that is INFO (OK), never a FAIL."""
    if not s.journal_present:
        return CheckResult(
            id="journal.presence", title="Journal", severity="ok",
            summary="no journal (pre-cutover install)", remediation=None,
            details={"present": False},
        )
    details = {"present": True, "appendable": s.journal_appendable,
               "segments": s.journal_segment_count}
    if s.journal_appendable is False:
        return CheckResult(
            id="journal.presence", title="Journal", severity="warn",
            summary="journal directory not writable",
            remediation="Fix permissions on ~/.local/share/cctally/journal/",
            details=details,
        )
    return CheckResult(
        id="journal.presence", title="Journal", severity="ok",
        summary=f"{s.journal_segment_count} segment(s), writable",
        remediation=None, details=details,
    )


def _check_journal_integrity(s: DoctorState) -> CheckResult:
    """Mid-file malformed lines are external damage (WARN); a torn final line is
    a known crash artifact healed by the next append (INFO). The scan is
    deep-gated — a None count means "not scanned", always OK."""
    if not s.journal_present:
        return CheckResult(
            id="journal.integrity", title="Journal integrity", severity="ok",
            summary="no journal", remediation=None, details={"present": False},
        )
    if s.journal_malformed_count is None:
        return CheckResult(
            id="journal.integrity", title="Journal integrity", severity="ok",
            summary="not scanned", remediation=None, details={"scanned": False},
        )
    torn = s.journal_torn_tail_count or 0
    details = {"malformed": s.journal_malformed_count, "torn_tail": torn}
    if s.journal_malformed_count > 0:
        return CheckResult(
            id="journal.integrity", title="Journal integrity", severity="warn",
            summary=f"{s.journal_malformed_count} malformed line(s)",
            remediation=("External damage to a journal segment — inspect "
                         "~/.local/share/cctally/journal/ (every other line stays "
                         "parseable; the ingester skips + counts the bad ones)"),
            details=details,
        )
    if torn > 0:
        return CheckResult(
            id="journal.integrity", title="Journal integrity", severity="ok",
            summary=f"{torn} torn tail (heals on next append)",
            remediation=None, details=details,
        )
    return CheckResult(
        id="journal.integrity", title="Journal integrity", severity="ok",
        summary="no malformed lines", remediation=None, details=details,
    )


def _check_journal_index_freshness(s: DoctorState) -> CheckResult:
    """The stats index cursor vs. the journal high-water. A large unconsumed gap
    → WARN; a small gap or caught-up cursor → OK (with the gap shown)."""
    if not s.journal_present or s.journal_cursor_lag_bytes is None:
        return CheckResult(
            id="journal.index_freshness", title="Journal index", severity="ok",
            summary="no cursor yet", remediation=None,
            details={"lag_bytes": None},
        )
    lag = s.journal_cursor_lag_bytes
    details = {"lag_bytes": lag, "hw_segment": s.journal_hw_segment,
               "cursor_segment": s.journal_cursor_segment,
               "warn_bytes": _JOURNAL_CURSOR_LAG_WARN_BYTES}
    if lag == 0:
        return CheckResult(
            id="journal.index_freshness", title="Journal index", severity="ok",
            summary="index caught up", remediation=None, details=details,
        )
    if lag > _JOURNAL_CURSOR_LAG_WARN_BYTES:
        return CheckResult(
            id="journal.index_freshness", title="Journal index", severity="warn",
            summary=f"index {lag:,} bytes behind journal",
            remediation=("Run any cctally command (or `cctally db rebuild --db "
                         "stats`) — the ingester consumes the backlog"),
            details=details,
        )
    return CheckResult(
        id="journal.index_freshness", title="Journal index", severity="ok",
        summary=f"index {lag:,} bytes behind (within threshold)",
        remediation=None, details=details,
    )


def _check_journal_auto_heal(s: DoctorState) -> CheckResult:
    """The most recent auto-heal incident (quarantine dir + forensics bundle).
    INFO listing the latest; WARN when it fired within the last 7 days."""
    incidents = s.journal_heal_incidents
    if not incidents:
        return CheckResult(
            id="journal.auto_heal", title="Auto-heal", severity="ok",
            summary="no auto-heal incidents", remediation=None,
            details={"incidents": 0},
        )
    latest = incidents[0]
    age_s = latest.get("age_s")
    name = latest.get("name", "?")
    details = {"incidents": len(incidents), "latest": latest}
    if age_s is not None and age_s <= _JOURNAL_HEAL_RECENT_SECONDS:
        return CheckResult(
            id="journal.auto_heal", title="Auto-heal", severity="warn",
            summary=f"auto-heal fired recently ({name}, {age_s // 86400}d ago)",
            remediation=("A DB corrupted and self-healed — inspect the forensics "
                         "bundle in ~/.local/share/cctally/logs/"),
            details=details,
        )
    return CheckResult(
        id="journal.auto_heal", title="Auto-heal", severity="ok",
        summary=f"last incident {name}", remediation=None, details=details,
    )


# Each entry is (category_id, category_title, ((check_id, evaluator_fn_name), ...)).
# The dotted check_id is the stable JSON-contract ID (spec §5.2) AND the
# fingerprint identity-slice key (spec §5.5). When an evaluator raises,
# `_evaluate_one` uses this id — not the function name — so the synthesized
# FAIL CheckResult retains the contract id and fingerprint stays stable across
# success-vs-raise transitions.
_CATEGORY_DEFINITIONS: tuple[tuple[str, str, tuple[tuple[str, str], ...]], ...] = (
    ("install", "Install", (
        ("install.mode", "_check_install_dev_mode"),
        ("install.update_channel", "_check_install_update_channel"),
        ("install.symlinks", "_check_install_symlinks"),
        ("install.path", "_check_install_path"),
        ("install.legacy_snippet", "_check_install_legacy_snippet"),
        ("install.legacy_bespoke_hooks", "_check_install_legacy_bespoke"),
    )),
    ("hooks", "Hooks", (
        ("hooks.installed", "_check_hooks_installed"),
        ("hooks.statusline_refresh_interval", "_check_statusline_refresh_interval"),
        ("hooks.recent_activity_24h", "_check_hooks_recent_activity_24h"),
        ("hooks.last_fire_age", "_check_hooks_last_fire_age"),
        ("hooks.codex_installed", "_check_hooks_codex_installed"),
        ("hooks.codex_recent_activity", "_check_hooks_codex_recent_activity"),
    )),
    ("auth", "Auth", (
        ("oauth.token_present", "_check_oauth_token_present"),
    )),
    ("db", "Database", (
        ("db.stats.file", "_check_db_stats_file"),
        ("db.cache.file", "_check_db_cache_file"),
        ("db.integrity", "_check_db_integrity"),
        ("db.version_ahead", "_check_db_version_ahead"),
        ("db.migrations.applied", "_check_db_migrations_applied"),
        ("db.migrations.pending", "_check_db_migrations_pending"),
        ("db.lock_state", "_check_db_lock_state"),
        ("db.wal_size", "_check_db_wal_size"),
        ("db.reclaimable", "_check_db_reclaimable"),
        ("db.conversations_reclaimable", "_check_db_conversations_reclaimable"),
    )),
    ("journal", "Journal", (
        ("journal.presence", "_check_journal_presence"),
        ("journal.integrity", "_check_journal_integrity"),
        ("journal.index_freshness", "_check_journal_index_freshness"),
        ("journal.auto_heal", "_check_journal_auto_heal"),
    )),
    ("data", "Data", (
        ("data.latest_snapshot_age", "_check_data_latest_snapshot_age"),
        ("data.statusline_pipeline", "_check_statusline_pipeline"),
        ("data.cache_sync_state", "_check_data_cache_sync_state"),
        ("data.codex_cache", "_check_data_codex_cache"),
        ("data.codex_project_metadata", "_check_data_codex_project_metadata"),
        ("data.codex_quota", "_check_data_codex_quota"),
        ("data.parse_health", "_check_data_parse_health"),
        ("data.forked_buckets", "_check_data_forked_buckets"),
        ("data.post_credit_milestones", "_check_data_post_credit_milestones"),
        ("data.conversation_sessions_rollup",
         "_check_data_conversation_sessions_rollup"),
    )),
    ("pricing", "Pricing", (
        ("pricing.coverage", "_check_pricing_coverage"),
    )),
    ("safety", "Safety", (
        ("safety.dashboard_bind", "_check_safety_dashboard_bind"),
        ("safety.config_json_valid", "_check_safety_config_json_valid"),
        ("safety.update_state", "_check_safety_update_state"),
        ("safety.update_suppress", "_check_safety_update_suppress"),
        ("safety.update_available", "_check_safety_update_available"),
    )),
    ("telemetry", "Telemetry", (
        ("telemetry.state", "_check_telemetry"),
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
    """Render a UTC datetime as ISO 8601 with trailing 'Z' (share-v2 convention).

    DELIBERATELY DIVERGES from the canonical ``_lib_json_envelope._iso_z``
    (#279 S6 W4) on two axes and therefore KEEPS this local name (gate F11):
    (1) a NAIVE datetime is treated as UTC via ``replace(tzinfo=utc)`` — the
    canonical's ``astimezone(utc)`` would reinterpret it as host-local; and
    (2) microseconds are preserved via ``isoformat()`` — the canonical floors
    to whole seconds via ``%S``. Renaming this to the canonical would silently
    degrade the doctor block for the cross-module consumers that resolve it by
    name through the sibling loader (``_cctally_tui`` and
    ``_cctally_dashboard_envelope``'s doctor-envelope builders, whose
    try/except swallows a rename). Divergence pinned by
    ``tests/test_lib_doctor.py::test_doctor_iso_z_naive_means_utc_and_keeps_microseconds``.
    """
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
