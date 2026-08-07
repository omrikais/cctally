"""doctor journal legs (DB journal redesign §9) — pure kernel, no DB/journal.

The four legs classify a constructed ``DoctorState`` over precomputed journal
evidence (the gather layer does the I/O). Covered states: no-journal (legacy
pre-cutover), healthy, un-writable dir, torn tail, mid-file malformed, stale
cursor, and a recent auto-heal incident.

`_lib_doctor` is imported off ``bin/`` — the same path mechanism the other
in-process doctor kernel tests use.
"""
import datetime as dt
import io
import pathlib
import sys

from conftest import load_script


sys.path.insert(0, str(pathlib.Path(load_script()["__file__"]).resolve().parent))


def _state(**overrides):
    import _lib_doctor
    from dataclasses import fields

    kwargs = {f.name: None for f in fields(_lib_doctor.DoctorState)}
    kwargs.update(overrides)
    return _lib_doctor.DoctorState(**kwargs)


# ── presence ──────────────────────────────────────────────────────────────

def test_presence_no_journal_is_ok_not_fail():
    import _lib_doctor
    r = _lib_doctor._check_journal_presence(_state(journal_present=False))
    assert r.severity == "ok" and r.id == "journal.presence"
    assert "pre-cutover" in r.summary


def test_presence_writable_ok():
    import _lib_doctor
    r = _lib_doctor._check_journal_presence(
        _state(journal_present=True, journal_appendable=True,
               journal_segment_count=3))
    assert r.severity == "ok"
    assert "3 segment" in r.summary


def test_presence_not_writable_warns():
    import _lib_doctor
    r = _lib_doctor._check_journal_presence(
        _state(journal_present=True, journal_appendable=False,
               journal_segment_count=2))
    assert r.severity == "warn"
    assert "not writable" in r.summary


# ── integrity ─────────────────────────────────────────────────────────────

def test_integrity_not_scanned_is_ok():
    import _lib_doctor
    r = _lib_doctor._check_journal_integrity(
        _state(journal_present=True, journal_malformed_count=None))
    assert r.severity == "ok" and "not scanned" in r.summary


def test_integrity_malformed_warns():
    import _lib_doctor
    r = _lib_doctor._check_journal_integrity(
        _state(journal_present=True, journal_malformed_count=2,
               journal_torn_tail_count=0))
    assert r.severity == "warn"
    assert "2 malformed" in r.summary
    assert r.details["malformed"] == 2


def test_integrity_torn_tail_is_ok():
    import _lib_doctor
    r = _lib_doctor._check_journal_integrity(
        _state(journal_present=True, journal_malformed_count=0,
               journal_torn_tail_count=1))
    assert r.severity == "ok"
    assert "torn tail" in r.summary


def test_integrity_clean_ok():
    import _lib_doctor
    r = _lib_doctor._check_journal_integrity(
        _state(journal_present=True, journal_malformed_count=0,
               journal_torn_tail_count=0))
    assert r.severity == "ok" and "no malformed" in r.summary


# ── index freshness (cursor lag) ────────────────────────────────────────────

def test_index_freshness_caught_up_ok():
    import _lib_doctor
    r = _lib_doctor._check_journal_index_freshness(
        _state(journal_present=True, journal_cursor_lag_bytes=0))
    assert r.severity == "ok" and "caught up" in r.summary


def test_index_freshness_small_gap_ok():
    import _lib_doctor
    r = _lib_doctor._check_journal_index_freshness(
        _state(journal_present=True, journal_cursor_lag_bytes=1024))
    assert r.severity == "ok"
    assert "within threshold" in r.summary


def test_index_freshness_large_gap_warns():
    import _lib_doctor
    r = _lib_doctor._check_journal_index_freshness(
        _state(journal_present=True,
               journal_cursor_lag_bytes=_lib_doctor._JOURNAL_CURSOR_LAG_WARN_BYTES + 1))
    assert r.severity == "warn"
    assert "behind journal" in r.summary
    assert "db rebuild --db stats" in (r.remediation or "")


def test_index_freshness_no_cursor_ok():
    import _lib_doctor
    r = _lib_doctor._check_journal_index_freshness(
        _state(journal_present=True, journal_cursor_lag_bytes=None))
    assert r.severity == "ok" and "no cursor" in r.summary


