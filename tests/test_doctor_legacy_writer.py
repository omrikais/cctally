"""#825/#832 Task 1.8 — the legacy-writer evidence sidecar and the doctor leg
`journal.legacy_writer` (spec section 8, case F9, operator Q4).

WARN while the latest excluded population was first observed under seven days
ago, naming the count, the last-seen time and the remedy; OK with a historical
note after seven quiet days; a new excluded write — including a backdated
event or a record added to an earlier segment or a new bootstrap segment —
re-raises it; a rebuild over unchanged evidence does not restart the age; a
missing summary is "unavailable", never zero.
"""
import datetime as dt
import json
import pathlib
import sys

import pytest

import _cctally_core
from conftest import load_script, redirect_paths

sys.path.insert(0, str(pathlib.Path(load_script()["__file__"]).resolve().parent))

import _lib_legacy_writer_evidence as LW  # noqa: E402

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 10, 9, 12, 0, 0, tzinfo=UTC)
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
REMEDY = ("an older cctally process (for example a dashboard started before "
          "the upgrade) is still writing; restart it")


def _state(**overrides):
    import _lib_doctor
    from dataclasses import fields

    kwargs = {f.name: None for f in fields(_lib_doctor.DoctorState)}
    kwargs.update(overrides)
    return _lib_doctor.DoctorState(**kwargs)


def _leg(now, legacy_writer):
    import _lib_doctor
    return _lib_doctor._check_journal_legacy_writer(
        _state(now_utc=now, legacy_writer=legacy_writer))


def _data(count, digest, evidence=None, cutover="account-bound-cutover-v1"):
    return {"available": True, "cutoverId": cutover, "excludedCount": count,
            "coordinateDigest": digest, "evidence": evidence}


def _evidence_doc(count, digest, first):
    return LW.to_document(LW.Evidence("account-bound-cutover-v1", count,
                                      digest, first.isoformat()))


# --------------------------------------------------------------------------
# pure update rule
# --------------------------------------------------------------------------

def test_update_rule_keeps_the_age_for_an_unchanged_population():
    first = LW.observe(None, cutover_id="c", excluded_count=2,
                       coordinate_digest=DIGEST_A, now=T0)
    later = LW.observe(first, cutover_id="c", excluded_count=2,
                       coordinate_digest=DIGEST_A,
                       now=T0 + dt.timedelta(days=3))
    assert later is first
    moved = LW.observe(first, cutover_id="c", excluded_count=2,
                       coordinate_digest=DIGEST_B,
                       now=T0 + dt.timedelta(days=3))
    assert moved.first_observed_at == (T0 + dt.timedelta(days=3)).isoformat()
    assert LW.observe(None, cutover_id="c", excluded_count=0,
                      coordinate_digest=None, now=T0) is None


def test_document_round_trip_and_rejection():
    ev = LW.Evidence("c", 3, DIGEST_A, T0.isoformat())
    assert LW.from_document(LW.to_document(ev)) == ev
    assert LW.from_document({"schemaVersion": 1}) is None
    assert LW.from_document("garbage") is None


# --------------------------------------------------------------------------
# the leg
# --------------------------------------------------------------------------

def test_warn_within_seven_days_then_ok_with_a_historical_note():
    doc = _evidence_doc(3, DIGEST_A, T0)
    warn = _leg(T0 + dt.timedelta(days=2), _data(3, DIGEST_A, doc))
    assert warn.id == "journal.legacy_writer"
    assert warn.severity == "warn"
    assert "3" in warn.summary
    assert T0.isoformat() in warn.summary
    assert warn.remediation == REMEDY
    quiet = _leg(T0 + dt.timedelta(days=7, seconds=1), _data(3, DIGEST_A, doc))
    assert quiet.severity == "ok"
    assert "3" in quiet.summary and "7" in quiet.summary
    assert quiet.details["state"] == "quiet"


