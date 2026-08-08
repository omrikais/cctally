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
import subprocess
import sys

from conftest import load_script


sys.path.insert(0, str(pathlib.Path(load_script()["__file__"]).resolve().parent))


def test_doctor_fixture_canonicalizer_redacts_allocated_bytes_but_not_absence():
    """Filesystem allocation varies by host; losing the measurement must not."""
    canonicalizer = (
        pathlib.Path(__file__).parent / "fixtures" / "doctor" / "_canonicalize.py"
    )
    raw = """{
  \"retainedBytes\": 9461760,
  \"reclaimableBytes\": 3153920,
  \"protectedBytes\": 6307840,
  \"floorRetainedBytes\": 3153920,
  \"unmeasuredRetainedBytes\": null
}
retainedBytes: 9461760
reclaimableBytes: 3153920
protectedBytes: 6307840
floorRetainedBytes: 3153920
retainedBytes: None
"""
    result = subprocess.run(
        [sys.executable, str(canonicalizer), "/unused/scratch"],
        input=raw,
        text=True,
        capture_output=True,
        check=True,
    )

    for key in (
        "retainedBytes", "reclaimableBytes", "protectedBytes",
        "floorRetainedBytes",
    ):
        assert f'\"{key}\": \"<redacted>\"' in result.stdout
        assert f"{key}: <redacted>" in result.stdout
    assert '"unmeasuredRetainedBytes": null' in result.stdout
    assert "retainedBytes: None" in result.stdout


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
    """#496 S6 §7.2 rewrote this leg; the count and a relative age replace
    the old "fired recently" phrasing, and the severity is unchanged."""
    import _lib_doctor
    r = _lib_doctor._check_journal_auto_heal(
        _state(journal_heal_incidents=[
            {"kind": "quarantine", "name": "stats.db-20260720T120000Z",
             "age_s": 2 * 86400}]))
    assert r.severity == "warn"
    assert "1 incident, 2d ago" in r.summary


def test_a_forensics_bundle_alone_is_not_an_incident():
    """§7.2's identity rule: a bundle is linked evidence, never an incident.

    The previous leg counted both, so a directory and the bundle written
    moments before it read as two incidents.
    """
    import _lib_doctor
    r = _lib_doctor._check_journal_auto_heal(
        _state(journal_heal_incidents=[
            {"kind": "forensics",
             "name": "stats.db-corruption-forensics-20260101T120000Z.json",
             "age_s": 30 * 86400}]))
    assert r.severity == "ok"
    assert r.details["incidents"] == 0


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


# ── §7.2 auto-heal, rewritten (#496 S6 F14) ───────────────────────────────
#
# Three defects the previous leg carried: the count never reached `summary`,
# `age_s // 86400` rendered every sub-day incident as "0d ago", and it read
# `incidents[0]` alone so a second incident could not escalate anything.
#
# Identity rules (§7.2): an INCIDENT is a quarantine directory and nothing
# else; a DETECTION is a heal-ring entry keyed by `healId`; a forensics bundle
# is linked evidence and is never counted as an incident.


def _incident(name="stats.db-20260720T120000Z", age_s=3600, shape=None):
    return {"kind": "quarantine", "name": name, "age_s": age_s, "shape": shape}


def _bundle(name="stats.db-corruption-forensics-20260720T120000Z.json",
            age_s=3600):
    return {"kind": "forensics", "name": name, "age_s": age_s, "shape": None}


def _detection(heal_id="h1", age_s=3600):
    return {"heal_id": heal_id, "age_s": age_s}


def test_the_count_appears_in_the_default_summary():
    import _lib_doctor
    r = _lib_doctor._check_journal_auto_heal(_state(
        journal_heal_incidents=[
            _incident(f"stats.db-2026072{i}T120000Z", age_s=(i + 1) * 86400)
            for i in range(3)
        ],
        journal_heal_detections=[],
    ))
    assert "3 incidents" in r.summary


def test_a_sub_day_age_renders_in_hours_not_zero_days():
    import _lib_doctor
    r = _lib_doctor._check_journal_auto_heal(_state(
        journal_heal_incidents=[_incident(age_s=3 * 3600)],
        journal_heal_detections=[],
    ))
    assert "3h ago" in r.summary
    assert "0d ago" not in r.summary


def test_one_incident_warns_regardless_of_age():
    import _lib_doctor
    r = _lib_doctor._check_journal_auto_heal(_state(
        journal_heal_incidents=[_incident(age_s=400 * 86400)],
        journal_heal_detections=[],
    ))
    assert r.severity == "warn"


