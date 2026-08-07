"""#496 S5b Stage 4 — segment elision (F12's read side).

A rebuild reads every journal byte on every pass, including the roughly 1.64 GB
of Codex quota observations that six of the maintainer's seven bootstrap
segments hold and that contribute no distinct effective event. Stage 3's
coverage certificate is what makes those bytes provably redundant: it says the
cache already holds every cache-relevant record in the covered prefix. Stage 4
stops re-reading them.

The three properties these tests establish, in the order the spec argues them:

- **Identity** (spec 5.5). A segment is elidable only when it is not
  canonically last, its summary matches the file on disk, its complete-line
  offset equals both its summarized size and this pass's pinned raw extent, it
  holds zero retained records, the certificate covers it, and no
  protocol-resolution operation has been met. Every failure reads the segment.
- **Sequence parity** (spec 5.4). `resolve_effective_events` numbers candidates
  with `enumerate(records)`, three of the seven structural violation kinds put
  that number inside `ProtocolViolation.evidence`, and the fingerprint hashes
  it. That fingerprint is durable — stored in `journal_protocol_violations` and
  referenced BY NAME from a `journal_protocol_resolution` op that
  `bin/_cctally_journal_repair.py` mints from the UNFILTERED record list — so
  renumbering makes a previously acknowledged violation unresolvable and raises
  on every later rebuild. An elided segment contributes its exact
  `decoded`-entry count of placeholders in its stead.
- **Hash non-composition** (spec 5.1). `PrefixHashAccumulator` absorbs
  completed segments into one sequential `sha256` and `hashlib` cannot restore
  midstate, so `journal_prefix_hash` cannot be computed over an elided prefix.
  Elision is optimistic with a re-read fallback: a `journal_protocol_resolution`
  op re-reads from the start for the exact hash.

Every assertion counts a structural fact — physical opens through
`_open_segment_for_read`, stored row sets, digests — never an elapsed time. A
wall-clock ceiling at fixture scale cannot fail.
"""
from __future__ import annotations

import importlib
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "bin"))

import _cctally_core
import _lib_journal_router
import _lib_segment_summary as ss

import journal_fixture_496_s5b as F
from conftest import load_script, redirect_paths
# The cache-writer-flock instrumentation Stage 3 already uses, reused here so
# the "outside both flocks" half of spec §8 criterion 4b is OBSERVED rather than
# argued from the order of two statements. Its own test builds a fresh journal
# and rebuilds once, so nothing elides there and the refill never runs.
from test_quota_journal import _LockTracker


def _journal():
    return importlib.import_module("_cctally_journal")