def test_a_new_excluded_write_re_raises_the_warning():
    old = _evidence_doc(3, DIGEST_A, T0)
    now = T0 + dt.timedelta(days=30)
    # A new population (one more record; or a backdated event, or a record in
    # an earlier/new bootstrap segment: all change the coordinate digest).
    leg = _leg(now, _data(4, DIGEST_B, old))
    assert leg.severity == "warn"
    assert leg.details["lastSeenAt"] == now.isoformat(timespec="seconds")


def test_a_missing_sidecar_only_lengthens_the_warning():
    leg = _leg(T0, _data(2, DIGEST_A, None))
    assert leg.severity == "warn"


def test_unavailable_is_never_zero():
    for data in (None, {"available": False}):
        leg = _leg(T0, data)
        assert leg.severity == "ok"
        assert "unavailable" in leg.summary
        assert leg.details["available"] is False


def test_no_cutover_and_no_exclusions_are_ok():
    assert "cutover" in _leg(T0, _data(0, None, cutover=None)).summary
    clean = _leg(T0, _data(0, None))
    assert clean.severity == "ok"
    assert clean.details["excludedCount"] == 0


def test_the_leg_is_registered_in_the_journal_category():
    import _lib_doctor
    journal = [legs for cat, _title, legs in _lib_doctor._CATEGORY_DEFINITIONS
               if cat == "journal"][0]
    assert ("journal.legacy_writer", "_check_journal_legacy_writer") in journal


# --------------------------------------------------------------------------
# sidecar maintenance by ingest and rebuild; deep doctor recompute
# --------------------------------------------------------------------------

@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


def _raw_append(segment, records):
    import _lib_journal as J
    jdir = _cctally_core.JOURNAL_DIR
    jdir.mkdir(parents=True, exist_ok=True)
    with open(jdir / segment, "ab") as fh:
        for record in records:
            fh.write(J.encode_line(record))


def _sidecar():
    path = _cctally_core.LEGACY_WRITER_EVIDENCE_PATH
    return json.loads(path.read_text()) if path.exists() else None


def _legacy_obs(J, at, pct):
    return J.unstamped_record(J.make_obs(
        at=at, src="statusline", provider="claude",
        payload={"weekly_percent": pct}))


def test_ingest_records_rebuild_keeps_and_new_population_restarts(
        ns, monkeypatch):
    import _cctally_journal as jr
    import _lib_journal as J
    clock = {"now": T0}
    monkeypatch.setattr(jr, "_evidence_now", lambda: clock["now"])
    jr.append_record(J.make_obs(at="2026-10-09T09:00:00Z", src="statusline",
                                provider="claude",
                                payload={"weekly_percent": 1.0}))
    jr.run_stats_ingest(mode="authoritative")
    jr.ensure_account_bound_cutover()
    seg = jr.journal_high_water()[0]
    assert _sidecar() is None  # nothing excluded yet: no sidecar at all

    _raw_append(seg, [_legacy_obs(J, "2026-10-09T10:00:00Z", 70.0)])
    jr.run_stats_ingest(mode="authoritative")
    first = _sidecar()
    assert first["excludedCount"] == 1
    assert first["firstObservedAt"] == T0.isoformat(timespec="seconds")

    # A rebuild over unchanged evidence does not restart the age.
    clock["now"] = T0 + dt.timedelta(days=8)
    jr.rebuild_stats_index(context=jr.RebuildContext(trigger="test-fixture"))
    assert _sidecar() == first

    # A backdated legacy event (old `at`, new physical coordinate) counts.
    _raw_append(seg, [_legacy_obs(J, "2020-01-01T00:00:00Z", 71.0)])
    jr.run_stats_ingest(mode="authoritative")
    second = _sidecar()
    assert second["excludedCount"] == 2
    assert second["firstObservedAt"] == clock["now"].isoformat(
        timespec="seconds")

    # F4: a record added to an EARLIER segment after the warning aged out is
    # invisible to ingest (behind its cursor) but a rebuild recomputes the
    # whole population and re-raises it.
    clock["now"] = T0 + dt.timedelta(days=20)
    _raw_append("observations-2026-01.jsonl",
                [_legacy_obs(J, "2026-01-05T00:00:00Z", 72.0)])
    jr.rebuild_stats_index(context=jr.RebuildContext(trigger="test-fixture"))
    third = _sidecar()
    assert third["excludedCount"] == 3
    assert third["firstObservedAt"] == clock["now"].isoformat(
        timespec="seconds")


