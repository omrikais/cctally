"""Evidence store identity and loss (contract C8; spec sections 6.3 step 4,
6.4 F20, 6.10 R7-F7; acceptance row E).

The retention record gains a store identity and seeded loss intervals that
later passes preserve until they age out, and an internal evidence index
records each run's expected length and digest so missing or truncated runs
are discovered without a pre-seeded flag.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import stat

import pytest


def _ev():
    import _lib_test_evidence as EV
    return EV


STORE_A = "STORE-A+IMAGE-A:1"
STORE_B = "STORE-B+IMAGE-B:2"
DAY = 86400


# --- the retention record ------------------------------------------------------------

def test_seeding_a_new_store_records_its_identity_and_loss(tmp_path):
    EV = _ev()
    record = tmp_path / ".retention.json"
    loss = [{"start": "2026-10-01T00:00:00Z", "end": "2026-10-05T00:00:00Z",
             "reason": "store STORE-A+IMAGE-A:1 replaced without copying its evidence"}]
    EV.seed_retention_record(str(record), store_id=STORE_B, loss=loss)
    doc = json.loads(record.read_text())
    assert doc["store"] == STORE_B
    assert doc["seededLoss"] == loss
    assert doc["coverage"] == "degraded"
    assert doc["passes"] == 0 and doc["schemaVersion"] == 1
    assert [p.name for p in tmp_path.iterdir()] == [".retention.json"]


def test_seeding_without_loss_is_complete_and_reseeding_is_idempotent(tmp_path):
    EV = _ev()
    record = tmp_path / ".retention.json"
    EV.seed_retention_record(str(record), store_id=STORE_B, loss=[])
    assert json.loads(record.read_text())["coverage"] == "complete"
    entry = {"start": "2026-10-01T00:00:00Z", "end": None, "reason": "unknown interval"}
    EV.seed_retention_record(str(record), store_id=STORE_B, loss=[entry])
    EV.seed_retention_record(str(record), store_id=STORE_B, loss=[entry])
    doc = json.loads(record.read_text())
    assert doc["seededLoss"] == [entry] and doc["coverage"] == "degraded"


def test_seeding_refuses_another_stores_record(tmp_path):
    EV = _ev()
    record = tmp_path / ".retention.json"
    EV.seed_retention_record(str(record), store_id=STORE_A, loss=[])
    with pytest.raises(ValueError):
        EV.seed_retention_record(str(record), store_id=STORE_B, loss=[])


def test_a_record_without_a_store_is_legacy():
    EV = _ev()
    assert EV.retention_store({"passes": 3}) == "legacy"
    assert EV.retention_store(None) == "legacy"
    assert EV.retention_store({"store": STORE_B}) == STORE_B


def test_seeded_loss_is_carried_until_it_ages_out_and_keeps_coverage_degraded():
    EV = _ev()
    now = 1791676800  # 2026-10-10T00:00:00Z
    old = {"start": "2026-09-01T00:00:00Z", "end": "2026-09-02T00:00:00Z", "reason": "a"}
    recent = {"start": "2026-10-05T00:00:00Z", "end": "2026-10-06T00:00:00Z", "reason": "b"}
    open_ended = {"start": "2026-08-01T00:00:00Z", "end": None, "reason": "c"}
    previous = {"store": STORE_B, "seededLoss": [old, recent, open_ended]}
    kept = EV.carried_seeded_loss(previous, now, max_age_days=12)
    assert kept == [recent, open_ended]
    assert EV.retention_coverage(gaps=[], orphans=0, seeded_loss=kept) == "degraded"
    assert EV.retention_coverage(gaps=[], orphans=0, seeded_loss=[]) == "complete"
    assert EV.retention_coverage(gaps=[{"from_epoch": 1}], orphans=0,
                                 seeded_loss=[]) == "degraded"
    assert EV.carried_seeded_loss({"seededLoss": [recent]}, now + 30 * DAY,
                                  max_age_days=12) == []
    assert EV.carried_seeded_loss(None, now) == []


def test_loss_instants_accept_fractional_seconds_and_utc_offsets():
    """`_iso_epoch` reads whole and fractional seconds with `Z`, `+00:00`,
    another offset, or none (UTC), so a carried interval never fails on the
    spelling a writer chose."""
    EV = _ev()
    base = 1791590400  # 2026-10-10T00:00:00Z
    for text, want in (("2026-10-10T00:00:00Z", base), ("2026-10-10T00:00:00+00:00", base),
                       ("2026-10-10T00:00:00", base), ("2026-10-10T00:00:00.250Z", base + 0.25),
                       ("2026-10-10T00:00:00.123456+00:00", base + 0.123456),
                       ("2026-10-10T02:00:00+02:00", base), ("2026-10-09T19:30:00-0430", base)):
        assert abs(EV._iso_epoch(text) - want) < 1e-6, text
    with pytest.raises(ValueError):
        EV._iso_epoch("yesterday")
    now = base
    recent = {"start": "2026-10-05T00:00:00.5Z", "end": "2026-10-06T00:00:00.123+00:00",
              "reason": "b"}
    assert EV.carried_seeded_loss({"seededLoss": [recent]}, now, max_age_days=12) == [recent]


# --- the internal evidence index (R7-F7) ----------------------------------------------

def _run(root, remote, run_id, payload):
    d = root / remote / run_id
    d.mkdir(parents=True)
    (d / "manifest.json").write_bytes(payload)
    return len(payload), "sha256:" + hashlib.sha256(payload).hexdigest()


def test_the_index_records_runs_and_loss_per_store(tmp_path):
    EV = _ev()
    index = tmp_path / "evidence-index.json"
    assert EV.read_evidence_index(str(index)) == {"schema": 1, "stores": {}}
    EV.update_evidence_index(str(index), store_id=STORE_B, run_id="r1",
                             started_at="2026-10-09T00:00:00Z", ended_at="2026-10-09T00:30:00Z",
                             length=10, digest="sha256:" + "0" * 64, lock=contextlib.nullcontext())
    EV.record_store_loss(str(index), store_id=STORE_A, start="2026-10-01T00:00:00Z",
                         end=None, reason="drive failed", lock=contextlib.nullcontext())
    doc = EV.read_evidence_index(str(index))
    assert doc["schema"] == 1
    assert doc["stores"][STORE_B]["runs"][0]["runId"] == "r1"
    assert doc["stores"][STORE_B]["runs"][0]["length"] == 10
    assert doc["stores"][STORE_A]["loss"] == [{"start": "2026-10-01T00:00:00Z", "end": None,
                                              "reason": "drive failed"}]
    # re-indexing a run replaces its entry rather than duplicating it
    EV.update_evidence_index(str(index), store_id=STORE_B, run_id="r1",
                             started_at="2026-10-09T00:00:00Z", ended_at="2026-10-09T00:31:00Z",
                             length=11, digest="sha256:" + "1" * 64, lock=contextlib.nullcontext())
    runs = EV.read_evidence_index(str(index))["stores"][STORE_B]["runs"]
    assert len(runs) == 1 and runs[0]["length"] == 11


def test_missing_and_truncated_runs_are_discovered_without_a_flag(tmp_path):
    EV = _ev()
    root = tmp_path / "suite"
    index = tmp_path / "evidence-index.json"
    for run_id, payload in (("r-ok", b"x" * 100), ("r-trunc", b"y" * 100),
                            ("r-gone", b"z" * 100), ("r-edit", b"w" * 100)):
        length, digest = _run(root, "cctally-dev", run_id, payload)
        EV.update_evidence_index(str(index), store_id=STORE_B, run_id=run_id,
                                 started_at="2026-10-09T00:00:00Z",
                                 ended_at="2026-10-09T00:10:00Z", length=length, digest=digest,
                                 lock=contextlib.nullcontext())
    assert EV.verify_indexed_runs(str(index), store_id=STORE_B, root=str(root)) == []
    (root / "cctally-dev" / "r-trunc" / "manifest.json").write_bytes(b"y" * 40)
    (root / "cctally-dev" / "r-gone" / "manifest.json").unlink()
    (root / "cctally-dev" / "r-edit" / "manifest.json").write_bytes(b"v" * 100)
    found = EV.verify_indexed_runs(str(index), store_id=STORE_B, root=str(root))
    assert sorted((f["runId"], f["kind"]) for f in found) == [
        ("r-edit", "digest"), ("r-gone", "missing"), ("r-trunc", "truncated")]
    assert EV.verify_indexed_runs(str(index), store_id=STORE_A, root=str(root)) == []