@pytest.fixture
def elision_fixture(tmp_path, monkeypatch):
    """A journal of two quota-only segments and one holding retained records."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    core = importlib.import_module("_cctally_core")
    ns["open_cache_db"]().close()
    shape = F.build_elision_scenario(core.APP_DIR)
    shape["ns"] = ns
    return shape


@pytest.fixture
def opened(monkeypatch, elision_fixture):
    """Every segment opened for reading, by basename, in the order opened.

    Physical opens, not lines or bytes: a per-pass counter stays correct while
    an implementation reopens a segment behind it, which is precisely the hidden
    re-read this stage removes.

    `_open_segment_for_read` is the chokepoint for reading segment CONTENT, and
    it is the one this stage is about. It is not the only `open` a rebuild makes
    of a segment file: `_complete_line_offset` probes each segment's last byte
    through a plain `open(path, "rb")` to build the certificate's pinned extent
    vector, and that probe is invisible here. So an elided segment is opened
    once for a one-byte tail read and never read for content — which is where
    the roughly 1.64 GB this stage stops re-reading lives.
    """
    jr = _journal()
    real = jr._open_segment_for_read
    seen: list = []

    def record(seg_path):
        seen.append(pathlib.Path(seg_path).name)
        return real(seg_path)

    monkeypatch.setattr(jr, "_open_segment_for_read", record)
    return seen


def _rebuild(jr, **kwargs):
    return jr.rebuild_stats_index(
        context=jr.RebuildContext(trigger="test-fixture"), **kwargs)


def _complete_summary(**overrides):
    """A summary of a complete, quota-only, 100-byte segment."""
    fields = {
        "segment_name": "observations-2026-07.jsonl",
        "st_dev": 1,
        "st_ino": 2,
        "summarized_size": 100,
        "complete_line_covered_offset": 100,
        "lines": 4,
        "bytes": 100,
        "decodes": 4,
        "malformed": 0,
        "quota_only": True,
        "decoded_entry_count": 4,
    }
    fields.update(overrides)
    return ss.SegmentSummary(**fields)


# --------------------------------------------------------------------------
# Task 17 — the pure predicate and the sidecar
# --------------------------------------------------------------------------

def test_a_torn_segment_is_never_elidable():
    """Raw size 120, last newline at 100.

    Storing only the boundary would collapse all three operands to 100 and let
    this pass, which is why the summary carries `summarized_size` AND
    `complete_line_covered_offset` and the pinned vector carries the raw size.
    """
    summary = _complete_summary(
        summarized_size=120, complete_line_covered_offset=100)
    ok, why = ss.summary_is_elidable(
        summary, pinned_raw_extent=120, is_last=False,
        certificate_covers=True, resolution_seen=False)
    assert not ok and "extent" in why


def test_a_permanently_torn_segment_is_also_refused():
    """Not only one repaired back to the same size.

    Fixing #511 stops a non-last segment being MUTATED later; it does not make
    an already-torn segment complete.
    """
    summary = _complete_summary(
        summarized_size=120, complete_line_covered_offset=100)
    assert not ss.summary_is_elidable(
        summary, pinned_raw_extent=120, is_last=False,
        certificate_covers=True, resolution_seen=False)[0]


def test_a_segment_that_grew_since_its_summary_is_refused():
    """The pinned raw extent is the third operand and it is not redundant."""
    ok, why = ss.summary_is_elidable(
        _complete_summary(), pinned_raw_extent=140, is_last=False,
        certificate_covers=True, resolution_seen=False)
    assert not ok and "extent" in why


def test_the_canonically_last_segment_is_never_elidable():
    ok, why = ss.summary_is_elidable(
        _complete_summary(), pinned_raw_extent=100, is_last=True,
        certificate_covers=True, resolution_seen=False)
    assert not ok and "last" in why


def test_a_segment_holding_one_retained_record_is_refused():
    ok, why = ss.summary_is_elidable(
        _complete_summary(quota_only=False), pinned_raw_extent=100,
        is_last=False, certificate_covers=True, resolution_seen=False)
    assert not ok and "retained" in why


def test_a_segment_the_certificate_does_not_cover_is_refused():
    ok, why = ss.summary_is_elidable(
        _complete_summary(), pinned_raw_extent=100, is_last=False,
        certificate_covers=False, resolution_seen=False)
    assert not ok and why == ss.REASON_UNCOVERED


def test_a_resolution_operation_refuses_every_segment():
    """Optimistic elision, re-read fallback — see spec 5.1."""
    ok, why = ss.summary_is_elidable(
        _complete_summary(), pinned_raw_extent=100, is_last=False,
        certificate_covers=True, resolution_seen=True)
    assert not ok and "resolution" in why


def test_a_summary_of_a_different_inode_is_refused():
    """`st_dev`/`st_ino` are part of the identity, per spec 5.5 item 2."""
    ok, why = ss.summary_is_elidable(
        _complete_summary(), pinned_raw_extent=100, is_last=False,
        certificate_covers=True, resolution_seen=False,
        stat_identity=(1, 999))
    assert not ok and "identity" in why


def test_a_complete_covered_quota_only_segment_is_elidable():
    """The positive case, so every refusal above is a real discrimination
    rather than a predicate that answers False for everything."""
    ok, why = ss.summary_is_elidable(
        _complete_summary(), pinned_raw_extent=100, is_last=False,
        certificate_covers=True, resolution_seen=False, stat_identity=(1, 2))
    assert ok and why == ss.REASON_OK


def test_a_summary_without_a_decoded_entry_count_is_refused():
    """Spec 5.5 item 7. Without the count the pass cannot contribute the
    placeholders, and contributing none would renumber every later sequence."""
    ok, why = ss.summary_is_elidable(
        _complete_summary(decoded_entry_count=None), pinned_raw_extent=100,
        is_last=False, certificate_covers=True, resolution_seen=False)
    assert not ok and "decoded" in why


def test_a_torn_sidecar_is_discarded_not_repaired(tmp_path):
    path = tmp_path / ".segment-summaries"
    ss.write_sidecar(path, [_complete_summary()])
    assert ss.read_sidecar(path) is not None
    path.write_bytes(path.read_bytes()[:-4])
    assert ss.read_sidecar(path) is None


def test_a_sidecar_whose_checksum_does_not_match_is_discarded(tmp_path):
    """Self-validating, because the sidecar is a pure cache: any content the
    checksum does not authenticate is dropped and the pass reads normally."""
    path = tmp_path / ".segment-summaries"
    ss.write_sidecar(path, [_complete_summary()])
    body = path.read_text(encoding="utf-8")
    path.write_text(body.replace('"lines": 4', '"lines": 5'), encoding="utf-8")
    assert ss.read_sidecar(path) is None


def test_a_sidecar_from_another_version_is_discarded(tmp_path):
    path = tmp_path / ".segment-summaries"
    ss.write_sidecar(path, [_complete_summary()])
    payload = ss.read_sidecar(path)
    assert payload is not None
    raw = path.read_text(encoding="utf-8")
    replaced = raw.replace(
        f'"version": "{ss.summary_version()}"', '"version": "not-this-one"')
    assert replaced != raw, "the version token is not where this test edits it"
    path.write_text(replaced, encoding="utf-8")
    assert ss.read_sidecar(path) is None


def test_adding_a_retained_record_type_invalidates_existing_summaries(
    tmp_path, monkeypatch,
):
    """`quota_only` is derived from `RETAINED_RECORD_TYPES` at WRITE time.

    If a record type joins that set later, a summary written before the change
    still reports `quota_only=True` for a segment that now holds a retained
    record. The segment is then elided and its retained records are replaced by
    placeholders — a wrong selection, with no fallback and nothing on stderr.
    Nothing else catches it: the coverage certificate's `interpretationVersion`
    tracks cache materialization, not the stats selector.

    So the version token is derived from the set rather than hand-written, and
    the guard cannot be forgotten.
    """
    path = tmp_path / ".segment-summaries"
    ss.write_sidecar(path, [_complete_summary()])
    assert ss.read_sidecar(path) is not None

    monkeypatch.setattr(
        _lib_journal_router, "RETAINED_RECORD_TYPES",
        frozenset(_lib_journal_router.RETAINED_RECORD_TYPES | {"obs"}))
    assert ss.read_sidecar(path) is None


def test_a_stats_epoch_bump_invalidates_existing_summaries(
    tmp_path, monkeypatch,
):
    """`STATS_INDEX_EPOCH` is this repository's marker for "what a rebuild
    derives from the journal changed", and every counter a summary stores is
    part of that derivation."""
    path = tmp_path / ".segment-summaries"
    ss.write_sidecar(path, [_complete_summary()])
    assert ss.read_sidecar(path) is not None

    monkeypatch.setattr(
        _cctally_core, "STATS_INDEX_EPOCH",
        _cctally_core.STATS_INDEX_EPOCH + 1)
    assert ss.read_sidecar(path) is None


def test_an_absent_sidecar_reads_as_none(tmp_path):
    assert ss.read_sidecar(tmp_path / "nothing-here") is None


def test_a_written_sidecar_round_trips_every_field(tmp_path):
    path = tmp_path / ".segment-summaries"
    summary = _complete_summary(
        malformed=2, decodes=6, decoded_entry_count=6,
        last_seen_stamped={"acct-a": "2026-07-27T06:00:00Z"},
        last_seen_legacy_claude_at="2026-07-27T05:00:00Z",
        last_seen_legacy_codex_at=None,
    )
    ss.write_sidecar(path, [summary])
    read = ss.read_sidecar(path)
    assert read == {summary.segment_name: summary}


# --------------------------------------------------------------------------
# Task 17 — the summaries the streaming pass records
# --------------------------------------------------------------------------

def test_a_rebuild_records_a_summary_for_every_segment_it_read(elision_fixture):
    """The sidecar is journal-side, so it survives an absent `stats.db` — which
    is precisely when a rebuild runs."""
    jr = _journal()
    _rebuild(jr)

    summaries = jr.read_segment_summaries()
    assert set(summaries) == set(elision_fixture["segments"])
    for name, summary in summaries.items():
        path = jr._cctally_core.JOURNAL_DIR / name
        stat = path.stat()
        assert summary.st_dev == stat.st_dev
        assert summary.st_ino == stat.st_ino
        assert summary.summarized_size == stat.st_size
        assert summary.complete_line_covered_offset == stat.st_size


def test_the_summaries_separate_quota_only_segments_from_the_rest(
    elision_fixture,
):
    jr = _journal()
    _rebuild(jr)
    summaries = jr.read_segment_summaries()
    for name in elision_fixture["elidable"]:
        assert summaries[name].quota_only is True
    assert summaries[elision_fixture["last"]].quota_only is False


def test_a_summary_counts_decoded_entries_not_lines(tmp_path, monkeypatch):
    """A malformed line is READ but produces no `decoded` entry.

    Folding it into the count would insert a placeholder a non-eliding pass
    never appends, renumbering every later candidate.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    core = importlib.import_module("_cctally_core")
    ns["open_cache_db"]().close()
    shape = F.build_elision_scenario(core.APP_DIR, malformed_in_first=True)
    jr = _journal()
    _rebuild(jr)

    summary = jr.read_segment_summaries()[shape["elidable"][0]]
    assert summary.lines == 4        # three records plus the malformed line
    assert summary.malformed == 1
    assert summary.decodes == 3
    assert summary.decoded_entry_count == 3


