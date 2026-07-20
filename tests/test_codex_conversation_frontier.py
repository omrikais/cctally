"""#294 S7 B3 — Codex child-discovery directory frontier (spec §5.4).

Pure kernel tests over ``bin/_lib_codex_conversation_watch.CodexChildFrontier``:
all six §5.4 bullets — walk_root-only seed, the shared per-cycle operation
budget with round-robin fairness + rotating continuation cursors + eventual
visitation, re-enumeration on own-mtime change, the ``follow_symlinks=False``
descendant boundary, the rotation watermark pinned to rotation-start, and the
``pending_candidates`` set (registered before first ingest, retried while
unclassified, size-vs-committed growth, reap on classification, bounded
expiry). The filesystem and clock are injected so no test sleeps or touches a
real tree.
"""
from __future__ import annotations

import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BIN_DIR = REPO_ROOT / "bin"
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

import _lib_codex_conversation_watch as fw  # noqa: E402


class FakeFS:
    """A synthetic directory tree with an operation counter. Every scandir /
    dir-mtime / file-size read bumps ``ops`` so tests can assert the per-cycle
    filesystem-operation ceiling directly."""

    def __init__(self):
        # dir -> {"mtime": int, "entries": [(name, is_dir, is_symlink)]}
        self.dirs: dict[str, dict] = {}
        self.files: dict[str, int] = {}   # file -> size
        self.ops = 0
        self.scandir_calls = 0

    def add_dir(self, path, *, mtime=1, entries=None):
        self.dirs[path] = {"mtime": mtime, "entries": list(entries or [])}

    def add_file(self, dirpath, name, *, size=100, is_symlink=False):
        self.dirs[dirpath]["entries"].append((name, False, is_symlink))
        self.files[dirpath.rstrip("/") + "/" + name] = size

    def add_subdir(self, parent, name, *, mtime=1, is_symlink=False):
        self.dirs[parent]["entries"].append((name, True, is_symlink))
        child = parent.rstrip("/") + "/" + name
        if not is_symlink:
            self.add_dir(child, mtime=mtime)
        return child

    def bump_mtime(self, path, mtime):
        self.dirs[path]["mtime"] = mtime

    # ── injected callables ────────────────────────────────────────────────
    def scandir(self, d):
        self.ops += 1
        self.scandir_calls += 1
        info = self.dirs.get(d)
        if info is None:
            return []
        out = []
        for (name, is_dir, is_sym) in info["entries"]:
            out.append((name, d.rstrip("/") + "/" + name, is_dir, is_sym))
        return out

    def dir_mtime(self, d):
        self.ops += 1
        info = self.dirs.get(d)
        return info["mtime"] if info else None

    def file_size(self, f):
        self.ops += 1
        return self.files.get(f)


def _frontier(fs, walk_root, **kw):
    return fw.CodexChildFrontier(
        walk_root,
        scandir_fn=fs.scandir,
        dir_mtime_fn=fs.dir_mtime,
        file_size_fn=fs.file_size,
        clock_fn=lambda: _frontier._clock[0],
        **kw,
    )


_frontier._clock = [0]


def _tick():
    _frontier._clock[0] += 1
    return _frontier._clock[0]


def _drain(front, fs, *, known=None, committed=None, cycles=40):
    """Run up to `cycles` cycles, returning the union of every ingest set and
    the max fs ops observed in any single cycle."""
    known = set(known or [])
    committed = committed or {}
    seen_ingest: set[str] = set()
    max_ops = 0
    for _ in range(cycles):
        before = fs.ops
        got = front.cycle(known_paths=known, committed_sizes=committed)
        max_ops = max(max_ops, fs.ops - before)
        seen_ingest |= got
    return seen_ingest, max_ops


# ── seed + basic discovery ─────────────────────────────────────────────────


def test_no_enumeration_at_construction():
    fs = FakeFS()
    fs.add_dir("/root")
    before = fs.ops
    _frontier(fs, "/root")
    assert fs.ops == before          # constructing walked nothing


def test_discovers_a_new_file_under_walk_root():
    fs = FakeFS()
    fs.add_dir("/root")
    fs.add_file("/root", "s.jsonl", size=200)
    front = _frontier(fs, "/root", op_budget=32)
    got, _ = _drain(front, fs, cycles=5)
    assert "/root/s.jsonl" in got


