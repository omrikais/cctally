"""#825/#832 handoff (Part 2 -> Part 3), #901: the detached reducer runs at
status-line cadence, so its steady state must not write.

Every reducer run establishes the cutover (a verified-cache read), rescans the
legacy spool against its journal-anchored baseline, and looks up the latest
anchor by scanning the journal tail past the cached point. Before this change
that lookup republished the coordinate cache — temp file, fsync, rename,
directory fsync — on every run. Now the cache is rewritten only when the
anchor moves or the scanned point has advanced far enough to be worth
remembering; the tail scan itself stays incremental. The pre-publication
rescan semantics (spec 5.5 step 6, section 7) are unchanged.
"""
import os

import pytest

import _outcome_quarantine_trace as T

E0 = "2026-10-12T12:00:00Z"
F0 = "2026-10-09T14:00:00Z"


@pytest.fixture
def app(monkeypatch, tmp_path):
    return T.make_app(monkeypatch, tmp_path)


class _Writes:
    def __init__(self, monkeypatch):
        import _cctally_core as core
        import _cctally_journal as jr
        self.fsyncs = 0
        self.cache_writes = 0
        real_fsync = os.fsync
        real_atomic = jr._atomic_write_json

        def fsync(fd):
            self.fsyncs += 1
            return real_fsync(fd)

        def atomic(path, doc):
            if str(path) == str(core.ACCOUNT_BOUND_CUTOVER_CACHE_PATH):
                self.cache_writes += 1
            return real_atomic(path, doc)

        monkeypatch.setattr(os, "fsync", fsync)
        monkeypatch.setattr(jr, "_atomic_write_json", atomic)


def _reduce():
    import _cctally_statusline as sl
    return sl._statusline_reduce_and_publish(nudge_sink=lambda: None)


def test_a_steady_state_reducer_run_writes_nothing(app, monkeypatch):
    T.current(app, monkeypatch, at="2026-10-09T10:00:00Z", weekly=10,
              week_end=E0, five=30, five_end=F0)
    _reduce()   # warm: the first lookup may publish the cache once
    writes = _Writes(monkeypatch)
    for _ in range(3):
        _reduce()
    assert writes.cache_writes == 0
    assert writes.fsyncs == 0


def test_a_small_journal_growth_does_not_republish_the_cache(app, monkeypatch):
    T.current(app, monkeypatch, at="2026-10-09T10:00:00Z", weekly=10,
              week_end=E0, five=30, five_end=F0)
    _reduce()
    T.current(app, monkeypatch, at="2026-10-09T10:05:00Z", weekly=11,
              week_end=E0, five=31, five_end=F0)
    writes = _Writes(monkeypatch)
    _reduce()
    assert writes.cache_writes == 0
    assert writes.fsyncs == 0


def test_an_anchor_change_still_republishes_and_is_found(app, monkeypatch):
    import _cctally_core as core
    import _cctally_journal as jr
    T.current(app, monkeypatch, at="2026-10-09T10:00:00Z", weekly=10,
              week_end=E0, five=30, five_end=F0)
    _reduce()
    core.STATUSLINE_SELECTED_PATH.write_text('{"legacy": 1}')
    writes = _Writes(monkeypatch)
    _reduce()   # the rescan journals the change and advances the anchor
    assert writes.cache_writes >= 1
    digest = jr.latest_legacy_spool_anchor_digest()
    assert jr.read_anchored_legacy_spool_baseline(digest) is not None
    # And the next steady-state run is quiet again.
    quiet = _Writes(monkeypatch)
    _reduce()
    assert quiet.cache_writes == 0 and quiet.fsyncs == 0
