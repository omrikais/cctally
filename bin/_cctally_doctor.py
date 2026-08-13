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
import contextlib
import datetime as dt
import fcntl
import json
import math
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys

import _cctally_core
import _lib_changelog
import _lib_journal_router
from _cctally_core import _now_utc, eprint, now_utc_iso, parse_iso_datetime
from _lib_dashboard_json import encode_dashboard_json


#: Record types the deep journal conflict scan retains (#374). The selector
#: (`_lib_journal.resolve_effective_events`) reads only `evt`, `correction` and
#: `correction_batch`; `op` is kept because the rebuild-equivalent account
#: normalization is defined over evt/op records. Everything else — above all the
#: `obs` lines, ~97% of a real journal — becomes a `None` positional slot as it
#: is decoded, so peak RSS tracks decision dictionaries plus one pointer per
#: decoded line rather than the whole decoded journal.
_CONFLICT_SCAN_RECORD_TYPES = _lib_journal_router.RETAINED_RECORD_TYPES

#: Doctor needs only recent evidence for this diagnostic. Bound both line count
#: and bytes so a corrupt single-line file cannot defeat the tail limit.
_GUARD_LOG_TAIL_LINES = 256
_GUARD_LOG_TAIL_BYTES = 64 * 1024


def _cctally():
    """Resolve the current `cctally` module at call-time (spec §3.1)."""
    return sys.modules["cctally"]


def _stats_ro_guarded():
    """A `mode=ro` stats connection that participates in the #386 opener protocol.

    Read-only does not mean side-effect-free: measured on this platform, a
    `mode=ro` connection to a WAL database with absent sidecars CREATES
    `stats.db-shm` and `stats.db-wal`. Spec §3.1's third clause therefore
    covers these diagnostics, and both callers already degrade on
    `sqlite3.Error` (which `StatsDbMaintenanceError` is).
    """
    import _cctally_store

    return _cctally_store.stats_open_guarded(
        _cctally_core.DB_PATH,
        connect=lambda p: sqlite3.connect(f"file:{p}?mode=ro", uri=True),
    )


@contextlib.contextmanager
def _conversation_ro_guarded(*, timeout: float):
    """Bounded read-only transcript handle participating in #415 recovery."""
    path = pathlib.Path(_cctally_core.CONVERSATIONS_DB_PATH)
    if not path.exists():
        yield None
        return
    marker = path.with_name(f"{path.name}.repairing")
    pending = path.with_name(f"{path.name}.quarantine-pending.json")
    recovery = path.with_name(f"{path.name}.recovery.json")
    maintenance = pathlib.Path(
        _cctally_core.CONVERSATIONS_LOCK_MAINTENANCE_PATH
    )
    lock_fh = conn = None
    try:
        maintenance.parent.mkdir(parents=True, exist_ok=True)
        lock_fh = open(maintenance, "a+")
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            yield None
            return
        if marker.exists() or pending.exists() or recovery.exists():
            yield None
            return
        conn = sqlite3.connect(
            path.resolve().as_uri() + "?mode=ro",
            uri=True,
            timeout=max(0.0, timeout),
        )
        if marker.exists() or pending.exists() or recovery.exists():
            conn.close()
            conn = None
            yield None
            return
        yield conn
    finally:
        if conn is not None:
            conn.close()
        if lock_fh is not None:
            try:
                fcntl.flock(lock_fh, fcntl.LOCK_UN)
            except OSError:
                pass
            lock_fh.close()


def _journal_heal_incident(kind: str, name: str, now_utc: dt.datetime) -> dict:
    """One auto-heal artifact record for the doctor journal leg (§9). Parses the
    legacy ``%Y%m%dT%H%M%SZ`` or collision-safe rebuild
    ``%Y%m%dT%H%M%S_%f`` timestamp trailing the name (quarantine dir
    ``<db>.db-<ts>`` or forensics file
    ``<db>.db-corruption-forensics-<ts>.json``) into an age; a name that
    doesn't parse degrades to ``age_s=None``."""
    base = name[:-5] if name.endswith(".json") else name
    ts = base.rsplit("-", 1)[-1]
    age_s = None
    for timestamp_format in ("%Y%m%dT%H%M%SZ", "%Y%m%dT%H%M%S_%f"):
        try:
            parsed = dt.datetime.strptime(ts, timestamp_format).replace(
                tzinfo=dt.timezone.utc)
            age_s = int((now_utc - parsed).total_seconds())
            break
        except ValueError:
            continue
    return {"kind": kind, "name": name, "age_s": age_s, "shape": None}


