import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bin"))
import _lib_conversation_watch as w


class _Stats:
    def __init__(self, clean, reason=None):
        self.targeted_clean = clean
        self.deferred_reason = reason


def test_changed_paths_detects_size_change():
    sigs = {"a": (10, 1), "b": (5, 1)}
    seen = {"a": (10, 1), "b": (4, 1)}
    assert w.changed_paths(["a", "b"], seen, lambda p: sigs[p]) == ["b"]


def test_changed_paths_missing_from_seen_is_changed():
    assert w.changed_paths(["a"], {}, lambda p: (3, 1)) == ["a"]


def test_changed_paths_skips_unstatable():
    def stat_fn(p):
        return None if p == "gone" else (1, 1)
    assert w.changed_paths(["gone", "a"], {"a": (0, 0)}, stat_fn) == ["a"]


def test_watch_step_clean_ingest_emits_and_advances():
    sigs = {"a": (20, 2)}
    seen = {"a": (10, 1)}
    calls = {}

    def ingest(paths):
        calls["paths"] = set(paths)
        return _Stats(True)

    new_seen, emitted = w.watch_step(
        ["a"], seen, stat_fn=lambda p: sigs[p], ingest_fn=ingest)
    assert emitted is True
    assert new_seen["a"] == (20, 2)          # advanced
    assert calls["paths"] == {"a"}


def test_watch_step_no_change_no_emit():
    new_seen, emitted = w.watch_step(
        ["a"], {"a": (10, 1)}, stat_fn=lambda p: (10, 1), ingest_fn=lambda p: _Stats(True))
    assert emitted is False
    assert new_seen == {"a": (10, 1)}


def test_watch_step_unclean_ingest_does_not_advance_or_emit():
    new_seen, emitted = w.watch_step(
        ["a"], {"a": (10, 1)}, stat_fn=lambda p: (20, 2),
        ingest_fn=lambda p: _Stats(False, "truncation"))
    assert emitted is False
    assert new_seen == {"a": (10, 1)}        # NOT advanced → retried next cycle