def test_doctor_json_carries_the_leg(ns, monkeypatch, capsys):
    import _cctally_journal as jr
    import _lib_journal as J
    jr.append_record(J.make_obs(at="2026-10-09T09:00:00Z", src="statusline",
                                provider="claude",
                                payload={"weekly_percent": 1.0}))
    jr.run_stats_ingest(mode="authoritative")
    jr.ensure_account_bound_cutover()
    seg = jr.journal_high_water()[0]
    _raw_append(seg, [_legacy_obs(J, "2026-10-09T10:00:00Z", 70.0)])
    jr.run_stats_ingest(mode="authoritative")
    import argparse
    import _cctally_doctor
    rc = _cctally_doctor.cmd_doctor(argparse.Namespace(
        json=True, quiet=False, verbose=False, deep=True, fix=False))
    out = json.loads(capsys.readouterr().out)
    legs = {check["id"]: check
            for category in out["categories"] for check in category["checks"]}
    leg = legs["journal.legacy_writer"]
    assert leg["severity"] == "warn"
    assert leg["details"]["excludedCount"] == 1
    assert rc in (0, 2)


# --------------------------------------------------------------------------
# #825/#832 fix round B, P2-2: a non-current index falls back to the sidecar
# --------------------------------------------------------------------------

def test_an_unavailable_summary_falls_back_to_the_durable_sidecar():
    """An older process's epoch-1017 republish leaves no current summary, but
    the durable evidence still names the excluded population and when it was
    first observed: WARN with the approved content, not OK/unavailable."""
    doc = _evidence_doc(3, DIGEST_A, T0)
    warn = _leg(T0 + dt.timedelta(days=2), {"available": False,
                                           "evidence": doc})
    assert warn.severity == "warn"
    assert warn.summary == (
        "3 record(s) an older cctally process wrote after the upgrade were "
        f"excluded from accounting (last seen {T0.isoformat()})")
    assert warn.remediation == REMEDY
    assert warn.details["available"] is False
    assert warn.details["excludedCount"] == 3
    quiet = _leg(T0 + dt.timedelta(days=8), {"available": False,
                                            "evidence": doc})
    assert quiet.severity == "ok"
    assert quiet.details["state"] == "quiet"
    # A sidecar with nothing excluded, or an invalid one, stays unavailable.
    for evidence in (_evidence_doc(0, DIGEST_A, T0), {"schemaVersion": 1}):
        leg = _leg(T0, {"available": False, "evidence": evidence})
        assert "unavailable" in leg.summary


def test_doctor_warns_from_the_sidecar_when_the_index_is_not_current(
        ns, monkeypatch):
    import sqlite3
    import _cctally_doctor
    import _cctally_journal as jr
    import _lib_doctor
    import _lib_journal as J
    monkeypatch.setattr(jr, "_evidence_now", lambda: T0)
    jr.append_record(J.make_obs(at="2026-10-09T09:00:00Z", src="statusline",
                                provider="claude",
                                payload={"weekly_percent": 1.0}))
    jr.run_stats_ingest(mode="authoritative")
    jr.ensure_account_bound_cutover()
    seg = jr.journal_high_water()[0]
    _raw_append(seg, [_legacy_obs(J, "2026-10-09T10:00:00Z", 70.0)])
    jr.run_stats_ingest(mode="authoritative")
    assert _sidecar()["excludedCount"] == 1
    # An older process republished its own epoch over the index.
    conn = sqlite3.connect(_cctally_core.DB_PATH)
    try:
        conn.execute(
            f"PRAGMA user_version = {_cctally_core.STATS_INDEX_EPOCH - 1}")
        conn.commit()
    finally:
        conn.close()
    data = _cctally_doctor._gather_legacy_writer(None, None)
    assert data["available"] is False
    leg = _lib_doctor._check_journal_legacy_writer(
        _state(now_utc=T0 + dt.timedelta(hours=1), legacy_writer=data))
    assert leg.severity == "warn"
    assert leg.remediation == REMEDY
    assert "1 record(s)" in leg.summary