def test_ignores_already_known_file():
    fs = FakeFS()
    fs.add_dir("/root")
    fs.add_file("/root", "s.jsonl", size=200)
    front = _frontier(fs, "/root", op_budget=32)
    got, _ = _drain(front, fs, known={"/root/s.jsonl"}, cycles=5)
    assert "/root/s.jsonl" not in got   # already tracked → not re-surfaced


# ── operation budget + eventual visitation ──────────────────────────────────


def test_per_cycle_op_ceiling_on_large_tree_with_eventual_visitation():
    fs = FakeFS()
    fs.add_dir("/root")
    # A wide+deep tree: 30 nested date-style dirs, each carrying a rollout.
    parent = "/root"
    deep_file = None
    for i in range(30):
        child = fs.add_subdir(parent, f"d{i}", mtime=1)
        fs.add_file(child, f"r{i}.jsonl", size=50)
        deep_file = child.rstrip("/") + f"/r{i}.jsonl"
        parent = child
    budget = 6
    front = _frontier(fs, "/root", op_budget=budget)
    got, max_ops = _drain(front, fs, cycles=200)
    assert max_ops <= budget                 # ceiling never exceeded
    assert deep_file in got                  # eventual visitation reaches the leaf


def test_per_cycle_op_ceiling_with_large_pending_population():
    fs = FakeFS()
    fs.add_dir("/root")
    for i in range(50):
        fs.add_file("/root", f"r{i:02d}.jsonl", size=50)
    budget = 6
    front = _frontier(fs, "/root", op_budget=budget)
    # No committed cursors + never reaped → every discovered file stays pending
    # and must be retried; assert the cap holds and all are eventually surfaced.
    got, max_ops = _drain(front, fs, cycles=300)
    assert max_ops <= budget
    assert len(got) == 50


# ── re-enumeration on own-mtime change ───────────────────────────────────────


def test_nested_create_preserving_ancestor_mtimes_is_found():
    fs = FakeFS()
    fs.add_dir("/root", mtime=1)
    a = fs.add_subdir("/root", "a", mtime=1)
    b = fs.add_subdir(a, "b", mtime=1)
    front = _frontier(fs, "/root", op_budget=32)
    _drain(front, fs, cycles=5)              # frontier now knows /root, a, b
    # Create a file deep in b; ONLY b's own mtime bumps (ancestors unchanged).
    fs.add_file(b, "late.jsonl", size=70)
    fs.bump_mtime(b, 2)                       # immediate parent only
    got, _ = _drain(front, fs, cycles=10)
    assert b.rstrip("/") + "/late.jsonl" in got


def test_unchanged_dir_is_not_re_enumerated():
    fs = FakeFS()
    fs.add_dir("/root", mtime=1)
    fs.add_file("/root", "s.jsonl", size=50)
    front = _frontier(fs, "/root", op_budget=32)
    _drain(front, fs, known={"/root/s.jsonl"}, cycles=5)
    scandirs_before = fs.scandir_calls
    _drain(front, fs, known={"/root/s.jsonl"}, cycles=5)
    # An unchanged directory mtime must not re-trigger a scandir.
    assert fs.scandir_calls == scandirs_before


def test_mid_rotation_create_found_next_rotation():
    fs = FakeFS()
    fs.add_dir("/root", mtime=1)
    dirs = [fs.add_subdir("/root", f"d{i}", mtime=1) for i in range(6)]
    budget = 4                                  # rotation spans several cycles
    front = _frontier(fs, "/root", op_budget=budget)
    _drain(front, fs, cycles=3)                 # partially through a rotation
    # Add a file to an already-visited dir; only that dir's mtime bumps.
    fs.add_file(dirs[0], "mid.jsonl", size=40)
    fs.bump_mtime(dirs[0], 5)
    got, _ = _drain(front, fs, cycles=20)
    assert dirs[0].rstrip("/") + "/mid.jsonl" in got


# ── symlink boundary ─────────────────────────────────────────────────────────


def test_external_directory_symlink_is_never_traversed():
    fs = FakeFS()
    fs.add_dir("/root", mtime=1)
    # A symlink entry pointing at an external dir the fake also knows about.
    fs.add_subdir("/root", "evil", is_symlink=True)
    fs.add_dir("/root/evil", mtime=1)           # the symlink target exists
    fs.add_file("/root/evil", "secret.jsonl", size=99)
    front = _frontier(fs, "/root", op_budget=32)
    got, _ = _drain(front, fs, cycles=20)
    assert "/root/evil/secret.jsonl" not in got   # never followed
    # And the symlink target directory itself was never scandir'd.
    assert "/root/evil" not in front.known_directories()


