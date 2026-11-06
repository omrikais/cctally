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
import re
import shutil
import sqlite3
import subprocess
import sys

import _cctally_core
import _lib_changelog
import _lib_journal_router
import _lib_perf
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

#: #1005: the total time one gather may spend waiting on maintenance locks
#: (every stats open, the open-time guard, and the cache-maintenance
#: admission), drawn through `_cctally_store.bounded_diagnostic_waits`. Large
#: enough to absorb a handshake-scale hold, small enough that a held lock is
#: reported at once instead of costing a full 5 s per independent open.
_DOCTOR_MAINTENANCE_WAIT_ALLOWANCE_S = 0.5

#: #1018: the stats epoch that added `claude_weekly_observation_index`
#: (#1015). Below it an absent table is expected; at or above it the table is
#: required, so its absence makes freshness unreadable rather than silent.
_OBSERVATION_INDEX_EPOCH = 1022


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
        # #778: forward the opener's `cached_statements=0`.
        connect=lambda p, **kw: sqlite3.connect(
            f"file:{p}?mode=ro", uri=True, **kw),
    )


def _conversation_ro_guarded(*, timeout: float):
    """Bounded read-only transcript handle participating in #415 recovery.

    #1000 §5.10: the admission now lives in
    ``_cctally_cache.conversation_ro_guarded``, shared with `db status` and
    the `explain` reader. Doctor keeps this name for its call sites (and the
    tests that patch it), and reaches the sibling call-time, as its other
    sibling imports do (F1), because doctor loads before `_cctally_cache`.
    """
    import _cctally_cache

    return _cctally_cache.conversation_ro_guarded(timeout=timeout)


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


#: Bounded per-instance probe of a running dashboard's write telemetry.
DASHBOARD_WRITE_IO_TIMEOUT_SECONDS = 2.0


def _fetch_dashboard_write_io(descriptor: dict, *, timeout_s: float) -> dict:
    """One loopback probe of a discovered dashboard (#901 §5.6).

    Reuses `dashboard-perf`'s client — IP-literal loopback target, an
    `Origin` matching the `Host` — and sends NO token, so an instance that
    requires one answers 401 and is reported `authentication_required`: no
    authentication bypass exists. The instance id in the answer must equal
    the descriptor's, or the probe is an `identity_mismatch`."""
    import _lib_write_io as wio

    perf = _cctally()._cctally_dashboard_perf
    base = {"instance_id": descriptor["instanceId"],
            "port": descriptor["port"], "pid": descriptor["pid"]}
    host = wio.loopback_target(descriptor.get("host"))
    if host is None:
        return {**base, "probe": "failed", "reason": "not_loopback_reachable",
                "write_io": None}
    try:
        payload = perf._request(host, descriptor["port"], perf.WRITE_IO_PATH,
                                token=None, timeout=timeout_s)
    except perf.DashboardPerfError as exc:
        return {**base, "probe": "failed", "reason": exc.reason,
                "write_io": None}
    write_io = payload.get("writeIo") if isinstance(payload, dict) else None
    if not isinstance(write_io, dict):
        return {**base, "probe": "failed", "reason": "malformed_response",
                "write_io": None}
    if write_io.get("instanceId") != descriptor["instanceId"]:
        return {**base, "probe": "failed", "reason": "identity_mismatch",
                "write_io": None}
    return {**base, "probe": "ok", "reason": None, "write_io": write_io}


_REBUILD_WORKERS = ("epoch", "heal")


def _rebuild_worker_gather_failed(worker: str, exc: BaseException) -> dict:
    """A snapshot that could not be gathered reads as an unreadable state
    (the existing row), with the error in `details`, never as "has run"."""
    return {"worker": worker, "state": None, "stateUnreadable": True,
            "ledger": None, "ledgerUnreadable": False, "identities": {},
            "pending": {}, "reread": False,
            "gatherError": f"{type(exc).__name__}: {exc}"}


def _gather_rebuild_workers() -> dict:
    """#997 D10: each detached rebuild worker's read-only snapshot; a worker
    whose snapshot fails is reported unreadable, the other still gathered."""
    out: dict = {}
    for worker in _REBUILD_WORKERS:
        try:
            import _cctally_store
            out[worker] = _cctally_store.rebuild_worker_snapshot(worker)
        except Exception as exc:  # noqa: BLE001 — reported, never fatal
            out[worker] = _rebuild_worker_gather_failed(worker, exc)
    return out


def _gather_dashboard_disk_writes(
    *, fetch=None, timeout_s: float = DASHBOARD_WRITE_IO_TIMEOUT_SECONDS,
    discover: bool = True,
) -> dict:
    """Running dashboards' write telemetry for `performance.dashboard_disk_
    writes` (#901 §5.6).

    In the dashboard process the local instance's own snapshot is read
    directly — no self-HTTP and no recursive gather. Anywhere else (the CLI,
    the TUI) instances are discovered through their owner-readable
    descriptors; a stale one (dead or reused pid) is ignored and never
    deleted, because doctor never changes your data. Discovery runs only for the
    `cctally doctor` CLI (PR-11); with ``discover`` false nothing is probed and
    the mode is ``not_probed``."""
    import _lib_write_io as wio

    local = wio.local_instance()
    if local is not None:
        return {"mode": "in_process", "instances": [{
            "instance_id": local["instanceId"], "port": local["port"],
            "pid": local["pid"], "probe": "ok", "reason": None,
            "write_io": wio.write_io_payload(),
        }]}
    if not discover:
        return {"mode": "not_probed", "instances": []}
    probe = fetch or _fetch_dashboard_write_io
    instances = [
        probe(descriptor, timeout_s=timeout_s)
        for descriptor in wio.read_instance_descriptors(_cctally_core.APP_DIR)
        if wio.descriptor_is_live(descriptor)
    ]
    instances.sort(key=lambda entry: (entry["port"], entry["instance_id"]))
    return {"mode": "discovery", "instances": instances}


def _gather_legacy_writer(deep_excluded, deep_boundary,
                          deep_error=None) -> dict:
    """The `journal.legacy_writer` evidence (#825/#832 spec section 8).

    Reads the admission summary on a validated current snapshot (an
    unvalidated or missing summary is unavailable, never zero — the durable
    evidence sidecar is still returned beside it, for the leg to report from)
    and the durable evidence sidecar. A deep run substitutes its own whole-journal
    recomputation of the excluded population; ``deep_error`` names why that
    recomputation could not complete (fix round K11, K9K10-R6/R11), and then
    the count is unavailable — never zero.

    Spec revision 5: the leg also reports an incomplete legacy spool
    inventory (``legacySpoolInventory``), independently of the count, and
    says the detection pause exists only when the validated index holds the
    durable `inventory-incomplete` marker. Read-only: the sidecar is never
    written here, the spool is only read, and nothing is journaled."""
    import _cctally_journal
    import _cctally_store
    import _lib_account_protocol

    out: dict = {"available": False}
    row = None
    inventory_marker = False
    try:
        if _cctally_core.DB_PATH.exists():
            pc = _stats_ro_guarded()
            try:
                with _cctally_store.current_stats_snapshot(pc):
                    row = pc.execute(
                        "SELECT cutover_id, excluded_total, coordinate_digest "
                        "FROM journal_admission_summary WHERE id = 1"
                    ).fetchone()
                    inventory_marker = _durable_inventory_marker(pc)
            finally:
                pc.close()
    except Exception:  # noqa: BLE001 — a diagnostic read degrades to unavailable
        row = None
    # #797/#823/#840 review RV-5: the v2 sidecar, else the previous
    # release's v1 one (read only) as its seed.
    evidence, _seeded = _cctally_journal.read_legacy_writer_evidence_document()
    inventory = _legacy_spool_inventory_probe(inventory_marker)
    if inventory is not None:
        out["legacySpoolInventory"] = inventory
    if deep_error is not None:
        return {**out, "unavailableCause": str(deep_error),
                "evidence": evidence}
    if deep_excluded is not None:
        cutover_id = (None if deep_boundary is None
                      else _lib_account_protocol.CUTOVER_OP_ID)
        count = len(deep_excluded)
        digest = (_lib_account_protocol.coordinate_population_digest(
            item.coordinate for item in deep_excluded) if count else None)
    elif row is not None:
        cutover_id, count, digest = row[0], int(row[1] or 0), row[2]
    else:
        # No current summary — exactly what an older process's epoch
        # republish leaves behind. The durable sidecar still carries the
        # excluded population and when it was first observed (#825/#832 fix
        # round B, P2-2), so the leg reports from it rather than "unavailable".
        return {**out, "evidence": evidence}
    return {**out, "available": True, "cutoverId": cutover_id,
            "excludedCount": count, "coordinateDigest": digest,
            "evidence": evidence}


def _scan_cause(exc: BaseException) -> str:
    """A journal read failure as a doctor cause (K9K10-R6): the message and,
    for an OS error, its errno name — never an empty string."""
    import errno as _errno
    name = _errno.errorcode.get(getattr(exc, "errno", None) or 0)
    text = getattr(exc, "strerror", None) or str(exc) or type(exc).__name__
    filename = getattr(exc, "filename", None)
    if filename:
        text = f"{text}: {filename}"
    return f"{text} [{name}]" if name else text


# ── #1054: the deep journal scan's shared per-record step ────────────────
#
# The deep scan reads every segment and, for every decoded record, runs the
# steps below in this order. A checkpointed run (`.doctor-scan-index`, see
# `bin/_lib_doctor_scan_index.py`) runs them only for the records that
# *matter* and fills every other position with the placeholder the step
# itself would have appended. That is exact only because both paths call ONE
# step function: a change to the step, to `_deep_scan_matters` or to any
# descriptor input must bump `_lib_doctor_scan_index.SHAPE_VERSION` unless
# `_deep_scan_index_descriptors` already captures it (docs/journal-gotchas.md).

#: The record ids the step acts on regardless of record type: the legacy
#: accounts cutover (doctor's cutover-candidate capture) and the account-bound
#: cutover and its two revisions (`_PassBoundary.observe`). Resolved on first
#: use because `_cctally_journal` is imported call-time.
_DEEP_SCAN_SPECIAL_IDS = None


def _deep_scan_special_ids() -> frozenset:
    global _DEEP_SCAN_SPECIAL_IDS
    if _DEEP_SCAN_SPECIAL_IDS is None:
        import _cctally_journal as _jr
        import _lib_account_protocol
        _DEEP_SCAN_SPECIAL_IDS = frozenset({
            _jr.CUTOVER_OP_ID,
            _lib_account_protocol.CUTOVER_OP_ID,
            _lib_account_protocol.REVISION_OP_ID,
            _lib_account_protocol.REVISION3_OP_ID,
        })
    return _DEEP_SCAN_SPECIAL_IDS


def _deep_scan_matters(record: dict) -> bool:
    """Whether the deep scan's step can do anything for ``record`` beyond
    appending a placeholder: a record the selector retains, or one carrying
    a special id. `id` is checked to be a string before set membership, so
    an unhashable id is simply not special."""
    if record.get("t") in _lib_journal_router.RETAINED_RECORD_TYPES:
        return True
    rid = record.get("id")
    if not isinstance(rid, str):
        return False
    special = _DEEP_SCAN_SPECIAL_IDS
    if special is None:
        special = _deep_scan_special_ids()
    return rid in special


class _DeepScanState:
    """The deep scan's per-pass accumulators, shared by the fresh and the
    checkpointed path. `decoded_records` and `deep_coordinates` are
    positionally parallel: one slot and one coordinate per decoded record,
    the coordinate None for a record that does not matter (its slot is None
    too, so `_apply_pass_admission` skips it either way)."""

    __slots__ = ("pass_boundary", "decoded_records", "deep_coordinates",
                 "protocol_evidence", "protocol_evidence_owners",
                 "cutover_candidates", "_capture_evidence", "_cutover_id",
                 "_observe", "_append_slot", "_append_coordinate",
                 "_append_owner", "_append_candidate", "_selector_slot",
                 "_special_ids")

    def __init__(self):
        import _cctally_journal as _jr
        self.pass_boundary = _jr._PassBoundary()
        self.decoded_records: list = []
        self.deep_coordinates: list = []
        self.protocol_evidence: list = []
        self.protocol_evidence_owners: list = []
        self.cutover_candidates: list = []
        self._capture_evidence = _jr._capture_protocol_prefix_evidence
        self._cutover_id = _jr.CUTOVER_OP_ID
        # #1054: the step runs once per decoded record (millions on a real
        # journal), so it reads these pre-bound slots instead of module
        # globals and attribute chains. Every container is created above and
        # never rebound, so each bound `append` stays its container's.
        self._observe = self.pass_boundary.observe
        self._append_slot = self.decoded_records.append
        self._append_coordinate = self.deep_coordinates.append
        self._append_owner = self.protocol_evidence_owners.append
        self._append_candidate = self.cutover_candidates.append
        self._selector_slot = _lib_journal_router.selector_slot
        self._special_ids = _deep_scan_special_ids()

    def placeholders(self, count: int) -> None:
        """``count`` positions for decoded records that do not matter."""
        if count > 0:
            filler = [None] * count
            self.decoded_records.extend(filler)
            self.deep_coordinates.extend(filler)