# ── auto-heal incidents ─────────────────────────────────────────────────────

def test_auto_heal_none_ok():
    import _lib_doctor
    r = _lib_doctor._check_journal_auto_heal(_state(journal_heal_incidents=[]))
    assert r.severity == "ok" and "no auto-heal" in r.summary


def test_auto_heal_recent_warns():
    import _lib_doctor
    r = _lib_doctor._check_journal_auto_heal(
        _state(journal_heal_incidents=[
            {"kind": "quarantine", "name": "stats.db-20260720T120000Z",
             "age_s": 2 * 86400}]))
    assert r.severity == "warn"
    assert "fired recently" in r.summary


def test_auto_heal_old_is_ok():
    import _lib_doctor
    r = _lib_doctor._check_journal_auto_heal(
        _state(journal_heal_incidents=[
            {"kind": "forensics",
             "name": "stats.db-corruption-forensics-20260101T120000Z.json",
             "age_s": 30 * 86400}]))
    assert r.severity == "ok"
    assert "last incident" in r.summary


def test_rebuild_cutover_incident_microseconds_have_an_age():
    import _cctally_doctor

    incident = _cctally_doctor._journal_heal_incident(
        "quarantine",
        "stats.db-20260726T120000_123456",
        dt.datetime(2026, 7, 26, 12, 0, 5, 123456, tzinfo=dt.timezone.utc),
    )
    assert incident["age_s"] == 5


# ── #374 same-revision conflict quarantine ────────────────────────────────
#
# TWO legs, because the two conditions are mutually exclusive: structural
# validation raises BEFORE event selection, so a structural violation means
# there is no `EffectiveSelection.conflicts` result to report at all.

def _conflict(event_id="wcs:o:abc:2026-07-18", rev=0):
    return {
        "eventId": event_id,
        "revision": rev,
        "contentHashes": ["sha256:aaa", "sha256:bbb"],
        "selectedHash": "sha256:aaa",
    }


def _protocol_violation(batch_id="batch:invalid", kind="marker_conflict"):
    return {
        "batchId": batch_id,
        "kind": kind,
        "evidence": {"phase": "begin"},
        "fingerprint": "sha256:" + "a" * 64,
    }


def test_conflicts_not_scanned_is_ok():
    import _lib_doctor
    r = _lib_doctor._check_journal_conflicts(
        _state(journal_present=True, journal_conflicts=None))
    assert (r.id, r.severity) == ("journal.conflicts", "ok")
    assert r.details["scanned"] is False


def test_conflicts_clean_selection_is_ok():
    import _lib_doctor
    r = _lib_doctor._check_journal_conflicts(
        _state(journal_present=True, journal_conflicts=[]))
    assert r.severity == "ok"
    assert r.details == {"scanned": True, "available": True, "conflicts": []}


def test_conflicts_present_warns_and_names_the_remedy():
    import _lib_doctor
    r = _lib_doctor._check_journal_conflicts(
        _state(journal_present=True,
               journal_conflicts=[_conflict(), _conflict("sa:o:def")]))
    assert r.severity == "warn"
    assert "2" in r.summary
    assert "db rederive" in (r.remediation or "")
    assert [c["eventId"] for c in r.details["conflicts"]] == [
        "wcs:o:abc:2026-07-18", "sa:o:def"]


def test_conflicts_remedy_is_family_aware_for_a_retained_family():
    """`db rederive` owns the re-derived Claude families only; it is the wrong
    remedy for a `qaa:`/unknown group, so the leg must not promise it."""
    import _lib_doctor
    r = _lib_doctor._check_journal_conflicts(
        _state(journal_present=True,
               journal_conflicts=[_conflict("qaa:versioned-state")]))
    assert r.severity == "warn"
    assert "db rederive" not in (r.remediation or "")


def test_conflicts_marked_unavailable_when_the_selector_raised():
    import _lib_doctor
    r = _lib_doctor._check_journal_conflicts(
        _state(journal_present=True, journal_conflicts=None,
               journal_protocol_error="correction batch b manifest hash mismatch"))
    assert r.severity == "ok", "the FAIL belongs to journal.protocol, not here"
    assert r.details["available"] is False
    assert "unavailable" in r.summary