# ── pending candidates ───────────────────────────────────────────────────────


def test_contended_first_ingest_retried_without_re_enumeration():
    fs = FakeFS()
    fs.add_dir("/root", mtime=1)
    fs.add_file("/root", "child.jsonl", size=50)
    front = _frontier(fs, "/root", op_budget=32)
    # Cycle 1 discovers + surfaces the file for ingest.
    first = front.cycle(known_paths=set(), committed_sizes={})
    assert "/root/child.jsonl" in first
    scandirs_after_discovery = fs.scandir_calls
    # Simulate a lock-contended first ingest: no committed cursor row, NOT
    # reaped. Dir mtime unchanged, so re-enumeration cannot re-find it — the
    # pending set alone must retry it.
    second = front.cycle(known_paths=set(), committed_sizes={})
    assert "/root/child.jsonl" in second
    assert fs.scandir_calls == scandirs_after_discovery


def test_incomplete_child_growth_without_dir_mtime_change_is_retried():
    fs = FakeFS()
    fs.add_dir("/root", mtime=1)
    fs.add_file("/root", "child.jsonl", size=50)
    front = _frontier(fs, "/root", op_budget=32)
    front.cycle(known_paths=set(), committed_sizes={})   # discover + first ingest
    # First ingest committed a cursor at size 50 but did NOT classify (incomplete
    # session_meta). Now the file grows to 90 with NO directory-mtime change.
    fs.files["/root/child.jsonl"] = 90
    got = front.cycle(
        known_paths={"/root/child.jsonl"}, committed_sizes={"/root/child.jsonl": 50})
    assert "/root/child.jsonl" in got            # growth beyond committed → re-ingest


def test_no_growth_beyond_committed_is_not_re_ingested():
    fs = FakeFS()
    fs.add_dir("/root", mtime=1)
    fs.add_file("/root", "child.jsonl", size=50)
    front = _frontier(fs, "/root", op_budget=32)
    front.cycle(known_paths=set(), committed_sizes={})
    got = front.cycle(
        known_paths={"/root/child.jsonl"}, committed_sizes={"/root/child.jsonl": 50})
    assert "/root/child.jsonl" not in got        # size == committed → nothing to do


def test_reap_drops_a_classified_candidate():
    fs = FakeFS()
    fs.add_dir("/root", mtime=1)
    fs.add_file("/root", "child.jsonl", size=50)
    front = _frontier(fs, "/root", op_budget=32)
    front.cycle(known_paths=set(), committed_sizes={})
    assert "/root/child.jsonl" in front.pending_candidates()
    front.reap({"/root/child.jsonl"})            # classified (child or non-child)
    assert "/root/child.jsonl" not in front.pending_candidates()
    # Reaped → classified → it now has a cursor row, so the driver passes it in
    # known_paths and the frontier never re-registers or re-surfaces it.
    got = front.cycle(
        known_paths={"/root/child.jsonl"}, committed_sizes={"/root/child.jsonl": 50})
    assert "/root/child.jsonl" not in got


def test_vanished_candidate_is_dropped():
    fs = FakeFS()
    fs.add_dir("/root", mtime=1)
    fs.add_file("/root", "child.jsonl", size=50)
    front = _frontier(fs, "/root", op_budget=32)
    front.cycle(known_paths=set(), committed_sizes={})
    del fs.files["/root/child.jsonl"]            # vanished
    front.cycle(known_paths=set(), committed_sizes={})
    assert "/root/child.jsonl" not in front.pending_candidates()


def test_bounded_expiry_drops_unclassified_candidate():
    fs = FakeFS()
    fs.add_dir("/root", mtime=1)
    fs.add_file("/root", "child.jsonl", size=50)
    front = _frontier(fs, "/root", op_budget=32, pending_expiry_cycles=3)
    front.cycle(known_paths=set(), committed_sizes={})   # discover → pending
    assert "/root/child.jsonl" in front.pending_candidates()
    for _ in range(5):                                    # unclassified, never reaped
        front.cycle(known_paths=set(), committed_sizes={})
    assert "/root/child.jsonl" not in front.pending_candidates()   # expired