def _deep_scan_step(record: dict, segment: str, offset: int,
                    prior_high_water, state: _DeepScanState) -> None:
    """One decoded record's deep-scan work, in the scan's order: boundary
    observation, protocol-prefix evidence with its owner ordinal, the legacy
    cutover-candidate capture, then the selector slot and the coordinate.

    "Matters" is computed inline and is exactly `_deep_scan_matters`:
    `selector_slot` returns the record iff its `t` is a retained type, else
    None, and the id test is the same string-then-membership check."""
    state._observe(record)
    evidence = state.protocol_evidence
    state._capture_evidence(record, prior_high_water, evidence)
    if len(evidence) > len(state.protocol_evidence_owners):
        state._append_owner(len(state.decoded_records))
    rid = record.get("id")
    # first cutover op wins, exactly as `find_accounts_cutover_op` scans —
    # captured here so the conflict scan does not decode the whole journal
    # twice.
    if rid == state._cutover_id:
        state._append_candidate((record, segment, offset))
    # RETAIN ONLY what the selector consumes. `obs` lines are ~97% of a real
    # journal and `resolve_effective_events` ignores them entirely — keeping
    # their dictionaries cost 4.3 GB of peak RSS for an identical result
    # (#374 review). They still consume a lightweight slot because their
    # physical sequence is part of three durable violation fingerprints
    # (#508).
    slot = state._selector_slot(record)
    state._append_slot(slot)
    state._append_coordinate(
        (segment, offset)
        if slot is not None
        or (isinstance(rid, str) and rid in state._special_ids)
        else None)


#: The deep scan's disposable checkpoint, beside the segments it describes.
#: `list_segments` lists only `*.jsonl` segment names, so it is invisible to
#: every journal reader; doctor is its only writer and its only reader.
_DOCTOR_SCAN_INDEX_NAME = ".doctor-scan-index"


#: A publication temporary older than this is an orphan of a killed doctor:
#: no live publication holds its temporary file anywhere near this long.
_DEEP_SCAN_TEMP_MAX_AGE_SECONDS = 3600
_DEEP_SCAN_TEMP_PREFIX = _DOCTOR_SCAN_INDEX_NAME + "."
_DEEP_SCAN_TEMP_SUFFIX = ".tmp"


def _load_deep_scan_index(jdir: pathlib.Path, version: str):
    """``(entries, clean)``: the checkpoint's valid entries, or ``{}`` when
    it is absent, foreign or unreadable, and whether the document held
    exactly those entries. Read with `open`, never `Path.read_bytes`, so a
    failure injected into the segment reads cannot reach it (and it cannot
    mask one). Only ordinary exceptions are swallowed."""
    import _lib_doctor_scan_index as _dsi
    try:
        with open(jdir / _DOCTOR_SCAN_INDEX_NAME, "rb") as handle:
            raw = handle.read()
        entries, clean = _dsi.decode_document(raw, version)
        return (entries or {}), clean
    except Exception:  # noqa: BLE001 — a missing or bad cache is a cold run
        return {}, False


def _sweep_stale_deep_scan_temps(jdir: pathlib.Path) -> None:
    """Remove the publication temporaries a killed doctor left behind: only
    names with this module's `mkstemp` prefix and suffix, only regular files
    (never a symlink) and only ones untouched for an hour, so a concurrent
    doctor's live temporary is kept. Best-effort; never creates the
    directory."""
    import stat
    import time
    try:
        names = os.listdir(jdir)
    except OSError:
        return
    cutoff = time.time() - _DEEP_SCAN_TEMP_MAX_AGE_SECONDS
    shortest = len(_DEEP_SCAN_TEMP_PREFIX) + len(_DEEP_SCAN_TEMP_SUFFIX)
    for name in names:
        if (len(name) <= shortest
                or not name.startswith(_DEEP_SCAN_TEMP_PREFIX)
                or not name.endswith(_DEEP_SCAN_TEMP_SUFFIX)):
            continue
        path = os.path.join(jdir, name)
        try:
            info = os.lstat(path)
            if stat.S_ISREG(info.st_mode) and info.st_mtime < cutoff:
                os.unlink(path)
        except OSError:
            continue


def _publish_deep_scan_index(jdir: pathlib.Path, version: str,
                             entries: list, previous: dict,
                             previous_clean: bool) -> None:
    """Atomically replace the checkpoint with ``entries`` (spec 5.5).

    Stale temporaries of killed doctors are swept first. When the loaded
    document was clean and holds exactly ``entries`` (``previous``) the
    write is skipped, so an idle doctor does not rewrite a multi-MB file.
    Otherwise a unique temporary file in the journal directory, mode 0600,
    then `os.replace`: no fsync (the file is disposable and every entry is
    verified on reuse) and no lock (concurrent doctors are last-writer-wins).
    The journal directory is never created. Any ordinary failure is
    swallowed and leaves no temporary file, so the run's output and exit
    status never depend on it."""
    import tempfile
    import _lib_doctor_scan_index as _dsi
    tmp_path = None
    try:
        _sweep_stale_deep_scan_temps(jdir)
        if (previous_clean and len(entries) == len(previous)
                and all(previous.get(entry.name) == entry
                        for entry in entries)):
            return
        payload = _dsi.encode(version, entries)
        fd, tmp_path = tempfile.mkstemp(
            dir=str(jdir), prefix=_DEEP_SCAN_TEMP_PREFIX,
            suffix=_DEEP_SCAN_TEMP_SUFFIX)
        try:
            view = memoryview(payload)
            while view:
                view = view[os.write(fd, view):]
        finally:
            os.close(fd)
        os.replace(tmp_path, jdir / _DOCTOR_SCAN_INDEX_NAME)
        tmp_path = None
    except Exception:  # noqa: BLE001 — the checkpoint is best-effort
        pass
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _deep_decode_hits(data: bytes, entry) -> "list | None":
    """The decoded hit records of a verified prefix, or None when any hit
    does not start a line, does not decode or does not matter — checked for
    every hit before the caller touches any scan state."""
    import _lib_journal as _jl
    records = []
    for offset, _ordinal in entry.hits:
        if offset and data[offset - 1] != 0x0A:
            return None
        newline = data.find(b"\n", offset, entry.covered)
        if newline <= offset:
            return None
        record = _jl.decode_line(data[offset:newline])
        if record is None or not _deep_scan_matters(record):
            return None
        records.append(record)
    return records


def _deep_scan_segment(segment: str, data: bytes, entry, prior_high_water,
                       state: _DeepScanState):
    """Scan one segment's bytes; return ``(new entry, malformed lines,
    prior high-water)``.

    With a usable checkpoint entry (its prefix verified against ``data`` and
    every hit decoding to a record that matters) the cached prefix
    contributes its malformed count, the shared step for each hit and a
    placeholder for every other decoded position; the running prior
    high-water then becomes ``(segment, covered)``, the value the fresh loop
    holds at the first suffix line. Everything from ``covered`` on — or the
    whole segment without a usable entry — is processed exactly like the
    fresh loop. The prefix hash is continued over the suffix, so each byte
    is hashed once. A torn tail is never covered and never stored."""
    import hashlib
    import _lib_journal as _jl
    import _lib_doctor_scan_index as _dsi
    end = data.rfind(b"\n") + 1
    hasher = None
    hit_records = None
    if entry is not None:
        hasher = _dsi.verify_prefix(entry.sha256, entry.covered, data)
        if hasher is not None:
            hit_records = _deep_decode_hits(data, entry)
            if hit_records is None:
                hasher = None
    if hasher is not None:
        malformed = entry.malformed
        position = 0
        for (offset, ordinal), record in zip(entry.hits, hit_records):
            state.placeholders(ordinal - position)
            _deep_scan_step(
                record, segment, offset,
                (segment, offset) if offset > 0 else prior_high_water,
                state)
            position = ordinal + 1
        state.placeholders(entry.decoded - position)
        decoded = entry.decoded
        hits = list(entry.hits)
        start = entry.covered
        if start > 0:
            prior_high_water = (segment, start)
    else:
        hasher = hashlib.sha256()
        malformed = 0
        decoded = 0
        hits = []
        start = 0
    hasher.update(memoryview(data)[start:end])
    coordinates = state.deep_coordinates
    # #1054: the loop below runs once per journal line, so its callables are
    # locals. The step is read from the module global at call time, not at
    # import, so a patched `_deep_scan_step` still intercepts every record.
    decode_line = _jl.decode_line
    step = _deep_scan_step
    append_hit = hits.append
    # every element except the last is a complete line; the last is either
    # "" (ended in \n) or the torn partial — not a mid-file line, so it is
    # never counted as malformed.
    offset = start
    for raw in (data if start == 0 else data[start:]).split(b"\n")[:-1]:
        if not raw:
            prior_high_water = (segment, offset + 1)
            offset += 1
            continue
        record = decode_line(raw)
        if record is None:
            malformed += 1
            prior_high_water = (segment, offset + len(raw) + 1)
            offset += len(raw) + 1
            continue
        step(record, segment, offset, prior_high_water, state)
        if coordinates[-1] is not None:
            append_hit((offset, decoded))
        decoded += 1
        prior_high_water = (segment, offset + len(raw) + 1)
        offset += len(raw) + 1
    if end > 0:
        prior_high_water = (segment, end)
    new_entry = _dsi.Entry(segment, end, hasher.hexdigest(), decoded,
                           malformed, tuple(hits), None)
    return new_entry, malformed, prior_high_water


def _deep_boundary_fields(boundary) -> "dict | None":
    """The admission boundary as the explicit fields `classify_admission`
    reads (spec 5.4): the cutover's position and frozen extents, and for
    each attached revision its presence, position, frozen extents and
    linkage. Spool baselines and inventory evidence are not admission
    inputs. The dataclass equality is deliberately not used: it ignores the
    attached revisions."""
    if boundary is None:
        return None

    def extents(fence):
        return sorted([str(seg), int(end)]
                      for seg, end in fence.frozen_extents.items())

    revision = boundary.revision
    revision3 = boundary.revision3
    return {
        "cutover": {"segment": boundary.segment,
                    "end_offset": boundary.end_offset,
                    "frozen_extents": extents(boundary)},
        "revision": None if revision is None else {
            "segment": revision.segment,
            "end_offset": revision.end_offset,
            "frozen_extents": extents(revision),
            "cutover_segment": revision.cutover_segment,
            "cutover_offset": revision.cutover_offset,
        },
        "revision3": None if revision3 is None else {
            "segment": revision3.segment,
            "end_offset": revision3.end_offset,
            "frozen_extents": extents(revision3),
            "cutover_segment": revision3.cutover_segment,
            "cutover_offset": revision3.cutover_offset,
            "revision2_segment": revision3.revision2_segment,
            "revision2_offset": revision3.revision2_offset,
        },
    }


def _deep_exclusion_read(path, segment: str, lo: int, hi: int, boundary,
                         cached):
    """One read of ``[lo, hi)`` classifying excluded records; returns
    ``(excluded records, hasher)``, or None when ``cached`` was offered and
    could not be used.

    With ``cached`` the `RangeHasher` proves ``[lo, cached.covered)`` from
    the very chunks this read classifies: lines starting before ``covered``
    are not decoded, lines from ``covered`` on are decoded only once the
    comparison matched, and a mismatch — or a stream that ended before
    reaching ``covered`` — abandons the read. Prefix lines are never
    buffered, so the iterator's one-chunk memory bound holds."""
    import _cctally_journal as _jr
    import _lib_account_protocol
    import _lib_doctor_scan_index as _dsi
    import _lib_journal as _jl
    hasher = _dsi.RangeHasher(
        lo, None if cached is None else cached.covered,
        None if cached is None else cached.sha256)
    covered = lo if cached is None else cached.covered
    out = []
    lines = _jr._iter_segment_lines(path, lo, hi, on_bytes=hasher)
    try:
        for _name, offset, raw in lines:
            if cached is not None:
                if hasher.prefix_state == "mismatch":
                    return None
                if offset < covered:
                    continue
            record = _jl.decode_line(raw)
            if record is None:
                continue
            point = _lib_account_protocol.Coordinate(segment, offset)
            verdict = _lib_account_protocol.classify_admission(
                record, point, boundary)
            if not verdict.admitted:
                out.append(_jr.ExcludedRecord(coordinate=point,
                                              family=verdict.family))
    finally:
        lines.close()
    if cached is not None:
        if hasher.prefix_state != "match":
            return None
        out = [
            _jr.ExcludedRecord(
                coordinate=_lib_account_protocol.Coordinate(segment, offset),
                family=family)
            for offset, family in cached.excluded
        ] + out
    return out, hasher