def test_the_upgrade_to_the_v2_directory_keeps_the_evidence_age(
        ns, monkeypatch):
    """Review RV-5 (#797/#823/#840): the sidecar moved from
    `statusline-account-bound-v1/` to `-v2/`. An upgraded store whose v1
    evidence names the same excluded population keeps its first-observed
    instant — a rebuild over unchanged evidence does not restart the age —
    read from v1 only, never writing, renaming or deleting anything there."""
    import _cctally_doctor
    import _cctally_journal as jr
    import _lib_doctor
    import _lib_journal as J
    clock = {"now": T0}
    monkeypatch.setattr(jr, "_evidence_now", lambda: clock["now"])
    jr.append_record(J.make_obs(at="2026-10-09T09:00:00Z", src="statusline",
                                provider="claude",
                                payload={"weekly_percent": 1.0}))
    jr.run_stats_ingest(mode="authoritative")
    jr.ensure_account_bound_cutover()
    seg = jr.journal_high_water()[0]
    _raw_append(seg, [_legacy_obs(J, "2026-10-09T10:00:00Z", 70.0)])
    jr.run_stats_ingest(mode="authoritative")
    first = _sidecar()
    assert first["firstObservedAt"] == T0.isoformat(timespec="seconds")
    # The previous release kept it under v1; the v2 directory starts without it.
    v1 = _cctally_core.STATUSLINE_ACCOUNT_BOUND_V1_DIR
    v1.mkdir(parents=True, exist_ok=True)
    (v1 / "legacy-writer-evidence.json").write_text(json.dumps(first))
    _cctally_core.LEGACY_WRITER_EVIDENCE_PATH.unlink()
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns)
              for p in sorted(v1.rglob("*")) if p.is_file()}

    clock["now"] = T0 + dt.timedelta(days=8)
    # Doctor before anything rewrites v2 reports the continuing age.
    data = _cctally_doctor._gather_legacy_writer(None, None)
    leg = _lib_doctor._check_journal_legacy_writer(
        _state(now_utc=clock["now"], legacy_writer=data))
    assert leg.severity == "ok" and leg.details["state"] == "quiet", leg
    # The rebuild over unchanged evidence carries the first observation.
    jr.rebuild_stats_index(context=jr.RebuildContext(trigger="test-fixture"))
    assert _sidecar() == first
    data = _cctally_doctor._gather_legacy_writer(None, None)
    leg = _lib_doctor._check_journal_legacy_writer(
        _state(now_utc=clock["now"], legacy_writer=data))
    assert leg.severity == "ok" and leg.details["state"] == "quiet", leg
    assert {p: (p.read_bytes(), p.stat().st_mtime_ns)
            for p in sorted(v1.rglob("*")) if p.is_file()} == before

    # A different v1 population is not this one: first observed now.
    _cctally_core.LEGACY_WRITER_EVIDENCE_PATH.unlink()
    other = dict(first, excludedCount=first["excludedCount"] + 5)
    (v1 / "legacy-writer-evidence.json").write_text(json.dumps(other))
    jr.rebuild_stats_index(context=jr.RebuildContext(trigger="test-fixture"))
    assert _sidecar()["firstObservedAt"] == clock["now"].isoformat(
        timespec="seconds")


# --------------------------------------------------------------------------
# #825/#832 spec revision 5 (section 8): an incomplete legacy spool inventory
# --------------------------------------------------------------------------

#: Operator-approved revision 5 wording (spec section 8), verbatim.
INVENTORY_TEXT = (
    "cctally cannot read {path} ({operation}: {error}), one of the "
    "status-line files an older cctally release used. Until it can, cctally "
    "cannot tell whether an older process changed it. Check the file's "
    "permissions and the disk.")