def test_three_detections_in_seven_days_fail():
    import _lib_doctor
    r = _lib_doctor._check_journal_auto_heal(_state(
        journal_heal_incidents=[_incident()],
        journal_heal_detections=[
            _detection(f"h{i}", age_s=i * 3600) for i in range(3)
        ],
    ))
    assert r.severity == "fail"
    assert "recurring" in r.summary


def test_three_detections_outside_the_window_do_not_fail():
    import _lib_doctor
    r = _lib_doctor._check_journal_auto_heal(_state(
        journal_heal_incidents=[_incident(age_s=90 * 86400)],
        journal_heal_detections=[
            _detection(f"h{i}", age_s=(30 + i) * 86400) for i in range(3)
        ],
    ))
    assert r.severity == "warn"


def test_a_repeated_shape_in_two_incidents_fails():
    import _lib_doctor
    r = _lib_doctor._check_journal_auto_heal(_state(
        journal_heal_incidents=[
            _incident("stats.db-20260720T120000Z", age_s=3600, shape="abc"),
            _incident("stats.db-20260721T120000Z", age_s=7200, shape="abc"),
        ],
        journal_heal_detections=[],
    ))
    assert r.severity == "fail"
    assert "abc" in r.summary


def test_the_same_shape_outside_the_window_does_not_fail():
    import _lib_doctor
    r = _lib_doctor._check_journal_auto_heal(_state(
        journal_heal_incidents=[
            _incident("stats.db-20260720T120000Z", age_s=3600, shape="abc"),
            _incident("stats.db-20250721T120000Z", age_s=400 * 86400,
                      shape="abc"),
        ],
        journal_heal_detections=[],
    ))
    assert r.severity == "warn"


def test_the_literal_none_shape_never_triggers_recurrence():
    import _lib_doctor
    r = _lib_doctor._check_journal_auto_heal(_state(
        journal_heal_incidents=[
            _incident("stats.db-20260720T120000Z", age_s=3600, shape="none"),
            _incident("stats.db-20260721T120000Z", age_s=7200, shape="none"),
        ],
        journal_heal_detections=[],
    ))
    assert r.severity != "fail"


def test_a_directory_and_its_bundle_count_as_one_incident():
    import _lib_doctor
    r = _lib_doctor._check_journal_auto_heal(_state(
        journal_heal_incidents=[_incident(), _bundle()],
        journal_heal_detections=[],
    ))
    assert r.details["incidents"] == 1


def test_no_incident_and_no_detection_is_ok():
    import _lib_doctor
    r = _lib_doctor._check_journal_auto_heal(_state(
        journal_heal_incidents=[], journal_heal_detections=[],
    ))
    assert r.severity == "ok" and "no auto-heal" in r.summary


def test_a_detection_with_no_incident_still_warns():
    import _lib_doctor
    r = _lib_doctor._check_journal_auto_heal(_state(
        journal_heal_incidents=[], journal_heal_detections=[_detection()],
    ))
    assert r.severity == "warn"


def test_a_repeated_heal_id_is_one_detection():
    """A detection is keyed by `healId`; the ring updates entries in place."""
    import _lib_doctor
    r = _lib_doctor._check_journal_auto_heal(_state(
        journal_heal_incidents=[_incident()],
        journal_heal_detections=[
            _detection("same", age_s=1), _detection("same", age_s=2),
            _detection("same", age_s=3),
        ],
    ))
    assert r.severity == "warn"


def test_the_journal_leg_roster_is_still_exactly_eight():
    import _lib_doctor
    journal_category = next(
        cat for cat in _lib_doctor._CATEGORY_DEFINITIONS if cat[0] == "journal")
    ids = [check_id for check_id, _fn in journal_category[2]]
    assert ids == [
        "journal.presence", "journal.integrity", "journal.index_freshness",
        "journal.auto_heal", "journal.writer_guard", "journal.conflicts",
        "journal.protocol", "journal.quota_projection",
    ]


# ── §7.3 db.retained_artifacts ────────────────────────────────────────────


def _retained(**overrides):
    state = {
        "policy_status": "valid",
        "policy_reason": None,
        "retained_bytes": 0,
        "reclaimable_bytes": 0,
        "protected_bytes": 0,
        "protected_roots": 0,
        "roots": 0,
        "free_disk_bytes": 100 * 1024 ** 3,
        "partial_scan": False,
        "unsatisfied_rules": [],
        "floor_retained_roots": 0,
        "floor_retained_bytes": 0,
        "max_age_seconds": 30 * 86400,
        "max_count_per_family": 20,
        "max_total_bytes": 4096 * 1024 ** 2,
        "min_free_bytes": 10240 * 1024 ** 2,
        "stuck_records": [],
    }
    state.update(overrides)
    return state