def _deep_excluded_population(boundary, index: dict,
                              contributions: dict) -> list:
    """`_cctally_journal.excluded_population` with per-segment reuse (#1054
    spec 5.4); same contract and observation point.

    It re-lists and re-stats the segments at call time, so it sees bytes
    and segments appended after the main pass. Per segment the range is
    ``[frozen extent, complete-line end)``; a segment removed since the
    listing is skipped and any other failure raises to the caller's
    unavailable handling. A cached exclusion is offered only when its
    boundary fingerprint and ``lo`` match and its ``covered`` lies in the
    current range; when it cannot be used the segment is recomputed cold
    by a second read. A contribution is recorded in ``contributions`` only
    from a read that consumed exactly ``hi - lo`` bytes."""
    import _cctally_journal as _jr
    import _lib_doctor_scan_index as _dsi
    import _lib_journal as _jl
    if boundary is None:
        return []
    fingerprint = _dsi.boundary_fingerprint(_deep_boundary_fields(boundary))
    out = []
    for seg in sorted(_jr._segment_sizes(), key=_jl.segment_sort_key):
        path = _cctally_core.JOURNAL_DIR / seg
        lo = boundary.frozen_extents.get(seg, 0)
        try:
            hi = _jr._complete_line_offset(path, os.path.getsize(path))
        except FileNotFoundError:
            continue
        if hi <= lo:
            continue
        entry = index.get(seg)
        cached = None if entry is None else entry.exclusion
        if cached is not None and not (
                cached.boundary == fingerprint and cached.lo == lo
                and lo <= cached.covered <= hi):
            cached = None
        result = None
        if cached is not None:
            result = _deep_exclusion_read(path, seg, lo, hi, boundary, cached)
        if result is None:
            result = _deep_exclusion_read(path, seg, lo, hi, boundary, None)
        excluded, hasher = result
        out.extend(excluded)
        if hasher.consumed == hi - lo:
            contributions[seg] = _dsi.Exclusion(
                fingerprint, lo, hi, hasher.digest(),
                tuple((item.coordinate.offset, item.family)
                      for item in excluded))
    return out


def _deep_scan_index_descriptors() -> dict:
    """Every derived input of the checkpoint version (spec 5.2): the
    retained record types, the special ids, the protocol-resolution kinds,
    each constant `_lib_account_protocol.classify_admission` and its helpers
    read, and the stats epoch. `CCTALLY_VERSION` is deliberately absent: a
    release that changes none of these keeps every checkpoint."""
    import _lib_account_protocol as ap
    import _lib_journal
    return {
        "retained_record_types": sorted(
            _lib_journal_router.RETAINED_RECORD_TYPES),
        "special_ids": sorted(_deep_scan_special_ids()),
        "protocol_resolution_kinds": sorted(
            _lib_journal.PROTOCOL_RESOLUTION_KINDS),
        "admission": {
            "writer_generation_key": ap.WRITER_GENERATION_KEY,
            "protocol_generation": ap.PROTOCOL_GENERATION,
            "revision_key": ap.REVISION_KEY,
            "protocol_revision": ap.PROTOCOL_REVISION,
            "successor_revision_key": ap.SUCCESSOR_REVISION_KEY,
            "successor_revision": ap.SUCCESSOR_REVISION,
            "administrative_record_types": sorted(ap._ADMIN_RECORD_TYPES),
            "claude_op_kinds": sorted(ap._CLAUDE_OP_KINDS),
            "codex_op_kinds": sorted(ap._CODEX_OP_KINDS),
            "administrative_op_kinds": sorted(ap._ADMIN_OP_KINDS),
            "protocol_op_kinds": sorted(ap._PROTOCOL_OP_KINDS),
            "claude_evt_kinds": sorted(ap._CLAUDE_EVT_KINDS),
            "quota_evt_kinds": sorted(ap._QUOTA_EVT_KINDS),
        },
        "stats_index_epoch": _cctally_core.STATS_INDEX_EPOCH,
    }


def _durable_inventory_marker(conn) -> bool:
    """Whether the validated index shows the legacy spool inventory episode
    open (#948 spec 7.3: the `journal.legacy_writer` addendum says detection
    is paused only then). Read inside the caller's validated snapshot; a
    missing quarantine table is no open episode."""
    import _cctally_quarantine
    try:
        state = _cctally_quarantine.load_state(conn)
    except Exception:  # noqa: BLE001 — no readable marker claims no pause
        return False
    return state.open_episode() is not None


def _legacy_spool_inventory_probe(paused: bool):
    """The legacy spool inventory doctor itself takes (spec revision 5,
    section 8): None when it completes, else ``{"available": False, "path",
    "operation", "error", "paused"}``. Doctor can be the first process to
    meet the error, so it probes rather than reading what the reducer saw.
    Read-only: the spool is listed and read to digest it, never changed.

    Fix round K12 (K11R-13): any other failure leaves the inventory
    unavailable with its cause (``{"available": False, "unavailableCause",
    "paused"}``), never complete."""
    import errno as _errno
    import _cctally_journal
    try:
        _cctally_journal.capture_legacy_spool_state()
    except _cctally_journal.LegacySpoolInventoryIncomplete as exc:
        code = exc.errno or _errno.EIO
        return {"available": False,
                "path": exc.failed_path or exc.rel,
                "operation": exc.operation,
                "error": f"{os.strerror(code)} [{exc.error}]",
                "paused": bool(paused)}
    except Exception as exc:  # noqa: BLE001 — a diagnostic probe never crashes doctor
        return {"available": False, "unavailableCause": _scan_cause(exc),
                "paused": bool(paused)}
    return None


def _quarantine_inventory_path() -> "str | None":
    """The file a limit waiting for `inventory` names (#948 spec 7.3): the
    path doctor's own legacy spool inventory fails on, else the first legacy
    spool location (the inventory now completes and a writer has yet to
    record that). Read-only; None when neither is known."""
    import _cctally_journal
    probe = _legacy_spool_inventory_probe(True)
    if probe and probe.get("path"):
        return str(probe["path"])
    try:
        for path in _cctally_journal._legacy_spool_paths():
            return str(path)
    except Exception:  # noqa: BLE001 — a diagnostic read never crashes doctor
        return None
    return None


def _gather_outcome_quarantine() -> dict:
    """The `accounting.outcome_quarantine` evidence (#948 spec 7.1): the
    per-account, per-limit state of the journal-derived recovery barrier.

    Read from ONE snapshot proven current (epoch and protocol); an absent,
    unreadable or unvalidated index is unavailable, never "no quarantine".
    Read-only."""
    import _cctally_quarantine
    import _cctally_store

    out: dict = {"available": False, "accounts": []}
    try:
        if not _cctally_core.DB_PATH.exists():
            return out
        pc = _stats_ro_guarded()
        try:
            with _cctally_store.current_stats_snapshot(pc):
                report = _cctally_quarantine.report(pc)
                labels = {}
                for row in pc.execute(
                        "SELECT account_key, label, email FROM accounts"):
                    labels[row[0]] = row[1] or row[2] or row[0]
        finally:
            pc.close()
    except Exception:  # noqa: BLE001 — a diagnostic read degrades to unavailable
        return out
    accounts = []
    for row in report["accounts"]:
        accounts.append({**row, "accountLabel": labels.get(
            row["account"], row["account"])})
    out = {"available": True, "triggered": report["triggered"],
           "generation": report.get("generation"),
           "cause": report.get("cause"),
           "inventoryEpisode": report.get("inventoryEpisode"),
           "accounts": accounts}
    if any(doc.get("waitingFor") == "inventory"
           for row in accounts for doc in (row.get("axes") or {}).values()):
        out["inventoryPath"] = _quarantine_inventory_path()
    return out


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
    # #825/#832 section 7: the current protocol's fences are per exact
    # account and window under the account-bound root; only live windows count.
    try:
        result["tombstones"] = c._statusline_tombstone_states(now_epoch=now_epoch)
    except Exception:
        result["tombstones"] = {"fiveHour": "invalid", "sevenDay": "invalid"}
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


_CODEX_ACCOUNT_KEY_PATTERN = re.compile(r"^[0-9a-f]{32}$")


def _codex_hook_liveness_markers(*, root_keys: set[str]) -> dict[str, dict]:
    """Enumerate `<root>.last-success` / `<root>.<account>.last-success` mtimes.

    Marker enumeration lives here, never in `bin/_lib_doctor.py`: the pure
    kernel decides every check without touching the filesystem, the network
    or the clock, and this check is not the exception.

    A bare `*` glob would also admit backup-like siblings, so the account
    suffix is filtered against the 32-hex `account_key` shape rather than
    globbed. Account suffixes are never exposed to the report.
    """
    if not root_keys:
        return {}
    base = _cctally_core.APP_DIR / "codex-hook-tick"
    records: dict[str, dict] = {
        key: {"last_success_at": None, "marker_count": 0, "unavailable": False}
        for key in root_keys
    }
    try:
        names = sorted(entry.name for entry in os.scandir(base))
    except FileNotFoundError:
        # No marker directory yet is `never`, not `unavailable`: the hook has
        # simply not run here.
        return records
    except OSError:
        for record in records.values():
            record["unavailable"] = True
        return records
    for name in names:
        if not name.endswith(".last-success"):
            continue
        stem = name[: -len(".last-success")]
        root_key = stem
        if stem not in records:
            head, _sep, suffix = stem.rpartition(".")
            if not head or not _CODEX_ACCOUNT_KEY_PATTERN.match(suffix):
                continue
            root_key = head
            if root_key not in records:
                continue
        try:
            stamp = (base / name).stat().st_mtime
        except OSError:
            continue
        record = records[root_key]
        record["marker_count"] += 1
        observed = dt.datetime.fromtimestamp(stamp, dt.timezone.utc)
        if record["last_success_at"] is None or observed > record["last_success_at"]:
            record["last_success_at"] = observed
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


#: How much journal tail the `accounts.codex_window_attribution` leg will read
#: to decide whether an attribution op is sitting unconsumed. Past this, the
#: condition is left unreported: `doctor` is a health report that never
#: changes your data, not an ingest, and the state it would have reported
#: self-heals on the next sync.
_WINDOW_ATTRIBUTION_TAIL_BUDGET_BYTES = 8 * 1024 * 1024

#: The same discipline on the same leg's OTHER unbounded read. Every distinct
#: `(source_root_key, source_path)` in `codex_session_entries` costs a path
#: canonicalization and one or two indexed probes, and a store carrying an
#: assertion pays that on every `doctor` run. Past the cap the condition is
#: reported as NOT CHECKED rather than partially counted, because a partial
#: count of a whole-store condition reads exactly like a complete one.
_WINDOW_ATTRIBUTION_BASELINE_FILE_CAP = 5_000


def _unconsumed_window_attribution_ops(cursor, high_water) -> bool:
    """Whether the journal tail after `cursor` holds an attribution op."""
    import _cctally_journal
    import _lib_journal

    if (_lib_journal.segment_sort_key(str(cursor[0])), int(cursor[1])) >= (
            _lib_journal.segment_sort_key(str(high_water[0])),
            int(high_water[1])):
        return False
    kinds = (b'"codex_window_attribution"',
             b'"codex_window_attribution_retract"')
    scanned = 0
    try:
        for _segment, _offset, raw in _cctally_journal.iter_range(
                cursor, high_water):
            scanned += len(raw)
            if scanned > _WINDOW_ATTRIBUTION_TAIL_BUDGET_BYTES:
                return False
            if any(kind in raw for kind in kinds):
                return True
    except Exception:
        return False
    return False


