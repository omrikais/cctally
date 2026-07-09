"""#279 S5 F6.3: armed-only owner-thread assertion (spec §8)."""
import threading

import _lib_snapshot_cache as sc


def _mutate_from_thread():
    err = []

    def run():
        try:
            sc._assert_owner()
        except RuntimeError as exc:
            err.append(exc)

    t = threading.Thread(target=run)
    t.start()
    t.join()
    return err


def test_unarmed_is_noop():
    sc.reset_owner_thread()
    assert _mutate_from_thread() == []


def test_armed_foreign_thread_raises():
    sc.reset_owner_thread()
    try:
        sc.mark_owner_thread()
        errs = _mutate_from_thread()
        assert len(errs) == 1 and "non-owner thread" in str(errs[0])
    finally:
        sc.reset_owner_thread()


def test_remark_transfers_ownership():
    sc.reset_owner_thread()
    try:
        done = threading.Event()

        def run():
            sc.mark_owner_thread()
            sc._assert_owner()  # must not raise for the marker itself
            done.set()

        t = threading.Thread(target=run)
        t.start()
        t.join()
        assert done.is_set()
    finally:
        sc.reset_owner_thread()