INVENTORY_PAUSED_TEXT = (
    "Automatic credit and reset detection is paused because of this, and "
    "making the file readable again does not end the pause (see "
    "`accounting.outcome_quarantine`); usage recording continues.")
_INVENTORY = {"available": False, "path": "/data/statusline-candidates/c.json",
              "operation": "read", "error": "Input/output error [EIO]"}


@pytest.mark.parametrize("paused", [False, True])
def test_an_incomplete_inventory_warns_at_zero_excluded_records(paused):
    """The leg reports the inventory independently of the excluded count: a
    WARN at zero, naming the path, operation and error, and claiming the
    pause only when the validated index holds the durable marker."""
    data = _data(0, None)
    data["legacySpoolInventory"] = {**_INVENTORY, "paused": paused}
    result = _leg(T0, data)
    assert result.severity == "warn"
    assert "unavailable" in result.summary
    text = INVENTORY_TEXT.format(**_INVENTORY)
    if paused:
        text += " " + INVENTORY_PAUSED_TEXT
    assert result.remediation == text
    assert result.details["excludedCount"] == 0
    assert result.details["legacySpoolInventory"]["paused"] is paused
    # Never claims an old writer exists.
    assert REMEDY not in result.remediation
    assert "excluded" not in result.summary


def test_an_incomplete_inventory_beside_recent_exclusions_keeps_both():
    data = _data(1, DIGEST_A, _evidence_doc(1, DIGEST_A, T0))
    data["legacySpoolInventory"] = {**_INVENTORY, "paused": False}
    result = _leg(T0 + dt.timedelta(days=1), data)
    assert result.severity == "warn"
    assert "1 record(s)" in result.summary
    assert REMEDY in result.remediation
    assert INVENTORY_TEXT.format(**_INVENTORY) in result.remediation


def _legs(capsys, deep):
    import argparse
    import _cctally_doctor
    _cctally_doctor.cmd_doctor(argparse.Namespace(
        json=True, quiet=False, verbose=False, deep=deep, fix=False))
    out = json.loads(capsys.readouterr().out)
    return {check["id"]: check
            for category in out["categories"] for check in category["checks"]}


def _unreadable_legacy_candidate(monkeypatch):
    import builtins
    import errno
    import os
    import _cctally_journal as jr
    cand = _cctally_core.STATUSLINE_CANDIDATE_DIR
    cand.mkdir(parents=True, exist_ok=True)
    (cand / "legacy-session.json").write_text('{"sevenDay": 70}')

    def refusing_open(file, *args, **kwargs):
        if (not isinstance(file, int)
                and os.path.dirname(os.fspath(file)) == os.fspath(cand)):
            raise OSError(errno.EIO, "Input/output error", os.fspath(file))
        return builtins.open(file, *args, **kwargs)

    monkeypatch.setattr(jr, "open", refusing_open, raising=False)
    return cand / "legacy-session.json"


def _journal_snapshot():
    import _cctally_journal as jr
    return {s: (_cctally_core.JOURNAL_DIR / s).read_bytes()
            for s in jr.list_segments()}


