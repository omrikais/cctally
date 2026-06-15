import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bin"))
import _lib_conversation_watch as w


class _Stats:
    def __init__(self, clean, reason=None):
        self.targeted_clean = clean
        self.deferred_reason = reason


def test_changed_paths_detects_size_change():
    sigs = {"a": 10, "b": 5}
    seen = {"a": 10, "b": 4}
    assert w.changed_paths(["a", "b"], seen, lambda p: sigs[p]) == ["b"]


def test_changed_paths_missing_from_seen_is_changed():
    assert w.changed_paths(["a"], {}, lambda p: 3) == ["a"]


def test_changed_paths_skips_unstatable():
    def stat_fn(p):
        return None if p == "gone" else 1
    assert w.changed_paths(["gone", "a"], {"a": 0}, stat_fn) == ["a"]


def test_changed_paths_mtime_only_change_is_not_changed():
    # REGRESSION (latent infinite-emit loop): the watch signature is SIZE-ONLY,
    # matching sync_cache's size-only delta. A file whose mtime advanced but
    # whose size did NOT change must NOT be re-detected — otherwise, since a
    # size-unchanged sync_cache ingest does not refresh session_files.mtime_ns,
    # the committed cursor would keep returning the old mtime, `seen` would never
    # catch up, and the watch loop would re-emit an empty tail ping every cycle
    # forever. The stat_fn here models "mtime advanced, size unchanged": it
    # returns the SAME size (100) that `seen` already holds.
    #
    # RED under the old (size, mtime_ns) tuple comparison: stat_fn would return
    # (100, <new mtime>) which differs from seen's (100, <old mtime>) → the path
    # would be (wrongly) reported as changed. GREEN now that the signature is the
    # bare size int → 100 == 100 → not changed.
    seen = {"p": 100}
    stat_fn = lambda p: 100      # same size, even though "mtime advanced"
    assert w.changed_paths(["p"], seen, stat_fn) == []


def test_watch_step_clean_ingest_emits_and_advances():
    sigs = {"a": 20}
    seen = {"a": 10}
    calls = {}

    def ingest(paths):
        calls["paths"] = set(paths)
        return _Stats(True)

    new_seen, emitted = w.watch_step(
        ["a"], seen, stat_fn=lambda p: sigs[p], ingest_fn=ingest)
    assert emitted is True
    assert new_seen["a"] == 20               # advanced
    assert calls["paths"] == {"a"}


def test_watch_step_no_change_no_emit():
    new_seen, emitted = w.watch_step(
        ["a"], {"a": 10}, stat_fn=lambda p: 10, ingest_fn=lambda p: _Stats(True))
    assert emitted is False
    assert new_seen == {"a": 10}


def test_watch_step_unclean_ingest_does_not_advance_or_emit():
    new_seen, emitted = w.watch_step(
        ["a"], {"a": 10}, stat_fn=lambda p: 20,
        ingest_fn=lambda p: _Stats(False, "truncation"))
    assert emitted is False
    assert new_seen == {"a": 10}             # NOT advanced → retried next cycle


def test_watch_step_advances_to_committed_cursor_not_fresh_restat():
    # The file grew DURING the ingest: stat_fn (the change-detection re-stat)
    # sees the new, larger size, but the targeted sync_cache only consumed up
    # to the size IT stat'd at read time — recorded in the cache cursor and
    # surfaced by committed_sig_fn (a SMALLER signature here). `seen` must
    # track the cache cursor, NOT the fresh filesystem re-stat, or the gap
    # between the cursor and the new disk size is silently lost.
    disk_sig = 30               # current filesystem size (grew during ingest)
    committed_sig = 20          # what the cache actually consumed (the cursor)
    seen = {"a": 10}

    new_seen, emitted = w.watch_step(
        ["a"], seen,
        stat_fn=lambda p: disk_sig,
        ingest_fn=lambda p: _Stats(True),
        committed_sig_fn=lambda p: committed_sig,
    )
    assert emitted is True
    # Advanced to the COMMITTED cursor, not the larger fresh re-stat.
    assert new_seen["a"] == committed_sig
    # The residual disk growth is still pending → next cycle re-detects it.
    assert w.changed_paths(["a"], new_seen, lambda p: disk_sig) == ["a"]


def test_watch_step_committed_sig_fn_defaults_to_stat_fn():
    # With no committed_sig_fn injected (pure unit tests / no cache), behavior
    # falls back to stat_fn so the existing single-stat semantics still hold.
    new_seen, emitted = w.watch_step(
        ["a"], {"a": 10}, stat_fn=lambda p: 20,
        ingest_fn=lambda p: _Stats(True))
    assert emitted is True
    assert new_seen["a"] == 20