def _gather_codex_window_attribution_state() -> "dict | None":
    """Read-only state for the ``accounts.codex_window_attribution`` leg (#500 §9).

    ``None`` means "this store cannot be inspected" — no cache.db, or a cache
    too old to carry the derived table — which the kernel renders as OK rather
    than as a finding, exactly as every other pre-feature cache shape degrades.

    Four conditions the spec names, plus one the Task-2 review added. The added
    one is an OBSERVABILITY check, not a health claim in its own right:
    ``_CodexFileBaselineResolver`` recovers a file identity by re-canonicalizing
    the stored ``source_path``, so a rollout whose path spelling changed after
    ingest reads as "no per-file decision" and is INDISTINGUISHABLE from a
    genuine absence. Counting the files whose derived identity has neither an
    incarnation row nor any account row is what makes that condition visible
    instead of silent, and it is computed only when this store carries an
    assertion at all — the restore path is the only thing it can affect.
    """
    if not _cctally_core.CACHE_DB_PATH.exists():
        return None
    try:
        conn = sqlite3.connect(
            f"file:{_cctally_core.CACHE_DB_PATH}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        import _cctally_cache
        import _cctally_quota
        import _lib_codex_window_attribution as _wa
        import _lib_journal

        try:
            active = _cctally_cache.load_active_window_attributions(conn)
            retracted = _cctally_cache.load_active_window_attributions(
                conn, retracted_only=True)
        except sqlite3.DatabaseError:
            return None
        state = {
            "active": len(active), "retracted": len(retracted),
            "dormant": 0, "split": 0, "conflicting": 0, "model_scoped": 0,
            "cursor_behind": False, "unrecoverable_baselines": 0,
            "baselines_checked": True,
        }
        # The cursor check runs BEFORE the empty-table early return, and that
        # ordering is the whole point of it: the state it exists to report is a
        # journal holding attribution records the derived index has not
        # consumed, which is exactly a store whose table is EMPTY. Returning
        # early on an empty table would make the leg blind to the condition it
        # names.
        #
        # The predicate is "an unconsumed attribution op EXISTS", never "the
        # cursor is behind the journal high-water". The attribution replay
        # cursor advances only inside a Codex sync while the high-water advances
        # on ALL journal traffic, so the positional comparison reports every
        # store that has appended anything since its last sync — measured
        # against this feature's own test fixture, which appends two
        # `account_observe` ops after its syncs and would report a stale index
        # with no attribution record in existence. It is the same trap §8.3
        # names for the projection certificate.
        #
        # A cursor of `None` reads as "not behind": it means no replay has ever
        # run, which is true of every store until its first Codex sync after
        # this feature shipped, and that sync replays from byte zero.
        #
        # The tail scan is BUDGETED. On a store whose Codex sync has not run in
        # a long time the tail is the whole journal — 1.7 GB on the maintainer's
        # store — and `doctor` must not read that. Past the budget the condition
        # is left unreported rather than guessed at, because it self-heals on
        # the next sync either way.
        cursor = _cctally_cache.load_codex_window_attribution_cursor(conn)
        # #1005: lock-free, like `stats_interrupted_rebuild_evidence`. The
        # leaf-locked `journal_high_water()` blocks while a writer holds
        # `journal.lock` and creates that file, and a read-only gather must
        # do neither; the scan below already tolerates a torn tail line.
        try:
            import _cctally_store

            high_water = _cctally_store._lock_free_journal_high_water()
        except OSError:
            high_water = None
        if cursor is not None and high_water is not None:
            state["cursor_behind"] = _unconsumed_window_attribution_ops(
                cursor, high_water)
        if not active and not retracted:
            return state

        resolutions, _ownership = _cctally_quota.resolve_codex_window_attributions(
            conn)
        for resolution in resolutions:
            if resolution.outcome == _wa.DORMANT:
                state["dormant"] += 1
            elif resolution.outcome == _wa.SPLIT:
                state["split"] += 1
            elif resolution.outcome in (_wa.SUPPRESSED_NATIVE,
                                        _wa.SUPPRESSED_CONFLICT):
                state["conflicting"] += 1
            elif resolution.outcome == _wa.SUPPRESSED_MODEL_SCOPED:
                state["model_scoped"] += 1

        try:
            files = conn.execute(
                "SELECT DISTINCT source_root_key, source_path "
                "  FROM codex_session_entries "
                " WHERE source_root_key IS NOT NULL AND source_path IS NOT NULL"
                " LIMIT ?", (_WINDOW_ATTRIBUTION_BASELINE_FILE_CAP + 1,)
            ).fetchall()
        except sqlite3.DatabaseError:
            files = []
        if len(files) > _WINDOW_ATTRIBUTION_BASELINE_FILE_CAP:
            # Declined whole rather than truncated: see the cap's own note.
            state["baselines_checked"] = False
            files = []
        for root_key, source_path in files:
            try:
                identity = _cctally_cache.codex_file_key_for_entry_path(
                    str(root_key), str(source_path))
            except (ValueError, OSError):
                state["unrecoverable_baselines"] += 1
                continue
            try:
                known = conn.execute(
                    "SELECT 1 FROM codex_file_incarnations "
                    " WHERE file_identity = ? LIMIT 1", (identity,)
                ).fetchone() or conn.execute(
                    "SELECT 1 FROM codex_file_accounts "
                    " WHERE file_identity = ? LIMIT 1", (identity,)
                ).fetchone()
            except sqlite3.DatabaseError:
                known = None
            if not known:
                state["unrecoverable_baselines"] += 1
        return state
    except Exception:
        return None
    finally:
        conn.close()


def _journal_protocol_violation_rows(conn: sqlite3.Connection) -> list:
    """Every stored protocol violation, parsed, in (batch, kind, fingerprint)
    order: the doctor's shallow journal leg.

    #901 Amendment 19 T1 (spec I1): the doctor holds every violation anyway,
    so they are ordered here, in Python, where ``ORDER BY batch_id, kind,
    fingerprint`` left a partial sorter over each batch's rows — and one batch
    is not a bound. ``fingerprint`` is the primary key, so the order is total
    and identical (text compared as UTF-8 bytes, SQLite's BINARY collation).
    """
    rows = conn.execute(
        "SELECT batch_id, kind, fingerprint, violation_json "
        "FROM journal_protocol_violations"
    ).fetchall()
    rows.sort(key=lambda row: tuple(
        (value is not None, str(value or "").encode("utf-8"))
        for value in row[:3]))
    return [json.loads(str(row[3])) for row in rows]


_CLAUDE_COST_EXPORT_MIGRATION = "050_preserve_claude_cost_attribution"


def _claude_cost_export_state(conn, doc) -> str:
    """Migration 050's export state (#944 spec 5.9): ``complete`` once
    applied, ``skipped`` when skipped, else ``stalled`` when its last attempt
    failed after its last completed chunk (or no chunk completed), else
    ``running``."""
    if conn.execute("SELECT 1 FROM schema_migrations WHERE name=?",
                    (_CLAUDE_COST_EXPORT_MIGRATION,)).fetchone():
        return "complete"
    try:
        if conn.execute(
                "SELECT 1 FROM schema_migrations_skipped WHERE name=?",
                (_CLAUDE_COST_EXPORT_MIGRATION,)).fetchone():
            return "skipped"
    except sqlite3.OperationalError:
        pass
    doc = doc or {}
    error_at, chunk_at = doc.get("lastErrorAt"), doc.get("lastChunkAt")
    if error_at and (not chunk_at or str(error_at) > str(chunk_at)):
        return "stalled"
    return "running"


def _gather_claude_cost_attribution(
        now_utc: "dt.datetime", *, cache_probe_allowed: bool = True) -> dict:
    """Read-only counts for ``accounts.claude_cost_attribution`` (#944 spec
    5.9). The population is the ``timeline_agreement`` / ``fallback``
    decisions decided in the preceding 7 UTC days that a retained accounting
    row links to: a keyed decision through ``idx_entries_dedup`` with link
    equality, a physical or legacy one exactly on the link through
    ``idx_entries_attr_nonkeyed``. A decision no row links to (a replaced
    physical record, an orphan) is not counted. ``preservedRows`` counts the
    ``preserved_history`` decisions decided in the window without probing
    their links (#944 F4): for a week after an upgrade that is every
    existing row (~700K measured), and probing each one's link cost ~2.8 s of
    SQL on every doctor run, while every export links the row it records.
    Never raises; an absent or unreadable cache.db yields the empty shape."""
    import _lib_claude_attribution as K
    end_us = K.us_from_iso(
        now_utc.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"))
    start_us = end_us - 7 * 86_400 * 1_000_000
    out = {
        "windowStart": K.iso_from_us(start_us),
        "windowEnd": K.iso_from_us(end_us),
        "population": "deduplicatedClaudeCostRows",
        "totalRows": 0, "fallbackRows": 0, "fallbackRealAccountRows": 0,
        "fallbackUnattributedRows": 0, "byReason": {},
        "firstFallbackAt": None, "lastFallbackAt": None,
        "preservedRows": 0, "unlinkedRows": 0,
        "preservation": {"state": None, "saved": None, "total": None,
                         "lastError": None, "lastErrorAt": None},
        "decisionConflicts": 0, "alignedRows": 0, "lastDeferral": None,
    }
    path = _cctally_core.CACHE_DB_PATH
    # A live cache repair owner bars every cache probe (#344).
    if not cache_probe_allowed or not path.exists():
        return out
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.0)
    except sqlite3.Error:
        return out
    try:
        import _cctally_claude_attribution as G
        if not G.schema_present(conn):
            return out
        # #944 F10: compare in decided_at's own format. ``decided_at_utc``
        # is written by ``now_utc_iso`` (seconds precision, ``Z``), and a
        # microsecond bound sorts after every decision of its own second
        # ('.' < 'Z'), dropping a decision made in the second that ends the
        # window. The bounds are the window's instants at that precision.
        window = tuple(
            _cctally_core.now_utc_iso(
                dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
                + dt.timedelta(microseconds=us))
            for us in (start_us, end_us))
        # The population is the evidence and fallback decisions only: the
        # preserved ones are excluded here, before any link is probed.
        population = (K.BASIS_AGREEMENT, K.BASIS_FALLBACK)
        rows = conn.execute(
            "SELECT d.basis, d.account_key, d.reason, d.decided_at_utc"
            " FROM claude_cost_decisions d"
            " WHERE d.decided_at_utc >= ? AND d.decided_at_utc <= ?"
            " AND d.basis IN (?, ?)"
            " AND d.form = ? AND EXISTS (SELECT 1 FROM session_entries e"
            "  WHERE e.msg_id = d.msg_id AND e.req_id = d.req_id"
            "  AND e.attribution_decision_key = d.decision_key)"
            " UNION ALL"
            " SELECT d.basis, d.account_key, d.reason, d.decided_at_utc"
            " FROM claude_cost_decisions d"
            " WHERE d.decided_at_utc >= ? AND d.decided_at_utc <= ?"
            " AND d.basis IN (?, ?)"
            " AND d.form <> ? AND EXISTS (SELECT 1 FROM session_entries e"
            "  WHERE e.attribution_decision_key = d.decision_key"
            "  AND (e.msg_id IS NULL OR e.req_id IS NULL))",
            (*window, *population, K.FORM_KEYED,
             *window, *population, K.FORM_KEYED)).fetchall()
        out["preservedRows"] = conn.execute(
            "SELECT COUNT(*) FROM claude_cost_decisions"
            " WHERE decided_at_utc >= ? AND decided_at_utc <= ?"
            " AND basis = ?", (*window, K.BASIS_PRESERVED)).fetchone()[0]
        fallback_at = []
        for basis, account, reason, decided_at in rows:
            out["totalRows"] += 1
            if basis != K.BASIS_FALLBACK:
                continue
            out["fallbackRows"] += 1
            if account and account != K.UNATTRIBUTED:
                out["fallbackRealAccountRows"] += 1
            else:
                out["fallbackUnattributedRows"] += 1
            key = reason or "unknown"
            out["byReason"][key] = out["byReason"].get(key, 0) + 1
            fallback_at.append(decided_at)
        if fallback_at:
            out["firstFallbackAt"] = min(fallback_at)
            out["lastFallbackAt"] = max(fallback_at)
        out["byReason"] = dict(sorted(out["byReason"].items()))
        out["unlinkedRows"] = conn.execute(
            "SELECT COUNT(*) FROM session_entries"
            " WHERE attribution_decision_key IS NULL").fetchone()[0]
        doc = G.read_preservation(conn)
        out["preservation"] = {
            "state": _claude_cost_export_state(conn, doc),
            "saved": (doc or {}).get("saved"),
            "total": (doc or {}).get("total"),
            "lastError": (doc or {}).get("lastError"),
            "lastErrorAt": (doc or {}).get("lastErrorAt"),
        }
        out["decisionConflicts"] = G.decision_conflicts(conn)
        out["alignedRows"] = G.aligned_rows(conn)
        out["lastDeferral"] = G.last_deferral(conn)
    except sqlite3.Error:
        return out
    finally:
        conn.close()
    return out


def _gather_accounts_state(now_utc: "dt.datetime", *,
                           cache_probe_allowed: bool = True) -> dict:
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
    # #944 spec 5.9: gathered from cache.db, so before the stats.db early
    # return; the check is always present.
    state["claude_cost_attribution"] = _gather_claude_cost_attribution(
        now_utc, cache_probe_allowed=cache_probe_allowed)
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
            # #901 Amendment 19 T1 (spec I1): counted in Python over the
            # streamed rows, one total per class, where `GROUP BY
            # account_key` sorted the trailing week's every snapshot through a
            # temp b-tree — and a week of activity is not a bound.
            attributed = unattributed = 0
            for (account_key,) in conn.execute(
                "SELECT account_key FROM weekly_usage_snapshots "
                "WHERE captured_at_utc >= ?", (cutoff,)
            ):
                if account_key and account_key != "unattributed":
                    attributed += 1
                else:
                    unattributed += 1
            state["recent_attributed"] += attributed
            state["recent_unattributed"] += unattributed
        except sqlite3.DatabaseError:
            pass
    finally:
        conn.close()
    return state


# ── The quota-observation input memo (#583 S5 §2.3) ─────────────────────────
# Task 4's attribution measured `doctor.quota_summary` at a 1,197.7 ms median
# against a copy of the real store (five fresh samples after one untimed
# warm-up, range 1,157.3-1,319.8) -- 69% of the doctor's own 1,731.2 ms median
# and 20% of the BUILDER. The 5,907.1 ms denominator behind that share is the
# wall time of `_tui_build_snapshot`, which the harness calls with
# `skip_sync=True` and which runs entirely inside `tick.build_span()`, so it
# contains no ingest and is not a whole tick; a whole tick is ingest plus
# builder, and the builder is 70.5% of one measured live. Against a whole tick
# the same probe is therefore 14%. It is one all-history
# `load_codex_quota_observations(latest_per_identity=True)` call. Its SQL used
# to run a `MAX(...) OVER (PARTITION BY ...)` across every retained row
# (277,207 on that store; 565K and ~1.1 GB of temp-file writes per call on the
# #901 live store); since #901 it reads the trigger-maintained
# `codex_quota_partition_latest` summary plus one indexed seek per raw
# partition. The memo below therefore remains a CPU optimisation and is no
# longer load-bearing for disk writes.
#
# Only the ROWS are retained, and only while their evidence is unmoved. Every
# `now`-derived interpretation over them is recomputed on every gather:
# `quota_freshness` and therefore `latest_capture_at`, `freshness_state`,
# `age_seconds` and `stale_after_seconds`. Those move with the clock alone, and
# retaining one would freeze a displayed health verdict at whatever instant the
# population happened to be loaded. This is the whole of Preserve 14's room --
# the memo caches a raw INPUT, never the payload, so the aggregate and its
# `generated_at` are still recomputed at every `DOCTOR_MEMO_TTL_S` expiry and
# `DoctorChip`'s rendered check time is never older than the TTL.
#
# At most ONE entry is retained: a moved signal replaces rather than
# accumulates, so the bound is one row per retained quota identity (16 on that
# store), not a multiple of anything. The dict is a plain module global with no
# `_assert_owner`, because the doctor gathers on the CLI's main thread as well
# as on the dashboard's rebuild thread and an owner assertion would refuse the
# CLI. That is safe here for the same reason it is for the source build's memo:
# a value is served only under an exactly-matching key, and the key IS the
# invalidation basis, so a racing writer can only cause a redundant cold read,
# never a stale answer.
_QUOTA_OBSERVATION_MEMO: "dict[tuple, tuple]" = {}


def _codex_quota_observation_signal() -> "tuple | None":
    """The exact mutation stream that can change the quota probe's rows.

    Returns ``None`` for "cannot establish identity", and every caller must
    treat that as a cold read rather than as a cache hit. An absent or
    unreadable change ledger is not an idle change ledger.

    Four legs, all of them cache.db. ``quota_window_change_log``'s high-water
    sequence covers every insert, delete and semantic update of
    ``quota_window_snapshots``: the three ledger triggers in
    ``bin/_cctally_db.py`` (``trg_qws_ledger_ins`` / ``_del`` / ``_upd``, the
    last firing on the semantic column set the loader interprets) record each
    one, and the sequence is read from ``sqlite_sequence`` so that
    ``prune_ledger_through``'s ``DELETE`` cannot walk it backwards.
    ``codex_window_attribution_revision`` covers the attribution overlay, which
    the journal bumps in the same transaction as the rows it applies. The
    database path and the file's identity together cover replacement: a
    restored or repaired cache.db can carry a LOWER sequence than the one
    already retained, so the path alone would serve rows from the superseded
    file.

    NO STATS.DB LEG, which is a deliberate difference from the source build's
    six-leg ``_codex_quota_reuse_identity``. That identity carries three stats
    digests because the build's quota consumers render account decoration. This
    probe does not: every field it publishes -- the five identity coordinates
    plus the freshness quadruple -- is derived from ``quota_window_snapshots``
    and the clock, and ``load_codex_quota_observations`` is a cache.db
    projection reader that is given no stats connection and opens none. Adding
    the stats legs would be over-conservative rather than safer, and it would
    surrender the reuse on every tick that publishes a new stats generation
    while the quota evidence sat still. ``test_stats_side_decoration_does_not
    _change_the_doctor_quota_probe`` is the executable form of that claim: if
    the probe ever grows a stats-side dependency, that test fails rather than
    this comment going quietly out of date.
    """
    path = _cctally_core.CACHE_DB_PATH
    try:
        stat = path.stat()
    except OSError:
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        # #857: the two ledger legs are stated once, in the snapshot-cache
        # kernel, and shared with the dashboard's Codex quota-dependency
        # identity rather than restated here.
        legs = _cctally()._load_sibling(
            "_lib_snapshot_cache"
        ).codex_quota_observation_legs(conn)
    finally:
        conn.close()
    if legs is None:
        return None
    return (str(path), stat.st_dev, stat.st_ino, *legs)


def _load_codex_quota_observations_for_doctor(*, force_cold: bool = False):
    """The probe's raw population, reused while its dependency signal holds."""
    c = _cctally()
    signal = None if force_cold else _codex_quota_observation_signal()
    if signal is not None:
        cached = _QUOTA_OBSERVATION_MEMO.get(signal)
        if cached is not None:
            return cached
    loaded = tuple(
        c._cctally_quota.load_codex_quota_observations(
            latest_per_identity=True,
        )
    )
    if signal is not None:
        # Replace rather than accumulate: the bound is one entry.
        _QUOTA_OBSERVATION_MEMO.clear()
        _QUOTA_OBSERVATION_MEMO[signal] = loaded
    return loaded


# The all-history project-metadata partition is the other large immutable input
# in a warm dashboard Doctor gather.  On the production-shaped #857 copy it
# cost 422-432 ms per call while returning the same six counts every 30-second
# Doctor refresh.  Retain only that raw partition, never a Doctor verdict or a
# clock-derived field, under the cache identity plus the existing physical
# mutation sequence.  Codex ingest advances that sequence in the same
# transaction as token, thread, root, cursor, and metadata writes, which are
# exactly the three-table inputs of ``load_codex_project_metadata_health``.
# Replacement is covered separately by device/inode, and an absent or malformed
# signal always takes the cold path.  The path is the one the passed connection
# opened, never the global ``CACHE_DB_PATH``: the sequence is read from that
# connection, so the file identity must describe the same file (#857).
_CODEX_PROJECT_METADATA_MEMO: "dict[tuple, object]" = {}


def _codex_project_metadata_signal(
    cache_conn: sqlite3.Connection,
) -> "tuple | None":
    try:
        path = next(
            str(row[2]) for row in cache_conn.execute("PRAGMA database_list")
            if len(row) > 2 and str(row[1]) == "main" and row[2]
        )
        stat = os.stat(path)
        row = cache_conn.execute(
            "SELECT value FROM cache_meta "
            "WHERE key='codex_physical_mutation_seq'"
        ).fetchone()
        if row is None:
            return None
        sequence = int(row[0])
        if sequence < 0:
            return None
    except (OSError, StopIteration, sqlite3.Error, TypeError, ValueError):
        return None
    return (path, stat.st_dev, stat.st_ino, sequence)


def _load_codex_project_metadata_health_for_doctor(
    cache_conn: sqlite3.Connection,
    *,
    force_cold: bool = False,
):
    signal = (
        None if force_cold
        else _codex_project_metadata_signal(cache_conn)
    )
    if signal is not None:
        cached = _CODEX_PROJECT_METADATA_MEMO.get(signal)
        if cached is not None:
            return cached
    analytics = _cctally()._load_sibling("_cctally_source_analytics")
    loaded = analytics.load_codex_project_metadata_health(
        cache_conn=cache_conn,
    )
    if signal is not None:
        _CODEX_PROJECT_METADATA_MEMO.clear()
        _CODEX_PROJECT_METADATA_MEMO[signal] = loaded
    return loaded


_QUOTA_NO_CHANGE: dict = {
    "active": False, "assessed": True, "effective_from": None,
    "previous_units_per_point": None, "new_units_per_point": None,
    "calibration_status": None,
}

#: No regime pair was examined, so no transition claim is available either
#: way (#688). Distinct from `_QUOTA_NO_CHANGE`, which is a real negative
#: finding over regimes that exist.
_QUOTA_NOT_ASSESSED: dict = {
    "active": False, "assessed": False, "effective_from": None,
    "previous_units_per_point": None, "new_units_per_point": None,
    "calibration_status": None,
}


def _gather_quota_rate_change(c, rejection=None) -> "dict | None":
    """§6.6's derived marker state, read WITHOUT the prediction gate.

    The rate-change surfaces read the regime pair deliberately: S1 marks a
    successor `detection-only` while its fit stays below the PREDICTION gate,
    and detection is exactly what a detection-grade fit is for. So this
    gather goes to the stored regimes rather than through the validated
    reader's default, which refuses that mark.

    An ABSENT calibration is "not assessed", NEVER "no change detected"
    (#688). It was the latter until a real metering change was reported to a
    user as a green all-clear: the fit was withheld, so no regime persisted,
    so the calibration was absent, and this gather turned that absence into a
    negative finding. A store with no fitted regime has no transition to
    report AND no evidence that none occurred, and `docs/commands/quota.md`
    forbids conflating the two. `None` is reserved for a calibration that
    exists and could not be read, where the sibling `quota.calibration` check
    is already WARNing about it; both render as not assessed.
    """
    if rejection == "calibration-absent":
        return dict(_QUOTA_NOT_ASSESSED)
    glue = c._load_sibling("_cctally_quota_model")
    calibration = c._load_sibling("_cctally_quota_calibration")
    loaded = glue.read_stored_state_readonly()
    if loaded is None:
        return None
    regimes = glue.stored_regimes(loaded, None)
    # #1015 spec 5.6: over the CLEAN regimes, with a superseded transition
    # suppressed. Correction facts that cannot be read (the fail-fast reader
    # never recovers or waits) leave the change unverified: not assessed,
    # never asserted.
    state, active = calibration.assess_active_rate_change(
        None, regimes=regimes)
    if state == calibration.MARKER_UNAVAILABLE:
        return dict(_QUOTA_NOT_ASSESSED)
    if active is None:
        return dict(_QUOTA_NO_CHANGE)
    return dict(active, assessed=True)


def _read_rollup_pricing_state(conn) -> "tuple[dict | None, str | None, object]":
    """Read the ordered-write guard's two ``cache_meta`` keys from ``conn``
    (#705), returning
    ``(refusal_record, stored_fingerprint, fingerprint_observation)``.

    ONE reader for BOTH stores. The guard runs on the cache.db connection in
    ``sync_cache`` and on the conversations.db connection in
    ``sync_claude_conversations``, so both files carry both keys and doctor
    must classify them identically — the failure this replaces was reading
    only one store, which reported OK for every refusal latched by a process
    that never opens the conversation store.

    A malformed record becomes ``{"__malformed__": True}`` rather than None,
    which is the ONE deliberate difference from the sibling Codex
    prune-refusal read beside it: a record that exists but cannot be read is
    still evidence the guard fired and must not read as "no refusal". The
    fingerprint is returned verbatim, because the check needs it to tell an
    ordinary refusal (an older process against a newer store) apart from a
    value no version can parse, which no restart or rebuild clears.

    The fingerprint read goes through ``_cctally_cache
    ._read_pricing_fingerprint_observation`` — the SAME helper the three writer
    sites use — so doctor classifies a store exactly as the guard acts on it.
    Doctor adds a READ here, not a fourth authorization site, but the read it
    performed was collapsing ``sqlite3.OperationalError`` to absence, so a
    store it could not open reported "no refused pricing write recorded" and
    doctor exited OK over a store it had no evidence about (#728). The third
    element carries that distinction; the first two keep their shapes so every
    existing consumer of the record and the value is unaffected.

    The refusal record still degrades to None on a store whose ``cache_meta``
    cannot be read, like every other probe in this gather — the observation is
    what reports that the read failed.
    """
    import _cctally_cache as _cc_sib
    record = None
    stored_fp = None
    try:
        row = conn.execute(
            "SELECT value FROM cache_meta WHERE key=?",
            (_cc_sib.CONVERSATION_ROLLUP_PRICING_REFUSED_KEY,),
        ).fetchone()
    except sqlite3.OperationalError:
        row = None
    if row and row[0]:
        try:
            parsed = json.loads(row[0])
        except (ValueError, TypeError):
            parsed = None
        record = parsed if isinstance(parsed, dict) else {"__malformed__": True}
    observation = _cc_sib._read_pricing_fingerprint_observation(conn)
    # #769 S3: only a state whose read actually produced the store's value may
    # publish one. The `and observation.raw` half is DEFENCE IN DEPTH and is
    # currently unreachable, which the Tranche 2 review was right to say the
    # previous comment obscured: `_read_pricing_fingerprint_observation` passes
    # `raw=None` on every degraded return, so a DEGRADED observation carries no
    # value to publish today. The guard is kept because the classifier's own
    # signature permits `found=True, raw=..., error_kind=...`, so a future
    # degraded path COULD carry one — and publishing it as `stored_fp` would
    # route doctor's unparseable-value branch, whose remedy is "delete that
    # cache_meta row", at a row this process never managed to read. MALFORMED
    # stays included because that value WAS read, and it is the input the
    # unparseable diagnosis is built from.
    if observation.state in ("present", "malformed") and observation.raw:
        stored_fp = str(observation.raw)
    return record, stored_fp, observation


@contextlib.contextmanager
def _freshness_read_transaction(conn):
    """One deferred read transaction around the snapshot selection (#1018).

    The tie-group selection runs several statements; without one read view a
    publication or delete between them could empty J1's group, and the
    fallback would then wrongly treat it as all-future. Always ended here, so
    no shared lock outlives the selection into the connection's later probes.
    """
    conn.execute("BEGIN")
    try:
        yield
    finally:
        if conn.in_transaction:
            conn.rollback()


def _stats_table_exists(conn, name: str) -> bool:
    """Table presence from `sqlite_master`, never from a caught error: a
    `database is locked` is an `OperationalError` too."""
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone() is not None


def _parse_capture_utc(value, label: str) -> dt.datetime:
    """A stored capture spelling as an aware UTC instant.

    A spelling without an offset is UTC, never host time: the selection's
    tie groups come from SQLite's ``julianday()``, which reads it as UTC, so
    a host-local parse would report a moment SQLite never grouped.
    """
    if not isinstance(value, str):
        raise ValueError(f"{label} is not an ISO datetime: {value!r}")
    text = value.strip()
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} must be ISO datetime") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _latest_snapshot_capture(conn, now_utc: dt.datetime):
    """Newest held-inclusive `weekly_usage_snapshots` capture <= ``now_utc``.

    Chronological, because the writer keeps its ISO spelling (offsets, mixed
    precision): text MAX is not. ``julianday`` buckets by millisecond, so the
    choice is exact by tie groups (spec 5.3): the newest bucket <= now, every
    row of it parsed and filtered against ``now_utc``; if that bucket is all
    after now, the next older bucket, all of whose rows precede now. Raises
    ``sqlite3.Error``/``ValueError`` when the source cannot be read.
    """
    if not _stats_table_exists(conn, "weekly_usage_snapshots"):
        return None  # no snapshots yet
    label = "weekly_usage_snapshots.captured_at_utc"
    now_jd = now_utc.astimezone(dt.timezone.utc).isoformat()

    def _group(julian):
        rows = conn.execute(
            "SELECT captured_at_utc FROM weekly_usage_snapshots "
            "WHERE julianday(captured_at_utc) = ?",
            (julian,),
        ).fetchall()
        return [
            moment for moment in
            (_parse_capture_utc(row[0], label) for row in rows)
            if moment <= now_utc
        ]

    with _freshness_read_transaction(conn):
        # fetchall, so each statement is exhausted and reset before the next.
        j1 = conn.execute(
            "SELECT MAX(julianday(captured_at_utc)) FROM weekly_usage_snapshots "
            "WHERE julianday(captured_at_utc) <= julianday(?)",
            (now_jd,),
        ).fetchall()[0][0]
        if j1 is None:
            return None
        valid = _group(j1)
        if valid:
            return max(valid)
        j2 = conn.execute(
            "SELECT MAX(julianday(captured_at_utc)) FROM weekly_usage_snapshots "
            "WHERE julianday(captured_at_utc) < ?",
            (j1,),
        ).fetchall()[0][0]
        if j2 is None:
            return None
        valid = _group(j2)
        return max(valid) if valid else None