@pytest.mark.parametrize("deep", [False, True])
def test_doctor_reports_an_unreadable_legacy_file_read_only(
        ns, monkeypatch, capsys, deep):
    """Doctor can be the first process to meet the error: it warns with the
    path, operation and error while nothing is excluded, appends nothing,
    changes no spool file, and claims no pause (no durable marker). Once the
    reducer has journaled the `inventory-incomplete` trigger and it is
    folded, the leg adds the pause sentence and `accounting.outcome_
    quarantine` warns with the revision 5 marker wording."""
    import _cctally_journal as jr
    import _lib_accounts
    import _lib_journal as J
    # Fix round K12 (K11R-15): the journal names a Claude account, so the
    # per-account marker wording below is asserted on a real row.
    jr.append_record(J.make_account_observe(
        at="2026-10-09T08:00:00Z", provider="claude", natural_id="A",
        account_key=_lib_accounts.account_key("claude", "A")))
    jr.append_record(J.make_obs(at="2026-10-09T09:00:00Z", src="statusline",
                                provider="claude",
                                payload={"weekly_percent": 1.0}))
    jr.run_stats_ingest(mode="authoritative")
    jr.ensure_account_bound_cutover()
    path = _unreadable_legacy_candidate(monkeypatch)
    before = _journal_snapshot()
    leg = _legs(capsys, deep)["journal.legacy_writer"]
    assert leg["severity"] == "warn", leg
    assert leg["details"]["excludedCount"] == 0
    inventory = leg["details"]["legacySpoolInventory"]
    assert inventory["path"] == str(path)
    assert inventory["operation"] == "read"
    assert inventory["paused"] is False
    assert leg["remediation"] == INVENTORY_TEXT.format(
        path=str(path), operation="read", error=inventory["error"])
    assert "EIO" in inventory["error"]
    assert _journal_snapshot() == before, "doctor appended to the journal"
    assert path.read_bytes() == b'{"sevenDay": 70}'

    import _cctally_statusline as sl
    assert sl._rescan_legacy_spool() is True
    jr.run_stats_ingest(mode="authoritative")
    legs = _legs(capsys, deep)
    leg = legs["journal.legacy_writer"]
    assert leg["details"]["legacySpoolInventory"]["paused"] is True
    assert leg["remediation"] == INVENTORY_TEXT.format(
        path=str(path), operation="read",
        error=leg["details"]["legacySpoolInventory"]["error"]
    ) + " " + INVENTORY_PAUSED_TEXT
    quarantine = legs["accounting.outcome_quarantine"]
    # The marker is durable in the validated index, and the account the
    # journal names is paused with the inventory-marker wording.
    assert [m["reason"] for m in
            quarantine["details"]["unresolvedSpoolMarkers"]] == [
        "inventory-incomplete"], quarantine
    rows = quarantine["details"]["accounts"]
    assert rows, quarantine
    for row in rows:
        assert row["unresolvedSpoolMarker"], row
        assert "could not read the status-line files" in row["message"], row


# --------------------------------------------------------------------------
# K9K10-R6: a failing deep scan is unavailable, never zero or unfiltered
# --------------------------------------------------------------------------

def _seed_excluded(ns):
    import _cctally_journal as jr
    import _lib_journal as J
    jr.append_record(J.make_obs(at="2026-10-09T09:00:00Z", src="statusline",
                                provider="claude",
                                payload={"weekly_percent": 1.0}))
    jr.run_stats_ingest(mode="authoritative")
    jr.ensure_account_bound_cutover()
    seg = jr.journal_high_water()[0]
    _raw_append(seg, [_legacy_obs(J, "2026-10-09T10:00:00Z", 70.0)])
    jr.run_stats_ingest(mode="authoritative")
    return seg


@pytest.mark.parametrize("failure", ["boundary-lookup", "segment-read"])
def test_a_failing_deep_scan_reports_its_checks_unavailable(
        ns, monkeypatch, capsys, failure):
    """K9K10-R6: when the deep scan's boundary lookup raised, the scan set
    the boundary to None: `excluded_population(None)` reported zero excluded
    records and the conflict scan ran with admission disabled. A segment the
    scan could not read was skipped. Each must render the affected checks
    unavailable, with the cause — never zero, never an unfiltered scan."""
    import pathlib
    import _cctally_journal as jr
    seg = _seed_excluded(ns)
    if failure == "boundary-lookup":
        def unreadable(self, high_water):
            raise jr.AccountBoundCutoverUnavailable(
                5, "injected boundary lookup failure", seg)

        monkeypatch.setattr(jr._PassBoundary, "resolve", unreadable)
    else:
        real_read_bytes = pathlib.Path.read_bytes

        def refusing_read_bytes(self):
            if self.name == seg and self.parent == _cctally_core.JOURNAL_DIR:
                raise OSError(5, "injected segment read failure", str(self))
            return real_read_bytes(self)

        monkeypatch.setattr(pathlib.Path, "read_bytes", refusing_read_bytes)
    legs = _legs(capsys, True)
    writer = legs["journal.legacy_writer"]
    assert writer["severity"] == "warn", writer
    assert writer["details"]["available"] is False, writer
    assert writer["details"].get("excludedCount") != 0, writer
    assert "unavailable" in writer["summary"], writer
    assert "injected" in writer["summary"], writer
    conflicts = legs["journal.conflicts"]
    assert conflicts["severity"] == "warn", conflicts
    assert conflicts["details"]["available"] is False, conflicts
    assert "injected" in conflicts["summary"], conflicts