def test_retained_artifacts_reports_exact_reclaimable_and_protected_bytes():
    import _lib_doctor
    r = _lib_doctor._check_db_retained_artifacts(_state(
        retained_artifacts=_retained(
            retained_bytes=1000, reclaimable_bytes=123, protected_bytes=456,
        )))
    assert r.details["reclaimableBytes"] == 123
    assert r.details["protectedBytes"] == 456


def test_a_corpus_inside_its_policy_is_ok():
    import _lib_doctor
    r = _lib_doctor._check_db_retained_artifacts(_state(
        retained_artifacts=_retained(retained_bytes=1024)))
    assert r.severity == "ok"
    assert "within policy" in r.summary


def test_reclamation_that_is_due_and_would_satisfy_the_policy_warns():
    import _lib_doctor
    r = _lib_doctor._check_db_retained_artifacts(_state(
        retained_artifacts=_retained(
            retained_bytes=20 * 1024 ** 3, reclaimable_bytes=17 * 1024 ** 3,
            driving_rules=["max_total_bytes"],
        )))
    assert r.severity == "warn"
    assert "reclaimable" in r.summary
    assert r.remediation and "db prune" in r.remediation


def test_the_warn_summary_names_the_bound_that_drove_the_reclamation():
    """The same false sentence the FAIL summary printed: reclamation due on
    the age bound rendered as "over the 4096 MiB budget"."""
    import _lib_doctor
    r = _lib_doctor._check_db_retained_artifacts(_state(
        retained_artifacts=_retained(
            retained_bytes=100 * 1024 ** 2, reclaimable_bytes=50 * 1024 ** 2,
            driving_rules=["max_age_seconds"],
        )))
    assert r.severity == "warn"
    assert "30-day age bound" in r.summary
    assert "4096 MiB" not in r.summary


def test_protected_evidence_holding_a_bound_fails():
    import _lib_doctor
    r = _lib_doctor._check_db_retained_artifacts(_state(
        retained_artifacts=_retained(
            retained_bytes=20 * 1024 ** 3, protected_bytes=5 * 1024 ** 3,
            protected_roots=7, unsatisfied_rules=["max_total_bytes"],
        )))
    assert r.severity == "fail"
    assert "protected" in r.summary


def test_the_fail_summary_names_the_bound_that_is_actually_unsatisfied():
    """A 100 MiB corpus is INSIDE a 4096 MiB budget, so naming that budget is
    a false sentence. §3.2 protects any unclassified incident and the default
    age bound is 30 days, so one unclassified incident older than a month is
    the likely FAIL on an install nowhere near the byte budget.
    """
    import _lib_doctor
    hundred_mib = 100 * 1024 ** 2
    seen = {}
    for rule in (
        "max_age_seconds", "max_count_per_family", "max_total_bytes",
        "min_free_bytes",
    ):
        r = _lib_doctor._check_db_retained_artifacts(_state(
            retained_artifacts=_retained(
                retained_bytes=hundred_mib, protected_bytes=hundred_mib,
                protected_roots=1, unsatisfied_rules=[rule],
            )))
        assert r.severity == "fail"
        seen[rule] = r.summary
    assert "30-day" in seen["max_age_seconds"]
    assert "4096 MiB" not in seen["max_age_seconds"]
    assert "20 per family" in seen["max_count_per_family"]
    assert "4096 MiB" not in seen["max_count_per_family"]
    assert "4096 MiB budget" in seen["max_total_bytes"]
    assert "10240 MiB free-disk floor" in seen["min_free_bytes"]
    assert "4096 MiB" not in seen["min_free_bytes"]
    # Four distinct bounds must produce four distinct sentences.
    assert len(set(seen.values())) == 4


def test_the_fail_summary_names_every_unsatisfied_bound_not_just_one():
    """The shipped protected-overage fixture carries two at once."""
    import _lib_doctor
    r = _lib_doctor._check_db_retained_artifacts(_state(
        retained_artifacts=_retained(
            retained_bytes=9449472, protected_bytes=9449472, protected_roots=3,
            max_total_bytes=1024 ** 2,
            unsatisfied_rules=["max_age_seconds", "max_total_bytes"],
        )))
    assert r.severity == "fail"
    assert "30-day" in r.summary and "1 MiB budget" in r.summary


