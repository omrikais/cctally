"""`cctally doctor` subcommand entry point.

I/O gather sibling: holds `doctor_gather_state` (reads install / hooks /
OAuth / DB / freshness / pricing / safety state) + `cmd_doctor` (thin
wrapper over the pure `_lib_doctor` kernel).

Honest *name* imports are KERNEL-ONLY (`_cctally_core`). `_lib_changelog`
is a qualified, eagerly-preloaded library kernel (bin/cctally:419) used
for `_lib_changelog._read_latest_changelog_version()`. **`_lib_doctor` is
imported CALL-TIME inside the functions (F1)** — NOT module-top — to
preserve the live lazy-load and avoid an unconditional ~1,239-line import
on every startup. Every other sibling-homed symbol (the whole `_setup_*`
family, `_db_status_for`, the update/refresh/config/pricing helpers, the
`_pricing_observed_models` seam) is reached via the call-time `_cctally()`
accessor so monkeypatches through `cctally`'s namespace are preserved —
see spec §3.1.

bin/cctally re-exports `cmd_doctor` AND `doctor_gather_state` (eager): the
parser resolves `c.cmd_doctor`, and the dashboard + tests reach
`sys.modules["cctally"].doctor_gather_state` (patchable binding).

Spec: docs/superpowers/specs/2026-05-30-extract-diagnostics-cmd-design.md
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import pathlib
import shutil
import sqlite3
import sys

import _cctally_core
import _lib_changelog
from _cctally_core import _now_utc, eprint, now_utc_iso, parse_iso_datetime


def _cctally():
    """Resolve the current `cctally` module at call-time (spec §3.1)."""
    return sys.modules["cctally"]


def _gather_statusline_pipeline(c, *, now_utc: dt.datetime) -> dict:
    """Read the #318 statusline pipeline without creating or pruning files."""
    now_epoch = int(now_utc.timestamp())
    result = {
        "transport_age_seconds": None,
        "selected_age_seconds": None,
        "active_candidate_count": 0,
        "control_db_agrees": None,
        "tombstones": {"fiveHour": "absent", "sevenDay": "absent"},
    }
    try:
        result["transport_age_seconds"] = c._statusline_transport_age_seconds()
    except Exception:
        pass
    try:
        result["selected_age_seconds"] = c._statusline_observe_age_seconds()
    except Exception:
        pass
    try:
        result["active_candidate_count"] = len(
            c._scan_active_candidate_spool(now_epoch=now_epoch)
        )
    except Exception:
        pass
    try:
        result["control_db_agrees"] = c._statusline_control_db_agreement(
            now_epoch=now_epoch
        )
    except Exception:
        pass
    for axis, path in (
        ("fiveHour", _cctally_core.STATUSLINE_AUTHORITATIVE_5H_PATH),
        ("sevenDay", _cctally_core.STATUSLINE_AUTHORITATIVE_7D_PATH),
    ):
        try:
            if not path.exists():
                continue
            tombstone = c._read_tombstone(
                axis, now_epoch=now_epoch, fail_closed=False
            )
            result["tombstones"][axis] = (
                tombstone.state if tombstone is not None else "invalid"
            )
        except Exception:
            result["tombstones"][axis] = "invalid"
    return result


def _codex_lifecycle_activity_24h(
    *, root_keys: set[str], now_utc: dt.datetime,
) -> dict[str, dict]:
    """Read root-qualified Codex lifecycle outcomes from bounded local logs.

    The parser intentionally accepts only timestamped token records and retains
    aggregate lifecycle counters.  It never loads session, prompt, or response
    content into doctor state.
    """
    cutoff = now_utc - dt.timedelta(hours=24)
    records: dict[str, dict] = {}
    for path in (
        _cctally_core.HOOK_TICK_LOG_ROTATED_PATH,
        _cctally_core.HOOK_TICK_LOG_PATH,
    ):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            tokens = line.split()
            if not tokens:
                continue
            try:
                captured_at = parse_iso_datetime(tokens[0], "codex lifecycle log timestamp")
                captured_at = captured_at.astimezone(dt.timezone.utc)
            except (IndexError, ValueError, TypeError):
                continue
            if captured_at > now_utc:
                continue
            fields = {
                token.split("=", 1)[0]: token.split("=", 1)[1]
                for token in tokens[1:] if "=" in token
            }
            if fields.get("provider") != "codex":
                continue
            key = fields.get("source_root_key")
            if key not in root_keys:
                continue
            outcome = fields.get("result")
            if outcome not in {"success", "error"}:
                continue
            row = records.setdefault(key, {
                "last_tick_at": None,
                "success_count_24h": 0,
                "error_count_24h": 0,
            })
            if outcome == "success":
                prior = row["last_tick_at"]
                if prior is None or captured_at > prior:
                    row["last_tick_at"] = captured_at
            if captured_at >= cutoff:
                if outcome == "success":
                    row["success_count_24h"] += 1
                else:
                    row["error_count_24h"] += 1
    return records