def test_protocol_leg_is_ok_without_a_violation():
    import _lib_doctor
    r = _lib_doctor._check_journal_protocol(
        _state(journal_present=True, journal_protocol_error=None))
    assert (r.id, r.severity) == ("journal.protocol", "ok")


def test_protocol_leg_fails_when_selector_cannot_complete():
    import _lib_doctor
    r = _lib_doctor._check_journal_protocol(
        _state(journal_present=True,
               journal_protocol_error="correction batch phase is invalid"))
    assert r.severity == "fail"
    assert "phase is invalid" in r.details["error"]
    assert "selector failed" in r.summary
    assert r.remediation


def test_protocol_leg_fails_honestly_when_tainted_batches_were_omitted():
    import _lib_doctor
    violations = [
        _protocol_violation(),
        _protocol_violation(
            "batch:other", "manifest_actions_hash_mismatch"
        ),
    ]
    r = _lib_doctor._check_journal_protocol(
        _state(
            journal_present=True,
            journal_conflicts=[],
            journal_protocol_violations=violations,
            journal_protocol_error=None,
        )
    )

    assert r.severity == "fail"
    assert "index rebuilt" in r.summary
    assert "tainted correction batches omitted" in r.summary
    assert r.details["violations"] == violations
    assert r.details["sample"] == [
        "batch:invalid: marker_conflict",
        "batch:other: manifest_actions_hash_mismatch",
    ]
    assert r.remediation


def test_both_new_legs_are_registered_in_the_journal_category():
    import _lib_doctor
    journal_category = next(
        cat for cat in _lib_doctor._CATEGORY_DEFINITIONS if cat[0] == "journal")
    ids = [check_id for check_id, _fn in journal_category[2]]
    assert ids == [
        "journal.presence", "journal.integrity", "journal.index_freshness",
        "journal.auto_heal", "journal.writer_guard", "journal.conflicts",
        "journal.protocol", "journal.quota_projection",
    ]


# ── writer guard (#386 spec §6.4) ─────────────────────────────────────────
#
# On an installed build the stats authorizer LOGS instead of raising, so this
# leg is the only surface an unsanctioned write reaches in the field. An ABSENT
# log is the normal state and must read INFO, never as a gather failure.


def test_writer_guard_absent_log_is_ok():
    import _lib_doctor

    r = _lib_doctor._check_journal_writer_guard(_state())
    assert r.severity == "ok"
    assert r.details["entries"] == 0
    assert r.remediation is None


def test_writer_guard_empty_log_is_ok():
    import _lib_doctor

    r = _lib_doctor._check_journal_writer_guard(
        _state(journal_writer_guard={
            "entries": 0, "newest_age_s": None, "path": "/x", "sample": None}))
    assert r.severity == "ok"
    assert r.details["entries"] == 0


def test_writer_guard_recent_entries_warn_and_name_the_log():
    import _lib_doctor

    r = _lib_doctor._check_journal_writer_guard(
        _state(journal_writer_guard={
            "entries": 3,
            "newest_age_s": 3600,
            "path": "/home/u/.local/share/cctally/logs/stats-writer-guard.log",
            "sample": "2026-07-26T00:00:00Z\tunsanctioned stats write\t"
                      "action=18\ttable=weekly_usage_snapshots",
        }))
    assert r.severity == "warn"
    assert "3 unsanctioned" in r.summary
    assert "stats-writer-guard.log" in r.remediation
    # The write was ALLOWED through on an installed build; the remediation must
    # not imply data loss, or it sends the user chasing a recovery they do not
    # need.
    assert "no data was lost" in r.remediation


def test_writer_guard_old_entries_do_not_warn():
    import _lib_doctor

    r = _lib_doctor._check_journal_writer_guard(
        _state(journal_writer_guard={
            "entries": 2,
            "newest_age_s": 30 * 24 * 3600,
            "path": "/x", "sample": "old",
        }))
    assert r.severity == "ok"
    assert r.details["entries"] == 2