def test_the_fail_summary_degrades_when_a_bound_value_is_absent():
    import _lib_doctor
    r = _lib_doctor._check_db_retained_artifacts(_state(
        retained_artifacts=_retained(
            retained_bytes=4096, protected_bytes=4096, protected_roots=1,
            max_age_seconds=None, unsatisfied_rules=["max_age_seconds"],
        )))
    assert r.severity == "fail"
    assert "age bound" in r.summary


def test_a_malformed_policy_fails_the_db_leg():
    import _lib_doctor
    r = _lib_doctor._check_db_retained_artifacts(_state(
        retained_artifacts=_retained(
            policy_status="malformed",
            policy_reason="storage.artifact_retention.max_age_days must be >= 1",
        )))
    assert r.severity == "fail"
    assert "malformed" in r.summary
    assert "automatic reclaim is off" in r.summary


def test_a_partial_scan_degrades_to_warn():
    import _lib_doctor
    r = _lib_doctor._check_db_retained_artifacts(_state(
        retained_artifacts=_retained(partial_scan=True)))
    assert r.severity == "warn"
    assert "partial" in r.summary


def test_the_shape_floor_is_information_and_never_a_failure():
    """§3.6: a permanent FAIL no action clears would defeat F14 itself."""
    import _lib_doctor
    r = _lib_doctor._check_db_retained_artifacts(_state(
        retained_artifacts=_retained(
            retained_bytes=2048, floor_retained_roots=2,
            floor_retained_bytes=1024,
        )))
    assert r.severity == "ok"
    assert r.details["floorRetainedRoots"] == 2


def test_a_stuck_reclaim_record_warns_and_names_what_to_inspect():
    import _lib_doctor
    r = _lib_doctor._check_db_retained_artifacts(_state(
        retained_artifacts=_retained(stuck_records=[{
            "planId": "1786-99", "memberIds": ["quarantine/stats.db-x"],
            "stuck": True, "ageSeconds": 3 * 86400,
        }])))
    assert r.severity == "warn"
    assert "1786-99" in r.summary
    assert "3d" in r.summary
    assert r.remediation and ".reclaim-pending-" in r.remediation


def test_a_failing_but_not_yet_stuck_record_is_not_reported():
    """The resume retries it every pass and it normally clears (§7.3)."""
    import _lib_doctor
    r = _lib_doctor._check_db_retained_artifacts(_state(
        retained_artifacts=_retained(stuck_records=[{
            "planId": "1786-99", "memberIds": ["quarantine/stats.db-x"],
            "stuck": False, "ageSeconds": 600,
        }])))
    assert r.severity == "ok"


def test_an_unavailable_scan_warns_rather_than_reading_as_healthy():
    """§7.5 degrades a PARTIAL scan to `warn`; a wholly unavailable one is
    strictly worse, so it cannot be quieter. At `ok` the leg is skipped by
    `render_text` under `--quiet` and `doctor` exits 0, which removes the only
    visibility into the retained corpus and into a stuck reclaim record.
    """
    import _lib_doctor
    for state in (
        {"policy_status": "unavailable"},
        {"policy_status": "unavailable", "scan_error": "PermissionError"},
    ):
        r = _lib_doctor._check_db_retained_artifacts(_state(
            retained_artifacts=state))
        assert r.severity == "warn", state
        assert "unavailable" in r.summary
        assert r.remediation
    named = _lib_doctor._check_db_retained_artifacts(_state(
        retained_artifacts={
            "policy_status": "unavailable", "scan_error": "PermissionError",
        }))
    assert "PermissionError" in named.summary
    # An ABSENT field is not a failed scan. It is a state nothing populated,
    # which is what a DoctorState assembled for another leg looks like, and
    # WARNing on it would make every such state warn.
    absent = _lib_doctor._check_db_retained_artifacts(
        _state(retained_artifacts=None))
    assert absent.severity == "ok" and "not scanned" in absent.summary


def test_a_shallow_gather_reports_not_scanned_rather_than_warning():
    """The `deep`-gated skip is a deliberate mode, not a failure — the same
    shape `db.integrity` uses when `quick_check` did not run.
    """
    import _lib_doctor
    r = _lib_doctor._check_db_retained_artifacts(_state(
        retained_artifacts={"policy_status": "not-scanned"}))
    assert r.severity == "ok"
    assert "not scanned" in r.summary and "cctally doctor" in r.summary
    assert r.details["scanned"] is False