def doctor_gather_state(
    *,
    now_utc: "dt.datetime | None" = None,
    runtime_bind: "str | None" = None,
    deep: bool = False,
):
    """I/O chokepoint for `cctally doctor` (spec §7.2).

    H1 invariant: config.json is read RAW (NOT via load_config), since
    load_config auto-creates the file on first run — a read-only
    diagnostic command must never mutate user state.

    `deep=True` (CLI cmd_doctor only) additionally runs `PRAGMA
    quick_check(1)` on each DB (#279 S2 F5b); the dashboard/TUI callers
    stay `deep=False` — the rebuild loop calls the gather every rebuild
    and quick_check on a large cache.db costs seconds.
    """
    import _lib_doctor

    c = _cctally()
    if now_utc is None:
        now_utc = _now_utc()

    # ── Install ──────────────────────────────────────────────────────
    # #279 S2 F5d: guard the only two unguarded statements in the
    # otherwise fail-soft gather — an exception here would kill the whole
    # report. Downstream consumers already degrade on None.
    try:
        repo_root = c._setup_resolve_repo_root()
    except Exception:
        repo_root = None
    try:
        dst_dir = c._setup_local_bin_dir()
    except Exception:
        dst_dir = None
    try:
        symlink_state = c._setup_compute_symlink_state(repo_root, dst_dir)
    except Exception:
        symlink_state = None
    try:
        path_includes = c._setup_path_includes_local_bin()
    except Exception:
        path_includes = None
    # Issue #119: availability-aware install checks. Precomputed here (the
    # I/O layer) so the kernel stays pure — `shutil.which` and the on-disk
    # legacy-link probe never run in _lib_doctor.
    #   * cctally_reachable_on_path — channel-agnostic "is the command on
    #     $PATH at all?" (brew <prefix>/bin, npm prefix, source ~/.local/bin
    #     all satisfy it). Lets install.path pass without a ~/.local/bin
    #     membership check.
    #   * symlinks_path_pinned — true iff cctally runs ONLY through a legacy
    #     ~/.local/bin link to a retired/foreign install (live retired link
    #     with no reachable_elsewhere fallback). Mirrors the pinned-only-path
    #     predicate in _setup_install so doctor + setup agree on the fix.
    try:
        cctally_reachable_on_path = shutil.which("cctally") is not None
    except Exception:
        cctally_reachable_on_path = None
    try:
        symlinks_path_pinned = any(
            s == "wrong"
            and (dst_dir / n).is_symlink()
            and c._setup_symlink_is_retired(dst_dir / n, n, repo_root)
            and (dst_dir / n).resolve(strict=False).exists()
            for n, s in (symlink_state or [])
        )
    except Exception:
        symlinks_path_pinned = False
    # install_is_brew — channel knowledge for the install.path WARN
    # remediation. Brew kegs own no ~/.local/bin symlinks (#119), so the
    # ~/.local/bin / `cctally setup` hint is wrong for them; the kernel
    # can't derive this from repo_root (no I/O), so precompute it here.
    try:
        install_is_brew = c._setup_is_brew_install(repo_root)
    except Exception:
        install_is_brew = False
    try:
        legacy_snippet = c._setup_detect_legacy_snippet()
    except Exception:
        legacy_snippet = None

    # ── Hooks ────────────────────────────────────────────────────────
    try:
        settings = c._load_claude_settings()
    except c.SetupError:
        settings = None
    # #311: precompute the statusLine.refreshInterval state via the setup
    # I/O-layer classifier (wrapper recognition does file scans), so the pure
    # doctor kernel stays I/O-free. `settings is None` (SetupError) → the
    # classifier's `unavailable`, matching the check's always-OK posture.
    try:
        statusline_refresh_state = c._classify_statusline_refresh(settings)[0]
    except Exception:
        statusline_refresh_state = "unavailable"
    # Below: fail-soft posture for the diagnostic — any unexpected error
    # in a sub-probe degrades that field to None rather than aborting the
    # whole report.
    try:
        hook_counts = c._setup_count_hook_entries(settings or {})
    except Exception:
        hook_counts = None
    try:
        legacy_bespoke = c._setup_detect_legacy_bespoke_hooks(settings or {})
    except Exception:
        legacy_bespoke = None
    try:
        activity = c._setup_recent_log_stats()
    except Exception:
        activity = None

    # ── Auth ─────────────────────────────────────────────────────────
    try:
        oauth_token_present = c._setup_oauth_token_present()
    except OSError:
        oauth_token_present = None

    # ── DB ───────────────────────────────────────────────────────────
    try:
        stats_db_status = c._db_status_for(_cctally_core.DB_PATH, c._STATS_MIGRATIONS, "stats.db")
        if not _cctally_core.DB_PATH.exists():
            stats_db_status["_file_exists"] = False
    except sqlite3.Error as exc:
        stats_db_status = {"path": str(_cctally_core.DB_PATH), "user_version": 0,
                           "registry_size": len(c._STATS_MIGRATIONS),
                           "migrations": [], "_open_error": str(exc)}
    try:
        cache_db_status = c._db_status_for(_cctally_core.CACHE_DB_PATH, c._CACHE_MIGRATIONS, "cache.db")
        if not _cctally_core.CACHE_DB_PATH.exists():
            cache_db_status["_file_exists"] = False
    except sqlite3.Error as exc:
        cache_db_status = {"path": str(_cctally_core.CACHE_DB_PATH), "user_version": 0,
                           "registry_size": len(c._CACHE_MIGRATIONS),
                           "migrations": [], "_open_error": str(exc)}

    # ── Data freshness ───────────────────────────────────────────────
    latest_snapshot_at = None
    forked_bucket_counts: dict | None = None
    credited_weeks: list[dict] | None = None
    try:
        if _cctally_core.DB_PATH.exists():
            conn = sqlite3.connect(str(_cctally_core.DB_PATH))
            try:
                try:
                    row = conn.execute(
                        "SELECT MAX(captured_at_utc) FROM weekly_usage_snapshots"
                    ).fetchone()
                    if row and row[0]:
                        latest_snapshot_at = parse_iso_datetime(
                            row[0], "weekly_usage_snapshots.captured_at_utc",
                        ).astimezone(dt.timezone.utc)
                except sqlite3.OperationalError:
                    pass  # table missing — treat as no snapshots yet
                # Forked-bucket invariant probe. Each fork count is
                # a raw SELECT against the already-open connection —
                # no bonus open_db() recursion. Tables missing →
                # count 0 (legacy DBs without one of these tables
                # are intact by definition for that table).
                forked_bucket_counts = {}
                for table, key in (
                    ("weekly_usage_snapshots", "usage"),
                    ("weekly_cost_snapshots", "cost"),
                    ("percent_milestones", "milestones"),
                ):
                    try:
                        row = conn.execute(
                            f"SELECT COUNT(*) FROM {table} "
                            f" WHERE week_start_at IS NOT NULL "
                            f"   AND week_start_date != substr(week_start_at, 1, 10)"
                        ).fetchone()
                        forked_bucket_counts[key] = (
                            int(row[0]) if row and row[0] else 0
                        )
                    except sqlite3.OperationalError:
                        forked_bucket_counts[key] = 0
                # v1.7.2 credited-week tracking. For each week with a
                # past-effective ``week_reset_events`` row, gather the
                # latest weekly_percent + count of post-credit milestones.
                # The check warns when latest_percent >= 1.0 AND
                # post_credit_milestone_count == 0.
                # unixepoch() normalizes the cross-offset comparison.
                try:
                    credit_rows = conn.execute(
                        """
                        SELECT wre.id AS event_id,
                               wre.new_week_end_at AS end_at,
                               wre.effective_reset_at_utc AS effective
                          FROM week_reset_events wre
                         WHERE unixepoch(wre.effective_reset_at_utc)
                               <= unixepoch(?)
                        """,
                        (now_utc_iso(),),
                    ).fetchall()
                    credited_weeks = []
                    for cr in credit_rows:
                        end_at = cr[1]
                        evt_id = cr[0]
                        latest = conn.execute(
                            """
                            SELECT week_start_date, weekly_percent
                              FROM weekly_usage_snapshots
                             WHERE week_end_at = ?
                             ORDER BY captured_at_utc DESC, id DESC
                             LIMIT 1
                            """,
                            (end_at,),
                        ).fetchone()
                        if latest is None or latest[0] is None:
                            continue
                        ws = latest[0]
                        lp = float(latest[1] or 0.0)
                        try:
                            mc_row = conn.execute(
                                "SELECT COUNT(*) FROM percent_milestones "
                                "WHERE week_start_date = ? AND reset_event_id = ?",
                                (ws, evt_id),
                            ).fetchone()
                            mc = int(mc_row[0]) if mc_row and mc_row[0] else 0
                        except sqlite3.OperationalError:
                            mc = 0
                        credited_weeks.append({
                            "week_start_date": ws,
                            "latest_weekly_percent": lp,
                            "post_credit_milestone_count": mc,
                            "event_id": evt_id,
                        })
                except sqlite3.OperationalError:
                    # week_reset_events table missing — treat as no
                    # credited weeks (pre-feature DB).
                    credited_weeks = []
            finally:
                conn.close()
    except Exception:
        pass

    cache_entries_count = None
    cache_last_entry_at = None
    try:
        if _cctally_core.CACHE_DB_PATH.exists():
            conn = sqlite3.connect(str(_cctally_core.CACHE_DB_PATH))
            try:
                row = conn.execute(
                    "SELECT COUNT(*), MAX(timestamp_utc) FROM session_entries"
                ).fetchone()
                if row:
                    cache_entries_count = int(row[0]) if row[0] is not None else 0
                    if row[1]:
                        cache_last_entry_at = parse_iso_datetime(
                            row[1], "session_entries.timestamp_utc",
                        ).astimezone(dt.timezone.utc)
            except sqlite3.OperationalError:
                pass  # table missing — treat as zero
            finally:
                conn.close()
    except Exception:
        pass

    # ── Statusline candidate arbitration (#318) ──────────────────────
    # This inspection is deliberately independent of SQLite mutation: marker
    # mtime, candidate/control files, and tombstones are all read fail-soft.
    # In particular it uses the scan-only candidate helper, never the reducer
    # loader that prunes expired or malformed spool files.
    try:
        statusline_pipeline = _gather_statusline_pipeline(c, now_utc=now_utc)
    except Exception:
        statusline_pipeline = None

    # Conversation-sessions rollup consistency (#217 S1 / U9). Two cheap COUNTs
    # (graceful None on a missing table / unreadable DB) + an in-progress signal
    # so a transient mid-sync mismatch never WARNs. The in-progress signal is a
    # NON-BLOCKING cache.db.lock flock probe (a writer mid-walk holds it) OR the
    # presence of any pending reingest/split/backfill cache_meta flag — doctor
    # stays read-only and never blocks on the lock.
    conv_sessions_rollup_count = None
    conv_messages_distinct_sessions = None
    conv_rollup_sync_in_progress = False
    try:
        if _cctally_core.CACHE_DB_PATH.exists():
            conn = sqlite3.connect(str(_cctally_core.CACHE_DB_PATH))
            try:
                try:
                    row = conn.execute(
                        "SELECT COUNT(*) FROM conversation_sessions"
                    ).fetchone()
                    if row is not None:
                        conv_sessions_rollup_count = int(row[0])
                except sqlite3.OperationalError:
                    pass  # table absent (pre-rollup) — leave None
                try:
                    row = conn.execute(
                        "SELECT COUNT(DISTINCT session_id) "
                        "FROM conversation_messages WHERE session_id IS NOT NULL"
                    ).fetchone()
                    if row is not None:
                        conv_messages_distinct_sessions = int(row[0])
                except sqlite3.OperationalError:
                    pass
                # Pending reingest/split/backfill flags ⇒ a full sync hasn't yet
                # reconciled the rollup. Read the canonical flag set from
                # _cctally_cache so it stays in lockstep with the sync consumers.
                try:
                    import _cctally_cache as _cc_sib  # lazy sibling
                    flags = tuple(_cc_sib._TARGETED_DECLINE_FLAGS)
                    placeholders = ",".join("?" for _ in flags)
                    pend = conn.execute(
                        f"SELECT 1 FROM cache_meta WHERE key IN ({placeholders}) "
                        "LIMIT 1", flags).fetchone()
                    if pend is not None:
                        conv_rollup_sync_in_progress = True
                except Exception:
                    pass
            finally:
                conn.close()
        # Non-blocking flock probe: if a writer (sync_cache / a reingest) holds
        # the cache.db.lock, the rollup may be mid-recompute → in progress. We
        # acquire LOCK_EX|LOCK_NB and immediately release; failure (held) is the
        # signal. Never blocks (LOCK_NB), so doctor stays read-only + prompt.
        if not conv_rollup_sync_in_progress:
            lock_path = _cctally_core.CACHE_LOCK_PATH
            if lock_path is not None and pathlib.Path(lock_path).exists():
                import fcntl as _fcntl
                lock_fh = open(str(lock_path), "w")
                try:
                    _fcntl.flock(lock_fh, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
                    _fcntl.flock(lock_fh, _fcntl.LOCK_UN)  # acquired ⇒ quiescent
                except (BlockingIOError, OSError):
                    conv_rollup_sync_in_progress = True  # held ⇒ writer mid-flight
                finally:
                    lock_fh.close()
    except Exception:
        pass

    claude_jsonl_present = False
    try:
        claude_dir = pathlib.Path.home() / ".claude" / "projects"
        if claude_dir.exists():
            claude_jsonl_present = next(claude_dir.glob("**/*.jsonl"), None) is not None
    except Exception:
        pass

    codex_entries_count = None
    codex_last_entry_at = None
    try:
        if _cctally_core.CACHE_DB_PATH.exists():
            conn = sqlite3.connect(str(_cctally_core.CACHE_DB_PATH))
            try:
                row = conn.execute(
                    "SELECT COUNT(*), MAX(timestamp_utc) FROM codex_session_entries"
                ).fetchone()
                if row:
                    codex_entries_count = int(row[0]) if row[0] is not None else 0
                    if row[1]:
                        codex_last_entry_at = parse_iso_datetime(
                            row[1], "codex_session_entries.timestamp_utc",
                        ).astimezone(dt.timezone.utc)
            except sqlite3.OperationalError:
                pass
            finally:
                conn.close()
    except Exception:
        pass

    # Issue #109: probe every $CODEX_HOME session root (not the single
    # hardcoded ~/.codex/sessions), matching the multi-root ingestion path
    # from #108. _codex_session_roots() already applies the sessions/-subdir
    # rule and filters to existing dirs, so a bare glob per root suffices.
    codex_jsonl_present = False
    try:
        for codex_dir in c._codex_session_roots():
            if next(codex_dir.glob("**/*.jsonl"), None) is not None:
                codex_jsonl_present = True
                break
    except Exception:
        pass

    # ── Codex quota lifecycle (#294 S2) ──────────────────────────────
    # All three probes are read-only and root-qualified.  The physical cache
    # adapter preserves S1's per-window degradation, while setup's existing
    # inspector supplies the exact owned-hook state without exposing paths.
    codex_quota_windows: list[dict] = []
    try:
        observations = c._cctally_quota.load_codex_quota_observations()
        by_identity: dict[object, list] = {}
        for observation in observations:
            by_identity.setdefault(observation.identity, []).append(observation)
        for identity in sorted(
            by_identity,
            key=lambda item: (
                item.source, item.source_root_key, item.logical_limit_key,
                item.observed_slot, item.window_minutes,
            ),
        ):
            freshness = c.quota_freshness(by_identity[identity], now_utc)
            codex_quota_windows.append({
                "identity": {
                    "source": identity.source,
                    "source_root_key": identity.source_root_key,
                    "logical_limit_key": identity.logical_limit_key,
                    "observed_slot": identity.observed_slot,
                    "window_minutes": identity.window_minutes,
                },
                "latest_capture_at": freshness.captured_at,
                "freshness_state": freshness.state,
                "age_seconds": freshness.age_seconds,
                "stale_after_seconds": freshness.stale_after_seconds,
            })
    except Exception:
        codex_quota_windows = []

    codex_hook_roots: list[dict] = []
    try:
        codex_binary = str(c._setup_resolve_hook_target(repo_root))
        hook_rows = [
            c._cctally_setup._codex_hook_row(root, codex_binary)
            for root in c._setup_codex_hook_roots()
        ]
        codex_hook_roots = [
            {"source_root_key": row["source_root_key"], "state": row["state"]}
            for row in sorted(hook_rows, key=lambda row: row["source_root_key"])
        ]
    except Exception:
        codex_hook_roots = []

    try:
        codex_lifecycle_activity_24h = _codex_lifecycle_activity_24h(
            root_keys={row["source_root_key"] for row in codex_hook_roots},
            now_utc=now_utc,
        )
    except Exception:
        codex_lifecycle_activity_24h = {}

    # ── Parse health (#279 S2 F5a) ───────────────────────────────────
    parse_health_claude = parse_health_codex = None
    try:
        if _cctally_core.CACHE_DB_PATH.exists():
            conn = sqlite3.connect(str(_cctally_core.CACHE_DB_PATH))
            try:
                for _key in ("parse_health_claude", "parse_health_codex"):
                    try:
                        row = conn.execute(
                            "SELECT value FROM cache_meta WHERE key = ?",
                            (_key,),
                        ).fetchone()
                        if row and row[0]:
                            _parsed = json.loads(row[0])
                            if isinstance(_parsed, dict):
                                if _key == "parse_health_claude":
                                    parse_health_claude = _parsed
                                else:
                                    parse_health_codex = _parsed
                    except (sqlite3.OperationalError, ValueError):
                        pass
            finally:
                conn.close()
    except Exception:
        pass

    # ── Integrity (deep only — #279 S2 F5b) ──────────────────────────
    stats_db_quick_check = cache_db_quick_check = None
    if deep:
        for _label, _path in (("stats", _cctally_core.DB_PATH),
                              ("cache", _cctally_core.CACHE_DB_PATH)):
            _result = None
            try:
                if _path.exists():
                    _conn = sqlite3.connect(str(_path))
                    try:
                        _row = _conn.execute(
                            "PRAGMA quick_check(1)").fetchone()
                        _result = (str(_row[0])
                                   if _row and _row[0] is not None else None)
                    finally:
                        _conn.close()
            except sqlite3.DatabaseError as exc:
                _result = f"open failed: {exc}"
            except Exception:
                _result = None
            if _label == "stats":
                stats_db_quick_check = _result
            else:
                cache_db_quick_check = _result

    # ── Lock state (#279 S2 F5c) — read-only: never create files ─────
    locks_held: "dict | None" = None
    try:
        locks_held = {}
        for _name, _lp in (
            ("cache.db.lock", _cctally_core.CACHE_LOCK_PATH),
            ("cache.db.codex.lock", _cctally_core.CACHE_LOCK_CODEX_PATH),
        ):
            if not _lp.exists():
                locks_held[_name] = False
                continue
            try:
                with open(_lp, "r") as _lf:
                    try:
                        fcntl.flock(_lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        fcntl.flock(_lf, fcntl.LOCK_UN)
                        locks_held[_name] = False
                    except OSError:
                        locks_held[_name] = True
            except OSError:
                locks_held[_name] = None
    except Exception:
        locks_held = None

    # ── cache.db WAL size (#297) — read-only backstop ────────────────
    # Gathered OUTSIDE the deep/quick_check branch (above) so the WAL-size
    # check runs in both shallow and deep gather modes. Best-effort getsize;
    # None on OSError/race (doctor never blocks or raises), 0 when absent.
    cache_db_wal_bytes: "int | None"
    try:
        _wal = pathlib.Path(f"{_cctally_core.CACHE_DB_PATH}-wal")
        cache_db_wal_bytes = _wal.stat().st_size if _wal.exists() else 0
    except OSError:
        cache_db_wal_bytes = None

    # ── Safety ───────────────────────────────────────────────────────
    # `dashboard.bind` is read via the same chokepoint that powers
    # `cctally config get dashboard.bind` — `_config_known_value`
    # normalizes hand-edited junk back to "loopback", matching the
    # value cmd_dashboard would actually bind to.
    #
    # Raw JSON read (NOT load_config or _load_config_unlocked): both
    # call `ensure_dirs()`, which creates `~/.local/share/cctally/`
    # and `logs/` on a fresh HOME. Doctor is a read-only diagnostic
    # (H1 invariant) — it must never mutate user state, even by
    # creating an empty directory tree. Corrupt JSON yields
    # `dashboard_bind_stored = "loopback"` (the same fallback the
    # original try/except gave); the dedicated `config_json_valid`
    # check surfaces the corruption separately.
    #
    # `dashboard.expose_transcripts` (Plan 2, spec §5) is read off the same raw
    # JSON via the same chokepoint (defaults False; hand-edited junk → False).
    # `_check_safety_dashboard_bind` only consults it when the bind is LAN, so
    # a loopback report is byte-identical whether or not it's set.
    dashboard_bind_stored = "loopback"
    expose_transcripts = False
    try:
        if _cctally_core.CONFIG_PATH.exists():
            raw_cfg = json.loads(_cctally_core.CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(raw_cfg, dict):
                dashboard_bind_stored = (
                    c._config_known_value(raw_cfg, "dashboard.bind") or "loopback"
                )
                expose_transcripts = bool(
                    c._config_known_value(raw_cfg, "dashboard.expose_transcripts")
                )
    except (json.JSONDecodeError, OSError):
        pass

    # ── Telemetry (anonymous install-count, spec 2026-07-07) ─────────
    # Resolve the opt-out state via the pure kernel predicate — it reads env
    # + config + the dev-checkout fact and NEVER mints an install_id / touches
    # any marker (read-only H1 invariant). Uses the same raw config read as the
    # safety block so doctor never auto-creates config.json; a missing/corrupt
    # config degrades to `{}` (env/dev precedence still resolves correctly).
    telemetry_enabled = True
    telemetry_reason = "enabled"
    try:
        raw_tele_cfg: dict = {}
        if _cctally_core.CONFIG_PATH.exists():
            loaded = json.loads(_cctally_core.CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                raw_tele_cfg = loaded
        telemetry_enabled, telemetry_reason = c.resolve_telemetry_state(raw_tele_cfg)
    except Exception:
        # Fail-soft: any read/parse/resolution error degrades to the enabled
        # default (the check renders OK regardless — it never FAILs/WARNs).
        telemetry_enabled, telemetry_reason = (True, "enabled")

    # config.json — RAW READ, never load_config(). load_config()
    # auto-creates on first run AND silently falls back to defaults
    # on corruption — both behaviors would hide diagnostic state
    # (codex H1).
    config_json_error = None
    try:
        if _cctally_core.CONFIG_PATH.exists():
            json.loads(_cctally_core.CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        config_json_error = f"{type(exc).__name__}: {exc}"
    except OSError as exc:
        config_json_error = f"OSError: {exc}"

    update_state = None
    update_state_error = None
    try:
        update_state = c._load_update_state()
    except Exception as exc:
        update_state_error = f"{type(exc).__name__}: {exc}"

    update_suppress = None
    update_suppress_error = None
    try:
        update_suppress = c._load_update_suppress()
    except Exception as exc:
        update_suppress_error = f"{type(exc).__name__}: {exc}"

    # Same predicate the update banner uses; doctor must not warn about
    # updates the user has already skipped or deferred.
    effective_update_available, effective_update_reason = (
        c._compute_effective_update_available(update_state, update_suppress, now_utc)
    )

    # ── Pricing coverage (spec §5.1) ─────────────────────────────────
    # Read-only trailing-30d scan + classification via the pure-fn kernel.
    # Any failure degrades to None so the check renders OK (never FAIL) and
    # the rest of the report is unaffected — same posture as the cache reads
    # above. `_pricing_observed_models` honors the no-mutation contract.
    pricing_coverage = None
    try:
        observed = c._pricing_observed_models(now_utc)
        # Detection-only: pass warn=False so finding an unpriced model here does
        # NOT fire the cost-engine's `[cost] unknown model` stderr warning (this
        # is a read-only diagnostic, and the warning would also poison the
        # dedup set, suppressing a later genuine cost-path warning).
        pricing_coverage = c.classify_coverage(
            observed,
            lambda m: c._resolve_model_pricing(m, warn=False),
            c._is_codex_fallback,
        )
    except Exception:
        pricing_coverage = None

    # ── Meta ─────────────────────────────────────────────────────────
    cctally_version_tuple = _lib_changelog._read_latest_changelog_version()
    cctally_version = (
        cctally_version_tuple[0] if cctally_version_tuple else "unknown"
    )

    return _lib_doctor.DoctorState(
        symlink_state=symlink_state,
        path_includes_local_bin=path_includes,
        # Issue #119: availability-aware install checks (precomputed above).
        cctally_reachable_on_path=cctally_reachable_on_path,
        symlinks_path_pinned=symlinks_path_pinned,
        install_is_brew=install_is_brew,
        legacy_snippet=legacy_snippet,
        legacy_bespoke=legacy_bespoke,
        claude_settings=settings,
        hook_counts=hook_counts,
        log_activity_24h=activity,
        oauth_token_present=oauth_token_present,
        stats_db_status=stats_db_status,
        cache_db_status=cache_db_status,
        latest_snapshot_at=latest_snapshot_at,
        cache_entries_count=cache_entries_count,
        cache_last_entry_at=cache_last_entry_at,
        claude_jsonl_present=claude_jsonl_present,
        forked_bucket_counts=forked_bucket_counts,
        credited_weeks=credited_weeks,
        codex_entries_count=codex_entries_count,
        codex_last_entry_at=codex_last_entry_at,
        codex_jsonl_present=codex_jsonl_present,
        dashboard_bind_stored=dashboard_bind_stored,
        runtime_bind=runtime_bind,
        # Conversation viewer (Plan 2, spec §5): only consulted on a LAN bind.
        expose_transcripts=expose_transcripts,
        config_json_error=config_json_error,
        update_state=update_state,
        update_state_error=update_state_error,
        update_suppress=update_suppress,
        update_suppress_error=update_suppress_error,
        effective_update_available=effective_update_available,
        effective_update_reason=effective_update_reason,
        now_utc=now_utc,
        cctally_version=cctally_version,
        # Dev-instance isolation (§4): which data dir resolved + how.
        dev_mode=_cctally_core.DEV_MODE,
        app_dir=str(_cctally_core.APP_DIR),
        is_dev_checkout=_cctally_core._is_dev_checkout(),
        # Preview channel (CCTALLY_CHANNEL=preview): surfaced in install.mode.
        channel=("preview" if _cctally_core.is_preview_channel() else "prod"),
        # Anonymous install-count telemetry (spec 2026-07-07): read-only
        # opt-out state, resolved above without minting an install_id.
        telemetry_enabled=telemetry_enabled,
        telemetry_reason=telemetry_reason,
        # Pricing-freshness check (spec §5.1): trailing-30d coverage gaps.
        pricing_coverage=pricing_coverage,
        # Conversation-sessions rollup consistency (#217 S1 / U9).
        conv_sessions_rollup_count=conv_sessions_rollup_count,
        conv_messages_distinct_sessions=conv_messages_distinct_sessions,
        conv_rollup_sync_in_progress=conv_rollup_sync_in_progress,
        # #279 S2 F5: parse-health records, deep quick_check results, and
        # non-blocking lock-file probes (appended after the defaulted tail).
        parse_health_claude=parse_health_claude,
        parse_health_codex=parse_health_codex,
        stats_db_quick_check=stats_db_quick_check,
        cache_db_quick_check=cache_db_quick_check,
        locks_held=locks_held,
        # #297: cache.db WAL size backstop (gathered outside the deep branch).
        cache_db_wal_bytes=cache_db_wal_bytes,
        codex_quota_windows=codex_quota_windows,
        codex_hook_roots=codex_hook_roots,
        codex_lifecycle_activity_24h=codex_lifecycle_activity_24h,
        # #311: precomputed statusLine.refreshInterval classification.
        statusline_refresh_state=statusline_refresh_state,
        statusline_pipeline=statusline_pipeline,
    )


def cmd_doctor(args: argparse.Namespace) -> int:
    """Run all doctor checks and emit the report. Spec §4, §7.3.

    Calls the I/O chokepoint (doctor_gather_state) → pure kernel
    (_lib_doctor.run_checks) → renderer (render_text or
    serialize_json). The argparse `add_mutually_exclusive_group`
    handles the --quiet/--verbose collision at parse time; the
    defense-in-depth check here covers programmatic invocation that
    bypasses argparse.

    Exit code follows the loose mapping in spec §4.5: 0 unless
    overall_severity == "fail", then 2. Note that warn → 0; doctor
    is read-only and warn-class findings are advisories, not errors.
    """
    import _lib_doctor
    c = _cctally()
    quiet = bool(getattr(args, "quiet", False))
    verbose = bool(getattr(args, "verbose", False))
    if quiet and verbose:
        eprint("doctor: --quiet and --verbose are mutually exclusive")
        return 2
    # #279 S2 F5b: deep=True runs PRAGMA quick_check(1) — CLI-only (the
    # dashboard/TUI gather callers stay deep=False so their per-rebuild
    # gather never pays the multi-second cost on a large cache.db).
    state = c.doctor_gather_state(deep=True)
    report = _lib_doctor.run_checks(state)
    if getattr(args, "json", False):
        print(json.dumps(
            _lib_doctor.serialize_json(report), indent=2, sort_keys=True,
        ))
    else:
        sys.stdout.write(_lib_doctor.render_text(
            report, quiet=quiet, verbose=verbose,
        ))
    return 2 if report.overall_severity == "fail" else 0