def test_writer_guard_unparseable_timestamp_does_not_warn_or_crash():
    """`newest_age_s=None` means the stamp could not be parsed. Guessing 'recent'
    would turn a corrupt log line into a permanent WARN the user cannot clear."""
    import _lib_doctor

    r = _lib_doctor._check_journal_writer_guard(
        _state(journal_writer_guard={
            "entries": 1, "newest_age_s": None, "path": "/x", "sample": "??"}))
    assert r.severity == "ok"


def test_writer_guard_gather_reads_only_the_bounded_tail(monkeypatch):
    """Doctor must not read an arbitrarily large guard log into memory."""
    import _cctally_doctor
    import _cctally_store

    tail_lines = 5
    tail_bytes = 512
    raw_lines = [
        (
            f"2026-07-26T00:{minute:02d}:00+00:00\t"
            f"unsanctioned stats write\taction=18\ttable=t{minute}\n"
        ).encode()
        for minute in range(60)
    ]
    payload = b"".join(raw_lines)

    class _BoundedReader(io.BytesIO):
        def __init__(self, data):
            super().__init__(data)
            self.bytes_read = 0

        def read(self, size=-1):
            assert 0 <= size <= tail_bytes
            self.bytes_read += size
            assert self.bytes_read <= tail_bytes
            return super().read(size)

    class _GuardPath:
        def exists(self):
            return True

        def open(self, mode):
            assert mode == "rb"
            return _BoundedReader(payload)

        def read_text(self, *args, **kwargs):
            raise AssertionError("Doctor attempted an unbounded read_text()")

        def __str__(self):
            return "/tmp/stats-writer-guard.log"

    monkeypatch.setattr(_cctally_store, "_guard_log_path", _GuardPath)
    monkeypatch.setattr(
        _cctally_doctor, "_GUARD_LOG_TAIL_LINES", tail_lines, raising=False)
    monkeypatch.setattr(
        _cctally_doctor, "_GUARD_LOG_TAIL_BYTES", tail_bytes, raising=False)

    gathered = _cctally_doctor._gather_writer_guard_log(
        dt.datetime(2026, 7, 26, 1, 0, tzinfo=dt.timezone.utc))

    assert gathered == {
        "entries": tail_lines,
        "newest_age_s": 60,
        "path": "/tmp/stats-writer-guard.log",
        "sample": (
            "2026-07-26T00:59:00+00:00\tunsanctioned stats write\t"
            "action=18\ttable=t59"
        ),
    }


# ── quota projection (#496 S5b §4.7) ──────────────────────────────────────

def test_quota_projection_incomplete_warns_and_names_cache_sync():
    """The flag has exactly two clearers — a reconciliation armed by
    `cctally cache-sync` or the dashboard, and a later complete rebuild. No
    ingest path clears it, so a user who never runs either sees every quota
    surface degrade with no cause and no remedy stated anywhere."""
    import _lib_doctor
    r = _lib_doctor._check_journal_quota_projection(
        _state(stats_quota_projection_incomplete=True))
    assert (r.id, r.severity) == ("journal.quota_projection", "warn")
    # The summary is the line a user actually reads, and its two siblings below
    # pin theirs. Without this the warn arm's wording was pinned by nothing.
    assert r.summary == "incomplete — quota projection reads are refused"
    assert "cctally cache-sync" in (r.remediation or "")
    assert r.details == {"incomplete": True}


def test_quota_projection_complete_is_ok():
    """Non-vacuity: a leg that always warned would also satisfy the case
    above."""
    import _lib_doctor
    r = _lib_doctor._check_journal_quota_projection(
        _state(stats_quota_projection_incomplete=False))
    assert (r.id, r.severity) == ("journal.quota_projection", "ok")
    assert r.summary == "complete"
    assert r.remediation is None


def test_quota_projection_unknown_is_not_applicable():
    """A pre-1009 index, an absent stats.db and an unreadable one all arrive as
    None, and none of them is a fault to report."""
    import _lib_doctor
    r = _lib_doctor._check_journal_quota_projection(
        _state(stats_quota_projection_incomplete=None))
    assert (r.id, r.severity) == ("journal.quota_projection", "ok")
    assert r.summary == "not applicable"
    assert r.remediation is None