def test_a_summary_carries_the_segments_own_last_seen_contribution(
    elision_fixture,
):
    """Elision imports this instead of re-folding the segment, and the fold is
    a per-key maximum, so the merged map equals a single whole-pass fold."""
    jr = _journal()
    _rebuild(jr)
    summaries = jr.read_segment_summaries()
    last = summaries[elision_fixture["last"]]
    assert last.last_seen_stamped, (
        "the mixed segment carries a stamped Claude observation, so an empty "
        "contribution would make every merge assertion vacuous"
    )


def test_the_summarized_totals_equal_the_traversal_the_rebuild_reported(
    elision_fixture,
):
    """Non-vacuity for every counter an elided segment later contributes: the
    per-segment parts have to add up to the whole-pass total, or importing them
    would move the rebuild record."""
    jr = _journal()
    result = _rebuild(jr)
    summaries = jr.read_segment_summaries()
    reported = result.traversal["stats_prefix"]
    assert sum(s.lines for s in summaries.values()) == reported["lines"]
    assert sum(s.bytes for s in summaries.values()) == reported["bytes"]
    assert sum(s.decodes for s in summaries.values()) == reported["decodes"]


# --------------------------------------------------------------------------
# Task 18 — the planner, and what it refuses
# --------------------------------------------------------------------------

def _prime(jr):
    """One full pass, so a sidecar and a coverage certificate both exist.

    This is the production sequence: the first rebuild reads everything, writes
    the summaries and mints the certificate from the observations it replayed;
    only the SECOND rebuild has both inputs the planner needs.
    """
    first = _rebuild(jr)
    assert first.quota_cache_coverage["status"] == "recovered", (
        "the priming rebuild must have replayed, or its certificate proves "
        "nothing about the cache"
    )
    return first


def _dump(path):
    """Every row of every table in one index, for the oracle comparison."""
    import sqlite3
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = [
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "ORDER BY name")
        ]
        return {
            table: conn.execute(
                f"SELECT * FROM {table}").fetchall()
            for table in tables
            # Written from the wall clock, so two passes never agree on it and
            # it says nothing about the journal either pass read.
            if table not in {"stats_publication_stamp"}
        }
    finally:
        conn.close()


def test_a_certified_quota_only_segment_is_never_opened(
    elision_fixture, opened,
):
    jr = _journal()
    _prime(jr)
    opened.clear()
    _rebuild(jr)
    for name in elision_fixture["elidable"]:
        assert name not in opened, f"{name} was read despite being certified"
    assert elision_fixture["last"] in opened, (
        "the canonically-last segment must always be read"
    )


def test_the_elision_counters_are_positive(elision_fixture):
    """Proves the branch fired, rather than the test passing because nothing
    was elidable."""
    jr = _journal()
    _prime(jr)
    result = _rebuild(jr)
    counters = result.traversal["elision"]
    assert counters["elidedSegments"] == len(elision_fixture["elidable"])
    assert counters["elidedLines"] > 0
    assert counters["elidedBytes"] > 0
    assert counters["coverage"] == "ok"


def test_the_result_byte_matches_a_never_eliding_oracle(
    elision_fixture, tmp_path,
):
    """The oracle is the same journal rebuilt with the sidecar removed, which
    is the one input that decides whether anything elides."""
    jr = _journal()
    _prime(jr)
    eliding = tmp_path / "eliding.sqlite"
    _rebuild(jr, target_path=str(eliding))
    assert jr.read_segment_summaries(), "the sidecar vanished"

    jr.segment_summary_sidecar_path().unlink()
    oracle = tmp_path / "oracle.sqlite"
    result = _rebuild(jr, target_path=str(oracle))
    assert result.traversal["elision"]["elidedSegments"] == 0, (
        "the oracle elided, so it is not an oracle"
    )
    assert _dump(eliding) == _dump(oracle)


def test_the_original_journal_bytes_are_unchanged(elision_fixture):
    """Acceptance criterion 4's last clause. The journal is append-only and
    elision is a READ optimization; nothing about it may rewrite a segment."""
    jr = _journal()
    core = importlib.import_module("_cctally_core")
    before = {
        name: (core.JOURNAL_DIR / name).read_bytes()
        for name in elision_fixture["segments"]
    }
    _prime(jr)
    _rebuild(jr)
    after = {
        name: (core.JOURNAL_DIR / name).read_bytes()
        for name in elision_fixture["segments"]
    }
    assert before == after


