"""#496 S5 — `_cctally_journal`'s segment reads go through one observable seam.

A per-pass line or byte counter cannot detect a hidden re-read: it stays
correct while an implementation reopens a segment behind it, which is exactly
how S4's read-path tests pass over a reopening implementation. These tests
instrument the physical open instead, so a pass that reads a segment twice
produces a visibly different sequence rather than an identical count.

SCOPE: the seam covers `bin/_cctally_journal.py`'s four read-only routes and
therefore the callers that reach the journal through them — `db journal-repair`
and `db rederive`. It is NOT repository-wide. `bin/_cctally_doctor.py`'s
conflict scan reads each segment with `Path.read_bytes()` directly and calls
`_capture_protocol_prefix_evidence` with no hasher, so its traversals are
invisible here; spec §8 excludes `doctor` from this session deliberately.
"""
from __future__ import annotations

import hashlib
import importlib
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "bin"))

import journal_fixture_496_s5 as F
from conftest import load_script, redirect_paths


def _journal():
    return importlib.import_module("_cctally_journal")


@pytest.fixture
def journal_fixture(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    core = importlib.import_module("_cctally_core")
    return F.build_tainted(core.APP_DIR, seed_cache=False)


@pytest.fixture
def opened(monkeypatch, journal_fixture):
    """Every segment opened for reading, by basename, in the order opened."""
    jr = _journal()
    real = jr._open_segment_for_read
    seen: list[str] = []

    def record(seg_path):
        seen.append(pathlib.Path(seg_path).name)
        return real(seg_path)

    monkeypatch.setattr(jr, "_open_segment_for_read", record)
    return seen


def test_a_prefix_hash_and_a_streaming_pass_are_each_one_open_per_segment(
    opened, journal_fixture
):
    """Both read routes go through the seam, so a hidden re-read is visible."""
    jr = _journal()
    hw = jr.journal_high_water()
    opened.clear()
    jr.journal_prefix_hash(hw)
    canonical = jr.list_segments()
    assert opened == canonical, (
        "journal_prefix_hash must open each segment through the seam exactly "
        "once, in canonical order"
    )
    opened.clear()
    list(jr.iter_range(None, hw))
    assert opened == canonical


def test_the_fixture_has_more_than_one_segment(journal_fixture):
    """A one-segment fixture would let a re-reading pass produce a sequence
    that still looks canonical, so the assertions above need this."""
    assert len(_journal().list_segments()) >= 3


#: `journal_prefix_hash` over the whole S5 fixture prefix, captured from the
#: PRE-CHANGE implementation before the seam existed. Written out as a literal
#: rather than recomputed, because a digest recomputed by the code under test
#: would accept a changed framing silently — and this framing is durable inside
#: every `journal_protocol_resolution` payload already written.
FIXTURE_PREFIX_HASH = (
    "sha256:092e517cd9560221590ff91264300a94f4895fe914ff8f55970cadc51b8ed472"
)


def test_routing_the_hasher_through_the_seam_left_the_digest_unchanged(
    journal_fixture
):
    jr = _journal()
    assert jr.journal_high_water() == journal_fixture["high_water"]
    assert jr.journal_prefix_hash(
        journal_fixture["high_water"]) == FIXTURE_PREFIX_HASH


def test_the_cutover_reuse_scan_reads_through_the_seam(opened, journal_fixture):
    """The bootstrap-reuse digest is the third read route in this module. It
    must be observable at the same seam, or "every physical read goes through
    it" would be prose rather than a property."""
    jr = _journal()
    core = importlib.import_module("_cctally_core")
    blob = b'{"a":1}\n'
    name = "bootstrap-20260101T000000_000000.jsonl"
    (core.JOURNAL_DIR / name).write_bytes(blob)
    opened.clear()
    assert jr._reusable_bootstrap(
        hashlib.sha256(blob).hexdigest(), len(blob)) == (name, len(blob))
    assert opened == [name], (
        "only the same-size bootstrap candidate is read, and it is read once")


def test_the_quota_dedup_rebuild_reads_through_the_seam(opened, journal_fixture):
    """The fourth read route: the re-derivable quota natural-key index rebuild
    scans every segment when its compact index is absent."""
    jr = _journal()
    jr._QUOTA_DEDUP_KEYS.clear()
    jr._QUOTA_DEDUP_LOADED = False
    jr._QUOTA_DEDUP_DIR = None
    opened.clear()
    jr._load_quota_dedup_keys()
    assert opened == jr.list_segments()