def _latest_admitted_reading(conn, now_utc: dt.datetime):
    """Newest admitted Claude weekly reading <= ``now_utc`` (#1018).

    `claude_weekly_observation_index` holds one row per admitted observation
    carrying a weekly reading, projected before snapshot dedup, and stores the
    fixed-width canonical instant, so text comparison is exact chronological
    comparison. A store below the epoch that added it has no table and falls
    back to snapshots; a current store missing it is unreadable.
    """
    if not _stats_table_exists(conn, "claude_weekly_observation_index"):
        epoch = conn.execute("PRAGMA user_version").fetchone()[0]
        if int(epoch or 0) < _OBSERVATION_INDEX_EPOCH:
            return None
        raise sqlite3.DatabaseError(
            "claude_weekly_observation_index is missing from a "
            f"current-epoch stats.db (user_version {epoch})"
        )
    row = conn.execute(
        "SELECT MAX(captured_at_utc) FROM claude_weekly_observation_index "
        "WHERE captured_at_utc <= ?",
        (now_utc.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return _parse_capture_utc(
        row[0], "claude_weekly_observation_index.captured_at_utc"
    )


def _gather_freshness_sources(conn, now_utc: dt.datetime):
    """``(latest_snapshot_at, latest_reading_at, reason)`` for one gather.

    Each source runs independently, so one failure keeps the other's value;
    ``reason`` is ``"stats_unreadable"`` when either could not be read.
    """
    unreadable = False
    latest_snapshot_at = latest_reading_at = None
    try:
        latest_snapshot_at = _latest_snapshot_capture(conn, now_utc)
    except (sqlite3.Error, ValueError):
        unreadable = True
    try:
        latest_reading_at = _latest_admitted_reading(conn, now_utc)
    except (sqlite3.Error, ValueError):
        unreadable = True
    return (
        latest_snapshot_at,
        latest_reading_at,
        "stats_unreadable" if unreadable else None,
    )


def _stats_maintenance_lock_refused() -> bool:
    """True when ``stats.db.maintenance.lock`` exists and a non-blocking
    shared probe is refused. Read-only: no ``O_CREAT``, never waits."""
    try:
        fd = os.open(str(_cctally_core.STATS_LOCK_MAINTENANCE_PATH), os.O_RDONLY)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        except OSError:
            return False
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def _classify_absent_stats(stats_db_status) -> "str | None":
    """Why an absent stats.db yields no freshness evidence (spec 5.3).

    During a rebuild's physical publication the old family can be quarantined
    before its replacement is installed, so an absent file is a fresh install
    only when no maintenance evidence says otherwise.
    """
    import _cctally_store

    status = stats_db_status if isinstance(stats_db_status, dict) else {}
    interrupted = status.get("_interrupted_rebuild")
    if interrupted:
        if interrupted.get("live") is True:
            return "stats_maintenance"
        return "stats_unreadable"  # db.stats.file names the recovery
    if status.get("_interrupted_inspection_error"):
        # An inspection failure never reads as a fresh install.
        if _stats_maintenance_lock_refused():
            return "stats_maintenance"
        return "stats_unreadable"
    db_path = _cctally_core.DB_PATH
    if (
        _cctally_store._cctally_db._quarantine_pending_path(db_path).exists()
        or _cctally_store._stats_repair_marker(db_path).exists()
    ):
        return "stats_maintenance"
    if _stats_maintenance_lock_refused():
        return "stats_maintenance"
    return None


def doctor_gather_state(
    *,
    now_utc: "dt.datetime | None" = None,
    runtime_bind: "str | None" = None,
    deep: bool = False,
    force_cold_inputs: bool = False,
    discover_dashboards: bool = False,
):
    """Gather doctor state while excluding cache-family replacement.

    Doctor never changes your data, so it never creates the maintenance
    lock.  When the lock already exists it holds maintenance-shared from the
    marker check through every cache probe.  A live/stale marker or a pending
    quarantine suppresses all raw SQLite cache opens; when a cache exists but
    its lock is absent, probes also degrade rather than racing a newly
    starting repair.

    ``force_cold_inputs`` bypasses every reused raw input (#583 S5 §2.3), so a
    caller can obtain a genuinely fresh gather at a chosen instant. It exists
    for the equality gate that compares a reusing gather against a fresh one at
    an identical ``now_utc``; production callers leave it false.

    ``discover_dashboards`` (PR-11) lets the gather probe other running
    dashboards for their disk writes. Only the `cctally doctor` CLI passes it:
    the TUI and every snapshot build gather on each rebuild, where serial
    2-second probes of every live descriptor do not belong.
    """
    import _cctally_store

    # #1005: entered FIRST, so the cache-maintenance admission below and every
    # stats open inside the impl share one wait allowance.
    with _cctally_store.bounded_diagnostic_waits(
        _DOCTOR_MAINTENANCE_WAIT_ALLOWANCE_S
    ):
        return _doctor_gather_state_admitted(
            now_utc=now_utc,
            runtime_bind=runtime_bind,
            deep=deep,
            force_cold_inputs=force_cold_inputs,
            discover_dashboards=discover_dashboards,
        )


def _doctor_gather_state_admitted(
    *, now_utc, runtime_bind, deep, force_cold_inputs, discover_dashboards,
):
    """The cache-maintenance admission and the gather, inside the scope."""
    import _cctally_store

    cache_lock = None
    cache_probe_allowed = not _cctally_core.CACHE_DB_PATH.exists()
    cache_repair_marker = {
        "exists": False,
        "live": None,
        "reason": None,
    }
    try:
        with _lib_perf.phase("doctor.gate"):
            lock_path = _cctally_core.CACHE_LOCK_MAINTENANCE_PATH
            if lock_path.exists():
                cache_lock = open(lock_path, "r")
                # #1005 R11: bounded by the gather's allowance instead of a
                # blocking flock. On decline every cache probe degrades
                # through the existing "cache maintenance excludes read
                # probes" path.
                if _cctally_store.diagnostic_flock(
                    cache_lock, fcntl.LOCK_SH, 5.0
                ):
                    cache_probe_allowed = True
                else:
                    cache_lock.close()
                    cache_lock = None
                    cache_probe_allowed = False

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

        with _cctally_store.suppress_interrupted_stats_recovery():
            return _doctor_gather_state_impl(
                now_utc=now_utc,
                runtime_bind=runtime_bind,
                deep=deep,
                _cache_probe_allowed=cache_probe_allowed,
                _cache_repair_marker=cache_repair_marker,
                _force_cold_inputs=force_cold_inputs,
                _discover_dashboards=discover_dashboards,
            )
    finally:
        if cache_lock is not None:
            try:
                fcntl.flock(cache_lock, fcntl.LOCK_UN)
            finally:
                cache_lock.close()


def _gather_codex_container_state(c, *, now_utc, cache_probe_allowed):
    """Live, read-only #968 census; never opens/migrates a writable store.

    Presence is independent of decoder availability. Missing pre-migration
    tables mean no retained evidence; an unreadable probe means unknown, not
    an invented zero population. No decoding or detached work runs here.
    """
    import _cctally_cache as cache
    import _lib_codex_rollout_container as rc

    def table_exists(conn, name):
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,)).fetchone() is not None

    def deletion_debt(conn, table):
        if not table_exists(conn, table):
            return 0, None
        row = conn.execute(
            f"SELECT COUNT(*), MIN(recorded_at_utc) FROM {table}").fetchone()
        return int(row[0]), row[1]

    try:
        decoder = rc.decoder_capability()
        # #970 spec 5.9: the census reads the same locations discovery does,
        # each native home's flat archive directory included.
        selected = [item for item in cache._iter_codex_logical_containers(
            cache._codex_home_locations()) if item.kind == rc.ZSTD]
        census = dict(compressedOnly=len(selected), retainedComplete=0,
                      retainedIncomplete=0, neverIngested=0, unverified=0)
        status = None
        verifications, deletions = 0, 0
        deletion_starts = []
        path = _cctally_core.CACHE_DB_PATH
        if path.exists():
            if not cache_probe_allowed:
                return None, decoder, None, None
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.0)
            try:
                conn.row_factory = sqlite3.Row
                cursor_exists = table_exists(conn, "codex_session_files")
                columns = ({row[1] for row in conn.execute(
                    "PRAGMA table_info(codex_session_files)")} if cursor_exists else set())
                if "container_state" in columns:
                    verifications = int(conn.execute(
                        "SELECT COUNT(*) FROM codex_session_files "
                        "WHERE container_state='pending'").fetchone()[0])
                for item in selected:
                    row = (conn.execute("SELECT * FROM codex_session_files WHERE path=?",
                                        (str(item.logical),)).fetchone()
                           if cursor_exists else None)
                    if row is None:
                        census["neverIngested"] += 1
                        continue
                    row = dict(row)
                    state = row.get("container_state")
                    if state == "unverified":
                        census["unverified"] += 1
                    elif state == "pending":
                        continue  # verification work, never read debt (D3)
                    elif (row.get("container_kind") == rc.ZSTD
                          and state == "accepted"
                          and row.get("ingest_complete") == 1
                          and row.get("size_bytes") == row.get("decoded_size_bytes")
                          and all(row.get(key) == value for key, value in (
                              ("container_size_bytes", item.fingerprint.size),
                              ("mtime_ns", item.fingerprint.mtime_ns),
                              ("device_id", item.fingerprint.device),
                              ("inode", item.fingerprint.inode)))):
                        census["retainedComplete"] += 1
                    else:
                        census["retainedIncomplete"] += 1
                if table_exists(conn, "cache_meta"):
                    row = conn.execute("SELECT value FROM cache_meta WHERE key=?",
                                       (cache.CODEX_CONTAINER_STATUS_KEY,)).fetchone()
                    if row:
                        parsed = json.loads(row[0])
                        status = parsed if isinstance(parsed, dict) else None
                count, since = deletion_debt(conn, "codex_prune_debt")
                deletions += count
                if count:
                    deletion_starts.append(since)
            finally:
                conn.close()
        else:
            census["neverIngested"] = len(selected)
        maintenance_coverage = "complete"
        if _cctally_core.CONVERSATIONS_DB_PATH.exists():
            # An unavailable second store cannot erase the first store's known
            # counts or ages. Its missing contribution remains explicitly unknown.
            try:
                with _conversation_ro_guarded(timeout=0.0) as conn:
                    if conn is None:
                        maintenance_coverage = "partial"
                    else:
                        count, since = deletion_debt(conn, "codex_conversation_prune_debt")
                        deletions += count
                        if count:
                            deletion_starts.append(since)
            except Exception:
                maintenance_coverage = "partial"
        record = status or {}
        starts = list(deletion_starts)
        if verifications:
            starts.append(record.get("verificationPendingSince"))
        reads = (census["neverIngested"] + census["retainedIncomplete"]
                 if decoder != "unavailable" else 0)
        if reads:
            starts.append(record.get("readsPendingSince"))
        old = False
        for stamp in starts:
            if not stamp:
                continue
            try:
                age = (now_utc - parse_iso_datetime(
                    stamp, "codex maintenance start")).total_seconds()
                old = old or age > 86400
            except (TypeError, ValueError):
                pass
        debt = dict(verifications=verifications, deletions=deletions,
                    olderThan24h=old)
        if maintenance_coverage != "complete":
            debt["coverage"] = maintenance_coverage
        return census, decoder, status, debt
    except Exception:
        return None, None, None, None