def _stale_certificate(jr, ns, shape):
    """Advance the physical mutation sequence, which no certificate survives."""
    conn = ns["open_cache_db"]()
    try:
        conn.execute(
            "INSERT INTO cache_meta (key, value) "
            "VALUES ('codex_physical_mutation_seq', '99999') "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value")
        conn.commit()
    finally:
        conn.close()


def _inserted_bootstrap(jr, ns, shape):
    """A bootstrap sorts BEFORE every observation segment, so its insertion
    changes the canonical order without changing any existing segment."""
    core = importlib.import_module("_cctally_core")
    (core.JOURNAL_DIR / "bootstrap-20260101T000000_000000.jsonl").write_bytes(
        b'{"v":1,"t":"obs","id":"x","at":"2026-04-01T00:00:00Z",'
        b'"src":"fixture","provider":"claude","payload":{}}\n')


def _torn_suffix(jr, ns, shape):
    """A partial trailing line: the covered offset falls short of the size."""
    core = importlib.import_module("_cctally_core")
    with open(core.JOURNAL_DIR / shape["elidable"][0], "ab") as handle:
        handle.write(b'{"v":1,"t":"obs"')


def _discarded_sidecar(jr, ns, shape):
    """A torn sidecar is dropped whole, not repaired."""
    path = jr.segment_summary_sidecar_path()
    path.write_bytes(path.read_bytes()[:-6])


@pytest.mark.parametrize("mutation", [
    _stale_certificate, _inserted_bootstrap, _torn_suffix, _discarded_sidecar,
], ids=["stale-certificate", "inserted-bootstrap", "torn-suffix",
        "discarded-sidecar"])
def test_elision_refuses_and_reopens(mutation, elision_fixture, opened):
    jr = _journal()
    _prime(jr)
    mutation(jr, elision_fixture["ns"], elision_fixture)
    opened.clear()
    _rebuild(jr)
    for name in elision_fixture["elidable"]:
        assert name in opened, (
            f"{name} was elided under {mutation.__name__}"
        )


def test_a_segment_holding_a_retained_record_is_read_even_when_certified(
    elision_fixture, opened,
):
    """The last segment holds retained records, so this pins the predicate on a
    segment that is refused for its CONTENT rather than for its position."""
    jr = _journal()
    core = importlib.import_module("_cctally_core")
    # A fourth segment, sorting after the mixed one, makes the mixed one
    # non-last so `quota_only` is the only condition it fails.
    F.append_to_segment(
        core.APP_DIR, "observations-2026-07.jsonl",
        [F.codex_quota_obs(line_offset=99, at="2026-07-15T12:00:00Z")])
    _prime(jr)
    opened.clear()
    _rebuild(jr)
    assert elision_fixture["last"] in opened
    for name in elision_fixture["elidable"]:
        assert name not in opened


def test_both_callers_use_the_same_planner(elision_fixture, opened):
    """Spec 5.6: one planner, two callers. A prefix validation that read a
    segment an eliding rebuild skipped would be validating a different
    journal from the one the rebuild folded."""
    jr = _journal()
    core = importlib.import_module("_cctally_core")
    _prime(jr)
    _rebuild(jr)
    opened.clear()
    assert jr.stats_index_matches_journal_prefix(
        pathlib.Path(core.DB_PATH), elision_fixture["high_water"]) is True
    for name in elision_fixture["elidable"]:
        assert name not in opened
    assert elision_fixture["last"] in opened


def test_a_same_size_torn_tail_repair_refuses_elision(
    elision_fixture, opened,
):
    """Spec §7 case 3's second half and §8 criterion 13b, at integration level.

    Those are the two it satisfies. It is NOT criterion 5, which the commit that
    added it named: criterion 5 enumerates the stale-certificate, inserted-
    bootstrap, canonically-last, retained-record and protocol-resolution
    refusals, and `test_elision_refuses_and_reopens`,
    `test_a_certified_quota_only_segment_is_never_opened`,
    `test_a_segment_holding_a_retained_record_is_read_even_when_certified` and
    `test_a_resolution_op_forces_a_re_read_and_an_identical_hash` are what cover
    those. A same-size torn-tail repair is criterion 13b's second clause.

    `_repair_torn_tail` truncates a partial trailing line before appending, so a
    segment's raw size can land back on EXACTLY its previous value while its
    bytes changed. Here the summary is written while the segment carries a torn
    tail — raw size S+L, last newline at S — and the repair then replaces that
    torn tail with a complete line of the same length, so the raw size is S+L
    again.

    Both size operands therefore AGREE, which is the whole point: a predicate
    comparing only the summary's size against the pinned raw extent elides this
    segment. The three-way equality refuses it, because the summary's own
    covered offset is short of the size it summarized.

    TWO independent mechanisms refuse this at integration level, and the test
    separates them rather than letting either stand in for the other. The
    repair moves the segment's complete-line offset, which is part of the pinned
    extent vector, so the certificate's identity root moves with it and the
    certificate stops covering anything. That guard alone would make the
    reopen assertion pass against a predicate with the extent check REMOVED. So
    the test also asserts the refusal REASON, which the predicate decides before
    coverage is consulted, and re-runs the predicate directly against the stored
    summary with `certificate_covers=True` — the arrangement in which the
    certificate cannot be what refused it.
    """
    jr = _journal()
    core = importlib.import_module("_cctally_core")
    target = elision_fixture["elidable"][0]
    path = core.JOURNAL_DIR / target

    body = path.read_bytes()
    complete_size = len(body)
    last_line = body[body[:-1].rfind(b"\n") + 1:]
    # A torn tail of EXACTLY one line's length: the same bytes with the trailing
    # newline replaced, so nothing after it decodes and the raw size is S + L.
    torn_tail = last_line[:-1] + b"X"
    path.write_bytes(body + torn_tail)
    torn_size = path.stat().st_size

    _prime(jr)
    summary = jr.read_segment_summaries()[target]
    assert summary.summarized_size == torn_size
    assert summary.complete_line_covered_offset == complete_size

    # The repair: drop the torn tail, append a COMPLETE line of the same length.
    path.write_bytes(body + last_line)
    assert path.stat().st_size == torn_size, (
        "the repair did not land back on the summarized size, so a plain size "
        "comparison would already have refused and the test proves nothing"
    )
    assert path.read_bytes()[complete_size:] != torn_tail, (
        "the repair changed no bytes, so nothing distinguishes it from the "
        "state the summary described"
    )

    opened.clear()
    result = _rebuild(jr)
    assert target in opened, "a same-size torn-tail repair was elided"
    assert result.traversal["elision"]["refusals"][target] == ss.REASON_EXTENT

    stat = (core.JOURNAL_DIR / target).stat()
    ok, why = ss.summary_is_elidable(
        summary, pinned_raw_extent=stat.st_size, is_last=False,
        certificate_covers=True, resolution_seen=False,
        stat_identity=(stat.st_dev, stat.st_ino))
    assert (ok, why) == (False, ss.REASON_EXTENT), (
        "the stored summary would have been elided under a certificate that "
        "covered it, so only the certificate was refusing this segment"
    )


def test_an_elision_refusal_writes_no_new_stderr_line(
    elision_fixture, capfd,
):
    """Acceptance criterion 10's silent half, for elision.

    The maintainer's decision was that skipped work is reported in the
    structured rebuild record and `db rebuild --json` only, with stderr quiet.
    A refusal is the state most likely to acquire a diagnostic line later,
    because it is the state somebody debugging wants to see — so it is the one
    worth pinning.
    """
    jr = _journal()
    _prime(jr)
    capfd.readouterr()

    _torn_suffix(jr, elision_fixture["ns"], elision_fixture)
    result = _rebuild(jr)
    captured = capfd.readouterr()

    refusals = result.traversal["elision"]["refusals"]
    assert refusals[elision_fixture["elidable"][0]] != ss.REASON_OK, (
        "nothing was refused, so the silence assertion is vacuous"
    )
    assert captured.err == "", captured.err

    # Non-vacuity for the capture itself: a line this module wrote to its own
    # stderr WOULD have been seen. Without this the assertion above passes just
    # as well against a harness that captures nothing.
    print("elision-stderr-probe", file=jr.sys.stderr)
    assert "elision-stderr-probe" in capfd.readouterr().err


# --------------------------------------------------------------------------
# The refill: a certificate that stops being valid between the plan and the leg
# --------------------------------------------------------------------------

def _advance_physical_seq(ns):
    """Move `codex_physical_mutation_seq`, which no stored certificate survives.

    Monotonic rather than a fixed literal, so a second call inside one test
    invalidates a certificate minted after the first.
    """
    conn = ns["open_cache_db"]()
    try:
        conn.execute(
            "INSERT INTO cache_meta (key, value) "
            "VALUES ('codex_physical_mutation_seq', '1000') "
            "ON CONFLICT(key) DO UPDATE SET "
            "value = CAST(cache_meta.value AS INTEGER) + 1000")
        conn.commit()
    finally:
        conn.close()


def _record_replay_order(jr, monkeypatch) -> list:
    """The `line_offset` of every record the recovery hands to the applier.

    ORDER, not membership. `_QUOTA_SNAPSHOT_INSERT` is `INSERT OR IGNORE` and
    resolves first-wins on the natural key, and `CodexResetAnchorResolver`
    decides per record in stream order — so a set comparison would pass against
    a stream replayed backwards.
    """
    real = jr._apply_quota_records
    seen: list = []

    def recording(cache, records, *args, **kwargs):
        seen.extend(
            int((rec.get("payload") or {}).get("line_offset", -1))
            for rec in records)
        return real(cache, records, *args, **kwargs)

    monkeypatch.setattr(jr, "_apply_quota_records", recording)
    return seen


def _invalidate_between_the_plan_and_the_leg(jr, ns, monkeypatch):
    """Advance the physical sequence AFTER the planner read the certificate.

    That is the race the refill exists for, reproduced exactly. The planner has
    to consult the certificate before the first segment is reached, and holding
    a `cache.db` read transaction open across a whole traversal would block WAL
    checkpointing for the length of the rebuild (#297) — so it takes a
    short-lived snapshot and the leg re-resolves coverage under its own. An
    ordinary Codex batch landing in between moves the verdict, and this seam is
    that batch.
    """
    real = jr.plan_segment_elision

    def planned(segments, high_water):
        plan = real(segments, high_water)
        _advance_physical_seq(ns)
        return plan

    monkeypatch.setattr(jr, "plan_segment_elision", planned)


def _stored_line_offsets(ns) -> list:
    conn = ns["open_cache_db"]()
    try:
        return [row[0] for row in conn.execute(
            "SELECT line_offset FROM quota_window_snapshots "
            "ORDER BY line_offset")]
    finally:
        conn.close()


def test_a_refill_after_a_mid_pass_invalidation_replays_in_journal_order(
    elision_fixture, monkeypatch,
):
    """The refill must reproduce journal order, not merely journal membership.

    An elided segment appends nothing to `quota_raw`, so every segment of a
    contiguous elided run records the SAME insertion index. Splicing each one at
    that shared index walks the run backwards. The maintainer's journal opens
    with six adjacent quota-only bootstrap segments, so on that journal the
    whole run reverses.

    The fixture's two elidable segments are adjacent and both precede every
    retained record, which is that shape at the smallest size that can show it.
    """
    jr = _journal()
    ns = elision_fixture["ns"]
    _prime(jr)

    replayed = _record_replay_order(jr, monkeypatch)

    # The order a pass that never elides hands to the applier: the journal's.
    jr.segment_summary_sidecar_path().unlink()
    _advance_physical_seq(ns)
    reference = _rebuild(jr)
    assert reference.traversal["elision"]["elidedSegments"] == 0, (
        "the reference pass elided, so it is not a reference"
    )
    journal_order = list(replayed)
    assert journal_order, "the reference replayed nothing; the comparison is vacuous"
    assert journal_order == sorted(journal_order), (
        "the fixture's own line offsets do not ascend with journal order, so "
        "this comparison cannot detect a reversal"
    )

    # Now the eliding pass, with the certificate invalidated mid-pass. Wiping
    # the materialized rows first — WITHOUT touching the physical sequence, so
    # the certificate the planner reads is still valid — is what makes the
    # materialization half of this test able to fail.
    conn = ns["open_cache_db"]()
    try:
        conn.execute("DELETE FROM quota_window_snapshots")
        conn.commit()
    finally:
        conn.close()
    assert _stored_line_offsets(ns) == []

    replayed.clear()
    _invalidate_between_the_plan_and_the_leg(jr, ns, monkeypatch)
    result = _rebuild(jr)

    assert result.traversal["elision"]["elidedSegments"] == 2, (
        "nothing was elided, so the refill never ran"
    )
    coverage = result.quota_cache_coverage
    assert coverage["status"] == "recovered"
    assert coverage["elisionRefill"]["observations"] == len(journal_order)
    assert coverage["elisionRefill"]["complete"] is True
    assert replayed == journal_order
    assert _stored_line_offsets(ns) == sorted(journal_order), (
        "an elided observation was never materialized, under a certificate the "
        "pass then minted over it"
    )


def _stored_certificate(jr, core):
    """The certificate `cache.db` holds, through the rebuild's own read path.

    `_read_coverage_snapshot` leaves its transaction OPEN by design, so this
    closes it. Reading through that function rather than through a fresh SELECT
    is deliberate: it is the path every coverage decision goes through, so a
    certificate it cannot see is one no decision can rest on either.
    """
    snapshot = jr._read_coverage_snapshot(core.CACHE_DB_PATH)
    if snapshot is None:
        return None
    try:
        return snapshot.certificate
    finally:
        jr._close_coverage_snapshot(snapshot)


def test_a_refill_read_failure_establishes_no_coverage(
    elision_fixture, monkeypatch,
):
    """A refill that cannot re-read an elided segment must mint nothing.

    A VANISHED segment self-heals: the pinned vector no longer matches the
    journal, so the certificate is invalid the moment it is read. A transient
    read error on an unchanged file does not — the vector still matches, so a
    certificate minted over the shortened stream would be valid and would claim
    observations nobody applied. That is the one direction the certificate
    design exists to prevent, so the pass drops its covered boundary and takes
    the `noCoverageEstablished` path instead.

    The reported `coveredHighWater` is the pass's own account of itself. This
    also reads the STORED certificate back, because "minted nothing" is a claim
    about `cache.db` rather than about the rebuild record: the pass must leave
    the priming certificate exactly as it found it, and that certificate must
    not validate against the journal and sequence the pass ran over.
    """
    jr = _journal()
    core = importlib.import_module("_cctally_core")
    coverage_kernel = importlib.import_module("_lib_cache_coverage")
    ns = elision_fixture["ns"]
    target = elision_fixture["elidable"][0]
    _prime(jr)
    before = _stored_certificate(jr, core)
    assert before is not None, (
        "priming minted no certificate, so an absence assertion afterwards "
        "would hold against a pass that never mints anything"
    )
    _invalidate_between_the_plan_and_the_leg(jr, ns, monkeypatch)

    real_lines = jr._iter_segment_lines

    def failing(seg_path, lo, hi, **kwargs):
        if pathlib.Path(seg_path).name == target:
            raise OSError("injected refill read failure")
        return real_lines(seg_path, lo, hi, **kwargs)

    monkeypatch.setattr(jr, "_iter_segment_lines", failing)
    result = _rebuild(jr)

    assert result.traversal["elision"]["elidedSegments"] == 2, (
        "nothing was elided, so no refill was attempted"
    )
    coverage = result.quota_cache_coverage
    assert coverage["elisionRefill"]["complete"] is False
    assert coverage["complete"] is False
    assert coverage["coveredHighWater"] is None
    assert coverage["remainder"]["reason"] == "noCoverageEstablished"

    after = _stored_certificate(jr, core)
    assert after == before, (
        "the pass wrote a certificate over a stream it could not complete"
    )
    snapshot = jr._read_coverage_snapshot(core.CACHE_DB_PATH)
    assert snapshot is not None
    try:
        valid, _why = coverage_kernel.certificate_is_valid(
            snapshot.certificate,
            pinned_vector=jr.coverage_pinned_vector(),
            physical_seq=snapshot.physical_seq,
        )
    finally:
        jr._close_coverage_snapshot(snapshot)
    assert valid is False, (
        "a stored certificate still validates over this journal, so something "
        "claims coverage of the observations the refill could not read"
    )


# --------------------------------------------------------------------------
# The refill's ORDER, over layouts the shipped fixture cannot express
# --------------------------------------------------------------------------
#
# `build_elision_scenario`'s two elidable segments are its only quota-bearing
# ones, so `quota_raw` is EMPTY when the refill runs over it and every ordering
# of the recovered lines produces the same stream. The layouts below put
# observations in segments the pass READ, which is the only arrangement in which
# a misplaced splice is observable at all.


def _offsets(raw_lines):
    """The `line_offset` of each raw line, which ascends with journal order."""
    import _lib_journal as jl

    out = []
    for raw in raw_lines:
        record = jl.decode_line(raw)
        assert record is not None, raw
        out.append(int((record.get("payload") or {})["line_offset"]))
    return out


@pytest.mark.parametrize("layout", [
    "EEN", "NEE", "NEEN", "EENEE", "EEEEEENN",
], ids=[
    "run-at-the-start", "run-at-the-end", "run-in-the-middle",
    "two-runs-separated-by-a-read-segment", "production-six-adjacent",
])
def test_the_refill_reproduces_journal_order_for_every_run_layout(
    layout, tmp_path, monkeypatch,
):
    """`E` marks a segment the pass elided, `N` one it read.

    An elided segment appends nothing to `quota_raw`, so every segment of a
    contiguous elided run records the SAME insertion index. Two families of
    wrong implementation survive a fixture whose read segments carry no
    observations: one that appends every recovered line before `quota_raw`, and
    one that sweeps the gaps in ascending order without advancing its cursor.
    The layouts here separate both from the correct answer — `run-at-the-end`,
    `run-in-the-middle` and `two-runs-separated-by-a-read-segment` each place a
    gap at a non-zero index, which is what neither wrong form can reproduce.

    `production-six-adjacent` is the maintainer's own shape — six adjacent
    quota-only bootstrap segments followed by segments that hold retained
    records — and it is carried for coverage rather than for discrimination:
    with every gap at index 0 it cannot separate the three implementations.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    core = importlib.import_module("_cctally_core")
    jr = _journal()
    shape = F.build_refill_layout(core.JOURNAL_DIR, layout)

    assert shape["gaps"], "the layout elides nothing"
    if "N" in layout:
        assert shape["quota_raw"], (
            "no read segment contributed an observation, so the recovered "
            "lines have nothing to be ordered against"
        )
    expected = _offsets(shape["journal_order"])
    assert expected == sorted(expected), (
        "the layout's own offsets do not ascend with journal order, so the "
        "comparison below cannot detect a reordering"
    )

    out, complete = jr._refill_elided_quota_raw(
        shape["quota_raw"], shape["gaps"])

    assert complete is True
    assert _offsets(out) == expected
    assert out == shape["journal_order"]


def test_a_short_read_during_the_refill_reports_incomplete(
    tmp_path, monkeypatch,
):
    """`_iter_segment_lines` stops SILENTLY at EOF.

    It reads until its own `read()` returns nothing, so a segment that yields
    fewer lines than it held produces a shorter stream and no exception. The
    refill therefore cannot learn about a short read from the `except` clause;
    it has to compare what it read against the line count the summary recorded.
    Without that comparison the pass replays a stream missing observations and
    then mints a certificate claiming coverage over them.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    core = importlib.import_module("_cctally_core")
    jr = _journal()
    shape = F.build_refill_layout(core.JOURNAL_DIR, "EN", per_segment=3)
    target = shape["gaps"][0][0]

    intact, complete = jr._refill_elided_quota_raw(
        shape["quota_raw"], shape["gaps"])
    assert (complete, intact) == (True, shape["journal_order"]), (
        "the layout does not refill cleanly to begin with, so a shortfall "
        "afterwards would prove nothing"
    )

    real_lines = jr._iter_segment_lines

    def truncated(seg_path, lo, hi, **kwargs):
        stream = real_lines(seg_path, lo, hi, **kwargs)
        if pathlib.Path(seg_path).name != target:
            yield from stream
            return
        # Exactly what the real reader does when the file ends early: it yields
        # what it has and returns, with nothing raised.
        for index, item in enumerate(stream):
            if index >= 1:
                return
            yield item

    monkeypatch.setattr(jr, "_iter_segment_lines", truncated)
    out, complete = jr._refill_elided_quota_raw(
        shape["quota_raw"], shape["gaps"])

    assert complete is False, (
        "a short read reported a complete refill, so the leg would keep its "
        "covered boundary and certify observations nobody replayed"
    )
    assert len(out) < len(shape["journal_order"]), (
        "the injection changed nothing, so the assertion above is vacuous"
    )


def test_a_non_oserror_during_the_refill_falls_back_instead_of_raising(
    tmp_path, monkeypatch,
):
    """Spec §6.3 makes every degraded state a silent full-replay fallback.

    Neither the call site nor its caller catches anything, so an exception the
    refill lets escape aborts the whole rebuild — which is the one outcome the
    taxonomy rules out for a re-derivable optimization.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    core = importlib.import_module("_cctally_core")
    jr = _journal()
    shape = F.build_refill_layout(core.JOURNAL_DIR, "NEN")
    target = shape["gaps"][0][0]

    real_lines = jr._iter_segment_lines

    def exploding(seg_path, lo, hi, **kwargs):
        if pathlib.Path(seg_path).name == target:
            raise ValueError("injected non-OSError refill failure")
        return real_lines(seg_path, lo, hi, **kwargs)

    monkeypatch.setattr(jr, "_iter_segment_lines", exploding)
    out, complete = jr._refill_elided_quota_raw(
        shape["quota_raw"], shape["gaps"])

    assert complete is False
    assert out == shape["quota_raw"], (
        "the failing segment contributed lines it could not read"
    )


# --------------------------------------------------------------------------
# The refill end to end, over a journal whose read segments carry observations
# --------------------------------------------------------------------------

@pytest.fixture
def interleaved_fixture(tmp_path, monkeypatch):
    """Elidable, read-and-quota-bearing, elidable, last.

    Its own scenario rather than an enrichment of `build_elision_scenario`, per
    spec §7.1: adding a quota-bearing read segment to that one would retire the
    empty-`quota_raw` shape its own tests describe.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    core = importlib.import_module("_cctally_core")
    ns["open_cache_db"]().close()
    shape = F.build_interleaved_elision_scenario(core.APP_DIR)
    shape["ns"] = ns
    return shape


def test_a_refill_splices_around_a_read_segment_and_stays_outside_the_flocks(
    interleaved_fixture, monkeypatch,
):
    """The refill's order AND its lock discipline, on the discriminating shape.

    The middle segment is read — it holds a retained `evt` — and carries a quota
    observation, so the second gap's insertion index is non-zero and the
    recovered lines have to land on either side of an observation the pass
    already held. That is the arrangement the shipped fixture cannot produce.

    Spec §8 criterion 4b also says the re-read happens OUTSIDE both flocks. That
    was structurally true and unobserved: `test_no_journal_segment_is_opened_
    while_the_flocks_are_held` rebuilds a fresh journal once, so nothing elides
    there and the refill never runs. Here the refill's OWN opens are attributed
    to it and checked against the lock state, so moving the call under the
    recovery hold fails this test rather than shipping green.
    """
    jr = _journal()
    ns = interleaved_fixture["ns"]
    _prime(jr)

    replayed = _record_replay_order(jr, monkeypatch)

    # The order a never-eliding pass hands to the applier: the journal's.
    jr.segment_summary_sidecar_path().unlink()
    _advance_physical_seq(ns)
    reference = _rebuild(jr)
    assert reference.traversal["elision"]["elidedSegments"] == 0, (
        "the reference pass elided, so it is not a reference"
    )
    journal_order = list(replayed)
    assert journal_order == [0, 1, 10, 20, 21, 30], journal_order
    assert journal_order == sorted(journal_order), (
        "the fixture's own line offsets do not ascend with journal order"
    )

    conn = ns["open_cache_db"]()
    try:
        conn.execute("DELETE FROM quota_window_snapshots")
        conn.commit()
    finally:
        conn.close()
    assert _stored_line_offsets(ns) == []

    replayed.clear()
    _invalidate_between_the_plan_and_the_leg(jr, ns, monkeypatch)

    tracker = _LockTracker().install(monkeypatch)
    refill_opens: list = []
    refill_opens_while_held: list = []
    inside_refill = {"now": False}

    real_refill = jr._refill_elided_quota_raw
    captured_gaps: list = []

    def refilling(quota_raw, gaps):
        captured_gaps.extend(gaps)
        inside_refill["now"] = True
        try:
            return real_refill(quota_raw, gaps)
        finally:
            inside_refill["now"] = False

    real_open = jr._open_segment_for_read

    def counted(seg_path, *args, **kwargs):
        if inside_refill["now"]:
            name = pathlib.Path(seg_path).name
            refill_opens.append(name)
            if tracker.held:
                refill_opens_while_held.append(name)
        return real_open(seg_path, *args, **kwargs)

    monkeypatch.setattr(jr, "_refill_elided_quota_raw", refilling)
    monkeypatch.setattr(jr, "_open_segment_for_read", counted)
    result = _rebuild(jr)

    assert result.traversal["elision"]["elidedSegments"] == 2, (
        "nothing was elided, so the refill never ran"
    )
    coverage = result.quota_cache_coverage
    assert coverage["status"] == "recovered"
    assert coverage["elisionRefill"]["observations"] == 4, (
        "the two elided segments hold four observations between them"
    )
    assert coverage["elisionRefill"]["complete"] is True
    assert replayed == journal_order
    assert _stored_line_offsets(ns) == sorted(journal_order)

    # The gaps the PRODUCTION path built, which is what grounds the layout
    # tests above: they construct gaps by hand, so the rule they use has to be
    # the rule `_elide_segment` uses. The second index is non-zero because the
    # middle segment contributed one observation before the second elision, and
    # a non-zero index is exactly what the shipped fixture cannot produce.
    assert [gap[0] for gap in captured_gaps] == interleaved_fixture["elidable"]
    assert [gap[1] for gap in captured_gaps] == [0, 1]
    assert [gap[3] for gap in captured_gaps] == [2, 2], (
        "each elided segment holds two lines, and the refill compares that "
        "count against what it reads back"
    )

    assert sorted(refill_opens) == sorted(interleaved_fixture["elidable"]), (
        "the refill did not open the segments it re-read, so the lock "
        "assertion below observed nothing"
    )
    assert len(tracker.holds) >= 1, (
        "no cache writer flock was taken during this pass, so 'outside the "
        "flocks' holds trivially"
    )
    assert refill_opens_while_held == []


# --------------------------------------------------------------------------
# Task 19 — sequence parity and the prefix-hash fallback
# --------------------------------------------------------------------------

def _violations(path):
    import sqlite3
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return conn.execute(
            "SELECT fingerprint, batch_id, kind, violation_json, "
            "available_after FROM journal_protocol_violations "
            "ORDER BY fingerprint").fetchall()
    finally:
        conn.close()


def _winning_sequences(path):
    import sqlite3
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return conn.execute(
            "SELECT event_id, rev, winning_sequence "
            "FROM journal_effective_events ORDER BY event_id, rev").fetchall()
    finally:
        conn.close()


def _both_ways(jr, tmp_path, tag):
    """``(eliding, oracle)`` index paths over the same journal.

    The oracle is the same rebuild with the sidecar removed — the one input the
    planner needs — so the two differ in exactly one thing.
    """
    eliding = tmp_path / f"{tag}-eliding.sqlite"
    result = _rebuild(jr, target_path=str(eliding))
    assert result.traversal["elision"]["elidedSegments"] > 0, (
        "nothing was elided, so the comparison would be vacuous"
    )
    jr.segment_summary_sidecar_path().unlink()
    oracle = tmp_path / f"{tag}-oracle.sqlite"
    reference = _rebuild(jr, target_path=str(oracle))
    assert reference.traversal["elision"]["elidedSegments"] == 0
    return eliding, oracle


def test_sequence_numbers_and_violation_fingerprints_are_identical(
    elision_fixture, tmp_path,
):
    """Three of the seven structural violation kinds hash the `enumerate()`
    index into a DURABLE fingerprint, stored in `journal_protocol_violations`
    and referenced BY NAME from a `journal_protocol_resolution` op that repair
    mints from the UNFILTERED record list. Renumbering makes a previously
    acknowledged violation unresolvable and raises on every later rebuild.

    The fixture places a quota-only prefix before a `commit_without_begin`,
    whose evidence carries `commitSequence` — so a wrong placeholder count from
    an elided segment moves this fingerprint and nothing else has to go wrong.
    """
    jr = _journal()
    _prime(jr)
    eliding, oracle = _both_ways(jr, tmp_path, "parity")

    violations = _violations(eliding)
    assert violations, "no violation materialized — the comparison is vacuous"
    assert violations == _violations(oracle)
    sequences = _winning_sequences(eliding)
    assert any(row[2] is not None for row in sequences), (
        "no winning sequence was recorded — the comparison is vacuous"
    )
    assert sequences == _winning_sequences(oracle)


def test_two_adjacent_elided_segments_preserve_numbering(
    elision_fixture, tmp_path,
):
    """Cumulative placeholder counts across a boundary. Two segments each
    contributing their own count is the case a single-segment fixture cannot
    distinguish from one contributing both."""
    jr = _journal()
    _prime(jr)
    result = _rebuild(jr, target_path=str(tmp_path / "adjacent.sqlite"))
    assert result.traversal["elision"]["elidedSegments"] == 2
    eliding, oracle = _both_ways(jr, tmp_path, "adjacent2")
    assert _winning_sequences(eliding) == _winning_sequences(oracle)
    assert _violations(eliding) == _violations(oracle)


def test_malformed_lines_do_not_contribute_placeholders(
    tmp_path, monkeypatch,
):
    """A malformed line is read and counted, and appends NO `decoded` entry.

    Folding it into the placeholder count would insert an element a non-eliding
    pass never appends, moving every later sequence number by one.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    core = importlib.import_module("_cctally_core")
    ns["open_cache_db"]().close()
    F.build_elision_scenario(core.APP_DIR, malformed_in_first=True)
    jr = _journal()
    _prime(jr)

    eliding, oracle = _both_ways(jr, tmp_path, "malformed")
    assert _winning_sequences(eliding) == _winning_sequences(oracle)
    assert _violations(eliding) == _violations(oracle)


def test_a_resolution_op_forces_a_re_read_and_an_identical_hash(
    elision_fixture, opened, tmp_path,
):
    """`journal_prefix_hash` absorbs completed segments into ONE sequential
    sha256 and `hashlib` cannot restore midstate, so it cannot be computed over
    an elided prefix. Optimistic elision, re-read fallback. Production holds
    zero resolution ops, which is exactly why this needs a test.
    """
    jr = _journal()
    core = importlib.import_module("_cctally_core")
    shape = F.add_elision_resolution_op(core.APP_DIR, elision_fixture)
    _prime(jr)

    opened.clear()
    result = _rebuild(jr, target_path=str(tmp_path / "resolved.sqlite"))
    assert result.traversal["elision"]["elidedSegments"] > 0, (
        "nothing was elided, so the fallback was never exercised"
    )
    assert result.acknowledged_protocol_violations, (
        "the resolution op acknowledged nothing — its digest was rejected, "
        "which is the failure this test exists to catch"
    )
    for name in shape["elidable"]:
        assert name in opened, (
            f"{name} was elided but never re-read for the prefix digest"
        )


def test_the_prefix_digest_over_an_elided_pass_is_byte_identical(
    elision_fixture, tmp_path,
):
    """The digest an ELIDING pass binds equals a never-eliding pass's.

    `resolve_effective_events` refuses a `journal_protocol_resolution` op whose
    recomputed raw-prefix binding does not match the one the op recorded, so a
    non-empty acknowledged list IS the digest comparison: a pass that composed
    a wrong digest over the gap would acknowledge nothing. Comparing the two
    lists pins that both passes reached the same one.
    """
    jr = _journal()
    core = importlib.import_module("_cctally_core")
    F.add_elision_resolution_op(core.APP_DIR, elision_fixture)
    _prime(jr)

    eliding = _rebuild(jr, target_path=str(tmp_path / "digest-eliding.sqlite"))
    assert eliding.traversal["elision"]["elidedSegments"] > 0
    jr.segment_summary_sidecar_path().unlink()
    oracle = _rebuild(jr, target_path=str(tmp_path / "digest-oracle.sqlite"))
    assert oracle.traversal["elision"]["elidedSegments"] == 0

    acknowledged = sorted(
        item.fingerprint for item in eliding.acknowledged_protocol_violations)
    assert acknowledged, (
        "nothing was acknowledged, so the digest was never compared"
    )
    assert acknowledged == sorted(
        item.fingerprint for item in oracle.acknowledged_protocol_violations)
