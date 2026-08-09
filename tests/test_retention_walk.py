"""#496 S6 §7.5 — the bounded metadata walk and its two latency gates.

The walk is NEW work on a periodic path: `doctor_gather_state` is reached from
the TUI and the dashboard snapshot precompute, not only from `GET /api/doctor`.
Revision 3 asserted "under 150 ms on 150 roots and 600 members" without
measuring either number, and both were wrong — the maintainer's install holds
**350 roots** and roughly **1150 members**, and a warm depth-2 walk over 1036
entries takes **3.5–4.8 ms**.

The gate is therefore two tests:

* a deterministic OPERATION-COUNT test, which fails identically on every
  machine and is what actually catches a regression;
* a generously bounded wall-clock backstop at 250 ms, roughly fifty times the
  measured cost, on a corpus built at or above production scale. A fixture
  smaller than the real corpus would make this leg unable to fail.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import time

import pytest

from conftest import load_script, redirect_paths


def _load(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _cctally_core
    import _cctally_retention

    return ns, _cctally_core, _cctally_retention


def _write(path: pathlib.Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


_STAMP_BASE = __import__("datetime").datetime(2026, 1, 1, tzinfo=None)


def _stamp(index: int) -> str:
    """A distinct, well-formed `%Y%m%dT%H%M%S` stamp per index.

    Uniqueness matters: a repeated stamp silently overwrites the artifact built
    for the earlier index, which shrinks the corpus the latency gate runs on.
    """
    import datetime as _dt

    return (_STAMP_BASE + _dt.timedelta(hours=index)).strftime("%Y%m%dT%H%M%S")


def build_incident(
    app_dir: pathlib.Path, family: str, stamp: str, *,
    forensics=None, record=None, shape=None, classified=True, moved=None,
    sidecar=False,
) -> pathlib.Path:
    incident = app_dir / "quarantine" / f"{family}-{stamp}Z"
    incident.mkdir(parents=True, exist_ok=True)
    moved = [family, f"{family}-wal"] if moved is None else moved
    for name in moved:
        (incident / name).write_bytes(b"x" * 32)
    manifest = {
        "schemaVersion": 2 if classified else 1,
        "quarantinedAtUtc": "2026-01-01T00:00:00Z",
        "originalPath": str(app_dir / family),
        "movedFiles": moved,
        "complete": True,
    }
    if classified:
        manifest["trigger"] = "corruption-heal"
    if forensics is not None:
        manifest["forensicsPath"] = str(forensics)
    if record is not None:
        manifest["rebuildRecordPath"] = str(record)
    if shape is not None:
        manifest["damage"] = {"preserved": {"shapeToken": shape}}
    _write(incident / "manifest.json", manifest)
    if sidecar:
        _write(incident / "classification.json", {
            "schemaVersion": 1, "incident": incident.name,
            "method": "forensics-correlation", "confidence": "candidate",
            "evidence": {},
        })
    return incident


def build_bundle(
    app_dir: pathlib.Path, family: str, stamp: str, *, origin="cache.open",
) -> pathlib.Path:
    path = app_dir / "logs" / f"{family}-corruption-forensics-{stamp}Z.json"
    payload = {"schemaVersion": 1, "db": family}
    if origin is not None:
        payload["trigger"] = {"origin": origin}
    _write(path, payload)
    return path


def build_evidence_dir(
    app_dir: pathlib.Path, family: str, stamp: str, *, members: int = 2,
) -> pathlib.Path:
    """A WAL-evidence DIRECTORY beside a bundle, sharing its stem.

    This is the only depth-2 shape under `logs/`, so a corpus without one
    leaves that recursion contributing nothing to `_expected_operations` and
    unexercised by either latency gate.
    """
    path = app_dir / "logs" / f"{family}-corruption-forensics-{stamp}Z"
    path.mkdir(parents=True, exist_ok=True)
    for index in range(members):
        (path / f"{family}-wal.{index}").write_bytes(b"w" * 32)
    return path


def build_rebuild_record(
    app_dir: pathlib.Path, stamp: str, *, status="ok", incident=None,
    forensics=None,
) -> pathlib.Path:
    path = app_dir / "logs" / f"stats-rebuild-{stamp}_000000.json"
    payload = {"status": status, "trigger": "corruption-heal"}
    if incident is not None:
        payload["incidentPath"] = str(incident)
    if forensics is not None:
        payload["forensicsPath"] = str(forensics)
    _write(path, payload)
    return path


def build_backup(
    app_dir: pathlib.Path, family: str, stamp: str, *, machine=True,
    sidecar=True,
) -> pathlib.Path:
    suffix = f".bak-corrupt-malformed-{stamp}Z" if machine else f".bak-{stamp}Z"
    stem = app_dir / f"{family}{suffix}"
    stem.write_bytes(b"y" * 64)
    (app_dir / f"{stem.name}-wal").write_bytes(b"z" * 16)
    if machine and sidecar:
        members = []
        for name in (stem.name, f"{stem.name}-wal"):
            info = (app_dir / name).stat()
            members.append({
                "name": name, "size": info.st_size, "mtime": info.st_mtime,
                "device": info.st_dev, "inode": info.st_ino,
            })
        _write(app_dir / f"{stem.name}.classification.json", {
            "schemaVersion": 1, "method": "db-repair", "confidence": "exact",
            "trigger": "db-repair", "members": members,
        })
    return stem


# --------------------------------------------------------------------------
# What the walk recognizes
# --------------------------------------------------------------------------


def test_the_walk_reports_each_recognized_kind_once(tmp_path, monkeypatch):
    _ns, core, ret = _load(tmp_path, monkeypatch)
    app = core.APP_DIR
    bundle = build_bundle(app, "stats.db", "20260101T000000")
    record = build_rebuild_record(app, "20260101T000001", forensics=bundle)
    build_incident(app, "stats.db", "20260101T000002", forensics=bundle,
                   record=record, shape="abc")
    (app / "logs" / "stats.db-corruption-forensics-20260101T000000Z").mkdir()
    build_backup(app, "cache.db", "20260101T000003")

    scan = ret.gather_retained_artifacts()
    kinds = sorted({member.kind for member in scan.members})
    assert kinds == [
        "backup", "backup_member", "bundle", "incident", "rebuild_record",
        "wal_evidence",
    ]
    assert scan.partial is False


def test_a_backup_family_is_one_root_keyed_by_its_stem(tmp_path, monkeypatch):
    _ns, core, ret = _load(tmp_path, monkeypatch)
    stem = build_backup(core.APP_DIR, "stats.db", "20260101T000000")
    scan = ret.gather_retained_artifacts()
    graph = ret._kernel.build_graph(scan.members)
    assert [root.id for root in graph.roots] == [stem.name]
    # `-wal` and the sidecar travel with the stem rather than being counted as
    # two more roots against the family budget.
    assert graph.roots[0].own_member_ids == frozenset({
        f"{stem.name}-wal", f"{stem.name}.classification.json",
    })


def test_a_machine_backup_whose_sidecar_no_longer_matches_is_unclassified(
    tmp_path, monkeypatch,
):
    _ns, core, ret = _load(tmp_path, monkeypatch)
    stem = build_backup(core.APP_DIR, "stats.db", "20260101T000000")
    stem.write_bytes(b"replaced")   # same name, different size
    scan = ret.gather_retained_artifacts()
    member = next(m for m in scan.members if m.id == stem.name)
    assert member.classification is None


def test_post_drain_evidence_does_not_stale_the_exact_backup_family(
    tmp_path, monkeypatch,
):
    _ns, core, ret = _load(tmp_path, monkeypatch)
    stem = build_backup(core.APP_DIR, "stats.db", "20260101T000000")
    survivor = core.APP_DIR / f"{stem.name}.post-drain-wal"
    survivor.write_bytes(b"late survivor")

    scan = ret.gather_retained_artifacts()
    by_id = {member.id: member for member in scan.members}

    assert by_id[stem.name].classification == "exact"
    assert by_id[survivor.name].kind == "backup"
    assert by_id[survivor.name].classification is None


@pytest.mark.parametrize("include", [False, True])
def test_include_backups_only_reaches_the_user_shaped_stem(
    tmp_path, monkeypatch, include,
):
    _ns, core, ret = _load(tmp_path, monkeypatch)
    user = build_backup(core.APP_DIR, "stats.db", "20260101T000000", machine=False)
    hand = core.APP_DIR / "stats.db.bak-pre-011-reversal"
    hand.write_bytes(b"hand-made")
    scan = ret.gather_retained_artifacts(include_backups=include)
    by_id = {member.id: member for member in scan.members}
    assert (by_id[user.name].classification is not None) is include
    # §3.7's third row is the fail-safe and does not move.
    assert by_id[hand.name].classification is None


def test_a_reference_outside_the_data_directory_is_reported_dangling(
    tmp_path, monkeypatch,
):
    _ns, core, ret = _load(tmp_path, monkeypatch)
    build_incident(
        core.APP_DIR, "stats.db", "20260101T000000",
        forensics=pathlib.Path("/somewhere/else/bundle.json"),
    )
    scan = ret.gather_retained_artifacts()
    graph = ret._kernel.build_graph(scan.members)
    assert "dangling-reference" in graph.roots[0].protected_reasons


def test_a_live_heal_request_marker_makes_its_bundle_active(tmp_path, monkeypatch):
    _ns, core, ret = _load(tmp_path, monkeypatch)
    bundle = build_bundle(core.APP_DIR, "stats.db", "20260101T000000")
    _write(core.APP_DIR / "stats-corruption-heal.pending",
           {"schemaVersion": 1, "forensicsPath": str(bundle)})
    scan = ret.gather_retained_artifacts()
    member = next(m for m in scan.members if m.id.endswith(bundle.name))
    assert member.active is True


def test_a_heal_ring_entry_at_detected_makes_its_evidence_active(
    tmp_path, monkeypatch,
):
    _ns, core, ret = _load(tmp_path, monkeypatch)
    bundle = build_bundle(core.APP_DIR, "stats.db", "20260101T000000")
    _write(core.LOG_DIR / "stats-heal-events.json", {
        "schemaVersion": 1,
        "events": [{"outcome": "detected", "forensicsPath": str(bundle)}],
    })
    scan = ret.gather_retained_artifacts()
    assert next(m for m in scan.members if m.id.endswith(bundle.name)).active


def test_a_settled_heal_ring_entry_does_not(tmp_path, monkeypatch):
    _ns, core, ret = _load(tmp_path, monkeypatch)
    bundle = build_bundle(core.APP_DIR, "stats.db", "20260101T000000")
    _write(core.LOG_DIR / "stats-heal-events.json", {
        "schemaVersion": 1,
        "events": [{"outcome": "rebuilt", "forensicsPath": str(bundle)}],
    })
    scan = ret.gather_retained_artifacts()
    assert not next(m for m in scan.members if m.id.endswith(bundle.name)).active


def test_a_pending_quarantine_marker_leaves_its_incident_unfinished(
    tmp_path, monkeypatch,
):
    _ns, core, ret = _load(tmp_path, monkeypatch)
    incident = build_incident(core.APP_DIR, "stats.db", "20260101T000000")
    _write(core.APP_DIR / "stats.db.quarantine-pending.json",
           {"schemaVersion": 1, "incidentPath": str(incident)})
    scan = ret.gather_retained_artifacts()
    member = next(m for m in scan.members if m.id.endswith(incident.name))
    assert member.finalized is False


def test_an_unrecognized_directory_in_quarantine_is_protected_not_swept(
    tmp_path, monkeypatch,
):
    _ns, core, ret = _load(tmp_path, monkeypatch)
    stray = core.APP_DIR / "quarantine" / "something-a-human-made"
    stray.mkdir(parents=True)
    scan = ret.gather_retained_artifacts()
    graph = ret._kernel.build_graph(scan.members)
    reasons = graph.roots_by_id[f"quarantine/{stray.name}"].protected_reasons
    assert "unrecognized-kind" in reasons


def test_the_entry_cap_reports_a_partial_scan(tmp_path, monkeypatch):
    _ns, core, ret = _load(tmp_path, monkeypatch)
    for index in range(10):
        build_incident(core.APP_DIR, "stats.db", _stamp(index))
    scan = ret.gather_retained_artifacts(max_entries=6)
    assert scan.partial is True
    assert scan.entries_seen == 6


# --------------------------------------------------------------------------
# §7.5 — the two latency gates
# --------------------------------------------------------------------------
#
# The corpus is built at or above the maintainer's real install (350 roots,
# ~1150 filesystem members). A fixture smaller than that would make the
# wall-clock leg unable to fail, which is the vacuity trap §10.1 names.

PRODUCTION_INCIDENTS = 142
PRODUCTION_BUNDLES = 132
PRODUCTION_RECORDS = 50
PRODUCTION_BACKUPS = 30


#: 28 of the maintainer's 142 incidents name both a bundle and a rebuild
#: record, and that record names the same bundle — the real diamond §5.4's
#: topological group order exists for. The rest are unreferenced.
PRODUCTION_LINKED_INCIDENTS = 30


#: WAL-evidence directories, the ONLY depth-2 shape under `logs/`. Without
#: them the walk never enters that recursion, so neither latency gate measures
#: it and a regression there moves no assertion.
PRODUCTION_EVIDENCE_DIRS = 12


def build_production_corpus(app_dir: pathlib.Path) -> None:
    """A corpus at or above the maintainer's measured 350 roots / 1150 members.

    §7.5 enumerates that 350 as 139 incidents, 132 bundles, 49 rebuild records
    and 30 backup stems, so the corpus is built to those four counts.
    """
    bundles = [
        build_bundle(app_dir, "stats.db", _stamp(i))
        for i in range(PRODUCTION_BUNDLES)
    ]
    records = [
        build_rebuild_record(app_dir, _stamp(i), forensics=bundles[i])
        for i in range(PRODUCTION_RECORDS)
    ]
    for i in range(PRODUCTION_INCIDENTS):
        linked = i < PRODUCTION_LINKED_INCIDENTS
        build_incident(
            app_dir, "stats.db", _stamp(i),
            forensics=bundles[i] if linked else None,
            record=records[i] if linked else None,
            shape=f"shape{i % 4}",
            moved=["stats.db", "stats.db-wal", "stats.db-shm"],
            sidecar=True,
        )
    for i in range(PRODUCTION_EVIDENCE_DIRS):
        # Beside an existing bundle, sharing its stem — the shape the real
        # heal path writes, and the only one that makes the walk recurse.
        build_evidence_dir(app_dir, "stats.db", _stamp(i))
    for i in range(PRODUCTION_BACKUPS):
        stem = build_backup(app_dir, "cache.db", _stamp(i))
        (app_dir / f"{stem.name}-shm").write_bytes(b"s" * 8)


def production_artifact_count() -> int:
    return (
        PRODUCTION_INCIDENTS + PRODUCTION_BUNDLES + PRODUCTION_RECORDS
        + PRODUCTION_BACKUPS
    )


def _expected_operations(app_dir: pathlib.Path) -> "tuple[int, int]":
    """`(scandir, lstat)` derived from the corpus, not from the walk.

    Computed with `pathlib` so the expectation is independent of the code under
    test: one `scandir` per recognized root directory, one `lstat` per entry.
    """
    containers = [app_dir, app_dir / "quarantine", app_dir / "logs"]
    nested = sorted(
        [p for p in (app_dir / "quarantine").iterdir() if p.is_dir()]
        + [
            p for p in (app_dir / "logs").iterdir()
            if p.is_dir() and "-corruption-forensics-" in p.name
        ]
    )
    scandirs = len(containers) + len(nested)
    lstats = sum(len(list(d.iterdir())) for d in containers + nested)
    return scandirs, lstats


def test_the_walk_performs_one_scandir_per_root_and_one_lstat_per_entry(
    tmp_path, monkeypatch,
):
    _ns, core, ret = _load(tmp_path, monkeypatch)
    build_production_corpus(core.APP_DIR)
    expected_scandirs, expected_lstats = _expected_operations(core.APP_DIR)

    ops = {"scandir": 0, "lstat": 0}
    real_scandir, real_lstat = os.scandir, os.lstat

    def counting_scandir(path):
        ops["scandir"] += 1
        return real_scandir(path)

    def counting_lstat(path):
        ops["lstat"] += 1
        return real_lstat(path)

    monkeypatch.setattr(ret, "_walk_scandir", counting_scandir)
    monkeypatch.setattr(ret, "_walk_lstat", counting_lstat)
    scan = ret.gather_retained_artifacts()

    assert scan.partial is False
    assert ops["scandir"] == expected_scandirs
    assert ops["lstat"] == expected_lstats
    # Non-vacuity: the corpus really is at production scale.
    assert expected_scandirs >= 143 and expected_lstats >= 1150
    # …and the depth-2 recursion under `logs/` really is exercised, so the
    # counts above are not a measurement of the quarantine branch alone.
    assert PRODUCTION_EVIDENCE_DIRS > 0
    assert sum(
        1 for p in (core.APP_DIR / "logs").iterdir() if p.is_dir()
    ) == PRODUCTION_EVIDENCE_DIRS


def test_the_corpus_the_latency_gate_uses_is_at_production_scale(
    tmp_path, monkeypatch,
):
    """A threshold test whose fixture is below the real scale cannot fail.

    §7.5's "350 roots" is enumerated there as 139 incidents, 132 bundles, 49
    rebuild records and 30 backup stems, so that is what is counted — the
    graph's own root set is smaller, because a bundle a manifest names is a
    member of that incident rather than a root of its own.
    """
    _ns, core, ret = _load(tmp_path, monkeypatch)
    build_production_corpus(core.APP_DIR)
    scan = ret.gather_retained_artifacts()
    graph = ret._kernel.build_graph(scan.members)
    assert production_artifact_count() >= 350
    assert scan.entries_seen >= 1150
    # §3.1 measured the maintainer's own graph root set at 269.
    assert len(graph.roots) >= 269


def test_the_walk_stays_under_the_wall_clock_backstop(tmp_path, monkeypatch):
    """250 ms, roughly fifty times the measured 3.5–4.8 ms. Warm."""
    _ns, core, ret = _load(tmp_path, monkeypatch)
    build_production_corpus(core.APP_DIR)
    ret.gather_retained_artifacts()          # warm the directory cache
    started = time.perf_counter()
    ret.gather_retained_artifacts()
    elapsed = time.perf_counter() - started
    assert elapsed < 0.250, f"metadata walk took {elapsed * 1000:.1f} ms"