def _doctor_gather_state_impl(
    *,
    now_utc: "dt.datetime | None" = None,
    runtime_bind: "str | None" = None,
    deep: bool = False,
    _cache_probe_allowed: bool,
    _cache_repair_marker: dict,
    _force_cold_inputs: bool = False,
    _discover_dashboards: bool = False,
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

    with _lib_perf.phase("doctor.backup_sync"):
        backup_sync_state = _gather_backup_sync_state(
            _cctally_core.APP_DIR,
            probe_time_machine=deep,
        )

    with _lib_perf.phase("doctor.install"):
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

    with _lib_perf.phase("doctor.hooks"):
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

    with _lib_perf.phase("doctor.auth"):
        # ── Auth ─────────────────────────────────────────────────────────
        try:
            oauth_token_present = c._setup_oauth_token_present()
        except OSError:
            oauth_token_present = None

    with _lib_perf.phase("doctor.db_status"):
        # ── DB ───────────────────────────────────────────────────────────
        interrupted_inspection_error = None
        try:
            import _cctally_store

            try:
                interrupted = _cctally_store.stats_interrupted_rebuild_evidence(
                    _cctally_core.DB_PATH
                )
            except OSError as exc:
                # #1005: the lock-free high-water read lost a listing race
                # twice, or the file system refused it. The evidence is
                # unknown, never "no rebuild": the report continues, and the
                # freshness leg refuses to read an absent file as a fresh
                # install.
                interrupted = None
                interrupted_inspection_error = str(exc)
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
        if interrupted_inspection_error is not None:
            stats_db_status["_interrupted_inspection_error"] = (
                interrupted_inspection_error
            )
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

    with _lib_perf.phase("doctor.stats_freshness"):
        # ── Data freshness ───────────────────────────────────────────────
        latest_snapshot_at = None
        latest_reading_at = None
        freshness_unavailable_reason = None
        forked_bucket_counts: dict | None = None
        credited_weeks: list[dict] | None = None
        try:
            conn = None
            if _cctally_core.DB_PATH.exists():
                # #386 spec section 3.1, third clause: EVERY opener of the live stats
                # family participates in the replacement protocol, read-only probes
                # included. The open mode stays read-WRITE deliberately — switching a
                # WAL DB whose `-shm` may be absent to `mode=ro` fails
                # SQLITE_CANTOPEN, which is not corruption and has been misread as
                # such on this project twice. Participation, not read-only-ness, is
                # what the clause requires.
                import _cctally_store as _store_mod
                try:
                    conn = _store_mod.stats_open_guarded(_cctally_core.DB_PATH)
                except _store_mod._cctally_db.StatsDbMaintenanceError:
                    # #1018 C6: neither source can be read, which is not
                    # silence; the check reports it as unavailable.
                    freshness_unavailable_reason = "stats_maintenance"
                except Exception:
                    freshness_unavailable_reason = "stats_unreadable"
            else:
                freshness_unavailable_reason = _classify_absent_stats(
                    stats_db_status
                )
            if conn is not None:
                try:
                    # HELD-INCLUSIVE, and that is the decision rather than an
                    # omission (#769 S11, #824). Two different freshness
                    # questions exist and this leg asks the first of them:
                    #
                    #   * PIPELINE freshness — did the write path run recently?
                    #     `data.latest_snapshot_age` remediates with "check
                    #     hooks are installed" and "hooks may be broken", so it
                    #     is asking whether ticks are still arriving. A
                    #     `weekly_observation_held` row is a real write by a
                    #     real tick carrying genuine five-hour evidence, so it
                    #     answers this question and must be counted. Excluding
                    #     held rows would make doctor report broken hooks
                    #     during a stretch when the hooks are working and only
                    #     the weekly meter is clamped.
                    #   * WEEKLY-AXIS freshness — how old is the newest genuine
                    #     weekly reading? That is what the status line, the
                    #     dashboard chip, `report`'s per-row stamp and the
                    #     hook's OAuth throttle ask, and every one of those
                    #     excludes held rows.
                    #
                    # Pinned by `test_doctor_latest_snapshot_age_counts_a_held_row`
                    # so a later change that silently converts this leg to the
                    # second question fails rather than passing quietly.
                    #
                    # #1018: a snapshot row is written only when a reading
                    # CHANGES value, so flat usage leaves it old while the
                    # pipeline is healthy. The second source, the newest
                    # admitted reading in the observation index, answers the
                    # same pipeline question; the check takes the newer.
                    (
                        latest_snapshot_at,
                        latest_reading_at,
                        freshness_unavailable_reason,
                    ) = _gather_freshness_sources(conn, now_utc)
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
                            # Held rows excluded: this reads a weekly VALUE
                            # and compares it against a threshold, unlike the
                            # freshness leg above, which asks whether the write
                            # path is alive and therefore counts a held tick as
                            # the real write it is (#769 S11, #824). The basis
                            # of a held row carries the same `week_end_at`, so
                            # the exclusion never empties this lookup.
                            latest = conn.execute(
                                """
                                SELECT week_start_date, weekly_percent
                                  FROM weekly_usage_snapshots
                                 WHERE week_end_at = ?
                                   AND weekly_observation_held = 0
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

    with _lib_perf.phase("doctor.cache_probes"):
        cache_entries_count = None
        cache_last_entry_at = None
        cache_db_page_count = None
        cache_db_freelist_count = None
        # #705: the ordered-write guard's two cache_meta keys, read from
        # cache.db as well as conversations.db. `sync_cache` runs the guard on
        # THIS connection, so a refusal latched by a process that never opens
        # the conversation store lives only here. Read inside the existing
        # cache.db probe so the gather opens the file once, and behind the same
        # `_cache_probe_allowed` gate as its neighbours.
        cache_rollup_pricing_refusal: "dict | None" = None
        cache_rollup_pricing_fp: "str | None" = None
        cache_rollup_pricing_observation = None
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
                    (
                        cache_rollup_pricing_refusal,
                        cache_rollup_pricing_fp,
                        cache_rollup_pricing_observation,
                    ) = _read_rollup_pricing_state(conn)
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

    with _lib_perf.phase("doctor.statusline"):
        # ── Statusline candidate arbitration (#318) ──────────────────────
        # This inspection is deliberately independent of SQLite mutation: marker
        # mtime, candidate/control files, and tombstones are all read fail-soft.
        # In particular it uses the scan-only candidate helper, never the reducer
        # loader that prunes expired or malformed spool files.
        try:
            statusline_pipeline = _gather_statusline_pipeline(c, now_utc=now_utc)
        except Exception:
            statusline_pipeline = None

    with _lib_perf.phase("doctor.conversations"):
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
        conversations_db_page_size = None
        conversations_reclaim_pending = None
        codex_prune_refusals: list[dict] = []
        conversation_rollup_pricing_refusal: "dict | None" = None
        conversation_rollup_pricing_fp: "str | None" = None
        conversation_rollup_pricing_observation = None
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
                        row = conn.execute("PRAGMA page_size").fetchone()
                        if row and row[0] is not None:
                            conversations_db_page_size = int(row[0])
                    except sqlite3.Error:
                        pass
                    try:
                        # #780: the durable reclaim backlog. Read here rather
                        # than derived from the freelist, because the freelist
                        # goes to zero while the bytes are still in the WAL.
                        import _lib_conversation_retention as _conv_retention
                        conversations_reclaim_pending = (
                            _conv_retention.read_reclaim_pending(conn))
                    except (ImportError, sqlite3.Error):
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
                    # #705: the ordered-write guard's refusal latch and the
                    # stored fingerprint, through the shared reader so both
                    # stores are read identically.
                    (
                        conversation_rollup_pricing_refusal,
                        conversation_rollup_pricing_fp,
                        conversation_rollup_pricing_observation,
                    ) = _read_rollup_pricing_state(conn)
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
            # signal. Never blocks (LOCK_NB), so doctor stays prompt and never
            # changes your data.
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

    with _lib_perf.phase("doctor.claude_files"):
        claude_jsonl_present = False
        try:
            claude_dir = pathlib.Path.home() / ".claude" / "projects"
            if claude_dir.exists():
                claude_jsonl_present = next(claude_dir.glob("**/*.jsonl"), None) is not None
        except Exception:
            pass

    with _lib_perf.phase("doctor.codex_cache"):
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
                            try:
                                codex_last_entry_at = parse_iso_datetime(
                                    row[1], "codex_session_entries.timestamp_utc",
                                ).astimezone(dt.timezone.utc)
                            except ValueError:
                                # A malformed newest row cannot establish cache
                                # freshness. Continue to the all-history health
                                # partition so doctor can count the bad row.
                                codex_last_entry_at = None
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
                        health = _load_codex_project_metadata_health_for_doctor(
                            conn,
                            force_cold=_force_cold_inputs,
                        )
                        codex_project_metadata_health = {
                            "total_rows": health.total_rows,
                            "qualified_rows": health.qualified_rows,
                            "missing_conversation_key_rows": health.missing_conversation_key_rows,
                            "missing_thread_join_rows": health.missing_thread_join_rows,
                            "undecodable_metadata_rows": (
                                health.undecodable_metadata_rows),
                        }
                        if health.malformed_accounting_rows:
                            codex_project_metadata_health[
                                "malformed_accounting_rows"] = (
                                    health.malformed_accounting_rows)
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

    with _lib_perf.phase("doctor.codex_files"):
        # Issue #109: probe every $CODEX_HOME session root (not the single
        # hardcoded ~/.codex/sessions), matching the multi-root ingestion path
        # from #108. _codex_session_roots() already applies the sessions/-subdir
        # rule and filters to existing dirs, so a bare glob per root suffices.
        # #970 spec 5.9: a native home's archived_sessions/ directory (globbed
        # flat, as discovery does) also holds Codex history.
        codex_jsonl_present = False
        try:
            import _cctally_cache as _cache_for_codex_files
            for location in _cache_for_codex_files._codex_home_locations():
                codex_dir = location.walk_root
                if (next(codex_dir.glob("**/*.jsonl"), None) is not None
                        or next(codex_dir.glob("**/*.jsonl.zst"), None) is not None):
                    codex_jsonl_present = True
                    break
                archive = location.archive_root
                if archive is not None and (
                        next(archive.glob("*.jsonl"), None) is not None
                        or next(archive.glob("*.jsonl.zst"), None) is not None):
                    codex_jsonl_present = True
                    break
        except Exception:
            pass

    with _lib_perf.phase("doctor.codex_containers"):
        (codex_compressed_census, codex_decoder_capability,
         codex_container_status, codex_maintenance_debt) = _gather_codex_container_state(
             c, now_utc=now_utc, cache_probe_allowed=_cache_probe_allowed)

    with _lib_perf.phase("doctor.quota_summary"):
        # ── Codex quota lifecycle (#294 S2) ──────────────────────────────
        # All three probes are read-only and root-qualified.  The physical cache
        # adapter preserves S1's per-window degradation, while setup's existing
        # inspector supplies the exact owned-hook state without exposing paths.
        codex_quota_windows: list[dict] = []
        try:
            # #566 §5.1 item 5: the population is unchanged — all history, every
            # root, no row cap — but the read returns each identity's latest
            # physical capture instead of every retained row, because that is the
            # only thing this probe consumes. On the maintainer's store the former
            # shape interpreted 266,337 rows to answer a question about 608 windows
            # and cost about 2.7s of every dashboard build. Nothing here may bound
            # the range: doing so would drop old and inactive-root identities and
            # silently change `window_count`, the responsible identity and the
            # WARN/OK verdict.
            # #583 S5 §2.3: the population is reused while its dependency
            # signal holds, and every `now`-derived interpretation below is
            # recomputed regardless. A suppressed probe reads nothing and is
            # never served from the memo, because `_cache_probe_allowed` gates
            # the call rather than the load.
            observations = (
                _load_codex_quota_observations_for_doctor(
                    force_cold=_force_cold_inputs,
                )
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

    with _lib_perf.phase("doctor.codex_hooks"):
        codex_hook_roots: list[dict] = []
        codex_hook_liveness: dict = {}
        try:
            hook_rows = [
                c._cctally_setup._codex_hook_row(root)
                for root in c._setup_codex_hook_roots()
            ]
            codex_hook_roots = [
                {
                    "source_root_key": row["source_root_key"],
                    "state": row["state"],
                    "requires_review": row["requires_review"],
                    "remediation": row["remediation"],
                }
                for row in sorted(hook_rows, key=lambda row: row["source_root_key"])
            ]
        except Exception:
            codex_hook_roots = []
        try:
            codex_hook_liveness = _codex_hook_liveness_markers(
                root_keys={row["source_root_key"] for row in codex_hook_roots},
            )
        except Exception:
            codex_hook_liveness = {}

    with _lib_perf.phase("doctor.codex_lifecycle"):
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

    with _lib_perf.phase("doctor.parse_health"):
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

    with _lib_perf.phase("doctor.integrity"):
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

    with _lib_perf.phase("doctor.locks"):
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

    with _lib_perf.phase("doctor.wal"):
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

        # ── conversations.db WAL size (#583 S4 / F39) ────────────────────
        # Same shallow/deep placement and the same best-effort contract as the
        # cache leg above: 0 when the sidecar is absent, None when it cannot be
        # stat'ed.
        conversations_db_wal_bytes: "int | None"
        try:
            _cwal = pathlib.Path(f"{_cctally_core.CONVERSATIONS_DB_PATH}-wal")
            conversations_db_wal_bytes = (
                _cwal.stat().st_size if _cwal.exists() else 0
            )
        except OSError:
            conversations_db_wal_bytes = None

    with _lib_perf.phase("doctor.config"):
        # ── Safety ───────────────────────────────────────────────────────
        # `dashboard.bind` is read via the same chokepoint that powers
        # `cctally config get dashboard.bind` — `_config_known_value`
        # normalizes hand-edited junk back to "loopback", matching the
        # value cmd_dashboard would actually bind to.
        #
        # Raw JSON read (NOT load_config or _load_config_unlocked): both
        # call `ensure_dirs()`, which creates `~/.local/share/cctally/`
        # and `logs/` on a fresh HOME. Doctor never changes your data
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

    with _lib_perf.phase("doctor.update"):
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

    with _lib_perf.phase("doctor.pricing_coverage"):
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

    with _lib_perf.phase("doctor.quota_calibration"):
        # ── Quota (#661 S2 §7) ───────────────────────────────────────────
        # Through the §1.1 NON-MUTATING reader. `load_calibrations` renames a
        # malformed or version-ahead file aside through `_quarantine`, so
        # calling it here would make a documented read-only command a writer.
        # An already-quarantined file is reported simply as a missing primary:
        # the reader cannot tell it from an absent one without scanning
        # sidecars, and it deliberately does not scan them.
        quota_calibration = None
        quota_rate_change = None
        try:
            qcg = c._load_sibling("_cctally_quota_calibration")
            read = qcg.read_calibration_file(account_key=None)
            quota_calibration = {
                "rejection": (None if read.rejection is None
                              else str(read.rejection.value)),
                "status": read.regime_status,
                "present": read.regime is not None,
            }
            quota_rate_change = _gather_quota_rate_change(
                c, quota_calibration["rejection"])
        except Exception:
            quota_calibration = None
            quota_rate_change = None

    # ── Meta ─────────────────────────────────────────────────────────
    with _lib_perf.phase("doctor.journal"):
        # ── Journal (DB journal redesign §9) ─────────────────────────────
        # Legs over the append-only journal, read-only except the deep scan's
        # disposable checkpoint (journal/.doctor-scan-index, #1054): presence +
        # appendability, torn-tail/malformed counts (deep-gated — reads whole
        # segments), the ingest cursor lag vs. the high-water, and the auto-heal
        # incident history. Every probe degrades to its always-OK posture on any
        # error (the pure kernel then reports "no journal" / "not scanned").
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
        journal_cursor_invalid = False
        journal_conflicts = None
        journal_protocol_violations = None
        journal_protocol_acknowledged = None
        journal_protocol_error = None
        # #825/#832 spec 8: the deep scan's whole-journal excluded population.
        deep_excluded = None
        deep_admission_boundary = None
        # K9K10-R6/R11: why the listing or the deep scan could not complete.
        journal_listing_error = None
        journal_scan_error = None
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
                except Exception as exc:  # noqa: BLE001 — reported, never empty
                    # K9K10-R6: an unlistable journal is unreadable, not an
                    # empty one; the presence leg names the cause.
                    segs = []
                    journal_listing_error = _scan_cause(exc)
                    if deep:
                        # The deep recomputation cannot run either.
                        journal_scan_error = journal_listing_error
                journal_segment_count = len(segs)
                # #402: the disposable stats index persists the most recent complete
                # selector result. Shallow Dashboard/TUI gathers read that bounded
                # summary instead of rescanning a production-sized journal and
                # therefore cannot turn known taint into a false OK.
                try:
                    if _cctally_core.DB_PATH.exists():
                        pc = _stats_ro_guarded()
                        try:
                            protocol_rows = (
                                _journal_protocol_violation_rows(pc))
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
                    prior_high_water = None
                    cutover_value = None
                    # #825/#832 (spec 5.3, 8): the deep scan applies the same
                    # admission predicate as ingest and rebuild — resolved
                    # within this one pass and applied after it — and
                    # recomputes the excluded population the legacy-writer leg
                    # compares.
                    scan_state = _DeepScanState()
                    pass_boundary = scan_state.pass_boundary
                    decoded_records = scan_state.decoded_records
                    deep_coordinates = scan_state.deep_coordinates
                    protocol_evidence = scan_state.protocol_evidence
                    protocol_evidence_owners = (
                        scan_state.protocol_evidence_owners)
                    cutover_candidates = scan_state.cutover_candidates
                    deep_excluded: list = []
                    # #1054: the doctor-written checkpoint. Every entry is
                    # proven against the bytes read below before any of it is
                    # used, and the new one covers exactly the segments read.
                    import _lib_doctor_scan_index as _dsi
                    scan_version = _dsi.index_version(
                        _deep_scan_index_descriptors())
                    scan_index, scan_index_clean = _load_deep_scan_index(
                        jdir, scan_version)
                    scan_entries: list = []
                    for seg in segs:
                        try:
                            data = (jdir / seg).read_bytes()
                        except FileNotFoundError:
                            continue
                        except OSError as exc:
                            # K9K10-R6: a skipped segment would let every
                            # check below report a partial journal as whole.
                            journal_scan_error = _scan_cause(exc)
                            break
                        if not data:
                            # Spec 5.3: an empty segment is skipped — no
                            # entry, and the prior high-water is untouched.
                            continue
                        if not data.endswith(b"\n"):
                            torn += 1
                        seg_entry, seg_malformed, prior_high_water = (
                            _deep_scan_segment(seg, data, scan_index.get(seg),
                                               prior_high_water, scan_state))
                        malformed += seg_malformed
                        scan_entries.append(seg_entry)
                    admission_boundary = None
                    if journal_scan_error is None:
                        try:
                            admission_boundary = pass_boundary.resolve(
                                (segs[-1], sizes.get(segs[-1], 0)))
                        except Exception as exc:  # noqa: BLE001
                            # K9K10-R6: a None boundary admits everything:
                            # zero excluded records and an unfiltered scan.
                            journal_scan_error = _scan_cause(exc)
                    exclusion_contributions: dict = {}
                    if journal_scan_error is None:
                        try:
                            deep_excluded = _deep_excluded_population(
                                admission_boundary, scan_index,
                                exclusion_contributions)
                        except Exception as exc:  # noqa: BLE001
                            # K9K10-R11: an unreadable tail is never a
                            # smaller population.
                            journal_scan_error = _scan_cause(exc)
                    if journal_scan_error is not None:
                        deep_excluded = None
                        journal_conflicts = None
                        journal_protocol_error = None
                    else:
                        journal_malformed_count = malformed
                        journal_torn_tail_count = torn
                        # An exclusion contribution rides on its segment's
                        # main entry; a segment the main pass did not read
                        # gets no entry at all.
                        _publish_deep_scan_index(jdir, scan_version, [
                            _dsi.Entry(entry.name, entry.covered,
                                       entry.sha256, entry.decoded,
                                       entry.malformed, entry.hits,
                                       exclusion_contributions.get(
                                           entry.name))
                            for entry in scan_entries
                        ], scan_index, scan_index_clean)
                if deep and segs and journal_scan_error is None:
                    _jr._apply_pass_admission(
                        decoded_records, deep_coordinates, admission_boundary)
                    # #825/#832 fix round C (CD2-005): an excluded
                    # acknowledgement's evidence is excluded with it.
                    protocol_evidence = _jr._admitted_prefix_evidence(
                        protocol_evidence, protocol_evidence_owners,
                        decoded_records)
                    # Every excluded record counts, obs included, so the
                    # population matches what ingest and rebuild record. Only
                    # the post-boundary byte ranges can hold one.
                    deep_admission_boundary = admission_boundary
                    for candidate, cand_seg, cand_off in cutover_candidates:
                        if _jr._is_admitted(candidate, cand_seg, cand_off,
                                            admission_boundary):
                            payload = candidate.get("payload")
                            if isinstance(payload, dict):
                                cutover_value = payload.get(
                                    "claude_legacy_account")
                            break
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
                        # #946: the lag of the SAFE applied cursor. A torn
                        # suffix past it stays outstanding until a repair, and
                        # a cursor past its segment or inside a line is no
                        # safe endpoint at all.
                        csize = sizes.get(segs[ci], 0)
                        if coff > csize:
                            journal_cursor_invalid = True
                        elif coff > 0:
                            try:
                                with open(_cctally_core.JOURNAL_DIR / cseg,
                                          "rb") as _seg_fh:
                                    _seg_fh.seek(coff - 1)
                                    if _seg_fh.read(1) != b"\n":
                                        journal_cursor_invalid = True
                            except OSError:
                                journal_cursor_invalid = True
                        lag = max(0, csize - coff)
                        for s in segs[ci + 1:]:
                            lag += sizes.get(s, 0)
                        journal_cursor_lag_bytes = lag
        except Exception:
            pass
    with _lib_perf.phase("doctor.stats_projection"):
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

    with _lib_perf.phase("doctor.heal"):
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

    with _lib_perf.phase("doctor.meta_accounts"):
        cctally_version_tuple = _lib_changelog._read_latest_changelog_version()
        cctally_version = (
            cctally_version_tuple[0] if cctally_version_tuple else "unknown"
        )
        accounts_state = _gather_accounts_state(
            now_utc, cache_probe_allowed=_cache_probe_allowed)
        accounts_state["codex_null_reset_anchors"] = codex_null_reset_anchors
        accounts_state["codex_window_attribution"] = (
            _gather_codex_window_attribution_state()
            if _cache_probe_allowed else None)

    # #901 §5.6: running dashboards' disk writes. Read-only; degrades.
    try:
        with _lib_perf.phase("doctor.dashboard_disk_writes"):
            dashboard_disk_writes = _gather_dashboard_disk_writes(
                discover=_discover_dashboards)
    except Exception:  # noqa: BLE001 — a probe failure must not fail doctor
        dashboard_disk_writes = {"mode": "error", "instances": []}
    # #997 D10: the detached rebuild workers, read-only (no worker or
    # admission flock is taken; a probe would make a starting worker `busy`).
    try:
        with _lib_perf.phase("doctor.rebuild_workers"):
            rebuild_workers = _gather_rebuild_workers()
    except Exception as exc:  # noqa: BLE001 — a gather failure must not fail doctor
        rebuild_workers = {worker: _rebuild_worker_gather_failed(worker, exc)
                           for worker in _REBUILD_WORKERS}
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
        latest_reading_at=latest_reading_at,
        freshness_unavailable_reason=freshness_unavailable_reason,
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
        # #661 S2 §7: the `quota` category's two inputs.
        quota_calibration=quota_calibration,
        quota_rate_change=quota_rate_change,
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
        codex_compressed_census=codex_compressed_census,
        codex_decoder_capability=codex_decoder_capability,
        codex_container_status=codex_container_status,
        codex_maintenance_debt=codex_maintenance_debt,
        conversation_rollup_pricing_refusal=(
            conversation_rollup_pricing_refusal),
        conversation_rollup_pricing_fp=conversation_rollup_pricing_fp,
        cache_rollup_pricing_refusal=cache_rollup_pricing_refusal,
        cache_rollup_pricing_fp=cache_rollup_pricing_fp,
        conversation_rollup_pricing_observation=(
            conversation_rollup_pricing_observation),
        cache_rollup_pricing_observation=cache_rollup_pricing_observation,
        stats_db_quick_check=stats_db_quick_check,
        cache_db_quick_check=cache_db_quick_check,
        conversations_db_quick_check=conversations_db_quick_check,
        locks_held=locks_held,
        # #297: cache.db WAL size backstop (gathered outside the deep branch).
        cache_db_wal_bytes=cache_db_wal_bytes,
        conversations_db_wal_bytes=conversations_db_wal_bytes,
        # #374: quarantined same-revision groups + structural protocol violation.
        journal_conflicts=journal_conflicts,
        # #825/#832 spec 8: the legacy-writer evidence.
        legacy_writer=_gather_legacy_writer(
            deep_excluded, deep_admission_boundary,
            deep_error=journal_scan_error),
        # #825/#832 spec 8: the per-account outcome quarantine.
        outcome_quarantine=_gather_outcome_quarantine(),
        journal_protocol_violations=journal_protocol_violations,
        journal_protocol_acknowledged=journal_protocol_acknowledged,
        journal_protocol_error=journal_protocol_error,
        journal_listing_error=journal_listing_error,
        journal_scan_error=journal_scan_error,
        # #496 S5b: the published generation's incomplete-quota-projection flag.
        stats_quota_projection_incomplete=stats_quota_projection_incomplete,
        # #315: read-only cache free-page evidence for the reclaim hint.
        cache_db_page_count=cache_db_page_count,
        cache_db_freelist_count=cache_db_freelist_count,
        conversations_db_page_count=conversations_db_page_count,
        conversations_db_freelist_count=conversations_db_freelist_count,
        conversations_db_page_size=conversations_db_page_size,
        dashboard_disk_writes=dashboard_disk_writes,
        rebuild_workers=rebuild_workers,
        conversations_reclaim_pending=conversations_reclaim_pending,
        codex_quota_windows=codex_quota_windows,
        codex_hook_roots=codex_hook_roots,
        codex_hook_liveness=codex_hook_liveness,
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
        journal_cursor_invalid=journal_cursor_invalid,
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
    state = c.doctor_gather_state(deep=True, discover_dashboards=True)
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
