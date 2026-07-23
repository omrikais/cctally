"""Task 3 — journal append I/O (bin/_cctally_journal.py append surface).

Exercises spec §4.3 append discipline: leaf-flock serialization, torn-tail
repair, month rotation, growing offsets, and the high-water snapshot. The
concurrency case runs real OS processes (spawn context — the CI runner is
macOS, where spawn is the multiprocessing default) so the flock discipline is
tested for real, not simulated.

Isolation: JOURNAL_DIR is redirected under a per-test CCTALLY_DATA_DIR (the
env override wins first in _init_paths_from_env), which the spawned children
inherit — module-attr monkeypatches would not cross the process boundary.
"""
import datetime as dt
import multiprocessing as mp
import os
import pathlib
import stat

import pytest

import _cctally_core
import _lib_journal as J
import _cctally_journal as journal

FIXED_JULY = dt.datetime(2026, 7, 22, 12, 0, 0, tzinfo=dt.timezone.utc)
FIXED_AUG = dt.datetime(2026, 8, 3, 12, 0, 0, tzinfo=dt.timezone.utc)

_BIN_DIR = str(pathlib.Path(__file__).resolve().parents[1] / "bin")


@pytest.fixture
def journal_env(tmp_path, monkeypatch):
    """Redirect the journal to a per-test tmp data dir via CCTALLY_DATA_DIR.

    No teardown re-init on purpose. ``monkeypatch`` restores CCTALLY_DATA_DIR
    on its own at fixture-stack unwind. Re-running ``_init_paths_from_env()``
    here would be a trap: this fixture is torn down BEFORE the ``monkeypatch``
    that owns the env var, so at that point CCTALLY_DATA_DIR is still set to the
    tmp dir (not yet restored) — but even if it weren't, dev-autodetect is
    disabled, so a bare re-init with no override resolves the path globals onto
    the REAL prod dir ``~/.local/share/cctally``. Test-suite globals must never
    end pointed there. Leaving them at the (torn-down) tmp path is safe; the
    next path-sensitive test re-inits or redirect_paths-patches them.
    """
    data_dir = tmp_path / "data"
    monkeypatch.setenv("CCTALLY_DATA_DIR", str(data_dir))
    monkeypatch.setenv("CCTALLY_DISABLE_DEV_AUTODETECT", "1")
    _cctally_core._init_paths_from_env()
    yield data_dir


def _decoded_lines(path):
    return [J.decode_line(line) for line in path.read_bytes().split(b"\n") if line]


# --------------------------------------------------------------------------
# (a) dir + segment creation, permissions, growing offsets
# --------------------------------------------------------------------------

def test_append_creates_dir_and_segment_0600_with_growing_offsets(journal_env):
    data_dir = journal_env
    rec1 = J.make_obs(at="t1", src="statusline", provider="claude", payload={"i": 1})
    seg1, off1 = journal.append_record(rec1, now_utc=FIXED_JULY)
    rec2 = J.make_obs(at="t2", src="statusline", provider="claude", payload={"i": 2})
    seg2, off2 = journal.append_record(rec2, now_utc=FIXED_JULY)

    assert seg1 == seg2 == "observations-2026-07.jsonl"
    assert off1 == len(J.encode_line(rec1))
    assert off2 == off1 + len(J.encode_line(rec2))

    jdir = data_dir / "journal"
    assert stat.S_IMODE(jdir.stat().st_mode) == 0o700
    seg_path = jdir / seg1
    assert stat.S_IMODE(seg_path.stat().st_mode) == 0o600
    assert _decoded_lines(seg_path) == [rec1, rec2]


# --------------------------------------------------------------------------
# (b) torn-tail repair
# --------------------------------------------------------------------------

def test_torn_tail_is_healed_by_next_append(journal_env):
    data_dir = journal_env
    rec1 = J.make_obs(at="t1", src="statusline", provider="claude", payload={"i": 1})
    seg1, _ = journal.append_record(rec1, now_utc=FIXED_JULY)
    seg_path = data_dir / "journal" / seg1

    # Simulate a crashed appender: raw partial line, no trailing newline.
    with open(seg_path, "ab") as fh:
        fh.write(b'{"t":"obs","payload":{"partial')

    rec2 = J.make_obs(at="t2", src="statusline", provider="claude", payload={"i": 2})
    _, off2 = journal.append_record(rec2, now_utc=FIXED_JULY)

    raw = seg_path.read_bytes()
    assert raw.endswith(b"\n")
    assert b"partial" not in raw  # torn bytes truncated before the append
    assert _decoded_lines(seg_path) == [rec1, rec2]
    # offset reflects a clean concatenation — the garbage is gone
    assert off2 == len(J.encode_line(rec1)) + len(J.encode_line(rec2))