def _incident_shape_token(incident) -> "str | None":
    """One incident's `damage.preserved.shapeToken` (#496 S6 §7.2 / §3.4).

    Read here rather than in the kernel, which takes no filesystem. A manifest
    that cannot be read contributes no shape, which is the safe direction: a
    missing shape cannot manufacture a recurrence.
    """
    try:
        manifest = json.loads(
            (incident / "manifest.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    if not isinstance(manifest, dict):
        return None
    damage = manifest.get("damage")
    preserved = damage.get("preserved") if isinstance(damage, dict) else None
    token = preserved.get("shapeToken") if isinstance(preserved, dict) else None
    return token if isinstance(token, str) and token else None


def _gather_heal_detections(now_utc: dt.datetime) -> "list | None":
    """The durable heal ring, as `{heal_id, age_s}` per entry (§7.2).

    A DETECTION is a ring entry keyed by `healId`, which is a different thing
    from an incident: a declined or coalesced detection produces a ring entry
    and no quarantine directory at all, and only the ring can report the RATE.
    """
    try:
        import _cctally_store

        events = _cctally_store.read_stats_heal_events()
    except Exception:
        return None
    found = []
    for event in events:
        if not isinstance(event, dict):
            continue
        age_s = None
        stamp = event.get("detectedAtUtc")
        if isinstance(stamp, str) and stamp:
            try:
                parsed = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                age_s = int((now_utc - parsed).total_seconds())
            except ValueError:
                age_s = None
        found.append({
            "heal_id": str(event.get("healId") or ""),
            "age_s": age_s,
            "outcome": event.get("outcome"),
        })
    return found


def _gather_retained_artifacts(
    now_utc: dt.datetime, *, deep: bool = False,
) -> "dict | None":
    """The read-only retention scan and plan behind `db.retained_artifacts`.

    Takes NO lock and writes nothing — it is the same walk and the same kernel
    plan `cctally db prune`'s preview runs. A malformed policy is reported
    rather than repaired, because §6.5 makes an unreadable policy a FAIL and
    not a fallback to the defaults.

    **The walk and the planner are `deep`-gated, like the `quick_check` legs.**
    This gather is reached from the TUI and the dashboard snapshot precompute
    on every rebuild, not only from `GET /api/doctor`, and the pair costs tens
    of milliseconds at the maintainer's corpus size and grows with it. What
    stays in the shallow path is what an operator must act on and what costs
    one file read and one glob: the policy resolution and the stuck reclaim
    records.

    `now_utc` is the gather's clock, which honours `CCTALLY_AS_OF`. Letting the
    planner read the wall clock instead would make the age bound — and every
    golden that depends on it — drift with the calendar.

    The failure path names the exception CLASS rather than returning a bare
    None. Doctor must not crash, but a degrade that says only "unavailable" is
    indistinguishable from a healthy install with nothing retained, and a
    programming error here reached four golden fixtures and made all four of
    them vacuous before anything reported it.
    """
    now_epoch = now_utc.timestamp()
    try:
        import _cctally_retention

        resolution = _cctally_retention.read_retention_policy()
        stuck = [
            {
                "planId": record["planId"],
                "memberIds": sorted(record["entries"]),
                "stuck": bool(record.get("stuck")),
                "ageSeconds": record.get("ageSeconds"),
            }
            for record in _cctally_retention.list_stuck_reclaim_records(
                now_epoch=now_epoch,
            )
        ]
        if resolution.status == "malformed":
            # No byte figures are reported, rather than zeros: the scan never
            # ran, and a zero here is a false measurement a consumer would
            # render as "nothing retained".
            return {
                "policy_status": "malformed",
                "policy_reason": resolution.reason,
                "stuck_records": stuck,
            }
        bounds = {
            "max_age_seconds": resolution.policy.max_age_seconds,
            "max_count_per_family": resolution.policy.max_count_per_family,
            "max_total_bytes": resolution.policy.max_total_bytes,
            "min_free_bytes": resolution.policy.min_free_bytes,
        }
        if not deep:
            return {
                "policy_status": "not-scanned",
                "policy_reason": None,
                "stuck_records": stuck,
                **bounds,
            }
        scan, plan, graph = _cctally_retention.plan_retention(
            policy=resolution.policy, now_epoch=now_epoch, with_graph=True,
        )
        return {
            "policy_status": resolution.status,
            "policy_reason": None,
            "retained_bytes": plan.before_bytes,
            "reclaimable_bytes": plan.reclaimable_bytes,
            "protected_bytes": _retention_protected_bytes(scan, plan, graph),
            "protected_roots": len(plan.protected_ids),
            "roots": len(plan.delete_ids) + len(plan.keep_ids)
            + len(plan.protected_ids),
            "free_disk_bytes": scan.free_disk_bytes,
            "partial_scan": bool(scan.partial),
            "unsatisfied_rules": list(plan.unsatisfied_rules),
            # Which bounds actually SELECTED something. The WARN summary used
            # to name the byte budget whatever drove the reclamation, the same
            # false sentence the FAIL summary printed.
            "driving_rules": [
                rule for rule in _cctally_retention._kernel.BOUND_ORDER
                if rule in set(plan.reasons.values())
            ],
            "floor_retained_roots": len(plan.floor_retained_ids),
            "floor_retained_bytes": plan.floor_retained_bytes,
            "stuck_records": stuck,
            **bounds,
        }
    except Exception as exc:  # noqa: BLE001 — doctor never crashes
        # Read-only diagnostics never fail the gather; the leg degrades. The
        # class is carried so the degrade is STATED rather than silent.
        return {"policy_status": "unavailable", "scan_error": type(exc).__name__}


def _retention_protected_bytes(scan, plan, graph=None) -> int:
    """Bytes held by protected roots, counted once across shared members.

    The graph is handed in. Letting `summarize_prune` build its own made the
    doctor leg construct the reference graph TWICE per gather, once inside
    `plan_retention` and once here, for a structure neither call mutates.
    """
    try:
        import _cctally_retention

        return int(
            _cctally_retention.summarize_prune(
                scan, plan, graph=graph,
            )["protectedBytes"]
        )
    except Exception:
        return 0


def _read_guard_log_tail(path) -> list[str]:
    """Read at most the configured byte and line tail from one guard log."""
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        read_size = min(size, _GUARD_LOG_TAIL_BYTES)
        fh.seek(size - read_size)
        raw = fh.read(read_size)
    if size > read_size:
        # The byte window may start mid-line. Drop that fragment so every
        # reported entry is one complete writer-guard record.
        _, separator, raw = raw.partition(b"\n")
        if not separator:
            raw = b""
    lines = [
        line for line in raw.decode("utf-8", errors="replace").splitlines()
        if line.strip()
    ]
    return lines[-_GUARD_LOG_TAIL_LINES:]


def _gather_backup_sync_state(
    app_dir,
    *,
    platform_name: str | None = None,
    home_dir=None,
    probe_time_machine: bool = True,
    which=shutil.which,
    run=subprocess.run,
) -> dict:
    """Classify known macOS file-level backup/sync coverage without mutation."""
    platform_name = (
        platform_name
        or os.environ.get("CCTALLY_DOCTOR_FIXTURE_PLATFORM")
        or sys.platform
    )
    if platform_name != "darwin":
        return {"status": "unsupported", "provider": None}

    try:
        app_path = pathlib.Path(app_dir).resolve(strict=False)
        home = pathlib.Path(
            home_dir if home_dir is not None else pathlib.Path.home()
        ).resolve(strict=False)
    except (OSError, RuntimeError):
        return {"status": "unavailable", "provider": None}

    def inside(root) -> bool:
        try:
            app_path.relative_to(pathlib.Path(root).resolve(strict=False))
            return True
        except (ValueError, OSError, RuntimeError):
            return False

    if inside(home / "Library/Mobile Documents/com~apple~CloudDocs"):
        return {"status": "included", "provider": "iCloud Drive"}
    if inside(home / "Dropbox"):
        return {"status": "included", "provider": "Dropbox"}
    cloud_storage = home / "Library/CloudStorage"
    if inside(cloud_storage):
        try:
            relative = app_path.relative_to(cloud_storage.resolve(strict=False))
        except (ValueError, OSError, RuntimeError):
            relative = pathlib.Path()
        if relative.parts and relative.parts[0].lower().startswith("dropbox"):
            return {"status": "included", "provider": "Dropbox"}

    # The dashboard gathers Doctor state on every envelope rebuild. Keep that
    # hot path subprocess-free; the CLI's deep gather performs the bounded
    # Time Machine probes below. Static cloud-root classification above is safe
    # and cheap in either mode.
    if not probe_time_machine:
        return {"status": "unavailable", "provider": "Time Machine"}

    try:
        tmutil = which("tmutil")
    except Exception:
        tmutil = None
    if not tmutil:
        return {"status": "unavailable", "provider": "Time Machine"}
    try:
        destinations = run(
            [tmutil, "destinationinfo"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {"status": "unavailable", "provider": "Time Machine"}
    destination_text = f"{destinations.stdout}\n{destinations.stderr}".lower()
    if destinations.returncode != 0:
        status = (
            "absent"
            if "no destinations configured" in destination_text
            else "unavailable"
        )
        return {"status": status, "provider": "Time Machine"}
    if not destinations.stdout.strip():
        return {"status": "unavailable", "provider": "Time Machine"}
    try:
        exclusion = run(
            [tmutil, "isexcluded", str(app_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {"status": "unavailable", "provider": "Time Machine"}
    if exclusion.returncode != 0:
        return {"status": "unavailable", "provider": "Time Machine"}
    output = exclusion.stdout.strip().lower()
    if output.startswith("[excluded]"):
        return {"status": "excluded", "provider": "Time Machine"}
    if output.startswith("[included]"):
        return {"status": "included", "provider": "Time Machine"}
    return {"status": "unavailable", "provider": "Time Machine"}


def _gather_writer_guard_log(now_utc: dt.datetime):
    """Gather the bounded writer-guard tail; absent/unreadable is normal."""
    import _cctally_store as _store_mod_guard

    guard_path = _store_mod_guard._guard_log_path()
    if not guard_path.exists():
        return None
    lines = _read_guard_log_tail(guard_path)
    newest_age = None
    if lines:
        stamp = lines[-1].split("\t", 1)[0]
        try:
            when = _cctally_core.parse_iso_datetime(stamp, "guard log")
            newest_age = max(0, int((now_utc - when).total_seconds()))
        except Exception:
            newest_age = None
    return {
        "entries": len(lines),
        "newest_age_s": newest_age,
        "path": str(guard_path),
        "sample": lines[-1] if lines else None,
    }


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
        age = c._statusline_transport_age_seconds()
        result["transport_age_seconds"] = (
            age
            if isinstance(age, (int, float))
            and not isinstance(age, bool)
            and math.isfinite(float(age))
            else None
        )
    except Exception:
        pass
    try:
        age = c._statusline_observe_age_seconds()
        result["selected_age_seconds"] = (
            age
            if isinstance(age, (int, float))
            and not isinstance(age, bool)
            and math.isfinite(float(age))
            else None
        )
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


def _codex_quota_verify_activity_24h(*, now_utc: "dt.datetime") -> dict:
    """Aggregate the detached `_codex-quota-verify` worker's 24h outcomes.

    The worker's three streams are `/dev/null` and its exit code is observed by
    nobody, so `hook-tick.log` is the only place its outcome can land — which is
    why it writes there. `_codex_lifecycle_activity_24h` above cannot supply
    this: worker lines carry no `source_root_key`, so its root filter drops
    every one of them.

    Same bounded-read contract as its sibling — timestamped records, aggregate
    counters only, never session/prompt/response content.
    """
    cutoff = now_utc - dt.timedelta(hours=24)
    counts: dict = {
        "success_count_24h": 0,
        "error_count_24h": 0,
        "spawn_failure_count_24h": 0,
        "last_success_at": None,
    }
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
                captured_at = parse_iso_datetime(
                    tokens[0], "codex quota verify log timestamp")
                captured_at = captured_at.astimezone(dt.timezone.utc)
            except (IndexError, ValueError, TypeError):
                continue
            if captured_at > now_utc or captured_at < cutoff:
                continue
            fields = {
                token.split("=", 1)[0]: token.split("=", 1)[1]
                for token in tokens[1:] if "=" in token
            }
            if fields.get("provider") != "codex":
                continue
            op = fields.get("op")
            outcome = fields.get("result")
            if op == "quota-verify" and outcome == "success":
                counts["success_count_24h"] += 1
                prior = counts["last_success_at"]
                if prior is None or captured_at > prior:
                    counts["last_success_at"] = captured_at
            elif op == "quota-verify" and outcome == "error":
                counts["error_count_24h"] += 1
            elif op == "quota-verify-spawn" and outcome == "failed":
                counts["spawn_failure_count_24h"] += 1
    return counts


def _gather_accounts_state(now_utc: "dt.datetime") -> dict:
    """Best-effort account-attribution state for the doctor `accounts.*` legs
    (#341). Never raises: identity + registry reads are read-only and each guard
    swallows OperationalError so a legacy / pre-account stats.db degrades to the
    empty (all-OK) shape."""
    state: dict = {
        "claude_identity_status": None, "claude_email": None,
        "real_account_count": 0, "by_provider": {}, "missing_provider": 0,
        "freshest_last_seen_age_s": None,
        "recent_attributed": 0, "recent_unattributed": 0,
    }
    try:
        ident = _cctally_core._resolve_active_claude_identity()
        state["claude_identity_status"] = ident.get("status")
        state["claude_email"] = ident.get("email")
    except Exception:
        pass
    if not _cctally_core.DB_PATH.exists():
        return state
    try:
        # #386: a `mode=ro` reader participates in the opener protocol too —
        # measured, it CREATES `-shm`/`-wal` when they are absent. See
        # `_cctally_store.stats_open_guarded`.
        conn = _stats_ro_guarded()
    except sqlite3.Error:
        return state
    try:
        try:
            rows = conn.execute(
                "SELECT provider, account_key, last_seen_utc FROM accounts"
            ).fetchall()
        except sqlite3.DatabaseError:
            # OperationalError (table missing) OR a corrupt / non-DB file
            # ("file is not a database") -> degrade to the empty (all-OK) shape.
            rows = []
        by_provider: dict = {}
        missing = 0
        freshest = None
        for provider, account_key, last_seen in rows:
            if not provider:
                missing += 1
            if account_key and account_key != "unattributed":
                p = provider or "?"
                by_provider[p] = by_provider.get(p, 0) + 1
            if last_seen:
                try:
                    ls = parse_iso_datetime(
                        last_seen, "accounts.last_seen_utc"
                    ).astimezone(dt.timezone.utc)
                    if freshest is None or ls > freshest:
                        freshest = ls
                except Exception:
                    pass
        state["by_provider"] = by_provider
        state["real_account_count"] = sum(by_provider.values())
        state["missing_provider"] = missing
        if freshest is not None:
            state["freshest_last_seen_age_s"] = max(
                0, int((now_utc - freshest).total_seconds()))
        cutoff = (now_utc - dt.timedelta(days=7)).astimezone(
            dt.timezone.utc).isoformat()
        try:
            for account_key, cnt in conn.execute(
                "SELECT account_key, COUNT(*) FROM weekly_usage_snapshots "
                "WHERE captured_at_utc >= ? GROUP BY account_key", (cutoff,)
            ).fetchall():
                if account_key and account_key != "unattributed":
                    state["recent_attributed"] += int(cnt)
                else:
                    state["recent_unattributed"] += int(cnt)
        except sqlite3.DatabaseError:
            pass
    finally:
        conn.close()
    return state


def doctor_gather_state(
    *,
    now_utc: "dt.datetime | None" = None,
    runtime_bind: "str | None" = None,
    deep: bool = False,
):
    """Gather doctor state while excluding cache-family replacement.

    Doctor is read-only, so it never creates the maintenance lock.  When the
    lock already exists it holds maintenance-shared from the marker check
    through every cache probe.  A live/stale marker or a pending quarantine
    suppresses all raw SQLite cache opens; when a cache exists but its lock is
    absent, probes also degrade rather than racing a newly starting repair.
    """
    cache_lock = None
    cache_probe_allowed = not _cctally_core.CACHE_DB_PATH.exists()
    cache_repair_marker = {
        "exists": False,
        "live": None,
        "reason": None,
    }
    try:
        lock_path = _cctally_core.CACHE_LOCK_MAINTENANCE_PATH
        if lock_path.exists():
            cache_lock = open(lock_path, "r")
            fcntl.flock(cache_lock, fcntl.LOCK_SH)
            cache_probe_allowed = True

        c = _cctally()
        db_mod = c._load_sibling("_cctally_db")
        repair_marker = db_mod._repair_marker_path(
            _cctally_core.CACHE_DB_PATH
        )
        pending = db_mod._quarantine_pending_path(
            _cctally_core.CACHE_DB_PATH
        )
        if repair_marker.exists():
            live, reason = db_mod._repair_marker_is_live(repair_marker)
            cache_repair_marker = {
                "exists": True,
                "live": live,
                "reason": reason,
            }
            cache_probe_allowed = False
        elif pending.exists():
            cache_repair_marker = {
                "exists": True,
                "live": False,
                "reason": "interrupted quarantine is pending",
            }
            cache_probe_allowed = False

        if not cache_probe_allowed and cache_lock is not None:
            fcntl.flock(cache_lock, fcntl.LOCK_UN)
            cache_lock.close()
            cache_lock = None

        import _cctally_store

        with _cctally_store.suppress_interrupted_stats_recovery():
            return _doctor_gather_state_impl(
                now_utc=now_utc,
                runtime_bind=runtime_bind,
                deep=deep,
                _cache_probe_allowed=cache_probe_allowed,
                _cache_repair_marker=cache_repair_marker,
            )
    finally:
        if cache_lock is not None:
            try:
                fcntl.flock(cache_lock, fcntl.LOCK_UN)
            finally:
                cache_lock.close()


def _doctor_gather_state_impl(
    *,
    now_utc: "dt.datetime | None" = None,
    runtime_bind: "str | None" = None,
    deep: bool = False,
    _cache_probe_allowed: bool,
    _cache_repair_marker: dict,
):
    """I/O chokepoint for `cctally doctor` (spec §7.2).

    H1 invariant: config.json is read RAW (NOT via load_config), since
    load_config auto-creates the file on first run — a read-only
    diagnostic command must never mutate user state.

    `deep=True` (CLI cmd_doctor only) additionally runs `PRAGMA
    quick_check(1)` on each DB (#279 S2 F5b, #415); the dashboard/TUI callers
    stay `deep=False` — the rebuild loop calls the gather every rebuild
    and quick_check on a large cache.db costs seconds.
    """
    import _lib_doctor

    c = _cctally()
    if now_utc is None:
        now_utc = _now_utc()

    backup_sync_state = _gather_backup_sync_state(
        _cctally_core.APP_DIR,
        probe_time_machine=deep,
    )

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
        import _cctally_store

        interrupted = _cctally_store.stats_interrupted_rebuild_evidence(
            _cctally_core.DB_PATH
        )
        if interrupted is not None and interrupted.get("live") is True:
            stats_db_status = {
                "path": str(_cctally_core.DB_PATH),
                "user_version": 0,
                "registry_size": len(c._STATS_MIGRATIONS),
                "migrations": [],
            }
        else:
            stats_db_status = c._db_status_for(
                _cctally_core.DB_PATH,
                c._STATS_MIGRATIONS,
                "stats.db",
                recover_interrupted_stats=False,
            )
        if not _cctally_core.DB_PATH.exists():
            stats_db_status["_file_exists"] = False
        if interrupted is not None:
            stats_db_status["_interrupted_rebuild"] = interrupted
    except sqlite3.Error as exc:
        stats_db_status = {"path": str(_cctally_core.DB_PATH), "user_version": 0,
                           "registry_size": len(c._STATS_MIGRATIONS),
                           "migrations": [], "_open_error": str(exc)}
    # stats.db is the epoch-versioned journal index (DB journal redesign §7.1):
    # feed the epoch constant to the pure kernel so db.version_ahead classifies
    # uv==1000 as HEALTHY (not a #145 version-ahead FAIL). registry_size stays the
    # frozen legacy head (13) and serves as the legacy-range boundary.
    stats_db_status["epoch"] = _cctally_core.STATS_INDEX_EPOCH
    if _cache_probe_allowed:
        try:
            cache_db_status = c._db_status_for(
                _cctally_core.CACHE_DB_PATH,
                c._CACHE_MIGRATIONS,
                "cache.db",
            )
            if not _cctally_core.CACHE_DB_PATH.exists():
                cache_db_status["_file_exists"] = False
        except sqlite3.Error as exc:
            cache_db_status = {
                "path": str(_cctally_core.CACHE_DB_PATH),
                "user_version": 0,
                "registry_size": len(c._CACHE_MIGRATIONS),
                "migrations": [],
                "_open_error": str(exc),
            }
    else:
        cache_db_status = {
            "path": str(_cctally_core.CACHE_DB_PATH),
            "user_version": 0,
            "registry_size": len(c._CACHE_MIGRATIONS),
            "migrations": [],
            "_open_error": "cache maintenance excludes read probes",
        }
    cache_repair_marker = _cache_repair_marker

    # ── Data freshness ───────────────────────────────────────────────
    latest_snapshot_at = None
    forked_bucket_counts: dict | None = None
    credited_weeks: list[dict] | None = None
    try:
        if _cctally_core.DB_PATH.exists():
            # #386 spec section 3.1, third clause: EVERY opener of the live stats
            # family participates in the replacement protocol, read-only probes
            # included. The open mode stays read-WRITE deliberately — switching a
            # WAL DB whose `-shm` may be absent to `mode=ro` fails
            # SQLITE_CANTOPEN, which is not corruption and has been misread as
            # such on this project twice. Participation, not read-only-ness, is
            # what the clause requires.
            import _cctally_store as _store_mod
            conn = _store_mod.stats_open_guarded(_cctally_core.DB_PATH)
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
    cache_db_page_count = None
    cache_db_freelist_count = None
    try:
        if _cache_probe_allowed and _cctally_core.CACHE_DB_PATH.exists():
            conn = sqlite3.connect(str(_cctally_core.CACHE_DB_PATH))
            try:
                try:
                    row = conn.execute("PRAGMA page_count").fetchone()
                    if row and row[0] is not None:
                        cache_db_page_count = int(row[0])
                    row = conn.execute("PRAGMA freelist_count").fetchone()
                    if row and row[0] is not None:
                        cache_db_freelist_count = int(row[0])
                except sqlite3.Error:
                    pass
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
    # NON-BLOCKING conversations flock probe (a writer mid-walk holds it) OR the
    # presence of any pending reingest/split/backfill cache_meta flag — doctor
    # stays read-only and never blocks on the lock.
    conv_sessions_rollup_count = None
    conv_messages_distinct_sessions = None
    conv_rollup_sync_in_progress = False
    conversations_db_page_count = None
    conversations_db_freelist_count = None
    codex_prune_refusals: list[dict] = []
    try:
        if _cctally_core.CONVERSATIONS_DB_PATH.exists():
            # This gather also runs inside dashboard snapshot precompute. A
            # transcript writer or recovery may hold an exclusive lock, so use
            # the recovery-aware read-only zero-timeout probe: conversation
            # health can degrade, but it must never delay core snapshot
            # freshness (#320, #415).
            with _conversation_ro_guarded(timeout=0.0) as conn:
                if conn is None:
                    raise sqlite3.OperationalError(
                        "conversation store maintenance in progress"
                    )
                try:
                    row = conn.execute("PRAGMA page_count").fetchone()
                    if row and row[0] is not None:
                        conversations_db_page_count = int(row[0])
                    row = conn.execute("PRAGMA freelist_count").fetchone()
                    if row and row[0] is not None:
                        conversations_db_freelist_count = int(row[0])
                except sqlite3.Error:
                    pass
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
                try:
                    import _cctally_cache as _cc_sib
                    row = conn.execute(
                        "SELECT value FROM cache_meta WHERE key=?",
                        (_cc_sib.CODEX_ORPHAN_PRUNE_REFUSED_KEY,),
                    ).fetchone()
                    if row and row[0]:
                        record = json.loads(row[0])
                        if isinstance(record, dict):
                            codex_prune_refusals.append(record)
                except (sqlite3.OperationalError, ValueError, TypeError):
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
        # Non-blocking flock probe: if a transcript writer/reingest holds the
        # conversations.db lock, the rollup may be mid-recompute → in progress. We
        # acquire LOCK_EX|LOCK_NB and immediately release; failure (held) is the
        # signal. Never blocks (LOCK_NB), so doctor stays read-only + prompt.
        if not conv_rollup_sync_in_progress:
            lock_path = _cctally_core.CONVERSATIONS_LOCK_PATH
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
    codex_project_metadata_health = None
    codex_project_metadata_error = None
    codex_null_reset_anchors = 0
    try:
        if _cache_probe_allowed and _cctally_core.CACHE_DB_PATH.exists():
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
                try:
                    row = conn.execute(
                        "SELECT COUNT(*) FROM quota_window_snapshots "
                        "WHERE source = 'codex' "
                        "AND canonical_resets_at_utc IS NULL"
                    ).fetchone()
                    if row and row[0] is not None:
                        codex_null_reset_anchors = int(row[0])
                except sqlite3.OperationalError:
                    # Pre-anchor cache shapes have no column to inspect. Their
                    # pending migration is reported by the DB checks instead.
                    pass
                # Keep the health probe on the existing read-only cache
                # connection.  A failed probe is health evidence, not an
                # empty corpus: the kernel renders it as a distinct FAIL.
                try:
                    import _cctally_source_analytics

                    health = _cctally_source_analytics.load_codex_project_metadata_health(
                        cache_conn=conn,
                    )
                    codex_project_metadata_health = {
                        "total_rows": health.total_rows,
                        "qualified_rows": health.qualified_rows,
                        "missing_conversation_key_rows": health.missing_conversation_key_rows,
                        "missing_thread_join_rows": health.missing_thread_join_rows,
                    }
                except Exception as exc:
                    codex_project_metadata_error = type(exc).__name__
            except sqlite3.OperationalError as exc:
                # Pre-Codex cache shapes still produce the established Codex
                # cache result, while the new health check fails explicitly.
                codex_project_metadata_error = type(exc).__name__
            finally:
                conn.close()
    except Exception as exc:
        codex_project_metadata_error = type(exc).__name__

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
        observations = (
            c._cctally_quota.load_codex_quota_observations()
            if _cache_probe_allowed
            else ()
        )
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

    try:
        codex_quota_verify_activity = _codex_quota_verify_activity_24h(
            now_utc=now_utc)
    except Exception:
        codex_quota_verify_activity = None

    # ── Parse health (#279 S2 F5a) ───────────────────────────────────
    parse_health_claude = parse_health_codex = None
    # #416 review B4: the durable record that a torn Codex `auth.json` halted
    # ingest. Same cache_meta read, same degrade-to-None-on-anything contract.
    codex_torn_deferred = None
    # The byte-zero Codex replay stall signal. The marker itself is a bare "1";
    # the sibling `blocked` record is the JSON one, so it is read through the
    # same loop while the marker gets a plain existence probe. Key names come
    # from the kernel constants, never inline literals.
    codex_replay_pending = None
    codex_replay_blocked = None
    # public #5: the budgeted-decline record. Same JSON-dict contract as the
    # blocked one, and the only signal a hook-only install produces when its
    # Codex ingest is frozen behind an un-runnable replay.
    codex_replay_deferred = None
    # public #5 spec §5: the hook's budgeted-ingest backlog record. Absent means
    # a zero backlog — a drained walk DELETES the row rather than zeroing it, so
    # None and "nothing owed" are the same state by construction.
    codex_ingest_backlog = None
    try:
        import _lib_codex_conversation as _codex_kern
        _blocked_key = _codex_kern.CODEX_REPLAY_BLOCKED_KEY
        _pending_key = _codex_kern.CODEX_REPLAY_FROM_ZERO_KEY
        _deferred_key = _codex_kern.CODEX_REPLAY_DEFERRED_KEY
    except Exception:
        _blocked_key = "codex_replay_from_zero_blocked"
        _pending_key = "codex_replay_from_zero_pending"
        _deferred_key = "codex_replay_from_zero_deferred"
    try:
        if _cache_probe_allowed and _cctally_core.CACHE_DB_PATH.exists():
            conn = sqlite3.connect(str(_cctally_core.CACHE_DB_PATH))
            try:
                for _key in ("parse_health_claude", "parse_health_codex",
                             "codex_torn_auth_deferred", _blocked_key,
                             _deferred_key, "codex_ingest_backlog",
                             "codex_orphan_prune_refused"):
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
                                elif _key == "parse_health_codex":
                                    parse_health_codex = _parsed
                                elif _key == _blocked_key:
                                    codex_replay_blocked = _parsed
                                elif _key == _deferred_key:
                                    codex_replay_deferred = _parsed
                                elif _key == "codex_ingest_backlog":
                                    codex_ingest_backlog = _parsed
                                elif _key == "codex_orphan_prune_refused":
                                    codex_prune_refusals.append(_parsed)
                                else:
                                    codex_torn_deferred = _parsed
                    except (sqlite3.OperationalError, ValueError):
                        pass
                try:
                    codex_replay_pending = conn.execute(
                        "SELECT 1 FROM cache_meta WHERE key = ?",
                        (_pending_key,),
                    ).fetchone() is not None
                except sqlite3.OperationalError:
                    pass
            finally:
                conn.close()
    except Exception:
        pass

    # ── Integrity (deep only — #279 S2 F5b) ──────────────────────────
    stats_db_quick_check = cache_db_quick_check = None
    conversations_db_quick_check = None
    if deep:
        for _label, _path in (("stats", _cctally_core.DB_PATH),
                              ("cache", _cctally_core.CACHE_DB_PATH),
                              ("conversations",
                               _cctally_core.CONVERSATIONS_DB_PATH)):
            _result = None
            try:
                if (
                    (
                        _path.exists()
                        or (
                            _label == "conversations"
                            and _path.with_name(
                                f"{_path.name}.recovery.json"
                            ).exists()
                        )
                    )
                    and (_label != "cache" or _cache_probe_allowed)
                ):
                    # #386: the stats leg holds a read-write handle for the whole
                    # of a full quick_check — the longest-lived stats handle any
                    # diagnostic takes — so it participates in the replacement
                    # protocol. The cache leg keeps its own opener.
                    if _label == "stats":
                        import _cctally_store as _store_mod
                        _conn_ctx = contextlib.closing(
                            _store_mod.stats_open_guarded(_path)
                        )
                    elif _label == "cache":
                        _conn_ctx = contextlib.closing(
                            sqlite3.connect(str(_path))
                        )
                    else:
                        _conn_ctx = _conversation_ro_guarded(timeout=2.0)
                    with _conn_ctx as _conn:
                        if _conn is not None:
                            _row = _conn.execute(
                                "PRAGMA quick_check(1)").fetchone()
                            _result = (
                                str(_row[0])
                                if _row and _row[0] is not None else None
                            )
                        elif (
                            _label == "conversations"
                            and _path.with_name(
                                f"{_path.name}.recovery.json"
                            ).exists()
                        ):
                            _result = "recovery in progress"
            except sqlite3.DatabaseError as exc:
                _result = f"open failed: {exc}"
            except Exception:
                _result = None
            if _label == "stats":
                stats_db_quick_check = _result
            elif _label == "cache":
                cache_db_quick_check = _result
            else:
                conversations_db_quick_check = _result

    # ── Lock state (#279 S2 F5c) — read-only: never create files ─────
    locks_held: "dict | None" = None
    try:
        locks_held = {}
        for _name, _lp in (
            ("cache.db.lock", _cctally_core.CACHE_LOCK_PATH),
            ("cache.db.codex.lock", _cctally_core.CACHE_LOCK_CODEX_PATH),
            ("conversations.db.lock", _cctally_core.CONVERSATIONS_LOCK_PATH),
            (
                "conversations.db.codex.lock",
                _cctally_core.CONVERSATIONS_LOCK_CODEX_PATH,
            ),
            (
                "conversations.db.maintenance.lock",
                _cctally_core.CONVERSATIONS_LOCK_MAINTENANCE_PATH,
            ),
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
    config_parsed: dict = {}
    try:
        if _cctally_core.CONFIG_PATH.exists():
            config_parsed = json.loads(
                _cctally_core.CONFIG_PATH.read_text(encoding="utf-8")
            )
    except json.JSONDecodeError as exc:
        config_json_error = f"{type(exc).__name__}: {exc}"
    except OSError as exc:
        config_json_error = f"OSError: {exc}"

    # Configured update (release) channel (beta-channel, spec 2026-07-21 §3):
    # derived from the SAME raw read (never load_config, which auto-creates on
    # first run). Fail-soft to "stable" — resolve_update_channel already
    # tolerates a non-dict block / junk value.
    try:
        update_channel = c.resolve_update_channel(
            config_parsed if isinstance(config_parsed, dict) else {}
        )
    except Exception:
        update_channel = "stable"

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
    if _cache_probe_allowed:
        try:
            observed = c._pricing_observed_models(now_utc)
            # Detection-only: pass warn=False so finding an unpriced model here
            # does NOT fire the cost-engine's unknown-model warning.
            pricing_coverage = c.classify_coverage(
                observed,
                lambda m: c._resolve_model_pricing(m, warn=False),
                c._is_codex_fallback,
            )
        except Exception:
            pricing_coverage = None

    # ── Meta ─────────────────────────────────────────────────────────
    # ── Journal (DB journal redesign §9) ─────────────────────────────
    # Read-only legs over the append-only journal: presence + appendability,
    # torn-tail/malformed counts (deep-gated — reads whole segments), the ingest
    # cursor lag vs. the high-water, and the auto-heal incident history. Every
    # probe degrades to its always-OK posture on any error (the pure kernel then
    # reports "no journal" / "not scanned").
    import _lib_journal as _jl
    journal_present = False
    journal_appendable = None
    journal_segment_count = 0
    journal_has_bytes = False
    journal_malformed_count = None
    journal_torn_tail_count = None
    journal_cursor_lag_bytes = None
    journal_hw_segment = None
    journal_cursor_segment = None
    journal_conflicts = None
    journal_protocol_violations = None
    journal_protocol_acknowledged = None
    journal_protocol_error = None
    try:
        jdir = _cctally_core.JOURNAL_DIR
        journal_present = jdir.exists()
        if journal_present:
            try:
                journal_appendable = os.access(str(jdir), os.W_OK)
            except OSError:
                journal_appendable = None
            import _cctally_journal as _jr
            try:
                segs = _jr.list_segments()  # canonical (segment) order
            except Exception:
                segs = []
            journal_segment_count = len(segs)
            # #402: the disposable stats index persists the most recent complete
            # selector result. Shallow Dashboard/TUI gathers read that bounded
            # summary instead of rescanning a production-sized journal and
            # therefore cannot turn known taint into a false OK.
            try:
                if _cctally_core.DB_PATH.exists():
                    pc = _stats_ro_guarded()
                    try:
                        protocol_rows = [
                            json.loads(str(row[0]))
                            for row in pc.execute(
                                "SELECT violation_json "
                                "FROM journal_protocol_violations "
                                "ORDER BY batch_id, kind, fingerprint"
                            )
                        ]
                        journal_protocol_violations = [
                            item for item in protocol_rows
                            if not item.get("auditId")
                        ]
                        journal_protocol_acknowledged = [
                            item for item in protocol_rows
                            if item.get("auditId")
                        ]
                    finally:
                        pc.close()
            except (sqlite3.Error, ValueError, TypeError):
                journal_protocol_violations = None
                journal_protocol_acknowledged = None
            sizes: dict = {}
            for seg in segs:
                try:
                    sizes[seg] = (jdir / seg).stat().st_size
                except OSError:
                    sizes[seg] = 0
            if segs:
                journal_hw_segment = segs[-1]
                journal_has_bytes = _jr._has_retained_journal_bytes(
                    sizes.values()
                )
            # deep-gated malformed / torn-tail scan (reads the whole journal;
            # the dashboard's per-rebuild gather stays deep=False so it never
            # pays this at the 10× envelope — mirrors the quick_check legs).
            if deep and segs:
                malformed = 0
                torn = 0
                decoded_records: list = []
                protocol_evidence = []
                prior_high_water = None
                cutover_value = None
                for seg in segs:
                    try:
                        data = (jdir / seg).read_bytes()
                    except OSError:
                        continue
                    if not data:
                        continue
                    if not data.endswith(b"\n"):
                        torn += 1
                    # every element except the last is a complete line; the last
                    # is either "" (ended in \n) or the torn partial — not a
                    # mid-file line, so it is never counted as malformed.
                    offset = 0
                    for raw in data.split(b"\n")[:-1]:
                        if not raw:
                            prior_high_water = (seg, offset + 1)
                            offset += 1
                            continue
                        record = _jl.decode_line(raw)
                        if record is None:
                            malformed += 1
                            prior_high_water = (
                                seg,
                                offset + len(raw) + 1,
                            )
                            offset += len(raw) + 1
                            continue
                        _jr._capture_protocol_prefix_evidence(
                            record,
                            prior_high_water,
                            protocol_evidence,
                        )
                        # first cutover op wins, exactly as
                        # `find_accounts_cutover_op` scans — captured here so the
                        # conflict scan does not decode the whole journal twice.
                        if (cutover_value is None
                                and record.get("id") == _jr.CUTOVER_OP_ID):
                            payload = record.get("payload")
                            if isinstance(payload, dict):
                                cutover_value = payload.get(
                                    "claude_legacy_account")
                        # RETAIN ONLY what the selector consumes. `obs` lines are
                        # ~97% of a real journal (984k of 1.02M) and
                        # `resolve_effective_events` ignores them entirely —
                        # keeping their dictionaries cost 4.3 GB of peak RSS for
                        # an identical result (#374 review). They still consume a
                        # lightweight slot because their physical sequence is
                        # part of three durable violation fingerprints (#508).
                        decoded_records.append(
                            _lib_journal_router.selector_slot(record)
                        )
                        prior_high_water = (
                            seg,
                            offset + len(raw) + 1,
                        )
                        offset += len(raw) + 1
                journal_malformed_count = malformed
                journal_torn_tail_count = torn
                # #374: same-revision quarantine, via the SHARED selector over
                # rebuild-equivalent input. Raw `(id, rev)` grouping would report
                # lower-revision groups a completed rev-1 batch legitimately
                # superseded, and false account conflicts that the rebuild's
                # `_normalize_legacy_account_stamp` resolves — so normalize
                # exactly as `rebuild_stats_index` does, then select.
                try:
                    cutover_claude = (
                        cutover_value if cutover_value is not None
                        else _jr.resolve_cutover_claude_account()
                    )
                    for record in decoded_records:
                        if record is not None:
                            _jr._normalize_legacy_account_stamp(
                                record, cutover_claude)
                    selection = _jl.resolve_effective_events(
                        decoded_records,
                        protocol_prefix_evidence=protocol_evidence,
                    )
                except _jl.JournalProtocolError as exc:
                    # Out-of-scope malformed known record: selection did not
                    # finish, so conflicts/tainted-batch results are unavailable.
                    journal_protocol_error = str(exc)
                    journal_conflicts = None
                    journal_protocol_violations = None
                    journal_protocol_acknowledged = None
                except Exception:
                    journal_conflicts = None
                    journal_protocol_violations = None
                    journal_protocol_acknowledged = None
                else:
                    journal_conflicts = [
                        conflict.to_dict() for conflict in selection.conflicts
                    ]
                    journal_protocol_violations = [
                        violation.to_dict()
                        for violation in selection.protocol_violations
                    ]
                    journal_protocol_acknowledged = [
                        violation.to_dict()
                        for violation in (
                            selection.acknowledged_protocol_violations
                        )
                    ]
            # ingest cursor lag: unconsumed bytes between the stats index cursor
            # and the journal high-water, in canonical (segment, offset) order.
            cursor = None
            try:
                if _cctally_core.DB_PATH.exists():
                    jc = _stats_ro_guarded()   # #386 opener protocol
                    try:
                        cursor_columns = {
                            str(row[1])
                            for row in jc.execute(
                                "PRAGMA table_info(journal_cursor)"
                            )
                        }
                        if {
                            "applied_segment", "applied_offset"
                        } <= cursor_columns:
                            crow = jc.execute(
                                "SELECT segment, offset, applied_segment, "
                                "applied_offset FROM journal_cursor "
                                "WHERE id = 1").fetchone()
                            if (
                                crow is not None
                                and crow[2] is not None
                                and crow[3] is not None
                            ):
                                cursor = (crow[2], int(crow[3]))
                        else:
                            legacy = jc.execute(
                                "SELECT segment, offset FROM journal_cursor "
                                "WHERE id = 1").fetchone()
                            if legacy is not None:
                                cursor = (legacy[0], int(legacy[1]))
                    except sqlite3.OperationalError:
                        pass  # pre-cutover DB has no journal_cursor table
                    finally:
                        jc.close()
            except sqlite3.Error:
                cursor = None
            if cursor is not None and segs:
                cseg, coff = cursor
                journal_cursor_segment = cseg
                order = {s: i for i, s in enumerate(segs)}
                if cseg in order:
                    ci = order[cseg]
                    lag = max(0, sizes.get(segs[ci], 0) - coff)
                    for s in segs[ci + 1:]:
                        lag += sizes.get(s, 0)
                    journal_cursor_lag_bytes = lag
    except Exception:
        pass
    # #496 S5b: the durable incomplete-quota-projection flag carried inside the
    # published stats generation. Read-only, and independent of journal presence
    # because the flag describes the INDEX rather than the journal. None means
    # "no epoch-1009 index to ask" (absent file, missing table, unreadable DB),
    # which the pure kernel reports as not applicable rather than as a fault.
    #
    # This is a THIRD read-only stats open in this function, and folding it into
    # the `jc` open above was considered and rejected: that open sits inside
    # `if journal_present:`, so carrying this SELECT there would make the flag
    # unreadable on an install whose journal directory is absent — exactly the
    # independence the paragraph above states. One extra guarded open on the
    # doctor path is the cheaper of the two.
    stats_quota_projection_incomplete: "bool | None" = None
    try:
        if _cctally_core.DB_PATH.exists():
            qp = _stats_ro_guarded()   # #386 opener protocol
            try:
                row = qp.execute(
                    "SELECT incomplete FROM stats_quota_projection_state "
                    "WHERE id = 1").fetchone()
                if row is not None:
                    stats_quota_projection_incomplete = bool(int(row[0] or 0))
            except sqlite3.OperationalError:
                pass  # pre-1009 index has no stats_quota_projection_state
            finally:
                qp.close()
    except Exception:
        stats_quota_projection_incomplete = None

    # Auto-heal incident history — independent of journal presence (a corruption
    # incident can predate cutover). None only if BOTH dirs were unreadable.
    journal_heal_incidents = None
    _incidents: list = []
    _incident_read_ok = False
    try:
        qroot = _cctally_core.APP_DIR / "quarantine"
        if qroot.exists():
            _incident_read_ok = True
            for entry in qroot.iterdir():
                if entry.is_dir():
                    record = _journal_heal_incident(
                        "quarantine", entry.name, now_utc)
                    # §7.2 escalates on a REPEATED damage shape, so the shape
                    # has to travel with the incident it belongs to — counted
                    # once per incident, never once per manifest read.
                    record["shape"] = _incident_shape_token(entry)
                    _incidents.append(record)
    except OSError:
        pass
    try:
        logdir = _cctally_core.LOG_DIR
        if logdir.exists():
            _incident_read_ok = True
            for entry in logdir.iterdir():
                n = entry.name
                if "-corruption-forensics-" in n and n.endswith(".json"):
                    _incidents.append(
                        _journal_heal_incident("forensics", n, now_utc))
    except OSError:
        pass
    if _incident_read_ok:
        # most-recent first; unparseable ages (None) sort last.
        _incidents.sort(key=lambda d: (d["age_s"] is None,
                                       d["age_s"] if d["age_s"] is not None else 0))
        journal_heal_incidents = _incidents

    # #496 S6 §7.2 / §7.3. Both are read-only and take no lock; both degrade to
    # None rather than failing the gather, because `doctor` is reached from the
    # TUI and the dashboard snapshot precompute as well as from the CLI.
    journal_heal_detections = _gather_heal_detections(now_utc)
    retained_artifacts = _gather_retained_artifacts(now_utc, deep=deep)

    # #386/#389 stats sole-writer guard log (spec §6.4). Read-only, fail-soft: an
    # absent log is the NORMAL state and must read as INFO, never as a gather
    # failure. Read only the bounded tail; rotation and cross-process throttling
    # bound the writer side independently.
    journal_writer_guard = None
    try:
        journal_writer_guard = _gather_writer_guard_log(now_utc)
    except (OSError, Exception):
        journal_writer_guard = None

    cctally_version_tuple = _lib_changelog._read_latest_changelog_version()
    cctally_version = (
        cctally_version_tuple[0] if cctally_version_tuple else "unknown"
    )
    accounts_state = _gather_accounts_state(now_utc)
    accounts_state["codex_null_reset_anchors"] = codex_null_reset_anchors

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
        codex_project_metadata_health=codex_project_metadata_health,
        codex_project_metadata_error=codex_project_metadata_error,
        update_channel=update_channel,
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
        codex_torn_deferred=codex_torn_deferred,
        codex_ingest_backlog=codex_ingest_backlog,
        codex_replay_pending=codex_replay_pending,
        codex_replay_blocked=codex_replay_blocked,
        codex_replay_deferred=codex_replay_deferred,
        codex_prune_refusals=codex_prune_refusals or None,
        stats_db_quick_check=stats_db_quick_check,
        cache_db_quick_check=cache_db_quick_check,
        conversations_db_quick_check=conversations_db_quick_check,
        locks_held=locks_held,
        # #297: cache.db WAL size backstop (gathered outside the deep branch).
        cache_db_wal_bytes=cache_db_wal_bytes,
        # #374: quarantined same-revision groups + structural protocol violation.
        journal_conflicts=journal_conflicts,
        journal_protocol_violations=journal_protocol_violations,
        journal_protocol_acknowledged=journal_protocol_acknowledged,
        journal_protocol_error=journal_protocol_error,
        # #496 S5b: the published generation's incomplete-quota-projection flag.
        stats_quota_projection_incomplete=stats_quota_projection_incomplete,
        # #315: read-only cache free-page evidence for the reclaim hint.
        cache_db_page_count=cache_db_page_count,
        cache_db_freelist_count=cache_db_freelist_count,
        conversations_db_page_count=conversations_db_page_count,
        conversations_db_freelist_count=conversations_db_freelist_count,
        codex_quota_windows=codex_quota_windows,
        codex_hook_roots=codex_hook_roots,
        codex_lifecycle_activity_24h=codex_lifecycle_activity_24h,
        codex_quota_verify_activity=codex_quota_verify_activity,
        # #311: precomputed statusLine.refreshInterval classification.
        statusline_refresh_state=statusline_refresh_state,
        statusline_pipeline=statusline_pipeline,
        # DB journal redesign §9: append-only journal legs.
        journal_present=journal_present,
        journal_appendable=journal_appendable,
        journal_segment_count=journal_segment_count,
        journal_has_bytes=journal_has_bytes,
        journal_malformed_count=journal_malformed_count,
        journal_torn_tail_count=journal_torn_tail_count,
        journal_cursor_lag_bytes=journal_cursor_lag_bytes,
        journal_hw_segment=journal_hw_segment,
        journal_cursor_segment=journal_cursor_segment,
        journal_heal_incidents=journal_heal_incidents,
        journal_heal_detections=journal_heal_detections,
        retained_artifacts=retained_artifacts,
        journal_writer_guard=journal_writer_guard,
        # Multi-account attribution legs (#341).
        accounts_state=accounts_state,
        cache_repair_marker=cache_repair_marker,
        backup_sync_state=backup_sync_state,
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
        print(encode_dashboard_json(
            _lib_doctor.serialize_json(report), indent=2, sort_keys=True,
        ))
    else:
        sys.stdout.write(_lib_doctor.render_text(
            report, quiet=quiet, verbose=verbose,
        ))
    return 2 if report.overall_severity == "fail" else 0