def test_an_unlistable_journal_is_never_an_empty_one(ns, monkeypatch, capsys):
    """K9K10-R6 (shallow path): a journal directory that cannot be listed
    read as zero segments. It is unreadable, with the cause."""
    import errno
    import _cctally_journal as jr
    _seed_excluded(ns)

    def unlistable():
        raise PermissionError(errno.EACCES, "injected listing failure",
                              str(_cctally_core.JOURNAL_DIR))

    monkeypatch.setattr(jr, "list_segments", unlistable)
    presence = _legs(capsys, False)["journal.presence"]
    assert presence["severity"] == "warn", presence
    assert "injected listing failure" in presence["summary"], presence


# --------------------------------------------------------------------------
# Fix round K12 (K11R-13): the doctor inventory probe never fails open
# --------------------------------------------------------------------------

def test_an_unexpected_inventory_failure_is_unavailable_never_complete(
        ns, monkeypatch, capsys):
    """An exception the probe does not expect read as "inventory complete"
    (None). It reports the inventory unavailable, with its cause."""
    import _cctally_journal as jr

    def broken():
        raise RuntimeError("injected inventory failure")

    monkeypatch.setattr(jr, "capture_legacy_spool_state", broken)
    leg = _legs(capsys, False)["journal.legacy_writer"]
    assert leg["severity"] == "warn", leg
    assert leg["summary"] == (
        "legacy status-line inventory unavailable "
        "(injected inventory failure)"), leg
    inventory = leg["details"]["legacySpoolInventory"]
    assert inventory["available"] is False, inventory
    assert "injected inventory failure" in inventory["unavailableCause"]
    # It names no file it could not read, and claims no old writer.
    assert REMEDY not in (leg.get("remediation") or "")
    assert "cannot read" not in (leg.get("remediation") or "")


def test_a_listing_error_without_a_filename_names_the_walked_root(
        ns, monkeypatch, capsys):
    """`os.walk` can hand `onerror` an error that carries no filename; the
    inventory named an empty path ("cannot read  (list: ...)"). It names the
    walked root instead."""
    import errno
    import os
    import _cctally_journal as jr
    cand = _cctally_core.STATUSLINE_CANDIDATE_DIR
    cand.mkdir(parents=True, exist_ok=True)
    real_walk = os.walk

    def walk(top, *args, **kwargs):
        if os.fspath(top) == os.fspath(cand):
            kwargs["onerror"](OSError(errno.EIO, "Input/output error"))
            return iter(())
        return real_walk(top, *args, **kwargs)

    monkeypatch.setattr(jr.os, "walk", walk)
    leg = _legs(capsys, False)["journal.legacy_writer"]
    inventory = leg["details"]["legacySpoolInventory"]
    assert inventory["path"] == str(cand), inventory
    assert inventory["operation"] == "list", inventory
    assert leg["remediation"].startswith(f"cctally cannot read {cand} "), leg


# --------------------------------------------------------------------------
# Fix round K12 (K11R-12): the remediation names the resolved data directory
# --------------------------------------------------------------------------

def test_the_unreadable_journal_remediation_names_the_resolved_data_dir():
    import _lib_doctor
    result = _lib_doctor._check_journal_legacy_writer(_state(
        now_utc=T0, app_dir="/data/cctally-dev/",
        legacy_writer={"unavailableCause": "injected"}))
    assert result.remediation == (
        "Check the permissions of /data/cctally-dev/journal/ and the disk, "
        "then run `cctally doctor --deep` again")
    assert "~/.local/share/cctally" not in result.remediation