# --------------------------------------------------------------------------
# (c) month rotation
# --------------------------------------------------------------------------

def test_month_rotation_leaves_prior_segment_untouched(journal_env):
    data_dir = journal_env
    jdir = data_dir / "journal"

    recA = J.make_obs(at="tA", src="statusline", provider="claude", payload={"m": 7})
    segA, _ = journal.append_record(recA, now_utc=FIXED_JULY)
    july_bytes = (jdir / segA).read_bytes()

    recB = J.make_obs(at="tB", src="statusline", provider="claude", payload={"m": 8})
    segB, _ = journal.append_record(recB, now_utc=FIXED_AUG)

    assert segA == "observations-2026-07.jsonl"
    assert segB == "observations-2026-08.jsonl"
    assert (jdir / segB).exists()
    assert (jdir / segA).read_bytes() == july_bytes  # July untouched
    assert _decoded_lines(jdir / segB) == [recB]


# --------------------------------------------------------------------------
# (d) concurrency storm — real processes
# --------------------------------------------------------------------------

def _append_worker(bin_dir, data_dir, worker_idx, n_lines):
    import os as _os
    import sys as _sys
    import datetime as _dt

    _os.environ["CCTALLY_DATA_DIR"] = data_dir
    _os.environ["CCTALLY_DISABLE_DEV_AUTODETECT"] = "1"
    if bin_dir not in _sys.path:
        _sys.path.insert(0, bin_dir)
    import _cctally_core as core
    core._init_paths_from_env()
    import _lib_journal as lj
    import _cctally_journal as jrnl

    now = _dt.datetime(2026, 7, 22, 12, 0, 0, tzinfo=_dt.timezone.utc)
    for i in range(n_lines):
        rec = lj.make_obs(at="2026-07-22T00:00:00Z", src="storm",
                          provider="claude", payload={"w": worker_idx, "i": i})
        jrnl.append_record(rec, now_utc=now)


def test_concurrent_appenders_no_interleaving(journal_env):
    data_dir = journal_env
    n_workers, n_lines = 8, 50

    ctx = mp.get_context("spawn")
    procs = [
        ctx.Process(target=_append_worker,
                    args=(_BIN_DIR, str(data_dir), w, n_lines))
        for w in range(n_workers)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(90)
    for p in procs:
        assert p.exitcode == 0, f"worker exited {p.exitcode}"

    seg_path = data_dir / "journal" / "observations-2026-07.jsonl"
    raw = seg_path.read_bytes()
    assert raw.endswith(b"\n")
    parts = [line for line in raw.split(b"\n") if line]
    decoded = [J.decode_line(line) for line in parts]
    assert len(decoded) == n_workers * n_lines  # 400 lines, no torn interleaving
    assert all(d is not None for d in decoded), "an interleaved/torn line failed to decode"
    ids = {d["id"] for d in decoded}
    assert len(ids) == n_workers * n_lines  # every appended id present exactly once


# --------------------------------------------------------------------------
# (e) high-water snapshot + segment listing
# --------------------------------------------------------------------------

def test_journal_high_water_reflects_latest_segment_and_size(journal_env):
    data_dir = journal_env
    assert journal.journal_high_water() is None  # nothing yet

    r1 = J.make_obs(at="t1", src="statusline", provider="claude", payload={"i": 1})
    journal.append_record(r1, now_utc=FIXED_JULY)
    july_path = data_dir / "journal" / "observations-2026-07.jsonl"
    assert journal.journal_high_water() == (
        "observations-2026-07.jsonl", july_path.stat().st_size)

    r2 = J.make_obs(at="t2", src="statusline", provider="claude", payload={"i": 2})
    journal.append_record(r2, now_utc=FIXED_AUG)
    aug_path = data_dir / "journal" / "observations-2026-08.jsonl"
    assert journal.journal_high_water() == (
        "observations-2026-08.jsonl", aug_path.stat().st_size)


def test_list_segments_canonical_order_excludes_partial(journal_env):
    data_dir = journal_env
    jdir = data_dir / "journal"
    jdir.mkdir(parents=True, exist_ok=True)
    (jdir / "observations-2026-07.jsonl").write_bytes(b"")
    (jdir / "observations-2026-06.jsonl").write_bytes(b"")
    (jdir / "bootstrap-1700000000.jsonl").write_bytes(b"")
    (jdir / "bootstrap-1700000000.jsonl.partial").write_bytes(b"")  # excluded

    assert journal.list_segments() == [
        "bootstrap-1700000000.jsonl",
        "observations-2026-06.jsonl",
        "observations-2026-07.jsonl",
    ]