def test_a_shallow_gather_still_fails_on_a_malformed_policy():
    """Resolving the policy and globbing the reclaim records are cheap, so the
    two conditions an operator must act on survive the `deep` gate.
    """
    import _lib_doctor
    r = _lib_doctor._check_db_retained_artifacts(_state(
        retained_artifacts={
            "policy_status": "malformed", "policy_reason": "bad",
            "stuck_records": [], "scanned": False,
        }))
    assert r.severity == "fail"
    stuck = _lib_doctor._check_db_retained_artifacts(_state(
        retained_artifacts={
            "policy_status": "not-scanned", "scanned": False,
            "stuck_records": [{
                "planId": "1786-99", "memberIds": ["quarantine/stats.db-x"],
                "stuck": True, "ageSeconds": 3 * 86400,
            }],
        }))
    assert stuck.severity == "warn" and "1786-99" in stuck.summary


def test_a_malformed_policy_reports_absent_byte_figures_not_zero():
    """The gather returns before scanning, so `retainedBytes: 0` is FALSE
    rather than absent — a dashboard reading it would show nothing retained
    on an install holding gigabytes.
    """
    import _lib_doctor
    r = _lib_doctor._check_db_retained_artifacts(_state(
        retained_artifacts={
            "policy_status": "malformed", "policy_reason": "bad",
            "stuck_records": [],
        }))
    assert r.severity == "fail"
    for key in ("retainedBytes", "reclaimableBytes", "protectedBytes", "roots"):
        assert r.details.get(key) is None, key


def test_the_new_db_leg_is_registered_in_the_db_category():
    import _lib_doctor
    db_category = next(
        cat for cat in _lib_doctor._CATEGORY_DEFINITIONS if cat[0] == "db")
    assert "db.retained_artifacts" in [
        check_id for check_id, _fn in db_category[2]
    ]


# ── §7.6 the four new fixtures must actually reach their branches ─────────
#
# A tripwire, not a duplicate of the golden harness. The gather's failure path
# degrades to "retention scan unavailable", which reads exactly like a healthy
# install with nothing retained — and one `NameError` in the gather made all
# four of these fixtures report that instead of the branch they exist to
# cover, with the harness still green because the goldens had been
# regenerated from the broken output.
#
# The literals are hardcoded rather than imported, so a change to the leg's
# wording cannot make this pass by construction.

import pytest as _pytest

_DOCTOR_FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "doctor"


@_pytest.mark.parametrize("scenario,marker,expected_exit", [
    ("31-retained-artifacts-over-budget",
     "⚠ Retained evidence", "0"),
    ("32-retained-artifacts-protected",
     "✗ Retained evidence", "2"),
    ("33-heal-repeated-shape",
     "✗ Auto-heal", "2"),
    ("34-retention-policy-malformed",
     "✗ Retained evidence", "2"),
])
def test_each_new_doctor_fixture_reaches_the_branch_it_covers(
    scenario, marker, expected_exit,
):
    text = (_DOCTOR_FIXTURES / scenario / "expected.txt").read_text(
        encoding="utf-8")
    assert marker in text, f"{scenario} no longer reaches {marker!r}"
    assert "retention scan unavailable" not in text, (
        f"{scenario} degraded instead of running the scan"
    )
    assert (_DOCTOR_FIXTURES / scenario / "expected.exit").read_text(
        encoding="utf-8").strip() == expected_exit


def test_the_over_budget_fixture_is_not_also_the_protected_one():
    """§7.6: the FAIL fixture must not stand in for the WARN one.

    Revision 2 added two fixtures and let the FAIL fixture choose either
    failure mode, which leaves one branch uncovered.
    """
    warn = (_DOCTOR_FIXTURES / "31-retained-artifacts-over-budget"
            / "expected.txt").read_text(encoding="utf-8")
    fail = (_DOCTOR_FIXTURES / "32-retained-artifacts-protected"
            / "expected.txt").read_text(encoding="utf-8")
    assert "reclaimable — over the 1-day age bound" in warn
    # The FAIL fixture names BOTH bounds its protected evidence blocks. The
    # literals are hardcoded rather than imported, so a renderer that stopped
    # naming the bound would move this test as well as the golden.
    assert "protected leaves the 1-day age bound and the 1 MiB budget" in fail
    assert "unsatisfied" in fail
    assert "reclaimable — over" not in fail
