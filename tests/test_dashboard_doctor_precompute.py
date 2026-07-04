"""M4 (#268) — doctor / config / update-state precompute + envelope purity.

Spec §6: the dashboard envelope (`snapshot_to_envelope`) used to fork the
`security` keychain subprocess (via `doctor_gather_state`) + read
`config.json` + the update-state files once PER SSE CLIENT PER TICK. M4
precomputes those on the sync-thread `DataSnapshot` (once per rebuild, doctor
behind a short-TTL memo) and restores `snapshot_to_envelope` to a pure
renderer that reads the attached values. The lazy `GET /api/doctor` endpoint
stays live.
"""
from __future__ import annotations

import datetime as dt
import sys

from conftest import load_script, redirect_paths  # type: ignore


NOW_UTC = dt.datetime(2026, 7, 4, 12, 0, 0, tzinfo=dt.timezone.utc)


# ===========================================================================
# Task 4.1 — DataSnapshot precompute fields + crash-recovery carry
# ===========================================================================
def test_datasnapshot_new_fields_default_none():
    """New precompute fields default None at the END of the dataclass so the
    positional fixture constructors (`_tui_empty_snapshot`, snapshot modules)
    keep working."""
    ns = load_script()
    DS = ns["DataSnapshot"]
    # 7 required positional fields, as `_tui_empty_snapshot` passes them.
    snap = DS(None, None, [], [], None, None, NOW_UTC)
    assert snap.doctor_payload is None
    assert snap.envelope_precompute is None


def test_crash_recovery_carries_doctor_payload_and_precompute(monkeypatch):
    """A sync crash (`_TuiSyncThread._run`) must carry the prior
    `doctor_payload` + `envelope_precompute` forward, not drop them to the
    dataclass defaults — otherwise a long-idle dashboard's doctor chip would
    blank out on a single transient sync failure (Codex F6)."""
    ns = load_script()
    DS = ns["DataSnapshot"]
    SnapshotRef = ns["_SnapshotRef"]
    Thread = ns["_TuiSyncThread"]

    prev = DS(
        None, None, [], [], None, None, NOW_UTC,
        doctor_payload={"severity": "ok", "fingerprint": "sha1:abc"},
        envelope_precompute={
            "config": {"x": 1},
            "update_state": {"latest_version": None},
            "update_suppress": {"skipped_versions": [], "remind_after": None},
        },
    )
    ref = SnapshotRef(prev)
    thread = Thread(ref, 0.01, skip_sync=True)

    def raiser(*a, **k):
        # Set stop BEFORE raising so the post-except wait loop returns after
        # exactly one iteration (deterministic; no real sleep).
        thread._stop.set()
        raise RuntimeError("boom")

    monkeypatch.setitem(
        sys.modules["cctally"].__dict__, "_tui_build_snapshot", raiser,
    )
    thread._run()

    crashed = ref.get()
    assert crashed.last_sync_error and "boom" in crashed.last_sync_error
    assert crashed.doctor_payload == {"severity": "ok", "fingerprint": "sha1:abc"}
    assert crashed.envelope_precompute["config"] == {"x": 1}
    assert crashed.envelope_precompute["update_state"] == {"latest_version": None}


# ===========================================================================
# Task 4.2 (integration) — precompute doctor ONCE per rebuild; TUI never does
# ===========================================================================
def _spy_doctor_gather(monkeypatch):
    calls = {"n": 0}
    real = sys.modules["cctally"].doctor_gather_state

    def spy(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setitem(
        sys.modules["cctally"].__dict__, "doctor_gather_state", spy,
    )
    return calls


def test_precompute_doctor_once_across_warm_rebuilds(monkeypatch, tmp_path):
    """Two back-to-back dashboard rebuilds within the doctor TTL gather doctor
    ONCE total — the `security` keychain subprocess is not re-forked per tick
    (spec §6, the whole point of the TTL memo)."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    sc.reset_doctor_memo()
    calls = _spy_doctor_gather(monkeypatch)
    ns["_tui_build_snapshot"](
        now_utc=NOW_UTC, skip_sync=False,
        precompute_envelope=True, runtime_bind="127.0.0.1",
    )
    ns["_tui_build_snapshot"](
        now_utc=NOW_UTC + dt.timedelta(seconds=3), skip_sync=False,
        precompute_envelope=True, runtime_bind="127.0.0.1",
    )
    assert calls["n"] == 1, (
        "doctor must be gathered ONCE across two warm rebuilds within the TTL"
    )


def test_tui_path_never_precomputes_doctor(monkeypatch, tmp_path):
    """The terminal-TUI rebuild (default `precompute_envelope=False`) must NOT
    fork `security` — no regression from moving doctor onto the snapshot."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    sc.reset_doctor_memo()
    calls = _spy_doctor_gather(monkeypatch)
    snap = ns["_tui_build_snapshot"](now_utc=NOW_UTC, skip_sync=False)
    assert calls["n"] == 0, "the TUI rebuild must not gather doctor"
    assert snap.doctor_payload is None
    assert snap.envelope_precompute is None


# ===========================================================================
# Task 4.3 — snapshot_to_envelope is a PURE renderer (reads attached values)
# ===========================================================================
def test_envelope_is_pure_reads_attached_doctor_and_config(monkeypatch, tmp_path):
    """With a precomputed snapshot, `snapshot_to_envelope` performs ZERO
    doctor/config/update-state I/O — monkeypatch every such reader to RAISE and
    the envelope must still build, sourcing the doctor + update blocks from the
    attached payloads."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)

    snap = ns["_tui_build_snapshot"](
        now_utc=NOW_UTC, skip_sync=False,
        precompute_envelope=True, runtime_bind="127.0.0.1",
    )
    assert snap.doctor_payload is not None  # try/except guarantees a dict
    assert snap.envelope_precompute is not None
    attached_doctor = snap.doctor_payload

    def boom(*a, **k):
        raise AssertionError("snapshot_to_envelope must not do config/doctor I/O")

    for name in ("doctor_gather_state", "load_config",
                 "_load_update_state", "_load_update_suppress"):
        monkeypatch.setitem(sys.modules["cctally"].__dict__, name, boom)

    env = ns["snapshot_to_envelope"](snap, now_utc=NOW_UTC)
    # Doctor block sourced verbatim from the attached precompute.
    assert env["doctor"] == attached_doctor
    # Update block derived from the attached update-state precompute.
    assert "update" in env and isinstance(env["update"], dict)
    # Config-derived blocks still render (from the attached raw config).
    assert "display" in env and env["display"].get("resolved_tz")


def test_envelope_falls_back_when_precompute_absent(monkeypatch, tmp_path):
    """A DataSnapshot with no precompute (doctor_payload / envelope_precompute
    None — fixtures / the initial empty snapshot / TUI path) must still render:
    the envelope falls back to the inline doctor/config/update reads, unchanged
    behavior."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)

    # TUI-path build → precompute fields stay None.
    snap = ns["_tui_build_snapshot"](now_utc=NOW_UTC, skip_sync=False)
    assert snap.doctor_payload is None and snap.envelope_precompute is None

    env = ns["snapshot_to_envelope"](snap, now_utc=NOW_UTC, runtime_bind="127.0.0.1")
    assert env["doctor"]["severity"] in {"ok", "warn", "fail"}
    assert "update" in env
    assert env["display"].get("resolved_tz")
